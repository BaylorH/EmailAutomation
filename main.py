import atexit
import json
import os
import signal
import sys
import traceback
from datetime import datetime
from msal import ConfidentialClientApplication, SerializableTokenCache
from firebase_helpers import download_token, upload_token
from email_automation.clients import list_user_ids, decode_token_payload, _fs
from email_automation.email import send_outboxes
from email_automation.processing import (
    _graph_operation_error_state,
    reconcile_stale_processing_failures,
    retry_processing_failures,
    scan_inbox_against_index,
    scan_sent_items_for_manual_replies,
)
from email_automation.followup import check_and_send_followups
from email_automation.pending_responses import process_pending_responses
from email_automation.app_config import CLIENT_ID, CLIENT_SECRET, AUTHORITY, SCOPES, TOKEN_CACHE, FIREBASE_API_KEY
from email_automation.scheduler_lease import run_with_scheduler_lease
from email_automation.scheduler_scope import SchedulerScopeError, resolve_scheduler_user_ids
from email_automation.system_health import record_user_health

# Thresholds for auto-cleanup (to stay within Firebase free tier)
PROCESSED_MESSAGES_THRESHOLD = 500
SHEET_CHANGELOG_THRESHOLD = 100
GRAPH_TOKEN_REFRESH_BUFFER_SECONDS = 15 * 60
PROCESSING_FAILURE_RETRY_DEFAULT_MAX_AGE_HOURS = 6


def _processing_failure_retry_enabled() -> bool:
    value = os.getenv("SITESIFT_ENABLE_PROCESSING_FAILURE_RETRY", "").strip().lower()
    if value in {"0", "false", "no", "off"}:
        return False
    return True


def _processing_failure_retry_max_age_hours() -> float:
    value = os.getenv("SITESIFT_PROCESSING_FAILURE_RETRY_MAX_AGE_HOURS", "")
    if not value.strip():
        return PROCESSING_FAILURE_RETRY_DEFAULT_MAX_AGE_HOURS
    try:
        hours = float(value)
    except ValueError:
        return PROCESSING_FAILURE_RETRY_DEFAULT_MAX_AGE_HOURS
    return max(0.0, hours)


def _headers_from_access_token(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }


def _expires_in_seconds(token_result) -> int:
    try:
        return int((token_result or {}).get("expires_in") or 0)
    except (TypeError, ValueError):
        return 0


def _timestamp_sort_value(doc, fields):
    data = doc.to_dict() or {}
    for field in fields:
        value = data.get(field)
        if value is None:
            continue
        if hasattr(value, "timestamp"):
            return value.timestamp()
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return 0
        return 0
    return 0


def _delete_oldest_excess_docs(collection_ref, threshold: int, timestamp_fields) -> int:
    docs = list(collection_ref.stream())
    excess_count = max(0, len(docs) - threshold)
    if excess_count <= 0:
        return 0

    docs.sort(key=lambda doc: _timestamp_sort_value(doc, timestamp_fields))
    for doc in docs[:excess_count]:
        doc.reference.delete()
    return excess_count


def auto_cleanup_firestore(user_id: str):
    """
    Automatically clean up Firestore collections if they exceed thresholds.
    This helps stay within Firebase free tier limits.
    """
    try:
        # Check processedMessages count
        pm_ref = _fs.collection("users").document(user_id).collection("processedMessages")
        pm_docs = list(pm_ref.limit(PROCESSED_MESSAGES_THRESHOLD + 1).stream())

        if len(pm_docs) > PROCESSED_MESSAGES_THRESHOLD:
            print(f"🧹 Auto-cleanup: processedMessages ({len(pm_docs)}+) exceeds threshold ({PROCESSED_MESSAGES_THRESHOLD})")
            deleted = _delete_oldest_excess_docs(
                pm_ref,
                PROCESSED_MESSAGES_THRESHOLD,
                ["processedAt", "timestamp", "createdAt"],
            )
            print(f"   ✅ Deleted {deleted} oldest processedMessages docs")

        # Check sheetChangeLog count
        cl_ref = _fs.collection("users").document(user_id).collection("sheetChangeLog")
        cl_docs = list(cl_ref.limit(SHEET_CHANGELOG_THRESHOLD + 1).stream())

        if len(cl_docs) > SHEET_CHANGELOG_THRESHOLD:
            print(f"🧹 Auto-cleanup: sheetChangeLog ({len(cl_docs)}+) exceeds threshold ({SHEET_CHANGELOG_THRESHOLD})")
            deleted = _delete_oldest_excess_docs(
                cl_ref,
                SHEET_CHANGELOG_THRESHOLD,
                ["timestamp", "createdAt", "updatedAt"],
            )
            print(f"   ✅ Deleted {deleted} oldest sheetChangeLog docs")

    except Exception as e:
        print(f"⚠️ Auto-cleanup error for {user_id}: {e}")


