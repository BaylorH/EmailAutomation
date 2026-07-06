"""
Surface C — conversation / opt-out / sheet-completion auth + ownership.

Adversarial coverage for the mutating dashboard routes that previously trusted a
body-supplied `uid`/`sheetId` with no proof of ownership. These routes each have
a pre-existing FE-contract fuzz file (Group B); this file adds the cross-tenant
attack cases those files cannot express (they authenticate every request):

  POST /api/stop-conversation      (app.py :: api_stop_conversation)
  POST /api/resume-conversation    (app.py :: api_resume_conversation)
  POST /api/decline-property       (app.py :: api_decline_property)
  POST /api/clear-optout           (app.py :: api_clear_optout)
  POST /api/list-optouts           (app.py :: api_list_optouts)
  POST /api/check-sheet-completion (app.py :: api_check_sheet_completion)

Attack (GAP 1, high): an UNAUTHENTICATED actor POSTs {uid:<victim>, ...} and
mutates another tenant's thread / sheet / opt-out. Attack (GAP 2, medium): an
unauthenticated actor supplies an arbitrary `sheetId` to check-sheet-completion
and exfiltrates a foreign tenant's property data (IDOR).

FIX asserted here: @verify_firebase_token on every route; identity derived ONLY
from the verified token; the body uid never builds a Firestore path; the sheet
is resolved server-side from the token uid's client and a foreign sheetId is
refused. Every external boundary is faked; no email is ever sent.

(accept-new-property lives in test_surface_c_accept_new_property_authz.py.)
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod  # noqa: E402


# --------------------------------------------------------------------------- #
# Minimal faithful in-memory Firestore fake keyed by full document path, so we
# can prove identity is scoped by the TOKEN uid (not the body uid).
# --------------------------------------------------------------------------- #
class _Snap:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Doc:
    def __init__(self, store, path):
        self.store = store
        self.path = path

    def collection(self, name):
        return _Coll(self.store, self.path + (name,))

    def get(self):
        return _Snap(self.store.get(self.path))

    def update(self, data):
        self.store.setdefault("_writes", []).append((self.path, dict(data)))
        cur = self.store.get(self.path)
        if cur is None:
            raise Exception("no document to update")
        cur.update(dict(data))

    def delete(self):
        self.store.setdefault("_deletes", []).append(self.path)
        self.store.pop(self.path, None)


class _Coll:
    def __init__(self, store, prefix):
        self.store = store
        self.prefix = prefix

    def document(self, doc_id=None):
        if not isinstance(doc_id, str):
            raise TypeError(f"document id must be str, got {type(doc_id).__name__}")
        if "/" in doc_id:
            raise ValueError("document id must not contain '/'")
        return _Doc(self.store, self.prefix + (doc_id,))

    def stream(self):
        return []


class _FS:
    def __init__(self, store):
        self.store = store

    def collection(self, name):
        return _Coll(self.store, (name,))


VICTIM = "victim_uid"
ATTACKER = "attacker_uid"


class _AuthBase(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()
        self._send_patches = []
        for tgt in (
            "email_automation.email.send_and_index_email",
            "email_automation.email.send_outboxes",
            "email_automation.email.send_email",
        ):
            try:
                p = patch(tgt, MagicMock(name=tgt))
                p.start()
                self._send_patches.append(p)
            except (AttributeError, ModuleNotFoundError):
                pass
        self.addCleanup(self._stop_sends)

    def _stop_sends(self):
        for p in self._send_patches:
            p.stop()

    def _auth(self, uid):
        p = patch("firebase_admin.auth.verify_id_token", return_value={"uid": uid})
        p.start()
        self.addCleanup(p.stop)
        return {"Authorization": "Bearer testtoken"}

    def _json(self, resp):
        try:
            return resp.get_json() or {}
        except Exception:
            return {}


# =========================================================================== #
# GAP 1 — stop / resume conversation ownership
# =========================================================================== #
class StopResumeOwnership(_AuthBase):
    ROUTES = ("/api/stop-conversation", "/api/resume-conversation")

    def _fs_with_victim_thread(self):
        store = {
            ("users", VICTIM, "threads", "t_victim"): {
                "status": "paused", "clientId": "c_victim", "rowNumber": 3,
                "followUpStatus": "paused",
            }
        }
        return store, _FS(store)

    def test_unauthenticated_is_rejected_401(self):
        for route in self.ROUTES:
            store, fs = self._fs_with_victim_thread()
            with patch("email_automation.clients._fs", fs), \
                 patch("email_automation.messaging._fs", fs):
                resp = self.client.post(
                    route, json={"uid": VICTIM, "threadId": "t_victim"}
                )
            self.assertEqual(resp.status_code, 401, f"{route}: {self._json(resp)}")
            self.assertEqual(store.get("_writes", []), [], route)
            self.assertEqual(
                store[("users", VICTIM, "threads", "t_victim")]["status"],
                "paused", route,
            )

    def test_authenticated_attacker_cannot_touch_victim_thread(self):
        # Attacker authenticates as THEMSELVES but names the victim's uid+thread.
        # Identity is the token uid, so the lookup happens under the attacker's
        # own (empty) namespace -> 404, victim thread untouched.
        for route in self.ROUTES:
            store, fs = self._fs_with_victim_thread()
            headers = self._auth(ATTACKER)
            with patch("email_automation.clients._fs", fs), \
                 patch("email_automation.messaging._fs", fs):
                resp = self.client.post(
                    route,
                    json={"uid": VICTIM, "threadId": "t_victim", "clientId": "c_victim"},
                    headers=headers,
                )
            self.assertEqual(resp.status_code, 404, f"{route}: {self._json(resp)}")
            self.assertEqual(store.get("_writes", []), [], route)
            self.assertEqual(
                store[("users", VICTIM, "threads", "t_victim")]["status"],
                "paused", route,
            )

    def test_clientid_mismatch_is_refused_403(self):
        for route in self.ROUTES:
            store = {
                ("users", VICTIM, "threads", "t1"): {
                    "status": "paused", "clientId": "real_client", "rowNumber": 1,
                }
            }
            fs = _FS(store)
            headers = self._auth(VICTIM)
            with patch("email_automation.clients._fs", fs), \
                 patch("email_automation.messaging._fs", fs):
                resp = self.client.post(
                    route,
                    json={"uid": VICTIM, "threadId": "t1", "clientId": "WRONG"},
                    headers=headers,
                )
            self.assertEqual(resp.status_code, 403, f"{route}: {self._json(resp)}")
            self.assertEqual(store.get("_writes", []), [], route)


# =========================================================================== #
# GAP 1 — decline-property sheet ownership
# =========================================================================== #
class DeclinePropertyOwnership(_AuthBase):
    ROUTE = "/api/decline-property"

    def _post(self, payload, headers=None, authorized_sheet="s_owned"):
        sheets = MagicMock(name="sheets")
        batch = sheets.spreadsheets.return_value.batchUpdate
        gcc = MagicMock(return_value=(authorized_sheet, None, None))
        with patch("email_automation.clients._sheets_client", return_value=sheets), \
             patch("email_automation.clients._get_client_config", gcc), \
             patch("email_automation.sheets._first_sheet_props", return_value=(0, "Sheet1")):
            resp = self.client.post(self.ROUTE, json=payload, headers=headers or {})
        return resp, batch

    def test_unauthenticated_is_rejected_401(self):
        resp, batch = self._post(
            {"uid": VICTIM, "clientId": "c1", "rowNumber": 2, "sheetId": "s_owned"}
        )
        self.assertEqual(resp.status_code, 401, self._json(resp))
        self.assertFalse(batch.called, "no destructive delete on an unauth request")

    def test_foreign_sheetid_is_refused_403(self):
        headers = self._auth(ATTACKER)
        resp, batch = self._post(
            {"uid": ATTACKER, "clientId": "c1", "rowNumber": 2, "sheetId": "victim_sheet"},
            headers=headers, authorized_sheet="s_owned",
        )
        self.assertEqual(resp.status_code, 403, self._json(resp))
        self.assertFalse(batch.called, "no delete against a sheet the caller doesn't own")

    def test_owned_sheet_happy(self):
        headers = self._auth(VICTIM)
        resp, batch = self._post(
            {"uid": VICTIM, "clientId": "c1", "rowNumber": 2, "sheetId": "s_owned"},
            headers=headers, authorized_sheet="s_owned",
        )
        self.assertEqual(resp.status_code, 200, self._json(resp))
        self.assertTrue(batch.called)


# =========================================================================== #
# GAP 1 — clear-optout / list-optouts caller scoping
# =========================================================================== #
class OptoutAuth(_AuthBase):
    def test_clear_optout_unauthenticated_401(self):
        with patch("email_automation.clients._fs", MagicMock()):
            resp = self.client.post(
                "/api/clear-optout", json={"uid": VICTIM, "email": "x@example.com"}
            )
        self.assertEqual(resp.status_code, 401, self._json(resp))

    def test_list_optouts_unauthenticated_401(self):
        with patch("email_automation.clients._fs", MagicMock()):
            resp = self.client.post("/api/list-optouts", json={"uid": VICTIM})
        self.assertEqual(resp.status_code, 401, self._json(resp))

    def test_clear_optout_scopes_path_to_token_uid_not_body(self):
        # Body names VICTIM, token mints ATTACKER -> the Firestore path is built
        # from the TOKEN uid; nothing is deleted under the victim's namespace.
        store = {}
        fs = _FS(store)
        headers = self._auth(ATTACKER)
        with patch("email_automation.clients._fs", fs):
            resp = self.client.post(
                "/api/clear-optout",
                json={"uid": VICTIM, "email": "x@example.com"},
                headers=headers,
            )
        self.assertEqual(resp.status_code, 404, self._json(resp))
        self.assertEqual(store.get("_deletes", []), [])


# =========================================================================== #
# GAP 2 — check-sheet-completion IDOR
# =========================================================================== #
class CheckSheetCompletionIDOR(_AuthBase):
    ROUTE = "/api/check-sheet-completion"

    def _post(self, payload, headers=None, authorized_sheet="s_owned"):
        sheets = MagicMock(name="sheets")
        gcc = MagicMock(return_value=(authorized_sheet, None, None))
        with patch("email_automation.clients._sheets_client", return_value=sheets), \
             patch("email_automation.clients._get_client_config", gcc), \
             patch("email_automation.sheets._get_first_tab_title", return_value="Sheet1"), \
             patch("email_automation.sheets._read_header_row2",
                   return_value=["Property Address"]), \
             patch("email_automation.sheets._header_index_map",
                   return_value={"property address": 1}):
            (sheets.spreadsheets.return_value.values.return_value
                   .get.return_value.execute.return_value) = {"values": []}
            resp = self.client.post(self.ROUTE, json=payload, headers=headers or {})
        return resp, gcc

    def test_unauthenticated_is_rejected_401(self):
        resp, gcc = self._post({"sheetId": "victim_sheet"})
        self.assertEqual(resp.status_code, 401, self._json(resp))
        self.assertFalse(gcc.called, "no sheet resolution on an unauth request")

    def test_missing_clientid_is_400(self):
        headers = self._auth(ATTACKER)
        resp, gcc = self._post({"sheetId": "victim_sheet"}, headers=headers)
        self.assertEqual(resp.status_code, 400, self._json(resp))

    def test_foreign_sheetid_is_refused_403(self):
        headers = self._auth(ATTACKER)
        resp, gcc = self._post(
            {"clientId": "c1", "sheetId": "victim_sheet"},
            headers=headers, authorized_sheet="s_owned",
        )
        self.assertEqual(resp.status_code, 403, self._json(resp))

    def test_sheetid_resolved_server_side_happy(self):
        headers = self._auth(VICTIM)
        resp, gcc = self._post({"clientId": "c1"}, headers=headers,
                               authorized_sheet="s_owned")
        self.assertEqual(resp.status_code, 200, self._json(resp))
        self.assertTrue(gcc.called)
        # Resolution used the authenticated uid + client, not a body sheetId.
        self.assertEqual(gcc.call_args[0], (VICTIM, "c1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
