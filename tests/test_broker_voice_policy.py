import json
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

        create.assert_called_once()
        self.assertEqual("gpt-5.2", create.call_args.kwargs["model"])
        self.assertEqual(0.1, create.call_args.kwargs["temperature"])
        prompt_text = next(
            item["text"]
            for item in create.call_args.kwargs["input"][0]["content"]
            if item.get("type") == "input_text"
        )
        self.assertIn("OUTPUT ONLY valid JSON in this exact format", prompt_text)
        self.assertIn(build_response_email_rules().strip(), prompt_text)

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

    def test_proposal_suppresses_model_reply_for_every_sensitive_event(self):
        scenarios = {
            "needs_user_input": (
                "Who is your client?",
                {"type": "needs_user_input", "reason": "confidential"},
            ),
            "tour_requested": (
                "Would you like to tour the space Tuesday?",
                {"type": "tour_requested", "question": "Tour Tuesday?"},
            ),
            "wrong_contact": (
                "Wrong person. Contact Dana at dana@example.com.",
                {
                    "type": "wrong_contact",
                    "reason": "wrong_person",
                    "suggestedContact": "Dana",
                    "suggestedEmail": "dana@example.com",
                },
            ),
            "contact_optout": (
                "Please remove me from your mailing list.",
                {"type": "contact_optout", "reason": "unsubscribe"},
            ),
            "call_requested": (
                "Let's hop on a call tomorrow.",
                {"type": "call_requested", "reason": "call_request"},
            ),
        }

        for event_type, (message, event) in scenarios.items():
            with self.subTest(event_type=event_type):
                fake_response = Mock(
                    output_text=json.dumps(
                        {
                            "updates": [],
                            "events": [event],
                            "response_email": "Hi Alex, I can handle that.",
                            "notes": "",
                        }
                    )
                )
                with patch.object(
                    ai_processing.client.responses,
                    "create",
                    return_value=fake_response,
                ):
                    proposal = ai_processing.propose_sheet_updates(
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
                                "content": message,
                            }
                        ],
                        column_config=get_default_column_config(),
                        dry_run=True,
                    )

                self.assertIn(event_type, [item["type"] for item in proposal["events"]])
                self.assertIsNone(proposal["response_email"])

    def test_sensitive_event_suppression_normalizes_event_type_text(self):
        variants = {
            "needs_user_input": "  NEEDS_USER_INPUT  ",
            "tour_requested": "\tTour_Requested\n",
            "wrong_contact": " Wrong_Contact ",
            "contact_optout": "CONTACT_OPTOUT ",
            "call_requested": " Call_Requested",
        }

        for event_type, raw_event_type in variants.items():
            with self.subTest(event_type=event_type):
                proposal = {
                    "events": [{"type": raw_event_type}],
                    "response_email": "Hi Alex, I can handle that.",
                }

                result = ai_processing._suppress_response_for_sensitive_events(
                    proposal
                )

                self.assertIsNone(result["response_email"])


