"""
Adversarial FE-contract fuzz for POST /api/check-sheet-completion.

The route (app.py :: api_check_sheet_completion) is READ-ONLY: given a JSON body
{"sheetId": "<id>"} it reads the first tab of a Google Sheet and reports how many
"viable" rows have every REQUIRED_FIELDS_FOR_CLOSE column filled. It touches ONE
external boundary — the Google Sheets client (email_automation.clients._sheets_client).
It performs NO Firestore write and NO email send.

Every external boundary is faked here so nothing real happens:
  - email_automation.clients._sheets_client -> FakeSheets (records the calls it receives)
  - the three send/refresh entrypoints (send_and_index_email, send_outboxes,
    exponential_backoff_request) are patched with MagicMocks and asserted NEVER called,
    so no mutation can smuggle out an email to a disallowed recipient.

Robustness contract asserted for EVERY mutation:
  * no unhandled 500 that leaks an internal Python/framework error string
  * fail-closed: a 4xx (or {"success": false}) for bad input, never a 2xx that
    pretends success on garbage
  * no unintended state mutation on the fakes (read-only route: zero writes)
  * no send-function invocation

Bugs found are pinned by asserting the CORRECT behavior; those assertions go RED.
"""
import json
import os
import unittest
from unittest.mock import patch, MagicMock

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

import app as appmod  # noqa: E402

ALLOWED_RECIPIENTS = {"bp21harrison@gmail.com", "baylor.freelance@outlook.com"}

# Header (row 2) + a complete viable data row for the happy path.
HEADER = [
    "Property Address", "Total SF", "Ops Ex /SF",
    "Drive Ins", "Docks", "Ceiling Ht", "Power", "Flyer / Link",
]
COMPLETE_ROW = ["123 Main St", "10000", "5.0", "2", "1", "24", "480V", "http://flyer/x"]
INCOMPLETE_ROW = ["456 Oak Ave", "8000", "", "1", "", "20", "", ""]  # missing several


# --------------------------------------------------------------------------- #
# Fake Google Sheets client. Records every call so we can prove read-only.
# Mirrors the real googleapiclient surface the handler + helpers exercise:
#   sheets.spreadsheets().get(spreadsheetId=...).execute()
#   sheets.spreadsheets().values().get(spreadsheetId=..., range=...).execute()
# --------------------------------------------------------------------------- #
class _FakeReq:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeValues:
    def __init__(self, owner):
        self.owner = owner

    def get(self, spreadsheetId=None, range=None):
        self.owner.calls.append(("values.get", spreadsheetId, range))
        if range and "!2:2" in range:
            return _FakeReq({"values": [self.owner.header]})
        return _FakeReq({"values": self.owner.rows})

    # Any write-shaped call would be a bug for this read-only route.
    def update(self, *a, **k):
        self.owner.writes.append(("values.update", a, k))
        return _FakeReq({})

    def batchUpdate(self, *a, **k):
        self.owner.writes.append(("values.batchUpdate", a, k))
        return _FakeReq({})


class _FakeSpreadsheets:
    def __init__(self, owner):
        self.owner = owner

    def get(self, spreadsheetId=None):
        self.owner.calls.append(("spreadsheets.get", spreadsheetId))
        return _FakeReq({"sheets": [{"properties": {"title": self.owner.tab}}]})

    def values(self):
        return _FakeValues(self.owner)

    def batchUpdate(self, *a, **k):
        self.owner.writes.append(("spreadsheets.batchUpdate", a, k))
        return _FakeReq({})


class FakeSheets:
    def __init__(self, header=None, rows=None, tab="Sheet1"):
        self.header = header if header is not None else list(HEADER)
        self.rows = rows if rows is not None else [list(COMPLETE_ROW)]
        self.tab = tab
        self.calls = []   # read calls
        self.writes = []  # any mutating call — must stay empty

    def spreadsheets(self):
        return _FakeSpreadsheets(self)


