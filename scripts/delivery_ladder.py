#!/usr/bin/env python3
"""Bisect the SiteSift delivery drop by sending a ladder of message variants.

A plain-text probe from the sending account reached the self-owned test mailbox in
seconds, while the campaign message sent from the same account to the same mailbox was
accepted, filed in Sent Items with a real message id, never bounced, and never arrived.
The channel is therefore healthy and the MESSAGE is the variable.

Five things differ between the probe that landed and the campaign message that vanished.
This walks them on one at a time and records which rung stops arriving:

    1 plain      plain text, no HTML          (the control - this rung already landed once)
    2 signature  + the rendered signature block
    3 links      + a mailto: and an external website link
    4 headers    + the two custom internet headers the product stamps on every outbound
    5 campaign   the full campaign HTML

Rungs 2-5 are built with the product's OWN signature formatter and the SAME header names
the outreach path stamps, so a rung that fails indicts the product's real artifact rather
than an approximation of it.

    # BEFORE: the send-exposure audit must exit 0
    python3 scripts/delivery_ladder.py --to <self-owned-address> --dry-run
    python3 scripts/delivery_ladder.py --to <self-owned-address> --send
    # AFTER: the send-exposure audit must exit 0 again

--dry-run renders every rung and sends nothing. --send refuses without an explicit
--to; there is no default recipient and none is ever inferred, because the only
address this may contact is the self-owned one named in the session that runs it.

A provider send receipt is NOT delivery evidence. Each rung prints the rfc822 message
id it was assigned; assert arrival at the RECIPIENT by that id, or the rung is
unmeasured and the ladder has told you nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402

from email_automation.utils import format_email_body_with_footer  # noqa: E402

GRAPH = "https://graph.microsoft.com/v1.0"

# Prose the campaign actually uses, so rung 5 is the real thing rather than lorem.
OUTREACH_PROSE = """Hi,

I represent a client evaluating commercial space in the market. Could you please
confirm whether the space is currently available?

Could you also provide:

- Total SF
- Rent/SF/Yr
- Ops Ex / SF"""

PLAIN_PROBE = """Hi,

Quick check on this address - is the space still available?

