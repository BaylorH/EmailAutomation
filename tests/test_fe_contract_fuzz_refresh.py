"""
Adversarial frontend-contract fuzz for POST /api/refresh  (app.py:api_refresh).

WHAT THE HANDLER ACTUALLY IS
----------------------------
Despite the "Manual inbox/outbox sync" framing, /api/refresh is a Microsoft
MSAL *token refresh* endpoint. It:
  - reads uid from the SERVER-SIDE session (session.get("uid", "web_user")) --
    it does NOT read any field from the request body,
  - touches the filesystem: msal_caches/<uid>/msal_token_cache.bin (read+write),
  - builds a ConfidentialClientApplication and calls
    get_accounts() / acquire_token_silent(force_refresh=True).

There is NO email-send boundary, NO Firestore write, NO Sheets call in this
handler, so no real mail can go out. We still patch every external boundary:
  * app.ConfidentialClientApplication  -> MagicMock (blocks the real MS Graph net call)
  * email_automation.clients._fs        -> MagicMock (defensive; unused here)
  * cwd is redirected to a temp dir      -> the msal_caches/ write is isolated

Because the handler ignores the request body, the body-mutation battery proves a
robustness property (no body can crash it or change its behavior). The three
correctness tests at the bottom pin real DEFECTS in the error paths.
"""
import os
import sys
import json
import shutil
import tempfile
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
CACHE_REL = "msal_caches/web_user/msal_token_cache.bin"


def make_msal_mock(accounts="__default__", token_result="__default__"):
    """A MagicMock standing in for ConfidentialClientApplication (the class)."""
    cls = MagicMock(name="ConfidentialClientApplication")
    inst = cls.return_value
    inst.get_accounts.return_value = (
        [{"username": "acct@example.com"}] if accounts == "__default__" else accounts
    )
    inst.acquire_token_silent.return_value = (
        {"access_token": "FAKE-TOKEN", "expires_in": 3600}
        if token_result == "__default__"
        else token_result
    )
    return cls


