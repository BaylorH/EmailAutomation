import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")

with patch("google.cloud.firestore.Client", return_value=MagicMock()):
    from email_automation import processing


class FakeSnapshot:
    def __init__(self, data=None, exists=True):
        self._data = data or {}
        self.exists = exists

    def to_dict(self):
        return dict(self._data)


class FakeDocumentRef:
    def __init__(self, data=None, exists=True):
        self._data = data or {}
        self._exists = exists

    def get(self):
        return FakeSnapshot(self._data, self._exists)

    def set(self, data, merge=False):
        if merge:
            self._data.update(data)
        else:
            self._data = dict(data)

    def update(self, data):
        self._data.update(data)


class FakeUserRef:
    def __init__(self, thread_ref, client_ref):
        self.thread_ref = thread_ref
        self.client_ref = client_ref

    def collection(self, name):
        if name == "threads":
            return FakeCollection(self.thread_ref)
        if name == "clients":
            return FakeCollection(self.client_ref)
        return FakeCollection(FakeDocumentRef({}, exists=False))


class FakeCollection:
    def __init__(self, doc_ref):
        self.doc_ref = doc_ref

    def document(self, *_args):
        return self.doc_ref


class FakeFirestore:
    def __init__(self, thread_ref, client_ref):
        self.thread_ref = thread_ref
        self.client_ref = client_ref

    def collection(self, name):
        if name == "users":
            return FakeCollection(FakeUserRef(self.thread_ref, self.client_ref))
        return FakeCollection(FakeDocumentRef({}, exists=False))


