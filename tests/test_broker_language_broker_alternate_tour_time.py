"""
Pressure test: broker_alternate_tour_time.

Drives the REAL deterministic guards that decide how the system reacts when a
broker proposes a DIFFERENT tour time than the one on the invite:

  * email_automation.processing._classify_tour_invite_reply
        -> classifies broker text into an outcome (alternate_requested / confirmed / ...)
  * email_automation.processing._extract_tour_reply_time_mentions
        -> pulls the time tokens out of the reply, in order of appearance
  * email_automation.tour_scheduling.evaluate_alternate_tour_time
        -> the schedule-feasibility SAFETY guard (fits / conflict / needs_review)
  * email_automation.tour_scheduling.build_schedule_aware_tour_reply
        -> the drafted reply the operator sees

These functions are PURE (no Firestore / Sheets / Graph), so nothing external is
called. We reproduce the exact call the production pipeline makes at
processing.py ~L3584-3598 (evaluate `alternateTimes[0]`, then build the reply)
WITHOUT touching `_load_sibling_tour_schedule` (which is the only Firestore hop);
instead we hand-build the schedule list exactly as that loader would return it
for a single-stop campaign: `[{**thread_data, "id": thread_id}]`.

stopIf guards under test:
  1. "generic let me check response is drafted when slot is free"
  2. "system changes tour order without operator visibility" (a rejected time
      being silently confirmed is the sharpest form of this)

Assertions are written to the CORRECT behavior; where current behavior is wrong
they are left RED and the defect is recorded.
"""

import os
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import processing as P
from email_automation import tour_scheduling as T


THREAD_ID = "T1"


def minutes(label):
    return T.parse_tour_time_minutes(label)


def build_schedule(thread_data, arrival="10:00 AM", departure="10:30 AM"):
    """Mirror _load_sibling_tour_schedule for a single-stop campaign (no Firestore)."""
    stop = {
        "id": THREAD_ID,
        "tourInvite": {
            "arrivalTime": arrival,
            "departureTime": departure,
            "address": "123 Main St",
        },
    }
    # Also carry the current thread_data through, as the real loader does.
    merged = {**thread_data, **stop}
    return [merged]


def run_pipeline(message, thread_data, schedule_arrival="10:00 AM", schedule_departure="10:30 AM"):
    """Reproduce processing.py's alternate-tour-time path end to end."""
    classification = P._classify_tour_invite_reply(
        message,
        thread_data=thread_data,
        contact_name="Bob Broker",
        recipient_email="bob@example.com",
    )
    decision = None
    reply = classification.get("suggestedEmail", "")
    if classification.get("outcome") == "alternate_requested" and classification.get("alternateTimes"):
        schedule = build_schedule(thread_data, schedule_arrival, schedule_departure)
        decision = T.evaluate_alternate_tour_time(
            schedule,
            THREAD_ID,
            classification["alternateTimes"][0],  # <-- exactly what production passes
        )
        reply = T.build_schedule_aware_tour_reply(
            "Bob Broker", "bob@example.com", thread_data, decision
        )
    return classification, decision, reply


# thread_data whose stored invite time does NOT let the reject-time filter catch
# the rejected token (arrival unset -> _filter_requested_tour_times is a no-op).
INVITE_NO_STORED_TIME = {
    "source": "dashboard_tour_planner",
    "tourInvite": {"address": "123 Main St"},
}
# thread_data with a stored arrival that matches the rejected token in the seed.
INVITE_ARRIVAL_10AM = {
    "source": "dashboard_tour_planner",
    "tourInvite": {"arrivalTime": "10:00 AM", "departureTime": "10:30 AM", "address": "123 Main St"},
}


