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

# Try to import scheduler logic - completely optional
SCHEDULER_AVAILABLE = False
try:
    # Only try to import if we have the basic required env vars
    firebase_key = os.getenv("FIREBASE_API_KEY")
    azure_app_id = os.getenv("AZURE_API_APP_ID")
    
    print(f"🔍 Environment check: FIREBASE_API_KEY={'✅' if firebase_key else '❌'}, AZURE_API_APP_ID={'✅' if azure_app_id else '❌'}")
    
    if firebase_key and azure_app_id:
        print("🚀 Attempting to import scheduler modules...")
        
        # Set environment variables that app_config expects
        if not os.getenv("AZURE_API_CLIENT_SECRET"):
            os.environ["AZURE_API_CLIENT_SECRET"] = os.getenv("AZURE_CLIENT_SECRET", "")
        
        from email_automation.clients import list_user_ids, decode_token_payload
        from email_automation.email import send_outboxes
        from email_automation.processing import scan_inbox_against_index
        SCHEDULER_AVAILABLE = True
        print("✅ Scheduler functionality available")
    else:
        print("⚠️ Scheduler functionality disabled - missing environment variables")
        print(f"   FIREBASE_API_KEY: {'present' if firebase_key else 'MISSING'}")
        print(f"   AZURE_API_APP_ID: {'present' if azure_app_id else 'MISSING'}")
except (ImportError, RuntimeError) as e:
    print(f"⚠️ Scheduler functionality not available: {e}")
    print(f"⚠️ Import error details: {type(e).__name__}: {str(e)}")

