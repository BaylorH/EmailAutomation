import os
import sys
import subprocess
import json
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session
from flask_cors import CORS
from msal import ConfidentialClientApplication, SerializableTokenCache
from firebase_helpers import upload_token
import threading
import time

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
    

app = Flask(__name__)

# Enable CORS for all routes and origins
CORS(app, origins=["http://localhost:3000", "https://your-frontend-domain.com", "*"])

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

# Get the base URL for redirect URI
def get_base_url():
    if "https://email-token-manager.onrender.com" == request.url_root.rstrip('/'):
        return "https://email-token-manager.onrender.com"
    else:
        return "NOT SAME"

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

def refresh_and_process_user(user_id: str):
    """Process a single user - same logic as main.py"""
    if not SCHEDULER_AVAILABLE:
        return {"success": False, "error": "Scheduler functionality not available - missing dependencies"}
    
    print(f"\n🔄 Processing user: {user_id}")
    
    try:
        from firebase_helpers import download_token, upload_token
        import atexit
        
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

        app_obj = ConfidentialClientApplication(
            CLIENT_ID,
            client_credential=CLIENT_SECRET,
            authority=AUTHORITY,
            token_cache=cache
        )

        accounts = app_obj.get_accounts()
        if not accounts:
            print(f"⚠️ No account found for {user_id}")
            return {"success": False, "error": f"No account found for {user_id}"}

        # Try to get access token
        before_state = cache.has_state_changed
        result = app_obj.acquire_token_silent(SCOPES, account=accounts[0])
        after_state = cache.has_state_changed

        if not result or "access_token" not in result:
            print(f"❌ Silent auth failed for {user_id}")
            return {"success": False, "error": f"Silent auth failed for {user_id}"}

        access_token = result["access_token"]

        # Helpful logging
        token_source = "refreshed_via_refresh_token" if (not before_state and after_state) else "cached_access_token"
        exp_secs = result.get("expires_in")
        print(f"🎯 Using {token_source}; expires_in≈{exp_secs}s – preview: {access_token[:40]}")

        # Optional sanity check on JWT-shaped token & appid
        if access_token.count(".") == 2:
            decoded = decode_token_payload(access_token)
            appid = decoded.get("appid", "unknown")
            if not appid.startswith("54cec"):
                print(f"⚠️ Unexpected appid: {appid}")
            else:
                print("✅ Token appid matches expected prefix")

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

        # Process outbound emails
        send_outboxes(user_id, headers)
        
        # Scan for reply matches
        print(f"\n🔍 Scanning inbox for replies...")
        scan_inbox_against_index(user_id, headers, only_unread=True, top=50)
        
        return {"success": True, "message": f"Successfully processed user {user_id}"}
        
    except Exception as e:
        error_msg = f"Error processing user {user_id}: {str(e)}"
        print(f"💥 {error_msg}")
        return {"success": False, "error": error_msg}

def run_scheduler():
    """Run the full scheduler for all users - same logic as main.py"""
    if not SCHEDULER_AVAILABLE:
        return {"success": False, "error": "Scheduler functionality not available - missing dependencies"}
    
    try:
        all_users = list_user_ids()
        print(f"📦 Found {len(all_users)} token cache users: {all_users}")
        
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
        print(f"💥 {error_msg}")
        return {"success": False, "error": error_msg}

