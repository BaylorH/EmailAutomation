import sys
import types
import unittest
from unittest.mock import patch

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
    def __init__(self, events):
        self.events = events
        self.scopes = []

    def new_scope(self):
        return FakeScopeContext(self)

    def capture_message(self, message, level=None):
        self.events.append(("capture", message, level))
        return "event-id-1"


class RaisingSentry(FakeSentry):
    def capture_message(self, message, level=None):
        self.events.append(("capture_error", message, level))
        raise RuntimeError("sentry unavailable")


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


class FakePendingCollection:
    def __init__(self, docs):
        self.docs = docs

    def stream(self):
        return self.docs


class FakeUserDoc:
    def __init__(self, pending_collection):
        self.pending_collection = pending_collection

    def collection(self, name):
        self.assertEqual(name, "pendingResponses")
        return self.pending_collection

    def assertEqual(self, actual, expected):
        if actual != expected:
            raise AssertionError(f"{actual!r} != {expected!r}")


class FakeUsersCollection:
    def __init__(self, pending_collection):
        self.pending_collection = pending_collection

    def document(self, _user_id):
        return FakeUserDoc(self.pending_collection)


class FakeFirestore:
    def __init__(self, docs):
        self.docs = docs

    def collection(self, name):
        if name != "users":
            raise AssertionError(f"unexpected collection {name!r}")
        return FakeUsersCollection(FakePendingCollection(self.docs))


class PendingResponseObservabilityTests(unittest.TestCase):
    def test_max_attempt_pending_response_captures_sentry_warning_before_delete(self):
        events = []
        doc = FakePendingDoc(
            "thread-123",
            {
                "threadId": "thread-123",
                "msgId": "message-456",
                "recipient": "broker@example.com",
                "clientId": "client-789",
                "attempts": pending_responses.MAX_RESPONSE_ATTEMPTS,
                "lastError": "Graph send failed",
            },
            events,
        )
        fake_clients = types.SimpleNamespace(_fs=FakeFirestore([doc]))
        fake_sentry = FakeSentry(events)

        with patch.dict(sys.modules, {"email_automation.clients": fake_clients}):
            with patch.object(pending_responses, "sentry_sdk", fake_sentry, create=True):
                result = pending_responses.get_pending_responses("uid-1")

        self.assertEqual(result, [])
        self.assertEqual(events[0], (
            "capture",
            "Pending response abandoned after 5 retries",
            "warning",
        ))
        self.assertEqual(events[1], ("delete",))
        self.assertEqual(fake_sentry.scopes[0].tags["worker"], "pending_responses")
        self.assertEqual(fake_sentry.scopes[0].tags["client_id"], "client-789")
        self.assertEqual(fake_sentry.scopes[0].contexts["pending_response"], {
            "uid": "uid-1",
            "thread_id": "thread-123",
            "message_id": "message-456",
            "doc_id": "thread-123",
            "attempts": pending_responses.MAX_RESPONSE_ATTEMPTS,
            "max_attempts": pending_responses.MAX_RESPONSE_ATTEMPTS,
            "last_error": "Graph send failed",
            "recipient": "broker@example.com",
            "client_id": "client-789",
        })

    def test_sentry_failure_does_not_block_pending_response_cleanup(self):
        events = []
        doc = FakePendingDoc(
            "thread-123",
            {
                "threadId": "thread-123",
                "attempts": pending_responses.MAX_RESPONSE_ATTEMPTS,
                "lastError": "Graph send failed",
            },
            events,
        )
        fake_clients = types.SimpleNamespace(_fs=FakeFirestore([doc]))
        fake_sentry = RaisingSentry(events)

        with patch.dict(sys.modules, {"email_automation.clients": fake_clients}):
            with patch.object(pending_responses, "sentry_sdk", fake_sentry, create=True):
                result = pending_responses.get_pending_responses("uid-1")

        self.assertEqual(result, [])
        self.assertEqual(events[0][0], "capture_error")
        self.assertEqual(events[1], ("delete",))
        self.assertTrue(doc.reference.deleted)


if __name__ == "__main__":
    unittest.main()
