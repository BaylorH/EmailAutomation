"""The chokepoint every automatic reply passes through now reads the thread.

A sweep of every outbound path (2026-08-22, prompted by the broker who was asked
for the specification of a building he had just said was leased) found the same
shape in eight places, and one structural reason underneath all of them:

  `send_reply_in_thread` is the single chokepoint for every automatic reply in
  the product. It loaded the whole thread document -- and read exactly ONE field
  off it, `clientId`. Status, statusReason, pendingTerminalReason, nonViableAt
  and optedOutAt were all sitting in the dict it had just fetched, and all
  discarded.

Worse, the fields that record "this conversation is over" were close to
write-only. `nonViableAt` and `nonViableReason` had NO reader anywhere in the
repository. `pendingTerminalReason` -- written to every thread root on the row
specifically so a terminal decision SURVIVES the fallible sheet work that
follows it -- was read by the follow-up lane and by nothing else.

So the guard goes at the chokepoint, not at each caller. A per-caller fix
protects the callers somebody remembered; the queued-reply replay lane, which
never re-reads the thread at all, was not one of them, and neither is whatever
gets written next year.

Two things this must NOT do, both pinned below:
  - it must not silence the terminal acknowledgement, which exists precisely to
    answer a conversation the product has just resolved;
  - it must not queue a retry, because a "failed" send is retried a few minutes
    later and would post the exact message the guard just refused.
"""
import os
import unittest
from unittest.mock import MagicMock, patch

# Set BEFORE importing the package: app_config raises at import time without
# credentials, and this module must be runnable on its own rather than only
# when some earlier-collected test happens to have set it first.
os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import processing  # noqa: E402


class ThreadResolvedBlockReasonTests(unittest.TestCase):
    """The predicate itself, over the states that mean the conversation is over."""

    def test_a_live_thread_is_not_blocked(self):
        for data in (
            {},
            None,
            {"status": "active"},
            {"status": "paused"},
            {"status": "active", "clientId": "client-1"},
        ):
            with self.subTest(data=data):
                self.assertIsNone(processing.thread_resolved_send_block_reason(data))

    def test_a_pending_terminal_decision_blocks(self):
        """The field that exists to survive a half-finished terminal pass.

        This is the one that matters most: it is written BEFORE the sheet work,
        so it is present exactly in the window where the in-request local that
        used to gate the reply has been recomputed and says the row is alive.
        """
        reason = processing.thread_resolved_send_block_reason(
            {"status": "active", "pendingTerminalReason": "property_unavailable"}
        )
        self.assertIsNotNone(reason)
        self.assertIn("property_unavailable", reason)

    def test_a_recorded_non_viable_property_blocks(self):
        reason = processing.thread_resolved_send_block_reason(
            {"status": "active", "nonViableAt": "2026-08-22T00:00:00Z",
             "nonViableReason": "requirements_mismatch"}
        )
        self.assertIsNotNone(reason)
        self.assertIn("requirements_mismatch", reason)

    def test_an_opted_out_contact_blocks(self):
        self.assertIsNotNone(
            processing.thread_resolved_send_block_reason(
                {"status": "active", "optedOutAt": "2026-08-22T00:00:00Z"}
            )
        )

    def test_terminal_statuses_block(self):
        for status in ("stopped", "completed", "archived", "STOPPED", " Completed "):
            with self.subTest(status=status):
                self.assertIsNotNone(
                    processing.thread_resolved_send_block_reason({"status": status})
                )


class SendReplyChokepointGuardTests(unittest.TestCase):
    """The guard as actually wired into send_reply_in_thread."""

    def setUp(self):
        processing._reset_reply_send_outcome()
        self.addCleanup(processing._reset_reply_send_outcome)

    def _fs_with_thread(self, thread_data):
        snapshot = MagicMock()
        snapshot.exists = True
        snapshot.to_dict.return_value = dict(thread_data)
        fs = MagicMock()
        (fs.collection.return_value.document.return_value
           .collection.return_value.document.return_value.get.return_value) = snapshot
        return fs

    def _send(self, thread_data, **kwargs):
        # NOTE: send_reply_in_thread does `from .clients import _fs` INSIDE its
        # own body, which shadows the module global. Patching
        # processing._fs here looks right and silently does nothing -- the
        # first version of this test passed the guard by accident and failed
        # later at the Graph call instead.
        fs = self._fs_with_thread(thread_data)
        from email_automation import clients
        with patch.object(clients, "_fs", fs), \
             patch.object(processing, "_fs", fs), \
             patch.object(processing, "resolve_outbound_mode",
                          return_value=processing.OUTBOUND_MODE_LIVE), \
             patch.object(processing, "_automatic_inbox_replies_allowed",
                          return_value=True), \
             patch.object(processing, "get_client_automation_decision") as decision:
            decision.return_value.denies_autonomous_work = False
            return processing.send_reply_in_thread(
                "uid-1", {}, "Hi Marcus,\n\nCould you confirm the square footage?",
                "msg-1", "broker@example.test", "thread-1", **kwargs,
            )

    def test_a_resolved_thread_refuses_the_send(self):
        sent = self._send({"clientId": "client-1", "pendingTerminalReason": "property_unavailable"})
        self.assertFalse(sent)
        self.assertEqual(
            "suppressed_thread_resolved",
            processing._get_reply_send_outcome().outcome,
        )

    def test_the_terminal_acknowledgement_is_allowed_through(self):
        """Without this the fix would silence the ONE reply that must go out.

        "Thank you for letting me know that property is no longer available" is
        addressed to a conversation the product has just resolved, by
        definition. A guard that cannot tell that reply apart from a
        specification request does not fix the defect -- it replaces a rude
        answer with no answer.
        """
        sent = self._send(
            {"clientId": "client-1", "pendingTerminalReason": "property_unavailable"},
            allow_terminal_thread=True,
        )
        self.assertNotEqual(
            "suppressed_thread_resolved",
            processing._get_reply_send_outcome().outcome,
            "the terminal acknowledgement was silenced by its own guard",
        )
        del sent  # the Graph call is not exercised here; only the gate is

    def test_a_live_thread_is_untouched_by_the_guard(self):
        self._send({"clientId": "client-1", "status": "active"})
        self.assertNotEqual(
            "suppressed_thread_resolved",
            processing._get_reply_send_outcome().outcome,
        )


class ResolvedSuppressionIsNotAFailureTests(unittest.TestCase):
    """A refusal must never be queued for retry."""

    def setUp(self):
        processing._reset_reply_send_outcome()
        self.addCleanup(processing._reset_reply_send_outcome)

    def test_no_retry_is_queued_for_a_resolved_conversation(self):
        """Otherwise the guard is worse than useless.

        The failure lane turns a False return into a queued pending response,
        which the scheduler replays for up to five attempts. That would post the
        exact message the guard just refused, a few minutes later, with the
        guard's own refusal as the reason it was queued.
        """
        processing._set_reply_send_outcome(
            error="refused: already resolved",
            outcome="suppressed_thread_resolved",
        )
        with patch.object(processing, "queue_pending_response") as queue:
            outcome = processing._queue_response_retry_or_reconciliation(
                "uid-1", "thread-1", "msg-1", "broker@example.test", "body", "client-1",
            )
        queue.assert_not_called()
        self.assertEqual("recipient_suppressed", outcome)


if __name__ == "__main__":
    unittest.main()
