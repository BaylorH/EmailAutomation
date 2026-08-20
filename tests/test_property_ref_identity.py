"""A property's identity is MINTED once, not re-derived from a display string.

LIVE break, 2026-08-06 production campaign: "suppressed tour-action
notification" (PROD-0806-3). `build_event_key` was made property-scoped for
tour/call/unavailable/close by putting `get_row_anchor(...)` output in the key.
That closed the reported instance and left the class open, because the anchor
is a DISPLAY STRING, not an identity:

  * `get_row_anchor` returns the literal `"Row data incomplete"` for a row whose
    address and city cells it cannot read, and `"Unknown property"` when it
    raises. Both are non-empty, so both sail past the `if not anchor` guard and
    become the property's "canonical identity". Two DIFFERENT unreadable
    properties on one surviving thread therefore share one key -- which is
    PROD-0806-3 exactly, re-armed. The replacement's tour is suppressed again.

  * Those two sentinels also differ from each other, so one property that is
    unreadable once and then raises once is billed as two properties.

  * The anchor is assembled out of live sheet cells. A broker correcting a
    misspelled street, or the city cell being filled in later, changes the
    anchor and therefore the key, so an already-handled event re-fires as a
    duplicate on a property that never moved.

A minted ref fixes all three at once: it is opaque, it is stable under
cosmetic anchor edits, and an unidentifiable row gets its own ref rather than
sharing the one global "Unknown property" bucket.
"""

import unittest

from email_automation.messaging import build_event_key
from email_automation.property_ref import (
    is_identifying_anchor,
    mint_property_ref,
    normalize_anchor,
)

PROPERTY_SCOPED = ("tour_requested", "call_requested", "property_unavailable", "close_conversation")

# The two literals get_row_anchor really returns when it cannot name the row.
SENTINELS = ("Row data incomplete", "Unknown property")


class SentinelAnchorsAreNotIdentities(unittest.TestCase):
    def test_the_sentinels_are_not_identifying(self):
        for sentinel in SENTINELS:
            with self.subTest(sentinel=sentinel):
                self.assertFalse(
                    is_identifying_anchor(sentinel),
                    f"{sentinel!r} is what get_row_anchor returns when it CANNOT name "
                    "the property; treating it as a name is how two properties come to "
                    "share one identity",
                )

    def test_blank_and_whitespace_are_not_identifying(self):
        for blank in ("", "   ", None):
            with self.subTest(blank=blank):
                self.assertFalse(is_identifying_anchor(blank))

    def test_a_real_address_is_identifying(self):
        self.assertTrue(is_identifying_anchor("100 Example Rd, Springfield"))


class AMintedRefIdentifiesTheProperty(unittest.TestCase):
    def test_two_unreadable_rows_on_one_thread_get_different_refs(self):
        # The replacement scenario: the thread survives, both rows are
        # unreadable, and the sentinel gives them the same name.
        original = mint_property_ref(
            client_id="c1", thread_id="t1", row_anchor="Row data incomplete", row_number=12,
        )
        replacement = mint_property_ref(
            client_id="c1", thread_id="t1", row_anchor="Row data incomplete", row_number=13,
        )
        self.assertTrue(original and replacement)
        self.assertNotEqual(
            original, replacement,
            "two properties the sheet could not describe are still two properties",
        )

    def test_the_ref_survives_cosmetic_anchor_edits(self):
        # Same property, anchor re-rendered: case, spacing, trailing comma.
        base = mint_property_ref(client_id="c1", row_anchor="100 Example Rd, Springfield")
        for variant in ("100 example rd, springfield", "100 Example Rd,  Springfield", "  100 Example Rd Springfield "):
            with self.subTest(variant=variant):
                self.assertEqual(
                    base, mint_property_ref(client_id="c1", row_anchor=variant),
                    "a display-string edit is not a different property",
                )

    def test_different_properties_get_different_refs(self):
        self.assertNotEqual(
            mint_property_ref(client_id="c1", row_anchor="100 Example Rd, Springfield"),
            mint_property_ref(client_id="c1", row_anchor="250 Other Way, Springfield"),
        )

    def test_the_ref_is_scoped_to_the_campaign(self):
        # The same address in two campaigns is two rows with two lives.
        self.assertNotEqual(
            mint_property_ref(client_id="c1", row_anchor="100 Example Rd, Springfield"),
            mint_property_ref(client_id="c2", row_anchor="100 Example Rd, Springfield"),
        )

    def test_the_ref_is_opaque_and_carries_no_customer_text(self):
        ref = mint_property_ref(client_id="c1", row_anchor="100 Example Rd, Springfield")
        self.assertNotIn("Example", ref)
        self.assertNotIn("Springfield", ref)

    def test_normalize_anchor_matches_the_ai_meta_normalizer(self):
        from email_automation import ai_processing
        for anchor in ("100 Example Rd, Springfield", "  A ,  B ", ""):
            with self.subTest(anchor=anchor):
                self.assertEqual(
                    normalize_anchor(anchor),
                    ai_processing._normalize_ai_meta_anchor(anchor),
                    "AI_META already normalizes anchors this exact way; one normalizer, not two",
                )


