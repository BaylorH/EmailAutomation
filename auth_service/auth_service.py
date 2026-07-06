import os, re, time, threading
from functools import wraps
from flask import Flask, request, jsonify, g
from msal import PublicClientApplication, SerializableTokenCache
from firebase_helpers import upload_token

# Firebase Admin SDK — verifies frontend-issued Firebase ID tokens on the
# device-flow routes (the only identity source we trust). Import is defensive so
# the module still loads where firebase_admin isn't installed; the auth decorator
# below fails closed (401) if verification is unavailable.
try:
    import firebase_admin
    from firebase_admin import auth as firebase_auth

    if not firebase_admin._apps:
        try:
            firebase_admin.initialize_app()
        except Exception as _fb_init_err:  # pragma: no cover - env dependent
            print(f"⚠️ firebase_admin.initialize_app() deferred: {_fb_init_err}", flush=True)
except Exception as _fb_import_err:  # pragma: no cover - env dependent
    firebase_admin = None
    firebase_auth = None
    print(f"⚠️ firebase_admin unavailable: {_fb_import_err}", flush=True)

app = Flask(__name__)

CLIENT_ID = os.getenv("API_APP_ID")              # set via env
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES    = ["Mail.ReadWrite","Mail.Send"]
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")

# Path-safe identifier (Firebase uid / storage object path). Rejects path
# separators, "..", null bytes, and any other traversal / injection vector.
_UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# Generic, non-revealing client-facing error text. Internal detail is logged
# server-side only — never echoed to the client.
_GENERIC_BAD_REQUEST = "Invalid request"
_GENERIC_SERVER_ERROR = "Internal server error"

# Bound the in-memory device-flow map so an authenticated caller cannot grow it
# without limit. Entries older than the TTL are pruned; a hard cap evicts the
# oldest when the map is full.
_FLOW_TTL_SECONDS = 15 * 60
_MAX_FLOWS = 1000

cache = SerializableTokenCache()
msal_app = PublicClientApplication(
  CLIENT_ID, authority=AUTHORITY, token_cache=cache
)
# uid -> {"flow": <msal flow dict>, "ts": <epoch seconds>}
flows = {}


def _safe_uid(value):
    """Return the value iff it is a path-safe identifier string, else None."""
    if isinstance(value, str) and _UID_RE.match(value):
        return value
    return None


def _is_nonempty_str(value):
    """True for a non-empty (after strip) string; False for every other type."""
    return isinstance(value, str) and value.strip() != ""


def _require_json_object():
    """
    Safely parse the request body as a JSON object.

    Returns (data, None) on success, or (None, (response, status)) on failure.
    Fails closed with a clean 400 and a GENERIC message (never raw werkzeug /
    Python internals) when the content-type isn't JSON, the body is malformed,
    or the decoded value is not a dict (None/str/int/list/bool).
    """
    if not request.is_json:
        return None, (jsonify({"status": "failed", "error": _GENERIC_BAD_REQUEST}), 400)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({"status": "failed", "error": _GENERIC_BAD_REQUEST}), 400)
    return data, None


