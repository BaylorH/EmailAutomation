"""
Frontend-contract adversarial fuzz for POST /api/firestore-cleanup
(Admin firestore cleanup — DESTRUCTIVE).

HARDENED contract (app.py api_firestore_cleanup):
  * @verify_firebase_token — a missing/invalid Bearer token is rejected 401
    before any handler logic runs.
  * Gated by _destructive_admin_routes_enabled() (403 when disabled) and the
    SCHEDULER_AVAILABLE guard (503 when unavailable).
  * The body must be a JSON object (400 otherwise), destructive flags must be
    STRICT booleans (`is True`), clear_old_threads must be a real non-bool
    positive int, and user_id must be a non-empty string (400 otherwise).
  * user_id must EQUAL the verified caller uid — a signed-in user naming any
    other tenant's uid is rejected 403 "Forbidden" and never reaches Firestore.

Every external boundary is faked: firebase token verification is patched to a
chosen verified uid, email_automation.clients._fs is a recording FakeFirestore
that logs every .delete(), and email_automation.clients.list_user_ids is patched
so no real Firebase Storage HTTP call is made. This route has NO send/email
capability, so there is no Graph/send entrypoint to guard; instead the guard here
is that NOTHING is deleted on the fakes unless an authenticated caller supplied a
genuine, well-typed destructive instruction targeting THEIR OWN uid.
"""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)

import app as appmod
import email_automation.clients as cl

URL = "/api/firestore-cleanup"
# The verified Firebase caller identity used for authenticated requests. The
# happy-path store is keyed on this same uid so a caller only ever touches its own.
CALLER = "u1"


# --------------------------------------------------------------------------
# Recording fakes
# --------------------------------------------------------------------------
class FakeTs:
    """Stand-in for a Firestore timestamp with .timestamp()."""
    def __init__(self, t):
        self._t = t

    def timestamp(self):
        return self._t


class FakeDocSnap:
    def __init__(self, ref, data):
        self.reference = ref
        self._data = data

    def to_dict(self):
        return dict(self._data)


class FakeDocRef:
    def __init__(self, ctrl, path, submsgs=None):
        self.ctrl = ctrl
        self.path = path
        self.submsgs = submsgs or []

    def delete(self):
        self.ctrl.deleted.append(self.path)

    def collection(self, name):
        docs = [
            FakeDocSnap(FakeDocRef(self.ctrl, self.path + (name, mid)), {})
            for mid in self.submsgs
        ]
        return FakeCollectionRef(self.ctrl, docs)


class FakeCollectionRef:
    def __init__(self, ctrl, docs):
        self.ctrl = ctrl
        self._docs = docs

    def stream(self):
        return list(self._docs)


class FakeUserDocRef:
    def __init__(self, ctrl, uid):
        self.ctrl = ctrl
        self.uid = uid

    def collection(self, name):
        specs = self.ctrl.store.get(self.uid, {}).get(name, [])
        docs = []
        for spec in specs:
            path = ("users", self.uid, name, spec["id"])
            if path in self.ctrl.deleted:  # already deleted -> idempotent retries
                continue
            docs.append(
                FakeDocSnap(
                    FakeDocRef(self.ctrl, path, spec.get("messages", [])),
                    spec.get("data", {}),
                )
            )
        return FakeCollectionRef(self.ctrl, docs)


class FakeUsersCollection:
    def __init__(self, ctrl):
        self.ctrl = ctrl

    def document(self, uid):
        return FakeUserDocRef(self.ctrl, uid)


class FakeFirestore:
    def __init__(self, store=None):
        self.store = store or {}
        self.deleted = []

    def collection(self, name):
        # handler only ever uses collection("users")
        return FakeUsersCollection(self)


