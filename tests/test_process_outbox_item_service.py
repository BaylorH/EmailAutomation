"""Reviewed-lane contract for exact outbox-item processing.

These tests are credential-free.  They permit one exact item delegation and
deliberately make every broad per-user pipeline entrypoint fatal if invoked.
"""

from __future__ import annotations

from contextlib import ExitStack
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "service-account.json",
    ),
)

with patch("google.cloud.firestore.Client", return_value=MagicMock()):
    import main
    import service
    from email_automation import email as email_module
    from email_automation import manual_reply


def _lease_runs(uid, callback, **_kwargs):
    callback()
    return True


def _lease_locked(uid, callback, **_kwargs):
    return False


class ProcessOutboxReviewedRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = service.app.test_client()
        os.environ.pop("PROCESS_USER_AUTH", None)

    def tearDown(self):
        os.environ.pop("PROCESS_USER_AUTH", None)

    def _post(self):
        return self.client.post(
            "/process-outbox",
            json={"uid": "user-123", "outboxId": "outbox-456"},
        )

    def test_reviewed_results_have_exact_http_codes_and_bounded_envelopes(self):
        cases = (
            ({"status": "processed", "reason": "sent"}, 200),
            (
                {"status": "terminal_no_effect", "reason": "not_found"},
                200,
            ),
            (
                {"status": "manual_review", "reason": "send_lane_pending"},
                409,
            ),
            ({"status": "invalid", "reason": "invalid_request"}, 400),
        )

        for result, expected_code in cases:
            downstream = {
                **result,
                "uid": "must-not-escape",
                "outboxId": "must-not-escape",
                "rawState": {"must": "not escape"},
            }
            with self.subTest(status=result["status"]), patch.object(
                service, "run_with_user_lease", side_effect=_lease_runs
            ), patch.object(
                service,
                "process_outbox_item_entry",
                return_value=downstream,
            ):
                response = self._post()

            self.assertEqual(expected_code, response.status_code)
            self.assertEqual(result, response.get_json())

    def test_unknown_status_or_reason_is_retryable_and_never_leaks(self):
        malformed = (
            {"status": "unknown", "reason": "PRIVATE-STATE"},
            {"status": "processed", "reason": "PRIVATE-STATE"},
            {"status": "manual_review"},
        )

        for result in malformed:
            with self.subTest(result=result), patch.object(
                service, "run_with_user_lease", side_effect=_lease_runs
            ), patch.object(
                service,
                "process_outbox_item_entry",
                return_value=result,
            ):
                response = self._post()

            self.assertEqual(500, response.status_code)
            self.assertEqual(
                {"status": "error", "reason": "processing_failed"},
                response.get_json(),
            )
            self.assertNotIn("PRIVATE-STATE", response.get_data(as_text=True))

    def test_legacy_generic_ownership_status_remains_status_only(self):
        downstream = {
            "status": "blocked_generic_owned",
            "uid": "must-not-escape",
            "outboxId": "must-not-escape",
            "private": {"must": "not escape"},
        }
        with patch.object(
            service, "run_with_user_lease", side_effect=_lease_runs
        ), patch.object(
            service,
            "process_outbox_item_entry",
            return_value=downstream,
        ):
            response = self._post()

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {"status": "blocked_generic_owned"},
            response.get_json(),
        )

    def test_exact_identity_runs_under_the_existing_per_user_lease(self):
        seen = {}

        def lease(uid, callback, **_kwargs):
            seen["uid"] = uid
            seen["result"] = callback()
            return True

        result = {"status": "manual_review", "reason": "send_lane_pending"}
        with patch.object(service, "run_with_user_lease", side_effect=lease), patch.object(
            service,
            "process_outbox_item_entry",
            return_value=result,
        ) as process_exact:
            response = self._post()

        self.assertEqual(409, response.status_code)
        self.assertEqual("user-123", seen["uid"])
        self.assertEqual(result, seen["result"])
        process_exact.assert_called_once_with("user-123", "outbox-456")

    def test_locked_user_is_retryable_without_exact_item_processing(self):
        with patch.object(
            service, "run_with_user_lease", side_effect=_lease_locked
        ), patch.object(service, "process_outbox_item_entry") as process_exact:
            response = self._post()

        self.assertEqual(503, response.status_code)
        self.assertEqual({"status": "skipped_locked"}, response.get_json())
        process_exact.assert_not_called()

    def test_existing_service_auth_is_required_when_configured(self):
        with patch.dict(os.environ, {"PROCESS_USER_AUTH": "synthetic-secret"}), patch.object(
            service, "run_with_user_lease", side_effect=_lease_runs
        ), patch.object(
            service,
            "process_outbox_item_entry",
            return_value={
                "status": "manual_review",
                "reason": "send_lane_pending",
            },
        ) as process_exact:
            missing = self._post()
            wrong = self.client.post(
                "/process-outbox",
                json={"uid": "user-123", "outboxId": "outbox-456"},
                headers={"Authorization": "Bearer wrong"},
            )
            allowed = self.client.post(
                "/process-outbox",
                json={"uid": "user-123", "outboxId": "outbox-456"},
                headers={"Authorization": "Bearer synthetic-secret"},
            )

        self.assertEqual(401, missing.status_code)
        self.assertEqual(401, wrong.status_code)
        self.assertEqual(409, allowed.status_code)
        process_exact.assert_called_once_with("user-123", "outbox-456")

    def test_body_is_exact_and_ids_are_canonical_before_the_lease(self):
        invalid_requests = (
            {"json": {"uid": "user-123"}},
            {"json": {"outboxId": "outbox-456"}},
            {
                "json": {
                    "uid": "user-123",
                    "outboxId": "outbox-456",
                    "extra": True,
                }
            },
            {"json": {"uid": " user-123", "outboxId": "outbox-456"}},
            {"json": {"uid": "user-123", "outboxId": "outbox-456 "}},
            {"json": {"uid": "user/123", "outboxId": "outbox-456"}},
            {"json": {"uid": "user-123", "outboxId": "outbox/456"}},
            {"json": {"uid": "__user__", "outboxId": "outbox-456"}},
            {"json": {"uid": "user-123", "outboxId": "__outbox__"}},
            {"json": {"uid": "user-123", "outboxId": "\ufeffoutbox-456"}},
            {"json": {"uid": "user-123", "outboxId": ""}},
            {"json": {"uid": "user-123", "outboxId": 7}},
            {"data": "not-json", "content_type": "text/plain"},
        )

        with patch.object(service, "run_with_user_lease") as lease, patch.object(
            service, "process_outbox_item_entry"
        ) as process_exact:
            for request_kwargs in invalid_requests:
                with self.subTest(request_kwargs=request_kwargs):
                    response = self.client.post(
                        "/process-outbox", **request_kwargs
                    )
                    self.assertEqual(400, response.status_code)
                    self.assertEqual(
                        {"status": "error", "reason": "invalid_request"},
                        response.get_json(),
                    )

        lease.assert_not_called()
        process_exact.assert_not_called()

    def test_runner_exception_is_retryable_without_exception_disclosure(self):
        def run_and_raise(uid, callback, **_kwargs):
            callback()
            return True

        with patch.object(
            service, "run_with_user_lease", side_effect=run_and_raise
        ), patch.object(
            service,
            "process_outbox_item_entry",
            side_effect=RuntimeError("PRIVATE-EXCEPTION-MARKER"),
        ):
            response = self._post()

        self.assertEqual(500, response.status_code)
        self.assertEqual(
            {"status": "error", "reason": "processing_failed"},
            response.get_json(),
        )
        self.assertNotIn("PRIVATE-EXCEPTION-MARKER", response.get_data(as_text=True))


