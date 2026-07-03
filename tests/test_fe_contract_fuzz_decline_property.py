"""
Adversarial frontend-contract fuzz for POST /api/decline-property.

The route deletes a row from a Google Sheet:
    body = { uid, clientId, rowNumber, sheetId }
It imports (inside the handler) email_automation.clients._sheets_client and
email_automation.sheets._first_sheet_props, then issues a destructive
sheets.spreadsheets().batchUpdate(deleteDimension).execute().

EVERY external boundary is faked here so nothing real happens:
  - email_automation.clients._sheets_client  -> fake Sheets client (records batchUpdate)
  - email_automation.sheets._first_sheet_props -> returns (grid_id, tab_title)
  - email_automation.email.send_and_index_email / send_outboxes -> guard mocks (must NEVER fire)
  - email_automation.clients._fs -> fake Firestore (route doesn't use it, patched for safety)

RECIPIENT GUARD: this route sends no email; we assert the send entrypoints are
never invoked at all, which trivially guarantees no disallowed recipient is mailed.

Some assertions below intentionally pin CORRECT behavior that the current handler
violates (documented as BUGS in the fuzz report). Those are left RED on purpose.
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
sys.path.insert(0, "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation")

import app as appmod  # noqa: E402

ROUTE = "/api/decline-property"
ALLOWED_RECIPIENTS = {"bp21harrison@gmail.com", "baylor.freelance@outlook.com"}

# A leaked-internal-error signature: raw Python exception text bubbled to the client.
_LEAK_SIGNATURES = (
    "unsupported operand type",
    "Traceback (most recent call last)",
    "'NoneType' object",
    "KeyError",
)


class DeclinePropertyFuzz(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()

        # Fake Sheets client — records the destructive batchUpdate call.
        self.fake_sheets = MagicMock(name="fake_sheets")
        self.batch = self.fake_sheets.spreadsheets.return_value.batchUpdate

        # Fake Firestore (unused by this route, patched for safety).
        self.fake_fs = MagicMock(name="fake_fs")

        # Send guards — must NEVER be called by this route.
        self.send_and_index = MagicMock(name="send_and_index_email")
        self.send_outboxes = MagicMock(name="send_outboxes")

        self._patches = [
            patch("email_automation.clients._sheets_client", return_value=self.fake_sheets),
            patch("email_automation.sheets._first_sheet_props", return_value=(0, "Sheet1")),
            patch("email_automation.clients._fs", self.fake_fs),
            patch("email_automation.email.send_and_index_email", self.send_and_index),
            patch("email_automation.email.send_outboxes", self.send_outboxes),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._stop_all)

    def _stop_all(self):
        for p in self._patches:
            p.stop()

    # ---- helpers ------------------------------------------------------------
    def valid(self, **over):
        payload = {"uid": "u1", "clientId": "c1", "rowNumber": 5, "sheetId": "s1"}
        payload.update(over)
        return payload

    def post(self, payload=None, raw=None, content_type="application/json"):
        self.fake_sheets.reset_mock()
        if raw is not None:
            return self.client.post(ROUTE, data=raw, content_type=content_type)
        return self.client.post(ROUTE, json=payload)

    def assert_no_send(self):
        self.assertFalse(self.send_and_index.called, "send_and_index_email must never fire")
        self.assertFalse(self.send_outboxes.called, "send_outboxes must never fire")

    def assert_no_leak(self, body):
        text = str(body)
        for sig in _LEAK_SIGNATURES:
            self.assertNotIn(
                sig, text, f"handler leaked internal error signature {sig!r}: {text}"
            )

    def assert_deleted(self):
        self.assertTrue(self.batch.called, "expected a batchUpdate delete")

    def assert_not_deleted(self):
        self.assertFalse(
            self.batch.called,
            f"UNEXPECTED destructive delete; call={self.batch.call_args}",
        )

    def assert_rejected(self, resp):
        """Fail-closed: 4xx (or success:false) and NO row deleted."""
        body = resp.get_json() or {}
        self.assertTrue(
            resp.status_code >= 400 or body.get("success") is False,
            f"expected fail-closed, got {resp.status_code} {body}",
        )
        self.assert_not_deleted()
        self.assert_no_send()

    # =========================================================================
    # HAPPY PATH
    # =========================================================================
    def test_happy_path_deletes_row(self):
        r = self.post(self.valid(rowNumber=7))
        body = r.get_json()
        self.assertEqual(r.status_code, 200, body)
        self.assertIs(body.get("success"), True)
        self.assertEqual(body.get("deletedRow"), 7)
        self.assert_deleted()
        # Correct 0-based conversion: row 7 -> startIndex 6, endIndex 7.
        rng = self.batch.call_args.kwargs["body"]["requests"][0]["deleteDimension"]["range"]
        self.assertEqual((rng["startIndex"], rng["endIndex"]), (6, 7))
        self.assert_no_send()

    # =========================================================================
    # ROBUST (current behavior is correct) — expected GREEN
    # =========================================================================
    def test_missing_each_required_field(self):
        for field in ("uid", "clientId", "rowNumber", "sheetId"):
            p = self.valid()
            p.pop(field)
            with self.subTest(field=field):
                self.assert_rejected(self.post(p))

    def test_null_each_required_field(self):
        for field in ("uid", "clientId", "rowNumber", "sheetId"):
            with self.subTest(field=field):
                self.assert_rejected(self.post(self.valid(**{field: None})))

    def test_empty_string_each_field(self):
        for field in ("uid", "clientId", "sheetId"):
            with self.subTest(field=field):
                self.assert_rejected(self.post(self.valid(**{field: ""})))

    def test_zero_rownumber_rejected(self):
        # 0 is falsy -> caught by the required-fields guard.
        self.assert_rejected(self.post(self.valid(rowNumber=0)))

    def test_empty_body(self):
        self.assert_rejected(self.post({}))

    def test_no_json_body(self):
        r = self.post(raw="not json at all", content_type="text/plain")
        # Fail-closed on state (no delete, no send) is what matters most.
        self.assert_not_deleted()
        self.assert_no_send()

    def test_malformed_json_body(self):
        r = self.post(raw="{ bad json", content_type="application/json")
        self.assert_not_deleted()
        self.assert_no_send()

    def test_extra_unexpected_fields_ignored(self):
        r = self.post(self.valid(rowNumber=3, evil="<script>", __proto__={"x": 1}, admin=True))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assert_deleted()
        self.assert_no_send()

    def test_oversized_sheetid_string(self):
        # 10KB sheetId — handler passes it straight to the (mocked) API; must not crash.
        r = self.post(self.valid(rowNumber=2, sheetId="S" * 10240))
        self.assertNotEqual(r.status_code, 500, r.get_json())
        self.assert_no_send()

    def test_injection_ish_string_fields(self):
        for evil in (
            "../../etc/passwd",
            "file:///etc/passwd",
            "[NAME]",
            "[BROKER]",
            "<script>alert(1)</script>",
            "line1\nline2\r\n",
            "‮abc_rlo",
            "'; DROP TABLE rows;--",
        ):
            with self.subTest(evil=evil):
                r = self.post(self.valid(rowNumber=2, uid=evil, clientId=evil, sheetId=evil))
                # Handler must not 500 / leak on these; either deletes on the mock or 4xx.
                body = r.get_json() or {}
                self.assertNotEqual(r.status_code, 500, body)
                self.assert_no_leak(body)
                self.assert_no_send()

    def test_backend_sheet_error_fails_closed_json(self):
        # Simulate a real "sheet not found" / API error from the boundary.
        with patch("email_automation.sheets._first_sheet_props", side_effect=RuntimeError("sheet 404")):
            r = self.post(self.valid(rowNumber=4, sheetId="ghost"))
            body = r.get_json()
            # Must stay JSON (not an HTML crash page) and fail closed.
            self.assertIsNotNone(body, "expected JSON error body")
            self.assertIs(body.get("success"), False)
            self.assert_not_deleted()
            self.assert_no_send()

    # =========================================================================
    # BUG PINS — assert CORRECT behavior; these currently FAIL (RED).
    # =========================================================================
    def test_bug_nonnumeric_rownumber_no_500_leak(self):
        """
        BUG: string / array / object rowNumber -> row_number - 1 raises TypeError,
        which is caught and returned as HTTP 500 with the raw Python exception text
        (e.g. "unsupported operand type(s) for -: 'str' and 'int'").
        Correct behavior: reject with 4xx and no internal-error leak.
        The frontend sets rowNumber from `property.rowIndex || null`, which can
        realistically arrive as a string from sheet-sourced data.
        """
        for bad in ("5", [5], {"x": 1}):
            with self.subTest(rowNumber=bad):
                r = self.post(self.valid(rowNumber=bad))
                body = r.get_json() or {}
                self.assert_not_deleted()
                self.assert_no_send()
                self.assertNotEqual(
                    r.status_code, 500,
                    f"non-numeric rowNumber={bad!r} produced a 500: {body}",
                )
                self.assert_no_leak(body)

    def test_bug_invalid_rownumber_must_not_delete(self):
        """
        BUG: rowNumber is trusted blindly and fed into a destructive deleteDimension.
        Negative / fractional / bool values pass the truthiness guard and issue a
        REAL delete (batchUpdate called) with a nonsensical range:
          -3   -> startIndex=-4, endIndex=-3   (deletes wrong/last rows)
          5.9  -> startIndex=4.9               (invalid range)
          True -> endIndex=True                (deletes near row 1)
        Correct behavior: validate rowNumber is a positive integer and reject
        (4xx, no delete) otherwise.
        """
        for bad in (-3, 5.9, True):
            with self.subTest(rowNumber=bad):
                r = self.post(self.valid(rowNumber=bad))
                self.assert_rejected(r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
