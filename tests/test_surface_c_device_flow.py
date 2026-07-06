"""
Surface-C adversarial hardening for the standalone auth_service (port 5001).

Two routes, both previously UNAUTHENTICATED with an attacker-controlled `uid`
read straight from the JSON body:

  POST /start-device-flow      (auth_service/auth_service.py: start_flow)
  POST /complete-device-flow   (auth_service/auth_service.py: complete_flow)

GAPS THIS PINS (both reproduced against the pre-hardening code):

  GAP-1 [HIGH] /complete-device-flow — no auth; `uid = request.json["uid"]` is
      fully attacker-controlled and the completed MSAL token is uploaded to
      Firebase under that uid (identity/mailbox confusion). `flows.get(uid)` can
      be None -> `acquire_token_by_device_flow(None)` -> unhandled 500.

  GAP-2 [MED] /start-device-flow — no auth or validation; `request.json["uid"]`
      raises KeyError/TypeError (500) on a missing uid / non-JSON body, and the
      module-level `flows` dict grows unbounded keyed by attacker-supplied uid
      (memory-exhaustion DoS).

CORRECT (post-hardening) contract, asserted below:
  * Both routes require a valid Firebase Bearer token (401 otherwise).
  * Identity is taken ONLY from the verified token uid; any body `uid` is
    ignored — the token is bound to g.firebase_uid.
  * Malformed / missing / non-object bodies fail closed with a clean 400 and no
    stack-trace leak (never a 500).
  * /complete with no active flow returns 400, never hands None to MSAL.
  * The `flows` map is bounded (TTL prune + hard cap).

Every external boundary is faked: firebase_admin.auth.verify_id_token controls
the token uid, msal_app.initiate/acquire_token_by_device_flow return canned
values, and upload_token (the Firebase network POST — the "send") is a MagicMock
that must only ever be called with the authenticated uid.
"""

import os
import sys
import time
import importlib.util
import unittest
from unittest.mock import patch, MagicMock

os.environ.setdefault("E2E_TEST_MODE", "true")
# firebase_admin is fully mocked in this suite (verify_id_token is patched and
# upload_token is a MagicMock), so no real service-account credentials are
# needed. Honor GOOGLE_APPLICATION_CREDENTIALS if the environment provides one,
# but never pin a maintainer-specific absolute path in a committed test.

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "auth_service"))

# The service lives in a subdirectory and is not importable as a top-level
# module; load it by path (its own imports resolve via REPO on sys.path).
_spec = importlib.util.spec_from_file_location(
    "auth_service_mod", os.path.join(REPO, "auth_service", "auth_service.py")
)
authmod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(authmod)

AUTH = {"Authorization": "Bearer testtoken"}

FAKE_FLOW = {
    "message": "Go to https://microsoft.com/devicelogin and enter CODE",
    "interval": 5,
    "user_code": "ABCD-EFGH",
    "verification_uri": "https://microsoft.com/devicelogin",
    "device_code": "dev-code-123",
    "expires_in": 900,
}


class DeviceFlowBase(unittest.TestCase):
    def setUp(self):
        authmod.app.testing = True
        self.client = authmod.app.test_client()
        authmod.flows.clear()

        # Fake identity: verify_id_token -> {"uid": <token uid>}.
        self._p_verify = patch("firebase_admin.auth.verify_id_token")
        self.verify_mock = self._p_verify.start()
        self.verify_mock.return_value = {"uid": "web_user"}

        # Fake MSAL device-flow calls.
        self._p_init = patch.object(
            authmod.msal_app, "initiate_device_flow", return_value=dict(FAKE_FLOW)
        )
        self._p_acq = patch.object(
            authmod.msal_app,
            "acquire_token_by_device_flow",
            return_value={"access_token": "AT", "token_type": "Bearer"},
        )
        self.init_mock = self._p_init.start()
        self.acq_mock = self._p_acq.start()

        # #20 identity-isolation: /start-device-flow now builds a fresh per-user MSAL
        # app + cache via _new_isolated_app() instead of the shared msal_app. Patch it
        # to a fake whose device-flow calls resolve to the same canned mocks (route
        # contract unchanged); initiate is routed through init_mock so the auth-gate
        # assertions (init not called on 401) still hold. /complete-device-flow tests
        # seed legacy {"flow","ts"} entries, which exercise the shared-msal_app fallback.
        self.fake_cache = MagicMock(name="isolated_cache")
        self.fake_cache.serialize.return_value = "{}"
        self.fake_app = MagicMock(name="isolated_app")
        self.fake_app.initiate_device_flow = self.init_mock
        self.fake_app.acquire_token_by_device_flow = self.acq_mock
        self.fake_app.get_accounts.return_value = [object()]
        self._p_isolated = patch.object(
            authmod, "_new_isolated_app", return_value=(self.fake_app, self.fake_cache)
        )
        self._p_isolated.start()

        # Fake the Firebase "send" boundary. Must only see the authenticated uid.
        self.upload_mock = MagicMock(name="upload_token")
        self._p_upload = patch.object(authmod, "upload_token", self.upload_mock)
        self._p_upload.start()

    def tearDown(self):
        self._p_verify.stop()
        self._p_init.stop()
        self._p_acq.stop()
        self._p_isolated.stop()
        self._p_upload.stop()
        authmod.flows.clear()

    def _set_token_uid(self, uid):
        self.verify_mock.return_value = {"uid": uid}

    def assert_no_stacktrace(self, resp):
        body = resp.get_data(as_text=True)
        self.assertNotIn("Traceback (most recent call last)", body)
        self.assertNotIn('File "', body)


