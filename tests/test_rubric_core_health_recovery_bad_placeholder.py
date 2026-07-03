import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)

import unittest

from email_automation import system_health


class FakeSnapshot:
    """Mimics a Firestore document snapshot exposing to_dict()."""

    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


class FakeQuery:
    def __init__(self, docs):
        self._docs = docs

    def limit(self, *_args, **_kwargs):
        return self

    def stream(self):
        return list(self._docs)


class FakeUserNode:
    """A user document whose sub-collections back the health counts."""

    def __init__(self, collections):
        # collections: {collection_name: [snapshot, ...]}
        self._collections = collections

    def collection(self, name):
        return FakeQuery(self._collections.get(name, []))


class FakeFirestore:
    def __init__(self, collections):
        self._collections = collections

    def collection(self, name):
        if name != "users":
            raise AssertionError(f"Unexpected root collection: {name}")
        return self

    def document(self, _user_id):
        return FakeUserNode(self._collections)


# A dead-letter item produced when an outbound draft still contained an
# unresolved [NAME] placeholder and was dead-lettered for manual review.
# It has NOT been resolved (no acknowledged/discarded/reconciled/requeued
# status), so it represents an active, operator-visible failure.
PLACEHOLDER_DEAD_LETTER = {
    "failureReason": "placeholder [NAME] unresolved — manual review required",
    "status": "dead_letter",
    "deadLetteredAt": "2026-06-30T12:00:00Z",
}


class CoreHealthRecoveryBadPlaceholderTests(unittest.TestCase):
    """Rubric: core.health_recovery / bad_placeholder.

    Proves the real system_health.collect_user_health counts a
    placeholder-dead-lettered item as an ACTIVE dead letter, so overall
    health degrades to "warning" and surfaces the operator-visible failure.
    """

    def _collect(self, dead_letters):
        fake_fs = FakeFirestore({
            "outbox": [],
            "deadLetterQueue": dead_letters,
            "pendingResponses": [],
            "processingFailures": [],
        })
        # Token/graph are healthy so status is driven ONLY by the queues.
        return system_health.collect_user_health(
            "uid-1",
            fs_client=fake_fs,
            token_state={"status": "ok"},
            graph_state={"status": "ok"},
        )

    def test_placeholder_dead_letter_is_counted_and_degrades_health(self):
        health = self._collect([FakeSnapshot(PLACEHOLDER_DEAD_LETTER)])

        # The placeholder dead letter is counted as an active dead letter.
        self.assertEqual(health["queues"]["deadLetterQueue"], 1)
        # Health reflects the operator-visible failure.
        self.assertEqual(health["status"], "warning")

        # --- Negative control -------------------------------------------------
        # Same failure, but an operator has resolved it (status flips into the
        # RESOLVED set). It must NOT be counted, and health must return to
        # "healthy". This makes the assertion above discriminating: it proves
        # the count keys on active/unresolved state, not merely on presence of
        # a document in the deadLetterQueue collection.
        resolved = dict(PLACEHOLDER_DEAD_LETTER)
        resolved["status"] = "discarded"
        control = self._collect([FakeSnapshot(resolved)])

        self.assertEqual(control["queues"]["deadLetterQueue"], 0)
        self.assertEqual(control["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