class ProcessOutboxExactRunnerTests(unittest.TestCase):
    def test_runner_calls_only_manual_reply_and_returns_bounded_result(self):
        forbidden = (
            "refresh_and_process_user",
            "send_outboxes",
            "scan_inbox_against_index",
            "scan_sent_items_for_manual_replies",
            "process_pending_responses",
            "check_and_send_followups",
            "reconcile_stale_processing_failures",
            "retry_processing_failures",
            "download_token",
            "upload_token",
            "list_user_ids",
            "decode_token_payload",
            "ConfidentialClientApplication",
            "SerializableTokenCache",
            "_run_graph_send_operation",
            "auto_cleanup_firestore",
            "record_user_health",
            "run_all_users",
            "run_with_scheduler_lease",
            "resolve_scheduler_user_ids",
            "process_exact_outbox_item",
        )
        downstream = {
            "status": "manual_review",
            "reason": "send_lane_pending",
            "uid": "must-not-escape",
            "outboxId": "must-not-escape",
            "rawState": "must-not-escape",
        }

        with ExitStack() as stack:
            blocked = {
                name: stack.enter_context(
                    patch.object(
                        main,
                        name,
                        side_effect=AssertionError(f"broad pipeline called: {name}"),
                        create=True,
                    )
                )
                for name in forbidden
            }
            process_manual = stack.enter_context(
                patch.object(
                    main,
                    "process_manual_reply_item",
                    return_value=downstream,
                    create=True,
                )
            )
            result = main.process_outbox_item("user-123", "outbox-456")

        self.assertEqual(
            {"status": "manual_review", "reason": "send_lane_pending"},
            result,
        )
        process_manual.assert_called_once_with("user-123", "outbox-456")
        for function in blocked.values():
            function.assert_not_called()

    def test_runner_rejects_unbounded_manual_reply_result(self):
        with patch.object(
            main,
            "process_manual_reply_item",
            return_value={"status": "processed", "reason": "raw-private-state"},
            create=True,
        ):
            with self.assertRaises(ValueError):
                main.process_outbox_item("user-123", "outbox-456")


