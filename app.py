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
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE='Lax',
)

CLIENT_ID = os.getenv("AZURE_API_APP_ID")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = ["Mail.ReadWrite", "Mail.Send"]

def get_base_url():
    if "https://email-token-manager.onrender.com" == request.url_root.rstrip('/'):
        return "https://email-token-manager.onrender.com"
    else:
        return "http://localhost:5000"  # Default for development

def check_token_status():
    """Check if we have a valid token"""
    uid = session.get("uid", "web_user") 
    user_dir = f"msal_caches/{uid}" 
    cache_file = f"{user_dir}/msal_token_cache.bin" 
    os.makedirs(user_dir, exist_ok=True)
    
    if not os.path.exists(cache_file):
        return {"status": "no_token", "message": "No authentication token found"}
    
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
            return {"status": "no_token", "message": "No authenticated accounts found"}
        
        result = app_obj.acquire_token_silent(SCOPES, account=accounts[0])
        
        if result and "access_token" in result:
            return {
                "status": "authenticated",
                "message": "Ready to upload token",
                "account": accounts[0].get("username", "Unknown"),
                "expires": result.get("expires_in", "Unknown")
            }
        else:
            return {
                "status": "expired",
                "message": "Token expired, please authenticate again",
                "error": result.get("error_description", "Unknown error") if result else "No result"
            }
    except Exception as e:
        return {"status": "error", "message": f"Error checking token: {str(e)}"}

@app.route("/")
def index():
    uid = request.args.get("uid", "web_user")
    session["uid"] = uid
    status = check_token_status()
    
    # If we have a valid token, show success page and auto-upload
    if status["status"] == "authenticated":
        return render_template_string("""
        <html>
            <head>
                <title>📧 Email Token Manager - Success</title>
                <style>
                    body { 
                        font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
                        padding: 2rem; 
                        max-width: 600px; 
                        margin: 0 auto; 
                        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                        min-height: 100vh;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                    }
                    .container {
                        background: white;
                        padding: 3rem;
                        border-radius: 20px;
                        box-shadow: 0 20px 40px rgba(0,0,0,0.1);
                        text-align: center;
                        animation: slideUp 0.6s ease-out;
                    }
                    @keyframes slideUp {
                        from { opacity: 0; transform: translateY(30px); }
                        to { opacity: 1; transform: translateY(0); }
                    }
                    .success-icon {
                        font-size: 4rem;
                        color: #28a745;
                        margin-bottom: 1rem;
                        animation: bounce 1s infinite alternate;
                    }
                    @keyframes bounce {
                        from { transform: scale(1); }
                        to { transform: scale(1.1); }
                    }
                    h1 { color: #333; margin-bottom: 1rem; }
                    .account-info {
                        background: #f8f9fa;
                        padding: 1rem;
                        border-radius: 10px;
                        margin: 1rem 0;
                        border-left: 4px solid #28a745;
                    }
                    .status-message {
                        color: #666;
                        margin: 1rem 0;
                        font-size: 1.1rem;
                    }
                    .loading {
                        display: inline-block;
                        width: 20px;
                        height: 20px;
                        border: 3px solid #f3f3f3;
                        border-top: 3px solid #007bff;
                        border-radius: 50%;
                        animation: spin 1s linear infinite;
                        margin-left: 10px;
                    }
                    @keyframes spin {
                        0% { transform: rotate(0deg); }
                        100% { transform: rotate(360deg); }
                    }
                    .btn {
                        padding: 12px 24px;
                        margin: 10px;
                        border: none;
                        border-radius: 8px;
                        cursor: pointer;
                        font-size: 1rem;
                        text-decoration: none;
                        display: inline-block;
                        transition: all 0.3s ease;
                    }
                    .btn-primary { background: #007bff; color: white; }
                    .btn-primary:hover { background: #0056b3; transform: translateY(-2px); }
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="success-icon">✅</div>
                    <h1>Authentication Successful!</h1>
                    
                    <div class="account-info">
                        <strong>Account:</strong> {{ status.account }}
                    </div>
                    
                    <div class="status-message" id="statusMessage">
                        Uploading token to Firebase<span class="loading"></span>
                    </div>
                    
                    <a href="/?uid={{ uid }}" class="btn btn-primary" style="display: none;" id="doneButton">
                        🔄 Start Over
                    </a>
                </div>
                
                <script>
                    // Auto-upload token on page load
                    async function uploadToken() {
                        try {
                            const response = await fetch('/api/upload', { method: 'POST' });
                            const data = await response.json();
                            
                            if (data.success) {
                                document.getElementById('statusMessage').innerHTML = '🎉 Token successfully uploaded to Firebase!';
                                document.getElementById('doneButton').style.display = 'inline-block';
                            } else {
                                document.getElementById('statusMessage').innerHTML = '❌ Upload failed: ' + (data.error || 'Unknown error');
                                document.getElementById('doneButton').style.display = 'inline-block';
                            }
                        } catch (error) {
                            document.getElementById('statusMessage').innerHTML = '❌ Upload failed: ' + error.message;
                            document.getElementById('doneButton').style.display = 'inline-block';
                        }
                    }
                    
                    // Start upload after a brief delay for better UX
                    setTimeout(uploadToken, 1500);
                </script>
            </body>
        </html>
        """, status=status, uid=uid)
    
    # Otherwise, show the simple authentication page
    base_url = get_base_url()
    return render_template_string("""
    <html>
        <head>
            <title>📧 Email Token Manager</title>
            <style>
                body { 
                    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
                    padding: 2rem; 
                    max-width: 500px; 
                    margin: 0 auto; 
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    min-height: 100vh;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }
                .container {
                    background: white;
                    padding: 3rem;
                    border-radius: 20px;
                    box-shadow: 0 20px 40px rgba(0,0,0,0.1);
                    text-align: center;
                    animation: fadeIn 0.6s ease-out;
                }
                @keyframes fadeIn {
                    from { opacity: 0; transform: scale(0.9); }
                    to { opacity: 1; transform: scale(1); }
                }
                h1 { 
                    color: #333; 
                    margin-bottom: 1rem;
                    font-size: 2.5rem;
                }
                .subtitle {
                    color: #666;
                    margin-bottom: 2rem;
                    font-size: 1.1rem;
                }
                .auth-button {
                    background: linear-gradient(45deg, #007bff, #0056b3);
                    color: white;
                    padding: 15px 30px;
                    border: none;
                    border-radius: 50px;
                    font-size: 1.2rem;
                    cursor: pointer;
                    transition: all 0.3s ease;
                    box-shadow: 0 4px 15px rgba(0, 123, 255, 0.3);
                    text-decoration: none;
                    display: inline-block;
                }
                .auth-button:hover {
                    transform: translateY(-3px);
                    box-shadow: 0 6px 20px rgba(0, 123, 255, 0.4);
                    background: linear-gradient(45deg, #0056b3, #004085);
                }
                .setup-info {
                    background: #e3f2fd;
                    padding: 1rem;
                    border-radius: 10px;
                    margin: 2rem 0;
                    border-left: 4px solid #2196f3;
                    font-size: 0.9rem;
                    text-align: left;
                }
                .user-info {
                    background: #f0f8f0;
                    padding: 1rem;
                    border-radius: 10px;
                    margin: 1rem 0;
                    border-left: 4px solid #28a745;
                    font-size: 0.9rem;
                }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>📧</h1>
                <h1>Email Token Manager</h1>
                <p class="subtitle">Secure Microsoft Outlook authentication</p>
                
                <div class="user-info">
                    <strong>User ID:</strong> {{ uid }}
                </div>
                
                <a href="/auth/login" class="auth-button">
                    🔐 Authenticate with Microsoft
                </a>
                
                <div class="setup-info">
                    <strong>Azure Setup Required:</strong><br>
                    Add this redirect URI to your Azure app:<br>
                    <code>{{ base_url }}/auth/callback</code>
                </div>
            </div>
        </body>
    </html>
    """, base_url=base_url, uid=uid)

