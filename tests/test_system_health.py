import os
import unittest
from datetime import datetime, timezone

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation import system_health


class FakeCollection:
    def __init__(self, count):
        self.count = count

    def limit(self, count):
        return self

    def stream(self):
        return [object() for _ in range(self.count)]


class FakeDocRef:
    def __init__(self, root, path):
        self.root = root
        self.path = tuple(path)

    def collection(self, name):
        if name in self.root.counts:
            return FakeCollection(self.root.counts[name])
        return FakeNode(self.root, list(self.path) + ["collection", name])

    def set(self, data, merge=False):
        self.root.set_calls.append((self.path, data, merge))


class FakeNode:
    def __init__(self, root, path=None):
        self.root = root
        self.path = path or []

    def collection(self, name):
        key = name
        if key in self.root.counts:
            return FakeCollection(self.root.counts[key])
        return FakeNode(self.root, self.path + ["collection", name])

    def document(self, name):
        return FakeDocRef(self.root, self.path + ["document", name])


class FakeFirestore:
    def __init__(self, counts):
        self.counts = counts
        self.set_calls = []

    def collection(self, name):
        return FakeNode(self, ["collection", name])


class SystemHealthTests(unittest.TestCase):
    def test_collect_user_health_warns_on_backlog_counts(self):
        fs = FakeFirestore({
            "outbox": 2,
            "deadLetterQueue": 1,
            "pendingResponses": 0,
            "processingFailures": 3,
        })
        now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy", "source": "cached_access_token"},
            graph_state={"status": "healthy"},
            now=now,
        )

        self.assertEqual("warning", payload["status"])
        self.assertEqual(2, payload["queues"]["outbox"])
        self.assertEqual(1, payload["queues"]["deadLetterQueue"])
        self.assertEqual(3, payload["queues"]["processingFailures"])
        self.assertEqual("healthy", payload["token"]["status"])
        self.assertEqual("healthy", payload["graph"]["status"])

    def test_collect_user_health_errors_on_token_failure(self):
        fs = FakeFirestore({
            "outbox": 0,
            "deadLetterQueue": 0,
            "pendingResponses": 0,
            "processingFailures": 0,
        })

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "error", "error": "silent_auth_failed"},
            graph_state={"status": "unknown"},
        )

        self.assertEqual("error", payload["status"])
        self.assertEqual("silent_auth_failed", payload["token"]["error"])

    def test_write_user_health_replaces_dashboard_snapshot(self):
        fs = FakeFirestore({
            "outbox": 0,
            "deadLetterQueue": 0,
            "pendingResponses": 0,
            "processingFailures": 0,
        })
        payload = {"status": "healthy", "queues": {}}

        system_health.write_user_health("uid-1", payload, fs_client=fs)

        self.assertEqual(1, len(fs.set_calls))
        self.assertEqual(
            ("collection", "users", "document", "uid-1", "collection", "systemHealth", "document", "emailAutomation"),
            fs.set_calls[0][0],
        )
        self.assertFalse(fs.set_calls[0][2])


if __name__ == "__main__":
    unittest.main()
