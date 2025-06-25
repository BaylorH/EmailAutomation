# get_token_and_upload.py

import os, sys
from msal import PublicClientApplication, SerializableTokenCache
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))  # Add ../ to import helpers

from firebase_helpers import upload_token

print("AZURE_API_APP_ID =", os.getenv("AZURE_API_APP_ID"))
CLIENT_ID   = os.getenv("AZURE_API_APP_ID")
AUTHORITY   = "https://login.microsoftonline.com/common"
SCOPES      = ["Mail.ReadWrite", "Mail.Send"]
CACHE_FILE  = "msal_token_cache.bin"
REDIRECT_URI = "http://localhost:5000"

if not CLIENT_ID:
    print("❌ set CLIENT_ID and rerun")
    sys.exit(1)

# Load or create cache
cache = SerializableTokenCache()
if os.path.exists(CACHE_FILE):
    cache.deserialize(open(CACHE_FILE).read())

# MSAL App
app = PublicClientApplication(
    CLIENT_ID,
    authority=AUTHORITY,
    token_cache=cache
)

# Try silent auth
accounts = app.get_accounts()
result = None
if accounts:
    result = app.acquire_token_silent(SCOPES, account=accounts[0])

# Fallback to interactive login (with redirect URI)
# Fallback to interactive login
if not result:
    result = app.acquire_token_interactive(
        scopes=SCOPES
    )

# Save token
with open(CACHE_FILE, "w") as f:
    f.write(cache.serialize())

# Upload to Firebase
if result and "access_token" in result:
    print("✅ token acquired; cache saved to", CACHE_FILE)
    upload_token(CACHE_FILE)
    print("⬆️  token uploaded to Firebase via upload_token()")
else:
    print("❌ token error:", result)
    sys.exit(1)
