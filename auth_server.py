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

    # Step 1: Start device flow
    flow = app_obj.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        return "Failed to create device flow", 500

    verification_uri = flow["verification_uri"]
    user_code = flow["user_code"]

    # HTML page with instructions
    html = f"""
    <html>
    <head><title>Authorize App</title></head>
    <body style="font-family: sans-serif; padding: 2rem;">
        <h2>📩 Authorize Access</h2>
        <p>Click the link below and paste the code to allow email access:</p>
        <a href="{verification_uri}" target="_blank" style="font-size: 18px;">{verification_uri}</a>
        <h3>🔐 Your Code: <code style="font-size: 24px;">{user_code}</code></h3>
        <p>Leave this page open. Once you're done, this page will refresh automatically when complete.</p>
        <script>
            setInterval(() => location.reload(), 3000);  // Poll by refreshing
        </script>
    </body>
    </html>
    """

    # Step 2: Try polling immediately
    result = app_obj.acquire_token_by_device_flow(flow)

    if "access_token" in result:
        with open(CACHE_FILE, "w") as f:
            f.write(cache.serialize())
        upload_token(CACHE_FILE)
        return "<h2>✅ Authorized and token uploaded. You may now close this window.</h2>"
    else:
        return html  # Still waiting

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
