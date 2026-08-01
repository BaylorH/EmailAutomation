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
