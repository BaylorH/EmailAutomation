import os
import unittest
from unittest.mock import patch


class LegacyEmailOperationsDisabledTests(unittest.TestCase):
    def setUp(self):
        self._env_patch = patch.dict(os.environ, {}, clear=True)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def test_legacy_send_helpers_are_disabled_before_graph_by_default(self):
        with patch.dict(
            os.environ,
            {
                "E2E_TEST_MODE": "true",
                "FIRESTORE_EMULATOR_HOST": "localhost:8080",
                "GOOGLE_CLOUD_PROJECT": "email-automation-cache",
            },
            clear=False,
        ):
            from email_automation import email_operations

        self.assertTrue(hasattr(email_operations, "LegacyEmailOperationsDisabled"))

        cases = [
            (
                email_operations.send_remaining_questions_email,
                (
                    "uid-1",
                    "client-1",
                    {"Authorization": "Bearer token"},
                    "broker@example.com",
                    ["Clear height"],
                    "<root@example.com>",
                    3,
                    "row-3",
                ),
            ),
            (
                email_operations.send_closing_email,
                (
                    "uid-1",
                    "client-1",
                    {"Authorization": "Bearer token"},
                    "broker@example.com",
                    "<root@example.com>",
                    3,
                    "row-3",
                ),
            ),
            (
                email_operations.send_new_property_email,
                (
                    "uid-1",
                    "client-1",
                    {"Authorization": "Bearer token"},
                    "broker@example.com",
                    "123 Test Way",
                    "Houston",
                    3,
                ),
            ),
            (
                email_operations.send_thankyou_closing_with_new_property,
                (
                    "uid-1",
                    "client-1",
                    {"Authorization": "Bearer token"},
                    "broker@example.com",
                    "<root@example.com>",
                    3,
                    "row-3",
                    "456 Backup Rd",
                ),
            ),
            (
                email_operations.send_thankyou_ask_alternatives,
                (
                    "uid-1",
                    "client-1",
                    {"Authorization": "Bearer token"},
                    "broker@example.com",
                    "<root@example.com>",
                    3,
                    "row-3",
                ),
            ),
        ]

        for func, args in cases:
            with self.subTest(func=func.__name__), patch.object(
                email_operations.requests,
                "post",
                side_effect=AssertionError("Legacy email helper touched Graph"),
            ):
                with self.assertRaises(email_operations.LegacyEmailOperationsDisabled):
                    func(*args)


if __name__ == "__main__":
    unittest.main()
