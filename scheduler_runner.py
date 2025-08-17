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

from datetime import datetime
import html
import re

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_BODY_TAG_RX = re.compile(r"<[^>]+>")  # crude HTML stripper for logs

def _strip_html(s: str) -> str:
    if not s:
        return ""
    # unescape entities then drop tags
    return _BODY_TAG_RX.sub("", html.unescape(s))

def _get_my_address(headers) -> str:
    r = requests.get(f"{GRAPH_BASE}/me", headers=headers, params={"$select":"mail,userPrincipalName"}, timeout=20)
    r.raise_for_status()
    me = r.json()
    return (me.get("mail") or me.get("userPrincipalName") or "").lower()

def get_sent_with_client_id(headers, client_id: str | None = None, top: int = 50):
    """
    Return recent sent messages that carry x-client-id (optionally match a specific client_id).
    Each item: {id, subject, to, sentDateTime, conversationId, internetMessageId, x_client_id}
    """
    params = {
        "$top": str(top),
        "$orderby": "sentDateTime desc",
        "$select": "id,subject,toRecipients,sentDateTime,conversationId,internetMessageId,internetMessageHeaders",
    }
    r = requests.get(f"{GRAPH_BASE}/me/mailFolders/SentItems/messages", headers=headers, params=params, timeout=20)
    r.raise_for_status()
    items = r.json().get("value", [])
    results = []
    for m in items:
        hdrs = m.get("internetMessageHeaders")
        # fallback fetch if headers missing
        if hdrs is None:
            r2 = requests.get(f"{GRAPH_BASE}/me/messages/{m['id']}",
                              headers=headers,
                              params={"$select":"internetMessageHeaders,subject,toRecipients,sentDateTime,conversationId,internetMessageId"},
                              timeout=20)
            if r2.ok:
                j = r2.json()
                m["internetMessageHeaders"] = hdrs = j.get("internetMessageHeaders", [])
                for k in ("subject","toRecipients","sentDateTime","conversationId","internetMessageId"):
                    m.setdefault(k, j.get(k))
            else:
                hdrs = []
        x_client = _get_header(hdrs, "x-client-id")
        if not x_client:
            continue
        if client_id and x_client != client_id:
            continue
        results.append({
            "id": m.get("id"),
            "subject": m.get("subject"),
            "to": [t["emailAddress"]["address"] for t in (m.get("toRecipients") or []) if "emailAddress" in t],
            "sentDateTime": m.get("sentDateTime"),
            "conversationId": m.get("conversationId"),
            "internetMessageId": m.get("internetMessageId"),
            "x_client_id": x_client
        })
    return results

def fetch_conversation_messages(headers, conversation_id: str, max_items: int = 200):
    """
    Fetch all messages for a conversationId, across folders.
    Returns list sorted by sentDateTime ascending.
    """
    # Pull in chunks (Graph paging); keep it simple for now
    messages = []
    url = f"{GRAPH_BASE}/me/messages"
    params = {
        "$filter": f"conversationId eq '{conversation_id}'",
        "$select": "id,subject,from,toRecipients,sentDateTime,receivedDateTime,conversationId,internetMessageId,body,bodyPreview",
        "$orderby": "sentDateTime asc",
        "$top": "50",
    }
    while True:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        messages.extend(data.get("value", []))
        if len(messages) >= max_items or "@odata.nextLink" not in data:
            break
        # page through
        url = data["@odata.nextLink"]
        params = None  # nextLink already has query
    # final sort (belt & suspenders)
    messages.sort(key=lambda m: (m.get("sentDateTime") or m.get("receivedDateTime") or ""))
    return messages

