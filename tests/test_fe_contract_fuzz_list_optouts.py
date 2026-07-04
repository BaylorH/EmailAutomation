"""
Adversarial frontend-contract fuzz for POST /api/list-optouts.

Handler (app.py api_list_optouts):
  - Required field: uid (string)
  - External boundary: email_automation.clients._fs (Firestore) ONLY.
        _fs.collection("users").document(uid).collection("optedOutContacts").stream()
  - Read-only: no send / refresh / write of any kind.

Every external boundary is faked so NOTHING real happens. Firestore is a
MagicMock whose .stream() returns fake docs; the send/refresh entrypoints are
patched with recording MagicMocks and asserted NEVER called (this route must
never send email).

Assertions pin the CORRECT behavior. Where the handler mishandles input
(returns HTTP 500 leaking internal error text for malformed / wrong-type
request bodies that should fail closed with 4xx), the assertion is written to
FAIL (red) so the bug is documented rather than papered over.
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


def make_fake_doc(doc_id, data):
    d = MagicMock()
    d.id = doc_id
    d.to_dict.return_value = data
    return d


def make_fake_fs(docs=None):
    """Fake Firestore whose optedOutContacts.stream() yields `docs`."""
    if docs is None:
        docs = []
    fake_fs = MagicMock()
    (
        fake_fs.collection.return_value.document.return_value.collection.return_value.stream
    ).return_value = list(docs)
    return fake_fs


# Recording mocks for every send/refresh entrypoint. This route must NEVER
# touch any of these; if it ever does, the recipient-guard assertion catches it.
SEND_TARGETS = [
    "email_automation.email.send_and_index_email",
    "email_automation.email.send_outboxes",
    "email_automation.email.send_email",
    "email_automation.processing.send_reply_in_thread",
]


class ListOptoutsFuzzBase(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()
        # The route is now @verify_firebase_token: patch the Admin SDK verifier
        # and attach a Bearer header to every request so the AUTHORISED path is
        # exercised. Identity is the token uid; the body uid is ignored.
        self._p_verify = patch(
            "firebase_admin.auth.verify_id_token", return_value={"uid": "u1"}
        )
        self._p_verify.start()
        self.addCleanup(self._p_verify.stop)
        self.client.environ_base["HTTP_AUTHORIZATION"] = "Bearer testtoken"
        self.send_mocks = []
        self._send_patchers = []
        for tgt in SEND_TARGETS:
            try:
                p = patch(tgt, MagicMock(name=tgt))
                m = p.start()
                self._send_patchers.append(p)
                self.send_mocks.append((tgt, m))
            except (AttributeError, ModuleNotFoundError):
                pass

    def tearDown(self):
        for p in self._send_patchers:
            p.stop()

    def assert_no_send(self):
        for tgt, m in self.send_mocks:
            self.assertEqual(
                m.call_count, 0, f"read-only route unexpectedly called send entrypoint {tgt}"
            )

    def assert_no_fs_write(self, fake_fs):
        """The whole call tree must never invoke a mutating Firestore method."""
        for call in fake_fs.mock_calls:
            name = call[0]
            leaf = name.split(".")[-1]
            self.assertNotIn(
                leaf,
                {"set", "update", "delete", "create", "add"},
                f"read-only route performed a Firestore write: {name}",
            )

    def assert_no_internal_leak(self, resp):
        """Response body must not leak Python/werkzeug internals or stack detail."""
        body = resp.get_data(as_text=True)
        for needle in [
            "object has no attribute",
            "Traceback",
            "line ",
            "Bad Request: The browser",
            "Unsupported Media Type",
        ]:
            self.assertNotIn(
                needle,
                body,
                f"response leaked internal error text ({needle!r}): {body[:200]}",
            )

    def assert_scoped_to_token_uid(self, fake_fs, body_uid, token_uid="u1"):
        """Prove the users/{uid} document was resolved with the VERIFIED token
        uid, and that the untrusted body uid never reached the Firestore path.
        A 200 with the right doc count does not, on its own, prove the body uid
        was discarded — this route's entire security value is that it does."""
        users_doc = fake_fs.collection.return_value.document
        users_doc.assert_any_call(token_uid)
        for call in users_doc.call_args_list:
            arg = call.args[0] if call.args else None
            self.assertNotEqual(
                arg, body_uid,
                f"body-supplied uid {body_uid!r} reached the Firestore path",
            )

    def post(self, **kw):
        return self.client.post("/api/list-optouts", **kw)


