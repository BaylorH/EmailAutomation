"""Rail: the SEND path must be visible to graph health.

Gap (audit HIGH): send_outboxes returned None and contributed no graph
operation state, so a fully broken Graph send left graph_state healthy while
inbox/sent receive scans succeeded — a silent send outage showed green.

These tests drive refresh_and_process_user with a broken send + healthy
receives and assert the recorded graph_state escalates to error (and that
system_health._overall_status therefore reports overall error).
"""

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import main
from email_automation import system_health


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
        return {"access_token": "fake-access-token", "expires_in": 3600}


class BrokenSendError(Exception):
    """Raised by a systemically-broken send driver (e.g. header provider dead)."""


HEALTHY_INBOX = {"status": "healthy", "operation": "inbox_scan"}
HEALTHY_SENT = {"status": "healthy", "operation": "sent_items_scan"}


def _run_refresh(*, send=None, send_side_effect=None, env=None,
                 inbox=None, sent=None):
    """Drive refresh_and_process_user with everything but the send path mocked.

    Returns the graph_state dict handed to record_user_health (or None if the
    call raised before recording).
    """
    inbox = inbox if inbox is not None else HEALTHY_INBOX
    sent = sent if sent is not None else HEALTHY_SENT

    with tempfile.NamedTemporaryFile("w", delete=False) as token_file:
        token_file.write("{}")
        token_path = token_file.name

    captured = {}

    def capture_health(user_id, **kwargs):
        captured["graph_state"] = kwargs.get("graph_state")
        captured["token_state"] = kwargs.get("token_state")
        return {}

    send_kwargs = {}
    if send_side_effect is not None:
        send_kwargs["side_effect"] = send_side_effect
    else:
        send_kwargs["return_value"] = send

    env = env or {}
    try:
        with patch.dict(os.environ, env, clear=False), \
             patch.object(main, "TOKEN_CACHE", token_path), \
             patch.object(main, "download_token"), \
             patch.object(main, "SerializableTokenCache", FakeTokenCache), \
             patch.object(main, "ConfidentialClientApplication", FakeMsalApp), \
             patch.object(main, "send_outboxes", **send_kwargs), \
             patch.object(main, "scan_inbox_against_index", return_value=inbox), \
             patch.object(main, "scan_sent_items_for_manual_replies", return_value=sent), \
             patch.object(main, "retry_processing_failures"), \
             patch.object(main, "process_pending_responses", return_value=0), \
             patch.object(main, "check_and_send_followups", return_value=0), \
             patch.object(main, "auto_cleanup_firestore"), \
             patch.object(main, "reconcile_stale_processing_failures"), \
             patch.object(main, "record_user_health", side_effect=capture_health):
            main.refresh_and_process_user("uid-1")
    finally:
        os.unlink(token_path)

    return captured


class SendPathGraphHealthTests(unittest.TestCase):
    def test_broken_send_returned_state_escalates_graph_to_error(self):
        """send_outboxes surfaces an error op-state → graph_state error, overall error."""
        broken = {
            "status": "error",
            "operation": "outbox_send",
            "errorCode": "ErrorAccessDenied",
            "error": "ErrorAccessDenied: Mail.Send permission revoked",
        }
        captured = _run_refresh(send=broken)

        graph_state = captured["graph_state"]
        self.assertEqual("error", graph_state["status"])
        failed_ops = [op["operation"] for op in graph_state["failedOperations"]]
        self.assertIn("outbox_send", failed_ops)

        overall = system_health._overall_status(
            {"status": "healthy"}, graph_state, {"outbox": 0}
        )
        self.assertEqual("error", overall)

    def test_send_that_raises_escalates_graph_to_error_but_keeps_receive_detail(self):
        """A systemic send exception is caught fail-closed, receive scans still recorded."""
        captured = _run_refresh(send_side_effect=BrokenSendError("header provider dead"))

        graph_state = captured["graph_state"]
        self.assertEqual("error", graph_state["status"])
        # Token still healthy (silent auth worked); the failure is on the send path.
        self.assertEqual("healthy", captured["token_state"]["status"])
        # Receive scans still contributed (their detail is preserved).
        recorded_ops = [op.get("operation") for op in graph_state["operations"]]
        self.assertIn("inbox_scan", recorded_ops)
        self.assertIn("sent_items_scan", recorded_ops)
        self.assertIn("outbox_send", recorded_ops)

    def test_legacy_none_return_does_not_false_positive(self):
        """No send state (None) + healthy receives → graph healthy (no false alarm)."""
        captured = _run_refresh(send=None)
        self.assertEqual("healthy", captured["graph_state"]["status"])

    def test_healthy_send_state_contributes_and_stays_healthy(self):
        captured = _run_refresh(send={"status": "healthy", "operation": "outbox_send"})
        graph_state = captured["graph_state"]
        self.assertEqual("healthy", graph_state["status"])
        self.assertIn("outbox_send", [op["operation"] for op in graph_state["operations"]])

    def test_escalation_off_restores_legacy_send_invisibility(self):
        """Rollback escape hatch: flag OFF → broken send raises through (legacy)."""
        with self.assertRaises(BrokenSendError):
            _run_refresh(
                send_side_effect=BrokenSendError("boom"),
                env={"SITESIFT_SEND_HEALTH_ESCALATION": "0"},
            )

    def test_escalation_off_ignores_returned_error_state(self):
        """Flag OFF → a returned error op-state is NOT consumed (legacy behavior)."""
        captured = _run_refresh(
            send={"status": "error", "operation": "outbox_send", "error": "x"},
            env={"SITESIFT_SEND_HEALTH_ESCALATION": "0"},
        )
        self.assertEqual("healthy", captured["graph_state"]["status"])


if __name__ == "__main__":
    unittest.main()