def log_conversation(messages: list[dict], my_address: str, label: str):
    """
    Print a readable transcript to console.
    """
    print(f"\n🧵 Conversation: {label} — {len(messages)} message(s)")
    for m in messages:
        frm = (m.get("from") or {}).get("emailAddress", {}).get("address", "").lower()
        to_addrs = [t["emailAddress"]["address"].lower() for t in (m.get("toRecipients") or []) if "emailAddress" in t]
        direction = "ME → THEM" if frm == my_address else "THEM → ME"
        ts = m.get("sentDateTime") or m.get("receivedDateTime") or ""
        subj = m.get("subject", "(no subject)")
        body = (m.get("body") or {}).get("content", "") or m.get("bodyPreview") or ""
        body_txt = _strip_html(body).strip()
        if len(body_txt) > 1200:
            body_txt = body_txt[:1200] + "…"

        print(f"— {direction} @ {ts}")
        print(f"   Subject: {subj}")
        print(f"   From: {frm}  To: {', '.join(to_addrs)}")
        print("   Body:")
        for line in body_txt.splitlines():
            print(f"     {line}")

def dump_conversations_for_client(headers, client_id: str | None = None, top_sent: int = 25):
    """
    Step 1: pull Sent with x-client-id (optionally filter specific client)
    Step 2: for each, fetch all messages in that conversation
    Step 3: console log the full thread
    """
    my_addr = _get_my_address(headers)
    sent_hits = get_sent_with_client_id(headers, client_id=client_id, top=top_sent)
    if not sent_hits:
        print("ℹ️ No sent items with x-client-id found in the window.")
        return

    # Deduplicate by conversationId (in case multiple sent messages exist in same thread)
    seen = set()
    for s in sent_hits:
        conv = s["conversationId"]
        if not conv or conv in seen:
            continue
        seen.add(conv)

        label = f"{s['subject']}  | x-client-id={s['x_client_id']}  | convId={conv}"
        msgs = fetch_conversation_messages(headers, conv, max_items=300)
        log_conversation(msgs, my_addr, label)


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

def _get_header(headers_list, name: str) -> str:
    """Case-insensitive lookup of an internet message header."""
    name_l = name.lower()
    for h in headers_list or []:
        if h.get("name", "").lower() == name_l:
            return h.get("value", "")
    return ""

def _tokenize_refs(refs_value: str) -> set[str]:
    # References header is space-separated message-ids like "<a@x> <b@y>"
    return set(t for t in (refs_value or "").split() if t.startswith("<") and t.endswith(">"))

