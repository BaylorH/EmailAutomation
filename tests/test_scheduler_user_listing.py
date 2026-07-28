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

    def test_every_legacy_raw_graph_helper_is_disabled_before_io_by_default(self):
        cases = (
            (
                scheduler_runner.send_remaining_questions_email,
                (
                    "uid-1",
                    "client-1",
                    {"Authorization": "Bearer test"},
                    "broker@example.com",
                    ["Clear height"],
                    "<thread@example.com>",
                    3,
                    "row-3",
                ),
            ),
            (
                scheduler_runner.send_closing_email,
                (
                    "uid-1",
                    "client-1",
                    {"Authorization": "Bearer test"},
                    "broker@example.com",
                    "<thread@example.com>",
                    3,
                    "row-3",
                ),
            ),
            (
                scheduler_runner.send_new_property_email,
                (
                    "uid-1",
                    "client-1",
                    {"Authorization": "Bearer test"},
                    "broker@example.com",
                    "123 Test Way",
                    "Houston",
                    3,
                ),
            ),
            (
                scheduler_runner.send_and_index_email,
                (
                    "uid-1",
                    {"Authorization": "Bearer test"},
                    "Hello",
                    ["broker@example.com"],
                    "client-1",
                    3,
                ),
            ),
            (
                scheduler_runner.send_weekly_email,
                (
                    {"Authorization": "Bearer test"},
                    ["broker@example.com"],
                ),
            ),
            (
                scheduler_runner.process_replies,
                (
                    {"Authorization": "Bearer test"},
                    "uid-1",
                ),
            ),
        )

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(
                "SITESIFT_ENABLE_LEGACY_EMAIL_OPERATIONS",
                None,
            )
            for function, arguments in cases:
                with self.subTest(function=function.__name__), patch.object(
                    scheduler_runner,
                    "requests",
                ) as graph_requests:
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "legacy direct-Graph",
                    ):
                        function(*arguments)
                    graph_requests.get.assert_not_called()
                    graph_requests.post.assert_not_called()
                    graph_requests.patch.assert_not_called()

    def test_legacy_scheduler_opt_in_requires_exact_migration_value(self):
        for value in ("true", " 1", "1 "):
            with self.subTest(value=repr(value)), patch.dict(
                os.environ,
                {"SITESIFT_ENABLE_LEGACY_EMAIL_OPERATIONS": value},
                clear=False,
            ), patch.object(scheduler_runner, "requests") as graph_requests:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "legacy direct-Graph",
                ):
                    scheduler_runner.send_weekly_email(
                        {"Authorization": "Bearer test"},
                        ["broker@example.com"],
                    )
                graph_requests.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
