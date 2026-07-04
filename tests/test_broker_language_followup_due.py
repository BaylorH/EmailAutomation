"""
Pressure test for the followup_due event class.

EVENT: followup_due -- a follow-up email is scheduled/"due" for a thread.

The DETERMINISTIC safety gate that decides whether a due follow-up may actually
be sent is:

    email_automation.followup._followup_terminal_block_reason(
        thread_data, followup_config, followup_index
    )

It is called from _send_followup_email() right before the Graph createReplyAll /
send sequence (followup.py ~line 721). It returns a human-readable *reason string*
when the follow-up MUST NOT send (guard FIRES), or None when the follow-up is
allowed to send (guard does NOT fire).

stopIf (safety violations we are hunting):
  - follow-up sends after broker or user reply
  - follow-up sends for a stopped/completed/escalated row

This suite drives the REAL guard on 15+ realistic thread-state "phrasings" of a
due follow-up plus the near-miss controls. No Firestore/Sheets/Graph calls happen
because _followup_terminal_block_reason is a pure function over dicts.

Assertions are written to the CORRECT behavior. Where the current guard is unsafe,
the assertion is left RED to pin the bug (do NOT weaken it).
"""

import os
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation.followup import _followup_terminal_block_reason


def _active_config(index=0, n_followups=3):
    """A normal, enabled follow-up config whose index matches and is in range."""
    return {
        "enabled": True,
        "currentFollowUpIndex": index,
        "followUps": [{"waitTime": 3, "waitUnit": "days"} for _ in range(n_followups)],
    }


def fires(thread_data, followup_config=None, followup_index=0):
    """True if the guard blocks the send (returns a reason)."""
    cfg = followup_config if followup_config is not None else _active_config(followup_index)
    return _followup_terminal_block_reason(thread_data, cfg, followup_index) is not None


