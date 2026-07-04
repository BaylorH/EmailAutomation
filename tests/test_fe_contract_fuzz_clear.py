"""
FE-contract adversarial fuzz for POST /api/clear  ("Clear campaign state").

Handler under test (app.py api_clear) — HARDENED contract:

    @app.route("/api/clear", methods=["POST"])
    @verify_firebase_token                       # 401 on missing/invalid token
    def api_clear():
        uid = _safe_uid(g.get("firebase_uid"))   # VERIFIED token uid, not session
        if uid is None:                          # path-unsafe verified uid -> 400
            return jsonify({"success": False, "error": ...}), 400
        try:
            user_dir = f"msal_caches/{uid}"
            cache_file = f"{user_dir}/msal_token_cache.bin"
            os.makedirs(user_dir, exist_ok=True)
            if os.path.exists(cache_file):
                os.remove(cache_file)
            return jsonify({"success": True, "message": "Token cache cleared"})
        except Exception:                        # fail-CLOSED 500, generic error
            return jsonify({"success": False, "error": "Failed to clear token cache"}), 500

Boundary facts established by reading source (post-hardening):
  * Identity is the Firebase-VERIFIED token uid (`g.firebase_uid`), NOT
    session["uid"]. The body is still never read; the only value that reaches
    the filesystem path is the verified uid.
  * That verified uid is run through `_safe_uid` (regex `^[A-Za-z0-9_-]{1,128}$`).
    A traversal / null-byte uid fails the regex -> the handler returns 400 and
    NEVER touches os.makedirs/os.remove. The path-traversal sandbox defense is
    therefore enforced on the verified identity itself.
  * An unauthenticated request (no `Authorization: Bearer <token>`) is rejected
    with 401 before any handler logic runs.
  * The route is NOT send-capable: it touches only the filesystem. No Firestore
    (_fs), no Sheets, no Microsoft Graph / send_* call. We still defensively
    patch the send entrypoints and assert they are NEVER invoked.

Everything external is faked: firebase token verification is patched to return a
chosen verified uid, os.makedirs / os.remove / os.path.exists are patched to
RECORD calls (so nothing is written to / deleted from the real disk), and the
send_* functions are MagicMocks that must never be called.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod  # noqa: E402

ALLOWED_RECIPIENTS = {"bp21harrison@gmail.com", "baylor.freelance@outlook.com"}
ROUTE = "/api/clear"

# The default verified caller identity for authenticated requests.
CALLER = "web_user"


class ClearFuzzBase(unittest.TestCase):
    def setUp(self):
        appmod.app.testing = True
        self.client = appmod.app.test_client()

        # Firebase ID-token verification: the authorised path resolves to
        # whatever uid the case is exercising (default CALLER). This REPLACES
        # session-uid control as the way a test drives the effective identity.
        self._p_verify = patch(
            "firebase_admin.auth.verify_id_token", return_value={"uid": CALLER}
        )
        self.verify_mock = self._p_verify.start()
        self.addCleanup(self._p_verify.stop)
        self.AUTH = {"Authorization": "Bearer testtoken"}

    def _set_verified_uid(self, uid):
        """Make the verified Firebase caller identity be `uid`."""
        self.verify_mock.return_value = {"uid": uid}

    def _invoke(self, *, uid=None, auth=True, **post_kwargs):
        """
        Drive POST /api/clear with every external boundary faked.

        `uid` (when given) becomes the VERIFIED Firebase token uid. `auth=True`
        attaches a Bearer header; `auth=False` sends no header (unauthenticated).

        Returns (response, recorder) where recorder exposes:
            .makedirs / .remove / .exists  -> the patched os mocks
            .send_and_index / .send_outboxes / .send_email -> send guards
            .paths  -> every path string passed to makedirs+remove
        """
        if uid is not None:
            self._set_verified_uid(uid)

        headers = post_kwargs.pop("headers", None)
        if headers is None and auth:
            headers = dict(self.AUTH)

        rec = MagicMock()
        rec.exists.return_value = True  # force the os.remove() branch to run

        with patch("app.os.makedirs", rec.makedirs), \
             patch("app.os.remove", rec.remove), \
             patch("app.os.path.exists", rec.exists), \
             patch("email_automation.email.send_and_index_email", rec.send_and_index), \
             patch("email_automation.email.send_outboxes", rec.send_outboxes), \
             patch("email_automation.email.send_email", rec.send_email):
            resp = self.client.post(ROUTE, headers=headers, **post_kwargs)

        rec.paths = [str(c.args[0]) for c in rec.makedirs.call_args_list if c.args]
        rec.paths += [str(c.args[0]) for c in rec.remove.call_args_list if c.args]
        return resp, rec

    # ---- shared robustness assertions -------------------------------------
    def assert_no_send(self, rec):
        self.assertFalse(rec.send_and_index.called, "send_and_index_email was called")
        self.assertFalse(rec.send_outboxes.called, "send_outboxes was called")
        self.assertFalse(rec.send_email.called, "send_email was called")

    def assert_no_stacktrace(self, resp):
        body = resp.get_data(as_text=True)
        self.assertNotIn("Traceback (most recent call last)", body)
        self.assertNotIn('File "', body)

    def assert_paths_stay_in_sandbox(self, rec):
        """Every fs path the handler acted on must stay inside msal_caches/."""
        for p in rec.paths:
            norm = os.path.normpath(p)
            self.assertTrue(
                norm == "msal_caches" or norm.startswith("msal_caches" + os.sep),
                f"handler escaped the msal_caches sandbox: {p!r} -> {norm!r}",
            )


# =========================================================================
# Authentication gate — the hardened contract's first line of defense
# =========================================================================
class TestClearAuthGate(ClearFuzzBase):
    def test_unauthenticated_post_is_rejected(self):
        # No Authorization header at all -> 401 before any handler logic and
        # before any filesystem boundary is touched.
        resp, rec = self._invoke(auth=False)
        self.assertEqual(
            resp.status_code, 401, resp.get_data(as_text=True)[:200]
        )
        rec.makedirs.assert_not_called()
        rec.remove.assert_not_called()
        self.assert_no_send(rec)
        self.assert_no_stacktrace(resp)

    def test_invalid_token_is_rejected(self):
        # A present-but-unverifiable token -> 401, no filesystem action.
        self.verify_mock.side_effect = ValueError("bad token")
        resp, rec = self._invoke(headers={"Authorization": "Bearer nope"})
        self.assertEqual(resp.status_code, 401)
        rec.makedirs.assert_not_called()
        rec.remove.assert_not_called()
        self.assert_no_send(rec)


# =========================================================================
# Happy path
# =========================================================================
class TestClearHappyPath(ClearFuzzBase):
    def test_realistic_empty_post_clears_cache(self):
        # The real frontend fires an empty POST with a Bearer token; the cache
        # path keys on the VERIFIED token uid (CALLER), never a session value.
        resp, rec = self._invoke(uid=CALLER)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"success": True, "message": "Token cache cleared"})
        # expected state change on the fakes: dir ensured + cache file removed
        rec.makedirs.assert_called_once_with(f"msal_caches/{CALLER}", exist_ok=True)
        rec.remove.assert_called_once_with(f"msal_caches/{CALLER}/msal_token_cache.bin")
        self.assert_no_send(rec)
        self.assert_paths_stay_in_sandbox(rec)

    def test_no_cache_file_still_succeeds(self):
        # exists() False -> remove() must NOT be called, still success (idempotent-ish)
        self._set_verified_uid(CALLER)
        rec = MagicMock()
        rec.exists.return_value = False
        with patch("app.os.makedirs", rec.makedirs), \
             patch("app.os.remove", rec.remove), \
             patch("app.os.path.exists", rec.exists):
            resp = self.client.post(ROUTE, headers=self.AUTH)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get("success"))
        rec.remove.assert_not_called()


# =========================================================================
# Body-mutation battery. The body is ignored by the handler, so each of
# these SHOULD remain robust (no crash / no leak / no send) for an
# authenticated caller. These pin the contract that junk bodies cannot break
# the endpoint.
# =========================================================================
class TestClearBodyMutations(ClearFuzzBase):
    def _expect_robust(self, label, **post_kwargs):
        with self.subTest(mutation=label):
            resp, rec = self._invoke(uid=CALLER, **post_kwargs)
            # must not be an unhandled 500 leaking a stack trace
            self.assertNotEqual(resp.status_code, 500, f"{label}: 500 unhandled")
            self.assert_no_stacktrace(resp)
            self.assert_no_send(rec)
            self.assert_paths_stay_in_sandbox(rec)

    def test_no_body_at_all(self):
        self._expect_robust("no-body")

    def test_empty_json_object(self):
        self._expect_robust("empty-json", json={})

    def test_empty_string_body(self):
        self._expect_robust("empty-string", data="")

    def test_null_json(self):
        self._expect_robust("json-null", data="null", content_type="application/json")

    def test_json_array(self):
        self._expect_robust("json-array", json=["a", "b"])

    def test_json_bool(self):
        self._expect_robust("json-bool", data="true", content_type="application/json")

    def test_json_int(self):
        self._expect_robust("json-int", data="12345", content_type="application/json")

    def test_wrong_types_for_expected_stringish_fields(self):
        self._expect_robust("wrong-types", json={"uid": 5, "threadId": ["x"], "clientId": {"a": 1}})

    def test_oversized_string(self):
        self._expect_robust("oversized-10kb", json={"blob": "A" * 10240})

    def test_injection_values(self):
        # A body carrying traversal/injection payloads must be ignored: identity
        # comes from the verified token, so the handler stays in the sandbox.
        self._expect_robust("injection", json={
            "uid": "../../../../etc/passwd",
            "path": "file:///etc/shadow",
            "placeholder": "[NAME] [BROKER]",
            "xss": "<script>alert(1)</script>",
            "newlines": "a\r\nb\nc",
            "unicode": "‮\U0001f4a9",
        })

    def test_malformed_json_body(self):
        self._expect_robust("malformed-json", data="{not:json,]", content_type="application/json")

    def test_non_json_content_type(self):
        self._expect_robust("text-plain", data="hello", content_type="text/plain")

    def test_unexpected_extra_fields(self):
        self._expect_robust("extra-fields", json={"foo": 1, "bar": 2, "admin": True, "uid": "web_user"})

    def test_duplicate_retry_idempotent(self):
        # Same request twice must not double-delete / mutate beyond the first.
        resp1, rec1 = self._invoke(uid=CALLER, json={})
        resp2, rec2 = self._invoke(uid=CALLER, json={})
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp1.get_json(), resp2.get_json())
        # each call touches exactly its own uid's sandbox, nothing else
        self.assert_paths_stay_in_sandbox(rec1)
        self.assert_paths_stay_in_sandbox(rec2)


# =========================================================================
# Verified-uid mutations -- the verified Firebase token uid is now the ONLY
# user-influenced value that reaches the handler. This is where the sandbox
# defense on the identity lives.
# =========================================================================
class TestClearUidMutations(ClearFuzzBase):
    def test_cache_keys_on_verified_token_uid(self):
        # A benign verified uid keys the cache directly; stays in sandbox.
        self._set_verified_uid("web_user")
        rec = MagicMock()
        rec.exists.return_value = True
        with patch("app.os.makedirs", rec.makedirs), \
             patch("app.os.remove", rec.remove), \
             patch("app.os.path.exists", rec.exists):
            resp = self.client.post(ROUTE, headers=self.AUTH)
        self.assertEqual(resp.status_code, 200)
        rec.makedirs.assert_called_once_with("msal_caches/web_user", exist_ok=True)

    def test_nonexistent_uid_is_graceful(self):
        # Unknown-but-benign (path-safe) verified uid: no cache file present ->
        # success, no crash, remove not called.
        self._set_verified_uid("totally-unknown-uid-xyz")
        rec = MagicMock()
        rec.exists.return_value = False
        with patch("app.os.makedirs", rec.makedirs), \
             patch("app.os.remove", rec.remove), \
             patch("app.os.path.exists", rec.exists):
            resp = self.client.post(ROUTE, headers=self.AUTH)
        self.assertEqual(resp.status_code, 200)
        rec.remove.assert_not_called()

    # ---- path traversal via a malicious VERIFIED uid ----------------------
    def test_path_traversal_uid_is_neutralised_by_safe_uid(self):
        """
        Even if a verified token carried a traversal-shaped uid, `_safe_uid`
        (regex ^[A-Za-z0-9_-]{1,128}$) rejects it: the handler returns 400 and
        NEVER reaches os.makedirs/os.remove. The traversal cannot escape the
        msal_caches sandbox because no filesystem call is made at all.
        """
        resp, rec = self._invoke(uid="../../../../tmp/evil-clear-target")
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True)[:200])
        rec.makedirs.assert_not_called()
        rec.remove.assert_not_called()
        self.assert_no_stacktrace(resp)
        # Nothing acted on -> vacuously inside the sandbox, and no send happened.
        self.assert_paths_stay_in_sandbox(rec)
        self.assert_no_send(rec)

    def test_path_traversal_null_byte_uid(self):
        # Null byte in a verified uid also fails _safe_uid -> 400, no os.* call.
        resp, rec = self._invoke(uid="../secret\x00")
        self.assertEqual(resp.status_code, 400, resp.get_data(as_text=True)[:200])
        rec.makedirs.assert_not_called()
        rec.remove.assert_not_called()
        self.assert_no_stacktrace(resp)
        self.assert_paths_stay_in_sandbox(rec)

    # ---- fail-closed status + internal-error hiding ------------------------
    def test_error_path_fails_closed_and_hides_internals(self):
        """
        A filesystem error inside the handler must fail CLOSED (>= 400, never a
        misleading 200) and must NOT echo the raw exception text / internal path
        back to the client.
        """
        self._set_verified_uid("web_user")
        secret = "OSError: [Errno 13] Permission denied: '/private/secret/path'"
        with patch("app.os.makedirs", side_effect=OSError(13, secret)), \
             patch("app.os.remove"), \
             patch("app.os.path.exists", return_value=False):
            resp = self.client.post(ROUTE, headers=self.AUTH)
        # fail-closed: an error must not be reported as HTTP 200
        self.assertNotEqual(resp.status_code, 200,
                            "error path returns HTTP 200 (fail-open)")
        self.assertGreaterEqual(resp.status_code, 400)
        # no internal error text leaked to the client
        body = resp.get_data(as_text=True)
        self.assertNotIn("Errno", body, "raw OSError text leaked to client")
        self.assertNotIn("/private/secret/path", body, "internal path leaked to client")

    def test_error_response_shape_flags_failure(self):
        """
        A caller must be able to tell a clear FAILED — either via a >= 400 status
        or an explicit success:false in the body.
        """
        self._set_verified_uid("web_user")
        with patch("app.os.makedirs", side_effect=OSError("boom")), \
             patch("app.os.remove"), \
             patch("app.os.path.exists", return_value=False):
            resp = self.client.post(ROUTE, headers=self.AUTH)
        data = resp.get_json() or {}
        failed = (resp.status_code >= 400) or (data.get("success") is False)
        self.assertTrue(failed, "failed clear is not distinguishable from success")


if __name__ == "__main__":
    unittest.main(verbosity=2)