SEND_HEALTH_ESCALATION_ENV = "SITESIFT_SEND_HEALTH_ESCALATION"


def _send_health_escalation_enabled() -> bool:
    """Fail-closed default: SEND-path failures must reach graph health.

    Rail 5 ("Health cannot lie") gap: the send drivers historically returned
    None, so a broken Graph send left graph_state healthy while receive scans
    succeeded — a silent outreach outage showed green. This rail makes the send
    path contribute a graph operation state so `_overall_status` can escalate.

    The rail is ON by default. Absence of the env var keeps it ON — the SAFE,
    fail-closed behavior. Set SITESIFT_SEND_HEALTH_ESCALATION=0/false/no/off
    ONLY as an explicit rollback escape hatch to restore the legacy behavior
    (send outages invisible to graph health, exceptions propagating).
    """
    value = os.getenv(SEND_HEALTH_ESCALATION_ENV, "").strip().lower()
    if value in {"0", "false", "no", "off"}:
        return False
    return True


def _coerce_graph_operation_state(operation, result):
    """Coerce a send-path driver's return value into a graph operation state.

    Send drivers historically return None or an int count; only an explicit
    dict carrying a ``status`` is treated as a health signal. Anything else
    contributes nothing, so the healthy path never raises a false alarm.
    """
    if isinstance(result, dict) and result.get("status"):
        state = dict(result)
        state.setdefault("operation", operation)
        return state
    return None


