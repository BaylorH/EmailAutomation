from datetime import datetime, timedelta, timezone

import pytest

from email_automation.worker_mailbox_readiness import read_worker_mailbox_readiness


class Snapshot:
    def __init__(self, data=None):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class Document:
    def __init__(self, snapshot=None, error=None):
        self._snapshot = snapshot
        self._error = error

    def get(self):
        if self._error:
            raise self._error
        return self._snapshot

    def collection(self, name):
        return Collection(self._children[name])


class Collection:
    def __init__(self, documents):
        self._documents = documents

    def document(self, name):
        return self._documents[name]


class Firestore:
    def __init__(self, current=None, reverse=None, error=None):
        current_doc = Document(snapshot=Snapshot(current), error=error)
        user_doc = Document()
        user_doc._children = {
            "graphSubscription": {"current": current_doc},
        }
        self._collections = {
            "users": {"user-1": user_doc},
            "graphSubscriptions": {
                "sub-1": Document(snapshot=Snapshot(reverse)),
            },
        }

    def collection(self, name):
        return Collection(self._collections[name])


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def active_subscription(**overrides):
    value = {
        "status": "active",
        "subscriptionId": "sub-1",
        "clientState": "state-1",
        "expirationDateTime": NOW + timedelta(days=2),
    }
    value.update(overrides)
    return value


def test_ready_requires_current_subscription_and_matching_reverse_lookup():
    result = read_worker_mailbox_readiness(
        Firestore(
            current=active_subscription(),
            reverse={"uid": "user-1", "clientState": "state-1"},
        ),
        "user-1",
        now=lambda: NOW,
    )

    assert result.ready is True
    assert result.reason == "ready"


def test_ready_accepts_the_iso_expiration_format_written_by_firebase_functions():
    result = read_worker_mailbox_readiness(
        Firestore(
            current=active_subscription(expirationDateTime="2026-07-16T00:00:00.000Z"),
            reverse={"uid": "user-1", "clientState": "state-1"},
        ),
        "user-1",
        now=lambda: NOW,
    )

    assert result.ready is True
    assert result.reason == "ready"


@pytest.mark.parametrize(
    ("current", "reverse"),
    [
        (None, None),
        (active_subscription(clientState=None), {"uid": "user-1", "clientState": None}),
        (active_subscription(expirationDateTime=NOW + timedelta(hours=23)), {"uid": "user-1", "clientState": "state-1"}),
        (active_subscription(expirationDateTime=NOW + timedelta(hours=24)), {"uid": "user-1", "clientState": "state-1"}),
        (active_subscription(), {"uid": "other-user", "clientState": "state-1"}),
        (active_subscription(), {"uid": "user-1", "clientState": "wrong"}),
    ],
)
def test_missing_malformed_expiring_and_reverse_mismatched_subscriptions_are_not_ready(current, reverse):
    result = read_worker_mailbox_readiness(
        Firestore(current=current, reverse=reverse),
        "user-1",
        now=lambda: NOW,
    )

    assert result.ready is False
    assert result.reason == "mailbox_not_ready"


def test_firestore_failures_are_retryable_and_redacted():
    result = read_worker_mailbox_readiness(
        Firestore(error=RuntimeError("private Firestore detail")),
        "user-1",
        now=lambda: NOW,
    )

    assert result.ready is False
    assert result.reason == "mailbox_readiness_unavailable"
    assert "private" not in repr(result)
