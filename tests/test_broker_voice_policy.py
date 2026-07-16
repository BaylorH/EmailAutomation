import inspect
import unittest

from email_automation import ai_processing
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


if __name__ == "__main__":
    unittest.main()
