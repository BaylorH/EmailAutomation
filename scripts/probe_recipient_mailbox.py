#!/usr/bin/env python3
"""Did the mail actually arrive, and did it land in INBOX or in SPAM?

The agent-side mail search CANNOT see the spam folder and has produced three
false "not delivered" reports. This probe reads the recipient mailbox directly
over IMAP, checks INBOX *and* SPAM, and reports which folder holds the message.
A delivery result that did not check spam is not a delivery result.

    cd <repo>
    set -a; source .env; set +a
    python3 scripts/probe_recipient_mailbox.py --subject "SiteSift Rung1"
    python3 scripts/probe_recipient_mailbox.py --message-id "<abc@example>"

STRICTLY READ-ONLY. Mailboxes are opened with readonly=True, so nothing is
marked read, moved, flagged or deleted. It sends nothing.

It refuses to run against any mailbox other than the self-owned test account, so
a stray credential in the environment cannot point it at someone real.
"""
import argparse
import email
import imaplib
import os
import sys
from email.header import decode_header, make_header

IMAP_HOST = "imap.gmail.com"

# This probe may only ever read the self-owned test mailbox.
ALLOWED_PREFIX = "bp21harrison"
ALLOWED_DOMAIN = "@gmail.com"

# Gmail exposes spam under this name; the others are the ordinary landing spots.
FOLDERS = ["INBOX", "[Gmail]/Spam", "[Gmail]/All Mail"]


def mailbox_allowed(address: str) -> bool:
    """Same allow-list shape as the send-exposure audit: exact self-owned account
    or a plus-alias on it. endswith, never substring, so a lookalike domain such
    as 'bp21harrison@gmail.com.attacker.net' cannot pass."""
    a = (address or "").strip().lower()
    return a.startswith(ALLOWED_PREFIX) and a.endswith(ALLOWED_DOMAIN)


def q(value: str) -> str:
    """IMAP-quote a search value.

    imaplib passes criteria through literally, so an unquoted multi-word value
    such as SUBJECT 951 E FM 646 is parsed as five separate tokens and the server
    answers BAD. That failure surfaced as "NOT FOUND" — i.e. a FALSE NEGATIVE on
    delivery, which is the exact error class this probe exists to eliminate.
    """
    escaped = (value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _decode(raw) -> str:
    if raw is None:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def search(conn, folder: str, criteria: list) -> list:
    """Return parsed summaries of messages matching criteria in one folder."""
    # Quote the mailbox name: '[Gmail]/All Mail' contains a space, and an
    # unquoted SELECT of it answers BAD. That was silently swallowed as "folder
    # absent", so an archived message read as NOT DELIVERED — the same false
    # negative in a second disguise.
    try:
        status, _ = conn.select(q(folder), readonly=True)
        if status != "OK":
            return []
    except imaplib.IMAP4.error as e:
        raise RuntimeError(f"IMAP could not open {folder}: {e}") from e

    try:
        status, data = conn.search(None, *criteria)
    except imaplib.IMAP4.error as e:
        # Loud on purpose: a search that ERRORED must never be reported as
        # "no messages found". That is how a false negative is born.
        raise RuntimeError(f"IMAP search failed in {folder}: {e}") from e
    if status != "OK" or not data or not data[0]:
        return []

    out = []
    for num in data[0].split():
        status, msg_data = conn.fetch(num, "(BODY.PEEK[HEADER])")
        if status != "OK" or not msg_data or not msg_data[0]:
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        out.append({
            "folder": folder,
            "message_id": (msg.get("Message-ID") or "").strip(),
            "subject": _decode(msg.get("Subject")),
            "from": _decode(msg.get("From")),
            "to": _decode(msg.get("To")),
            "date": (msg.get("Date") or "").strip(),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--subject", help="substring of the Subject header to look for")
    ap.add_argument("--message-id", help="exact Message-ID to look for")
    ap.add_argument("--since", help='IMAP date, e.g. "21-Aug-2026", to narrow the search')
    args = ap.parse_args()

    if not args.subject and not args.message_id:
        print("error: pass --subject or --message-id", file=sys.stderr)
        return 2

    address = os.getenv("GMAIL_ADDRESS", "")
    password = os.getenv("GMAIL_APP_PASSWORD", "")
    if not address or not password:
        print("error: GMAIL_ADDRESS / GMAIL_APP_PASSWORD not in environment "
              "(set -a; source .env; set +a)", file=sys.stderr)
        return 2

    if not mailbox_allowed(address):
        print(f"REFUSING: {address!r} is not the self-owned test mailbox. "
              "This probe only reads the allow-listed account.", file=sys.stderr)
        return 2

    criteria = []
    if args.message_id:
        criteria += ["HEADER", "Message-ID", q(args.message_id)]
    if args.subject:
        criteria += ["SUBJECT", q(args.subject)]
    if args.since:
        criteria += ["SINCE", q(args.since)]

    conn = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        conn.login(address, password)
        hits = []
        for folder in FOLDERS:
            hits.extend(search(conn, folder, criteria))
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    print("=== RECIPIENT MAILBOX PROBE ===")
    if not hits:
        print("  NOT FOUND in INBOX, SPAM or All Mail.")
        print("  (A genuine negative — spam was checked. Confirm the instrument on a")
        print("   known-present message before trusting this as 'not delivered'.)")
        return 1

    # All Mail duplicates INBOX/Spam in Gmail; report the meaningful folder first.
    seen = {}
    for h in hits:
        key = h["message_id"] or (h["subject"], h["date"])
        if key not in seen or h["folder"] != "[Gmail]/All Mail":
            seen[key] = h

    spam = [h for h in seen.values() if h["folder"] == "[Gmail]/Spam"]
    for h in seen.values():
        where = "SPAM ⚠️" if h["folder"] == "[Gmail]/Spam" else h["folder"]
        print(f"  [{where}] {h['subject']!r}")
        print(f"      Message-ID: {h['message_id']}")
        print(f"      From: {h['from']}")
        print(f"      Date: {h['date']}")

    if spam:
        print(f"\n  ⚠️  {len(spam)} message(s) landed in SPAM, not the inbox.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
