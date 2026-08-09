import unittest
from contextlib import ExitStack
from unittest import mock

from email_automation import ai_processing as ai
from email_automation import processing as proc


class FlyerLinkApplyTests(unittest.TestCase):
    def _apply(
        self,
        *,
        proposed_url,
        evidence_urls,
        existing_value="",
        confidence=0.99,
        fresh_value=None,
    ):
        sheets = mock.MagicMock()
        append_meta = mock.MagicMock()
        self.retry_operations = []

        def execute_with_retry(_request, operation_name):
            self.retry_operations.append(operation_name)
            if operation_name == "read_flyer_link_before_fallback":
                return {"values": [[fresh_value]]} if fresh_value is not None else {}
            return {}

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(ai, "_sheets_client", return_value=sheets))
            stack.enter_context(mock.patch.object(ai, "_get_first_tab_title", return_value="Sheet1"))
            stack.enter_context(mock.patch.object(ai, "_ensure_ai_meta_tab"))
            stack.enter_context(mock.patch.object(ai, "_load_ai_meta_rows", return_value=[]))
            stack.enter_context(mock.patch.object(ai, "_append_ai_meta", new=append_meta))
            stack.enter_context(mock.patch.object(ai, "_append_notes_to_comments"))
            stack.enter_context(
                mock.patch(
                    "email_automation.sheet_operations._apply_gross_rent_formula_for_row",
                    return_value=False,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    ai,
                    "_execute_with_retry",
                    side_effect=execute_with_retry,
                )
            )
            result = ai.apply_proposal_to_sheet(
                uid="user-1",
                client_id="client-1",
                sheet_id="sheet-1",
                header=["Property Address", "Flyer / Link"],
                rownum=3,
                current_rowvals=["9250 W Thunderbird Rd", existing_value],
                proposal={
                    "updates": [
                        {
                            "column": "Flyer / Link",
                            "value": proposed_url,
                            "confidence": confidence,
                        }
                    ]
                },
                broker_flyer_url_evidence=evidence_urls,
            )
        return result, sheets, append_meta

    def test_safe_current_message_url_writes_empty_flyer_cell(self):
        result, _, append_meta = self._apply(
            proposed_url="HTTPS://Example.COM/flyer.pdf",
            evidence_urls=["https://example.com/flyer.pdf"],
        )

        self.assertEqual(
            [("Flyer / Link", "https://example.com/flyer.pdf")],
            [
                (item.get("column"), item.get("newValue"))
                for item in result["applied"]
            ],
        )
        append_meta.assert_called_once()
        self.assertEqual(
            ["read_flyer_link_before_fallback", "apply_proposal_batch_update"],
            self.retry_operations,
        )

    def test_different_safe_url_is_rejected_when_not_in_current_message(self):
        result, sheets, _ = self._apply(
            proposed_url="https://unrelated.example.com/other.pdf",
            evidence_urls=["https://broker.example.com/current.pdf"],
        )

        self.assertEqual([], result["applied"])
        self.assertIn(
            ("Flyer / Link", "unverified-current-message-url"),
            {(item.get("column"), item.get("reason")) for item in result["skipped"]},
        )
        sheets.spreadsheets.return_value.values.return_value.batchUpdate.assert_not_called()

    def test_high_confidence_never_overwrites_existing_flyer_value(self):
        result, sheets, _ = self._apply(
            proposed_url="https://broker.example.com/new.pdf",
            evidence_urls=["https://broker.example.com/new.pdf"],
            existing_value="https://human.example.com/curated.pdf",
            confidence=1.0,
        )

        self.assertEqual([], result["applied"])
        self.assertIn(
            ("Flyer / Link", "existing-human-value"),
            {(item.get("column"), item.get("reason")) for item in result["skipped"]},
        )
        sheets.spreadsheets.return_value.values.return_value.batchUpdate.assert_not_called()
        self.assertEqual([], self.retry_operations)

    def test_unsafe_url_is_rejected_even_when_present_in_evidence(self):
        for unsafe_url in (
            "file:///tmp/flyer.pdf",
            "javascript:alert(1)",
            "http://localhost/flyer.pdf",
            "http://127.0.0.1/flyer.pdf",
            "http://127.1/flyer.pdf",
            "http://2130706433/flyer.pdf",
            "http://0x7f000001/flyer.pdf",
            "http://10.0.0.1/flyer.pdf",
            "http://169.254.169.254/flyer.pdf",
            "http://[::1]/flyer.pdf",
            "http://[fe80::1]/flyer.pdf",
            "https://user:password@example.com/flyer.pdf",
            "https://example.com/%ZZ",
            "https://example.com/\x7f",
            "https://example.com/\x80",
            "https://[2606:4700:4700::1111%25eth0]/flyer.pdf",
            "https://example..com/flyer.pdf",
            "https://-bad.example.com/flyer.pdf",
            "https://example.com\\@evil.test/flyer.pdf",
            "https://example.com:not-a-port/flyer.pdf",
        ):
            with self.subTest(url=unsafe_url):
                result, sheets, _ = self._apply(
                    proposed_url=unsafe_url,
                    evidence_urls=[unsafe_url],
                )
                self.assertEqual([], result["applied"])
                self.assertIn(
                    ("Flyer / Link", "invalid-asset-url"),
                    {
                        (item.get("column"), item.get("reason"))
                        for item in result["skipped"]
                    },
                )
                sheets.spreadsheets.return_value.values.return_value.batchUpdate.assert_not_called()

    def test_fresh_sheet_value_blocks_stale_empty_snapshot_overwrite(self):
        result, sheets, _ = self._apply(
            proposed_url="https://broker.example.com/new.pdf",
            evidence_urls=["https://broker.example.com/new.pdf"],
            existing_value="",
            fresh_value="https://human.example.com/added-during-processing.pdf",
            confidence=1.0,
        )

        self.assertEqual([], result["applied"])
        self.assertIn(
            ("Flyer / Link", "existing-human-value"),
            {(item.get("column"), item.get("reason")) for item in result["skipped"]},
        )
        sheets.spreadsheets.return_value.values.return_value.batchUpdate.assert_not_called()
        self.assertEqual(["read_flyer_link_before_fallback"], self.retry_operations)


