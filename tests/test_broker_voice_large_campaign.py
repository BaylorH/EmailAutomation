import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from tests.run_broker_voice_large_campaign import (
    SafetyError,
    _markdown_report,
    _token_usage,
    grade_case,
    run_live_case,
    safety_errors,
)


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "broker_voice_large_campaign.json"
)
RUNNER_PATH = Path(__file__).parent / "run_broker_voice_large_campaign.py"

EXPECTED_SCENARIOS = {
    "partial details",
    "complete details",
    "one missing item",
    "two missing items",
    "four missing items",
    "flyer attachment supplied",
    "floorplan attachment supplied",
    "drive-in count supplied",
    "unavailable",
    "unavailable plus alternative",
    "alternative with new contact",
    "wrong contact",
    "explicit call request",
    "tour offer",
    "tour time reply",
    "negotiation",
    "client requirement question",
    "client identity question",
    "legal/LOI question",
    "property issue",
    "opt-out",
    "ambiguous",
}

REQUIRED_CASE_KEYS = {
    "scenario",
    "row",
    "column_config",
    "conversation",
    "event_payloads",
    "expect",
    "expected_updates",
    "forbidden_update_columns",
    "offline_proposal",
}

REQUIRED_EXPECT_KEYS = {
    "event_types",
    "response_mode",
    "must_mention",
    "must_not_mention",
    "max_words",
}


class BrokerVoiceLargeCampaignFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def test_fixture_has_exactly_22_unique_scenarios_and_row_ids(self):
        self.assertEqual(22, len(self.cases))
        self.assertEqual(EXPECTED_SCENARIOS, {case["scenario"] for case in self.cases})

        row_ids = [case["row"]["id"] for case in self.cases]
        self.assertEqual(22, len(set(row_ids)))

    def test_every_case_has_the_complete_offline_grading_contract(self):
        for case in self.cases:
            with self.subTest(scenario=case.get("scenario")):
                self.assertTrue(REQUIRED_CASE_KEYS <= set(case))
                self.assertTrue(REQUIRED_EXPECT_KEYS <= set(case["expect"]))
                self.assertIsInstance(case["column_config"], dict)
                self.assertIsInstance(case["conversation"], list)
                self.assertTrue(case["conversation"])
                self.assertIsInstance(case.get("pdf_manifest", []), list)
                self.assertIsInstance(case["offline_proposal"], dict)
                self.assertIsInstance(case["expected_updates"], list)
                self.assertIsInstance(case["event_payloads"], list)
                self.assertIsInstance(case["forbidden_update_columns"], list)
                self.assertEqual(
                    case["expect"]["event_types"],
                    [
                        event["type"]
                        for event in case["event_payloads"]
                    ],
                )
                self.assertIn(
                    case["expect"]["response_mode"],
                    {"send", "null"},
                )
                self.assertNotIn("deterministic_fallback", case["expect"])
                optional_events = case["expect"].get(
                    "allowed_optional_event_types", []
                )
                self.assertIsInstance(optional_events, list)
                self.assertTrue(
                    all(isinstance(event_type, str) for event_type in optional_events)
                )
                self.assertFalse(
                    set(optional_events) & set(case["expect"]["event_types"])
                )
                for aliases in case["expect"].get("must_mention_any", []):
                    self.assertIsInstance(aliases, list)
                    self.assertGreaterEqual(len(aliases), 2)
                    self.assertTrue(all(isinstance(alias, str) for alias in aliases))

    def test_proposal_only_completed_rows_do_not_require_runtime_close_events(self):
        by_scenario = {case["scenario"]: case for case in self.cases}

        for scenario in ("complete details", "flyer attachment supplied"):
            with self.subTest(scenario=scenario):
                case = by_scenario[scenario]
                self.assertEqual([], case["expect"]["event_types"])
                self.assertEqual([], case["offline_proposal"]["events"])

        drive_in_case = by_scenario["drive-in count supplied"]
        self.assertEqual([], drive_in_case["expect"]["event_types"])
        self.assertEqual(
            ["close_conversation"],
            drive_in_case["expect"]["allowed_optional_event_types"],
        )

    def test_unavailable_rows_encode_production_send_or_manual_contract(self):
        by_scenario = {case["scenario"]: case for case in self.cases}

        for scenario in ("unavailable", "unavailable plus alternative"):
            with self.subTest(scenario=scenario):
                self.assertEqual(
                    "send",
                    by_scenario[scenario]["expect"]["response_mode"],
                )
        referral = by_scenario["alternative with new contact"]
        self.assertEqual("null", referral["expect"]["response_mode"])
        self.assertTrue(referral["offline_proposal"]["skip_response"])
        self.assertIsNone(referral["offline_proposal"]["response_email"])

    def test_missing_field_cases_define_semantic_acceptable_term_groups(self):
        by_scenario = {case["scenario"]: case for case in self.cases}

        for scenario in (
            "partial details",
            "one missing item",
            "two missing items",
            "four missing items",
            "floorplan attachment supplied",
        ):
            with self.subTest(scenario=scenario):
                self.assertTrue(by_scenario[scenario]["expect"]["must_mention_any"])

        dock_aliases = by_scenario["two missing items"]["expect"][
            "must_mention_any"
        ][0]
        self.assertIn("dock count", dock_aliases)
        self.assertIn("number of dock-high doors", dock_aliases)

    def test_fixture_uses_only_reserved_example_test_recipients(self):
        recipients = [case["row"]["recipient"] for case in self.cases]
        self.assertEqual(
            [f"broker+row{row_number:02d}@example.test" for row_number in range(1, 23)],
            recipients,
        )

        serialized = json.dumps(self.cases)
        emails = re.findall(r"[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+", serialized)
        self.assertTrue(emails)
        normalized_emails = [email.rstrip(".,").lower() for email in emails]
        self.assertTrue(
            all(email.endswith("@example.test") for email in normalized_emails)
        )

    def test_fixture_contains_no_known_production_identity_markers(self):
        serialized = json.dumps(self.cases).lower()
        for forbidden in (
            "mohr",
            "jill",
            "baylor",
            "sitesiftai.com",
            "outlook.com",
            "gmail.com",
            "firebase",
            "campaignid",
            "clientid",
            "userid",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialized)


class BrokerVoiceLargeCampaignGraderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = patch.dict(
            os.environ,
            {
                "E2E_TEST_MODE": "true",
                "FIRESTORE_EMULATOR_HOST": "127.0.0.1:8080",
                "GOOGLE_CLOUD_PROJECT": "sitesift-test",
                "SITESIFT_DISABLE_GRAPH_SENDS": "1",
            },
        )
        cls.environment.start()
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.cases = {case["scenario"]: case for case in cases}

    @classmethod
    def tearDownClass(cls):
        cls.environment.stop()

    def broken_proposal(self, scenario):
        return deepcopy(self.cases[scenario]["offline_proposal"])

    def assert_named_veto(self, scenario, proposal, veto):
        result = grade_case(self.cases[scenario], proposal)
        self.assertIn(veto, result["vetoes"])
        self.assertLess(result["score"], 100)

    def test_all_fixture_owned_clean_proposals_score_100(self):
        for scenario, case in self.cases.items():
            with self.subTest(scenario=scenario):
                result = grade_case(case, case["offline_proposal"])
                self.assertEqual(100, result["score"])
                self.assertEqual([], result["vetoes"])

    def test_proposed_updates_are_applied_before_runtime_field_selection(self):
        partial = grade_case(
            self.cases["partial details"],
            self.cases["partial details"]["offline_proposal"],
        )
        complete = grade_case(
            self.cases["complete details"],
            self.cases["complete details"]["offline_proposal"],
        )

        self.assertEqual(3, partial["applied_update_count"])
        self.assertEqual(
            ["Rent/SF /Yr", "Ops Ex /SF", "Drive Ins", "Power"],
            partial["missing_fields"],
        )
        self.assertEqual(7, complete["applied_update_count"])
        self.assertEqual([], complete["missing_fields"])

    def test_exact_event_expectations_are_vetoed(self):
        proposal = self.broken_proposal("wrong contact")
        proposal["events"] = [{"type": "call_requested"}]
        self.assert_named_veto(
            "wrong contact", proposal, "event_types_mismatch"
        )

    def test_missing_expected_update_is_vetoed(self):
        case = deepcopy(self.cases["partial details"])
        case["expected_updates"] = [
            {"column": "Total SF", "value": "18400"}
        ]
        proposal = self.broken_proposal("partial details")
        proposal["updates"] = []

        result = grade_case(case, proposal)

        self.assertIn("missing_expected_update", result["vetoes"])
        self.assertIn("missing_expected_update", result["safety_vetoes"])

    def test_unexpected_update_is_vetoed(self):
        proposal = self.broken_proposal("one missing item")
        proposal["updates"] = [{"column": "Docks", "value": "9"}]

        self.assert_named_veto(
            "one missing item", proposal, "unexpected_update"
        )

    def test_fabricated_update_value_is_vetoed_after_normalization(self):
        case = deepcopy(self.cases["partial details"])
        case["expected_updates"] = [
            {"column": "Total SF", "value": "18400"}
        ]
        proposal = self.broken_proposal("partial details")
        proposal["updates"] = [{"column": " total sf ", "value": "18,401"}]

        result = grade_case(case, proposal)

        self.assertIn("fabricated_update_value", result["vetoes"])
        self.assertNotIn("missing_expected_update", result["vetoes"])

    def test_normalized_expected_update_is_accepted(self):
        case = deepcopy(self.cases["partial details"])
        case["expected_updates"] = [
            {"column": "Total SF", "value": "18400"}
        ]
        proposal = self.broken_proposal("partial details")
        proposal["updates"] = [{"column": " total sf ", "value": "18,400"}]

        result = grade_case(case, proposal)

        self.assertNotIn("missing_expected_update", result["vetoes"])
        self.assertNotIn("unexpected_update", result["vetoes"])
        self.assertNotIn("fabricated_update_value", result["vetoes"])

    def test_semantically_equivalent_power_notation_is_accepted(self):
        case = deepcopy(self.cases["complete details"])
        case["expected_updates"] = [
            {"column": "Power", "value": "1,200 amps at 480V"}
        ]
        proposal = deepcopy(case["offline_proposal"])
        proposal["updates"] = [
            {"column": "power", "value": "1200A, 480V"}
        ]

        result = grade_case(case, proposal)

        self.assertNotIn("fabricated_update_value", result["vetoes"])
        self.assertNotIn("missing_expected_update", result["vetoes"])
        self.assertNotIn("unexpected_update", result["vetoes"])

    def test_changed_power_value_is_still_rejected_as_fabricated(self):
        case = deepcopy(self.cases["complete details"])
        case["expected_updates"] = [
            {"column": "Power", "value": "1,200 amps at 480V"}
        ]
        proposal = deepcopy(case["offline_proposal"])
        proposal["updates"] = [
            {"column": "Power", "value": "1201A, 480V"}
        ]

        result = grade_case(case, proposal)

        self.assertIn("fabricated_update_value", result["vetoes"])

    def test_formula_update_is_vetoed(self):
        proposal = self.broken_proposal("one missing item")
        proposal["updates"] = [{"column": "Gross Rent", "value": "12.85"}]

        self.assert_named_veto(
            "one missing item", proposal, "formula_update"
        )

    def test_fixture_forbidden_update_is_vetoed(self):
        case = deepcopy(self.cases["one missing item"])
        case["forbidden_update_columns"] = ["Email"]
        proposal = self.broken_proposal("one missing item")
        proposal["updates"] = [
            {"column": "Email", "value": "other@example.test"}
        ]

        result = grade_case(case, proposal)

        self.assertIn("forbidden_update", result["vetoes"])

    def test_event_reason_semantics_are_vetoed(self):
        proposal = self.broken_proposal("negotiation")
        proposal["events"][0]["reason"] = "client_question"

        self.assert_named_veto(
            "negotiation", proposal, "event_payload_mismatch"
        )

    def test_property_issue_severity_semantics_are_vetoed(self):
        proposal = self.broken_proposal("property issue")
        proposal["events"][0]["severity"] = "minor"

        self.assert_named_veto(
            "property issue", proposal, "event_payload_mismatch"
        )

    def test_new_property_address_semantics_are_vetoed(self):
        proposal = self.broken_proposal("unavailable plus alternative")
        proposal["events"][1]["address"] = "999 Fabricated Test Road"

        self.assert_named_veto(
            "unavailable plus alternative",
            proposal,
            "event_payload_mismatch",
        )

    def test_referral_contact_and_email_semantics_are_vetoed(self):
        for field, value in (
            ("contactName", "Fabricated Contact"),
            ("email", "fabricated@example.test"),
        ):
            with self.subTest(field=field):
                proposal = self.broken_proposal("alternative with new contact")
                proposal["events"][1][field] = value

                self.assert_named_veto(
                    "alternative with new contact",
                    proposal,
                    "event_payload_mismatch",
                )

    def test_grade_reports_raw_updates_and_complete_events(self):
        case = self.cases["alternative with new contact"]
        proposal = case["offline_proposal"]

        result = grade_case(case, proposal)

        self.assertEqual(proposal["updates"], result["raw_updates"])
        self.assertEqual(proposal["events"], result["actual_events"])

    def test_required_response_cannot_be_null(self):
        proposal = self.broken_proposal("unavailable")
        proposal["skip_response"] = True
        self.assert_named_veto(
            "unavailable", proposal, "response_missing"
        )

    def test_null_response_mode_rejects_generated_copy(self):
        proposal = self.broken_proposal("wrong contact")
        proposal["response_email"] = "Hi Reese, I will contact the other person."
        self.assert_named_veto(
            "wrong contact", proposal, "unexpected_response"
        )

    def test_missing_required_term_is_vetoed(self):
        proposal = self.broken_proposal("unavailable")
        proposal["response_email"] = "Hi Skyler,\n\nThank you for the update."
        self.assert_named_veto(
            "unavailable", proposal, "missing_required_term"
        )

    def test_semantic_missing_field_aliases_are_accepted(self):
        proposal = self.broken_proposal("two missing items")
        proposal["response_email"] = (
            "Hi Morgan,\n\nCould you share the number of dock-high doors and "
            "electrical service?"
        )

        result = grade_case(self.cases["two missing items"], proposal)

        self.assertEqual(100, result["score"])
        self.assertEqual([], result["vetoes"])

    def test_singular_dock_high_door_count_alias_is_accepted(self):
        proposal = self.broken_proposal("four missing items")
        proposal["response_email"] = (
            "Hi,\n\nCould you share the asking rent, operating expenses, "
            "dock-high door count, and electrical service?"
        )

        result = grade_case(self.cases["four missing items"], proposal)

        self.assertEqual(100, result["score"])
        self.assertEqual([], result["vetoes"])

    def test_live_row_01_drive_in_door_alias_scores_100(self):
        proposal = self.broken_proposal("partial details")
        proposal["response_email"] = (
            "Hi,\n\nThank you - that's helpful. Confirming the building is "
            "18,400 SF with 4 dock-high doors and 28-foot clear. Do you have "
            "the asking rent and operating expenses, and can you confirm the "
            "number of drive-in doors and the electrical service?"
        )

        result = grade_case(self.cases["partial details"], proposal)

        self.assertEqual(100, result["score"])
        self.assertEqual([], result["vetoes"])

    def test_live_row_02_incomplete_close_uses_quality_gated_fallback(self):
        proposal = self.broken_proposal("complete details")
        raw_response = (
            "Hi,\n\nThank you - received. We'll review the 32,000 SF offering "
            "and circle back with any questions."
        )
        proposal["response_email"] = raw_response

        result = grade_case(self.cases["complete details"], proposal)

        self.assertEqual(100, result["score"])
        self.assertEqual([], result["vetoes"])
        self.assertEqual(raw_response, result["raw_response"])
        self.assertNotEqual(raw_response, result["effective_response"])
        self.assertEqual(
            "production_complete_fallback", result["effective_response_source"]
        )
        self.assertIn("other relevant properties", result["effective_response"])

    def test_complete_close_that_meets_quality_contract_remains_raw(self):
        case = self.cases["complete details"]

        result = grade_case(case, case["offline_proposal"])

        self.assertEqual(100, result["score"])
        self.assertEqual(
            "production_complete_model_copy",
            result["effective_response_source"],
        )
        self.assertEqual(result["raw_response"], result["effective_response"])

    def test_live_row_07_43_word_reply_is_within_realistic_limit(self):
        proposal = self.broken_proposal("floorplan attachment supplied")
        proposal["response_email"] = (
            "Hi,\n\nThanks for sending the floor plan for 707 Lathe Test "
            "Boulevard. Understood that the other details are current. When "
            "you\u2019re able to confirm the electrical service/power specs for "
            "the building, please send them over and I\u2019ll update our file."
        )

        result = grade_case(self.cases["floorplan attachment supplied"], proposal)

        self.assertEqual(43, result["raw_word_count"])
        self.assertLessEqual(result["word_count"], 50)
        self.assertEqual(100, result["score"])

    def test_live_row_08_optional_close_and_numeric_drive_ins_score_100(self):
        proposal = self.broken_proposal("drive-in count supplied")
        proposal["events"] = [{"type": "close_conversation"}]
        proposal["response_email"] = (
            "Hi,\n\nThanks - confirmed. I've noted 3 drive-ins and will review "
            "the details with the team. If you have other similar availabilities, "
            "feel free to send them over."
        )

        result = grade_case(self.cases["drive-in count supplied"], proposal)

        self.assertEqual(100, result["score"])
        self.assertEqual([], result["vetoes"])
        self.assertEqual([], result["safety_vetoes"])
        self.assertEqual(
            ["close_conversation"], result["allowed_optional_event_types"]
        )

    def test_row_08_still_vetoes_any_nonoptional_extra_event(self):
        proposal = self.broken_proposal("drive-in count supplied")
        proposal["events"] = [{"type": "new_property"}]

        result = grade_case(self.cases["drive-in count supplied"], proposal)

        self.assertIn("event_types_mismatch", result["vetoes"])
        self.assertIn("event_types_mismatch", result["safety_vetoes"])

    def test_unavailable_rows_use_production_fallback_when_raw_is_null(self):
        for scenario, expected_fragment in (
            ("unavailable", "another relevant property"),
            ("unavailable plus alternative", "alternative"),
        ):
            with self.subTest(scenario=scenario):
                case = self.cases[scenario]
                proposal = self.broken_proposal(scenario)
                proposal["response_email"] = None

                result = grade_case(case, proposal)

                self.assertEqual(100, result["score"])
                self.assertEqual([], result["vetoes"])
                self.assertIsNone(result["raw_response"])
                self.assertEqual(
                    "production_automatic_fallback",
                    result["effective_response_source"],
                )
                self.assertIn(
                    expected_fragment, result["effective_response"].lower()
                )

    def test_unavailable_rows_accept_safe_raw_copy_without_fallback(self):
        case = self.cases["unavailable"]
        proposal = self.broken_proposal("unavailable")
        proposal["response_email"] = (
            "Hi Skyler,\n\nThank you for the update. Do you have another "
            "relevant property we should consider?"
        )

        result = grade_case(case, proposal)

        self.assertEqual(100, result["score"])
        self.assertEqual(
            "production_automatic_model_copy",
            result["effective_response_source"],
        )
        self.assertEqual(result["raw_response"], result["effective_response"])

    def test_alternative_fallback_does_not_invent_addresses(self):
        proposal = self.broken_proposal("unavailable plus alternative")
        proposal["response_email"] = None

        result = grade_case(self.cases["unavailable plus alternative"], proposal)

        self.assertEqual(100, result["score"])
        for detail in ("1010 Hoist Test Trail", "1110 Kiln Test Way"):
            self.assertNotIn(detail, result["effective_response"])

    def test_different_contact_referral_remains_manual_with_no_effective_reply(self):
        case = self.cases["alternative with new contact"]

        result = grade_case(case, case["offline_proposal"])

        self.assertEqual(100, result["score"])
        self.assertIsNone(result["raw_response"])
        self.assertIsNone(result["effective_response"])
        self.assertEqual("skip_response", result["effective_response_source"])

    def test_zero_update_complete_row_gets_no_synthetic_completion_fallback(self):
        case = deepcopy(self.cases["complete details"])
        case["row"]["cells"].update(
            {
                update["column"]: update["value"]
                for update in case["expected_updates"]
            }
        )
        case["expected_updates"] = []
        case["expect"]["response_mode"] = "null"
        proposal = deepcopy(case["offline_proposal"])
        proposal["updates"] = []
        proposal["response_email"] = "Hi Jordan,\n\nGot it, thanks."

        result = grade_case(case, proposal)

        self.assertIsNone(result["effective_response"])
        self.assertEqual(
            "zero_update_complete_no_fallback",
            result["effective_response_source"],
        )

    def test_forbidden_term_is_vetoed(self):
        proposal = self.broken_proposal("unavailable plus alternative")
        proposal["response_email"] += " This looks like a perfect fit."
        self.assert_named_veto(
            "unavailable plus alternative", proposal, "forbidden_term"
        )

    def test_placeholder_is_vetoed(self):
        proposal = self.broken_proposal("unavailable")
        proposal["response_email"] = (
            "Hi Skyler,\n\nThank you for the update. Do you have another "
            "relevant property we should consider? [PROPERTY NAME]"
        )
        self.assert_named_veto(
            "unavailable", proposal, "placeholder"
        )

    def test_signature_is_vetoed(self):
        proposal = self.broken_proposal("unavailable")
        proposal["response_email"] = (
            "Hi Skyler,\n\nThank you for the update. Do you have another "
            "relevant property we should consider?\n\nBest regards,\nTest Assistant"
        )
        self.assert_named_veto(
            "unavailable", proposal, "signature"
        )

    def test_reasking_a_supplied_field_is_vetoed(self):
        case = deepcopy(self.cases["unavailable"])
        case["expect"]["supplied_fields"] = ["Docks"]
        proposal = deepcopy(case["offline_proposal"])
        proposal["response_email"] = (
            "Hi Skyler,\n\nThank you for the update. Do you have another "
            "relevant property we should consider? Could you confirm the docks?"
        )

        result = grade_case(case, proposal)

        self.assertIn("reasks_supplied_field", result["vetoes"])
        self.assertLess(result["score"], 100)

    def test_reasking_a_supplied_value_is_vetoed(self):
        proposal = self.broken_proposal("complete details")
        proposal["response_email"] += " Could you confirm 32,000 SF?"
        self.assert_named_veto(
            "complete details", proposal, "reasks_supplied_value"
        )

    def test_word_limit_is_vetoed(self):
        proposal = self.broken_proposal("unavailable")
        proposal["response_email"] = (
            "Hi Skyler,\n\nThank you for the update. Do you have another "
            "relevant property we should consider? "
            + "extra " * 50
        )
        self.assert_named_veto(
            "unavailable", proposal, "word_limit"
        )

    def test_excessive_exclamation_is_vetoed(self):
        proposal = self.broken_proposal("unavailable")
        proposal["response_email"] = (
            "Hi Skyler!\n\nThank you for the update! Do you have another "
            "relevant property we should consider?"
        )
        self.assert_named_veto(
            "unavailable", proposal, "excessive_exclamation"
        )

    def test_sensitive_event_auto_response_is_vetoed(self):
        proposal = self.broken_proposal("property issue")
        proposal["response_email"] = "Hi Sage, we will handle the roof issue."
        self.assert_named_veto(
            "property issue", proposal, "sensitive_event_auto_response"
        )

    def test_sensitive_event_cannot_gain_a_runtime_fallback(self):
        case = deepcopy(self.cases["property issue"])
        case["expect"]["response_mode"] = "send"

        result = grade_case(case, case["offline_proposal"])

        self.assertIsNone(result["effective_response"])
        self.assertEqual("sensitive_event", result["effective_response_source"])
        self.assertIn("response_missing", result["vetoes"])
        self.assertLess(result["score"], 100)


