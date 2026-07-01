import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import requests

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)

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


class FakeThreadRef:
    def __init__(self, data=None):
        self.updates = []
        self._data = data or {}

    def update(self, data):
        self.updates.append(data)

    def get(self):
        return FakeThreadSnapshot(self._data)


class FakeThreadSnapshot:
    def __init__(self, data):
        self.exists = True
        self._data = data

    def to_dict(self):
        return self._data


class FakeFirestore:
    def __init__(self, thread_ref):
        self.thread_ref = thread_ref

    def collection(self, _name):
        return self

    def document(self, _name):
        return self

    def update(self, data):
        self.thread_ref.update(data)

    def get(self):
        return self.thread_ref.get()


def _fixed_datetime(value):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None) -> datetime:
            return value.astimezone(tz) if tz else value

    return FixedDateTime


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


class FollowupTerminalStateTests(unittest.TestCase):
    def test_weekend_followup_window_defers_to_monday_business_start(self):
        sunday = datetime(2026, 6, 21, 17, 1, tzinfo=timezone.utc)

        deferred = followup._next_business_followup_time(sunday)

        self.assertEqual(
            deferred.isoformat(),
            "2026-06-22T13:00:00+00:00",
        )

    def test_weekday_followup_window_is_unchanged(self):
        monday = datetime(2026, 6, 22, 15, 1, tzinfo=timezone.utc)

        self.assertEqual(followup._next_business_followup_time(monday), monday)

    def test_initial_followup_schedule_defers_weekend_due_time(self):
        thread_ref = FakeThreadRef()
        followup_config = {
            "enabled": True,
            "timeZone": "America/New_York",
            "followUps": [
                {
                    "waitTime": 24,
                    "waitUnit": "hours",
                    "message": "Hi Alex,\n\nJust following up.",
                }
            ],
        }

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), \
             patch.object(
                 followup,
                 "datetime",
                 _fixed_datetime(datetime(2026, 6, 19, 22, 0, tzinfo=timezone.utc)),
             ):
            followup.schedule_followup_for_thread("uid-1", "thread-1", followup_config)

        update = thread_ref.updates[-1]
        scheduled_at = update["followUpConfig"]["nextFollowUpAt"]
        self.assertEqual(
            scheduled_at.isoformat(),
            "2026-06-22T13:00:00+00:00",
        )

    def test_initial_followup_schedule_preserves_business_day_due_time(self):
        thread_ref = FakeThreadRef()
        followup_config = {
            "enabled": True,
            "timeZone": "America/New_York",
            "followUps": [
                {
                    "waitTime": 24,
                    "waitUnit": "hours",
                    "message": "Hi Alex,\n\nJust following up.",
                }
            ],
        }

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), \
             patch.object(
                 followup,
                 "datetime",
                 _fixed_datetime(datetime(2026, 6, 22, 15, 0, tzinfo=timezone.utc)),
             ):
            followup.schedule_followup_for_thread("uid-1", "thread-1", followup_config)

        update = thread_ref.updates[-1]
        scheduled_at = update["followUpConfig"]["nextFollowUpAt"]
        self.assertEqual(
            scheduled_at.isoformat(),
            "2026-06-23T15:00:00+00:00",
        )

    @patch.object(followup, "_clear_followup_row_highlight", create=True)
    def test_max_reached_stops_thread_and_clears_highlight(self, clear_highlight):
        thread_ref = FakeThreadRef()

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)):
            followup._mark_followup_complete("uid-1", "thread-1", "max_reached")

        self.assertEqual(thread_ref.updates[-1]["followUpStatus"], "max_reached")
        self.assertEqual(thread_ref.updates[-1]["status"], "stopped")
        self.assertEqual(thread_ref.updates[-1]["statusReason"], "max_followups_reached")
        clear_highlight.assert_called_once_with("uid-1", "thread-1")

    def test_reply_anchor_skips_synthetic_followup_history(self):
        synthetic_latest = FakeMessageDoc({
            "direction": "outbound",
            "source": "followup_scheduler",
            "headers": {"internetMessageId": "followup-thread-123"},
            "sentDateTime": "2026-05-06T09:00:00Z",
        })
        graph_backed_original = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<real-message@example.com>"},
            "sentDateTime": "2026-05-06T08:00:00Z",
        })

        selected = followup._select_reply_anchor_message([synthetic_latest, graph_backed_original])

        self.assertEqual(selected["headers"]["internetMessageId"], "<real-message@example.com>")

    def test_reply_anchor_returns_none_when_only_synthetic_history_exists(self):
        selected = followup._select_reply_anchor_message([
            FakeMessageDoc({
                "direction": "outbound",
                "source": "dashboard_outbox_reply",
                "headers": {"internetMessageId": "dashboard-reply-123"},
            })
        ])

        self.assertIsNone(selected)

    def test_auto_response_reschedules_paused_active_thread(self):
        thread_ref = FakeThreadRef({
            "status": "active",
            "followUpStatus": "paused",
            "hasInboundReply": True,
            "followUpConfig": {
                "enabled": True,
                "currentFollowUpIndex": 1,
                "followUps": [
                    {"waitTime": 1, "waitUnit": "hours", "message": "First"},
                    {"waitTime": 2, "waitUnit": "hours", "message": "Second"},
                ],
            },
        })

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)):
            result = followup.schedule_followup_after_auto_response("uid-1", "thread-1")

        self.assertTrue(result)
        update = thread_ref.updates[-1]
        self.assertEqual(update["followUpStatus"], "waiting")
        self.assertFalse(update["hasInboundReply"])
        self.assertIsNone(update["followUpConfig.pausedAt"])
        self.assertIn("followUpConfig.nextFollowUpAt", update)

    def test_followup_blocks_malformed_recipient_before_graph_send(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
        })
        fake_fs = FakeFollowupFirestore([outbound])
        followup_config = {
            "followUps": [{"message": "Hi Riley,\n\nJust following up."}],
        }
        thread_data = {
            "email": ["not an email"],
            "contactName": "Riley Broker",
        }

        with patch.object(followup, "_fs", fake_fs), \
             patch.object(followup, "exponential_backoff_request", return_value=FakeResponse(200, {
                 "value": [{"id": "graph-root", "subject": "0 Gemini Ave", "conversationId": "conv-1"}]
             })), \
             patch.object(requests, "post") as post:
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
            )

        self.assertFalse(result)
        post.assert_not_called()
        self.assertIn("Invalid follow-up recipient", followup._send_followup_email.last_error)
        self.assertTrue(followup._send_followup_email.guard_failed_closed)

    def test_followup_blocks_opted_out_recipient_before_graph_send(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
        })
        fake_fs = FakeFollowupFirestore([outbound])
        followup_config = {
            "followUps": [{"message": "Hi Riley,\n\nJust following up."}],
        }
        thread_data = {
            "email": ["optout@example.com"],
            "contactName": "Riley Broker",
        }

        with patch.object(followup, "_fs", fake_fs), \
             patch.object(followup, "exponential_backoff_request", return_value=FakeResponse(200, {
                 "value": [{"id": "graph-root", "subject": "0 Gemini Ave", "conversationId": "conv-1"}]
             })), \
             patch("email_automation.processing.is_contact_opted_out", return_value={"reason": "unsubscribe"}), \
             patch.object(requests, "post") as post:
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
            )

        self.assertFalse(result)
        post.assert_not_called()
        self.assertIn("opted out", followup._send_followup_email.last_error)
        self.assertTrue(followup._send_followup_email.guard_failed_closed)

    def test_followup_preserves_safe_ccs_with_reply_all_draft(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
            "to": ["bp21harrison@gmail.com"],
            "cc": ["assistant@example.com", "baylor.freelance@outlook.com"],
        })
        fake_fs = FakeFollowupFirestore([outbound])
        followup_config = {
            "followUps": [{"message": "Hi Riley,\n\nJust following up."}],
        }
        thread_data = {
            "email": ["bp21harrison@gmail.com"],
            "contactName": "Riley Broker",
        }
        post_urls = []
        patched_payloads = []

        def run_request(callback, *args, **kwargs):
            return callback()

        def fake_get(url, **kwargs):
            self.assertIn("/me/messages", url)
            return FakeResponse(200, {
                "value": [{
                    "id": "graph-root",
                    "subject": "0 Gemini Ave",
                    "conversationId": "conv-1",
                }]
            })

        def fake_post(url, **kwargs):
            post_urls.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {"id": "reply-draft-1", "toRecipients": [], "ccRecipients": []})
            if url.endswith("/send"):
                return FakeResponse(202, {})
            raise AssertionError(f"Follow-up used non reply-all endpoint: {url}")

        def fake_patch(url, **kwargs):
            patched_payloads.append(kwargs.get("json") or {})
            return FakeResponse(200, {})

        with patch.object(followup, "_fs", fake_fs), \
             patch.object(followup, "exponential_backoff_request", side_effect=run_request), \
             patch.object(requests, "get", side_effect=fake_get), \
             patch.object(requests, "post", side_effect=fake_post), \
             patch.object(requests, "patch", side_effect=fake_patch), \
             patch.object(followup, "_save_followup_message") as save_followup, \
             patch("email_automation.processing.is_contact_opted_out", return_value=None):
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
            )

        self.assertTrue(result)
        self.assertTrue(any(url.endswith("/createReplyAll") for url in post_urls))
        self.assertTrue(any(url.endswith("/send") for url in post_urls))
        patch_payload = patched_payloads[-1]
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["toRecipients"]],
            ["bp21harrison@gmail.com"],
        )
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["ccRecipients"]],
            ["assistant@example.com"],
        )
        save_followup.assert_called_once()

    def test_failed_followup_retry_uses_sent_items_match_without_resending(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
        })
        fake_fs = FakeFollowupFirestore([outbound])
        followup_config = {
            "lastSendError": "Read timed out after Graph accepted send",
            "lastSendAttemptAt": "2026-06-26T12:05:00Z",
            "followUps": [{"message": "Hi [NAME],\n\nJust following up."}],
        }
        thread_data = {
            "email": ["bp21harrison@gmail.com"],
            "contactName": "Ryan Broker",
        }

        with patch.object(followup, "_fs", fake_fs), \
             patch.object(followup, "exponential_backoff_request", return_value=FakeResponse(200, {
                 "value": [{"id": "graph-root", "subject": "0 Gemini Ave", "conversationId": "conv-1"}]
             })), \
             patch.object(followup, "find_matching_sent_message_for_retry", return_value={
                 "id": "sent-followup-1",
                 "internetMessageId": "<sent-followup-1@example.com>",
                 "conversationId": "conv-1",
             }) as sent_guard, \
             patch.object(followup, "_save_followup_message") as save_followup, \
             patch.object(requests, "post") as post:
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
            )

        self.assertTrue(result)
        sent_guard.assert_called_once()
        post.assert_not_called()
        save_followup.assert_called_once()
        self.assertIsNone(fake_fs.updates[-1]["followUpConfig.lastSendError"])

    def test_failed_followup_retry_blocks_when_sent_items_lookup_fails(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
        })
        fake_fs = FakeFollowupFirestore([outbound])
        followup_config = {
            "lastSendError": "Read timed out after Graph accepted send",
            "lastSendAttemptAt": "2026-06-26T12:05:00Z",
            "followUps": [{"message": "Hi [NAME],\n\nJust following up."}],
        }
        thread_data = {
            "email": ["bp21harrison@gmail.com"],
            "contactName": "Ryan Broker",
        }

        with patch.object(followup, "_fs", fake_fs), \
             patch.object(followup, "exponential_backoff_request", return_value=FakeResponse(200, {
                 "value": [{"id": "graph-root", "subject": "0 Gemini Ave", "conversationId": "conv-1"}]
             })), \
             patch.object(
                 followup,
                 "find_matching_sent_message_for_retry",
                 side_effect=followup.SentMailGuardLookupError("Graph 401"),
             ), \
             patch.object(requests, "post") as post:
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
            )

        self.assertFalse(result)
        post.assert_not_called()
        self.assertIn("Sent Items retry guard failed", followup._send_followup_email.last_error)
        self.assertTrue(followup._send_followup_email.guard_failed_closed)

    def test_failed_followup_retry_blocks_when_conversation_was_manually_continued(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
        })
        fake_fs = FakeFollowupFirestore([outbound])
        followup_config = {
            "lastSendError": "Read timed out after Graph accepted send",
            "lastSendAttemptAt": "2026-06-26T12:05:00Z",
            "followUps": [{"message": "Hi [NAME],\n\nJust following up."}],
        }
        thread_data = {
            "email": ["bp21harrison@gmail.com"],
            "contactName": "Ryan Broker",
        }
        manual_continuation = {
            "id": "manual-sent-1",
            "internetMessageId": "<manual-sent-1@example.com>",
            "conversationId": "conv-1",
            "sentDateTime": "2026-06-26T12:08:00Z",
        }

        with patch.object(followup, "_fs", fake_fs), \
             patch.object(followup, "exponential_backoff_request", return_value=FakeResponse(200, {
                 "value": [{"id": "graph-root", "subject": "0 Gemini Ave", "conversationId": "conv-1"}]
             })), \
             patch.object(followup, "find_matching_sent_message_for_retry", return_value=None), \
             patch.object(followup, "find_sent_conversation_continuation_for_retry", return_value=manual_continuation, create=True) as continuation_guard, \
             patch.object(requests, "post") as post:
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
            )

        self.assertFalse(result)
        continuation_guard.assert_called_once()
        self.assertEqual(continuation_guard.call_args.kwargs["conversation_id"], "conv-1")
        post.assert_not_called()
        self.assertIn("manually continued", followup._send_followup_email.last_error)
        self.assertTrue(followup._send_followup_email.guard_failed_closed)

    def test_guard_lookup_failure_release_marks_manual_review(self):
        thread_ref = FakeThreadRef()
        attempted_at = datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc)

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)):
            followup._release_followup_claim(
                "uid-1",
                "thread-1",
                reason="Sent Items retry guard failed: Graph 401",
                attempted_at=attempted_at,
                current_index=1,
                fail_closed=True,
            )

        update = thread_ref.updates[-1]
        self.assertEqual(update["followUpStatus"], "needs_review")
        self.assertEqual(update["status"], "action_needed")
        self.assertEqual(update["statusReason"], "followup_send_guard_failed")
        self.assertFalse(update["followUpConfig.enabled"])
        self.assertEqual(update["followUpConfig.lastSendAttemptIndex"], 1)

    def test_schedule_next_followup_clears_previous_retry_guard_state(self):
        thread_ref = FakeThreadRef()
        followup_config = {
            "lastSendError": "Read timed out",
            "lastSendAttemptAt": "2026-06-26T12:05:00Z",
            "lastSendAttemptIndex": 0,
            "followUps": [
                {"waitTime": 1, "waitUnit": "hours", "message": "First"},
                {"waitTime": 2, "waitUnit": "hours", "message": "Second"},
            ],
        }

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)):
            followup._schedule_next_followup(
                "uid-1",
                "thread-1",
                followup_config,
                just_sent_index=0,
            )

        update = thread_ref.updates[-1]
        self.assertEqual(update["followUpConfig.currentFollowUpIndex"], 1)
        self.assertIsNone(update["followUpConfig.lastSendError"])
        self.assertIsNone(update["followUpConfig.lastSendAttemptAt"])
        self.assertIsNone(update["followUpConfig.lastSendAttemptIndex"])


if __name__ == "__main__":
    unittest.main()
