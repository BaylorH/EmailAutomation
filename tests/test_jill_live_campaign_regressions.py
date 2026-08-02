import os
import unittest


os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "service-account.json",
    ),
)

from email_automation import ai_processing, processing


def _conversation(body):
    return [{"direction": "inbound", "content": body}]


class JillLiveCampaignRegressionTests(unittest.TestCase):
    def test_explicit_opex_wins_over_earlier_nnn_rent_basis(self):
        examples = {
            (
                "We are marketing the Units at $14.00 psf NNN, "
                "OPEX approximately $4.00 psf."
            ): "4.00",
            (
                "The lease price is $15.50 psf nnn and estimated "
                "Taxes & CAM are $3.00 psf."
            ): "3.00",
            (
                "The NNN lease rate is $14.00 per SF; OPEX is $4.00 per SF."
            ): "4.00",
            (
                "The asking rental rate for the space is $14.00/SF NNN, and "
                "$4.00/SF in operating expenses."
            ): "4.00",
        }

        for text, expected in examples.items():
            with self.subTest(text=text):
                self.assertEqual(
                    expected,
                    ai_processing._extract_ops_ex_sf_from_text(text),
                )

    def test_rampable_dock_is_not_a_terminal_drive_in_mismatch(self):
        proposal = {
            "updates": [],
            "events": [
                {"type": "property_unavailable", "reason": "requirements_mismatch"}
            ],
            "response_email": "We'll cross this one off.",
        }
        conversation = _conversation(
            "No drive in door. 1 loading dock. The loading dock can be ramped "
            "for drive in. The unit is 7753 sf."
        )

        result = ai_processing._augment_events_with_deterministic_signals(
            proposal,
            conversation,
            target_anchor="102 Iron Mountain Rd, Mine Hill",
        )

        event_types = [event.get("type") for event in result["events"]]
        self.assertNotIn("property_unavailable", event_types)
        self.assertIn("needs_user_input", event_types)
        self.assertIsNone(result["response_email"])

    def test_rampable_dock_does_not_hide_separate_office_mismatch(self):
        self.assertTrue(
            ai_processing._looks_like_requirements_mismatch_nonviable(
                "The space is too office-heavy for the client. The dock could be "
                "ramped for drive-in access, but there is almost no warehouse."
            )
        )

    def test_access_remediation_variants_do_not_terminalize_the_property(self):
        examples = (
            "No drive-in door. The loading dock is rampable for drive-in access.",
            "There is no grade-level door today, but the owner will convert the "
            "loading dock to grade-level access.",
            "This is not a fit as-is because there is no drive-in, but the dock "
            "can be ramped for drive-in access.",
        )

        for body in examples:
            with self.subTest(body=body):
                proposal = {
                    "updates": [],
                    "events": [
                        {
                            "type": "property_unavailable",
                            "reason": "requirements_mismatch",
                        }
                    ],
                    "response_email": "We'll cross this one off.",
                }
                result = ai_processing._augment_events_with_deterministic_signals(
                    proposal,
                    _conversation(body),
                    target_anchor="102 Iron Mountain Rd, Mine Hill",
                )

                event_types = [event.get("type") for event in result["events"]]
                self.assertNotIn("property_unavailable", event_types)
                self.assertIn("needs_user_input", event_types)
                self.assertIsNone(result["response_email"])

    def test_negated_access_remediation_remains_terminal(self):
        examples = (
            "No drive-in door, and the loading dock could not be ramped.",
            "There is no grade-level access and the dock is not rampable.",
        )

        for body in examples:
            with self.subTest(body=body):
                self.assertFalse(ai_processing._looks_like_access_remediation(body))
                self.assertTrue(
                    ai_processing._looks_like_requirements_mismatch_nonviable(body)
                )

    def test_matching_route_address_brochure_is_not_treated_as_competing(self):
        proposal = {
            "updates": [{"column": "Total SF", "value": "7500"}],
            "events": [],
            "response_email": None,
        }
        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("We have the current building available; brochure attached."),
            "3344 S Carolina 51, Fort Mill",
            [{
                "name": "3344 SC-51 brochure.pdf",
                "text": "3344 S Carolina 51, Fort Mill - 7,500 SF",
            }],
        )

        self.assertEqual([{"column": "Total SF", "value": "7500"}], result["updates"])

    def test_target_brochure_does_not_bless_value_from_competing_brochure(self):
        proposal = {
            "updates": [
                {"column": "Ceiling Ht", "value": "32", "confidence": 0.96},
                {"column": "Total SF", "value": "20000", "confidence": 0.98},
            ],
            "events": [],
            "response_email": None,
        }
        target_brochure = {
            "name": "100 Main St brochure.pdf",
            "text": "100 Main St - 20,000 SF industrial building.",
        }
        competing_brochure = {
            "name": "200 Oak Ave brochure.pdf",
            "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("We have two options; the brochures are attached."),
            "100 Main St, Phoenix",
            [target_brochure, competing_brochure],
        )

        self.assertEqual(
            [{"column": "Total SF", "value": "20000", "confidence": 0.98}],
            result["updates"],
        )

    def test_named_alternate_in_fresh_text_cannot_be_target_evidence(self):
        proposal = {
            "updates": [{"column": "Ceiling Ht", "value": "32"}],
            "events": [],
            "response_email": "Thanks.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation(
                "100 Main St remains available. Another option, Oak Commerce "
                "Center, has 32 feet clear. Brochures attached."
            ),
            "100 Main St, Phoenix",
            [
                {
                    "name": "100 Main St brochure.pdf",
                    "text": "100 Main St - 20,000 SF.",
                },
                {
                    "name": "200 Oak Ave brochure.pdf",
                    "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([], result["updates"])
        self.assertTrue(any(
            event.get("type") == "needs_user_input"
            and event.get("reason") == "multi_property_attachment"
            for event in result["events"]
        ))
        self.assertIsNone(result["response_email"])

    def test_named_addressless_alternate_brochure_is_competing(self):
        proposal = {
            "updates": [{"column": "Ceiling Ht", "value": "32"}],
            "events": [],
            "response_email": "Thanks.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation(
                "We also have another option, Oak Commerce Center; brochures "
                "attached."
            ),
            "100 Main St, Phoenix",
            [
                {
                    "name": "100 Main St brochure.pdf",
                    "text": "100 Main St - 20,000 SF.",
                },
                {
                    "name": "Oak Commerce Center brochure.pdf",
                    "text": "Oak Commerce Center - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([], result["updates"])
        self.assertTrue(any(
            event.get("type") == "needs_user_input"
            and event.get("reason") == "multi_property_attachment"
            for event in result["events"]
        ))
        self.assertIsNone(result["response_email"])

    def test_addressless_target_attachment_survives_parseable_competitor(self):
        expected_update = {"column": "Ceiling Ht", "value": "28"}
        proposal = {
            "updates": [expected_update],
            "events": [],
            "response_email": None,
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation(
                "For 100 Main St, use current-property specs.pdf: it has the target "
                "property specifications. 200 Oak Ave is the other option."
            ),
            "100 Main St, Phoenix",
            [
                {
                    "name": "current-property specs.pdf",
                    "text": "Ceiling Ht: 28 feet clear.",
                },
                {
                    "name": "200 Oak Ave brochure.pdf",
                    "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([expected_update], result["updates"])

    def test_postfixed_alternate_cue_cannot_supply_target_evidence(self):
        proposal = {
            "updates": [{"column": "Ceiling Ht", "value": "32"}],
            "events": [],
            "response_email": "Thanks.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation(
                "100 Main St remains available. Oak Commerce Center has 32 feet "
                "clear and is another option."
            ),
            "100 Main St, Phoenix",
            [
                {
                    "name": "100 Main St brochure.pdf",
                    "text": "100 Main St - 20,000 SF.",
                },
                {
                    "name": "200 Oak Ave brochure.pdf",
                    "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([], result["updates"])
        self.assertTrue(any(
            event.get("type") == "needs_user_input"
            and event.get("reason") == "multi_property_attachment"
            for event in result["events"]
        ))
        self.assertIsNone(result["response_email"])

    def test_separate_named_property_clause_cannot_supply_target_evidence(self):
        proposal = {
            "updates": [{"column": "Ceiling Ht", "value": "32"}],
            "events": [],
            "response_email": "Thanks.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation(
                "100 Main St remains available. Separately, Oak Commerce Center "
                "has 32 feet clear."
            ),
            "100 Main St, Phoenix",
            [
                {
                    "name": "100 Main St brochure.pdf",
                    "text": "100 Main St - 20,000 SF.",
                },
                {
                    "name": "200 Oak Ave brochure.pdf",
                    "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([], result["updates"])
        self.assertTrue(any(
            event.get("type") == "needs_user_input"
            and event.get("reason") == "multi_property_attachment"
            for event in result["events"]
        ))
        self.assertIsNone(result["response_email"])

    def test_named_property_in_target_address_pdf_is_mixed_evidence(self):
        proposal = {
            "updates": [{"column": "Ceiling Ht", "value": "32"}],
            "events": [],
            "response_email": "Thanks.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("The brochure is attached."),
            "100 Main St, Phoenix",
            [{
                "name": "mixed brochure.pdf",
                "text": (
                    "100 Main St - 20,000 SF. Oak Commerce Center - Ceiling Ht: "
                    "32 feet clear."
                ),
            }],
        )

        self.assertEqual([], result["updates"])
        self.assertTrue(any(
            event.get("type") == "needs_user_input"
            and event.get("reason") == "multi_property_attachment"
            for event in result["events"]
        ))
        self.assertIsNone(result["response_email"])

    def test_named_addressless_second_choice_brochure_is_competing(self):
        proposal = {
            "updates": [{"column": "Ceiling Ht", "value": "32"}],
            "events": [],
            "response_email": "Thanks.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("Oak Commerce Center is a second choice."),
            "100 Main St, Phoenix",
            [
                {
                    "name": "100 Main St brochure.pdf",
                    "text": "100 Main St - 20,000 SF.",
                },
                {
                    "name": "Oak Commerce Center brochure.pdf",
                    "text": "Oak Commerce Center - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([], result["updates"])
        self.assertTrue(any(
            event.get("type") == "needs_user_input"
            and event.get("reason") == "multi_property_attachment"
            for event in result["events"]
        ))
        self.assertIsNone(result["response_email"])

    def test_versioned_named_brochure_with_postfixed_alternate_cue_is_competing(self):
        proposal = {
            "updates": [{"column": "Ceiling Ht", "value": "32"}],
            "events": [],
            "response_email": "Thanks.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation(
                "oak commerce center has 32 feet clear and is an alternative "
                "property."
            ),
            "100 Main St, Phoenix",
            [
                {
                    "name": "100 Main St brochure.pdf",
                    "text": "100 Main St - 20,000 SF.",
                },
                {
                    "name": "Oak Commerce Center brochure v2.pdf",
                    "text": "Oak Commerce Center - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([], result["updates"])
        self.assertTrue(any(
            event.get("type") == "needs_user_input"
            and event.get("reason") == "multi_property_attachment"
            for event in result["events"]
        ))
        self.assertIsNone(result["response_email"])

    def test_addressless_attachment_is_untrusted_with_multiple_attachments(self):
        introductions = (
            "As a fallback, Oak Commerce Center is attached.",
            "The backup is Oak Commerce Center.",
            "For comparison, Oak Commerce Center is attached.",
            "Plan B is Oak Commerce Center.",
            "Instead, consider Oak Commerce Center.",
        )

        for introduction in introductions:
            with self.subTest(introduction=introduction):
                proposal = {
                    "updates": [{"column": "Ceiling Ht", "value": "32"}],
                    "events": [{
                        "type": "needs_user_input",
                        "reason": "multi_property_attachment",
                        "question": "Which property is this for?",
                    }],
                    "response_email": "Thanks.",
                }

                result = ai_processing._suppress_competing_attachment_updates(
                    proposal,
                    _conversation(introduction),
                    "100 Main St, Phoenix",
                    [
                        {
                            "name": "100 Main St brochure.pdf",
                            "text": "100 Main St - 20,000 SF.",
                        },
                        {
                            "name": "Oak Commerce Center brochure.pdf",
                            "text": "Ceiling Ht: 32 feet clear.",
                        },
                    ],
                )

                self.assertEqual([], result["updates"])
                self.assertIsNone(result["response_email"])

    def test_generic_fresh_text_cannot_prove_competing_attachment_value(self):
        messages = (
            "This property has 28 feet clear. As a fallback.",
            "In contrast, Oak Commerce Center has 32 feet clear.",
        )

        for message in messages:
            with self.subTest(message=message):
                proposal = {
                    "updates": [{"column": "Ceiling Ht", "value": "32"}],
                    "events": [{
                        "type": "needs_user_input",
                        "reason": "multi_property_attachment",
                        "question": "Which property is this for?",
                    }],
                    "response_email": "Thanks.",
                }

                result = ai_processing._suppress_competing_attachment_updates(
                    proposal,
                    _conversation(message),
                    "100 Main St, Phoenix",
                    [
                        {
                            "name": "100 Main St brochure.pdf",
                            "text": "100 Main St - 20,000 SF.",
                        },
                        {
                            "name": "200 Oak Ave brochure.pdf",
                            "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
                        },
                    ],
                )

                self.assertEqual([], result["updates"])
                self.assertIsNone(result["response_email"])

    def test_unbound_identity_clause_makes_single_target_address_pdf_mixed(self):
        alternate_clauses = (
            "oak commerce center - Ceiling Ht: 32 feet clear.",
            "Westgate Logistics Hub - Ceiling Ht: 32 feet clear.",
        )

        for alternate_clause in alternate_clauses:
            with self.subTest(alternate_clause=alternate_clause):
                proposal = {
                    "updates": [{"column": "Ceiling Ht", "value": "32"}],
                    "events": [],
                    "response_email": "Thanks.",
                }

                result = ai_processing._suppress_competing_attachment_updates(
                    proposal,
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": (
                            "100 Main St - 20,000 SF. "
                            f"{alternate_clause}"
                        ),
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_two_word_unbound_identity_clause_makes_target_address_pdf_mixed(self):
        alternate_clauses = (
            "westgate hub - Ceiling Ht: 32 feet clear.",
            "oak center — Clear Height = 32 ft.",
            "river park - Ceiling Clearance: 32 feet.",
            "Oak Center - 32 feet clear.",
            "Westgate: 32 ft clear.",
            "Oak Center | 32 feet clear.",
            "Oak Center • 32 feet clear.",
            "Oak Center / 32 feet clear.",
            "Oak Center, 32 feet clear.",
            "Oak Center  32 feet clear.",
            "Oak Center\n32 feet clear.",
            "Oak Center\nCeiling Ht: 32 feet clear.",
        )

        for alternate_clause in alternate_clauses:
            with self.subTest(alternate_clause=alternate_clause):
                proposal = {
                    "updates": [{"column": "Ceiling Ht", "value": "32"}],
                    "events": [],
                    "response_email": "Thanks.",
                }

                result = ai_processing._suppress_competing_attachment_updates(
                    proposal,
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": (
                            "100 Main St - 20,000 SF. "
                            f"{alternate_clause}"
                        ),
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_two_word_target_spec_label_remains_supported(self):
        target_specs = (
            "clear height - 28 feet clear.",
            "ceiling height - 28 feet clear.",
            "ceiling ht: 28 feet clear.",
            "ceiling clearance = 28 ft clear.",
            "clear height\n28 feet clear.",
            "Property Highlights\n28 feet clear.",
        )

        for target_spec in target_specs:
            with self.subTest(target_spec=target_spec):
                expected_update = {"column": "Ceiling Ht", "value": "28"}
                proposal = {
                    "updates": [expected_update],
                    "events": [],
                    "response_email": None,
                }

                result = ai_processing._suppress_competing_attachment_updates(
                    proposal,
                    _conversation("The target brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "100 Main St brochure.pdf",
                        "text": f"100 Main St - 20,000 SF. {target_spec}",
                    }],
                )

                self.assertEqual([expected_update], result["updates"])

    def test_short_unbound_identity_suppresses_every_mapped_fact_type(self):
        competing_facts = (
            ("Docks", "6", "Oak Center - 6 dock doors."),
            ("Docks", "6", "Oak Center | Docks: 6."),
            ("Docks", "6", "Oak Center | Dock Positions: 6."),
            ("Docks", "6", "Oak Center) Docks: 6."),
            ("Docks", "6", "Oak Center] Docks: 6."),
            ("Docks", "6", "Oak Center} Docks: 6."),
            ("Docks", "6", "Oak Center ~ Docks: 6."),
            ("Drive Ins", "2", "Oak Center - 2 drive-in doors."),
            ("Drive Ins", "2", "Oak Center | Drive Ins: 2."),
            ("Power", "1200A 480V 3-phase", "Oak Center - 1200A 480V 3-phase power."),
            ("Power", "1200A 480V 3-phase", "Oak Center | Power: 1200A 480V 3-phase."),
            ("Rent/SF/Yr", "6.75", "Oak Center - $6.75/SF NNN."),
            ("Rent/SF/Yr", "6.75", "Oak Center | Rent/SF/Yr: $6.75 NNN."),
            ("Ops Ex/SF/Yr", "1.85", "Oak Center - $1.85/SF operating expenses."),
            ("Ops Ex/SF/Yr", "1.85", "Oak Center | Op Ex: $1.85/SF."),
        )

        for column, value, competing_clause in competing_facts:
            with self.subTest(column=column, competing_clause=competing_clause):
                proposal = {
                    "updates": [{"column": column, "value": value}],
                    "events": [],
                    "response_email": "Thanks.",
                }

                result = ai_processing._suppress_competing_attachment_updates(
                    proposal,
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St - 20,000 SF. {competing_clause}",
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_postfixed_unbound_identity_suppresses_every_mapped_fact_type(self):
        competing_facts = (
            ("Docks", "6", "Docks: 6 | Oak Center"),
            ("Docks", "6", "6 Dock Doors | Oak Center"),
            ("Docks", "6", "Docks: 6. Oak Center"),
            ("Drive Ins", "2", "Grade Level Doors: 2 — Oak Center"),
            ("Power", "1200A", "Electrical Capacity: 1200A • Oak Center"),
            ("Power", "1200A", "Power: 1200A\tOak Center"),
            ("Rent/SF/Yr", "6.75", "Asking Rate: $6.75 / Oak Center"),
            ("Ops Ex/SF/Yr", "1.85", "CAM Charges: $1.85, Oak Center"),
            ("Ceiling Ht", "32", "Clear Height: 32; Oak Center"),
            ("Total SF", "45000", "Available Sq Ft: 45,000\nOak Center"),
        )

        for column, value, competing_clause in competing_facts:
            with self.subTest(column=column, competing_clause=competing_clause):
                proposal = {
                    "updates": [{"column": column, "value": value}],
                    "events": [],
                    "response_email": "Thanks.",
                }

                result = ai_processing._suppress_competing_attachment_updates(
                    proposal,
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": (
                            "100 Main St - 20,000 SF. "
                            f"{competing_clause}."
                        ),
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_postfixed_identity_after_same_fragment_target_fails_closed(self):
        mixed_fragments = (
            "100 Main St | 20,000 SF | Docks: 6 | Oak Center",
            "100 Main St - 20,000 SF, Docks: 6 | Oak Center",
            "100 Main St — 20,000 SF — Docks: 6 — Oak Center",
        )

        for mixed_fragment in mixed_fragments:
            with self.subTest(mixed_fragment=mixed_fragment):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [{"column": "Docks", "value": "6"}],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"{mixed_fragment}.",
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_three_row_postfixed_identity_suppresses_every_mapped_fact_type(self):
        competing_tables = (
            ("Docks", "6", "Docks\n6\nOak Center"),
            ("Drive Ins", "2", "Drive Ins\n2\nOak Center"),
            ("Power", "1200A", "Power\n1200A\nOak Center"),
            ("Ceiling Ht", "32", "Clear Height\n32\nOak Center"),
            ("Total SF", "45000", "Total SF\n45000\nOak Center"),
            ("Rent/SF/Yr", "6.75", "Asking Rate\n$6.75/SF NNN\nOak Center"),
            ("Ops Ex/SF/Yr", "1.85", "CAM Charges\n$1.85/SF\nOak Center"),
            ("Docks", "6", "Docks\n6\nPower Center"),
            ("Power", "1200A", "Power\n1200A\nWestgate Power"),
        )

        for column, value, competing_table in competing_tables:
            with self.subTest(column=column, competing_table=competing_table):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [{"column": column, "value": value}],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St\n{competing_table}",
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_three_row_target_tables_remain_supported(self):
        target_tables = (
            ("Docks", "6", "Docks\n6"),
            ("Docks", "6", "Docks\n6\n100 Main St"),
            ("Power", "1200A 480V 3-phase", "Power\n1200A 480V 3-phase"),
            ("Rent/SF/Yr", "6.75", "Asking Rate\n$6.75/SF NNN"),
        )

        for column, value, target_table in target_tables:
            with self.subTest(column=column, target_table=target_table):
                expected_update = {"column": column, "value": value}
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [expected_update],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The target brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "100 Main St brochure.pdf",
                        "text": f"100 Main St\n{target_table}",
                    }],
                )

                self.assertEqual([expected_update], result["updates"])
                self.assertEqual([], result["events"])
                self.assertEqual("Thanks.", result["response_email"])

    def test_multi_column_postfixed_identity_tables_fail_closed(self):
        competing_tables = (
            (
                "Docks | Power\n"
                "6 | 1200A 480V 3-phase\n"
                "Oak Center | Westgate Logistics Hub"
            ),
            (
                "| Docks | Power |\n"
                "| 6 | 1200A 480V 3-phase |\n"
                "| Oak Center | Westgate Logistics Hub |"
            ),
            (
                "Docks, Power\n"
                "6, 1200A 480V 3-phase\n"
                "Oak Center, Westgate Logistics Hub"
            ),
            (
                "Docks\tPower\n"
                "6\t1200A 480V 3-phase\n"
                "Oak Center\tWestgate Logistics Hub"
            ),
            (
                "Power | Docks\n"
                "1200A 480V 3-phase | 6\n"
                "Westgate Logistics Hub | Oak Center"
            ),
            (
                "Docks | Power\n"
                "6 | 1200A\n"
                "Power Center | Westgate Power"
            ),
            (
                "Docks | Power\n"
                "6 | 1200A 480V 3-phase\n"
                "200 Oak Ave | 300 Pine Rd"
            ),
            (
                "Docks | Power\n"
                "6 | 1200A 480V 3-phase\n"
                "100 Main St / Oak Center | Westgate Logistics Hub"
            ),
        )

        for competing_table in competing_tables:
            with self.subTest(competing_table=competing_table):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [
                            {"column": "Docks", "value": "6"},
                            {"column": "Power", "value": "1200A 480V 3-phase"},
                        ],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St\n{competing_table}",
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_multi_column_mixed_target_cells_keep_only_target_updates(self):
        mixed_tables = (
            (
                "Docks | Power\n"
                "6 | 1200A 480V 3-phase\n"
                "100 Main St | Oak Center",
                {"column": "Docks", "value": "6"},
                {"column": "Power", "value": "1200A 480V 3-phase"},
            ),
            (
                "Power | Docks\n"
                "1200A 480V 3-phase | 6\n"
                "Oak Center | 100 Main St",
                {"column": "Docks", "value": "6"},
                {"column": "Power", "value": "1200A 480V 3-phase"},
            ),
            (
                "Drive Ins | Power\n"
                "2 | 1200A\n"
                "100 Main St | Oak Center",
                {"column": "Drive Ins", "value": "2"},
                {"column": "Power", "value": "1200A"},
            ),
            (
                "Docks | Power\n"
                "6 | 1200A 480V 3-phase\n"
                "Oak Center | 100 Main St",
                {"column": "Power", "value": "1200A 480V 3-phase"},
                {"column": "Docks", "value": "6"},
            ),
            (
                "Clear Height | Docks\n"
                "32 | 6\n"
                "100 Main St | Oak Center",
                {"column": "Ceiling Ht", "value": "32"},
                {"column": "Docks", "value": "6"},
            ),
            (
                "Total SF | Docks\n"
                "45,000 | 6\n"
                "100 Main St | Oak Center",
                {"column": "Total SF", "value": "45000"},
                {"column": "Docks", "value": "6"},
            ),
            (
                "Asking Rate | Docks\n"
                "$6.75/SF NNN | 6\n"
                "100 Main St | Oak Center",
                {"column": "Rent/SF/Yr", "value": "6.75"},
                {"column": "Docks", "value": "6"},
            ),
            (
                "CAM Charges | Docks\n"
                "$1.85/SF | 6\n"
                "100 Main St | Oak Center",
                {"column": "Ops Ex/SF/Yr", "value": "1.85"},
                {"column": "Docks", "value": "6"},
            ),
            (
                "Docks | Power\n"
                "6 | 1200A 480V 3-phase\n"
                "100 Main St | 200 Oak Ave",
                {"column": "Docks", "value": "6"},
                {"column": "Power", "value": "1200A 480V 3-phase"},
            ),
        )

        for mixed_table, expected_update, competing_update in mixed_tables:
            with self.subTest(mixed_table=mixed_table, expected_update=expected_update):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [expected_update, competing_update],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St\n{mixed_table}",
                    }],
                )

                self.assertEqual([expected_update], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_comma_table_address_cells_keep_column_alignment(self):
        docks = {"column": "Docks", "value": "6"}
        power = {"column": "Power", "value": "1200A 480V 3-phase"}
        total_sf = {"column": "Total SF", "value": "45000"}
        drive_ins = {"column": "Drive Ins", "value": "2"}
        cases = (
            (
                "target city first",
                "100 Main St, Phoenix",
                "Docks, Power\n"
                "6, 1200A 480V 3-phase\n"
                "100 Main St, Phoenix, Oak Center",
                [docks, power],
                [docks],
            ),
            (
                "target city last",
                "100 Main St, Phoenix",
                "Power, Docks\n"
                "1200A 480V 3-phase, 6\n"
                "Oak Center, 100 Main St, Phoenix",
                [power, docks],
                [docks],
            ),
            (
                "target city state zip in middle column",
                "100 Main St, Phoenix, AZ 85001",
                "Docks, Power, Drive Ins\n"
                "6, 1200A 480V 3-phase, 2\n"
                "Oak Center, 100 Main St, Phoenix, AZ 85001, Westgate",
                [docks, power, drive_ins],
                [power],
            ),
            (
                "short identity row with target city",
                "100 Main St, Phoenix",
                "Docks, Power, Drive Ins\n"
                "6, 1200A 480V 3-phase, 2\n"
                "100 Main St, Phoenix, Oak Center",
                [docks, power, drive_ins],
                [docks],
            ),
            (
                "target and competitor addresses with cities",
                "100 Main St, Phoenix",
                "Docks, Power\n"
                "6, 1200A 480V 3-phase\n"
                "100 Main St, Phoenix, 200 Oak Ave, Tempe",
                [docks, power],
                [docks],
            ),
            (
                "quoted address and unknown cells",
                "100 Main St, Phoenix",
                "Docks, Power\n"
                "6, 1200A 480V 3-phase\n"
                "\"100 Main St, Phoenix\", \"Oak Center\"",
                [docks, power],
                [docks],
            ),
            (
                "compact quoted csv cells",
                "100 Main St, Phoenix",
                "Docks, Power\n"
                "6, 1200A 480V 3-phase\n"
                "\"100 Main St,Phoenix\",\"Oak Center\"",
                [docks, power],
                [docks],
            ),
            (
                "fully quoted csv rows",
                "100 Main St, Phoenix",
                "\"Docks\", \"Power\"\n"
                "\"6\", \"1200A 480V 3-phase\"\n"
                "\"100 Main St, Phoenix\", \"Oak Center\"",
                [docks, power],
                [docks],
            ),
            (
                "mixed quoting and spaces",
                "100 Main St, Phoenix",
                "  \"Docks\"  ,   Power  \n"
                "  \"6\"  ,   1200A 480V 3-phase  \n"
                "  \"100 Main St, Phoenix\"  ,   Oak Center  ",
                [docks, power],
                [docks],
            ),
            (
                "escaped quotes in labels and identities",
                "100 Main St, Phoenix",
                "\"Dock \"\"Doors\"\"\", \"Power\"\n"
                "\"6\", \"1200A 480V 3-phase\"\n"
                "\"100 Main St, Phoenix\", \"Oak \"\"Power\"\" Center\"",
                [docks, power],
                [docks],
            ),
            (
                "fully quoted numeric comma",
                "100 Main St, Phoenix",
                "\"Total SF\",\"Docks\"\n"
                "\"45,000\",\"6\"\n"
                "\"100 Main St,Phoenix\",\"Oak Center\"",
                [total_sf, docks],
                [total_sf],
            ),
            (
                "numeric and address commas",
                "100 Main St, Phoenix",
                "Total SF, Docks\n"
                "45,000, 6\n"
                "100 Main St, Phoenix, Oak Center",
                [total_sf, docks],
                [total_sf],
            ),
        )

        for name, target_anchor, table, updates, expected_updates in cases:
            with self.subTest(name=name):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": list(updates),
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    target_anchor,
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"{target_anchor}\n{table}",
                    }],
                )

                self.assertEqual(expected_updates, result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_malformed_multi_column_table_shapes_fail_closed(self):
        updates = [
            {"column": "Docks", "value": "6"},
            {"column": "Power", "value": "1200A 480V 3-phase"},
            {"column": "Drive Ins", "value": "2"},
        ]
        malformed_tables = (
            (
                "quoted short identity row",
                "\"Docks\",\"Power\",\"Drive Ins\"\n"
                "\"6\",\"1200A 480V 3-phase\",\"2\"\n"
                "\"100 Main St, Phoenix\",\"Oak Center\"",
            ),
            (
                "unquoted short identity row",
                "Docks, Power, Drive Ins\n"
                "6, 1200A 480V 3-phase, 2\n"
                "100 Main St, Oak Center",
            ),
            (
                "pipe extra label",
                "Docks | Power | Drive Ins\n"
                "6 | 1200A 480V 3-phase\n"
                "100 Main St | Oak Center",
            ),
            (
                "tab missing label",
                "Docks\tDrive Ins\n"
                "6\t1200A 480V 3-phase\t2\n"
                "100 Main St\tOak Center\tWestgate",
            ),
            (
                "quoted missing trailing label",
                "\"Docks\",\n"
                "\"6\",\"1200A 480V 3-phase\"\n"
                "\"100 Main St\",\"Oak Center\"",
            ),
            (
                "quoted missing value cell",
                "\"Docks\",\"Power\",\"Drive Ins\"\n"
                "\"6\",,\"2\"\n"
                "\"100 Main St\",\"Oak Center\",\"Westgate\"",
            ),
            (
                "unquoted extra value",
                "Docks, Power\n"
                "6, 1200A 480V 3-phase, 2\n"
                "100 Main St, Oak Center",
            ),
            (
                "quoted two-row extra value",
                "\"Docks\",\"Power\"\n"
                "\"6\",\"1200A 480V 3-phase\",\"2\"",
            ),
            (
                "pipe missing identity cell",
                "Docks | Power | Drive Ins\n"
                "6 | 1200A 480V 3-phase | 2\n"
                "100 Main St | | Oak Center",
            ),
            (
                "tab extra identity",
                "Docks\tPower\n"
                "6\t1200A 480V 3-phase\n"
                "100 Main St\tOak Center\tWestgate",
            ),
            (
                "unquoted extra all-target identity",
                "Docks, Power\n"
                "6, 1200A 480V 3-phase\n"
                "100 Main St, 100 Main St, 100 Main St",
            ),
            (
                "quoted extra all-target identity",
                "\"Docks\",\"Power\"\n"
                "\"6\",\"1200A 480V 3-phase\"\n"
                "\"100 Main St\",\"100 Main St\",\"100 Main St\"",
            ),
            (
                "pipe extra all-target identity",
                "Docks | Power\n"
                "6 | 1200A 480V 3-phase\n"
                "100 Main St | 100 Main St | 100 Main St",
            ),
            (
                "tab extra all-target identity",
                "Docks\tPower\n"
                "6\t1200A 480V 3-phase\n"
                "100 Main St\t100 Main St\t100 Main St",
            ),
        )

        for name, malformed_table in malformed_tables:
            with self.subTest(name=name):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": list(updates),
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St, Phoenix\n{malformed_table}",
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_balanced_three_column_table_shapes_remain_supported(self):
        updates = [
            {"column": "Docks", "value": "6"},
            {"column": "Power", "value": "1200A 480V 3-phase"},
            {"column": "Drive Ins", "value": "2"},
        ]
        target_tables = (
            (
                "Docks, Power, Drive Ins\n"
                "6, 1200A 480V 3-phase, 2\n"
                "100 Main St, 100 Main St, 100 Main St"
            ),
            (
                "\"Docks\",\"Power\",\"Drive Ins\"\n"
                "\"6\",\"1200A 480V 3-phase\",\"2\"\n"
                "\"100 Main St, Phoenix\",\"100 Main St, Phoenix\","
                "\"100 Main St, Phoenix\""
            ),
            (
                "| Docks | Power | Drive Ins |\n"
                "| 6 | 1200A 480V 3-phase | 2 |\n"
                "| 100 Main St, Phoenix | 100 Main St, Phoenix | "
                "100 Main St, Phoenix |"
            ),
            (
                "Docks\tPower\tDrive Ins\n"
                "6\t1200A 480V 3-phase\t2\n"
                "100 Main St, Phoenix\t100 Main St, Phoenix\t"
                "100 Main St, Phoenix"
            ),
        )

        for target_table in target_tables:
            with self.subTest(target_table=target_table):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": list(updates),
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "100 Main St brochure.pdf",
                        "text": f"100 Main St, Phoenix\n{target_table}",
                    }],
                )

                self.assertEqual(updates, result["updates"])
                self.assertEqual([], result["events"])
                self.assertEqual("Thanks.", result["response_email"])

    def test_markdown_separator_rows_keep_property_cells_aligned(self):
        docks = {"column": "Docks", "value": "6"}
        power = {"column": "Power", "value": "1200A 480V 3-phase"}
        drive_ins = {"column": "Drive Ins", "value": "2"}
        cases = (
            (
                "bordered separator",
                "| Docks | Power |\n"
                "| --- | --- |\n"
                "| 6 | 1200A 480V 3-phase |\n"
                "| 100 Main St | Oak Center |",
                [docks, power],
                [docks],
                True,
            ),
            (
                "unbordered aligned separator",
                "Power | Docks\n"
                ":--- | ---:\n"
                "1200A 480V 3-phase | 6\n"
                "Oak Center | 100 Main St",
                [power, docks],
                [docks],
                True,
            ),
            (
                "centered separator with whitespace",
                "  |  Docks  |  Power  |  \n"
                "  |  :---:  |  ---:  |  \n"
                "  |  6  |  1200A 480V 3-phase  |  \n"
                "  |  100 Main St, Phoenix  |  Oak Center  |  ",
                [docks, power],
                [docks],
                True,
            ),
            (
                "separator has an extra cell",
                "| Docks | Power |\n"
                "| --- | --- | --- |\n"
                "| 6 | 1200A 480V 3-phase |\n"
                "| 100 Main St | Oak Center |",
                [docks, power],
                [docks],
                True,
            ),
            (
                "separator before malformed short identity row",
                "| Docks | Power | Drive Ins |\n"
                "| --- | --- |\n"
                "| 6 | 1200A 480V 3-phase | 2 |\n"
                "| 100 Main St | Oak Center |",
                [docks, power, drive_ins],
                [],
                True,
            ),
            (
                "separator before malformed value row",
                "| Docks | Power | Drive Ins |\n"
                "| :--- | :---: | ---: |\n"
                "| 6 | 2 |\n"
                "| 100 Main St | Oak Center | Westgate |",
                [docks, power, drive_ins],
                [],
                True,
            ),
            (
                "balanced all-target separator control",
                "| Docks | Power |\n"
                "| --- | --- |\n"
                "| 6 | 1200A 480V 3-phase |\n"
                "| 100 Main St | 100 Main St |",
                [docks, power],
                [docks, power],
                False,
            ),
            (
                "no-separator control",
                "| Docks | Power |\n"
                "| 6 | 1200A 480V 3-phase |\n"
                "| 100 Main St | Oak Center |",
                [docks, power],
                [docks],
                True,
            ),
        )

        for name, table, updates, expected_updates, escalated in cases:
            with self.subTest(name=name):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": list(updates),
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St, Phoenix\n{table}",
                    }],
                )

                self.assertEqual(expected_updates, result["updates"])
                self.assertEqual(
                    escalated,
                    any(
                        event.get("type") == "needs_user_input"
                        and event.get("reason") == "multi_property_attachment"
                        for event in result["events"]
                    ),
                )
                self.assertEqual(
                    None if escalated else "Thanks.",
                    result["response_email"],
                )

    def test_numbered_document_captions_do_not_create_property_boundaries(self):
        captions = (
            "Table 1: Building Facts",
            "Figure 2 - Building Overview",
            "Page 3 of 12",
            "Section 4.2: Loading Details",
            "Schedule 5 — Property Facts",
            "Exhibit 6: Building Facts",
            "Version 7: Building Facts",
            "Revision 8 - Building Facts",
            "Table IV: Property Summary",
            "Figure A-1 — Building Facts",
            "Exhibit B.2 (Property Summary)",
            "Schedule 1-A: Property Summary",
            "Table (1): Building Facts",
            "fIgUrE (IV) — Property Summary",
            "Section (4.2): Loading Details",
            "Schedule (A-1): Property Summary",
            "Exhibit [B.2] (Property Summary)",
            "VERSION [7A] - Building Facts",
            "Figure I",
            "Figure I Building Facts",
            "Table: Building Facts",
        )
        expected_updates = [
            {"column": "Docks", "value": "6"},
            {"column": "Power", "value": "1200A 480V 3-phase"},
        ]
        target_tables = (
            (
                "markdown",
                "| Docks | Power |\n"
                "| --- | --- |\n"
                "| 6 | 1200A 480V 3-phase |\n"
                "| 100 Main St | 100 Main St |",
            ),
            (
                "csv",
                "Docks, Power\n"
                "6, 1200A 480V 3-phase\n"
                "100 Main St, 100 Main St",
            ),
        )

        for caption in captions:
            for table_format, target_table in target_tables:
                with self.subTest(caption=caption, table_format=table_format):
                    result = ai_processing._suppress_competing_attachment_updates(
                        {
                            "updates": list(expected_updates),
                            "events": [],
                            "response_email": "Thanks.",
                        },
                        _conversation("The brochure is attached."),
                        "100 Main St, Phoenix",
                        [{
                            "name": "100 Main St brochure.pdf",
                            "text": (
                                f"100 Main St, Phoenix\n{caption}\n{target_table}"
                            ),
                        }],
                    )

                    self.assertEqual(expected_updates, result["updates"])
                    self.assertEqual([], result["events"])
                    self.assertEqual("Thanks.", result["response_email"])

    def test_structural_caption_lines_do_not_bind_neighboring_facts(self):
        fragments = (
            "Figure I\nDocks: 6",
            "Docks: 6\nFigure I",
            "Figure I Building Facts\nDocks: 6",
            "Docks: 6\nFigure I Building Facts",
            "Figure (I)\nDocks: 6",
            "Docks: 6\nFigure [I]: Building Facts",
            "Docks\nFigure I\n6\n100 Main St",
        )

        for fragment in fragments:
            with self.subTest(fragment=fragment):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [{"column": "Docks", "value": "6"}],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "100 Main St brochure.pdf",
                        "text": f"100 Main St, Phoenix\n{fragment}",
                    }],
                )

                self.assertEqual(
                    [{"column": "Docks", "value": "6"}],
                    result["updates"],
                )
                self.assertEqual([], result["events"])
                self.assertEqual("Thanks.", result["response_email"])

    def test_caption_property_residuals_fail_closed_next_to_facts(self):
        fragments = (
            "Figure I Oak Center\nDocks: 6",
            "Docks: 6\nFigure I Oak Center",
            "Figure (I): Oak Center\nDocks: 6",
            "Docks: 6\nFigure [I]: Oak Center",
            "Oak Center\nFigure I\nDocks: 6",
            "Docks: 6\nFigure I\nOak Center",
        )

        for fragment in fragments:
            with self.subTest(fragment=fragment):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [{"column": "Docks", "value": "6"}],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St, Phoenix\n{fragment}",
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_numbered_captions_preserve_mixed_table_competitor_detection(self):
        docks = {"column": "Docks", "value": "6"}
        power = {"column": "Power", "value": "1200A 480V 3-phase"}
        cases = (
            (
                "Figure 10: Building Facts",
                "| Docks | Power |\n"
                "| :--- | ---: |\n"
                "| 6 | 1200A 480V 3-phase |\n"
                "| 100 Main St | Oak Center |",
            ),
            (
                "Section 11: Property Summary",
                "Docks, Power\n"
                "6, 1200A 480V 3-phase\n"
                "100 Main St, Oak Center",
            ),
        )

        for caption, table in cases:
            with self.subTest(caption=caption):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [docks, power],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St, Phoenix\n{caption}\n{table}",
                    }],
                )

                self.assertEqual([docks], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_structural_caption_rows_do_not_break_mixed_table_alignment(self):
        docks = {"column": "Docks", "value": "6"}
        power = {"column": "Power", "value": "1200A 480V 3-phase"}
        result = ai_processing._suppress_competing_attachment_updates(
            {
                "updates": [docks, power],
                "events": [],
                "response_email": "Thanks.",
            },
            _conversation("The brochure is attached."),
            "100 Main St, Phoenix",
            [{
                "name": "mixed brochure.pdf",
                "text": (
                    "100 Main St, Phoenix\n"
                    "| Docks | Power |\n"
                    "Figure I\n"
                    "| 6 | 1200A 480V 3-phase |\n"
                    "| 100 Main St | Oak Center |"
                ),
            }],
        )

        self.assertEqual([docks], result["updates"])
        self.assertTrue(any(
            event.get("type") == "needs_user_input"
            and event.get("reason") == "multi_property_attachment"
            for event in result["events"]
        ))
        self.assertIsNone(result["response_email"])

    def test_one_cell_caption_rows_do_not_break_target_table_alignment(self):
        caption_rows = (
            "Figure I",
            "| Figure I |",
            "|| Figure I ||",
            '| "Figure I" |',
            '"| Figure I |"',
            "“Figure I”",
        )
        table_formats = (
            "{caption}\n| Docks |\n| --- |\n| 6 |\n| 100 Main St |",
            "| Docks |\n{caption}\n| --- |\n| 6 |\n| 100 Main St |",
            "| Docks |\n| --- |\n| 6 |\n{caption}\n| 100 Main St |",
            "| Docks |\n| --- |\n| 6 |\n| 100 Main St |\n{caption}",
        )

        for caption in caption_rows:
            for table_format in table_formats:
                with self.subTest(caption=caption, table_format=table_format):
                    result = ai_processing._suppress_competing_attachment_updates(
                        {
                            "updates": [{"column": "Docks", "value": "6"}],
                            "events": [],
                            "response_email": "Thanks.",
                        },
                        _conversation("The brochure is attached."),
                        "100 Main St, Phoenix",
                        [{
                            "name": "100 Main St brochure.pdf",
                            "text": (
                                "100 Main St, Phoenix\n"
                                f"{table_format.format(caption=caption)}"
                            ),
                        }],
                    )

                    self.assertEqual(
                        [{"column": "Docks", "value": "6"}],
                        result["updates"],
                    )
                    self.assertEqual([], result["events"])
                    self.assertEqual("Thanks.", result["response_email"])

    def test_one_cell_unsafe_caption_rows_still_fail_closed(self):
        caption_rows = (
            "| Figure I Oak Center |",
            '| "Figure I Oak Center" |',
            '"| Figure I Oak Center |"',
            "| Figure (I]: Building Facts |",
            "| Figure ((I)): Building Facts |",
        )

        for caption in caption_rows:
            with self.subTest(caption=caption):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [{"column": "Docks", "value": "6"}],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St, Phoenix\n{caption}\nDocks: 6",
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_balanced_escaped_caption_quotes_follow_caption_verdict(self):
        cases = (
            (r'| \"Figure I\" |', "structural"),
            (r'| \"Figure I Building Facts\" |', "structural"),
            (r'| \"Figure I Oak Center\" |', "competing"),
            (r'| \"Figure (I]: Building Facts\" |', "competing"),
        )

        for caption, expected_verdict in cases:
            with self.subTest(caption=caption):
                self.assertEqual(
                    expected_verdict,
                    ai_processing._document_caption_verdict(caption),
                )
                escalated = expected_verdict == "competing"
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [{"column": "Docks", "value": "6"}],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "100 Main St brochure.pdf",
                        "text": (
                            "100 Main St, Phoenix\n"
                            "| Docks |\n"
                            "| --- |\n"
                            "| 6 |\n"
                            f"{caption}\n"
                            "| 100 Main St |"
                        ),
                    }],
                )

                self.assertEqual(
                    [] if escalated else [{"column": "Docks", "value": "6"}],
                    result["updates"],
                )
                self.assertEqual(
                    escalated,
                    any(
                        event.get("type") == "needs_user_input"
                        and event.get("reason") == "multi_property_attachment"
                        for event in result["events"]
                    ),
                )
                self.assertEqual(
                    None if escalated else "Thanks.",
                    result["response_email"],
                )

    def test_unbalanced_caption_quotes_fail_closed_across_positions(self):
        caption_rows = (
            '| "Figure I |',
            '| Figure I" |',
            "| “Figure I Building Facts |",
            "| Figure I Building Facts” |",
            "| 'Figure I Oak Center’ |",
            r'| \"Figure I Building Facts |',
            r'| Figure I Oak Center\" |',
            r'| \"Figure (I]: Building Facts" |',
        )
        fragment_formats = (
            "{caption}\nDocks: 6",
            "Docks: 6\n{caption}",
            "| Docks |\n| --- |\n| 6 |\n{caption}\n| 100 Main St |",
        )

        for caption in caption_rows:
            self.assertEqual(
                "competing",
                ai_processing._document_caption_verdict(caption),
            )
            for fragment_format in fragment_formats:
                fragment = fragment_format.format(caption=caption)
                with self.subTest(caption=caption, fragment=fragment):
                    result = ai_processing._suppress_competing_attachment_updates(
                        {
                            "updates": [{"column": "Docks", "value": "6"}],
                            "events": [],
                            "response_email": "Thanks.",
                        },
                        _conversation("The brochure is attached."),
                        "100 Main St, Phoenix",
                        [{
                            "name": "mixed brochure.pdf",
                            "text": f"100 Main St, Phoenix\n{fragment}",
                        }],
                    )

                    self.assertEqual([], result["updates"])
                    self.assertTrue(any(
                        event.get("type") == "needs_user_input"
                        and event.get("reason") == "multi_property_attachment"
                        for event in result["events"]
                    ))
                    self.assertIsNone(result["response_email"])

    def test_caption_cell_normalization_does_not_collapse_multi_cell_rows(self):
        multi_cell_rows = (
            "| Figure I | Oak Center |",
            "Figure I | Building Facts",
            '| "Figure I | Oak Center |',
        )

        for row in multi_cell_rows:
            with self.subTest(row=row):
                self.assertIsNone(
                    ai_processing._document_caption_verdict(row)
                )

    def test_malformed_caption_designator_wrappers_fail_closed(self):
        caption_formats = (
            "Table (1]: {residual}",
            "fIgUrE [IV) — {residual}",
            "Section (4.2: {residual}",
            "Schedule [A-1 - {residual}",
            "Exhibit B.2) ({residual})",
            "VERSION IV] — {residual}",
            "Figure ((IV)): {residual}",
            "Figure ([IV]): {residual}",
            "Figure [[IV]]: {residual}",
            "Figure (IV)): {residual}",
            "Table ((1)): {residual}",
            "Schedule [[A-1]] — {residual}",
        )
        residuals = ("Building Facts", "Oak Center")

        for caption_format in caption_formats:
            for residual in residuals:
                caption = caption_format.format(residual=residual)
                with self.subTest(caption=caption):
                    result = ai_processing._suppress_competing_attachment_updates(
                        {
                            "updates": [{"column": "Docks", "value": "6"}],
                            "events": [],
                            "response_email": "Thanks.",
                        },
                        _conversation("The brochure is attached."),
                        "100 Main St, Phoenix",
                        [{
                            "name": "mixed brochure.pdf",
                            "text": f"100 Main St, Phoenix\n{caption}\nDocks: 6",
                        }],
                    )

                    self.assertEqual([], result["updates"])
                    self.assertTrue(any(
                        event.get("type") == "needs_user_input"
                        and event.get("reason") == "multi_property_attachment"
                        for event in result["events"]
                    ))
                    self.assertIsNone(result["response_email"])

    def test_caption_syntax_precheck_ignores_property_name_tokens(self):
        property_names = (
            "Figure (Ivy Commerce Park)",
            "Table [Rock Center]",
            "Page (Commerce Center)",
            "Section [Eight Plaza]",
            "Exhibit Westgate Logistics Hub",
        )

        for property_name in property_names:
            with self.subTest(property_name=property_name):
                self.assertIsNone(
                    ai_processing._document_caption_verdict(property_name)
                )

    def test_valid_roman_caption_designators_remain_structural(self):
        roman_numerals = (
            "I", "ii", "III", "iv", "VIII", "ix", "XIV", "xlii",
            "XCIX", "cdxliv", "CMXCIX", "M", "MMXXIV", "mMmCmXcIx",
        )

        for roman_numeral in roman_numerals:
            for wrapper_format in ("({})", "[{}]"):
                caption = (
                    f"Figure {wrapper_format.format(roman_numeral)}: "
                    "Building Facts"
                )
                with self.subTest(caption=caption):
                    self.assertEqual(
                        "structural",
                        ai_processing._document_caption_verdict(caption),
                    )

    def test_invalid_roman_like_caption_tokens_fail_closed(self):
        caption_bases = (
            "Figure (Civic)",
            "Table [Mill]",
            "Page (Civil)",
            "Section [Mid]",
            "Exhibit (Livid)",
            "Figure (MMMM)",
            "Figure ((IIII))",
            "Table ([VV])",
            "Page [IC]]",
            "Section (XM]",
        )
        residuals = ("", ": Building Facts", ": Oak Center")

        for caption_base in caption_bases:
            for residual in residuals:
                caption = f"{caption_base}{residual}"
                with self.subTest(caption=caption):
                    result = ai_processing._suppress_competing_attachment_updates(
                        {
                            "updates": [{"column": "Docks", "value": "6"}],
                            "events": [],
                            "response_email": "Thanks.",
                        },
                        _conversation("The brochure is attached."),
                        "100 Main St, Phoenix",
                        [{
                            "name": "mixed brochure.pdf",
                            "text": f"100 Main St, Phoenix\n{caption}\nDocks: 6",
                        }],
                    )

                    self.assertEqual([], result["updates"])
                    self.assertTrue(any(
                        event.get("type") == "needs_user_input"
                        and event.get("reason") == "multi_property_attachment"
                        for event in result["events"]
                    ))
                    self.assertIsNone(result["response_email"])

    def test_numbered_unknown_property_headings_still_fail_closed(self):
        headings = (
            "Oak Center 1",
            "Oak Center 1: Building Facts",
            "Table Center 1: Building Facts",
            "Table 1: Oak Center",
            "Figure IV — Westgate Logistics Hub",
            "Page A-1: Oak Center",
            "Section 4.2: Oak Center",
            "Schedule B.2 (Oak Center)",
            "Schedule 1-A: Oak Center",
            "Exhibit C-3: Westgate Logistics Hub",
            "Version II: Oak Center",
            "Revision 7A - Oak Center",
            "Table (1): Oak Center",
            "fIgUrE (IV) — Westgate Logistics Hub",
            "Section (4.2): Oak Center",
            "Schedule (A-1): Oak Center",
            "Exhibit [B.2] (Oak Center)",
            "VERSION [7A] - Oak Center",
        )

        for heading in headings:
            with self.subTest(heading=heading):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [{"column": "Docks", "value": "6"}],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": (
                            f"100 Main St, Phoenix\n{heading}\nDocks: 6"
                        ),
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertTrue(any(
                    event.get("type") == "needs_user_input"
                    and event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))
                self.assertIsNone(result["response_email"])

    def test_multi_column_target_tables_remain_supported(self):
        target_tables = (
            (
                "Docks | Power\n"
                "6 | 1200A 480V 3-phase"
            ),
            (
                "Docks | Power\n"
                "6 | 1200A 480V 3-phase\n"
                "100 Main St | 100 Main St"
            ),
            (
                "Docks | Power\n"
                "6 | 1200A 480V 3-phase\n"
                "100 Main St, Phoenix | 100 Main St, Phoenix"
            ),
            (
                "Asking Rate | Power\n"
                "$6.75/SF NNN | 1200A 480V 3-phase"
            ),
            (
                "Power, Docks\n"
                "1200A 480V 3-phase, 6"
            ),
            (
                "Docks\tPower\n"
                "6\t1200A 480V 3-phase"
            ),
        )

        for target_table in target_tables:
            with self.subTest(target_table=target_table):
                expected_updates = [
                    {"column": "Docks", "value": "6"},
                    {"column": "Power", "value": "1200A 480V 3-phase"},
                ]
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": list(expected_updates),
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The target brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "100 Main St brochure.pdf",
                        "text": f"100 Main St\n{target_table}",
                    }],
                )

                self.assertEqual(expected_updates, result["updates"])
                self.assertEqual([], result["events"])
                self.assertEqual("Thanks.", result["response_email"])

    def test_exact_target_postfix_preserves_target_fact_updates(self):
        target_clauses = (
            "Docks: 6 | 100 Main St",
            "Docks: 6; 100 Main St",
            "Docks: 6\n100 Main St",
        )

        for target_clause in target_clauses:
            with self.subTest(target_clause=target_clause):
                expected_update = {"column": "Docks", "value": "6"}
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [expected_update],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The target brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "100 Main St brochure.pdf",
                        "text": f"100 Main St - 20,000 SF. {target_clause}.",
                    }],
                )

                self.assertEqual([expected_update], result["updates"])
                self.assertEqual([], result["events"])
                self.assertEqual("Thanks.", result["response_email"])

    def test_postfixed_fact_token_property_names_remain_competing(self):
        competing_headings = (
            ("Docks", "6", "Docks: 6 | Power Center"),
            ("Docks", "6", "Docks: 6 | Oak Docks"),
            ("Power", "1200A", "Power: 1200A | Westgate Power"),
        )

        for column, value, competing_heading in competing_headings:
            with self.subTest(competing_heading=competing_heading):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [{"column": column, "value": value}],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St - 20,000 SF. {competing_heading}.",
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertIsNone(result["response_email"])
                self.assertTrue(any(
                    event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))

    def test_street_suffix_period_cannot_hide_competing_table_heading(self):
        proposal = {
            "updates": [{"column": "Docks", "value": "6"}],
            "events": [],
            "response_email": "Thanks.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("The brochure is attached."),
            "100 Main St, Phoenix",
            [{
                "name": "mixed brochure.pdf",
                "text": "100 Main St. Oak Center | Docks: 6.",
            }],
        )

        self.assertEqual([], result["updates"])
        self.assertTrue(any(
            event.get("type") == "needs_user_input"
            and event.get("reason") == "multi_property_attachment"
            for event in result["events"]
        ))
        self.assertIsNone(result["response_email"])

    def test_exact_target_pdf_preserves_semantic_fact_label_synonym(self):
        target_facts = (
            ("Docks", "6", "Dock Positions: 6"),
            ("Drive Ins", "2", "Grade Level Doors: 2"),
            ("Power", "1200A", "Electrical Capacity: 1200A"),
            ("Power", "1200A 480V 3-phase", "Power: 1200A 480V 3-phase"),
            ("Rent/SF/Yr", "6.75", "Asking Rate: $6.75"),
            ("Rent/SF/Yr", "6.75", "Asking Rate: $6.75/SF NNN"),
            ("Ops Ex/SF/Yr", "1.85", "CAM Charges: $1.85"),
            ("Total SF", "45000", "Available Sq Ft: 45000"),
        )

        for column, value, target_fact in target_facts:
            with self.subTest(column=column, target_fact=target_fact):
                expected_update = {"column": column, "value": value}
                proposal = {
                    "updates": [expected_update],
                    "events": [],
                    "response_email": "Thanks for confirming.",
                }

                result = ai_processing._suppress_competing_attachment_updates(
                    proposal,
                    _conversation("The 100 Main St brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "100 Main St brochure.pdf",
                        "text": f"100 Main St. {target_fact}.",
                    }],
                )

                self.assertEqual([expected_update], result["updates"])
                self.assertEqual([], result["events"])
                self.assertEqual(
                    "Thanks for confirming.",
                    result["response_email"],
                )

    def test_fact_token_property_names_remain_competing(self):
        competing_headings = (
            ("Power", "1200A", "Power Center | 1200A"),
            ("Power", "1200A", "Power Square | 1200A"),
            ("Power", "1200A", "Westgate Power | 1200A"),
            ("Docks", "6", "Oak Docks | 6"),
        )

        for column, value, competing_heading in competing_headings:
            with self.subTest(competing_heading=competing_heading):
                result = ai_processing._suppress_competing_attachment_updates(
                    {
                        "updates": [{"column": column, "value": value}],
                        "events": [],
                        "response_email": "Thanks.",
                    },
                    _conversation("The brochure is attached."),
                    "100 Main St, Phoenix",
                    [{
                        "name": "mixed brochure.pdf",
                        "text": f"100 Main St. {competing_heading}.",
                    }],
                )

                self.assertEqual([], result["updates"])
                self.assertIsNone(result["response_email"])
                self.assertTrue(any(
                    event.get("reason") == "multi_property_attachment"
                    for event in result["events"]
                ))

    def test_versioned_addressless_attachment_is_competing_by_default(self):
        attachment_names = (
            "Oak Commerce Center brochure version 2.pdf",
            "Oak Commerce Center brochure (2).pdf",
        )

        for attachment_name in attachment_names:
            with self.subTest(attachment_name=attachment_name):
                proposal = {
                    "updates": [{"column": "Ceiling Ht", "value": "32"}],
                    "events": [],
                    "response_email": "Thanks.",
                }

                result = ai_processing._suppress_competing_attachment_updates(
                    proposal,
                    _conversation(
                        "Another option is Oak Commerce Center; brochure attached."
                    ),
                    "100 Main St, Phoenix",
                    [
                        {
                            "name": "100 Main St brochure.pdf",
                            "text": "100 Main St - 20,000 SF.",
                        },
                        {
                            "name": attachment_name,
                            "text": "Ceiling Ht: 32 feet clear.",
                        },
                    ],
                )

                self.assertEqual([], result["updates"])
                self.assertIsNone(result["response_email"])

    def test_exact_target_address_clause_preserves_value(self):
        expected_update = {"column": "Ceiling Ht", "value": "28"}
        proposal = {
            "updates": [expected_update],
            "events": [],
            "response_email": None,
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("100 Main St has 28 feet clear."),
            "100 Main St, Phoenix",
            [
                {
                    "name": "100 Main St brochure.pdf",
                    "text": "100 Main St - 20,000 SF.",
                },
                {
                    "name": "200 Oak Ave brochure.pdf",
                    "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([expected_update], result["updates"])

    def test_independent_target_attachment_evidence_preserves_value(self):
        expected_update = {
            "column": "Ceiling Ht",
            "value": "32",
            "confidence": 0.96,
        }
        proposal = {
            "updates": [expected_update],
            "events": [],
            "response_email": None,
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("We have two options; the brochures are attached."),
            "100 Main St, Phoenix",
            [
                {
                    "name": "100 Main St brochure.pdf",
                    "text": "100 Main St - Ceiling Ht: 32 feet clear.",
                },
                {
                    "name": "200 Oak Ave brochure.pdf",
                    "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([expected_update], result["updates"])

    def test_generic_this_property_does_not_prove_attachment_value(self):
        proposal = {
            "updates": [{"column": "Ceiling Ht", "value": "32"}],
            "events": [],
            "response_email": None,
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation(
                "This property has 32 feet clear. We also have another option; "
                "the brochures are attached."
            ),
            "100 Main St, Phoenix",
            [
                {
                    "name": "100 Main St brochure.pdf",
                    "text": "100 Main St - 20,000 SF industrial building.",
                },
                {
                    "name": "200 Oak Ave brochure.pdf",
                    "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([], result["updates"])

    def test_attachment_classification_does_not_depend_on_offer_wording(self):
        proposal = {
            "updates": [{"column": "Ceiling Ht", "value": "32"}],
            "events": [],
            "response_email": None,
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("Please see the attached brochures."),
            "100 Main St, Phoenix",
            [
                {
                    "name": "100 Main St brochure.pdf",
                    "text": "100 Main St - 20,000 SF industrial building.",
                },
                {
                    "name": "200 Oak Ave brochure.pdf",
                    "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([], result["updates"])

    def test_target_fact_before_brokerage_footer_address_remains_supported(self):
        expected_update = {"column": "Ceiling Ht", "value": "28"}
        proposal = {
            "updates": [expected_update],
            "events": [],
            "response_email": None,
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("Please see the attached brochure."),
            "105 W Dewey Ave, Wharton",
            [{
                "name": "105 W Dewey Ave brochure.pdf",
                "text": (
                    "105 W Dewey Ave FOR LEASE. Ceiling Ht: 28 feet clear. "
                    "Garden State Realty, 204 Passaic Ave, Fairfield."
                ),
            }],
        )

        self.assertEqual([expected_update], result["updates"])

    def test_target_address_number_is_not_ceiling_height_evidence(self):
        proposal = {
            "updates": [{"column": "Ceiling Ht", "value": "32"}],
            "events": [],
            "response_email": None,
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("Please see the attached brochures."),
            "32 Main St, Phoenix",
            [
                {
                    "name": "32 Main St brochure.pdf",
                    "text": "32 Main St - 20,000 SF industrial building.",
                },
                {
                    "name": "200 Oak Ave brochure.pdf",
                    "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
                },
            ],
        )

        self.assertEqual([], result["updates"])

    def test_new_property_event_keeps_attachment_binding_owned_by_event_path(self):
        expected_update = {"column": "Ceiling Ht", "value": "32"}
        proposal = {
            "updates": [expected_update],
            "events": [
                {"type": "new_property", "address": "200 Oak Ave", "city": "Phoenix"}
            ],
            "response_email": "Thanks for the option.",
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("The alternate brochure is attached."),
            "100 Main St, Phoenix",
            [{
                "name": "200 Oak Ave brochure.pdf",
                "text": "200 Oak Ave - Ceiling Ht: 32 feet clear.",
            }],
        )

        self.assertEqual([expected_update], result["updates"])
        self.assertEqual("new_property", result["events"][0]["type"])
        self.assertEqual("Thanks for the option.", result["response_email"])

    def test_competing_multi_property_brochure_escalates_instead_of_writing_current_row(self):
        proposal = {
            "updates": [
                {"column": "Rent/SF /Yr", "value": "15.75", "confidence": 0.72},
                {"column": "Total SF", "value": "9500", "confidence": 0.92},
            ],
            "events": [{"type": "tour_requested", "question": "Glad to show."}],
            "response_email": "Thanks.",
        }
        brochure = {
            "name": "AUSTIN BUSINESS PARK NEW.pdf",
            "text": (
                "Austin Business Park 3336 SC-51 Fort Mill. "
                "Building 1: 9,500 SF, $18 PSF. "
                "Building 2: 3,000 SF, $13 PSF. "
                "Building 3: 7,500 SF, $15 PSF."
            ),
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation(
                "I have 2 buildings here: 7,500 SF and 9,500 SF. "
                "Brochure with rent info attached."
            ),
            "3344 S Carolina 51, Fort Mill",
            [brochure],
        )

        self.assertEqual([], result["updates"])
        self.assertIn(
            "multi_property_attachment",
            [event.get("reason") for event in result["events"]],
        )
        self.assertIsNone(result["response_email"])

        current, _ = processing._partition_property_attachments(
            [brochure],
            current_anchor="3344 S Carolina 51, Fort Mill",
            events=result["events"],
        )
        self.assertEqual([], current)

    def test_replacement_only_reply_cannot_update_original_row(self):
        proposal = {
            "updates": [
                {"column": "Total SF", "value": "8000", "confidence": 0.72},
                {"column": "Drive Ins", "value": "1", "confidence": 0.78},
                {"column": "Ceiling Ht", "value": "12", "confidence": 0.86},
            ],
            "events": [
                {
                    "type": "new_property",
                    "address": "48 Richboynton Road",
                    "city": "Dover",
                }
            ],
        }

        result = ai_processing._suppress_cross_property_current_row_updates(
            proposal,
            _conversation(
                "I have ~8K S.F. at 48 Richboynton Road in Dover. It has a "
                "10' drive-in door. Ceilings are 14' to the deck but only 12' clear."
            ),
            "53 Richboynton Rd, Dover",
        )

        self.assertEqual([], result["updates"])

    def test_mixed_reply_keeps_current_property_updates(self):
        proposal = {
            "updates": [
                {"column": "Total SF", "value": "7200", "confidence": 0.95},
                {"column": "Drive Ins", "value": "3", "confidence": 0.9},
            ],
            "events": [
                {
                    "type": "new_property",
                    "address": "[TBD] Sterling Plaza Phase II",
                    "city": "Ponte Vedra, FL",
                }
            ],
        }

        result = ai_processing._suppress_cross_property_current_row_updates(
            proposal,
            _conversation(
                "Yes, this space meets your criteria and is available for sale. "
                "It is 7,200 sf and has three grade-level doors. We also have a "
                "newly built park adjacent to this location called Sterling Plaza Phase II."
            ),
            "200 Sterling Plaza Dr, Town Of Nocatee",
        )

        self.assertEqual(2, len(result["updates"]))

    def test_replacement_this_building_language_cannot_update_original_row(self):
        proposal = {
            "updates": [
                {"column": "Total SF", "value": "8000", "confidence": 0.93}
            ],
            "events": [
                {"type": "new_property", "address": "Suite B", "city": "Phoenix"}
            ],
        }

        result = ai_processing._suppress_cross_property_current_row_updates(
            proposal,
            _conversation("This building is Suite B and has 8,000 SF."),
            "100 SiteSift Canary Way, Phoenix",
        )

        self.assertEqual([], result["updates"])

    def test_target_mention_does_not_license_alternate_property_updates(self):
        proposal = {
            "updates": [
                {"column": "Total SF", "value": "8000", "confidence": 0.93}
            ],
            "events": [
                {"type": "new_property", "address": "Suite B", "city": "Phoenix"}
            ],
        }

        result = ai_processing._suppress_cross_property_current_row_updates(
            proposal,
            _conversation(
                "For 100 SiteSift Canary Way, see the prior note. The alternative "
                "Suite B has 8,000 SF."
            ),
            "100 SiteSift Canary Way, Phoenix",
        )

        self.assertEqual([], result["updates"])

    def test_explicit_current_facts_before_alternate_remain_on_current_row(self):
        proposal = {
            "updates": [
                {"column": "Total SF", "value": "7200", "confidence": 0.95}
            ],
            "events": [
                {"type": "new_property", "address": "Suite B", "city": "Phoenix"}
            ],
        }

        result = ai_processing._suppress_cross_property_current_row_updates(
            proposal,
            _conversation(
                "100 SiteSift Canary Way is 7,200 SF. We also have Suite B, "
                "which is 8,000 SF."
            ),
            "100 SiteSift Canary Way, Phoenix",
        )

        self.assertEqual(
            [{"column": "Total SF", "value": "7200", "confidence": 0.95}],
            result["updates"],
        )

    def test_sterling_attachments_are_partitioned_by_property(self):
        permit = {
            "name": "2121 American Wall Beds Co PERMIT REV2 11 18 22.pdf",
            "text": "ROF TUO DLIUB TNANET .OC DEB LLAW NACIREMA RD AZALP GNILRETS 002",
        }
        alternate_flyer = {
            "name": "STERLING PLAZA PHASE II FLYER UPDATE 5.8.pdf",
            "text": "STERLING PLAZA PHASE II PONTE VEDRA, FL - 2,400 SF units",
        }
        events = [
            {
                "type": "new_property",
                "address": "[TBD] newly built park adjacent to Sterling Plaza "
                "(FutureFlex / Sterling Plaza Phase II)",
                "city": "Ponte Vedra, FL",
            }
        ]

        current, by_event = processing._partition_property_attachments(
            [permit, alternate_flyer],
            current_anchor="200 Sterling Plaza Dr, Town Of Nocatee",
            events=events,
        )

        self.assertEqual([permit], current)
        self.assertEqual([alternate_flyer], by_event[0])

    def test_replacement_floorplan_does_not_land_on_original_row(self):
        floorplan = {
            "name": "48RichboyntonRoad1stFloor8910.pdf",
            "text": "48 Richboynton Road - 1st Floor - 8,910 S.F.",
        }
        events = [
            {
                "type": "new_property",
                "address": "48 Richboynton Road",
                "city": "Dover",
            }
        ]

        current, by_event = processing._partition_property_attachments(
            [floorplan],
            current_anchor="53 Richboynton Rd, Dover",
            events=events,
        )

        self.assertEqual([], current)
        self.assertEqual([floorplan], by_event[0])

    def test_target_brochure_ignores_brokerage_office_address(self):
        brochure = {
            "name": "105 W Dewey Ave, Bldg B, 9&10, Wharton_Brochure.pdf",
            "text": (
                "105 W Dewey Ave FOR LEASE. 8,000 SF. "
                "Garden State Realty, 204 Passaic Ave, Fairfield."
            ),
        }

        current, by_event = processing._partition_property_attachments(
            [brochure],
            current_anchor="105 W Dewey Ave, Wharton",
            events=[],
        )

        self.assertEqual([brochure], current)
        self.assertEqual([], by_event)

    def test_same_city_phase_attachments_route_to_the_unique_event(self):
        phase_one = {
            "name": "Sterling Plaza Phase I brochure.pdf",
            "text": "Sterling Plaza Phase I, Phoenix - 5,000 SF",
        }
        phase_two = {
            "name": "Sterling Plaza Phase II brochure.pdf",
            "text": "Sterling Plaza Phase II, Phoenix - 8,000 SF",
        }
        events = [
            {
                "type": "new_property",
                "address": "Sterling Plaza Phase I",
                "city": "Phoenix",
            },
            {
                "type": "new_property",
                "address": "Sterling Plaza Phase II",
                "city": "Phoenix",
            },
        ]

        current, by_event = processing._partition_property_attachments(
            [phase_one, phase_two],
            current_anchor="100 SiteSift Canary Way, Phoenix",
            events=events,
        )

        self.assertEqual([], current)
        self.assertEqual([phase_one], by_event[0])
        self.assertEqual([phase_two], by_event[1])

    def test_unresolved_replacement_attachment_is_not_defaulted_to_first_event(self):
        ambiguous = {
            "name": "Phoenix options brochure.pdf",
            "text": "Two industrial options are available in Phoenix.",
        }
        events = [
            {"type": "new_property", "address": "Suite A", "city": "Phoenix"},
            {"type": "new_property", "address": "Suite B", "city": "Phoenix"},
        ]

        current, by_event = processing._partition_property_attachments(
            [ambiguous],
            current_anchor="100 SiteSift Canary Way, Phoenix",
            events=events,
        )

        self.assertEqual([], current)
        self.assertEqual([[], []], by_event)

    def test_mixed_current_and_alternate_brochure_is_left_for_review(self):
        mixed = {
            "name": "Current and Suite B brochure.pdf",
            "text": (
                "100 SiteSift Canary Way - 7,500 SF. "
                "200 Alternate Road Suite B - 8,000 SF."
            ),
        }
        events = [
            {
                "type": "new_property",
                "address": "200 Alternate Road Suite B",
                "city": "Phoenix",
            }
        ]

        current, by_event = processing._partition_property_attachments(
            [mixed],
            current_anchor="100 SiteSift Canary Way, Phoenix",
            events=events,
        )

        self.assertEqual([], current)
        self.assertEqual([[]], by_event)

    def test_requirements_mismatch_has_truthful_terminal_label(self):
        event = {"type": "property_unavailable", "reason": "requirements_mismatch"}

        self.assertEqual(
            "requirements_mismatch",
            processing._nonviable_status_reason(event),
        )
        comment = processing._build_property_unavailable_comment(
            "07/21/2026",
            "requirements_mismatch",
            [event],
        )
        self.assertIn("does not meet client requirements", comment.lower())
        self.assertNotIn("marked unavailable", comment.lower())

    def test_deterministic_mismatch_normalizes_model_reason(self):
        for model_reason in ("physical_non_fit", "Requirements_Mismatch", "bad_fit"):
            with self.subTest(model_reason=model_reason):
                proposal = {
                    "updates": [],
                    "events": [
                        {
                            "type": "property_unavailable",
                            "reason": model_reason,
                        }
                    ],
                    "response_email": "Thanks for the update.",
                }
                result = ai_processing._augment_events_with_deterministic_signals(
                    proposal,
                    _conversation(
                        "The space is too office-heavy and does not meet the "
                        "client's warehouse requirements."
                    ),
                    target_anchor="100 SiteSift Canary Way, Phoenix",
                )

                unavailable = [
                    event
                    for event in result["events"]
                    if event.get("type") == "property_unavailable"
                ]
                self.assertEqual(1, len(unavailable))
                self.assertEqual("requirements_mismatch", unavailable[0]["reason"])
                self.assertIsNone(result["response_email"])

    def test_requirements_mismatch_fallback_never_claims_property_is_unavailable(self):
        body = processing._select_automatic_response_body(
            "requirements_mismatch",
            None,
            {},
            "Baylor",
        )

        self.assertIn("does not meet", body.lower())
        self.assertIn("requirements", body.lower())
        self.assertNotIn("no longer available", body.lower())

    def test_requirements_mismatch_with_alternative_fallback_is_truthful(self):
        body = processing._select_automatic_response_body(
            "requirements_mismatch_with_alternative",
            None,
            {},
            "Baylor",
        )

        self.assertIn("does not meet", body.lower())
        self.assertIn("alternative", body.lower())
        self.assertNotIn("no longer available", body.lower())

    def test_requirements_mismatch_stops_followups_before_sheet_move(self):
        events = [{"type": "property_unavailable", "reason": "requirements_mismatch"}]

        patch = processing._pending_nonviable_followup_patch(
            events,
            row_anchor="111 Canfield Ave, Randolph",
            message_text="The units do not have a drive in door.",
        )

        self.assertEqual("stopped", patch["followUpStatus"])
        self.assertIsNone(patch["followUpConfig.nextFollowUpAt"])
        self.assertEqual("requirements_mismatch", patch["pendingTerminalReason"])


if __name__ == "__main__":
    unittest.main()
