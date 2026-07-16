import unittest

from email_automation.ai_processing import build_response_email_rules


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


if __name__ == "__main__":
    unittest.main()
