import unittest
import os
from contextlib import ExitStack
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import ai_processing, messaging, processing


_MAILBOX_UNSET = object()


class _ProposalReached(Exception):
    pass


class _FakeSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = True

    def to_dict(self):
        return self._data


class _FakeMessageSnapshot:
    def __init__(self, message_id, data):
        self.id = message_id
        self._data = data

    def to_dict(self):
        return self._data


class _FakeMessagesCollection:
    def __init__(self, messages):
        self._messages = messages

    def stream(self):
        return [
            _FakeMessageSnapshot(message_id, data)
            for message_id, data in self._messages
        ]


class _FakeFirestore:
    def __init__(self, thread_data=None, messages=None):
        self._thread_data = thread_data or {}
        self._messages = _FakeMessagesCollection(messages or [])

    def collection(self, name):
        if name == "messages":
            return self._messages
        return self

    def document(self, _document_id):
        return self

    def get(self):
        return _FakeSnapshot(self._thread_data)

    def set(self, _payload, merge=False):
        return None

    def update(self, _payload):
        return None


class _FakeGraphResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class MessagingConversationPayloadTests(unittest.TestCase):
    def _build_with_graph(
        self,
        graph_messages,
        *,
        firestore_messages=None,
        authenticated_mailbox_email=_MAILBOX_UNSET,
    ):
        graph_response = _FakeGraphResponse({"value": graph_messages})
        fake_fs = _FakeFirestore({"conversationId": "conversation-1"})
        with patch.object(
            messaging,
            "_get_thread_messages_chronological",
            return_value=firestore_messages or [],
        ), patch.object(messaging, "_fs", fake_fs), patch(
            "email_automation.utils.exponential_backoff_request",
            return_value=graph_response,
        ):
            kwargs = {
                "headers": {"Authorization": "Bearer test-token"},
            }
            if authenticated_mailbox_email is not _MAILBOX_UNSET:
                kwargs["authenticated_mailbox_email"] = authenticated_mailbox_email
            return messaging.build_conversation_payload(
                "uid-1",
                "thread-1",
                **kwargs,
            )

    def test_new_property_event_key_normalizes_null_optional_fields(self):
        key = messaging.build_event_key(
            "new_property",
            {
                "address": "27610 Commerce Oaks Dr",
                "city": None,
                "email": None,
            },
            thread_id="thread-1",
        )

        self.assertEqual("new_property:27610 Commerce Oaks Dr::", key)

    def test_property_issue_event_key_tolerates_null_and_non_string_issue(self):
        self.assertEqual(
            "property_issue:",
            messaging.build_event_key("property_issue", {"issue": None}, thread_id="thread-1"),
        )
        self.assertEqual(
            "property_issue:12345",
            messaging.build_event_key("property_issue", {"issue": 12345}, thread_id="thread-1"),
        )

    def test_build_conversation_payload_tolerates_string_body_messages(self):
        mixed_history = [
            {
                "id": "initial-outbound",
                "data": {
                    "direction": "outbound",
                    "from": "me",
                    "to": ["bp21harrison@gmail.com"],
                    "subject": "3660 N 5th St",
                    "sentDateTime": "2026-05-06T16:49:01Z",
                    "body": {"content": "Could you send specs?", "preview": "Could you send specs?"},
                },
            },
            {
                "id": "dashboard-reply-1",
                "data": {
                    "direction": "outbound",
                    "from": "me",
                    "to": ["bp21harrison@gmail.com"],
                    "subject": "RE: 3660 N 5th St",
                    "sentDateTime": "2026-05-06T18:00:48Z",
                    "body": "The tenant is confidential for now.",
                    "bodyPreview": "The tenant is confidential for now.",
                },
            },
            {
                "id": "broker-specs",
                "data": {
                    "direction": "inbound",
                    "from": "bp21harrison@gmail.com",
                    "to": ["me"],
                    "subject": "Re: 3660 N 5th St",
                    "receivedDateTime": "2026-05-06T18:05:15Z",
                    "body": {
                        "content": "Understood. 14,267 SF is available with 5 docks, 8 drive-ins, and 20' clear.",
                        "preview": "Understood. 14,267 SF is available...",
                    },
                },
            },
        ]

        with patch.object(messaging, "_get_thread_messages_chronological", return_value=mixed_history):
            payload = messaging.build_conversation_payload("uid-1", "thread-1")

        self.assertEqual(len(payload), 3)
        self.assertEqual(payload[1]["content"], "The tenant is confidential for now.")
        self.assertIn("14,267 SF", payload[2]["content"])

    def test_firestore_history_uses_direction_aware_timestamp_for_sorting(self):
        fake_fs = _FakeFirestore(
            messages=[
                (
                    "indexed-inbound",
                    {
                        "direction": "inbound",
                        "sentDateTime": "2026-08-01T10:00:00Z",
                        "receivedDateTime": "2026-08-01T10:05:00Z",
                    },
                ),
                (
                    "indexed-outbound",
                    {
                        "direction": "outbound",
                        "sentDateTime": "2026-08-01T10:03:00Z",
                        "receivedDateTime": "2026-08-01T10:07:00Z",
                    },
                ),
            ]
        )

        with patch.object(messaging, "_fs", fake_fs):
            history = messaging._get_thread_messages_chronological("uid-1", "thread-1")

        self.assertEqual(
            ["indexed-outbound", "indexed-inbound"],
            [message["id"] for message in history],
        )

    def test_mixed_history_orders_manual_outbound_before_indexed_inbound(self):
        firestore_messages = [
            {
                "id": "indexed-inbound",
                "data": {
                    "direction": "inbound",
                    "from": "broker@example.test",
                    "to": ["operator@example.test"],
                    "subject": "Re: Property details",
                    "sentDateTime": "2026-08-01T10:00:00Z",
                    "receivedDateTime": "2026-08-01T10:05:00Z",
                    "body": {"content": "Inbound at 10:05", "preview": "Inbound"},
                },
            }
        ]
        graph_messages = [
            {
                "id": "manual-outbound",
                "internetMessageId": "<manual-outbound@example.test>",
                "conversationId": "conversation-1",
                "subject": "Re: Property details",
                "from": {"emailAddress": {"address": "operator@example.test"}},
                "toRecipients": [
                    {"emailAddress": {"address": "broker@example.test"}}
                ],
                "sentDateTime": "2026-08-01T10:03:00Z",
                "body": {"contentType": "Text", "content": "Manual outbound"},
                "bodyPreview": "Manual outbound",
            }
        ]

        payload = self._build_with_graph(
            graph_messages,
            firestore_messages=firestore_messages,
        )

        self.assertEqual(["outbound", "inbound"], [item["direction"] for item in payload])
        self.assertEqual(
            ["2026-08-01T10:03:00Z", "2026-08-01T10:05:00Z"],
            [item["timestamp"] for item in payload],
        )

    def test_limit_returns_exact_chronological_last_ten_from_unsorted_history(self):
        input_order = [5, 0, 11, 3, 8, 1, 10, 2, 7, 4, 9, 6]
        history = [
            {
                "id": f"message-{index:02d}",
                "data": {
                    "direction": "inbound",
                    "sentDateTime": f"2026-08-01T11:{11 - index:02d}:00Z",
                    "receivedDateTime": f"2026-08-01T10:{index:02d}:00Z",
                    "body": {
                        "content": f"message-{index:02d}",
                        "preview": f"message-{index:02d}",
                    },
                },
            }
            for index in input_order
        ]

        with patch.object(
            messaging,
            "_get_thread_messages_chronological",
            return_value=history,
        ):
            payload = messaging.build_conversation_payload("uid-1", "thread-1", limit=10)

        self.assertEqual(10, len(payload))
        self.assertEqual(
            [f"message-{index:02d}" for index in range(2, 12)],
            [item["content"] for item in payload],
        )
        self.assertEqual(
            [f"2026-08-01T10:{index:02d}:00Z" for index in range(2, 12)],
            [item["timestamp"] for item in payload],
        )

    def test_graph_message_with_both_dates_is_outbound_when_from_matches_mailbox(self):
        payload = self._build_with_graph(
            [
                {
                    "id": "graph-outbound",
                    "conversationId": "conversation-1",
                    "from": {
                        "emailAddress": {"address": " Operator@Example.Test "}
                    },
                    "sentDateTime": "2026-08-01T10:03:00Z",
                    "receivedDateTime": "2026-08-01T10:04:00Z",
                    "body": {"contentType": "Text", "content": "Manual reply"},
                }
            ],
            authenticated_mailbox_email="operator@example.test",
        )

        self.assertEqual("outbound", payload[0]["direction"])
        self.assertEqual("2026-08-01T10:03:00Z", payload[0]["timestamp"])

    def test_graph_message_with_both_dates_is_inbound_when_from_differs(self):
        payload = self._build_with_graph(
            [
                {
                    "id": "graph-inbound",
                    "conversationId": "conversation-1",
                    "from": {"emailAddress": {"address": "broker@example.test"}},
                    "sentDateTime": "2026-08-01T10:00:00Z",
                    "receivedDateTime": "2026-08-01T10:05:00Z",
                    "body": {"contentType": "Text", "content": "Broker reply"},
                }
            ],
            authenticated_mailbox_email="operator@example.test",
        )

        self.assertEqual("inbound", payload[0]["direction"])
        self.assertEqual("2026-08-01T10:05:00Z", payload[0]["timestamp"])

    def test_graph_message_with_both_dates_defaults_inbound_for_unknown_identity(self):
        cases = [
            (None, "broker@example.test"),
            ("", "broker@example.test"),
            ("operator@example.test", ""),
        ]

        for mailbox_email, from_email in cases:
            with self.subTest(mailbox_email=mailbox_email, from_email=from_email):
                payload = self._build_with_graph(
                    [
                        {
                            "id": "graph-ambiguous",
                            "conversationId": "conversation-1",
                            "from": {"emailAddress": {"address": from_email}},
                            "sentDateTime": "2026-08-01T10:00:00Z",
                            "receivedDateTime": "2026-08-01T10:05:00Z",
                            "body": {
                                "contentType": "Text",
                                "content": "Ambiguous direction",
                            },
                        }
                    ],
                    authenticated_mailbox_email=mailbox_email,
                )

                self.assertEqual("inbound", payload[0]["direction"])
                self.assertEqual("2026-08-01T10:05:00Z", payload[0]["timestamp"])

    def test_graph_message_with_malformed_from_defaults_inbound(self):
        payload = self._build_with_graph(
            [
                {
                    "id": "graph-missing-from",
                    "conversationId": "conversation-1",
                    "from": None,
                    "sentDateTime": "2026-08-01T10:00:00Z",
                    "receivedDateTime": "2026-08-01T10:05:00Z",
                    "body": {
                        "contentType": "Text",
                        "content": "Missing sender identity",
                    },
                }
            ],
            authenticated_mailbox_email="operator@example.test",
        )

        self.assertEqual(1, len(payload))
        self.assertEqual("inbound", payload[0]["direction"])
        self.assertEqual("", payload[0]["from"])
        self.assertEqual("2026-08-01T10:05:00Z", payload[0]["timestamp"])

    def test_graph_one_date_direction_rules_remain_safe(self):
        payload = self._build_with_graph(
            [
                {
                    "id": "sent-only",
                    "conversationId": "conversation-1",
                    "from": {"emailAddress": {"address": "broker@example.test"}},
                    "sentDateTime": "2026-08-01T10:00:00Z",
                    "body": {"contentType": "Text", "content": "Sent only"},
                },
                {
                    "id": "received-only",
                    "conversationId": "conversation-1",
                    "from": {"emailAddress": {"address": "operator@example.test"}},
                    "receivedDateTime": "2026-08-01T10:01:00Z",
                    "body": {"contentType": "Text", "content": "Received only"},
                },
            ],
            authenticated_mailbox_email="operator@example.test",
        )

        self.assertEqual(["outbound", "inbound"], [item["direction"] for item in payload])

    def test_propose_sheet_updates_forwards_authenticated_mailbox_to_payload_builder(self):
        with patch.object(
            ai_processing,
            "build_conversation_payload",
            return_value=[],
        ) as build_payload:
            ai_processing.propose_sheet_updates(
                "uid-1",
                "client-1",
                "broker@example.test",
                "sheet-1",
                ["Property Address"],
                3,
                ["123 Test St"],
                "thread-1",
                headers={"Authorization": "Bearer test-token"},
                authenticated_mailbox_email="operator@example.test",
            )

        build_payload.assert_called_once_with(
            "uid-1",
            "thread-1",
            limit=10,
            headers={"Authorization": "Bearer test-token"},
            authenticated_mailbox_email="operator@example.test",
        )

    def test_process_inbox_forwards_locally_resolved_mailbox_to_proposal(self):
        message = {
            "id": "graph-inbound",
            "internetMessageId": "<graph-inbound@example.test>",
            "conversationId": "conversation-1",
            "subject": "Re: Property details",
            "from": {
                "emailAddress": {
                    "address": "broker@example.test",
                    "name": "Broker",
                }
            },
            "sender": {"emailAddress": {"address": "broker@example.test"}},
            "toRecipients": [
                {"emailAddress": {"address": "operator@example.test"}}
            ],
            "receivedDateTime": "2026-08-01T10:05:00Z",
            "bodyPreview": "The property is available.",
            "hasAttachments": False,
            "internetMessageHeaders": [
                {"name": "In-Reply-To", "value": "<tracked-outbound@example.test>"}
            ],
        }
        full_message = _FakeGraphResponse(
            {
                "body": {
                    "contentType": "Text",
                    "content": "The property is available.",
                },
                "hasAttachments": False,
            }
        )
        fake_fs = _FakeFirestore(
            {
                "status": processing.THREAD_STATUS["active"],
                "clientId": "client-1",
                "email": ["broker@example.test"],
            }
        )

        with ExitStack() as stack:
            stack.enter_context(patch.object(processing, "_fs", fake_fs))
            stack.enter_context(
                patch.object(
                    processing,
                    "exponential_backoff_request",
                    return_value=full_message,
                )
            )
            resolve_mailbox = stack.enter_context(
                patch.object(
                    processing,
                    "_resolve_current_mailbox_email",
                    return_value="operator@example.test",
                )
            )
            stack.enter_context(
                patch.object(
                    processing,
                    "lookup_thread_by_message_id",
                    return_value="thread-1",
                )
            )
            stack.enter_context(
                patch.object(processing, "get_client_automation_decision")
            )
            stack.enter_context(
                patch.object(processing, "classify_campaign_suppression", return_value=None)
            )
            stack.enter_context(
                patch.object(processing, "_active_replacement_context", return_value=None)
            )
            stack.enter_context(
                patch.object(
                    processing,
                    "_should_skip_processing_for_terminal_thread",
                    return_value=False,
                )
            )
            stack.enter_context(patch.object(processing, "save_message", return_value=True))
            stack.enter_context(patch.object(processing, "index_message_id", return_value=True))
            stack.enter_context(patch.object(processing.time, "sleep"))
            stack.enter_context(
                patch("email_automation.followup.cancel_followup_on_response")
            )
            stack.enter_context(patch.object(processing, "dump_thread_from_firestore"))
            stack.enter_context(
                patch.object(
                    processing,
                    "fetch_and_log_sheet_for_thread",
                    return_value=(
                        "client-1",
                        "sheet-1",
                        ["Property Address", "Leasing Contact", "Leasing Contact Email"],
                        3,
                        ["123 Test St", "Broker", "broker@example.test"],
                        None,
                        [],
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    processing,
                    "_resolve_reply_identity",
                    return_value={
                        "recipient_email": "broker@example.test",
                        "contact_name": "Broker",
                        "original_email": "broker@example.test",
                        "source": "test",
                    },
                )
            )
            stack.enter_context(
                patch.object(processing, "fetch_and_process_pdfs", return_value=[])
            )
            stack.enter_context(
                patch.object(processing, "fetch_and_process_linked_assets", return_value=[])
            )
            stack.enter_context(patch.object(processing, "write_message_order_test"))
            propose_updates = stack.enter_context(
                patch.object(
                    processing,
                    "propose_sheet_updates",
                    side_effect=_ProposalReached,
                )
            )

            with self.assertRaises(_ProposalReached):
                processing.process_inbox_message(
                    "uid-1",
                    {"Authorization": "Bearer test-token"},
                    message,
                )

        resolve_mailbox.assert_called_once_with({"Authorization": "Bearer test-token"})
        self.assertEqual(
            "operator@example.test",
            propose_updates.call_args.kwargs["authenticated_mailbox_email"],
        )


if __name__ == "__main__":
    unittest.main()
