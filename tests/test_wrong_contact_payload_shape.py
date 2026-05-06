import unittest

from email_automation.notification_payloads import build_wrong_contact_suggested_email


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


if __name__ == "__main__":
    unittest.main()