class BrokerVoiceLargeCampaignRunnerSafetyTests(unittest.TestCase):
    SAFE_ENV = {
        "FIRESTORE_EMULATOR_HOST": "127.0.0.1:8080",
        "GOOGLE_CLOUD_PROJECT": "sitesift-test",
        "SITESIFT_DISABLE_GRAPH_SENDS": "1",
    }

    @classmethod
    def setUpClass(cls):
        cls.case = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))[0]

    def test_safety_errors_name_each_missing_or_invalid_precondition(self):
        invalid_environments = (
            ({}, "FIRESTORE_EMULATOR_HOST"),
            (
                {
                    "FIRESTORE_EMULATOR_HOST": "127.0.0.1:8080",
                    "GOOGLE_CLOUD_PROJECT": "production-project",
                    "SITESIFT_DISABLE_GRAPH_SENDS": "1",
                },
                "GOOGLE_CLOUD_PROJECT",
            ),
            (
                {
                    "FIRESTORE_EMULATOR_HOST": "127.0.0.1:8080",
                    "GOOGLE_CLOUD_PROJECT": "demo-sitesift",
                    "SITESIFT_DISABLE_GRAPH_SENDS": "0",
                },
                "SITESIFT_DISABLE_GRAPH_SENDS",
            ),
        )
        for environment, expected_name in invalid_environments:
            with self.subTest(expected_name=expected_name):
                errors = safety_errors(environment)
                self.assertTrue(any(expected_name in error for error in errors))

    def test_model_supplied_token_claim_is_not_reported_as_provider_usage(self):
        usage, note = _token_usage(
            {"token_usage": {"input_tokens": 123, "output_tokens": 45}},
            "live-model",
        )
        self.assertIsNone(usage)
        self.assertIn("does not return provider usage", note)

    def test_markdown_renders_unknown_aggregate_token_usage_as_literal_null(self):
        markdown = _markdown_report(
            {
                "mode": "offline",
                "summary": {
                    "row_count": 0,
                    "aggregate_score": 0,
                    "veto_count": 0,
                    "safety_veto_count": 0,
                    "runtime_ms": 0,
                    "token_usage": None,
                    "token_usage_note": "Unavailable because no model call was made.",
                },
                "rows": [],
            }
        )

        self.assertIn("- Token usage: null", markdown)
        self.assertNotIn("- Token usage: None", markdown)
        self.assertIn("- Token note: Unavailable because no model call was made.", markdown)

    def test_markdown_distinguishes_raw_null_from_effective_fallback(self):
        markdown = _markdown_report(
            {
                "mode": "offline",
                "summary": {
                    "row_count": 1,
                    "aggregate_score": 100,
                    "veto_count": 0,
                    "safety_veto_count": 0,
                    "runtime_ms": 1,
                    "token_usage": None,
                    "token_usage_note": "Unavailable in offline mode.",
                },
                "rows": [
                    {
                        "row_id": "synthetic-row-09",
                        "scenario": "unavailable",
                        "score": 100,
                        "vetoes": [],
                        "runtime_ms": 1,
                        "token_usage": None,
                        "raw_response": None,
                        "effective_response_source": "production_automatic_fallback",
                    }
                ],
            }
        )

        self.assertIn("| Raw proposal response | Effective response source |", markdown)
        self.assertIn("| null | production_automatic_fallback |", markdown)

    def test_direct_live_script_resolves_application_package_before_model_call(self):
        environment = {
            "FIRESTORE_EMULATOR_HOST": self.SAFE_ENV["FIRESTORE_EMULATOR_HOST"],
            "GOOGLE_CLOUD_PROJECT": self.SAFE_ENV["GOOGLE_CLOUD_PROJECT"],
            "SITESIFT_DISABLE_GRAPH_SENDS": self.SAFE_ENV[
                "SITESIFT_DISABLE_GRAPH_SENDS"
            ],
        }
        with tempfile.TemporaryDirectory() as output_dir:
            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER_PATH),
                    "--mode",
                    "live-model",
                    "--output",
                    output_dir,
                ],
                cwd=RUNNER_PATH.parent.parent,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(0, result.returncode)
        self.assertNotIn("No module named 'email_automation'", result.stderr)
        self.assertIn("Missing required env vars", result.stderr)

    def test_live_case_rejects_unsafe_environment_before_application_import(self):
        with patch("builtins.__import__", wraps=__import__) as import_module:
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(SafetyError):
                    run_live_case(self.case)

        attempted_imports = [call.args[0] for call in import_module.call_args_list]
        self.assertNotIn("email_automation.ai_processing", attempted_imports)

    def test_live_case_calls_only_dry_run_proposal_with_fixture_conversation(self):
        fake_propose = Mock(return_value=self.case["offline_proposal"])
        fake_ai_processing = ModuleType("email_automation.ai_processing")
        fake_ai_processing.propose_sheet_updates = fake_propose

        with patch.dict(os.environ, self.SAFE_ENV, clear=True):
            with patch.dict(
                sys.modules,
                {"email_automation.ai_processing": fake_ai_processing},
            ):
                proposal = run_live_case(self.case)

        self.assertEqual(self.case["offline_proposal"], proposal)
        fake_propose.assert_called_once()
        call = fake_propose.call_args
        self.assertEqual(self.case["conversation"], call.kwargs["conversation"])
        self.assertIs(call.kwargs["dry_run"], True)


if __name__ == "__main__":
    unittest.main()
