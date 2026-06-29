import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)

from email_automation import processing


class FakeResponse:
    def __init__(self, payload=None):
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeDocument:
    def set(self, *_args, **_kwargs):
        return None


class FakeCollection:
    def document(self, _doc_id):
        return FakeDocument()


class FakeFirestore:
    def collection(self, _name):
        return FakeCollection()


class SourceMessageEnvelopeTests(unittest.TestCase):
    def test_source_message_envelope_preserves_reply_all_recipients(self):
        msg = {
            "id": "graph-msg-1",
            "internetMessageId": "<source@example.com>",
            "conversationId": "conv-1",
            "subject": "RE: 410 Genesis Blvd",
            "from": {
                "emailAddress": {
                    "name": "BP21 Broker",
                    "address": "bp21harrison@gmail.com",
                }
            },
            "sender": {
                "emailAddress": {
                    "name": "BP21 Sender",
                    "address": "bp21harrison@gmail.com",
                }
            },
            "replyTo": [
                {"emailAddress": {"address": "replyto-broker@example.com"}},
            ],
            "toRecipients": [
                {"emailAddress": {"address": "baylor.freelance@outlook.com"}},
            ],
            "ccRecipients": [
                {"emailAddress": {"address": "baylor@manifoldengineering.ai"}},
            ],
            "receivedDateTime": "2026-06-28T22:00:00Z",
        }

        envelope = processing._source_message_envelope(msg)

        self.assertEqual(envelope["graphMessageId"], "graph-msg-1")
        self.assertEqual(envelope["internetMessageId"], "<source@example.com>")
        self.assertEqual(envelope["fromEmail"], "bp21harrison@gmail.com")
        self.assertEqual(envelope["replyToEmails"], ["replyto-broker@example.com"])
        self.assertEqual(envelope["to"], ["baylor.freelance@outlook.com"])
        self.assertEqual(envelope["cc"], ["baylor@manifoldengineering.ai"])
        self.assertEqual(envelope["ccRecipients"], msg["ccRecipients"])

    def test_source_message_identity_meta_exposes_cc_for_dashboard_outbox(self):
        msg = {
            "id": "graph-msg-1",
            "internetMessageId": "<source@example.com>",
            "from": {
                "emailAddress": {
                    "address": "bp21harrison@gmail.com",
                }
            },
            "toRecipients": [
                {"emailAddress": {"address": "baylor.freelance@outlook.com"}},
            ],
            "ccRecipients": [
                {"emailAddress": {"address": "baylor@manifoldengineering.ai"}},
            ],
        }

        meta = processing._source_message_identity_meta(
            "graph-msg-1",
            "<source@example.com>",
            msg,
        )

        self.assertEqual(meta["sourceGraphMessageId"], "graph-msg-1")
        self.assertEqual(meta["ccEmails"], ["baylor@manifoldengineering.ai"])
        self.assertEqual(meta["sourceMessage"]["cc"], ["baylor@manifoldengineering.ai"])
        self.assertEqual(
            meta["sourceMessage"]["toRecipients"],
            msg["toRecipients"],
        )

    def test_batched_inbound_message_save_persists_cc_envelope(self):
        saved_messages = []
        msg = {
            "id": "graph-msg-1",
            "internetMessageId": "<source@example.com>",
            "conversationId": "conv-1",
            "subject": "RE: 410 Genesis Blvd",
            "from": {
                "emailAddress": {
                    "address": "bp21harrison@gmail.com",
                }
            },
            "sender": {
                "emailAddress": {
                    "address": "bp21harrison@gmail.com",
                }
            },
            "replyTo": [
                {"emailAddress": {"address": "replyto-broker@example.com"}},
            ],
            "toRecipients": [
                {"emailAddress": {"address": "baylor.freelance@outlook.com"}},
            ],
            "ccRecipients": [
                {"emailAddress": {"address": "baylor@manifoldengineering.ai"}},
            ],
            "receivedDateTime": "2026-06-28T22:00:00Z",
            "bodyPreview": "Question about timing",
            "internetMessageHeaders": [
                {"name": "In-Reply-To", "value": "<outbound@example.com>"},
            ],
        }

        with patch.object(processing, "exponential_backoff_request", return_value=FakeResponse({
            "body": {"contentType": "Text", "content": "Question about timing"},
            "hasAttachments": False,
        })), \
                patch.object(processing, "save_message", side_effect=lambda *args: saved_messages.append(args) or True), \
                patch.object(processing, "index_message_id", return_value=True), \
                patch.object(processing, "_fs", FakeFirestore()):
            processing._save_message_to_thread(
                "uid-1",
                "thread-1",
                msg,
                {"Authorization": "Bearer token"},
            )

        message_record = saved_messages[0][3]
        self.assertEqual(message_record["cc"], ["baylor@manifoldengineering.ai"])
        self.assertEqual(message_record["replyTo"], ["replyto-broker@example.com"])
        self.assertEqual(message_record["sourceMessage"]["cc"], ["baylor@manifoldengineering.ai"])


if __name__ == "__main__":
    unittest.main()
