import unittest
from unittest.mock import patch

from email_automation import followup


class FakeThreadRef:
    def __init__(self):
        self.updates = []

    def update(self, data):
        self.updates.append(data)


class FakeFirestore:
    def __init__(self, thread_ref):
        self.thread_ref = thread_ref

    def collection(self, _name):
        return self

    def document(self, _name):
        return self

    def update(self, data):
        self.thread_ref.update(data)


class FakeMessageDoc:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


class FollowupTerminalStateTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
