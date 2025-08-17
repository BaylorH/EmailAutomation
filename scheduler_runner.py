import os
import json
import atexit
import base64
import requests
from urllib.parse import quote
from openpyxl import Workbook
from msal import ConfidentialClientApplication, SerializableTokenCache

from firebase_helpers import download_token, upload_token, upload_excel

from google.cloud import firestore
import re

# ─── Config ─────────────────────────────────────────────
CLIENT_ID         = os.getenv("AZURE_API_APP_ID")
CLIENT_SECRET     = os.getenv("AZURE_API_CLIENT_SECRET")
FIREBASE_API_KEY  = os.getenv("FIREBASE_API_KEY")
FIREBASE_BUCKET   = "email-automation-cache.firebasestorage.app"
AUTHORITY         = "https://login.microsoftonline.com/common"
SCOPES            = ["Mail.ReadWrite", "Mail.Send"]
TOKEN_CACHE       = "msal_token_cache.bin"

SUBJECT = "Weekly Questions"
BODY = (
    "Hi,\n\nPlease answer the following:\n"
    "1. How was your week?\n"
    "2. What challenges did you face?\n"
    "3. Any updates to share?\n\nThanks!"
)
THANK_YOU_BODY = "Thanks for your response."

if not CLIENT_ID or not CLIENT_SECRET or not FIREBASE_API_KEY:
    raise RuntimeError("❌ Missing required env vars")

# Firestore Admin client (uses GOOGLE_APPLICATION_CREDENTIALS)
_fs = firestore.Client()

# ─── Helper: detect HTML vs text ───────────────────────
_html_rx = re.compile(r"<[a-zA-Z/][^>]*>")

def _body_kind(script: str):
    if script and _html_rx.search(script):
        return "HTML", script
    return "Text", script or ""

# ─── Send email via Graph ──────────────────────────────
def send_email(headers, script: str, emails: list[str]):
    if not emails:
        return {"sent": [], "errors": {"_all": "No recipients"}}

    content_type, content = _body_kind(script)
    results = {"sent": [], "errors": {}}

    for addr in emails:
        payload = {
            "message": {
                "subject": "Client Outreach",
                "body": {"contentType": content_type, "content": content},
                "toRecipients": [{"emailAddress": {"address": addr}}],
            },
            "saveToSentItems": True,
        }
        try:
            r = requests.post(
                "https://graph.microsoft.com/v1.0/me/sendMail",
                headers=headers,
                json=payload,
                timeout=20,
            )
            r.raise_for_status()
            results["sent"].append(addr)
            print(f"✅ Sent to {addr}")
        except Exception as e:
            msg = str(e)
            print(f"❌ Failed to send to {addr}: {msg}")
            results["errors"][addr] = msg

    return results

# ─── Process outbox for one user ───────────────────────
def send_outboxes(user_id: str, headers):
    """
    Reads users/{uid}/outbox/* docs.
    Each doc should contain only:
      - assignedEmails: string[]
      - script:         string
    Success: delete the doc.
    Failure: keep the doc with { attempts += 1, lastError }.
    """
    outbox_ref = _fs.collection("users").document(user_id).collection("outbox")
    docs = list(outbox_ref.stream())

    if not docs:
        print("📭 Outbox empty")
        return

    print(f"📬 Found {len(docs)} outbox item(s)")
    for d in docs:
        data = d.to_dict() or {}
        emails = data.get("assignedEmails") or []
        script = data.get("script") or ""

        print(f"→ Sending outbox item {d.id} to {len(emails)} recipient(s)")

        try:
            res = send_email(headers, script, emails)
            any_errors = bool(res["errors"])

            if not any_errors and res["sent"]:
                d.reference.delete()
                print(f"🗑️  Deleted outbox item {d.id}")
            else:
                attempts = int(data.get("attempts") or 0) + 1
                d.reference.set(
                    {"attempts": attempts, "lastError": json.dumps(res["errors"])[:1500]},
                    merge=True,
                )
                print(f"⚠️  Kept item {d.id} with error; attempts={attempts}")

        except Exception as e:
            attempts = int(data.get("attempts") or 0) + 1
            d.reference.set(
                {"attempts": attempts, "lastError": str(e)[:1500]},
                merge=True,
            )
            print(f"💥 Error sending item {d.id}: {e}; attempts={attempts}")

