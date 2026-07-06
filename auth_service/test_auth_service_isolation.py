"""MSAL identity-isolation guard (CONDITIONAL-GO blocker #2).

The old auth_service shared ONE process-wide token cache + app across every
user, so /complete-device-flow uploaded the whole accumulated cache under the
current uid — mixing identities and risking sends AS THE WRONG USER. These
tests pin the isolation invariant WITHOUT a real device flow: MSAL is mocked so
we assert (a) each pending flow gets its own cache, (b) a successful completion
persists ONLY that user's single-identity cache, and (c) a cache that resolves
to != 1 account is refused (fail closed), never uploaded.
"""
import os
import sys
import types
import unittest
from unittest.mock import create_autospec, patch

os.environ.setdefault("API_APP_ID", "test-client-id")
os.environ.setdefault("FIREBASE_API_KEY", "test-firebase-key")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _load_module_with_mocked_msal():
    """Import auth_service with msal + firebase_helpers replaced by fakes."""
    created_apps = []

    class FakeCache:
        def __init__(self):
            self._id = id(self)
        def serialize(self):
            return f"cache-{self._id}"

    class FakePublicClientApplication:
        def __init__(self, client_id, authority=None, token_cache=None):
            self.token_cache = token_cache
            self.accounts = [{"username": "user@example.com"}]  # default: 1 identity
            self.device_flow = {"user_code": "ABC", "message": "go here",
                                "interval": 5, "verification_uri": "https://ms/device"}
            self.acquire_result = {"access_token": "tok"}
            created_apps.append(self)
        def initiate_device_flow(self, scopes=None):
            return self.device_flow
        def acquire_token_by_device_flow(self, flow):
            return self.acquire_result
        def get_accounts(self):
            return self.accounts

    fake_msal = types.ModuleType("msal")
    fake_msal.PublicClientApplication = FakePublicClientApplication
    fake_msal.SerializableTokenCache = FakeCache

    # Autospec against the REAL upload_token so the fake enforces the production
    # signature — an impossible kwarg (e.g. a removed cache_content) fails the
    # test instead of being silently accepted by an unconstrained MagicMock.
    import firebase_helpers as _real_fh
    fake_fh = types.ModuleType("firebase_helpers")
    fake_fh.upload_token = create_autospec(_real_fh.upload_token)

    with patch.dict(sys.modules, {"msal": fake_msal, "firebase_helpers": fake_fh}):
        sys.modules.pop("auth_service", None)
        import auth_service as mod
        mod._created_apps = created_apps
        mod._upload_mock = fake_fh.upload_token
        return mod


class MsalIdentityIsolationTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module_with_mocked_msal()
        self.mod.flows.clear()
        self.mod._upload_mock.reset_mock()
        self.client = self.mod.app.test_client()

    def test_each_pending_flow_gets_its_own_cache_and_app(self):
        self.client.post("/start-device-flow", json={"uid": "userA"})
        self.client.post("/start-device-flow", json={"uid": "userB"})
        a = self.mod.flows["userA"]
        b = self.mod.flows["userB"]
        self.assertIsNot(a["cache"], b["cache"], "users must not share a token cache")
        self.assertIsNot(a["app"], b["app"], "users must not share an MSAL app")

    def test_completion_uploads_only_this_users_single_identity_cache(self):
        self.client.post("/start-device-flow", json={"uid": "userA"})
        cache_a = self.mod.flows["userA"]["cache"]
        resp = self.client.post("/complete-device-flow", json={"uid": "userA"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "ok")
        self.mod._upload_mock.assert_called_once()
        _, kwargs = self.mod._upload_mock.call_args
        self.assertEqual(kwargs["user_id"], "userA")
        self.assertEqual(kwargs["cache_content"], cache_a.serialize(),
                         "must upload exactly this user's cache, not a shared one")
        self.assertNotIn("userA", self.mod.flows, "pending flow must be cleared after success")

    def test_multi_account_cache_is_refused_fail_closed(self):
        self.client.post("/start-device-flow", json={"uid": "userA"})
        # Simulate a cache that somehow resolved to TWO identities.
        self.mod.flows["userA"]["app"].accounts = [
            {"username": "a@example.com"}, {"username": "b@example.com"}]
        resp = self.client.post("/complete-device-flow", json={"uid": "userA"})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("identity_isolation_violation", resp.get_json()["error"])
        self.mod._upload_mock.assert_not_called()
        self.assertNotIn("userA", self.mod.flows)

    def test_zero_account_cache_is_refused_fail_closed(self):
        self.client.post("/start-device-flow", json={"uid": "userA"})
        self.mod.flows["userA"]["app"].accounts = []
        resp = self.client.post("/complete-device-flow", json={"uid": "userA"})
        self.assertEqual(resp.status_code, 409)
        self.mod._upload_mock.assert_not_called()

    def test_expired_pending_flow_is_pruned_on_completion(self):
        self.client.post("/start-device-flow", json={"uid": "userA"})
        # Age the pending entry past the TTL without any new /start-device-flow.
        self.mod.flows["userA"]["created"] -= self.mod._PENDING_TTL_SECONDS + 1
        resp = self.client.post("/complete-device-flow", json={"uid": "userA"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "no_pending_flow")
        self.mod._upload_mock.assert_not_called()
        self.assertNotIn("userA", self.mod.flows)

    def test_complete_without_pending_flow_is_rejected(self):
        resp = self.client.post("/complete-device-flow", json={"uid": "ghost"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "no_pending_flow")
        self.mod._upload_mock.assert_not_called()

    def test_two_users_completing_do_not_cross_contaminate_uploads(self):
        self.client.post("/start-device-flow", json={"uid": "userA"})
        self.client.post("/start-device-flow", json={"uid": "userB"})
        cache_a = self.mod.flows["userA"]["cache"].serialize()
        cache_b = self.mod.flows["userB"]["cache"].serialize()
        self.assertNotEqual(cache_a, cache_b)
        self.client.post("/complete-device-flow", json={"uid": "userA"})
        self.client.post("/complete-device-flow", json={"uid": "userB"})
        calls = {kwargs["user_id"]: kwargs["cache_content"]
                 for _, kwargs in self.mod._upload_mock.call_args_list}
        self.assertEqual(calls["userA"], cache_a)
        self.assertEqual(calls["userB"], cache_b)
        self.assertNotEqual(calls["userA"], calls["userB"],
                            "user A and user B must never receive each other's token cache")


if __name__ == "__main__":
    unittest.main()
