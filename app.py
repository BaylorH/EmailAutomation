import os
import sys
import subprocess
import json
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string
from msal import PublicClientApplication, SerializableTokenCache
from firebase_helpers import upload_token

app = Flask(__name__)

CLIENT_ID        = os.getenv("AZURE_API_APP_ID")
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")
AUTHORITY        = "https://login.microsoftonline.com/common"
SCOPES           = ["Mail.ReadWrite", "Mail.Send"]
CACHE_FILE       = "msal_token_cache.bin"

def check_token_status():
    """Check if we have a valid token"""
    if not os.path.exists(CACHE_FILE):
        return {"status": "no_cache", "message": "No token cache found"}
    
    try:
        cache = SerializableTokenCache()
        cache.deserialize(open(CACHE_FILE).read())
        
        app_obj = PublicClientApplication(
            CLIENT_ID,
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
    return render_template_string("""
    <html>
        <head>
            <title>📧 Email Token Manager</title>
            <style>
                body { font-family: sans-serif; padding: 2rem; max-width: 800px; margin: 0 auto; }
                .status { padding: 1rem; border-radius: 8px; margin: 1rem 0; }
                .valid { background: #d4edda; border: 1px solid #c3e6cb; color: #155724; }
                .expired { background: #f8d7da; border: 1px solid #f5c6cb; color: #721c24; }
                .no_cache { background: #fff3cd; border: 1px solid #ffeaa7; color: #856404; }
                .error { background: #f8d7da; border: 1px solid #f5c6cb; color: #721c24; }
                button { padding: 0.5rem 1rem; margin: 0.5rem; border: none; border-radius: 4px; cursor: pointer; }
                .btn-primary { background: #007bff; color: white; }
                .btn-success { background: #28a745; color: white; }
                .btn-warning { background: #ffc107; color: black; }
                #output { background: #f8f9fa; padding: 1rem; border-radius: 4px; font-family: monospace; white-space: pre-wrap; }
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
            
            <div>
                {% if status.status == 'valid' %}
                    <button class="btn-success" onclick="uploadToken()">☁️ Upload Valid Token to Firebase</button>
                    <button class="btn-warning" onclick="refreshToken()">🔄 Refresh Token</button>
                {% else %}
                    <button class="btn-primary" onclick="startDeviceFlow()">🔐 Start Device Authentication</button>
                {% endif %}
                <button class="btn-primary" onclick="checkStatus()">🔍 Check Status</button>
            </div>
            
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
                
                async function startDeviceFlow() {
                    const data = await apiCall('/api/device-flow', 'POST');
                    if (data && data.verification_uri && data.user_code) {
                        log(`\n🔐 DEVICE CODE: ${data.user_code}`);
                        log(`🌐 Go to: ${data.verification_uri}`);
                        log(`\nOpening in new window...`);
                        window.open(data.verification_uri, '_blank');
                        
                        // Start polling
                        pollDeviceFlow();
                    }
                }
                
                async function pollDeviceFlow() {
                    log("📡 Polling for device flow completion...");
                    const poll = async () => {
                        const data = await apiCall('/api/poll-device');
                        if (data) {
                            if (data.status === 'completed') {
                                log("✅ Device flow completed successfully!");
                                setTimeout(() => location.reload(), 2000);
                                return;
                            } else if (data.status === 'error') {
                                log(`❌ Device flow error: ${data.error}`);
                                return;
                            } else if (data.status === 'pending') {
                                log("⏳ Still waiting for user authentication...");
                                setTimeout(poll, 5000);
                            }
                        }
                    };
                    setTimeout(poll, 5000);
                }
            </script>
        </body>
    </html>
    """, status=status)

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

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    try:
        cache = SerializableTokenCache()
        if os.path.exists(CACHE_FILE):
            cache.deserialize(open(CACHE_FILE).read())
        
        app_obj = PublicClientApplication(
            CLIENT_ID,
            authority=AUTHORITY,
            token_cache=cache
        )
        
        accounts = app_obj.get_accounts()
        if not accounts:
            return jsonify({"error": "No accounts found"})
        
        # Force refresh by passing force_refresh=True
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

        flow = app_obj.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            return jsonify({"error": "Failed to initiate device flow"})

        current_device_flow = (app_obj, flow, cache)

        # Force the standard Microsoft device login URI
        verification_uri = "https://microsoft.com/devicelogin"

        return jsonify({
            "success": True,
            "verification_uri": verification_uri,
            "user_code": flow["user_code"]
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
            
            return jsonify({"status": "completed", "message": "Token acquired successfully"})
        
        elif "error" in result:
            error = result["error"]
            if error == "authorization_pending":
                return jsonify({"status": "pending"})
            else:
                current_device_flow = None
                return jsonify({"status": "error", "error": result.get("error_description", error)})
        
        else:
            return jsonify({"status": "pending"})
    
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Starting Token Manager on port {port}")
    app.run(host="0.0.0.0", port=port, debug=True)