class HappyPath(ListOptoutsFuzzBase):
    def test_happy_path_with_records(self):
        docs = [
            make_fake_doc("id1", {"email": "a@x.com", "reason": "unsub", "optedOutAt": "t1"}),
            make_fake_doc("id2", {"email": "b@x.com", "reason": None, "optedOutAt": "t2"}),
        ]
        fake_fs = make_fake_fs(docs)
        with patch("email_automation.clients._fs", fake_fs):
            r = self.post(json={"uid": "user123"})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["count"], 2)
        self.assertEqual(len(body["optouts"]), 2)
        self.assertEqual(body["optouts"][0]["id"], "id1")
        self.assertEqual(body["optouts"][0]["email"], "a@x.com")
        self.assert_no_send()
        self.assert_no_fs_write(fake_fs)
        # The IDOR guarantee: the list was scoped to the verified token uid
        # ("u1"), NOT the body-supplied "user123".
        self.assert_scoped_to_token_uid(fake_fs, body_uid="user123")

    def test_foreign_body_uid_lists_only_token_uid(self):
        """A caller passing another tenant's uid must still only ever read their
        own (token uid's) opt-out collection — the body uid is ignored."""
        docs = [make_fake_doc("id1", {"email": "a@x.com", "reason": "unsub", "optedOutAt": "t1"})]
        fake_fs = make_fake_fs(docs)
        with patch("email_automation.clients._fs", fake_fs):
            r = self.post(json={"uid": "victim-tenant-uid"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["success"])
        self.assert_scoped_to_token_uid(fake_fs, body_uid="victim-tenant-uid")
        self.assert_no_send()

    def test_happy_path_empty(self):
        fake_fs = make_fake_fs([])
        with patch("email_automation.clients._fs", fake_fs):
            r = self.post(json={"uid": "user123"})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["optouts"], [])
        self.assert_no_send()


class RequiredFieldValidation(ListOptoutsFuzzBase):
    def test_empty_json_object(self):
        fake_fs = make_fake_fs([])
        with patch("email_automation.clients._fs", fake_fs):
            r = self.post(json={})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["success"])
        self.assert_no_send()

    def test_uid_null(self):
        fake_fs = make_fake_fs([])
        with patch("email_automation.clients._fs", fake_fs):
            r = self.post(json={"uid": None})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["success"])
        self.assert_no_send()

    def test_uid_empty_string(self):
        fake_fs = make_fake_fs([])
        with patch("email_automation.clients._fs", fake_fs):
            r = self.post(json={"uid": ""})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["success"])
        self.assert_no_send()


class WrongTypeUid(ListOptoutsFuzzBase):
    """A non-string uid should not crash or leak; ideally 4xx, at worst a clean
    200 on the fake. It must never 500 with an internal leak."""

    def _run(self, uid_val):
        fake_fs = make_fake_fs([])
        with patch("email_automation.clients._fs", fake_fs):
            r = self.post(json={"uid": uid_val})
        self.assertNotEqual(
            r.status_code, 500, f"uid={uid_val!r} produced a 500 (crash)"
        )
        self.assert_no_internal_leak(r)
        self.assert_no_send()
        self.assert_no_fs_write(fake_fs)
        return r

    def test_uid_int(self):
        self._run(12345)

    def test_uid_array(self):
        self._run(["a", "b"])

    def test_uid_object(self):
        self._run({"nested": "value"})

    def test_uid_bool(self):
        self._run(True)


class OversizedAndInjection(ListOptoutsFuzzBase):
    def _run(self, uid_val):
        fake_fs = make_fake_fs([])
        with patch("email_automation.clients._fs", fake_fs):
            r = self.post(json={"uid": uid_val})
        self.assertNotEqual(r.status_code, 500)
        self.assert_no_internal_leak(r)
        self.assert_no_send()
        self.assert_no_fs_write(fake_fs)
        return r

    def test_oversized_uid(self):
        self._run("A" * 10240)

    def test_path_traversal(self):
        self._run("../../../../etc/passwd")

    def test_file_uri(self):
        self._run("file:///etc/passwd")

    def test_placeholder_tokens(self):
        self._run("[NAME] [BROKER]")

    def test_script_tag(self):
        self._run("<script>alert(1)</script>")

    def test_newlines_and_unicode(self):
        self._run("line1\nline2\r\n\u202e\U0001f600  tail")


