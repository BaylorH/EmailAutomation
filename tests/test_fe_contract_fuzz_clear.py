"""
FE-contract adversarial fuzz for POST /api/clear  ("Clear campaign state").

Handler under test (app.py:531 api_clear):

    uid = session.get("uid", "web_user")
    user_dir = f"msal_caches/{uid}"
    cache_file = f"{user_dir}/msal_token_cache.bin"
    os.makedirs(user_dir, exist_ok=True)
    if os.path.exists(cache_file):
        os.remove(cache_file)
    return jsonify({"success": True, "message": "Token cache cleared"})
    # except Exception as e: return jsonify({"error": str(e)})    <-- HTTP 200

Boundary facts established by reading source + frontend:
  * The request BODY is never read. There are no required/optional body fields;
    the only user-controlled input that reaches the handler is session["uid"],
    which the `/` index route sets verbatim from `request.args.get("uid")`
    with NO sanitisation (app.py:208). So an attacker fully controls `uid`.
  * `uid` is interpolated directly into a filesystem path that is then passed to
    os.makedirs() and os.remove().  ==> path-traversal / arbitrary-directory
    -create + arbitrary-file-delete surface.
  * The route is NOT send-capable: it touches only the filesystem. No Firestore
    (_fs), no Sheets, no Microsoft Graph / send_* call. We still defensively
    patch the send entrypoints and assert they are NEVER invoked.

Everything external is faked: os.makedirs / os.remove / os.path.exists are
patched to RECORD calls (so nothing is written to / deleted from the real disk),
and the send_* functions are MagicMocks that must never be called.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod  # noqa: E402

ALLOWED_RECIPIENTS = {"bp21harrison@gmail.com", "baylor.freelance@outlook.com"}
ROUTE = "/api/clear"


class ClearFuzzBase(unittest.TestCase):
    def setUp(self):
        appmod.app.testing = True
        self.client = appmod.app.test_client()

    def _set_session_uid(self, uid):
        with self.client.session_transaction() as sess:
            if uid is _UNSET:
                sess.pop("uid", None)
            else:
                sess["uid"] = uid

    def _invoke(self, *, uid=None, **post_kwargs):
        """
        Drive POST /api/clear with every external boundary faked.

        Returns (response, recorder) where recorder exposes:
            .makedirs / .remove / .exists  -> the patched os mocks
            .send_and_index / .send_outboxes / .send_email -> send guards
            .paths  -> every path string passed to makedirs+remove
        """
        if uid is not None:
            self._set_session_uid(uid)

        rec = MagicMock()
        rec.exists.return_value = True  # force the os.remove() branch to run

        with patch("app.os.makedirs", rec.makedirs), \
             patch("app.os.remove", rec.remove), \
             patch("app.os.path.exists", rec.exists), \
             patch("email_automation.email.send_and_index_email", rec.send_and_index), \
             patch("email_automation.email.send_outboxes", rec.send_outboxes), \
             patch("email_automation.email.send_email", rec.send_email):
            resp = self.client.post(ROUTE, **post_kwargs)

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


_UNSET = object()


# =========================================================================
# Happy path
# =========================================================================
class TestClearHappyPath(ClearFuzzBase):
    def test_realistic_empty_post_clears_cache(self):
        # The real frontend fires an empty POST; uid comes from the session.
        resp, rec = self._invoke(uid="web_user")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"success": True, "message": "Token cache cleared"})
        # expected state change on the fakes: dir ensured + cache file removed
        rec.makedirs.assert_called_once_with("msal_caches/web_user", exist_ok=True)
        rec.remove.assert_called_once_with("msal_caches/web_user/msal_token_cache.bin")
        self.assert_no_send(rec)
        self.assert_paths_stay_in_sandbox(rec)

    def test_no_cache_file_still_succeeds(self):
        # exists() False -> remove() must NOT be called, still success (idempotent-ish)
        self._set_session_uid("web_user")
        rec = MagicMock()
        rec.exists.return_value = False
        with patch("app.os.makedirs", rec.makedirs), \
             patch("app.os.remove", rec.remove), \
             patch("app.os.path.exists", rec.exists):
            resp = self.client.post(ROUTE)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get("success"))
        rec.remove.assert_not_called()


# =========================================================================
# Body-mutation battery. The body is ignored by the handler, so each of
# these SHOULD remain robust (no crash / no leak / no send). These pin the
# contract that junk bodies cannot break the endpoint.
# =========================================================================
class TestClearBodyMutations(ClearFuzzBase):
    def _expect_robust(self, label, **post_kwargs):
        with self.subTest(mutation=label):
            resp, rec = self._invoke(uid="web_user", **post_kwargs)
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
        resp1, rec1 = self._invoke(uid="web_user", json={})
        resp2, rec2 = self._invoke(uid="web_user", json={})
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp1.get_json(), resp2.get_json())
        # each call touches exactly its own uid's sandbox, nothing else
        self.assert_paths_stay_in_sandbox(rec1)
        self.assert_paths_stay_in_sandbox(rec2)


# =========================================================================
# Session / uid mutations -- the ONLY user-controlled value that reaches
# the handler. This is where the real defects live.
# =========================================================================
class TestClearUidMutations(ClearFuzzBase):
    def test_default_uid_when_session_missing(self):
        # No uid in session -> falls back to "web_user"; must stay in sandbox.
        self._set_session_uid(_UNSET)
        rec = MagicMock()
        rec.exists.return_value = True
        with patch("app.os.makedirs", rec.makedirs), \
             patch("app.os.remove", rec.remove), \
             patch("app.os.path.exists", rec.exists):
            resp = self.client.post(ROUTE)
        self.assertEqual(resp.status_code, 200)
        rec.makedirs.assert_called_once_with("msal_caches/web_user", exist_ok=True)

    def test_nonexistent_uid_is_graceful(self):
        # Unknown-but-benign uid: no cache file present -> success, no crash.
        self._set_session_uid("totally-unknown-uid-xyz")
        rec = MagicMock()
        rec.exists.return_value = False
        with patch("app.os.makedirs", rec.makedirs), \
             patch("app.os.remove", rec.remove), \
             patch("app.os.path.exists", rec.exists):
            resp = self.client.post(ROUTE)
        self.assertEqual(resp.status_code, 200)
        rec.remove.assert_not_called()

    # ---- BUG 1: path traversal via unsanitised uid ------------------------
    def test_path_traversal_uid_must_not_escape_sandbox(self):
        """
        session['uid'] is set verbatim from ?uid= at the `/` route with no
        validation, then interpolated into a filesystem path that is passed to
        os.makedirs()/os.remove(). A uid of '../../..' escapes msal_caches and
        lets a caller create directories / DELETE arbitrary files outside the
        intended sandbox. A robust handler MUST reject or neutralise the
        traversal. This assertion pins the CORRECT behaviour and therefore
        FAILS against the current code (documents the vuln).
        """
        resp, rec = self._invoke(uid="../../../../tmp/evil-clear-target")
        self.assert_no_stacktrace(resp)
        # The dangerous fact: the handler happily acted on a path OUTSIDE the sandbox.
        self.assert_paths_stay_in_sandbox(rec)

    def test_path_traversal_null_byte_uid(self):
        # Null byte in a path component is a classic truncation trick; must not
        # reach os.* unsanitised. Documents the same missing-validation bug.
        resp, rec = self._invoke(uid="../secret\x00")
        self.assert_no_stacktrace(resp)
        self.assert_paths_stay_in_sandbox(rec)

    # ---- BUG 2 + 3: fail-open status + internal-error leak -----------------
    def test_error_path_fails_closed_and_hides_internals(self):
        """
        The bare `except Exception as e: return jsonify({"error": str(e)})`
        returns HTTP 200 (fail-OPEN) and echoes the raw exception text back to
        the client (internal-detail leak). Simulate a filesystem error and
        assert the CORRECT behaviour: a 4xx/5xx status AND no raw exception
        text in the body. Both assertions FAIL against current code.
        """
        self._set_session_uid("web_user")
        secret = "OSError: [Errno 13] Permission denied: '/private/secret/path'"
        with patch("app.os.makedirs", side_effect=OSError(13, secret)), \
             patch("app.os.remove"), \
             patch("app.os.path.exists", return_value=False):
            resp = self.client.post(ROUTE)
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
        Even setting the status-code bug aside, a caller must be able to tell a
        clear FAILED. Current code returns {"error": ...} with no `success:false`
        AND HTTP 200 -- a naive client that only checks `resp.ok` treats a failed
        clear as success. Assert the response signals failure.
        """
        self._set_session_uid("web_user")
        with patch("app.os.makedirs", side_effect=OSError("boom")), \
             patch("app.os.remove"), \
             patch("app.os.path.exists", return_value=False):
            resp = self.client.post(ROUTE)
        data = resp.get_json() or {}
        failed = (resp.status_code >= 400) or (data.get("success") is False)
        self.assertTrue(failed, "failed clear is not distinguishable from success")


if __name__ == "__main__":
    unittest.main(verbosity=2)