@app.route("/auth/login")
def auth_login():
    uid = session.get("uid", "web_user")
    
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
    
    base_url = get_base_url()
    auth_url = app_obj.get_authorization_request_url(
        SCOPES,
        redirect_uri=f"{base_url}/auth/callback",
        state=uid
    )
    
    return redirect(auth_url)

@app.route("/auth/callback")
def auth_callback():
    # Show a brief loading screen while processing
    return render_template_string("""
    <html>
        <head>
            <title>Processing Authentication...</title>
            <style>
                body { 
                    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
                    padding: 2rem; 
                    max-width: 500px; 
                    margin: 0 auto; 
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    min-height: 100vh;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }
                .container {
                    background: white;
                    padding: 3rem;
                    border-radius: 20px;
                    box-shadow: 0 20px 40px rgba(0,0,0,0.1);
                    text-align: center;
                }
                .spinner {
                    width: 50px;
                    height: 50px;
                    border: 5px solid #f3f3f3;
                    border-top: 5px solid #007bff;
                    border-radius: 50%;
                    animation: spin 1s linear infinite;
                    margin: 0 auto 1rem auto;
                }
                @keyframes spin {
                    0% { transform: rotate(0deg); }
                    100% { transform: rotate(360deg); }
                }
                h2 { color: #333; }
                p { color: #666; }
            </style>
        </head>
        <body>
            <div class="container">
                <div class="spinner"></div>
                <h2>Processing Authentication</h2>
                <p>Please wait while we complete your authentication...</p>
            </div>
            
            <script>
                // Immediately process the callback
                setTimeout(() => {
                    window.location.href = '/auth/process?' + window.location.search.substring(1);
                }, 1000);
            </script>
        </body>
    </html>
    """)

@app.route("/auth/process")
def auth_process():
    try:
        uid = request.args.get("state", "web_user")
        session["uid"] = uid 
        
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
        
        code = request.args.get('code')
        if not code:
            error = request.args.get('error_description', 'Authentication was cancelled or failed')
            return redirect(f"/?uid={uid}&error={error}")
        
        base_url = get_base_url()
        result = app_obj.acquire_token_by_authorization_code(
            code,
            scopes=SCOPES,
            redirect_uri=f"{base_url}/auth/callback"
        )
        
        if "access_token" in result:
            with open(cache_file, "w") as f:
                f.write(cache.serialize())
            
            # Redirect to main page which will now show success screen
            return redirect(f"/?uid={uid}")
        else:
            error = result.get("error_description", "Failed to acquire token")
            return redirect(f"/?uid={uid}&error={error}")
    
    except Exception as e:
        uid = request.args.get("state", "web_user")
        return redirect(f"/?uid={uid}&error={str(e)}")

@app.route("/api/upload", methods=["POST"])
def api_upload():
    try:
        uid = session.get("uid", "web_user") 
        user_dir = f"msal_caches/{uid}" 
        cache_file = f"{user_dir}/msal_token_cache.bin" 
        os.makedirs(user_dir, exist_ok=True)
        
        if not os.path.exists(cache_file):
            return jsonify({"success": False, "error": "No token cache file found"})
        
        upload_token(FIREBASE_API_KEY, input_file=cache_file, user_id=uid)
        return jsonify({"success": True, "message": "Token uploaded to Firebase successfully"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Starting Simplified Token Manager on port {port}")
    print(f"🌐 Access the app at: http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=True)