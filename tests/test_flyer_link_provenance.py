import unittest
from contextlib import ExitStack
from unittest import mock

from email_automation import ai_processing as ai
from email_automation import processing as proc


PRODUCTION_CANARY_BODY = (
    "Hi John, the suite is available. It contains 22,400 square feet, the asking "
    "rent is $12.95 per square foot per year NNN, and estimated operating expenses "
    "are $2.85 per square foot. The property flyer and listing details are here: "
    "https://sitesiftai.com/help. Please let me know if you need a tour. Best, Jordan"
)


class FlyerLinkApplyTests(unittest.TestCase):
    def _apply(
        self,
        *,
        proposed_url,
        evidence_urls,
        existing_value="",
        confidence=0.99,
        fresh_value=None,
        flyer_column="Flyer / Link",
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
                header=["Property Address", flyer_column],
                rownum=3,
                current_rowvals=["9250 W Thunderbird Rd", existing_value],
                proposal={
                    "updates": [
                        {
                            "column": flyer_column,
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

    def test_exact_production_message_url_reaches_sheet_gate(self):
        events = [{"type": "tour_requested", "question": "Need a tour?"}]
        evidence = proc._current_target_flyer_url_evidence(
            PRODUCTION_CANARY_BODY,
            "4402 Rex Rd, Houston",
            events,
        )

        result, _, _ = self._apply(
            proposed_url="https://sitesiftai.com/help",
            evidence_urls=evidence,
            flyer_column="Flyer/Link",
        )

        self.assertEqual(
            [("Flyer/Link", "https://sitesiftai.com/help")],
            [
                (item.get("column"), item.get("newValue"))
                for item in result["applied"]
            ],
        )
        self.assertNotIn(
            "unverified-current-message-url",
            {item.get("reason") for item in result["skipped"]},
        )


class CurrentMessageFlyerEvidenceTests(unittest.TestCase):
    TARGET = "9250 W Thunderbird Rd, Peoria"

    def test_exact_production_rate_language_does_not_create_property_claims(self):
        events = [{"type": "tour_requested", "question": "Need a tour?"}]

        self.assertEqual(
            ["https://sitesiftai.com/help"],
            proc._current_target_flyer_url_evidence(
                PRODUCTION_CANARY_BODY,
                "4402 Rex Rd, Houston",
                events,
            ),
        )

    def test_integer_per_square_rate_does_not_create_property_claim(self):
        body = (
            "The asking rent is 12 per square foot NNN. "
            "Flyer: https://target.example.com/flyer.pdf"
        )

        self.assertEqual(
            ["https://target.example.com/flyer.pdf"],
            proc._current_target_flyer_url_evidence(body, self.TARGET, []),
        )

    def test_common_square_foot_rate_variants_do_not_create_property_claims(self):
        for rate_phrase in (
            "$12.95 per square ft",
            "$12.95 per sq foot",
            "$12.95 per sq. foot",
            "$12.95 per sq feet",
            "$12.95 per square/foot",
            "Asking rate: 12.95 per square foot",
            "$12.95 per rentable square foot",
            "$12.95 per usable square foot",
            "$12.95 NNN per square foot",
            "$12.95 gross per square foot",
            "$12.95 net per square foot",
            "Rent: USD 12.95 per square foot",
        ):
            with self.subTest(rate_phrase=rate_phrase):
                body = (
                    f"{rate_phrase}. "
                    "Flyer: https://target.example.com/flyer.pdf"
                )
                self.assertEqual(
                    ["https://target.example.com/flyer.pdf"],
                    proc._current_target_flyer_url_evidence(body, self.TARGET, []),
                )

    def test_rate_language_does_not_hide_real_other_property_claim(self):
        body = (
            "The asking rent is $12.95 per square foot. "
            "For 500 W Cactus Rd, use https://other.example.com/flyer.pdf"
        )
        events = [{"type": "new_property", "address": "500 W Cactus Rd"}]

        self.assertEqual(
            [],
            proc._current_target_flyer_url_evidence(body, self.TARGET, events),
        )

    def test_real_square_street_suffix_remains_property_identity(self):
        body = (
            "For 95 Market Square, use https://target.example.com/flyer.pdf. "
            "For 500 W Cactus Rd, no flyer is available."
        )
        events = [{"type": "new_property", "address": "500 W Cactus Rd"}]

        self.assertEqual(
            ["https://target.example.com/flyer.pdf"],
            proc._current_target_flyer_url_evidence(
                body,
                "95 Market Square, Houston",
                events,
            ),
        )

    def test_square_street_is_not_erased_by_following_foot_language(self):
        for body in (
            "For 500 Market Square. Foot traffic is excellent. "
            "Flyer: https://listing.example.com/flyer.pdf",
            "For 500 Market Sq. Foot traffic is excellent. "
            "Flyer: https://listing.example.com/flyer.pdf",
            "For 500 Market Square-Foot traffic is excellent. "
            "Flyer: https://listing.example.com/flyer.pdf",
            "For 500 Market Square/Foot traffic is excellent. "
            "Flyer: https://listing.example.com/flyer.pdf",
            "For 500 Market Sq. Ft Worth brochure: "
            "https://listing.example.com/flyer.pdf",
            "For 500 Per Square. Foot traffic is excellent. "
            "Flyer: https://listing.example.com/flyer.pdf",
            "For 500 NNN Per Square. Foot traffic is excellent. "
            "Flyer: https://listing.example.com/flyer.pdf",
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    [],
                    proc._current_target_flyer_url_evidence(body, self.TARGET, []),
                )

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
