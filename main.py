import atexit
import json
from datetime import datetime
from msal import ConfidentialClientApplication, SerializableTokenCache
from firebase_helpers import download_token, upload_token
from email_automation.clients import list_user_ids, decode_token_payload, _fs
from email_automation.email import send_outboxes
from email_automation.processing import (
    reconcile_stale_processing_failures,
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

    # Process outbound emails (now with indexing)
    send_outboxes(user_id, headers, headers_provider=get_graph_headers)

    graph_operation_states = []
    
    # Scan for client replies (inbox - catch all replies, not just unread)
    print(f"\n🔍 Scanning inbox for client replies...")
    graph_operation_states.append(
        scan_inbox_against_index(user_id, get_graph_headers(), only_unread=False, top=50)
    )
    
    # Scan for Jill's manual replies (SentItems - catch manual replies we didn't index)
    print(f"\n📤 Scanning SentItems for manual replies...")
    graph_operation_states.append(
        scan_sent_items_for_manual_replies(user_id, get_graph_headers(), top=50)
    )

    # Retry any pending responses that failed to send previously
    process_pending_responses(user_id, get_graph_headers())

    # Check and send follow-up emails for threads without responses
    check_and_send_followups(user_id, get_graph_headers())

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


if __name__ == "__main__":
    run_with_scheduler_lease(run_all_users)
