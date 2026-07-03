"""
Adversarial front-end contract fuzz for POST /api/upload  (app.py:514 api_upload).

WHAT THIS ROUTE ACTUALLY DOES
-----------------------------
Despite the "Campaign upload/launch (Add Client)" feature label, the real
`/api/upload` handler does NOT parse the request body at all. It:
  1. reads `uid = session.get("uid", "web_user")`   (user-controlled via GET /?uid=...)
  2. builds  user_dir  = f"msal_caches/{uid}"
             cache_file = f"{user_dir}/msal_token_cache.bin"
  3. os.makedirs(user_dir, exist_ok=True)
  4. if the cache file is missing -> returns {"error": "..."} (HTTP 200)
  5. else calls firebase_helpers.upload_token(...) -> a network POST to Firebase.

So the interesting attack surface is the SESSION `uid` (which the front-end sets
from the query string on the index route, unsanitised), NOT the JSON body.

EXTERNAL BOUNDARIES FAKED HERE (nothing real happens):
  * app.upload_token          -> MagicMock  (the Firebase network POST / "send")
  * email_automation.clients._fs -> MagicMock (defensive; route doesn't use it)

The MagicMock RECORDS every call so we can assert the boundary is never hit with
an unexpected / malicious target and that no real upload leaves the process.
"""

import os
import sys
import json
import shutil
import unittest
import uuid
from unittest.mock import MagicMock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

REPO = "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation"
sys.path.insert(0, REPO)

import app as appmod  # noqa: E402


# adversarial body battery -- the handler ignores the body, so these must all be
# handled without a crash / stack-trace leak / unexpected upload.
MUTATIONS = [
    ("missing_all_fields", {"__json__": {}}),
    ("null_values", {"__json__": {"uid": None, "clientId": None, "threadId": None}}),
    ("empty_strings", {"__json__": {"uid": "", "clientId": "", "threadId": ""}}),
    ("wrong_type_int", {"__json__": {"uid": 12345}}),
    ("wrong_type_array", {"__json__": {"uid": [1, 2, 3]}}),
    ("wrong_type_object", {"__json__": {"uid": {"nested": "obj"}}}),
    ("wrong_type_bool", {"__json__": {"uid": True}}),
    ("oversized_string", {"__json__": {"uid": "A" * 10240}}),
    ("path_traversal_field", {"__json__": {"uid": "../../../../etc/passwd"}}),
    ("file_uri_field", {"__json__": {"uid": "file:///etc/passwd"}}),
    ("placeholder_name", {"__json__": {"clientId": "[NAME]"}}),
    ("placeholder_broker", {"__json__": {"clientId": "[BROKER]"}}),
    ("script_tag", {"__json__": {"uid": "<script>alert(1)</script>"}}),
    ("newlines", {"__json__": {"uid": "a\r\nb\nc"}}),
    ("unicode", {"__json__": {"uid": "\U0001f4a5\u202eabc d\u00e9"}}),
    ("extra_unexpected_fields", {"__json__": {"uid": "x", "evil": "y", "admin": True, "__proto__": {}}}),
    ("nonexistent_ids", {"__json__": {"uid": "does-not-exist", "clientId": "nope", "threadId": "ghost"}}),
    ("non_json_body", {"data": "this is not json {{{", "content_type": "text/plain"}),
    ("empty_raw_body", {"data": "", "content_type": "application/json"}),
    ("malformed_json", {"data": '{"uid": ', "content_type": "application/json"}),
]


