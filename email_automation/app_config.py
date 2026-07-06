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
# Storage bucket is env-parameterizable for the Cloud Run Job runtime.
# Defaults to the historical hardcoded value so behavior is unchanged when
# FIREBASE_BUCKET is unset (GitHub Actions cron, local runs).
FIREBASE_BUCKET = os.getenv("FIREBASE_BUCKET", "email-automation-cache.firebasestorage.app")
FRONTEND_EMAIL_ACCESS_URL = os.getenv(
    "FRONTEND_EMAIL_ACCESS_URL",
    "https://email-automation-cache.web.app/email-access",
)

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
    "Drive Ins", "Docks", "Ceiling Ht", "Power",
    "Flyer / Link"
]

# Email scanning configuration
# How far back to scan for emails (in hours)
# Set higher to catch delayed/overnight emails, lower for faster processing
# Default: 24 hours to handle overnight and weekend delays
INBOX_SCAN_WINDOW_HOURS = int(os.getenv("INBOX_SCAN_WINDOW_HOURS", "24"))

DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://email-automation-cache.web.app",
    "https://sitesiftai.com",
    "https://www.sitesiftai.com",
    "https://sitesift.ai",
    "https://www.sitesift.ai",
]


def split_csv_env(name, fallback=None):
    raw = os.getenv(name)
    values = raw.split(",") if raw else (fallback or [])
    return [value.strip() for value in values if value and value.strip() and value.strip() != "*"]


def cors_origins(name="ALLOWED_CORS_ORIGINS"):
    origins = [*DEFAULT_CORS_ORIGINS, *split_csv_env(name, [])]
    return list(dict.fromkeys(origins))


def is_production_env():
    env = (
        os.getenv("FLASK_ENV")
        or os.getenv("APP_ENV")
        or os.getenv("ENV")
        or ""
    ).strip().lower()
    return env in {"prod", "production"}


def destructive_admin_routes_enabled():
    if is_production_env():
        return False
    return os.getenv("ENABLE_DESTRUCTIVE_ADMIN_ROUTES", "").strip().lower() == "true"


def legacy_flask_oauth_enabled():
    if is_production_env():
        return False
    return os.getenv("ENABLE_LEGACY_FLASK_OAUTH", "").strip().lower() == "true"


def legacy_flask_oauth_redirect_uri():
    if not legacy_flask_oauth_enabled():
        return None
    return os.getenv("LEGACY_FLASK_OAUTH_REDIRECT_URI")


# Validation (skip in E2E test mode - mock values are used)
if not E2E_TEST_MODE:
    if not CLIENT_ID or not CLIENT_SECRET or not FIREBASE_API_KEY:
        raise RuntimeError("Missing required env vars")

    if not OPENAI_API_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY env var")
