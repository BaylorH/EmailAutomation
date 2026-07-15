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


READY_MAILBOX = type("MailboxReadiness", (), {"ready": True, "reason": "ready"})()


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
        self._readiness = patch.object(
            service,
            "read_worker_mailbox_readiness",
            return_value=READY_MAILBOX,
        )
        self._readiness.start()
        self.addCleanup(self._readiness.stop)
        # Auth disabled by default (env unset) unless a test opts in.
        os.environ.pop("PROCESS_USER_AUTH", None)

    def test_healthz_ok(self):
        resp = self.client.get("/healthz")
        self.assertEqual(200, resp.status_code)

    def test_cloud_run_safe_health_alias_ok(self):
        resp = self.client.get("/health")
        self.assertEqual(200, resp.status_code)

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

    def test_unready_mailbox_never_enters_the_pipeline_and_is_acknowledged(self):
        unready = type(
            "MailboxReadiness",
            (),
            {"ready": False, "reason": "mailbox_not_ready"},
        )()
        with patch.object(service, "read_worker_mailbox_readiness", return_value=unready), \
                patch.object(service, "run_with_user_lease", side_effect=_lease_runs), \
                patch.object(service, "refresh_and_process_user") as refresh:
            resp = self.client.post("/process-user", json={"uid": "user-123"})

        self.assertEqual(200, resp.status_code)
        self.assertEqual("blocked_mailbox_not_ready", resp.get_json()["status"])
        refresh.assert_not_called()

    def test_queued_task_rechecks_mailbox_after_lease_acquisition_before_pipeline(self):
        events = []
        unready = type(
            "MailboxReadiness",
            (),
            {"ready": False, "reason": "mailbox_not_ready"},
        )()

        def acquire_then_run(uid, fn, **kwargs):
            events.append("lease_acquired")
            fn()
            return True

        def read_after_enqueue(firestore_client, uid):
            events.append("mailbox_rechecked")
            return unready

        with patch.object(service, "run_with_user_lease", side_effect=acquire_then_run), \
                patch.object(service, "read_worker_mailbox_readiness", side_effect=read_after_enqueue), \
                patch.object(service, "refresh_and_process_user") as refresh:
            resp = self.client.post("/process-user", json={"uid": "user-123"})

        self.assertEqual(200, resp.status_code)
        self.assertEqual("blocked_mailbox_not_ready", resp.get_json()["status"])
        self.assertEqual(["lease_acquired", "mailbox_rechecked"], events)
        refresh.assert_not_called()

    def test_mailbox_read_failure_is_retryable_and_never_enters_the_pipeline(self):
        unavailable = type(
            "MailboxReadiness",
            (),
            {"ready": False, "reason": "mailbox_readiness_unavailable"},
        )()
        with patch.object(service, "read_worker_mailbox_readiness", return_value=unavailable), \
                patch.object(service, "run_with_user_lease", side_effect=_lease_runs), \
                patch.object(service, "refresh_and_process_user") as refresh:
            resp = self.client.post("/process-user", json={"uid": "user-123"})

        self.assertEqual(503, resp.status_code)
        self.assertEqual("mailbox_readiness_unavailable", resp.get_json()["status"])
        refresh.assert_not_called()

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
        self.assertEqual("processing_failed", body["error"])
        self.assertNotIn("graph exploded", resp.get_data(as_text=True))


class ProcessUserAuthTests(unittest.TestCase):
    def setUp(self):
        self.client = service.app.test_client()
        self._readiness = patch.object(
            service,
            "read_worker_mailbox_readiness",
            return_value=READY_MAILBOX,
        )
        self._readiness.start()
        self.addCleanup(self._readiness.stop)

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


if __name__ == "__main__":
    unittest.main()
