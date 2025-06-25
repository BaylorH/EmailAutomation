# get_token.py
import os, sys
from msal import PublicClientApplication, SerializableTokenCache

CLIENT_ID  = os.getenv("CLIENT_ID")
AUTHORITY  = "https://login.microsoftonline.com/256868d6-80ad-401e-b4fa-e7be6ec6446d"
SCOPES     = ["Mail.ReadWrite", "Mail.Send"]
CACHE_FILE = "msal.cache.bin"

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

       # ① the human‐friendly message
       print(flow["message"])

       # ② build (and print) a link that pre-fills the code for you:
       complete_url = (
           f"{flow['verification_uri']}?user_code={flow['user_code']}"
       )
       print("\n🔗  Open this link (it pre-fills your code):\n", complete_url, "\n")

       # now launch your browser at that URL, sign in/consent, then…
       result = app.acquire_token_by_device_flow(flow)

# 5) persist cache
with open(CACHE_FILE, "w") as f:
    f.write(cache.serialize())

# 6) report
if result and "access_token" in result:
    print("✅ token acquired; cache saved to", CACHE_FILE)
else:
    print("❌ token error:", result)
    sys.exit(1)