def _run_graph_send_operation(operation, func, *args, **kwargs):
    """Run a SEND-path driver, surfacing its outcome as a graph operation state.

    Returns ``(result, state)`` where ``state`` is either a graph operation
    state dict or ``None`` (nothing to contribute).

    Fail-closed: with the rail enabled (default) an exception is converted to an
    error state instead of aborting the whole health record, and is NOT
    re-raised — so receive-scan detail collected in the same run is preserved
    while graph_state still escalates to error. With the rail disabled the
    legacy behavior is restored exactly (return passed through, exceptions
    propagate to the caller's outer handler).
    """
    if not _send_health_escalation_enabled():
        return func(*args, **kwargs), None
    try:
        result = func(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 - deliberately broad: any send failure is a health signal
        # Capture the traceback so a genuine code bug (not just a Graph HTTP
        # error) stays diagnosable — fail-closed must not also erase the stack.
        print(f"❌ Graph send operation '{operation}' failed: {e}\n{traceback.format_exc()}")
        return None, _graph_operation_error_state(operation, e)
    return result, _coerce_graph_operation_state(operation, result)


def _combine_graph_operation_states(operation_states):
    states = [
        state for state in operation_states
        if isinstance(state, dict) and state.get("status")
    ]
    failed_states = [state for state in states if state.get("status") == "error"]
    unknown_states = [state for state in states if state.get("status") == "unknown"]

    if failed_states:
        return {
            "status": "error",
            "failedOperations": failed_states,
            "operations": states,
        }
    if unknown_states:
        return {
            "status": "unknown",
            "operations": states,
        }
    return {
        "status": "healthy",
        "operations": states,
    }


def refresh_and_process_user(user_id: str):
    print(f"\n🔄 Processing user: {user_id}")

    download_token(FIREBASE_API_KEY, output_file=TOKEN_CACHE, user_id=user_id)

    cache = SerializableTokenCache()
    with open(TOKEN_CACHE, "r") as f:
        cache.deserialize(f.read())

    def _save_cache():
        if cache.has_state_changed:
            with open(TOKEN_CACHE, "w") as f:
                f.write(cache.serialize())
            upload_token(FIREBASE_API_KEY, input_file=TOKEN_CACHE, user_id=user_id)
            print(f"✅ Token cache uploaded for {user_id}")

    atexit.unregister(_save_cache)
    atexit.register(_save_cache)

    app = ConfidentialClientApplication(
        CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=AUTHORITY,
        token_cache=cache
    )

    accounts = app.get_accounts()
    if not accounts:
        print(f"⚠️ No account found for {user_id}")
        record_user_health(
            user_id,
            token_state={"status": "error", "error": "no_account_found"},
            graph_state={"status": "unknown"},
        )
        return

    account = accounts[0]
    latest_token_state = {"status": "unknown"}

    def get_graph_headers(min_expires_in: int = GRAPH_TOKEN_REFRESH_BUFFER_SECONDS):
        nonlocal latest_token_state

        # Prefer cached access tokens when they have enough runway, but refresh before
        # long Graph operations so throttled outbox batches do not expire mid-send.
        before_state = cache.has_state_changed
        result = app.acquire_token_silent(SCOPES, account=account)
        after_state = cache.has_state_changed
        token_source = "refreshed_via_refresh_token" if (not before_state and after_state) else "cached_access_token"

        if result and "access_token" in result and _expires_in_seconds(result) < min_expires_in:
            print(
                f"🔄 Token expires in≈{_expires_in_seconds(result)}s; "
                f"refreshing before Graph operation"
            )
            forced_result = app.acquire_token_silent(
                SCOPES,
                account=account,
                force_refresh=True,
            )
            if forced_result and "access_token" in forced_result:
                result = forced_result
                token_source = "refreshed_before_graph_operation"
            else:
                print("⚠️ Forced Graph token refresh failed; using existing token if available")

        if not result or "access_token" not in result:
            raise RuntimeError("silent_auth_failed")

        access_token = result["access_token"]
        exp_secs = result.get("expires_in")
        latest_token_state = {
            "status": "healthy",
            "source": token_source,
            "expiresIn": exp_secs,
        }

        print(f"🎯 Using {token_source}; expires_in≈{exp_secs}s – preview: {access_token[:40]}")

        # (Optional) sanity check on JWT-shaped token & appid
        if access_token.count(".") == 2:
            decoded = decode_token_payload(access_token)
            appid = decoded.get("appid", "unknown")
            if not appid.startswith("54cec"):
                print(f"⚠️ Unexpected appid: {appid}")
            else:
                print("✅ Token appid matches expected prefix")

        return _headers_from_access_token(access_token)

    try:
        headers = get_graph_headers()
    except RuntimeError as e:
        print(f"❌ Silent auth failed for {user_id}: {e}")
        record_user_health(
            user_id,
            token_state={"status": "error", "error": str(e)},
            graph_state={"status": "unknown"},
        )
        return

    graph_operation_states = []

    # Process outbound emails (now with indexing). Rail 5: the send path feeds
    # graph health so a broken Graph send can no longer read as healthy.
    _, send_state = _run_graph_send_operation(
        "outbox_send",
        send_outboxes,
        user_id,
        headers,
        headers_provider=get_graph_headers,
    )
    if send_state is not None:
        graph_operation_states.append(send_state)

    # Scan for client replies (inbox - catch all replies, not just unread)
    print("\n🔍 Scanning inbox for client replies...")
    graph_operation_states.append(
        scan_inbox_against_index(user_id, get_graph_headers(), only_unread=False, top=50)
    )

    # Scan for Jill's manual replies (SentItems - catch manual replies we didn't index)
    print(f"\n📤 Scanning SentItems for manual replies...")
    graph_operation_states.append(
        scan_sent_items_for_manual_replies(user_id, get_graph_headers(), top=50)
    )

    if _processing_failure_retry_enabled():
        retry_processing_failures(
            user_id,
            get_graph_headers(),
            max_failure_age_hours=_processing_failure_retry_max_age_hours(),
        )
    else:
        print("ℹ️ Stored processing failure replay disabled; failures remain visible for review")

    # Retry any pending responses that failed to send previously (send path).
    _, pending_state = _run_graph_send_operation(
        "pending_responses_send",
        process_pending_responses,
        user_id,
        get_graph_headers(),
    )
    if pending_state is not None:
        graph_operation_states.append(pending_state)

    # Check and send follow-up emails for threads without responses (send path).
    _, followup_state = _run_graph_send_operation(
        "followup_send",
        check_and_send_followups,
        user_id,
        get_graph_headers(),
    )
    if followup_state is not None:
        graph_operation_states.append(followup_state)

    # Auto-cleanup Firestore if collections are getting large (stay within free tier)
    auto_cleanup_firestore(user_id)

    # Keep dashboard health from staying red after a retry eventually succeeds.
    reconcile_stale_processing_failures(user_id)

    record_user_health(
        user_id,
        token_state=latest_token_state,
        graph_state=_combine_graph_operation_states(graph_operation_states),
    )


def run_all_users():
    all_users = list_user_ids()
    print(f"📦 Found {len(all_users)} token cache users: {all_users}")

    try:
        scope = resolve_scheduler_user_ids(all_users)
    except SchedulerScopeError as e:
        raise SystemExit(f"🚫 Scheduler scope blocked: {e}") from e

    print(f"🛡️ Scheduler scope: {scope.mode}; processing users: {scope.user_ids}")

    for uid in scope.user_ids:
        try:
            refresh_and_process_user(uid)
        except Exception as e:
            print(f"💥 Error for user {uid}:", str(e))
            record_user_health(
                uid,
                token_state={"status": "unknown"},
                graph_state={"status": "error", "error": str(e)},
            )


EXPECTED_AZURE_APP_ID_PREFIX = "54cec"


def _validate_startup_env():
    """Hard pre-run gate, parity with the legacy GHA 'Validate CLIENT_ID
    prefix' step (.github/workflows/email.yml): refuse to start when
    AZURE_API_APP_ID is missing or does not carry the expected app prefix
    (wrong tenant / wrong app registration). Runs BEFORE lease acquisition so
    a misconfigured runtime can never touch Firestore or any user.

    The in-run appid check at get_graph_headers is a soft warning only; this
    is the fail-closed version. Skipped under E2E_TEST_MODE (mock env), same
    as app_config's import-time missing-env validation.
    """
    if os.getenv("E2E_TEST_MODE") == "true":
        print("ℹ️ E2E_TEST_MODE: skipping AZURE_API_APP_ID startup gate")
        return

    app_id = os.getenv("AZURE_API_APP_ID", "")
    if not app_id.startswith(EXPECTED_AZURE_APP_ID_PREFIX):
        problem = "missing" if not app_id else f"unexpected ('{app_id[:8]}…')"
        raise SystemExit(
            f"🚫 Startup gate: AZURE_API_APP_ID is {problem}; expected prefix "
            f"'{EXPECTED_AZURE_APP_ID_PREFIX}'. Refusing to run before lease "
            f"acquisition."
        )
    print("✅ Startup gate: AZURE_API_APP_ID prefix OK")


def _install_sigterm_atexit_bridge() -> None:
    """Make atexit handlers (e.g. the token-cache upload registered in
    refresh_and_process_user) survive container shutdown.

    Under GitHub Actions the process ends naturally, so atexit fires. Under a
    Cloud Run Job the platform sends SIGTERM before the container is stopped;
    Python's default SIGTERM disposition terminates the process WITHOUT running
    atexit handlers, which would drop a pending token-cache upload. Translating
    SIGTERM into ``sys.exit`` raises SystemExit, which unwinds normally and lets
    atexit-registered handlers run before exit.

    The exit status is non-zero (143 = 128 + SIGTERM), NOT 0: a Cloud Run task
    is marked succeeded only when the container exits 0, so exiting 0 on a
    timeout/cancel would mask an interrupted run (possibly stopped mid-send or
    mid-write) as a success — and release the lease as if the work had
    completed, letting the next execution repeat partial work. A non-zero exit
    marks the interrupted run failed (triggering retry/alerting) while still
    unwinding through atexit and run_with_scheduler_lease's ``finally`` so the
    token-cache upload runs and the lease is released.
    """
    def _handle_sigterm(signum, frame) -> None:  # noqa: ARG001 (signal API)
        print(
            "🛑 Received SIGTERM; exiting 143 (non-zero) so atexit handlers run "
            "and the interrupted run is marked failed, not silently succeeded"
        )
        sys.exit(128 + signal.SIGTERM)

    signal.signal(signal.SIGTERM, _handle_sigterm)


if __name__ == "__main__":
    _validate_startup_env()
    _install_sigterm_atexit_bridge()
    run_with_scheduler_lease(run_all_users)
