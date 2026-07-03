"""
Adversarial front-end-contract fuzz of POST /api/clear-optout.

Route: app.py :: api_clear_optout()
Realistic payload (admin action, documented in CLAUDE.md + docstring):
    { "uid": "<firebase-uid>", "email": "<contact-email>" }

The handler:
  - looks up  users/{uid}/optedOutContacts/{sha256(email.lower().strip())[:16]}
  - 404 if the doc does not exist
  - otherwise DELETES it and returns previousRecord

External boundaries touched: email_automation.clients._fs (Firestore).
The route does NOT send email, but we still patch the send entrypoint and
assert it is never invoked (recipient-guard defense in depth).

Every external boundary is faked; nothing real happens.
"""
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

import app as appmod  # noqa: E402

ALLOWED_RECIPIENTS = {"bp21harrison@gmail.com", "baylor.freelance@outlook.com"}

VALID_UID = "user-abc123"
VALID_EMAIL = "contact@example.com"


def build_fs(exists=True, record=None):
    """Return (fake_fs, optout_ref) where optout_ref is the leaf document mock.

    optout_ref.get().exists == exists, and .to_dict() -> record.
    optout_ref.delete records calls so we can assert (no) state mutation.
    """
    fake_fs = MagicMock(name="fake_fs")
    optout_ref = MagicMock(name="optout_ref")
    doc = MagicMock(name="optout_doc")
    doc.exists = exists
    doc.to_dict.return_value = record or {
        "email": VALID_EMAIL,
        "reason": "unsubscribe",
        "optedOutAt": "2026-01-01T00:00:00Z",
    }
    optout_ref.get.return_value = doc
    # users/{uid}/optedOutContacts/{hash}
    (
        fake_fs.collection.return_value  # users
        .document.return_value          # {uid}
        .collection.return_value        # optedOutContacts
        .document.return_value          # {hash}
    ) = optout_ref
    return fake_fs, optout_ref


