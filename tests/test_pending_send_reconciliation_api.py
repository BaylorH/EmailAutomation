import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")

import app as appmod


CALLER = "authenticated-operator-uid"
VICTIM = "other-user"
ROUTE = "/api/pending-send-reconciliation/acknowledge"


def _valid_payload():
    return {
        "action": "acknowledge_ambiguous_no_retry",
        "pendingDocumentId": "pending-1",
        "expectedPermitId": "graph-send-permit-1",
        "expectedPermitHash": "a" * 64,
        "expectedReconciliationEvidenceHash": "b" * 64,
        "operatorReason": "Fresh Sent Items lookup was readable and inconclusive.",
        "settlementId": "operator-settlement-1",
        # An attacker-controlled identity is ignored; Firebase UID is canonical.
        "user_id": VICTIM,
        "operatorId": VICTIM,
    }


class PendingSendReconciliationApiTests(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()
        self.original_available = appmod.SCHEDULER_AVAILABLE
        appmod.SCHEDULER_AVAILABLE = True
        self.addCleanup(
            lambda: setattr(
                appmod,
                "SCHEDULER_AVAILABLE",
                self.original_available,
            )
        )
        self.verify_patcher = patch(
            "firebase_admin.auth.verify_id_token",
            return_value={"uid": CALLER},
        )
        self.verify = self.verify_patcher.start()
        self.addCleanup(self.verify_patcher.stop)
        self.auth = {"Authorization": "Bearer test-token"}

    def test_missing_invalid_and_revoked_auth_are_rejected_before_mailbox_read(self):
        mailbox = MagicMock()
        with patch.object(appmod, "_graph_headers_for_user", mailbox):
            response = self.client.post(ROUTE, json=_valid_payload())
            self.assertEqual(401, response.status_code)
            self.verify.side_effect = ValueError("invalid or revoked")
            response = self.client.post(
                ROUTE,
                json=_valid_payload(),
                headers=self.auth,
            )
            self.assertEqual(401, response.status_code)
        mailbox.assert_not_called()

    def test_exact_success_uses_verified_uid_only_and_has_no_send_or_requeue(self):
        settle = MagicMock(return_value="settled_ambiguous_no_retry")
        send = MagicMock()
        requeue = MagicMock()
        with patch.object(
            appmod,
            "_graph_headers_for_user",
            return_value={"Authorization": "Bearer server-token"},
        ) as mailbox, patch(
            "email_automation.pending_responses.acknowledge_pending_graph_send_ambiguity",
            settle,
        ), patch(
            "email_automation.processing.send_reply_in_thread",
            send,
        ), patch(
            "email_automation.pending_responses.queue_pending_response",
            requeue,
        ):
            response = self.client.post(
                ROUTE,
                json=_valid_payload(),
                headers=self.auth,
            )

        self.assertEqual(200, response.status_code, response.get_data(as_text=True))
        self.assertEqual("settled_ambiguous_no_retry", response.get_json()["status"])
        mailbox.assert_not_called()
        self.verify.assert_called_once_with("test-token", check_revoked=True)
        self.assertEqual(CALLER, settle.call_args.args[0])
        self.assertEqual(CALLER, settle.call_args.kwargs["operator_id"])
        self.assertNotEqual(VICTIM, settle.call_args.args[0])
        self.assertTrue(callable(settle.call_args.kwargs["headers_factory"]))
        mailbox.assert_not_called()
        send.assert_not_called()
        requeue.assert_not_called()

    def test_malformed_contracts_fail_before_token_or_settlement(self):
        cases = {
            "wrong_action": {"action": "requeue"},
            "path_pending": {"pendingDocumentId": "victim/pending"},
            "path_settlement": {"settlementId": "../settlement"},
            "bad_permit_id": {"expectedPermitId": "permit-1"},
            "short_permit_hash": {"expectedPermitHash": "a" * 63},
            "nonhex_evidence_hash": {
                "expectedReconciliationEvidenceHash": "z" * 64
            },
            "oversize_reason": {"operatorReason": "x" * 1501},
            "oversize_document": {"pendingDocumentId": "p" * 201},
        }
        for label, drift in cases.items():
            with self.subTest(case=label):
                payload = _valid_payload()
                payload.update(drift)
                mailbox = MagicMock()
                settle = MagicMock()
                with patch.object(
                    appmod,
                    "_graph_headers_for_user",
                    mailbox,
                ), patch(
                    "email_automation.pending_responses.acknowledge_pending_graph_send_ambiguity",
                    settle,
                ):
                    response = self.client.post(
                        ROUTE,
                        json=payload,
                        headers=self.auth,
                    )
                self.assertEqual(400, response.status_code)
                mailbox.assert_not_called()
                settle.assert_not_called()

        response = self.client.post(
            ROUTE,
            data="[]",
            content_type="application/json",
            headers=self.auth,
        )
        self.assertEqual(400, response.status_code)

    def test_cas_or_mailbox_failure_returns_sanitized_conflict(self):
        secret = "victim@example.test graph-send-secret"
        for boundary in ("mailbox", "cas"):
            with self.subTest(boundary=boundary):
                mailbox = MagicMock(
                    return_value={"Authorization": "Bearer server-token"}
                )
                settle = MagicMock(return_value="settled_ambiguous_no_retry")
                if boundary == "mailbox":
                    mailbox.side_effect = RuntimeError(secret)
                    settle.side_effect = (
                        lambda *_args, **kwargs: kwargs["headers_factory"]()
                    )
                else:
                    settle.side_effect = RuntimeError(secret)
                with patch.object(
                    appmod,
                    "_graph_headers_for_user",
                    mailbox,
                ), patch(
                    "email_automation.pending_responses.acknowledge_pending_graph_send_ambiguity",
                    settle,
                ):
                    response = self.client.post(
                        ROUTE,
                        json=_valid_payload(),
                        headers=self.auth,
                    )
                self.assertEqual(409, response.status_code)
                body = response.get_data(as_text=True)
                self.assertNotIn("victim@example.test", body)
                self.assertNotIn("graph-send-secret", body)
                self.assertEqual(
                    "Exact pending-send reconciliation was refused",
                    response.get_json()["error"],
                )


if __name__ == "__main__":
    unittest.main()
