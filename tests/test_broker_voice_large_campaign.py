import json
import re
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
