"""Per-user lease contract for the webhook (Phase-1) migration.

The batch scheduler uses a single global lease (schedulerLeases/emailAutomation).
The webhook path processes ONE user per HTTP request, so it needs a lease keyed
per user — schedulerLeases/emailAutomation:{uid} — acting as a user-scoped
mutex with a SHORT TTL (a single user's run is seconds, not the whole batch).

These tests pin:
  * the per-user doc key + short TTL,
  * same-uid concurrent claim is refused (mutex),
  * different uids are independent,
  * run_with_user_lease acquires → runs → releases, and skips cleanly when locked.

They use the same in-memory FakeFirestore style as tests/test_scheduler_lease.py
(extended to key docs by id so distinct uids get distinct lease docs). No real
Firestore is touched. The GLOBAL lease path is deliberately NOT exercised here —
run_with_scheduler_lease/DEFAULT_LEASE_ID/DEFAULT_TTL_SECONDS stay untouched.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import scheduler_lease


# --- Multi-doc in-memory Firestore double (docs keyed by id) -----------------

class FakeSnapshot:
    def __init__(self, data=None):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class FakeDocRef:
    def __init__(self, doc_id, data=None):
        self.id = doc_id
        self.data = data

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
        # existing: {doc_id: data}
        self._docs = {}
        for doc_id, data in (existing or {}).items():
            self._docs[doc_id] = FakeDocRef(doc_id, data)
        self.tx = FakeTransaction()

    def transaction(self):
        # Fresh transaction per call so a second acquisition in the same test
        # doesn't inherit the first one's recorded calls.
        self.tx = FakeTransaction()
        return self.tx

    def collection(self, name):
        return self

    def document(self, name):
        if name not in self._docs:
            self._docs[name] = FakeDocRef(name, None)
        return self._docs[name]


IDENTITY_TRANSACTIONAL = lambda: patch.object(scheduler_lease, "transactional", lambda fn: fn)


class UserLeaseKeyAndTtlTests(unittest.TestCase):
    def test_doc_key_is_namespaced_per_uid(self):
        self.assertEqual(
            "emailAutomation:user-123",
            scheduler_lease.user_lease_id("user-123"),
        )

    def test_user_lease_ttl_is_short(self):
        # A single user's run is seconds; the short TTL bounds how long a
        # crashed webhook run can wedge that one user (10 min), independent of
        # the 45-min global batch TTL.
        self.assertEqual(10 * 60, scheduler_lease.DEFAULT_USER_LEASE_TTL_SECONDS)
        self.assertLess(
            scheduler_lease.DEFAULT_USER_LEASE_TTL_SECONDS,
            scheduler_lease.DEFAULT_TTL_SECONDS,
        )

    def test_global_lease_constants_untouched(self):
        # Guardrail: the global batch lease path must remain intact for the GHA cron.
        self.assertEqual("emailAutomation", scheduler_lease.DEFAULT_LEASE_ID)
        self.assertEqual(45 * 60, scheduler_lease.DEFAULT_TTL_SECONDS)


class UserLeaseMutexTests(unittest.TestCase):
    def test_second_concurrent_same_uid_claim_is_refused(self):
        now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
        fs = FakeFirestore()

        with IDENTITY_TRANSACTIONAL():
            first = scheduler_lease.acquire_user_lease(
                "userA", fs_client=fs, owner="worker-1", now=now,
            )
            # userA still held (not released): a second, distinct owner is refused.
            second = scheduler_lease.acquire_user_lease(
                "userA", fs_client=fs, owner="worker-2", now=now,
            )

        self.assertTrue(first.acquired)
        self.assertEqual("emailAutomation:userA", first.lease_id)
        self.assertFalse(second.acquired)
        self.assertEqual("worker-1", second.owner)  # sees incumbent, not itself

    def test_different_uids_are_independent(self):
        now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
        fs = FakeFirestore()

        with IDENTITY_TRANSACTIONAL():
            a = scheduler_lease.acquire_user_lease(
                "userA", fs_client=fs, owner="worker-1", now=now,
            )
            # userA held by worker-1; a DIFFERENT uid must still be claimable.
            b = scheduler_lease.acquire_user_lease(
                "userB", fs_client=fs, owner="worker-2", now=now,
            )

        self.assertTrue(a.acquired)
        self.assertTrue(b.acquired)
        self.assertEqual("emailAutomation:userA", a.lease_id)
        self.assertEqual("emailAutomation:userB", b.lease_id)

    def test_expired_user_lease_can_be_reclaimed(self):
        now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
        fs = FakeFirestore({
            "emailAutomation:userA": {
                "owner": "stale-worker",
                "status": "running",
                "expiresAt": now - timedelta(minutes=1),
            }
        })

        with IDENTITY_TRANSACTIONAL():
            result = scheduler_lease.acquire_user_lease(
                "userA", fs_client=fs, owner="fresh-worker", now=now,
            )

        self.assertTrue(result.acquired)
        self.assertEqual("fresh-worker", result.owner)

    def test_release_user_lease_only_matches_owner(self):
        now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
        fs = FakeFirestore({
            "emailAutomation:userA": {
                "owner": "worker-1",
                "status": "running",
                "expiresAt": now + timedelta(minutes=10),
            }
        })

        with IDENTITY_TRANSACTIONAL():
            wrong = scheduler_lease.release_user_lease(
                "userA", fs_client=fs, owner="worker-2", now=now,
            )
            right = scheduler_lease.release_user_lease(
                "userA", fs_client=fs, owner="worker-1", now=now,
            )

        self.assertFalse(wrong)
        self.assertTrue(right)


class RunWithUserLeaseTests(unittest.TestCase):
    def test_acquires_runs_and_releases(self):
        fs = FakeFirestore()
        calls = []

        with IDENTITY_TRANSACTIONAL():
            ran = scheduler_lease.run_with_user_lease(
                "userA", lambda: calls.append("ran"),
                fs_client=fs, owner="worker-1",
            )

        self.assertTrue(ran)
        self.assertEqual(["ran"], calls)
        # Released in finally so the same user can be processed again next request.
        self.assertEqual("released", fs.document("emailAutomation:userA").data["status"])

    def test_skips_cleanly_when_locked(self):
        now_future = scheduler_lease._utc_now() + timedelta(minutes=9)
        fs = FakeFirestore({
            "emailAutomation:userA": {
                "owner": "other-worker",
                "status": "running",
                "expiresAt": now_future,
            }
        })
        calls = []

        with IDENTITY_TRANSACTIONAL():
            ran = scheduler_lease.run_with_user_lease(
                "userA", lambda: calls.append("ran"),
                fs_client=fs, owner="worker-1",
            )

        self.assertFalse(ran)
        self.assertEqual([], calls)  # callback never runs while locked

    def test_releases_even_when_callback_raises(self):
        fs = FakeFirestore()

        def boom():
            raise ValueError("downstream failure")

        with IDENTITY_TRANSACTIONAL():
            with self.assertRaises(ValueError):
                scheduler_lease.run_with_user_lease(
                    "userA", boom, fs_client=fs, owner="worker-1",
                )

        # Lease released despite the exception (finally), so retries aren't wedged.
        self.assertEqual("released", fs.document("emailAutomation:userA").data["status"])

    def test_different_uids_both_run(self):
        fs = FakeFirestore()
        ran = []

        with IDENTITY_TRANSACTIONAL():
            r1 = scheduler_lease.run_with_user_lease(
                "userA", lambda: ran.append("A"), fs_client=fs, owner="w1",
            )
            r2 = scheduler_lease.run_with_user_lease(
                "userB", lambda: ran.append("B"), fs_client=fs, owner="w1",
            )

        self.assertTrue(r1)
        self.assertTrue(r2)
        self.assertEqual(["A", "B"], ran)


if __name__ == "__main__":
    unittest.main()
