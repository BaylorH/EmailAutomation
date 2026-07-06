"""
Adversarial frontend-contract fuzz for POST /api/trigger-scheduler.

Route context
-------------
Feature: "Manual scheduler trigger (dev-scoped guard)".
The real frontend caller (email-admin-ui, see git history: AbsoluteNavigation /
NotificationsSidebar "Run Now" + Email Sync buttons) POSTs with
`Content-Type: application/json` and an EMPTY body -- NO fields at all:

    fetch(`${RENDER_API_URL}/api/trigger-scheduler`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' } })

So the realistic valid payload carries no JSON fields. The handler
(app.py:585) never parses the body; it reads the `Authorization` / `X-API-Key`
headers and then IGNORES them ("we'll allow any request"), and spawns a daemon
thread that calls run_scheduler() -> refresh_and_process_user(uid) for every
user. That is a real SEND boundary (Microsoft Graph Mail.Send).

Test safety
-----------
Every external boundary is faked. The send boundary itself -- app.run_scheduler
(and the deeper app.refresh_and_process_user / app.send_outboxes / list_user_ids
and email_automation.clients._fs) -- is replaced with recording MagicMocks so
NOTHING real happens and NO email can leave. We assert the deep send functions
are never reached and that no send to a disallowed recipient can occur.
threading.Thread is replaced with a synchronous stub so the async trigger is
deterministic and observable.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod  # noqa: E402

ALLOWED_RECIPIENTS = {"bp21harrison@gmail.com", "baylor.freelance@outlook.com"}


class SyncThread:
    """Thread stub that runs the target synchronously on start() -- makes the
    handler's 'background' trigger deterministic and observable in-test."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **_):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.daemon = daemon

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)

    def join(self, *a, **k):
        pass


class DeferredThread(SyncThread):
    """Thread stub whose start() does NOT run the target -- models the real OS
    window between Thread.start() returning to the handler and the worker
    actually executing its first line (where scheduler_status['running'] is
    finally set). Used to expose the request-level TOCTOU."""

    def start(self):
        return None


