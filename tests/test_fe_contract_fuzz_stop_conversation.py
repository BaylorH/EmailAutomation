"""
Frontend-contract adversarial fuzz for POST /api/stop-conversation.

Feature: Stop button in ConversationsPanel (email-admin-ui).
Real frontend payload (ConversationsPanel.jsx performStopConversation):
    { uid: <firebase uid str>, threadId: <thread doc id str>, clientId: <client id str|undefined> }

Every external boundary is faked so NOTHING real happens:
  - email_automation.clients._fs            (Firestore used by the handler)
  - email_automation.messaging._fs          (Firestore used by update_thread_status)
  - email_automation.clients._get_client_config (sheet lookup)
  - email_automation.sheets.clear_row_highlight  (Google Sheets write)
  - send entrypoints (send_and_index_email / send_outboxes / send_email /
    exponential_backoff_request) are patched and asserted NEVER called — this
    route must not send any email to any recipient.

Bugs are pinned with assertions that describe CORRECT behavior. Where the
handler is NOT robust, the assertion is left RED on purpose (do not weaken).
"""
import os
import json
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

import app as appmod  # noqa: E402

ROUTE = "/api/stop-conversation"

ALLOWED_RECIPIENTS = {"bp21harrison@gmail.com", "baylor.freelance@outlook.com"}

# Substrings that indicate an internal Python/werkzeug error leaked to the client.
LEAK_SIGNATURES = (
    "object has no attribute",
    "not subscriptable",
    "Unsupported Media Type",
    "could not understand",
    "Traceback",
    "unhashable",
    "NoneType",
)


