import os
import sys
import subprocess
import json
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, redirect, url_for
from msal import ConfidentialClientApplication, SerializableTokenCache
from firebase_helpers import upload_token

app = Flask(__name__)

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
    if not os.path.exists(CACHE_FILE):
        return {"status": "no_cache", "message": "No token cache found"}
    
    try:
        cache = SerializableTokenCache()
        cache.deserialize(open(CACHE_FILE).read())
        
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

@app.route("/")
def index():
    status = check_token_status()
    base_url = get_base_url()
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
                button { padding: 0.5rem 1rem; margin: 0.5rem; border: none; border-radius: 4px; cursor: pointer; }
                .btn-primary { background: #007bff; color: white; }
                .btn-success { background: #28a745; color: white; }
                .btn-warning { background: #ffc107; color: black; }
                .btn-danger { background: #dc3545; color: white; }
                #output { background: #f8f9fa; padding: 1rem; border-radius: 4px; font-family: monospace; white-space: pre-wrap; max-height: 400px; overflow-y: auto; }
                .auth-methods { display: flex; gap: 1rem; flex-wrap: wrap; margin: 1rem 0; }
                .method-card { border: 1px solid #ddd; padding: 1rem; border-radius: 8px; flex: 1; min-width: 300px; }
                .method-card h4 { margin-top: 0; }
                .redirect-info { background: #e3f2fd; padding: 1rem; border-radius: 4px; margin: 1rem 0; border-left: 4px solid #2196f3; }
            </style>
        </head>
        <body>
            <h1>📧 Email Token Manager</h1>
            
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
            
            {% if status.status == 'valid' %}
                <div>
                    <button class="btn-success" onclick="uploadToken()">☁️ Upload Valid Token to Firebase</button>
                    <button class="btn-warning" onclick="refreshToken()">🔄 Refresh Token</button>
                    <button class="btn-danger" onclick="clearToken()">🗑️ Clear Token</button>
                    <button class="btn-primary" onclick="checkStatus()">🔍 Check Status</button>
                </div>
            {% else %}
                <div class="redirect-info">
                    <h4>🔧 Azure App Registration Setup</h4>
                    <p>For web authentication to work, add this redirect URI to your Azure app:</p>
                    <code>{{ base_url }}/auth/callback</code>
                    <p><small>Go to Azure Portal → App Registrations → Your App → Authentication → Add Platform → Web</small></p>
                </div>
                
                <div class="auth-methods">
                    <div class="method-card">
                        <h4>🌐 Web Authentication (Recommended)</h4>
                        <p>Uses browser redirect - works well for web deployments</p>
                        <button class="btn-primary" onclick="startWebAuth()">🔐 Start Web Authentication</button>
                    </div>
                    
                    <div class="method-card">
                        <h4>📱 Device Code Flow</h4>
                        <p>Use another device/browser - good for servers</p>
                        <button class="btn-primary" onclick="startDeviceFlow()">📱 Start Device Authentication</button>
                    </div>
                </div>
                
                <button class="btn-primary" onclick="checkStatus()">🔍 Check Status</button>
            {% endif %}
            
            <h3>📋 Output:</h3>
            <div id="output">Ready...</div>
            
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
                
                async function checkStatus() {
                    const data = await apiCall('/api/status');
                    if (data) {
                        setTimeout(() => location.reload(), 1000);
                    }
                }
                
                async function uploadToken() {
                    await apiCall('/api/upload', 'POST');
                }
                
                async function refreshToken() {
                    await apiCall('/api/refresh', 'POST');
                    setTimeout(() => location.reload(), 2000);
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
    """, status=status, base_url=base_url)

# Global variable to store device flow
current_device_flow = None

@app.route("/api/status")
def api_status():
    return jsonify(check_token_status())

@app.route("/api/upload", methods=["POST"])
def api_upload():
    try:
        if not os.path.exists(CACHE_FILE):
            return jsonify({"error": "No token cache file found"})
        
        upload_token(FIREBASE_API_KEY, input_file=CACHE_FILE, user_id="web_user")
        return jsonify({"success": True, "message": "Token uploaded to Firebase"})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/clear", methods=["POST"])
def api_clear():
    try:
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
        global current_device_flow
        current_device_flow = None
        return jsonify({"success": True, "message": "Token cache cleared"})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    try:
        cache = SerializableTokenCache()
        if os.path.exists(CACHE_FILE):
            cache.deserialize(open(CACHE_FILE).read())
        
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
            with open(CACHE_FILE, "w") as f:
                f.write(cache.serialize())
            return jsonify({"success": True, "message": "Token refreshed successfully"})
        else:
            return jsonify({"error": f"Failed to refresh token: {result.get('error_description', 'Unknown error')}"})
    
    except Exception as e:
        return jsonify({"error": str(e)})

# Web-based authentication routes
@app.route("/auth/login")
def auth_login():
    cache = SerializableTokenCache()
    if os.path.exists(CACHE_FILE):
        cache.deserialize(open(CACHE_FILE).read())
    
    app_obj = ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        token_cache=cache
    )
    
    # Build authorization URL
    auth_url = app_obj.get_authorization_request_url(
        SCOPES,
        redirect_uri="https://email-token-manager.onrender.com/auth/callback"
    )
    
    return redirect(auth_url)

@app.route("/auth/callback")
def auth_callback():
    try:
        cache = SerializableTokenCache()
        if os.path.exists(CACHE_FILE):
            cache.deserialize(open(CACHE_FILE).read())
        
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
                        <a href="/" style="padding: 0.5rem 1rem; background: #007bff; color: white; text-decoration: none; border-radius: 4px;">← Back to Token Manager</a>
                    </body>
                </html>
            """, error=error)
        
        # Exchange code for token
        result = app_obj.acquire_token_by_authorization_code(
            code,
            scopes=SCOPES,
            redirect_uri="https://email-token-manager.onrender.com/auth/callback"
        )
        
        if "access_token" in result:
            # Save token
            with open(CACHE_FILE, "w") as f:
                f.write(cache.serialize())
            
            account = result.get("account", {}).get("username", "Unknown")
            
            return render_template_string("""
                <html>
                    <body style="font-family: sans-serif; padding: 2rem; text-align: center;">
                        <h2>✅ Authentication Successful!</h2>
                        <p>Account: {{ account }}</p>
                        <p>Token has been saved and cached.</p>
                        <a href="/" style="padding: 0.5rem 1rem; background: #28a745; color: white; text-decoration: none; border-radius: 4px;">← Back to Token Manager</a>
                        <script>
                            setTimeout(() => window.location.href = '/', 3000);
                        </script>
                    </body>
                </html>
            """, account=account)
        else:
            error = result.get("error_description", "Failed to acquire token")
            return render_template_string("""
                <html>
                    <body style="font-family: sans-serif; padding: 2rem; text-align: center;">
                        <h2>❌ Token Acquisition Failed</h2>
                        <p>{{ error }}</p>
                        <a href="/" style="padding: 0.5rem 1rem; background: #007bff; color: white; text-decoration: none; border-radius: 4px;">← Back to Token Manager</a>
                    </body>
                </html>
            """, error=error)
    
    except Exception as e:
        return render_template_string("""
            <html>
                <body style="font-family: sans-serif; padding: 2rem; text-align: center;">
                    <h2>❌ Authentication Error</h2>
                    <p>{{ error }}</p>
                    <a href="/" style="padding: 0.5rem 1rem; background: #007bff; color: white; text-decoration: none; border-radius: 4px;">← Back to Token Manager</a>
                </body>
            </html>
        """, error=str(e))

# Device flow endpoints (fixed)
@app.route("/api/device-flow", methods=["POST"])
def api_device_flow():
    global current_device_flow

    try:
        cache = SerializableTokenCache()
        app_obj = PublicClientApplication(
            CLIENT_ID,
            authority=AUTHORITY,
            token_cache=cache
        )

        # Initiate device flow with proper error handling
        flow = app_obj.initiate_device_flow(scopes=SCOPES)
        
        if "user_code" not in flow:
            error_msg = flow.get("error_description", "Failed to initiate device flow")
            return jsonify({"error": error_msg})

        current_device_flow = (app_obj, flow, cache)

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
    
    app_obj, flow, cache = current_device_flow
    
    try:
        result = app_obj.acquire_token_by_device_flow(flow)
        
        if "access_token" in result:
            # Success! Save token
            with open(CACHE_FILE, "w") as f:
                f.write(cache.serialize())
            
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
    print(f"🚀 Starting Token Manager on port {port}")
    
    # Print helpful setup info
    print("\n📋 Setup Instructions:")
    print("1. Ensure AZURE_API_APP_ID environment variable is set")
    print("2. In Azure Portal, add this redirect URI to your app:")
    print(f"   http://localhost:{port}/auth/callback")
    print("   (or your production URL + /auth/callback)")
    print("\n🌐 Access the app at:")
    print(f"   http://localhost:{port}")
    
    app.run(host="0.0.0.0", port=port, debug=True)
