import os, json, threading, time
from flask import Flask, request, jsonify
from msal import PublicClientApplication, SerializableTokenCache
from firebase_helpers import upload_token

app = Flask(__name__)

CLIENT_ID = os.getenv("API_APP_ID")              # set via env
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES    = ["Mail.ReadWrite","Mail.Send"]
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")

# ---------------------------------------------------------------------------
# IDENTITY-ISOLATION FIX (Major, CONDITIONAL-GO blocker #2).
#
# The previous design shared ONE process-wide SerializableTokenCache + one
# PublicClientApplication across every user. Each /complete-device-flow ran
# `acquire_token_by_device_flow` against that shared cache and then uploaded
# `cache.serialize()` (the ENTIRE accumulated cache) under the current uid. So
# after user A authenticated, user B's uploaded token file also carried A's
# account/tokens — and any later `get_accounts()[0]` could resolve to the wrong
# identity and send mail AS THE WRONG USER (login-path mailbox confusion).
#
# Fix: every device-flow uses its OWN cache + app. We serialize and upload ONLY
# that user's single-account cache, and FAIL CLOSED if the cache resolves to
# anything other than exactly one account (never persist a mixed-identity cache).
# ---------------------------------------------------------------------------

_PENDING_TTL_SECONDS = 15 * 60
_flows_lock = threading.Lock()
# uid -> {"flow": ..., "app": PublicClientApplication, "cache": SerializableTokenCache, "created": ts}
flows = {}


def _new_isolated_app():
    """A fresh single-identity MSAL app + cache — never shared between users."""
    cache = SerializableTokenCache()
    app_ = PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)
    return app_, cache


def _prune_expired_flows(now=None):
    now = now if now is not None else time.time()
    with _flows_lock:
        stale = [u for u, e in flows.items() if now - e.get("created", 0) > _PENDING_TTL_SECONDS]
        for u in stale:
            flows.pop(u, None)


@app.route("/start-device-flow", methods=["POST"])
def start_flow():
    uid = request.json["uid"]
    _prune_expired_flows()
    app_, cache = _new_isolated_app()
    flow = app_.initiate_device_flow(scopes=SCOPES)
    with _flows_lock:
        flows[uid] = {"flow": flow, "app": app_, "cache": cache, "created": time.time()}
    return jsonify({
        "message": flow["message"],
        "interval": flow["interval"],
        "user_code": flow["user_code"],
        "verification_uri": flow["verification_uri"]
    })


@app.route("/complete-device-flow", methods=["POST"])
def complete_flow():
    uid = request.json["uid"]
    with _flows_lock:
        entry = flows.get(uid)
    if not entry:
        return jsonify({"status": "failed", "error": "no_pending_flow"}), 400

    app_ = entry["app"]
    cache = entry["cache"]
    flow = entry["flow"]
    result = app_.acquire_token_by_device_flow(flow)

    if "access_token" in result:
        # Fail closed: this per-user cache must hold exactly one identity. If it
        # somehow resolved to zero or multiple accounts, refuse to persist rather
        # than risk uploading a cross-identity token file.
        accounts = app_.get_accounts()
        if len(accounts) != 1:
            with _flows_lock:
                flows.pop(uid, None)
            return jsonify({
                "status": "failed",
                "error": f"identity_isolation_violation: expected 1 account, got {len(accounts)}",
            }), 409

        upload_token(
            FIREBASE_API_KEY,
            input_file=None,
            cache_content=cache.serialize(),
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
    return jsonify({"status": "failed", "error": result}), 400


if __name__ == "__main__":
    app.run(port=5001)