# (name, message, thread_data, rejected_labels, proposed_labels)
# rejected_labels = times the broker explicitly said do NOT work
# proposed_labels = the replacement times the broker offered
ALTERNATE_THREATS = [
    ("seed_10am_no_stored_invite",
     "10 AM does not work; can you do 2 PM instead?",
     INVITE_NO_STORED_TIME, ["10 AM"], ["2 PM"]),
    ("seed_10am_stored_invite",
     "10 AM does not work; can you do 2 PM instead?",
     INVITE_ARRIVAL_10AM, ["10 AM"], ["2 PM"]),
    ("terse",
     "2pm instead of 10am?",
     INVITE_NO_STORED_TIME, ["10 AM"], ["2 PM"]),
    ("verbose",
     "Unfortunately the 10:00 AM slot we discussed will not work for my client. "
     "Would 3:00 PM instead be possible on your end?",
     INVITE_NO_STORED_TIME, ["10 AM"], ["3 PM"]),
    ("typod",
     "10am doesnt work, can we do 2pm insted?",
     INVITE_NO_STORED_TIME, ["10 AM"], ["2 PM"]),
    ("multi_intent_rent",
     "The rent is $32/SF. Also 10 AM doesn't work - can you do 1 PM instead?",
     INVITE_NO_STORED_TIME, ["10 AM"], ["1 PM"]),
    ("all_caps",
     "10 AM DOES NOT WORK. CAN YOU DO 2 PM INSTEAD?",
     INVITE_NO_STORED_TIME, ["10 AM"], ["2 PM"]),
    ("signature_block",
     "9 AM won't work for us, let's do 11 AM instead.\n\nBest,\nJane Doe\nSenior Broker\nAcme CRE",
     INVITE_NO_STORED_TIME, ["9 AM"], ["11 AM"]),
    ("conflicting_old_quote",
     "Following up - I know I said 10 AM works, but that no longer works. 2 PM instead?",
     INVITE_NO_STORED_TIME, ["10 AM"], ["2 PM"]),
    ("works_better",
     "Need to reschedule - 2 PM works better than the 10 AM we had.",
     INVITE_NO_STORED_TIME, ["10 AM"], ["2 PM"]),
    ("cant_do",
     "Can't do 10 AM, how about 2 PM in the afternoon?",
     INVITE_NO_STORED_TIME, ["10 AM"], ["2 PM"]),
    ("wont_work_please",
     "10 AM won't work for us. 2 PM instead please.",
     INVITE_NO_STORED_TIME, ["10 AM"], ["2 PM"]),
    ("unavailable_then_offer",
     "We are not available at 10 AM. 4 PM instead?",
     INVITE_NO_STORED_TIME, ["10 AM"], ["4 PM"]),
]


class TestAlternateTourTimeFiresCorrectly(unittest.TestCase):
    """Real-threat alternate-time phrasings must be classified as an alternate request."""

    def test_alternate_requested_outcome_fires(self):
        misses = []
        for name, msg, td, _rej, _prop in ALTERNATE_THREATS:
            classification = P._classify_tour_invite_reply(
                msg, thread_data=td, contact_name="Bob", recipient_email="bob@example.com"
            )
            if classification.get("outcome") != "alternate_requested":
                misses.append((name, classification.get("outcome")))
        self.assertEqual(misses, [], f"alternate-time phrasings not classified as alternate_requested: {misses}")


class TestNoRejectedTimeGetsConfirmed(unittest.TestCase):
    """
    SAFETY: the drafted reply must never auto-confirm a time the broker just
    rejected. Correct behavior = evaluate/confirm the PROPOSED alternate.
    """

    def test_reply_never_confirms_a_rejected_time(self):
        offenders = []
        for name, msg, td, rejected_labels, proposed_labels in ALTERNATE_THREATS:
            classification, decision, reply = run_pipeline(msg, td)
            if decision is None:
                continue
            evaluated = decision.get("arrivalTime", "")
            eval_min = minutes(evaluated)
            rejected_mins = {minutes(r) for r in rejected_labels}
            proposed_mins = {minutes(p) for p in proposed_labels}
            confirmed = "please consider that confirmed" in reply.lower()
            # The evaluated time must be a proposed alternate, never a rejected one.
            if eval_min in rejected_mins and eval_min not in proposed_mins:
                offenders.append((name, evaluated, "REJECTED time evaluated/confirmed", confirmed, reply.strip().splitlines()[2:3]))
        self.assertEqual(
            offenders, [],
            "System evaluated/confirmed a time the broker explicitly rejected "
            f"(should have used the proposed alternate): {offenders}",
        )


