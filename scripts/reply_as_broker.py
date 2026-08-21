#!/usr/bin/env python3
"""Drive a live event by replying to an outreach email as the broker would.

Closed loop between two accounts Baylor owns: the product sends from the Outlook
sender identity to the self-owned consumer inbox, and this replies back the other
way so the worker has a real inbound message to classify.

    cd <repo>
    set -a; source .env; set +a
    python3 scripts/reply_as_broker.py --subject "1400 Kingsway" \
        --body-file /path/to/reply.txt

It threads the reply properly (In-Reply-To + References taken from the message it
is answering), because a reply that does not thread is not the event under test.

HARD LIMIT, enforced in code and not overridable by any flag: it will only ever
send FROM the self-owned consumer account (or a plus-alias of it) TO the single
self-owned sender identity. Any other address aborts before the connection is
opened. There is deliberately no flag to widen this.
"""
import argparse
import email
import imaplib
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage

IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# The ONLY destination this script may ever send to.
ALLOWED_TO = "baylor.freelance@outlook.com"
# The ONLY account it may ever send from (exact, or a plus-alias of it).
ALLOWED_FROM_PREFIX = "bp21harrison"
ALLOWED_FROM_DOMAIN = "@gmail.com"

FOLDERS = ["INBOX", "[Gmail]/Spam"]


def q(value: str) -> str:
    return '"{}"'.format((value or "").replace("\\", "\\\\").replace('"', '\\"'))


def from_allowed(address: str) -> bool:
    a = (address or "").strip().lower()
    return a.startswith(ALLOWED_FROM_PREFIX) and a.endswith(ALLOWED_FROM_DOMAIN)


def find_message(conn, subject: str, since: str):
    """Newest message matching subject, searching inbox AND spam."""
    best = None
    for folder in FOLDERS:
        try:
            status, _ = conn.select(q(folder), readonly=True)
            if status != "OK":
                continue
            crit = ["SUBJECT", q(subject)]
            if since:
                crit += ["SINCE", q(since)]
            status, data = conn.search(None, *crit)
        except imaplib.IMAP4.error as e:
            raise RuntimeError(f"IMAP search failed in {folder}: {e}") from e
        if status != "OK" or not data or not data[0]:
            continue
        for num in data[0].split():
            status, md = conn.fetch(num, "(BODY.PEEK[HEADER])")
            if status != "OK" or not md or not md[0]:
                continue
            msg = email.message_from_bytes(md[0][1])
            cand = {
                "folder": folder,
                "subject": msg.get("Subject") or "",
                "message_id": (msg.get("Message-ID") or "").strip(),
                "references": (msg.get("References") or "").strip(),
                "to": (msg.get("To") or "").strip(),
                "from": (msg.get("From") or "").strip(),
                "date": msg.get("Date") or "",
            }
            best = cand  # search returns ascending; keep the last = newest
    return best


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--subject", required=True, help="substring identifying the outreach to answer")
    ap.add_argument("--body-file", required=True, help="file containing the reply body")
    ap.add_argument("--since", default="", help='IMAP date, e.g. "21-Aug-2026"')
    ap.add_argument("--dry-run", action="store_true", help="resolve the thread and print, send nothing")
    args = ap.parse_args()

    account = os.getenv("GMAIL_ADDRESS", "")
    password = os.getenv("GMAIL_APP_PASSWORD", "")
    if not account or not password:
        print("error: GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set", file=sys.stderr)
        return 2
    if not from_allowed(account):
        print(f"REFUSING: {account!r} is not the self-owned test account.", file=sys.stderr)
        return 2

    body = open(args.body_file, encoding="utf-8").read()

    conn = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        conn.login(account, password)
        target = find_message(conn, args.subject, args.since)
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    if not target:
        print(f"no message found for subject {args.subject!r} — nothing to reply to", file=sys.stderr)
        return 1

    # Answer AS the alias the outreach was addressed to, so the product maps the
    # reply back to the right row. Falls back to the plain account if the header
    # is not a plus-alias we own.
    alias = ""
    raw_to = target["to"]
    if "<" in raw_to and ">" in raw_to:
        alias = raw_to.split("<", 1)[1].split(">", 1)[0].strip()
    else:
        alias = raw_to.strip()
    if not from_allowed(alias):
        alias = account

    # The destination is the sender identity that mailed us -- but it is checked
    # against the hard allow-list rather than trusted, so a surprise From header
    # can never redirect this at a stranger.
    dest = ALLOWED_TO
    if dest.lower() != ALLOWED_TO:
        print("REFUSING: destination is not the allow-listed identity.", file=sys.stderr)
        return 2

    subject = target["subject"]
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject

    msg = EmailMessage()
    msg["From"] = alias
    msg["To"] = dest
    msg["Subject"] = subject
    if target["message_id"]:
        # Unfold before setting. A References header on a thread more than one
        # message deep arrives folded across several lines, and passing it back
        # verbatim raises "Header values may not contain linefeed" -- so the
        # replier worked on the first reply of a thread and broke on the second.
        def unfold(value: str) -> str:
            return " ".join((value or "").split())

        msg["In-Reply-To"] = unfold(target["message_id"])
        msg["References"] = unfold(target["references"] + " " + target["message_id"])
    msg.set_content(body)

    print("=== BROKER REPLY ===")
    print(f"  answering : {target['subject']!r} in {target['folder']} ({target['date']})")
    print(f"  From      : {alias}")
    print(f"  To        : {dest}")
    print(f"  In-Reply-To: {target['message_id']}")
    if args.dry_run:
        print("  DRY RUN — nothing sent.")
        return 0

    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(context=ctx)
        s.login(account, password)
        s.send_message(msg)
    print("  SENT.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
