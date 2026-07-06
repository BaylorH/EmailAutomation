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

    def test_existing_unavailable_event_is_not_duplicated(self):
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
        self.assertEqual("model", augmented["events"][0]["reason"])

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
