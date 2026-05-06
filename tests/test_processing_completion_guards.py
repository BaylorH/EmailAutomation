import unittest

from email_automation import ai_processing, processing


class ProcessingCompletionGuardTests(unittest.TestCase):
    def test_closing_copy_does_not_satisfy_missing_field_response(self):
        body = "Thanks for sending this over. This covers everything I needed."

        self.assertFalse(processing._response_mentions_missing_fields(body, ["Rail Access"]))

    def test_missing_field_response_must_reference_requested_detail(self):
        body = "Thanks for the info. Could you also confirm whether the building has rail access?"

        self.assertTrue(processing._response_mentions_missing_fields(body, ["Rail Access"]))

    def test_all_info_close_event_requires_complete_required_fields(self):
        event = {"type": "close_conversation", "notes": "all_info_gathered"}

        self.assertFalse(processing._close_event_can_bypass_missing_fields(event))

    def test_terminal_non_info_close_reason_can_bypass_missing_fields(self):
        event = {"type": "close_conversation", "notes": "deal_pending"}

        self.assertTrue(processing._close_event_can_bypass_missing_fields(event))

    def test_deterministic_rent_fallback_extracts_asking_rent_not_nnn(self):
        value = ai_processing._extract_rent_sf_yr_from_text(
            "Asking $9.00/SF/year, NNN $0.39/SF, power is 200 amps."
        )

        self.assertEqual(value, "9.00")

    def test_deterministic_rent_fallback_augments_blank_rent_cell(self):
        header = ["Property Address", "Rent/SF /Yr", "Ops Ex /SF"]
        proposal = {"updates": [{"column": "Ops Ex /SF", "value": "0.39"}]}
        rowvals = ["3100 Sirius Ave", "", ""]
        config = {"mappings": {"rent_sf_yr": "Rent/SF /Yr"}}
        conversation = [{
            "direction": "inbound",
            "content": "Asking $9.00/SF/year, NNN $0.39/SF.",
        }]

        augmented = ai_processing._augment_proposal_with_deterministic_extractions(
            proposal, rowvals, header, config, conversation
        )

        self.assertIn(
            {"column": "Rent/SF /Yr", "value": "9.00", "confidence": 0.92,
             "reason": "Deterministic fallback parsed asking rent per SF per year from the latest broker message."},
            augmented["updates"],
        )


if __name__ == "__main__":
    unittest.main()
