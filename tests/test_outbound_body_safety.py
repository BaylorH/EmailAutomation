import unittest

from email_automation import outbound_safety


class OutboundBodyApprovalScopingTests(unittest.TestCase):
    """CodeRabbit PR#15 (outbound_safety.py:133): the ``approved ... by``
    alternative of APPROVAL_BUDGET_RE must only fire on FABRICATED (completed)
    approval assertions, not routine conditional/future broker phrasing.
    Blocking the latter would refuse legitimate broker email."""

    def test_completed_approved_by_assertion_is_blocked(self):
        # Fabricated: asserts approval already granted.
        for body in (
            "The lease was approved by our board last week.",
            "Good news -- the budget has been approved by our board.",
            "The deal is already approved by their lender.",
        ):
            with self.subTest(body=body):
                self.assertTrue(
                    outbound_safety.contains_fabricated_approval_or_budget(body),
                    msg="completed 'approved by' assertion must be blocked",
                )
                self.assertFalse(
                    outbound_safety.validate_outbound_body(body).is_safe
                )

    def test_conditional_future_approval_by_is_not_blocked(self):
        # Conditional / future: legitimate routine broker phrasing.
        for body in (
            "This would need to be approved by our board before we proceed.",
            "The terms still need to be approved by their board.",
            "Pricing will have to be approved by the landlord first.",
        ):
            with self.subTest(body=body):
                self.assertFalse(
                    outbound_safety.contains_fabricated_approval_or_budget(body),
                    msg=(
                        "FALSE POSITIVE: conditional 'to be approved by' "
                        "phrasing must NOT be treated as a fabricated approval."
                    ),
                )
                self.assertTrue(
                    outbound_safety.validate_outbound_body(body).is_safe
                )


class OutboundBodyBracketAcronymTests(unittest.TestCase):
    """CodeRabbit PR#15 (outbound_safety.py:153): the ``isupper()`` shortcut
    over-flagged legitimate bracketed acronyms ([TBD], [ASAP], [FYI], [N/A]) as
    unresolved placeholders. Real merge-field placeholders must stay blocked."""

    def test_bracketed_acronyms_are_not_placeholders(self):
        for token in ("[TBD]", "[ASAP]", "[FYI]", "[N/A]", "[EOD]", "[ETA]"):
            with self.subTest(token=token):
                body = f"Hi Connor,\n\nThe rate is {token} pending final numbers."
                self.assertEqual(
                    [],
                    outbound_safety.find_unresolved_placeholders(body),
                    msg=f"{token} is a legitimate acronym, not a placeholder",
                )
                self.assertTrue(outbound_safety.validate_outbound_body(body).is_safe)

    def test_real_bracket_placeholders_stay_blocked(self):
        for token in ("[NAME]", "[COMPANY]", "[ADDRESS]", "[FIRST_NAME]", "[BROKER]"):
            with self.subTest(token=token):
                body = f"Hi {token},\n\nCould you confirm the SF available?"
                self.assertIn(
                    token,
                    outbound_safety.find_unresolved_placeholders(body),
                    msg=f"{token} is a real merge field and must be blocked",
                )
                self.assertFalse(outbound_safety.validate_outbound_body(body).is_safe)

    def test_non_bracket_allcaps_merge_fields_stay_blocked(self):
        # Non-bracket merge syntaxes never appear in prose, so a bare all-caps
        # token like %FIELD% must remain a blocked placeholder.
        for body in ("Offer %FIELD% at the quoted rate.", "Suite <<FIELD>> is open."):
            with self.subTest(body=body):
                self.assertNotEqual(
                    [],
                    outbound_safety.find_unresolved_placeholders(body),
                )
                self.assertFalse(outbound_safety.validate_outbound_body(body).is_safe)


class OutboundBodyPercentEscalationTests(unittest.TestCase):
    """CodeRabbit PR#15 (outbound_safety.py:22): PERCENT_PLACEHOLDER_RE allowed
    spaces in the captured span, so rent-escalation prose spanning two percent
    signs (``3% annual property increase and a 5% fee``) falsely triggered."""

    def test_rent_escalation_prose_is_not_a_placeholder(self):
        for body in (
            "We're proposing a 3% annual property increase and a 5% management fee.",
            "Rent escalates 3% each year with a 10% renewal cap.",
            "The tenant pays 2% of gross plus a 5% contact reserve.",
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    [],
                    outbound_safety.find_unresolved_placeholders(body),
                    msg="rent-escalation prose must not read as a percent placeholder",
                )
                self.assertTrue(outbound_safety.validate_outbound_body(body).is_safe)

    def test_real_percent_merge_fields_stay_blocked(self):
        for body in ("Offer %FIELD% now.", "Hi %FirstName%,", "Rate is %First_Name%."):
            with self.subTest(body=body):
                self.assertNotEqual(
                    [],
                    outbound_safety.find_unresolved_placeholders(body),
                    msg="a real %merge% field must still be blocked",
                )
                self.assertFalse(outbound_safety.validate_outbound_body(body).is_safe)


class OutboundBodySafetyTests(unittest.TestCase):
    def test_name_placeholder_blocks_outbound_body(self):
        result = outbound_safety.validate_outbound_body(
            "Hi [NAME],\n\nCould you confirm the SF available?"
        )

        self.assertFalse(result.is_safe)
        self.assertIn("[NAME]", result.placeholders)

    def test_real_broker_name_passes_outbound_body(self):
        result = outbound_safety.validate_outbound_body(
            "Hi Connor,\n\nCould you confirm the SF available?"
        )

        self.assertTrue(result.is_safe)
        self.assertEqual([], result.placeholders)

    def test_tour_scheduling_language_blocks_normal_outreach(self):
        result = outbound_safety.validate_outbound_body(
            "Hi Connor,\n\nBefore we proceed with tour scheduling and/or LOIs, "
            "can you please confirm the following?"
        )

        self.assertFalse(result.is_safe)
        self.assertIn("tour", result.reason.lower())

    def test_karsen_mattress_firm_launch_copy_is_blocked(self):
        result = outbound_safety.validate_outbound_body(
            "Hi [NAME],\n\n"
            "I’m representing a tenant (national corporation, retail distributor "
            "name to be disclosed once a tour is being scheduled) that is looking "
            "to lease industrial space in the area.\n\n"
            "Before we proceed with tour scheduling and/or LOIs, could you please "
            "verify the SF available, lease rate, clear height, docks, and drive-ins?"
        )

        self.assertFalse(result.is_safe)
        self.assertIn("[NAME]", result.placeholders)

    def test_reviewed_tour_invites_can_use_tour_language(self):
        result = outbound_safety.validate_outbound_body(
            "Hi Connor,\n\nWe are confirming the tour for Tuesday at 10:00 AM.",
            allow_scheduling_language=True,
        )

        self.assertTrue(result.is_safe)

    def test_broker_named_lois_does_not_trigger_loi_guard(self):
        result = outbound_safety.validate_outbound_body(
            "Hi Lois,\n\nCould you please confirm the asking rate and clear height?"
        )

        self.assertTrue(result.is_safe)


if __name__ == "__main__":
    unittest.main()