def _load_recent_sent(headers, top: int = 100):
    url = "https://graph.microsoft.com/v1.0/me/mailFolders/SentItems/messages"
    params = {
        "$top": str(top),
        "$orderby": "sentDateTime desc",
        "$select": "id,subject,sentDateTime,conversationId,internetMessageId,internetMessageHeaders"
    }
    r = requests.get(url, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    items = r.json().get("value", [])
    by_imid, by_conv = {}, {}

    for m in items:
        # 🔹 Fallback: fetch headers if missing
        if m.get("internetMessageHeaders") is None:
            r2 = requests.get(
                f"https://graph.microsoft.com/v1.0/me/messages/{m['id']}",
                headers=headers,
                params={"$select": "internetMessageHeaders,subject,sentDateTime,conversationId,internetMessageId"},
                timeout=20
            )
            if r2.ok:
                j = r2.json()
                m["internetMessageHeaders"] = j.get("internetMessageHeaders", [])
                m.setdefault("subject", j.get("subject"))
                m.setdefault("sentDateTime", j.get("sentDateTime"))
                m.setdefault("conversationId", j.get("conversationId"))
                m.setdefault("internetMessageId", j.get("internetMessageId"))

        imid = m.get("internetMessageId")
        if imid:
            by_imid[imid] = m
        conv = m.get("conversationId")
        if conv:
            by_conv.setdefault(conv, []).append(m)

    return by_imid, by_conv


# ─── Send email via Graph ──────────────────────────────
def send_email(headers, script: str, emails: list[str], client_id: str | None = None):
    if not emails:
        return {"sent": [], "errors": {"_all": "No recipients"}}

    content_type, content = _body_kind(script)
    results = {"sent": [], "errors": {}}
    base = "https://graph.microsoft.com/v1.0"

    for addr in emails:
        msg = {
            "subject": "Client Outreach",
            "body": {"contentType": content_type, "content": content},
            "toRecipients": [{"emailAddress": {"address": addr}}],
        }
        if client_id:
            msg["internetMessageHeaders"] = [{"name": "x-client-id", "value": client_id}]

        try:
            # create draft (this is where custom headers are supported)
            r = requests.post(f"{base}/me/messages", headers=headers, json=msg, timeout=20)
            r.raise_for_status()
            draft_id = r.json()["id"]

            # send draft
            r = requests.post(f"{base}/me/messages/{draft_id}/send", headers=headers, timeout=20)
            r.raise_for_status()

            results["sent"].append(addr)
            print(f"✅ Sent to {addr} (x-client-id={client_id or 'n/a'})")
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
        clientId = (data.get("clientId") or "").strip()

        print(f"→ Sending outbox item {d.id} to {len(emails)} recipient(s) (clientId={clientId or 'n/a'})")

        try:
            res = send_email(headers, script, emails, client_id=clientId)
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

def scan_new_mail_and_find_client_from_sent(headers, only_unread: bool = True, top_inbox: int = 10, top_sent: int = 100):
    """
    For each recent inbound message:
      - detect reply via In-Reply-To/References
      - find the related SENT message
      - print the x-client-id from the SENT message's headers
    """
    # 1) Load Sent Items once
    by_imid, by_conv = _load_recent_sent(headers, top=top_sent)

    # 2) Load recent Inbox messages
    inbox_url = "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages"
    params = {
        "$top": str(top_inbox),
        "$orderby": "receivedDateTime desc",
        "$select": "id,subject,from,replyTo,receivedDateTime,conversationId,internetMessageId,internetMessageHeaders",
    }
    if only_unread:
        params["$filter"] = "isRead eq false"

    resp = requests.get(inbox_url, headers=headers, params=params, timeout=20)
    resp.raise_for_status()
    messages = resp.json().get("value", [])

    if not messages:
        print("📭 Inbox scan: no messages.")
        return

    print(f"🔎 Scanning {len(messages)} inbound message(s) and resolving x-client-id from Sent Items ...")

    for m in messages:
        hdrs = m.get("internetMessageHeaders") or []
        in_reply_to = _get_header(hdrs, "In-Reply-To")
        references  = _tokenize_refs(_get_header(hdrs, "References"))
        conv_id     = m.get("conversationId")
        subj        = m.get("subject", "(no subject)")
        frm         = (m.get("from") or {}).get("emailAddress", {}).get("address", "unknown")
        when        = m.get("receivedDateTime", "")

        # Try to locate the related SENT message
        sent_msg = None
        if in_reply_to and in_reply_to in by_imid:
            sent_msg = by_imid[in_reply_to]
        if not sent_msg and references:
            for ref in references:
                if ref in by_imid:
                    sent_msg = by_imid[ref]
                    break
        if not sent_msg and conv_id and conv_id in by_conv:
            # fallback: any message we sent in the same conversation
            # pick the most recent sent item for that conversation
            sent_msg = sorted(by_conv[conv_id], key=lambda x: x.get("sentDateTime",""), reverse=True)[0]

        print(f"• [{when}] from {frm} — {subj}")
        if in_reply_to:
            print(f"   ↪ In-Reply-To: {in_reply_to}")
        if references:
            preview = " ".join(list(references))[:120] + ("…" if len(" ".join(list(references))) > 120 else "")
            print(f"   ↪ References:  {preview}")
        if conv_id:
            print(f"   ↪ conversationId: {conv_id}")

        if sent_msg:
            x_client_id = _get_header(sent_msg.get("internetMessageHeaders", []), "x-client-id")
            imid_sent   = sent_msg.get("internetMessageId")
            subj_sent   = sent_msg.get("subject")
            print(f"   ✅ Matched SENT item: {subj_sent}")
            print(f"      internetMessageId={imid_sent}")
            if x_client_id:
                print(f"      🧩 x-client-id={x_client_id}")
            else:
                print("      (SENT item has no x-client-id header)")
        else:
            print("   ⚠️ Could not find a related SENT item (try increasing top_sent, or store the original imid at send-time)")


def scan_new_mail_for_client_header(headers, only_unread: bool = True, top: int = 10):
    """
    Scan recent Inbox messages and log whether custom x-headers are present.
    Intended as a diagnostic to confirm header behavior on replies.
    """
    base = "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages"
    params = {
        "$top": str(top),
        "$orderby": "receivedDateTime desc",
        "$select": "id,subject,from,replyTo,receivedDateTime,conversationId,internetMessageId,internetMessageHeaders",
    }
    if only_unread:
        params["$filter"] = "isRead eq false"

    resp = requests.get(base, headers=headers, params=params, timeout=20)
    resp.raise_for_status()
    messages = resp.json().get("value", [])

    if not messages:
        print("📭 Inbox scan: no messages.")
        return

    print(f"🔎 Scanning {len(messages)} inbound message(s) for x-client-id ...")

    for m in messages:
        hdrs = m.get("internetMessageHeaders")

        # Fallback: some tenants/clients require fetching the item again with a $select for headers
        if hdrs is None:
            r2 = requests.get(
                f"https://graph.microsoft.com/v1.0/me/messages/{m['id']}",
                headers=headers,
                params={"$select": "internetMessageHeaders,subject,from,receivedDateTime,conversationId,internetMessageId"},
                timeout=20
            )
            if r2.ok:
                j = r2.json()
                hdrs = j.get("internetMessageHeaders", [])
                # keep other fields if the first call omitted them
                m.setdefault("subject", j.get("subject"))
                m.setdefault("from", j.get("from"))
                m.setdefault("receivedDateTime", j.get("receivedDateTime"))
                m.setdefault("conversationId", j.get("conversationId"))
                m.setdefault("internetMessageId", j.get("internetMessageId"))
            else:
                hdrs = []

        in_reply_to = _get_header(hdrs, "In-Reply-To")
        references  = _get_header(hdrs, "References")
        x_client_id = _get_header(hdrs, "x-client-id")
        x_thread_id = _get_header(hdrs, "x-thread-id")

        is_reply = bool(in_reply_to or m.get("replyTo"))
        subj = m.get("subject", "(no subject)")
        frm  = (m.get("from") or {}).get("emailAddress", {}).get("address", "unknown")
        when = m.get("receivedDateTime", "")

        print(f"• {'REPLY' if is_reply else 'NEW  '} [{when}] from {frm} — {subj}")
        if in_reply_to:
            print(f"   ↪ In-Reply-To: {in_reply_to}")
        if references:
            preview = references[:120] + ("…" if len(references) > 120 else "")
            print(f"   ↪ References:  {preview}")

        if x_client_id or x_thread_id:
            print(f"   🧩 Custom headers found → x-client-id={x_client_id or '—'}; x-thread-id={x_thread_id or '—'}")
        else:
            print("   (no custom x-headers found)")


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

# ─── Main Loop ─────────────────────────────────────────
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
    print(f"🎯 Using {token_source}; expires_in≈{exp_secs}s — preview: {access_token[:40]}")

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

    # show *all* conversations you initiated that carry an x-client-id (recent window)
    dump_conversations_for_client(headers, client_id=None, top_sent=25)

    # …or focus a single client:
    # dump_conversations_for_client(headers, client_id="3WI5hjxYqmbOim2b1oQS", top_sent=50)

    # send_weekly_email(headers, ["bp21harrison@gmail.com"])
    # process_replies(headers, user_id)
    send_outboxes(user_id, headers)
    # scan_new_mail_for_client_header(headers, only_unread=True, top=10)
    scan_new_mail_and_find_client_from_sent(headers, only_unread=True, top_inbox=10, top_sent=100)


# ─── Entry ─────────────────────────────────────────────
if __name__ == "__main__":
    all_users = list_user_ids()
    print(f"📦 Found {len(all_users)} token cache users: {all_users}")

    for uid in all_users:
        try:
            refresh_and_process_user(uid)
        except Exception as e:
            print(f"💥 Error for user {uid}:", str(e))
