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
SCOPES = ["mail.readwrite", "mail.send", "openid", "profile"]
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


def refresh_and_process_user(user_id: str):
    print(f"\n🔄 Processing user: {user_id}")
    
    # Check MSAL version first
    try:
        import msal
        print(f"🔍 MSAL version: {msal.__version__}")
    except:
        print("⚠️ Could not determine MSAL version")

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

    # 2) Extract tenant info from cache BEFORE creating any app
    cache_json = json.loads(cache.serialize() or "{}")
    rts = list((cache_json.get("RefreshToken") or {}).values())
    rt_client_id = rts[0].get("client_id") if rts else None
    rt_home = rts[0].get("home_account_id") if rts else None
    rt_env = rts[0].get("environment") if rts else None

    print(f"🔎 CLIENT_ID (scheduler env): {CLIENT_ID}")
    print(f"🔎 CLIENT_ID (in cache RT):   {rt_client_id}")
    print(f"🔎 Authority (scheduler):     {AUTHORITY}")
    print(f"🔎 RT env:                    {rt_env}")
    print(f"🔎 RT home_account_id:        {rt_home}")

    # CRITICAL: Check scopes in cached tokens vs requested scopes
    print(f"\n🔍 SCOPE ANALYSIS:")
    print(f"🔍 Requested scopes: {SCOPES}")
    
    # Check scopes in refresh tokens
    for rt_key, rt_data in (cache_json.get("RefreshToken") or {}).items():
        print(f"🔍 RT key parts: {rt_key}")
        # RT key format includes scopes, usually at the end
        if "--" in rt_key:
            scope_part = rt_key.split("--")[-1] if rt_key.split("--")[-1] else "no-scopes"
            print(f"🔍 RT scopes from key: '{scope_part}'")
        
    # Check scopes in access tokens (if any)
    ats = cache_json.get("AccessToken", {})
    for at_key, at_data in ats.items():
        print(f"🔍 AT key: {at_key}")
        if "target" in at_data:
            print(f"🔍 AT target scopes: {at_data['target']}")
    
    # Check scopes in ID tokens
    ids = cache_json.get("IdToken", {})
    for id_key, id_data in ids.items():
        print(f"🔍 ID token key: {id_key}")
        # ID tokens don't have scopes, but show for completeness
        
    print(f"🔍 Scopes comparison:")
    print(f"   Requesting: {' '.join(sorted(SCOPES))}")
    if rts:
        first_rt_key = list((cache_json.get("RefreshToken") or {}).keys())[0]
        if "--" in first_rt_key:
            cached_scope_str = first_rt_key.split("--")[-1]
            cached_scopes = cached_scope_str.replace("-", " ").split() if cached_scope_str != "no-scopes" else []
            print(f"   In cache:   {' '.join(sorted(cached_scopes))}")
            
            # Check for exact match
            if sorted(SCOPES) == sorted(cached_scopes):
                print("✅ SCOPES MATCH - not a scope issue")
            else:
                print("❌ SCOPES MISMATCH - this is likely the problem!")
                print("💡 SOLUTION: Request token with original scopes first, then use incremental consent")
        else:
            print("   In cache:   <could not parse from RT key>")
    print()

    # 3) Determine the correct authority to use
    utid = _extract_utid(rt_home) if rt_home else None
    if utid:
        # Use tenant-specific authority from the start
        auth_to_use = f"https://login.microsoftonline.com/{utid}"
        print(f"🧭 Using tenant-specific authority: {auth_to_use}")
    else:
        # Fall back to common
        auth_to_use = AUTHORITY
        print(f"🧭 Using common authority: {auth_to_use}")

    # 4) Create MSAL app with the correct authority
    app = ConfidentialClientApplication(
        CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=auth_to_use,
        token_cache=cache,
    )

    # 5) Get accounts from the app
    accts = app.get_accounts()
    print("👤 Accounts in cache:", [a.get("username") for a in accts] or "<none>")
    if not accts:
        print("⚠️ No account objects found; cache likely not matching this app/authority.")
        return
    
    account = accts[0]
    print(f"🔍 Using account: {account.get('username')} (account object type: {type(account)})")
    print(f"🔍 Account keys: {list(account.keys()) if hasattr(account, 'keys') else 'N/A'}")
    
    # Validate account is not None (critical for MSAL 1.23+)
    if account is None:
        print("❌ Account is None - this will cause acquire_token_silent to return None in MSAL 1.23+")
        return

    # 6) Try silent auth (first without force, then with force if needed)
    print("🔐 Attempting silent token acquisition...")
    print(f"🔍 Scopes: {SCOPES}")
    print(f"🔍 Account username: {account.get('username')}")
    
    result = app.acquire_token_silent(SCOPES, account=account)
    print(f"🔍 First attempt result type: {type(result)}, is None: {result is None}")
    
    if not (result and "access_token" in result):
        print("🔄 Silent auth failed, trying with force_refresh=True...")
        result = app.acquire_token_silent(SCOPES, account=account, force_refresh=True)
        print(f"🔍 Force refresh result type: {type(result)}, is None: {result is None}")
        
        # If still None, try with explicit parameters
        if result is None:
            print("🔄 Still None, trying with explicit username...")
            result = app.acquire_token_silent(
                SCOPES, 
                account=account,
                force_refresh=True,
                claims_challenge=None
            )

    # 7) If tenant-specific didn't work and we haven't tried common, try common as fallback
    if not (result and "access_token" in result) and auth_to_use != AUTHORITY:
        print("🔄 Tenant-specific failed, trying common authority as fallback...")
        app_common = ConfidentialClientApplication(
            CLIENT_ID,
            client_credential=CLIENT_SECRET,
            authority=AUTHORITY,
            token_cache=cache,
        )
        accts_common = app_common.get_accounts()
        print(f"👤 Common authority accounts: {[a.get('username') for a in accts_common] or '<none>'}")
        
        if accts_common:
            common_account = accts_common[0]
            print(f"🔍 Using common account: {common_account.get('username')} (type: {type(common_account)})")
            
            result = app_common.acquire_token_silent(SCOPES, account=common_account)
            print(f"🔍 Common auth result type: {type(result)}, is None: {result is None}")
            
            if not (result and "access_token" in result):
                result = app_common.acquire_token_silent(SCOPES, account=common_account, force_refresh=True)
                print(f"🔍 Common auth force refresh result type: {type(result)}, is None: {result is None}")

    # Alternative approach: Try without account parameter to see if we get better error info
    if not (result and "access_token" in result):
        print("🔄 All account-based attempts failed, trying acquire_token_by_refresh_token if available...")
        # This is a diagnostic attempt - check if we can get better error information
        try:
            # Try to extract refresh token and use it directly
            cache_data = json.loads(cache.serialize() or "{}")
            refresh_tokens = cache_data.get("RefreshToken", {})
            if refresh_tokens:
                print(f"🔍 Found {len(refresh_tokens)} refresh tokens in cache")
                # Log first RT for debugging (safely)
                first_rt_key = list(refresh_tokens.keys())[0]
                first_rt = refresh_tokens[first_rt_key]
                print(f"🔍 RT client_id: {first_rt.get('client_id')}")
                print(f"🔍 RT environment: {first_rt.get('environment')}")
                print(f"🔍 RT home_account_id: {first_rt.get('home_account_id')}")
                
                # Check if refresh token has expiration info
                rt_expires_on = first_rt.get("expires_on")
                if rt_expires_on:
                    import time
                    current_time = int(time.time())
                    rt_expires_on_int = int(rt_expires_on)
                    time_until_expiry = rt_expires_on_int - current_time
                    
                    print(f"🔍 RT expires_on: {rt_expires_on} (timestamp)")
                    print(f"🔍 Current time: {current_time} (timestamp)")
                    print(f"🔍 Time until RT expiry: {time_until_expiry} seconds")
                    
                    if time_until_expiry <= 0:
                        print("❌ REFRESH TOKEN HAS EXPIRED!")
                        print("   User needs to re-authenticate through your web app.")
                    elif time_until_expiry < 3600:  # Less than 1 hour
                        print(f"⚠️  REFRESH TOKEN EXPIRES SOON! ({time_until_expiry//60} minutes)")
                    else:
                        print(f"✅ Refresh token is still valid for {time_until_expiry//3600} hours")
                else:
                    print("⚠️ RT does not have expires_on field - checking cached_at")
                    cached_at = first_rt.get("cached_at")
                    if cached_at:
                        import time
                        current_time = int(time.time())
                        cached_at_int = int(cached_at)
                        age_seconds = current_time - cached_at_int
                        age_days = age_seconds // 86400
                        
                        print(f"🔍 RT cached_at: {cached_at} (timestamp)")
                        print(f"🔍 RT age: {age_days} days")
                        
                        # Refresh tokens typically last 90 days for personal accounts, 
                        # but can vary based on tenant policies
                        if age_days > 90:
                            print("❌ REFRESH TOKEN IS VERY OLD (>90 days) - likely expired!")
                            print("   User needs to re-authenticate through your web app.")
                        elif age_days > 60:
                            print("⚠️  REFRESH TOKEN IS OLD (>60 days) - may be nearing expiration")
                        else:
                            print(f"✅ Refresh token age seems reasonable ({age_days} days)")
                    else:
                        print("⚠️ No expiration or cached_at info available in RT")
                
        except Exception as e:
            print(f"⚠️ Error inspecting refresh tokens: {e}")

    # 8) Final check
    if not (result and "access_token" in result):
        if result:
            error_code = result.get("error")
            error_desc = result.get("error_description", "")
            
            print("❌ Silent auth failed (dict):", error_code, "-", error_desc)
            print("correlation_id:", result.get("correlation_id"), "trace_id:", result.get("trace_id"))
            
            # Check for specific error codes that indicate expiration or re-auth needed
            if error_code in ["invalid_grant"]:
                print("💡 ERROR ANALYSIS: 'invalid_grant' usually means:")
                print("   - Refresh token has expired")
                print("   - Refresh token has been revoked")  
                print("   - User changed password")
                print("   - Conditional Access policy changed")
                print("   → User needs to re-authenticate through your web app.")
                
            elif error_code in ["interaction_required", "consent_required"]:
                print("💡 ERROR ANALYSIS: This error suggests:")
                print("   - Additional consent is required")
                print("   - MFA/Conditional Access requires user interaction")
                print("   → User needs to re-authenticate through your web app.")
                
            elif error_code in ["expired_token"]:
                print("💡 ERROR ANALYSIS: 'expired_token' means:")
                print("   - The token in cache has expired")
                print("   → This should have been handled by force_refresh, but apparently failed")
                
            elif "expired" in error_desc.lower() or "invalid" in error_desc.lower():
                print("💡 ERROR ANALYSIS: Error description suggests token expiration/invalidity")
                print("   → User likely needs to re-authenticate through your web app.")
                
            else:
                print("💡 ERROR ANALYSIS: Unknown error - check Azure AD logs for more details")
                
        else:
            print("❌ Silent auth failed: None (no result)")
            print("💡 ANALYSIS: This typically means:")
            print("   - No matching account found in cache for this authority")
            print("   - Account object is None/invalid (MSAL 1.23+ requirement)")
            print("   - Authority mismatch between cache and app configuration")
            print("   - Refresh token may have expired (check expiration analysis above)")
        return

    # 9) Success → proceed
    access_token = result["access_token"]
    print(f"🎯 Token acquired — preview: {access_token[:40]}")

    # Optional sanity check on appid in JWT
    if access_token.count(".") == 2:
        try:
            decoded = decode_token_payload(access_token)
            appid = decoded.get("appid", "unknown")
            print(f"🔐 JWT appid: {appid}")
        except Exception as e:
            print(f"⚠️ Could not decode JWT: {e}")

    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    # Do work
    send_weekly_email(headers, ["bp21harrison@gmail.com"])
    # process_replies(headers, user_id)
    # send_outboxes(user_id, headers)

def _extract_utid(home_account_id: str):
    """Extract tenant ID from MSAL home_account_id format: <uid>.<utid>"""
    try:
        return (home_account_id or "").split(".")[1]
    except (IndexError, AttributeError):
        return None


# ─── Entry ─────────────────────────────────────────────
if __name__ == "__main__":
    all_users = list_user_ids()
    print(f"📦 Found {len(all_users)} token cache users: {all_users}")

    for uid in all_users:
        try:
            refresh_and_process_user(uid)
        except Exception as e:
            print(f"💥 Error for user {uid}:", str(e))