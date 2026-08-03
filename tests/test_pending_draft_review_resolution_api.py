import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")

import app as appmod


CALLER = "authenticated-operator-uid"
VICTIM = "other-user"
ROUTE = "/api/pending-draft-review/resolve"


def _valid_payload():
    return {
        "action": "confirm_retained_draft_not_actionable",
        "threadId": "thread-1",
        "expectedPermitId": "graph-send-permit-1",
        "expectedPermitHash": "a" * 64,
        "expectedReviewEvidenceHash": "b" * 64,
        "operatorReason": "The retained provider draft was manually discarded.",
        "settlementId": "draft-review-settlement-1",
        "user_id": VICTIM,
        "operatorId": VICTIM,
    }


class PendingDraftReviewResolutionApiTests(unittest.TestCase):
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

    def test_missing_invalid_and_revoked_auth_are_rejected_before_resolution(self):
        resolve = MagicMock()
        with patch(
            "email_automation.pending_responses.resolve_pending_graph_draft_review",
            resolve,
            create=True,
        ):
            response = self.client.post(ROUTE, json=_valid_payload())
            self.assertEqual(401, response.status_code)
            self.verify.side_effect = ValueError("invalid or revoked")
            response = self.client.post(
                ROUTE,
                json=_valid_payload(),
                headers=self.auth,
            )
            self.assertEqual(401, response.status_code)
        resolve.assert_not_called()

    def test_exact_success_uses_verified_uid_and_has_no_provider_effect(self):
        resolve = MagicMock(return_value="settled_draft_review_resolved")
        with patch(
            "email_automation.pending_responses.resolve_pending_graph_draft_review",
            resolve,
            create=True,
        ), patch.object(
            appmod,
            "_graph_headers_for_user",
        ) as graph_headers, patch(
            "email_automation.processing.send_reply_in_thread",
        ) as send, patch(
            "email_automation.email._delete_graph_reply_draft",
        ) as delete_draft, patch(
            "email_automation.pending_responses.queue_pending_response",
        ) as requeue:
            response = self.client.post(
                ROUTE,
                json=_valid_payload(),
                headers=self.auth,
            )

        self.assertEqual(200, response.status_code, response.get_data(as_text=True))
        self.assertEqual(
            "settled_draft_review_resolved",
            response.get_json()["status"],
        )
        self.verify.assert_called_once_with("test-token", check_revoked=True)
        self.assertEqual(CALLER, resolve.call_args.args[0])
        self.assertEqual(CALLER, resolve.call_args.kwargs["operator_id"])
        self.assertNotEqual(VICTIM, resolve.call_args.args[0])
        graph_headers.assert_not_called()
        send.assert_not_called()
        delete_draft.assert_not_called()
        requeue.assert_not_called()

    def test_malformed_contracts_fail_before_resolution(self):
        cases = {
            "wrong_action": {"action": "delete_draft"},
            "path_thread": {"threadId": "victim/thread"},
            "path_settlement": {"settlementId": "../settlement"},
            "bad_permit_id": {"expectedPermitId": "permit-1"},
            "short_permit_hash": {"expectedPermitHash": "a" * 63},
            "nonhex_review_hash": {"expectedReviewEvidenceHash": "z" * 64},
            "oversize_reason": {"operatorReason": "x" * 1501},
            "oversize_thread": {"threadId": "t" * 201},
        }
        for label, drift in cases.items():
            with self.subTest(case=label):
                payload = _valid_payload()
                payload.update(drift)
                resolve = MagicMock()
                with patch(
                    "email_automation.pending_responses.resolve_pending_graph_draft_review",
                    resolve,
                    create=True,
                ):
                    response = self.client.post(
                        ROUTE,
                        json=payload,
                        headers=self.auth,
                    )
                self.assertEqual(400, response.status_code)
                resolve.assert_not_called()

        response = self.client.post(
            ROUTE,
            data="[]",
            content_type="application/json",
            headers=self.auth,
        )
        self.assertEqual(400, response.status_code)

    def test_cas_failure_returns_sanitized_conflict(self):
        secret = "victim@example.test graph-send-secret"
        resolve = MagicMock(side_effect=RuntimeError(secret))
        with patch(
            "email_automation.pending_responses.resolve_pending_graph_draft_review",
            resolve,
            create=True,
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
            "Exact pending draft-review resolution was refused",
            response.get_json()["error"],
        )


if __name__ == "__main__":
    unittest.main()
