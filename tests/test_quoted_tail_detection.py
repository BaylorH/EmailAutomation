"""Deterministic tests for quoted-tail detection edge cases in ai_processing.

Two LIVE-testing breaks left quoted prior-thread history glued to the fresh
inbound message, so a stale "leased / off the market" signal in the quote bled
into a fresh reply and fired property_unavailable:

  H36 — forwarded Outlook header with a BARE ``From:`` line (no <email> in angle
        brackets), so _QUOTE_FWD_HEADER_RE did not match.
  H37 — Gmail/Apple attribution line whose "wrote" is NOT at line end
        ("...wrote the following:"), so _QUOTE_ATTRIBUTION_RE ($-anchored) missed.

Each test drives _split_fresh_and_quoted / the suppression pipeline directly (no
live OpenAI), so behavior is model-independent. Fixtures use synthetic broker
names, emails, and addresses — the regex behavior under test does not depend on
the data being real, and committing live third-party contact info is a PII risk.
"""
import os
import sys
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from email_automation import ai_processing as a  # noqa: E402


def _conv(body, direction="inbound"):
    return [{"direction": direction, "from": "broker.demo@example-cre.com", "to": ["operator@example.com"],
             "subject": "Re: 123 Test Warehouse Way, Austin", "timestamp": "2026-07-05T00:00:00Z",
             "content": body}]


def _pipeline(events, body):
    """Mirror production order: suppress quote-only events, then augment."""
    proposal = {"events": [dict(e) for e in events], "updates": [],
                "response_email": "auto-reply body"}
    proposal = a._suppress_quote_only_events(proposal, _conv(body))
    proposal = a._augment_events_with_deterministic_signals(proposal, _conv(body))
    return [e.get("type") for e in proposal["events"]]


class OutlookForwardHeaderTests(unittest.TestCase):
    BODY = (
        "Still available - see the thread below for background.\n"
        "\n"
        "From: Riley Nolan\n"
        "Sent: Monday, June 1, 2026 3:00 PM\n"
        "To: Jill Anderson\n"
        "Subject: RE: 123 Test Warehouse Way, Austin\n"
        "123 Test Warehouse Way is now fully leased and off the market.")

    def test_h36_bare_outlook_header_splits_quote(self):
        fresh, quoted = a._split_fresh_and_quoted(self.BODY)
        self.assertIn("still available", fresh.lower())
        self.assertNotIn("fully leased", fresh.lower())
        self.assertIn("fully leased", quoted.lower())

    def test_h36_bare_outlook_header_suppresses_unavailable(self):
        types = _pipeline([{"type": "property_unavailable", "reason": "leased"}], self.BODY)
        self.assertNotIn("property_unavailable", types,
                         f"leased signal lives only in quoted Outlook forward: {types}")

    def test_control_prose_from_line_not_split(self):
        # A lone "From:" prose line with no Outlook block must stay fresh.
        _, quoted = a._split_fresh_and_quoted(
            "From: my perspective the space is still available and marketing.")
        self.assertEqual(quoted, "")

    def test_control_bracketed_from_header_still_splits(self):
        # Pre-existing bracketed-email header contract preserved.
        fresh, quoted = a._split_fresh_and_quoted(
            "forwarding my colleague's specs below.\n"
            "From: Dana Lee <dlee@cbre.com>\n8200 Trade Center Dr: 25,000 SF")
        self.assertIn("forwarding", fresh.lower())
        self.assertIn("dlee@cbre.com", quoted.lower())


class DatedAttributionTests(unittest.TestCase):
    BODY = (
        "Yes it's available, actively marketing.\n"
        "On Jun 1, 2026 at 3:00 PM Alex Carver wrote the following:\n"
        "456 Sample Industrial Blvd is fully leased, no longer on the market.")

    def test_h37_attribution_wrote_not_lineend_splits_quote(self):
        fresh, quoted = a._split_fresh_and_quoted(self.BODY)
        self.assertIn("actively marketing", fresh.lower())
        self.assertNotIn("fully leased", fresh.lower())
        self.assertIn("no longer on the market", quoted.lower())

    def test_h37_attribution_wrote_not_lineend_suppresses_unavailable(self):
        types = _pipeline([{"type": "property_unavailable", "reason": "leased"}], self.BODY)
        self.assertNotIn("property_unavailable", types,
                         f"leased signal lives only in quoted attribution tail: {types}")

    def test_control_on_wrote_without_date_not_split(self):
        # "On ... wrote" with no date/time token is casual prose, not an attribution.
        _, quoted = a._split_fresh_and_quoted(
            "On our recent call I wrote up the numbers; still available.")
        self.assertEqual(quoted, "")

    def test_control_strict_wrote_lineend_still_splits(self):
        fresh, quoted = a._split_fresh_and_quoted(
            "Sounds good.\n"
            "On Mon, Jun 1, 2026 at 3:00 PM Alex Carver <alex.carver@example-cre.com> wrote:\n"
            "> old quoted text")
        self.assertIn("sounds good", fresh.lower())
        self.assertIn("old quoted text", quoted.lower())


if __name__ == "__main__":
    unittest.main()