class StopConversationContractTest(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()

    # ---- fake wiring -------------------------------------------------------
    def _make_fs(self, exists=True, data=None):
        fs = MagicMock(name="fake_fs")
        thread_ref = (
            fs.collection.return_value.document.return_value
            .collection.return_value.document.return_value
        )
        doc = MagicMock(name="thread_doc")
        doc.exists = exists
        doc.to_dict.return_value = data if data is not None else {"clientId": "c1", "rowNumber": 5}
        thread_ref.get.return_value = doc
        return fs, thread_ref

    def _post(self, payload=None, raw=None, content_type="application/json",
              exists=True, data=None):
        """POST with every boundary faked. Returns (resp, recorders)."""
        fs, thread_ref = self._make_fs(exists=exists, data=data)
        gcc = MagicMock(name="_get_client_config", return_value=("sheet123", None, None))
        crh = MagicMock(name="clear_row_highlight", return_value=True)
        send_index = MagicMock(name="send_and_index_email")
        send_out = MagicMock(name="send_outboxes")
        send_email = MagicMock(name="send_email")
        backoff = MagicMock(name="exponential_backoff_request")

        with patch("email_automation.clients._fs", fs), \
             patch("email_automation.messaging._fs", fs), \
             patch("email_automation.clients._get_client_config", gcc), \
             patch("email_automation.sheets.clear_row_highlight", crh), \
             patch("email_automation.email.send_and_index_email", send_index), \
             patch("email_automation.email.send_outboxes", send_out), \
             patch("email_automation.email.send_email", send_email), \
             patch("email_automation.utils.exponential_backoff_request", backoff):
            if raw is not None:
                resp = self.client.post(ROUTE, data=raw, content_type=content_type)
            else:
                resp = self.client.post(
                    ROUTE, data=json.dumps(payload), content_type=content_type
                )

        recorders = {
            "fs": fs,
            "thread_ref": thread_ref,
            "clear_row_highlight": crh,
            "get_client_config": gcc,
            "send_and_index_email": send_index,
            "send_outboxes": send_out,
            "send_email": send_email,
            "backoff": backoff,
        }
        return resp, recorders

    # ---- shared invariants -------------------------------------------------
    def _assert_no_send(self, rec, label):
        for name in ("send_and_index_email", "send_outboxes", "send_email"):
            self.assertFalse(
                rec[name].called,
                f"[{label}] {name} was called — this route must never send email",
            )

    def _body(self, resp):
        try:
            return resp.get_json() or {}
        except Exception:
            return {"_raw": resp.data.decode("utf-8", "replace")}

    def _assert_no_leak(self, resp, label):
        text = json.dumps(self._body(resp))
        for sig in LEAK_SIGNATURES:
            self.assertNotIn(
                sig, text,
                f"[{label}] response leaks internal error text ({sig!r}): {text[:200]}",
            )

    def _assert_fail_closed(self, resp, rec, label):
        """A rejected request: 4xx, success falsey, no side effects, no leak."""
        self.assertGreaterEqual(resp.status_code, 400, f"[{label}] expected 4xx")
        self.assertLess(
            resp.status_code, 500,
            f"[{label}] expected fail-closed 4xx, got {resp.status_code}: {self._body(resp)}",
        )
        body = self._body(resp)
        self.assertFalse(body.get("success", False), f"[{label}] success should be falsey")
        self.assertFalse(
            rec["thread_ref"].update.called,
            f"[{label}] no thread write should happen on a rejected request",
        )
        self.assertFalse(
            rec["clear_row_highlight"].called,
            f"[{label}] no sheet write should happen on a rejected request",
        )
        self._assert_no_send(rec, label)
        self._assert_no_leak(resp, label)

    # ======================================================================
    # HAPPY PATH
    # ======================================================================
    def test_happy_path_realistic_frontend_payload(self):
        payload = {"uid": "user_abc", "threadId": "thread_xyz", "clientId": "client_1"}
        resp, rec = self._post(payload, data={"clientId": "client_1", "rowNumber": 7})
        self.assertEqual(resp.status_code, 200, self._body(resp))
        body = self._body(resp)
        self.assertTrue(body.get("success"))
        self.assertEqual(body.get("newStatus"), "stopped")
        self.assertEqual(body.get("threadId"), "thread_xyz")
        # expected state change: follow-ups stopped + sheet highlight cleared
        self.assertTrue(rec["thread_ref"].update.called, "thread follow-up update expected")
        self.assertTrue(rec["clear_row_highlight"].called, "row highlight clear expected")
        args = rec["clear_row_highlight"].call_args[0]
        self.assertEqual(args, ("sheet123", 7))
        self._assert_no_send(rec, "happy")
        self._assert_no_leak(resp, "happy")

    def test_happy_path_without_clientid(self):
        # clientId is optional in the frontend (client?.id can be undefined).
        payload = {"uid": "u", "threadId": "t"}
        resp, rec = self._post(payload, data={"clientId": "c_from_thread", "rowNumber": 3})
        self.assertEqual(resp.status_code, 200, self._body(resp))
        self.assertTrue(self._body(resp).get("success"))
        self._assert_no_send(rec, "happy-no-clientid")

    # ======================================================================
    # REQUIRED-FIELD / NULL / EMPTY  (expected: clean 4xx fail-closed)
    # ======================================================================
    def test_missing_uid(self):
        resp, rec = self._post({"threadId": "t", "clientId": "c1"})
        self._assert_fail_closed(resp, rec, "missing uid")

    def test_missing_threadId(self):
        resp, rec = self._post({"uid": "u", "clientId": "c1"})
        self._assert_fail_closed(resp, rec, "missing threadId")

    def test_empty_object(self):
        resp, rec = self._post({})
        self._assert_fail_closed(resp, rec, "empty {}")

    def test_uid_null(self):
        resp, rec = self._post({"uid": None, "threadId": "t"})
        self._assert_fail_closed(resp, rec, "uid null")

    def test_threadId_null(self):
        resp, rec = self._post({"uid": "u", "threadId": None})
        self._assert_fail_closed(resp, rec, "threadId null")

    def test_uid_empty_string(self):
        resp, rec = self._post({"uid": "", "threadId": "t"})
        self._assert_fail_closed(resp, rec, "uid empty")

    def test_threadId_empty_string(self):
        resp, rec = self._post({"uid": "u", "threadId": ""})
        self._assert_fail_closed(resp, rec, "threadId empty")

    # ======================================================================
    # WRONG TYPES  (expected: fail-closed 4xx, no 500, no leak)
    # ======================================================================
    def test_threadId_int_type_confusion(self):
        # BUG CANDIDATE: int threadId -> 500. A wrong-typed field from a client
        # must be rejected 4xx, never crash the handler into a 500.
        resp, rec = self._post({"uid": "u", "threadId": 123})
        self.assertLess(
            resp.status_code, 500,
            f"threadId int should fail-closed 4xx, got {resp.status_code}: {self._body(resp)}",
        )
        self._assert_no_leak(resp, "threadId int")
        self._assert_no_send(rec, "threadId int")

    def test_threadId_dict_type_confusion(self):
        resp, rec = self._post({"uid": "u", "threadId": {"x": 1}})
        self.assertLess(
            resp.status_code, 500,
            f"threadId dict should fail-closed 4xx, got {resp.status_code}: {self._body(resp)}",
        )
        self._assert_no_leak(resp, "threadId dict")
        self._assert_no_send(rec, "threadId dict")

    def test_threadId_bool_type_confusion(self):
        resp, rec = self._post({"uid": "u", "threadId": True})
        self.assertLess(
            resp.status_code, 500,
            f"threadId bool should fail-closed 4xx, got {resp.status_code}: {self._body(resp)}",
        )
        self._assert_no_leak(resp, "threadId bool")
        self._assert_no_send(rec, "threadId bool")

    def test_threadId_list_type_confusion(self):
        # Currently accepted (list is sliceable) -> 200. At minimum it must not
        # crash and must not send.
        resp, rec = self._post({"uid": "u", "threadId": ["t"]})
        self.assertLess(resp.status_code, 500, self._body(resp))
        self._assert_no_leak(resp, "threadId list")
        self._assert_no_send(rec, "threadId list")

    def test_uid_int_type_confusion(self):
        resp, rec = self._post({"uid": 123, "threadId": "t"})
        self.assertLess(resp.status_code, 500, self._body(resp))
        self._assert_no_leak(resp, "uid int")
        self._assert_no_send(rec, "uid int")

    # ======================================================================
    # OVERSIZED / INJECTION-ISH VALUES  (must not crash, must not send)
    # ======================================================================
    def test_oversized_threadId(self):
        resp, rec = self._post({"uid": "u", "threadId": "A" * 10240})
        self.assertLess(resp.status_code, 500, self._body(resp))
        self._assert_no_leak(resp, "oversized")
        self._assert_no_send(rec, "oversized")

    def test_injection_path_traversal(self):
        resp, rec = self._post({"uid": "u", "threadId": "../../etc/passwd"})
        self.assertLess(resp.status_code, 500, self._body(resp))
        self._assert_no_leak(resp, "path traversal")
        self._assert_no_send(rec, "path traversal")

    def test_injection_file_uri_and_placeholders(self):
        for val in ("file:///etc/passwd", "[NAME]", "[BROKER]",
                    "<script>alert(1)</script>", "line1\nline2\r\n", "üñîçödé—✓"):
            resp, rec = self._post({"uid": "u", "threadId": val})
            self.assertLess(resp.status_code, 500, f"{val!r}: {self._body(resp)}")
            self._assert_no_leak(resp, f"injection {val!r}")
            self._assert_no_send(rec, f"injection {val!r}")

    # ======================================================================
    # NONEXISTENT RESOURCE  (fake _fs returns not-found -> 404 fail-closed)
    # ======================================================================
    def test_nonexistent_thread(self):
        resp, rec = self._post({"uid": "u", "threadId": "ghost"}, exists=False)
        self.assertEqual(resp.status_code, 404, self._body(resp))
        self.assertFalse(self._body(resp).get("success", False))
        self.assertFalse(rec["thread_ref"].update.called, "no write on not-found")
        self.assertFalse(rec["clear_row_highlight"].called, "no sheet write on not-found")
        self._assert_no_send(rec, "nonexistent")
        self._assert_no_leak(resp, "nonexistent")

    # ======================================================================
    # IDEMPOTENCY / RETRY  (same request twice -> stable, no double send)
    # ======================================================================
    def test_duplicate_retry_idempotent(self):
        payload = {"uid": "u", "threadId": "t", "clientId": "c1"}
        r1, rec1 = self._post(payload, data={"clientId": "c1", "rowNumber": 5})
        r2, rec2 = self._post(payload, data={"clientId": "c1", "rowNumber": 5})
        self.assertEqual(r1.status_code, 200, self._body(r1))
        self.assertEqual(r2.status_code, 200, self._body(r2))
        self.assertEqual(self._body(r1).get("newStatus"), "stopped")
        self.assertEqual(self._body(r2).get("newStatus"), "stopped")
        self._assert_no_send(rec1, "retry-1")
        self._assert_no_send(rec2, "retry-2")

    # ======================================================================
    # UNEXPECTED EXTRA FIELDS  (must be ignored, still succeed)
    # ======================================================================
    def test_unexpected_extra_fields(self):
        payload = {
            "uid": "u", "threadId": "t", "clientId": "c1",
            "__proto__": {"admin": True}, "status": "active",
            "rowNumber": 99999, "evil": "x" * 100, "recipients": ["a@b.com"],
        }
        resp, rec = self._post(payload, data={"clientId": "c1", "rowNumber": 5})
        self.assertEqual(resp.status_code, 200, self._body(resp))
        self.assertTrue(self._body(resp).get("success"))
        # extra "recipients" field must not cause any send
        self._assert_no_send(rec, "extra fields")
        # highlight cleared for the REAL rowNumber from thread doc (5), not the
        # attacker-supplied 99999 in the body.
        if rec["clear_row_highlight"].called:
            self.assertEqual(rec["clear_row_highlight"].call_args[0], ("sheet123", 5))

    # ======================================================================
    # NON-JSON / MALFORMED BODY  (expected: clean 4xx, no leak)
    # ======================================================================
    def test_json_array_empty_body(self):
        resp, rec = self._post(raw="[]")
        self._assert_fail_closed(resp, rec, "json []")

    def test_json_string_body(self):
        # BUG CANDIDATE: valid JSON but not an object -> 500 + leaked
        # "'str' object has no attribute 'get'".
        resp, rec = self._post(raw='"hello"')
        self._assert_fail_closed(resp, rec, "json string body")

    def test_json_number_body(self):
        resp, rec = self._post(raw="123")
        self._assert_fail_closed(resp, rec, "json number body")

    def test_json_nonempty_array_body(self):
        resp, rec = self._post(raw='[1, 2, 3]')
        self._assert_fail_closed(resp, rec, "json array body")

    def test_json_true_body(self):
        resp, rec = self._post(raw="true")
        self._assert_fail_closed(resp, rec, "json true body")

    def test_malformed_json_body(self):
        # BUG CANDIDATE: broken JSON -> werkzeug BadRequest re-wrapped as 500.
        resp, rec = self._post(raw="{bad json")
        self._assert_fail_closed(resp, rec, "malformed json")

    def test_non_json_content_type(self):
        # BUG CANDIDATE: wrong Content-Type -> UnsupportedMediaType(415)
        # re-wrapped as 500 leaking the werkzeug message.
        resp, rec = self._post(raw="not json at all", content_type="text/plain")
        self._assert_fail_closed(resp, rec, "non-json body")


if __name__ == "__main__":
    unittest.main(verbosity=2)
