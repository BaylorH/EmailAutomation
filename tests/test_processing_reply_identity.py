import os
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation import processing


class ProcessingReplyIdentityTests(unittest.TestCase):
    def test_forwarded_thread_replies_use_current_sender_identity(self):
        identity = processing._resolve_reply_identity(
            thread_data={
                "email": ["jeff.beard@svn.com"],
                "contactName": "Jeff Beard",
            },
            rowvals=["6455 Highway 105", "Jeff Beard", "jeff.beard@svn.com"],
            header=["Address", "Leasing Contact", "Email"],
            from_addr="neal.king@svn.com",
            from_name="Neal King",
        )

        self.assertEqual(identity["recipient_email"], "neal.king@svn.com")
        self.assertEqual(identity["contact_name"], "Neal King")
        self.assertEqual(identity["source"], "current_sender")

    def test_same_sender_thread_keeps_stored_contact_identity(self):
        identity = processing._resolve_reply_identity(
            thread_data={
                "email": ["jeff.beard@svn.com"],
                "contactName": "Jeff Beard",
            },
            rowvals=["6455 Highway 105", "Jeff Beard", "jeff.beard@svn.com"],
            header=["Address", "Leasing Contact", "Email"],
            from_addr="jeff.beard@svn.com",
            from_name="Jeff B.",
        )

        self.assertEqual(identity["recipient_email"], "jeff.beard@svn.com")
        self.assertEqual(identity["contact_name"], "Jeff Beard")
        self.assertEqual(identity["source"], "stored_contact")

    def test_llm_greeting_is_aligned_to_current_sender_name(self):
        body = "Hi Jeff,\n\nPerfect, thank you. This covers everything."

        self.assertEqual(
            processing._align_response_greeting(body, "Neal King"),
            "Hi Neal,\n\nPerfect, thank you. This covers everything.",
        )

    def test_llm_greeting_drops_stale_name_when_sender_name_unknown(self):
        body = "Hi Jeff,\n\nCould you confirm the clear height?"

        self.assertEqual(
            processing._align_response_greeting(body, None),
            "Hi,\n\nCould you confirm the clear height?",
        )


if __name__ == "__main__":
    unittest.main()