@app.route("/")
def index():
    uid = request.args.get("uid", "web_user")
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
                    window.location.href = '/auth/login';
                }
            </script>
        </body>
    </html>
    """, status=status, base_url=base_url, uid=uid, upload_result=upload_result)

@app.route("/api/status")
def api_status():
    return jsonify(check_token_status())

@app.route("/api/upload", methods=["POST"])
def api_upload():
    try:
        uid = session.get("uid", "web_user") 
        print(f"[UPLOAD] Upload requested for UID: {uid}")
        
        user_dir = f"msal_caches/{uid}" 
        cache_file = f"{user_dir}/msal_token_cache.bin" 
        os.makedirs(user_dir, exist_ok=True)
        if not os.path.exists(cache_file):
            return jsonify({"error": "No token cache file found"})
        
        upload_token(FIREBASE_API_KEY, input_file=cache_file, user_id=session.get("uid", "web_user"))
        return jsonify({"success": True, "message": "Token uploaded to Firebase"})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/clear", methods=["POST"])
def api_clear():
    try:
        uid = session.get("uid", "web_user") 
        user_dir = f"msal_caches/{uid}" 
        cache_file = f"{user_dir}/msal_token_cache.bin" 
        os.makedirs(user_dir, exist_ok=True)
        if os.path.exists(cache_file):
            os.remove(cache_file)
        return jsonify({"success": True, "message": "Token cache cleared"})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    try:
        uid = session.get("uid", "web_user") 
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
            return jsonify({"error": "No accounts found"})
        
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
            return jsonify({"error": f"Failed to refresh token: {result.get('error_description', 'Unknown error')}"})
    
    except Exception as e:
        return jsonify({"error": str(e)})

# Global variable to track scheduler status
scheduler_status = {"running": False, "last_run": None, "last_result": None}

@app.route("/api/trigger-scheduler", methods=["POST"])
def api_trigger_scheduler():
    """
    API endpoint to manually trigger the email scheduler.
    This runs the same logic as the GitHub Actions workflow.
    """
    global scheduler_status
    
    # Check if scheduler functionality is available
    if not SCHEDULER_AVAILABLE:
        return jsonify({
            "success": False,
            "error": "Scheduler functionality not available - missing required environment variables or dependencies"
        }), 503
    
    # Check if scheduler is already running
    if scheduler_status["running"]:
        return jsonify({
            "success": False, 
            "error": "Scheduler is already running",
            "status": scheduler_status
        }), 409
    
    # Optional: Add basic authentication
    auth_header = request.headers.get('Authorization')
    api_key = request.headers.get('X-API-Key')
    
    # You can add your own API key validation here
    # For now, we'll allow any request, but you should add security
    
    def run_scheduler_async():
        """Run scheduler in background thread"""
        global scheduler_status
        try:
            scheduler_status["running"] = True
            scheduler_status["last_run"] = datetime.now().isoformat()
            
            print("🚀 Manual scheduler trigger initiated")
            result = run_scheduler()
            
            scheduler_status["last_result"] = result
            scheduler_status["running"] = False
            
            print(f"✅ Manual scheduler completed: {result}")
            
        except Exception as e:
            scheduler_status["last_result"] = {"success": False, "error": str(e)}
            scheduler_status["running"] = False
            print(f"💥 Manual scheduler failed: {e}")
    
    # Start scheduler in background thread
    thread = threading.Thread(target=run_scheduler_async)
    thread.daemon = True
    thread.start()
    
    return jsonify({
        "success": True,
        "message": "Scheduler started successfully",
        "status": "running",
        "started_at": datetime.now().isoformat()
    })

@app.route("/api/scheduler-status", methods=["GET"])
def api_scheduler_status():
    """Get the current status of the scheduler"""
    global scheduler_status
    
    # Debug information
    env_vars = {
        "FIREBASE_API_KEY": "✅" if os.getenv("FIREBASE_API_KEY") else "❌",
        "AZURE_API_APP_ID": "✅" if os.getenv("AZURE_API_APP_ID") else "❌", 
        "OPENAI_API_KEY": "✅" if os.getenv("OPENAI_API_KEY") else "❌",
        "AZURE_API_CLIENT_SECRET": "✅" if os.getenv("AZURE_API_CLIENT_SECRET") else "❌",
        "AZURE_CLIENT_SECRET": "✅" if os.getenv("AZURE_CLIENT_SECRET") else "❌"
    }
    
    return jsonify({
        **scheduler_status,
        "scheduler_available": SCHEDULER_AVAILABLE,
        "debug_env_vars": env_vars,
        "import_error": globals().get('IMPORT_ERROR', 'No import error recorded')
    })

@app.route("/api/decline-property", methods=["POST"])
def api_decline_property():
    """
    Delete a property row from a Google Sheet when user declines a new property suggestion.
    Expects JSON body: { uid, clientId, rowNumber, sheetId }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No JSON data provided"}), 400

        uid = data.get("uid")
        client_id = data.get("clientId")
        row_number = data.get("rowNumber")
        sheet_id = data.get("sheetId")

        if not all([uid, client_id, row_number, sheet_id]):
            return jsonify({"success": False, "error": "Missing required fields: uid, clientId, rowNumber, sheetId"}), 400

        # Import sheets client
        from email_automation.clients import _sheets_client
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
        print(f"❌ Failed to decline property: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/check-sheet-completion", methods=["POST"])
def api_check_sheet_completion():
    """
    Check if all rows in a sheet have all required fields filled.
    Returns completion status and details.
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No JSON data provided"}), 400

        sheet_id = data.get("sheetId")
        if not sheet_id:
            return jsonify({"success": False, "error": "Missing sheetId"}), 400

        from email_automation.clients import _sheets_client
        from email_automation.sheets import _get_first_tab_title, _read_header_row2, _header_index_map
        from email_automation.app_config import REQUIRED_FIELDS_FOR_CLOSE

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
        print(f"❌ Failed to check sheet completion: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/debug-inbox", methods=["GET"])
def api_debug_inbox():
    """Debug endpoint to check inbox status and email processing"""
    if not SCHEDULER_AVAILABLE:
        return jsonify({"error": "Scheduler functionality not available"}), 503
    
    try:
        from email_automation.clients import list_user_ids
        from firebase_helpers import download_token
        from msal import ConfidentialClientApplication, SerializableTokenCache
        import requests
        from datetime import datetime, timedelta, timezone
        
        # Get first user for debugging
        user_ids = list_user_ids()
        if not user_ids:
            return jsonify({"error": "No users found"}), 404
        
        user_id = user_ids[0]
        
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
        
        # Check processed status for each email
        from email_automation.messaging import has_processed
        
        debug_info = {
            "user_id": user_id,
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
def api_debug_thread_matching():
    """Debug endpoint to check thread matching for specific conversation"""
    if not SCHEDULER_AVAILABLE:
        return jsonify({"error": "Scheduler functionality not available"}), 503
    
    try:
        from email_automation.clients import list_user_ids
        from email_automation.messaging import lookup_thread_by_conversation_id
        from firebase_helpers import download_token
        from msal import ConfidentialClientApplication, SerializableTokenCache
        import requests
        from datetime import datetime, timedelta, timezone
        
        # Get first user for debugging
        user_ids = list_user_ids()
        if not user_ids:
            return jsonify({"error": "No users found"}), 404
        
        user_id = user_ids[0]
        
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

# Web-based authentication routes
@app.route("/auth/login")
def auth_login():
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
        redirect_uri="https://email-token-manager.onrender.com/auth/callback",
        state=uid  # This preserves the UID through the OAuth flow
    )
    
    print(f"[LOGIN] Redirecting to auth URL with state={uid}")
    return redirect(auth_url)

@app.route("/auth/callback")
def auth_callback():
    try:
        # CRITICAL: Get UID from state parameter (this is how it survives the redirect)
        uid = request.args.get("state", "web_user")
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
            redirect_uri="https://email-token-manager.onrender.com/auth/callback"
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)