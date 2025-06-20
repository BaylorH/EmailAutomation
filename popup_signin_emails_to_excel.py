import os
import json
import atexit
from msal import PublicClientApplication, SerializableTokenCache
from firebase_helpers import upload_token

print("🔥 Starting local token saver")

# ─── Config ──────────────────────
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")

CLIENT_ID   = os.getenv("CLIENT_ID")
AUTHORITY   = "https://login.microsoftonline.com/common"
SCOPES      = ["Mail.ReadWrite", "Mail.Send"]
TOKEN_CACHE = "msal_token_cache.bin"

if not CLIENT_ID:
    raise RuntimeError("Missing CLIENT_ID")

print("✅ CLIENT_ID:", CLIENT_ID)

# ─── Token Cache ─────────────────
cache = SerializableTokenCache()
if os.path.exists(TOKEN_CACHE):
    cache.deserialize(open(TOKEN_CACHE, 'r').read())
    print("📁 Loaded existing token cache")

def _save_cache():
    if cache.has_state_changed:
        with open(TOKEN_CACHE, 'w') as f:
            f.write(cache.serialize())
        print("💾 Token cache saved to", TOKEN_CACHE)
atexit.register(_save_cache)

app = PublicClientApplication(
    CLIENT_ID,
    authority=AUTHORITY,
    token_cache=cache
)

# ─── Auth ────────────────────────
accounts = app.get_accounts()
result = None
if accounts:
    print("🔐 Trying silent sign-in...")
    result = app.acquire_token_silent(SCOPES, account=accounts[0])

if not result:
    print("🌐 No cached token found. Starting interactive login...")
    result = app.acquire_token_interactive(SCOPES, prompt="select_account")
    print("✅ Logged in successfully")

access_token = result.get("access_token")
if not access_token:
    raise RuntimeError(f"Token acquisition failed: {json.dumps(result, indent=2)}")

print("🟢 Access token acquired")

upload_token(FIREBASE_API_KEY, input_file="msal_token_cache.bin", user_id="default_user")
