"""A' regression pins for email_automation/tour_scheduling.py.

Two aligned fixes, both driven against the REAL deterministic guard
`looks_like_tour_only_unavailable` (and the augmenter that consumes it) with the
verbatim misread text:

  FIX-06 (M20, HIGH) — slot-scoped unavailability ("that 10 AM window is no
    longer available") must be recognised as *tour-scoped*, not property-scoped.
    `_TOUR_SUBJECT` gains slot|window|time|appointment. The guard must return
    True, and the augmenter must NOT terminalise the row while the broker says
    the listing is fine and offers an alternate time — the tour_requested must
    survive.

  CodeRabbit (PR#15, Major) — a genuine PROPERTY terminal phrase that the shared
    canonical list (`ai_processing._UNAVAILABLE_PATTERNS`) already recognises
    ("... no longer available, so we wont be able to arrange any tours") must NOT
    be swallowed by the tour-only guard. The guard must return False so the
    property_unavailable path runs.

The guard is pure — no Firestore / Sheets / Graph is touched.
"""

import os
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation.ai_processing import _augment_events_with_deterministic_signals
from email_automation.tour_scheduling import looks_like_tour_only_unavailable


# --- Verbatim misread text ---------------------------------------------------

# M20 (as sent to the live gpt-5.2 model). Slot-scoped unavailability + explicit
# viability + alternate-time offer.
M20_TEXT = (
    "Unfortunately that 10 AM window is no longer available on my end - I got "
    "double-booked. The listing itself is totally fine, nothing has changed with "
    "the space. Could we do 2 PM on Friday instead?"
)

# CodeRabbit PR#15 phrasing — the PROPERTY is terminal ("no longer available"),
# tours are merely the downstream consequence.
CODERABBIT_TEXT = (
    "The suite is no longer available, so we wont be able to arrange any tours."
)


def _tour_thread(broker_msg):
    """Conversation carrying real tour-scheduling context + the broker reply."""
    return [
        {
            "direction": "outbound",
            "content": "Can you confirm a tour date and requested arrival time?",
        },
        {"direction": "inbound", "content": broker_msg},
    ]


def _conv(text):
    return [{"direction": "inbound", "content": text}]


def _event_types(proposal):
    return [(e or {}).get("type") for e in (proposal or {}).get("events", [])]


class Fix06SlotScopedIsTourOnly(unittest.TestCase):
    """FIX-06 / M20: slot|window|time|appointment count as tour subjects."""

    def test_m20_guard_reads_window_as_tour_scoped(self):
        self.assertTrue(
            looks_like_tour_only_unavailable(M20_TEXT),
            "FALSE NEGATIVE (M20): a slot-scoped decline ('that 10 AM window is "
            "no longer available') was not recognised as tour-scoped, so the "
            "property_unavailable branch wins and the row is wrongly terminalised.",
        )

    def test_m20_slot_and_appointment_variants_are_tour_scoped(self):
        for msg in (
            "That 2 PM slot is no longer available, but the space is still on the market.",
            "That appointment isn't available anymore; the listing is unchanged.",
            "That time is no longer available on my end, but the unit is still active.",
        ):
            with self.subTest(msg=msg):
                self.assertTrue(
                    looks_like_tour_only_unavailable(msg),
                    f"FALSE NEGATIVE: slot-scoped decline not tour-scoped: {msg!r}",
                )

    def test_m20_augmenter_keeps_tour_and_never_terminalizes(self):
        # The LLM legitimately emits a reschedule tour_requested; the augmenter
        # must neither inject property_unavailable nor strip the tour_requested.
        proposal = {"events": [{"type": "tour_requested", "reason": "reschedule"}]}
        out = _augment_events_with_deterministic_signals(proposal, _tour_thread(M20_TEXT))
        types = _event_types(out)
        self.assertNotIn(
            "property_unavailable",
            types,
            "SAFETY (M20): slot-scoped decline wrongly terminalised the property.",
        )
        self.assertIn(
            "tour_requested",
            types,
            "SAFETY (M20): the broker's reschedule/tour_requested was dropped.",
        )


class CodeRabbitPropertyTerminalNotTourOnly(unittest.TestCase):
    """CodeRabbit PR#15: a canonical PROPERTY terminal must not read tour-only."""

    def test_property_terminal_is_not_tour_only(self):
        self.assertFalse(
            looks_like_tour_only_unavailable(CODERABBIT_TEXT),
            "FALSE POSITIVE (CodeRabbit): a genuine property terminal ('no longer "
            "available') was swallowed by the tour-only guard, skipping the "
            "property_unavailable path.",
        )

    def test_canonical_terminals_that_also_mention_tours_are_not_tour_only(self):
        for msg in (
            "We just leased the building, so no tours going forward.",
            "The property is under contract; we can't offer any tours now.",
            "That space has been leased, so no showings anymore.",
        ):
            with self.subTest(msg=msg):
                self.assertFalse(
                    looks_like_tour_only_unavailable(msg),
                    f"FALSE POSITIVE: property terminal read as tour-only: {msg!r}",
                )

    def test_property_terminal_augmenter_fires_property_unavailable(self):
        proposal = {"events": []}
        out = _augment_events_with_deterministic_signals(proposal, _conv(CODERABBIT_TEXT))
        self.assertIn(
            "property_unavailable",
            _event_types(out),
            "SAFETY (CodeRabbit): a dead property was not terminalised because "
            "the tour-only guard swallowed the terminal phrase.",
        )


class DiscriminatorHoldsBothWays(unittest.TestCase):
    """Pin the tour-scope discriminator so neither fix regresses the other."""

    def test_available_for_tours_scoping_stays_tour_only(self):
        # 'no longer available FOR TOURS' is availability scoped to touring -> the
        # suite is fine; must remain tour-only True even though the canonical
        # terminal 'no longer available' substring is present.
        self.assertTrue(
            looks_like_tour_only_unavailable(
                "The suite is no longer available for tours this week."
            )
        )

    def test_no_availability_to_show_stays_tour_only(self):
        self.assertTrue(
            looks_like_tour_only_unavailable(
                "No availability to show the space this week, but it's still listed."
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
