import unittest
import os

os.environ.setdefault("E2E_TEST_MODE", "true")
for candidate_credentials in [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
]:
    if os.path.exists(candidate_credentials):
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", candidate_credentials)
        break

from email_automation import ai_processing, processing


class JillJuneRegressionTests(unittest.TestCase):
    def test_no_longer_represent_property_adds_unavailable_event(self):
        proposal = {"updates": [], "events": []}
        conversation = [
            {
                "direction": "inbound",
                "content": "Sorry for the delay, we no longer represent this property.",
            }
        ]

        augmented = ai_processing._augment_events_with_deterministic_signals(
            proposal,
            conversation,
        )

        self.assertIn(
            {"type": "property_unavailable", "reason": "no_longer_represented"},
            augmented["events"],
        )

    def test_no_space_and_signed_loi_adds_unavailable_event(self):
        proposal = {"updates": [], "events": []}
        conversation = [
            {
                "direction": "inbound",
                "content": "We do not have any space available and already have a signed LOI.",
            }
        ]

        augmented = ai_processing._augment_events_with_deterministic_signals(
            proposal,
            conversation,
        )

        self.assertEqual("property_unavailable", augmented["events"][0]["type"])
        self.assertEqual("signed_loi", augmented["events"][0]["reason"])

    def test_signed_lease_no_space_no_tour_availability_marks_property_unavailable(self):
        proposal = {
            "updates": [],
            "events": [
                {
                    "type": "tour_requested",
                    "reason": "tour_slot_reply",
                    "question": "No tour availability.",
                    "suggestedEmail": "",
                },
                {"type": "close_conversation", "notes": "deal_pending"},
            ],
        }
        conversation = [
            {
                "direction": "inbound",
                "content": (
                    "Unfortunately 3535 Statesman is no longer available. "
                    "The owner signed a lease on it this week, so there is no "
                    "space to offer and no tour availability."
                ),
            }
        ]

        augmented = ai_processing._augment_events_with_deterministic_signals(
            proposal,
            conversation,
        )
        event_types = [event.get("type") for event in augmented["events"]]

        self.assertEqual("property_unavailable", augmented["events"][0]["type"])
        self.assertIn(augmented["events"][0]["reason"], {"no_longer_available", "signed_lease"})
        self.assertNotIn("tour_requested", event_types)
        self.assertNotIn("close_conversation", event_types)

    def test_existing_unavailable_event_is_not_duplicated_and_reason_is_canonicalized(self):
        proposal = {
            "updates": [],
            "events": [{"type": "property_unavailable", "reason": "model"}],
        }
        conversation = [
            {
                "direction": "inbound",
                "content": "We no longer represent this property.",
            }
        ]

        augmented = ai_processing._augment_events_with_deterministic_signals(
            proposal,
            conversation,
        )

        self.assertEqual(1, len(augmented["events"]))
        self.assertEqual("no_longer_represented", augmented["events"][0]["reason"])

    def test_requirements_mismatch_adds_nonviable_event(self):
        proposal = {"updates": [], "events": []}
        conversation = [
            {
                "direction": "inbound",
                "content": (
                    "Hi Jill,\n\n"
                    "This space wouldn’t be a good fit for your client as it is more "
                    "office heavy as opposed to a true warehouse with drive in space."
                ),
            }
        ]

        augmented = ai_processing._augment_events_with_deterministic_signals(
            proposal,
            conversation,
        )

        self.assertEqual("property_unavailable", augmented["events"][0]["type"])
        self.assertEqual("requirements_mismatch", augmented["events"][0]["reason"])

    def test_requirements_mismatch_variants_add_nonviable_event(self):
        examples = [
            (
                "This property is not the right fit for your client because it "
                "lacks warehouse space and does not have drive-in access."
            ),
            (
                "The suite does not meet the client's requirements. It is mostly "
                "office and lacks industrial warehouse area."
            ),
        ]

        for example in examples:
            with self.subTest(example=example):
                proposal = {"updates": [], "events": []}
                conversation = [{"direction": "inbound", "content": example}]

                augmented = ai_processing._augment_events_with_deterministic_signals(
                    proposal,
                    conversation,
                )

                self.assertEqual("property_unavailable", augmented["events"][0]["type"])
                self.assertEqual("requirements_mismatch", augmented["events"][0]["reason"])

    def test_tour_slot_alternate_reply_adds_tour_event_when_model_misses_it(self):
        proposal = {"updates": [], "events": []}
        conversation = [
            {
                "direction": "outbound",
                "content": "Requested arrival: 10:47 AM\nPlease confirm whether this tour slot works.",
            },
            {
                "direction": "inbound",
                "content": "The 10:47 AM slot does not work for us. We could do 1:30 PM instead.",
            },
        ]

        augmented = ai_processing._augment_events_with_deterministic_signals(
            proposal,
            conversation,
        )

        self.assertEqual("tour_requested", augmented["events"][0]["type"])
        self.assertEqual("tour_slot_reply", augmented["events"][0]["reason"])
        self.assertIn("1:30 PM", augmented["events"][0]["question"])

    def test_tour_unavailable_reply_does_not_mark_property_unavailable(self):
        proposal = {"updates": [], "events": [{"type": "property_unavailable", "reason": "model"}]}
        conversation = [
            {
                "direction": "outbound",
                "content": (
                    "Tour date: Tuesday, June 23, 2026\n"
                    "Requested arrival: 10:47 AM\n"
                    "Please confirm whether this tour slot works."
                ),
            },
            {
                "direction": "inbound",
                "content": "The space is no longer available for tours.",
            },
        ]

        augmented = ai_processing._augment_events_with_deterministic_signals(
            proposal,
            conversation,
        )

        self.assertNotIn(
            "property_unavailable",
            [event.get("type") for event in augmented["events"]],
        )
        self.assertEqual("tour_requested", augmented["events"][0]["type"])
        self.assertEqual("tour_unavailable", augmented["events"][0]["reason"])

    def test_tour_unavailable_slash_phrase_stays_tour_specific(self):
        proposal = {"updates": [], "events": [{"type": "property_unavailable", "reason": "model"}]}
        conversation = [
            {
                "direction": "outbound",
                "content": (
                    "Tour date: Tuesday, June 23, 2026\n"
                    "Requested arrival: 10:47 AM\n"
                    "Please confirm whether this tour slot works."
                ),
            },
            {
                "direction": "inbound",
                "content": "The space is no longer available for tours/showings/walkthroughs.",
            },
        ]

        augmented = ai_processing._augment_events_with_deterministic_signals(
            proposal,
            conversation,
        )

        self.assertNotIn(
            "property_unavailable",
            [event.get("type") for event in augmented["events"]],
        )
        self.assertEqual("tour_requested", augmented["events"][0]["type"])
        self.assertEqual("tour_unavailable", augmented["events"][0]["reason"])

    def test_tour_unavailable_availability_phrase_stays_tour_specific(self):
        for inbound in [
            "There is no tour availability for this space right now.",
            "There is no availability for tours this week.",
            "The owner is not offering interior tours right now.",
        ]:
            with self.subTest(inbound=inbound):
                proposal = {"updates": [], "events": [{"type": "property_unavailable", "reason": "model"}]}
                conversation = [
                    {
                        "direction": "outbound",
                        "content": (
                            "Tour date: Tuesday, June 23, 2026\n"
                            "Requested arrival: 10:47 AM\n"
                            "Please confirm whether this tour slot works."
                        ),
                    },
                    {
                        "direction": "inbound",
                        "content": inbound,
                    },
                ]

                augmented = ai_processing._augment_events_with_deterministic_signals(
                    proposal,
                    conversation,
                )

                self.assertNotIn(
                    "property_unavailable",
                    [event.get("type") for event in augmented["events"]],
                )
                self.assertEqual("tour_requested", augmented["events"][0]["type"])
                self.assertEqual("tour_unavailable", augmented["events"][0]["reason"])

    def test_initial_outreach_tour_unavailable_note_does_not_emit_tour_request(self):
        proposal = {"updates": [], "events": []}
        conversation = [
            {
                "direction": "outbound",
                "content": "Could you confirm availability and tour availability?",
            },
            {
                "direction": "inbound",
                "content": (
                    "903 Bay Star Blvd is still available. The owner is not offering "
                    "interior tours right now, but a drive-by is fine."
                ),
            },
        ]

        augmented = ai_processing._augment_events_with_deterministic_signals(
            proposal,
            conversation,
        )

        self.assertNotIn(
            "tour_requested",
            [event.get("type") for event in augmented["events"]],
        )

    def test_tour_scheduling_thread_with_intermediate_outbound_keeps_tour_unavailable(self):
        proposal = {"updates": [], "events": [{"type": "property_unavailable", "reason": "model"}]}
        conversation = [
            {
                "direction": "outbound",
                "content": (
                    "Tour date: Tuesday, June 30, 2026\n"
                    "Requested arrival: 2:15 PM\n"
                    "Please confirm whether this tour slot works."
                ),
            },
            {
                "direction": "outbound",
                "content": "I am checking the route and schedule and will confirm shortly.",
            },
            {
                "direction": "inbound",
                "content": "The owner is not offering interior tours right now.",
            },
        ]

        augmented = ai_processing._augment_events_with_deterministic_signals(
            proposal,
            conversation,
        )

        self.assertNotIn(
            "property_unavailable",
            [event.get("type") for event in augmented["events"]],
        )
        self.assertEqual("tour_requested", augmented["events"][0]["type"])
        self.assertEqual("tour_unavailable", augmented["events"][0]["reason"])

    def test_requirements_mismatch_downstream_guard_applies_to_current_row(self):
        event = {"type": "property_unavailable", "reason": "requirements_mismatch"}
        message_text = (
            "19241 David Memorial Dr is not the right fit for your client because "
            "it lacks warehouse space and does not have drive-in access."
        )

        self.assertTrue(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="19241 David Memorial Dr, The Woodlands",
                message_text=message_text,
                unavailable_keywords=processing.PROPERTY_UNAVAILABLE_KEYWORDS,
            )
        )

    def test_ancillary_requirements_mismatch_does_not_terminalize_target(self):
        event = {"type": "property_unavailable", "reason": "requirements_mismatch"}
        messages = (
            "The outparcel is mostly office.",
            "At 100 Main St, the outparcel is mostly office.",
            "The trailer yard does not have drive-in access.",
            "At 100 Main St, the trailer yard does not have drive-in access.",
        )

        for message_text in messages:
            with self.subTest(message_text=message_text):
                proposal = ai_processing._augment_events_with_deterministic_signals(
                    {"updates": [], "events": [], "response_email": "Thanks."},
                    [{"direction": "inbound", "content": message_text}],
                    target_anchor="100 Main St, Phoenix",
                )
                self.assertNotIn(
                    "property_unavailable",
                    [event.get("type") for event in proposal["events"]],
                )
                self.assertFalse(
                    processing._property_unavailable_event_applies_to_row(
                        event,
                        row_anchor="100 Main St, Phoenix",
                        message_text=message_text,
                    )
                )

    def test_target_requirements_mismatch_survives_separate_ancillary_mismatch(self):
        message_text = (
            "The outparcel is mostly office. "
            "100 Main St lacks warehouse space."
        )
        proposal = ai_processing._augment_events_with_deterministic_signals(
            {"updates": [], "events": [], "response_email": "Thanks."},
            [{"direction": "inbound", "content": message_text}],
            target_anchor="100 Main St, Phoenix",
        )

        self.assertIn(
            "property_unavailable",
            [event.get("type") for event in proposal["events"]],
        )
        self.assertTrue(
            processing._property_unavailable_event_applies_to_row(
                {"type": "property_unavailable", "reason": "requirements_mismatch"},
                row_anchor="100 Main St, Phoenix",
                message_text=message_text,
            )
        )

    def test_target_requirements_mismatch_after_ancillary_comma_and_reaches_row_guard(self):
        message_text = (
            "The outparcel is fully leased, and 100 Main St is mostly office."
        )
        proposal = ai_processing._augment_events_with_deterministic_signals(
            {"updates": [], "events": [], "response_email": "Thanks."},
            [{"direction": "inbound", "content": message_text}],
            target_anchor="100 Main St, Phoenix",
        )
        unavailable = [
            event
            for event in proposal["events"]
            if event.get("type") == "property_unavailable"
        ]

        self.assertEqual("requirements_mismatch", unavailable[0]["reason"])
        self.assertTrue(
            processing._property_unavailable_event_applies_to_row(
                unavailable[0],
                row_anchor="100 Main St, Phoenix",
                message_text=message_text,
            )
        )

    def test_target_availability_does_not_cancel_requirements_mismatch_terminalization(self):
        messages = (
            (
                "The outparcel is fully leased, but 100 Main St is still "
                "available but mostly office."
            ),
            "100 Main St is still available but mostly office.",
            "100 Main St is still available. It is mostly office.",
        )

        for message_text in messages:
            with self.subTest(message_text=message_text):
                proposal = ai_processing._augment_events_with_deterministic_signals(
                    {"updates": [], "events": [], "response_email": "Thanks."},
                    [{"direction": "inbound", "content": message_text}],
                    target_anchor="100 Main St, Phoenix",
                )
                unavailable = [
                    event
                    for event in proposal["events"]
                    if event.get("type") == "property_unavailable"
                ]
                event = unavailable[0] if unavailable else {}
                applies_to_row = (
                    processing._property_unavailable_event_applies_to_row(
                        event,
                        row_anchor="100 Main St, Phoenix",
                        message_text=message_text,
                    )
                )
                patch = processing._pending_nonviable_followup_patch(
                    unavailable,
                    row_anchor="100 Main St, Phoenix",
                    message_text=message_text,
                )

                self.assertEqual(
                    {
                        "reasons": ["requirements_mismatch"],
                        "applies_to_row": True,
                        "follow_up_status": "stopped",
                        "pending_reason": "requirements_mismatch",
                    },
                    {
                        "reasons": [event.get("reason") for event in unavailable],
                        "applies_to_row": applies_to_row,
                        "follow_up_status": (patch or {}).get("followUpStatus"),
                        "pending_reason": (patch or {}).get("pendingTerminalReason"),
                    },
                )

    def test_target_availability_does_not_cancel_normalized_mismatch_reasons(self):
        message_text = "100 Main St is still available but mostly office."

        for reason in (
            "requirements_mismatch",
            "physical_non_fit",
            "physical_mismatch",
            "bad_fit",
            "requirements_non_fit",
        ):
            with self.subTest(reason=reason):
                self.assertTrue(
                    processing._property_unavailable_event_applies_to_row(
                        {"type": "property_unavailable", "reason": reason},
                        row_anchor="100 Main St, Phoenix",
                        message_text=message_text,
                    )
                )

    def test_competitor_availability_does_not_cancel_target_requirements_mismatch(self):
        message_text = (
            "200 Oak Ave is still available, but 100 Main St is mostly office."
        )
        proposal = ai_processing._augment_events_with_deterministic_signals(
            {"updates": [], "events": [], "response_email": "Thanks."},
            [{"direction": "inbound", "content": message_text}],
            target_anchor="100 Main St, Phoenix",
        )
        unavailable = [
            event
            for event in proposal["events"]
            if event.get("type") == "property_unavailable"
        ]

        self.assertEqual(
            ["requirements_mismatch"],
            [event.get("reason") for event in unavailable],
        )
        self.assertTrue(
            processing._property_unavailable_event_applies_to_row(
                unavailable[0],
                row_anchor="100 Main St, Phoenix",
                message_text=message_text,
            )
        )

    def test_competitor_mismatch_does_not_bind_to_viable_target_across_conjunctions(self):
        address_pairs = (
            ("1 Target Way", "2 Rival Rd"),
            ("12 Target Way", "34 Rival Rd"),
            ("123 Target Way", "456 Rival Rd"),
            ("1234 Target Way", "5678 Rival Rd"),
            ("12345 Target Way", "56789 Rival Rd"),
            ("123456 Target Way", "654321 Rival Rd"),
        )

        for target_address, competitor_address in address_pairs:
            for conjunction in (", and ", " and ", ", but ", " but ", ", or ", " or "):
                message_text = (
                    f"{competitor_address} is mostly office{conjunction}"
                    f"{target_address} remains available."
                )
                for reason in ("requirements_mismatch", "physical_non_fit"):
                    event = {"type": "property_unavailable", "reason": reason}
                    with self.subTest(
                        target_address=target_address,
                        conjunction=conjunction,
                        reason=reason,
                    ):
                        patch = processing._pending_nonviable_followup_patch(
                            [event],
                            row_anchor=f"{target_address}, Phoenix",
                            message_text=message_text,
                        )
                        self.assertEqual(
                            {"applies_to_row": False, "pending_patch": None},
                            {
                                "applies_to_row": (
                                    processing._property_unavailable_event_applies_to_row(
                                        event,
                                        row_anchor=f"{target_address}, Phoenix",
                                        message_text=message_text,
                                    )
                                ),
                                "pending_patch": patch,
                            },
                        )

    def test_target_binding_after_ancillary_supports_address_led_conjunctions(self):
        street_addresses = (
            "1 A St",
            "12 B St",
            "123 Main St",
            "1234 Main St",
            "12345 Long Rd",
            "123456 Long Rd",
        )

        for street_address in street_addresses:
            for conjunction in (", and ", " and ", ", but ", " but ", ", or ", " or "):
                message_text = (
                    f"The outparcel is fully leased{conjunction}"
                    f"{street_address} is mostly office."
                )
                with self.subTest(
                    street_address=street_address,
                    conjunction=conjunction,
                ):
                    proposal = ai_processing._augment_events_with_deterministic_signals(
                        {"updates": [], "events": [], "response_email": "Thanks."},
                        [{"direction": "inbound", "content": message_text}],
                        target_anchor=f"{street_address}, Phoenix",
                    )
                    unavailable = [
                        event
                        for event in proposal["events"]
                        if event.get("type") == "property_unavailable"
                    ]
                    self.assertEqual(
                        ["requirements_mismatch"],
                        [event.get("reason") for event in unavailable],
                    )
                    self.assertTrue(
                        processing._property_unavailable_event_applies_to_row(
                            unavailable[0],
                            row_anchor=f"{street_address}, Phoenix",
                            message_text=message_text,
                        )
                    )

    def test_ancillary_conjunction_without_independent_target_assertion_stays_scoped(self):
        event = {"type": "property_unavailable", "reason": "requirements_mismatch"}
        messages = (
            "The outparcel is fully leased, and mostly office.",
            "The outparcel is fully leased, or mostly office.",
            "At 100 Main St, the outparcel is fully leased, and mostly office.",
        )

        for message_text in messages:
            with self.subTest(message_text=message_text):
                proposal = ai_processing._augment_events_with_deterministic_signals(
                    {"updates": [], "events": [], "response_email": "Thanks."},
                    [{"direction": "inbound", "content": message_text}],
                    target_anchor="100 Main St, Phoenix",
                )
                self.assertNotIn(
                    "property_unavailable",
                    [event.get("type") for event in proposal["events"]],
                )
                self.assertFalse(
                    processing._property_unavailable_event_applies_to_row(
                        event,
                        row_anchor="100 Main St, Phoenix",
                        message_text=message_text,
                    )
                )

    def test_ancillary_only_but_stays_scoped_across_address_lengths(self):
        event = {"type": "property_unavailable", "reason": "requirements_mismatch"}
        street_addresses = (
            "1 A St",
            "12 B St",
            "123 Main St",
            "1234 Main St",
            "12345 Long Rd",
            "123456 Long Rd",
        )

        for street_address in street_addresses:
            for conjunction in (", but ", " but "):
                message_text = (
                    f"At {street_address}, the outparcel is fully leased"
                    f"{conjunction}mostly office."
                )
                with self.subTest(
                    street_address=street_address,
                    conjunction=conjunction,
                ):
                    proposal = ai_processing._augment_events_with_deterministic_signals(
                        {"updates": [], "events": [], "response_email": "Thanks."},
                        [{"direction": "inbound", "content": message_text}],
                        target_anchor=f"{street_address}, Phoenix",
                    )
                    actual = {
                        "generated": "property_unavailable" in [
                            event.get("type") for event in proposal["events"]
                        ],
                        "applies_to_row": (
                            processing._property_unavailable_event_applies_to_row(
                                event,
                                row_anchor=f"{street_address}, Phoenix",
                                message_text=message_text,
                            )
                        ),
                    }
                    self.assertEqual(
                        {"generated": False, "applies_to_row": False},
                        actual,
                    )

    def test_downstream_guard_rejects_tour_only_unavailability_as_nonviable(self):
        event = {"type": "property_unavailable", "reason": "model"}

        self.assertFalse(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="4402 Rex Rd, Friendswood",
                message_text=(
                    "The space is no longer available for tours on Tuesday, "
                    "but the listing package is still accurate."
                ),
            )
        )

    def test_fit_question_does_not_add_nonviable_event(self):
        proposal = {"updates": [], "events": []}
        conversation = [
            {
                "direction": "inbound",
                "content": "Can you confirm whether this space would be a good fit for your client?",
            }
        ]

        augmented = ai_processing._augment_events_with_deterministic_signals(
            proposal,
            conversation,
        )

        self.assertEqual([], augmented["events"])

    def test_new_property_event_defers_pdf_links_from_current_row(self):
        events = [{"type": "new_property", "address": "Elam Business Park"}]

        self.assertTrue(processing._has_new_property_path(events))

    def test_new_property_event_text_tolerates_null_fields(self):
        event = {
            "type": "new_property",
            "address": None,
            "city": None,
            "email": None,
            "contactName": None,
            "link": None,
            "notes": None,
        }

        self.assertEqual("", processing._event_text(event, "address"))
        self.assertEqual("", processing._event_text(event, "city"))
        self.assertEqual("", processing._event_text(event, "email"))
        self.assertEqual("", processing._event_text(event, "contactName"))
        self.assertEqual("", processing._event_text(event, "link"))
        self.assertEqual("", processing._event_text(event, "notes"))

    def test_proposal_events_none_is_treated_as_empty(self):
        self.assertEqual([], processing._proposal_events({"events": None}))

    def test_proposal_events_skip_non_dict_entries_without_dropping_valid_events(self):
        proposal = {
            "events": [
                None,
                "new_property",
                {"type": None, "address": "No type should skip"},
                {"type": "new_property", "address": "27610 Commerce Oaks Dr"},
            ]
        }

        self.assertEqual(
            [{"type": "new_property", "address": "27610 Commerce Oaks Dr"}],
            processing._proposal_events(proposal),
        )

    def test_terminalized_original_row_skips_stale_operator_escalations(self):
        for event_type in [
            "tour_requested",
            "call_requested",
            "needs_user_input",
            "wrong_contact",
            "property_issue",
            "close_conversation",
        ]:
            with self.subTest(event_type=event_type):
                self.assertTrue(
                    processing._should_skip_event_after_original_row_terminalized(
                        event_type,
                        old_row_became_nonviable=True,
                    )
                )

    def test_terminalized_original_row_still_allows_replacement_and_optout_events(self):
        for event_type in ["new_property", "contact_optout"]:
            with self.subTest(event_type=event_type):
                self.assertFalse(
                    processing._should_skip_event_after_original_row_terminalized(
                        event_type,
                        old_row_became_nonviable=True,
                    )
                )

    def test_viable_original_row_does_not_skip_operator_escalations(self):
        self.assertFalse(
            processing._should_skip_event_after_original_row_terminalized(
                "tour_requested",
                old_row_became_nonviable=False,
            )
        )

    def test_unavailable_event_without_address_does_not_apply_to_replacement_row(self):
        event = {"type": "property_unavailable", "reason": "fully_leased"}
        message_text = (
            "404 Replacement Signal Ave is fully leased. "
            "A similar option is 414 Alternate Signal Ave in Las Vegas. "
            "Following up with the package details for 414 Alternate Signal Ave: "
            "19,250 SF, asking $1.05/SF/month NNN."
        )

        self.assertFalse(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="414 Alternate Signal Ave, Las Vegas",
                message_text=message_text,
            )
        )

    def test_unavailable_event_without_address_applies_when_current_row_is_named_unavailable(self):
        event = {"type": "property_unavailable", "reason": "fully_leased"}
        message_text = (
            "404 Replacement Signal Ave is fully leased. "
            "A similar option is 414 Alternate Signal Ave in Las Vegas."
        )

        self.assertTrue(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="404 Replacement Signal Ave, Las Vegas",
                message_text=message_text,
            )
        )

    def test_addressless_unavailable_event_rejects_terminal_bound_to_other_address(self):
        event = {"type": "property_unavailable", "reason": "leased"}

        self.assertFalse(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="100 Main St, Phoenix",
                message_text=(
                    "The other building at 200 Oak Ave is leased. "
                    "I attached the requested specs."
                ),
            )
        )

    def test_addressless_unavailable_event_does_not_bind_target_specs_to_competing_terminal(self):
        event = {"type": "property_unavailable", "reason": "leased"}

        self.assertFalse(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="100 Main St, Phoenix",
                message_text=(
                    "200 Oak Ave is leased. "
                    "I attached the requested specs for 100 Main St."
                ),
            )
        )

    def test_addressless_requirements_mismatch_rejects_competing_property(self):
        event = {"type": "property_unavailable", "reason": "requirements_mismatch"}

        self.assertFalse(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="100 Main St, Phoenix",
                message_text=(
                    "200 Oak Ave is not the right fit because it is mostly office. "
                    "Here are the requested specs."
                ),
            )
        )

    def test_addressless_unavailable_event_handles_single_digit_target_street_number(self):
        event = {"type": "property_unavailable", "reason": "leased"}

        self.assertFalse(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="1 Kuhlke Dr",
                message_text="200 Oak Ave is leased. I attached specs.",
            )
        )

    def test_upstream_terminal_grounding_handles_one_to_six_digit_addresses(self):
        address_pairs = (
            ("1", "2"),
            ("12", "34"),
            ("123", "456"),
            ("1234", "5678"),
            ("12345", "56789"),
            ("123456", "654321"),
        )

        for target_number, competitor_number in address_pairs:
            target_anchor = f"{target_number} Target Way, Phoenix"
            with self.subTest(target_anchor=target_anchor, subject="target"):
                target = ai_processing._augment_events_with_deterministic_signals(
                    {"updates": [], "events": [], "response_email": "Thanks."},
                    [{
                        "direction": "inbound",
                        "content": f"{target_number} Target Way has been leased.",
                    }],
                    target_anchor=target_anchor,
                )
                self.assertIn(
                    "property_unavailable",
                    [event.get("type") for event in target["events"]],
                )

            with self.subTest(target_anchor=target_anchor, subject="competitor"):
                competitor = ai_processing._augment_events_with_deterministic_signals(
                    {"updates": [], "events": [], "response_email": "Thanks."},
                    [{
                        "direction": "inbound",
                        "content": f"{competitor_number} Rival Rd has been leased.",
                    }],
                    target_anchor=target_anchor,
                )
                self.assertNotIn(
                    "property_unavailable",
                    [event.get("type") for event in competitor["events"]],
                )
                self.assertEqual("Thanks.", competitor["response_email"])

    def test_addressless_unavailable_event_rejects_named_competitor_when_target_viable(self):
        event = {"type": "property_unavailable", "reason": "leased"}

        self.assertFalse(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="100 Main St, Phoenix",
                message_text=(
                    "Oak Commerce Center is leased. "
                    "100 Main St is still available."
                ),
            )
        )

    def test_competitor_viability_does_not_override_target_terminal(self):
        event = {"type": "property_unavailable", "reason": "leased"}

        for message_text in (
            "200 Oak Ave is still available and 100 Main St is leased.",
            "100 Main St is leased and 200 Oak Ave is still available.",
        ):
            with self.subTest(message_text=message_text):
                self.assertTrue(
                    processing._property_unavailable_event_applies_to_row(
                        event,
                        row_anchor="100 Main St, Phoenix",
                        message_text=message_text,
                    )
                )

    def test_shared_viability_predicate_preserves_target_across_address_lists(self):
        address_pairs = (
            ("1 Main St", "2 Oak Ave"),
            ("123 Main St", "456 Oak Ave"),
            ("123456 Main St", "654321 Oak Ave"),
        )
        shared_predicates = (
            (", and ", "are both still available"),
            (" and ", "are both still available"),
            (", and ", "are still available"),
            (" and ", "are still available"),
            (", but ", "are both still available"),
            (" but ", "are both still available"),
            (", or ", "are both still available"),
            (" or ", "are both still available"),
        )

        for target_address, competitor_address in address_pairs:
            for first, second in (
                (target_address, competitor_address),
                (competitor_address, target_address),
            ):
                for conjunction, predicate in shared_predicates:
                    message_text = f"{first}{conjunction}{second} {predicate}."
                    event = {"type": "property_unavailable", "reason": "leased"}
                    with self.subTest(
                        target_address=target_address,
                        first=first,
                        conjunction=conjunction,
                        predicate=predicate,
                    ):
                        patch = processing._pending_nonviable_followup_patch(
                            [event],
                            row_anchor=f"{target_address}, Phoenix",
                            message_text=message_text,
                        )
                        self.assertEqual(
                            {"applies_to_row": False, "pending_patch": None},
                            {
                                "applies_to_row": (
                                    processing._property_unavailable_event_applies_to_row(
                                        event,
                                        row_anchor=f"{target_address}, Phoenix",
                                        message_text=message_text,
                                    )
                                ),
                                "pending_patch": patch,
                            },
                        )

    def test_negated_availability_never_suppresses_target_terminal_event(self):
        address_pairs = (
            ("1 Main St", "2 Oak Ave"),
            ("12 Main St", "34 Oak Ave"),
            ("123 Main St", "456 Oak Ave"),
            ("1234 Main St", "5678 Oak Ave"),
            ("12345 Main St", "56789 Oak Ave"),
            ("123456 Main St", "654321 Oak Ave"),
        )
        messages = (
            "Neither {first} nor {second} remains available.",
            "Neither {first}, nor {second}, remains available.",
            "{first} and {second} are not still available.",
            "{first}, and {second}, are not still available.",
            "{first} and {second} do not remain available.",
            "{first}, and {second}, do not remain available.",
        )

        for target_address, competitor_address in address_pairs:
            for first, second in (
                (target_address, competitor_address),
                (competitor_address, target_address),
            ):
                for reason in ("leased", "sold", "no_space_available"):
                    for message_template in messages:
                        message_text = message_template.format(
                            first=first,
                            second=second,
                        )
                        event = {"type": "property_unavailable", "reason": reason}
                        with self.subTest(
                            target_address=target_address,
                            first=first,
                            reason=reason,
                            message_template=message_template,
                        ):
                            patch = processing._pending_nonviable_followup_patch(
                                [event],
                                row_anchor=f"{target_address}, Phoenix",
                                message_text=message_text,
                            )
                            self.assertTrue(
                                processing._property_unavailable_event_applies_to_row(
                                    event,
                                    row_anchor=f"{target_address}, Phoenix",
                                    message_text=message_text,
                                )
                            )
                            self.assertIsNotNone(patch)
                            self.assertEqual("stopped", patch["followUpStatus"])
                            self.assertEqual(reason, patch["pendingTerminalReason"])

    def test_unrelated_neither_nor_does_not_negate_later_target_viability(self):
        address_pairs = (
            ("1 Main St", "2 Oak Ave"),
            ("12 Main St", "34 Oak Ave"),
            ("123 Main St", "456 Oak Ave"),
            ("1234 Main St", "5678 Oak Ave"),
            ("12345 Main St", "56789 Oak Ave"),
            ("123456 Main St", "654321 Oak Ave"),
        )
        separators = (", but ", " but ", ". ", "; ")
        unrelated_prefixes = (
            "Neither the broker nor the owner objected",
            "Neither the broker for {competitor} nor the owner objected",
            "Neither {competitor} nor 999 Side Rd objected",
        )
        affirmative_predicates = (
            "clearly remains available",
            "could possibly remain available",
            "could quite conceivably still remain available",
            "appears to theoretically remain available",
        )

        for target_address, competitor_address in address_pairs:
            for reason in ("leased", "sold", "no_space_available"):
                event = {"type": "property_unavailable", "reason": reason}
                for predicate in affirmative_predicates:
                    for prefix_template in unrelated_prefixes:
                        prefix = prefix_template.format(competitor=competitor_address)
                        for separator in separators:
                            message_text = (
                                f"{prefix}{separator}{target_address} "
                                f"{predicate}."
                            )
                            with self.subTest(
                                target_address=target_address,
                                reason=reason,
                                predicate=predicate,
                                prefix_template=prefix_template,
                                separator=separator,
                            ):
                                self.assertFalse(
                                    processing._property_unavailable_event_applies_to_row(
                                        event,
                                        row_anchor=f"{target_address}, Phoenix",
                                        message_text=message_text,
                                    )
                                )
                                self.assertIsNone(
                                    processing._pending_nonviable_followup_patch(
                                        [event],
                                        row_anchor=f"{target_address}, Phoenix",
                                        message_text=message_text,
                                    )
                                )

                for separator in separators:
                    message_text = (
                        f"Neither {competitor_address} nor {target_address} "
                        f"was rejected{separator}it could possibly remain available."
                    )
                    with self.subTest(
                        target_address=target_address,
                        reason=reason,
                        scope="bounded_link_clause_break",
                        separator=separator,
                    ):
                        self.assertFalse(
                            processing._property_unavailable_event_applies_to_row(
                                event,
                                row_anchor=f"{target_address}, Phoenix",
                                message_text=message_text,
                            )
                        )
                        self.assertIsNone(
                            processing._pending_nonviable_followup_patch(
                                [event],
                                row_anchor=f"{target_address}, Phoenix",
                                message_text=message_text,
                            )
                        )

    def test_neither_nor_property_subject_supports_predicate_modifiers(self):
        address_pairs = (
            ("1 Main St", "2 Oak Ave"),
            ("12 Main St", "34 Oak Ave"),
            ("123 Main St", "456 Oak Ave"),
            ("1234 Main St", "5678 Oak Ave"),
            ("12345 Main St", "56789 Oak Ave"),
            ("123456 Main St", "654321 Oak Ave"),
        )
        adverb_phrases = (
            "possibly",
            "conceivably",
            "currently",
            "quite possibly",
            "quite conceivably still",
            "theoretically even now",
        )
        predicate_frames = (
            "could {adverbs} remain available",
            "is {adverbs} still available",
            "appears to {adverbs} remain available",
        )
        predicates = tuple(
            frame.format(adverbs=adverbs)
            for frame in predicate_frames
            for adverbs in adverb_phrases
        )

        for target_address, competitor_address in address_pairs:
            for first, second in (
                (target_address, competitor_address),
                (competitor_address, target_address),
            ):
                for reason in ("leased", "sold", "no_space_available"):
                    event = {"type": "property_unavailable", "reason": reason}
                    for predicate in predicates:
                        for punctuation in (False, True):
                            if punctuation:
                                message_text = (
                                    f"Neither {first}, nor {second}, {predicate}."
                                )
                            else:
                                message_text = (
                                    f"Neither {first} nor {second} {predicate}."
                                )
                            with self.subTest(
                                target_address=target_address,
                                first=first,
                                reason=reason,
                                predicate=predicate,
                                punctuation=punctuation,
                            ):
                                patch = processing._pending_nonviable_followup_patch(
                                    [event],
                                    row_anchor=f"{target_address}, Phoenix",
                                    message_text=message_text,
                                )
                                self.assertTrue(
                                    processing._property_unavailable_event_applies_to_row(
                                        event,
                                        row_anchor=f"{target_address}, Phoenix",
                                        message_text=message_text,
                                    )
                                )
                                self.assertIsNotNone(patch)
                                self.assertEqual("stopped", patch["followUpStatus"])
                                self.assertEqual(
                                    reason,
                                    patch["pendingTerminalReason"],
                                )

    def test_neither_nor_does_not_cross_subordinate_or_relative_clause(self):
        address_pairs = (
            ("1 Main St", "2 Oak Ave"),
            ("12 Main St", "34 Oak Ave"),
            ("123 Main St", "456 Oak Ave"),
            ("1234 Main St", "5678 Oak Ave"),
            ("12345 Main St", "56789 Oak Ave"),
            ("123456 Main St", "654321 Oak Ave"),
        )
        subordinate_links = (
            "since each",
            "because each",
            "although each",
            "while each",
            "when each",
            "where each",
            "if each",
            "unless each",
            "which means each",
            "that means each",
            "who says each",
        )

        for target_address, competitor_address in address_pairs:
            for first, second in (
                (target_address, competitor_address),
                (competitor_address, target_address),
            ):
                for reason in ("leased", "sold", "no_space_available"):
                    event = {"type": "property_unavailable", "reason": reason}
                    for subordinate_link in subordinate_links:
                        for punctuation in (" ", ", "):
                            message_text = (
                                f"Neither {first} nor {second} was rejected"
                                f"{punctuation}{subordinate_link} remains available."
                            )
                            with self.subTest(
                                target_address=target_address,
                                first=first,
                                reason=reason,
                                subordinate_link=subordinate_link,
                                punctuation=punctuation,
                            ):
                                self.assertFalse(
                                    processing._property_unavailable_event_applies_to_row(
                                        event,
                                        row_anchor=f"{target_address}, Phoenix",
                                        message_text=message_text,
                                    )
                                )
                                self.assertIsNone(
                                    processing._pending_nonviable_followup_patch(
                                        [event],
                                        row_anchor=f"{target_address}, Phoenix",
                                        message_text=message_text,
                                    )
                                )

    def test_property_set_quantifiers_bind_all_preceding_addresses(self):
        address_pairs = (
            ("1 Main St", "2 Oak Ave"),
            ("12 Main St", "34 Oak Ave"),
            ("123 Main St", "456 Oak Ave"),
            ("1234 Main St", "5678 Oak Ave"),
            ("12345 Main St", "56789 Oak Ave"),
            ("123456 Main St", "654321 Oak Ave"),
        )
        connectors = (", and ", " and ", ", but ", " but ", ", ", ". ", "; ")
        quantifier_phrases = (
            "both are still available",
            "each property is still available",
            "both properties remain available",
            "each site currently remains available",
        )

        for target_address, competitor_address in address_pairs:
            for first, second in (
                (target_address, competitor_address),
                (competitor_address, target_address),
            ):
                for reason in ("leased", "sold", "no_space_available"):
                    event = {"type": "property_unavailable", "reason": reason}
                    for connector in connectors:
                        for quantifier_phrase in quantifier_phrases:
                            message_text = (
                                f"{first} and {second}{connector}"
                                f"{quantifier_phrase}."
                            )
                            with self.subTest(
                                target_address=target_address,
                                first=first,
                                reason=reason,
                                connector=connector,
                                quantifier_phrase=quantifier_phrase,
                            ):
                                self.assertFalse(
                                    processing._property_unavailable_event_applies_to_row(
                                        event,
                                        row_anchor=f"{target_address}, Phoenix",
                                        message_text=message_text,
                                    )
                                )
                                self.assertIsNone(
                                    processing._pending_nonviable_followup_patch(
                                        [event],
                                        row_anchor=f"{target_address}, Phoenix",
                                        message_text=message_text,
                                    )
                                )

    def test_nonproperty_set_quantifiers_do_not_bind_preceding_addresses(self):
        address_pairs = (
            ("1 Main St", "2 Oak Ave"),
            ("123 Main St", "456 Oak Ave"),
            ("123456 Main St", "654321 Oak Ave"),
        )
        connectors = (", and ", " and ", ", but ", " but ", ", ", ". ", "; ")
        quantifier_phrases = (
            "both brokers are still available",
            "each owner is still available",
            "both prices remain available",
            "each property manager is still available",
        )

        for target_address, competitor_address in address_pairs:
            for first, second in (
                (target_address, competitor_address),
                (competitor_address, target_address),
            ):
                for reason in ("leased", "sold", "no_space_available"):
                    event = {"type": "property_unavailable", "reason": reason}
                    for connector in connectors:
                        for quantifier_phrase in quantifier_phrases:
                            message_text = (
                                f"{first} and {second}{connector}"
                                f"{quantifier_phrase}."
                            )
                            with self.subTest(
                                target_address=target_address,
                                first=first,
                                reason=reason,
                                connector=connector,
                                quantifier_phrase=quantifier_phrase,
                            ):
                                patch = processing._pending_nonviable_followup_patch(
                                    [event],
                                    row_anchor=f"{target_address}, Phoenix",
                                    message_text=message_text,
                                )
                                self.assertTrue(
                                    processing._property_unavailable_event_applies_to_row(
                                        event,
                                        row_anchor=f"{target_address}, Phoenix",
                                        message_text=message_text,
                                    )
                                )
                                self.assertIsNotNone(patch)
                                self.assertEqual("stopped", patch["followUpStatus"])
                                self.assertEqual(
                                    reason,
                                    patch["pendingTerminalReason"],
                                )

    def test_nonproperty_quantifiers_before_address_lists_do_not_bind_properties(self):
        address_pairs = (
            ("1 Harbor Pkwy", "2 Copper Mesa Blvd"),
            ("12 Harbor Pkwy", "34 Copper Mesa Blvd"),
            ("123 Harbor Pkwy", "456 Copper Mesa Blvd"),
            ("1234 Harbor Pkwy", "5678 Copper Mesa Blvd"),
            ("12345 Harbor Pkwy", "56789 Copper Mesa Blvd"),
            ("123456 Harbor Pkwy", "654321 Copper Mesa Blvd"),
        )
        address_list_templates = (
            "{first} and {second}",
            "{first}, and {second}",
            "{first} and {second},",
            "{first}, and {second},",
        )
        quantified_subjects = (
            ("brokers", "for", "are still available"),
            ("owners", "at", "remain available"),
            ("prices", "across", "are still available"),
            ("contacts", "for", "remain available"),
            ("agents", "at", "are currently still available"),
        )

        for target_address, competitor_address in address_pairs:
            for first, second in (
                (target_address, competitor_address),
                (competitor_address, target_address),
            ):
                for reason in ("leased", "sold", "no_space_available"):
                    event = {"type": "property_unavailable", "reason": reason}
                    for address_list_template in address_list_templates:
                        address_list = address_list_template.format(
                            first=first,
                            second=second,
                        )
                        for subject, preposition, predicate in quantified_subjects:
                            message_text = (
                                f"Both {subject} {preposition} {address_list} "
                                f"{predicate}."
                            )
                            with self.subTest(
                                target_address=target_address,
                                first=first,
                                reason=reason,
                                address_list_template=address_list_template,
                                subject=subject,
                            ):
                                patch = processing._pending_nonviable_followup_patch(
                                    [event],
                                    row_anchor=f"{target_address}, Phoenix",
                                    message_text=message_text,
                                )
                                self.assertTrue(
                                    processing._property_unavailable_event_applies_to_row(
                                        event,
                                        row_anchor=f"{target_address}, Phoenix",
                                        message_text=message_text,
                                    )
                                )
                                self.assertIsNotNone(patch)
                                self.assertEqual("stopped", patch["followUpStatus"])
                                self.assertEqual(
                                    reason,
                                    patch["pendingTerminalReason"],
                                )

    def test_property_quantifiers_before_address_lists_bind_properties(self):
        address_pairs = (
            ("1 Harbor Pkwy", "2 Copper Mesa Blvd"),
            ("12 Harbor Pkwy", "34 Copper Mesa Blvd"),
            ("123 Harbor Pkwy", "456 Copper Mesa Blvd"),
            ("1234 Harbor Pkwy", "5678 Copper Mesa Blvd"),
            ("12345 Harbor Pkwy", "56789 Copper Mesa Blvd"),
            ("123456 Harbor Pkwy", "654321 Copper Mesa Blvd"),
        )
        address_list_templates = (
            "{first} and {second}",
            "{first}, and {second}",
            "{first} and {second},",
            "{first}, and {second},",
        )
        quantified_subjects = (
            ("properties", "at", "are still available"),
            ("buildings", "at", "remain available"),
            ("sites", "across", "are currently still available"),
        )

        for target_address, competitor_address in address_pairs:
            for first, second in (
                (target_address, competitor_address),
                (competitor_address, target_address),
            ):
                for reason in ("leased", "sold", "no_space_available"):
                    event = {"type": "property_unavailable", "reason": reason}
                    for address_list_template in address_list_templates:
                        address_list = address_list_template.format(
                            first=first,
                            second=second,
                        )
                        for subject, preposition, predicate in quantified_subjects:
                            message_text = (
                                f"Both {subject} {preposition} {address_list} "
                                f"{predicate}."
                            )
                            with self.subTest(
                                target_address=target_address,
                                first=first,
                                reason=reason,
                                address_list_template=address_list_template,
                                subject=subject,
                            ):
                                self.assertFalse(
                                    processing._property_unavailable_event_applies_to_row(
                                        event,
                                        row_anchor=f"{target_address}, Phoenix",
                                        message_text=message_text,
                                    )
                                )
                                self.assertIsNone(
                                    processing._pending_nonviable_followup_patch(
                                        [event],
                                        row_anchor=f"{target_address}, Phoenix",
                                        message_text=message_text,
                                    )
                                )

    def test_possessive_nonproperty_heads_before_address_lists_stay_nonproperty(self):
        target_address = "17 Harbor Pkwy"
        competitor_address = "8021 Copper Mesa Blvd"
        quantified_subjects = (
            "Both brokers' teams",
            "Each of the owners’ contacts",
            "Both agents' representatives",
            "Each of the contacts’ coordinators",
            "Both properties' brokers",
            "Each of the buildings’ owners",
        )
        address_list_templates = (
            "{first} and {second}",
            "{first}, and {second},",
        )

        for first, second in (
            (target_address, competitor_address),
            (competitor_address, target_address),
        ):
            for reason in ("leased", "sold", "no_space_available"):
                event = {"type": "property_unavailable", "reason": reason}
                for quantified_subject in quantified_subjects:
                    for preposition in ("for", "at", "across"):
                        for address_list_template in address_list_templates:
                            address_list = address_list_template.format(
                                first=first,
                                second=second,
                            )
                            message_text = (
                                f"{quantified_subject} {preposition} {address_list} "
                                "are still available."
                            )
                            with self.subTest(
                                first=first,
                                reason=reason,
                                quantified_subject=quantified_subject,
                                preposition=preposition,
                                address_list_template=address_list_template,
                            ):
                                patch = processing._pending_nonviable_followup_patch(
                                    [event],
                                    row_anchor=f"{target_address}, Phoenix",
                                    message_text=message_text,
                                )
                                self.assertTrue(
                                    processing._property_unavailable_event_applies_to_row(
                                        event,
                                        row_anchor=f"{target_address}, Phoenix",
                                        message_text=message_text,
                                    )
                                )
                                self.assertIsNotNone(patch)
                                self.assertEqual("stopped", patch["followUpStatus"])
                                self.assertEqual(
                                    reason,
                                    patch["pendingTerminalReason"],
                                )

    def test_possessive_property_heads_before_address_lists_bind_properties(self):
        target_address = "17 Harbor Pkwy"
        competitor_address = "8021 Copper Mesa Blvd"
        quantified_subjects = (
            "Both brokers' properties",
            "Each of the owners’ listings",
            "Both agents' buildings",
            "Each of the contacts’ sites",
            "Both properties' listings",
            "Each of the buildings’ spaces",
        )
        address_list_templates = (
            "{first} and {second}",
            "{first}, and {second},",
        )

        for first, second in (
            (target_address, competitor_address),
            (competitor_address, target_address),
        ):
            for reason in ("leased", "sold", "no_space_available"):
                event = {"type": "property_unavailable", "reason": reason}
                for quantified_subject in quantified_subjects:
                    for preposition in ("for", "at", "across"):
                        for address_list_template in address_list_templates:
                            address_list = address_list_template.format(
                                first=first,
                                second=second,
                            )
                            message_text = (
                                f"{quantified_subject} {preposition} {address_list} "
                                "are currently still available."
                            )
                            with self.subTest(
                                first=first,
                                reason=reason,
                                quantified_subject=quantified_subject,
                                preposition=preposition,
                                address_list_template=address_list_template,
                            ):
                                self.assertFalse(
                                    processing._property_unavailable_event_applies_to_row(
                                        event,
                                        row_anchor=f"{target_address}, Phoenix",
                                        message_text=message_text,
                                    )
                                )
                                self.assertIsNone(
                                    processing._pending_nonviable_followup_patch(
                                        [event],
                                        row_anchor=f"{target_address}, Phoenix",
                                        message_text=message_text,
                                    )
                                )

    def test_unclassified_quantified_address_subjects_fail_closed(self):
        target_address = "17 Harbor Pkwy"
        competitor_address = "8021 Copper Mesa Blvd"
        explicit_subjects = (
            (
                "senior regional industrial leasing brokerage client service "
                "account support teams"
            ),
            (
                "primary senior regional industrial leasing brokerage client "
                "service account support teams"
            ),
            (
                "primary senior regional industrial commercial leasing brokerage "
                "client service account support coordination teams"
            ),
            "tier 2 portfolio coordination teams",
            "broker/owner coordination teams",
            "unrecognized custodial representatives",
        )
        quantified_subject_templates = (
            "Both {subject}",
            "Each of the {subject}",
        )
        address_list_templates = (
            "{first} and {second}",
            "{first}, and {second},",
        )

        for first, second in (
            (target_address, competitor_address),
            (competitor_address, target_address),
        ):
            for reason in ("leased", "sold", "no_space_available"):
                event = {"type": "property_unavailable", "reason": reason}
                for subject in explicit_subjects:
                    for quantified_subject_template in quantified_subject_templates:
                        quantified_subject = quantified_subject_template.format(
                            subject=subject
                        )
                        for preposition in ("for", "at", "across"):
                            for address_list_template in address_list_templates:
                                address_list = address_list_template.format(
                                    first=first,
                                    second=second,
                                )
                                message_text = (
                                    f"{quantified_subject} {preposition} "
                                    f"{address_list} are still available."
                                )
                                with self.subTest(
                                    first=first,
                                    reason=reason,
                                    subject=subject,
                                    quantified_subject_template=(
                                        quantified_subject_template
                                    ),
                                    preposition=preposition,
                                    address_list_template=address_list_template,
                                ):
                                    patch = (
                                        processing._pending_nonviable_followup_patch(
                                            [event],
                                            row_anchor=f"{target_address}, Phoenix",
                                            message_text=message_text,
                                        )
                                    )
                                    self.assertTrue(
                                        processing._property_unavailable_event_applies_to_row(
                                            event,
                                            row_anchor=f"{target_address}, Phoenix",
                                            message_text=message_text,
                                        )
                                    )
                                    self.assertIsNotNone(patch)
                                    self.assertEqual(
                                        "stopped",
                                        patch["followUpStatus"],
                                    )
                                    self.assertEqual(
                                        reason,
                                        patch["pendingTerminalReason"],
                                    )

    def test_quantified_address_subject_tri_state_preserves_property_sets(self):
        target_address = "17 Harbor Pkwy"
        competitor_address = "8021 Copper Mesa Blvd"
        message_templates = (
            "{first} and {second} are still available.",
            "Both {first} and {second} are still available.",
            "Each of {first} and {second} remains available.",
            (
                "Both properties at {first} and {second} are currently still "
                "available."
            ),
            (
                "Both selected subject regional industrial portfolio candidate "
                "client option listing properties at {first} and {second} are "
                "currently still available."
            ),
            (
                "Each of the brokers' properties for {first} and {second} are "
                "currently still available."
            ),
        )

        for first, second in (
            (target_address, competitor_address),
            (competitor_address, target_address),
        ):
            for reason in ("leased", "sold", "no_space_available"):
                event = {"type": "property_unavailable", "reason": reason}
                for message_template in message_templates:
                    message_text = message_template.format(
                        first=first,
                        second=second,
                    )
                    with self.subTest(
                        first=first,
                        reason=reason,
                        message_template=message_template,
                    ):
                        self.assertFalse(
                            processing._property_unavailable_event_applies_to_row(
                                event,
                                row_anchor=f"{target_address}, Phoenix",
                                message_text=message_text,
                            )
                        )
                        self.assertIsNone(
                            processing._pending_nonviable_followup_patch(
                                [event],
                                row_anchor=f"{target_address}, Phoenix",
                                message_text=message_text,
                            )
                        )

    def test_near_negated_availability_never_suppresses_target_terminal_event(self):
        address_pairs = (
            ("1 Main St", "2 Oak Ave"),
            ("12 Main St", "34 Oak Ave"),
            ("123 Main St", "456 Oak Ave"),
            ("1234 Main St", "5678 Oak Ave"),
            ("12345 Main St", "56789 Oak Ave"),
            ("123456 Main St", "654321 Oak Ave"),
        )
        independent_templates = (
            "{target} is {adverb} still available.",
            "{target} {adverb} remains available.",
        )
        shared_templates = (
            "{first} and {second} are {adverb} still available.",
            "{first}, and {second}, are {adverb} still available.",
            "{first} and {second} {adverb} remain available.",
            "{first}, and {second}, {adverb} remain available.",
        )

        def assert_terminal(event, target_address, message_text):
            patch = processing._pending_nonviable_followup_patch(
                [event],
                row_anchor=f"{target_address}, Phoenix",
                message_text=message_text,
            )
            self.assertTrue(
                processing._property_unavailable_event_applies_to_row(
                    event,
                    row_anchor=f"{target_address}, Phoenix",
                    message_text=message_text,
                )
            )
            self.assertIsNotNone(patch)
            self.assertEqual("stopped", patch["followUpStatus"])
            self.assertEqual(event["reason"], patch["pendingTerminalReason"])

        for target_address, competitor_address in address_pairs:
            for reason in ("leased", "sold", "no_space_available"):
                event = {"type": "property_unavailable", "reason": reason}
                for adverb in ("hardly", "barely", "scarcely"):
                    for message_template in independent_templates:
                        message_text = message_template.format(
                            target=target_address,
                            adverb=adverb,
                        )
                        with self.subTest(
                            scope="independent",
                            target_address=target_address,
                            reason=reason,
                            adverb=adverb,
                            message_template=message_template,
                        ):
                            assert_terminal(event, target_address, message_text)

                    for first, second in (
                        (target_address, competitor_address),
                        (competitor_address, target_address),
                    ):
                        for message_template in shared_templates:
                            message_text = message_template.format(
                                first=first,
                                second=second,
                                adverb=adverb,
                            )
                            with self.subTest(
                                scope="shared",
                                target_address=target_address,
                                first=first,
                                reason=reason,
                                adverb=adverb,
                                message_template=message_template,
                            ):
                                assert_terminal(event, target_address, message_text)

    def test_affirmative_availability_adverbs_still_preserve_target(self):
        event = {"type": "property_unavailable", "reason": "leased"}
        messages = (
            "100 Main St is definitely still available.",
            "100 Main St clearly remains available.",
            "100 Main St is very much still available.",
        )

        for message_text in messages:
            with self.subTest(message_text=message_text):
                self.assertFalse(
                    processing._property_unavailable_event_applies_to_row(
                        event,
                        row_anchor="100 Main St, Phoenix",
                        message_text=message_text,
                    )
                )
                self.assertIsNone(
                    processing._pending_nonviable_followup_patch(
                        [event],
                        row_anchor="100 Main St, Phoenix",
                        message_text=message_text,
                    )
                )

    def test_shared_terminal_predicate_applies_to_target_in_address_list(self):
        message_text = "100 Main St, Phoenix, and 200 Oak Ave are both leased."
        proposal = ai_processing._augment_events_with_deterministic_signals(
            {"updates": [], "events": [], "response_email": "Thanks."},
            [{"direction": "inbound", "content": message_text}],
            target_anchor="100 Main St, Phoenix",
        )
        unavailable = [
            event
            for event in proposal["events"]
            if event.get("type") == "property_unavailable"
        ]

        self.assertEqual(["leased"], [event.get("reason") for event in unavailable])
        self.assertTrue(
            processing._property_unavailable_event_applies_to_row(
                unavailable[0],
                row_anchor="100 Main St, Phoenix",
                message_text=message_text,
            )
        )

    def test_bare_terminal_pronoun_inherits_named_competitor(self):
        event = {"type": "property_unavailable", "reason": "leased"}

        self.assertFalse(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="100 Main St, Phoenix",
                message_text=(
                    "Oak Commerce Center is the other property. It is leased."
                ),
            )
        )

    def test_bare_terminal_pronoun_inherits_explicit_target(self):
        event = {"type": "property_unavailable", "reason": "leased"}

        self.assertTrue(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="100 Main St, Phoenix",
                message_text="100 Main St is the subject. It is leased.",
            )
        )

    def test_addressless_ancillary_lease_does_not_terminalize_target_property(self):
        event = {"type": "property_unavailable", "reason": "leased"}

        for message_text in (
            "The trailer lot is leased separately.",
            "The parking lot has been leased to another tenant.",
            "The yard is already leased.",
            "The outparcel is under contract.",
            "That tour slot is no longer available.",
            "The 2:00 tour slot has been leased.",
            "At 100 Main St, the trailer lot is leased separately.",
            "100 Main St's parking lot has been leased to another tenant.",
        ):
            with self.subTest(message_text=message_text):
                self.assertFalse(
                    processing._property_unavailable_event_applies_to_row(
                        event,
                        row_anchor="100 Main St, Phoenix",
                        message_text=message_text,
                    )
                )

    def test_addressless_current_property_lease_remains_terminal(self):
        event = {"type": "property_unavailable", "reason": "leased"}

        self.assertTrue(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="100 Main St, Phoenix",
                message_text="It has been leased.",
            )
        )

    def test_explicit_target_address_lease_remains_terminal(self):
        event = {"type": "property_unavailable", "reason": "leased"}

        self.assertTrue(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="100 Main St, Phoenix",
                message_text="100 Main St has been leased.",
            )
        )

    def test_target_terminal_survives_ancillary_lease_in_same_clause(self):
        event = {"type": "property_unavailable", "reason": "leased"}
        messages = (
            "The trailer lot is leased separately, and 100 Main St has been leased too.",
            "100 Main St has been leased, and the trailer lot is leased separately.",
        )

        for message_text in messages:
            with self.subTest(message_text=message_text):
                proposal = ai_processing._augment_events_with_deterministic_signals(
                    {"updates": [], "events": [], "response_email": "Thanks."},
                    [{"direction": "inbound", "content": message_text}],
                    target_anchor="100 Main St, Phoenix",
                )
                self.assertIn(
                    "property_unavailable",
                    [event.get("type") for event in proposal["events"]],
                )
                self.assertIsNone(proposal["response_email"])
                self.assertTrue(
                    processing._property_unavailable_event_applies_to_row(
                        event,
                        row_anchor="100 Main St, Phoenix",
                        message_text=message_text,
                    )
                )

    def test_addressless_unavailable_event_preserves_explicitly_viable_target(self):
        event = {"type": "property_unavailable", "reason": "leased"}

        self.assertFalse(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="100 Main St, Phoenix",
                message_text=(
                    "200 Oak Ave is leased. "
                    "100 Main St is still available."
                ),
            )
        )

    def test_addressless_unavailable_event_binds_competing_terminal_within_same_sentence(self):
        event = {"type": "property_unavailable", "reason": "leased"}

        self.assertFalse(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="100 Main St, Phoenix",
                message_text=(
                    "200 Oak Ave is leased, but 100 Main St is still available."
                ),
            )
        )

    def test_addressless_unavailable_event_keeps_bare_target_context_terminal(self):
        event = {"type": "property_unavailable", "reason": "leased"}

        self.assertTrue(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="100 Main St, Phoenix",
                message_text="It has been leased.",
            )
        )

    def test_addressless_unavailable_event_keeps_bare_no_longer_available(self):
        event = {"type": "property_unavailable", "reason": "no_longer_available"}

        self.assertTrue(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="100 Main St, Phoenix",
                message_text="It is no longer available.",
            )
        )

    def test_addressless_requirements_mismatch_keeps_current_row_behavior(self):
        event = {"type": "property_unavailable", "reason": "requirements_mismatch"}

        self.assertTrue(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="100 Main St, Phoenix",
                message_text=(
                    "It is not the right fit because it is mostly office and "
                    "lacks warehouse space."
                ),
            )
        )

    def test_unavailable_event_with_different_address_does_not_apply_to_current_row(self):
        event = {
            "type": "property_unavailable",
            "address": "404 Replacement Signal Ave",
            "city": "Las Vegas",
        }

        self.assertFalse(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="414 Alternate Signal Ave, Las Vegas",
                message_text="404 Replacement Signal Ave is fully leased.",
            )
        )


if __name__ == "__main__":
    unittest.main()
