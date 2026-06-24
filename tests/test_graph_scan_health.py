import os
import tempfile
import unittest
from unittest.mock import patch

import requests

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)

import main
from email_automation import processing


class FakeTokenCache:
    has_state_changed = False

    def deserialize(self, _payload):
        return None

    def serialize(self):
        return "{}"


class FakeMsalApp:
    def __init__(self, *args, **kwargs):
        pass

    def get_accounts(self):
        return [{"home_account_id": "account-1"}]

    def acquire_token_silent(self, *args, **kwargs):
        return {
            "access_token": "fake-access-token",
            "expires_in": 3600,
        }


class ExpiringMsalApp:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    def get_accounts(self):
        return [{"home_account_id": "account-1"}]

    def acquire_token_silent(self, *args, **kwargs):
        force_refresh = bool(kwargs.get("force_refresh"))
        self.__class__.calls.append(force_refresh)
        if force_refresh:
            return {
                "access_token": "fresh-token",
                "expires_in": 3600,
            }
        return {
            "access_token": "expiring-token",
            "expires_in": 60,
        }


class FakeGraphErrorResponse:
    status_code = 404
    url = "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages?$top=50&$select=id"
    text = '{"error":{"code":"MailboxNotEnabledForRESTAPI","message":"The mailbox is either inactive, soft-deleted, or is hosted on-premise."}}'

    def json(self):
        return {
            "error": {
                "code": "MailboxNotEnabledForRESTAPI",
                "message": "The mailbox is either inactive, soft-deleted, or is hosted on-premise.",
            }
        }


def mailbox_not_enabled_error():
    return requests.exceptions.HTTPError(
        "404 Client Error: Not Found for url: https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages?$top=50&$select=id",
        response=FakeGraphErrorResponse(),
    )


