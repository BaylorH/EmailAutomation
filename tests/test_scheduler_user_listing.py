import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault("AZURE_API_APP_ID", "test-client-id")
os.environ.setdefault("AZURE_API_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("FIREBASE_API_KEY", "test-firebase-api-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-api-key")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import clients
import scheduler_runner


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class FakeDocSnapshot:
    def __init__(self, exists, data=None):
        self.exists = exists
        self._data = data or {}

    def to_dict(self):
        return self._data


class FakeDocRef:
    def __init__(self, users, uid):
        self.users = users
        self.uid = uid

    def get(self):
        if self.uid not in self.users:
            return FakeDocSnapshot(False)
        return FakeDocSnapshot(True, self.users[self.uid])


class FakeCollectionRef:
    def __init__(self, users):
        self.users = users

    def document(self, uid):
        return FakeDocRef(self.users, uid)


class FakeFirestore:
    def __init__(self, users):
        self.users = users

    def collection(self, name):
        if name != "users":
            raise AssertionError(f"unexpected collection {name}")
        return FakeCollectionRef(self.users)


class ExplodingFirestore:
    def collection(self, name):
        raise AssertionError(f"legacy send path touched Firestore collection {name}")


class SchedulerUserListingTests(unittest.TestCase):
    def test_list_user_ids_skips_api_key_and_non_mailbox_users(self):
        payload = {
            "items": [
                {"name": "msal_caches/real-user-1/msal_token_cache.bin"},
                {"name": "msal_caches/AIzaSyFakeFirebaseApiKey/msal_token_cache.bin"},
                {"name": "msal_caches/signup-no-mailbox/msal_token_cache.bin"},
                {"name": "msal_caches/missing-user-doc/msal_token_cache.bin"},
                {"name": "msal_caches/real-user-2/msal_token_cache.bin"},
                {"name": "msal_caches/real-user-1/other.bin"},
                {"name": "excels/real-user-1/responses.xlsx"},
            ]
        }
        fake_fs = FakeFirestore({
            "real-user-1": {"hasMsalToken": True, "email": "one@example.com"},
            "real-user-2": {"hasMsalToken": True, "email": "two@example.com"},
            "signup-no-mailbox": {"hasMsalToken": False, "email": "three@example.com"},
            "AIzaSyFakeFirebaseApiKey": {},
        })

        with patch.object(clients.requests, "get", return_value=FakeResponse(payload)), \
             patch.object(clients, "_fs", fake_fs):
            self.assertEqual(["real-user-1", "real-user-2"], clients.list_user_ids())

    def test_legacy_scheduler_runner_uses_same_mailbox_filter(self):
        payload = {
            "items": [
                {"name": "msal_caches/real-user-1/msal_token_cache.bin"},
                {"name": "msal_caches/AIzaSyFakeFirebaseApiKey/msal_token_cache.bin"},
                {"name": "msal_caches/signup-no-mailbox/msal_token_cache.bin"},
                {"name": "msal_caches/missing-user-doc/msal_token_cache.bin"},
                {"name": "msal_caches/real-user-2/msal_token_cache.bin"},
            ]
        }
        fake_fs = FakeFirestore({
            "real-user-1": {"hasMsalToken": True, "email": "one@example.com"},
            "real-user-2": {"hasMsalToken": True, "email": "two@example.com"},
            "signup-no-mailbox": {"hasMsalToken": False, "email": "three@example.com"},
        })

        with patch.object(scheduler_runner.requests, "get", return_value=FakeResponse(payload)), \
             patch.object(scheduler_runner, "_fs", fake_fs):
            self.assertEqual(["real-user-1", "real-user-2"], scheduler_runner.list_user_ids())

    def test_legacy_scheduler_runner_send_outboxes_is_disabled(self):
        with patch.object(scheduler_runner, "_fs", ExplodingFirestore()):
            with self.assertRaisesRegex(RuntimeError, "guarded email_automation.email.send_outboxes"):
                scheduler_runner.send_outboxes("real-user-1", {"Authorization": "Bearer test"})


if __name__ == "__main__":
    unittest.main()
