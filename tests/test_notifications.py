import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation import notifications


class NotificationTests(unittest.TestCase):
    def test_extract_row_number_from_update_range(self):
        self.assertEqual(
            notifications.extract_row_number_from_update({"range": "Campaign!F27"}),
            27,
        )
        self.assertEqual(
            notifications.extract_row_number_from_update({"range": "'Leasing Sheet'!A104:B104"}),
            104,
        )
        self.assertEqual(
            notifications.extract_row_number_from_update({"rowNumber": 19, "range": "Campaign!F27"}),
            19,
        )

    def test_sheet_update_notifications_include_row_number_from_range(self):
        with patch.object(notifications, "write_notification", return_value="notification-1") as write_notification, \
             patch.object(notifications, "_fs"):
            notifications.add_client_notifications(
                uid="uid-1",
                client_id="client-1",
                email="broker@example.com",
                thread_id="thread-1",
                applied_updates=[{
                    "range": "Campaign!G42",
                    "column": "Asking Rent",
                    "oldValue": "",
                    "newValue": "$12.50",
                    "reason": "Broker replied",
                    "confidence": 0.92,
                }],
                address="123 Row Anchor Ave",
            )

        _, kwargs = write_notification.call_args
        self.assertEqual(kwargs["row_number"], 42)
        self.assertEqual(kwargs["row_anchor"], "123 Row Anchor Ave")
        self.assertEqual(kwargs["meta"]["rowNumber"], 42)


if __name__ == "__main__":
    unittest.main()
