import os
from flask import Flask, render_template_string
from msal import PublicClientApplication, SerializableTokenCache
from firebase_helpers import upload_token

app = Flask(__name__)
CLIENT_ID = os.getenv("AZURE_API_APP_ID")
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = ["Mail.ReadWrite", "Mail.Send"]
CACHE_FILE = "msal_token_cache.bin"

@app.route("/start-auth")
def start_auth():
    cache = SerializableTokenCache()
    app_obj = PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)

    # Start device flow
    flow = app_obj.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        return "Failed to create device flow", 500

    verification_uri = flow["verification_uri"]
    user_code = flow["user_code"]

    # Just render this for now (no blocking)
    html = f"""
    <html>
    <head><title>Authorize App</title></head>
    <body style="font-family: sans-serif; padding: 2rem;">
        <h2>📩 Authorize Access</h2>
        <p>Click the link below and paste the code to allow email access:</p>
        <a href="{verification_uri}" target="_blank" style="font-size: 18px;">{verification_uri}</a>
        <h3>🔐 Your Code: <code style="font-size: 24px;">{user_code}</code></h3>
        <p>This part works! Polling will be added next.</p>
    </body>
    </html>
    """
    return html

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
