import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
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
    def _record_batched_authority(self, msg, body):
        with patch.object(
            processing,
            "exponential_backoff_request",
            return_value=FakeResponse({
                "body": {"contentType": "Text", "content": body},
                "hasAttachments": bool(msg.get("hasAttachments")),
            }),
        ), patch.object(
            processing,
            "_resolve_current_mailbox_email",
            return_value="operator@example.com",
        ), patch.object(processing, "save_message", return_value=True), patch.object(
            processing,
            "index_message_id",
            return_value=True,
        ), patch.object(processing, "_fs", FakeFirestore()), patch(
            "email_automation.followup.cancel_followup_on_response",
        ) as record_inbound:
            processing._save_message_to_thread(
                "uid-1",
                "thread-1",
                msg,
                {"Authorization": "Bearer token"},
            )
        return record_inbound

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

        self.assertEqual(meta["replyToMessageId"], "graph-msg-1")
        self.assertEqual(meta["sourceMessageId"], "graph-msg-1")
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
                patch.object(processing, "_resolve_current_mailbox_email", return_value="operator@example.com"), \
                patch.object(processing, "_fs", FakeFirestore()), \
                patch("email_automation.followup.cancel_followup_on_response") as record_inbound:
            processing._save_message_to_thread(
                "uid-1",
                "thread-1",
                msg,
                {"Authorization": "Bearer token"},
            )

        message_record = saved_messages[0][3]
        record_inbound.assert_called_once_with("uid-1", "thread-1")
        self.assertEqual(message_record["cc"], ["baylor@manifoldengineering.ai"])
        self.assertEqual(message_record["replyTo"], ["replyto-broker@example.com"])
        self.assertEqual(message_record["sourceMessage"]["cc"], ["baylor@manifoldengineering.ai"])

    def test_batched_quote_only_message_does_not_record_inbound_authority(self):
        quoted_only = (
            "On Thu, Aug 6, 2026 at 9:00 PM Baylor wrote:\n"
            "> Can you confirm the available power?"
        )
        msg = {
            "id": "graph-quote-only",
            "internetMessageId": "<quote-only@example.com>",
            "subject": "RE: 410 Genesis Blvd",
            "from": {"emailAddress": {"address": "bp21harrison@gmail.com"}},
            "receivedDateTime": "2026-08-07T04:01:00Z",
            "bodyPreview": quoted_only,
            "hasAttachments": False,
        }

        record_inbound = self._record_batched_authority(msg, quoted_only)
        record_inbound.assert_not_called()

    def test_batched_auto_reply_does_not_record_inbound_authority(self):
        msg = {
            "id": "graph-auto-reply",
            "internetMessageId": "<auto-reply@example.com>",
            "subject": "Automatic reply: RE: 410 Genesis Blvd",
            "from": {"emailAddress": {"address": "broker@example.com"}},
            "sender": {"emailAddress": {"address": "broker@example.com"}},
            "receivedDateTime": "2026-08-07T04:02:00Z",
            "bodyPreview": "I am out of the office until Monday.",
            "hasAttachments": False,
            "internetMessageHeaders": [
                {"name": "Auto-Submitted", "value": "auto-replied"},
            ],
        }

        record_inbound = self._record_batched_authority(
            msg,
            "I am out of the office until Monday.",
        )
        record_inbound.assert_not_called()

    def test_batched_self_email_does_not_record_inbound_authority(self):
        msg = {
            "id": "graph-self-email",
            "internetMessageId": "<self-email@example.com>",
            "subject": "FW: 410 Genesis Blvd",
            "from": {"emailAddress": {"address": "operator@example.com"}},
            "sender": {"emailAddress": {"address": "operator@example.com"}},
            "receivedDateTime": "2026-08-07T04:03:00Z",
            "bodyPreview": "Forwarding this campaign message for my records.",
            "hasAttachments": False,
        }

        record_inbound = self._record_batched_authority(
            msg,
            "Forwarding this campaign message for my records.",
        )
        record_inbound.assert_not_called()

    def test_batched_substantive_broker_reply_records_inbound_authority(self):
        msg = {
            "id": "graph-broker-reply",
            "internetMessageId": "<broker-reply@example.com>",
            "subject": "RE: 410 Genesis Blvd",
            "from": {"emailAddress": {"address": "broker@example.com"}},
            "sender": {"emailAddress": {"address": "broker@example.com"}},
            "receivedDateTime": "2026-08-07T04:04:00Z",
            "bodyPreview": "The property has 600A power.",
            "hasAttachments": False,
        }

        record_inbound = self._record_batched_authority(
            msg,
            "The property has 600A power.",
        )
        record_inbound.assert_called_once_with("uid-1", "thread-1")

    def test_batched_attachment_without_new_text_records_inbound_authority(self):
        quoted_only = (
            "On Thu, Aug 6, 2026 at 9:00 PM Baylor wrote:\n"
            "> Please attach the flyer."
        )
        msg = {
            "id": "graph-attachment-reply",
            "internetMessageId": "<attachment-reply@example.com>",
            "subject": "RE: 410 Genesis Blvd",
            "from": {"emailAddress": {"address": "broker@example.com"}},
            "sender": {"emailAddress": {"address": "broker@example.com"}},
            "receivedDateTime": "2026-08-07T04:05:00Z",
            "bodyPreview": quoted_only,
            "hasAttachments": True,
        }

        record_inbound = self._record_batched_authority(msg, quoted_only)
        record_inbound.assert_called_once_with("uid-1", "thread-1")


if __name__ == "__main__":
    unittest.main()
