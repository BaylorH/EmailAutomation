"""
Surface C — dashboard-surface authentication + validation hardening.

Adversarial coverage for the previously-UNAUTHENTICATED dashboard routes that
either disclosed multi-tenant data or performed destructive actions against an
attacker-supplied user/sheet. Every gap here is reproduced as a failing attack
first, then the hardened route is asserted to fail closed.

Routes covered (Group A — no pre-existing fuzz file):
  GET  /api/debug-inbox              (gap 5  — first-user inbox disclosure)
  GET  /api/debug-thread-matching    (gap 6  — first-user inbox+sheet disclosure)
  GET  /api/firestore-inspect        (gap 7  — full multi-tenant Firestore dump)
  GET  /api/console-logs             (gap 12 — read/clear victim logs + limit 500)
  POST /api/console-logs/clear       (gap 13 — wipe victim consoleLogs)
  POST /api/clear-outlook-emails     (gap 15 — delete victim real mail + keyword over-match)
  GET  /auth/callback                (gap 17 — state path traversal on token write)
  GET  /api/scheduler-status         (gap 18 — env-var presence + import-error recon)

Nothing real is ever contacted: the send/delete boundaries (download_token,
Graph, Firestore) are faked, and os.makedirs is stubbed for the traversal test.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod  # noqa: E402

CALLER = "u1"
VICTIM = "victim-tenant-0"


class _AuthBase(unittest.TestCase):
    """Common Firebase-token fake + a helper for the authorised header."""

    def setUp(self):
        self.client = appmod.app.test_client()

        # Force the send-capable dev-scope branch on for the debug/admin routes.
        self._orig_available = appmod.SCHEDULER_AVAILABLE
        appmod.SCHEDULER_AVAILABLE = True
        self.addCleanup(self._restore_available)

        # Firebase ID-token verification: authorised path resolves to CALLER.
        self._p_verify = patch(
            "firebase_admin.auth.verify_id_token", return_value={"uid": CALLER}
        )
        self.verify_mock = self._p_verify.start()
        self.addCleanup(self._p_verify.stop)
        self.AUTH = {"Authorization": "Bearer testtoken"}

    def _restore_available(self):
        appmod.SCHEDULER_AVAILABLE = self._orig_available


# =========================================================================
# gap 5 / gap 6 — debug endpoints must not disclose the first user's inbox
# =========================================================================
class DebugEndpointsAuth(_AuthBase):
    ROUTES = ("/api/debug-inbox", "/api/debug-thread-matching")

    def test_unauthenticated_is_rejected(self):
        for route in self.ROUTES:
            with self.subTest(route=route):
                r = self.client.get(route)  # no Authorization header
                self.assertEqual(
                    r.status_code, 401,
                    f"{route} disclosed data without auth: {r.get_data(as_text=True)[:200]}",
                )

    def test_invalid_token_is_rejected(self):
        self.verify_mock.side_effect = ValueError("bad token")
        for route in self.ROUTES:
            with self.subTest(route=route):
                r = self.client.get(route, headers={"Authorization": "Bearer nope"})
                self.assertEqual(r.status_code, 401)

    def test_debug_inbox_scoped_to_caller_not_first_user(self):
        # list_user_ids would previously choose [0]; the hardened route must use
        # the verified caller uid regardless of who is first.
        dl = MagicMock(side_effect=RuntimeError("stop before network"))
        with patch("firebase_helpers.download_token", dl), \
             patch("email_automation.clients.list_user_ids", return_value=[VICTIM, CALLER]):
            r = self.client.get("/api/debug-inbox", headers=self.AUTH)
        # Route fails closed (generic 500) but crucially used the CALLER's token,
        # never the first (victim) user's.
        self.assertTrue(dl.called, "download_token should have been reached")
        self.assertEqual(dl.call_args.kwargs.get("user_id"), CALLER)
        self.assertNotEqual(dl.call_args.kwargs.get("user_id"), VICTIM)


# =========================================================================
# gap 7 — firestore-inspect must only ever expose the caller's own subtree
# =========================================================================
class FirestoreInspectAuth(_AuthBase):
    def _fake_fs(self):
        fake = MagicMock()
        users_doc = fake.collection.return_value.document.return_value
        subcoll = users_doc.collection.return_value
        subcoll.limit.return_value.stream.return_value = []
        subcoll.stream.return_value = []
        return fake

    def test_unauthenticated_is_rejected(self):
        r = self.client.get("/api/firestore-inspect")
        self.assertEqual(r.status_code, 401)

    def test_only_caller_subtree_returned(self):
        fake = self._fake_fs()
        with patch("email_automation.clients._fs", fake), \
             patch("email_automation.clients.list_user_ids", return_value=[VICTIM, "other", CALLER]):
            r = self.client.get("/api/firestore-inspect", headers=self.AUTH)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = r.get_json()["data"]
        self.assertEqual(set(data["users"].keys()), {CALLER})
        self.assertNotIn(VICTIM, data["users"])


# =========================================================================
# gap 12 / gap 13 — console-logs read/clear must be caller-scoped + robust
# =========================================================================
class ConsoleLogsAuth(_AuthBase):
    def _fake_read_fs(self):
        fake = MagicMock()
        logs_ref = fake.collection.return_value.document.return_value.collection.return_value
        q = logs_ref.order_by.return_value
        q.where.return_value = q
        q.limit.return_value = q
        q.stream.return_value = []
        self.doc = fake.collection.return_value.document
        self.q = q
        return fake

    def _fake_clear_fs(self):
        fake = MagicMock()
        logs_ref = fake.collection.return_value.document.return_value.collection.return_value
        logs_ref.limit.return_value.stream.return_value = []
        self.doc = fake.collection.return_value.document
        return fake

    def test_get_unauthenticated_is_rejected(self):
        r = self.client.get("/api/console-logs?user_id=%s&clear=true" % VICTIM)
        self.assertEqual(r.status_code, 401)

    def test_post_clear_unauthenticated_is_rejected(self):
        r = self.client.post("/api/console-logs/clear", json={"user_id": VICTIM})
        self.assertEqual(r.status_code, 401)

    def test_get_ignores_query_user_id_and_scopes_to_caller(self):
        fake = self._fake_read_fs()
        with patch("google.cloud.firestore.Client", return_value=fake):
            r = self.client.get(
                "/api/console-logs?user_id=%s" % VICTIM, headers=self.AUTH
            )
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # Firestore was addressed with the CALLER uid, never the query victim.
        self.assertEqual(self.doc.call_args.args[0], CALLER)

    def test_get_nonnumeric_limit_does_not_500(self):
        fake = self._fake_read_fs()
        with patch("google.cloud.firestore.Client", return_value=fake):
            r = self.client.get(
                "/api/console-logs?limit=not-a-number", headers=self.AUTH
            )
        self.assertNotEqual(r.status_code, 500, r.get_data(as_text=True))
        self.assertEqual(r.status_code, 200)

    def test_get_oversized_limit_is_capped(self):
        fake = self._fake_read_fs()
        with patch("google.cloud.firestore.Client", return_value=fake):
            r = self.client.get(
                "/api/console-logs?limit=99999999", headers=self.AUTH
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.q.limit.call_args.args[0], 500)

    def test_post_clear_scopes_to_caller(self):
        fake = self._fake_clear_fs()
        with patch("google.cloud.firestore.Client", return_value=fake):
            r = self.client.post(
                "/api/console-logs/clear", json={"user_id": VICTIM}, headers=self.AUTH
            )
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["user_id"], CALLER)
        self.assertEqual(self.doc.call_args.args[0], CALLER)

    def test_post_clear_non_object_body_rejected(self):
        r = self.client.post(
            "/api/console-logs/clear", data="[1,2,3]",
            content_type="application/json", headers=self.AUTH,
        )
        self.assertEqual(r.status_code, 400)


# =========================================================================
# gap 15 — clear-outlook-emails: auth + caller scope + keyword validation
# =========================================================================
class ClearOutlookEmailsAuth(_AuthBase):
    def setUp(self):
        super().setUp()
        self._p_flag = patch.object(
            appmod, "_destructive_admin_routes_enabled", return_value=True
        )
        self._p_flag.start()
        self.addCleanup(self._p_flag.stop)
        # The delete boundary: must NEVER be reached on a rejected request.
        self.dl = MagicMock(side_effect=RuntimeError("stop before network"))
        self._p_dl = patch("firebase_helpers.download_token", self.dl)
        self._p_dl.start()
        self.addCleanup(self._p_dl.stop)

    def test_unauthenticated_is_rejected(self):
        r = self.client.post("/api/clear-outlook-emails", json={"user_id": VICTIM})
        self.assertEqual(r.status_code, 401)
        self.dl.assert_not_called()

    def test_bare_string_keywords_rejected_no_delete(self):
        # 'a' as a str makes `kw in subject` iterate characters -> deletes nearly
        # everything. Must be rejected before any mail boundary is touched.
        r = self.client.post(
            "/api/clear-outlook-emails", json={"keywords": "a"}, headers=self.AUTH
        )
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.dl.assert_not_called()

    def test_nonlist_keywords_rejected(self):
        for bad in (5, {"x": 1}, True):
            with self.subTest(keywords=bad):
                r = self.client.post(
                    "/api/clear-outlook-emails", json={"keywords": bad}, headers=self.AUTH
                )
                self.assertEqual(r.status_code, 400)
                self.dl.assert_not_called()

    def test_keywords_with_nonstring_element_rejected(self):
        r = self.client.post(
            "/api/clear-outlook-emails", json={"keywords": ["ok", 5, None]}, headers=self.AUTH
        )
        self.assertEqual(r.status_code, 400)
        self.dl.assert_not_called()

    def test_non_object_body_rejected(self):
        r = self.client.post(
            "/api/clear-outlook-emails", data="null",
            content_type="application/json", headers=self.AUTH,
        )
        self.assertEqual(r.status_code, 400)
        self.dl.assert_not_called()

    def test_valid_request_targets_caller_mailbox(self):
        # A well-formed request reaches the (faked, exploding) download boundary
        # with the CALLER's uid — never an attacker-supplied victim id.
        r = self.client.post(
            "/api/clear-outlook-emails",
            json={"user_id": VICTIM, "keywords": ["Commerce"]},
            headers=self.AUTH,
        )
        self.assertTrue(self.dl.called)
        self.assertEqual(self.dl.call_args.kwargs.get("user_id"), CALLER)


# =========================================================================
# gap 17 — /auth/callback state must be path-sanitised before FS write
# =========================================================================
class AuthCallbackTraversal(_AuthBase):
    def test_traversal_state_does_not_escape_msal_caches(self):
        made = []

        def _record_makedirs(path, *a, **k):
            made.append(path)

        with patch.object(appmod, "_legacy_flask_oauth_enabled", return_value=True), \
             patch.object(appmod, "_legacy_flask_oauth_redirect_uri", return_value="https://x/cb"), \
             patch.object(appmod, "ConfidentialClientApplication", MagicMock()), \
             patch.object(appmod.os, "makedirs", side_effect=_record_makedirs):
            # No `code` param -> handler returns the error page, but only AFTER
            # os.makedirs(user_dir) has run. state carries a traversal payload.
            r = self.client.get("/auth/callback?state=../../../etc/evil")

        self.assertTrue(made, "expected os.makedirs to be exercised")
        for path in made:
            self.assertNotIn("..", path, f"traversal escaped msal_caches: {path!r}")
            self.assertTrue(
                path.startswith("msal_caches/"), f"unexpected write path: {path!r}"
            )
        # Sanitised fallback identity.
        self.assertIn("msal_caches/web_user", made)


# =========================================================================
# gap 18 — scheduler-status must not leak env-var presence / import internals
# =========================================================================
class SchedulerStatusRecon(_AuthBase):
    def test_no_env_or_import_internals_disclosed(self):
        r = self.client.get("/api/scheduler-status")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertNotIn("debug_env_vars", body)
        self.assertNotIn("import_error", body)
        # Only a boolean flag is exposed, never the raw error text.
        self.assertIn("has_import_error", body)
        self.assertIsInstance(body["has_import_error"], bool)
        raw = r.get_data(as_text=True)
        for leak in ("FIREBASE_API_KEY", "AZURE_API_APP_ID", "OPENAI_API_KEY", "✅", "❌"):
            self.assertNotIn(leak, raw, f"scheduler-status leaked {leak!r}")


# =========================================================================
# gap 11 — POST /api/clear must scope to the verified caller, not session uid
# =========================================================================
class ApiClearAuth(_AuthBase):
    """POST /api/clear previously trusted session['uid'] (set via GET /?uid=),
    letting an unauthenticated caller wipe another user's MSAL token cache."""

    def test_unauthenticated_is_rejected(self):
        r = self.client.post("/api/clear")
        self.assertEqual(r.status_code, 401, r.get_data(as_text=True)[:200])

    def test_invalid_token_is_rejected(self):
        self.verify_mock.side_effect = ValueError("bad token")
        r = self.client.post("/api/clear", headers={"Authorization": "Bearer nope"})
        self.assertEqual(r.status_code, 401)

    def test_clears_caller_cache_ignoring_session_uid(self):
        seen = {}

        def fake_exists(path):
            seen["exists_path"] = path
            return False

        with patch("os.makedirs"), patch("os.path.exists", side_effect=fake_exists):
            with self.client.session_transaction() as sess:
                sess["uid"] = VICTIM  # attacker-primed session uid
            r = self.client.post("/api/clear", headers=self.AUTH)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # The cache path must key on the verified caller, never the session uid.
        self.assertIn(CALLER, seen["exists_path"])
        self.assertNotIn(VICTIM, seen["exists_path"])


