import os
from flask import Flask, request, jsonify, render_template_string
from msal import PublicClientApplication, SerializableTokenCache
from firebase_helpers import upload_token

app = Flask(__name__)

CLIENT_ID        = os.getenv("AZURE_API_APP_ID")
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")
AUTHORITY        = "https://login.microsoftonline.com/common"
SCOPES           = ["Mail.ReadWrite", "Mail.Send"]
CACHE_FILE       = "msal_token_cache.bin"

# Global dict to store user device flows
user_flows = {}

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

    user_flows[uid] = (flow, cache)

    return render_template_string("""
        <html>
        <head><title>Authorize App</title></head>
        <body style="font-family: sans-serif; padding: 2rem;">
            <h2>📩 Authorize Access</h2>
            <p>Click the link below and paste the code to allow email access:</p>
            <a href="{{ uri }}" target="_blank" style="font-size: 18px;">{{ uri }}</a>
            <h3>🔐 Your Code: <code style="font-size: 24px;">{{ code }}</code></h3>
            <p>This page will check automatically while you complete sign-in.</p>
            <script>
              const uid = "{{ uid }}";
              const poll = async () => {
                const res = await fetch(`/poll-token?uid=${uid}`);
                const data = await res.json();
                if (data.status === "done") {
                  alert("✅ Email access granted!");
                  window.close();
                } else if (data.status === "error") {
                  console.error("Token error:", data.error);
                } else {
                  setTimeout(poll, 3000);
                }
              };
              poll();
            </script>
        </body>
        </html>
    """, uri=flow["verification_uri"], code=flow["user_code"], uid=uid)

@app.route("/poll-token")
def poll_token():
    uid = request.args.get("uid", "default_user")
    if uid not in user_flows:
        return jsonify({"status": "not_started"})

    flow, cache = user_flows[uid]
    app_obj = PublicClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        token_cache=cache
    )

    result = app_obj.acquire_token_by_device_flow(flow)
    if "access_token" in result:
        with open(CACHE_FILE, "w") as f:
            f.write(cache.serialize())
        upload_token(FIREBASE_API_KEY, input_file=CACHE_FILE, user_id=uid)
        del user_flows[uid]
        return jsonify({"status": "done"})
    elif "error" in result:
        return jsonify({"status": "error", "error": result["error"]})
    else:
        return jsonify({"status": "pending"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
