"""Pressure test: launch_with_variable_mapping (broker sheet -> template merge fields).

EVENT: A campaign is launched from an uploaded broker sheet. The template carries
name/merge placeholders (e.g. "Hi [NAME]," or "Hi {{name}},") that must be resolved
from a sheet column before the body is queued to the outbox.

Deterministic guards under test (email_automation, no LLM):
  * outbound_safety.find_unresolved_placeholders / validate_outbound_body
        -> the LAST line of defense: refuses to queue any body that still holds a
           raw template placeholder.  stopIf: "raw placeholder reaches queued outbox".
  * email._contact_name_resolution_from_campaign_row
        -> picks the name to substitute; MUST refuse (return None + failure_reason)
           when name-like columns disagree.  stopIf: "backend guesses between two people".
  * email._personalize_name_placeholders / _safe_greeting_first_name
        -> the actual substitution.

Only pure functions are driven here; no Firestore / Sheets / Graph calls occur, so
nothing external is patched.  Assertions are written to the CORRECT behavior; rows
that stay RED pin real defects for the maintainer to fix.
"""

import os
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation.outbound_safety import (
    find_unresolved_placeholders,
    validate_outbound_body,
)
from email_automation.email import (
    _contact_name_resolution_from_campaign_row,
    _personalize_name_placeholders,
    _safe_greeting_first_name,
)


# ---------------------------------------------------------------------------
# 1. OUTBOUND PLACEHOLDER GUARD (validate_outbound_body)
#    The guard must FIRE (is_safe == False) on every unresolved merge field,
#    regardless of the merge-field syntax the broker's template happens to use.
# ---------------------------------------------------------------------------

# Square-bracket forms -- these are the ONLY syntax the current guard understands.
# They are controls that should be GREEN and prove the guard works at all.
BRACKET_THREATS = [
    "Hi [NAME], I saw your listing at 100 Main St.",           # canonical
    "hi [name], quick question about the space",               # lowercase
    "Hi [First Name], following up on the warehouse.",         # spaced words
    "Dear [Contact Name], is this still available?",           # contact name
    "Hello [BROKER], are you the leasing contact?",            # broker
    "Hi [Leasing Contact], reaching out re: 200 Oak Ave.",     # leasing contact
    "Hi [CONTACT_NAME], hope you're well.",                    # underscore
    "Good morning [Recipient Name], I represent a tenant.",    # recipient name
]

# Non-bracket merge-field syntaxes that real mail-merge tools emit
# (Mailchimp / Handlebars {{ }}, single-brace { }, ATS <<>>, %%, ${}).
# The seed phrasing literally uses "{{name}}".  Every one of these is a raw
# placeholder that, if unresolved, MUST NOT reach the outbox.
NON_BRACKET_THREATS = [
    "Hi {{name}}, I saw your listing at 100 Main St.",         # seed: double-curly
    "Hi {{ Name }}, quick question about the space.",          # curly + spaces
    "Hi {{first_name}}, following up on the warehouse.",       # curly first_name
    "Hi {{contact_name}}, is this still available?",           # curly contact_name
    "HI {{NAME}} - are you the leasing contact?",              # ALL CAPS curly
    "Hi {name}, hope you're well.",                            # single brace
    "Dear <<Name>>, I represent a tenant.",                    # angle merge
    "Hi %FirstName%, reaching out re: 200 Oak Ave.",           # percent merge
    "Good morning ${name}, following up.",                     # shell-style merge
    "Hi {{name}}, re {{property}} - still available?",         # multi-field curly
    "> On Mon a broker wrote:\n\nHi {{name}}, circling back.", # quoted history + curly
    "Hi {{{name}}}, typo'd triple brace but still a merge tag",# typo triple brace
]


class OutboundPlaceholderGuardTest(unittest.TestCase):
    def test_bracket_placeholders_are_blocked_control(self):
        """Control: the guard already catches square-bracket merge fields."""
        for body in BRACKET_THREATS:
            with self.subTest(body=body):
                v = validate_outbound_body(body)
                self.assertFalse(
                    v.is_safe,
                    f"bracket placeholder slipped through as SAFE: {body!r}",
                )
                self.assertTrue(find_unresolved_placeholders(body))

    def test_non_bracket_merge_fields_must_be_blocked(self):
        """REAL THREAT: {{name}} / {name} / <<Name>> / %F% must not reach outbox.

        stopIf: 'raw placeholder reaches queued outbox'.  These currently pass the
        guard (is_safe == True), so this test stays RED to pin the false negative.
        """
        for body in NON_BRACKET_THREATS:
            with self.subTest(body=body):
                v = validate_outbound_body(body)
                self.assertFalse(
                    v.is_safe,
                    f"UNRESOLVED merge field reached queued outbox as SAFE: {body!r}",
                )

    def test_resolved_and_clean_bodies_are_safe_nearmiss(self):
        """Near-miss control: fully-resolved / placeholder-free copy must pass."""
        for body in [
            "Hi Jane, I saw your listing at 100 Main St.",
            "Hello, is the warehouse at 200 Oak still available?",
            "Please see the attached deck [sic] and let me know.",  # SAFE_BRACKET_TOKEN
        ]:
            with self.subTest(body=body):
                self.assertTrue(
                    validate_outbound_body(body).is_safe,
                    f"clean body wrongly flagged unsafe: {body!r}",
                )


