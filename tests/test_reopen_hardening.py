"""Pre-reopen hardening: BUG-A (out-of-office auto-reply suppression), BUG-B
(deterministic OpEx fabrication on bare "$X NNN"), the flag-gated budget guard
wiring in propose_sheet_updates, and the new-vs-frozen campaign boundary.
No live API — the OpenAI client + metering are mocked."""
import json
import os
import unittest
from unittest import mock

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import ai_processing as ai  # noqa: E402
from email_automation.budget_guard import BudgetDeferredError  # noqa: E402
from email_automation.campaign_safety import is_client_automation_paused  # noqa: E402

HEADER = ["Property Address", "City", "Total SF", "Rent/SF /Yr", "Ops Ex /SF", "Notes"]
ROW = ["1 X St", "Yville", "", "", "", ""]


def _fake_client(output_text):
    resp = mock.Mock()
    resp.output_text = output_text
    resp.usage = None
    resp.id = "r"
    fake = mock.Mock()
    fake.responses.create.return_value = resp
    return fake


def _propose(conv, canned, **over):
    kw = dict(uid="u", client_id="c", email="b@x.com", sheet_id="s", header=HEADER,
              rownum=3, rowvals=list(ROW), thread_id="t", conversation=conv,
              contact_name=None, dry_run=True)
    kw.update(over)
    with mock.patch.object(ai, "client", _fake_client(canned)), \
         mock.patch.object(ai, "track_openai_usage_safely"):
        return ai.propose_sheet_updates(**kw)


class BugA_OutOfOffice(unittest.TestCase):
    def test_ooo_suppresses_drafted_reply_and_skips_send(self):
        # The model drafts a reply; an OOO auto-reply inbound must force a hard skip.
        canned = json.dumps({"updates": [], "events": [],
                             "response_email": "Hi, I'll follow up after you're back on July 20.",
                             "notes": ""})
        conv = [{"direction": "outbound", "content": "following up on 1 X St"},
                {"direction": "inbound", "from": "b@x.com",
                 "content": "Automatic reply: I am out of office until July 20 with limited email. "
                            "For urgent leasing matters contact Dana Reed at dana@x.com."}]
        p = _propose(conv, canned)
        self.assertTrue(p.get("skip_response"), "OOO must set skip_response")
        self.assertIsNone(p.get("response_email"), "OOO must null the drafted reply")

    def test_normal_reply_not_suppressed(self):
        canned = json.dumps({"updates": [], "events": [],
                             "response_email": "Thanks, noted.", "notes": ""})
        conv = [{"direction": "inbound", "from": "b@x.com",
                 "content": "It's 20,000 SF, available now. Happy to help."}]
        p = _propose(conv, canned)
        self.assertFalse(p.get("skip_response"))
        self.assertEqual(p.get("response_email"), "Thanks, noted.")


class BugB_OpexNNN(unittest.TestCase):
    def test_bare_nnn_is_rent_not_opex(self):
        self.assertIsNone(ai._extract_ops_ex_sf_from_text("It's 22,000 SF, $9.25 NNN, 28' clear, 3 docks."))

    def test_nnn_rent_does_not_clobber_real_opex(self):
        # was returning 8.50 (the rent); must return the real 3.50 opex
        self.assertEqual(ai._extract_ops_ex_sf_from_text("$8.50/SF NNN with $3.50 opex"), "3.50")

    def test_keyword_first_opex_still_extracted(self):
        self.assertEqual(ai._extract_ops_ex_sf_from_text("OpEx is $16/SF"), "16.00")

    def test_directly_qualified_nnn_estimate_kept_as_opex(self):
        self.assertEqual(ai._extract_ops_ex_sf_from_text("$3.50 NNN est"), "3.50")


class BudgetGuardWiring(unittest.TestCase):
    def test_over_budget_defers_with_visible_retryable_error_without_model_call(self):
        fake = _fake_client(json.dumps({"updates": [], "events": [], "response_email": None}))
        with mock.patch.object(ai, "client", fake), \
             mock.patch.object(ai, "should_block_openai_call", return_value=True):
            with self.assertRaises(BudgetDeferredError):
                ai.propose_sheet_updates(uid="u", client_id="c", email="b@x.com", sheet_id="s",
                                         header=HEADER, rownum=3, rowvals=list(ROW), thread_id="t",
                                         conversation=[{"direction": "inbound", "content": "hi"}], dry_run=True)
        fake.responses.create.assert_not_called()

    def test_under_budget_proceeds_and_calls_model(self):
        fake = _fake_client(json.dumps({"updates": [], "events": [], "response_email": None, "notes": ""}))
        with mock.patch.object(ai, "client", fake), \
             mock.patch.object(ai, "track_openai_usage_safely"), \
             mock.patch.object(ai, "should_block_openai_call", return_value=False):
            p = ai.propose_sheet_updates(uid="u", client_id="c", email="b@x.com", sheet_id="s",
                                         header=HEADER, rownum=3, rowvals=list(ROW), thread_id="t",
                                         conversation=[{"direction": "inbound", "content": "hi"}], dry_run=True)
        self.assertIsInstance(p, dict)
        fake.responses.create.assert_called_once()


class NewVsFrozenBoundary(unittest.TestCase):
    """A NEW campaign (no automationPaused) is monitored; a FROZEN one is skipped —
    the exact guarantee behind the pre-reopen freeze."""
    def test_new_campaign_is_not_paused(self):
        self.assertFalse(is_client_automation_paused({}))
        self.assertFalse(is_client_automation_paused({"status": "live"}))

    def test_frozen_campaign_is_paused(self):
        self.assertTrue(is_client_automation_paused({"automationPaused": True}))
        self.assertTrue(is_client_automation_paused({"status": "live", "automationPaused": True}))


if __name__ == "__main__":
    unittest.main()
