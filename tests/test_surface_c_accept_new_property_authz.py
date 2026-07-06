"""
Surface C — authz / IDOR hardening for POST /api/accept-new-property.

Editable Flask mirror of the read-only Cloud Function
`exports.acceptNewProperty` (email-admin-ui/functions/index.js:1616).

Recon gap (adversarial): the handler took the caller's identity and target
sheet ENTIRELY from the request body — no Firebase token, `uid` trusted as-is,
and `propertyData.sheetId` written to with no check that the sheet belongs to
the caller's client / the notification under review. An attacker who reached
the URL could POST a victim `uid` + an arbitrary `sheetId` and inject rows /
drive OpenAI spend / corrupt a victim's sheet (IDOR + row-anchor corruption).

These tests pin the fix:
  1. no bearer token                       -> 401, no row written
  2. body uid != verified token uid        -> 403, no row written  (spoof guard)
  3. notification does not exist for user   -> 403, no row written  (IDOR guard)
  4. sheetId != the notification's sheetId  -> 403, no row written  (anchor guard)
  5. token uid + matching notification/sheet-> 200, exactly one row written

Every external boundary is faked so NOTHING real happens: no Google Sheets
call, no Firestore, no OpenAI. The single state-changing call
(insert_property_row_above_divider) is a MagicMock; we assert it is NOT called
on any rejected request.
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
TOKEN_UID = "user-1"
CLIENT_ID = "client-1"
NOTIF_ID = "notif-1"
CLIENT_SHEET_ID = "sheet-owned-by-user-1"


def _doc(exists, data=None):
    d = MagicMock()
    d.exists = exists
    d.to_dict.return_value = data or {}
    return d


class _RoutingFirestore:
    """
    Minimal Firestore double that resolves the exact path the handler walks:
        users/{uid}/clients/{clientId}/notifications/{notificationId}
    and the client-config read used by the AI branch:
        users/{uid}/clients/{clientId}

    `notifications` maps (uid, clientId, notifId) -> notification dict (or absent).
    `clients` maps (uid, clientId) -> client dict.
    """

    def __init__(self, notifications, clients):
        self._notifications = notifications
        self._clients = clients

    def collection(self, name):
        return _RoutingCollection(self, [(name, None)])


class _RoutingCollection:
    def __init__(self, root, path):
        self._root = root
        self._path = path

    def document(self, doc_id):
        return _RoutingDoc(self._root, self._path[:-1] + [(self._path[-1][0], doc_id)])


class _RoutingDoc:
    def __init__(self, root, path):
        self._root = root
        self._path = path

    def collection(self, name):
        return _RoutingCollection(self._root, self._path + [(name, None)])

    def _key(self):
        # path like [("users", uid), ("clients", cid)] or + [("notifications", nid)]
        return tuple(seg[1] for seg in self._path)

    def get(self):
        segs = [s[0] for s in self._path]
        ids = [s[1] for s in self._path]
        if segs == ["users", "clients", "notifications"]:
            data = self._root._notifications.get((ids[0], ids[1], ids[2]))
            return _doc(data is not None, data)
        if segs == ["users", "clients"]:
            data = self._root._clients.get((ids[0], ids[1]))
            return _doc(data is not None, data)
        # users/{uid} or anything else -> not found (safe default)
        return _doc(False, None)


class AcceptNewPropertyAuthz(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()

        self.insert_row = MagicMock(return_value=7)  # THE state-changing call
        self.apply_proposal = MagicMock(return_value={"applied": []})
        self.propose = MagicMock(return_value={"updates": []})

        # Notification exists for the legit user, anchored to CLIENT_SHEET_ID.
        notifications = {
            (TOKEN_UID, CLIENT_ID, NOTIF_ID): {
                "kind": "action_needed",
                "meta": {
                    "status": "pending_approval",
                    "address": "123 Main St",
                    "city": "Austin",
                    "sheetId": CLIENT_SHEET_ID,
                    "tabTitle": "Tab1",
                },
            }
        }
        clients = {
            (TOKEN_UID, CLIENT_ID): {"sheetId": CLIENT_SHEET_ID, "columnConfig": {}},
        }
        self.fake_fs = _RoutingFirestore(notifications, clients)

        self._patchers = [
            patch("email_automation.clients._sheets_client", MagicMock(return_value=MagicMock())),
            patch("email_automation.clients._fs", self.fake_fs),
            patch("email_automation.sheet_operations.insert_property_row_above_divider", self.insert_row),
            patch("email_automation.sheets._get_first_tab_title", MagicMock(return_value="Tab1")),
            patch("email_automation.sheets._read_header_row2", MagicMock(return_value=["address", "city", "email"])),
            patch("email_automation.sheets.format_sheet_columns_autosize_with_exceptions", MagicMock()),
            patch("email_automation.sheets.append_links_to_flyer_link_column", MagicMock()),
            patch("email_automation.sheets._read_row", MagicMock(return_value=[]), create=True),
            patch("email_automation.ai_processing.propose_sheet_updates", self.propose),
            patch("email_automation.ai_processing.apply_proposal_to_sheet", self.apply_proposal),
            patch("email_automation.column_config.get_default_column_config", MagicMock(return_value={})),
        ]
        for p in self._patchers:
            p.start()
        self.addCleanup(self._stop_all)

        self._verify_patch = patch(
            "firebase_admin.auth.verify_id_token", return_value={"uid": TOKEN_UID}
        )
        self.verify_mock = self._verify_patch.start()
        self.addCleanup(self._verify_patch.stop)
        self.AUTH = {"Authorization": "Bearer testtoken"}

    def _stop_all(self):
        for p in self._patchers:
            try:
                p.stop()
            except RuntimeError:
                pass

    def _property_data(self, **over):
        pd = {
            "address": "123 Main St",
            "city": "Austin",
            "sheetId": CLIENT_SHEET_ID,
            "tabTitle": "Tab1",
            "pdfLinks": [],
            "pdfManifest": [],
        }
        pd.update(over)
        return pd

    def _payload(self, **over):
        body = {
            "uid": TOKEN_UID,
            "clientId": CLIENT_ID,
            "notificationId": NOTIF_ID,
            "propertyData": self._property_data(),
        }
        body.update(over)
        return body

    def _post(self, payload, auth=True):
        headers = dict(self.AUTH) if auth else {}
        return self.client.post(ROUTE, json=payload, headers=headers)

    # ---- 1. no token -------------------------------------------------------
    def test_missing_token_rejected_401_no_row(self):
        resp = self._post(self._payload(), auth=False)
        self.assertEqual(resp.status_code, 401, resp.get_data(as_text=True))
        self.assertFalse(self.insert_row.called, "unauthenticated request wrote a row")

    def test_invalid_token_rejected_401_no_row(self):
        self.verify_mock.side_effect = ValueError("bad token")
        resp = self.client.post(ROUTE, json=self._payload(), headers={"Authorization": "Bearer nope"})
        self.assertEqual(resp.status_code, 401, resp.get_data(as_text=True))
        self.assertFalse(self.insert_row.called)

    # ---- 2. uid spoof ------------------------------------------------------
    def test_body_uid_mismatch_rejected_403_no_row(self):
        # Attacker authenticates as themselves but names a victim uid in the body.
        resp = self._post(self._payload(uid="victim-uid"))
        self.assertEqual(resp.status_code, 403, resp.get_data(as_text=True))
        self.assertFalse(resp.get_json().get("success"))
        self.assertFalse(self.insert_row.called, "uid-spoof request wrote a row")

    # ---- 3. IDOR: notification not under this user -------------------------
    def test_unknown_notification_rejected_403_no_row(self):
        resp = self._post(self._payload(notificationId="does-not-exist"))
        self.assertEqual(resp.status_code, 403, resp.get_data(as_text=True))
        self.assertFalse(self.insert_row.called, "unknown-notification request wrote a row")

    def test_unknown_client_rejected_403_no_row(self):
        resp = self._post(self._payload(clientId="not-my-client"))
        self.assertEqual(resp.status_code, 403, resp.get_data(as_text=True))
        self.assertFalse(self.insert_row.called)

    # ---- 4. row-anchor / sheet IDOR ---------------------------------------
    def test_foreign_sheetId_rejected_403_no_row(self):
        # Notification exists, but the request points the write at a DIFFERENT
        # sheet than the notification was raised for (a victim's Drive file id).
        pd = self._property_data(sheetId="victim-sheet-id")
        resp = self._post(self._payload(propertyData=pd))
        self.assertEqual(resp.status_code, 403, resp.get_data(as_text=True))
        self.assertFalse(self.insert_row.called, "foreign-sheet request wrote a row")
        self.assertFalse(self.apply_proposal.called)

    # ---- 5. happy path -----------------------------------------------------
    def test_authorized_request_writes_one_row(self):
        resp = self._post(self._payload())
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertTrue(resp.get_json().get("success"))
        self.assertEqual(self.insert_row.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