class RefreshContractFuzz(unittest.TestCase):
    def setUp(self):
        # Isolate the msal_caches/ filesystem write into a throwaway cwd.
        self.tmp = tempfile.mkdtemp(prefix="refresh_fuzz_")
        self._prev_cwd = os.getcwd()
        os.chdir(self.tmp)

        self.client = appmod.app.test_client()

        # Defensive: fake Firestore (this handler does not use it, but the task
        # requires every external boundary be faked so nothing real can happen).
        self._fs_patch = patch("email_automation.clients._fs", MagicMock())
        self._fs_patch.start()

        # Block the real MS Graph network call. Default = happy token refresh.
        self.msal = make_msal_mock()
        self._msal_patch = patch.object(
            appmod, "ConfidentialClientApplication", self.msal
        )
        self._msal_patch.start()

        # The hardened /api/refresh requires a verified Firebase ID token; the
        # uid now comes from the token (not the session), so the verifier returns
        # the "web_user" uid the cache path (msal_caches/web_user/...) expects.
        self._verify_patch = patch(
            "firebase_admin.auth.verify_id_token", return_value={"uid": "web_user"}
        )
        self.verify_mock = self._verify_patch.start()
        self.AUTH = {"Authorization": "Bearer testtoken"}

    def tearDown(self):
        self._verify_patch.stop()
        self._msal_patch.stop()
        self._fs_patch.stop()
        os.chdir(self._prev_cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers -----------------------------------------------------------
    def _post(self, **kw):
        # Every authorised request carries the bearer header (verifier patched).
        headers = kw.pop("headers", None) or dict(self.AUTH)
        return self.client.post("/api/refresh", headers=headers, **kw)

    def _assert_no_stacktrace_no_500(self, resp):
        body = resp.get_data(as_text=True)
        self.assertNotEqual(
            resp.status_code, 500, f"unhandled 500: {body[:400]}"
        )
        for marker in ("Traceback (most recent call last)", 'File "', "raise "):
            self.assertNotIn(
                marker, body, f"stacktrace leaked in response: {body[:400]}"
            )

    def _assert_no_disallowed_send(self):
        """This handler has no send boundary. Prove nothing was asked to send
        mail and no disallowed recipient ever appears in the mocked call log."""
        calls = str(self.msal.mock_calls) + str(self.msal.return_value.mock_calls)
        self.assertNotIn("send_mail", calls.lower())
        self.assertNotIn("sendmail", calls.lower())
        # acquire_token_silent must only ever be called with force_refresh + scopes,
        # never with anything resembling a message/recipient payload.
        for c in self.msal.return_value.acquire_token_silent.call_args_list:
            self.assertNotIn("@", str(c.args))

    # ---- authentication ----------------------------------------------------
    def test_missing_token_is_rejected(self):
        """No Authorization header -> 401, no MSAL work, no cache written."""
        resp = self.client.post("/api/refresh", json={})
        self.assertEqual(resp.status_code, 401, resp.get_data(as_text=True))
        self.assertFalse(self.msal.return_value.acquire_token_silent.called)
        self.assertFalse(os.path.exists(CACHE_REL))

    def test_invalid_token_is_rejected(self):
        """Present-but-invalid bearer token -> 401, no MSAL work."""
        self.verify_mock.side_effect = ValueError("bad token")
        resp = self.client.post(
            "/api/refresh", json={}, headers={"Authorization": "Bearer nope"}
        )
        self.assertEqual(resp.status_code, 401, resp.get_data(as_text=True))
        self.assertFalse(self.msal.return_value.acquire_token_silent.called)

    # ---- happy path --------------------------------------------------------
    def test_happy_path(self):
        """Realistic valid call is a plain POST (no body needed). Expect success
        + the token cache file gets written + force_refresh was requested."""
        resp = self._post(json={})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data.get("success"), data)
        self.assertEqual(data.get("message"), "Token refreshed successfully")
        self.assertTrue(os.path.exists(CACHE_REL), "token cache file not written")
        _, kwargs = self.msal.return_value.acquire_token_silent.call_args
        self.assertTrue(kwargs.get("force_refresh"), "force_refresh not set")
        self._assert_no_disallowed_send()

    def test_happy_path_no_body_at_all(self):
        """No JSON body, no content-type -> still works (body is ignored)."""
        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json().get("success"))

    def test_idempotent_retry(self):
        """Same request twice -> idempotent, both succeed, single cache file."""
        r1 = self._post(json={})
        r2 = self._post(json={})
        self.assertTrue(r1.get_json().get("success"))
        self.assertTrue(r2.get_json().get("success"))
        self.assertTrue(os.path.exists(CACHE_REL))
        # No double/unexpected side effect: exactly one cache dir for web_user.
        self.assertEqual(sorted(os.listdir("msal_caches")), ["web_user"])
        self._assert_no_disallowed_send()

    # ---- adversarial body-mutation battery ---------------------------------
    # The handler ignores the request body, so each of these must be a no-op on
    # behavior: no 500, no stacktrace, valid JSON, no disallowed send. This is a
    # genuine robustness property (untrusted body cannot break or steer it).
    def test_body_mutation_battery(self):
        big = "A" * 10_240
        mutations = [
            ("empty_object", dict(json={})),
            ("null_uid", dict(json={"uid": None})),
            ("empty_uid", dict(json={"uid": ""})),
            ("uid_int", dict(json={"uid": 12345})),
            ("uid_array", dict(json={"uid": ["a", "b"]})),
            ("uid_object", dict(json={"uid": {"nested": 1}})),
            ("uid_bool", dict(json={"uid": True})),
            ("oversized_uid", dict(json={"uid": big})),
            ("path_traversal_uid", dict(json={"uid": "../../../../etc/passwd"})),
            ("file_scheme_uid", dict(json={"uid": "file:///etc/passwd"})),
            ("placeholder_uid", dict(json={"uid": "[NAME] [BROKER]"})),
            ("script_tag_uid", dict(json={"uid": "<script>alert(1)</script>"})),
            ("newline_uid", dict(json={"uid": "web\r\nuser\ninjected"})),
            ("unicode_uid", dict(json={"uid": "rtl-\u202eabc-emoji-\U0001f680"})),
            ("null_bytes_threadId", dict(json={"threadId": "t\x00id"})),
            ("extra_unexpected_fields", dict(json={"uid": "x", "evil": {"a": [1, 2]}, "z": 9})),
            ("array_body", dict(json=[1, 2, 3])),
            ("scalar_body", dict(json="just-a-string")),
            ("number_body", dict(json=42)),
            ("bool_body", dict(json=False)),
            ("malformed_json", dict(data="{not: valid json", content_type="application/json")),
            ("non_json_text", dict(data="hello world", content_type="text/plain")),
            ("empty_raw_body", dict(data="", content_type="application/json")),
        ]
        for name, kw in mutations:
            with self.subTest(mutation=name):
                resp = self._post(**kw)
                self._assert_no_stacktrace_no_500(resp)
                # Handler always returns jsonify(...) -> parseable JSON object.
                data = resp.get_json(silent=True)
                self.assertIsInstance(
                    data, dict, f"{name}: non-JSON-object response {resp.get_data(as_text=True)[:200]}"
                )
                # Body is ignored -> behavior identical to happy path.
                self.assertTrue(
                    data.get("success"),
                    f"{name}: body mutation changed behavior -> {data}",
                )
                self._assert_no_disallowed_send()
                # Untrusted uid must NOT have escaped into a real path on disk.
                self.assertFalse(os.path.exists("/etc/passwd.bin"))
                self.assertEqual(sorted(os.listdir("msal_caches")), ["web_user"])

    # ---- nonexistent identity / not-found ----------------------------------
    def test_no_accounts_present(self):
        """MSAL has no cached accounts (unauthenticated). Handler returns an
        error body. NOTE the fail-open status is pinned in the BUG test below."""
        self.msal.return_value.get_accounts.return_value = []
        resp = self._post(json={})
        self._assert_no_stacktrace_no_500(resp)
        data = resp.get_json()
        self.assertIn("error", data)
        # Hardened contract: an unauthenticated MSAL state fails closed with
        # success:false (was previously a bare {"error": ...} 200 fail-open).
        self.assertFalse(data.get("success"))
        # No cache written when there was nothing to refresh.
        self.assertFalse(os.path.exists(CACHE_REL))
        self._assert_no_disallowed_send()

    # =======================================================================
    #  CORRECTNESS TESTS THAT PIN REAL DEFECTS (expected RED)
    # =======================================================================

    def test_BUG_no_accounts_returns_http_200_fail_open(self):
        """BUG: the 'No accounts found' error is returned with HTTP 200 and no
        success:false field. A frontend checking response.ok (HTTP status) treats
        an auth failure as success -> fail-open. Error conditions must be 4xx."""
        self.msal.return_value.get_accounts.return_value = []
        resp = self._post(json={})
        data = resp.get_json()
        self.assertIn("error", data)
        self.assertGreaterEqual(
            resp.status_code,
            400,
            "BUG: 'No accounts found' error returned with HTTP 200 (fail-open); "
            "should be a 4xx.",
        )

    def test_BUG_none_token_result_leaks_internal_attributeerror(self):
        """BUG: when acquire_token_silent() returns None (the normal 'silent
        refresh not possible, interaction required' case), the else-branch runs
        result.get('error_description', ...) on None -> AttributeError, which is
        caught and returned to the client as
        {"error": "'NoneType' object has no attribute 'get'"}. The user gets a
        nonsensical internal Python error instead of a clean 're-auth needed'."""
        self.msal.return_value.acquire_token_silent.return_value = None
        resp = self._post(json={})
        self._assert_no_stacktrace_no_500(resp)
        body = json.dumps(resp.get_json())
        self.assertNotIn(
            "NoneType", body,
            "BUG: else-branch calls .get() on a None token result, leaking "
            "\"'NoneType' object has no attribute 'get'\" to the client.",
        )
        self.assertNotIn("has no attribute", body, body)

    def test_BUG_internal_exception_leaks_message_and_returns_200(self):
        """BUG: the top-level `except Exception as e: return jsonify({"error":
        str(e)})` (a) echoes the raw internal exception string to the client
        (information disclosure) and (b) returns it with HTTP 200 (fail-open)."""
        self.msal.return_value.acquire_token_silent.side_effect = RuntimeError(
            "SECRET-INTERNAL /var/secrets/token.bin corrupt"
        )
        resp = self._post(json={})
        self._assert_no_stacktrace_no_500(resp)
        body = json.dumps(resp.get_json())
        self.assertNotIn(
            "SECRET-INTERNAL", body,
            "BUG: raw internal exception text leaked to the client via str(e).",
        )
        self.assertGreaterEqual(
            resp.status_code, 400,
            "BUG: internal exception returned with HTTP 200 (fail-open); "
            "should be a 5xx.",
        )
        self._assert_no_disallowed_send()


if __name__ == "__main__":
    unittest.main()
