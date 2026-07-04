"""Surface C hardening: client-written followUpConfig bounds validation.

The dashboard (ClientsTable StartProjectModal onConfirm, AddClientModal) writes
followUpConfig straight onto client/outbox docs. schedule_followup_for_thread
(followup.py) consumed waitTime/waitUnit and the followUps array with no bounds
validation, so a manipulated payload could:
  - set waitTime <= 0 (nextFollowUpAt at/before now -> immediate duplicate send
    pressure on the very next scheduler run), or
  - ship a 100-step followUps list (unbounded auto-send sequence), or
  - ship a non-numeric waitTime (crashed scheduling with a TypeError).

These tests assert out-of-range config is rejected fail-closed (disabled +
needs_review, never schedulable) at ingest, and that the mid-flight paths
(_schedule_next_followup, schedule_followup_after_auto_response) clamp raw
stored values as defense in depth.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)

from email_automation import followup


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


# Monday, well inside the business week (no weekend deferral in play).
MONDAY_NOW = datetime(2026, 6, 22, 15, 0, tzinfo=timezone.utc)


def _config(followups, **extra):
    config = {"enabled": True, "timeZone": "America/New_York", "followUps": followups}
    config.update(extra)
    return config


class ScheduleFollowupIngestValidationTests(unittest.TestCase):
    """schedule_followup_for_thread must reject out-of-range client config fail-closed."""

    def _schedule(self, followup_config):
        thread_ref = FakeThreadRef()
        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), \
             patch.object(followup, "datetime", _fixed_datetime(MONDAY_NOW)):
            followup.schedule_followup_for_thread("uid-1", "thread-1", followup_config)
        return thread_ref

    def _assert_rejected_fail_closed(self, thread_ref):
        self.assertEqual(len(thread_ref.updates), 1)
        update = thread_ref.updates[-1]
        self.assertEqual(update["followUpStatus"], "needs_review")
        self.assertEqual(update["status"], "action_needed")
        self.assertEqual(update["statusReason"], "followup_config_invalid")
        self.assertFalse(update["followUpConfig"]["enabled"])
        self.assertNotIn("nextFollowUpAt", update["followUpConfig"])
        self.assertIn("invalidReason", update["followUpConfig"])

    def test_negative_wait_time_is_rejected_not_scheduled(self):
        # Attack: waitTime -5 makes nextFollowUpAt land in the past, so the
        # scheduler fires an unintended second email on its very next run.
        thread_ref = self._schedule(
            _config([{"waitTime": -5, "waitUnit": "days", "message": "ping"}])
        )
        self._assert_rejected_fail_closed(thread_ref)

    def test_zero_wait_time_is_rejected_not_scheduled(self):
        thread_ref = self._schedule(
            _config([{"waitTime": 0, "waitUnit": "minutes", "message": "ping"}])
        )
        self._assert_rejected_fail_closed(thread_ref)

    def test_oversized_followups_list_is_rejected(self):
        # Attack: 100-step list queues an arbitrarily long auto-send sequence.
        steps = [
            {"waitTime": 1, "waitUnit": "days", "message": f"ping {i}"}
            for i in range(100)
        ]
        thread_ref = self._schedule(_config(steps))
        self._assert_rejected_fail_closed(thread_ref)

    def test_non_numeric_wait_time_is_rejected_without_crashing(self):
        # Previously raised TypeError inside timedelta(days="3").
        thread_ref = self._schedule(
            _config([{"waitTime": "3", "waitUnit": "days", "message": "ping"}])
        )
        self._assert_rejected_fail_closed(thread_ref)

    def test_boolean_wait_time_is_rejected(self):
        thread_ref = self._schedule(
            _config([{"waitTime": True, "waitUnit": "days", "message": "ping"}])
        )
        self._assert_rejected_fail_closed(thread_ref)

    def test_wait_time_beyond_unit_max_is_rejected(self):
        thread_ref = self._schedule(
            _config([{"waitTime": 100000, "waitUnit": "days", "message": "ping"}])
        )
        self._assert_rejected_fail_closed(thread_ref)

    def test_unknown_wait_unit_is_rejected(self):
        thread_ref = self._schedule(
            _config([{"waitTime": 2, "waitUnit": "weeks", "message": "ping"}])
        )
        self._assert_rejected_fail_closed(thread_ref)

    def test_non_list_followups_is_rejected(self):
        thread_ref = self._schedule(_config({"waitTime": 1}))
        self._assert_rejected_fail_closed(thread_ref)

    def test_non_dict_followup_step_is_rejected(self):
        thread_ref = self._schedule(_config(["not-a-step"]))
        self._assert_rejected_fail_closed(thread_ref)

    def test_invalid_step_later_in_sequence_is_rejected(self):
        # A valid first step must not smuggle in an invalid later step.
        thread_ref = self._schedule(
            _config([
                {"waitTime": 5, "waitUnit": "days", "message": "ok"},
                {"waitTime": -1, "waitUnit": "days", "message": "bad"},
            ])
        )
        self._assert_rejected_fail_closed(thread_ref)

    def test_valid_config_still_schedules(self):
        thread_ref = self._schedule(
            _config([
                {"waitTime": 5, "waitUnit": "days", "message": "first"},
                {"waitTime": 3, "waitUnit": "days", "message": "second"},
                {"waitTime": 2, "waitUnit": "days", "message": "third"},
            ])
        )
        update = thread_ref.updates[-1]
        self.assertEqual(update["followUpStatus"], "waiting")
        config = update["followUpConfig"]
        self.assertTrue(config["enabled"])
        # Monday +5 days lands on Saturday; weekend deferral moves it to
        # Monday 9am ET (13:00 UTC).
        self.assertEqual(
            config["nextFollowUpAt"].isoformat(), "2026-06-29T13:00:00+00:00"
        )

    def test_step_with_defaults_only_still_schedules(self):
        # waitTime/waitUnit omitted -> module defaults apply; must stay valid.
        thread_ref = self._schedule(_config([{"message": "just following up"}]))
        update = thread_ref.updates[-1]
        self.assertEqual(update["followUpStatus"], "waiting")
        self.assertTrue(update["followUpConfig"]["enabled"])


class MidFlightWaitClampTests(unittest.TestCase):
    """Stored configs (writable straight to Firestore) are clamped in-flight."""

    def test_schedule_next_followup_clamps_non_positive_wait_to_default(self):
        thread_ref = FakeThreadRef()
        followup_config = {
            "followUps": [
                {"waitTime": 1, "waitUnit": "hours", "message": "first"},
                {"waitTime": -10, "waitUnit": "hours", "message": "second"},
            ],
        }
        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), \
             patch.object(followup, "datetime", _fixed_datetime(MONDAY_NOW)):
            followup._schedule_next_followup("uid-1", "thread-1", followup_config, 0)

        update = thread_ref.updates[-1]
        scheduled = update["followUpConfig.nextFollowUpAt"]
        # Falls back to the default wait (3 hours) instead of firing immediately.
        self.assertEqual(scheduled.isoformat(), "2026-06-22T18:00:00+00:00")

    def test_schedule_next_followup_clamps_excessive_wait_to_unit_max(self):
        thread_ref = FakeThreadRef()
        followup_config = {
            "followUps": [
                {"waitTime": 1, "waitUnit": "days", "message": "first"},
                {"waitTime": 100000, "waitUnit": "days", "message": "second"},
            ],
        }
        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), \
             patch.object(followup, "datetime", _fixed_datetime(MONDAY_NOW)):
            followup._schedule_next_followup("uid-1", "thread-1", followup_config, 0)

        scheduled = thread_ref.updates[-1]["followUpConfig.nextFollowUpAt"]
        self.assertGreater(scheduled, MONDAY_NOW)
        # 90-day cap (+ up to 2 days of weekend deferral).
        self.assertLessEqual(scheduled, MONDAY_NOW + timedelta(days=92))

    def test_auto_response_reschedule_clamps_non_positive_wait(self):
        thread_ref = FakeThreadRef({
            "status": "active",
            "followUpStatus": "paused",
            "followUpConfig": {
                "enabled": True,
                "currentFollowUpIndex": 0,
                "followUps": [
                    {"waitTime": 0, "waitUnit": "hours", "message": "first"},
                ],
            },
        })
        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), \
             patch.object(followup, "datetime", _fixed_datetime(MONDAY_NOW)):
            result = followup.schedule_followup_after_auto_response("uid-1", "thread-1")

        self.assertTrue(result)
        scheduled = thread_ref.updates[-1]["followUpConfig.nextFollowUpAt"]
        self.assertEqual(scheduled.isoformat(), "2026-06-22T18:00:00+00:00")


class FakeInboundTimestamp:
    """Mimics a Firestore timestamp exposing .timestamp()."""

    def __init__(self, dt):
        self._dt = dt

    def timestamp(self):
        return self._dt.timestamp()


class ResumeFollowupWaitTests(unittest.TestCase):
    """resume_followup_if_silent must honour the step's unit-aware wait delta.

    _followup_wait_delta() already returns a unit-correct timedelta. The resume
    path must reuse that delta (capped at 1 day) instead of reinterpreting the
    raw scalar as days — otherwise a 30-minute or 2-hour step is silently
    stretched into a full 1-day delay on resume.
    """

    def _resume(self, followups, current_index=0):
        old_inbound = MONDAY_NOW - timedelta(days=10)  # well past the 3-day silence gate
        thread_ref = FakeThreadRef({
            "followUpStatus": "paused",
            "lastInboundAt": FakeInboundTimestamp(old_inbound),
            "followUpConfig": {
                "enabled": True,
                "currentFollowUpIndex": current_index,
                "followUps": followups,
            },
        })
        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), \
             patch.object(followup, "datetime", _fixed_datetime(MONDAY_NOW)):
            result = followup.resume_followup_if_silent("uid-1", "thread-1")
        return result, thread_ref

    def test_minutes_step_resumes_after_minutes_not_a_day(self):
        result, thread_ref = self._resume(
            [{"waitTime": 30, "waitUnit": "minutes", "message": "ping"}]
        )
        self.assertTrue(result)
        scheduled = thread_ref.updates[-1]["followUpConfig.nextFollowUpAt"]
        self.assertEqual(scheduled, MONDAY_NOW + timedelta(minutes=30))

    def test_hours_step_resumes_after_hours_not_a_day(self):
        result, thread_ref = self._resume(
            [{"waitTime": 2, "waitUnit": "hours", "message": "ping"}]
        )
        self.assertTrue(result)
        scheduled = thread_ref.updates[-1]["followUpConfig.nextFollowUpAt"]
        self.assertEqual(scheduled, MONDAY_NOW + timedelta(hours=2))

    def test_multiday_step_is_capped_at_one_day(self):
        result, thread_ref = self._resume(
            [{"waitTime": 5, "waitUnit": "days", "message": "ping"}]
        )
        self.assertTrue(result)
        scheduled = thread_ref.updates[-1]["followUpConfig.nextFollowUpAt"]
        self.assertEqual(scheduled, MONDAY_NOW + timedelta(days=1))

    def test_poisoned_negative_wait_falls_back_and_stays_capped(self):
        # Negative stored waitTime -> _followup_wait_delta falls back to the
        # default (1 day here), which is then capped at 1 day. Never immediate.
        result, thread_ref = self._resume(
            [{"waitTime": -99, "waitUnit": "minutes", "message": "ping"}]
        )
        self.assertTrue(result)
        scheduled = thread_ref.updates[-1]["followUpConfig.nextFollowUpAt"]
        self.assertGreater(scheduled, MONDAY_NOW)
        self.assertLessEqual(scheduled, MONDAY_NOW + timedelta(days=1))


if __name__ == "__main__":
    unittest.main()