class CheckSheetCompletionFuzz(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()
        # Send/refresh guard: patch every send-capable entrypoint and prove
        # none of them ever fire on this route.
        self.send_mocks = {}
        self._send_patchers = []
        for mod, attr in (
            ("email_automation.email", "send_and_index_email"),
            ("email_automation.email", "send_outboxes"),
            ("email_automation.utils", "exponential_backoff_request"),
        ):
            try:
                p = patch(f"{mod}.{attr}", MagicMock(name=attr))
                m = p.start()
                self._send_patchers.append(p)
                self.send_mocks[attr] = m
            except (AttributeError, ModuleNotFoundError):
                pass

    def tearDown(self):
        for p in self._send_patchers:
            p.stop()

    # ---- shared invocation + universal robustness assertions --------------- #
    def _post(self, fake, **request_kwargs):
        with patch("email_automation.clients._sheets_client", return_value=fake):
            resp = self.client.post("/api/check-sheet-completion", **request_kwargs)
        try:
            body = resp.get_json(silent=True)
        except Exception:
            body = None
        return resp, body

    def _assert_no_send(self):
        for name, m in self.send_mocks.items():
            self.assertFalse(
                m.called,
                f"send entrypoint {name} was invoked by a read-only route",
            )

    def _assert_read_only(self, fake):
        self.assertEqual(
            fake.writes, [],
            f"read-only route performed sheet writes: {fake.writes}",
        )

    def _assert_robust(self, resp, body, fake, label):
        """Universal contract for adversarial inputs: no leaky 500, fail-closed, no side effects."""
        self._assert_no_send()
        self._assert_read_only(fake)
        # No unhandled 500 that leaks an internal error string.
        if resp.status_code == 500:
            leaked = ""
            if isinstance(body, dict):
                leaked = str(body.get("error", ""))
            self.fail(
                f"[{label}] handler returned 500 leaking internal error: {leaked!r} "
                f"(expected a clean 4xx / fail-closed response)"
            )
        # Fail-closed: bad input must not masquerade as 2xx success.
        if 200 <= resp.status_code < 300:
            self.assertTrue(
                isinstance(body, dict) and body.get("success") is False,
                f"[{label}] adversarial input accepted as success (status "
                f"{resp.status_code}, body {body})",
            )
        else:
            self.assertTrue(400 <= resp.status_code < 500,
                            f"[{label}] expected 4xx, got {resp.status_code}")

    # ----------------------------------------------------------------------- #
    # HAPPY PATH
    # ----------------------------------------------------------------------- #
    def test_happy_path_complete_sheet(self):
        fake = FakeSheets(rows=[list(COMPLETE_ROW)])
        resp, body = self._post(fake, json={"sheetId": "sheet-abc"})
        self.assertEqual(resp.status_code, 200, body)
        self.assertEqual(body["success"], True)
        self.assertEqual(body["isComplete"], True)
        self.assertEqual(body["totalViableProperties"], 1)
        self.assertEqual(body["completedProperties"], 1)
        self.assertEqual(body["completionPercentage"], 100.0)
        # The real sheetId reached the (faked) Sheets boundary; no writes, no sends.
        self.assertIn(("spreadsheets.get", "sheet-abc"), fake.calls)
        self._assert_read_only(fake)
        self._assert_no_send()

    def test_happy_path_incomplete_sheet(self):
        fake = FakeSheets(rows=[list(COMPLETE_ROW), list(INCOMPLETE_ROW)])
        resp, body = self._post(fake, json={"sheetId": "sheet-xyz"})
        self.assertEqual(resp.status_code, 200, body)
        self.assertEqual(body["isComplete"], False)
        self.assertEqual(body["totalViableProperties"], 2)
        self.assertEqual(body["completedProperties"], 1)
        self.assertEqual(len(body["incompleteRows"]), 1)
        self.assertEqual(body["incompleteRows"][0]["rowNumber"], 4)
        self._assert_read_only(fake)
        self._assert_no_send()

    # ----------------------------------------------------------------------- #
    # REQUIRED-FIELD / EMPTY-VALUE MUTATIONS  (expected robust -> should pass)
    # ----------------------------------------------------------------------- #
    def test_missing_sheetId(self):
        fake = FakeSheets()
        resp, body = self._post(fake, json={})
        self.assertEqual(resp.status_code, 400, body)
        self.assertEqual(body["success"], False)
        # never touched the sheet boundary
        self.assertEqual(fake.calls, [])
        self._assert_no_send()

    def test_null_sheetId(self):
        fake = FakeSheets()
        resp, body = self._post(fake, json={"sheetId": None})
        self.assertEqual(resp.status_code, 400, body)
        self.assertEqual(body["success"], False)
        self.assertEqual(fake.calls, [])

    def test_empty_string_sheetId(self):
        fake = FakeSheets()
        resp, body = self._post(fake, json={"sheetId": ""})
        self.assertEqual(resp.status_code, 400, body)
        self.assertEqual(body["success"], False)
        self.assertEqual(fake.calls, [])

    # ----------------------------------------------------------------------- #
    # WRONG-TYPE sheetId values  (truthy non-strings)
    # A robust handler should reject non-string ids with a clean 4xx rather
    # than forward them to the Sheets API. Current handler forwards them.
    # ----------------------------------------------------------------------- #
    def test_sheetId_wrong_type_int(self):
        fake = FakeSheets()
        resp, body = self._post(fake, json={"sheetId": 123456})
        self._assert_robust(resp, body, fake, "sheetId=int")

    def test_sheetId_wrong_type_array(self):
        fake = FakeSheets()
        resp, body = self._post(fake, json={"sheetId": ["a", "b"]})
        self._assert_robust(resp, body, fake, "sheetId=array")

    def test_sheetId_wrong_type_object(self):
        fake = FakeSheets()
        resp, body = self._post(fake, json={"sheetId": {"nested": "obj"}})
        self._assert_robust(resp, body, fake, "sheetId=object")

    def test_sheetId_wrong_type_bool(self):
        fake = FakeSheets()
        resp, body = self._post(fake, json={"sheetId": True})
        self._assert_robust(resp, body, fake, "sheetId=bool")

    # ----------------------------------------------------------------------- #
    # OVERSIZED + INJECTION-ISH sheetId strings.
    # These are valid strings; the read-only route forwards them to the faked
    # Sheets client, which returns normally -> 200 success is acceptable, and
    # crucially there must be NO write and NO send.
    # ----------------------------------------------------------------------- #
    def test_sheetId_oversized_10kb(self):
        fake = FakeSheets()
        resp, body = self._post(fake, json={"sheetId": "A" * 10240})
        self.assertNotEqual(resp.status_code, 500, body)
        self._assert_read_only(fake)
        self._assert_no_send()

    def test_sheetId_injection_values(self):
        payloads = [
            "../../../../etc/passwd",
            "file:///etc/passwd",
            "[NAME]", "[BROKER]",
            "<script>alert(1)</script>",
            "line1\nline2\r\nline3",
            "sheet‮evilnull",
            "spread\U0001F600emoji",
        ]
        for val in payloads:
            fake = FakeSheets()
            resp, body = self._post(fake, json={"sheetId": val})
            self.assertNotEqual(resp.status_code, 500,
                                f"injection sheetId {val!r} -> 500: {body}")
            self._assert_read_only(fake)
            self._assert_no_send()

    # ----------------------------------------------------------------------- #
    # UNEXPECTED EXTRA FIELDS — must be ignored, still succeeds.
    # ----------------------------------------------------------------------- #
    def test_extra_unexpected_fields(self):
        fake = FakeSheets(rows=[list(COMPLETE_ROW)])
        resp, body = self._post(
            fake,
            json={"sheetId": "s1", "uid": "u1", "__proto__": {"x": 1},
                  "admin": True, "range": "A1:Z999", "unexpected": "field"},
        )
        self.assertEqual(resp.status_code, 200, body)
        self.assertEqual(body["success"], True)
        self._assert_read_only(fake)
        self._assert_no_send()

    # ----------------------------------------------------------------------- #
    # NONEXISTENT / DEGENERATE SHEET CONTENT (empty + divider) — robust.
    # ----------------------------------------------------------------------- #
    def test_empty_sheet_no_rows(self):
        fake = FakeSheets(rows=[])
        resp, body = self._post(fake, json={"sheetId": "empty"})
        self.assertEqual(resp.status_code, 200, body)
        self.assertEqual(body["totalViableProperties"], 0)
        self.assertEqual(body["isComplete"], False)
        self.assertEqual(body["completionPercentage"], 0)
        self._assert_no_send()

    def test_non_viable_divider_stops_counting(self):
        fake = FakeSheets(rows=[
            list(COMPLETE_ROW),
            ["NON-VIABLE"],
            list(INCOMPLETE_ROW),  # below divider — must be ignored
        ])
        resp, body = self._post(fake, json={"sheetId": "div"})
        self.assertEqual(resp.status_code, 200, body)
        self.assertEqual(body["totalViableProperties"], 1)
        self.assertEqual(body["isComplete"], True)
        self._assert_no_send()

    # ----------------------------------------------------------------------- #
    # DUPLICATE / RETRY — read-only route must be idempotent (no double effect).
    # ----------------------------------------------------------------------- #
    def test_duplicate_retry_idempotent(self):
        fake1 = FakeSheets(rows=[list(COMPLETE_ROW)])
        r1, b1 = self._post(fake1, json={"sheetId": "dup"})
        fake2 = FakeSheets(rows=[list(COMPLETE_ROW)])
        r2, b2 = self._post(fake2, json={"sheetId": "dup"})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(b1, b2)
        self._assert_read_only(fake1)
        self._assert_read_only(fake2)
        self._assert_no_send()

    # ----------------------------------------------------------------------- #
    # TOP-LEVEL NON-OBJECT JSON BODIES — handler assumes dict, only guards
    # `if not data`. A JSON string / number / array / bool is truthy and non-dict
    # -> data.get() raises AttributeError -> caught by bare except -> 500 leak.
    # Correct behavior: clean 4xx, no leaked internal error. (BUG — goes RED.)
    # ----------------------------------------------------------------------- #
    def test_body_json_string(self):
        fake = FakeSheets()
        resp, body = self._post(fake, data=json.dumps("hello"),
                                content_type="application/json")
        self._assert_robust(resp, body, fake, "body=json-string")

    def test_body_json_number(self):
        fake = FakeSheets()
        resp, body = self._post(fake, data=json.dumps(123),
                                content_type="application/json")
        self._assert_robust(resp, body, fake, "body=json-number")

    def test_body_json_array(self):
        fake = FakeSheets()
        resp, body = self._post(fake, data=json.dumps([1, 2, 3]),
                                content_type="application/json")
        self._assert_robust(resp, body, fake, "body=json-array")

    def test_body_json_bool(self):
        fake = FakeSheets()
        resp, body = self._post(fake, data=json.dumps(True),
                                content_type="application/json")
        self._assert_robust(resp, body, fake, "body=json-bool")

    # ----------------------------------------------------------------------- #
    # MALFORMED / NON-JSON BODY — the bare `except Exception` swallows the
    # werkzeug HTTPException raised by get_json() and re-emits it as a 500 with
    # the framework error text. Correct: clean 4xx. (BUG — goes RED.)
    # ----------------------------------------------------------------------- #
    def test_body_malformed_json(self):
        fake = FakeSheets()
        resp, body = self._post(fake, data="{not valid json",
                                content_type="application/json")
        self._assert_robust(resp, body, fake, "body=malformed")

    def test_body_wrong_content_type(self):
        fake = FakeSheets()
        resp, body = self._post(fake, data="sheetId=abc")  # no json content-type
        self._assert_robust(resp, body, fake, "body=wrong-content-type")

    def test_body_form_encoded(self):
        fake = FakeSheets()
        resp, body = self._post(fake, data={"sheetId": "abc"})  # form, not json
        self._assert_robust(resp, body, fake, "body=form-encoded")

    def test_body_empty_raw(self):
        fake = FakeSheets()
        resp, body = self._post(fake, data="", content_type="application/json")
        # empty JSON body -> get_json returns None -> "No JSON data" 400 (robust)
        self._assert_robust(resp, body, fake, "body=empty-raw")


if __name__ == "__main__":
    unittest.main(verbosity=2)
