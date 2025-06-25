# get_token_and_upload.py

import os, sys
from msal import PublicClientApplication, SerializableTokenCache
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))  # Add ../ to import helpers

from firebase_helpers import upload_token

CLIENT_ID  = os.getenv("CLIENT_ID")
AUTHORITY  = "https://login.microsoftonline.com/256868d6-80ad-401e-b4fa-e7be6ec6446d"
SCOPES     = ["Mail.ReadWrite", "Mail.Send"]
CACHE_FILE = "msal_token_cache.bin"

if not CLIENT_ID:
    print("❌ set CLIENT_ID and rerun")
    sys.exit(1)

# 1) load or create cache
cache = SerializableTokenCache()
if os.path.exists(CACHE_FILE):
    cache.deserialize(open(CACHE_FILE).read())

# 2) build PublicClientApplication
app = PublicClientApplication(
    CLIENT_ID,
    authority=AUTHORITY,
    token_cache=cache
)

# 3) try silent first
accounts = app.get_accounts()
result = None
if accounts:
    result = app.acquire_token_silent(SCOPES, account=accounts[0])

# 4) fallback to device flow
if not result:
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "message" not in flow:
        print("❌ device-flow failed:", flow)
        sys.exit(1)

    print(flow["message"])
    print("\n🔗  Open this link (it pre-fills your code):\n", f"{flow['verification_uri']}?user_code={flow['user_code']}\n")

    result = app.acquire_token_by_device_flow(flow)

# 5) persist cache locally
with open(CACHE_FILE, "w") as f:
    f.write(cache.serialize())

# 6) report + upload
if result and "access_token" in result:
    print("✅ token acquired; cache saved to", CACHE_FILE)
    upload_token(CACHE_FILE)
    print("⬆️  token uploaded to Firebase via upload_token()")
else:
    print("❌ token error:", result)
    sys.exit(1)
