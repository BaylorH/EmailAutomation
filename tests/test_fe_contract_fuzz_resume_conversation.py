"""
Adversarial frontend-contract fuzz for POST /api/resume-conversation.

Route contract (from app.py api_resume_conversation and the sibling
/api/stop-conversation fetch in email-admin-ui ConversationsPanel.jsx):
    body = { uid: str, threadId: str, clientId?: str }

The handler:
  - reads users/{uid}/threads/{threadId} via email_automation.clients._fs
  - if status == "paused": flips status to "active" (update_thread_status,
    which writes via email_automation.messaging._fs), resets followUpStatus,
    and highlights the sheet row yellow (email_automation.sheets.highlight_row)
  - otherwise short-circuits idempotently.

This route performs NO email send. There is nonetheless a hard guard below:
every send entrypoint (send_and_index_email / send_outboxes / send_email) is
patched with a recording MagicMock and asserted NEVER called, so a regression
that wires a send into this path would fail loudly and can never email a
non-allowlisted recipient.

EVERY external boundary is faked (Firestore _fs on both clients+messaging,
_get_client_config, highlight_row). Nothing real is touched.

The Firestore fake mimics the two real google-cloud-firestore invariants that
matter for input validation:
  * CollectionReference.document(id) raises TypeError if id is not a str
  * a document id containing "/" yields an invalid (odd-length) path -> ValueError
These are real client-library behaviors, so a 500 they induce reflects a real
handler gap (untrusted uid/threadId used directly in path construction with no
validation), not a fake artifact.
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
THREAD_KEY = ("users", "u1", "threads", "t1")


# --------------------------------------------------------------------------
# Faithful-ish in-memory Firestore fake
# --------------------------------------------------------------------------
class FakeSnap:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _DocPath:
    def __init__(self, store, path):
        self.store = store
        self.path = path

    def collection(self, name):
        return FakeCollection(self.store, self.path + (name,))

    def get(self):
        return FakeSnap(self.store.get(self.path))

    def update(self, data):
        # record every write so tests can assert (no) mutation
        self.store.setdefault("_updates", []).append((self.path, dict(data)))
        cur = self.store.get(self.path)
        if cur is None:
            raise Exception("404: no document to update")
        cur.update(dict(data))


class FakeCollection:
    def __init__(self, store, prefix):
        self.store = store
        self.prefix = prefix

    def document(self, doc_id=None):
        # Mirror real google-cloud-firestore path validation.
        if not isinstance(doc_id, str):
            raise TypeError(
                f"document id must be str, got {type(doc_id).__name__}"
            )
        if "/" in doc_id:
            raise ValueError(
                "A document must have an even number of path elements"
            )
        return _DocPath(self.store, self.prefix + (doc_id,))


class FakeFS:
    def __init__(self, store):
        self.store = store

    def collection(self, name):
        return FakeCollection(self.store, (name,))


def make_store(status="paused"):
    """Fresh store with a single thread doc (or none if status is None)."""
    store = {}
    if status is not None:
        store[THREAD_KEY] = {
            "status": status,
            "clientId": "c1",
            "rowNumber": 5,
            "followUpStatus": "paused",
        }
    return store


class ResumeConversationFuzzBase(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()
        # The route is now @verify_firebase_token: patch the Admin SDK verifier
        # (minting the SAME uid the in-memory store is keyed on) and attach a
        # Bearer header to every request. Identity is the token uid; the body uid
        # is ignored for the Firestore path.
        self._p_verify = patch(
            "firebase_admin.auth.verify_id_token", return_value={"uid": "u1"}
        )
        self._p_verify.start()
        self.addCleanup(self._p_verify.stop)
        self.client.environ_base["HTTP_AUTHORIZATION"] = "Bearer testtoken"

    def _invoke(self, payload=None, *, raw=None,
                content_type="application/json", store_status="paused"):
        """POST once against fully-faked boundaries. Returns
        (response, store, highlight_mock, send_mocks)."""
        store = make_store(store_status)
        fs = FakeFS(store)
        get_cfg = MagicMock(return_value=("sheet123", None, None))
        highlight = MagicMock()
        send_and_index = MagicMock()
        send_outboxes = MagicMock()
        send_email = MagicMock()

        with patch("email_automation.clients._fs", fs), \
             patch("email_automation.messaging._fs", fs), \
             patch("email_automation.clients._get_client_config", get_cfg), \
             patch("email_automation.sheets.highlight_row", highlight), \
             patch("email_automation.email.send_and_index_email", send_and_index), \
             patch("email_automation.email.send_outboxes", send_outboxes), \
             patch("email_automation.email.send_email", send_email):
            if raw is not None:
                resp = self.client.post(
                    "/api/resume-conversation", data=raw, content_type=content_type
                )
            else:
                resp = self.client.post("/api/resume-conversation", json=payload)

        send_mocks = {
            "send_and_index_email": send_and_index,
            "send_outboxes": send_outboxes,
            "send_email": send_email,
        }
        return resp, store, highlight, send_mocks

    def assert_no_send(self, send_mocks):
        for name, m in send_mocks.items():
            self.assertFalse(
                m.called,
                f"{name} was called on /api/resume-conversation — this route "
                f"must never send email (recipient guard: {ALLOWED_RECIPIENTS})",
            )

    @staticmethod
    def writes(store):
        return store.get("_updates", [])


# --------------------------------------------------------------------------
# Happy path + benign behavior (expected GREEN — documents correct behavior)
# --------------------------------------------------------------------------
class TestHappyAndBenign(ResumeConversationFuzzBase):
    def test_happy_path_resumes_paused_thread(self):
        resp, store, highlight, send = self._invoke(
            {"uid": "u1", "threadId": "t1", "clientId": "c1"}, store_status="paused"
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["newStatus"], "active")
        # status flipped + followUpStatus reset => at least 2 writes recorded
        self.assertGreaterEqual(len(self.writes(store)), 2)
        self.assertEqual(store[THREAD_KEY]["status"], "active")
        self.assertEqual(store[THREAD_KEY]["followUpStatus"], "waiting")
        self.assertTrue(highlight.called)  # row highlighted yellow
        self.assert_no_send(send)

    def test_idempotent_when_already_active(self):
        # Not paused -> must short-circuit with NO writes / no highlight / no send.
        resp, store, highlight, send = self._invoke(
            {"uid": "u1", "threadId": "t1", "clientId": "c1"}, store_status="active"
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["success"])
        self.assertIn("already", body["message"].lower())
        self.assertEqual(len(self.writes(store)), 0)
        self.assertFalse(highlight.called)
        self.assert_no_send(send)

    def test_idempotent_when_already_stopped(self):
        resp, store, highlight, send = self._invoke(
            {"uid": "u1", "threadId": "t1"}, store_status="stopped"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.writes(store)), 0)
        self.assert_no_send(send)

    def test_thread_not_found(self):
        resp, store, highlight, send = self._invoke(
            {"uid": "u1", "threadId": "t1"}, store_status=None
        )
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(resp.get_json()["success"])
        self.assertEqual(len(self.writes(store)), 0)
        self.assert_no_send(send)

    def test_extra_unexpected_fields_ignored(self):
        resp, store, highlight, send = self._invoke(
            {"uid": "u1", "threadId": "t1", "clientId": "c1",
             "evil": "x", "__proto__": "y", "admin": True},
            store_status="paused",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["success"])
        self.assert_no_send(send)

    def test_oversized_threadId_is_treated_as_not_found_not_crash(self):
        resp, store, highlight, send = self._invoke(
            {"uid": "u1", "threadId": "A" * 10240}, store_status="paused"
        )
        # Unknown 10KB id -> not the seeded thread -> graceful 404, no crash.
        self.assertLess(resp.status_code, 500)
        self.assertEqual(len(self.writes(store)), 0)
        self.assert_no_send(send)


# --------------------------------------------------------------------------
# Required-field / empty / null handling (expected GREEN — fail-closed 400)
# --------------------------------------------------------------------------
class TestRequiredFields(ResumeConversationFuzzBase):
    def _expect_400(self, payload):
        resp, store, highlight, send = self._invoke(payload, store_status="paused")
        self.assertEqual(
            resp.status_code, 400,
            f"expected 400 fail-closed for {payload}, got {resp.status_code}: "
            f"{resp.get_json()}",
        )
        self.assertFalse(resp.get_json()["success"])
        self.assertEqual(len(self.writes(store)), 0)
        self.assertFalse(highlight.called)
        self.assert_no_send(send)

    def test_missing_uid(self):
        self._expect_400({"threadId": "t1"})

    def test_missing_threadId(self):
        self._expect_400({"uid": "u1"})

    def test_missing_both(self):
        self._expect_400({})  # empty dict -> "No JSON data provided"/missing -> 400

    def test_null_uid(self):
        self._expect_400({"uid": None, "threadId": "t1"})

    def test_null_threadId(self):
        self._expect_400({"uid": "u1", "threadId": None})

    def test_empty_uid(self):
        self._expect_400({"uid": "", "threadId": "t1"})

    def test_empty_threadId(self):
        self._expect_400({"uid": "u1", "threadId": ""})


# --------------------------------------------------------------------------
# CRITICAL SECURITY INVARIANT (expected GREEN):
# no bad input may ever cause a state mutation or an email send.
# This holds even for the inputs that (buggily) 500 below.
# --------------------------------------------------------------------------
class TestNoMutationOrSendOnBadInput(ResumeConversationFuzzBase):
    BAD_INPUTS = [
        ("int_uid", {"uid": 123, "threadId": "t1"}, None, "application/json"),
        ("int_threadId", {"uid": "u1", "threadId": 999}, None, "application/json"),
        ("list_threadId", {"uid": "u1", "threadId": [1, 2]}, None, "application/json"),
        ("dict_uid", {"uid": {"a": 1}, "threadId": "t1"}, None, "application/json"),
        ("bool_uid", {"uid": True, "threadId": "t1"}, None, "application/json"),
        ("slash_threadId", {"uid": "u1", "threadId": "../../etc/passwd"}, None, "application/json"),
        ("file_uri_threadId", {"uid": "u1", "threadId": "file:///etc/passwd"}, None, "application/json"),
        ("placeholder_threadId", {"uid": "u1", "threadId": "[NAME]/[BROKER]"}, None, "application/json"),
        ("script_tag", {"uid": "u1", "threadId": "<script>alert(1)</script>/x"}, None, "application/json"),
        ("newline_uid", {"uid": "u1\n\r evil", "threadId": "t1"}, None, "application/json"),
        ("unicode_threadId", {"uid": "u1", "threadId": "‮t1"}, None, "application/json"),
        ("json_array_body", None, "[1,2,3]", "application/json"),
        ("json_string_body", None, '"hello"', "application/json"),
        ("json_number_body", None, "123", "application/json"),
        ("json_bool_body", None, "true", "application/json"),
        ("json_null_body", None, "null", "application/json"),
        ("malformed_json", None, "{not json}", "application/json"),
        ("non_json_content_type", None, "uid=u1&threadId=t1",
         "application/x-www-form-urlencoded"),
        ("empty_body_no_ct", None, "", "text/plain"),
    ]

    def test_no_mutation_or_send_for_any_bad_input(self):
        failures = []
        for name, payload, raw, ct in self.BAD_INPUTS:
            resp, store, highlight, send = self._invoke(
                payload, raw=raw, content_type=ct, store_status="paused"
            )
            # (1) never mutate the thread on bad input
            if self.writes(store):
                failures.append(f"{name}: unexpected writes {self.writes(store)}")
            # (2) never highlight (side effect)
            if highlight.called:
                failures.append(f"{name}: highlight_row called")
            # (3) never send email
            for sname, m in send.items():
                if m.called:
                    failures.append(f"{name}: {sname} called")
            # (4) body, if JSON, must not claim success
            body = resp.get_json(silent=True)
            if isinstance(body, dict) and body.get("success") is True:
                failures.append(f"{name}: success=True on bad input")
        self.assertEqual(failures, [], "; ".join(failures))


# --------------------------------------------------------------------------
# BUG BATTERY (expected RED): handler converts client errors into 500s that
# leak internal exception text, instead of returning a clean 4xx.
# The security invariants above still hold (no send/mutation), so these are
# error-handling / info-leak defects, not data-integrity ones.
# Assertions pin the CORRECT behavior (4xx), so they fail until fixed.
# --------------------------------------------------------------------------
class TestFailClosedStatusCodes(ResumeConversationFuzzBase):
    def _assert_client_error(self, label, *, payload=None, raw=None,
                             content_type="application/json"):
        resp, store, highlight, send = self._invoke(
            payload, raw=raw, content_type=content_type, store_status="paused"
        )
        # invariant that DOES hold — record for clarity
        self.assertEqual(len(self.writes(store)), 0, f"{label}: mutated state")
        self.assert_no_send(send)
        # the bug: bad *input* should be a 4xx, not a 5xx
        self.assertLess(
            resp.status_code, 500,
            f"{label}: bad input returned {resp.status_code} (server error) "
            f"instead of a 4xx client error. Body leaks internal text: "
            f"{resp.get_json()}",
        )
        # and must not leak internal exception text
        body = resp.get_json(silent=True) or {}
        err = str(body.get("error", ""))
        for leak in ("object has no attribute", "Bad Request:",
                     "Unsupported Media Type", "path elements",
                     "document id must be str", "Traceback"):
            self.assertNotIn(
                leak, err,
                f"{label}: response leaks internal error detail: {err!r}",
            )

    # --- non-object top-level JSON -> AttributeError -> 500 (BUG) ---
    def test_json_array_body_is_client_error(self):
        self._assert_client_error("json_array_body", raw="[1,2,3]")

    def test_json_string_body_is_client_error(self):
        self._assert_client_error("json_string_body", raw='"hello"')

    def test_json_number_body_is_client_error(self):
        self._assert_client_error("json_number_body", raw="123")

    def test_json_bool_body_is_client_error(self):
        self._assert_client_error("json_bool_body", raw="true")

    # --- malformed / wrong content-type -> werkzeug HTTPException swallowed
    #     by `except Exception` -> re-emitted as 500 (BUG) ---
    def test_malformed_json_is_client_error(self):
        self._assert_client_error("malformed_json", raw="{not json}")

    def test_non_json_content_type_is_client_error(self):
        self._assert_client_error(
            "non_json_content_type", raw="uid=u1&threadId=t1",
            content_type="application/x-www-form-urlencoded",
        )

    # --- untrusted id types / path injection used directly in Firestore path
    #     construction -> TypeError/ValueError -> 500 (BUG) ---
    def test_int_uid_is_client_error(self):
        self._assert_client_error("int_uid", payload={"uid": 123, "threadId": "t1"})

    def test_list_threadId_is_client_error(self):
        self._assert_client_error("list_threadId",
                                  payload={"uid": "u1", "threadId": [1, 2]})

    def test_slash_threadId_path_injection_is_client_error(self):
        self._assert_client_error(
            "slash_threadId", payload={"uid": "u1", "threadId": "../../etc/passwd"}
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