class GraphScanHealthTests(unittest.TestCase):
    def test_inbox_scan_returns_error_state_when_graph_request_fails(self):
        with patch.object(processing, "exponential_backoff_request", side_effect=Exception("404 mailbox not found")):
            state = processing.scan_inbox_against_index("uid-1", {"Authorization": "Bearer fake"})

        self.assertEqual("error", state["status"])
        self.assertEqual("inbox_scan", state["operation"])
        self.assertIn("404 mailbox not found", state["error"])

    def test_inbox_scan_returns_admin_hint_for_mailbox_not_enabled(self):
        with patch.object(processing, "exponential_backoff_request", side_effect=mailbox_not_enabled_error()):
            state = processing.scan_inbox_against_index("uid-1", {"Authorization": "Bearer fake"})

        self.assertEqual("error", state["status"])
        self.assertEqual("inbox_scan", state["operation"])
        self.assertEqual("MailboxNotEnabledForRESTAPI", state["errorCode"])
        self.assertIn("mailbox is either inactive", state["errorMessage"])
        self.assertIn("Exchange Online mailbox", state["recoveryHint"])
        self.assertNotIn("https://graph.microsoft.com", state["error"])

    def test_sent_items_scan_returns_admin_hint_for_mailbox_not_enabled(self):
        with patch.object(processing, "_fs") as fs, \
             patch("email_automation.utils.exponential_backoff_request", side_effect=mailbox_not_enabled_error()):
            fs.collection.return_value.document.return_value.collection.return_value.stream.return_value = [
                type("ThreadDoc", (), {"to_dict": lambda self: {"conversationId": "conversation-1"}})()
            ]
            state = processing.scan_sent_items_for_manual_replies("uid-1", {"Authorization": "Bearer fake"})

        self.assertEqual("error", state["status"])
        self.assertEqual("sent_items_scan", state["operation"])
        self.assertEqual("MailboxNotEnabledForRESTAPI", state["errorCode"])
        self.assertIn("Graph can authenticate this user", state["recoveryHint"])
        self.assertNotIn("https://graph.microsoft.com", state["error"])

    def test_refresh_records_graph_error_when_inbox_scan_reports_error(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as token_file:
            token_file.write("{}")
            token_path = token_file.name

        record_calls = []

        def capture_health(user_id, **kwargs):
            record_calls.append((user_id, kwargs))
            return {}

        try:
            with patch.object(main, "TOKEN_CACHE", token_path), \
                 patch.object(main, "download_token"), \
                 patch.object(main, "SerializableTokenCache", FakeTokenCache), \
                 patch.object(main, "ConfidentialClientApplication", FakeMsalApp), \
                 patch.object(main, "send_outboxes"), \
                 patch.object(main, "scan_inbox_against_index", return_value={
                     "status": "error",
                     "operation": "inbox_scan",
                     "error": "404 mailbox not found",
                 }), \
                 patch.object(main, "scan_sent_items_for_manual_replies", return_value={
                     "status": "healthy",
                     "operation": "sent_items_scan",
                 }), \
                 patch.object(main, "process_pending_responses"), \
                 patch.object(main, "check_and_send_followups"), \
                 patch.object(main, "auto_cleanup_firestore"), \
                 patch.object(main, "reconcile_stale_processing_failures"), \
                 patch.object(main, "record_user_health", side_effect=capture_health):
                main.refresh_and_process_user("uid-1")
        finally:
            os.unlink(token_path)

        self.assertEqual(1, len(record_calls))
        graph_state = record_calls[0][1]["graph_state"]
        self.assertEqual("error", graph_state["status"])
        self.assertEqual("inbox_scan", graph_state["failedOperations"][0]["operation"])
        self.assertIn("404 mailbox not found", graph_state["failedOperations"][0]["error"])

    def test_refresh_passes_header_provider_that_forces_refresh_for_expiring_tokens(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as token_file:
            token_file.write("{}")
            token_path = token_file.name

        captured = {}

        def fake_send_outboxes(user_id, headers, headers_provider=None):
            captured["user_id"] = user_id
            captured["initial_headers"] = headers
            captured["refreshed_headers"] = headers_provider()

        try:
            ExpiringMsalApp.calls = []
            with patch.object(main, "TOKEN_CACHE", token_path), \
                 patch.object(main, "download_token"), \
                 patch.object(main, "SerializableTokenCache", FakeTokenCache), \
                 patch.object(main, "ConfidentialClientApplication", ExpiringMsalApp), \
                 patch.object(main, "send_outboxes", side_effect=fake_send_outboxes), \
                 patch.object(main, "scan_inbox_against_index", return_value={
                     "status": "healthy",
                     "operation": "inbox_scan",
                 }), \
                 patch.object(main, "scan_sent_items_for_manual_replies", return_value={
                     "status": "healthy",
                     "operation": "sent_items_scan",
                 }), \
                 patch.object(main, "process_pending_responses"), \
                 patch.object(main, "check_and_send_followups"), \
                 patch.object(main, "auto_cleanup_firestore"), \
                 patch.object(main, "reconcile_stale_processing_failures"), \
                 patch.object(main, "record_user_health"):
                main.refresh_and_process_user("uid-1")
        finally:
            os.unlink(token_path)

        self.assertEqual("uid-1", captured["user_id"])
        self.assertEqual("Bearer fresh-token", captured["initial_headers"]["Authorization"])
        self.assertEqual("Bearer fresh-token", captured["refreshed_headers"]["Authorization"])
        self.assertIn(True, ExpiringMsalApp.calls)

    def test_refresh_skips_processing_failure_replay_by_default(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as token_file:
            token_file.write("{}")
            token_path = token_file.name

        try:
            with patch.dict(os.environ, {"SITESIFT_ENABLE_PROCESSING_FAILURE_RETRY": ""}, clear=False), \
                 patch.object(main, "TOKEN_CACHE", token_path), \
                 patch.object(main, "download_token"), \
                 patch.object(main, "SerializableTokenCache", FakeTokenCache), \
                 patch.object(main, "ConfidentialClientApplication", FakeMsalApp), \
                 patch.object(main, "send_outboxes"), \
                 patch.object(main, "scan_inbox_against_index", return_value={
                     "status": "healthy",
                     "operation": "inbox_scan",
                 }), \
                 patch.object(main, "scan_sent_items_for_manual_replies", return_value={
                     "status": "healthy",
                     "operation": "sent_items_scan",
                 }), \
                 patch.object(main, "retry_processing_failures") as retry_processing_failures, \
                 patch.object(main, "process_pending_responses"), \
                 patch.object(main, "check_and_send_followups"), \
                 patch.object(main, "auto_cleanup_firestore"), \
                 patch.object(main, "reconcile_stale_processing_failures"), \
                 patch.object(main, "record_user_health"):
                main.refresh_and_process_user("uid-1")
        finally:
            os.unlink(token_path)

        retry_processing_failures.assert_not_called()

    def test_refresh_runs_processing_failure_replay_when_explicitly_enabled(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as token_file:
            token_file.write("{}")
            token_path = token_file.name

        try:
            with patch.dict(os.environ, {"SITESIFT_ENABLE_PROCESSING_FAILURE_RETRY": "1"}, clear=False), \
                 patch.object(main, "TOKEN_CACHE", token_path), \
                 patch.object(main, "download_token"), \
                 patch.object(main, "SerializableTokenCache", FakeTokenCache), \
                 patch.object(main, "ConfidentialClientApplication", FakeMsalApp), \
                 patch.object(main, "send_outboxes"), \
                 patch.object(main, "scan_inbox_against_index", return_value={
                     "status": "healthy",
                     "operation": "inbox_scan",
                 }), \
                 patch.object(main, "scan_sent_items_for_manual_replies", return_value={
                     "status": "healthy",
                     "operation": "sent_items_scan",
                 }), \
                 patch.object(main, "retry_processing_failures") as retry_processing_failures, \
                 patch.object(main, "process_pending_responses"), \
                 patch.object(main, "check_and_send_followups"), \
                 patch.object(main, "auto_cleanup_firestore"), \
                 patch.object(main, "reconcile_stale_processing_failures"), \
                 patch.object(main, "record_user_health"):
                main.refresh_and_process_user("uid-1")
        finally:
            os.unlink(token_path)

        retry_processing_failures.assert_called_once()


if __name__ == "__main__":
    unittest.main()
