import os
import json
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
    
    print(f"🚀 Starting auth for uid={uid}")

    cache = SerializableTokenCache()
    app_obj = PublicClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        token_cache=cache
    )

    # Initiate device flow
    flow = app_obj.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        print("❌ Failed to create device flow")
        return "❌ Failed to create device flow", 500

    # Store the flow and app instance
    user_flows[uid] = {
        'flow': flow,
        'cache': cache,
        'app': app_obj
    }
    
    print(f"✅ Device flow created for {uid}: {flow.get('user_code')}")

    return render_template_string("""
        <html>
            <head><title>Authorize App</title></head>
            <body style="font-family: sans-serif; padding: 2rem;">
                <h2>📩 Authorize Access</h2>
                <p>Click the link below and paste the code to allow email access:</p>
                <a href="{{ uri }}" target="_blank" style="font-size: 18px;">{{ uri }}</a>
                <h3>🔐 Your Code: <code style="font-size: 24px;">{{ code }}</code></h3>
                <p>This page will check automatically while you complete sign-in.</p>
                <div id="status">⏳ Waiting for authentication...</div>
                <script>
                  const uid = "{{ uid | safe }}";
                  console.log("✅ Polling initialized for UID:", uid);
                  
                  let pollCount = 0;
                  const maxPolls = 60; // 5 minutes max
            
                  const poll = async () => {
                    try {
                      pollCount++;
                      console.log(`📡 Poll attempt ${pollCount}/${maxPolls} for UID: ${uid}`);
                      
                      const res = await fetch(`/poll-token?uid=${encodeURIComponent(uid)}`);
                      
                      if (!res.ok) {
                        console.error(`❌ HTTP ${res.status}: ${res.statusText}`);
                        document.getElementById('status').innerHTML = `❌ Error: ${res.status} ${res.statusText}`;
                        return;
                      }
                      
                      const data = await res.json();
                      console.log("📡 Poll response:", data);
            
                      if (data.status === "done") {
                        document.getElementById('status').innerHTML = "✅ Email access granted! You can close this window.";
                        alert("✅ Email access granted!");
                      } else if (data.status === "error") {
                        console.error("❌ Token error:", data.error);
                        document.getElementById('status').innerHTML = `❌ Error: ${data.error}`;
                      } else if (data.status === "pending") {
                        document.getElementById('status').innerHTML = "⏳ Still waiting for authentication...";
                        if (pollCount < maxPolls) {
                          setTimeout(poll, 5000); // Poll every 5 seconds
                        } else {
                          document.getElementById('status').innerHTML = "⏰ Timeout: Please refresh and try again.";
                        }
                      } else if (data.status === "not_started") {
                        document.getElementById('status').innerHTML = "❌ Session not found. Please refresh.";
                      }
                    } catch (err) {
                      console.error("⚠️ Polling failed:", err);
                      document.getElementById('status').innerHTML = `⚠️ Network error: ${err.message}`;
                    }
                  };
                  
                  // Start polling after a short delay
                  setTimeout(poll, 2000);
                </script>
            </body>
        </html>
    """, uri=flow["verification_uri"], code=flow["user_code"], uid=uid)

@app.route("/poll-token")
def poll_token():
    uid = request.args.get("uid", "default_user")
    print(f"📡 /poll-token called for uid={uid}")
    
    if uid not in user_flows:
        print(f"❌ UID {uid} not found in user_flows")
        return jsonify({"status": "not_started"})

    flow_data = user_flows[uid]
    flow = flow_data['flow']
    cache = flow_data['cache']
    app_obj = flow_data['app']
    
    try:
        # This call will not block and will return immediately with current status
        result = app_obj.acquire_token_by_device_flow(flow)
        
        print(f"🔍 Token acquisition result keys: {list(result.keys())}")
        
        if "access_token" in result:
            print("✅ Access token acquired successfully")
            
            # Save token to cache file
            try:
                with open(CACHE_FILE, "w") as f:
                    f.write(cache.serialize())
                print(f"💾 Token cache saved to {CACHE_FILE}")
            except Exception as e:
                print(f"⚠️ Failed to save cache file: {e}")
            
            # Upload to Firebase
            try:
                upload_token(FIREBASE_API_KEY, input_file=CACHE_FILE, user_id=uid)
                print(f"☁️ Token uploaded to Firebase for {uid}")
            except Exception as e:
                print(f"⚠️ Failed to upload to Firebase: {e}")
            
            # Clean up
            del user_flows[uid]
            return jsonify({"status": "done"})
            
        elif "error" in result:
            error_msg = result.get("error", "Unknown error")
            error_desc = result.get("error_description", "")
            print(f"❌ Token acquisition error: {error_msg} - {error_desc}")
            
            # If it's authorization_pending, that's normal - keep polling
            if error_msg == "authorization_pending":
                return jsonify({"status": "pending"})
            else:
                # Clean up on actual error
                del user_flows[uid]
                return jsonify({"status": "error", "error": f"{error_msg}: {error_desc}"})
        else:
            print("⏳ Token acquisition still pending")
            return jsonify({"status": "pending"})
            
    except Exception as e:
        print(f"💥 Exception in poll_token: {e}")
        return jsonify({"status": "error", "error": str(e)})

@app.route("/health")
def health_check():
    return jsonify({"status": "healthy", "active_flows": len(user_flows)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Starting Flask app on port {port}")
    app.run(host="0.0.0.0", port=port, debug=True)
