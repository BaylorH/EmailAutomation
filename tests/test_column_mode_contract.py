import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import ai_processing, column_config, processing
from email_automation.column_config import (
    get_column_config_error,
    get_default_column_config,
    get_default_mode_for_canonical,
)


class CanonicalColumnModeDefaultsTests(unittest.TestCase):
    def test_rent_is_ask_required_and_flyer_is_note(self):
        config = get_default_column_config()

        self.assertEqual("ask_required", get_default_mode_for_canonical("rent_sf_yr"))
        self.assertIn("rent_sf_yr", config["requiredFields"])
        self.assertNotIn("rent_sf_yr", config["neverRequest"])

        self.assertEqual("note", get_default_mode_for_canonical("flyer_link"))
        self.assertNotIn("flyer_link", config["requiredFields"])
        self.assertIn("flyer_link", config["neverRequest"])


class ColumnConfigFailClosedTests(unittest.TestCase):
    def _propose(self, column_config, extraction_fields=None):
        return ai_processing.propose_sheet_updates(
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
            column_config=column_config,
            extraction_fields=extraction_fields,
            dry_run=True,
        )

    def test_missing_or_malformed_config_never_reaches_openai(self):
        malformed_configs = [
            None,
            {},
            {"mappings": []},
            {
                "mappings": {"rent_sf_yr": "Rent/SF /Yr"},
                "requiredFields": "rent_sf_yr",
                "formulaFields": [],
                "neverRequest": [],
                "customFields": {},
            },
        ]

        for column_config in malformed_configs:
            with self.subTest(column_config=column_config), patch.object(
                ai_processing.client.responses,
                "create",
            ) as create:
                proposal = self._propose(column_config)

                self.assertIsNone(proposal)
                create.assert_not_called()

    def test_duplicate_extraction_fields_drift_fails_closed(self):
        with patch.object(ai_processing.client.responses, "create") as create:
            proposal = self._propose(
                get_default_column_config(),
                extraction_fields=["flyer_link"],
            )

        self.assertIsNone(proposal)
        create.assert_not_called()


