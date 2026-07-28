import json
import sys
import types
import unittest
from unittest import mock

from email_automation import pending_responses


class FakeScope:
    def __init__(self):
        self.tags = {}
        self.contexts = {}

    def set_tag(self, key, value):
        self.tags[key] = value

    def set_context(self, key, value):
        self.contexts[key] = value


class FakeScopeContext:
    def __init__(self, sentry):
        self.sentry = sentry
        self.scope = FakeScope()

    def __enter__(self):
        self.sentry.scopes.append(self.scope)
        return self.scope

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeSentry:
    def __init__(self, events, raises=False):
        self.events = events
        self.raises = raises
        self.scopes = []

    def new_scope(self):
        return FakeScopeContext(self)

    def capture_message(self, message, level=None):
        self.events.append(("capture", message, level))
        if self.raises:
            raise RuntimeError("sentry unavailable")
        return "event-id-1"


class FakeDocRef:
    def __init__(self, events):
        self.events = events
        self.deleted = False

    def delete(self):
        self.events.append(("delete",))
        self.deleted = True


class FakePendingDoc:
    def __init__(self, doc_id, data, events):
        self.id = doc_id
        self._data = data
        self.reference = FakeDocRef(events)

    def to_dict(self):
        return self._data


class FakeCollection:
    def __init__(self, name, docs, events):
        self.name = name
        self.docs = docs
        self.events = events
        self.added = []

    def stream(self):
        return list(self.docs) if self.name == "pendingResponses" else []

    def add(self, payload):
        self.events.append(("dead_letter", payload["originalDocId"]))
        self.added.append(payload)


class FakeUserDoc:
    def __init__(self, collections):
        self.collections = collections

    def collection(self, name):
        return self.collections[name]


class FakeUsersCollection:
    def __init__(self, collections):
        self.collections = collections

    def document(self, _user_id):
        return FakeUserDoc(self.collections)


class FakeFirestore:
    def __init__(self, docs, events):
        self.collections = {
            "pendingResponses": FakeCollection("pendingResponses", docs, events),
            "deadLetterQueue": FakeCollection("deadLetterQueue", [], events),
        }

    def collection(self, name):
        if name != "users":
            raise AssertionError(f"unexpected collection {name!r}")
        return FakeUsersCollection(self.collections)


class PendingResponseObservabilityTests(unittest.TestCase):
    def _run_terminal_drop(self, *, sentry_raises=False):
        events = []
        doc = FakePendingDoc(
            "thread-secret-id",
            {
                "threadId": "thread-secret-id",
                "msgId": "message-secret-id",
                "recipient": "broker-secret@example.test",
                "clientId": "client-secret-id",
                "attempts": pending_responses.MAX_RESPONSE_ATTEMPTS,
                "lastError":
                    "token=secret-token body=private-message provider raw text",
            },
            events,
        )
        fake_fs = FakeFirestore([doc], events)
        fake_clients = types.SimpleNamespace(_fs=fake_fs)
        fake_sentry = FakeSentry(events, raises=sentry_raises)

        with mock.patch.dict(
            sys.modules,
            {"email_automation.clients": fake_clients},
        ), mock.patch.object(
            pending_responses,
            "_gate_pending_response",
            return_value=False,
        ), mock.patch.object(
            pending_responses,
            "sentry_sdk",
            fake_sentry,
            create=True,
        ):
            result = pending_responses.get_pending_responses("uid-secret-id")

        return events, doc, fake_fs, fake_sentry, result

    def test_max_attempt_terminal_drop_warns_before_durable_dead_letter_cleanup(self):
        events, doc, fake_fs, fake_sentry, result = self._run_terminal_drop()

        self.assertEqual(result, [])
        self.assertEqual(
            events[0],
            (
                "capture",
                "Pending response abandoned after 5 retries",
                "warning",
            ),
        )
        self.assertEqual(
            events[1:],
            [("dead_letter", "thread-secret-id"), ("delete",)],
        )
        self.assertTrue(doc.reference.deleted)
        self.assertEqual(
            fake_sentry.scopes[0].tags,
            {
                "worker": "pending_responses",
                "event_category": "max_attempt_terminal_drop",
            },
        )
        self.assertEqual(
            fake_sentry.scopes[0].contexts["pending_response"],
            {
                "source": "pendingResponses",
                "category": "max_attempt_terminal_drop",
                "attempts": pending_responses.MAX_RESPONSE_ATTEMPTS,
                "max_attempts": pending_responses.MAX_RESPONSE_ATTEMPTS,
            },
        )
        serialized_event = json.dumps(
            {
                "message": events[0][1],
                "level": events[0][2],
                "tags": fake_sentry.scopes[0].tags,
                "contexts": fake_sentry.scopes[0].contexts,
            },
            sort_keys=True,
        )
        for sensitive in (
            "uid-secret-id",
            "thread-secret-id",
            "message-secret-id",
            "broker-secret@example.test",
            "client-secret-id",
            "secret-token",
            "private-message",
            "provider raw text",
        ):
            self.assertNotIn(sensitive, serialized_event)
        self.assertEqual(
            len(fake_fs.collections["deadLetterQueue"].added),
            1,
        )

    def test_observability_failure_never_blocks_terminal_cleanup(self):
        events, doc, fake_fs, _fake_sentry, result = self._run_terminal_drop(
            sentry_raises=True
        )

        self.assertEqual(result, [])
        self.assertEqual(events[0][0], "capture")
        self.assertEqual(
            events[1:],
            [("dead_letter", "thread-secret-id"), ("delete",)],
        )
        self.assertTrue(doc.reference.deleted)
        self.assertEqual(
            len(fake_fs.collections["deadLetterQueue"].added),
            1,
        )


if __name__ == "__main__":
    unittest.main()
