import os
import unittest
from copy import deepcopy
from unittest.mock import patch


os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "service-account.json",
    ),
)

from email_automation import followup, processing


class _ThreadSnapshot:
    def __init__(self, data=None, *, exists=True):
        self._data = deepcopy(data or {})
        self.exists = exists

    def to_dict(self):
        return deepcopy(self._data)


class _ThreadFirestore:
    """Small stateful double for one thread document."""

    def __init__(self, data=None, *, exists=True, update_error=None):
        self.data = deepcopy(data or {})
        self.exists = exists
        self.update_error = update_error
        self.updates = []

    def collection(self, _name):
        return self

    def document(self, _doc_id):
        return self

    def get(self):
        return _ThreadSnapshot(self.data, exists=self.exists)

    def update(self, payload):
        if self.update_error:
            raise self.update_error
        self.updates.append(dict(payload))
        self.data.update(payload)


class CanonicalInboundMarkerTests(unittest.TestCase):
    def _cancel(self, thread_data=None, *, exists=True, update_error=None):
        fake_fs = _ThreadFirestore(
            thread_data,
            exists=exists,
            update_error=update_error,
        )
        with patch.object(followup, "_fs", fake_fs):
            result = followup.cancel_followup_on_response("uid-1", "thread-1")
        return fake_fs, result

    def test_disabled_followups_still_record_canonical_inbound_markers(self):
        fake_fs, _ = self._cancel({
            "status": "active",
            "followUpStatus": "waiting",
            "followUpConfig": {"enabled": False},
        })

        self.assertEqual(1, len(fake_fs.updates))
        update = fake_fs.updates[0]
        self.assertTrue(update["hasInboundReply"])
        self.assertIs(update["lastInboundAt"], followup.SERVER_TIMESTAMP)
        self.assertIs(update["updatedAt"], followup.SERVER_TIMESTAMP)
        self.assertNotIn("followUpStatus", update)
        self.assertNotIn("followUpConfig.pausedAt", update)

    def test_terminal_followup_state_keeps_terminal_fields_and_updates_markers(self):
        fake_fs, _ = self._cancel({
            "status": "completed",
            "statusReason": "all_fields_gathered",
            "followUpStatus": "completed",
            "followUpConfig": {"enabled": True},
        })

        self.assertEqual(1, len(fake_fs.updates))
        update = fake_fs.updates[0]
        self.assertEqual(
            {"hasInboundReply", "lastInboundAt", "updatedAt"},
            set(update),
        )
        self.assertEqual("completed", fake_fs.data["status"])
        self.assertEqual("all_fields_gathered", fake_fs.data["statusReason"])
        self.assertEqual("completed", fake_fs.data["followUpStatus"])

    def test_archived_thread_does_not_rewrite_stale_waiting_followup_state(self):
        fake_fs, _ = self._cancel({
            "status": "archived",
            "statusReason": "archived_by_user",
            "followUpStatus": "waiting",
            "followUpConfig": {"enabled": True},
        })

        self.assertEqual(1, len(fake_fs.updates))
        update = fake_fs.updates[0]
        self.assertEqual(
            {"hasInboundReply", "lastInboundAt", "updatedAt"},
            set(update),
        )
        self.assertEqual("archived", fake_fs.data["status"])
        self.assertEqual("waiting", fake_fs.data["followUpStatus"])

    def test_enabled_waiting_followup_records_markers_and_pauses_sequence(self):
        fake_fs, _ = self._cancel({
            "status": "active",
            "followUpStatus": "waiting",
            "followUpConfig": {"enabled": True},
        })

        self.assertEqual(1, len(fake_fs.updates))
        update = fake_fs.updates[0]
        self.assertTrue(update["hasInboundReply"])
        self.assertEqual("paused", update["followUpStatus"])
        self.assertIs(update["followUpConfig.pausedAt"], followup.SERVER_TIMESTAMP)
        self.assertEqual(
            "mid_conversation",
            update["followUpConfig.conversationStage"],
        )

    def test_missing_thread_is_a_noop(self):
        fake_fs, result = self._cancel(exists=False)

        self.assertEqual([], fake_fs.updates)
        self.assertIsNone(result)

    def test_marker_write_failure_surfaces_to_the_inbox_retry_boundary(self):
        with self.assertRaisesRegex(RuntimeError, "marker write unavailable"):
            self._cancel(
                {
                    "status": "active",
                    "followUpStatus": "waiting",
                    "followUpConfig": {"enabled": False},
                },
                update_error=RuntimeError("marker write unavailable"),
            )


class TourPhraseClassifierTests(unittest.TestCase):
    def test_set_a_tour_and_set_up_a_tour_are_explicit_tour_requests(self):
        for phrase in (
            "We can set a tour for Tuesday at 10:30.",
            "We can set up a tour for Tuesday at 10:30.",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(
                    processing._looks_like_explicit_tour_offer_or_request(phrase)
                )


if __name__ == "__main__":
    unittest.main()
