"""HTTP contract for the /process-user webhook service (Phase-1 migration).

service.py wraps the existing per-user pipeline (main.refresh_and_process_user)
as an HTTP endpoint so a queue (Cloud Tasks) can drive one user per request,
guarded by the per-user Firestore lease (run_with_user_lease). This is
FUNCTIONALITY-NEUTRAL: the endpoint reuses refresh_and_process_user unchanged.

Contract pinned here:
  * POST /process-user {"uid": ...}  → 200 {"status":"processed"} and the
    pipeline runs under the per-user lease,
  * a locked user            → 503 {"status":"skipped_locked"} (pipeline NOT run,
    so Cloud Tasks retries after the active worker releases the lease),
  * missing/blank uid        → 400,
  * downstream exception     → 500 (so Cloud Tasks retries),
  * GET /health and /healthz → 200,
  * shared-secret auth gate  → 401 when PROCESS_USER_AUTH is set and the secret
    is missing/wrong; open when the env var is unset.

The pipeline and lease are mocked — no Graph calls, no Firestore, no email.
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import service


def _lease_runs(uid, fn, **kwargs):
    """Fake run_with_user_lease that acquires: run the callback, report processed."""
    fn()
    return True


def _lease_locked(uid, fn, **kwargs):
    """Fake run_with_user_lease that is locked: skip the callback."""
    return False


class ProcessUserServiceTests(unittest.TestCase):
    def setUp(self):
        self.client = service.app.test_client()
        # Auth disabled by default (env unset) unless a test opts in.
        os.environ.pop("PROCESS_USER_AUTH", None)

    def test_healthz_keeps_exact_legacy_body(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("K_SERVICE", None)
            os.environ.pop("K_REVISION", None)
            resp = self.client.get("/healthz")

        self.assertEqual(200, resp.status_code)
        self.assertEqual({"status": "ok"}, resp.get_json())

    def test_cloud_run_safe_health_alias_keeps_exact_legacy_body(self):
        with patch.dict(
            os.environ,
            {
                "K_SERVICE": "process-user",
                "K_REVISION": "process-user-stage-1234567890ab",
            },
        ):
            resp = self.client.get("/health")

        self.assertEqual(200, resp.status_code)
        self.assertEqual({"status": "ok"}, resp.get_json())

    def test_unapproved_identity_health_route_is_absent(self):
        with patch.dict(
            os.environ,
            {
                "K_SERVICE": "process-user",
                "K_REVISION": "process-user-stage-1234567890ab",
            },
        ):
            resp = self.client.get("/health/identity/v1")

        self.assertEqual(404, resp.status_code)

    def test_process_user_runs_pipeline_under_lease(self):
        with patch.object(service, "run_with_user_lease", side_effect=_lease_runs), \
                patch.object(service, "refresh_and_process_user") as refresh:
            resp = self.client.post("/process-user", json={"uid": "user-123"})

        self.assertEqual(200, resp.status_code)
        self.assertEqual("processed", resp.get_json()["status"])
        refresh.assert_called_once_with("user-123")

    def test_process_user_lease_wraps_refresh(self):
        """The endpoint must run refresh_and_process_user THROUGH the lease,
        not call it directly — pin that run_with_user_lease is invoked with the
        uid and a callable that triggers refresh_and_process_user(uid)."""
        seen = {}

        def capture(uid, fn, **kwargs):
            seen["uid"] = uid
            fn()
            return True

        with patch.object(service, "run_with_user_lease", side_effect=capture), \
                patch.object(service, "refresh_and_process_user") as refresh:
            resp = self.client.post("/process-user", json={"uid": "abc"})

        self.assertEqual(200, resp.status_code)
        self.assertEqual("abc", seen["uid"])
        refresh.assert_called_once_with("abc")

    def test_locked_user_returns_retryable_status(self):
        with patch.object(service, "run_with_user_lease", side_effect=_lease_locked), \
                patch.object(service, "refresh_and_process_user") as refresh:
            resp = self.client.post("/process-user", json={"uid": "user-123"})

        self.assertEqual(503, resp.status_code)
        self.assertEqual("skipped_locked", resp.get_json()["status"])
        refresh.assert_not_called()

    def test_locked_delivery_then_redelivery_processes_exactly_once(self):
        attempts = {"count": 0}

        def acquire_on_redelivery(uid, fn, **kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                return False
            fn()
            return True

        with patch.object(service, "run_with_user_lease", side_effect=acquire_on_redelivery), \
                patch.object(service, "refresh_and_process_user") as refresh:
            first = self.client.post("/process-user", json={"uid": "user-123"})
            second = self.client.post("/process-user", json={"uid": "user-123"})

        self.assertEqual(503, first.status_code)
        self.assertEqual("skipped_locked", first.get_json()["status"])
        self.assertEqual(200, second.status_code)
        self.assertEqual("processed", second.get_json()["status"])
        refresh.assert_called_once_with("user-123")

    def test_missing_uid_returns_400(self):
        with patch.object(service, "run_with_user_lease", side_effect=_lease_runs), \
                patch.object(service, "refresh_and_process_user") as refresh:
            resp = self.client.post("/process-user", json={})

        self.assertEqual(400, resp.status_code)
        refresh.assert_not_called()

    def test_blank_uid_returns_400(self):
        with patch.object(service, "run_with_user_lease", side_effect=_lease_runs):
            resp = self.client.post("/process-user", json={"uid": "   "})
        self.assertEqual(400, resp.status_code)

    def test_no_json_body_returns_400(self):
        resp = self.client.post("/process-user", data="not json",
                                content_type="text/plain")
        self.assertEqual(400, resp.status_code)

    def test_downstream_exception_returns_500(self):
        def boom(uid, fn, **kwargs):
            fn()
            return True

        with patch.object(service, "run_with_user_lease", side_effect=boom), \
                patch.object(service, "refresh_and_process_user",
                             side_effect=RuntimeError("graph exploded")):
            resp = self.client.post("/process-user", json={"uid": "user-123"})

        self.assertEqual(500, resp.status_code)
        body = resp.get_json()
        self.assertEqual("error", body["status"])
        self.assertIn("graph exploded", body["error"])


class ProcessOutboxServiceTests(unittest.TestCase):
    TASK1_STATUSES = frozenset({
        "manual_ready",
        "cancelled",
        "not_found",
        "blocked_state_changed",
        "blocked_non_manual",
        "blocked_invalid_client",
        "blocked_invalid_thread",
        "blocked_invalid_notification",
        "blocked_invalid_action_audit",
        "blocked_missing_action_audit",
        "blocked_audit_status",
        "blocked_audit_actor",
        "blocked_audit_source",
        "blocked_audit_action_type",
        "blocked_audit_client",
        "blocked_audit_thread",
        "blocked_audit_notification",
        "blocked_audit_outbox",
    })

    def setUp(self):
        self.client = service.app.test_client()
        os.environ.pop("PROCESS_USER_AUTH", None)

    def tearDown(self):
        os.environ.pop("PROCESS_USER_AUTH", None)

    def test_process_outbox_runs_exact_item_under_same_user_lease(self):
        seen = {}

        def lease(uid, fn, **kwargs):
            seen["uid"] = uid
            seen["result"] = fn()
            return True

        downstream = {
            "status": "manual_ready",
            "uid": "must-not-escape",
            "outboxId": "must-not-escape",
            "internal": {"must": "not escape"},
        }
        with patch.object(service, "run_with_user_lease", side_effect=lease), \
                patch.object(
                    service,
                    "process_outbox_item_entry",
                    return_value=downstream,
                    create=True,
                ) as process_exact:
            resp = self.client.post(
                "/process-outbox",
                json={"uid": "user-123", "outboxId": "outbox-456"},
            )

        self.assertEqual(200, resp.status_code)
        self.assertEqual({"status": "manual_ready"}, resp.get_json())
        self.assertEqual("user-123", seen["uid"])
        self.assertEqual(downstream, seen["result"])
        process_exact.assert_called_once_with("user-123", "outbox-456")

    def test_process_outbox_locked_user_is_retryable_and_does_not_process(self):
        with patch.object(service, "run_with_user_lease", side_effect=_lease_locked), \
                patch.object(
                    service,
                    "process_outbox_item_entry",
                    create=True,
                ) as process_exact:
            resp = self.client.post(
                "/process-outbox",
                json={"uid": "user-123", "outboxId": "outbox-456"},
            )

        self.assertEqual(503, resp.status_code)
        self.assertEqual({"status": "skipped_locked"}, resp.get_json())
        process_exact.assert_not_called()

    def test_process_outbox_rejects_extra_body_keys_before_lease(self):
        with patch.object(service, "run_with_user_lease") as lease, \
                patch.object(service, "process_outbox_item_entry") as process_exact:
            resp = self.client.post(
                "/process-outbox",
                json={
                    "uid": "user-123",
                    "outboxId": "outbox-456",
                    "unexpected": "must-not-be-accepted",
                },
            )

        self.assertEqual(400, resp.status_code)
        self.assertEqual(
            {"status": "error", "reason": "invalid_request"},
            resp.get_json(),
        )
        lease.assert_not_called()
        process_exact.assert_not_called()

    def test_process_outbox_rejects_non_object_or_wrong_type_inputs_before_lease(self):
        invalid_requests = [
            (
                "non_json",
                {"data": "not json", "content_type": "text/plain"},
            ),
            (
                "json_list",
                {"json": ["user-123", "outbox-456"]},
            ),
            (
                "wrong_uid_type",
                {"json": {"uid": 123, "outboxId": "outbox-456"}},
            ),
            (
                "wrong_outbox_type",
                {"json": {"uid": "user-123", "outboxId": {"id": "outbox-456"}}},
            ),
        ]

        with patch.object(service, "run_with_user_lease") as lease, \
                patch.object(service, "process_outbox_item_entry") as process_exact:
            for label, request_kwargs in invalid_requests:
                with self.subTest(case=label):
                    resp = self.client.post("/process-outbox", **request_kwargs)
                    self.assertEqual(400, resp.status_code)
                    self.assertEqual(
                        {"status": "error", "reason": "invalid_request"},
                        resp.get_json(),
                    )

        lease.assert_not_called()
        process_exact.assert_not_called()

    def test_process_outbox_rejects_padded_ids_before_lease(self):
        invalid_cases = [
            {"uid": " user-123", "outboxId": "outbox-456"},
            {"uid": "user-123 ", "outboxId": "outbox-456"},
            {"uid": "user-123", "outboxId": " outbox-456"},
            {"uid": "user-123", "outboxId": "outbox-456 "},
        ]

        with patch.object(service, "run_with_user_lease") as lease, \
                patch.object(service, "process_outbox_item_entry") as process_exact:
            for payload in invalid_cases:
                with self.subTest(payload=payload):
                    resp = self.client.post("/process-outbox", json=payload)
                    self.assertEqual(400, resp.status_code)
                    self.assertEqual(
                        {"status": "error", "reason": "invalid_request"},
                        resp.get_json(),
                    )

        lease.assert_not_called()
        process_exact.assert_not_called()

    def test_process_outbox_requires_exact_uid_and_outbox_id(self):
        invalid_bodies = [
            {"outboxId": "outbox-456"},
            {"uid": "user-123"},
            {"uid": "user-123", "outboxId": "   "},
        ]

        with patch.object(service, "run_with_user_lease") as lease, \
                patch.object(service, "process_outbox_item_entry") as process_exact:
            for body in invalid_bodies:
                with self.subTest(body=body):
                    resp = self.client.post("/process-outbox", json=body)
                    self.assertEqual(400, resp.status_code)
                    self.assertEqual(
                        {"status": "error", "reason": "invalid_request"},
                        resp.get_json(),
                    )

        lease.assert_not_called()
        process_exact.assert_not_called()

    def test_process_outbox_rejects_unsafe_or_unbounded_document_ids_before_lease(self):
        invalid_cases = [
            {"uid": "user/other", "outboxId": "outbox-1"},
            {"uid": "user\nother", "outboxId": "outbox-1"},
            {"uid": "u" * 129, "outboxId": "outbox-1"},
            {"uid": "user-1", "outboxId": "folder/outbox-1"},
            {"uid": "user-1", "outboxId": "outbox\x00one"},
            {"uid": "user-1", "outboxId": "o" * 1501},
        ]

        with patch.object(service, "run_with_user_lease") as lease, \
                patch.object(
                    service,
                    "process_outbox_item_entry",
                    create=True,
                ) as process_exact:
            for payload in invalid_cases:
                with self.subTest(payload=payload):
                    resp = self.client.post("/process-outbox", json=payload)
                    self.assertEqual(400, resp.status_code)
                    self.assertEqual(
                        {"status": "error", "reason": "invalid_request"},
                        resp.get_json(),
                    )

        lease.assert_not_called()
        process_exact.assert_not_called()

    def test_process_outbox_returns_every_task1_status_as_status_only(self):
        for status in self.TASK1_STATUSES:
            with self.subTest(status=status), \
                    patch.object(service, "run_with_user_lease", side_effect=_lease_runs), \
                    patch.object(
                        service,
                        "process_outbox_item_entry",
                        return_value={
                            "status": status,
                            "uid": "must-not-escape",
                            "outboxId": "must-not-escape",
                            "arbitrary": ["must-not-escape"],
                        },
                    ):
                resp = self.client.post(
                    "/process-outbox",
                    json={"uid": "user-123", "outboxId": "outbox-456"},
                )

            self.assertEqual(200, resp.status_code)
            self.assertEqual({"status": status}, resp.get_json())

    def test_process_outbox_rejects_unknown_or_malformed_downstream_results(self):
        malformed_results = [
            None,
            "manual_ready",
            {},
            {"status": None},
            {"status": "unknown_status", "private": "must-not-escape"},
        ]

        for downstream in malformed_results:
            with self.subTest(downstream=downstream), \
                    patch.object(service, "run_with_user_lease", side_effect=_lease_runs), \
                    patch.object(
                        service,
                        "process_outbox_item_entry",
                        return_value=downstream,
                    ):
                resp = self.client.post(
                    "/process-outbox",
                    json={"uid": "user-123", "outboxId": "outbox-456"},
                )

            self.assertEqual(500, resp.status_code)
            self.assertEqual(
                {"status": "error", "reason": "processing_failed"},
                resp.get_json(),
            )

    def test_process_outbox_uses_existing_shared_secret_gate(self):
        with patch.dict(os.environ, {"PROCESS_USER_AUTH": "s3cret"}):
            with patch.object(service, "run_with_user_lease", side_effect=_lease_runs), \
                    patch.object(
                        service,
                        "process_outbox_item_entry",
                        return_value={"status": "manual_ready"},
                        create=True,
                    ):
                missing = self.client.post(
                    "/process-outbox",
                    json={"uid": "user-123", "outboxId": "outbox-456"},
                )
                allowed = self.client.post(
                    "/process-outbox",
                    json={"uid": "user-123", "outboxId": "outbox-456"},
                    headers={"Authorization": "Bearer s3cret"},
                )

        self.assertEqual(401, missing.status_code)
        self.assertEqual(
            {"status": "error", "reason": "unauthorized"},
            missing.get_json(),
        )
        self.assertNotEqual(401, allowed.status_code)
        self.assertEqual({"status": "manual_ready"}, allowed.get_json())

    def test_process_outbox_downstream_exception_returns_retryable_500(self):
        def lease(uid, fn, **kwargs):
            fn()
            return True

        with patch.object(service, "run_with_user_lease", side_effect=lease), \
                patch.object(
                    service,
                    "process_outbox_item_entry",
                    side_effect=RuntimeError("PRIVATE-EXCEPTION-MARKER"),
                    create=True,
                ):
            resp = self.client.post(
                "/process-outbox",
                json={"uid": "user-123", "outboxId": "outbox-456"},
            )

        self.assertEqual(500, resp.status_code)
        self.assertEqual(
            {"status": "error", "reason": "processing_failed"},
            resp.get_json(),
        )
        self.assertNotIn("PRIVATE-EXCEPTION-MARKER", resp.get_data(as_text=True))


class ProcessUserAuthTests(unittest.TestCase):
    def setUp(self):
        self.client = service.app.test_client()

    def tearDown(self):
        os.environ.pop("PROCESS_USER_AUTH", None)

    def test_auth_open_when_env_unset(self):
        os.environ.pop("PROCESS_USER_AUTH", None)
        with patch.object(service, "run_with_user_lease", side_effect=_lease_runs), \
                patch.object(service, "refresh_and_process_user"):
            resp = self.client.post("/process-user", json={"uid": "u"})
        self.assertEqual(200, resp.status_code)

    def test_missing_secret_returns_401_when_required(self):
        with patch.dict(os.environ, {"PROCESS_USER_AUTH": "s3cret"}):
            with patch.object(service, "run_with_user_lease", side_effect=_lease_runs), \
                    patch.object(service, "refresh_and_process_user") as refresh:
                resp = self.client.post("/process-user", json={"uid": "u"})
        self.assertEqual(401, resp.status_code)
        refresh.assert_not_called()

    def test_wrong_secret_returns_401(self):
        with patch.dict(os.environ, {"PROCESS_USER_AUTH": "s3cret"}):
            resp = self.client.post(
                "/process-user", json={"uid": "u"},
                headers={"Authorization": "Bearer nope"},
            )
        self.assertEqual(401, resp.status_code)

    def test_correct_bearer_secret_allows(self):
        with patch.dict(os.environ, {"PROCESS_USER_AUTH": "s3cret"}):
            with patch.object(service, "run_with_user_lease", side_effect=_lease_runs), \
                    patch.object(service, "refresh_and_process_user"):
                resp = self.client.post(
                    "/process-user", json={"uid": "u"},
                    headers={"Authorization": "Bearer s3cret"},
                )
        self.assertEqual(200, resp.status_code)

    def test_correct_shared_secret_header_allows(self):
        with patch.dict(os.environ, {"PROCESS_USER_AUTH": "s3cret"}):
            with patch.object(service, "run_with_user_lease", side_effect=_lease_runs), \
                    patch.object(service, "refresh_and_process_user"):
                resp = self.client.post(
                    "/process-user", json={"uid": "u"},
                    headers={"X-Process-User-Auth": "s3cret"},
                )
        self.assertEqual(200, resp.status_code)

    def test_healthz_open_even_when_auth_required(self):
        with patch.dict(os.environ, {"PROCESS_USER_AUTH": "s3cret"}):
            resp = self.client.get("/healthz")
        self.assertEqual(200, resp.status_code)

    def test_cloud_run_safe_health_alias_open_even_when_auth_required(self):
        with patch.dict(os.environ, {"PROCESS_USER_AUTH": "s3cret"}):
            resp = self.client.get("/health")
        self.assertEqual(200, resp.status_code)

    def test_unapproved_identity_health_stays_absent_when_auth_required(self):
        with patch.dict(os.environ, {"PROCESS_USER_AUTH": "s3cret"}):
            resp = self.client.get("/health/identity/v1")
        self.assertEqual(404, resp.status_code)


if __name__ == "__main__":
    unittest.main()
