import inspect
import unittest
from unittest.mock import Mock, patch

from email_automation import ai_processing, processing
from email_automation.ai_processing import build_response_email_rules
from email_automation.column_config import get_default_column_config


class BrokerVoicePolicyTests(unittest.TestCase):
    def setUp(self):
        self.rules = build_response_email_rules()
        self.rules_lower = self.rules.lower()

    def test_policy_preserves_missing_field_and_footer_safety(self):
        self.assertIn("only request fields", self.rules_lower)
        self.assertIn("missing required fields", self.rules_lower)
        self.assertIn("never request \"gross rent\"", self.rules_lower)
        self.assertIn("do not include any signature", self.rules_lower)
        self.assertIn("do not include \"best,\"", self.rules_lower)

    def test_policy_preserves_sensitive_event_null_responses(self):
        for event_type in (
            "call_requested",
            "needs_user_input",
            "contact_optout",
            "wrong_contact",
            "tour_requested",
        ):
            with self.subTest(event_type=event_type):
                self.assertIn(event_type, self.rules)
        self.assertIn("response_email to null", self.rules)

    def test_policy_requires_attentive_concise_natural_copy(self):
        self.assertIn("specific details", self.rules_lower)
        self.assertIn("attachment", self.rules_lower)
        self.assertIn("concise", self.rules_lower)
        self.assertIn("natural", self.rules_lower)

    def test_policy_removes_phrase_rotation_menu_and_canned_transitions(self):
        forbidden = (
            "PHRASE VARIATION RULES",
            "rotate through these options",
            "This is great, thanks",
            "Perfect, thank you",
            "Got it - thanks for the breakdown",
            "One more thing - do you have",
        )

        for phrase in forbidden:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, self.rules)

    def test_policy_uses_authoritative_evidence_without_reasking(self):
        self.assertIn(
            "acknowledge one concrete detail from the newest message when useful",
            self.rules_lower,
        )
        self.assertIn("authoritative missing required fields", self.rules_lower)
        self.assertIn(
            "never re-ask facts already present in the current row, newest message, "
            "or attachment evidence",
            self.rules_lower,
        )

    def test_policy_formats_missing_fields_by_count(self):
        self.assertIn("one or two missing fields", self.rules_lower)
        self.assertIn("one natural sentence", self.rules_lower)
        self.assertIn("three or more missing fields", self.rules_lower)
        self.assertIn("bulleted list", self.rules_lower)

    def test_policy_rejects_robotic_tone_without_becoming_curt(self):
        self.assertIn("concise but not curt", self.rules_lower)
        self.assertIn("no fake enthusiasm", self.rules_lower)
        self.assertIn("no canned filler", self.rules_lower)
        self.assertIn("no mandatory phrase rotation", self.rules_lower)

    def test_policy_sets_completed_reply_standard(self):
        self.assertIn("reviewed with the client", self.rules_lower)
        self.assertIn("relevant alternatives", self.rules_lower)
        self.assertIn("questions", self.rules_lower)

    def test_model_and_single_structured_call_contract_are_unchanged(self):
        source = inspect.getsource(ai_processing.propose_sheet_updates)

        self.assertEqual(1, source.count("client.responses.create("))
        self.assertIn('model="gpt-5.2"', source)
        self.assertIn("temperature=0.1", source)
        self.assertIn("OUTPUT ONLY valid JSON in this exact format", source)
        self.assertIn("build_response_email_rules()", source)

    def test_assembled_output_contract_nulls_every_sensitive_event(self):
        fake_response = Mock(
            output_text=(
                '{"updates": [], "events": [], "response_email": null, "notes": ""}'
            )
        )
        with patch.object(
            ai_processing.client.responses,
            "create",
            return_value=fake_response,
        ) as create:
            ai_processing.propose_sheet_updates(
                "uid",
                "client",
                "broker@example.com",
                "sheet",
                ["Property Address", "Rent/SF /Yr", "Flyer / Link"],
                3,
                ["123 Main St", "", ""],
                "thread",
                conversation=[
                    {
                        "direction": "inbound",
                        "from": "broker@example.com",
                        "content": "The space is available.",
                    }
                ],
                column_config=get_default_column_config(),
                dry_run=True,
            )

        content = create.call_args.kwargs["input"][0]["content"]
        prompt_text = next(
            item["text"] for item in content if item.get("type") == "input_text"
        )
        contract_start = prompt_text.rindex('"response_email":')
        contract_end = prompt_text.index('"notes":', contract_start)
        response_contract = prompt_text[contract_start:contract_end]

        for event_type in (
            "call_requested",
            "needs_user_input",
            "contact_optout",
            "wrong_contact",
            "tour_requested",
        ):
            with self.subTest(event_type=event_type):
                self.assertIn(event_type, response_contract)
        self.assertNotIn("phone number provided", response_contract)