class NonexistentUid(ListOptoutsFuzzBase):
    def test_nonexistent_uid_returns_empty(self):
        fake_fs = make_fake_fs([])  # stream() -> [] == not found
        with patch("email_automation.clients._fs", fake_fs):
            r = self.post(json={"uid": "does-not-exist"})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["count"], 0)
        self.assert_no_send()


class Idempotency(ListOptoutsFuzzBase):
    def test_duplicate_retry_is_read_only_and_idempotent(self):
        docs = [make_fake_doc("id1", {"email": "a@x.com", "reason": "x", "optedOutAt": "t"})]
        fake_fs = make_fake_fs(docs)
        with patch("email_automation.clients._fs", fake_fs):
            r1 = self.post(json={"uid": "user123"})
            r2 = self.post(json={"uid": "user123"})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.get_json(), r2.get_json())
        self.assert_no_send()
        self.assert_no_fs_write(fake_fs)


class ExtraFields(ListOptoutsFuzzBase):
    def test_unexpected_extra_fields_ignored(self):
        fake_fs = make_fake_fs([])
        payload = {"uid": "user123", "threadId": "t", "evil": {"x": [1, 2]}, "admin": True}
        with patch("email_automation.clients._fs", fake_fs):
            r = self.post(json=payload)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["success"])
        self.assert_no_send()


class MalformedBody(ListOptoutsFuzzBase):
    """Malformed / non-JSON / wrong-shape bodies must fail CLOSED with a 4xx and
    must not leak internal error text. The handler currently returns HTTP 500
    with str(exception) for all of these -> BUG. These assertions pin it."""

    def test_malformed_json(self):
        fake_fs = make_fake_fs([])
        with patch("email_automation.clients._fs", fake_fs):
            r = self.post(data="{not valid json", content_type="application/json")
        self.assert_no_send()
        self.assert_no_internal_leak(r)  # BUG: leaks werkzeug "Bad Request" text
        self.assertLess(
            r.status_code, 500, f"malformed JSON should be 4xx, got {r.status_code}"
        )

    def test_wrong_content_type(self):
        fake_fs = make_fake_fs([])
        with patch("email_automation.clients._fs", fake_fs):
            r = self.post(data="hello", content_type="text/plain")
        self.assert_no_send()
        self.assert_no_internal_leak(r)  # BUG: leaks "Unsupported Media Type"
        self.assertLess(
            r.status_code, 500, f"non-JSON body should be 4xx, got {r.status_code}"
        )

    def test_no_body(self):
        fake_fs = make_fake_fs([])
        with patch("email_automation.clients._fs", fake_fs):
            r = self.post()
        self.assert_no_send()
        self.assert_no_internal_leak(r)  # BUG
        self.assertLess(
            r.status_code, 500, f"empty body should be 4xx, got {r.status_code}"
        )

    def test_json_array_body(self):
        fake_fs = make_fake_fs([])
        with patch("email_automation.clients._fs", fake_fs):
            r = self.post(json=[1, 2, 3])
        self.assert_no_send()
        # BUG: raises AttributeError("'list' object has no attribute 'get'")
        # -> 500 leaking the Python internal message.
        self.assert_no_internal_leak(r)
        self.assertLess(
            r.status_code, 500, f"JSON array body should be 4xx, got {r.status_code}"
        )

    def test_json_string_body(self):
        fake_fs = make_fake_fs([])
        with patch("email_automation.clients._fs", fake_fs):
            r = self.post(json="just-a-string")
        self.assert_no_send()
        # BUG: raises AttributeError("'str' object has no attribute 'get'") -> 500 leak.
        self.assert_no_internal_leak(r)
        self.assertLess(
            r.status_code, 500, f"JSON string body should be 4xx, got {r.status_code}"
        )

    def test_json_number_body(self):
        fake_fs = make_fake_fs([])
        with patch("email_automation.clients._fs", fake_fs):
            r = self.post(json=42)
        self.assert_no_send()
        self.assert_no_internal_leak(r)
        self.assertLess(
            r.status_code, 500, f"JSON number body should be 4xx, got {r.status_code}"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
