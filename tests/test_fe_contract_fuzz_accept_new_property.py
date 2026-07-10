"""
Frontend-contract adversarial fuzz for POST /api/accept-new-property.

Target: app.api_accept_new_property (app.py:729).
Frontend caller: email-admin-ui/src/components/InlineNewPropertyCard.jsx handleSend()
    POST body: { uid, clientId, notificationId, propertyData: {
        address, city, link, notes, leasingCompany, leasingContact,
        brokerEmail, sheetId, tabTitle, pdfLinks[], pdfManifest[] } }

Every external boundary is faked so NOTHING real happens:
  - email_automation.clients._sheets_client   (Google Sheets client)
  - email_automation.clients._fs              (Firestore)
  - email_automation.sheet_operations.insert_property_row_above_divider  (row write = state change)
  - email_automation.sheets._get_first_tab_title / _read_header_row2 /
        format_sheet_columns_autosize_with_exceptions /
        append_links_to_flyer_link_column / _read_row
  - email_automation.ai_processing.propose_sheet_updates / apply_proposal_to_sheet
  - email_automation.column_config.get_default_column_config

SEND boundary note: this handler does NOT itself send email. The actual outreach
is queued to Firestore `outbox` by the FRONTEND and dispatched by a separate
worker. So there is no send/refresh entrypoint in this route to guard. We still
assert (a) the AI-write path (apply_proposal_to_sheet) is only reachable via
pdfManifest, and (b) NO real sheet row is written when the request is rejected.

The tests assert the handler should be ROBUST: reject bad input fail-closed
(4xx / {success:false}) WITHOUT an unhandled 500 that leaks an internal error
string or stack trace, and WITHOUT writing a sheet row. Where the current
handler instead raises an unhandled 500, the assertion is left RED to pin the
bug (do not weaken it).
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

ROUTE = "/api/accept-new-property"
ALLOWED_RECIPIENTS = {"bp21harrison@gmail.com", "baylor.freelance@outlook.com"}


def valid_property_data(**overrides):
    pd = {
        "address": "123 Main St",
        "city": "Austin",
        "link": "https://example.com/flyer.pdf",
        "notes": "corner unit",
        "leasingCompany": "Acme Realty",
        "leasingContact": "Jane Doe",
        "brokerEmail": "bp21harrison@gmail.com",
        "sheetId": "sheet-abc",
        "tabTitle": "Tab1",
        "pdfLinks": [],
        "pdfManifest": [],
    }
    pd.update(overrides)
    return pd


def valid_payload(**overrides):
    body = {
        "uid": "user-1",
        "clientId": "client-1",
        "notificationId": "notif-1",
        "propertyData": valid_property_data(),
    }
    body.update(overrides)
    return body


class AcceptNewPropertyFuzz(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()

        # --- fakes for every external boundary -------------------------------
        self.insert_row = MagicMock(return_value=7)  # THE state-changing call
        self.apply_proposal = MagicMock(return_value={"applied": []})  # AI write
        self.propose = MagicMock(return_value={"updates": []})
        self.read_single_row = MagicMock(return_value=[])
        self.should_block_openai = MagicMock(return_value=False)
        self.append_links = MagicMock()

        # Firestore double. The hardened handler now loads the notification
        # (users/{uid}/clients/{clientId}/notifications/{notificationId}) and
        # requires the request sheetId to match the notification's stored
        # meta.sheetId. This recursive node resolves EVERY collection/document
        # path to the same doc, which exists and is anchored to the default
        # valid sheetId ("sheet-abc") so the happy-path payload passes the
        # ownership guard. (uid-mismatch tests reject before this read.)
        self.notif_data = {
            "kind": "action_needed",
            "meta": {"status": "pending_approval", "sheetId": "sheet-abc",
                     "address": "123 Main St"},
            "sheetId": "sheet-abc",
            "columnConfig": {},
        }
        self.fake_fs = MagicMock()
        self.fake_fs.collection.return_value = self.fake_fs
        self.fake_fs.document.return_value = self.fake_fs

        def get_snapshot():
            snapshot = MagicMock()
            snapshot.exists = True
            snapshot.to_dict.return_value = dict(self.notif_data)
            return snapshot

        def merge_notification(payload, merge=False):
            if merge:
                self.notif_data.update(payload)
            else:
                self.notif_data = dict(payload)

        self.fake_fs.get.side_effect = get_snapshot
        self.fake_fs.set.side_effect = merge_notification

        # Identity now comes from a verified Firebase ID token; the valid payload
        # uid ("user-1") is the token uid, and every request carries a bearer.
        self._verify_patch = patch(
            "firebase_admin.auth.verify_id_token", return_value={"uid": "user-1"}
        )
        self.verify_mock = self._verify_patch.start()
        self.addCleanup(self._verify_patch.stop)
        self.AUTH = {"Authorization": "Bearer testtoken"}

        self._patchers = [
            patch("email_automation.clients._sheets_client", MagicMock(return_value=MagicMock())),
            patch("email_automation.clients._fs", self.fake_fs),
            patch("email_automation.sheet_operations.insert_property_row_above_divider", self.insert_row),
            patch("email_automation.sheets._get_first_tab_title", MagicMock(return_value="Tab1")),
            patch("email_automation.sheets._read_header_row2", MagicMock(return_value=["address", "city", "email"])),
            patch("email_automation.sheets.format_sheet_columns_autosize_with_exceptions", MagicMock()),
            patch("email_automation.sheets.append_links_to_flyer_link_column", self.append_links),
            patch("email_automation.sheet_operations._read_single_row", self.read_single_row),
            patch("email_automation.ai_processing.propose_sheet_updates", self.propose),
            patch("email_automation.ai_processing.apply_proposal_to_sheet", self.apply_proposal),
            patch.object(appmod, "should_block_openai_call", self.should_block_openai),
            patch("email_automation.column_config.get_default_column_config", MagicMock(return_value={})),
        ]
        for p in self._patchers:
            p.start()
        self.addCleanup(self._stop_all)

    def _stop_all(self):
        for p in self._patchers:
            try:
                p.stop()
            except RuntimeError:
                pass

    # --- assertion helpers ---------------------------------------------------
    def _post(self, payload, **kw):
        # Every authorised request carries the verified bearer token.
        headers = kw.pop("headers", None) or dict(self.AUTH)
        return self.client.post(ROUTE, json=payload, headers=headers, **kw)

    def assert_no_leak(self, resp, label):
        """A rejection must not surface an internal exception string / stack trace."""
        body = resp.get_data(as_text=True)
        low = body.lower()
        leaks = [
            "traceback", "attributeerror", "keyerror", "typeerror",
            "nonetype", "object has no attribute", "line 1 column",
            "did not attempt to load json", "could not understand",
        ]
        for tok in leaks:
            self.assertNotIn(
                tok, low,
                msg=f"[{label}] response leaks internal error detail '{tok}': {body[:300]}",
            )

    def assert_fail_closed(self, resp, label):
        """Bad input must be rejected 4xx with no unhandled 500, no leak, no row write."""
        self.assertNotEqual(
            resp.status_code, 500,
            msg=f"[{label}] handler returned unhandled 500 (should fail-closed 4xx): "
                f"{resp.get_data(as_text=True)[:300]}",
        )
        self.assertGreaterEqual(resp.status_code, 400, msg=f"[{label}] expected a 4xx rejection")
        self.assertLess(resp.status_code, 500, msg=f"[{label}] expected a 4xx rejection")
        self.assert_no_leak(resp, label)
        self.assertFalse(
            self.insert_row.called,
            msg=f"[{label}] rejected request still wrote a sheet row (unintended mutation)",
        )

    def assert_no_send_to_disallowed(self, label):
        """This route has no send; the only downstream write is apply_proposal_to_sheet.
        Guard that if it ever fires, it is not carrying a disallowed broker recipient."""
        for call in self.apply_proposal.call_args_list:
            for arg in list(call.args) + list(call.kwargs.values()):
                if isinstance(arg, str) and "@" in arg and arg not in ALLOWED_RECIPIENTS:
                    self.fail(f"[{label}] AI write path invoked with disallowed recipient '{arg}'")

    # ========================================================================
    # HAPPY PATH
    # ========================================================================
    def test_happy_path(self):
        resp = self._post(valid_payload())
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertTrue(data.get("success"))
        self.assertEqual(data.get("rowNumber"), 7)
        # state change: exactly one row written
        self.assertEqual(self.insert_row.call_count, 1)
        # no pdfManifest -> AI extraction path must NOT run
        self.assertFalse(self.apply_proposal.called)
        self.assert_no_send_to_disallowed("happy_path")

    def test_happy_path_with_pdf_manifest_runs_ai_but_no_bad_send(self):
        self.propose.return_value = {"updates": [{"column": "city", "newValue": "Austin"}]}
        self.apply_proposal.return_value = {"applied": [{"column": "city", "newValue": "Austin"}]}
        pd = valid_property_data(pdfManifest=[{"text": "some pdf text"}])
        resp = self._post(valid_payload(propertyData=pd))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertTrue(resp.get_json().get("success"))
        self.assertTrue(self.insert_row.called)
        self.read_single_row.assert_called_once()
        self.propose.assert_called_once()
        self.assert_no_send_to_disallowed("happy_pdf")

    def test_budget_deferral_is_retryable_and_does_not_write_a_sheet_row(self):
        self.should_block_openai.return_value = True
        pd = valid_property_data(pdfManifest=[{"text": "some pdf text"}])

        resp = self._post(valid_payload(propertyData=pd))

        self.assertEqual(resp.status_code, 503, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertFalse(body.get("success"))
        self.assertTrue(body.get("retryable"))
        self.assertEqual(body.get("code"), "openai_budget_deferred")
        self.insert_row.assert_not_called()
        self.propose.assert_not_called()

    # ========================================================================
    # REQUIRED-FIELD MUTATIONS (expected robust -> 400)
    # ========================================================================
    def test_missing_uid(self):
        p = valid_payload(); del p["uid"]
        self.assert_fail_closed(self._post(p), "missing_uid")

    def test_missing_clientId(self):
        p = valid_payload(); del p["clientId"]
        self.assert_fail_closed(self._post(p), "missing_clientId")

    def test_missing_notificationId(self):
        p = valid_payload(); del p["notificationId"]
        self.assert_fail_closed(self._post(p), "missing_notificationId")

    def test_missing_propertyData_key(self):
        # propertyData absent -> defaults to {} -> should reject on missing sheetId/address
        p = valid_payload(); del p["propertyData"]
        self.assert_fail_closed(self._post(p), "missing_propertyData_key")

    def test_missing_sheetId(self):
        p = valid_payload(propertyData=valid_property_data()); del p["propertyData"]["sheetId"]
        self.assert_fail_closed(self._post(p), "missing_sheetId")

    def test_missing_address(self):
        p = valid_payload(propertyData=valid_property_data()); del p["propertyData"]["address"]
        self.assert_fail_closed(self._post(p), "missing_address")

    # ========================================================================
    # NULL / EMPTY MUTATIONS
    # ========================================================================
    def test_null_uid(self):
        self.assert_fail_closed(self._post(valid_payload(uid=None)), "null_uid")

    def test_empty_uid(self):
        self.assert_fail_closed(self._post(valid_payload(uid="")), "empty_uid")

    def test_empty_notificationId(self):
        self.assert_fail_closed(self._post(valid_payload(notificationId="")), "empty_notificationId")

    def test_empty_sheetId(self):
        p = valid_payload(propertyData=valid_property_data(sheetId=""))
        self.assert_fail_closed(self._post(p), "empty_sheetId")

    def test_empty_address(self):
        p = valid_payload(propertyData=valid_property_data(address=""))
        self.assert_fail_closed(self._post(p), "empty_address")

    def test_null_propertyData(self):
        # BUG CANDIDATE: {"propertyData": null} -> data.get("propertyData", {}) returns None
        # -> None.get("address") -> AttributeError -> unhandled 500.
        self.assert_fail_closed(self._post(valid_payload(propertyData=None)), "null_propertyData")

    # ========================================================================
    # WRONG-TYPE MUTATIONS on propertyData (container)
    # ========================================================================
    def test_propertyData_is_string(self):
        # BUG CANDIDATE: "str".get(...) -> AttributeError -> 500
        self.assert_fail_closed(self._post(valid_payload(propertyData="oops")), "propertyData_string")

    def test_propertyData_is_array(self):
        # BUG CANDIDATE: [].get(...) -> AttributeError -> 500
        self.assert_fail_closed(self._post(valid_payload(propertyData=["a", "b"])), "propertyData_array")

    def test_propertyData_is_int(self):
        # BUG CANDIDATE: (5).get(...) -> AttributeError -> 500
        self.assert_fail_closed(self._post(valid_payload(propertyData=5)), "propertyData_int")

    def test_propertyData_is_bool(self):
        # BUG CANDIDATE: True.get(...) -> AttributeError -> 500
        self.assert_fail_closed(self._post(valid_payload(propertyData=True)), "propertyData_bool")

    # ========================================================================
    # WRONG-TYPE MUTATIONS on inner fields (handler may accept -> must not crash/send)
    # ========================================================================
    def test_address_is_int(self):
        p = valid_payload(propertyData=valid_property_data(address=42))
        resp = self._post(p)
        self.assertNotEqual(resp.status_code, 500,
                            msg=f"address=int crashed 500: {resp.get_data(as_text=True)[:300]}")
        self.assert_no_send_to_disallowed("address_int")

    def test_pdfLinks_wrong_type(self):
        # pdfLinks as a string instead of list -> len()/iteration risk
        p = valid_payload(propertyData=valid_property_data(pdfLinks="notalist"))
        resp = self._post(p)
        self.assertNotEqual(resp.status_code, 500,
                            msg=f"pdfLinks=string crashed 500: {resp.get_data(as_text=True)[:300]}")

    def test_pdfManifest_wrong_type(self):
        # pdfManifest as an object instead of list -> truthy -> AI branch entered
        p = valid_payload(propertyData=valid_property_data(pdfManifest={"k": "v"}))
        resp = self._post(p)
        self.assertNotEqual(resp.status_code, 500,
                            msg=f"pdfManifest=object crashed 500: {resp.get_data(as_text=True)[:300]}")
        self.assert_no_send_to_disallowed("pdfManifest_obj")

    # ========================================================================
    # OVERSIZED / INJECTION-ISH content in accepted string fields
    # ========================================================================
    def test_oversized_address(self):
        big = "A" * 10240
        p = valid_payload(propertyData=valid_property_data(address=big))
        resp = self._post(p)
        self.assertNotEqual(resp.status_code, 500,
                            msg=f"oversized address crashed 500: {resp.get_data(as_text=True)[:200]}")

    def test_injection_values(self):
        for val in [
            "../../../../etc/passwd",
            "file:///etc/passwd",
            "[BROKER]",
            "[NAME]",
            "<script>alert(1)</script>",
            "line1\nline2\r\nline3",
            "propriété café 日本語 ‮RTL",
            "=HYPERLINK(\"http://evil\")",  # sheet formula injection
        ]:
            with self.subTest(val=val):
                p = valid_payload(propertyData=valid_property_data(address=val, notes=val))
                resp = self._post(p)
                self.assertNotEqual(
                    resp.status_code, 500,
                    msg=f"injection value {val!r} crashed 500: {resp.get_data(as_text=True)[:200]}",
                )
                self.assert_no_send_to_disallowed(f"injection:{val[:12]}")

    # ========================================================================
    # NONEXISTENT ids (fake _fs returns not-found)
    # ========================================================================
    def test_nonexistent_client_with_pdf(self):
        # client doc exists=False (default). pdfManifest present so the _fs lookup runs.
        self.propose.return_value = {"updates": []}
        pd = valid_property_data(pdfManifest=[{"text": "x"}])
        resp = self._post(valid_payload(uid="ghost", clientId="ghost", propertyData=pd))
        # AI extraction is best-effort/try-except; must still succeed and not crash.
        self.assertNotEqual(resp.status_code, 500, resp.get_data(as_text=True)[:300])
        self.assert_no_send_to_disallowed("nonexistent_client")

    # ========================================================================
    # DUPLICATE / RETRY
    # ========================================================================
    def test_duplicate_retry(self):
        p = valid_payload()
        r1 = self._post(p)
        r2 = self._post(p)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assert_no_send_to_disallowed("duplicate")
        self.assertEqual(
            self.insert_row.call_count,
            1,
            "replaying the same notification must reuse its accepted row",
        )

    def test_post_insert_budget_deferral_retry_reuses_the_inserted_row(self):
        self.propose.side_effect = [
            appmod.BudgetDeferredError("budget reached after row insert"),
            {"updates": []},
        ]
        pd = valid_property_data(pdfManifest=[{"text": "some pdf text"}])
        payload = valid_payload(propertyData=pd)

        first = self._post(payload)
        second = self._post(payload)

        self.assertEqual(first.status_code, 503, first.get_data(as_text=True))
        self.assertTrue(first.get_json().get("retryable"))
        self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
        self.assertEqual(
            self.insert_row.call_count,
            1,
            "a retry after post-insert AI deferral must not add a second row",
        )
        self.assertEqual(second.get_json().get("rowNumber"), 7)

    # ========================================================================
    # UNEXPECTED EXTRA FIELDS
    # ========================================================================
    def test_extra_fields_ignored(self):
        p = valid_payload(evil="x", __proto__="y", admin=True)
        p["propertyData"]["extra"] = "ignored"
        resp = self._post(p)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:300])
        self.assertTrue(resp.get_json().get("success"))

    # ========================================================================
    # NON-JSON / MALFORMED BODY
    # ========================================================================
    def test_non_json_body(self):
        # BUG CANDIDATE: wrong content-type -> get_json() raises 415 which is
        # caught by the bare except -> re-emitted as 500 with leaked message.
        resp = self.client.post(ROUTE, data="not json at all", content_type="text/plain", headers=dict(self.AUTH))
        self.assert_fail_closed(resp, "non_json_body")

    def test_malformed_json_body(self):
        # BUG CANDIDATE: broken JSON with json content-type -> 400 raised then
        # caught -> re-emitted as 500 with leaked message.
        resp = self.client.post(ROUTE, data="{bad json", content_type="application/json", headers=dict(self.AUTH))
        self.assert_fail_closed(resp, "malformed_json_body")

    def test_empty_body(self):
        resp = self.client.post(ROUTE, data="", content_type="application/json", headers=dict(self.AUTH))
        self.assertNotEqual(resp.status_code, 500,
                            msg=f"empty body 500: {resp.get_data(as_text=True)[:200]}")
        self.assert_no_leak(resp, "empty_body")


if __name__ == "__main__":
    unittest.main(verbosity=2)
