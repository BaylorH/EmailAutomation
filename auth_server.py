import os
import threading
from flask import Flask, request
from msal import PublicClientApplication, SerializableTokenCache
from firebase_helpers import upload_token

app = Flask(__name__)

CLIENT_ID        = os.getenv("AZURE_API_APP_ID")
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")
AUTHORITY        = "https://login.microsoftonline.com/common"
SCOPES           = ["Mail.ReadWrite", "Mail.Send"]
CACHE_FILE       = "msal_token_cache.bin"

@app.route("/start-auth")
def start_auth():
    uid = request.args.get("uid", "default_user")

    cache = SerializableTokenCache()
    app_obj = PublicClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        token_cache=cache
    )

    flow = app_obj.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        return "❌ Failed to create device flow", 500

    verification_uri = flow["verification_uri"]
    user_code = flow["user_code"]

    def poll_and_upload():
        print(f"⏳ Polling for token for {uid}...")
        result = app_obj.acquire_token_by_device_flow(flow)
        if "access_token" in result:
            with open(CACHE_FILE, "w") as f:
                f.write(cache.serialize())
            upload_token(FIREBASE_API_KEY, input_file=CACHE_FILE, user_id=uid)
            print(f"✅ Token acquired and uploaded for {uid}")
        else:
            print(f"❌ Failed to acquire token for {uid}:", result)

    threading.Thread(target=poll_and_upload).start()

    return f"""
    <html>
    <head><title>Authorize App</title></head>
    <body style="font-family: sans-serif; padding: 2rem;">
        <h2>📩 Authorize Access</h2>
        <p>Click the link below and paste the code to allow email access:</p>
        <a href="{verification_uri}" target="_blank" style="font-size: 18px;">{verification_uri}</a>
        <h3>🔐 Your Code: <code style="font-size: 24px;">{user_code}</code></h3>
        <p>This window will automatically poll while you complete sign-in.</p>
    </body>
    </html>
    """

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
