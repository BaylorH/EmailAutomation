import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault("SITESIFT_AUTO_REPLY_ALLOWLIST", "*")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import unittest
from unittest.mock import patch

from email_automation import processing


# ---------------------------------------------------------------------------
# Minimal path-addressed Firestore fake. It stores documents keyed by their
# full collection/document path so that the REAL messaging.index_message_id /
# lookup_thread_by_message_id (and the conversation variants) execute their
# real read/write logic against it. Nothing about the unit under test
# (_match_message_to_thread) or the index lookups is mocked -- only the
# Firestore transport is faked.
# ---------------------------------------------------------------------------
class _FakeSnapshot:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data or {})


class _FakeDocRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def collection(self, name):
        return _FakeCollectionRef(self._store, f"{self._path}/{name}")

    def set(self, data, merge=False):
        if merge and self._path in self._store:
            self._store[self._path] = {**self._store[self._path], **data}
        else:
            self._store[self._path] = dict(data)

    def get(self):
        return _FakeSnapshot(self._store.get(self._path))


class _FakeCollectionRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def document(self, doc_id):
        return _FakeDocRef(self._store, f"{self._path}/{doc_id}")


class _FakeFirestore:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return _FakeCollectionRef(self.store, name)


class CoreInboxMatchingWrongRecipientTests(unittest.TestCase):
    def test_reply_from_non_broker_sender_matches_origin_thread_not_other_campaign(self):
        """wrong_recipient: an inbound reply whose sender is NOT the expected
        broker still routes to its own thread (via In-Reply-To lineage) and is
        never misrouted into a different concurrent campaign's thread."""
        from email_automation import messaging

        fake_fs = _FakeFirestore()
        user_id = "uid-1"

        # Message-id of the outbound outreach that opened the CORRECT campaign.
        origin_msg_id = "<campaign-a-outreach@acme-listings.com>"
        origin_thread = "thread-campaign-a"

        # A completely separate, concurrent campaign the reply must NOT land in.
        other_msg_id = "<campaign-b-outreach@other-brokerage.com>"
        other_thread = "thread-campaign-b"

        with patch.object(messaging, "_fs", fake_fs):
            # Seed both campaigns through the REAL indexing functions.
            self.assertTrue(
                messaging.index_message_id(user_id, origin_msg_id, origin_thread)
            )
            self.assertTrue(
                messaging.index_conversation_id(user_id, "conv-a", origin_thread)
            )
            self.assertTrue(
                messaging.index_message_id(user_id, other_msg_id, other_thread)
            )
            self.assertTrue(
                messaging.index_conversation_id(user_id, "conv-b", other_thread)
            )

            # Inbound reply on the campaign-A thread, but the sender is a DIFFERENT
            # person than the broker we originally emailed (an assistant replying
            # on the broker's behalf) -- the "wrong recipient" of the expected
            # sender identity. Matching must ignore sender identity entirely.
            inbound_reply = {
                "id": "inbound-graph-id-1",
                "conversationId": "conv-a",
                "from": {
                    "emailAddress": {
                        "name": "Front Desk Assistant",
                        "address": "assistant@acme-listings.com",
                    }
                },
                "internetMessageHeaders": [
                    {"name": "In-Reply-To", "value": origin_msg_id},
                    {"name": "References", "value": origin_msg_id},
                ],
            }

            matched_thread = processing._match_message_to_thread(
                user_id, inbound_reply, {"Authorization": "Bearer token"}
            )

        # Sanity: the reply's sender is genuinely not the broker address that
        # opened the thread -- otherwise the test would prove nothing.
        self.assertNotEqual(
            inbound_reply["from"]["emailAddress"]["address"],
            "broker@acme-listings.com",
        )

        # Core assertion: matched to its OWN thread, and NOT misrouted to the
        # concurrent campaign, and not dropped (None).
        self.assertEqual(matched_thread, origin_thread)
        self.assertNotEqual(matched_thread, other_thread)
        self.assertIsNotNone(matched_thread)


if __name__ == "__main__":
    unittest.main()
