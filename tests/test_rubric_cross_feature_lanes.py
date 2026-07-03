import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

import unittest
from unittest.mock import patch

from email_automation.outbound_safety import validate_outbound_body
from email_automation.ai_processing import apply_proposal_to_sheet


# ---------------------------------------------------------------------------
# Sheet datastore fake (mirrors the per-cell rubric fixtures).
# ---------------------------------------------------------------------------
class _FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _FakeValues:
    def __init__(self):
        self.batch_update_calls = []

    def get(self, spreadsheetId=None, range=None, **kwargs):
        if range and range.startswith("AI_META!"):
            return _FakeRequest({"values": [[
                "rowNumber", "columnName", "last_ai_value",
                "last_ai_write_iso", "human_override", "rowAnchor",
            ]]})
        return _FakeRequest({"values": []})

    def batchUpdate(self, spreadsheetId=None, body=None, **kwargs):
        self.batch_update_calls.append(body)
        return _FakeRequest({})


class _FakeSpreadsheets:
    def __init__(self, values):
        self._values = values

    def values(self):
        return self._values

    def get(self, spreadsheetId=None, **kwargs):
        return _FakeRequest({"sheets": [
            {"properties": {"title": "Sheet1", "sheetId": 0}},
            {"properties": {"title": "AI_META", "sheetId": 1}},
        ]})

    def batchUpdate(self, spreadsheetId=None, body=None, **kwargs):
        return _FakeRequest({})


class _FakeSheets:
    def __init__(self):
        self.values_api = _FakeValues()
        self.spreadsheets_api = _FakeSpreadsheets(self.values_api)

    def spreadsheets(self):
        return self.spreadsheets_api


class CrossFeaturePlaceholderThroughSendPathTests(unittest.TestCase):
    """crossFeature: placeholder_name_through_send_path.

    Interaction group: upload_mapping -> name_resolution -> launch_draft ->
    outbox_send -> signature_identity, plus property_extraction/sheet_update on
    the sheet surface. The invariant that ties these features together is that an
    unresolved name placeholder introduced ANYWHERE upstream must be blocked at
    EVERY externally-visible write surface, not just one. This integration test
    drives the two real, independent surfaces with the SAME "[NAME]" token -
    the outbound-email validator and the sheet-write applier - and proves both
    reject it, while a resolved name passes both. If either surface regressed,
    the placeholder would leak on that channel.
    """

    HEADER = ["Property Address", "City", "Broker"]
    ROWNUM = 3
    CURRENT_ROW = ["404 New Way", "Dallas", ""]

    def _apply_to_sheet(self, value):
        proposal = {"updates": [{"column": "Broker", "value": value, "confidence": 0.95, "reason": "x"}]}
        fake = _FakeSheets()
        with patch("email_automation.ai_processing._sheets_client", return_value=fake), \
             patch("email_automation.ai_processing._get_first_tab_title", return_value="Sheet1"), \
             patch("email_automation.sheet_operations._apply_gross_rent_formula_for_row", return_value=False):
            result = apply_proposal_to_sheet(
                "uid", "client", "sheet", self.HEADER, self.ROWNUM, self.CURRENT_ROW, proposal
            )
        return fake, result

    def test_placeholder_name_is_blocked_on_every_send_surface(self):
        # --- MAIN CASE: the same "[NAME]" token on both surfaces.
        # Email body surface (outbound_safety, owner of the send-body guard):
        email = validate_outbound_body("Hi [NAME], following up on 404 New Way.")
        self.assertFalse(email.is_safe)
        self.assertIn("[NAME]", email.placeholders)

        # Sheet-write surface (ai_processing.apply_proposal_to_sheet):
        fake, result = self._apply_to_sheet("[NAME]")
        self.assertEqual([], result["applied"])
        self.assertEqual("placeholder-value", result["skipped"][0]["reason"])
        self.assertEqual([], fake.values_api.batch_update_calls)

        # --- NEGATIVE CONTROL: a resolved name passes BOTH surfaces, proving the
        # block above is the placeholder guard firing, not a dead channel.
        email_ok = validate_outbound_body("Hi Karsen, following up on 404 New Way.")
        self.assertTrue(email_ok.is_safe)

        fake_ok, result_ok = self._apply_to_sheet("Karsen Ellsworth")
        self.assertEqual(1, len(result_ok["applied"]))
        self.assertEqual("Karsen Ellsworth", result_ok["applied"][0]["newValue"])
        self.assertEqual(1, len(fake_ok.values_api.batch_update_calls))


