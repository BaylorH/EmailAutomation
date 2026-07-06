import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import unittest
from unittest.mock import patch

import requests

from email_automation import followup


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)


class FakeThreadSnapshot:
    def __init__(self, data):
        self.exists = True
        self._data = data

    def to_dict(self):
        return self._data


class FakeMessageDoc:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


class FakeMessagesCollection:
    def __init__(self, docs):
        self.docs = docs

    def where(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def stream(self):
        return self.docs


class FakeFollowupThreadNode:
    def __init__(self, updates, messages):
        self.updates = updates
        self.messages = messages

    def collection(self, name):
        if name != "messages":
            raise AssertionError(f"Unexpected thread collection: {name}")
        return FakeMessagesCollection(self.messages)

    def update(self, data):
        self.updates.append(data)


class FakeFollowupThreadsCollection:
    def __init__(self, updates, messages):
        self.updates = updates
        self.messages = messages

    def document(self, _thread_id):
        return FakeFollowupThreadNode(self.updates, self.messages)


class FakeFollowupUserNode:
    def __init__(self, updates, messages):
        self.updates = updates
        self.messages = messages

    def get(self):
        # User document lookup for signature settings.
        return FakeThreadSnapshot({"email": "baylor.freelance@outlook.com"})

    def collection(self, name):
        if name != "threads":
            raise AssertionError(f"Unexpected user collection: {name}")
        return FakeFollowupThreadsCollection(self.updates, self.messages)


class FakeFollowupFirestore:
    def __init__(self, messages):
        self.updates = []
        self.messages = messages

    def collection(self, name):
        if name != "users":
            raise AssertionError(f"Unexpected root collection: {name}")
        return self

    def document(self, _user_id):
        return FakeFollowupUserNode(self.updates, self.messages)


class CoreFollowupsBadPlaceholderTests(unittest.TestCase):
    """Rubric: core.followups / bad_placeholder.

    Proves the real followup._send_followup_email refuses to send a follow-up
    whose template still contains an unresolved [NAME] placeholder.
    """

    def test_unresolved_name_placeholder_blocks_followup_send(self):
        # A root outbound message exists so the reply-anchor lookup succeeds and
        # execution reaches the outbound-body safety gate.
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
        })
        fake_fs = FakeFollowupFirestore([outbound])

        # Template keeps a literal [NAME] token. thread_data intentionally omits
        # contactName / clientId / rowNumber so nothing resolves the placeholder
        # before the safety validation runs.
        followup_config = {
            "followUps": [{"message": "Hi [NAME],\n\nJust following up on the space."}],
        }
        thread_data = {
            "email": ["broker@example.com"],
        }

        with patch.object(followup, "_fs", fake_fs), \
             patch.object(
                 followup,
                 "exponential_backoff_request",
                 return_value=FakeResponse(200, {
                     "value": [{
                         "id": "graph-root",
                         "subject": "0 Gemini Ave",
                         "conversationId": "conv-1",
                     }]
                 }),
             ), \
             patch("email_automation.processing.is_contact_opted_out", return_value=None), \
             patch.object(requests, "post") as post:
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
            )

        # Blocked, not sent.
        self.assertFalse(result)
        post.assert_not_called()
        # Fail-closed with an actionable placeholder reason.
        self.assertTrue(followup._send_followup_email.guard_failed_closed)
        self.assertIsNotNone(followup._send_followup_email.last_error)
        self.assertIn("[NAME]", followup._send_followup_email.last_error)
        self.assertIn(
            "manual review required",
            followup._send_followup_email.last_error,
        )


if __name__ == "__main__":
    unittest.main()
