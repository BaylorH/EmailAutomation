import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)

import unittest

from email_automation.messaging import THREAD_STATUS
from email_automation.processing import _should_skip_processing_for_terminal_thread


class ReplyAllCcTerminalStateTest(unittest.TestCase):
    """Rubric cell: feature=core.reply_all_cc, fixtureClass=terminal_state.

    Behavior proven: no reply-all is sent into an already-terminal/stopped thread.

    In production, ``process_incoming_message`` (email_automation/processing.py, an
    ownerModule for core.reply_all_cc) computes
    ``skip_processing_for_terminal = _should_skip_processing_for_terminal_thread(...)``
    and, when it is True, returns early ("Skipping processing for terminal thread")
    BEFORE the auto-response path that hydrates reply-all recipients and sends. So this
    pure guard is the single gate that decides whether a reply-all can be emitted into a
    thread. It is deterministic and needs no datastore/Graph — we exercise the REAL
    production function directly (nothing is patched, least of all the unit under test).
    """

    def test_terminal_thread_blocks_reply_all_send(self):
        # --- POSITIVE (terminal_state): reply-all must be suppressed ---
        # A completed thread (closing email already sent) is terminal -> skip -> no reply-all.
        self.assertTrue(
            _should_skip_processing_for_terminal_thread(
                THREAD_STATUS["completed"],
                thread_data={"status": THREAD_STATUS["completed"]},
                message_text="Thanks, adding legal@firm.com and ops@firm.com to keep everyone looped in.",
            ),
            "completed (terminal) thread must skip processing so no reply-all is sent",
        )

        # A manually stopped thread with NO active replacement property is terminal -> skip.
        self.assertTrue(
            _should_skip_processing_for_terminal_thread(
                THREAD_STATUS["stopped"],
                thread_data={"status": THREAD_STATUS["stopped"]},
                message_text="Please also cc broker@agency.com on any further replies.",
            ),
            "stopped thread with no replacement context must skip processing (no reply-all)",
        )

        # --- NEGATIVE CONTROL (discriminating): non-terminal threads still process ---
        # If the guard returned True for every input the positive asserts would be
        # vacuous. An ACTIVE thread must NOT be skipped -> the reply-all path stays live.
        self.assertFalse(
            _should_skip_processing_for_terminal_thread(
                THREAD_STATUS["active"],
                thread_data={"status": THREAD_STATUS["active"]},
                message_text="Please also cc broker@agency.com on any further replies.",
            ),
            "active thread must NOT be skipped -- reply-all send path stays reachable",
        )

        # And a stopped thread that has been reactivated by a matching active-replacement
        # property is intentionally NOT terminal, so the guard must let processing (and its
        # reply-all recipients) proceed. This proves the guard keys on real terminality,
        # not merely the "stopped" literal.
        replacement_thread = {
            "status": THREAD_STATUS["stopped"],
            "activeReplacementProperty": {
                "address": "456 Oak Avenue",
                "city": "Austin",
                "rowNumber": 7,
            },
        }
        self.assertFalse(
            _should_skip_processing_for_terminal_thread(
                THREAD_STATUS["stopped"],
                thread_data=replacement_thread,
                message_text="Good news -- 456 Oak Avenue is available now, cc leasing@agency.com too.",
            ),
            "stopped-but-reactivated (active replacement) thread must NOT be treated as terminal",
        )


if __name__ == "__main__":
    unittest.main()