class CurrentMessageFlyerEvidenceTests(unittest.TestCase):
    TARGET = "9250 W Thunderbird Rd, Peoria"

    def test_quoted_history_url_is_not_current_message_evidence(self):
        body = (
            "Thanks, I will confirm the remaining details.\n\n"
            "On Fri, Aug 7, 2026 at 1:00 PM Avery wrote:\n"
            "> Old flyer: https://history.example.com/old.pdf"
        )

        self.assertEqual([], proc._current_target_flyer_url_evidence(body, self.TARGET, []))

    def test_other_property_url_is_not_current_target_evidence(self):
        body = (
            "The current property remains available. "
            "For 500 W Cactus Rd, use https://other.example.com/flyer.pdf"
        )
        events = [{"type": "new_property", "address": "500 W Cactus Rd"}]

        self.assertEqual([], proc._current_target_flyer_url_evidence(body, self.TARGET, events))

    def test_prior_target_address_does_not_carry_into_generic_alternate_clause(self):
        body = (
            "For 9250 W Thunderbird Rd, no flyer. "
            "For the alternate property, use https://other.example.com/flyer.pdf"
        )
        events = [{"type": "new_property", "address": "500 W Cactus Rd"}]

        self.assertEqual([], proc._current_target_flyer_url_evidence(body, self.TARGET, events))

    def test_alternate_cue_rejects_url_even_when_model_misses_event(self):
        body = (
            "For 9250 W Thunderbird Rd, no flyer. "
            "For the alternate property, use https://other.example.com/flyer.pdf"
        )

        self.assertEqual([], proc._current_target_flyer_url_evidence(body, self.TARGET, []))

    def test_url_before_other_address_is_not_current_target_evidence(self):
        body = (
            "For 9250 W Thunderbird Rd, no flyer. "
            "See https://other.example.com/flyer.pdf for 500 W Cactus Rd"
        )
        events = [{"type": "new_property", "address": "500 W Cactus Rd"}]

        self.assertEqual([], proc._current_target_flyer_url_evidence(body, self.TARGET, events))

    def test_url_before_target_address_is_valid_with_competing_property(self):
        body = (
            "See https://target.example.com/flyer.pdf for 9250 W Thunderbird Rd. "
            "For 500 W Cactus Rd, no flyer is available."
        )
        events = [{"type": "new_property", "address": "500 W Cactus Rd"}]

        self.assertEqual(
            ["https://target.example.com/flyer.pdf"],
            proc._current_target_flyer_url_evidence(body, self.TARGET, events),
        )

    def test_same_street_alternate_suite_url_is_not_current_suite_evidence(self):
        body = (
            "Suite 100 at 9250 W Thunderbird Rd has no flyer. "
            "Suite 200 brochure: https://other.example.com/suite-200.pdf"
        )
        events = [
            {
                "type": "new_property",
                "address": "Suite 200, 9250 W Thunderbird Rd",
            }
        ]

        self.assertEqual(
            [],
            proc._current_target_flyer_url_evidence(
                body,
                "9250 W Thunderbird Rd, Suite 100, Peoria",
                events,
            ),
        )

    def test_competing_suite_rejects_url_even_when_model_misses_event(self):
        body = (
            "Suite 100 at 9250 W Thunderbird Rd has no flyer. "
            "Suite 200 brochure: https://other.example.com/suite-200.pdf"
        )

        self.assertEqual(
            [],
            proc._current_target_flyer_url_evidence(
                body,
                "9250 W Thunderbird Rd, Suite 100, Peoria",
                [],
            ),
        )

    def test_explicit_target_suite_link_survives_competing_suite(self):
        body = (
            "For Suite 100 at 9250 W Thunderbird Rd, use "
            "https://target.example.com/suite-100.pdf. "
            "For Suite 200 at 9250 W Thunderbird Rd, use "
            "https://other.example.com/suite-200.pdf"
        )
        events = [
            {
                "type": "new_property",
                "address": "Suite 200, 9250 W Thunderbird Rd",
            }
        ]

        self.assertEqual(
            ["https://target.example.com/suite-100.pdf"],
            proc._current_target_flyer_url_evidence(
                body,
                "9250 W Thunderbird Rd, Suite 100, Peoria",
                events,
            ),
        )

    def test_target_link_before_alternate_is_the_only_allowed_evidence(self):
        body = (
            "For 9250 W Thunderbird Rd, the flyer is "
            "https://target.example.com/flyer.pdf. "
            "For 500 W Cactus Rd, the brochure is "
            "https://other.example.com/flyer.pdf"
        )
        events = [{"type": "new_property", "address": "500 W Cactus Rd"}]

        self.assertEqual(
            ["https://target.example.com/flyer.pdf"],
            proc._current_target_flyer_url_evidence(body, self.TARGET, events),
        )


if __name__ == "__main__":
    unittest.main()