class TheEventKeyReadsTheRef(unittest.TestCase):
    def test_the_replacements_tour_is_not_suppressed_when_neither_row_is_readable(self):
        # THE DEFECT. Same thread, both rows unreadable, different properties.
        for event_type in PROPERTY_SCOPED:
            with self.subTest(event_type=event_type):
                original = build_event_key(
                    event_type, {}, thread_id="t1", row_anchor="Row data incomplete",
                    property_ref=mint_property_ref(
                        client_id="c1", thread_id="t1",
                        row_anchor="Row data incomplete", row_number=12),
                )
                replacement = build_event_key(
                    event_type, {}, thread_id="t1", row_anchor="Row data incomplete",
                    property_ref=mint_property_ref(
                        client_id="c1", thread_id="t1",
                        row_anchor="Row data incomplete", row_number=13),
                )
                self.assertNotEqual(
                    original, replacement,
                    f"a {event_type} on the replacement property must not be suppressed by the "
                    "original's already-handled entry just because the sheet could not name either",
                )

    def test_the_same_property_still_dedupes_through_the_ref(self):
        ref = mint_property_ref(client_id="c1", row_anchor="100 Example Rd, Springfield")
        for event_type in PROPERTY_SCOPED:
            with self.subTest(event_type=event_type):
                self.assertEqual(
                    build_event_key(event_type, {}, thread_id="t1", row_anchor="anything", property_ref=ref),
                    build_event_key(event_type, {}, thread_id="t1", row_anchor="anything else", property_ref=ref),
                    "the ref is the identity; the display string must not be able to split it",
                )

    def test_a_sentinel_anchor_with_no_ref_falls_back_to_the_thread(self):
        # Without a ref there is nothing better than the thread -- but the
        # sentinel must not be mistaken for a property name.
        key = build_event_key("tour_requested", {}, thread_id="t1", row_anchor="Row data incomplete")
        self.assertNotIn("Row data incomplete", key)
        self.assertIn("t1", key)

    def test_the_key_still_names_its_event_type(self):
        ref = mint_property_ref(client_id="c1", row_anchor="100 Example Rd, Springfield")
        for event_type in PROPERTY_SCOPED:
            with self.subTest(event_type=event_type):
                self.assertIn(event_type, build_event_key(event_type, {}, thread_id="t1", property_ref=ref))

    def test_the_already_correct_kinds_are_unchanged_by_a_ref(self):
        ref = mint_property_ref(client_id="c1", row_anchor="100 Example Rd, Springfield")
        self.assertEqual(
            build_event_key("new_property", {"address": "A", "city": "B", "email": "c@d.invalid"},
                            thread_id="t1", row_anchor="X", property_ref=ref),
            "new_property:A:B:c@d.invalid",
        )
        self.assertEqual(
            build_event_key("wrong_contact", {"suggestedEmail": "x@y.invalid"},
                            thread_id="t1", row_anchor="X", property_ref=ref),
            "wrong_contact:x@y.invalid",
        )


if __name__ == "__main__":
    unittest.main()