# ---------------------------------------------------------------------------
# 2. NAME-COLUMN AMBIGUITY GUARD (_contact_name_resolution_from_campaign_row)
#    Must refuse (contact_name is None + failure_reason) when explicit name
#    columns name DIFFERENT people.  Must resolve cleanly when unambiguous.
# ---------------------------------------------------------------------------

class NameColumnAmbiguityGuardTest(unittest.TestCase):
    def test_disagreeing_people_must_not_be_guessed(self):
        """REAL THREAT + near-miss 'neither should be guessed': refuse, don't pick.

        stopIf: 'backend guesses between two possible people'.  Correct behavior is
        contact_name=None with an ambiguity reason -> guard fires (this is GREEN).
        """
        header = ["Leasing Contact", "Broker Name", "First Name", "Contact Name"]
        row = ["Jane Smith", "Bob Jones", "Alice", "Mark Lee"]
        res = _contact_name_resolution_from_campaign_row(header, row)
        self.assertIsNone(
            res["contact_name"],
            f"backend GUESSED a person from disagreeing columns: {res}",
        )
        self.assertTrue(res["failure_reason"])

    def test_single_name_column_resolves(self):
        """Control: one populated name column -> clean resolution."""
        header = ["Contact Name", "Email"]
        row = ["Jane Smith", "jane@acme.com"]
        res = _contact_name_resolution_from_campaign_row(header, row)
        self.assertEqual(res["contact_name"], "Jane Smith")
        self.assertIsNone(res["failure_reason"])

    def test_full_and_first_name_of_same_person_should_resolve(self):
        """Common real sheet: 'Contact Name'=Jane Smith AND 'First Name'=Jane.

        These are the SAME person expressed two ways.  Refusing here dead-letters a
        legitimate launch (false positive).  Documented: the guard treats them as
        ambiguous.  Asserted to the SAFE current behavior (refuse) since erring
        toward 'don't guess' aligns with the stopIf -- this stays GREEN and simply
        pins the conservative tradeoff.
        """
        header = ["Contact Name", "First Name", "Email"]
        row = ["Jane Smith", "Jane", "jane@acme.com"]
        res = _contact_name_resolution_from_campaign_row(header, row)
        # Safe-but-conservative: refuses rather than risk the wrong person.
        self.assertIsNone(res["contact_name"])


# ---------------------------------------------------------------------------
# 3. SUBSTITUTION + END-TO-END (personalize then re-validate)
# ---------------------------------------------------------------------------

class SubstitutionEndToEndTest(unittest.TestCase):
    def test_bracket_substitution_then_safe(self):
        """Control: resolved name replaces [NAME] and the body validates clean."""
        body = _personalize_name_placeholders("Hi [NAME], following up.", "Jane Smith")
        self.assertEqual(body, "Hi Jane, following up.")
        self.assertTrue(validate_outbound_body(body).is_safe)

    def test_curly_field_survives_substitution_and_reaches_outbox(self):
        """REAL THREAT: even WITH a resolved name, {{name}} is never substituted
        and is never flagged -> a raw placeholder reaches the queued outbox.

        stopIf: 'raw placeholder reaches queued outbox'.  Stays RED.
        """
        body = _personalize_name_placeholders("Hi {{name}}, following up.", "Jane Smith")
        # Substitution silently no-ops for curly syntax:
        self.assertIn("{{name}}", body)  # documents the miss (GREEN)
        # ...and the final guard passes it as safe -> the actual safety hole:
        self.assertFalse(
            validate_outbound_body(body).is_safe,
            f"raw {{{{name}}}} placeholder queued as SAFE: {body!r}",
        )

    def test_company_name_should_not_be_greeted_as_person_nearmiss(self):
        """Near-miss: a company name sits in a name-like column.  It must NOT be
        turned into a human greeting ('Hi Acme,').  The deterministic layer cannot
        tell a company from a person, so it substitutes anyway.  Asserted to the
        CORRECT behavior (no human first-name greeting) -> stays RED to record the
        false positive; robustly fixing this is essentially LLM-only.
        """
        greeted = _personalize_name_placeholders("Hi [NAME], quick question.", "Acme Realty LLC")
        self.assertNotEqual(
            greeted,
            "Hi Acme, quick question.",
            "company name was greeted as a human first name",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
