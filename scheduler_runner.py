import os
import json
import atexit
import base64
import requests
import time
import logging
from urllib.parse import quote
from openpyxl import Workbook
from msal import ConfidentialClientApplication, SerializableTokenCache

from firebase_helpers import download_token, upload_token, upload_excel
from google.cloud import firestore
import re

# Enable MSAL logging for better debugging
logging.basicConfig(level=logging.DEBUG)
msal_logger = logging.getLogger("msal")
msal_logger.setLevel(logging.DEBUG)

# ─── Config ─────────────────────────────────────────────
CLIENT_ID = os.getenv("AZURE_API_APP_ID")
CLIENT_SECRET = os.getenv("AZURE_API_CLIENT_SECRET")
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")
FIREBASE_BUCKET = "email-automation-cache.firebasestorage.app"
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = ["mail.readwrite", "mail.send"]
TOKEN_CACHE = "msal_token_cache.bin"

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

# Firestore Admin client
_fs = firestore.Client()

# ─── Enhanced Token Validation ─────────────────────────
def validate_token_freshness(token_data):
    """Check if access token is fresh enough"""
    if not token_data or "expires_in" not in token_data:
        return False
    
    issued_at = token_data.get("cached_at", time.time())
    expires_in = token_data.get("expires_in", 3600)
    
    # Check if token expires in less than 5 minutes
    time_remaining = (issued_at + expires_in) - time.time()
    return time_remaining > 300  # 5 minutes buffer

def decode_token_payload(token):
    """Decode JWT payload for debugging"""
    try:
        payload = token.split(".")[1]
        padded = payload + '=' * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception as e:
        print(f"⚠️ Failed to decode token: {e}")
        return {}

# ─── Enhanced Cache Analysis ────────────────────────────
def analyze_cache_state(cache, client_id):
    """Perform deep analysis of token cache state"""
    cache_data = json.loads(cache.serialize() or "{}")
    
    access_tokens = cache_data.get("AccessToken", {})
    refresh_tokens = cache_data.get("RefreshToken", {})
    id_tokens = cache_data.get("IdToken", {})
    accounts = cache_data.get("Account", {})
    
    print(f"\n🔬 DEEP CACHE ANALYSIS:")
    print(f"   Access Tokens: {len(access_tokens)}")
    print(f"   Refresh Tokens: {len(refresh_tokens)}")
    print(f"   ID Tokens: {len(id_tokens)}")
    print(f"   Accounts: {len(accounts)}")
    
    # Analyze refresh tokens in detail
    for rt_key, rt_data in refresh_tokens.items():
        print(f"\n   🔍 Refresh Token Analysis:")
        print(f"      Key: {rt_key}")
        print(f"      Client ID: {rt_data.get('client_id')}")
        print(f"      Environment: {rt_data.get('environment')}")
        print(f"      Home Account ID: {rt_data.get('home_account_id')}")
        print(f"      Family ID: {rt_data.get('family_id', 'None')}")
        
        # Check expiration
        expires_on = rt_data.get("expires_on")
        cached_at = rt_data.get("cached_at")
        
        if expires_on:
            try:
                exp_time = int(expires_on)
                current_time = int(time.time())
                time_left = exp_time - current_time
                
                print(f"      Expires On: {exp_time} ({time.ctime(exp_time)})")
                print(f"      Time Remaining: {time_left} seconds ({time_left//3600}h {(time_left%3600)//60}m)")
                
                if time_left <= 0:
                    print("      ❌ REFRESH TOKEN EXPIRED!")
                    return False, "refresh_token_expired"
                elif time_left < 86400:  # Less than 24 hours
                    print("      ⚠️ REFRESH TOKEN EXPIRES SOON!")
            except ValueError:
                print(f"      ⚠️ Invalid expires_on format: {expires_on}")
        
        elif cached_at:
            try:
                cache_time = int(cached_at)
                current_time = int(time.time())
                age_seconds = current_time - cache_time
                age_days = age_seconds // 86400
                
                print(f"      Cached At: {cache_time} ({time.ctime(cache_time)})")
                print(f"      Age: {age_days} days, {(age_seconds%86400)//3600} hours")
                
                if age_days > 90:
                    print("      ❌ REFRESH TOKEN TOO OLD (>90 days)!")
                    return False, "refresh_token_too_old"
                elif age_days > 60:
                    print("      ⚠️ REFRESH TOKEN IS GETTING OLD (>60 days)")
            except ValueError:
                print(f"      ⚠️ Invalid cached_at format: {cached_at}")
        
        # Check client ID match
        rt_client_id = rt_data.get("client_id")
        if rt_client_id != client_id:
            print(f"      ❌ CLIENT ID MISMATCH!")
            print(f"         RT Client ID: {rt_client_id}")
            print(f"         App Client ID: {client_id}")
            return False, "client_id_mismatch"
        else:
            print(f"      ✅ Client ID matches")
    
    # Analyze accounts
    for acc_key, acc_data in accounts.items():
        print(f"\n   👤 Account Analysis:")
        print(f"      Username: {acc_data.get('username')}")
        print(f"      Environment: {acc_data.get('environment')}")
        print(f"      Authority Type: {acc_data.get('authority_type')}")
        print(f"      Home Account ID: {acc_data.get('home_account_id')}")
        print(f"      Local Account ID: {acc_data.get('local_account_id')}")
        print(f"      Realm: {acc_data.get('realm')}")
    
    return True, "cache_valid"

