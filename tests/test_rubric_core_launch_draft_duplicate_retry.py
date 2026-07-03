import os

os.environ.setdefault("E2E_TEST_MODE", "true")

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import google.cloud.firestore as _gcf


# ---------------------------------------------------------------------------
# Fakes that model ONLY the Firestore datastore boundary. The unit under test
# (_claim_outbox_item and its transactional claim logic) is exercised for real.
# ---------------------------------------------------------------------------
class _FakeFsForImport:
    """Stand-in returned by firestore.Client() so email_automation.clients is
    importable offline (no ADC). Never used for the actual claim logic; the
    per-call _FakeFs below supplies the transaction the unit exercises."""

    def transaction(self):
        return _FakeTransaction()

    def __getattr__(self, name):
        raise AssertionError(
            f"real Firestore access '{name}' during test -- boundary not faked"
        )


# Patch the Firestore client constructor (datastore boundary) BEFORE importing
# the production module, whose clients.py does `_fs = firestore.Client()` at
# import time.
_gcf.Client = lambda *a, **k: _FakeFsForImport()

from email_automation.email import _claim_outbox_item, CLAIM_TIMEOUT_SECONDS, WORKER_ID


class _FakeSnapshot:
    def __init__(self, exists, data):
        self.exists = exists
        self._data = data

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _FakeDocRef:
    """A single Firestore document. Backing dict IS the persisted state."""

    def __init__(self, doc_id, store):
        self.id = doc_id
        self._store = store  # None => deleted / nonexistent
        self.reference = self

    def get(self, transaction=None):
        return _FakeSnapshot(self._store is not None, self._store)

    # values().update on the real doc goes through the transaction object below.


class _FakeTransaction:
    """Passthrough transaction: writes land directly on the fake doc's store,
    faithfully modeling a committed Firestore transaction."""

    def update(self, doc_ref, fields):
        doc_ref._store.update(fields)

    def delete(self, doc_ref):
        doc_ref._store = None


class _FakeFs:
    def transaction(self):
        return _FakeTransaction()


def _passthrough_transactional(fn):
    """Stand-in for google.cloud.firestore.transactional: runs the wrapped
    claim body once against our fake transaction (the retry/commit machinery
    is the datastore boundary, not the unit under test)."""

    def wrapper(transaction, *args, **kwargs):
        return fn(transaction, *args, **kwargs)

    return wrapper


class CoreLaunchDraftDuplicateRetryTests(unittest.TestCase):
    def _claim(self, doc_ref):
        """Invoke the REAL _claim_outbox_item with only the Firestore boundary
        (the _fs transaction factory + the @transactional decorator) faked."""
        with mock.patch("email_automation.clients._fs", _FakeFs()), \
             mock.patch("google.cloud.firestore.transactional", _passthrough_transactional):
            return _claim_outbox_item(doc_ref, {}, user_id="user-1")

    def test_same_launch_item_is_not_claimed_twice(self):
        """Proves the launch outbox item is claimed exactly once: a fresh item
        claims True, and an immediate duplicate retry of the SAME item claims
        False because a live claim already exists -- so the draft is never sent
        twice. Negative controls (fresh item, and a stale-claim reclaim) claim
        True, proving the duplicate False is discriminating, not blanket."""

        # --- Duplicate-retry: same item claimed twice back-to-back ---
        item = _FakeDocRef("launch-outbox-1", {"type": "launch"})

        first = self._claim(item)
        self.assertTrue(first, "fresh launch item must be claimable")
        # The real function recorded the claim in the datastore.
        self.assertEqual(item._store.get("processingBy"), WORKER_ID)
        self.assertIsNotNone(item._store.get("processingAt"))
        claimed_at = item._store["processingAt"]

        second = self._claim(item)
        self.assertFalse(
            second,
            "duplicate retry of an already-claimed launch item must be refused",
        )
        # The winning claim's ownership/timestamp is untouched by the loser.
        self.assertEqual(item._store.get("processingBy"), WORKER_ID)
        self.assertEqual(item._store["processingAt"], claimed_at)

        # --- Negative control 1: a DIFFERENT fresh item still claims True. ---
        other = _FakeDocRef("launch-outbox-2", {"type": "launch"})
        self.assertTrue(
            self._claim(other),
            "a distinct unclaimed item must claim True (guards against blanket-False)",
        )

        # --- Negative control 2: a STALE claim on the same item reclaims True. ---
        stale = _FakeDocRef(
            "launch-outbox-1",
            {
                "type": "launch",
                "processingBy": "dead-worker",
                "processingAt": datetime.now(timezone.utc)
                - timedelta(seconds=CLAIM_TIMEOUT_SECONDS + 60),
            },
        )
        self.assertTrue(
            self._claim(stale),
            "an expired claim must be reclaimable (proves False was due to a "
            "LIVE claim, not the mere presence of processingBy)",
        )
        self.assertEqual(stale._store.get("processingBy"), WORKER_ID)


if __name__ == "__main__":
    unittest.main()
