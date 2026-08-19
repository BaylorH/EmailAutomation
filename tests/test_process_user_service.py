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


class BidirectionalServiceFenceTests(unittest.TestCase):
    """One image, two services, and neither may run the other's routes.

    The certification twin runs the EXACT immutable candidate image, which is
    the whole point -- proving a different build proves nothing about the one
    being shipped. But the same image therefore contains `/process-user` and
    `/process-outbox`, and on the twin those routes must be unreachable rather
    than merely unused. The fence runs BEFORE the body is parsed and before any
    provider client is constructed, because a route that validates first has
    already touched the thing it was supposed to refuse.
    """

    ORDINARY = "process-user"
    CERTIFICATION = "process-user-certification"

    def setUp(self):
        service.app.config["TESTING"] = True
        self.client = service.app.test_client()

    def _as(self, k_service):
        return patch.dict(os.environ, {"K_SERVICE": k_service}, clear=False)

    # -- ordinary routes are inert on the certification twin ---------------

    def test_process_user_is_inert_on_the_certification_service(self):
        with self._as(self.CERTIFICATION), \
                patch("service.refresh_and_process_user") as pipeline, \
                patch("service.run_with_user_lease") as lease:
            response = self.client.post("/process-user", json={"uid": "real-user"})
        self.assertEqual(response.status_code, 404)
        pipeline.assert_not_called()
        lease.assert_not_called()

    def test_process_outbox_is_inert_on_the_certification_service(self):
        with self._as(self.CERTIFICATION), \
                patch("service.process_outbox_item_entry") as entry, \
                patch("service.run_with_user_lease") as lease:
            response = self.client.post(
                "/process-outbox", json={"uid": "real-user", "outboxId": "outbox-1"}
            )
        self.assertEqual(response.status_code, 404)
        entry.assert_not_called()
        lease.assert_not_called()

    def test_the_twin_refuses_the_ordinary_route_without_reading_the_body(self):
        """A malformed body must not change the answer.

        If the fence ran after parsing, a bad body would return 400 and a good
        body 404 -- and that difference is a probe telling a caller which
        service it reached.
        """
        with self._as(self.CERTIFICATION):
            malformed = self.client.post("/process-user", data="{{not json")
            wellformed = self.client.post("/process-user", json={"uid": "real-user"})
        self.assertEqual(malformed.status_code, 404)
        self.assertEqual(wellformed.status_code, 404)
        self.assertEqual(malformed.get_json(), wellformed.get_json())

    # -- certification routes are inert on ordinary production -------------

    def test_every_certification_route_is_inert_on_ordinary_process_user(self):
        operations = ("prepare", "run", "status", "review-input",
                      "review", "abort", "recover", "cleanup")
        with self._as(self.ORDINARY):
            for operation in operations:
                with self.subTest(operation=operation):
                    response = self.client.post(
                        f"/certification/{operation}",
                        json={"scenarioId": "campaign-one-property",
                              "runId": "r-1", "expectedRevision": "x"},
                    )
                    self.assertEqual(response.status_code, 404)

    def test_certification_routes_are_reachable_on_the_twin(self):
        """Guards against a fence that passes by disabling both directions."""
        with self._as(self.CERTIFICATION):
            response = self.client.post("/certification/status", json={})
        self.assertNotEqual(response.status_code, 404)

    # -- health is the only shared route -----------------------------------

    def test_health_answers_on_both_services(self):
        for k_service in (self.ORDINARY, self.CERTIFICATION):
            with self.subTest(service=k_service), self._as(k_service):
                self.assertEqual(self.client.get("/health").status_code, 200)
                self.assertEqual(self.client.get("/healthz").status_code, 200)

    # -- an unknown identity is not a free pass ----------------------------

    def test_an_unrecognised_k_service_refuses_both_families(self):
        """Fail closed. An unknown service name means the deployment changed in
        a way this fence cannot reason about, and guessing either way is worse
        than refusing both."""
        with self._as("some-other-service"), \
                patch("service.refresh_and_process_user") as pipeline:
            ordinary = self.client.post("/process-user", json={"uid": "real-user"})
            certification = self.client.post("/certification/status", json={})
        self.assertEqual(ordinary.status_code, 404)
        self.assertEqual(certification.status_code, 404)
        pipeline.assert_not_called()


class CertificationRevisionBindingTests(unittest.TestCase):
    """A certification route may only answer for the revision it is running.

    A stamp binds a verdict to an exact source and image. If a route would serve
    a request naming some other revision, the resulting stamp would certify code
    that never executed -- which is worse than no stamp, because it reads as
    proof.
    """

    REVISION = "1a20ba44a46e0aeed7620a6408856c0aacf6c7d9"
    IMAGE = "sha256:" + "b" * 64

    def setUp(self):
        service.app.config["TESTING"] = True
        self.client = service.app.test_client()

    def _env(self, **overrides):
        env = {
            "K_SERVICE": "process-user-certification",
            "SITESIFT_SOURCE_REVISION": self.REVISION,
            "SITESIFT_IMAGE_DIGEST": self.IMAGE,
        }
        env.update(overrides)
        return patch.dict(os.environ, env, clear=False)

    def _post(self, operation="status", **body):
        payload = {"runId": "cert-route-0001", "expectedRevision": self.REVISION}
        payload.update(body)
        return self.client.post(f"/certification/{operation}", json=payload)

    def test_a_matching_revision_is_accepted(self):
        with self._env():
            response = self._post()
        self.assertNotIn(response.status_code, (400, 409, 503))

    def test_a_mismatched_expected_revision_is_refused(self):
        """A stamp may only bind the revision it actually executed against."""
        with self._env():
            response = self._post(expectedRevision="0" * 40)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["reason"], "revision_mismatch")

    def test_a_missing_source_revision_fails_closed(self):
        with self._env(SITESIFT_SOURCE_REVISION=""):
            response = self._post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["reason"], "revision_binding_unavailable")

    def test_a_missing_image_digest_fails_closed(self):
        with self._env(SITESIFT_IMAGE_DIGEST=""):
            response = self._post()
        self.assertEqual(response.status_code, 503)

    def test_a_non_canonical_revision_or_digest_is_refused(self):
        """Forged or truncated values reject rather than degrade."""
        for override in ({"SITESIFT_SOURCE_REVISION": "1a20ba44"},          # abbreviated
                         {"SITESIFT_SOURCE_REVISION": self.REVISION.upper()},
                         {"SITESIFT_IMAGE_DIGEST": "b" * 64},               # no algorithm
                         {"SITESIFT_IMAGE_DIGEST": "sha256:xyz"}):
            with self.subTest(**override), self._env(**override):
                response = self._post()
            self.assertEqual(response.status_code, 503, override)

    def test_the_binding_is_checked_before_the_body_schema(self):
        """An unavailable binding must not be reported as a bad request; the two
        are different failures and a caller has to be able to tell them apart."""
        with self._env(SITESIFT_SOURCE_REVISION=""):
            response = self.client.post("/certification/status", json={"nonsense": True})
        self.assertEqual(response.status_code, 503)