class FirestoreCleanupFuzz(unittest.TestCase):
    def setUp(self):
        self.client = appmod.app.test_client()
        # Route early-returns 503 unless the scheduler is available; in prod it is.
        # Force it on so the handler body (the thing under test) actually runs.
        self._sched = appmod.SCHEDULER_AVAILABLE
        appmod.SCHEDULER_AVAILABLE = True
        # Route early-returns 403 unless destructive admin routes are enabled.
        # In the target (non-prod dev) deployment this gate is open; enable it so
        # the handler body under test runs (mirrors the SCHEDULER_AVAILABLE force).
        self._destructive_patch = patch.object(
            appmod, "_destructive_admin_routes_enabled", lambda: True
        )
        self._destructive_patch.start()
        # Firebase ID-token verification: authorised requests resolve to the
        # verified caller uid (default CALLER). This drives the effective identity
        # the same way tests/test_surface_c_dashboard_auth.py does.
        self._p_verify = patch(
            "firebase_admin.auth.verify_id_token", return_value={"uid": CALLER}
        )
        self.verify_mock = self._p_verify.start()
        self.AUTH = {"Authorization": "Bearer testtoken"}

    def tearDown(self):
        appmod.SCHEDULER_AVAILABLE = self._sched
        self._destructive_patch.stop()
        self._p_verify.stop()

    # -- helpers ----------------------------------------------------------
    def _post(self, payload=None, store=None, users=("u1",), raw=None,
              content_type=None, caller=CALLER, auth=True):
        ctrl = FakeFirestore(store or {})
        # The verified caller identity for this request.
        self.verify_mock.return_value = {"uid": caller}
        headers = dict(self.AUTH) if auth else None
        with patch.object(cl, "_fs", ctrl), \
             patch.object(cl, "list_user_ids", lambda: list(users)):
            if raw is not None:
                r = self.client.post(URL, data=raw,
                                     content_type=content_type or "application/json",
                                     headers=headers)
            else:
                r = self.client.post(URL, json=(payload or {}), headers=headers)
        return r, ctrl

    def assert_no_server_error(self, r):
        body = r.get_json(silent=True) or {}
        self.assertLess(
            r.status_code, 500,
            f"handler returned {r.status_code} (unhandled crash / leak): {body}",
        )
        # Fail-closed responses must not leak Python/werkzeug internals.
        err = str(body.get("error", "")).lower()
        for leak in ("traceback", "not supported between instances",
                     "nonetype", "'>' not supported"):
            self.assertNotIn(leak, err, f"error text leaks internals: {body}")

    # =====================================================================
    # HAPPY PATH
    # =====================================================================
    def test_happy_dead_letter_only(self):
        store = {"u1": {"deadLetterQueue": [{"id": "d1"}, {"id": "d2"}]}}
        r, ctrl = self._post({"clear_dead_letter": True, "user_id": "u1"}, store=store)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["success"])
        # both dead-letter docs deleted, nothing else touched
        self.assertEqual(len(ctrl.deleted), 2)

    def test_happy_all_flags(self):
        store = {"u1": {
            "deadLetterQueue": [{"id": "d1"}],
            "processedMessages": [{"id": "p1"}],
            "sheetChangeLog": [{"id": "c1", "data": {"createdAt": FakeTs(0.0)}}],
            "threads": [{"id": "t1", "data": {"updatedAt": FakeTs(0.0)},
                          "messages": ["m1"]}],
        }}
        r, ctrl = self._post({
            "clear_dead_letter": True,
            "clear_processed_messages": True,
            "clear_sheet_change_log": True,
            "clear_old_threads": 30,
            "user_id": "u1",
        }, store=store)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["success"])
        # dead-letter + processed + changelog + thread + thread-message
        self.assertGreaterEqual(len(ctrl.deleted), 5)

    # =====================================================================
    # ROBUST no-ops / graceful handling (expected green)
    # =====================================================================
    def test_empty_body_noop(self):
        r, ctrl = self._post({})
        self.assert_no_server_error(r)
        self.assertEqual(ctrl.deleted, [], "empty body must not delete anything")

    def test_all_flags_false_noop(self):
        store = {"u1": {"deadLetterQueue": [{"id": "d1"}]}}
        r, ctrl = self._post({
            "clear_dead_letter": False,
            "clear_processed_messages": False,
            "clear_sheet_change_log": False,
            "clear_old_threads": 0,
            "user_id": "u1",
        }, store=store)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ctrl.deleted, [])

    def test_negative_thread_days_noop(self):
        store = {"u1": {"threads": [{"id": "t1", "data": {"updatedAt": FakeTs(0.0)}}]}}
        r, ctrl = self._post({"clear_old_threads": -5, "user_id": "u1"}, store=store)
        self.assert_no_server_error(r)
        self.assertEqual(ctrl.deleted, [], "negative day count must not delete")

    def test_null_user_id_rejected(self):
        # A null user_id is not a non-empty string -> rejected 400 (never fanned
        # out across all users), no crash, nothing deleted.
        r, ctrl = self._post({"user_id": None, "clear_dead_letter": True},
                             users=("u1", "u2"))
        self.assert_no_server_error(r)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(ctrl.deleted, [])

    def test_int_user_id_no_crash(self):
        r, ctrl = self._post({"user_id": 12345, "clear_dead_letter": True})
        self.assert_no_server_error(r)

    def test_oversized_user_id_noop(self):
        r, ctrl = self._post({"user_id": "A" * 10240})
        self.assert_no_server_error(r)
        self.assertEqual(ctrl.deleted, [])

    def _assert_foreign_uid_deletes_nothing(self, malicious_uid):
        """A malicious user_id that differs from the verified caller ("u1") must
        be refused by the ownership gate (403) and NEVER reach the delete loop.

        The store is seeded with a deletable dead-letter doc UNDER the malicious
        uid and clear_dead_letter=True is set, so if the caller==user_id
        ownership check ever regressed to trust the body uid, the loop would
        delete that doc and this assertion would catch it. On a DESTRUCTIVE
        endpoint "didn't crash" is not enough — "didn't delete" is the property."""
        store = {malicious_uid: {"deadLetterQueue": [{"id": "d1"}]}}
        r, ctrl = self._post(
            {"user_id": malicious_uid, "clear_dead_letter": True}, store=store,
        )
        self.assert_no_server_error(r)
        self.assertEqual(r.status_code, 403, r.get_json())
        self.assertEqual(
            ctrl.deleted, [],
            f"foreign user_id {malicious_uid!r} reached the delete loop",
        )

    def test_path_traversal_user_id_no_crash(self):
        self._assert_foreign_uid_deletes_nothing("../../etc/passwd")

    def test_file_scheme_user_id_no_crash(self):
        self._assert_foreign_uid_deletes_nothing("file:///etc/shadow")

    def test_placeholder_injection_user_id(self):
        self._assert_foreign_uid_deletes_nothing("[NAME]/[BROKER]")

    def test_script_tag_user_id(self):
        self._assert_foreign_uid_deletes_nothing("<script>alert(1)</script>")

    def test_unicode_newline_user_id(self):
        self._assert_foreign_uid_deletes_nothing("u\n1\t☃\U0001F4A9")

    def test_extra_unexpected_fields_ignored(self):
        r, ctrl = self._post({"user_id": "u1", "bogus": 1, "drop_table": True,
                              "__proto__": {"x": 1}})
        self.assert_no_server_error(r)
        self.assertEqual(ctrl.deleted, [])

    def test_bool_thread_days_graceful(self):
        # clear_old_threads must be a real (non-bool) positive int; `True` is a
        # bool, so it is treated as "don't clear" (0) rather than coerced. Must
        # not crash and must not delete.
        r, ctrl = self._post({"clear_old_threads": True, "user_id": "u1"})
        self.assert_no_server_error(r)
        self.assertEqual(ctrl.deleted, [])

    def test_retry_idempotent(self):
        store = {"u1": {"deadLetterQueue": [{"id": "d1"}]}}
        ctrl = FakeFirestore(store)
        self.verify_mock.return_value = {"uid": "u1"}
        with patch.object(cl, "_fs", ctrl), \
             patch.object(cl, "list_user_ids", lambda: ["u1"]):
            r1 = self.client.post(URL, json={"clear_dead_letter": True,
                                             "user_id": "u1"}, headers=self.AUTH)
            r2 = self.client.post(URL, json={"clear_dead_letter": True,
                                             "user_id": "u1"}, headers=self.AUTH)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        # Only one real delete despite two identical requests.
        self.assertEqual(len(ctrl.deleted), 1, "retry double-deleted")

    # =====================================================================
    # BUG-PINNING mutations (assert CORRECT behavior -> currently RED)
    # =====================================================================
    def test_bug_string_thread_days_500(self):
        # clear_old_threads sent as a string (realistic from a number input)
        # previously hit `"30" > 0` -> TypeError -> HTTP 500 leaking the raw
        # Python error. HARDENED contract: a non-int thread-days is coerced to
        # "don't clear" (0) — a clean 200 no-op, never a 500 and never a
        # deletion. Pin the exact status so a regression to 500 (crash) or a
        # silent deletion is caught.
        r, ctrl = self._post({"clear_old_threads": "30", "user_id": "u1"})
        self.assert_no_server_error(r)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(ctrl.deleted, [])

    def test_bug_array_thread_days_500(self):
        # Same class (array where int expected): coerced to a 200 no-op.
        r, ctrl = self._post({"clear_old_threads": [30], "user_id": "u1"})
        self.assert_no_server_error(r)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(ctrl.deleted, [])

    def test_bug_object_thread_days_500(self):
        # Same class (object where int expected): coerced to a 200 no-op.
        r, ctrl = self._post({"clear_old_threads": {"days": 30}, "user_id": "u1"})
        self.assert_no_server_error(r)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(ctrl.deleted, [])

    def test_bug_nonjson_body_500(self):
        # BUG B: non-JSON content-type -> request.get_json() raises 415 INSIDE
        # the try -> caught by `except Exception` -> HTTP 500 leaking the raw
        # werkzeug message. Should surface as 4xx, not a 500.
        r, ctrl = self._post(raw="not json at all", content_type="text/plain")
        self.assert_no_server_error(r)

    def test_bug_malformed_json_body_500(self):
        # BUG C: malformed JSON with a JSON content-type -> get_json() raises
        # 400 -> swallowed by `except Exception` -> re-emitted as HTTP 500.
        r, ctrl = self._post(raw="{ not valid json ",
                             content_type="application/json")
        self.assert_no_server_error(r)

    def test_bug_truthy_string_flag_deletes(self):
        # BUG D: destructive flags use bare `if flag:` truthiness. A caller that
        # sends the STRING "false" (or any non-empty string) triggers a real
        # deletion because non-empty strings are truthy. On a destructive
        # endpoint this must fail closed: a non-bool flag must NOT delete.
        store = {"u1": {"deadLetterQueue": [{"id": "d1"}]}}
        r, ctrl = self._post({"clear_dead_letter": "false", "user_id": "u1"},
                             store=store)
        self.assert_no_server_error(r)
        self.assertEqual(
            ctrl.deleted, [],
            "string 'false' for clear_dead_letter caused a real deletion",
        )

    def test_bug_empty_string_user_id_fans_out_to_all(self):
        # BUG E: user_id == "" is falsy, so `[target_user] if target_user else
        # list_user_ids()` silently expands an empty/blank target into ALL
        # users. On a destructive endpoint a blank user_id should be rejected
        # (400), not fanned out across every account.
        store = {
            "u1": {"deadLetterQueue": [{"id": "d1"}]},
            "u2": {"deadLetterQueue": [{"id": "d2"}]},
        }
        r, ctrl = self._post({"user_id": "", "clear_dead_letter": True},
                             store=store, users=("u1", "u2"))
        # Correct: blank user_id is invalid -> rejected 400 fail-closed, nothing
        # deleted across all users (never fanned out via list_user_ids()).
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertEqual(
            ctrl.deleted, [],
            "blank user_id fanned a destructive op out across ALL users",
        )

    # =====================================================================
    # AUTHENTICATION + AUTHORIZATION gate (hardened contract)
    # =====================================================================
    def test_unauthenticated_is_rejected_no_delete(self):
        # No Bearer token -> 401 before the handler body runs; a well-formed
        # destructive instruction must NOT reach Firestore.
        store = {"u1": {"deadLetterQueue": [{"id": "d1"}]}}
        r, ctrl = self._post({"clear_dead_letter": True, "user_id": "u1"},
                             store=store, auth=False)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(ctrl.deleted, [], "unauthenticated request deleted data")

    def test_caller_cannot_target_another_user(self):
        # Authenticated as u1 but naming victim u2 as the cleanup target must be
        # Forbidden (403) and must never reach the Firestore delete loop — even
        # with a genuine, well-typed destructive flag and the admin gate open.
        store = {
            "u1": {"deadLetterQueue": [{"id": "d1"}]},
            "u2": {"deadLetterQueue": [{"id": "d2"}]},
        }
        r, ctrl = self._post({"user_id": "u2", "clear_dead_letter": True},
                             store=store, caller="u1", users=("u1", "u2"))
        self.assertEqual(r.status_code, 403, r.get_json())
        self.assertEqual(
            ctrl.deleted, [],
            "cross-tenant cleanup deleted another user's data",
        )


if __name__ == "__main__":
    unittest.main()
