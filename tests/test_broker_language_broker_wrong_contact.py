"""
Pressure test for the broker_wrong_contact event class.

EVENT: broker_wrong_contact
  A broker replies "I'm not the right contact — reach out to <someone> (copied here)"
  or "looping in our team". Two safety invariants (stopIf) must hold on the
  reply-all audience that SiteSift builds afterward:

    stopIf #1: system emails a redirected contact WITHOUT approval
    stopIf #2: a SAFE cc teammate is DROPPED from reply-all

DETERMINISTIC GUARD UNDER TEST
  email_automation.email._filter_reply_all_draft_recipients
      - Keeps Microsoft Graph's reply-all audience but removes unsafe recipients
        (operator's own address, duplicates, invalid addresses, opted-out/blocked
        contacts) BEFORE the draft is sent.
      - This is the concrete, testable deterministic backstop for both stopIf
        conditions: it must KEEP a safe teammate cc (invariant #2) and DROP an
        opted-out / blocked redirected contact (invariant #1).

  NOTE on detection vs. handling:
      The classification of a broker message AS wrong_contact is LLM-only
      (ai_processing._augment_events_with_deterministic_signals has property_unavailable
      and tour backstops but NO wrong_contact backstop). So the *decision to pause*
      is not deterministically guarded. What IS deterministic — and what actually
      protects the two stopIf conditions on any reply that does go out — is the
      reply-all recipient filter. That is the function this test drives on every
      broker phrasing (each phrasing encoded as the reply-all audience it produces).

External boundaries patched: is_contact_opted_out (Firestore-backed). No Graph /
Sheets / Firestore / real sends are touched.
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import email as email_mod


OPERATOR = "broker.user@myfirm.com"


def graph_recipient(address, name=None):
    ea = {"address": address}
    if name:
        ea["name"] = name
    return {"emailAddress": ea}


def run_filter(draft, opted_out=None, user_email=OPERATOR):
    """
    Drive the REAL _filter_reply_all_draft_recipients with is_contact_opted_out
    patched to reflect an opt-out/block list.
    """
    opted_out = {(e or "").strip().lower() for e in (opted_out or [])}

    def fake_opted_out(user_id, addr):
        if (addr or "").strip().lower() in opted_out:
            return {"reason": "do_not_contact"}
        return None

    # _filter_reply_all_draft_recipients does `from .processing import is_contact_opted_out`
    with mock.patch("email_automation.processing.is_contact_opted_out", side_effect=fake_opted_out):
        return email_mod._filter_reply_all_draft_recipients(
            "user-123", draft, user_email=user_email
        )


def kept_addresses(result):
    return {a.strip().lower() for a in result.get("sentRecipients", []) if a}


class BrokerWrongContactReplyAllSafety(unittest.TestCase):
    # ------------------------------------------------------------------ #
    # REAL-THREAT GROUP A: safe teammate cc MUST be KEPT (stopIf #2)
    # Each broker phrasing implies a reply-all audience with a legit teammate
    # copied. Dropping that teammate = false_negative on "don't drop safe cc".
    # ------------------------------------------------------------------ #

    def test_seed_reach_out_to_alex_copied_here(self):
        # "I am not the leasing contact; please reach out to Alex copied here."
        draft = {
            "toRecipients": [graph_recipient("original.broker@brokerage.com", "Pat Broker")],
            "ccRecipients": [graph_recipient("alex@brokerage.com", "Alex Leasing")],
        }
        kept = kept_addresses(run_filter(draft))
        self.assertIn("alex@brokerage.com", kept,
                      "Safe teammate Alex (copied by broker) was dropped from reply-all")
        self.assertIn("original.broker@brokerage.com", kept)

    def test_seed_looping_in_agency_team(self):
        # "Looping in our agency team who handles this listing."
        draft = {
            "toRecipients": [graph_recipient("first.broker@agency.com")],
            "ccRecipients": [
                graph_recipient("teamlead@agency.com", "Team Lead"),
                graph_recipient("associate@agency.com", "Associate"),
            ],
        }
        kept = kept_addresses(run_filter(draft))
        self.assertIn("teamlead@agency.com", kept)
        self.assertIn("associate@agency.com", kept)

    def test_seed_wrong_person_but_sarah_can_help(self):
        # "Wrong person, but Sarah can help."
        draft = {
            "toRecipients": [graph_recipient("wrongguy@brokerage.com")],
            "ccRecipients": [graph_recipient("sarah@brokerage.com", "Sarah H")],
        }
        kept = kept_addresses(run_filter(draft))
        self.assertIn("sarah@brokerage.com", kept)

    def test_terse_see_cc(self):
        # terse: "not me — see cc"
        draft = {
            "toRecipients": [graph_recipient("nope@brokerage.com")],
            "ccRecipients": [graph_recipient("realcontact@brokerage.com")],
        }
        kept = kept_addresses(run_filter(draft))
        self.assertIn("realcontact@brokerage.com", kept)

    def test_allcaps_mixed_case_cc(self):
        # ALL CAPS "PLEASE CONTACT ALEX@BROKERAGE.COM" — cc address arrives mixed-case
        draft = {
            "toRecipients": [graph_recipient("Sender@Brokerage.com")],
            "ccRecipients": [graph_recipient("ALEX@Brokerage.COM", "Alex")],
        }
        kept = kept_addresses(run_filter(draft))
        self.assertIn("alex@brokerage.com", kept,
                      "Mixed-case teammate address dropped instead of normalized+kept")

    def test_teammate_as_plain_string_recipient(self):
        # some Graph payloads arrive as bare strings
        draft = {
            "toRecipients": ["broker@brokerage.com"],
            "ccRecipients": ["helper@brokerage.com"],
        }
        kept = kept_addresses(run_filter(draft))
        self.assertIn("helper@brokerage.com", kept)

    def test_teammate_display_name_preserved(self):
        draft = {
            "toRecipients": [graph_recipient("broker@brokerage.com")],
            "ccRecipients": [graph_recipient("alex.chen@brokerage.com", "Alex Chen")],
        }
        result = run_filter(draft)
        cc = result["payload"]["ccRecipients"]
        match = [r for r in cc if email_mod._recipient_address(r) == "alex.chen@brokerage.com"]
        self.assertTrue(match, "Teammate cc missing from payload")
        self.assertEqual(email_mod._recipient_display_name(match[0]), "Alex Chen",
                         "Display name dropped from preserved teammate cc")

    def test_teammate_with_surrounding_whitespace(self):
        draft = {
            "toRecipients": [graph_recipient("broker@brokerage.com")],
            "ccRecipients": [graph_recipient("  sarah@brokerage.com  ")],
        }
        kept = kept_addresses(run_filter(draft))
        self.assertIn("sarah@brokerage.com", kept)

    def test_teammate_duplicated_across_to_and_cc(self):
        # broker put the teammate in both To and CC — must survive (once), not vanish
        draft = {
            "toRecipients": [
                graph_recipient("broker@brokerage.com"),
                graph_recipient("alex@brokerage.com"),
            ],
            "ccRecipients": [graph_recipient("alex@brokerage.com")],
        }
        kept = kept_addresses(run_filter(draft))
        self.assertIn("alex@brokerage.com", kept,
                      "Teammate present in both To and CC was fully removed by dedup")

    def test_verbose_with_signature_block_multiple_ccs(self):
        # verbose broker email w/ signature; several teammates copied
        draft = {
            "toRecipients": [graph_recipient("longwinded.broker@agency.com")],
            "ccRecipients": [
                graph_recipient("leasing1@agency.com"),
                graph_recipient("leasing2@agency.com"),
                graph_recipient("assistant@agency.com"),
            ],
        }
        kept = kept_addresses(run_filter(draft))
        for addr in ("leasing1@agency.com", "leasing2@agency.com", "assistant@agency.com"):
            self.assertIn(addr, kept, f"Teammate {addr} dropped from reply-all")

    def test_regional_domain_teammate_kept(self):
        draft = {
            "toRecipients": [graph_recipient("broker@agency.co.uk")],
            "ccRecipients": [graph_recipient("colleague@agency.co.uk")],
        }
        kept = kept_addresses(run_filter(draft))
        self.assertIn("colleague@agency.co.uk", kept)

    def test_plus_addressed_teammate_kept(self):
        draft = {
            "toRecipients": [graph_recipient("broker@agency.com")],
            "ccRecipients": [graph_recipient("alex+leasing@agency.com")],
        }
        kept = kept_addresses(run_filter(draft))
        self.assertIn("alex+leasing@agency.com", kept)

    # ------------------------------------------------------------------ #
    # REAL-THREAT GROUP B: opted-out / blocked redirected contact MUST be
    # DROPPED (stopIf #1: don't email a redirected contact w/o approval).
    # ------------------------------------------------------------------ #

    def test_redirected_contact_opted_out_is_dropped(self):
        # broker redirects to someone who previously opted out / is blocked
        draft = {
            "toRecipients": [graph_recipient("broker@brokerage.com")],
            "ccRecipients": [graph_recipient("blocked.alex@brokerage.com")],
        }
        kept = kept_addresses(run_filter(draft, opted_out=["blocked.alex@brokerage.com"]))
        self.assertNotIn("blocked.alex@brokerage.com", kept,
                         "Opted-out / blocked redirected contact was kept — would be emailed w/o approval")

    def test_redirected_optout_case_insensitive_drop(self):
        draft = {
            "toRecipients": [graph_recipient("broker@brokerage.com")],
            "ccRecipients": [graph_recipient("Blocked.Sarah@Brokerage.com")],
        }
        kept = kept_addresses(run_filter(draft, opted_out=["blocked.sarah@brokerage.com"]))
        self.assertNotIn("blocked.sarah@brokerage.com", kept,
                         "Case-variant opted-out redirected contact leaked into reply-all")

    def test_redirected_optout_dropped_but_safe_teammate_kept(self):
        # mixed: one blocked redirect + one safe teammate in same cc line
        draft = {
            "toRecipients": [graph_recipient("broker@brokerage.com")],
            "ccRecipients": [
                graph_recipient("blocked.redirect@brokerage.com"),
                graph_recipient("safe.teammate@brokerage.com"),
            ],
        }
        kept = kept_addresses(run_filter(draft, opted_out=["blocked.redirect@brokerage.com"]))
        self.assertNotIn("blocked.redirect@brokerage.com", kept)
        self.assertIn("safe.teammate@brokerage.com", kept,
                      "Safe teammate dropped alongside the blocked redirect")

    def test_operator_self_address_dropped(self):
        draft = {
            "toRecipients": [graph_recipient("broker@brokerage.com")],
            "ccRecipients": [graph_recipient(OPERATOR)],
        }
        kept = kept_addresses(run_filter(draft))
        self.assertNotIn(OPERATOR.lower(), kept, "Operator's own address left in reply-all audience")

    def test_invalid_redirect_address_dropped(self):
        draft = {
            "toRecipients": [graph_recipient("broker@brokerage.com")],
            "ccRecipients": [graph_recipient("not-an-email")],
        }
        kept = kept_addresses(run_filter(draft))
        self.assertNotIn("not-an-email", kept)

    # ------------------------------------------------------------------ #
    # NEAR-MISS CONTROLS (must NOT mis-behave)
    # ------------------------------------------------------------------ #

    def test_nearmiss_teammate_for_awareness_is_not_dropped(self):
        # NM1: "Broker copies a teammate for awareness without redirecting authority."
        # The teammate is NOT opted out -> MUST be kept. Dropping = false positive.
        draft = {
            "toRecipients": [graph_recipient("broker@brokerage.com", "Pat Broker")],
            "ccRecipients": [graph_recipient("awareness.teammate@brokerage.com", "FYI Teammate")],
        }
        kept = kept_addresses(run_filter(draft, opted_out=[]))
        self.assertIn("awareness.teammate@brokerage.com", kept,
                      "Awareness-only teammate cc was dropped (false positive)")
        self.assertIn("broker@brokerage.com", kept)

    def test_nearmiss_redirected_contact_is_optout_blocked(self):
        # NM2: "Redirected contact is an opt-out or blocked recipient." -> MUST drop.
        draft = {
            "toRecipients": [graph_recipient("broker@brokerage.com")],
            "ccRecipients": [graph_recipient("optout.redirect@brokerage.com")],
        }
        kept = kept_addresses(run_filter(draft, opted_out=["optout.redirect@brokerage.com"]))
        self.assertNotIn("optout.redirect@brokerage.com", kept)

    # ------------------------------------------------------------------ #
    # HARDENING: operator filter must not depend on caller remembering
    # to pass user_email. If user_email is omitted, the operator's own
    # address should still be removed; otherwise reply-all self-emails.
    # This asserts the SAFE behavior and will go RED if it is not honored.
    # ------------------------------------------------------------------ #

    def test_operator_dropped_even_when_user_email_not_passed(self):
        draft = {
            "toRecipients": [graph_recipient("broker@brokerage.com")],
            "ccRecipients": [graph_recipient(OPERATOR)],
        }
        # user_email omitted (None) — mirrors call sites that don't thread it through
        kept = kept_addresses(run_filter(draft, user_email=None))
        self.assertNotIn(
            OPERATOR.lower(), kept,
            "With user_email unset the operator's own address is retained -> reply-all "
            "would email the mailbox itself (self-loop).",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
