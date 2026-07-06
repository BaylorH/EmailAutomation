import os
import sys
import subprocess
import json
import re
from functools import wraps
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session, g
from flask_cors import CORS
from msal import ConfidentialClientApplication, SerializableTokenCache
from firebase_helpers import upload_token
from email_automation.app_config import (
    cors_origins as _cors_origins,
    destructive_admin_routes_enabled as _destructive_admin_routes_enabled,
    FRONTEND_EMAIL_ACCESS_URL,
    legacy_flask_oauth_enabled as _legacy_flask_oauth_enabled,
    legacy_flask_oauth_redirect_uri as _legacy_flask_oauth_redirect_uri,
)
import threading
import time

# Firebase Admin SDK — used to verify frontend-issued Firebase ID tokens on the
# mutating / send-capable /api/* routes. Import is defensive so the module still
# loads in environments where firebase_admin isn't installed; the auth decorator
# below fails closed (401) if verification is unavailable.
try:
    import firebase_admin
    from firebase_admin import auth as firebase_auth

    if not firebase_admin._apps:
        try:
            firebase_admin.initialize_app()
        except Exception as _fb_init_err:  # pragma: no cover - env dependent
            print(f"⚠️ firebase_admin.initialize_app() deferred: {_fb_init_err}")
except Exception as _fb_import_err:  # pragma: no cover - env dependent
    firebase_admin = None
    firebase_auth = None
    print(f"⚠️ firebase_admin unavailable: {_fb_import_err}")

# Constants for basic Flask app functionality (same as before)
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = ["Mail.ReadWrite", "Mail.Send"]
TOKEN_CACHE = "msal_token_cache.bin"

# Fix environment variable naming before anything else
if not os.getenv("AZURE_API_CLIENT_SECRET") and os.getenv("AZURE_CLIENT_SECRET"):
    os.environ["AZURE_API_CLIENT_SECRET"] = os.getenv("AZURE_CLIENT_SECRET")
    print(f"🔧 Fixed: Set AZURE_API_CLIENT_SECRET from AZURE_CLIENT_SECRET")

# Try to import scheduler logic - completely optional
SCHEDULER_AVAILABLE = False
try:
    # Only try to import if we have the basic required env vars
    firebase_key = os.getenv("FIREBASE_API_KEY")
    azure_app_id = os.getenv("AZURE_API_APP_ID")
    
    print(f"🔍 Environment check: FIREBASE_API_KEY={'✅' if firebase_key else '❌'}, AZURE_API_APP_ID={'✅' if azure_app_id else '❌'}")
    
    if firebase_key and azure_app_id:
        print("🚀 Attempting to import scheduler modules...")
        
        # Set up Google credentials if we have the service account JSON
        firebase_sa_key = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
        if firebase_sa_key:
            import tempfile
            import json
            # Create temporary service account file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
                if isinstance(firebase_sa_key, str):
                    # If it's a string, assume it's JSON
                    f.write(firebase_sa_key)
                else:
                    json.dump(firebase_sa_key, f)
                temp_sa_path = f.name
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = temp_sa_path
            print(f"🔧 Set GOOGLE_APPLICATION_CREDENTIALS to temporary file")
        else:
            print("⚠️ No FIREBASE_SERVICE_ACCOUNT_JSON found - Firestore may not work")
        
        print("🔍 Importing email_automation.clients...")
        from email_automation.clients import list_user_ids, decode_token_payload
        print("✅ Successfully imported clients")
        
        print("🔍 Importing email_automation.email...")
        from email_automation.email import send_outboxes
        print("✅ Successfully imported email")
        
        print("🔍 Importing email_automation.processing...")
        from email_automation.processing import scan_inbox_against_index
        print("✅ Successfully imported processing")

        # Import the SINGLE source of truth for processing logic
        print("🔍 Importing refresh_and_process_user from main...")
        from main import refresh_and_process_user
        print("✅ Successfully imported refresh_and_process_user from main.py")

        SCHEDULER_AVAILABLE = True
        print("✅ Scheduler functionality available")
    else:
        print("⚠️ Scheduler functionality disabled - missing environment variables")
        print(f"   FIREBASE_API_KEY: {'present' if firebase_key else 'MISSING'}")
        print(f"   AZURE_API_APP_ID: {'present' if azure_app_id else 'MISSING'}")
except (ImportError, RuntimeError) as e:
    import_error_message = f"{type(e).__name__}: {str(e)}"
    print(f"⚠️ Scheduler functionality not available: {e}")
    print(f"⚠️ Import error details: {import_error_message}")
    
    # Store error for debugging
    globals()['IMPORT_ERROR'] = import_error_message

# Define dummy functions if scheduler not available
if not SCHEDULER_AVAILABLE:
    def list_user_ids():
        return []

    def decode_token_payload(token):
        return {}

    def send_outboxes(user_id, headers):
        return {"success": False, "error": "Scheduler not available"}

    def refresh_and_process_user(user_id):
        return {"success": False, "error": "Scheduler not available"}
    

app = Flask(__name__)

# Explicit origins only; production must not allow wildcard CORS.
CORS(app, origins=_cors_origins())

app.secret_key = os.getenv("SECRET_KEY", "some-default-secret")

app.config.update(
    SESSION_COOKIE_SECURE=True,        # ensures cookie is sent over HTTPS
    SESSION_COOKIE_SAMESITE='Lax',     # allows session to persist during OAuth redirect
)

CLIENT_ID        = os.getenv("AZURE_API_APP_ID")
CLIENT_SECRET = os.getenv("AZURE_API_CLIENT_SECRET") or os.getenv("AZURE_CLIENT_SECRET")  # Support both names
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")
AUTHORITY        = "https://login.microsoftonline.com/common"
SCOPES           = ["Mail.ReadWrite", "Mail.Send"]
CACHE_FILE       = "msal_token_cache.bin"

# ---------------------------------------------------------------------------
# Request-hardening helpers (shared by every /api/* POST handler)
# ---------------------------------------------------------------------------

# Strict charset for any identifier that is interpolated into a filesystem path
# or a Firebase storage object path. Rejects path separators, "..", null bytes,
# and any other traversal / injection vector.
_UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# Generic, non-revealing client-facing error text. Internal detail is logged
# server-side only — never echoed to the client.
_GENERIC_BAD_REQUEST = "Invalid request"
_GENERIC_SERVER_ERROR = "Internal server error"


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
        return None, (jsonify({"success": False, "error": _GENERIC_BAD_REQUEST}), 400)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({"success": False, "error": _GENERIC_BAD_REQUEST}), 400)
    return data, None