# ─── Utility: List user IDs from Firebase ──────────────
def list_user_ids():
    url = f"https://firebasestorage.googleapis.com/v0/b/{FIREBASE_BUCKET}/o?prefix=msal_caches%2F&key={FIREBASE_API_KEY}"
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

# ─── Email Functions ───────────────────────────────────
def send_weekly_email(headers, to_addresses):
    for addr in to_addresses:
        payload = {
            "message": {
                "subject": SUBJECT,
                "body": {"contentType": "Text", "content": BODY},
                "toRecipients": [{"emailAddress": {"address": addr}}]
            },
            "saveToSentItems": True
        }
        resp = requests.post("https://graph.microsoft.com/v1.0/me/sendMail", headers=headers, json=payload)
        resp.raise_for_status()
        print(f"✅ Sent '{SUBJECT}' to {addr}")

def process_replies(headers, user_id):
    url = "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages"
    params = {
        '$filter': f"isRead eq false and startswith(subject,'Re: {SUBJECT}')",
        '$top': '10',
        '$orderby': 'receivedDateTime desc'
    }

    resp = requests.get(url, headers=headers, params=params)
    messages = resp.json().get("value", [])

    if not messages:
        print("ℹ️  No new replies.")
        return

    wb = Workbook()
    ws = wb.active
    ws.append(["Sender", "Response", "ReceivedDateTime"])

    for msg in messages:
        sender = msg["from"]["emailAddress"]["address"]
        body   = msg["body"]["content"].strip()
        dt     = msg["receivedDateTime"]

        reply_url = f"https://graph.microsoft.com/v1.0/me/messages/{msg['id']}/reply"
        reply_payload = {"message": {"body": {"contentType": "Text", "content": THANK_YOU_BODY}}}
        requests.post(reply_url, headers=headers, json=reply_payload)

        mark_read_url = f"https://graph.microsoft.com/v1.0/me/messages/{msg['id']}"
        requests.patch(mark_read_url, headers=headers, json={"isRead": True})

        ws.append([sender, body, dt])
        print(f"📥 Replied to and logged reply from {sender}")

    file = f"responses_{user_id}.xlsx"
    wb.save(file)
    upload_excel(FIREBASE_API_KEY, input_file=file)
    print(f"✅ Saved replies to {file}")

import hashlib

def debug_dump_cache(cache, label=""):
    raw = cache.serialize() or "{}"
    data = json.loads(raw)
    ats = data.get("AccessToken", {})
    rts = data.get("RefreshToken", {})
    ids = data.get("IdToken", {})
    print(f"\n🧪 Cache dump [{label}]")
    print(f"   AccessTokens:  {len(ats)}")
    print(f"   RefreshTokens: {len(rts)}")
    print(f"   IdTokens:      {len(ids)}")
    # Print RT metadata (safe)
    for k, v in rts.items():
        print("   ↳ RT key:", k)
        print("      client_id:", v.get("client_id"))
        print("      environment:", v.get("environment"))
        print("      home_account_id:", v.get("home_account_id"))

def extract_utid(home_account_id):
    # MSAL home_account_id format: <uid>.<utid>
    try:
        return (home_account_id or "").split(".")[1]
    except Exception:
        return None