class ManualReplyContinuationTests(unittest.TestCase):
    def _processor(self):
        processor = getattr(manual_reply, "process_outbox_item", None)
        self.assertTrue(
            callable(processor),
            "email_automation.manual_reply.process_outbox_item is missing",
        )
        return processor

    def test_manual_ready_stays_fail_closed_until_delivery_task(self):
        with patch.object(
            email_module,
            "process_outbox_item",
            return_value={"status": "manual_ready", "private": "must-not-escape"},
        ) as classify, patch.object(
            manual_reply,
            "prepare_manual_reply_item",
            side_effect=AssertionError("Task9A preparation must remain unwired"),
            create=True,
        ) as prepare, patch.object(
            manual_reply,
            "send_prepared_manual_reply_once",
            side_effect=AssertionError("Task9A transport must remain unwired"),
            create=True,
        ) as transport:
            result = self._processor()("user-123", "outbox-456")

        self.assertEqual(
            {"status": "manual_review", "reason": "send_lane_pending"},
            result,
        )
        classify.assert_called_once_with("user-123", "outbox-456")
        prepare.assert_not_called()
        transport.assert_not_called()

    def test_terminal_task1_results_have_no_effect(self):
        processor = self._processor()
        for task1_status in ("cancelled", "not_found"):
            with self.subTest(task1_status=task1_status), patch.object(
                email_module,
                "process_outbox_item",
                return_value={"status": task1_status},
            ):
                result = processor("user-123", "outbox-456")

            self.assertEqual(
                {"status": "terminal_no_effect", "reason": task1_status},
                result,
            )

    def test_blocked_task1_result_requires_review_without_raw_state(self):
        with patch.object(
            email_module,
            "process_outbox_item",
            return_value={
                "status": "blocked_missing_action_audit",
                "rawAudit": "must-not-escape",
            },
        ):
            result = self._processor()("user-123", "outbox-456")

        self.assertEqual(
            {"status": "manual_review", "reason": "item_not_sendable"},
            result,
        )

    def test_direct_invalid_identity_does_not_read_an_item(self):
        invalid_pairs = (
            (" user-123", "outbox-456"),
            ("user-123", "outbox/456"),
            ("__user__", "outbox-456"),
            ("user-123", "\ufeffoutbox-456"),
        )
        with patch.object(email_module, "process_outbox_item") as classify:
            for uid, outbox_id in invalid_pairs:
                with self.subTest(uid=uid, outbox_id=outbox_id):
                    result = self._processor()(uid, outbox_id)
                    self.assertEqual(
                        {"status": "invalid", "reason": "invalid_request"},
                        result,
                    )

        classify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