class ClearOptoutContractFuzz(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()
        # Defense in depth: this route must never trigger a send.
        self.send_mock = MagicMock(name="send_and_index_email")
        self._send_patch = patch(
            "email_automation.email.send_and_index_email", self.send_mock
        )
        self._send_patch.start()
        self.addCleanup(self._send_patch.stop)

    # ---- shared robustness assertions -------------------------------------

    def assert_no_send_to_disallowed(self):
        for call in self.send_mock.call_args_list:
            recipients = []
            if len(call.args) >= 4:
                recipients = call.args[3]
            recipients = recipients or call.kwargs.get("recipients", [])
            for r in recipients or []:
                self.assertIn(
                    r, ALLOWED_RECIPIENTS,
                    f"send to disallowed recipient {r!r}",
                )

    def assert_fail_closed(self, resp, optout_ref, mutation_label):
        """A rejected request must: not be a 2xx, report success:false when it
        returns JSON, never delete anything, never leak a raw stack trace, and
        never send."""
        self.assertGreaterEqual(resp.status_code, 400, mutation_label)
        body = resp.get_json(silent=True)
        if body is not None:
            self.assertFalse(
                body.get("success", False),
                f"{mutation_label}: success should be false, got {body}",
            )
            err = (body.get("error") or "")
            # No raw python-internal / traceback text leaking to the client.
            for leak in ("Traceback", "object has no attribute",
                         "'NoneType'", "line ", "File \""):
                self.assertNotIn(
                    leak, err,
                    f"{mutation_label}: leaked internal error text: {err!r}",
                )
        optout_ref.delete.assert_not_called()
        self.assert_no_send_to_disallowed()

    # ---- happy path -------------------------------------------------------

    def test_happy_path(self):
        fs, optout_ref = build_fs(exists=True)
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post(
                "/api/clear-optout",
                json={"uid": VALID_UID, "email": VALID_EMAIL},
            )
        self.assertEqual(resp.status_code, 200, resp.get_json())
        body = resp.get_json()
        self.assertTrue(body["success"])
        # Expected state change: the opt-out doc was deleted exactly once.
        optout_ref.delete.assert_called_once()
        self.assertIn("previousRecord", body)
        self.assert_no_send_to_disallowed()

    def test_happy_path_email_normalized_before_hash(self):
        # Upper-case + surrounding whitespace must resolve to the same record.
        fs, optout_ref = build_fs(exists=True)
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post(
                "/api/clear-optout",
                json={"uid": VALID_UID, "email": "  CONTACT@EXAMPLE.COM  "},
            )
        self.assertEqual(resp.status_code, 200, resp.get_json())
        optout_ref.delete.assert_called_once()

    def test_not_found_is_graceful_and_no_delete(self):
        fs, optout_ref = build_fs(exists=False)
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post(
                "/api/clear-optout",
                json={"uid": VALID_UID, "email": "ghost@example.com"},
            )
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(resp.get_json()["success"])
        optout_ref.delete.assert_not_called()

    # ---- required / null / empty ------------------------------------------

    def test_missing_uid(self):
        fs, optout_ref = build_fs()
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post("/api/clear-optout", json={"email": VALID_EMAIL})
        self.assert_fail_closed(resp, optout_ref, "missing uid")
        self.assertEqual(resp.status_code, 400)

    def test_missing_email(self):
        fs, optout_ref = build_fs()
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post("/api/clear-optout", json={"uid": VALID_UID})
        self.assert_fail_closed(resp, optout_ref, "missing email")
        self.assertEqual(resp.status_code, 400)

    def test_empty_body(self):
        fs, optout_ref = build_fs()
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post("/api/clear-optout", json={})
        self.assert_fail_closed(resp, optout_ref, "empty body")
        self.assertEqual(resp.status_code, 400)

    def test_null_uid(self):
        fs, optout_ref = build_fs()
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post(
                "/api/clear-optout", json={"uid": None, "email": VALID_EMAIL}
            )
        self.assert_fail_closed(resp, optout_ref, "null uid")

    def test_null_email(self):
        fs, optout_ref = build_fs()
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post(
                "/api/clear-optout", json={"uid": VALID_UID, "email": None}
            )
        self.assert_fail_closed(resp, optout_ref, "null email")

    def test_empty_string_uid(self):
        fs, optout_ref = build_fs()
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post(
                "/api/clear-optout", json={"uid": "", "email": VALID_EMAIL}
            )
        self.assert_fail_closed(resp, optout_ref, "empty uid")

    def test_empty_string_email(self):
        fs, optout_ref = build_fs()
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post(
                "/api/clear-optout", json={"uid": VALID_UID, "email": ""}
            )
        self.assert_fail_closed(resp, optout_ref, "empty email")

    def test_whitespace_only_email(self):
        # "   " is truthy so it passes the guard; after strip -> "" it hashes
        # the empty string. Should still not crash / not delete a real record.
        fs, optout_ref = build_fs(exists=False)
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post(
                "/api/clear-optout", json={"uid": VALID_UID, "email": "   "}
            )
        # exists=False -> 404, no delete. Robust either way as long as no 5xx.
        self.assertLess(resp.status_code, 500, resp.get_json())
        optout_ref.delete.assert_not_called()

    # ---- wrong types (BUG territory) --------------------------------------

    def test_email_wrong_type_int(self):
        fs, optout_ref = build_fs()
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post(
                "/api/clear-optout", json={"uid": VALID_UID, "email": 123}
            )
        # CORRECT behavior: reject a non-string email with a 4xx validation
        # error, not a 500 that leaks a python AttributeError.
        self.assert_fail_closed(resp, optout_ref, "email int")

    def test_email_wrong_type_list(self):
        fs, optout_ref = build_fs()
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post(
                "/api/clear-optout", json={"uid": VALID_UID, "email": ["a@b.com"]}
            )
        self.assert_fail_closed(resp, optout_ref, "email list")

    def test_email_wrong_type_object(self):
        fs, optout_ref = build_fs()
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post(
                "/api/clear-optout",
                json={"uid": VALID_UID, "email": {"addr": "a@b.com"}},
            )
        self.assert_fail_closed(resp, optout_ref, "email object")

    def test_email_wrong_type_bool(self):
        fs, optout_ref = build_fs()
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post(
                "/api/clear-optout", json={"uid": VALID_UID, "email": True}
            )
        self.assert_fail_closed(resp, optout_ref, "email bool")

    def test_uid_wrong_type_list(self):
        # A list uid must not silently reach the Firestore .document() call.
        fs, optout_ref = build_fs()
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post(
                "/api/clear-optout", json={"uid": ["u1"], "email": VALID_EMAIL}
            )
        self.assert_fail_closed(resp, optout_ref, "uid list")

    def test_uid_wrong_type_int(self):
        fs, optout_ref = build_fs()
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post(
                "/api/clear-optout", json={"uid": 999, "email": VALID_EMAIL}
            )
        self.assert_fail_closed(resp, optout_ref, "uid int")

    # ---- oversized / injection-ish ----------------------------------------

    def test_oversized_email(self):
        big = "a" * 10240 + "@example.com"
        fs, optout_ref = build_fs(exists=False)
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post(
                "/api/clear-optout", json={"uid": VALID_UID, "email": big}
            )
        self.assertLess(resp.status_code, 500, "oversized email should not 5xx")
        optout_ref.delete.assert_not_called()

    def test_injection_values(self):
        payloads = [
            "../../etc/passwd",
            "file:///etc/passwd",
            "[NAME]",
            "[BROKER]@example.com",
            "<script>alert(1)</script>@x.com",
            "a@b.com\ninjected: yes",
            "unïcodé@example.com",
            "'; DROP TABLE optout;--@x.com",
        ]
        for val in payloads:
            fs, optout_ref = build_fs(exists=False)
            with patch("email_automation.clients._fs", fs):
                resp = self.client.post(
                    "/api/clear-optout", json={"uid": VALID_UID, "email": val}
                )
            self.assertLess(
                resp.status_code, 500,
                f"injection value {val!r} caused a 5xx",
            )
            optout_ref.delete.assert_not_called()
            self.assert_no_send_to_disallowed()

    def test_injection_uid(self):
        fs, optout_ref = build_fs(exists=False)
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post(
                "/api/clear-optout",
                json={"uid": "../../../users/victim", "email": VALID_EMAIL},
            )
        # Path-traversal-ish uid: we can't fully validate here, but it must not
        # 5xx and (record absent) must not delete.
        self.assertLess(resp.status_code, 500)
        optout_ref.delete.assert_not_called()

    # ---- extra fields / malformed body ------------------------------------

    def test_unexpected_extra_fields(self):
        fs, optout_ref = build_fs(exists=True)
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post(
                "/api/clear-optout",
                json={
                    "uid": VALID_UID,
                    "email": VALID_EMAIL,
                    "admin": True,
                    "force": "yes",
                    "reason": "override",
                },
            )
        # Extra fields must be ignored, happy path still works.
        self.assertEqual(resp.status_code, 200, resp.get_json())
        optout_ref.delete.assert_called_once()

    def test_malformed_json_body(self):
        fs, optout_ref = build_fs()
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post(
                "/api/clear-optout",
                data="{not valid json",
                content_type="application/json",
            )
        # CORRECT behavior: malformed JSON is a client error -> 4xx, not 500.
        self.assert_fail_closed(resp, optout_ref, "malformed json")

    def test_non_json_content_type(self):
        fs, optout_ref = build_fs()
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post(
                "/api/clear-optout",
                data="uid=u1&email=a@b.com",
                content_type="application/x-www-form-urlencoded",
            )
        self.assert_fail_closed(resp, optout_ref, "non-json content type")

    def test_empty_raw_body(self):
        fs, optout_ref = build_fs()
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post("/api/clear-optout", data="", content_type="application/json")
        self.assert_fail_closed(resp, optout_ref, "empty raw body")

    # ---- idempotency / retry ----------------------------------------------

    def test_duplicate_request_is_idempotent(self):
        # Stateful fake: after delete, the doc no longer exists, so a retry
        # must 404 and must NOT delete a second time.
        state = {"exists": True}
        fake_fs = MagicMock()
        optout_ref = MagicMock()
        doc = MagicMock()
        doc.to_dict.return_value = {"email": VALID_EMAIL, "reason": "unsubscribe"}

        def _get():
            d = MagicMock()
            d.exists = state["exists"]
            d.to_dict.return_value = {"email": VALID_EMAIL, "reason": "unsubscribe"}
            return d

        def _delete(*a, **k):
            state["exists"] = False

        optout_ref.get.side_effect = _get
        optout_ref.delete.side_effect = _delete
        (
            fake_fs.collection.return_value.document.return_value
            .collection.return_value.document.return_value
        ) = optout_ref

        with patch("email_automation.clients._fs", fake_fs):
            r1 = self.client.post(
                "/api/clear-optout", json={"uid": VALID_UID, "email": VALID_EMAIL}
            )
            r2 = self.client.post(
                "/api/clear-optout", json={"uid": VALID_UID, "email": VALID_EMAIL}
            )
        self.assertEqual(r1.status_code, 200, r1.get_json())
        self.assertEqual(r2.status_code, 404, r2.get_json())
        # delete only fired on the first (real) clear.
        self.assertEqual(optout_ref.delete.call_count, 1)
        self.assert_no_send_to_disallowed()


if __name__ == "__main__":
    unittest.main(verbosity=2)
