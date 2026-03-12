"""
Overnight Campaign Analysis Script
===================================
Run this in the morning to analyze the timing of emails sent during the overnight E2E test.

This will:
1. Fetch all emails from baylor.freelance Outlook (sent items)
2. Group by property/thread
3. Calculate timing gaps between outreach and each follow-up
4. Evaluate if the 1hr/2hr/3hr follow-up setup worked correctly
"""

import requests
from datetime import datetime, timezone
from collections import defaultdict
from msal import ConfidentialClientApplication, SerializableTokenCache

# Config - loads from environment or .env file
import os
from pathlib import Path

# Try to load from .env if it exists
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")
CLIENT_ID = os.getenv("AZURE_API_APP_ID")
CLIENT_SECRET = os.getenv("AZURE_API_CLIENT_SECRET")
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = ["https://graph.microsoft.com/Mail.ReadWrite"]
USER_ID = "NO7lVYVp6BaplKYEfMlWCgBnpdh2"  # baylor.freelance@outlook.com

def get_access_token():
    """Download token from Firebase and get access token."""
    object_path = f"msal_caches/{USER_ID}/msal_token_cache.bin"
    encoded_path = object_path.replace("/", "%2F")
    url = f"https://firebasestorage.googleapis.com/v0/b/email-automation-cache.firebasestorage.app/o/{encoded_path}?alt=media&key={FIREBASE_API_KEY}"

    r = requests.get(url)
    if r.status_code != 200:
        raise Exception(f"Failed to download token cache: {r.status_code}")

    cache = SerializableTokenCache()
    cache.deserialize(r.text)

    app = ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET,
        token_cache=cache
    )

    accounts = app.get_accounts()
    if not accounts:
        raise Exception("No accounts in cache")

    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        raise Exception("Failed to acquire token")

    return result["access_token"]

def fetch_emails(token, folder="sentitems", top=200):
    """Fetch emails from a folder."""
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder}/messages?$top={top}&$orderby=sentDateTime desc"

    all_messages = []
    while url:
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            print(f"Error fetching {folder}: {resp.status_code}")
            break
        data = resp.json()
        all_messages.extend(data.get("value", []))
        url = data.get("@odata.nextLink")

    return all_messages

def extract_property_from_subject(subject):
    """Extract property address from email subject."""
    # Remove RE:, FW:, etc.
    clean = subject
    for prefix in ["RE: ", "Re: ", "FW: ", "Fw: ", "RE:", "Re:", "FW:", "Fw:"]:
        clean = clean.replace(prefix, "")

    # Try to extract property address (before " - " or first part)
    if " - " in clean:
        return clean.split(" - ")[0].strip()
    return clean.strip()

def parse_datetime(dt_str):
    """Parse ISO datetime string."""
    if dt_str:
        # Handle various formats
        dt_str = dt_str.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(dt_str)
        except:
            return None
    return None