def verify_firebase_token(f):
    """
    Decorator: require a valid Firebase ID token on a device-flow route.

    Reads `Authorization: Bearer <token>`, verifies it with the Firebase Admin
    SDK, and stashes the verified uid on `g.firebase_uid`. Any missing / malformed
    / unverifiable token fails closed with 401. The verified uid is the ONLY
    trusted source of identity — handlers must ignore any body/query uid.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "") or ""
        if not auth_header.startswith("Bearer "):
            return jsonify({"status": "failed", "error": "Authentication required"}), 401
        token = auth_header[len("Bearer "):].strip()
        if not token:
            return jsonify({"status": "failed", "error": "Authentication required"}), 401
        if firebase_auth is None:
            print("❌ Firebase auth unavailable; rejecting authenticated request", flush=True)
            return jsonify({"status": "failed", "error": "Authentication unavailable"}), 401
        try:
            decoded = firebase_auth.verify_id_token(token)
        except Exception as e:
            print(f"⚠️ Firebase token verification failed: {type(e).__name__}", flush=True)
            return jsonify({"status": "failed", "error": "Invalid authentication token"}), 401
        uid = decoded.get("uid") if isinstance(decoded, dict) else None
        if not _is_nonempty_str(uid):
            return jsonify({"status": "failed", "error": "Invalid authentication token"}), 401
        g.firebase_uid = uid
        return f(*args, **kwargs)

    return wrapper


def _prune_flows(now=None):
    """Drop expired device flows and enforce the hard size cap (evict oldest)."""
    now = time.time() if now is None else now
    for k in [k for k, v in flows.items()
              if not isinstance(v, dict) or (now - v.get("ts", 0)) > _FLOW_TTL_SECONDS]:
        flows.pop(k, None)
    if len(flows) > _MAX_FLOWS:
        # evict oldest until back within cap
        oldest = sorted(flows, key=lambda k: flows[k].get("ts", 0))
        for k in oldest[:len(flows) - _MAX_FLOWS]:
            flows.pop(k, None)


# ---------------------------------------------------------------------------
# IDENTITY-ISOLATION (#20, CONDITIONAL-GO blocker #2).
#
# A single process-wide token cache + PublicClientApplication shared across every
# user let one user's uploaded token file carry another user's account (login-path
# mailbox confusion — mail sent AS THE WRONG USER). Fix: every device flow uses its
# OWN isolated app + cache; we upload ONLY that user's single-account cache and
# FAIL CLOSED if the cache resolves to anything other than exactly one account. The
# module-level msal_app/cache above remain as a legacy fallback for flow entries
# that predate isolation.
# ---------------------------------------------------------------------------
_flows_lock = threading.Lock()


def _new_isolated_app():
    """A fresh single-identity MSAL app + cache — never shared between users."""
    isolated_cache = SerializableTokenCache()
    isolated_app = PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=isolated_cache)
    return isolated_app, isolated_cache


@app.route("/start-device-flow", methods=["POST"])
@verify_firebase_token
def start_flow():
    # Body is validated but NOT trusted for identity; uid comes from the token (#16).
    _data, err = _require_json_object()
    if err:
        return err
    uid = _safe_uid(g.get("firebase_uid"))
    if not uid:
        return jsonify({"status": "failed", "error": _GENERIC_BAD_REQUEST}), 400
    # #20 identity isolation: a fresh per-user MSAL app + cache, never shared.
    isolated_app, isolated_cache = _new_isolated_app()
    try:
        flow = isolated_app.initiate_device_flow(scopes=SCOPES)
    except Exception as e:
        print(f"⚠️ initiate_device_flow failed: {type(e).__name__}", flush=True)
        return jsonify({"status": "failed", "error": _GENERIC_SERVER_ERROR}), 500
    if not isinstance(flow, dict) or "user_code" not in flow or "message" not in flow:
        print("⚠️ initiate_device_flow returned an unexpected shape", flush=True)
        return jsonify({"status": "failed", "error": _GENERIC_SERVER_ERROR}), 500
    _prune_flows()
    with _flows_lock:
        flows[uid] = {"flow": flow, "app": isolated_app, "cache": isolated_cache, "ts": time.time()}
    _prune_flows()
    return jsonify({
        "message": flow["message"],
        "interval": flow.get("interval"),
        "user_code": flow["user_code"],
        "verification_uri": flow.get("verification_uri")
    })


@app.route("/complete-device-flow", methods=["POST"])
@verify_firebase_token
def complete_flow():
    # Body is validated but NOT trusted for identity; uid comes from the token (#16).
    _data, err = _require_json_object()
    if err:
        return err
    uid = _safe_uid(g.get("firebase_uid"))
    if not uid:
        return jsonify({"status": "failed", "error": _GENERIC_BAD_REQUEST}), 400
    # Enforce the pending-flow TTL here too (#20 544fd72): without a prune, an
    # expired flow could still be completed if no later /start-device-flow
    # triggered cleanup.
    _prune_flows()
    with _flows_lock:
        entry = flows.get(uid)
    if not isinstance(entry, dict) or not isinstance(entry.get("flow"), dict):
        # No active flow for this identity — fail closed, do NOT hand None to MSAL.
        return jsonify({"status": "failed", "error": "No active device flow"}), 400
    flow = entry["flow"]
    # #20 isolation: prefer the per-user isolated app + cache; fall back to the shared
    # module app/cache only for legacy flow entries that predate isolation.
    app_ = entry.get("app") or msal_app
    cache_ = entry.get("cache") or cache
    isolated = entry.get("app") is not None
    try:
        result = app_.acquire_token_by_device_flow(flow)
    except Exception as e:
        print(f"⚠️ acquire_token_by_device_flow failed: {type(e).__name__}", flush=True)
        return jsonify({"status": "failed", "error": _GENERIC_SERVER_ERROR}), 500
    if not isinstance(result, dict):
        return jsonify({"status": "failed", "error": _GENERIC_SERVER_ERROR}), 500

    if "access_token" in result:
        # #20 fail-closed: a per-user isolated cache must hold EXACTLY one identity.
        # If it resolved to zero or multiple accounts, refuse to persist rather than
        # risk uploading a cross-identity token file. Enforced only on the isolated
        # path (the shared legacy app legitimately accumulates multiple accounts).
        if isolated:
            accounts = app_.get_accounts()
            if len(accounts) != 1:
                with _flows_lock:
                    flows.pop(uid, None)
                return jsonify({
                    "status": "failed",
                    "error": f"identity_isolation_violation: expected 1 account, got {len(accounts)}",
                }), 409
        # The token is bound to the AUTHENTICATED uid, never a body-supplied one.
        upload_token(
            FIREBASE_API_KEY,
            input_file=None,
            cache_content=cache_.serialize(),
            user_id=uid
        )
        with _flows_lock:
            flows.pop(uid, None)
        return jsonify({"status": "ok"})

    if "error_description" in result and "admin" in result["error_description"].lower():
        admin_url = (
            f"https://login.microsoftonline.com/common/adminconsent?"
            f"client_id={CLIENT_ID}"
            f"&redirect_uri=https%3A%2F%2Fyourapp.com%2Foauth-callback"
        )
        return jsonify({"status": "admin_needed", "url": admin_url}), 403
    return jsonify({"status": "failed", "error": result.get("error", "authorization_failed")}), 400


if __name__ == "__main__":
    app.run(port=5001)
