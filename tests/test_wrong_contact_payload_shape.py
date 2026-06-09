import unittest

from email_automation.notification_payloads import (
    build_new_property_suggested_email,
    build_wrong_contact_suggested_email,
    sanitize_new_property_referral_response,
    should_skip_original_reply_for_new_property_referral,
)


class WrongContactPayloadShapeTests(unittest.TestCase):
    def test_wrong_contact_suggested_email_uses_frontend_payload_shape(self):
        payload = build_wrong_contact_suggested_email(
            original_contact="mike.wrong@example.com",
            suggested_contact="Dana Correct",
            suggested_email="Dana.Correct@Example.com",
            row_anchor="1200 Test Loop, North Las Vegas",
            referrer_name="Mike Wrong",
        )

        self.assertEqual(payload["to"], ["dana.correct@example.com"])
        self.assertEqual(payload["subject"], "RE: 1200 Test Loop, North Las Vegas")
        self.assertIn("Hi Dana,", payload["body"])
        self.assertIn("Mike Wrong mentioned", payload["body"])
        self.assertIn("1200 Test Loop, North Las Vegas", payload["body"])
        self.assertEqual(payload["contactName"], "Dana Correct")

    def test_wrong_contact_without_suggested_email_does_not_fall_back_to_original_contact(self):
        payload = build_wrong_contact_suggested_email(
            original_contact="mike.wrong@example.com",
            suggested_contact="Dana Correct",
            suggested_email="",
            row_anchor="1200 Test Loop",
            referrer_name="Mike Wrong",
        )

        self.assertEqual(payload["to"], [])
        self.assertNotIn("mike.wrong@example.com", payload["to"])

    def test_new_property_suggested_email_reads_like_fresh_outreach(self):
        payload = build_new_property_suggested_email(
            address="2629 E Craig Rd",
            city="North Las Vegas",
            to_email="Avery.Brooks@Example.com",
            contact_name="Avery Brooks",
            referrer_name="Monica Reyes",
            client_id="client-123",
        )

        self.assertEqual(payload["to"], ["avery.brooks@example.com"])
        self.assertEqual(payload["subject"], "2629 E Craig Rd, North Las Vegas")
        self.assertIn("Hi Avery,", payload["body"])
        self.assertIn("Monica Reyes mentioned", payload["body"])
        self.assertIn("Could you confirm availability", payload["body"])
        self.assertNotIn("Just like before", payload["body"])
        self.assertNotIn("If you think this might be a good fit", payload["body"])
        self.assertEqual(payload["contactName"], "Avery Brooks")
        self.assertEqual(payload["clientId"], "client-123")

    def test_new_property_suggested_email_same_contact_does_not_claim_broker_mentioned_them(self):
        payload = build_new_property_suggested_email(
            address="414 Alternate Signal Pkwy",
            city="North Las Vegas",
            to_email="devin@example.com",
            contact_name="Devin Replacement",
            referrer_name="",
            client_id="client-123",
        )

        self.assertIn("Hi Devin,", payload["body"])
        self.assertIn("Thanks for sending over 414 Alternate Signal Pkwy, North Las Vegas", payload["body"])
        self.assertIn("flyer or floor plans", payload["body"])
        self.assertNotIn("A broker mentioned", payload["body"])
        self.assertNotIn("you might be the right contact", payload["body"])

    def test_new_property_referral_to_different_contact_skips_original_auto_reply(self):
        self.assertTrue(
            should_skip_original_reply_for_new_property_referral(
                original_contact_email="monica@example.com",
                new_property_email="avery@example.com",
            )
        )
        self.assertFalse(
            should_skip_original_reply_for_new_property_referral(
                original_contact_email="monica@example.com",
                new_property_email="monica@example.com",
            )
        )

    def test_new_property_referral_to_different_contact_suppresses_reply_draft(self):
        proposal = {
            "events": [
                {
                    "type": "property_unavailable",
                    "address": "2860 N Commerce St",
                },
                {
                    "type": "new_property",
                    "address": "2629 E Craig Rd",
                    "city": "North Las Vegas",
                    "email": "avery@example.com",
                    "contactName": "Avery Brooks",
                },
            ],
            "response_email": "Hi Monica,\n\nPlease connect me with Avery.",
        }

        sanitized = sanitize_new_property_referral_response(
            proposal,
            original_contact_email="monica@example.com",
        )

        self.assertIsNone(sanitized["response_email"])
        self.assertTrue(sanitized["skip_response"])

    def test_new_property_same_contact_preserves_reply_draft(self):
        proposal = {
            "events": [
                {
                    "type": "new_property",
                    "address": "2629 E Craig Rd",
                    "city": "North Las Vegas",
                    "email": "monica@example.com",
                },
            ],
            "response_email": "Hi Monica,\n\nPlease send the details.",
        }

        sanitized = sanitize_new_property_referral_response(
            proposal,
            original_contact_email="monica@example.com",
        )

        self.assertEqual(sanitized["response_email"], proposal["response_email"])
        self.assertNotIn("skip_response", sanitized)


if __name__ == "__main__":
    unittest.main()