Thanks."""

LINK_BLOCK = (
    '<p>You can reply here or reach me at '
    '<a href="mailto:baylor.freelance@outlook.com">baylor.freelance@outlook.com</a>. '
    'More about the practice at <a href="https://sitesiftai.com">sitesiftai.com</a>.</p>'
)


def _rungs(signature: Optional[str], signature_mode: Optional[str],
           user_email: Optional[str]) -> List[dict]:
    """The five rungs, each adding exactly ONE of the five differences."""
    signed_html = format_email_body_with_footer(
        OUTREACH_PROSE, signature, signature_mode, user_email=user_email
    )
    return [
        {
            "key": "1-plain",
            "why": "control; this rung is known to have arrived once",
            "content_type": "Text",
            "content": PLAIN_PROBE,
            "headers": [],
        },
        {
            "key": "2-signature",
            "why": "adds the rendered signature block (an HTML table)",
            "content_type": "HTML",
            "content": signed_html,
            "headers": [],
        },
        {
            "key": "3-links",
            "why": "adds a mailto: link and an external website link",
            "content_type": "HTML",
            "content": signed_html.replace("</body>", LINK_BLOCK + "</body>")
            if "</body>" in signed_html else signed_html + LINK_BLOCK,
            "headers": [],
        },
        {
            "key": "4-headers",
            "why": "adds the two custom internet headers stamped on every outbound",
            "content_type": "HTML",
            "content": signed_html.replace("</body>", LINK_BLOCK + "</body>")
            if "</body>" in signed_html else signed_html + LINK_BLOCK,
            # Same header NAMES the outreach path uses; values are ladder-local.
            "headers": [
                {"name": "x-client-id", "value": "delivery-ladder"},
                {"name": "x-row-anchor", "value": "rowNumber=1"},
            ],
        },
        {
            "key": "5-campaign",
            "why": "the full campaign message as production composes it",
            "content_type": "HTML",
            "content": signed_html.replace("</body>", LINK_BLOCK + "</body>")
            if "</body>" in signed_html else signed_html + LINK_BLOCK,
            "headers": [
                {"name": "x-client-id", "value": "delivery-ladder"},
                {"name": "x-row-anchor", "value": "rowNumber=1"},
            ],
        },
    ]


def _send_rung(access_token: str, recipient: str, subject: str, rung: dict) -> dict:
    """Create a draft, send it, then read the rfc822 id back off the sent item."""
    headers = {"Authorization": f"Bearer {access_token}",
               "Content-Type": "application/json"}
    msg = {
        "subject": subject,
        "body": {"contentType": rung["content_type"], "content": rung["content"]},
        "toRecipients": [{"emailAddress": {"address": recipient}}],
    }
    if rung["headers"]:
        msg["internetMessageHeaders"] = rung["headers"]

    draft = requests.post(f"{GRAPH}/me/messages", headers=headers, json=msg, timeout=30)
    draft.raise_for_status()
    draft_id = draft.json()["id"]

    sent = requests.post(f"{GRAPH}/me/messages/{draft_id}/send", headers=headers, timeout=30)
    sent.raise_for_status()

    # The draft id survives the send; read the rfc822 id off the sent item so the
    # recipient-side query has something exact to look for.
    rfc822 = None
    for _ in range(6):
        time.sleep(2)
        look = requests.get(f"{GRAPH}/me/messages/{draft_id}",
                            headers=headers, params={"$select": "internetMessageId"},
                            timeout=30)
        if look.status_code == 200:
            rfc822 = look.json().get("internetMessageId")
            if rfc822:
                break
    return {"rung": rung["key"], "rfc822MessageId": rfc822, "subject": subject}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", help="the self-owned recipient named in THIS session")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="render every rung, send nothing")
    mode.add_argument("--send", action="store_true", help="actually send the ladder")
    ap.add_argument("--subject-prefix", default="Ladder")
    ap.add_argument("--uid", default=os.environ.get("SITESIFT_UID", ""))
    ap.add_argument("--access-token", default=os.environ.get("GRAPH_ACCESS_TOKEN", ""))
    ap.add_argument("--only", help="run a single rung by key, e.g. 4-headers")
    args = ap.parse_args(argv)

    signature = signature_mode = user_email = None
    if args.uid:
        try:
            from email_automation.email_operations import _get_user_signature_settings
            signature, signature_mode, user_email = _get_user_signature_settings(args.uid)
        except Exception as exc:  # pragma: no cover - environment dependent
            print(f"⚠️  could not load the account's signature settings: {exc}")

    rungs = _rungs(signature, signature_mode, user_email)
    if args.only:
        rungs = [r for r in rungs if r["key"] == args.only]
        if not rungs:
            print(f"no rung named {args.only!r}")
            return 2

    if args.dry_run:
        for r in rungs:
            print("=" * 72)
            print(f"RUNG {r['key']}  --  {r['why']}")
            print(f"  contentType : {r['content_type']}")
            print(f"  headers     : {[h['name'] for h in r['headers']] or 'none'}")
            print(f"  body bytes  : {len(r['content'])}")
            print("-" * 72)
            print(r["content"][:600])
        print("=" * 72)
        print("dry run - nothing was sent")
        return 0

    if not args.to:
        print("REFUSING: --send requires an explicit --to.\n"
              "There is no default recipient and none is inferred. Pass the exact\n"
              "self-owned address named in this session.")
        return 2
    if not args.access_token:
        print("REFUSING: no Graph access token. Set GRAPH_ACCESS_TOKEN or pass "
              "--access-token.")
        return 2

    results = []
    for r in rungs:
        subject = f"{args.subject_prefix} {r['key']}"
        print(f"→ sending rung {r['key']} ({r['why']})")
        try:
            results.append(_send_rung(args.access_token, args.to, subject, r))
        except Exception as exc:
            results.append({"rung": r["key"], "error": str(exc)})
            print(f"  ✗ {exc}")
            continue
        print(f"  ✓ accepted, rfc822 id {results[-1]['rfc822MessageId']}")

    print("\n" + json.dumps(results, indent=2))
    print("\nA send receipt is NOT delivery. Query the RECIPIENT mailbox by each")
    print("rfc822 id above; a rung with no recipient-side hit is the rung that drops.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