class BrokerVoiceFallbackTests(unittest.TestCase):
    def test_missing_field_mention_without_request_intent_is_rejected(self):
        self.assertFalse(
            processing._response_mentions_missing_fields(
                "Hi Alex,\n\nThanks for sending the docks.",
                ["Docks"],
                get_default_column_config(),
            )
        )

    def test_polite_field_statement_without_request_intent_is_rejected(self):
        self.assertFalse(
            processing._response_mentions_missing_fields(
                "Please note the docks and power are included below.",
                ["Docks", "Power"],
                get_default_column_config(),
            )
        )

    def test_negated_field_request_is_rejected(self):
        self.assertFalse(
            processing._response_mentions_missing_fields(
                "We don't need you to send the docks or power.",
                ["Docks", "Power"],
                get_default_column_config(),
            )
        )

    def test_request_for_only_one_of_two_missing_fields_is_rejected(self):
        self.assertFalse(
            processing._response_mentions_missing_fields(
                "Hi Alex,\n\nCould you confirm the docks?",
                ["Docks", "Power"],
                get_default_column_config(),
            )
        )

    def test_natural_request_for_every_missing_field_is_accepted(self):
        self.assertTrue(
            processing._response_mentions_missing_fields(
                "Hi Alex,\n\nCould you confirm the dock count and electrical service?",
                ["Docks", "Power"],
                get_default_column_config(),
            )
        )

    def test_bare_what_statement_is_not_request_intent(self):
        self.assertFalse(
            processing._response_mentions_missing_fields(
                "What you sent covers the docks and power",
                ["Docks", "Power"],
                get_default_column_config(),
            )
        )

    def test_attachment_tag_question_is_not_request_intent(self):
        self.assertFalse(
            processing._response_mentions_missing_fields(
                "The docks and power are already in the attachment, right?",
                ["Docks", "Power"],
                get_default_column_config(),
            )
        )

    def test_explicit_confirmation_request_is_accepted(self):
        self.assertTrue(
            processing._response_mentions_missing_fields(
                "Could you confirm the docks and power?",
                ["Docks", "Power"],
                get_default_column_config(),
            )
        )

    def test_do_you_know_request_with_precise_aliases_is_accepted(self):
        self.assertTrue(
            processing._response_mentions_missing_fields(
                "Do you know the dock count and electrical capacity?",
                ["Docks", "Power"],
                get_default_column_config(),
            )
        )

    def test_operating_hours_statement_does_not_match_ops_ex(self):
        self.assertFalse(
            processing._response_mentions_missing_fields(
                "Operating hours are 8-5",
                ["Ops Ex /SF"],
                get_default_column_config(),
            )
        )

    def test_grade_statement_does_not_match_drive_ins(self):
        self.assertFalse(
            processing._response_mentions_missing_fields(
                "The grade of the property is excellent",
                ["Drive Ins"],
                get_default_column_config(),
            )
        )

    def test_operating_hours_request_does_not_match_ops_ex(self):
        self.assertFalse(
            processing._response_mentions_missing_fields(
                "Could you confirm the operating hours?",
                ["Ops Ex /SF"],
                get_default_column_config(),
            )
        )

    def test_property_grade_request_does_not_match_drive_ins(self):
        self.assertFalse(
            processing._response_mentions_missing_fields(
                "Could you confirm the grade of the property?",
                ["Drive Ins"],
                get_default_column_config(),
            )
        )

    def test_custom_missing_fields_are_all_required_in_request(self):
        config = get_default_column_config()
        config["customFields"] = {
            "HVAC": {"mode": "ask_required", "description": "HVAC system"},
            "TI Allowance": {
                "mode": "ask_required",
                "description": "Tenant improvement allowance",
            },
            "ESFR": {"mode": "ask_required", "description": "ESFR sprinklers"},
        }

        self.assertTrue(
            processing._response_mentions_missing_fields(
                "Could you confirm the HVAC, TI allowance, and ESFR?",
                ["HVAC", "TI Allowance", "ESFR"],
                config,
            )
        )

    def test_custom_missing_field_requires_its_exact_normalized_label(self):
        config = get_default_column_config()
        config["customFields"] = {
            "Building Condition": {
                "mode": "ask_required",
                "description": "Current building condition",
            }
        }

        self.assertFalse(
            processing._response_mentions_missing_fields(
                "Could you confirm the building?",
                ["Building Condition"],
                config,
            )
        )
        self.assertTrue(
            processing._response_mentions_missing_fields(
                "Could you confirm the building condition?",
                ["Building Condition"],
                config,
            )
        )

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

    def test_looking_forward_cleanup_is_case_insensitive_with_terminal_punctuation(self):
        variants = (
            "looking forward to your response.",
            "Looking Forward To Hearing From You!",
            "LOOKING FORWARD TO YOUR RESPONSE...",
        )

        for phrase in variants:
            with self.subTest(phrase=phrase):
                body = processing._sanitize_missing_fields_response_body(
                    "Hi Alex,\n\nCould you confirm the docks and power?\n\n" + phrase
                )

                self.assertNotIn("looking forward", body.lower())
                self.assertNotRegex(body, r"(?m)^\s*[.!?,;:-]+\s*$")

    def test_looking_forward_cleanup_removes_standalone_punctuation_debris(self):
        body = processing._sanitize_missing_fields_response_body(
            "Hi Alex,\n\n"
            "Could you confirm the docks and power?\n\n"
            "looking forward to hearing from you.\n"
            "."
        )

        self.assertNotRegex(body, r"(?m)^\s*[.!?,;:-]+\s*$")
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

    def test_acronym_missing_fields_preserve_casing(self):
        for field in ("A/C", "R&D Budget", "HVAC", "TI Allowance", "ESFR"):
            with self.subTest(field=field):
                body = processing._build_missing_fields_response("Alex Morgan", [field])

                self.assertIn(f"Could you confirm the {field}?", body)

    def test_ordinary_missing_fields_remain_sentence_cased(self):
        expected_labels = {
            "Docks": "docks",
            "Ceiling Ht": "clear height",
        }

        for field, expected_label in expected_labels.items():
            with self.subTest(field=field):
                body = processing._build_missing_fields_response(
                    "Alex Morgan", [field]
                )

                self.assertIn(f"Could you confirm the {expected_label}?", body)

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

    def test_missing_field_selector_accepts_complete_model_request_and_sanitizes(self):
        body = processing._select_missing_fields_response_body(
            "Hi Alex,\n\nCould you confirm the docks and power?\n\n"
            "looking forward to your response.",
            ["Docks", "Power"],
            get_default_column_config(),
            "Alex Morgan",
        )

        self.assertIn("confirm the docks and power", body)
        self.assertNotIn("looking forward", body.lower())

    def test_missing_field_selector_replaces_partial_model_request(self):
        body = processing._select_missing_fields_response_body(
            "Hi Alex,\n\nCould you confirm the docks?",
            ["Docks", "Power"],
            get_default_column_config(),
            "Alex Morgan",
        )

        self.assertIn("Could you confirm the docks and power?", body)

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
