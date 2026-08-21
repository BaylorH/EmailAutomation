"""A malformed address must not take down the whole reply-processing turn.

`_first_name` guards the empty string, but it does that BEFORE stripping the
domain. An input that is non-empty yet whose local part reduces to nothing --
"@somebrokerage.com", "@", "._@x.com" -- reached `value.split()[0]` with an
empty list and raised IndexError.

That mattered because the inputs are the model's raw `contactName` and `email`
event fields, which are never validated as addresses. So an ordinary model
emission, not a corrupt mailbox, could crash processing for that message.

The correct answer is the empty string: every caller already treats a missing
first name as "no name" and falls back to a plain greeting.
"""
import unittest

from email_automation.notification_payloads import (
    _first_name,
    build_new_property_suggested_email,
    build_wrong_contact_suggested_email,
)

# Non-empty inputs whose local part reduces to nothing.
DEGENERATE = ["@somebrokerage.com", "@", "._@x.com", "  @  ", "@@", ".@.", "_@_"]


class FirstNameDegenerateLocalPartTests(unittest.TestCase):
    def test_a_local_part_that_reduces_to_nothing_returns_empty_not_indexerror(self):
        for value in DEGENERATE:
            with self.subTest(value=value):
                self.assertEqual(_first_name(value), "")

    def test_ordinary_names_and_addresses_are_unchanged(self):
        self.assertEqual(_first_name("Dana Whitfield"), "Dana")
        self.assertEqual(_first_name("dana.whitfield@example.com"), "Dana")
        self.assertEqual(_first_name("marcus_ling@example.com"), "Marcus")
        self.assertEqual(_first_name(""), "")
        self.assertEqual(_first_name(None), "")

    def test_the_payload_builders_survive_a_degenerate_address(self):
        """The crash was reached through these, so they are the real regression."""
        for value in DEGENERATE:
            with self.subTest(value=value):
                wrong = build_wrong_contact_suggested_email(
                    original_contact=value,
                    suggested_contact=value,
                    suggested_email=value,
                    row_anchor="1200 Test Loop",
                    referrer_name=None,
                )
                self.assertIn("Hi,", wrong["body"])

                new_prop = build_new_property_suggested_email(
                    address="1200 Test Loop",
                    city="North Las Vegas",
                    to_email=value,
                    contact_name=value,
                    referrer_name=None,
                    client_id="c1",
                )
                self.assertIsInstance(new_prop, dict)


if __name__ == "__main__":
    unittest.main()