class TestNearMissesDoNotFire(unittest.TestCase):
    """Controls: internal/near-miss phrasings must NOT be handled as an alternate tour request."""

    NEAR_MISSES = [
        ("lane_disabled", "Tour Scheduling lane is disabled for normal users."),
        ("conflict_note", "Alternate time conflicts with another confirmed stop."),
    ]

    def test_near_miss_not_alternate_requested(self):
        for name, msg in self.NEAR_MISSES:
            classification = P._classify_tour_invite_reply(
                msg, thread_data=INVITE_ARRIVAL_10AM,
                contact_name="Bob", recipient_email="bob@example.com",
            )
            self.assertNotEqual(
                classification.get("outcome"), "alternate_requested",
                f"near-miss {name!r} wrongly classified as alternate_requested",
            )

    def test_near_miss_does_not_auto_confirm_and_close(self):
        """A near-miss must not silently mark the tour confirmed / close the thread."""
        for name, msg in self.NEAR_MISSES:
            classification = P._classify_tour_invite_reply(
                msg, thread_data=INVITE_ARRIVAL_10AM,
                contact_name="Bob", recipient_email="bob@example.com",
            )
            with self.subTest(near_miss=name):
                self.assertNotEqual(
                    classification.get("outcome"), "confirmed",
                    f"near-miss {name!r} wrongly classified as a tour CONFIRMATION",
                )
                self.assertFalse(
                    classification.get("canCloseThread"),
                    f"near-miss {name!r} would auto-close the thread without operator action",
                )


class TestSlotFreeDraftsRealAnswerNotHold(unittest.TestCase):
    """
    stopIf #1: when the proposed slot is actually free, the reply must be a real
    schedule-aware answer, not a generic 'let me check' hold. (Sanity: the
    schedule-aware builder path is what production uses; this pins that a free
    slot yields a concrete answer once the proposed time is the one evaluated.)
    """

    def test_free_proposed_slot_yields_schedule_aware_reply(self):
        # 2 PM proposed, schedule only has the 10:00-10:30 stop -> 2 PM is free.
        thread_data = INVITE_NO_STORED_TIME
        schedule = build_schedule(thread_data)
        decision = T.evaluate_alternate_tour_time(schedule, THREAD_ID, "2 PM")
        reply = T.build_schedule_aware_tour_reply("Bob", "bob@example.com", thread_data, decision)
        self.assertEqual(decision["feasibility"], "fits")
        self.assertNotIn(
            "i'm checking the route and schedule", reply.lower(),
            "free slot drafted the generic 'let me check' hold instead of a real answer",
        )


class TestReorderDropsFullyRejected(unittest.TestCase):
    """CodeRabbit PR#15: when every extracted time is explicitly rejected and none
    is proposed, `_reorder_alternate_tour_times` must return [] rather than
    restoring the original (rejected) order — otherwise a REJECTED slot lands at
    alternateTimes[0] and the schedule pipeline evaluates/offers it."""

    def test_all_rejected_none_proposed_returns_empty(self):
        text = "Unfortunately 10 AM does not work and 2 PM does not work either."
        self.assertEqual(
            P._reorder_alternate_tour_times(["10:00 AM", "2:00 PM"], text, None),
            [],
        )

    def test_stored_slot_rejected_none_proposed_returns_empty(self):
        td = {"tourInvite": {"arrivalTime": "10:00 AM", "departureTime": "10:30 AM"}}
        self.assertEqual(
            P._reorder_alternate_tour_times(["10:00 AM"], "that time does not work for us", td),
            [],
        )

    def test_proposed_slot_still_kept(self):
        # Positive control: a genuine proposal is still surfaced.
        text = "10 AM does not work, let's do 2 PM instead."
        self.assertEqual(
            P._reorder_alternate_tour_times(["10:00 AM", "2:00 PM"], text, None),
            ["2:00 PM"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
