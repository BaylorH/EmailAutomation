import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation import scheduler_lease


class FakeSnapshot:
    def __init__(self, data=None):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class FakeDocRef:
    def __init__(self, data=None):
        self.data = data
        self.id = "emailAutomation"

    def get(self, transaction=None):
        return FakeSnapshot(self.data)


class FakeTransaction:
    def __init__(self):
        self.set_calls = []
        self.update_calls = []

    def set(self, ref, data, merge=False):
        self.set_calls.append((ref, data, merge))
        ref.data = {**(ref.data or {}), **data}

    def update(self, ref, data):
        self.update_calls.append((ref, data))
        ref.data = {**(ref.data or {}), **data}


class FakeFirestore:
    def __init__(self, existing=None):
        self.doc_ref = FakeDocRef(existing)
        self.tx = FakeTransaction()

    def transaction(self):
        return self.tx

    def collection(self, name):
        return self

    def document(self, name):
        return self.doc_ref


class SchedulerLeaseTests(unittest.TestCase):
    def test_unexpired_lease_blocks_second_runner(self):
        now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
        fs = FakeFirestore({
            "owner": "runner-a",
            "status": "running",
            "expiresAt": now + timedelta(minutes=10),
        })

        with patch.object(scheduler_lease, "transactional", lambda fn: fn):
            result = scheduler_lease.acquire_scheduler_lease(
                fs_client=fs,
                owner="runner-b",
                now=now,
            )

        self.assertFalse(result.acquired)
        self.assertEqual("runner-a", result.owner)
        self.assertEqual([], fs.tx.set_calls)

    def test_expired_lease_can_be_claimed_by_new_runner(self):
        now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
        fs = FakeFirestore({
            "owner": "runner-a",
            "status": "running",
            "expiresAt": now - timedelta(minutes=1),
        })

        with patch.object(scheduler_lease, "transactional", lambda fn: fn):
            result = scheduler_lease.acquire_scheduler_lease(
                fs_client=fs,
                owner="runner-b",
                ttl_seconds=1800,
                now=now,
            )

        self.assertTrue(result.acquired)
        self.assertEqual("runner-b", result.owner)
        self.assertEqual(1, len(fs.tx.set_calls))
        written = fs.tx.set_calls[0][1]
        self.assertEqual("running", written["status"])
        self.assertEqual("runner-b", written["owner"])
        self.assertGreater(written["expiresAt"], now)

    def test_release_only_marks_matching_owner_released(self):
        now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
        fs = FakeFirestore({
            "owner": "runner-a",
            "status": "running",
            "expiresAt": now + timedelta(minutes=10),
        })

        with patch.object(scheduler_lease, "transactional", lambda fn: fn):
            released = scheduler_lease.release_scheduler_lease(
                fs_client=fs,
                owner="runner-a",
                now=now,
            )

        self.assertTrue(released)
        self.assertEqual(1, len(fs.tx.update_calls))
        self.assertEqual("released", fs.tx.update_calls[0][1]["status"])


if __name__ == "__main__":
    unittest.main()
