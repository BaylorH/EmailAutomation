#!/usr/bin/env python3
"""
Email Integration Test Suite

This script provides programmatic access to real email accounts for testing
the email automation system. It can:

1. Inspect actual Graph API responses to understand data structures
2. Send test emails and track their IDs
3. Simulate broker replies
4. Verify conversation threading works correctly

Usage:
    # First, create a .env file in the project root with your credentials:
    # AZURE_API_APP_ID=your_app_id
    # AZURE_API_CLIENT_SECRET=your_secret
    # FIREBASE_API_KEY=your_firebase_key
    # OPENAI_API_KEY=your_openai_key

    # Inspect inbox and show raw API response structure
    python tests/email_integration_test.py inspect-inbox

    # Show details of conversation IDs and message IDs
    python tests/email_integration_test.py inspect-ids

    # Send a test email and capture its IDs
    python tests/email_integration_test.py send-test --to someone@example.com

    # Run full conversation test
    python tests/email_integration_test.py conversation-test --to test@example.com

    # List all available user accounts
    python tests/email_integration_test.py list-users

Requirements:
    - Valid MSAL token cache in Firebase
    - .env file OR environment variables set
"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from pathlib import Path

# Load .env file if it exists (before importing anything else)
def load_dotenv():
    """Load environment variables from .env file."""
    env_paths = [
        Path(__file__).parent.parent / ".env",  # Project root
        Path(__file__).parent / ".env",  # tests directory
        Path.home() / ".emailautomation.env"  # Home directory
    ]

    for env_path in env_paths:
        if env_path.exists():
            print(f"Loading environment from: {env_path}")
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if key and value:
                            os.environ[key] = value
            return True

    print("No .env file found. Checking environment variables...")
    return False

load_dotenv()

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msal import ConfidentialClientApplication, SerializableTokenCache
from firebase_helpers import download_token, upload_token
from email_automation.clients import list_user_ids, decode_token_payload
from email_automation.app_config import CLIENT_ID, CLIENT_SECRET, AUTHORITY, SCOPES, TOKEN_CACHE, FIREBASE_API_KEY
import requests


class EmailTestClient:
    """Client for testing email functionality with real Graph API."""

    def __init__(self, user_id: str = None):
        """Initialize with optional specific user ID."""
        self.user_id = user_id or self._get_default_user()
        self.access_token = None
        self.headers = None
        self._authenticate()

    def _get_default_user(self) -> str:
        """Get first available user ID."""
        users = list_user_ids()
        if not users:
            raise RuntimeError("No user accounts found in Firebase")
        return users[0]

    def _authenticate(self):
        """Authenticate and get access token."""
        print(f"Authenticating as user: {self.user_id}")

        download_token(FIREBASE_API_KEY, output_file=TOKEN_CACHE, user_id=self.user_id)

        cache = SerializableTokenCache()
        with open(TOKEN_CACHE, "r") as f:
            cache.deserialize(f.read())

        app = ConfidentialClientApplication(
            CLIENT_ID,
            client_credential=CLIENT_SECRET,
            authority=AUTHORITY,
            token_cache=cache
        )

        accounts = app.get_accounts()
        if not accounts:
            raise RuntimeError(f"No account found for {self.user_id}")

        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if not result or "access_token" not in result:
            raise RuntimeError(f"Silent auth failed for {self.user_id}")

        self.access_token = result["access_token"]
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

        # Decode and show account info
        if self.access_token.count(".") == 2:
            decoded = decode_token_payload(self.access_token)
            upn = decoded.get("upn", decoded.get("unique_name", "unknown"))
            print(f"Authenticated as: {upn}")

    def raw_get(self, endpoint: str, params: dict = None) -> dict:
        """Make raw GET request and return full response."""
        url = f"https://graph.microsoft.com/v1.0{endpoint}"
        resp = requests.get(url, headers=self.headers, params=params)
        return {
            "status_code": resp.status_code,
            "headers": dict(resp.headers),
            "body": resp.json() if resp.status_code == 200 else resp.text
        }

    def raw_post(self, endpoint: str, payload: dict) -> dict:
        """Make raw POST request and return full response."""
        url = f"https://graph.microsoft.com/v1.0{endpoint}"
        resp = requests.post(url, headers=self.headers, json=payload)
        body = None
        if resp.status_code in [200, 201, 202]:
            try:
                body = resp.json() if resp.text else {}
            except:
                body = {}
        else:
            body = resp.text
        return {
            "status_code": resp.status_code,
            "headers": dict(resp.headers),
            "body": body
        }

    def list_messages(self, folder: str = "inbox", top: int = 10,
                      filter_query: str = None) -> List[dict]:
        """List messages with all fields for inspection."""
        params = {
            "$top": top,
            "$orderby": "receivedDateTime desc",
            # Request ALL potentially useful fields
            "$select": ",".join([
                "id",
                "internetMessageId",
                "conversationId",
                "conversationIndex",
                "subject",
                "bodyPreview",
                "from",
                "toRecipients",
                "ccRecipients",
                "replyTo",
                "receivedDateTime",
                "sentDateTime",
                "hasAttachments",
                "importance",
                "isRead",
                "parentFolderId",
                "inferenceClassification",
                "internetMessageHeaders"
            ])
        }
        if filter_query:
            params["$filter"] = filter_query

        endpoint = "/me/messages" if folder == "all" else f"/me/mailFolders/{folder}/messages"
        result = self.raw_get(endpoint, params)

        if result["status_code"] != 200:
            print(f"Error: {result['body']}")
            return []

        return result["body"].get("value", [])

    def get_message_full(self, message_id: str) -> dict:
        """Get a single message with ALL fields including headers."""
        params = {
            "$select": ",".join([
                "id",
                "internetMessageId",
                "conversationId",
                "conversationIndex",
                "subject",
                "body",
                "bodyPreview",
                "from",
                "sender",
                "toRecipients",
                "ccRecipients",
                "bccRecipients",
                "replyTo",
                "receivedDateTime",
                "sentDateTime",
                "hasAttachments",
                "importance",
                "isRead",
                "isReadReceiptRequested",
                "isDeliveryReceiptRequested",
                "parentFolderId",
                "inferenceClassification",
                "flag",
                "internetMessageHeaders"
            ])
        }
        result = self.raw_get(f"/me/messages/{message_id}", params)
        return result["body"] if result["status_code"] == 200 else None

    def send_test_email(self, to: str, subject: str = None, body: str = None) -> dict:
        """Send a test email and return all IDs for tracking."""
        subject = subject or f"Test Email - {datetime.now().isoformat()}"
        body = body or f"This is a test email sent at {datetime.now().isoformat()}"

        # First create as draft to get IDs
        draft_payload = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to}}]
        }

        draft_result = self.raw_post("/me/messages", draft_payload)
        if draft_result["status_code"] not in [200, 201]:
            return {"error": f"Failed to create draft: {draft_result['body']}"}

        draft = draft_result["body"]
        draft_id = draft["id"]

        # Get full draft info including conversationId
        draft_full = self.get_message_full(draft_id)

        # Send the draft
        send_result = self.raw_post(f"/me/messages/{draft_id}/send", {})

        # Wait a moment and fetch from SentItems to get final IDs
        import time
        time.sleep(2)

        sent_items = self.list_messages("SentItems", top=5)
        sent_msg = None
        for msg in sent_items:
            if msg.get("subject") == subject:
                sent_msg = msg
                break

        return {
            "draft_id": draft_id,
            "draft_internet_message_id": draft_full.get("internetMessageId") if draft_full else None,
            "draft_conversation_id": draft_full.get("conversationId") if draft_full else None,
            "sent_message": sent_msg,
            "subject": subject
        }

    def reply_to_message(self, message_id: str, body: str) -> dict:
        """Reply to a message and track the IDs."""
        payload = {
            "message": {
                "body": {"contentType": "Text", "content": body}
            }
        }

        result = self.raw_post(f"/me/messages/{message_id}/reply", payload)
        return result

    def inspect_conversation_ids(self, top: int = 20):
        """Analyze conversation ID patterns across messages."""
        messages = self.list_messages("all", top=top)

        print("\n" + "="*80)
        print("CONVERSATION ID ANALYSIS")
        print("="*80)

        conversations = {}
        for msg in messages:
            conv_id = msg.get("conversationId", "NONE")
            if conv_id not in conversations:
                conversations[conv_id] = []
            conversations[conv_id].append({
                "subject": (msg.get("subject") or "")[:50],
                "from": msg.get("from", {}).get("emailAddress", {}).get("address", "") if msg.get("from") else "",
                "internet_message_id": msg.get("internetMessageId") or "",
                "date": msg.get("receivedDateTime") or msg.get("sentDateTime")
            })

        print(f"\nFound {len(conversations)} unique conversations in {top} messages:\n")

        for conv_id, msgs in conversations.items():
            print(f"\nConversation: {conv_id}")
            print(f"  Length: {len(conv_id)} chars")
            print(f"  Ends with '=': {conv_id.endswith('=')}")
            print(f"  Messages ({len(msgs)}):")
            for m in msgs:
                print(f"    - {m['date']}: {m['from']} - {m['subject']}")

    def test_filter_by_conversation_id(self, conversation_id: str) -> dict:
        """Test filtering by conversationId to understand the API behavior."""
        print(f"\nTesting $filter with conversationId: {conversation_id}")
        print(f"  ID length: {len(conversation_id)}")
        print(f"  Ends with '=': {conversation_id.endswith('=')}")

        # Try the filter
        params = {
            "$filter": f"conversationId eq '{conversation_id}'",
            "$top": 10,
            "$select": "id,subject,conversationId,internetMessageId"
        }

        result = self.raw_get("/me/messages", params)

        print(f"\nAPI Response:")
        print(f"  Status: {result['status_code']}")

        if result["status_code"] != 200:
            print(f"  Error: {result['body']}")

            # Try URL encoding the conversation ID
            import urllib.parse
            encoded_id = urllib.parse.quote(conversation_id, safe='')
            params_encoded = {
                "$filter": f"conversationId eq '{encoded_id}'",
                "$top": 10
            }
            result2 = self.raw_get("/me/messages", params_encoded)
            print(f"\n  Retry with URL-encoded ID:")
            print(f"  Status: {result2['status_code']}")
            if result2["status_code"] != 200:
                print(f"  Error: {result2['body']}")
        else:
            messages = result["body"].get("value", [])
            print(f"  Found {len(messages)} messages")
            for m in messages:
                print(f"    - {m.get('subject', '')[:40]}")

        return result


def cmd_list_users():
    """List all available user accounts."""
    users = list_user_ids()
    print(f"\nFound {len(users)} user accounts:")
    for uid in users:
        print(f"  - {uid}")


def cmd_inspect_inbox(args):
    """Inspect inbox messages and show raw structure."""
    client = EmailTestClient(args.user)
    messages = client.list_messages("inbox", top=args.top)

    print("\n" + "="*80)
    print(f"INBOX INSPECTION - {len(messages)} messages")
    print("="*80)

    for i, msg in enumerate(messages):
        print(f"\n--- Message {i+1} ---")
        print(json.dumps(msg, indent=2, default=str))


def cmd_inspect_ids(args):
    """Analyze ID patterns across messages."""
    client = EmailTestClient(args.user)
    client.inspect_conversation_ids(top=args.top)


def cmd_send_test(args):
    """Send a test email."""
    client = EmailTestClient(args.user)
    result = client.send_test_email(args.to, args.subject, args.body)

    print("\n" + "="*80)
    print("SENT TEST EMAIL")
    print("="*80)
    print(json.dumps(result, indent=2, default=str))


def cmd_test_filter(args):
    """Test filtering by conversation ID."""
    client = EmailTestClient(args.user)

    if args.conversation_id:
        conv_id = args.conversation_id
    else:
        # Get a conversation ID from recent messages
        messages = client.list_messages("inbox", top=5)
        if messages:
            conv_id = messages[0].get("conversationId")
        else:
            print("No messages found to test with")
            return

    if not conv_id:
        print("No conversationId found in recent messages")
        return

    # Test on /me/messages
    print("\n--- Testing /me/messages endpoint ---")
    client.test_filter_by_conversation_id(conv_id)

    # Also test on SentItems specifically (where the original error occurred)
    print("\n--- Testing /me/mailFolders/SentItems/messages endpoint ---")
    params = {
        "$filter": f"conversationId eq '{conv_id}'",
        "$top": 10,
        "$select": "id,subject,conversationId,internetMessageId"
    }
    result = client.raw_get("/me/mailFolders/SentItems/messages", params)
    print(f"Status: {result['status_code']}")
    if result["status_code"] == 200:
        messages = result["body"].get("value", [])
        print(f"Found {len(messages)} messages in SentItems")
        for m in messages:
            print(f"  - {(m.get('subject') or '')[:40]}")
    else:
        print(f"Error: {result['body']}")


def cmd_full_message(args):
    """Get full message details including headers."""
    client = EmailTestClient(args.user)

    if args.message_id:
        msg = client.get_message_full(args.message_id)
    else:
        # Get most recent message
        messages = client.list_messages("inbox", top=1)
        if not messages:
            print("No messages found")
            return
        msg = client.get_message_full(messages[0]["id"])

    print("\n" + "="*80)
    print("FULL MESSAGE DETAILS")
    print("="*80)
    print(json.dumps(msg, indent=2, default=str))


def cmd_conversation_test(args):
    """Run a full conversation test flow."""
    print("\n" + "="*80)
    print("CONVERSATION THREADING TEST")
    print("="*80)

    client = EmailTestClient(args.user)

    # Step 1: Send initial email
    print("\n1. Sending initial email...")
    test_subject = f"Thread Test - {datetime.now().strftime('%Y%m%d_%H%M%S')}"
    result = client.send_test_email(
        args.to,
        subject=test_subject,
        body="This is the initial email in a conversation test."
    )

    print(f"   Draft conversation ID: {result.get('draft_conversation_id')}")
    print(f"   Draft internet message ID: {result.get('draft_internet_message_id')}")

    if result.get("sent_message"):
        sent = result["sent_message"]
        print(f"   Sent conversation ID: {sent.get('conversationId')}")
        print(f"   Sent internet message ID: {sent.get('internetMessageId')}")

        # Test filtering by this conversation ID
        conv_id = sent.get("conversationId")
        if conv_id:
            print(f"\n2. Testing filter by conversation ID: {conv_id[:50]}...")
            client.test_filter_by_conversation_id(conv_id)

    print("\n" + "="*80)
    print("To complete the test:")
    print(f"  1. Reply to the email '{test_subject}' from {args.to}")
    print(f"  2. Run: python tests/email_integration_test.py --inspect-ids")
    print("="*80)


class GmailSender:
    """Send emails via Gmail SMTP to simulate broker replies."""

    def __init__(self, email: str, app_password: str):
        self.email = email
        self.app_password = app_password

    def send_reply(self, to: str, subject: str, body: str,
                   in_reply_to: str = None, references: str = None) -> bool:
        """Send an email (optionally as a reply to maintain threading)."""
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        msg = MIMEMultipart()
        msg['From'] = self.email
        msg['To'] = to
        msg['Subject'] = subject

        # Add threading headers if replying
        if in_reply_to:
            msg['In-Reply-To'] = in_reply_to
        if references:
            msg['References'] = references

        msg.attach(MIMEText(body, 'plain'))

        try:
            server = smtplib.SMTP('smtp.gmail.com', 587)
            server.starttls()
            server.login(self.email, self.app_password)
            server.sendmail(self.email, to, msg.as_string())
            server.quit()
            print(f"Sent email from {self.email} to {to}")
            return True
        except Exception as e:
            print(f"Failed to send via Gmail: {e}")
            return False


def cmd_full_e2e_test(args):
    """Run a complete end-to-end email conversation test.

    This test:
    1. Sends an initial email from Outlook (simulating Jill's outreach)
    2. Waits 2 minutes
    3. Sends a reply from Gmail (simulating broker response)
    4. Waits 2 minutes
    5. Runs the processing pipeline
    6. Verifies the conversation was tracked correctly
    """
    import time

    gmail_addr = os.environ.get('GMAIL_ADDRESS') or args.gmail
    gmail_pass = os.environ.get('GMAIL_APP_PASSWORD') or args.password

    if not gmail_addr or not gmail_pass:
        print("ERROR: Need Gmail credentials for broker simulation")
        print("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env")
        print("Or use --gmail and --password arguments")
        return

    print("\n" + "="*80)
    print("FULL END-TO-END EMAIL CONVERSATION TEST")
    print("="*80)

    # Step 1: Connect to Outlook
    print("\n[Step 1] Connecting to Outlook account...")
    client = EmailTestClient(args.outlook_user)
    outlook_email = "baylor.freelance@outlook.com"

    # Step 2: Send initial email from Outlook to Gmail
    test_id = datetime.now().strftime('%Y%m%d_%H%M%S')
    subject = f"E2E Test Property - {test_id}"
    body = f"""Hi,

I'm reaching out about a property listing at 123 Test Street.

Could you please provide the following details:
- Total SF
- Rent per SF
- Number of dock doors

Thanks!

Test ID: {test_id}
"""

    print(f"\n[Step 2] Sending initial email from Outlook to Gmail...")
    print(f"   From: {outlook_email}")
    print(f"   To: {gmail_addr}")
    print(f"   Subject: {subject}")

    result = client.send_test_email(gmail_addr, subject=subject, body=body)

    if not result.get("sent_message"):
        print("ERROR: Failed to send initial email")
        print(json.dumps(result, indent=2))
        return

    sent_msg = result["sent_message"]
    internet_msg_id = sent_msg.get("internetMessageId")
    conversation_id = sent_msg.get("conversationId")

    print(f"\n   Sent successfully!")
    print(f"   Internet Message ID: {internet_msg_id}")
    print(f"   Conversation ID: {conversation_id}")

    # Step 3: Wait 2 minutes
    print(f"\n[Step 3] Waiting 2 minutes before sending broker reply...")
    for i in range(120, 0, -30):
        print(f"   {i} seconds remaining...")
        time.sleep(30)

    # Step 4: Send reply from Gmail (simulating broker)
    print(f"\n[Step 4] Sending broker reply from Gmail...")
    broker_reply = f"""Hi,

Here are the details for 123 Test Street:

- Total SF: 15,000
- Rent: $8.50/SF NNN
- Dock doors: 4

Let me know if you need anything else.

Best,
Test Broker

Test ID: {test_id}
"""

    gmail_sender = GmailSender(gmail_addr, gmail_pass)
    reply_subject = f"Re: {subject}"

    success = gmail_sender.send_reply(
        to=outlook_email,
        subject=reply_subject,
        body=broker_reply,
        in_reply_to=internet_msg_id,
        references=internet_msg_id
    )

    if not success:
        print("ERROR: Failed to send broker reply")
        return

    print(f"   Sent broker reply!")

    # Step 5: Wait 2 minutes for email to arrive
    print(f"\n[Step 5] Waiting 2 minutes for reply to arrive...")
    for i in range(120, 0, -30):
        print(f"   {i} seconds remaining...")
        time.sleep(30)

    # Step 6: Check if reply arrived
    print(f"\n[Step 6] Checking for reply in Outlook inbox...")
    messages = client.list_messages("inbox", top=20)

    reply_found = None
    for msg in messages:
        if test_id in (msg.get("bodyPreview") or ""):
            reply_found = msg
            break

    if reply_found:
        print(f"   Found reply!")
        print(f"   Subject: {reply_found.get('subject')}")
        print(f"   From: {reply_found.get('from', {}).get('emailAddress', {}).get('address')}")
        print(f"   Conversation ID: {reply_found.get('conversationId')}")

        # Verify it's in the same conversation
        if reply_found.get('conversationId') == conversation_id:
            print(f"\n   THREADING: Correctly threaded in same conversation")
        else:
            print(f"\n   WARNING: Different conversation ID!")
            print(f"   Original: {conversation_id}")
            print(f"   Reply: {reply_found.get('conversationId')}")
    else:
        print("   Reply not found yet - may need more time")

    print("\n" + "="*80)
    print("TEST COMPLETE")
    print("="*80)
    print(f"\nTo process this email through the automation system:")
    print(f"  GOOGLE_APPLICATION_CREDENTIALS=./service-account.json python main.py")


def cmd_send_gmail_reply(args):
    """Send a simulated broker reply via Gmail."""
    gmail_addr = os.environ.get('GMAIL_ADDRESS') or args.gmail
    gmail_pass = os.environ.get('GMAIL_APP_PASSWORD') or args.password

    if not gmail_addr or not gmail_pass:
        print("ERROR: Need Gmail credentials")
        print("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env")
        print("Or use --gmail and --password arguments")
        return

    sender = GmailSender(gmail_addr, gmail_pass)

    # If replying to a specific message, get its headers
    in_reply_to = None
    references = None

    if args.reply_to_message_id:
        # This should be the internetMessageId of the message we're replying to
        in_reply_to = args.reply_to_message_id
        references = args.reply_to_message_id

    # Ensure subject has Re: prefix if it's a reply
    subject = args.subject
    if in_reply_to and not subject.lower().startswith('re:'):
        subject = f"Re: {subject}"

    success = sender.send_reply(
        to=args.to,
        subject=subject,
        body=args.body,
        in_reply_to=in_reply_to,
        references=references
    )

    if success:
        print(f"\nSent reply to {args.to}")
        print(f"Subject: {subject}")
        if in_reply_to:
            print(f"In-Reply-To: {in_reply_to}")
        print("\nWait 2+ minutes, then run the main.py to process the reply")


def main():
    parser = argparse.ArgumentParser(description="Email Integration Testing")
    parser.add_argument("--user", help="Specific user ID to authenticate as")

    subparsers = parser.add_subparsers(dest="command")

    # List users
    subparsers.add_parser("list-users", help="List available user accounts")

    # Inspect inbox
    p = subparsers.add_parser("inspect-inbox", help="Inspect inbox messages")
    p.add_argument("--top", type=int, default=5, help="Number of messages")

    # Inspect IDs
    p = subparsers.add_parser("inspect-ids", help="Analyze conversation ID patterns")
    p.add_argument("--top", type=int, default=20, help="Number of messages to analyze")

    # Send test
    p = subparsers.add_parser("send-test", help="Send a test email")
    p.add_argument("--to", required=True, help="Recipient email")
    p.add_argument("--subject", help="Email subject")
    p.add_argument("--body", help="Email body")

    # Test filter
    p = subparsers.add_parser("test-filter", help="Test conversationId filtering")
    p.add_argument("--conversation-id", help="Specific conversation ID to test")

    # Full message
    p = subparsers.add_parser("full-message", help="Get full message with headers")
    p.add_argument("--message-id", help="Specific message ID")

    # Conversation test
    p = subparsers.add_parser("conversation-test", help="Full conversation flow test")
    p.add_argument("--to", required=True, help="Email to send test to (should be accessible)")

    # Gmail reply (simulate broker)
    p = subparsers.add_parser("gmail-reply", help="Send a reply via Gmail (simulate broker)")
    p.add_argument("--to", required=True, help="Recipient (the Outlook account)")
    p.add_argument("--subject", required=True, help="Email subject")
    p.add_argument("--body", required=True, help="Email body")
    p.add_argument("--reply-to-message-id", help="internetMessageId to reply to (for threading)")
    p.add_argument("--gmail", help="Gmail address (or set GMAIL_ADDRESS env)")
    p.add_argument("--password", help="Gmail app password (or set GMAIL_APP_PASSWORD env)")

    # Run full e2e test
    p = subparsers.add_parser("full-e2e", help="Run complete end-to-end email test")
    p.add_argument("--gmail", help="Gmail address for broker simulation")
    p.add_argument("--password", help="Gmail app password")
    p.add_argument("--outlook-user", default="NO7lVYVp6BaplKYEfMlWCgBnpdh2",
                   help="User ID for baylor.freelance@outlook.com")

    args = parser.parse_args()

    if args.command == "list-users":
        cmd_list_users()
    elif args.command == "inspect-inbox":
        cmd_inspect_inbox(args)
    elif args.command == "inspect-ids":
        cmd_inspect_ids(args)
    elif args.command == "send-test":
        cmd_send_test(args)
    elif args.command == "test-filter":
        cmd_test_filter(args)
    elif args.command == "full-message":
        cmd_full_message(args)
    elif args.command == "conversation-test":
        cmd_conversation_test(args)
    elif args.command == "gmail-reply":
        cmd_send_gmail_reply(args)
    elif args.command == "full-e2e":
        cmd_full_e2e_test(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