def verify_firebase_token(f=None, *, check_revoked=False):
    """
    Decorator: require a valid Firebase ID token on a mutating/send-capable route.

    Reads `Authorization: Bearer <token>`, verifies it with the Firebase Admin
    SDK, and stashes the verified uid on `g.firebase_uid`. Any missing / malformed
    / unverifiable token fails closed with 401. The verified uid is the ONLY
    trusted source of identity — handlers must ignore body/session uid.

    Usable bare (``@verify_firebase_token``) or parameterized
    (``@verify_firebase_token(check_revoked=True)``). When ``check_revoked`` is
    True the token is additionally checked against Firebase for revocation /
    disabled-user state — reserved for destructive routes where a stale-but-
    unexpired token from a revoked operator must not act. It costs one extra
    Admin round-trip, so the default stays False for high-volume routes.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            auth_header = request.headers.get("Authorization", "") or ""
            if not auth_header.startswith("Bearer "):
                return jsonify({"success": False, "error": "Authentication required"}), 401
            token = auth_header[len("Bearer "):].strip()
            if not token:
                return jsonify({"success": False, "error": "Authentication required"}), 401
            if firebase_auth is None:
                print("❌ Firebase auth unavailable; rejecting authenticated request", flush=True)
                return jsonify({"success": False, "error": "Authentication unavailable"}), 401
            try:
                decoded = firebase_auth.verify_id_token(token, check_revoked=check_revoked)
            except Exception as e:
                print(f"⚠️ Firebase token verification failed: {type(e).__name__}", flush=True)
                return jsonify({"success": False, "error": "Invalid authentication token"}), 401
            uid = decoded.get("uid") if isinstance(decoded, dict) else None
            if not _is_nonempty_str(uid):
                return jsonify({"success": False, "error": "Invalid authentication token"}), 401
            g.firebase_uid = uid
            return func(*args, **kwargs)

        return wrapper

    # Bare usage: @verify_firebase_token
    if f is not None:
        return decorator(f)
    # Parameterized usage: @verify_firebase_token(check_revoked=True)
    return decorator


# Get the base URL for redirect URI
def get_base_url():
    return FRONTEND_EMAIL_ACCESS_URL

def check_token_status():
    """Check if we have a valid token"""
    uid = session.get("uid", "web_user") 
    user_dir = f"msal_caches/{uid}" 
    cache_file = f"{user_dir}/msal_token_cache.bin" 
    os.makedirs(user_dir, exist_ok=True)
    if not os.path.exists(cache_file):
        return {"status": "no_cache", "message": "No token cache found"}
    
    try:
        cache = SerializableTokenCache()
        cache.deserialize(open(cache_file).read())
        
        app_obj = ConfidentialClientApplication(
            CLIENT_ID,
            client_credential=CLIENT_SECRET,
            authority=AUTHORITY,
            token_cache=cache
        )
        
        accounts = app_obj.get_accounts()
        if not accounts:
            return {"status": "no_accounts", "message": "No accounts in cache"}
        
        # Try silent token acquisition
        result = app_obj.acquire_token_silent(SCOPES, account=accounts[0])
        
        if result and "access_token" in result:
            return {
                "status": "valid",
                "message": "Token is valid",
                "account": accounts[0].get("username", "Unknown"),
                "expires": result.get("expires_in", "Unknown")
            }
        else:
            return {
                "status": "expired",
                "message": "Token exists but expired or invalid",
                "error": result.get("error_description", "Unknown error") if result else "No result"
            }
    except Exception as e:
        return {"status": "error", "message": f"Error checking token: {str(e)}"}

def auto_upload_token():
    """Automatically upload token to Firebase if valid"""
    try:
        uid = session.get("uid", "web_user") 
        user_dir = f"msal_caches/{uid}" 
        cache_file = f"{user_dir}/msal_token_cache.bin" 
        
        if os.path.exists(cache_file):
            upload_token(FIREBASE_API_KEY, input_file=cache_file, user_id=uid)
            return {"success": True, "message": "Token automatically uploaded to Firebase"}
    except Exception as e:
        return {"success": False, "error": str(e)}
    return {"success": False, "error": "No token file found"}

def run_scheduler():
    """Run the full scheduler for all users - same logic as main.py"""
    if not SCHEDULER_AVAILABLE:
        return {"success": False, "error": "Scheduler functionality not available - missing dependencies"}

    try:
        all_users = list_user_ids()
        print(f"📦 Found {len(all_users)} token cache users: {all_users}", flush=True)
        
        results = []
        for uid in all_users:
            result = refresh_and_process_user(uid)
            results.append({"user_id": uid, "result": result})
        
        return {
            "success": True, 
            "message": f"Scheduler completed for {len(all_users)} users",
            "results": results
        }
    except Exception as e:
        error_msg = f"Scheduler failed: {str(e)}"
        print(f"💥 {error_msg}", flush=True)
        return {"success": False, "error": error_msg}

@app.route("/")
def index():
    # Sanitise the caller-supplied uid before it ever reaches the session (and,
    # from there, filesystem / Firebase object paths). Anything that isn't a
    # strict path-safe identifier falls back to the default rather than being
    # trusted verbatim (previously a `?uid=../../..` set the session uid raw).
    raw_uid = request.args.get("uid", "web_user")
    uid = _safe_uid(raw_uid) or "web_user"
    session["uid"] = uid
    print(f"[INDEX] Setting UID in session: {uid}")
    status = check_token_status()
    base_url = get_base_url()
    
    # Auto-upload if token is valid
    upload_result = None
    if status["status"] == "valid":
        upload_result = auto_upload_token()
    
    return render_template_string("""
    <!DOCTYPE html>
    <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Email Access Setup - PropertyFlow</title>
            <link rel="preconnect" href="https://fonts.googleapis.com">
            <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
            <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
            <style>
                * {
                    margin: 0;
                    padding: 0;
                    box-sizing: border-box;
                }

                body {
                    font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    background: linear-gradient(145deg, #0f172a 0%, #1e293b 50%, #334155 100%);
                    min-height: 100vh;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 20px;
                }

                .container {
                    background: white;
                    border-radius: 16px;
                    box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
                    padding: 48px;
                    max-width: 440px;
                    width: 100%;
                }

                .header {
                    text-align: center;
                    margin-bottom: 32px;
                }

                .header-icon {
                    width: 56px;
                    height: 56px;
                    background: linear-gradient(135deg, #3b82f6, #1d4ed8);
                    border-radius: 14px;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    margin: 0 auto 20px;
                }

                .header-icon svg {
                    width: 28px;
                    height: 28px;
                    color: white;
                }

                .header h1 {
                    color: #0f172a;
                    font-size: 1.75rem;
                    font-weight: 700;
                    margin-bottom: 8px;
                    letter-spacing: -0.025em;
                }

                .header p {
                    color: #64748b;
                    font-size: 0.9375rem;
                    line-height: 1.5;
                }

                .status-card {
                    padding: 20px;
                    border-radius: 12px;
                    margin: 24px 0;
                }

                .status-connected {
                    background: #f0fdf4;
                    border: 1px solid #bbf7d0;
                }

                .status-connected .status-header {
                    color: #166534;
                }

                .status-connected .status-text {
                    color: #15803d;
                }

                .status-pending {
                    background: #fefce8;
                    border: 1px solid #fef08a;
                }

                .status-pending .status-header {
                    color: #854d0e;
                }

                .status-pending .status-text {
                    color: #a16207;
                }

                .status-header {
                    font-size: 1rem;
                    font-weight: 600;
                    margin-bottom: 8px;
                    display: flex;
                    align-items: center;
                    gap: 10px;
                }

                .status-header svg {
                    width: 20px;
                    height: 20px;
                    flex-shrink: 0;
                }

                .status-text {
                    font-size: 0.875rem;
                    line-height: 1.5;
                }

                .email-badge {
                    display: inline-flex;
                    align-items: center;
                    gap: 8px;
                    font-weight: 500;
                    background: #dcfce7;
                    color: #166534;
                    padding: 8px 12px;
                    border-radius: 8px;
                    margin-top: 12px;
                    font-size: 0.875rem;
                }

                .sync-status {
                    display: flex;
                    align-items: center;
                    gap: 6px;
                    margin-top: 12px;
                    font-size: 0.8125rem;
                    color: #16a34a;
                }

                .sync-status svg {
                    width: 16px;
                    height: 16px;
                }

                .btn {
                    width: 100%;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    gap: 10px;
                    padding: 14px 24px;
                    font-size: 0.9375rem;
                    font-weight: 600;
                    text-decoration: none;
                    border-radius: 10px;
                    border: none;
                    cursor: pointer;
                    transition: all 0.15s ease;
                }

                .btn-primary {
                    background: #0f172a;
                    color: white;
                }

                .btn-primary:hover {
                    background: #1e293b;
                    transform: translateY(-1px);
                    box-shadow: 0 4px 12px rgba(15, 23, 42, 0.15);
                }

                .btn-primary svg {
                    width: 20px;
                    height: 20px;
                }

                .security-badge {
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    gap: 8px;
                    margin-top: 24px;
                    padding-top: 24px;
                    border-top: 1px solid #e2e8f0;
                    color: #64748b;
                    font-size: 0.8125rem;
                }

                .security-badge svg {
                    width: 16px;
                    height: 16px;
                    color: #22c55e;
                }

                @media (max-width: 480px) {
                    .container {
                        padding: 32px 24px;
                    }

                    .header h1 {
                        font-size: 1.5rem;
                    }
                }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <div class="header-icon">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/>
                            <polyline points="22,6 12,13 2,6"/>
                        </svg>
                    </div>
                    <h1>Email Access Setup</h1>
                    <p>Connect your email account to enable automated email management</p>
                </div>

                {% if status.status == 'valid' %}
                    <div class="status-card status-connected">
                        <div class="status-header">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                                <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
                                <polyline points="22 4 12 14.01 9 11.01"/>
                            </svg>
                            Email Access Connected
                        </div>
                        <p class="status-text">Your email account is successfully connected and ready to use.</p>
                        <div class="email-badge">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width: 16px; height: 16px;">
                                <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/>
                                <polyline points="22,6 12,13 2,6"/>
                            </svg>
                            {{ status.account }}
                        </div>
                        {% if upload_result and upload_result.success %}
                        <div class="sync-status">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <polyline points="20 6 9 17 4 12"/>
                            </svg>
                            Securely synced to cloud
                        </div>
                        {% endif %}
                    </div>
                {% else %}
                    <div class="status-card status-pending">
                        <div class="status-header">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <circle cx="12" cy="12" r="10"/>
                                <polyline points="12 6 12 12 16 14"/>
                            </svg>
                            Email Access Required
                        </div>
                        <p class="status-text">To use automated email features, please connect your Microsoft email account.</p>
                    </div>

                    <button class="btn btn-primary" onclick="connectEmail()">
                        <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 32 32" fill="none">
                            <rect x="17" y="17" width="10" height="10" fill="#FEBA08"/>
                            <rect x="5" y="17" width="10" height="10" fill="#05A6F0"/>
                            <rect x="17" y="5" width="10" height="10" fill="#80BC06"/>
                            <rect x="5" y="5" width="10" height="10" fill="#F25325"/>
                        </svg>
                        Connect with Microsoft
                    </button>
                {% endif %}

                <div class="security-badge">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
                    </svg>
                    Your credentials are never stored. Only secure tokens are used.
                </div>
            </div>

            <script>
                function connectEmail() {
                    window.location.href = {{ email_access_url|tojson }};
                }
            </script>
        </body>
    </html>
    """, status=status, base_url=base_url, uid=uid, upload_result=upload_result, email_access_url=FRONTEND_EMAIL_ACCESS_URL)

@app.route("/api/status")
def api_status():
    return jsonify(check_token_status())

@app.route("/api/upload", methods=["POST"])
@verify_firebase_token
def api_upload():
    # Identity comes ONLY from the verified Firebase token, and is re-validated
    # against the strict charset before touching the filesystem / Firebase path.
    uid = _safe_uid(g.get("firebase_uid"))
    if uid is None:
        return jsonify({"success": False, "error": _GENERIC_BAD_REQUEST}), 400
    try:
        print(f"[UPLOAD] Upload requested for UID: {uid}")

        user_dir = f"msal_caches/{uid}"
        cache_file = f"{user_dir}/msal_token_cache.bin"
        os.makedirs(user_dir, exist_ok=True)
        if not os.path.exists(cache_file):
            return jsonify({"success": False, "error": "No token cache file found"}), 404

        upload_token(FIREBASE_API_KEY, input_file=cache_file, user_id=uid)
        return jsonify({"success": True, "message": "Token uploaded to Firebase"})
    except Exception as e:
        print(f"❌ Upload failed: {type(e).__name__}: {e}", flush=True)
        return jsonify({"success": False, "error": "Upload failed"}), 500

@app.route("/api/clear", methods=["POST"])
@verify_firebase_token
def api_clear():
    # Scope to the verified caller's own token cache. Trusting session["uid"]
    # (set via GET /?uid=) let an unauthenticated caller wipe another user's
    # MSAL cache; the Firebase-verified uid is the only trustworthy identity.
    uid = _safe_uid(g.get("firebase_uid"))
    if uid is None:
        return jsonify({"success": False, "error": _GENERIC_BAD_REQUEST}), 400
    try:
        user_dir = f"msal_caches/{uid}"
        cache_file = f"{user_dir}/msal_token_cache.bin"
        os.makedirs(user_dir, exist_ok=True)
        if os.path.exists(cache_file):
            os.remove(cache_file)
        return jsonify({"success": True, "message": "Token cache cleared"})
    except Exception as e:
        print(f"❌ Clear failed: {type(e).__name__}: {e}", flush=True)
        return jsonify({"success": False, "error": "Failed to clear token cache"}), 500

@app.route("/api/refresh", methods=["POST"])
@verify_firebase_token
def api_refresh():
    uid = _safe_uid(g.get("firebase_uid"))
    if uid is None:
        return jsonify({"success": False, "error": _GENERIC_BAD_REQUEST}), 400
    try:
        user_dir = f"msal_caches/{uid}"
        cache_file = f"{user_dir}/msal_token_cache.bin" 
        os.makedirs(user_dir, exist_ok=True)
        cache = SerializableTokenCache()
        if os.path.exists(cache_file):
            cache.deserialize(open(cache_file).read())
        
        app_obj = ConfidentialClientApplication(
            CLIENT_ID,
            client_credential=CLIENT_SECRET,
            authority=AUTHORITY,
            token_cache=cache
        )
        
        accounts = app_obj.get_accounts()
        if not accounts:
            # Fail-closed: an unauthenticated MSAL state is a client error, not a 200.
            return jsonify({"success": False, "error": "No accounts found"}), 401

        result = app_obj.acquire_token_silent(
            SCOPES,
            account=accounts[0],
            force_refresh=True
        )

        if result and "access_token" in result:
            with open(cache_file, "w") as f:
                f.write(cache.serialize())
            return jsonify({"success": True, "message": "Token refreshed successfully"})
        else:
            # result may be None (silent refresh not possible -> interaction
            # required) or a dict carrying an MSAL error. Either way this is a
            # clean re-auth signal, never a leaked internal AttributeError.
            return jsonify({"success": False, "error": "Re-authentication required"}), 401

    except Exception as e:
        # Upstream (MSAL / filesystem) failure. Fail closed with a generic 5xx
        # (502) — never the raw exception text, never a fail-open 200.
        print(f"❌ Token refresh failed: {type(e).__name__}: {e}", flush=True)
        return jsonify({"success": False, "error": "Token refresh failed"}), 502

# Global variable to track scheduler status
scheduler_status = {"running": False, "last_run": None, "last_result": None}
# Request-level mutex so two back-to-back triggers can't both pass the
# "already running" guard before either worker thread flips the flag (TOCTOU).
_scheduler_lock = threading.Lock()

@app.route("/api/trigger-scheduler", methods=["POST"])
@verify_firebase_token(check_revoked=True)
def api_trigger_scheduler():
    """
    API endpoint to manually trigger the email scheduler.
    This runs the same logic as the GitHub Actions workflow.

    Send-capable (real Microsoft Graph Mail.Send) -> requires a verified Firebase
    ID token (@verify_firebase_token) AND the dev-scope availability guard below.
    """
    global scheduler_status

    # Check if scheduler functionality is available (dev-scope guard).
    if not SCHEDULER_AVAILABLE:
        return jsonify({
            "success": False,
            "error": "Scheduler functionality not available - missing required environment variables or dependencies"
        }), 503

    # Atomically claim the run so concurrent/rapid triggers can't double-start.
    with _scheduler_lock:
        if scheduler_status["running"]:
            return jsonify({
                "success": False,
                "error": "Scheduler is already running",
                "status": scheduler_status
            }), 409
        scheduler_status["running"] = True
        scheduler_status["last_run"] = datetime.now().isoformat()

    def run_scheduler_async():
        """Run scheduler in background thread"""
        global scheduler_status
        import sys
        try:
            print("🚀 Manual scheduler trigger initiated", flush=True)
            result = run_scheduler()

            scheduler_status["last_result"] = result
            scheduler_status["running"] = False

            print(f"✅ Manual scheduler completed: {result}", flush=True)
            sys.stdout.flush()

        except Exception as e:
            scheduler_status["last_result"] = {"success": False, "error": str(e)}
            scheduler_status["running"] = False
            print(f"💥 Manual scheduler failed: {e}", flush=True)
            sys.stdout.flush()

    # Start scheduler in background thread. If starting the thread fails, release
    # the claimed run so the endpoint isn't wedged in a permanent "running" state.
    thread = threading.Thread(target=run_scheduler_async)
    thread.daemon = True
    try:
        thread.start()
    except Exception as e:
        scheduler_status["running"] = False
        print(f"💥 Failed to start scheduler thread: {e}", flush=True)
        return jsonify({"success": False, "error": "Failed to start scheduler"}), 500

    return jsonify({
        "success": True,
        "message": "Scheduler started successfully",
        "status": "running",
        "started_at": datetime.now().isoformat()
    })

@app.route("/api/scheduler-status", methods=["GET"])
def api_scheduler_status():
    """Get the current status of the scheduler.

    This endpoint is polled unauthenticated by the dashboard, so it must NOT
    disclose server configuration. Previously it returned which environment
    variables were set (FIREBASE/AZURE/OPENAI presence flags) and the RAW
    IMPORT_ERROR string — a recon aid. Those internals are stripped here; only
    a boolean `has_import_error` is surfaced.
    """
    global scheduler_status

    return jsonify({
        **scheduler_status,
        "scheduler_available": SCHEDULER_AVAILABLE,
        "has_import_error": bool(globals().get('IMPORT_ERROR')),
    })

@app.route("/api/decline-property", methods=["POST"])
@verify_firebase_token
def api_decline_property():
    """
    Delete a property row from a Google Sheet when user declines a new property suggestion.
    Expects JSON body: { uid, clientId, rowNumber, sheetId }

    Issues a DESTRUCTIVE deleteDimension against a Google Sheet, so identity
    comes ONLY from the verified Firebase ID token (@verify_firebase_token). The
    body-supplied uid/sheetId are untrusted: the target sheetId must be the exact
    sheet registered on the TOKEN uid's client. This closes the IDOR of deleting
    rows out of an arbitrary tenant's sheet by guessing its spreadsheet id.
    """
    data, err = _require_json_object()
    if err:
        return err
    try:
        # The verified token uid is the ONLY trusted identity; never authorize a
        # destructive sheet write off a body-supplied uid.
        token_uid = _safe_uid(g.get("firebase_uid"))
        if token_uid is None:
            return jsonify({"success": False, "error": _GENERIC_BAD_REQUEST}), 400

        uid = data.get("uid")
        client_id = data.get("clientId")
        row_number = data.get("rowNumber")
        sheet_id = data.get("sheetId")

        # uid / clientId / sheetId must be non-empty strings.
        if not all(_is_nonempty_str(v) for v in (uid, client_id, sheet_id)):
            return jsonify({"success": False, "error": "Missing required fields: uid, clientId, sheetId"}), 400

        # rowNumber must be a POSITIVE INTEGER — validated BEFORE the destructive
        # deleteDimension. bool is a subclass of int, so reject it explicitly;
        # floats, strings, and non-positive values are rejected too.
        if isinstance(row_number, bool) or not isinstance(row_number, int) or row_number < 1:
            return jsonify({"success": False, "error": "rowNumber must be a positive integer"}), 400

        # === Ownership guard ===============================================
        # Resolve the authoritative sheetId for THIS (token) user's client and
        # refuse any body sheetId that doesn't match — fail closed BEFORE any
        # sheet read/write. A body uid naming a different tenant, or a foreign
        # sheetId, never reaches the destructive delete.
        from email_automation.clients import _sheets_client, _get_client_config
        try:
            authorized_sheet_id, _, _ = _get_client_config(token_uid, client_id)
        except Exception:
            return jsonify({"success": False, "error": "Not authorized for this client"}), 403
        if not _is_nonempty_str(authorized_sheet_id) or sheet_id != authorized_sheet_id:
            return jsonify({"success": False, "error": "sheetId does not match this client"}), 403
        # ===================================================================

        # Import sheets client
        from email_automation.sheets import _first_sheet_props

        sheets = _sheets_client()
        grid_id, tab_title = _first_sheet_props(sheets, sheet_id)

        # Delete the row
        delete_request = {
            "requests": [{
                "deleteDimension": {
                    "range": {
                        "sheetId": grid_id,
                        "dimension": "ROWS",
                        "startIndex": row_number - 1,  # 0-based
                        "endIndex": row_number
                    }
                }
            }]
        }

        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body=delete_request
        ).execute()

        print(f"🗑️ Deleted row {row_number} from sheet {sheet_id} for client {client_id}")

        return jsonify({
            "success": True,
            "message": f"Row {row_number} deleted successfully",
            "deletedRow": row_number
        })

    except Exception as e:
        print(f"❌ Failed to decline property: {type(e).__name__}: {e}")
        return jsonify({"success": False, "error": "Failed to decline property"}), 500


@app.route("/api/accept-new-property", methods=["POST"])
@verify_firebase_token
def api_accept_new_property():
    """
    Accept a new property suggestion - creates the row in the sheet.
    Called when user clicks 'Accept' on a pending_approval notification.
    Expects JSON body: { uid, clientId, notificationId, propertyData }
    propertyData: { address, city, link, notes, leasingCompany, leasingContact, brokerEmail, sheetId, tabTitle }

    Writes rows into a client-owned Google Sheet and can spend OpenAI on PDF
    extraction, so identity comes ONLY from the verified Firebase ID token
    (@verify_firebase_token). The body-supplied uid/clientId/notificationId/
    sheetId are all untrusted: we require the notification to exist under the
    TOKEN uid's client, and we require the target sheetId to be the exact sheet
    that notification was raised for. This closes the IDOR (writing into an
    arbitrary tenant's sheet) and row-anchor corruption (retargeting the row at
    a different property than the operator reviewed).
    """
    data, err = _require_json_object()
    if err:
        return err
    try:
        # The verified token uid is the ONLY trusted identity; never build a
        # Firestore path from a body-supplied uid.
        token_uid = _safe_uid(g.get("firebase_uid"))
        if token_uid is None:
            return jsonify({"success": False, "error": _GENERIC_BAD_REQUEST}), 400

        uid = data.get("uid")
        client_id = data.get("clientId")
        notification_id = data.get("notificationId")
        property_data = data.get("propertyData", {})

        if not all(_is_nonempty_str(v) for v in (uid, client_id, notification_id)):
            return jsonify({"success": False, "error": "Missing required fields: uid, clientId, notificationId"}), 400

        # A body uid that names a different tenant than the verified token is a
        # spoof attempt -> fail closed before any Firestore/Sheets work.
        if uid != token_uid:
            return jsonify({"success": False, "error": "Not authorized for this account"}), 403

        # From here on the verified token uid is authoritative.
        uid = token_uid

        # propertyData must be an object (absent -> {} which then fails on the
        # required sheetId/address checks below). null / str / int / list -> 400.
        if not isinstance(property_data, dict):
            return jsonify({"success": False, "error": "propertyData must be an object"}), 400

        # Extract property data
        address = property_data.get("address", "")
        city = property_data.get("city", "")
        link = property_data.get("link", "")
        notes = property_data.get("notes", "")
        leasing_company = property_data.get("leasingCompany", "")
        leasing_contact = property_data.get("leasingContact", "")
        broker_email = property_data.get("brokerEmail", "")
        sheet_id = property_data.get("sheetId")
        tab_title = property_data.get("tabTitle")
        pdf_links = property_data.get("pdfLinks", [])
        pdf_manifest = property_data.get("pdfManifest", [])  # Full PDF data for extraction

        if not sheet_id:
            return jsonify({"success": False, "error": "Missing sheetId in propertyData"}), 400

        if not address:
            return jsonify({"success": False, "error": "Missing address in propertyData"}), 400

        # === Ownership / row-anchor guard ===================================
        # The notification must exist under THIS (token) user's client, and the
        # target sheetId must be the exact sheet the notification was raised for.
        # Anything else is an IDOR / row-anchor-corruption attempt -> fail closed
        # BEFORE any sheet write or OpenAI spend.
        from email_automation.clients import _fs
        try:
            notif_snapshot = (
                _fs.collection("users").document(uid)
                .collection("clients").document(client_id)
                .collection("notifications").document(notification_id).get()
            )
        except Exception as e:
            print(f"❌ accept-new-property notification lookup failed: {type(e).__name__}: {e}")
            return jsonify({"success": False, "error": "Failed to accept new property"}), 502

        if not getattr(notif_snapshot, "exists", False):
            return jsonify({"success": False, "error": "Not authorized for this notification"}), 403

        notif_data = notif_snapshot.to_dict() or {}
        notif_meta = notif_data.get("meta") or {}
        expected_sheet_id = notif_meta.get("sheetId") or notif_data.get("sheetId")
        if not _is_nonempty_str(expected_sheet_id) or sheet_id != expected_sheet_id:
            return jsonify({"success": False, "error": "sheetId does not match the notification"}), 403
        # ===================================================================

        # Import required modules
        from email_automation.clients import _sheets_client
        from email_automation.sheets import _get_first_tab_title, _read_header_row2, format_sheet_columns_autosize_with_exceptions, append_links_to_flyer_link_column
        from email_automation.sheet_operations import insert_property_row_above_divider

        sheets = _sheets_client()

        # Get tab title if not provided
        if not tab_title:
            tab_title = _get_first_tab_title(sheets, sheet_id)

        # Build values_by_header for the new row
        values_by_header = {}
        if address:
            values_by_header["property address"] = address
            values_by_header["address"] = address
        if city:
            values_by_header["city"] = city
        if broker_email:
            values_by_header["email"] = broker_email
            values_by_header["email address"] = broker_email
        if leasing_company:
            values_by_header["leasing company"] = leasing_company
            values_by_header["leasing company "] = leasing_company
        if leasing_contact:
            values_by_header["leasing contact"] = leasing_contact
        if link:
            values_by_header["flyer / link"] = link
        if notes:
            values_by_header["listing brokers comments"] = notes

        # Create the row
        new_rownum = insert_property_row_above_divider(sheets, sheet_id, tab_title, values_by_header)

        # Read header and format sheet
        header = _read_header_row2(sheets, sheet_id, tab_title)
        format_sheet_columns_autosize_with_exceptions(sheet_id, header)

        # Apply PDF links if provided (from original message that suggested this property)
        if pdf_links and new_rownum:
            try:
                append_links_to_flyer_link_column(sheets, sheet_id, header, new_rownum, pdf_links)
                print(f"🔗 Applied {len(pdf_links)} PDF link(s) to new property row")
            except Exception as e:
                print(f"⚠️ Could not apply PDF links to new row: {e}")

        # If we have PDF manifest with extracted text, run AI extraction to pre-fill columns
        extracted_updates = []
        if pdf_manifest and new_rownum:
            try:
                from email_automation.ai_processing import propose_sheet_updates, apply_proposal_to_sheet
                from email_automation.sheets import _read_row
                from email_automation.column_config import get_default_column_config

                # Read the current row values (just created, mostly empty)
                rowvals = _read_row(sheets, sheet_id, tab_title, new_rownum) or []

                # Get client's column config if available
                from email_automation.clients import _fs
                client_doc = _fs.collection("users").document(uid).collection("clients").document(client_id).get()
                column_config = None
                if client_doc.exists:
                    client_data = client_doc.to_dict() or {}
                    column_config = client_data.get("columnConfig") or get_default_column_config()

                # Build a minimal conversation for context
                conversation = [{
                    "direction": "inbound",
                    "from": broker_email,
                    "subject": f"Property Info: {address}",
                    "content": f"Here is information about {address}" + (f", {city}" if city else "") + ".",
                    "timestamp": "now"
                }]

                # Call AI to extract data from PDFs
                proposal = propose_sheet_updates(
                    uid=uid,
                    client_id=client_id,
                    email=broker_email,
                    sheet_id=sheet_id,
                    header=header,
                    rownum=new_rownum,
                    rowvals=rowvals,
                    thread_id=f"new_property_{notification_id}",  # Synthetic thread ID
                    pdf_manifest=pdf_manifest,
                    conversation=conversation,
                    column_config=column_config,
                    dry_run=True  # Don't log to Firestore
                )

                if proposal and proposal.get("updates"):
                    # Apply the extracted updates to the sheet
                    result = apply_proposal_to_sheet(
                        uid, client_id, sheet_id, header, new_rownum, rowvals, proposal
                    )
                    extracted_updates = result.get("applied", [])
                    print(f"🤖 AI extracted {len(extracted_updates)} field(s) from PDF for new property")
                    for upd in extracted_updates:
                        print(f"   • {upd.get('column')}: {upd.get('newValue')}")

            except Exception as e:
                print(f"⚠️ Could not run AI extraction on PDFs: {e}")
                import traceback
                traceback.print_exc()

        print(f"✅ Created new property row {new_rownum} for '{address}' in sheet {sheet_id}")

        return jsonify({
            "success": True,
            "message": f"Property row created successfully",
            "rowNumber": new_rownum
        })

    except Exception as e:
        print(f"❌ Failed to accept new property: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": "Failed to accept new property"}), 500


def _resolve_current_highlight_row(uid, client_id, thread_data):
    """
    Re-resolve a paused/escalated thread's CURRENT sheet row before painting or
    clearing a highlight.

    A paused thread stores the ``rowNumber`` captured when the escalation was
    raised. If the broker's property row was later marked non-viable, deleted, or
    re-sorted in the Google Sheet, that stored index is stale — highlighting or
    clearing it would paint/clear the WRONG row and give the operator false visual
    state on a different property. The outbox anchors safely by email
    (``_find_row_by_email``); the stop/resume highlight path historically did not.

    Strategy: re-resolve by the thread's participant email(s) (the same anchor the
    outbox uses). Returns the CURRENT 1-based row when found, ``None`` when the row
    no longer exists (skip the highlight rather than paint a stale row), and falls
    back to the stored ``rowNumber`` only when no email anchor is available or
    resolution errors out.
    """
    stored_row = thread_data.get("rowNumber")

    emails = thread_data.get("email")
    if isinstance(emails, str):
        emails = [emails]
    emails = [e for e in (emails or []) if e]
    if not client_id or not emails:
        # No anchor to re-resolve with → best-effort stored row.
        return stored_row

    try:
        from email_automation.clients import _get_client_config, _sheets_client
        from email_automation.sheets import (
            _find_row_by_email,
            _get_first_tab_title,
            _read_header_row2,
        )

        sheet_id, _, _ = _get_client_config(uid, client_id)
        if not sheet_id:
            return stored_row

        sheets = _sheets_client()
        tab_title = _get_first_tab_title(sheets, sheet_id)
        header = _read_header_row2(sheets, sheet_id, tab_title)

        for email in emails:
            rownum, _ = _find_row_by_email(sheets, sheet_id, tab_title, header, email)
            if rownum:
                if stored_row and rownum != stored_row:
                    print(
                        f"📍 Re-anchored highlight row {stored_row}→{rownum} "
                        f"(sheet moved) for {email}",
                        flush=True,
                    )
                return rownum

        # Broker email(s) no longer present in the sheet (row removed / non-viable):
        # do NOT fall back to the stale stored row — skip the highlight entirely.
        print(
            "⚠️ Thread row no longer in sheet (removed/non-viable) — skipping highlight",
            flush=True,
        )
        return None
    except Exception as e:
        print(f"⚠️ Could not re-resolve current row, using stored rowNumber: {e}", flush=True)
        return stored_row


@app.route("/api/resume-conversation", methods=["POST"])
@verify_firebase_token
def api_resume_conversation():
    """
    Resume monitoring a paused conversation thread.
    Called when user sends a reply from the frontend.
    - Sets thread status to 'active'
    - Highlights row yellow in Google Sheet
    - Resets follow-up status to 'waiting'

    Expects JSON body: { uid, threadId, clientId? }

    Mutates a live conversation thread, so identity comes ONLY from the verified
    Firebase ID token (@verify_firebase_token). The body-supplied uid is ignored
    for the Firestore path; the thread is loaded strictly under the TOKEN uid, so
    a caller can only ever resume their OWN threads. If a clientId is supplied it
    must match the thread's clientId. This closes the cross-tenant thread hijack.
    """
    data, err = _require_json_object()
    if err:
        return err
    try:
        # Identity is the verified token uid ONLY; the body uid is untrusted.
        token_uid = _safe_uid(g.get("firebase_uid"))
        if token_uid is None:
            return jsonify({"success": False, "error": _GENERIC_BAD_REQUEST}), 400

        uid = data.get("uid") or data.get("userId") or data.get("user_id")
        thread_id = data.get("threadId") or data.get("thread_id")
        client_id = data.get("clientId") or data.get("client_id")  # Optional, used to find sheet

        # threadId must be a non-empty string with no path separators (it is
        # interpolated directly into a Firestore document path). The body uid is
        # ignored for identity, but a well-formed uid is still required by the
        # request contract (reject malformed values such as embedded newlines).
        if not _is_nonempty_str(uid) or not _is_nonempty_str(thread_id):
            return jsonify({"success": False, "error": "Missing required fields: uid, threadId"}), 400
        if _safe_uid(uid) is None or "/" in thread_id:
            return jsonify({"success": False, "error": "Invalid uid or threadId"}), 400
        if client_id is not None and not isinstance(client_id, str):
            return jsonify({"success": False, "error": "Invalid clientId"}), 400

        # From here on the verified token uid is authoritative — ignore body uid.
        uid = token_uid

        from email_automation.clients import _fs, _get_client_config
        from email_automation.messaging import update_thread_status, THREAD_STATUS
        from email_automation.sheets import highlight_row, ROW_HIGHLIGHT_YELLOW
        from google.cloud.firestore import SERVER_TIMESTAMP

        # Get thread data
        thread_ref = _fs.collection("users").document(uid).collection("threads").document(thread_id)
        thread_doc = thread_ref.get()

        if not thread_doc.exists:
            return jsonify({"success": False, "error": "Thread not found"}), 404

        thread_data = thread_doc.to_dict() or {}

        # Ownership guard: if the caller named a client, it must be THIS thread's
        # client — refuse a mismatched clientId rather than acting on it.
        if _is_nonempty_str(client_id) and thread_data.get("clientId") != client_id:
            return jsonify({"success": False, "error": "Not authorized for this thread"}), 403

        current_status = thread_data.get("status", "")

        # Only resume if currently paused
        if current_status != "paused":
            print(f"ℹ️ Thread already {current_status}, not paused - skipping resume", flush=True)
            return jsonify({
                "success": True,
                "message": f"Thread already {current_status}",
                "threadId": thread_id,
                "newStatus": current_status
            })

        # Update thread status to active
        if not update_thread_status(uid, thread_id, THREAD_STATUS["active"], "user_replied"):
            return jsonify({"success": False, "error": "Failed to update thread status"}), 500

        # Reset follow-up status to waiting
        try:
            thread_ref.update({
                "followUpStatus": "waiting",
                "updatedAt": SERVER_TIMESTAMP
            })
            print(f"▶️ Reset follow-up status for thread {thread_id[:20]}...", flush=True)
        except Exception as e:
            print(f"⚠️ Could not reset follow-up status: {e}", flush=True)

        # Highlight row yellow in Google Sheet.
        # Re-resolve the CURRENT row by email so a moved/re-sorted/removed row is
        # not painted by a stale stored rowNumber (see _resolve_current_highlight_row).
        client_id = client_id or thread_data.get("clientId")
        row_number = _resolve_current_highlight_row(uid, client_id, thread_data)

        if client_id and row_number:
            try:
                sheet_id, _, _ = _get_client_config(uid, client_id)
                if sheet_id:
                    highlight_row(sheet_id, row_number, ROW_HIGHLIGHT_YELLOW)
                    print(f"🟡 Highlighted row {row_number} yellow (resumed)", flush=True)
            except Exception as e:
                print(f"⚠️ Could not highlight row: {e}", flush=True)

        print(f"▶️ Resumed monitoring thread {thread_id[:20]}...", flush=True)

        return jsonify({
            "success": True,
            "message": "Conversation monitoring resumed",
            "threadId": thread_id,
            "newStatus": "active"
        })

    except Exception as e:
        print(f"❌ Failed to resume conversation: {type(e).__name__}: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": "Failed to resume conversation"}), 500


@app.route("/api/stop-conversation", methods=["POST"])
@verify_firebase_token
def api_stop_conversation():
    """
    Stop monitoring a conversation thread.
    - Sets thread status to 'stopped'
    - Pauses any pending follow-ups
    - Clears row highlight in Google Sheet

    Expects JSON body: { uid, threadId, clientId? }

    Force-sets a live thread to stopped and wipes its follow-up scheduling, so
    identity comes ONLY from the verified Firebase ID token
    (@verify_firebase_token). The body-supplied uid is ignored for the Firestore
    path; the thread is loaded strictly under the TOKEN uid, so a caller can only
    ever stop their OWN threads. If a clientId is supplied it must match the
    thread's clientId. This closes the cross-tenant thread-kill attack.
    """
    data, err = _require_json_object()
    if err:
        return err
    try:
        # Identity is the verified token uid ONLY; the body uid is untrusted.
        token_uid = _safe_uid(g.get("firebase_uid"))
        if token_uid is None:
            return jsonify({"success": False, "error": _GENERIC_BAD_REQUEST}), 400

        uid = data.get("uid")
        thread_id = data.get("threadId")
        client_id = data.get("clientId")  # Optional, used to find sheet

        # threadId must be a non-empty string with no path separators (it is
        # interpolated directly into a Firestore document path). The body uid is
        # ignored for identity, but a well-formed uid is still required by the
        # request contract (reject malformed values such as embedded newlines).
        if not _is_nonempty_str(uid) or not _is_nonempty_str(thread_id):
            return jsonify({"success": False, "error": "Missing required fields: uid, threadId"}), 400
        if _safe_uid(uid) is None or "/" in thread_id:
            return jsonify({"success": False, "error": "Invalid uid or threadId"}), 400
        if client_id is not None and not isinstance(client_id, str):
            return jsonify({"success": False, "error": "Invalid clientId"}), 400

        # From here on the verified token uid is authoritative — ignore body uid.
        uid = token_uid

        from email_automation.clients import _fs, _get_client_config
        from email_automation.messaging import update_thread_status, THREAD_STATUS
        from email_automation.sheets import clear_row_highlight
        from google.cloud.firestore import SERVER_TIMESTAMP

        # Get thread data
        thread_ref = _fs.collection("users").document(uid).collection("threads").document(thread_id)
        thread_doc = thread_ref.get()

        if not thread_doc.exists:
            return jsonify({"success": False, "error": "Thread not found"}), 404

        thread_data = thread_doc.to_dict() or {}

        # Ownership guard: if the caller named a client, it must be THIS thread's
        # client — refuse a mismatched clientId rather than acting on it.
        if _is_nonempty_str(client_id) and thread_data.get("clientId") != client_id:
            return jsonify({"success": False, "error": "Not authorized for this thread"}), 403

        # Update thread status to stopped
        if not update_thread_status(uid, thread_id, THREAD_STATUS["stopped"], "user_requested"):
            return jsonify({"success": False, "error": "Failed to update thread status"}), 500

        # Stop follow-ups
        try:
            thread_ref.update({
                "followUpStatus": "stopped",
                "followUpConfig.stoppedAt": SERVER_TIMESTAMP,
                "followUpConfig.processingBy": None,
                "followUpConfig.processingAt": None,
                "nextFollowUpAt": None,
                "updatedAt": SERVER_TIMESTAMP
            })
            print(f"⏹️ Stopped follow-ups for thread {thread_id[:20]}...", flush=True)
        except Exception as e:
            print(f"⚠️ Could not stop follow-ups: {e}", flush=True)

        # Cancel any queued outbox item for this thread. Stopping a paused/escalated
        # thread must also halt an already-queued send (an AI reply queued just
        # before escalation, or a resumed reply the operator second-guessed).
        # The outbox worker's cancel guard keys off cancelRequested/status on the
        # OUTBOX doc, so we must set them here — updating only the thread doc leaves
        # the queued send live and the worker still sends it.
        try:
            outbox_ref = (
                _fs.collection("users").document(uid).collection("outbox")
            )
            cancelled_count = 0
            for outbox_doc in outbox_ref.where("threadId", "==", thread_id).stream():
                outbox_item = outbox_doc.to_dict() or {}
                status = str(outbox_item.get("status") or "").strip().lower()
                if outbox_item.get("cancelRequested") is True or status in (
                    "cancel_requested",
                    "cancelled",
                    "canceled",
                ):
                    continue  # already cancelled
                outbox_doc.reference.update({
                    "cancelRequested": True,
                    "status": "cancel_requested",
                    "cancelledAt": SERVER_TIMESTAMP,
                })
                cancelled_count += 1
            if cancelled_count:
                print(
                    f"🚫 Cancelled {cancelled_count} queued outbox item(s) for thread {thread_id[:20]}...",
                    flush=True,
                )
        except Exception as e:
            print(f"⚠️ Could not cancel queued outbox items: {e}", flush=True)

        # Clear row highlight in Google Sheet.
        # Re-resolve the CURRENT row by email so a moved/re-sorted/removed row is
        # not cleared by a stale stored rowNumber (see _resolve_current_highlight_row).
        client_id = client_id or thread_data.get("clientId")
        row_number = _resolve_current_highlight_row(uid, client_id, thread_data)

        if client_id and row_number:
            try:
                sheet_id, _, _ = _get_client_config(uid, client_id)
                if sheet_id:
                    clear_row_highlight(sheet_id, row_number)
                    print(f"✨ Cleared highlight for row {row_number}", flush=True)
            except Exception as e:
                print(f"⚠️ Could not clear row highlight: {e}", flush=True)

        print(f"⏹️ Stopped monitoring thread {thread_id[:20]}...", flush=True)

        return jsonify({
            "success": True,
            "message": "Conversation monitoring stopped",
            "threadId": thread_id,
            "newStatus": "stopped"
        })

    except Exception as e:
        print(f"❌ Failed to stop conversation: {type(e).__name__}: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": "Failed to stop conversation"}), 500


@app.route("/api/dismiss-notification", methods=["POST"])
def api_dismiss_notification():
    """Dismiss an action_needed escalation the operator has decided needs no reply.

    A bare frontend delete of the notification silently DROPS the escalation:
    the alert disappears but the thread stays 'paused' forever (no follow-ups,
    no reply, no record) — invisible and unresolvable. This route terminalizes
    the escalation instead:
      - deletes the notification via the counter-safe backend helper
      - moves the paused thread to 'stopped' (reason 'user_dismissed_escalation')
      - writes a terminal actionAudit record (status='dismissed')
      - cancels any queued outbox send and clears the sheet row highlight

    Expects JSON: { uid, notificationId, clientId,
                    threadId?, notificationClientId?, actionAuditId? }
    (clientId is the notification's parent client; notificationClientId is an
    accepted alias.)
    """
    try:
        data = request.get_json(silent=True) or request.form.to_dict() or request.args.to_dict()
        if not data:
            return jsonify({"success": False, "error": "No JSON data provided"}), 400

        uid = data.get("uid") or data.get("userId") or data.get("user_id")
        notification_id = data.get("notificationId") or data.get("notification_id")
        notif_client_id = (
            data.get("notificationClientId")
            or data.get("clientId")
            or data.get("client_id")
        )
        thread_id = data.get("threadId") or data.get("thread_id")
        action_audit_id = data.get("actionAuditId")

        if not uid or not notification_id or not notif_client_id:
            return jsonify({
                "success": False,
                "error": "Missing required fields: uid, notificationId, clientId",
            }), 400

        from email_automation.clients import _fs, _get_client_config
        from email_automation.messaging import update_thread_status, THREAD_STATUS
        from email_automation.notifications import delete_notification_and_decrement_counters
        from email_automation.email import _update_action_audit
        from email_automation.sheets import clear_row_highlight
        from google.cloud.firestore import SERVER_TIMESTAMP

        thread_data = {}
        if thread_id:
            thread_ref = _fs.collection("users").document(uid).collection("threads").document(thread_id)
            thread_doc = thread_ref.get()
            if thread_doc.exists:
                thread_data = thread_doc.to_dict() or {}

        # 1) Delete the notification (counter-safe) so the alert clears exactly
        #    like the bare frontend path, but through the backend helper that
        #    keeps the client rollup counters in sync.
        try:
            delete_notification_and_decrement_counters(uid, notif_client_id, notification_id)
        except Exception as e:
            print(f"⚠️ Could not delete notification {notification_id}: {e}", flush=True)

        # 2) Terminalize the thread so it is not left stuck in 'paused'. Dismiss
        #    (unlike reply, which resumes to 'active') means the operator handled
        #    it out of band — stop monitoring rather than resume.
        new_status = thread_data.get("status")
        if thread_id and thread_data.get("status") == THREAD_STATUS["paused"]:
            if update_thread_status(uid, thread_id, THREAD_STATUS["stopped"], "user_dismissed_escalation"):
                new_status = THREAD_STATUS["stopped"]
            try:
                _fs.collection("users").document(uid).collection("threads").document(thread_id).update({
                    "followUpStatus": "stopped",
                    "followUpConfig.stoppedAt": SERVER_TIMESTAMP,
                    "followUpConfig.processingBy": None,
                    "followUpConfig.processingAt": None,
                    "nextFollowUpAt": None,
                    "updatedAt": SERVER_TIMESTAMP,
                })
            except Exception as e:
                print(f"⚠️ Could not stop follow-ups on dismiss: {e}", flush=True)

            # Cancel any queued outbox send for this thread (an AI reply queued
            # just before escalation must not fire after the operator dismissed).
            try:
                outbox_ref = _fs.collection("users").document(uid).collection("outbox")
                for outbox_doc in outbox_ref.where("threadId", "==", thread_id).stream():
                    item = outbox_doc.to_dict() or {}
                    status = str(item.get("status") or "").strip().lower()
                    if item.get("cancelRequested") is True or status in (
                        "cancel_requested", "cancelled", "canceled",
                    ):
                        continue
                    outbox_doc.reference.update({
                        "cancelRequested": True,
                        "status": "cancel_requested",
                        "cancelledAt": SERVER_TIMESTAMP,
                    })
            except Exception as e:
                print(f"⚠️ Could not cancel queued outbox items on dismiss: {e}", flush=True)

            # Clear the sheet row highlight (re-resolve the CURRENT row).
            client_id = thread_data.get("clientId") or notif_client_id
            row_number = _resolve_current_highlight_row(uid, client_id, thread_data)
            if client_id and row_number:
                try:
                    sheet_id, _, _ = _get_client_config(uid, client_id)
                    if sheet_id:
                        clear_row_highlight(sheet_id, row_number)
                except Exception as e:
                    print(f"⚠️ Could not clear row highlight on dismiss: {e}", flush=True)

        # 3) Write a terminal actionAudit record so the dismissal is never a
        #    silent drop — it always leaves a durable trail.
        _update_action_audit(uid, action_audit_id, {
            "status": "dismissed",
            "clientId": notif_client_id,
            "notificationId": notification_id,
            "threadId": thread_id,
            "reason": (thread_data.get("statusReason") or data.get("reason")),
            "dismissedAt": SERVER_TIMESTAMP,
            "updatedAt": SERVER_TIMESTAMP,
        })

        print(f"🗑️ Dismissed notification {notification_id} (thread {str(thread_id)[:20]})", flush=True)
        return jsonify({
            "success": True,
            "message": "Escalation dismissed",
            "notificationId": notification_id,
            "threadId": thread_id,
            "newStatus": new_status,
        })

    except Exception as e:
        print(f"❌ Failed to dismiss notification: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/clear-optout", methods=["POST"])
@verify_firebase_token
def api_clear_optout():
    """
    Clear an opt-out record for a contact, allowing emails to be sent again.
    Expects JSON body: { uid, email }

    Deletes a compliance record (an opt-out) that gates whether the platform may
    email a contact again, so identity comes ONLY from the verified Firebase ID
    token (@verify_firebase_token). The body-supplied uid is ignored for the
    Firestore path — a caller can only ever clear opt-outs under their OWN uid,
    never re-enable email to another tenant's opted-out contacts.
    """
    data, err = _require_json_object()
    if err:
        return err
    try:
        # Identity is the verified token uid ONLY; the body uid is untrusted.
        token_uid = _safe_uid(g.get("firebase_uid"))
        if token_uid is None:
            return jsonify({"success": False, "error": _GENERIC_BAD_REQUEST}), 400

        uid = data.get("uid")
        email = data.get("email")

        # Both must be non-empty strings — a non-string email would blow up on
        # .lower()/.strip(); a non-string uid would corrupt the Firestore path.
        if not _is_nonempty_str(uid) or not _is_nonempty_str(email):
            return jsonify({"success": False, "error": "Missing required fields: uid, email"}), 400

        # From here on the verified token uid is authoritative — ignore body uid.
        uid = token_uid

        import hashlib
        from email_automation.clients import _fs

        # Hash the email to find the document
        email_lower = email.lower().strip()
        email_hash = hashlib.sha256(email_lower.encode('utf-8')).hexdigest()[:16]

        # Delete the opt-out record
        optout_ref = _fs.collection("users").document(uid).collection("optedOutContacts").document(email_hash)
        doc = optout_ref.get()

        if not doc.exists:
            return jsonify({
                "success": False,
                "error": f"No opt-out record found for {email_lower}"
            }), 404

        # Get the record details before deleting
        record_data = doc.to_dict()
        optout_ref.delete()

        print(f"✅ Cleared opt-out for {email_lower} (was: {record_data.get('reason', 'unknown')})", flush=True)

        return jsonify({
            "success": True,
            "message": f"Opt-out cleared for {email_lower}",
            "previousRecord": {
                "email": record_data.get("email"),
                "reason": record_data.get("reason"),
                "optedOutAt": str(record_data.get("optedOutAt", ""))
            }
        })

    except Exception as e:
        print(f"❌ Failed to clear opt-out: {type(e).__name__}: {e}", flush=True)
        return jsonify({"success": False, "error": "Failed to clear opt-out"}), 500


@app.route("/api/list-optouts", methods=["POST"])
@verify_firebase_token
def api_list_optouts():
    """
    List all opted-out contacts for a user.
    Expects JSON body: { uid }

    Discloses a tenant's opted-out contact list (emails + reasons), so identity
    comes ONLY from the verified Firebase ID token (@verify_firebase_token). The
    body-supplied uid is ignored for the Firestore path — a caller can only ever
    list opt-outs under their OWN uid, never enumerate another tenant's contacts.
    """
    data, err = _require_json_object()
    if err:
        return err
    try:
        # Identity is the verified token uid ONLY; the body uid is untrusted.
        token_uid = _safe_uid(g.get("firebase_uid"))
        if token_uid is None:
            return jsonify({"success": False, "error": _GENERIC_BAD_REQUEST}), 400

        uid = data.get("uid")
        if not _is_nonempty_str(uid):
            return jsonify({"success": False, "error": "Missing required field: uid"}), 400

        # From here on the verified token uid is authoritative — ignore body uid.
        uid = token_uid

        from email_automation.clients import _fs

        # Get all opt-out records
        optouts_ref = _fs.collection("users").document(uid).collection("optedOutContacts")
        docs = optouts_ref.stream()

        optouts = []
        for doc in docs:
            record = doc.to_dict()
            optouts.append({
                "id": doc.id,
                "email": record.get("email"),
                "reason": record.get("reason"),
                "optedOutAt": str(record.get("optedOutAt", ""))
            })

        print(f"📋 Listed {len(optouts)} opt-outs for user {uid}", flush=True)

        return jsonify({
            "success": True,
            "count": len(optouts),
            "optouts": optouts
        })

    except Exception as e:
        print(f"❌ Failed to list opt-outs: {type(e).__name__}: {e}", flush=True)
        return jsonify({"success": False, "error": "Failed to list opt-outs"}), 500


@app.route("/api/check-sheet-completion", methods=["POST"])
@verify_firebase_token
def api_check_sheet_completion():
    """
    Check if all rows in a sheet have all required fields filled.
    Returns completion status and details.

    Reads a Google Sheet and returns per-row property addresses + missing-field
    detail, so identity comes ONLY from the verified Firebase ID token
    (@verify_firebase_token). The sheet is NOT taken from the request: the caller
    supplies a clientId and the sheetId is resolved server-side from the TOKEN
    uid's client doc. Any body-supplied sheetId that doesn't match the resolved
    one is refused. This closes the IDOR that let an unauthenticated caller
    exfiltrate an arbitrary tenant's sheet by guessing its spreadsheet id.

    Expects JSON body: { clientId, sheetId? }
    """
    data, err = _require_json_object()
    if err:
        return err
    try:
        # Identity is the verified token uid ONLY.
        token_uid = _safe_uid(g.get("firebase_uid"))
        if token_uid is None:
            return jsonify({"success": False, "error": _GENERIC_BAD_REQUEST}), 400

        client_id = data.get("clientId")
        if not _is_nonempty_str(client_id):
            return jsonify({"success": False, "error": "Missing clientId"}), 400

        from email_automation.clients import _sheets_client, _get_client_config
        from email_automation.sheets import _get_first_tab_title, _read_header_row2, _header_index_map
        from email_automation.app_config import REQUIRED_FIELDS_FOR_CLOSE

        # Resolve the sheet server-side from the authenticated user's client —
        # never trust a client-supplied sheetId. Refuse a foreign sheetId.
        try:
            sheet_id, _, _ = _get_client_config(token_uid, client_id)
        except Exception:
            return jsonify({"success": False, "error": "Not authorized for this client"}), 403
        if not _is_nonempty_str(sheet_id):
            return jsonify({"success": False, "error": "Not authorized for this client"}), 403
        body_sheet_id = data.get("sheetId")
        if body_sheet_id is not None and body_sheet_id != sheet_id:
            return jsonify({"success": False, "error": "sheetId does not match this client"}), 403

        sheets = _sheets_client()
        tab_title = _get_first_tab_title(sheets, sheet_id)
        header = _read_header_row2(sheets, sheet_id, tab_title)
        idx_map = _header_index_map(header)

        # Read all data rows
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{tab_title}!A3:ZZZ"
        ).execute()
        rows = resp.get("values", [])

        total_viable = 0
        completed = 0
        incomplete_rows = []

        for row_offset, row in enumerate(rows, start=3):
            # Skip NON-VIABLE divider and rows below it
            if row and str(row[0]).strip().upper() == "NON-VIABLE":
                break

            # Skip empty rows
            if not row or not any(cell.strip() for cell in row if cell):
                continue

            total_viable += 1
            padded = row + [""] * (max(0, len(header) - len(row)))

            # Check required fields
            missing = []
            for field in REQUIRED_FIELDS_FOR_CLOSE:
                key = field.strip().lower()
                if key in idx_map:
                    i = idx_map[key] - 1  # 0-based
                    if i >= len(padded) or not (padded[i] or "").strip():
                        missing.append(field)

            if missing:
                # Get property address for context
                addr_idx = idx_map.get("property address", idx_map.get("address", 1)) - 1
                address = padded[addr_idx] if addr_idx < len(padded) else f"Row {row_offset}"
                incomplete_rows.append({
                    "rowNumber": row_offset,
                    "address": address,
                    "missingFields": missing
                })
            else:
                completed += 1

        is_complete = total_viable > 0 and completed == total_viable

        return jsonify({
            "success": True,
            "isComplete": is_complete,
            "totalViableProperties": total_viable,
            "completedProperties": completed,
            "incompleteRows": incomplete_rows[:10],  # Limit to first 10 for response size
            "completionPercentage": round((completed / total_viable * 100) if total_viable > 0 else 0, 1)
        })

    except Exception as e:
        print(f"❌ Failed to check sheet completion: {type(e).__name__}: {e}")
        return jsonify({"success": False, "error": "Failed to check sheet completion"}), 500


@app.route("/api/debug-inbox", methods=["GET"])
@verify_firebase_token
def api_debug_inbox():
    """Debug endpoint to check inbox status and email processing.

    Scoped to the AUTHENTICATED caller's own mailbox only — previously this
    returned list_user_ids()[0]'s live inbox (subjects/senders/recipients/ids)
    to any unauthenticated request (cross-tenant PII disclosure).
    """
    if not SCHEDULER_AVAILABLE:
        return jsonify({"error": "Scheduler functionality not available"}), 503

    # Identity comes ONLY from the verified token; the caller can only ever see
    # their OWN inbox, never an arbitrary first-user's mail.
    user_id = _safe_uid(g.get("firebase_uid"))
    if user_id is None:
        return jsonify({"error": _GENERIC_BAD_REQUEST}), 400

    try:
        from firebase_helpers import download_token
        from msal import ConfidentialClientApplication, SerializableTokenCache
        import requests
        from datetime import datetime, timedelta, timezone

        # Download token and setup client
        download_token(FIREBASE_API_KEY, output_file=TOKEN_CACHE, user_id=user_id)
        cache = SerializableTokenCache()
        with open(TOKEN_CACHE, "r") as f:
            cache.deserialize(f.read())
        
        app_obj = ConfidentialClientApplication(
            CLIENT_ID,
            client_credential=CLIENT_SECRET,
            authority=AUTHORITY,
            token_cache=cache
        )
        
        accounts = app_obj.get_accounts()
        if not accounts:
            return jsonify({"error": "No account found"}), 404
        
        result = app_obj.acquire_token_silent(SCOPES, account=accounts[0])
        if not result or "access_token" not in result:
            return jsonify({"error": "Failed to get access token"}), 401
        
        headers = {
            "Authorization": f"Bearer {result['access_token']}",
            "Content-Type": "application/json"
        }
        
        # Check inbox with 5-hour filter (same as scheduler)
        now_utc = datetime.now(timezone.utc)
        cutoff_time = now_utc - timedelta(hours=5)
        cutoff_iso = cutoff_time.isoformat().replace("+00:00", "Z")
        
        # Get recent emails
        response = requests.get(
            "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages",
            headers=headers,
            params={
                "$top": "10",
                "$orderby": "receivedDateTime desc",
                "$select": "id,subject,from,toRecipients,receivedDateTime,isRead,conversationId,internetMessageId",
                "$filter": f"receivedDateTime ge {cutoff_iso}"
            },
            timeout=30
        )
        
        if response.status_code != 200:
            return jsonify({"error": f"Failed to fetch emails: {response.status_code}"}), 500
        
        emails_data = response.json()
        emails = emails_data.get("value", [])

        # Get the current user's email address to verify which account we're connected to
        me_response = requests.get(
            "https://graph.microsoft.com/v1.0/me",
            headers=headers,
            params={"$select": "mail,userPrincipalName,displayName"},
            timeout=30
        )
        account_info = {}
        if me_response.status_code == 200:
            me_data = me_response.json()
            account_info = {
                "email": me_data.get("mail") or me_data.get("userPrincipalName"),
                "displayName": me_data.get("displayName"),
                "userPrincipalName": me_data.get("userPrincipalName")
            }

        # Check processed status for each email
        from email_automation.messaging import has_processed

        debug_info = {
            "user_id": user_id,
            "connected_account": account_info,
            "cutoff_time": cutoff_iso,
            "total_emails_in_window": len(emails),
            "emails": []
        }
        
        for email in emails:
            processed_key = email.get("internetMessageId") or email.get("id")
            is_processed = has_processed(user_id, processed_key) if processed_key else False
            
            debug_info["emails"].append({
                "id": email.get("id"),
                "internetMessageId": email.get("internetMessageId"),
                "subject": email.get("subject"),
                "from": email.get("from", {}).get("emailAddress", {}).get("address"),
                "receivedDateTime": email.get("receivedDateTime"),
                "isRead": email.get("isRead"),
                "conversationId": email.get("conversationId"),
                "processed_key": processed_key,
                "is_processed": is_processed
            })
        
        return jsonify(debug_info)
        
    except Exception as e:
        return jsonify({"error": f"Debug failed: {str(e)}"}), 500

@app.route("/api/debug-thread-matching", methods=["GET"])
@verify_firebase_token
def api_debug_thread_matching():
    """Debug endpoint to check thread matching for specific conversation.

    Scoped to the AUTHENTICATED caller's own mailbox + sheets only — previously
    it disclosed list_user_ids()[0]'s live inbox message and matched Google
    Sheet row contents to any unauthenticated request.
    """
    if not SCHEDULER_AVAILABLE:
        return jsonify({"error": "Scheduler functionality not available"}), 503

    user_id = _safe_uid(g.get("firebase_uid"))
    if user_id is None:
        return jsonify({"error": _GENERIC_BAD_REQUEST}), 400

    try:
        from email_automation.messaging import lookup_thread_by_conversation_id
        from firebase_helpers import download_token
        from msal import ConfidentialClientApplication, SerializableTokenCache
        import requests
        from datetime import datetime, timedelta, timezone

        # Download token and setup client
        download_token(FIREBASE_API_KEY, output_file=TOKEN_CACHE, user_id=user_id)
        cache = SerializableTokenCache()
        with open(TOKEN_CACHE, "r") as f:
            cache.deserialize(f.read())
        
        app_obj = ConfidentialClientApplication(
            CLIENT_ID,
            client_credential=CLIENT_SECRET,
            authority=AUTHORITY,
            token_cache=cache
        )
        
        accounts = app_obj.get_accounts()
        if not accounts:
            return jsonify({"error": "No account found"}), 404
        
        result = app_obj.acquire_token_silent(SCOPES, account=accounts[0])
        if not result or "access_token" not in result:
            return jsonify({"error": "Failed to get access token"}), 401
        
        headers = {
            "Authorization": f"Bearer {result['access_token']}",
            "Content-Type": "application/json"
        }
        
        # Get the unprocessed email
        now_utc = datetime.now(timezone.utc)
        cutoff_time = now_utc - timedelta(hours=5)
        cutoff_iso = cutoff_time.isoformat().replace("+00:00", "Z")
        
        response = requests.get(
            "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages",
            headers=headers,
            params={
                "$top": "10",
                "$orderby": "receivedDateTime desc",
                "$select": "id,subject,from,toRecipients,receivedDateTime,isRead,conversationId,internetMessageId",
                "$filter": f"receivedDateTime ge {cutoff_iso}"
            },
            timeout=30
        )
        
        if response.status_code != 200:
            return jsonify({"error": f"Failed to fetch emails: {response.status_code}"}), 500
        
        emails_data = response.json()
        emails = emails_data.get("value", [])
        
        # Find unprocessed email
        unprocessed_email = None
        for email in emails:
            processed_key = email.get("internetMessageId") or email.get("id")
            from email_automation.messaging import has_processed
            if not has_processed(user_id, processed_key):
                unprocessed_email = email
                break
        
        if not unprocessed_email:
            return jsonify({"error": "No unprocessed emails found"}), 404
        
        # Check thread matching
        conversation_id = unprocessed_email.get("conversationId")
        thread_id = lookup_thread_by_conversation_id(user_id, conversation_id)
        
        debug_info = {
            "unprocessed_email": {
                "id": unprocessed_email.get("id"),
                "subject": unprocessed_email.get("subject"),
                "from": unprocessed_email.get("from", {}).get("emailAddress", {}).get("address"),
                "conversationId": conversation_id,
                "internetMessageId": unprocessed_email.get("internetMessageId")
            },
            "thread_matching": {
                "conversation_id": conversation_id,
                "thread_id_found": thread_id,
                "thread_exists": thread_id is not None
            }
        }
        
        # If thread found, check sheet matching
        if thread_id:
            try:
                from email_automation.processing import fetch_and_log_sheet_for_thread
                client_id, sheet_id, header, rownum, rowvals = fetch_and_log_sheet_for_thread(
                    user_id, thread_id, unprocessed_email.get("from", {}).get("emailAddress", {}).get("address")
                )
                
                debug_info["sheet_matching"] = {
                    "client_id": client_id,
                    "sheet_id": sheet_id,
                    "header": header,
                    "row_found": rownum is not None,
                    "row_number": rownum,
                    "row_values": rowvals
                }
            except Exception as e:
                debug_info["sheet_matching"] = {"error": str(e)}
        
        return jsonify(debug_info)
        
    except Exception as e:
        return jsonify({"error": f"Debug failed: {str(e)}"}), 500


@app.route("/api/console-logs", methods=["GET"])
@verify_firebase_token
def api_console_logs():
    """Read browser console logs forwarded from frontend to Firestore.

    Scoped to the AUTHENTICATED caller's own logs. Any query `user_id` is
    IGNORED — previously an unauthenticated caller could read (and with
    clear=true permanently delete) an arbitrary victim's console log entries.

    Query params:
        limit: (optional) max logs to return, defaults to 50 (capped at 500)
        level: (optional) filter by level (error, warn, log)
        since: (optional) ISO timestamp to get logs after
        clear: (optional) if 'true', delete logs after reading
    """
    from google.cloud import firestore
    from datetime import datetime

    # Identity is the verified token uid only; ignore any attacker-supplied
    # ?user_id.
    user_id = _safe_uid(g.get("firebase_uid"))
    if user_id is None:
        return jsonify({"error": _GENERIC_BAD_REQUEST, "logs": []}), 400

    try:
        # `limit` is attacker-controlled: parse defensively (non-numeric ->
        # default, never a 500) and cap it so a huge value can't be used to
        # exhaust reads / responses.
        _raw_limit = request.args.get("limit", "50")
        try:
            limit = int(_raw_limit)
        except (TypeError, ValueError):
            limit = 50
        if limit < 1:
            limit = 1
        if limit > 500:
            limit = 500
        level_filter = request.args.get("level")
        since = request.args.get("since")
        clear = request.args.get("clear", "false").lower() == "true"

        # Get Firestore client
        fs = firestore.Client()
        logs_ref = fs.collection("users").document(user_id).collection("consoleLogs")

        # Build query
        query = logs_ref.order_by("createdAt", direction=firestore.Query.DESCENDING)

        if level_filter:
            query = query.where("level", "==", level_filter)

        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                query = query.where("createdAt", ">", since_dt)
            except ValueError:
                pass  # Ignore invalid timestamp

        query = query.limit(limit)

        # Execute query
        docs = list(query.stream())

        logs = []
        doc_ids = []
        for doc in docs:
            data = doc.to_dict()
            data["id"] = doc.id
            # Convert Firestore timestamp to ISO string
            if data.get("createdAt"):
                data["createdAt"] = data["createdAt"].isoformat() if hasattr(data["createdAt"], "isoformat") else str(data["createdAt"])
            logs.append(data)
            doc_ids.append(doc.id)

        # Optionally clear logs after reading
        deleted_count = 0
        if clear and doc_ids:
            batch = fs.batch()
            for doc_id in doc_ids:
                batch.delete(logs_ref.document(doc_id))
            batch.commit()
            deleted_count = len(doc_ids)

        return jsonify({
            "user_id": user_id,
            "count": len(logs),
            "deleted": deleted_count,
            "logs": logs
        })

    except Exception as e:
        return jsonify({"error": f"Failed to fetch console logs: {str(e)}", "logs": []}), 500


@app.route("/api/console-logs/clear", methods=["POST"])
@verify_firebase_token
def api_console_logs_clear():
    """Clear all console logs for the AUTHENTICATED caller.

    Any body `user_id` is IGNORED — previously an unauthenticated POST could
    wipe an arbitrary victim's entire consoleLogs collection.
    """
    from google.cloud import firestore

    # Fail closed on a non-object body rather than silently defaulting.
    data, err = _require_json_object()
    if err:
        return err

    user_id = _safe_uid(g.get("firebase_uid"))
    if user_id is None:
        return jsonify({"error": _GENERIC_BAD_REQUEST}), 400

    try:
        fs = firestore.Client()
        logs_ref = fs.collection("users").document(user_id).collection("consoleLogs")

        # Delete all documents in batches
        deleted = 0
        while True:
            docs = list(logs_ref.limit(100).stream())
            if not docs:
                break

            batch = fs.batch()
            for doc in docs:
                batch.delete(doc.reference)
            batch.commit()
            deleted += len(docs)

        return jsonify({"success": True, "deleted": deleted, "user_id": user_id})

    except Exception as e:
        return jsonify({"error": f"Failed to clear logs: {str(e)}"}), 500


# Web-based authentication routes
@app.route("/auth/login")
def auth_login():
    redirect_uri = _legacy_flask_oauth_redirect_uri()
    if not _legacy_flask_oauth_enabled() or not redirect_uri:
        return redirect(FRONTEND_EMAIL_ACCESS_URL)

    # Get UID from session, set during initial page load
    uid = session.get("uid", "web_user")
    print(f"[LOGIN] Using UID from session: {uid}")
    
    # Setup cache for this user
    cache = SerializableTokenCache()
    user_dir = f"msal_caches/{uid}" 
    cache_file = f"{user_dir}/msal_token_cache.bin" 
    os.makedirs(user_dir, exist_ok=True)
    if os.path.exists(cache_file):
        cache.deserialize(open(cache_file).read())
    
    app_obj = ConfidentialClientApplication(
        CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=AUTHORITY,
        token_cache=cache
    )
    
    # CRITICAL: Pass UID as state parameter to preserve it through OAuth redirect
    auth_url = app_obj.get_authorization_request_url(
        SCOPES,
        redirect_uri=redirect_uri,
        state=uid  # This preserves the UID through the OAuth flow
    )
    
    print(f"[LOGIN] Redirecting to auth URL with state={uid}")
    return redirect(auth_url)

@app.route("/auth/callback")
def auth_callback():
    redirect_uri = _legacy_flask_oauth_redirect_uri()
    if not _legacy_flask_oauth_enabled() or not redirect_uri:
        return redirect(FRONTEND_EMAIL_ACCESS_URL)

    try:
        # CRITICAL: Get UID from state parameter (this is how it survives the redirect)
        # Sanitise it through the strict path-safe charset BEFORE it is ever
        # interpolated into a filesystem path (msal_caches/<uid>/...). A crafted
        # state like "../../etc/x" would otherwise traverse out of msal_caches
        # on os.makedirs / token write. Fall back to the default on failure,
        # matching the "/" handler.
        uid = _safe_uid(request.args.get("state", "web_user")) or "web_user"
        print(f"[CALLBACK] Received UID from state parameter: {uid}")

        # Update session with the recovered UID
        session["uid"] = uid
        
        # Setup paths for this specific user
        user_dir = f"msal_caches/{uid}" 
        cache_file = f"{user_dir}/msal_token_cache.bin" 
        os.makedirs(user_dir, exist_ok=True)
        print(f"[CALLBACK] Will save token to: {cache_file}")

        cache = SerializableTokenCache()
        if os.path.exists(cache_file):
            cache.deserialize(open(cache_file).read())
        
        app_obj = ConfidentialClientApplication(
            CLIENT_ID,
            client_credential=CLIENT_SECRET,
            authority=AUTHORITY,
            token_cache=cache
        )
        
        # Get authorization code from callback
        code = request.args.get('code')
        if not code:
            error = request.args.get('error_description', 'Authorization was cancelled or failed')
            return render_template_string("""
                <!DOCTYPE html>
                <html lang="en">
                    <head>
                        <meta charset="UTF-8">
                        <meta name="viewport" content="width=device-width, initial-scale=1.0">
                        <title>Connection Failed - PropertyFlow</title>
                        <link rel="preconnect" href="https://fonts.googleapis.com">
                        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
                        <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
                        <style>
                            * { margin: 0; padding: 0; box-sizing: border-box; }
                            body {
                                font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                                background: linear-gradient(145deg, #0f172a 0%, #1e293b 50%, #334155 100%);
                                min-height: 100vh;
                                display: flex;
                                align-items: center;
                                justify-content: center;
                                padding: 20px;
                            }
                            .container {
                                background: white;
                                border-radius: 16px;
                                padding: 48px;
                                text-align: center;
                                max-width: 420px;
                                width: 100%;
                                box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
                            }
                            .error-icon {
                                width: 64px;
                                height: 64px;
                                background: #fef2f2;
                                border-radius: 50%;
                                display: flex;
                                align-items: center;
                                justify-content: center;
                                margin: 0 auto 24px;
                            }
                            .error-icon svg {
                                width: 32px;
                                height: 32px;
                                color: #dc2626;
                            }
                            h2 {
                                color: #0f172a;
                                margin-bottom: 8px;
                                font-size: 1.5rem;
                                font-weight: 700;
                                letter-spacing: -0.025em;
                            }
                            .error-message {
                                background: #fef2f2;
                                border: 1px solid #fecaca;
                                border-radius: 10px;
                                padding: 16px;
                                margin: 20px 0 24px;
                                color: #991b1b;
                                font-size: 0.875rem;
                                line-height: 1.5;
                            }
                            .btn {
                                display: inline-flex;
                                align-items: center;
                                gap: 8px;
                                padding: 14px 24px;
                                background: #0f172a;
                                color: white;
                                text-decoration: none;
                                border-radius: 10px;
                                font-weight: 600;
                                font-size: 0.9375rem;
                                transition: all 0.15s ease;
                            }
                            .btn:hover {
                                background: #1e293b;
                                transform: translateY(-1px);
                                box-shadow: 0 4px 12px rgba(15, 23, 42, 0.15);
                            }
                            .btn svg {
                                width: 18px;
                                height: 18px;
                            }
                        </style>
                    </head>
                    <body>
                        <div class="container">
                            <div class="error-icon">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                    <circle cx="12" cy="12" r="10"/>
                                    <line x1="15" y1="9" x2="9" y2="15"/>
                                    <line x1="9" y1="9" x2="15" y2="15"/>
                                </svg>
                            </div>
                            <h2>Connection Failed</h2>
                            <div class="error-message">{{ error }}</div>
                            <a href="/?uid={{ uid }}" class="btn">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                    <line x1="19" y1="12" x2="5" y2="12"/>
                                    <polyline points="12 19 5 12 12 5"/>
                                </svg>
                                Try Again
                            </a>
                        </div>
                    </body>
                </html>
            """, error=error, uid=uid)
        
        # Exchange code for token
        result = app_obj.acquire_token_by_authorization_code(
            code,
            scopes=SCOPES,
            redirect_uri=redirect_uri
        )
        
        if "access_token" in result:
            # Save token to user-specific cache file
            with open(cache_file, "w") as f:
                f.write(cache.serialize())
            
            account = result.get("account", {}).get("username", "Unknown")
            print(f"[CALLBACK] Successfully saved token for UID {uid}, account: {account}")
            
            return render_template_string("""
                <!DOCTYPE html>
                <html lang="en">
                <head>
                    <meta charset="UTF-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1.0">
                    <title>Connection Successful - PropertyFlow</title>
                    <link rel="preconnect" href="https://fonts.googleapis.com">
                    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
                    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
                    <style>
                        * { margin: 0; padding: 0; box-sizing: border-box; }
                        body {
                            font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                            background: linear-gradient(145deg, #0f172a 0%, #1e293b 50%, #334155 100%);
                            min-height: 100vh;
                            display: flex;
                            align-items: center;
                            justify-content: center;
                            padding: 20px;
                        }
                        .container {
                            background: white;
                            border-radius: 16px;
                            padding: 48px;
                            text-align: center;
                            max-width: 420px;
                            width: 100%;
                            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
                        }
                        .success-icon {
                            width: 64px;
                            height: 64px;
                            background: #f0fdf4;
                            border-radius: 50%;
                            display: flex;
                            align-items: center;
                            justify-content: center;
                            margin: 0 auto 24px;
                        }
                        .success-icon svg {
                            width: 32px;
                            height: 32px;
                            color: #22c55e;
                        }
                        h2 {
                            color: #0f172a;
                            margin-bottom: 8px;
                            font-size: 1.5rem;
                            font-weight: 700;
                            letter-spacing: -0.025em;
                        }
                        .subtitle {
                            color: #64748b;
                            font-size: 0.9375rem;
                            margin-bottom: 24px;
                        }
                        .email-badge {
                            display: inline-flex;
                            align-items: center;
                            gap: 8px;
                            background: #f0fdf4;
                            border: 1px solid #bbf7d0;
                            padding: 12px 20px;
                            border-radius: 10px;
                            color: #166534;
                            font-weight: 500;
                            font-size: 0.9375rem;
                            margin-bottom: 24px;
                        }
                        .email-badge svg {
                            width: 18px;
                            height: 18px;
                        }
                        .loading-section {
                            padding-top: 24px;
                            border-top: 1px solid #e2e8f0;
                        }
                        .loading-text {
                            color: #64748b;
                            font-size: 0.875rem;
                            margin-bottom: 16px;
                        }
                        .spinner {
                            width: 32px;
                            height: 32px;
                            border: 3px solid #e2e8f0;
                            border-top: 3px solid #3b82f6;
                            border-radius: 50%;
                            animation: spin 0.8s linear infinite;
                            margin: 0 auto;
                        }
                        @keyframes spin {
                            0% { transform: rotate(0deg); }
                            100% { transform: rotate(360deg); }
                        }
                    </style>
                </head>
                <body>
                    <div class="container">
                        <div class="success-icon">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                                <polyline points="20 6 9 17 4 12"/>
                            </svg>
                        </div>
                        <h2>Successfully Connected</h2>
                        <p class="subtitle">Your email access has been set up successfully</p>
                        <div class="email-badge">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/>
                                <polyline points="22,6 12,13 2,6"/>
                            </svg>
                            {{ account }}
                        </div>
                        <div class="loading-section">
                            <p class="loading-text">Completing setup...</p>
                            <div class="spinner"></div>
                        </div>
                    </div>
                    <script>
                        setTimeout(() => window.location.href = '/?uid={{ uid }}', 2500);
                    </script>
                </body>
                </html>
                """, account=account, uid=uid)

        else:
            error = result.get("error_description", "Failed to connect your email account")
            return render_template_string("""
                <!DOCTYPE html>
                <html lang="en">
                    <head>
                        <meta charset="UTF-8">
                        <meta name="viewport" content="width=device-width, initial-scale=1.0">
                        <title>Connection Failed - PropertyFlow</title>
                        <link rel="preconnect" href="https://fonts.googleapis.com">
                        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
                        <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
                        <style>
                            * { margin: 0; padding: 0; box-sizing: border-box; }
                            body {
                                font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                                background: linear-gradient(145deg, #0f172a 0%, #1e293b 50%, #334155 100%);
                                min-height: 100vh;
                                display: flex;
                                align-items: center;
                                justify-content: center;
                                padding: 20px;
                            }
                            .container {
                                background: white;
                                border-radius: 16px;
                                padding: 48px;
                                text-align: center;
                                max-width: 420px;
                                width: 100%;
                                box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
                            }
                            .error-icon {
                                width: 64px;
                                height: 64px;
                                background: #fef2f2;
                                border-radius: 50%;
                                display: flex;
                                align-items: center;
                                justify-content: center;
                                margin: 0 auto 24px;
                            }
                            .error-icon svg {
                                width: 32px;
                                height: 32px;
                                color: #dc2626;
                            }
                            h2 {
                                color: #0f172a;
                                margin-bottom: 8px;
                                font-size: 1.5rem;
                                font-weight: 700;
                                letter-spacing: -0.025em;
                            }
                            .error-message {
                                background: #fef2f2;
                                border: 1px solid #fecaca;
                                border-radius: 10px;
                                padding: 16px;
                                margin: 20px 0 24px;
                                color: #991b1b;
                                font-size: 0.875rem;
                                line-height: 1.5;
                            }
                            .btn {
                                display: inline-flex;
                                align-items: center;
                                gap: 8px;
                                padding: 14px 24px;
                                background: #0f172a;
                                color: white;
                                text-decoration: none;
                                border-radius: 10px;
                                font-weight: 600;
                                font-size: 0.9375rem;
                                transition: all 0.15s ease;
                            }
                            .btn:hover {
                                background: #1e293b;
                                transform: translateY(-1px);
                                box-shadow: 0 4px 12px rgba(15, 23, 42, 0.15);
                            }
                            .btn svg {
                                width: 18px;
                                height: 18px;
                            }
                        </style>
                    </head>
                    <body>
                        <div class="container">
                            <div class="error-icon">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                    <circle cx="12" cy="12" r="10"/>
                                    <line x1="15" y1="9" x2="9" y2="15"/>
                                    <line x1="9" y1="9" x2="15" y2="15"/>
                                </svg>
                            </div>
                            <h2>Connection Failed</h2>
                            <div class="error-message">{{ error }}</div>
                            <a href="/?uid={{ uid }}" class="btn">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                    <line x1="19" y1="12" x2="5" y2="12"/>
                                    <polyline points="12 19 5 12 12 5"/>
                                </svg>
                                Try Again
                            </a>
                        </div>
                    </body>
                </html>
            """, error=error, uid=uid)

    except Exception as e:
        print(f"[CALLBACK] Exception: {str(e)}")
        uid = request.args.get("state", "web_user")  # Try to get UID for error page
        return render_template_string("""
            <!DOCTYPE html>
            <html lang="en">
                <head>
                    <meta charset="UTF-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1.0">
                    <title>Connection Error - PropertyFlow</title>
                    <link rel="preconnect" href="https://fonts.googleapis.com">
                    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
                    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
                    <style>
                        * { margin: 0; padding: 0; box-sizing: border-box; }
                        body {
                            font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                            background: linear-gradient(145deg, #0f172a 0%, #1e293b 50%, #334155 100%);
                            min-height: 100vh;
                            display: flex;
                            align-items: center;
                            justify-content: center;
                            padding: 20px;
                        }
                        .container {
                            background: white;
                            border-radius: 16px;
                            padding: 48px;
                            text-align: center;
                            max-width: 420px;
                            width: 100%;
                            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
                        }
                        .error-icon {
                            width: 64px;
                            height: 64px;
                            background: #fef2f2;
                            border-radius: 50%;
                            display: flex;
                            align-items: center;
                            justify-content: center;
                            margin: 0 auto 24px;
                        }
                        .error-icon svg {
                            width: 32px;
                            height: 32px;
                            color: #dc2626;
                        }
                        h2 {
                            color: #0f172a;
                            margin-bottom: 8px;
                            font-size: 1.5rem;
                            font-weight: 700;
                            letter-spacing: -0.025em;
                        }
                        .error-message {
                            background: #fef2f2;
                            border: 1px solid #fecaca;
                            border-radius: 10px;
                            padding: 16px;
                            margin: 20px 0 24px;
                            color: #991b1b;
                            font-size: 0.875rem;
                            line-height: 1.5;
                            word-break: break-word;
                        }
                        .btn {
                            display: inline-flex;
                            align-items: center;
                            gap: 8px;
                            padding: 14px 24px;
                            background: #0f172a;
                            color: white;
                            text-decoration: none;
                            border-radius: 10px;
                            font-weight: 600;
                            font-size: 0.9375rem;
                            transition: all 0.15s ease;
                        }
                        .btn:hover {
                            background: #1e293b;
                            transform: translateY(-1px);
                            box-shadow: 0 4px 12px rgba(15, 23, 42, 0.15);
                        }
                        .btn svg {
                            width: 18px;
                            height: 18px;
                        }
                    </style>
                </head>
                <body>
                    <div class="container">
                        <div class="error-icon">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <circle cx="12" cy="12" r="10"/>
                                <line x1="12" y1="8" x2="12" y2="12"/>
                                <line x1="12" y1="16" x2="12.01" y2="16"/>
                            </svg>
                        </div>
                        <h2>Connection Error</h2>
                        <div class="error-message">{{ error }}</div>
                        <a href="/?uid={{ uid }}" class="btn">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <line x1="19" y1="12" x2="5" y2="12"/>
                                <polyline points="12 19 5 12 12 5"/>
                            </svg>
                            Try Again
                        </a>
                    </div>
                </body>
            </html>
        """, error=str(e), uid=uid)


# ---------------------------------------------------------------------------
# Rail 4 — Dead-letter visibility
#
# Dead-letters store the human-readable failure under `failureReason` /
# `lastError` (email.py:2213-2266, pending_responses.py:32); the legacy `reason`
# key is a last-resort fallback. Reading `reason` alone rendered every item
# blank, so an operator saw the stuck item but not why it failed.
#
# The queue also had no active alert: systemHealth tops out at "warning" on
# backlog, so a growing pile of stuck/misdirected sends read as routine depth.
# The inspect view now raises an error-severity alert when active dead-letter
# items are present. Fail-closed: the threshold defaults to 1 and can never be
# configured below 1, and a read failure forces the alert rather than reporting
# all-clear.
# ---------------------------------------------------------------------------

_DEAD_LETTER_REASON_KEYS = ("failureReason", "lastError", "reason")


def _dead_letter_reason(data):
    """Human-readable failure reason for a dead-letter / pending doc.

    Falls back across every key a dead-letter may store the message under so
    the debug view never renders blank when a reason exists.
    """
    for key in _DEAD_LETTER_REASON_KEYS:
        value = (data or {}).get(key)
        if value:
            return str(value)
    return ""


def _dead_letter_alert_threshold():
    """Active dead-letter count at/above which the inspect view alerts.

    Defaults to 1 (any active item pages). Clamped to a minimum of 1 so an
    absent, zero, negative, or corrupt env value can NEVER silently disable the
    rail — the SAFE behavior is the default.
    """
    raw = os.environ.get("DEAD_LETTER_ALERT_THRESHOLD")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 1
    return value if value >= 1 else 1


def _dead_letter_alert(active_count, needs_reconciliation_count, threshold=None, read_error=False):
    """Build an operator alert for the dead-letter / reconciliation backlog.

    Returns an error-severity alert dict when the queue could not be read
    (fail-closed) or when active items reach the threshold; otherwise ``None``.
    """
    if threshold is None:
        threshold = _dead_letter_alert_threshold()

    if read_error:
        return {
            "severity": "error",
            "activeDeadLetters": active_count,
            "needsReconciliation": needs_reconciliation_count,
            "threshold": threshold,
            "readError": True,
            "message": (
                "Dead-letter queue could not be read — visibility degraded; "
                "treat as needing operator attention."
            ),
        }

    if active_count > 0 and active_count >= threshold:
        return {
            "severity": "error",
            "activeDeadLetters": active_count,
            "needsReconciliation": needs_reconciliation_count,
            "threshold": threshold,
            "readError": False,
            "message": (
                f"{active_count} active dead-letter item(s) require operator "
                f"attention ({needs_reconciliation_count} awaiting reconciliation)."
            ),
        }
    return None


@app.route("/api/firestore-inspect", methods=["GET"])
@verify_firebase_token
def api_firestore_inspect():
    """
    Inspect Firestore database structure and count documents.

    Restricted to the AUTHENTICATED caller's OWN account only — previously this
    iterated list_user_ids() and dumped every tenant's client names, outbox
    subjects and dead-letter reasons to any unauthenticated request.
    """
    if not SCHEDULER_AVAILABLE:
        return jsonify({"success": False, "error": "Scheduler not available"}), 503

    caller_uid = _safe_uid(g.get("firebase_uid"))
    if caller_uid is None:
        return jsonify({"success": False, "error": _GENERIC_BAD_REQUEST}), 400

    try:
        from email_automation.clients import _fs
        from email_automation.system_health import _is_resolved_dead_letter

        result = {"users": {}}
        # Only ever inspect the caller's own subtree.
        users = [caller_uid]

        # Rail 4 aggregates across all users.
        total_active_dead_letters = 0
        total_needs_reconciliation = 0
        dead_letter_read_error = False

        for uid in users:
            user_data = {"collections": {}}

            subcollections = [
                "clients",
                "archivedClients",
                "threads",
                "msgIndex",
                "convIndex",
                "outbox",
                "deadLetterQueue",
                "processedMessages",
                "sheetChangeLog",
                "optedOutContacts",
                "sync"
            ]

            for coll_name in subcollections:
                try:
                    coll_ref = _fs.collection("users").document(uid).collection(coll_name)
                    docs = list(coll_ref.limit(500).stream())
                    if docs:
                        coll_data = {"count": len(docs), "sample_ids": [d.id[:30] for d in docs[:5]]}

                        # Add more detail for specific collections
                        if coll_name == "outbox":
                            coll_data["items"] = [{"id": d.id, "subject": d.to_dict().get("subject", "")[:50]} for d in docs]
                        elif coll_name == "deadLetterQueue":
                            dl_items = []
                            active_count = 0
                            needs_recon_count = 0
                            for d in docs:
                                dd = d.to_dict() or {}
                                status = str(dd.get("status") or "").strip().lower()
                                resolved = _is_resolved_dead_letter(dd)
                                if not resolved:
                                    active_count += 1
                                if status == "needs_reconciliation":
                                    needs_recon_count += 1
                                dl_items.append({
                                    "id": d.id,
                                    "reason": _dead_letter_reason(dd)[:200],
                                    "status": dd.get("status", ""),
                                    "resolved": resolved,
                                })
                            coll_data["items"] = dl_items
                            coll_data["activeCount"] = active_count
                            coll_data["needsReconciliation"] = needs_recon_count
                            total_active_dead_letters += active_count
                            total_needs_reconciliation += needs_recon_count
                        elif coll_name == "clients":
                            coll_data["items"] = [{"id": d.id, "name": d.to_dict().get("name", "")} for d in docs]

                        user_data["collections"][coll_name] = coll_data
                except Exception as e:
                    user_data["collections"][coll_name] = {"error": str(e)}
                    # Fail-closed: a queue we could not read must NOT read as all-clear.
                    if coll_name == "deadLetterQueue":
                        dead_letter_read_error = True

            # Count notifications across all clients
            try:
                clients_ref = _fs.collection("users").document(uid).collection("clients")
                clients = list(clients_ref.stream())
                total_notifs = 0
                notif_details = []
                for client in clients:
                    notifs_ref = _fs.collection("users").document(uid).collection("clients").document(client.id).collection("notifications")
                    notifs = list(notifs_ref.stream())
                    if notifs:
                        total_notifs += len(notifs)
                        notif_details.append({"client": client.id, "count": len(notifs)})
                if total_notifs > 0:
                    user_data["collections"]["notifications_total"] = {"count": total_notifs, "by_client": notif_details}
            except Exception as e:
                user_data["collections"]["notifications_total"] = {"error": str(e)}

            result["users"][uid] = user_data

        result["alert"] = _dead_letter_alert(
            total_active_dead_letters,
            total_needs_reconciliation,
            read_error=dead_letter_read_error,
        )

        return jsonify({"success": True, "data": result})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/firestore-cleanup", methods=["POST"])
@verify_firebase_token(check_revoked=True)
def api_firestore_cleanup():
    """
    Clean up stale Firestore data.
    Accepts JSON body with options:
    - clear_dead_letter: bool - Clear dead letter queue
    - clear_processed_messages: bool - Clear processed messages log
    - clear_sheet_change_log: bool - Clear sheet change log (older than 30 days)
    - clear_old_threads: int - Clear threads older than N days (0 = don't clear)
    - user_id: str - Specific user ID (must equal the verified caller)
    """
    if not _destructive_admin_routes_enabled():
        return jsonify({"success": False, "error": "Destructive admin route disabled"}), 403

    if not SCHEDULER_AVAILABLE:
        return jsonify({"success": False, "error": "Scheduler not available"}), 503

    data, err = _require_json_object()
    if err:
        return err

    # Destructive flags must be STRICT booleans — a string like "false"/"0"/"off"
    # (or any non-empty string) must NEVER trigger a deletion. Only real `true`.
    clear_dead_letter = data.get("clear_dead_letter", False) is True
    clear_processed = data.get("clear_processed_messages", False) is True
    clear_changelog = data.get("clear_sheet_change_log", False) is True

    # clear_old_threads must be a real (non-bool) positive int; any other type
    # (str/list/dict/bool) is treated as "don't clear" rather than crashing.
    _raw_days = data.get("clear_old_threads", 0)
    clear_old_threads_days = _raw_days if (type(_raw_days) is int and _raw_days > 0) else 0

    # A blank / missing / non-string user_id must be REJECTED — never silently
    # fanned out across every account by falling back to list_user_ids().
    target_user = data.get("user_id")
    if not _is_nonempty_str(target_user):
        return jsonify({"success": False, "error": "user_id is required"}), 400

    # A destructive cleanup may only target the verified caller's own data — a
    # signed-in user must not be able to wipe another tenant's queues/threads by
    # naming their uid in the body.
    caller_uid = _safe_uid(g.get("firebase_uid"))
    if caller_uid is None or target_user != caller_uid:
        return jsonify({"success": False, "error": "Forbidden"}), 403

    try:
        from email_automation.clients import _fs, list_user_ids
        from datetime import datetime, timedelta

        users = [target_user]
        results = {}

        for uid in users:
            user_results = {}

            # Clear dead letter queue
            if clear_dead_letter:
                try:
                    dl_ref = _fs.collection("users").document(uid).collection("deadLetterQueue")
                    docs = list(dl_ref.stream())
                    for doc in docs:
                        doc.reference.delete()
                    user_results["deadLetterQueue"] = f"Deleted {len(docs)} docs"
                except Exception as e:
                    user_results["deadLetterQueue"] = f"Error: {e}"

            # Clear processed messages
            if clear_processed:
                try:
                    pm_ref = _fs.collection("users").document(uid).collection("processedMessages")
                    docs = list(pm_ref.stream())
                    for doc in docs:
                        doc.reference.delete()
                    user_results["processedMessages"] = f"Deleted {len(docs)} docs"
                except Exception as e:
                    user_results["processedMessages"] = f"Error: {e}"

            # Clear old sheet change log (older than 30 days)
            if clear_changelog:
                try:
                    cl_ref = _fs.collection("users").document(uid).collection("sheetChangeLog")
                    docs = list(cl_ref.stream())
                    cutoff = datetime.now() - timedelta(days=30)
                    deleted = 0
                    for doc in docs:
                        doc_data = doc.to_dict()
                        created_at = doc_data.get("createdAt")
                        if created_at:
                            # Handle Firestore timestamp
                            if hasattr(created_at, 'timestamp'):
                                doc_time = datetime.fromtimestamp(created_at.timestamp())
                            else:
                                doc_time = datetime.now()  # Keep if can't parse
                            if doc_time < cutoff:
                                doc.reference.delete()
                                deleted += 1
                        else:
                            # No timestamp, delete it
                            doc.reference.delete()
                            deleted += 1
                    user_results["sheetChangeLog"] = f"Deleted {deleted} old docs (kept {len(docs) - deleted})"
                except Exception as e:
                    user_results["sheetChangeLog"] = f"Error: {e}"

            # Clear old threads
            if clear_old_threads_days > 0:
                try:
                    threads_ref = _fs.collection("users").document(uid).collection("threads")
                    docs = list(threads_ref.stream())
                    cutoff = datetime.now() - timedelta(days=clear_old_threads_days)
                    deleted = 0
                    for doc in docs:
                        doc_data = doc.to_dict()
                        updated_at = doc_data.get("updatedAt") or doc_data.get("createdAt")
                        if updated_at:
                            if hasattr(updated_at, 'timestamp'):
                                doc_time = datetime.fromtimestamp(updated_at.timestamp())
                            else:
                                doc_time = datetime.now()
                            if doc_time < cutoff:
                                # Also delete messages subcollection
                                msgs_ref = doc.reference.collection("messages")
                                for msg in msgs_ref.stream():
                                    msg.reference.delete()
                                doc.reference.delete()
                                deleted += 1
                    user_results["threads"] = f"Deleted {deleted} old threads (kept {len(docs) - deleted})"
                except Exception as e:
                    user_results["threads"] = f"Error: {e}"

            results[uid] = user_results

        return jsonify({"success": True, "results": results})

    except Exception as e:
        print(f"❌ Firestore cleanup failed: {type(e).__name__}: {e}", flush=True)
        return jsonify({"success": False, "error": "Firestore cleanup failed"}), 500


@app.route("/api/clear-outlook-emails", methods=["POST"])
@verify_firebase_token(check_revoked=True)
def api_clear_outlook_emails():
    """
    Clear campaign-related emails from Outlook (SentItems and optionally Inbox).

    Destructive real-mail deletion: gated by BOTH the admin env flag AND a
    verified Firebase token, and scoped to the AUTHENTICATED caller's own
    mailbox (body `user_id` is ignored). Previously a single admin-flag-on
    environment allowed an unauthenticated POST to delete real Outlook messages
    from an arbitrary victim's mailbox, with an unvalidated `keywords` that let
    a bare string over-match (`'a' in subject`).

    Accepts JSON body with options:
    - keywords: list[str] - Subject keywords to match (e.g., ["Commerce"])
    - clear_inbox: bool - Also clear from Inbox (default False)
    - clear_sent: bool - Clear from SentItems (default True)
    """
    if not _destructive_admin_routes_enabled():
        return jsonify({"success": False, "error": "Destructive admin route disabled"}), 403

    if not SCHEDULER_AVAILABLE:
        return jsonify({"success": False, "error": "Scheduler not available"}), 503

    data, err = _require_json_object()
    if err:
        return err

    # Identity is the verified token uid only — a caller can only clear their
    # OWN mailbox, never an arbitrary victim's.
    user_id = _safe_uid(g.get("firebase_uid"))
    if user_id is None:
        return jsonify({"success": False, "error": _GENERIC_BAD_REQUEST}), 400

    # keywords must be a list of non-empty strings, bounded in length. A bare
    # string ("a") would make `kw in subject` iterate characters (over-broad,
    # deletes almost everything); a non-iterable would raise -> 500.
    _DEFAULT_KEYWORDS = ["Logistics", "Commerce", "Industrial", "Warehouse", "Distribution", "Storage"]
    if "keywords" not in data:
        keywords = _DEFAULT_KEYWORDS
    else:
        keywords = data.get("keywords")
        if not isinstance(keywords, list) or len(keywords) > 100:
            return jsonify({"success": False, "error": "keywords must be a list of up to 100 strings"}), 400
        if not all(_is_nonempty_str(kw) for kw in keywords):
            return jsonify({"success": False, "error": "keywords must be non-empty strings"}), 400

    clear_inbox = data.get("clear_inbox", False) is True
    clear_sent = data.get("clear_sent", True) is True

    try:
        import requests
        from firebase_helpers import download_token
        from msal import ConfidentialClientApplication, SerializableTokenCache

        # Download and setup token
        download_token(FIREBASE_API_KEY, output_file=TOKEN_CACHE, user_id=user_id)
        cache = SerializableTokenCache()
        with open(TOKEN_CACHE, "r") as f:
            cache.deserialize(f.read())

        app_obj = ConfidentialClientApplication(
            CLIENT_ID,
            client_credential=CLIENT_SECRET,
            authority=AUTHORITY,
            token_cache=cache
        )

        accounts = app_obj.get_accounts()
        if not accounts:
            return jsonify({"success": False, "error": "No account found in token cache"}), 404

        result = app_obj.acquire_token_silent(SCOPES, account=accounts[0])
        if not result or "access_token" not in result:
            return jsonify({"success": False, "error": "Failed to acquire token"}), 401

        headers = {"Authorization": f"Bearer {result['access_token']}"}
        base = "https://graph.microsoft.com/v1.0"
        deleted = {"sent": 0, "inbox": 0}

        def delete_matching_emails(folder):
            nonlocal deleted
            folder_key = "inbox" if folder == "Inbox" else "sent"
            resp = requests.get(
                f"{base}/me/mailFolders/{folder}/messages",
                headers=headers,
                params={"$top": 100, "$select": "id,subject"}
            )
            if resp.status_code != 200:
                return

            for msg in resp.json().get("value", []):
                subj = msg.get("subject", "")
                if any(kw in subj for kw in keywords):
                    del_resp = requests.delete(f"{base}/me/messages/{msg['id']}", headers=headers)
                    if del_resp.status_code in [200, 204]:
                        deleted[folder_key] += 1
                        print(f"Deleted from {folder}: {subj[:50]}")

        if clear_sent:
            delete_matching_emails("SentItems")
        if clear_inbox:
            delete_matching_emails("Inbox")

        return jsonify({
            "success": True,
            "deleted": deleted,
            "keywords_matched": keywords
        })

    except Exception as e:
        print(f"❌ Clear Outlook emails failed: {type(e).__name__}: {e}", flush=True)
        return jsonify({"success": False, "error": "Failed to clear Outlook emails"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