class BrokerReplyColumnModeValidationTests(unittest.TestCase):
    def setUp(self):
        self.config = get_default_column_config()
        self.config["customFields"] = {
            "Broker Context": {"mode": "note", "description": "Context only"},
            "Internal Score": {"mode": "skip", "description": "Ignored"},
            "Building Condition Notes": {"mode": "note", "description": "Condition context"},
            "Condition Notes": {"mode": "note", "description": "Generic condition context"},
        }

    def test_accepts_request_for_missing_ask_field_only(self):
        body = "Thanks for the details. Could you also confirm the asking rent?"

        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )

    def test_requested_ask_fields_returns_every_explicit_question_target(self):
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        self.assertEqual(
            {"rent/sf /yr", "ops ex /sf"},
            set(helper(
                "Could you confirm both the asking rent and operating expenses?",
                self.config,
            )),
        )

    def test_requested_ask_fields_fails_closed_for_malformed_config(self):
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        self.assertEqual(
            [],
            helper(
                "Could you confirm both the asking rent and operating expenses?",
                {"mappings": self.config["mappings"]},
            ),
        )

    def test_rejects_question_reasking_known_ask_field_with_missing_field(self):
        body = "Could you confirm both the asking rent and operating expenses?"

        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

    def test_rejects_direct_structural_questions_for_known_ask_fields(self):
        cases = (
            (
                "Could you confirm operating expenses? What is the asking rent?",
                {"ops ex /sf", "rent/sf /yr"},
            ),
            (
                "Could you confirm operating expenses? How many docks are there?",
                {"ops ex /sf", "docks"},
            ),
            (
                "Could you confirm operating expenses? Do the premises have docks?",
                {"ops ex /sf", "docks"},
            ),
        )
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        for body, expected_fields in cases:
            with self.subTest(body=body):
                self.assertEqual(
                    expected_fields,
                    set(helper(body, self.config)),
                )
                self.assertFalse(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )

    def test_declarative_dock_context_does_not_become_a_request(self):
        bodies = (
            "There are many docks at the premises. Could you confirm operating expenses?",
            "The premises do have docks. Could you confirm operating expenses?",
        )
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(
                    {"ops ex /sf"},
                    set(helper(body, self.config)),
                )
                self.assertTrue(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )

    def test_decimal_rate_does_not_split_shared_request_clause(self):
        body = (
            "Could you confirm operating expenses at $3.25/SF and the asking rent?"
        )
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        self.assertEqual(
            {"ops ex /sf", "rent/sf /yr"},
            set(helper(body, self.config)),
        )
        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

    def test_rejects_semicolon_clauses_reasking_known_ask_field(self):
        body = "Could you confirm the asking rent; please provide operating expenses?"

        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

    def test_rejects_shared_request_intent_when_known_field_comes_last(self):
        for separator in (",", ";"):
            body = f"Could you confirm operating expenses{separator} asking rent?"

            with self.subTest(separator=separator):
                self.assertFalse(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )

    def test_rejects_conjoined_is_request_for_known_field(self):
        body = (
            "Could you confirm operating expenses, and is the asking rent still $12?"
        )
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        self.assertEqual(
            {"rent/sf /yr", "ops ex /sf"},
            set(helper(body, self.config)),
        )
        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

    def test_rejects_bullet_list_reasking_known_ask_field(self):
        body = "Could you please provide:\n- asking rent\n- operating expenses"

        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

    def test_rejects_question_leadin_bullets_reasking_known_ask_field(self):
        body = "Could you provide the following?\n- operating expenses\n- asking rent"

        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

        helper = getattr(column_config, "get_requested_ask_fields", None)
        self.assertTrue(callable(helper))
        self.assertEqual(
            {"rent/sf /yr", "ops ex /sf"},
            set(helper(body, self.config)),
        )

    def test_known_field_acknowledgement_does_not_become_a_request(self):
        bodies = (
            "Thanks for confirming the asking rent. Could you confirm operating expenses?",
            "Thanks for confirming the asking rent; please provide operating expenses.",
            "Thanks for confirming the asking rent.\nCould you please provide:\n- operating expenses",
            "Please note that the asking rent is already confirmed. Could you confirm operating expenses?",
            "Thanks for confirming the asking rent and could you confirm operating expenses?",
            "We already have the asking rent we need. Could you confirm operating expenses?",
        )

        for body in bodies:
            with self.subTest(body=body):
                self.assertTrue(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )

    def test_negated_known_field_request_verb_does_not_become_a_request(self):
        body = "We don't need the asking rent. Could you confirm operating expenses?"
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        self.assertEqual(
            {"ops ex /sf"},
            set(helper(body, self.config)),
        )
        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

    def test_negated_known_field_ask_clauses_do_not_become_requests(self):
        bodies = (
            "No need to confirm the asking rent. Could you confirm operating expenses?",
            "We don't need to ask about the asking rent. Could you confirm operating expenses?",
        )
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(
                    {"ops ex /sf"},
                    set(helper(body, self.config)),
                )
                self.assertTrue(
                    processing._response_mentions_missing_fields(
                        body,
                        ["Ops Ex /SF"],
                        self.config,
                    )
                )

    def test_configured_rent_header_does_not_trigger_short_size_alias(self):
        body = "Could you confirm the Rent/SF /Yr?"

        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )

    def test_per_sf_unit_does_not_become_a_total_size_request(self):
        body = "Could you confirm operating expenses per SF?"

        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                self.config,
            )
        )

    def test_sf_alias_disambiguation_is_local_to_each_match(self):
        body = "Could you confirm SF and Rent/SF /Yr?"
        helper = getattr(column_config, "get_requested_ask_fields", None)

        self.assertTrue(callable(helper))
        self.assertEqual(
            {"total sf", "rent/sf /yr"},
            set(helper(body, self.config)),
        )
        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )

    def test_unconfigured_missing_field_is_not_promoted_to_requestable(self):
        self.assertFalse(
            processing._response_mentions_missing_fields(
                "Could you confirm Rail Access?",
                ["Rail Access"],
                get_default_column_config(),
            )
        )

    def test_optional_ask_must_be_deliberately_listed_as_missing(self):
        config = get_default_column_config()
        config["mappings"]["total_sf"] = "Available Size"
        body = "Could you confirm the square footage and asking rent?"

        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                config,
            )
        )
        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr", "Available Size"],
                config,
            )
        )

    def test_missing_field_subset_normalizes_configured_header_spacing(self):
        config = get_default_column_config()
        config["mappings"]["total_sf"] = "Available / Size"
        body = "Could you confirm the square footage and asking rent?"

        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr", "available/size"],
                config,
            )
        )

    def test_rejects_known_custom_required_ask_field_paraphrase(self):
        config = get_default_column_config()
        config["customFields"]["Loading Capacity Details"] = {
            "mode": "ask_required",
            "description": "Required loading-area capacity",
        }
        body = "Could you confirm the loading capacity and operating expenses?"

        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Ops Ex /SF"],
                config,
            )
        )

    def test_formula_mode_drift_cannot_promote_formula_header_to_ask(self):
        config = get_default_column_config()
        config["formulaFields"] = list(config["formulaFields"])
        config["formulaFields"].remove("gross_rent")
        config["extractionFields"].append("gross_rent")
        config["requiredFields"].append("gross_rent")

        self.assertIsNotNone(get_column_config_error(config))
        self.assertFalse(
            processing._response_mentions_missing_fields(
                "Could you confirm Gross Rent?",
                [config["mappings"]["gross_rent"]],
                config,
            )
        )

    def test_unknown_canonical_cannot_be_promoted_from_missing_fields(self):
        config = get_default_column_config()
        config["mappings"]["mystery"] = "Rail Access"
        config["extractionFields"].append("mystery")
        config["requiredFields"].append("mystery")

        self.assertIsNotNone(get_column_config_error(config))
        self.assertFalse(
            processing._response_mentions_missing_fields(
                "Could you confirm Rail Access?",
                ["Rail Access"],
                config,
            )
        )

    def test_benign_listing_link_context_does_not_invalidate_rent_request(self):
        body = (
            "Could you confirm the asking rent, and here is the link to the listing "
            "for context: https://example.com/listing."
        )

        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )

    def test_short_alias_does_not_match_inside_unrelated_word(self):
        body = "Could you confirm the asking rent? This will be useful for review."

        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )

    def test_identity_column_words_do_not_count_as_skip_requests(self):
        body = "Could you confirm the asking rent for this city property?"

        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )

    def test_rejects_allowed_ask_mixed_with_note_field(self):
        body = (
            "Could you confirm the asking rent and also send the flyer or brochure?"
        )

        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )

    def test_rejects_allowed_ask_mixed_with_custom_skip_field(self):
        body = "Could you confirm the asking rent and your internal score?"

        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )

    def test_independent_guard_rejects_actual_flyer_request(self):
        for body in (
            "Could you please send the flyer or brochure?",
            "Is there a flyer available?",
            "What about the brochure?",
            "Do you have the flyer?",
            "We are interested in the flyer/listing link.",
            "Any chance you can share the flyer?",
            "Do you know the flyer?",
            "Could I get the flyer?",
            "May I get the flyer?",
            "Would it be possible to send the flyer?",
            "I would appreciate a flyer.",
        ):
            with self.subTest(body=body):
                self.assertTrue(
                    processing._response_requests_nonrequestable_fields(body, self.config)
                )

    def test_independent_guard_allows_benign_listing_link_context(self):
        body = "Here is the link to the listing for context."

        self.assertFalse(
            processing._response_requests_nonrequestable_fields(body, self.config)
        )

    def test_independent_guard_allows_informational_offer_of_flyer(self):
        for body in (
            "Happy to send over the flyer and asking rate whenever useful.",
            "Here is the flyer you asked for.",
            "I know the flyer is available.",
            "I could get the flyer from our files.",
            "It would be possible to send the flyer later.",
            "I appreciate the flyer you sent.",
        ):
            with self.subTest(body=body):
                self.assertFalse(
                    processing._response_requests_nonrequestable_fields(body, self.config)
                )

    def test_independent_guard_rejects_configured_canonical_and_custom_notes(self):
        for body in (
            "What about the Listing Broker Comments?",
            "Do you have the Client Comments?",
            "We are interested in the Broker Context.",
        ):
            with self.subTest(body=body):
                self.assertTrue(
                    processing._response_requests_nonrequestable_fields(body, self.config)
                )

    def test_custom_note_paraphrase_uses_distinctive_tokens(self):
        self.assertTrue(
            processing._response_requests_nonrequestable_fields(
                "Could you share the building condition?",
                self.config,
            )
        )

    def test_custom_note_paraphrase_does_not_create_single_word_alias(self):
        self.assertFalse(
            processing._response_requests_nonrequestable_fields(
                "Could you confirm the asking rent given the condition?",
                self.config,
            )
        )

    def test_incomplete_legacy_shape_is_rejected_without_guessing_ask_fields(self):
        incomplete = {
            "mappings": self.config["mappings"],
            "requiredFields": self.config["requiredFields"],
            "formulaFields": self.config["formulaFields"],
            "neverRequest": self.config["neverRequest"],
        }

        self.assertTrue(
            processing._response_requests_nonrequestable_fields(
                "Could you confirm the asking rent?",
                incomplete,
            )
        )


