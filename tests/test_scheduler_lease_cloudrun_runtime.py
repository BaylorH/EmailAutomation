"""Lease-owner + mutual-exclusion contract for the Cloud Run Job runtime.

The scheduler is migrating off GitHub Actions cron (which supplies
``GITHUB_RUN_ID`` as the natural lease owner) onto a Python Cloud Run Job on
Cloud Scheduler. In a Cloud Run container ``GITHUB_RUN_ID`` is unset, so the
lease owner must degrade to a stable ``hostname:pid`` identity WITHOUT losing
the single-runner mutual-exclusion guarantee that ``scheduler_lease`` provides.

These tests pin that behaviour so the Cloud Run migration can't silently
weaken the lease. They use the same in-memory FakeFirestore double as
``tests/test_scheduler_lease.py`` — no real Firestore is touched.
"""

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation import scheduler_lease


# --- In-memory Firestore double (mirrors tests/test_scheduler_lease.py) -------

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
        # Return a fresh transaction each call so a second acquisition in the
        # same process doesn't inherit the first one's recorded calls.
        self.tx = FakeTransaction()
        return self.tx

    def collection(self, name):
        return self

    def document(self, name):
        return self.doc_ref


# --- Env helper: strip every runtime id so we exercise the true fallback ------

RUNTIME_ID_ENV_VARS = ("GITHUB_RUN_ID", "RENDER_INSTANCE_ID")


def _clear_runtime_ids():
    env = dict(os.environ)
    for key in RUNTIME_ID_ENV_VARS:
        env.pop(key, None)
    return patch.dict(os.environ, env, clear=True)


class CloudRunLeaseOwnerTests(unittest.TestCase):
    def test_owner_falls_back_to_hostname_pid_when_github_run_id_unset(self):
        """With no GITHUB_RUN_ID (Cloud Run), owner is a hostname:pid identity."""
        import socket

        with _clear_runtime_ids():
            owner = scheduler_lease._default_owner()

        expected = f"{socket.gethostname()}:{os.getpid()}"
        self.assertEqual(expected, owner)
        # Shape guard: exactly "<host>:<pid>" with a numeric, current pid.
        host, _, pid = owner.rpartition(":")
        self.assertTrue(host)
        self.assertTrue(pid.isdigit())
        self.assertEqual(os.getpid(), int(pid))

    def test_owner_is_deterministic_within_a_process(self):
        """Repeated calls in one process yield the identical owner string."""
        with _clear_runtime_ids():
            first = scheduler_lease._default_owner()
            second = scheduler_lease._default_owner()
        self.assertEqual(first, second)

    def test_github_run_id_still_wins_when_present(self):
        """Migration must not break the existing GitHub Actions behaviour."""
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "gha-999"}, clear=False):
            self.assertEqual("gha-999", scheduler_lease._default_owner())

    def test_two_owners_cannot_both_hold_the_lease(self):
        """Mutual exclusion: once one hostname:pid owner holds a live lease,
        a second, different owner is refused — the core single-runner
        guarantee the Cloud Run migration must preserve."""
        now = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
        fs = FakeFirestore()  # empty: no lease yet

        with patch.object(scheduler_lease, "transactional", lambda fn: fn):
            first = scheduler_lease.acquire_scheduler_lease(
                fs_client=fs,
                owner="host-a:111",
                ttl_seconds=1800,
                now=now,
            )
            # Second, distinct owner attempts to grab the still-live lease.
            second = scheduler_lease.acquire_scheduler_lease(
                fs_client=fs,
                owner="host-b:222",
                ttl_seconds=1800,
                now=now,
            )

        self.assertTrue(first.acquired)
        self.assertEqual("host-a:111", first.owner)
        self.assertFalse(second.acquired)
        # The blocked runner sees the incumbent, not itself.
        self.assertEqual("host-a:111", second.owner)

    def test_default_owner_flows_into_acquire_and_is_honored(self):
        """When acquire() is called with no explicit owner (as main.py does via
        run_with_scheduler_lease), it stamps the hostname:pid identity."""
        now = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
        fs = FakeFirestore()

        with _clear_runtime_ids(), patch.object(
            scheduler_lease, "transactional", lambda fn: fn
        ):
            result = scheduler_lease.acquire_scheduler_lease(
                fs_client=fs,
                now=now,
            )
            expected = scheduler_lease._default_owner()

        self.assertTrue(result.acquired)
        self.assertEqual(expected, result.owner)
        self.assertEqual(expected, fs.doc_ref.data["owner"])


if __name__ == "__main__":
    unittest.main()