class TriggerSchedulerFuzz(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()

        # --- Reset shared global state so tests don't cross-contaminate.
        appmod.scheduler_status = {
            "running": False,
            "last_run": None,
            "last_result": None,
        }

        # --- Force the interesting (send-capable) branch on.
        self._orig_available = appmod.SCHEDULER_AVAILABLE
        appmod.SCHEDULER_AVAILABLE = True

        # --- Fake the SEND boundary. run_scheduler is what the worker calls.
        self.run_scheduler_mock = MagicMock(
            return_value={"success": True, "message": "faked", "results": []}
        )
        self._p_run = patch.object(appmod, "run_scheduler", self.run_scheduler_mock)
        self._p_run.start()
        self.addCleanup(self._p_run.stop)

        # --- Deeper send boundaries: must NEVER be reached (run_scheduler mocked).
        self.refresh_mock = MagicMock(return_value={"success": True})
        self._p_refresh = patch.object(
            appmod, "refresh_and_process_user", self.refresh_mock
        )
        self._p_refresh.start()
        self.addCleanup(self._p_refresh.stop)

        self.send_outboxes_mock = MagicMock(return_value={"success": True})
        self._p_send = patch.object(appmod, "send_outboxes", self.send_outboxes_mock)
        self._p_send.start()
        self.addCleanup(self._p_send.stop)

        self.list_users_mock = MagicMock(return_value=[])
        self._p_list = patch.object(appmod, "list_user_ids", self.list_users_mock)
        self._p_list.start()
        self.addCleanup(self._p_list.stop)

        # --- Fake Firestore so nothing hits the network.
        self.fake_fs = MagicMock()
        fake_doc = MagicMock()
        fake_doc.exists = False
        self.fake_fs.collection.return_value.document.return_value.get.return_value = (
            fake_doc
        )
        try:
            self._p_fs = patch("email_automation.clients._fs", self.fake_fs)
            self._p_fs.start()
            self.addCleanup(self._p_fs.stop)
        except (ImportError, AttributeError):
            pass

        # --- Deterministic, synchronous "background" thread.
        self._p_thread = patch.object(appmod.threading, "Thread", SyncThread)
        self._p_thread.start()
        self.addCleanup(self._p_thread.stop)

        # --- Firebase ID-token verification: the hardened route now requires a
        # verified token. Patch the Admin SDK verifier so the authorised path is
        # exercised; requests still must carry an Authorization: Bearer header.
        self._p_verify = patch(
            "firebase_admin.auth.verify_id_token", return_value={"uid": "u1"}
        )
        self.verify_mock = self._p_verify.start()
        self.addCleanup(self._p_verify.stop)
        self.AUTH = {"Authorization": "Bearer testtoken"}

        self.addCleanup(self._restore_available)

    def _restore_available(self):
        appmod.SCHEDULER_AVAILABLE = self._orig_available
        appmod.scheduler_status = {
            "running": False,
            "last_run": None,
            "last_result": None,
        }

    # ------------------------------------------------------------------ helpers
    def _assert_no_disallowed_send(self):
        """No real send may occur, and certainly not to a non-allowed recipient."""
        self.refresh_mock.assert_not_called()
        self.send_outboxes_mock.assert_not_called()
        for call in self.run_scheduler_mock.call_args_list:
            for arg in list(call.args) + list(call.kwargs.values()):
                if isinstance(arg, str) and "@" in arg:
                    self.assertIn(
                        arg,
                        ALLOWED_RECIPIENTS,
                        f"send boundary reached a disallowed recipient: {arg!r}",
                    )

    def _assert_robust(self, resp, label):
        """A malformed request must fail closed: no 500, no leaked stack trace."""
        body = resp.get_data(as_text=True)
        self.assertNotEqual(
            resp.status_code, 500, f"[{label}] unhandled 500: {body[:300]}"
        )
        self.assertNotIn(
            "Traceback (most recent call last)",
            body,
            f"[{label}] leaked a Python traceback: {body[:300]}",
        )
        # A send-capable route must never leak internal file paths in errors.
        self.assertNotIn(
            "/Users/", body, f"[{label}] leaked an internal filesystem path"
        )

    def _reset_running(self):
        appmod.scheduler_status["running"] = False
        self.run_scheduler_mock.reset_mock()
        self.refresh_mock.reset_mock()
        self.send_outboxes_mock.reset_mock()

    # ------------------------------------------------------------------- happy
    def test_happy_path_empty_json_body(self):
        """Realistic FE payload: empty body, application/json + auth -> 200 + triggers."""
        resp = self.client.post(
            "/api/trigger-scheduler", data="", content_type="application/json",
            headers=self.AUTH,
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertIsNotNone(data)
        self.assertTrue(data.get("success"))
        self.assertIn("message", data)
        # The (faked) scheduler was actually kicked off exactly once.
        self.assertEqual(self.run_scheduler_mock.call_count, 1)
        # Deep real-send functions were never touched; no disallowed recipient.
        self.refresh_mock.assert_not_called()
        self.send_outboxes_mock.assert_not_called()
        # Worker completed -> running flag cleared.
        self.assertFalse(appmod.scheduler_status["running"])

    def test_happy_path_no_body_at_all(self):
        """Some callers POST with no Content-Type/body -> still handled (authorised)."""
        resp = self.client.post("/api/trigger-scheduler", headers=self.AUTH)
        self._assert_robust(resp, "no-body")
        self.assertIn(resp.status_code, (200, 400, 415))
        self._assert_no_disallowed_send() if resp.status_code != 200 else None

    # ------------------------------------------------------- correct guard paths
    def test_already_running_returns_409(self):
        """If a run is in progress the trigger is refused (correct guard)."""
        appmod.scheduler_status["running"] = True
        resp = self.client.post(
            "/api/trigger-scheduler", data="", content_type="application/json",
            headers=self.AUTH,
        )
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.get_json().get("success"))
        self.run_scheduler_mock.assert_not_called()
        self._assert_no_disallowed_send()

    def test_unavailable_returns_503(self):
        """If scheduler deps are missing -> 503 fail-closed, no send."""
        appmod.SCHEDULER_AVAILABLE = False
        resp = self.client.post(
            "/api/trigger-scheduler", data="", content_type="application/json",
            headers=self.AUTH,
        )
        self.assertEqual(resp.status_code, 503)
        self.assertFalse(resp.get_json().get("success"))
        self.run_scheduler_mock.assert_not_called()

    def test_get_method_not_allowed(self):
        resp = self.client.get("/api/trigger-scheduler")
        self.assertEqual(resp.status_code, 405)
        self.run_scheduler_mock.assert_not_called()

    # --------------------------------------------------- adversarial body battery
    def test_adversarial_body_battery_is_robust(self):
        """
        The handler ignores the request body entirely. Fire a wide battery of
        malformed / hostile bodies and assert the handler stays robust: never a
        500, never a leaked traceback/path, always JSON-ish, and the deep send
        functions are never reached (no disallowed recipient can be emailed).
        """
        big = "A" * 10240
        mutations = [
            ("empty_object", "{}", "application/json"),
            ("json_null", "null", "application/json"),
            ("json_true", "true", "application/json"),
            ("json_int", "12345", "application/json"),
            ("json_float", "3.14", "application/json"),
            ("json_array", "[1,2,3]", "application/json"),
            ("json_string", '"hello"', "application/json"),
            ("wrong_type_uid_int", '{"uid": 12345}', "application/json"),
            ("wrong_type_uid_array", '{"uid": [1,2]}', "application/json"),
            ("wrong_type_uid_object", '{"uid": {"x": 1}}', "application/json"),
            ("wrong_type_uid_bool", '{"uid": true}', "application/json"),
            ("null_uid", '{"uid": null}', "application/json"),
            ("empty_uid", '{"uid": ""}', "application/json"),
            ("oversized_field", '{"uid": "%s"}' % big, "application/json"),
            ("path_traversal", '{"uid": "../../etc/passwd"}', "application/json"),
            ("file_scheme", '{"uid": "file:///etc/passwd"}', "application/json"),
            ("placeholder_name", '{"clientId": "[NAME]"}', "application/json"),
            ("placeholder_broker", '{"clientId": "[BROKER]"}', "application/json"),
            ("script_tag", '{"uid": "<script>alert(1)</script>"}', "application/json"),
            ("newlines", '{"uid": "a\\nb\\r\\nc"}', "application/json"),
            ("unicode", '{"uid": "\\u2603\\u0000\\uffff"}', "application/json"),
            ("nonexistent_uid", '{"uid": "does-not-exist-xyz"}', "application/json"),
            ("nonexistent_thread", '{"threadId": "nope"}', "application/json"),
            ("extra_fields", '{"uid":"u1","evil":true,"x":[1]}', "application/json"),
            ("malformed_json", "{not valid json", "application/json"),
            ("truncated_json", '{"uid":', "application/json"),
            ("non_json_ctype", "garbage&stuff=1", "text/plain"),
            ("form_encoded", "uid=u1&x=2", "application/x-www-form-urlencoded"),
        ]

        for label, body, ctype in mutations:
            with self.subTest(mutation=label):
                self._reset_running()
                resp = self.client.post(
                    "/api/trigger-scheduler", data=body, content_type=ctype
                )
                self._assert_robust(resp, label)
                # Response is always JSON for this route.
                self.assertIsNotNone(
                    resp.get_json(silent=True),
                    f"[{label}] response was not JSON: "
                    f"{resp.get_data(as_text=True)[:200]}",
                )
                # Crucially: deep real-send functions must never be reached and
                # no disallowed recipient can ever be contacted, whatever the body.
                self._assert_no_disallowed_send()

    # -------------------------------------------------------------------- BUGS
    def test_unauthenticated_trigger_is_rejected(self):
        """
        BUG (dev-scoped guard is absent). The route is documented as a
        "dev-scoped guard" and it CAN SEND REAL EMAIL to arbitrary third-party
        recipients (run_scheduler -> refresh_and_process_user for every user).
        It reads Authorization / X-API-Key but never validates them
        (comment: "we'll allow any request"). A send-capable trigger must
        reject an unauthenticated caller. It currently accepts it (200) and
        fires the send.
        """
        resp = self.client.post(
            "/api/trigger-scheduler", data="", content_type="application/json"
        )
        # CORRECT behavior: unauthenticated -> refused (401/403), send NOT fired.
        self.assertIn(
            resp.status_code,
            (401, 403),
            "Unauthenticated request to a send-capable 'dev-scoped guard' route "
            "was ACCEPTED (status %s) and triggered the scheduler "
            "(run_scheduler called %d time(s)). No auth is enforced -- any "
            "anonymous POST sends real email."
            % (resp.status_code, self.run_scheduler_mock.call_count),
        )
        self.run_scheduler_mock.assert_not_called()

    def test_invalid_token_is_rejected(self):
        """A present-but-invalid bearer token must be refused (401), no send."""
        self.verify_mock.side_effect = ValueError("bad token")
        resp = self.client.post(
            "/api/trigger-scheduler", data="", content_type="application/json",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        self.assertEqual(resp.status_code, 401, resp.get_data(as_text=True))
        self.run_scheduler_mock.assert_not_called()
        self._assert_no_disallowed_send()

    def test_no_request_level_mutex_allows_double_trigger(self):
        """
        BUG (TOCTOU / duplicate-send). The 'already running' guard checks
        scheduler_status['running'] in the request handler, but that flag is set
        to True only INSIDE the worker thread (run_scheduler_async's first
        line), after Thread.start() has already returned success to the client.
        Two back-to-back requests (double-click / retry / concurrent) both pass
        the guard before either worker sets the flag -> two full scheduler runs
        -> duplicate emails. DeferredThread models that real OS window.
        """
        with patch.object(appmod.threading, "Thread", DeferredThread):
            appmod.scheduler_status["running"] = False
            r1 = self.client.post(
                "/api/trigger-scheduler", data="", content_type="application/json",
                headers=self.AUTH,
            )
            # Worker hasn't run yet (flag still False), exactly like the real race.
            r2 = self.client.post(
                "/api/trigger-scheduler", data="", content_type="application/json",
                headers=self.AUTH,
            )

        statuses = (r1.status_code, r2.status_code)
        # CORRECT behavior: the second concurrent trigger is refused (409) so the
        # scheduler cannot be double-started.
        self.assertEqual(
            r2.status_code,
            409,
            "Two back-to-back triggers were BOTH accepted %s -- no request-level "
            "mutex. The 'running' guard is set inside the worker thread, so "
            "rapid/concurrent requests each spawn a full scheduler run "
            "(duplicate sends)." % (statuses,),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