# ─── Authority Matching Logic ───────────────────────────
def determine_correct_authority(account, rt_data=None):
    """Determine the correct authority based on account type and token data"""
    username = account.get('username', '') if account else ''
    
    # Personal Microsoft account detection
    personal_domains = ('@outlook.com', '@hotmail.com', '@live.com', '@msn.com')
    is_personal = any(username.endswith(domain) for domain in personal_domains)
    
    if is_personal:
        return "https://login.microsoftonline.com/consumers", "personal"
    
    # Extract tenant ID from home_account_id or refresh token
    home_account_id = account.get('home_account_id') if account else None
    if not home_account_id and rt_data:
        home_account_id = rt_data.get('home_account_id')
    
    if home_account_id and '.' in home_account_id:
        try:
            tenant_id = home_account_id.split('.')[1]
            # Validate it looks like a GUID
            if len(tenant_id) == 36 and tenant_id.count('-') == 4:
                return f"https://login.microsoftonline.com/{tenant_id}", "organizational"
        except (IndexError, AttributeError):
            pass
    
    # Check realm from account
    realm = account.get('realm') if account else None
    if realm and realm not in ['common', 'consumers', 'organizations']:
        return f"https://login.microsoftonline.com/{realm}", "organizational"
    
    # Default to common
    return "https://login.microsoftonline.com/common", "common"

# ─── Enhanced Token Acquisition ─────────────────────────
def acquire_token_with_comprehensive_retry(client_id, client_secret, cache, scopes):
    """Try multiple strategies to acquire a token"""
    
    # First, analyze the cache
    cache_valid, cache_status = analyze_cache_state(cache, client_id)
    if not cache_valid:
        print(f"❌ Cache validation failed: {cache_status}")
        return None, f"Cache validation failed: {cache_status}"
    
    # Get accounts from cache using a temporary common authority app
    temp_app = ConfidentialClientApplication(
        client_id,
        client_credential=client_secret,
        authority="https://login.microsoftonline.com/common",
        token_cache=cache,
    )
    
    accounts = temp_app.get_accounts()
    if not accounts:
        print("❌ No accounts found in cache")
        return None, "No accounts found in cache"
    
    account = accounts[0]
    print(f"👤 Primary account: {account.get('username')}")
    
    # Get refresh token data for authority determination
    cache_data = json.loads(cache.serialize() or "{}")
    refresh_tokens = list(cache_data.get("RefreshToken", {}).values())
    rt_data = refresh_tokens[0] if refresh_tokens else None
    
    # Determine the correct authority
    correct_authority, auth_type = determine_correct_authority(account, rt_data)
    print(f"🧭 Determined authority: {correct_authority} (type: {auth_type})")
    
    # Create app with the correct authority
    app = ConfidentialClientApplication(
        client_id,
        client_credential=client_secret,
        authority=correct_authority,
        token_cache=cache,
    )
    
    # Get accounts from the correct authority app
    accounts = app.get_accounts()
    if accounts:
        account = accounts[0]
        print(f"✅ Account found with correct authority: {account.get('username')}")
    else:
        print("⚠️ No accounts with correct authority, trying with original account")
    
    # Strategy 1: Normal silent acquisition
    print("🔄 Strategy 1: Normal silent acquisition")
    result = app.acquire_token_silent(scopes, account=account)
    
    if result and "access_token" in result:
        print("✅ Strategy 1 successful")
        return result, None
    elif result and "error" in result:
        error_msg = f"{result.get('error')}: {result.get('error_description', '')}"
        print(f"❌ Strategy 1 failed with error: {error_msg}")
        return None, error_msg
    
    # Strategy 2: Force refresh
    print("🔄 Strategy 2: Force refresh")
    result = app.acquire_token_silent(scopes, account=account, force_refresh=True)
    
    if result and "access_token" in result:
        print("✅ Strategy 2 successful")
        return result, None
    elif result and "error" in result:
        error_msg = f"{result.get('error')}: {result.get('error_description', '')}"
        print(f"❌ Strategy 2 failed with error: {error_msg}")
        return None, error_msg
    
    # Strategy 3: Try alternative authorities
    alternative_authorities = [
        ("common", "https://login.microsoftonline.com/common"),
        ("consumers", "https://login.microsoftonline.com/consumers"),
        ("organizations", "https://login.microsoftonline.com/organizations")
    ]
    
    # Remove the current authority from alternatives
    alternative_authorities = [(name, url) for name, url in alternative_authorities 
                             if url != correct_authority]
    
    for auth_name, auth_url in alternative_authorities:
        print(f"🔄 Strategy 3.{auth_name}: Trying {auth_url}")
        
        alt_app = ConfidentialClientApplication(
            client_id,
            client_credential=client_secret,
            authority=auth_url,
            token_cache=cache,
        )
        
        alt_accounts = alt_app.get_accounts()
        if not alt_accounts:
            print(f"   No accounts found with {auth_name} authority")
            continue
        
        alt_account = alt_accounts[0]
        print(f"   Found account: {alt_account.get('username')}")
        
        # Try normal then force refresh
        for force_refresh in [False, True]:
            refresh_text = "with force_refresh" if force_refresh else "normal"
            result = alt_app.acquire_token_silent(
                scopes, 
                account=alt_account, 
                force_refresh=force_refresh
            )
            
            if result and "access_token" in result:
                print(f"✅ Strategy 3.{auth_name} successful ({refresh_text})")
                return result, None
            elif result and "error" in result:
                error_msg = f"{result.get('error')}: {result.get('error_description', '')}"
                print(f"❌ Strategy 3.{auth_name} failed ({refresh_text}): {error_msg}")
                # Continue trying other strategies
    
    # If we get here, all strategies failed
    return None, "All token acquisition strategies failed"

