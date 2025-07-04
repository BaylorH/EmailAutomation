# config.py
import os, json

# MSAL / Azure AD
CLIENT_ID     = os.getenv("AZURE_API_APP_ID")
CLIENT_SECRET = os.getenv("AZURE_API_CLIENT_SECRET")
TENANT_ID     = os.getenv("AZURE_TENANT_ID")
if not TENANT_ID:
    raise RuntimeError("AZURE_TENANT_ID is not set")
AUTHORITY     = f"https://login.microsoftonline.com/{TENANT_ID}"
GRAPH_SCOPES  = [
    "https://graph.microsoft.com/Mail.Send",
    "https://graph.microsoft.com/Mail.ReadWrite",
]

# Firebase Admin
from firebase_admin import credentials, initialize_app, firestore
sa_key = json.loads(os.getenv("FIREBASE_SA_KEY"))
initialize_app(credentials.Certificate(sa_key))
db = firestore.client()
