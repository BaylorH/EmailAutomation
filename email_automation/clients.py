import os
import json
import base64
import requests
from google.cloud import firestore
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import openai
from .app_config import FIREBASE_API_KEY, OPENAI_API_KEY, OPENAI_ASSISTANT_MODEL

# Initialize clients
_fs = firestore.Client()
openai.api_key = OPENAI_API_KEY
client = openai.OpenAI(api_key=OPENAI_API_KEY)

def _helper_google_creds():
    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        raise RuntimeError("Missing GOOGLE_OAUTH_CLIENT_ID/SECRET/REFRESH_TOKEN")

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ],
    )
    creds.refresh(Request())
    return creds

def _sheets_client():
    creds = _helper_google_creds()
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return sheets

def _get_sheet_id_or_fail(uid: str, client_id: str) -> str:
    # Try active clients
    doc_ref = _fs.collection("users").document(uid).collection("clients").document(client_id)
    doc_snapshot = doc_ref.get()
    if doc_snapshot.exists:
        doc_data = doc_snapshot.to_dict() or {}
        sid = doc_data.get("sheetId")
        if sid:
            return sid

    # Try archived clients (emails might keep flowing after archive)
    archived_doc_ref = _fs.collection("users").document(uid).collection("archivedClients").document(client_id)
    archived_doc_snapshot = archived_doc_ref.get()
    if archived_doc_snapshot.exists:
        archived_doc_data = archived_doc_snapshot.to_dict() or {}
        sid = archived_doc_data.get("sheetId")
        if sid:
            return sid

    # Required by design → fail loudly
    raise RuntimeError(f"sheetId not found for uid={uid} clientId={client_id}. This field is required.")

def list_user_ids():
    url = f"https://firebasestorage.googleapis.com/v0/b/email-automation-cache.firebasestorage.app/o?prefix=msal_caches%2F&key={FIREBASE_API_KEY}"
    r = requests.get(url)
    data = r.json()
    user_ids = set()
    for item in data.get("items", []):
        parts = item["name"].split("/")
        if len(parts) == 3 and parts[0] == "msal_caches" and parts[2] == "msal_token_cache.bin":
            user_ids.add(parts[1])
    return list(user_ids)

def decode_token_payload(token):
    payload = token.split(".")[1]
    padded = payload + '=' * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))