class CompoundNonviableProcessingTests(unittest.TestCase):
    def test_nonviable_with_replacement_and_tour_does_not_pause_old_row_for_tour(self):
        user_id = "test-user"
        client_id = "client-1"
        thread_id = "thread-19241"
        from_email = "bp21harrison+19241@gmail.com"
        body = (
            "This space wouldn't be a good fit for your client as it is more "
            "office heavy as opposed to a true warehouse with drive in space. "
            "27610 Commerce Oaks Dr could work and I can tour it Wednesday."
        )
        msg = {
            "id": "msg-1",
            "subject": "RE: 19241 David Memorial Dr, The Woodlands",
            "from": {"emailAddress": {"address": from_email, "name": "BP21 Broker"}},
            "toRecipients": [{"emailAddress": {"address": "baylor.freelance@outlook.com"}}],
            "internetMessageId": "<inbound-msg-1@mock.test>",
            "conversationId": "conv-19241",
            "receivedDateTime": "2026-06-17T08:00:00Z",
            "sentDateTime": "2026-06-17T08:00:00Z",
            "bodyPreview": body[:200],
            "internetMessageHeaders": [
                {"name": "In-Reply-To", "value": "<outbound-msg-1@mock.test>"},
            ],
        }
        header = [
            "Property Address",
            "City",
            "Leasing Contact",
            "Leasing Company",
            "Comments",
        ]
        rowvals = [
            "19241 David Memorial Dr",
            "The Woodlands",
            "BP21 Broker",
            "Example Brokerage",
            "",
        ]
        proposal = {
            "updates": [],
            "events": [
                {"type": "property_unavailable", "reason": "requirements_mismatch"},
                {
                    "type": "new_property",
                    "address": "27610 Commerce Oaks Dr",
                    "city": "The Woodlands",
                    "email": from_email,
                    "notes": "Suggested alternate with tour availability",
                },
                {
                    "type": "tour_requested",
                    "question": "I can tour it Wednesday.",
                    "suggestedEmail": "Wednesday works for us.",
                },
            ],
            "response_email": "Thanks for the update. I will review the alternate.",
        }
        thread_ref = FakeDocumentRef(
            {
                "clientId": client_id,
                "email": [from_email],
                "status": processing.THREAD_STATUS["active"],
                "rowNumber": 3,
            }
        )
        client_ref = FakeDocumentRef({"criteria": "Industrial search"})

        full_body_response = MagicMock()
        full_body_response.json.return_value = {
            "body": {"content": body, "contentType": "Text"}
        }
        me_response = MagicMock(status_code=200)
        me_response.json.return_value = {"mail": "baylor.freelance@outlook.com"}

        notifications = []
        handled_events = []
        status_updates = []

        def fake_write_notification(*args, **kwargs):
            notif_id = f"notif-{len(notifications) + 1}"
            notifications.append({"args": args, "kwargs": kwargs, "id": notif_id})
            return notif_id

        def fake_mark_event_handled(_user_id, _thread_id, event_key, _msg_id, notif_id):
            handled_events.append({"eventKey": event_key, "notifId": notif_id})

        def fake_update_thread_status(_user_id, _thread_id, status, reason):
            status_updates.append({"status": status, "reason": reason})

        patches = [
            patch.object(processing, "_fs", FakeFirestore(thread_ref, client_ref)),
            patch.object(processing, "exponential_backoff_request", return_value=full_body_response),
            patch.object(processing.requests, "get", return_value=me_response),
            patch.object(processing, "lookup_thread_by_message_id", return_value=thread_id),
            patch.object(processing, "lookup_thread_by_conversation_id", return_value=None),
            patch.object(processing, "get_thread_status", return_value=processing.THREAD_STATUS["active"]),
            patch.object(processing, "save_message", return_value=True),
            patch.object(processing, "index_message_id", return_value=True),
            patch.object(processing, "dump_thread_from_firestore"),
            patch("email_automation.followup.cancel_followup_on_response"),
            patch.object(
                processing,
                "fetch_and_log_sheet_for_thread",
                return_value=(client_id, "sheet-1", header, 3, rowvals, None, []),
            ),
            patch.object(
                processing,
                "_resolve_reply_identity",
                return_value={
                    "recipient_email": from_email,
                    "contact_name": "BP21 Broker",
                    "original_email": from_email,
                    "source": "test",
                },
            ),
            patch.object(processing, "fetch_and_process_pdfs", return_value=[]),
            patch.object(processing, "write_message_order_test"),
            patch.object(processing, "fetch_url_as_text", return_value=None),
            patch.object(processing, "propose_sheet_updates", return_value=proposal),
            patch.object(processing, "_sheets_client", return_value=MagicMock()),
            patch.object(processing, "_get_first_tab_title", return_value="Sheet1"),
            patch.object(processing, "is_event_handled", return_value=False),
            patch.object(processing, "write_notification", side_effect=fake_write_notification),
            patch.object(processing, "mark_event_handled", side_effect=fake_mark_event_handled),
            patch.object(processing, "ensure_nonviable_divider", return_value=10),
            patch.object(processing, "move_row_below_divider", return_value=11),
            patch.object(processing, "sync_thread_row_numbers_after_move"),
            patch.object(processing, "stop_threads_for_row", return_value=1),
            patch.object(processing, "find_notes_comment_column_index", return_value=None),
            patch.object(processing, "format_sheet_columns_autosize_with_exceptions"),
            patch.object(processing, "clear_row_highlight"),
            patch.object(processing, "_property_exists_in_sheet", return_value=False),
            patch.object(
                processing,
                "build_new_property_suggested_email",
                return_value={
                    "to": [from_email],
                    "subject": "27610 Commerce Oaks Dr",
                    "body": "Hi BP21 Broker, can you send details?",
                },
            ),
            patch.object(processing, "send_reply_in_thread", return_value=True),
            patch.object(processing, "update_thread_status", side_effect=fake_update_thread_status),
            patch.object(processing, "_maybe_mark_client_completed"),
        ]

        for patcher in patches:
            patcher.start()
        try:
            processing.process_inbox_message(
                user_id,
                {"Authorization": "Bearer test-token"},
                msg,
            )
        finally:
            for patcher in reversed(patches):
                patcher.stop()

        notification_kinds = [
            item["kwargs"].get("kind") for item in notifications
        ]
        action_reasons = [
            (item["kwargs"].get("meta") or {}).get("reason")
            for item in notifications
            if item["kwargs"].get("kind") == "action_needed"
        ]

        self.assertIn("property_unavailable", notification_kinds)
        self.assertIn("new_property_pending_approval", action_reasons)
        self.assertNotIn("tour_requested", action_reasons)
        self.assertFalse(
            any(
                update["status"] == processing.THREAD_STATUS["paused"]
                and update["reason"] == "tour_requested"
                for update in status_updates
            )
        )
        self.assertTrue(
            any(
                handled["eventKey"] == "tour_requested"
                or handled["eventKey"].startswith("tour_requested:")
                and handled["notifId"] is None
                for handled in handled_events
            ),
            "stale tour event should be marked handled without a dashboard notification",
        )


if __name__ == "__main__":
    unittest.main()