# =========================================================================
# gap 14 — POST /api/firestore-cleanup needs auth + caller-only target scoping
# =========================================================================
class FirestoreCleanupAuth(_AuthBase):
    def test_unauthenticated_is_rejected(self):
        r = self.client.post("/api/firestore-cleanup", json={"user_id": CALLER})
        self.assertEqual(r.status_code, 401)

    def test_invalid_token_is_rejected(self):
        self.verify_mock.side_effect = ValueError("bad token")
        r = self.client.post(
            "/api/firestore-cleanup",
            json={"user_id": CALLER},
            headers={"Authorization": "Bearer nope"},
        )
        self.assertEqual(r.status_code, 401)

    def test_cannot_target_another_users_data(self):
        # Authenticated as CALLER but naming VICTIM as the cleanup target must be
        # forbidden, and must never reach the Firestore delete loop. The
        # destructive-admin flag is enabled so the auth/scoping check — not the
        # flag gate — is what rejects the request.
        with patch.object(appmod, "_destructive_admin_routes_enabled", return_value=True), \
             patch("email_automation.clients._fs") as fake_fs:
            r = self.client.post(
                "/api/firestore-cleanup",
                json={"user_id": VICTIM, "clear_dead_letter": True},
                headers=self.AUTH,
            )
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True)[:200])
        fake_fs.collection.assert_not_called()

    def test_missing_user_id_rejected(self):
        with patch.object(appmod, "_destructive_admin_routes_enabled", return_value=True):
            r = self.client.post(
                "/api/firestore-cleanup",
                json={"clear_dead_letter": True},
                headers=self.AUTH,
            )
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)