# Define dummy functions if scheduler not available
if not SCHEDULER_AVAILABLE:
    def list_user_ids():
        return []
    
    def decode_token_payload(token):
        return {}
    
    def send_outboxes(user_id, headers):
        return {"success": False, "error": "Scheduler not available"}
    
    def scan_inbox_against_index(user_id, headers, only_unread=True, top=50):
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
            <title>Email Access Setup</title>
            <style>
                * {
                    margin: 0;
                    padding: 0;
                    box-sizing: border-box;
                }
                
                body {
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    min-height: 100vh;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 20px;
                }
                
                .container {
                    background: white;
                    border-radius: 16px;
                    box-shadow: 0 20px 40px rgba(0,0,0,0.1);
                    padding: 40px;
                    max-width: 500px;
                    width: 100%;
                    text-align: center;
                }
                
                .header {
                    margin-bottom: 30px;
                }
                
                .header h1 {
                    color: #2d3748;
                    font-size: 28px;
                    font-weight: 600;
                    margin-bottom: 8px;
                }
                
                .header p {
                    color: #718096;
                    font-size: 16px;
                    line-height: 1.5;
                }
                
                .status-card {
                    padding: 20px;
                    border-radius: 12px;
                    margin: 20px 0;
                    text-align: left;
                }
                
                .status-connected {
                    background: linear-gradient(135deg, #48bb78, #38a169);
                    color: white;
                }
                
                .status-pending {
                    background: linear-gradient(135deg, #ed8936, #dd6b20);
                    color: white;
                }
                
                .status-card h3 {
                    font-size: 18px;
                    font-weight: 600;
                    margin-bottom: 8px;
                    display: flex;
                    align-items: center;
                    gap: 8px;
                }
                
                .status-card p {
                    margin-bottom: 6px;
                    opacity: 0.9;
                }
                
                .status-card .email {
                    font-weight: 500;
                    background: rgba(255,255,255,0.2);
                    padding: 4px 8px;
                    border-radius: 6px;
                    display: inline-block;
                    margin-top: 8px;
                }
                
                .connect-section {
                    background: #f7fafc;
                    border: 2px dashed #cbd5e0;
                    border-radius: 12px;
                    padding: 30px;
                    margin: 20px 0;
                }
                
                .connect-section h3 {
                    color: #2d3748;
                    font-size: 20px;
                    margin-bottom: 12px;
                }
                
                .connect-section p {
                    color: #4a5568;
                    margin-bottom: 20px;
                    line-height: 1.5;
                }
                
                .btn {
                    display: inline-block;
                    padding: 12px 24px;
                    font-size: 16px;
                    font-weight: 500;
                    text-decoration: none;
                    border-radius: 8px;
                    border: none;
                    cursor: pointer;
                    transition: all 0.2s;
                    margin: 8px;
                }
                
                .btn-primary {
                    background: linear-gradient(135deg, #4299e1, #3182ce);
                    color: white;
                }
                
                .btn-primary:hover {
                    transform: translateY(-2px);
                    box-shadow: 0 8px 16px rgba(66, 153, 225, 0.3);
                }
                
                .btn-danger {
                    background: linear-gradient(135deg, #f56565, #e53e3e);
                    color: white;
                }
                
                .btn-danger:hover {
                    transform: translateY(-2px);
                    box-shadow: 0 8px 16px rgba(245, 101, 101, 0.3);
                }
                
                .footer {
                    margin-top: 30px;
                    padding-top: 20px;
                    border-top: 1px solid #e2e8f0;
                    color: #718096;
                    font-size: 14px;
                }
                
                .icon {
                    font-size: 20px;
                }
                
                @media (max-width: 480px) {
                    .container {
                        padding: 30px 20px;
                    }
                    
                    .header h1 {
                        font-size: 24px;
                    }
                    
                    .btn {
                        width: 100%;
                        margin: 8px 0;
                    }
                }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>📧 Email Access Setup</h1>
                    <p>Connect your email account to enable automated email management</p>
                </div>
                
                {% if status.status == 'valid' %}
                    <div class="status-card status-connected">
                        <h3><span class="icon">✅</span> Email Access Connected</h3>
                        <p>Your email account is successfully connected and ready to use.</p>
                        <div class="email">{{ status.account }}</div>
                        {% if upload_result and upload_result.success %}
                        <p style="margin-top: 12px; font-size: 14px;">
                            <span class="icon">☁️</span> Securely synced to cloud
                        </p>
                        {% endif %}
                    </div>
                    
                    <!-- <button class="btn btn-danger" onclick="disconnectEmail()">
                        🔓 Disconnect Email Access
                    </button> -->
                {% else %}
                    <div class="status-card status-pending">
                        <h3><span class="icon">⏳</span> Email Access Required</h3>
                        <p>To use automated email features, please connect your email account.</p>
                    </div>
                    
                    <div class="connect-section">
                        <h3>🔐 Connect Your Email</h3>
                        <p>Click below to securely connect your Microsoft email account. You'll be redirected to Microsoft's secure login page.</p>
                        <button class="btn btn-primary" onclick="connectEmail()">
                            Connect Email Account
                        </button>
                    </div>
                {% endif %}
                
                <div class="footer">
                    <p>🔒 Your email credentials are never stored. Only secure access tokens are used.</p>
                </div>
            </div>
            
            <script>
                function connectEmail() {
                    window.location.href = '/auth/login';
                }
                
                async function disconnectEmail() {
                    if (confirm('Are you sure you want to disconnect your email account? You will need to reconnect it to use email features.')) {
                        try {
                            const response = await fetch('/api/clear', { method: 'POST' });
                            await response.json();
                            setTimeout(() => location.reload(), 1000);
                        } catch (error) {
                            console.error('Error:', error);
                        }
                    }
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
        "debug_env_vars": env_vars
    })

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
                        <title>Connection Failed</title>
                        <style>
                            body {
                                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                                min-height: 100vh;
                                display: flex;
                                align-items: center;
                                justify-content: center;
                                padding: 20px;
                            }
                            .container {
                                background: white;
                                border-radius: 16px;
                                padding: 40px;
                                text-align: center;
                                max-width: 400px;
                                box-shadow: 0 20px 40px rgba(0,0,0,0.1);
                            }
                            h2 { color: #e53e3e; margin-bottom: 16px; }
                            p { color: #4a5568; margin-bottom: 24px; }
                            .btn {
                                display: inline-block;
                                padding: 12px 24px;
                                background: linear-gradient(135deg, #4299e1, #3182ce);
                                color: white;
                                text-decoration: none;
                                border-radius: 8px;
                                font-weight: 500;
                            }
                        </style>
                    </head>
                    <body>
                        <div class="container">
                            <h2>❌ Connection Failed</h2>
                            <p>{{ error }}</p>
                            <a href="/?uid={{ uid }}" class="btn">← Try Again</a>
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
                    <title>Connection Successful</title>
                    <style>
                        body {
                            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                            min-height: 100vh;
                            display: flex;
                            align-items: center;
                            justify-content: center;
                            padding: 20px;
                        }
                        .container {
                            background: white;
                            border-radius: 16px;
                            padding: 40px;
                            text-align: center;
                            max-width: 400px;
                            box-shadow: 0 20px 40px rgba(0,0,0,0.1);
                        }
                        h2 { 
                            color: #38a169; 
                            margin-bottom: 16px;
                            font-size: 24px;
                        }
                        .email {
                            background: #f7fafc;
                            padding: 8px 16px;
                            border-radius: 8px;
                            color: #2d3748;
                            font-weight: 500;
                            margin: 16px 0;
                        }
                        .spinner {
                            width: 40px;
                            height: 40px;
                            border: 4px solid #e2e8f0;
                            border-top: 4px solid #4299e1;
                            border-radius: 50%;
                            animation: spin 1s linear infinite;
                            margin: 20px auto;
                        }
                        @keyframes spin {
                            0% { transform: rotate(0deg); }
                            100% { transform: rotate(360deg); }
                        }
                        p { color: #4a5568; margin-bottom: 16px; }
                    </style>
                </head>
                <body>
                    <div class="container">
                        <h2>✅ Successfully Connected!</h2>
                        <div class="email">{{ account }}</div>
                        <p>Your email access has been set up successfully.</p>
                        <p>Completing setup...</p>
                        <div class="spinner"></div>
                    </div>
                    <script>
                        setTimeout(() => window.location.href = '/?uid={{ uid }}', 3000);
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
                        <title>Connection Failed</title>
                        <style>
                            body {
                                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                                min-height: 100vh;
                                display: flex;
                                align-items: center;
                                justify-content: center;
                                padding: 20px;
                            }
                            .container {
                                background: white;
                                border-radius: 16px;
                                padding: 40px;
                                text-align: center;
                                max-width: 400px;
                                box-shadow: 0 20px 40px rgba(0,0,0,0.1);
                            }
                            h2 { color: #e53e3e; margin-bottom: 16px; }
                            p { color: #4a5568; margin-bottom: 24px; }
                            .btn {
                                display: inline-block;
                                padding: 12px 24px;
                                background: linear-gradient(135deg, #4299e1, #3182ce);
                                color: white;
                                text-decoration: none;
                                border-radius: 8px;
                                font-weight: 500;
                            }
                        </style>
                    </head>
                    <body>
                        <div class="container">
                            <h2>❌ Connection Failed</h2>
                            <p>{{ error }}</p>
                            <a href="/?uid={{ uid }}" class="btn">← Try Again</a>
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
                    <title>Connection Error</title>
                    <style>
                        body {
                            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                            min-height: 100vh;
                            display: flex;
                            align-items: center;
                            justify-content: center;
                            padding: 20px;
                        }
                        .container {
                            background: white;
                            border-radius: 16px;
                            padding: 40px;
                            text-align: center;
                            max-width: 400px;
                            box-shadow: 0 20px 40px rgba(0,0,0,0.1);
                        }
                        h2 { color: #e53e3e; margin-bottom: 16px; }
                        p { color: #4a5568; margin-bottom: 24px; }
                        .btn {
                            display: inline-block;
                            padding: 12px 24px;
                            background: linear-gradient(135deg, #4299e1, #3182ce);
                            color: white;
                            text-decoration: none;
                            border-radius: 8px;
                            font-weight: 500;
                        }
                    </style>
                </head>
                <body>
                    <div class="container">
                        <h2>❌ Connection Error</h2>
                        <p>{{ error }}</p>
                        <a href="/?uid={{ uid }}" class="btn">← Try Again</a>
                    </div>
                </body>
            </html>
        """, error=str(e), uid=uid)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)