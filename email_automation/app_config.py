import os

# Azure/Microsoft Graph Config
CLIENT_ID = os.getenv("AZURE_API_APP_ID")
CLIENT_SECRET = os.getenv("AZURE_API_CLIENT_SECRET")
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = ["Mail.ReadWrite", "Mail.Send"]
TOKEN_CACHE = "msal_token_cache.bin"

# Firebase Config
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")
FIREBASE_BUCKET = "email-automation-cache.firebasestorage.app"

# OpenAI Config
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_ASSISTANT_MODEL = os.getenv("OPENAI_ASSISTANT_MODEL", "gpt-4o")

# Email Templates
SUBJECT = "Weekly Questions"
BODY = (
    "Hi,\n\nPlease answer the following:\n"
    "1. How was your week?\n"
    "2. What challenges did you face?\n"
    "3. Any updates to share?\n\nThanks!"
)
THANK_YOU_BODY = "Thanks for your response."

# Required fields for closing conversations
REQUIRED_FIELDS_FOR_CLOSE = [
    "Total SF","Rent/SF /Yr","Ops Ex /SF","Gross Rent",
    "Drive Ins","Docks","Ceiling Ht","Power"
]

# Validation
if not CLIENT_ID or not CLIENT_SECRET or not FIREBASE_API_KEY:
    raise RuntimeError("Missing required env vars")

if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY env var")