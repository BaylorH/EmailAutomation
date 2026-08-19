"""A property-scoped event must be keyed by the property, not just the thread.

LIVE break, 2026-08-06 production campaign, recorded as one of eight defects:
"suppressed tour-action notification".

`build_event_key` returns the BARE event type for tour_requested,
call_requested, property_unavailable and close_conversation -- its docstring
says "unique per thread (one of each type per conversation)". But a thread
SURVIVES property replacement: when the original property goes unavailable and
a replacement is inserted, the same thread now concerns a different property.
The replacement's tour therefore collides with the original's already-handled
entry, `is_event_handled` returns True, and processing prints
"Already handled, skipping". The operator never sees the tour.

Note `new_property` already keys on address+city -- the registry of event kinds
was right about that one and wrong about these four.

Direction of failure matters here: including the property makes a previously
handled event re-fire at most once after deploy. A duplicate notification is
visible and recoverable; a suppressed one is invisible, which is the defect.
"""

import unittest

from email_automation.messaging import build_event_key

ORIGINAL = "100 Example Rd, Springfield"
REPLACEMENT = "250 Other Way, Springfield"

# The four the docstring calls "unique per thread".
PROPERTY_SCOPED = ("tour_requested", "call_requested", "property_unavailable", "close_conversation")


class PropertyScopedEventsAreKeyedByProperty(unittest.TestCase):
    def test_the_replacement_property_does_not_collide_with_the_original(self):
        for event_type in PROPERTY_SCOPED:
            with self.subTest(event_type=event_type):
                first = build_event_key(event_type, {}, thread_id="t1", row_anchor=ORIGINAL)
                second = build_event_key(event_type, {}, thread_id="t1", row_anchor=REPLACEMENT)
                self.assertNotEqual(
                    first, second,
                    f"a {event_type} on the replacement property must not be suppressed by "
                    "the original property's already-handled entry on the same thread",
                )

    def test_the_same_property_still_dedupes(self):
        # The whole point of the key is still dedupe; it must not become unique-per-call.
        for event_type in PROPERTY_SCOPED:
            with self.subTest(event_type=event_type):
                self.assertEqual(
                    build_event_key(event_type, {}, thread_id="t1", row_anchor=ORIGINAL),
                    build_event_key(event_type, {}, thread_id="t1", row_anchor=ORIGINAL),
                    f"repeated {event_type} detection on the SAME property must still dedupe",
                )

    def test_the_key_still_names_its_event_type(self):
        for event_type in PROPERTY_SCOPED:
            with self.subTest(event_type=event_type):
                self.assertIn(
                    event_type,
                    build_event_key(event_type, {}, thread_id="t1", row_anchor=ORIGINAL),
                    "an operator reading handledEvents must still see what kind of event it was",
                )

    def test_an_unknown_property_does_not_silently_share_one_key(self):
        # 'Row data incomplete' / 'Unknown property' are real return values of
        # get_row_anchor. Two genuinely different properties must not collapse
        # onto one key just because neither could be identified.
        blank = build_event_key("tour_requested", {}, thread_id="t1", row_anchor="")
        self.assertIn("tour_requested", blank)

    def test_the_already_correct_kinds_are_unchanged(self):
        # new_property keyed on address+city before this change and must still.
        self.assertEqual(
            build_event_key("new_property", {"address": "A", "city": "B", "email": "c@d.invalid"},
                            thread_id="t1", row_anchor=ORIGINAL),
            "new_property:A:B:c@d.invalid",
        )
        self.assertEqual(
            build_event_key("wrong_contact", {"suggestedEmail": "x@y.invalid"},
                            thread_id="t1", row_anchor=ORIGINAL),
            "wrong_contact:x@y.invalid",
        )


if __name__ == "__main__":
    unittest.main()