# ─── Main Loop ─────────────────────────────────────────
def refresh_and_process_user(user_id: str):
    print(f"\n🔄 Processing user: {user_id}")

    # 1) Download & deserialize cache
    download_token(FIREBASE_API_KEY, output_file=TOKEN_CACHE, user_id=user_id)
    cache = SerializableTokenCache()
    with open(TOKEN_CACHE, "r") as f:
        cache.deserialize(f.read())

    # Debug counts
    debug_dump_cache(cache, label=user_id)

    # Ensure cache uploads back if mutated
    def _save_cache():
        if cache.has_state_changed:
            with open(TOKEN_CACHE, "w") as f:
                f.write(cache.serialize())
            upload_token(FIREBASE_API_KEY, input_file=TOKEN_CACHE, user_id=user_id)
            print(f"✅ Token cache uploaded for {user_id}")

    atexit.unregister(_save_cache)
    atexit.register(_save_cache)

    # 2) Create MSAL app (do this BEFORE using 'app')
    app = ConfidentialClientApplication(
        CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=AUTHORITY,              # starts with 'common'
        token_cache=cache,
    )

    # 3) Inspect RT vs env (now 'app' exists)
    cache_json = json.loads(cache.serialize() or "{}")
    rts = list((cache_json.get("RefreshToken") or {}).values())
    rt_client_id = rts[0].get("client_id") if rts else None
    rt_home      = rts[0].get("home_account_id") if rts else None
    rt_env       = rts[0].get("environment") if rts else None

    print(f"🔎 CLIENT_ID (scheduler env): {CLIENT_ID}")
    print(f"🔎 CLIENT_ID (in cache RT):   {rt_client_id}")
    print(f"🔎 Authority (scheduler):     {AUTHORITY}")
    print(f"🔎 RT env:                    {rt_env}")
    print(f"🔎 RT home_account_id:        {rt_home}")

    accts = app.get_accounts()
    print("👤 Accounts in cache:", [a.get("username") for a in accts] or "<none>")
    if not accts:
        print("⚠️ No account objects found; cache likely not matching this app/authority.")
        return
    account = accts[0]

    # 4) Try silent WITHOUT force, then WITH force
    result = app.acquire_token_silent(SCOPES, account=account)
    if not (result and "access_token" in result):
        result = app.acquire_token_silent(SCOPES, account=account, force_refresh=True)

    # 5) If still no token, retry with tenant-specific authority inferred from home_account_id
    def _extract_utid(home_account_id: str):
        try:
            return (home_account_id or "").split(".")[1]
        except Exception:
            return None

    if not (result and "access_token" in result) and rt_home:
        utid = _extract_utid(rt_home)
        if utid:
            tenant_auth = f"https://login.microsoftonline.com/{utid}"
            print(f"🧭 Retrying silent auth with tenant authority: {tenant_auth}")
            app_tenant = ConfidentialClientApplication(
                CLIENT_ID,
                client_credential=CLIENT_SECRET,
                authority=tenant_auth,
                token_cache=cache,
            )
            result = app_tenant.acquire_token_silent(SCOPES, account=account)
            if not (result and "access_token" in result):
                result = app_tenant.acquire_token_silent(SCOPES, account=account, force_refresh=True)

    # 6) Final check
    if not (result and "access_token" in result):
        if result:
            print("❌ Silent auth failed (dict):", result.get("error"), "-", result.get("error_description"))
            print("correlation_id:", result.get("correlation_id"), "trace_id:", result.get("trace_id"))
        else:
            print("❌ Silent auth failed: None (no result) — likely authority mismatch/CA policy.")
        return

    # 7) Success → proceed
    access_token = result["access_token"]
    print(f"🎯 Token acquired — preview: {access_token[:40]}")

    # Optional sanity on appid in JWT
    if access_token.count(".") == 2:
        decoded = decode_token_payload(access_token)
        appid = decoded.get("appid", "unknown")
        print(f"🔐 JWT appid: {appid}")

    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    # Do work
    send_weekly_email(headers, ["bp21harrison@gmail.com"])
    # process_replies(headers, user_id)
    # send_outboxes(user_id, headers)


# ─── Entry ─────────────────────────────────────────────
if __name__ == "__main__":
    all_users = list_user_ids()
    print(f"📦 Found {len(all_users)} token cache users: {all_users}")

    for uid in all_users:
        try:
            refresh_and_process_user(uid)
        except Exception as e:
            print(f"💥 Error for user {uid}:", str(e))
