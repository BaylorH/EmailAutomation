import os, json
from flask import Flask, request, jsonify
from msal import PublicClientApplication, SerializableTokenCache
from firebase_helpers import upload_token

app = Flask(__name__)

CLIENT_ID = os.getenv("API_APP_ID")              # set via env
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES    = ["Mail.ReadWrite","Mail.Send"]
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")

cache = SerializableTokenCache()
msal_app = PublicClientApplication(
  CLIENT_ID, authority=AUTHORITY, token_cache=cache
)
flows = {}

@app.route("/start-device-flow", methods=["POST"])
def start_flow():
  uid = request.json["uid"]
  flow = msal_app.initiate_device_flow(scopes=SCOPES)
  flows[uid] = flow
  return jsonify({
    "message": flow["message"],
    "interval": flow["interval"],
    "user_code": flow["user_code"],
    "verification_uri": flow["verification_uri"]
  })

@app.route("/complete-device-flow", methods=["POST"])
def complete_flow():
  uid = request.json["uid"]
  flow = flows.get(uid)
  result = msal_app.acquire_token_by_device_flow(flow)
  if "access_token" in result:
    upload_token(
      FIREBASE_API_KEY,
      input_file=None,
      cache_content=cache.serialize(),
      user_id=uid
    )
    flows.pop(uid, None)
    return jsonify({"status":"ok"})
  if "error_description" in result and "admin" in result["error_description"].lower():
    admin_url = (
      f"https://login.microsoftonline.com/common/adminconsent?"
      f"client_id={CLIENT_ID}"
      f"&redirect_uri=https%3A%2F%2Fyourapp.com%2Foauth-callback"
    )
    return jsonify({"status":"admin_needed","url":admin_url}), 403
  return jsonify({"status":"failed","error":result}), 400

if __name__=="__main__":
  app.run(port=5001)
