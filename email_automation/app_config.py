import os

# E2E Test Mode - skips validation and uses mock values where needed
E2E_TEST_MODE = os.getenv("E2E_TEST_MODE") == "true"

# Azure/Microsoft Graph Config
CLIENT_ID = os.getenv("AZURE_API_APP_ID") or ("mock-client-id" if E2E_TEST_MODE else None)
CLIENT_SECRET = os.getenv("AZURE_API_CLIENT_SECRET") or ("mock-client-secret" if E2E_TEST_MODE else None)
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = ["Mail.ReadWrite", "Mail.Send"]
TOKEN_CACHE = "msal_token_cache.bin"

# Firebase Config
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY") or ("mock-firebase-key" if E2E_TEST_MODE else None)
FIREBASE_BUCKET = "email-automation-cache.firebasestorage.app"

# OpenAI Config
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or ("mock-openai-key" if E2E_TEST_MODE else None)
OPENAI_ASSISTANT_MODEL = os.getenv("OPENAI_ASSISTANT_MODEL", "gpt-5.2")

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
# NOTE: "Rent/SF /Yr" is NOT included - it should never be requested from clients
# NOTE: "Gross Rent" is NOT included - it's a formula on the sheet (H+I+G/12), not a value we collect
REQUIRED_FIELDS_FOR_CLOSE = [
    "Total SF", "Ops Ex /SF",
    "Drive Ins", "Docks", "Ceiling Ht", "Power"
]

# Email scanning configuration
# How far back to scan for emails (in hours)
# Set higher to catch delayed/overnight emails, lower for faster processing
# Default: 24 hours to handle overnight and weekend delays
INBOX_SCAN_WINDOW_HOURS = int(os.getenv("INBOX_SCAN_WINDOW_HOURS", "24"))

# Validation (skip in E2E test mode - mock values are used)
if not E2E_TEST_MODE:
    if not CLIENT_ID or not CLIENT_SECRET or not FIREBASE_API_KEY:
        raise RuntimeError("Missing required env vars")

    if not OPENAI_API_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY env var")