def format_duration(seconds):
    """Format seconds as human-readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds/60:.1f}m"
    else:
        hours = seconds / 3600
        return f"{hours:.2f}h"

def analyze_campaign():
    print("=" * 70)
    print("OVERNIGHT CAMPAIGN ANALYSIS")
    print("=" * 70)
    print()

    # Get token
    print("🔑 Getting access token...")
    token = get_access_token()
    print("✅ Got token")
    print()

    # Fetch sent emails
    print("📤 Fetching sent emails...")
    sent = fetch_emails(token, "sentitems", top=100)
    print(f"   Found {len(sent)} sent emails")
    print()

    # Fetch inbox (broker replies)
    print("📥 Fetching inbox...")
    inbox = fetch_emails(token, "inbox", top=100)
    print(f"   Found {len(inbox)} inbox emails")
    print()

    # Group sent emails by property
    properties = defaultdict(list)
    for email in sent:
        subject = email.get("subject", "")
        sent_time = parse_datetime(email.get("sentDateTime"))
        to_recipients = email.get("toRecipients", [])
        to_email = to_recipients[0].get("emailAddress", {}).get("address", "") if to_recipients else ""

        prop = extract_property_from_subject(subject)
        if prop and sent_time:
            properties[prop].append({
                "subject": subject,
                "sent_time": sent_time,
                "to": to_email,
                "is_followup": "following up" in email.get("body", {}).get("content", "").lower() or
                               "follow up" in email.get("body", {}).get("content", "").lower() or
                               "checking in" in email.get("body", {}).get("content", "").lower()
            })

    # Sort each property's emails by time
    for prop in properties:
        properties[prop].sort(key=lambda x: x["sent_time"])

    # Analyze timing
    print("=" * 70)
    print("TIMING ANALYSIS BY PROPERTY")
    print("=" * 70)
    print()

    timing_data = []

    for prop, emails in sorted(properties.items()):
        print(f"📍 {prop}")
        print("-" * 50)

        outreach_time = None
        followup_times = []

        for i, email in enumerate(emails):
            time_str = email["sent_time"].strftime("%Y-%m-%d %H:%M:%S")
            email_type = "Follow-up" if email["is_followup"] or i > 0 else "Outreach"

            if i == 0:
                outreach_time = email["sent_time"]
                print(f"   {email_type}: {time_str}")
            else:
                gap = (email["sent_time"] - emails[i-1]["sent_time"]).total_seconds()
                gap_from_outreach = (email["sent_time"] - outreach_time).total_seconds()
                followup_times.append(gap_from_outreach)
                print(f"   {email_type} #{i}: {time_str} (gap: {format_duration(gap)}, from outreach: {format_duration(gap_from_outreach)})")

        if len(emails) > 1:
            timing_data.append({
                "property": prop,
                "email_count": len(emails),
                "followup_gaps": followup_times
            })

        print()

    # Summary
    print("=" * 70)
    print("SUMMARY & EVALUATION")
    print("=" * 70)
    print()

    print(f"📊 Properties with outreach: {len(properties)}")
    print(f"📊 Properties with follow-ups: {len([p for p in properties.values() if len(p) > 1])}")
    print()

    # Evaluate follow-up timing accuracy
    if timing_data:
        print("⏱️  FOLLOW-UP TIMING ACCURACY:")
        print("-" * 50)

        # Expected: 1 hour, 2 hours, 3 hours from outreach
        expected_gaps = [3600, 7200, 10800]  # 1h, 2h, 3h in seconds

        for data in timing_data:
            print(f"\n   {data['property']}:")
            for i, actual_gap in enumerate(data["followup_gaps"]):
                expected = expected_gaps[i] if i < len(expected_gaps) else None
                if expected:
                    diff = actual_gap - expected
                    accuracy = 100 - abs(diff / expected * 100)
                    status = "✅" if abs(diff) < 600 else "⚠️"  # Within 10 min = good
                    print(f"      Follow-up #{i+1}: Expected {format_duration(expected)}, Actual {format_duration(actual_gap)}, Diff: {format_duration(abs(diff))} {status}")
                else:
                    print(f"      Follow-up #{i+1}: {format_duration(actual_gap)}")

        print()
        print("EVALUATION:")
        print("-" * 50)
        print("✅ = Within 10 minutes of expected time (good)")
        print("⚠️  = More than 10 minutes off (investigate)")
        print()
        print("Note: The scheduler runs every 30 minutes, so some variance is expected.")
        print("Follow-ups scheduled at exact times may be sent on the next scheduler run.")
    else:
        print("⚠️  No follow-ups detected yet. Campaign may still be running.")

    # Show inbox activity
    print()
    print("=" * 70)
    print("INBOX ACTIVITY (Broker Replies)")
    print("=" * 70)
    print()

    for email in inbox[:20]:  # Show latest 20
        subject = email.get("subject", "")
        recv_time = parse_datetime(email.get("receivedDateTime"))
        from_addr = email.get("from", {}).get("emailAddress", {}).get("address", "")
        time_str = recv_time.strftime("%Y-%m-%d %H:%M:%S") if recv_time else "?"
        print(f"   {time_str} | From: {from_addr}")
        print(f"   Subject: {subject}")
        print()

if __name__ == "__main__":
    analyze_campaign()