class UploadFuzz(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # os.makedirs / os.path.exists in the handler are relative to CWD.
        os.chdir(REPO)
        cls.msal_root = os.path.join(REPO, "msal_caches")

    def setUp(self):
        self.client = appmod.app.test_client()
        # Fake the Firebase "send" boundary + Firestore. Records every call.
        self.upload_mock = MagicMock(name="upload_token")
        self._p1 = patch.object(appmod, "upload_token", self.upload_mock)
        self._p2 = patch("email_automation.clients._fs", MagicMock())
        self._p1.start()
        self._p2.start()
        self._cleanup_paths = []

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        for p in self._cleanup_paths:
            shutil.rmtree(p, ignore_errors=True)

    # ---- helpers -------------------------------------------------------
    def _set_uid(self, uid):
        with self.client.session_transaction() as sess:
            sess["uid"] = uid

    def _make_cache(self, uid):
        """Create a real (empty) token cache so the handler reaches upload_token."""
        d = os.path.join(REPO, "msal_caches", uid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "msal_token_cache.bin"), "wb") as f:
            f.write(b"fake-token-cache")
        top = os.path.join(REPO, "msal_caches", uid.split("/")[0])
        self._cleanup_paths.append(top)
        return d

    def _post(self, mut):
        if "__json__" in mut:
            return self.client.post("/api/upload", json=mut["__json__"])
        return self.client.post(
            "/api/upload", data=mut["data"], content_type=mut["content_type"]
        )

    # ---- happy path ----------------------------------------------------
    def test_happy_path_valid_upload(self):
        uid = "fuzz_safe_" + uuid.uuid4().hex[:8]
        self._make_cache(uid)
        self._set_uid(uid)

        r = self.client.post("/api/upload", json={})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body.get("success"), f"expected success, got {body}")

        # The Firebase boundary was hit exactly once, with the SAFE uid.
        self.assertEqual(self.upload_mock.call_count, 1)
        _, kwargs = self.upload_mock.call_args
        self.assertEqual(kwargs.get("user_id"), uid)
        self.assertTrue(kwargs.get("input_file", "").endswith(f"msal_caches/{uid}/msal_token_cache.bin"))

    def test_duplicate_retry_no_double_effect_crash(self):
        uid = "fuzz_dup_" + uuid.uuid4().hex[:8]
        self._make_cache(uid)
        self._set_uid(uid)
        r1 = self.client.post("/api/upload", json={})
        r2 = self.client.post("/api/upload", json={})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r1.get_json().get("success"))
        self.assertTrue(r2.get_json().get("success"))
        # Re-uploading the same cache is an idempotent overwrite (2 calls, both safe uid).
        self.assertEqual(self.upload_mock.call_count, 2)

    # ---- adversarial body battery (handler ignores body -> must stay robust) ----
    def test_body_mutations_are_robust(self):
        for name, mut in MUTATIONS:
            with self.subTest(mutation=name):
                uid = "fuzz_nocache_" + uuid.uuid4().hex[:8]  # no cache file exists
                self._set_uid(uid)
                r = self._post(mut)
                text = r.get_data(as_text=True)

                # 1. no unhandled 500
                self.assertNotEqual(r.status_code, 500,
                                    f"{name}: 500 on ignored-body input -> {text[:200]}")
                # 2. no stack-trace leak
                self.assertNotIn("Traceback (most recent call last)", text,
                                 f"{name}: stack trace leaked")
                self.assertNotIn('File "', text, f"{name}: file path / traceback leaked")
                # 3. body is ignored + no cache -> upload boundary MUST NOT fire
                self.assertEqual(self.upload_mock.call_count, 0,
                                 f"{name}: upload_token fired on a request that should not send")
                # 4. fail-closed at the app layer (no success:true)
                body = r.get_json(silent=True)
                if isinstance(body, dict):
                    self.assertNotEqual(body.get("success"), True,
                                        f"{name}: reported success with no cache file")

    # ---- BUG 1: path traversal via session uid (escapes msal_caches sandbox) ----
    def test_uid_path_traversal_does_not_escape_sandbox(self):
        sentinel = "fuzz_traversal_sentinel_" + uuid.uuid4().hex[:8]
        escaped_dir = os.path.join(REPO, sentinel)  # sibling of msal_caches, OUTSIDE it
        self._cleanup_paths.append(escaped_dir)
        self.assertFalse(os.path.exists(escaped_dir))

        # uid the front-end could set via GET /?uid=../<x>
        self._set_uid("../" + sentinel)
        r = self.client.post("/api/upload", json={})

        # CORRECT behaviour: reject the traversal, create nothing outside msal_caches.
        # Current handler runs os.makedirs("msal_caches/../<sentinel>") -> escapes.
        self.assertFalse(
            os.path.isdir(escaped_dir),
            "PATH TRAVERSAL: os.makedirs created a directory OUTSIDE msal_caches/ "
            "from an attacker-controlled uid (../sentinel). uid is set unsanitised "
            "from GET /?uid= and flows into makedirs + the Firebase object path.",
        )
        self.assertNotEqual(r.status_code, 500)

    # ---- BUG 2: raw internal exception text leaked to the client -------------
    def test_upload_exception_is_not_leaked_to_client(self):
        uid = "fuzz_leak_" + uuid.uuid4().hex[:8]
        self._make_cache(uid)
        self._set_uid(uid)
        secret = "INTERNAL_SECRET_/srv/creds/service-account.json:line42"
        self.upload_mock.side_effect = Exception(secret)

        r = self.client.post("/api/upload", json={})
        text = r.get_data(as_text=True)

        # CORRECT: generic error, no internal detail. Current: returns str(e) verbatim.
        self.assertNotIn(
            secret, text,
            "INFO LEAK: handler reflects raw str(exception) from the Firebase boundary "
            "back to the client (app.py:528-529 `return jsonify({'error': str(e)})`).",
        )

    # ---- BUG 3: error responses use HTTP 200 (fail-open status) --------------
    def test_error_uses_4xx_status(self):
        uid = "fuzz_nostatus_" + uuid.uuid4().hex[:8]  # no cache -> error branch
        self._set_uid(uid)
        r = self.client.post("/api/upload", json={})
        body = r.get_json(silent=True) or {}
        self.assertIn("error", body)  # it is an error response...
        # CORRECT: an error response should carry a 4xx status, not 200.
        self.assertGreaterEqual(
            r.status_code, 400,
            "FAIL-OPEN STATUS: 'No token cache file found' error is returned with "
            "HTTP 200 (app.py:524). A client keying on HTTP status treats the failed "
            "upload as success.",
        )
        # And it must not have reached the Firebase boundary.
        self.assertEqual(self.upload_mock.call_count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