# ─── Main Processing Function ───────────────────────────
def refresh_and_process_user(user_id: str):
    print(f"\n🔄 Processing user: {user_id}")
    
    try:
        import msal
        print(f"🔍 MSAL version: {msal.__version__}")
    except:
        print("⚠️ Could not determine MSAL version")

    # Download and setup cache
    download_token(FIREBASE_API_KEY, output_file=TOKEN_CACHE, user_id=user_id)
    cache = SerializableTokenCache()
    
    try:
        with open(TOKEN_CACHE, "r") as f:
            cache_content = f.read().strip()
            if cache_content:
                cache.deserialize(cache_content)
                print("✅ Token cache loaded successfully")
            else:
                print("⚠️ Token cache file is empty")
                return
    except FileNotFoundError:
        print("❌ Token cache file not found")
        return
    except Exception as e:
        print(f"❌ Error loading token cache: {e}")
        return

    # Setup cache auto-save
    def save_cache():
        if cache.has_state_changed:
            try:
                with open(TOKEN_CACHE, "w") as f:
                    f.write(cache.serialize())
                upload_token(FIREBASE_API_KEY, input_file=TOKEN_CACHE, user_id=user_id)
                print(f"✅ Token cache uploaded for {user_id}")
            except Exception as e:
                print(f"❌ Failed to save cache: {e}")

    atexit.unregister(save_cache)
    atexit.register(save_cache)

    # Attempt comprehensive token acquisition
    result, error = acquire_token_with_comprehensive_retry(
        CLIENT_ID, CLIENT_SECRET, cache, SCOPES
    )
    
    if not result:
        print(f"❌ All token acquisition attempts failed: {error}")
        print("\n💡 RECOMMENDED ACTIONS:")
        print("   1. User needs to re-authenticate through your web application")
        print("   2. Check if user changed password or enabled 2FA")
        print("   3. Verify Azure AD app registration permissions")
        print("   4. Check for Conditional Access policy changes")
        return

    # Success!
    access_token = result["access_token"]
    print(f"🎯 Token acquired successfully!")
    
    # Validate token
    token_info = decode_token_payload(access_token)
    if token_info:
        print(f"🔐 Token info:")
        print(f"   App ID: {token_info.get('appid', 'unknown')}")
        print(f"   Audience: {token_info.get('aud', 'unknown')}")
        print(f"   Expires: {time.ctime(token_info.get('exp', 0)) if token_info.get('exp') else 'unknown'}")
        print(f"   Issuer: {token_info.get('iss', 'unknown')}")
    
    headers = {
        "Authorization": f"Bearer {access_token}", 
        "Content-Type": "application/json"
    }

    # Execute your business logic
    try:
        send_weekly_email(headers, ["bp21harrison@gmail.com"])
        # process_replies(headers, user_id)
        # send_outboxes(user_id, headers)
        print("✅ All operations completed successfully")
    except Exception as e:
        print(f"❌ Error during operations: {e}")
        import traceback
        traceback.print_exc()

# ─── Helper Functions (unchanged) ───────────────────────
def _body_kind(script: str):
    _html_rx = re.compile(r"<[a-zA-Z/][^>]*>")
    if script and _html_rx.search(script):
        return "HTML", script
    return "Text", script or ""

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
        body = msg["body"]["content"].strip()
        dt = msg["receivedDateTime"]

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

def send_outboxes(user_id: str, headers):
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

# ─── Entry Point ───────────────────────────────────────
if __name__ == "__main__":
    all_users = list_user_ids()
    print(f"📦 Found {len(all_users)} token cache users: {all_users}")

    for uid in all_users:
        try:
            refresh_and_process_user(uid)
        except Exception as e:
            print(f"💥 Error for user {uid}:", str(e))
            import traceback
            traceback.print_exc()