class CrossFeatureManualContinuationCollisionTests(unittest.TestCase):
    """crossFeature: manual_reply_retry_followup_collision.

    Interaction group: outbox_send / inbox_matching / inbox_auto_reply /
    followups / health_recovery / scheduler_scope. When a user manually replies
    in a conversation that also has a queued retry and/or a due follow-up, the
    automation must NOT collide and double-send. The single point that makes this
    safe across all those features is one shared guard,
    sent_mail_guard.find_sent_conversation_continuation_for_retry: the outbox
    retry path (email.py) AND the dead-letter/health-recovery path
    (dead_letter_recovery.py) both consult it. This test proves (a) the guard
    detects a newer human send in the same conversation (positive) and stays
    silent when there is none (negative control), and (b) both consumer modules
    reference the SAME guard object - so the collision protection cannot be
    enforced on one feature but silently missing on another.
    """

    HEADERS = {"Authorization": "Bearer t"}
    CONV_ID = "conv-abc"

    def _run_guard(self, graph_value):
        from email_automation import sent_mail_guard
        from datetime import datetime, timezone

        class _Resp:
            status_code = 200

            def json(self):
                return {"value": graph_value}

        with patch("email_automation.sent_mail_guard.requests.get", return_value=_Resp()), \
             patch("email_automation.sent_mail_guard.exponential_backoff_request", side_effect=lambda fn: fn()):
            return sent_mail_guard.find_sent_conversation_continuation_for_retry(
                self.HEADERS,
                conversation_id=self.CONV_ID,
                sent_after=datetime(2026, 7, 1, tzinfo=timezone.utc),
            )

    def test_manual_continuation_is_the_shared_retry_followup_collision_guard(self):
        # --- POSITIVE: a newer human send exists in the same conversation ->
        # the guard returns its metadata, which is the collision signal both the
        # retry and recovery paths use to stop automated work.
        newer_send = [{
            "id": "m1",
            "internetMessageId": "<newer@contoso>",
            "conversationId": self.CONV_ID,
            "subject": "Re: 404 New Way",
            "toRecipients": [{"emailAddress": {"address": "broker@contoso.com"}}],
            "sentDateTime": "2026-07-02T12:00:00Z",
        }]
        hit = self._run_guard(newer_send)
        self.assertIsNotNone(hit, "A newer human send in-conversation must be detected as a collision.")
        self.assertEqual(1, hit["recipientCount"])

        # --- NEGATIVE CONTROL: no newer send in this conversation -> guard is
        # silent, so automation is free to proceed (proves the positive is the
        # detection firing, not an unconditional block).
        self.assertIsNone(self._run_guard([]))
        # A send in a DIFFERENT conversation must also not count.
        other = [dict(newer_send[0], conversationId="conv-other")]
        self.assertIsNone(self._run_guard(other))

    def test_retry_and_recovery_paths_share_one_collision_guard(self):
        # Cross-feature wiring: the outbox retry path and the health-recovery path
        # must consult the SAME guard object, or collision protection could hold
        # for one feature and silently be absent for the other.
        from email_automation import sent_mail_guard, email, dead_letter_recovery
        self.assertIs(
            email.find_sent_conversation_continuation_for_retry,
            sent_mail_guard.find_sent_conversation_continuation_for_retry,
        )
        self.assertIs(
            dead_letter_recovery.find_sent_conversation_continuation_for_retry,
            sent_mail_guard.find_sent_conversation_continuation_for_retry,
        )


if __name__ == "__main__":
    unittest.main()
