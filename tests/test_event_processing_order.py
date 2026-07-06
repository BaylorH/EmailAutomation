"""Event-processing order — crash-window guard (LIVE break 900 Alt Suggest St).

A scan run died between processing property_unavailable (which STOPPED the
thread) and new_property. The retry re-scanned the message, hit the
terminal-thread guard, and saved it "for history only" — the suggested
replacement property was permanently lost with no operator notification and
the queued reply never sent.

Guard: terminalizing events (contact_optout / property_unavailable /
close_conversation) always process LAST, so a crash mid-loop can no longer
strand informational escalations behind the terminal-thread retry guard. The
stale-event skip is driven by a PRECOMPUTED will-terminalize check, so its
semantics no longer depend on the LLM's arbitrary event order.
"""
import os
import sys
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from email_automation.processing import (  # noqa: E402
    _order_events_for_processing,
    _property_unavailable_event_applies_to_row,
    _should_skip_event_after_original_row_terminalized,
    PROPERTY_UNAVAILABLE_KEYWORDS,
)


def _types(events):
    return [e.get("type") for e in events]


class OrderEventsForProcessingTests(unittest.TestCase):
    def test_terminalizing_events_run_last(self):
        # The exact live shape: unavailable emitted BEFORE new_property.
        events = [
            {"type": "property_unavailable", "reason": "under_loi"},
            {"type": "new_property", "address": "1100 Fresh Listing Ave"},
        ]
        self.assertEqual(
            _types(_order_events_for_processing(events)),
            ["new_property", "property_unavailable"],
        )

    def test_optout_last_makes_final_state_deterministic(self):
        # [contact_optout, wrong_contact] previously ended the thread PAUSED
        # (wrong_contact ran second); terminal-last always ends stopped.
        events = [
            {"type": "contact_optout", "reason": "do_not_contact"},
            {"type": "wrong_contact", "suggestedEmail": "dana@x.com"},
        ]
        self.assertEqual(
            _types(_order_events_for_processing(events)),
            ["wrong_contact", "contact_optout"],
        )

    def test_stable_order_within_partitions(self):
        events = [
            {"type": "tour_requested"},
            {"type": "close_conversation"},
            {"type": "call_requested"},
            {"type": "property_unavailable"},
            {"type": "needs_user_input"},
        ]
        self.assertEqual(
            _types(_order_events_for_processing(events)),
            ["tour_requested", "call_requested", "needs_user_input",
             "close_conversation", "property_unavailable"],
        )

    def test_empty_and_informational_only_lists_unchanged(self):
        self.assertEqual(_order_events_for_processing([]), [])
        events = [{"type": "tour_requested"}, {"type": "new_property"}]
        self.assertEqual(_types(_order_events_for_processing(events)),
                         ["tour_requested", "new_property"])


class PrecomputedNonviableSkipTests(unittest.TestCase):
    """The stale-event skip must fire for informational events even though the
    terminalizing event now processes AFTER them — via the precomputed flag."""

    def _will_go_nonviable(self, events, row_anchor, message_text):
        return any(
            (e or {}).get("type") == "property_unavailable"
            and _property_unavailable_event_applies_to_row(
                e,
                row_anchor=row_anchor,
                message_text=message_text,
                unavailable_keywords=PROPERTY_UNAVAILABLE_KEYWORDS,
            )
            for e in events
        )

    def test_tour_for_dying_row_still_skipped(self):
        events = _order_events_for_processing([
            {"type": "property_unavailable", "reason": "leased",
             "address": "900 Alt Suggest St", "city": "Austin"},
            {"type": "tour_requested"},
        ])
        flag = self._will_go_nonviable(
            events, "900 Alt Suggest St, Austin",
            "900 Alt Suggest St is fully leased. Want to tour it anyway?")
        self.assertTrue(flag)
        self.assertTrue(_should_skip_event_after_original_row_terminalized(
            "tour_requested", old_row_became_nonviable=flag))
        # new_property must NOT be skipped in the same proposal.
        self.assertFalse(_should_skip_event_after_original_row_terminalized(
            "new_property", old_row_became_nonviable=flag))

    def test_unavailable_for_other_property_does_not_skip(self):
        # The unavailable event names ANOTHER property — current row stays live,
        # so informational events must process normally.
        events = [
            {"type": "property_unavailable", "reason": "leased",
             "address": "123 Other Rd", "city": "Dallas"},
            {"type": "tour_requested"},
        ]
        flag = self._will_go_nonviable(
            events, "900 Alt Suggest St, Austin",
            "123 Other Rd got leased, but 900 Alt Suggest St is available - come tour it.")
        self.assertFalse(flag)
        self.assertFalse(_should_skip_event_after_original_row_terminalized(
            "tour_requested", old_row_became_nonviable=flag))


if __name__ == "__main__":
    unittest.main()
