import os
import unittest
from unittest import mock

from googleapiclient.errors import HttpError

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation.sheets import _execute_with_retry


def _http_error(status: int) -> HttpError:
    response = mock.Mock(status=status, reason="test failure")
    return HttpError(response, b'{"error":{"message":"test failure"}}')


class _SequencedRequest:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def execute(self):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class SheetsRetryTests(unittest.TestCase):
    @mock.patch("email_automation.sheets.time.sleep")
    @mock.patch("email_automation.sheets.random.uniform", return_value=0)
    def test_transient_server_error_is_retried(self, _jitter, _sleep):
        request = _SequencedRequest([_http_error(500), {"values": [["ok"]]}])

        try:
            result = _execute_with_retry(request, "campaign row verification")
        except HttpError as exc:
            self.fail(f"transient Sheets 500 was not retried: {exc}")

        self.assertEqual(result, {"values": [["ok"]]})
        self.assertEqual(request.calls, 2)
        _sleep.assert_called_once()

    @mock.patch("email_automation.sheets.time.sleep")
    def test_client_error_is_not_retried(self, sleep):
        request = _SequencedRequest([_http_error(400)])

        with self.assertRaises(HttpError):
            _execute_with_retry(request, "campaign row verification")

        self.assertEqual(request.calls, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