# =========================================================================
# Auth gate — both routes must reject unauthenticated callers (GAP-1 & GAP-2).
# =========================================================================
class TestAuthRequired(DeviceFlowBase):
    def test_start_requires_bearer(self):
        r = self.client.post("/start-device-flow", json={})
        self.assertEqual(r.status_code, 401)
        self.assertFalse(self.init_mock.called)
        self.assertEqual(authmod.flows, {})

    def test_complete_requires_bearer(self):
        r = self.client.post("/complete-device-flow", json={"uid": "victim"})
        self.assertEqual(r.status_code, 401)
        self.assertFalse(self.acq_mock.called)
        self.assertFalse(self.upload_mock.called)

    def test_malformed_bearer_rejected(self):
        for hdr in ({"Authorization": "Bearer "}, {"Authorization": "Basic xyz"},
                    {"Authorization": "token abc"}):
            with self.subTest(hdr=hdr):
                r = self.client.post("/start-device-flow", json={}, headers=hdr)
                self.assertEqual(r.status_code, 401)

    def test_invalid_token_rejected(self):
        self.verify_mock.side_effect = ValueError("bad token")
        r = self.client.post("/start-device-flow", json={}, headers=AUTH)
        self.assertEqual(r.status_code, 401)
        self.assert_no_stacktrace(r)