class AutomaticResponseScenarioValidationTests(unittest.TestCase):
    def setUp(self):
        self.config = get_default_column_config()
        self.unsafe_llm_body = (
            "Thanks for the update. Could you also send the flyer or brochure?"
        )

    def test_scenario_1_uses_safe_alternative_property_fallback(self):
        body = processing._select_automatic_response_body(
            "nonviable_with_alternative",
            self.unsafe_llm_body,
            self.config,
            "Alex",
        )

        self.assertNotIn("flyer", body.lower())
        self.assertIn("alternative property", body.lower())

    def test_scenario_2_uses_safe_alternatives_fallback(self):
        body = processing._select_automatic_response_body(
            "nonviable",
            self.unsafe_llm_body,
            self.config,
            "Alex",
        )

        self.assertNotIn("flyer", body.lower())
        self.assertIn("other properties", body.lower())

    def test_scenario_4_uses_safe_completion_fallback(self):
        body = processing._select_automatic_response_body(
            "complete",
            self.unsafe_llm_body,
            self.config,
            "Alex",
        )

        self.assertNotIn("flyer", body.lower())
        self.assertIn("everything we need", body.lower())

    def test_scenario_keeps_llm_copy_with_benign_link_context(self):
        safe_llm_body = (
            "Thanks for the details. Here is the link to the listing I reviewed."
        )

        body = processing._select_automatic_response_body(
            "complete",
            safe_llm_body,
            self.config,
            "Alex",
        )

        self.assertEqual(safe_llm_body, body)


class ProposalFailureVisibilityRegressionTests(unittest.TestCase):
    def test_proposal_none_branch_records_failure_and_raises_retryable(self):
        source = Path(processing.__file__).read_text(encoding="utf-8")

        self.assertIn('else:\n            print("ℹ️ No proposal generated; nothing to apply.")', source)
        self.assertIn("_record_ai_processing_failure(", source)
        self.assertIn(
            'raise RetryableProcessingError("OpenAI proposal was unavailable or invalid JSON")',
            source,
        )


if __name__ == "__main__":
    unittest.main()
