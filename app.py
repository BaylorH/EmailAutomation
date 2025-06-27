import os
import sys
import subprocess
import json
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session
from msal import ConfidentialClientApplication, SerializableTokenCache
from firebase_helpers import upload_token

app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY", "some-default-secret")

app.config.update(
    SESSION_COOKIE_SECURE=True,        # ensures cookie is sent over HTTPS
    SESSION_COOKIE_SAMESITE='Lax',     # allows session to persist during OAuth redirect
)

CLIENT_ID        = os.getenv("AZURE_API_APP_ID")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
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
    <html>
        <head>
            <title>📧 Email Token Manager</title>
            <style>
                body { font-family: sans-serif; padding: 2rem; max-width: 900px; margin: 0 auto; }
                .status { padding: 1rem; border-radius: 8px; margin: 1rem 0; }
                .valid { background: #d4edda; border: 1px solid #c3e6cb; color: #155724; }
                .expired { background: #f8d7da; border: 1px solid #f5c6cb; color: #721c24; }
                .no_cache { background: #fff3cd; border: 1px solid #ffeaa7; color: #856404; }
                .error { background: #f8d7da; border: 1px solid #f5c6cb; color: #721c24; }
                .completed { background: #d1ecf1; border: 1px solid #bee5eb; color: #0c5460; }
                button { padding: 0.5rem 1rem; margin: 0.5rem; border: none; border-radius: 4px; cursor: pointer; }
                .btn-primary { background: #007bff; color: white; }
                .btn-success { background: #28a745; color: white; }
                .btn-warning { background: #ffc107; color: black; }
                .btn-danger { background: #dc3545; color: white; }
                .auth-methods { display: flex; gap: 1rem; flex-wrap: wrap; margin: 1rem 0; }
                .method-card { border: 1px solid #ddd; padding: 1rem; border-radius: 8px; flex: 1; min-width: 300px; }
                .method-card h4 { margin-top: 0; }
                .uid-info { background: #f0f8f0; padding: 1rem; border-radius: 4px; margin: 1rem 0; border-left: 4px solid #28a745; }
            </style>
        </head>
        <body>
            <h1>📧 Email Token Manager</h1>
            
            <div class="uid-info">
                <h4>🆔 Current User ID</h4>
                <p><strong>UID:</strong> {{ uid }}</p>
                <p><small>This ID is used to separate token caches for different users</small></p>
            </div>
            
            {% if status.status == 'valid' %}
                <div class="status completed">
                    <h3>✅ Authentication Completed</h3>
                    <p><strong>Account:</strong> {{ status.account }}</p>
                    <p><strong>Token Status:</strong> Valid and automatically uploaded to Firebase</p>
                    {% if status.expires %}
                    <p><strong>Expires in:</strong> {{ status.expires }} seconds</p>
                    {% endif %}
                    {% if upload_result %}
                        {% if upload_result.success %}
                        <p><strong>Upload Status:</strong> ✅ {{ upload_result.message }}</p>
                        {% else %}
                        <p><strong>Upload Status:</strong> ❌ {{ upload_result.error }}</p>
                        {% endif %}
                    {% endif %}
                </div>
                
                <div>
                    <button class="btn-danger" onclick="clearToken()">🗑️ Clear Token</button>
                </div>
            {% else %}
                <div class="status {{ status.status }}">
                    <h3>🔍 Token Status: {{ status.status.title() }}</h3>
                    <p>{{ status.message }}</p>
                    {% if status.account %}
                    <p><strong>Account:</strong> {{ status.account }}</p>
                    {% endif %}
                    {% if status.expires %}
                    <p><strong>Expires in:</strong> {{ status.expires }} seconds</p>
                    {% endif %}
                </div>
                
                <div class="auth-methods">
                    <div class="method-card">
                        <h4>🌐 Web Authentication</h4>
                        <p>Uses browser redirect - works well for web deployments</p>
                        <button class="btn-primary" onclick="startWebAuth()">🔐 Start Web Authentication</button>
                    </div>
                    
                    <div class="method-card">
                        <h4>📱 Device Code Flow</h4>
                        <p>Use another device/browser - good for servers</p>
                        <button class="btn-primary" onclick="startDeviceFlow()">📱 Start Device Authentication</button>
                    </div>
                </div>
                
            {% endif %}
            
            <script>
                function log(message) {
                    const output = document.getElementById('output');
                    const timestamp = new Date().toLocaleTimeString();
                    output.textContent += `[${timestamp}] ${message}\n`;
                    output.scrollTop = output.scrollHeight;
                }
                
                async function apiCall(endpoint, method = 'GET') {
                    try {
                        log(`Making ${method} request to ${endpoint}...`);
                        const response = await fetch(endpoint, { method });
                        const data = await response.json();
                        log(`Response: ${JSON.stringify(data, null, 2)}`);
                        return data;
                    } catch (error) {
                        log(`Error: ${error.message}`);
                        return null;
                    }
                }
                
                async function clearToken() {
                    if (confirm('Are you sure you want to clear the token cache?')) {
                        await apiCall('/api/clear', 'POST');
                        setTimeout(() => location.reload(), 1000);
                    }
                }
                
                function startWebAuth() {
                    log("🌐 Starting web authentication...");
                    window.location.href = '/auth/login';
                }
                
                async function startDeviceFlow() {
                    const data = await apiCall('/api/device-flow', 'POST');
                    if (data && data.verification_uri && data.user_code) {
                        log(`\n🔐 DEVICE CODE: ${data.user_code}`);
                        log(`🌐 Go to: ${data.verification_uri}`);
                        log(`📋 Copy this code: ${data.user_code}`);
                        log(`\nOpening in new window...`);
                        
                        // Create a more user-friendly page
                        const newWindow = window.open('', '_blank', 'width=600,height=400');
                        newWindow.document.write(`
                            <html>
                                <head><title>Device Authentication</title></head>
                                <body style="font-family: sans-serif; padding: 2rem; text-align: center;">
                                    <h2>📱 Device Authentication</h2>
                                    <p>1. Go to: <a href="${data.verification_uri}" target="_blank">${data.verification_uri}</a></p>
                                    <p>2. Enter this code:</p>
                                    <div style="font-size: 2rem; font-weight: bold; background: #f0f0f0; padding: 1rem; border-radius: 8px; margin: 1rem;">
                                        ${data.user_code}
                                    </div>
                                    <button onclick="navigator.clipboard.writeText('${data.user_code}')" style="padding: 0.5rem 1rem; background: #007bff; color: white; border: none; border-radius: 4px;">
                                        📋 Copy Code
                                    </button>
                                    <br><br>
                                    <a href="${data.verification_uri}" target="_blank" style="padding: 0.5rem 1rem; background: #28a745; color: white; text-decoration: none; border-radius: 4px;">
                                        🔗 Open Microsoft Login
                                    </a>
                                </body>
                            </html>
                        `);
                        
                        // Start polling
                        pollDeviceFlow();
                    }
                }
                
                async function pollDeviceFlow() {
                    log("📡 Polling for device flow completion...");
                    let pollCount = 0;
                    const maxPolls = 180; // 15 minutes
                    
                    const poll = async () => {
                        pollCount++;
                        const data = await apiCall('/api/poll-device');
                        
                        if (data) {
                            if (data.status === 'completed') {
                                log(`✅ Device flow completed successfully! Account: ${data.account || 'Unknown'}`);
                                setTimeout(() => location.reload(), 2000);
                                return;
                            } else if (data.status === 'error') {
                                log(`❌ Device flow error: ${data.error}`);
                                return;
                            } else if (data.status === 'pending') {
                                const remainingTime = Math.max(0, maxPolls - pollCount);
                                const minutes = Math.floor(remainingTime * 5 / 60);
                                const seconds = (remainingTime * 5) % 60;
                                log(`⏳ Still waiting... (${minutes}:${seconds.toString().padStart(2, '0')} remaining)`);
                                
                                if (pollCount < maxPolls) {
                                    setTimeout(poll, 5000);
                                } else {
                                    log("⏰ Polling timeout reached. Please start a new authentication if needed.");
                                }
                            }
                        }
                    };
                    
                    setTimeout(poll, 5000);
                }
            </script>
        </body>
    </html>
    """, status=status, base_url=base_url, uid=uid, upload_result=upload_result)

# Global variable to store device flow

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
        global current_device_flow
        current_device_flow = None
        return jsonify({"success": True, "message": "Token cache cleared"})
    except Exception as e:
        return jsonify({"error": str(e)})

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
            error = request.args.get('error_description', 'No authorization code received')
            return render_template_string("""
                <html>
                    <body style="font-family: sans-serif; padding: 2rem; text-align: center;">
                        <h2>❌ Authentication Failed</h2>
                        <p>{{ error }}</p>
                        <a href="/?uid={{ uid }}" style="padding: 0.5rem 1rem; background: #007bff; color: white; text-decoration: none; border-radius: 4px;">← Back to Token Manager</a>
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
                <html>
                <head>
                    <style>
                    body {
                        font-family: sans-serif;
                        padding: 2rem;
                        text-align: center;
                        background-color: #f5f5f5;
                    }
                    .card {
                        background: white;
                        display: inline-block;
                        padding: 2rem 3rem;
                        border-radius: 12px;
                        box-shadow: 0 4px 12px rgba(0,0,0,0.1);
                        animation: fadeIn 0.6s ease-in-out;
                    }
                    .spinner {
                        margin-top: 1.5rem;
                        width: 48px;
                        height: 48px;
                        border: 5px solid #ddd;
                        border-top: 5px solid #28a745;
                        border-radius: 50%;
                        animation: spin 1s linear infinite;
                    }
                    @keyframes spin {
                        0% { transform: rotate(0deg); }
                        100% { transform: rotate(360deg); }
                    }
                    @keyframes fadeIn {
                        from { opacity: 0; transform: translateY(20px); }
                        to { opacity: 1; transform: translateY(0); }
                    }
                    </style>
                </head>
                <body>
                    <div class="card">
                    <h2>✅ Authentication Successful</h2>
                    <p><strong>User ID:</strong> {{ uid }}</p>
                    <p><strong>Account:</strong> {{ account }}</p>
                    <p>Uploading token to Firestore...</p>
                    <div class="spinner"></div>
                    </div>
                    <script>
                    setTimeout(() => window.location.href = '/?uid={{ uid }}', 3000);
                    </script>
                </body>
                </html>
                """, account=account, uid=uid)

        else:
            error = result.get("error_description", "Failed to acquire token")
            return render_template_string("""
                <html>
                    <body style="font-family: sans-serif; padding: 2rem; text-align: center;">
                        <h2>❌ Token Acquisition Failed</h2>
                        <p>{{ error }}</p>
                        <a href="/?uid={{ uid }}" style="padding: 0.5rem 1rem; background: #007bff; color: white; text-decoration: none; border-radius: 4px;">← Back to Token Manager</a>
                    </body>
                </html>
            """, error=error, uid=uid)
    
    except Exception as e:
        print(f"[CALLBACK] Exception: {str(e)}")
        uid = request.args.get("state", "web_user")  # Try to get UID for error page
        return render_template_string("""
            <html>
                <body style="font-family: sans-serif; padding: 2rem; text-align: center;">
                    <h2>❌ Authentication Error</h2>
                    <p>{{ error }}</p>
                    <a href="/?uid={{ uid }}" style="padding: 0.5rem 1rem; background: #007bff; color: white; text-decoration: none; border-radius: 4px;">← Back to Token Manager</a>
                </body>
            </html>
        """, error=str(e), uid=uid)

# Device flow endpoints (updated to handle UID properly)
@app.route("/api/device-flow", methods=["POST"])
def api_device_flow():
    global current_device_flow

    try:
        uid = session.get("uid", "web_user")
        print(f"[DEVICE-FLOW] Starting device flow for UID: {uid}")
        
        cache = SerializableTokenCache()
        app_obj = ConfidentialClientApplication(
            CLIENT_ID,
            client_credential=CLIENT_SECRET,
            authority=AUTHORITY,
            token_cache=cache
        )

        # Initiate device flow with proper error handling
        flow = app_obj.initiate_device_flow(scopes=SCOPES)
        
        if "user_code" not in flow:
            error_msg = flow.get("error_description", "Failed to initiate device flow")
            return jsonify({"error": error_msg})

        # Store with UID context
        current_device_flow = (app_obj, flow, cache, uid)

        return jsonify({
            "success": True,
            "verification_uri": flow["verification_uri"],
            "user_code": flow["user_code"],
            "expires_in": flow.get("expires_in", 900)
        })

    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/poll-device")
def api_poll_device():
    global current_device_flow
    
    if not current_device_flow:
        return jsonify({"error": "No device flow in progress"})
    
    app_obj, flow, cache, uid = current_device_flow
    print(f"[POLL-DEVICE] Polling for UID: {uid}")
    
    try:
        result = app_obj.acquire_token_by_device_flow(flow)
        
        if "access_token" in result:
            # Success! Save token to user-specific location
            user_dir = f"msal_caches/{uid}" 
            cache_file = f"{user_dir}/msal_token_cache.bin" 
            os.makedirs(user_dir, exist_ok=True)
            
            with open(cache_file, "w") as f:
                f.write(cache.serialize())
            
            print(f"[POLL-DEVICE] Successfully saved token for UID {uid}")
            
            # Clean up
            current_device_flow = None
            
            return jsonify({
                "status": "completed", 
                "message": "Token acquired successfully",
                "account": result.get("account", {}).get("username", "Unknown")
            })
        
        elif "error" in result:
            error = result["error"]
            if error == "authorization_pending":
                return jsonify({"status": "pending", "message": "Waiting for user authentication..."})
            elif error == "authorization_declined":
                current_device_flow = None
                return jsonify({"status": "error", "error": "User declined the authentication request"})
            elif error == "expired_token":
                current_device_flow = None
                return jsonify({"status": "error", "error": "Device code expired. Please start a new authentication."})
            else:
                current_device_flow = None
                return jsonify({"status": "error", "error": result.get("error_description", error)})
        
        else:
            return jsonify({"status": "pending", "message": "Authentication in progress..."})
    
    except Exception as e:
        current_device_flow = None
        return jsonify({"status": "error", "error": f"Unexpected error: {str(e)}"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)