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

    def test_default_tour_suggested_email_uses_offered_times_without_placeholders(self):
        body = processing._build_default_tour_suggested_email(
            "Devin",
            "Tour availability offered: Monday at 2:00 PM or Wednesday at 10:00 AM.",
        )

        self.assertIn("Monday at 2:00 PM", body)
        self.assertIn("Wednesday at 10:00 AM", body)
        self.assertNotIn("[Day/Time option", body)

    def test_default_tour_suggested_email_without_times_asks_for_windows(self):
        body = processing._build_default_tour_suggested_email("Devin", "Tour requested")

        self.assertIn("what tour windows are available", body)
        self.assertNotIn("[Day/Time option", body)

    def test_confirmed_tour_without_suggested_email_is_not_actionable(self):
        event = {
            "type": "tour_requested",
            "question": (
                "Monday at 2:00 PM is confirmed. Park at the main office entrance; "
                "I will meet you in the lobby. No additional access instructions."
            ),
            "suggestedEmail": "",
        }

        self.assertFalse(processing._tour_event_needs_operator_action(event))

    def test_follow_up_tour_choice_still_needs_operator_action(self):
        event = {
            "type": "tour_requested",
            "question": "Jordan offered tour times: Tuesday at 11:00 AM or Wednesday at 1:30 PM for a follow-up tour.",
            "suggestedEmail": {
                "body": "Can you pencil us in for Tuesday at 11:00 AM?",
            },
        }

        self.assertTrue(processing._tour_event_needs_operator_action(event))

    def test_deterministic_rent_fallback_extracts_asking_rent_not_nnn(self):
        value = ai_processing._extract_rent_sf_yr_from_text(
            "Asking $9.00/SF/year, NNN $0.39/SF, power is 200 amps."
        )

        self.assertEqual(value, "9.00")

    def test_deterministic_rent_fallback_annualizes_monthly_asking_rent(self):
        value = ai_processing._extract_rent_sf_yr_from_text(
            "Asking rate: $1.25/SF/month NNN."
        )

        self.assertEqual(value, "15.00")

    def test_deterministic_rent_fallback_annualizes_per_square_foot_per_month(self):
        value = ai_processing._extract_rent_sf_yr_from_text(
            "Base rent is $0.95 per square foot per month plus operating expenses."
        )

        self.assertEqual(value, "11.40")

    def test_deterministic_rent_fallback_annualizes_nnn_monthly_suffix(self):
        value = ai_processing._extract_rent_sf_yr_from_text(
            "Asking rent: $1.12/SF NNN monthly."
        )

        self.assertEqual(value, "13.44")

    def test_deterministic_rent_fallback_does_not_treat_next_month_as_monthly_rent(self):
        value = ai_processing._extract_rent_sf_yr_from_text(
            "Asking rent: $9.00/SF NNN, available next month."
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

    def test_deterministic_rent_fallback_corrects_existing_monthly_llm_update(self):
        header = ["Property Address", "Rent/SF /Yr", "Ops Ex /SF"]
        proposal = {
            "updates": [
                {"column": "Rent/SF /Yr", "value": "1.12", "confidence": 0.92, "reason": "LLM copied monthly rent"},
                {"column": "Ops Ex /SF", "value": "3.24"},
            ]
        }
        rowvals = ["414 Alternate Signal Pkwy", "", ""]
        config = {"mappings": {"rent_sf_yr": "Rent/SF /Yr"}}
        conversation = [{
            "direction": "inbound",
            "content": "Asking rent: $1.12/SF NNN monthly. Ops Ex / NNN: $0.27/SF monthly.",
        }]

        augmented = ai_processing._augment_proposal_with_deterministic_extractions(
            proposal, rowvals, header, config, conversation
        )

        self.assertIn(
            {"column": "Rent/SF /Yr", "value": "13.44", "confidence": 0.92,
             "reason": "Deterministic fallback parsed asking rent per SF per year from the latest broker message."},
            augmented["updates"],
        )


if __name__ == "__main__":
    unittest.main()