# =========================================================================
# GAP-2: /start-device-flow validation + bounded flows.
# =========================================================================
class TestStartDeviceFlow(DeviceFlowBase):
    def test_happy_path_uses_token_uid(self):
        self._set_token_uid("alice")
        r = self.client.post("/start-device-flow", json={}, headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["user_code"], "ABCD-EFGH")
        # flow is stored under the AUTHENTICATED uid
        self.assertIn("alice", authmod.flows)
        self.assertIsInstance(authmod.flows["alice"], dict)
        self.assertIn("ts", authmod.flows["alice"])

    def test_body_uid_is_ignored_identity_from_token(self):
        # Attacker submits a victim uid in the body; it must NOT be used as a key.
        self._set_token_uid("attacker")
        r = self.client.post("/start-device-flow", json={"uid": "victim"}, headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertIn("attacker", authmod.flows)
        self.assertNotIn("victim", authmod.flows)

    def test_missing_uid_body_no_500(self):
        # Pre-hardening: request.json["uid"] -> KeyError -> 500. Now: clean 200
        # (uid comes from token; empty object is a valid body).
        r = self.client.post("/start-device-flow", json={}, headers=AUTH)
        self.assertNotEqual(r.status_code, 500)
        self.assert_no_stacktrace(r)

    def test_non_json_body_clean_400(self):
        # Pre-hardening: request.json on non-JSON -> 500. Now: clean 400.
        r = self.client.post(
            "/start-device-flow", data="not json", content_type="text/plain", headers=AUTH
        )
        self.assertEqual(r.status_code, 400)
        self.assert_no_stacktrace(r)

    def test_malformed_json_clean_400(self):
        r = self.client.post(
            "/start-device-flow", data='{"uid": ', content_type="application/json", headers=AUTH
        )
        self.assertEqual(r.status_code, 400)
        self.assert_no_stacktrace(r)

    def test_json_array_and_null_rejected(self):
        for label, data in (("array", "[1,2]"), ("null", "null"), ("int", "5")):
            with self.subTest(body=label):
                r = self.client.post(
                    "/start-device-flow", data=data,
                    content_type="application/json", headers=AUTH,
                )
                self.assertEqual(r.status_code, 400)
                self.assert_no_stacktrace(r)

    def test_unsafe_token_uid_rejected(self):
        # A token uid that fails the path-safe check must not reach the flows map.
        self._set_token_uid("../../etc/passwd")
        r = self.client.post("/start-device-flow", json={}, headers=AUTH)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(authmod.flows, {})

    def test_flows_map_is_bounded(self):
        # Even authenticated, the map cannot grow past the hard cap.
        authmod.flows.clear()
        cap = authmod._MAX_FLOWS
        now = time.time()
        for i in range(cap + 50):
            authmod.flows[f"u{i}"] = {"flow": dict(FAKE_FLOW), "ts": now}
        authmod._prune_flows()
        self.assertLessEqual(len(authmod.flows), cap)

    def test_expired_flows_pruned(self):
        authmod.flows.clear()
        stale = time.time() - (authmod._FLOW_TTL_SECONDS + 60)
        authmod.flows["old"] = {"flow": dict(FAKE_FLOW), "ts": stale}
        authmod.flows["fresh"] = {"flow": dict(FAKE_FLOW), "ts": time.time()}
        authmod._prune_flows()
        self.assertNotIn("old", authmod.flows)
        self.assertIn("fresh", authmod.flows)


# =========================================================================
# GAP-1: /complete-device-flow identity binding + None-flow crash.
# =========================================================================
class TestCompleteDeviceFlow(DeviceFlowBase):
    def _seed_flow(self, uid):
        authmod.flows[uid] = {"flow": dict(FAKE_FLOW), "ts": time.time()}

    def test_happy_path_uploads_under_token_uid(self):
        self._set_token_uid("alice")
        self._seed_flow("alice")
        r = self.client.post("/complete-device-flow", json={}, headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"status": "ok"})
        self.assertEqual(self.upload_mock.call_count, 1)
        _, kwargs = self.upload_mock.call_args
        self.assertEqual(kwargs.get("user_id"), "alice")
        self.assertNotIn("alice", authmod.flows)  # consumed

    def test_body_uid_cannot_hijack_identity(self):
        # THE GAP-1 ATTACK: attacker authenticates as themselves, completes their
        # own MS device flow, but submits uid='victim' to bind the token to the
        # victim. Hardened: identity is the token uid; the body uid is ignored,
        # so the token is uploaded under the attacker, never the victim.
        self._set_token_uid("attacker")
        self._seed_flow("attacker")
        r = self.client.post(
            "/complete-device-flow", json={"uid": "victim"}, headers=AUTH
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.upload_mock.call_count, 1)
        _, kwargs = self.upload_mock.call_args
        self.assertEqual(kwargs.get("user_id"), "attacker")
        self.assertNotEqual(kwargs.get("user_id"), "victim")

    def test_unknown_flow_returns_400_not_500(self):
        # Pre-hardening: flows.get(uid) is None -> acquire_token_by_device_flow(None)
        # crashes (500). Hardened: fail closed with 400, MSAL never called with None.
        self._set_token_uid("nobody")
        # no seeded flow for 'nobody'
        r = self.client.post("/complete-device-flow", json={"uid": "nobody"}, headers=AUTH)
        self.assertEqual(r.status_code, 400)
        self.assert_no_stacktrace(r)
        self.assertFalse(self.acq_mock.called, "MSAL was handed a missing flow")
        self.assertFalse(self.upload_mock.called)

    def test_non_json_body_clean_400(self):
        self._set_token_uid("alice")
        self._seed_flow("alice")
        r = self.client.post(
            "/complete-device-flow", data="xxx", content_type="text/plain", headers=AUTH
        )
        self.assertEqual(r.status_code, 400)
        self.assert_no_stacktrace(r)
        self.assertFalse(self.upload_mock.called)

    def test_admin_consent_branch_preserved(self):
        self._set_token_uid("alice")
        self._seed_flow("alice")
        self.acq_mock.return_value = {
            "error": "consent_required",
            "error_description": "AADSTS65001: admin consent is required.",
        }
        r = self.client.post("/complete-device-flow", json={}, headers=AUTH)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json().get("status"), "admin_needed")
        self.assertFalse(self.upload_mock.called)

    def test_generic_failure_does_not_leak_raw_result(self):
        self._set_token_uid("alice")
        self._seed_flow("alice")
        self.acq_mock.return_value = {
            "error": "authorization_pending",
            "error_description": "internal-only detail SECRET-XYZ",
        }
        r = self.client.post("/complete-device-flow", json={}, headers=AUTH)
        self.assertEqual(r.status_code, 400)
        body = r.get_data(as_text=True)
        self.assertNotIn("SECRET-XYZ", body)
        self.assertFalse(self.upload_mock.called)

    def test_msal_raises_is_contained(self):
        self._set_token_uid("alice")
        self._seed_flow("alice")
        self.acq_mock.side_effect = RuntimeError("boom internal")
        r = self.client.post("/complete-device-flow", json={}, headers=AUTH)
        self.assertGreaterEqual(r.status_code, 400)
        self.assertNotEqual(r.status_code, 200)
        self.assert_no_stacktrace(r)
        self.assertNotIn("boom internal", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