class BrokerVoiceFallbackTests(unittest.TestCase):
    def test_looking_forward_cleanup_does_not_append_a_closing(self):
        body = processing._sanitize_missing_fields_response_body(
            "Hi Alex,\n\n"
            "Could you confirm the docks and power?\n\n"
            "Looking forward to your response"
        )

        self.assertNotIn("Looking forward", body)
        self.assertIn("docks", body.lower())
        self.assertIn("power", body.lower())
        self.assertNotRegex(
            body,
            r"(?im)^\s*(best|best regards|regards|thanks)[,!]?\s*$",
        )
        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Docks", "Power"],
                get_default_column_config(),
            )
        )

    def test_two_missing_fields_use_a_natural_sentence(self):
        body = processing._build_missing_fields_response(
            "Alex Morgan",
            ["Docks", "Power"],
        )

        self.assertIn("Hi Alex,", body)
        self.assertIn("docks", body.lower())
        self.assertIn("power", body.lower())
        self.assertNotRegex(body, r"(?m)^- ")
        self.assertNotIn("Thank you for the information!", body)
        self.assertNotIn("To complete the property details", body)
        self.assertNotIn("Best,", body)
        self.assertNotIn("!", body)

    def test_one_missing_field_uses_a_natural_sentence(self):
        body = processing._build_missing_fields_response("Alex Morgan", ["Docks"])

        self.assertIn("docks", body.lower())
        self.assertNotRegex(body, r"(?m)^- ")
        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Docks"],
                get_default_column_config(),
            )
        )

    def test_three_missing_fields_use_bullets_and_pass_the_functional_gate(self):
        missing_fields = ["Docks", "Power", "Ceiling Ht"]
        body = processing._build_missing_fields_response("Alex Morgan", missing_fields)

        for field in missing_fields:
            with self.subTest(field=field):
                self.assertIn(f"- {field}", body)
        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                missing_fields,
                get_default_column_config(),
            )
        )
        self.assertNotIn("Best,", body)
        self.assertNotIn("!", body)

    def test_missing_field_call_site_keeps_the_functional_gate(self):
        source = inspect.getsource(processing.process_inbox_message)

        self.assertIn("_response_mentions_missing_fields(", source)
        self.assertIn("_build_missing_fields_response(", source)

    def test_complete_fallback_reviews_with_client_and_welcomes_options(self):
        body = processing._select_automatic_response_body(
            "complete",
            None,
            None,
            "Alex Morgan",
        )

        self.assertIn("review", body.lower())
        self.assertIn("with the client", body.lower())
        self.assertIn("questions", body.lower())
        self.assertIn("relevant", body.lower())
        self.assertNotIn("Best,", body)
        self.assertNotIn("!", body)

    def test_unavailable_fallbacks_are_warm_without_fake_enthusiasm(self):
        for scenario in ("nonviable", "nonviable_with_alternative"):
            with self.subTest(scenario=scenario):
                body = processing._select_automatic_response_body(
                    scenario,
                    None,
                    None,
                    "Alex Morgan",
                )

                self.assertIn("Hi Alex,", body)
                self.assertIn("update", body.lower())
                self.assertNotIn("Best,", body)
                self.assertNotIn("!", body)


if __name__ == "__main__":
    unittest.main()