class FollowupDueRealThreats(unittest.TestCase):
    """Real-threat 'phrasings' (thread states) where a due follow-up MUST be blocked."""

    # ---- Terminal thread status: must block (stopIf: stopped/completed row) ----

    def test_status_stopped(self):
        # "Follow-up due after user stopped the row."
        self.assertTrue(fires({"status": "stopped"}), "must block: user stopped the row")

    def test_status_completed(self):
        self.assertTrue(fires({"status": "completed"}), "must block: thread completed")

    def test_status_completed_uppercase_padded(self):
        # Messy/regional casing + whitespace from a UI write.
        self.assertTrue(fires({"status": "  COMPLETED  "}), "must block: COMPLETED (caps/pad)")

    def test_status_stopped_titlecase(self):
        self.assertTrue(fires({"status": "Stopped"}), "must block: Stopped (titlecase)")

    def test_status_archived(self):
        self.assertTrue(fires({"status": "archived"}), "must block: archived thread")

    def test_status_action_needed_literal(self):
        # The one 'action_needed' string the code actually writes (fail-closed path).
        self.assertTrue(fires({"status": "action_needed"}), "must block: action_needed")

    # ---- Broker replied: must block (stopIf: sends after broker reply) ----

    def test_has_inbound_reply_true(self):
        # "Broker replied" -- recorded on the thread.
        self.assertTrue(
            fires({"status": "active", "hasInboundReply": True}),
            "must block: broker has replied",
        )

    def test_has_inbound_reply_true_even_if_status_active(self):
        # Reply recorded but status not yet flipped -- still must block.
        self.assertTrue(
            fires({"status": "active", "followUpStatus": "waiting", "hasInboundReply": True}),
            "must block: reply flag set regardless of status",
        )

    # ---- Follow-up tracking terminal states: must block ----

    def test_followup_status_paused(self):
        self.assertTrue(fires({"followUpStatus": "paused"}), "must block: follow-up paused")

    def test_followup_status_needs_review(self):
        self.assertTrue(fires({"followUpStatus": "needs_review"}), "must block: needs_review")

    def test_followup_status_max_reached(self):
        self.assertTrue(fires({"followUpStatus": "max_reached"}), "must block: max_reached")

    def test_followup_status_stopped(self):
        self.assertTrue(fires({"followUpStatus": "stopped"}), "must block: follow-up stopped")

    # ---- Review-required status reasons ----

    def test_status_reason_manual_continuation(self):
        # User manually continued the conversation.
        self.assertTrue(
            fires({"status": "active", "statusReason": "manual_continuation"}),
            "must block: manual_continuation",
        )

    def test_status_reason_guard_failed(self):
        self.assertTrue(
            fires({"status": "active", "statusReason": "followup_send_guard_failed"}),
            "must block: prior guard failed closed",
        )

    # ---- Config-level terminal states ----

    def test_config_disabled(self):
        cfg = {"enabled": False, "currentFollowUpIndex": 0, "followUps": [{}]}
        self.assertTrue(fires({"status": "active"}, cfg), "must block: follow-up disabled")

    def test_index_changed_out_from_under_us(self):
        # Another worker advanced the index (dup-send race).
        cfg = {"enabled": True, "currentFollowUpIndex": 2, "followUps": [{}, {}, {}]}
        self.assertTrue(fires({"status": "active"}, cfg, followup_index=1),
                        "must block: index changed 1 -> 2")

    def test_index_beyond_max(self):
        cfg = {"enabled": True, "currentFollowUpIndex": 3, "followUps": [{}, {}, {}]}
        self.assertTrue(fires({"status": "active"}, cfg, followup_index=3),
                        "must block: index past end of sequence")

    # ---- ESCALATED THREADS (status="paused") -- the real safety hole ----
    # update_thread_status() writes status="paused" for call_requested / needs_user_input /
    # wrong_contact escalations and does NOT touch followUpStatus or followUpConfig.enabled.
    # resume_followup_if_silent() can later flip followUpStatus back to "waiting" and reset
    # hasInboundReply=False, so a due follow-up reaches the guard with status="paused".
    # These MUST block (thread escalated to a human). Left RED to pin the bug.

    def test_status_paused_escalated_needs_user_input(self):
        self.assertTrue(
            fires({"status": "paused", "followUpStatus": "waiting",
                   "statusReason": "needs_user_input:missing_size", "hasInboundReply": False}),
            "must block: thread escalated to user (needs_user_input)",
        )

    def test_status_paused_escalated_call_requested(self):
        self.assertTrue(
            fires({"status": "paused", "followUpStatus": "waiting",
                   "statusReason": "call_requested", "hasInboundReply": False}),
            "must block: broker asked for a call (escalated)",
        )

    def test_status_paused_escalated_wrong_contact(self):
        self.assertTrue(
            fires({"status": "paused", "followUpStatus": "waiting",
                   "statusReason": "wrong_contact:not_leasing", "hasInboundReply": False}),
            "must block: wrong-contact escalation",
        )


class FollowupDueNearMisses(unittest.TestCase):
    """Controls: legitimate due follow-up (must send) and reply-window controls."""

    def test_legit_due_no_reply(self):
        # Seed 1: "No broker reply after configured follow-up window." MUST be allowed.
        self.assertFalse(
            fires({"status": "active", "followUpStatus": "waiting", "hasInboundReply": False}),
            "legit follow-up should be allowed to send",
        )

    def test_legit_due_missing_optional_fields(self):
        # Minimal active thread, nothing terminal -> allowed.
        self.assertFalse(fires({"status": "active"}), "minimal active thread should send")

    def test_near_miss_broker_replied_recorded(self):
        # "Broker replied but inbox processing has not completed yet." -- once the reply is
        # recorded (hasInboundReply=True) the guard MUST block the follow-up.
        self.assertTrue(
            fires({"status": "active", "followUpStatus": "waiting", "hasInboundReply": True}),
            "near-miss: recorded broker reply must block send",
        )

    def test_near_miss_user_manual_followup(self):
        # "User manually followed up in Sent Items." Surfaces as manual_continuation status.
        self.assertTrue(
            fires({"status": "active", "statusReason": "manual_continuation"}),
            "near-miss: user manual continuation must block send",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
