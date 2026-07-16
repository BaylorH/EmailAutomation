import json
import os
import re
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from tests.run_broker_voice_large_campaign import (
    SafetyError,
    _token_usage,
    grade_case,
    run_live_case,
    safety_errors,
)


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "broker_voice_large_campaign.json"
)

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
    "expect",
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
                self.assertIn(case["expect"]["response_mode"], {"send", "null"})

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
        cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.cases = {case["scenario"]: case for case in cases}

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

    def test_exact_event_expectations_are_vetoed(self):
        proposal = self.broken_proposal("wrong contact")
        proposal["events"] = [{"type": "call_requested"}]
        self.assert_named_veto(
            "wrong contact", proposal, "event_types_mismatch"
        )

    def test_required_response_cannot_be_null(self):
        proposal = self.broken_proposal("one missing item")
        proposal["response_email"] = None
        self.assert_named_veto(
            "one missing item", proposal, "response_missing"
        )

    def test_null_response_mode_rejects_generated_copy(self):
        proposal = self.broken_proposal("wrong contact")
        proposal["response_email"] = "Hi Reese, I will contact the other person."
        self.assert_named_veto(
            "wrong contact", proposal, "unexpected_response"
        )

    def test_missing_required_term_is_vetoed(self):
        proposal = self.broken_proposal("one missing item")
        proposal["response_email"] = "Hi Casey,\n\nCould you share one more detail?"
        self.assert_named_veto(
            "one missing item", proposal, "missing_required_term"
        )

    def test_forbidden_term_is_vetoed(self):
        proposal = self.broken_proposal("unavailable plus alternative")
        proposal["response_email"] += " This looks like a perfect fit."
        self.assert_named_veto(
            "unavailable plus alternative", proposal, "forbidden_term"
        )

    def test_placeholder_is_vetoed(self):
        proposal = self.broken_proposal("one missing item")
        proposal["response_email"] += " The property is [PROPERTY NAME]."
        self.assert_named_veto(
            "one missing item", proposal, "placeholder"
        )

    def test_signature_is_vetoed(self):
        proposal = self.broken_proposal("one missing item")
        proposal["response_email"] += "\n\nBest regards,\nTest Assistant"
        self.assert_named_veto(
            "one missing item", proposal, "signature"
        )

    def test_reasking_a_supplied_field_is_vetoed(self):
        proposal = self.broken_proposal("drive-in count supplied")
        proposal["response_email"] += " Could you confirm the drive-in count?"
        self.assert_named_veto(
            "drive-in count supplied", proposal, "reasks_supplied_field"
        )

    def test_reasking_a_supplied_value_is_vetoed(self):
        proposal = self.broken_proposal("complete details")
        proposal["response_email"] += " Could you confirm 32,000 SF?"
        self.assert_named_veto(
            "complete details", proposal, "reasks_supplied_value"
        )

    def test_word_limit_is_vetoed(self):
        proposal = self.broken_proposal("one missing item")
        proposal["response_email"] += " " + "extra " * 50
        self.assert_named_veto(
            "one missing item", proposal, "word_limit"
        )

    def test_excessive_exclamation_is_vetoed(self):
        proposal = self.broken_proposal("one missing item")
        proposal["response_email"] += " Great!!"
        self.assert_named_veto(
            "one missing item", proposal, "excessive_exclamation"
        )

    def test_sensitive_event_auto_response_is_vetoed(self):
        proposal = self.broken_proposal("property issue")
        proposal["response_email"] = "Hi Sage, we will handle the roof issue."
        self.assert_named_veto(
            "property issue", proposal, "sensitive_event_auto_response"
        )


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
