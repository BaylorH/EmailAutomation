import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import resend_failed_responses


class ResendFailedResponsesTests(unittest.TestCase):
    def test_resend_failed_responses_refuses_live_send_without_guard(self):
        with patch.object(resend_failed_responses, "get_headers_for_user") as get_headers, \
                patch.object(resend_failed_responses, "send_reply_in_thread") as send_reply:
            result = resend_failed_responses.resend_responses(
                "uid-1",
                dry_run=False,
                date_filter="2026-03-01",
            )

        self.assertFalse(result)
        get_headers.assert_not_called()
        send_reply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
