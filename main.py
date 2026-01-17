import atexit
import json
from msal import ConfidentialClientApplication, SerializableTokenCache
from firebase_helpers import download_token, upload_token
from email_automation.clients import list_user_ids, decode_token_payload, _fs
from email_automation.email import send_outboxes
from email_automation.processing import scan_inbox_against_index, scan_sent_items_for_manual_replies
from email_automation.app_config import CLIENT_ID, CLIENT_SECRET, AUTHORITY, SCOPES, TOKEN_CACHE, FIREBASE_API_KEY

# Thresholds for auto-cleanup (to stay within Firebase free tier)
PROCESSED_MESSAGES_THRESHOLD = 500
SHEET_CHANGELOG_THRESHOLD = 100


def auto_cleanup_firestore(user_id: str):
    """
    Automatically clean up Firestore collections if they exceed thresholds.
    This helps stay within Firebase free tier limits.
    """
    try:
        # Check processedMessages count
        pm_ref = _fs.collection("users").document(user_id).collection("processedMessages")
        pm_docs = list(pm_ref.limit(PROCESSED_MESSAGES_THRESHOLD + 1).stream())

        if len(pm_docs) > PROCESSED_MESSAGES_THRESHOLD:
            print(f"🧹 Auto-cleanup: processedMessages ({len(pm_docs)}+) exceeds threshold ({PROCESSED_MESSAGES_THRESHOLD})")
            # Delete all - safe to clear, just prevents duplicate processing temporarily
            all_pm = list(pm_ref.stream())
            for doc in all_pm:
                doc.reference.delete()
            print(f"   ✅ Deleted {len(all_pm)} processedMessages docs")

        # Check sheetChangeLog count
        cl_ref = _fs.collection("users").document(user_id).collection("sheetChangeLog")
        cl_docs = list(cl_ref.limit(SHEET_CHANGELOG_THRESHOLD + 1).stream())

        if len(cl_docs) > SHEET_CHANGELOG_THRESHOLD:
            print(f"🧹 Auto-cleanup: sheetChangeLog ({len(cl_docs)}+) exceeds threshold ({SHEET_CHANGELOG_THRESHOLD})")
            # Delete all - just audit logs
            all_cl = list(cl_ref.stream())
            for doc in all_cl:
                doc.reference.delete()
            print(f"   ✅ Deleted {len(all_cl)} sheetChangeLog docs")

    except Exception as e:
        print(f"⚠️ Auto-cleanup error for {user_id}: {e}")

def refresh_and_process_user(user_id: str):
    print(f"\n🔄 Processing user: {user_id}")

    download_token(FIREBASE_API_KEY, output_file=TOKEN_CACHE, user_id=user_id)

    cache = SerializableTokenCache()
    with open(TOKEN_CACHE, "r") as f:
        cache.deserialize(f.read())

    def _save_cache():
        if cache.has_state_changed:
            with open(TOKEN_CACHE, "w") as f:
                f.write(cache.serialize())
            upload_token(FIREBASE_API_KEY, input_file=TOKEN_CACHE, user_id=user_id)
            print(f"✅ Token cache uploaded for {user_id}")

    atexit.unregister(_save_cache)
    atexit.register(_save_cache)

    app = ConfidentialClientApplication(
        CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=AUTHORITY,
        token_cache=cache
    )

    accounts = app.get_accounts()
    if not accounts:
        print(f"⚠️ No account found for {user_id}")
        return

    # --- KEY CHANGE: do NOT force refresh; let MSAL use cached AT first ---
    before_state = cache.has_state_changed  # usually False right after deserialize
    result = app.acquire_token_silent(SCOPES, account=accounts[0])  # <-- no force_refresh
    after_state = cache.has_state_changed

    if not result or "access_token" not in result:
        print(f"❌ Silent auth failed for {user_id}")
        return

    access_token = result["access_token"]

    # Helpful logging: was it cached or refreshed?
    token_source = "refreshed_via_refresh_token" if (not before_state and after_state) else "cached_access_token"
    exp_secs = result.get("expires_in")
    print(f"🎯 Using {token_source}; expires_in≈{exp_secs}s – preview: {access_token[:40]}")

    # (Optional) sanity check on JWT-shaped token & appid
    if access_token.count(".") == 2:
        decoded = decode_token_payload(access_token)
        appid = decoded.get("appid", "unknown")
        if not appid.startswith("54cec"):
            print(f"⚠️ Unexpected appid: {appid}")
        else:
            print("✅ Token appid matches expected prefix")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # Process outbound emails (now with indexing)
    send_outboxes(user_id, headers)
    
    # Scan for client replies (inbox - catch all replies, not just unread)
    print(f"\n🔍 Scanning inbox for client replies...")
    scan_inbox_against_index(user_id, headers, only_unread=False, top=50)
    
    # Scan for Jill's manual replies (SentItems - catch manual replies we didn't index)
    print(f"\n📤 Scanning SentItems for manual replies...")
    scan_sent_items_for_manual_replies(user_id, headers, top=50)

    # Auto-cleanup Firestore if collections are getting large (stay within free tier)
    auto_cleanup_firestore(user_id)


if __name__ == "__main__":
    all_users = list_user_ids()
    print(f"📦 Found {len(all_users)} token cache users: {all_users}")

    for uid in all_users:
        try:
            refresh_and_process_user(uid)
        except Exception as e:
            print(f"💥 Error for user {uid}:", str(e))