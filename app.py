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
                    
                    <button class="btn btn-danger" onclick="disconnectEmail()">
                        🔓 Disconnect Email Access
                    </button>
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