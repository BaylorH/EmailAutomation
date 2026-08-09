import unittest
from unittest.mock import patch

from email_automation import ai_processing, processing
from email_automation.column_config import get_default_column_config


class ProcessingCompletionGuardTests(unittest.TestCase):
    def test_close_event_defers_campaign_completion_for_pending_closing_reply(self):
        proposal = {
            "response_email": "Hi,\n\nThanks for the details.",
            "skip_response": False,
        }

        self.assertTrue(
            processing._should_defer_client_completion_for_closing_reply(proposal)
        )

    def test_close_event_does_not_defer_completion_without_a_sendable_reply(self):
        for proposal in (
            {"response_email": None},
            {"response_email": ""},
            {"response_email": "Hi,\n\nThanks.", "skip_response": True},
        ):
            with self.subTest(proposal=proposal):
                self.assertFalse(
                    processing._should_defer_client_completion_for_closing_reply(proposal)
                )

    def test_closing_copy_does_not_satisfy_missing_field_response(self):
        body = "Thanks for sending this over. This covers everything I needed."

        self.assertFalse(processing._response_mentions_missing_fields(body, ["Rail Access"]))

    def test_missing_field_response_must_reference_requested_detail(self):
        body = "Thanks for the info. Could you also confirm whether the building has rail access?"

        self.assertTrue(processing._response_mentions_missing_fields(
            body,
            ["Rail Access"],
            get_default_column_config(),
        ))

    def test_all_info_close_event_requires_complete_required_fields(self):
        event = {"type": "close_conversation", "notes": "all_info_gathered"}

        self.assertFalse(processing._close_event_can_bypass_missing_fields(event))

    def test_terminal_non_info_close_reason_can_bypass_missing_fields(self):
        event = {"type": "close_conversation", "notes": "deal_pending"}

        self.assertTrue(processing._close_event_can_bypass_missing_fields(event))

    def test_default_tour_suggested_email_uses_offered_times_without_placeholders(self):
        body = processing._build_default_tour_suggested_email(
            "Devin",
            "Tour availability offered: Monday at 2:00 PM or Wednesday at 10:00 AM.",
        )

        self.assertIn("Monday at 2:00 PM", body)
        self.assertIn("Wednesday at 10:00 AM", body)
        self.assertNotIn("[Day/Time option", body)

    def test_default_tour_suggested_email_without_times_asks_for_windows(self):
        body = processing._build_default_tour_suggested_email("Devin", "Tour requested")

        self.assertIn("what tour windows are available", body)
        self.assertNotIn("[Day/Time option", body)

    def test_tour_fallback_draft_uses_contact_name_not_recipient_local_part(self):
        body = processing._build_tour_fallback_suggested_email(
            contact_name="Drew",
            recipient_email="bp21harrison@gmail.com",
            question=(
                "Please confirm whether this tour slot works, would work on my end. "
                "If that time is no longer available, reply with the closest available alternate. "
                "(Drew offered 11:30 AM instead, or any time after 2:00 PM.)"
            ),
        )

        self.assertIn("Hi Drew,", body)
        self.assertNotIn("Bp21Harrison", body)
        self.assertNotIn("Please confirm whether this tour slot works", body)
        self.assertNotIn("reply with the closest available alternate", body)
        self.assertIn("11:30 AM", body)

    def test_tour_fallback_draft_keeps_duration_out_of_alternate_time_sentence(self):
        body = processing._build_tour_fallback_suggested_email(
            contact_name="Ron Allon",
            recipient_email="bp21harrison@gmail.com",
            question=(
                "Broker offered tour times: Tuesday, June 23 at 2:00 PM CT or "
                "Thursday, June 25 at 11:30 AM CT (about 30 minutes on site)."
            ),
        )

        self.assertIn("Tuesday, June 23 at 2:00 PM CT would work on my end.", body)
        self.assertIn(
            "If that time is no longer available, Thursday, June 25 at 11:30 AM CT could also work.",
            body,
        )
        self.assertIn("Please plan for about 30 minutes on site.", body)
        self.assertNotIn("(about 30 minutes on site could also work", body)

    def test_confirmed_tour_without_suggested_email_is_not_actionable(self):
        event = {
            "type": "tour_requested",
            "question": (
                "Monday at 2:00 PM is confirmed. Park at the main office entrance; "
                "I will meet you in the lobby. No additional access instructions."
            ),
            "suggestedEmail": "",
        }

        self.assertFalse(processing._tour_event_needs_operator_action(event))

    def test_follow_up_tour_choice_still_needs_operator_action(self):
        event = {
            "type": "tour_requested",
            "question": "Jordan offered tour times: Tuesday at 11:00 AM or Wednesday at 1:30 PM for a follow-up tour.",
            "suggestedEmail": {
                "body": "Can you pencil us in for Tuesday at 11:00 AM?",
            },
        }

        self.assertTrue(processing._tour_event_needs_operator_action(event))

    def test_tour_invite_confirmation_closes_without_operator_action(self):
        classification = processing._classify_tour_invite_reply(
            "That time works. We are confirmed for 10:47 AM.",
            event={"type": "tour_requested", "question": "Confirmed for the requested tour slot."},
            thread_data={
                "source": "dashboard_tour_planner",
                "actionType": "tour_invite",
                "tourInvite": {"arrivalTime": "10:47 AM", "departureTime": "11:17 AM"},
            },
        )

        self.assertEqual("confirmed", classification["outcome"])
        self.assertFalse(classification["needsOperatorAction"])
        self.assertTrue(classification["canCloseThread"])

    def test_tour_invite_property_specific_works_reply_closes_without_operator_action(self):
        classification = processing._classify_tour_invite_reply(
            (
                "Hi John,\n\n"
                "10:30 AM on Tuesday, June 30, 2026 works for 555 Geocoded Map Dr. "
                "Please meet me at the front office entrance.\n\n"
                "Best,\nTaylor"
            ),
            event={
                "type": "tour_requested",
                "reason": "tour_slot_reply",
                "question": "10:30 AM on Tuesday, June 30, 2026 works for 555 Geocoded Map Dr.",
            },
            thread_data={
                "source": "dashboard_tour_planner",
                "actionType": "tour_invite",
                "tourInvite": {
                    "tourDate": "2026-06-30",
                    "arrivalTime": "10:30 AM",
                    "departureTime": "11:00 AM",
                },
            },
        )

        self.assertEqual("confirmed", classification["outcome"])
        self.assertFalse(classification["needsOperatorAction"])
        self.assertTrue(classification["canCloseThread"])

    def test_tour_invite_alternate_time_requires_operator_review_not_auto_shuffle(self):
        classification = processing._classify_tour_invite_reply(
            "The 10:47 AM requested time does not work. I can do 1:30 PM instead.",
            event={"type": "tour_requested", "question": "Broker offered 1:30 PM instead."},
            thread_data={
                "source": "dashboard_tour_planner",
                "actionType": "tour_invite",
                "tourInvite": {"arrivalTime": "10:47 AM", "departureTime": "11:17 AM"},
            },
        )

        self.assertEqual("alternate_requested", classification["outcome"])
        self.assertTrue(classification["needsOperatorAction"])
        self.assertFalse(classification["canCloseThread"])
        self.assertIn("1:30 PM", classification["alternateTimes"])
        self.assertNotIn("10:47 AM", classification["alternateTimes"])
        self.assertNotIn("10:47 AM", classification["suggestedEmail"])
        self.assertNotIn("move the other tour", classification["suggestedEmail"].lower())
        self.assertNotRegex(
            classification["suggestedEmail"],
            r"(?im)^\s*(thanks|best|best regards|regards)[,!]?\s*$",
        )

    def test_dashboard_suggested_email_strips_body_signoff_before_user_signature(self):
        body = (
            "Hi Lawton,\n\n"
            "Got it -- 2:15 PM works on our end for 4402 Rex Rd.\n\n"
            "Can you confirm the date for the 2:15 PM tour, and the best address/entry point "
            "to meet you on-site?\n\n"
            "Thanks,\n"
            "BP21"
        )

        sanitized = processing._sanitize_dashboard_suggested_email_body(body)

        self.assertIn("Hi Lawton,", sanitized)
        self.assertIn("best address/entry point", sanitized)
        self.assertNotRegex(
            sanitized,
            r"(?im)^\s*(thanks|best|best regards|regards)[,!]?\s*$",
        )
        self.assertNotIn("BP21", sanitized)

    def test_dashboard_suggested_email_payload_keeps_recipients_while_cleaning_body(self):
        payload = {
            "to": ["broker@example.com"],
            "subject": "RE: 4402 Rex Rd",
            "body": "Hi Drew,\n\nThat time works.\n\nThanks,",
        }

        sanitized = processing._sanitize_dashboard_suggested_email_payload(payload)

        self.assertEqual(["broker@example.com"], sanitized["to"])
        self.assertEqual("RE: 4402 Rex Rd", sanitized["subject"])
        self.assertEqual("Hi Drew,\n\nThat time works.", sanitized["body"])

    def test_dashboard_suggested_email_keeps_real_builder_metadata(self):
        payload = processing.build_new_property_suggested_email(
            address="4402 Rex Rd",
            city="Friendswood",
            to_email="broker@example.com",
            contact_name="Drew Broker",
            referrer_name="Lawton",
            client_id="client-1",
        )

        sanitized = processing._sanitize_dashboard_suggested_email_payload(payload)

        self.assertEqual(["broker@example.com"], sanitized["to"])
        self.assertEqual("Drew Broker", sanitized["contactName"])
        self.assertEqual("client-1", sanitized["clientId"])
        self.assertIsNone(sanitized["rowNumber"])
        self.assertIn("4402 Rex Rd", sanitized["body"])
        self.assertNotRegex(
            sanitized["body"],
            r"(?im)^\s*(thanks|best|best regards|regards)[,!]?\s*$",
        )

    def test_tour_invite_alternate_reply_builds_durable_thread_state(self):
        payload = processing._build_tour_invite_reply_state_update({
            "outcome": "alternate_requested",
            "alternateTimes": ["1:30 PM"],
            "details": "Broker offered an alternate time.",
        })

        self.assertEqual("alternate_requested", payload["tourStatus"])
        self.assertEqual("alternate_requested", payload["tourInvite.status"])
        self.assertEqual(["1:30 PM"], payload["tourInvite.alternateTimes"])
        self.assertEqual("Broker offered an alternate time.", payload["tourInvite.lastReplyDetails"])
        self.assertEqual(processing.SERVER_TIMESTAMP, payload["tourInvite.rescheduleRequestedAt"])

    def test_tour_invite_alternate_reply_stores_schedule_decision(self):
        decision = {
            "feasibility": "fits",
            "arrivalTime": "2:15 PM",
            "departureTime": "2:45 PM",
            "conflicts": [],
            "suggestedOpenSlots": [],
        }

        payload = processing._build_tour_invite_reply_state_update({
            "outcome": "alternate_requested",
            "alternateTimes": ["2:15 PM"],
            "scheduleDecision": decision,
            "details": "Broker offered an alternate time.",
        })

        self.assertEqual(decision, payload["tourInvite.requestedAlternate"])

    def test_sibling_schedule_load_failure_degrades_alternate_to_needs_review(self):
        test_case = self

        class BrokenThreadsRef:
            def where(self, *, filter):
                raise RuntimeError("emulator unavailable")

        class FakeUserRef:
            def collection(self, name):
                test_case.assertEqual("threads", name)
                return BrokenThreadsRef()

        class FakeUsersCollection:
            def document(self, user_id):
                test_case.assertEqual("uid-1", user_id)
                return FakeUserRef()

        class BrokenFirestore:
            def collection(self, name):
                test_case.assertEqual("users", name)
                return FakeUsersCollection()

        thread_data = {
            "clientId": "client-1",
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
            "propertyAddress": "4402 Rex Rd",
            "tourInvite": {
                "tourDate": "2026-06-23",
                "arrivalTime": "10:00 AM",
                "departureTime": "10:30 AM",
            },
        }

        with patch.object(processing, "_fs", BrokenFirestore()):
            schedule = processing._load_sibling_tour_schedule(
                "uid-1",
                "client-1",
                "thread-current",
                thread_data,
            )

        decision = processing.evaluate_alternate_tour_time(
            schedule,
            "thread-current",
            "2:15 PM",
        )

        self.assertEqual("needs_review", decision["feasibility"])
        self.assertIn("could not be loaded", decision["reviewReason"].lower())

    def test_tour_invite_alternate_reply_uses_schedule_aware_copy_when_decision_exists(self):
        decision = {
            "feasibility": "fits",
            "arrivalTime": "2:15 PM",
            "departureTime": "2:45 PM",
            "tourDate": "2026-06-23",
            "conflicts": [],
            "suggestedOpenSlots": [],
        }

        classification = processing._classify_tour_invite_reply(
            "The 10:47 AM requested time does not work. I can do 2:15 PM instead.",
            event={"type": "tour_requested", "question": "Broker offered 2:15 PM instead."},
            thread_data={
                "source": "dashboard_tour_planner",
                "actionType": "tour_invite",
                "propertyAddress": "4402 Rex Rd",
                "tourInvite": {
                    "tourDate": "2026-06-23",
                    "arrivalTime": "10:47 AM",
                    "departureTime": "11:17 AM",
                },
            },
            contact_name="Lawton",
            recipient_email="lawton@example.com",
            schedule_decision=decision,
        )

        self.assertEqual("alternate_requested", classification["outcome"])
        self.assertIn("Tuesday, June 23, 2026 at 2:15 PM works on our end for 4402 Rex Rd.", classification["suggestedEmail"])
        self.assertIn("Please consider that confirmed.", classification["suggestedEmail"])
        self.assertNotIn("checking the route", classification["suggestedEmail"].lower())
        self.assertNotRegex(
            classification["suggestedEmail"],
            r"(?im)^\s*(thanks|best|best regards|regards)[,!]?\s*$",
        )

    def test_tour_invite_alternate_reply_uses_nested_tour_invite_address(self):
        decision = {
            "feasibility": "fits",
            "arrivalTime": "2:15 PM",
            "departureTime": "2:45 PM",
            "conflicts": [],
            "suggestedOpenSlots": [],
        }

        classification = processing._classify_tour_invite_reply(
            "The 10:47 AM requested time does not work. I can do 2:15 PM instead.",
            event={"type": "tour_requested", "question": "Broker offered 2:15 PM instead."},
            thread_data={
                "source": "dashboard_tour_planner",
                "actionType": "tour_invite",
                "subject": "Tour slot: 4402 Rex Rd at 10:47 AM",
                "tourInvite": {
                    "address": "4402 Rex Rd",
                    "arrivalTime": "10:47 AM",
                    "departureTime": "11:17 AM",
                },
            },
            contact_name="Lawton",
            recipient_email="lawton@example.com",
            schedule_decision=decision,
        )

        self.assertIn("2:15 PM works on our end for 4402 Rex Rd.", classification["suggestedEmail"])
        self.assertNotIn("Tour slot:", classification["suggestedEmail"])

    def test_tour_invite_unavailable_for_tours_stays_tour_specific(self):
        classification = processing._classify_tour_invite_reply(
            "The space is no longer available for tours.",
            event={"type": "tour_requested", "question": "Tours are no longer available."},
            thread_data={
                "source": "dashboard_tour_planner",
                "actionType": "tour_invite",
                "propertyAddress": "4402 Rex Rd",
                "tourInvite": {
                    "tourDate": "2026-06-23",
                    "arrivalTime": "10:47 AM",
                    "departureTime": "11:17 AM",
                },
            },
            contact_name="Lawton",
            recipient_email="lawton@example.com",
        )

        self.assertEqual("tour_unavailable", classification["outcome"])
        self.assertTrue(classification["needsOperatorAction"])
        self.assertFalse(classification["canCloseThread"])
        self.assertEqual("2026-06-23", classification["tourDate"])
        self.assertIn("Tours are unavailable", classification["details"])
        self.assertIn("Tuesday, June 23, 2026", classification["suggestedEmail"])
        self.assertIn("4402 Rex Rd", classification["suggestedEmail"])

    def test_tour_invite_no_tour_availability_stays_tour_specific(self):
        for message in [
            "There is no tour availability for this space right now.",
            "There is no availability for tours this week.",
            "The owner is not offering interior tours right now.",
        ]:
            with self.subTest(message=message):
                classification = processing._classify_tour_invite_reply(
                    message,
                    event={"type": "tour_requested", "question": message},
                    thread_data={
                        "source": "dashboard_tour_planner",
                        "actionType": "tour_invite",
                        "propertyAddress": "4402 Rex Rd",
                        "tourInvite": {
                            "tourDate": "2026-06-23",
                            "arrivalTime": "10:47 AM",
                            "departureTime": "11:17 AM",
                        },
                    },
                    contact_name="Lawton",
                    recipient_email="lawton@example.com",
                )

                self.assertEqual("tour_unavailable", classification["outcome"])
                self.assertTrue(classification["needsOperatorAction"])
                self.assertFalse(classification["canCloseThread"])

    def test_tour_invite_unavailable_reply_builds_durable_thread_state(self):
        payload = processing._build_tour_invite_reply_state_update({
            "outcome": "tour_unavailable",
            "alternateTimes": [],
            "details": "Tours are unavailable for this property.",
            "tourDate": "2026-06-23",
        })

        self.assertEqual("tour_unavailable", payload["tourStatus"])
        self.assertEqual("tour_unavailable", payload["tourInvite.status"])
        self.assertEqual("2026-06-23", payload["tourInvite.tourDate"])
        self.assertEqual(processing.SERVER_TIMESTAMP, payload["tourInvite.tourUnavailableAt"])

    def test_tour_invite_decline_reply_builds_durable_thread_state(self):
        payload = processing._build_tour_invite_reply_state_update({
            "outcome": "declined",
            "alternateTimes": [],
            "details": "Broker declined the requested tour slot.",
        })

        self.assertEqual("declined", payload["tourStatus"])
        self.assertEqual("declined", payload["tourInvite.status"])
        self.assertEqual([], payload["tourInvite.alternateTimes"])
        self.assertEqual(processing.SERVER_TIMESTAMP, payload["tourInvite.declinedAt"])

    def test_tour_invite_decline_reply_preserves_tour_date_in_state_and_draft(self):
        classification = processing._classify_tour_invite_reply(
            "We cannot show the space at that time anymore.",
            event={"type": "tour_requested", "question": "Broker declined the requested tour slot."},
            thread_data={
                "source": "dashboard_tour_planner",
                "actionType": "tour_invite",
                "propertyAddress": "4402 Rex Rd",
                "tourInvite": {
                    "tourDate": "2026-06-23",
                    "arrivalTime": "10:47 AM",
                    "departureTime": "11:17 AM",
                },
            },
            contact_name="Lawton",
            recipient_email="lawton@example.com",
        )

        payload = processing._build_tour_invite_reply_state_update(classification)

        self.assertEqual("declined", classification["outcome"])
        self.assertEqual("2026-06-23", classification["tourDate"])
        self.assertEqual("2026-06-23", payload["tourInvite.tourDate"])
        self.assertIn("Tuesday, June 23, 2026", classification["suggestedEmail"])

    def test_specs_and_flyer_reply_is_not_treated_as_tour_offer(self):
        message = (
            "Hi John,\n"
            "Gemini Business Park has a few options that could work. The strongest fit is "
            "4,531 SF total with one drive-in, 17' clear height, asking $10.00/SF/YR NNN "
            "plus $3.31/SF opex. Attached is a flyer with the broader park information.\n"
            "Best,\nBP21 Gemini Broker\n"
            "On Wed, Jun 17, 2026 at 9:28 PM Baylor wrote:\n"
            "Hi Ryan, please include tour availability if tours are being offered."
        )
        event = {
            "type": "tour_requested",
            "question": message,
            "suggestedEmail": "",
        }

        classification = processing._classify_tour_invite_reply(
            message,
            event=event,
            thread_data={"actionType": "campaign_creation"},
        )

        self.assertEqual("not_tour", classification["outcome"])
        self.assertFalse(processing._tour_event_needs_operator_action(event, message))
        self.assertEqual([], classification["alternateTimes"])

    def test_model_question_cannot_upgrade_passive_broker_tour_courtesy(self):
        message = "Please let me know if you need a tour."
        event = {
            "type": "tour_requested",
            "question": "Would you like to schedule a tour Tuesday at 2 PM?",
            "notes": "Broker offered a concrete Tuesday tour slot.",
            "suggestedEmail": "Tuesday at 2 PM works for us.",
        }

        classification = processing._classify_tour_invite_reply(
            message,
            event=event,
            thread_data={"actionType": "campaign_creation"},
        )

        self.assertEqual("not_tour", classification["outcome"])
        self.assertFalse(classification["needsOperatorAction"])

    def test_nonphysical_show_meet_and_virtual_language_are_not_tour_actions(self):
        messages = (
            "The flyer is showing the available suites.",
            "I can meet the asking rate.",
            "Can we see whether the numbers work?",
            "Virtual tours are available at https://example.com/virtual.",
            "A 360 tour is available in the flyer.",
            "Would you like to see the space in the attached photos?",
            "Are there any dates that work for the lease commencement?",
            "Pick a time for the pricing call.",
            "I can show you the rent schedule Tuesday.",
            "Ownership offered several lease dates.",
            "The financial model is ready. I can show it to you Tuesday.",
            "I can walk through the lease terms Tuesday.",
            "I cannot show the financial model at that time.",
            "I can't show you the rent schedule Tuesday.",
            "I am not able to show you the lease terms Tuesday.",
        )
        for message in messages:
            with self.subTest(message=message):
                classification = processing._classify_tour_invite_reply(
                    message,
                    event={
                        "type": "tour_requested",
                        "question": "Would you like to schedule a tour Tuesday at 2 PM?",
                    },
                    thread_data={"actionType": "campaign_creation"},
                )
                self.assertEqual("not_tour", classification["outcome"])
                self.assertFalse(classification["needsOperatorAction"])

    def test_non_tour_reply_subjects_cannot_confirm_or_update_tour_invite(self):
        messages = (
            "The lease commencement date works for us Tuesday.",
            "I confirmed the rent schedule at 2 PM.",
            "I could do the rent schedule tomorrow.",
            "I am available at 2 PM for a pricing call.",
            "The rent schedule is attached. Tuesday works for us.",
            "The pricing call moved. I'm available at 2 PM.",
            "The lease terms are attached. Sounds good.",
            "I cannot show the financial model at that time.",
            "I can't show you the rent schedule Tuesday.",
            "I am not able to show you the lease terms Tuesday.",
            "The pricing meeting is confirmed for Tuesday at 2 PM.",
            "Our call is confirmed for Tuesday at 2 PM.",
            "I can't show you the floor plan until Tuesday.",
            "I cannot show the cash-flow projections Tuesday.",
            "I cannot show the floor plan Tuesday; could we do Wednesday?",
            "The pricing call moved Tuesday; could we do Wednesday?",
            "The pricing call moved. Could we do Wednesday at 2 PM instead?",
            "I reviewed the property tax model. I can show it Tuesday.",
            "The floor plan covers the property. I can show it Tuesday.",
            "This one is a property tax model. I can show it Tuesday.",
            "I reviewed the tenant vacating schedule. I can show it Tuesday.",
            "We discussed the tenant move out timeline. I can show it Tuesday.",
            "You are welcome to visit the property page Tuesday.",
            "I can let your client into the property model Tuesday.",
            "We can accommodate a visit to discuss pricing Tuesday.",
            "I can provide access to the floor plan Tuesday.",
            "The rent review at that time is confirmed.",
            "The pricing call at that slot works for us.",
            "The financial model at that time is unavailable.",
            "The floor plan at that slot is confirmed.",
            "The lease schedule at that time doesn't work.",
            "Rent is confirmed at that time.",
            "Pricing does not work at that slot.",
            "Model review is unavailable at that time.",
            "Floor plan is confirmed at that slot.",
            "Lease terms work for us at that time.",
            "The tour report at that time is confirmed.",
            "We reviewed when the tenant vacates. I can show it Tuesday.",
            "We discussed when the tenant moves out. I can show it Tuesday.",
            "The schedule notes when the tenant vacates. I can show it Tuesday.",
            "The timeline records when the tenant moves out. I can show it Tuesday.",
            "We discussed the tour schedule for when the tenant moves out. I can show it Tuesday.",
            "The tour timeline notes when the tenant vacates. I can show it Tuesday.",
            "The pricing call is at 2 PM. That time works.",
            "The rent review is scheduled for Tuesday. That time is confirmed.",
            "The pricing call is confirmed. That no longer works.",
            "The lease meeting is at 10 AM. That slot is unavailable.",
            "I can provide access to the tenant schedule after the tenant moves out. I can show it Tuesday.",
            "I can let them review the floor plan after the tenant moves out. I can show it Tuesday.",
            "I can visit the pricing model once the tenant vacates. I can show it Tuesday.",
            "I can show you the property Tuesday online.",
            "I can show you the property Tuesday in the financial model.",
            "Would your client like to see the property on the listing page?",
            "Let me know if your client wants to schedule a tour Tuesday via Zoom.",
            "Let me know if your client wants to schedule a tour Tuesday online.",
            "Let me know if your client wants to schedule a tour Tuesday in the financial model.",
            "You are welcome to visit the property Tuesday to review the lease.",
            "I can let your client into the property Tuesday for the pricing call.",
            "We can accommodate a visit Tuesday to discuss pricing.",
            "I can provide access at 2 PM to the floor plan.",
            "The tour report is available Tuesday.",
        )
        for message in messages:
            with self.subTest(message=message):
                classification = processing._classify_tour_invite_reply(
                    message,
                    event={"type": "tour_requested", "question": message},
                    thread_data={
                        "source": "dashboard_tour_planner",
                        "actionType": "tour_invite",
                        "tourInvite": {
                            "tourDate": "2026-08-11",
                            "arrivalTime": "2:00 PM",
                            "departureTime": "2:30 PM",
                        },
                    },
                )
                self.assertEqual("not_tour", classification["outcome"])
                self.assertFalse(classification["needsOperatorAction"])
                self.assertFalse(classification["canCloseThread"])

    def test_only_tour_bound_clauses_can_drive_invite_outcome(self):
        cases = (
            (
                "Can I show you the property Tuesday? The rent schedule is confirmed.",
                "tour_offer_or_request",
            ),
            (
                "Tour confirmed for Tuesday at 2 PM. The rent schedule doesn't work.",
                "confirmed",
            ),
            (
                "Happy to show you the space, when works for you?",
                "tour_offer_or_request",
            ),
            (
                "Happy to show you the property if Tuesday works for you.",
                "tour_offer_or_request",
            ),
            (
                "Happy to show you the property whenever works for you.",
                "tour_offer_or_request",
            ),
            ("The Tuesday slot works for us.", "confirmed"),
            ("The 2 PM slot is confirmed.", "confirmed"),
            ("The requested tour slot works.", "confirmed"),
            (
                "Let me know if your client wants to schedule a tour Tuesday at 2 PM.",
                "tour_offer_or_request",
            ),
        )
        for message, expected_outcome in cases:
            with self.subTest(message=message):
                classification = processing._classify_tour_invite_reply(
                    message,
                    event={"type": "tour_requested", "question": message},
                    thread_data={
                        "source": "dashboard_tour_planner",
                        "actionType": "tour_invite",
                        "tourInvite": {
                            "tourDate": "2026-08-11",
                            "arrivalTime": "2:00 PM",
                            "departureTime": "2:30 PM",
                        },
                    },
                )

                self.assertEqual(expected_outcome, classification["outcome"])

    def test_physical_antecedents_direct_questions_and_mixed_virtual_copy_are_actionable(self):
        messages = (
            "The property is available, and I can show it Tuesday.",
            "The suite is ready. I can show it Tuesday.",
            "The building is open and I can walk through it Tuesday.",
            "Would your client like a tour?",
            "Do you want a tour?",
            "Does your client want a tour?",
            "Come see the property Tuesday.",
            "The property is available. You can see it Tuesday.",
            "This one is a warehouse. You can see it Tuesday.",
            "You are welcome to visit the property Tuesday.",
            "I can let your client into the property Tuesday.",
            "We can accommodate a visit Tuesday.",
            "I can provide access Tuesday.",
            "I can provide access at 2 PM.",
            "You are welcome to visit the property Tuesday with your client.",
            "We can accommodate a visit Tuesday at the property.",
            "I can provide access at 2 PM to the suite.",
            "The current tenant will vacate next month. We can show it Tuesday.",
            "The tenant moves out Friday. You can walk through it Tuesday.",
            "Virtual tours are available online or I can show the property Tuesday.",
            "Virtual tours are available online — I can show the property Tuesday.",
        )
        for message in messages:
            with self.subTest(message=message):
                classification = processing._classify_tour_invite_reply(
                    message,
                    event={"type": "tour_requested", "question": message},
                    thread_data={"actionType": "campaign_creation"},
                )

                self.assertEqual("tour_offer_or_request", classification["outcome"])
                self.assertTrue(classification["needsOperatorAction"])
                self.assertFalse(classification["canCloseThread"])

        virtual_only = processing._classify_tour_invite_reply(
            "Virtual tours are available online.",
            event={"type": "tour_requested", "question": "Virtual tours are available online."},
            thread_data={"actionType": "campaign_creation"},
        )
        self.assertEqual("not_tour", virtual_only["outcome"])

    def test_precomputed_tour_classification_is_authoritative_for_operator_action(self):
        message = "Can I show you the property Tuesday? The rent schedule is confirmed."
        event = {
            "type": "tour_requested",
            "question": message,
            "suggestedEmail": "",
        }
        thread_data = {
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
            "tourInvite": {
                "tourDate": "2026-08-11",
                "arrivalTime": "2:00 PM",
                "departureTime": "2:30 PM",
            },
        }
        classification = processing._classify_tour_invite_reply(
            message,
            event=event,
            thread_data=thread_data,
        )

        self.assertEqual("tour_offer_or_request", classification["outcome"])
        self.assertTrue(classification["needsOperatorAction"])
        self.assertTrue(processing._tour_event_needs_operator_action(
            event,
            message,
            thread_data,
            classification=classification,
        ))

    def test_fresh_bare_time_model_reason_cannot_create_tour_context(self):
        event = {
            "type": "tour_requested",
            "reason": "tour_slot_reply",
            "question": "2 PM.",
        }

        fresh = processing._classify_tour_invite_reply(
            "2 PM.",
            event=event,
            thread_data={"actionType": "campaign_creation"},
        )
        metadata_only = processing._classify_tour_invite_reply(
            "",
            event=event,
            thread_data={"actionType": "campaign_creation"},
        )

        self.assertEqual("not_tour", fresh["outcome"])
        self.assertFalse(fresh["needsOperatorAction"])
        self.assertEqual("tour_offer_or_request", metadata_only["outcome"])
        self.assertTrue(metadata_only["needsOperatorAction"])

    def test_negative_slot_without_alternate_is_declined(self):
        message = "That no longer works."
        classification = processing._classify_tour_invite_reply(
            message,
            event={"type": "tour_requested", "question": message},
            thread_data={
                "source": "dashboard_tour_planner",
                "actionType": "tour_invite",
                "tourInvite": {
                    "tourDate": "2026-08-11",
                    "arrivalTime": "2:00 PM",
                    "departureTime": "2:30 PM",
                },
            },
        )

        self.assertEqual("declined", classification["outcome"])
        self.assertTrue(classification["needsOperatorAction"])
        self.assertFalse(classification["canCloseThread"])

    def test_day_only_and_timed_cannot_show_replies_preserve_proposed_alternate(self):
        cases = (
            ("I cannot show Tuesday; could we do Wednesday?", "Wednesday"),
            ("I cannot show Tuesday; could we do Wednesday at 2 PM?", "Wednesday at 2 PM"),
            (
                "The rent schedule is attached. That time no longer works; "
                "could we do Wednesday at 2 PM for the tour?",
                "Wednesday at 2 PM",
            ),
        )
        for message, expected_alternate in cases:
            with self.subTest(message=message):
                classification = processing._classify_tour_invite_reply(
                    message,
                    event={"type": "tour_requested", "question": message},
                    thread_data={
                        "source": "dashboard_tour_planner",
                        "actionType": "tour_invite",
                        "tourInvite": {
                            "tourDate": "2026-08-11",
                            "arrivalTime": "2:00 PM",
                            "departureTime": "2:30 PM",
                        },
                    },
                )

                self.assertEqual("alternate_requested", classification["outcome"])
                self.assertIn(expected_alternate, classification["alternateTimes"])
                self.assertNotEqual("tour_unavailable", classification["outcome"])

    def test_subject_bound_tour_positive_controls_remain_stable(self):
        cases = (
            ("Tour confirmed for Tuesday at 2 PM.", "confirmed"),
            ("That time doesn't work; could we do 3 PM instead?", "alternate_requested"),
            ("No tours till further notice.", "tour_unavailable"),
            ("I'm available at 2 PM.", "tour_offer_or_request"),
        )
        for message, expected_outcome in cases:
            with self.subTest(message=message):
                classification = processing._classify_tour_invite_reply(
                    message,
                    event={"type": "tour_requested", "question": message},
                    thread_data={
                        "source": "dashboard_tour_planner",
                        "actionType": "tour_invite",
                        "tourInvite": {
                            "tourDate": "2026-08-11",
                            "arrivalTime": "2:00 PM",
                            "departureTime": "2:30 PM",
                        },
                    },
                )
                self.assertEqual(expected_outcome, classification["outcome"])

    def test_tour_classifier_uses_event_metadata_only_when_broker_text_is_absent(self):
        classification = processing._classify_tour_invite_reply(
            "",
            event={
                "type": "tour_requested",
                "question": "Can your client tour Tuesday at 2 PM?",
            },
            thread_data={"actionType": "campaign_creation"},
        )

        self.assertEqual("tour_offer_or_request", classification["outcome"])
        self.assertTrue(classification["needsOperatorAction"])

    def test_confirmed_tour_with_courtesy_directions_still_closes_invite(self):
        classification = processing._classify_tour_invite_reply(
            "Tuesday at 2 PM is confirmed. Let me know if you need directions for the tour.",
            event={
                "type": "tour_requested",
                "question": "Tuesday at 2 PM is confirmed.",
            },
            thread_data={
                "source": "dashboard_tour_planner",
                "actionType": "tour_invite",
                "tourInvite": {
                    "tourDate": "2026-08-11",
                    "arrivalTime": "2:00 PM",
                    "departureTime": "2:30 PM",
                },
            },
        )

        self.assertEqual("confirmed", classification["outcome"])
        self.assertFalse(classification["needsOperatorAction"])
        self.assertTrue(classification["canCloseThread"])

    def test_short_confirmations_remain_valid_in_established_tour_context(self):
        for message in ("Sounds good.", "We're confirmed.", "See you then."):
            with self.subTest(message=message):
                classification = processing._classify_tour_invite_reply(
                    message,
                    event={"type": "tour_requested", "question": message},
                    thread_data={
                        "source": "dashboard_tour_planner",
                        "actionType": "tour_invite",
                        "tourInvite": {
                            "tourDate": "2026-08-11",
                            "arrivalTime": "2:00 PM",
                            "departureTime": "2:30 PM",
                        },
                    },
                )

                self.assertEqual("confirmed", classification["outcome"])
                self.assertFalse(classification["needsOperatorAction"])
                self.assertTrue(classification["canCloseThread"])

    def test_broker_tours_are_available_next_week_requires_operator_action(self):
        message = (
            "Hi Baylor,\n\n"
            "0 Gemini Ave is available as a 6,000 SF industrial/flex space. "
            "I can share a flyer/floor plan, and tours are available next week.\n\n"
            "Best,\nBP21"
        )
        event = {
            "type": "tour_requested",
            "question": "Broker indicated tours are available next week.",
            "suggestedEmail": "",
        }

        classification = processing._classify_tour_invite_reply(
            message,
            event=event,
            thread_data={"actionType": "campaign_creation"},
        )

        self.assertEqual("tour_offer_or_request", classification["outcome"])
        self.assertTrue(processing._tour_event_needs_operator_action(event, message))

    def test_completion_cleanup_deletes_thread_action_notifications(self):
        class FakeReference:
            def __init__(self):
                self.deleted = False

            def delete(self):
                self.deleted = True

        class FakeDoc:
            def __init__(self):
                self.id = "notification-1"
                self.reference = FakeReference()

        class FakeNotificationsRef:
            def __init__(self, docs):
                self.docs = docs
                self.filters = []

            def where(self, *, filter):
                self.filters.append(filter)
                return self

            def stream(self):
                return self.docs

        stale_action = FakeDoc()
        notifications_ref = FakeNotificationsRef([stale_action])

        with patch.object(processing, "delete_notification_and_decrement_counters") as delete_notification:
            deleted = processing._clear_thread_action_notifications(
                "uid-1",
                "client-1",
                "thread-1",
                notifications_ref=notifications_ref,
            )

        self.assertEqual(1, deleted)
        delete_notification.assert_called_once_with("uid-1", "client-1", "notification-1")
        self.assertFalse(stale_action.reference.deleted)
        self.assertEqual(2, len(notifications_ref.filters))

    def test_operator_manual_continuation_clears_action_and_resumes_paused_thread(self):
        thread_data = {
            "status": processing.THREAD_STATUS["paused"],
            "statusReason": "needs_user_input:confidential",
            "statusUpdatedAt": "2026-07-06T10:00:00Z",
            "clientId": "client-9",
        }
        msg = {"conversationId": "conv-123"}
        continuation = {"id": "sent-1", "conversationId": "conv-123"}

        with patch.object(
            processing,
            "find_sent_conversation_continuation_for_retry",
            return_value=continuation,
        ) as find_continuation, patch.object(
            processing, "_clear_thread_action_notifications", return_value=1
        ) as clear_action, patch.object(
            processing, "update_thread_status", return_value=True
        ) as update_status:
            resumed = processing._resume_paused_thread_after_manual_continuation(
                "uid-9",
                {"Authorization": "Bearer x"},
                "thread-9",
                thread_data,
                msg,
            )

        self.assertTrue(resumed)
        find_continuation.assert_called_once()
        _, kwargs = find_continuation.call_args
        self.assertEqual("conv-123", kwargs.get("conversation_id"))
        clear_action.assert_called_once_with("uid-9", "client-9", "thread-9")
        update_status.assert_called_once_with(
            "uid-9",
            "thread-9",
            processing.THREAD_STATUS["active"],
            "manual_continuation_resumed",
        )

    def test_no_manual_continuation_leaves_paused_thread_untouched(self):
        thread_data = {
            "status": processing.THREAD_STATUS["paused"],
            "statusUpdatedAt": "2026-07-06T10:00:00Z",
            "clientId": "client-9",
        }
        msg = {"conversationId": "conv-123"}

        with patch.object(
            processing,
            "find_sent_conversation_continuation_for_retry",
            return_value=None,
        ), patch.object(
            processing, "_clear_thread_action_notifications", return_value=0
        ) as clear_action, patch.object(
            processing, "update_thread_status", return_value=True
        ) as update_status:
            resumed = processing._resume_paused_thread_after_manual_continuation(
                "uid-9",
                {"Authorization": "Bearer x"},
                "thread-9",
                thread_data,
                msg,
            )

        self.assertFalse(resumed)
        clear_action.assert_not_called()
        update_status.assert_not_called()

    def test_manual_continuation_guard_unreadable_does_not_resume(self):
        thread_data = {
            "status": processing.THREAD_STATUS["paused"],
            "statusUpdatedAt": "2026-07-06T10:00:00Z",
            "clientId": "client-9",
        }
        msg = {"conversationId": "conv-123"}

        with patch.object(
            processing,
            "find_sent_conversation_continuation_for_retry",
            side_effect=processing.SentMailGuardLookupError("Sent Items unreadable"),
        ), patch.object(
            processing, "_clear_thread_action_notifications", return_value=0
        ) as clear_action, patch.object(
            processing, "update_thread_status", return_value=True
        ) as update_status:
            resumed = processing._resume_paused_thread_after_manual_continuation(
                "uid-9",
                {"Authorization": "Bearer x"},
                "thread-9",
                thread_data,
                msg,
            )

        self.assertFalse(resumed)
        clear_action.assert_not_called()
        update_status.assert_not_called()

    def test_active_thread_is_not_probed_for_manual_continuation(self):
        thread_data = {
            "status": processing.THREAD_STATUS["active"],
            "clientId": "client-9",
        }
        msg = {"conversationId": "conv-123"}

        with patch.object(
            processing, "find_sent_conversation_continuation_for_retry"
        ) as find_continuation, patch.object(
            processing, "_clear_thread_action_notifications", return_value=0
        ) as clear_action, patch.object(
            processing, "update_thread_status", return_value=True
        ) as update_status:
            resumed = processing._resume_paused_thread_after_manual_continuation(
                "uid-9",
                {"Authorization": "Bearer x"},
                "thread-9",
                thread_data,
                msg,
            )

        self.assertFalse(resumed)
        find_continuation.assert_not_called()
        clear_action.assert_not_called()
        update_status.assert_not_called()

    def test_marks_client_completed_when_all_threads_terminal_and_no_current_work(self):
        class FakeDocSnapshot:
            def __init__(self, data=None, exists=True):
                self._data = dict(data or {})
                self.exists = exists

            def to_dict(self):
                return dict(self._data)

        class FakeDoc:
            def __init__(self, doc_id, data=None, exists=True):
                self.id = doc_id
                self._data = dict(data or {})
                self._exists = exists
                self.set_calls = []

            def to_dict(self):
                return dict(self._data)

            def get(self):
                return FakeDocSnapshot(self._data, self._exists)

            def set(self, payload, merge=False):
                self.set_calls.append((payload, merge))
                self._data.update(payload)

        class FakeQuery:
            def __init__(self, docs):
                self.docs = list(docs)
                self.filters = []

            def where(self, *, filter):
                self.filters.append(filter)
                return self

            def stream(self):
                docs = self.docs
                for field_filter in self.filters:
                    field = field_filter.field_path
                    value = field_filter.value
                    docs = [doc for doc in docs if doc.to_dict().get(field) == value]
                return docs

        client_ref = FakeDoc("client-1", {"status": "live"})
        threads_ref = FakeQuery([
            FakeDoc("thread-1", {"clientId": "client-1", "status": "completed"}),
            FakeDoc("thread-2", {"clientId": "client-1", "status": "stopped"}),
            FakeDoc("other-thread", {"clientId": "client-2", "status": "active"}),
        ])
        notifications_ref = FakeQuery([])
        outbox_ref = FakeQuery([])

        completed = processing._maybe_mark_client_completed(
            "uid-1",
            "client-1",
            client_ref=client_ref,
            threads_ref=threads_ref,
            notifications_ref=notifications_ref,
            outbox_ref=outbox_ref,
            pending_responses_ref=FakeQuery([]),
            dead_letter_ref=FakeQuery([]),
        )

        self.assertTrue(completed)
        self.assertEqual("completed", client_ref._data["status"])
        self.assertEqual(
            {
                "terminalThreads": 2,
                "activeThreads": 0,
                "pendingOutbox": 0,
                "pendingResponses": 0,
                "unresolvedDeadLetters": 0,
                "currentActions": 0,
            },
            client_ref._data["completionSummary"],
        )
        self.assertTrue(client_ref.set_calls[-1][1])

    def test_does_not_mark_client_completed_when_any_thread_is_active(self):
        class FakeDoc:
            def __init__(self, doc_id, data=None):
                self.id = doc_id
                self._data = dict(data or {})
                self.set_calls = []

            def to_dict(self):
                return dict(self._data)

            def get(self):
                class Snapshot:
                    exists = True

                    def to_dict(inner_self):
                        return dict(self._data)
                return Snapshot()

            def set(self, payload, merge=False):
                self.set_calls.append((payload, merge))
                self._data.update(payload)

        class FakeQuery:
            def __init__(self, docs):
                self.docs = list(docs)
                self.filters = []

            def where(self, *, filter):
                self.filters.append(filter)
                return self

            def stream(self):
                docs = self.docs
                for field_filter in self.filters:
                    docs = [
                        doc for doc in docs
                        if doc.to_dict().get(field_filter.field_path) == field_filter.value
                    ]
                return docs

        client_ref = FakeDoc("client-1", {"status": "live"})
        completed = processing._maybe_mark_client_completed(
            "uid-1",
            "client-1",
            client_ref=client_ref,
            threads_ref=FakeQuery([
                FakeDoc("thread-1", {"clientId": "client-1", "status": "completed"}),
                FakeDoc("thread-2", {"clientId": "client-1", "status": "active"}),
            ]),
            notifications_ref=FakeQuery([]),
            outbox_ref=FakeQuery([]),
            pending_responses_ref=FakeQuery([]),
            dead_letter_ref=FakeQuery([]),
        )

        self.assertFalse(completed)
        self.assertEqual([], client_ref.set_calls)

    def test_does_not_overwrite_stopped_client_as_completed(self):
        class FakeDoc:
            def __init__(self, doc_id, data=None):
                self.id = doc_id
                self._data = dict(data or {})
                self.set_calls = []

            def to_dict(self):
                return dict(self._data)

            def get(self):
                class Snapshot:
                    exists = True

                    def to_dict(inner_self):
                        return dict(self._data)
                return Snapshot()

            def set(self, payload, merge=False):
                self.set_calls.append((payload, merge))
                self._data.update(payload)

        class FakeQuery:
            def __init__(self, docs):
                self.docs = list(docs)
                self.filters = []

            def where(self, *, filter):
                self.filters.append(filter)
                return self

            def stream(self):
                docs = self.docs
                for field_filter in self.filters:
                    docs = [
                        doc for doc in docs
                        if doc.to_dict().get(field_filter.field_path) == field_filter.value
                    ]
                return docs

        client_ref = FakeDoc("client-1", {"status": "stopped"})
        completed = processing._maybe_mark_client_completed(
            "uid-1",
            "client-1",
            client_ref=client_ref,
            threads_ref=FakeQuery([
                FakeDoc("thread-1", {"clientId": "client-1", "status": "completed"}),
            ]),
            notifications_ref=FakeQuery([]),
            outbox_ref=FakeQuery([]),
            pending_responses_ref=FakeQuery([]),
            dead_letter_ref=FakeQuery([]),
        )

        self.assertFalse(completed)
        self.assertEqual([], client_ref.set_calls)

    def test_current_work_blocks_completion_but_terminal_dead_letter_history_does_not(self):
        class FakeDoc:
            def __init__(self, doc_id, data=None):
                self.id = doc_id
                self._data = dict(data or {})
                self.set_calls = []

            def to_dict(self):
                return dict(self._data)

            def get(self):
                class Snapshot:
                    exists = True

                    def to_dict(inner_self):
                        return dict(self._data)
                return Snapshot()

            def set(self, payload, merge=False):
                self.set_calls.append((payload, merge))
                self._data.update(payload)

        class FakeQuery:
            def __init__(self, docs):
                self.docs = list(docs)
                self.filters = []

            def where(self, *, filter):
                self.filters.append(filter)
                return self

            def stream(self):
                docs = self.docs
                for field_filter in self.filters:
                    docs = [
                        doc for doc in docs
                        if doc.to_dict().get(field_filter.field_path) == field_filter.value
                    ]
                return docs

        terminal_threads = FakeQuery([
            FakeDoc("thread-1", {"clientId": "client-1", "status": "completed"}),
            FakeDoc("thread-2", {"clientId": "client-1", "status": "stopped"}),
        ])

        with_action = FakeDoc("client-1", {"status": "live"})
        self.assertFalse(processing._maybe_mark_client_completed(
            "uid-1",
            "client-1",
            client_ref=with_action,
            threads_ref=terminal_threads,
            notifications_ref=FakeQuery([
                FakeDoc("action-1", {"kind": "action_needed", "threadId": "thread-2"}),
            ]),
            outbox_ref=FakeQuery([]),
            pending_responses_ref=FakeQuery([]),
            dead_letter_ref=FakeQuery([]),
        ))
        self.assertEqual([], with_action.set_calls)

        with_outbox = FakeDoc("client-1", {"status": "live"})
        self.assertFalse(processing._maybe_mark_client_completed(
            "uid-1",
            "client-1",
            client_ref=with_outbox,
            threads_ref=terminal_threads,
            notifications_ref=FakeQuery([]),
            outbox_ref=FakeQuery([
                FakeDoc("outbox-1", {"clientId": "client-1", "status": "queued"}),
            ]),
            pending_responses_ref=FakeQuery([]),
            dead_letter_ref=FakeQuery([]),
        ))
        self.assertEqual([], with_outbox.set_calls)

        with_pending_reply = FakeDoc("client-1", {"status": "live"})
        self.assertFalse(processing._maybe_mark_client_completed(
            "uid-1",
            "client-1",
            client_ref=with_pending_reply,
            threads_ref=terminal_threads,
            notifications_ref=FakeQuery([]),
            outbox_ref=FakeQuery([]),
            pending_responses_ref=FakeQuery([
                FakeDoc("pending-1", {"clientId": "client-1", "status": "queued"}),
            ]),
            dead_letter_ref=FakeQuery([]),
        ))
        self.assertEqual([], with_pending_reply.set_calls)

        with_reconciliation = FakeDoc("client-1", {"status": "live"})
        self.assertFalse(processing._maybe_mark_client_completed(
            "uid-1",
            "client-1",
            client_ref=with_reconciliation,
            threads_ref=terminal_threads,
            notifications_ref=FakeQuery([]),
            outbox_ref=FakeQuery([]),
            pending_responses_ref=FakeQuery([]),
            dead_letter_ref=FakeQuery([
                FakeDoc("dead-1", {
                    "clientId": "client-1",
                    "status": "needs_reconciliation",
                    "alreadySent": True,
                }),
            ]),
        ))
        self.assertEqual([], with_reconciliation.set_calls)

        with_resolved_reconciliation = FakeDoc("client-1", {"status": "live"})
        self.assertTrue(processing._maybe_mark_client_completed(
            "uid-1",
            "client-1",
            client_ref=with_resolved_reconciliation,
            threads_ref=terminal_threads,
            notifications_ref=FakeQuery([]),
            outbox_ref=FakeQuery([]),
            pending_responses_ref=FakeQuery([]),
            dead_letter_ref=FakeQuery([
                FakeDoc("dead-1", {
                    "clientId": "client-1",
                    "status": "reconciled",
                    "alreadySent": True,
                }),
            ]),
        ))
        self.assertEqual("completed", with_resolved_reconciliation._data["status"])

        with_campaign_stopped_history = FakeDoc("client-1", {"status": "live"})
        self.assertTrue(processing._maybe_mark_client_completed(
            "uid-1",
            "client-1",
            client_ref=with_campaign_stopped_history,
            threads_ref=terminal_threads,
            notifications_ref=FakeQuery([]),
            outbox_ref=FakeQuery([]),
            pending_responses_ref=FakeQuery([]),
            dead_letter_ref=FakeQuery([
                FakeDoc("dead-1", {
                    "clientId": "client-1",
                    "status": "dead_lettered",
                    "recoveryStatus": "campaign_stopped",
                }),
            ]),
        ))
        self.assertEqual("completed", with_campaign_stopped_history._data["status"])

        with_nonretryable_history = FakeDoc("client-1", {"status": "live"})
        self.assertTrue(processing._maybe_mark_client_completed(
            "uid-1",
            "client-1",
            client_ref=with_nonretryable_history,
            threads_ref=terminal_threads,
            notifications_ref=FakeQuery([]),
            outbox_ref=FakeQuery([]),
            pending_responses_ref=FakeQuery([]),
            dead_letter_ref=FakeQuery([
                FakeDoc("dead-1", {
                    "clientId": "client-1",
                    "status": "dead_lettered",
                    "retryable": False,
                }),
            ]),
        ))
        self.assertEqual("completed", with_nonretryable_history._data["status"])

    def test_deterministic_rent_fallback_extracts_asking_rent_not_nnn(self):
        value = ai_processing._extract_rent_sf_yr_from_text(
            "Asking $9.00/SF/year, NNN $0.39/SF, power is 200 amps."
        )

        self.assertEqual(value, "9.00")

    def test_deterministic_rent_fallback_keeps_figure_first_rent_before_opex(self):
        value = ai_processing._extract_rent_sf_yr_from_text(
            "Forwarding the owner's confirmed current specs: 42,500 SF, "
            "$12.75/SF/year asking rent, $3.95/SF operating expenses."
        )

        self.assertEqual(value, "12.75")

    def test_deterministic_rent_fallback_annualizes_monthly_asking_rent(self):
        value = ai_processing._extract_rent_sf_yr_from_text(
            "Asking rate: $1.25/SF/month NNN."
        )

        self.assertEqual(value, "15.00")

    def test_deterministic_rent_fallback_annualizes_per_square_foot_per_month(self):
        value = ai_processing._extract_rent_sf_yr_from_text(
            "Base rent is $0.95 per square foot per month plus operating expenses."
        )

        self.assertEqual(value, "11.40")

    def test_deterministic_rent_fallback_annualizes_nnn_monthly_suffix(self):
        value = ai_processing._extract_rent_sf_yr_from_text(
            "Asking rent: $1.12/SF NNN monthly."
        )

        self.assertEqual(value, "13.44")

    def test_deterministic_rent_fallback_does_not_treat_next_month_as_monthly_rent(self):
        value = ai_processing._extract_rent_sf_yr_from_text(
            "Asking rent: $9.00/SF NNN, available next month."
        )

        self.assertEqual(value, "9.00")

    def test_deterministic_rent_fallback_augments_blank_rent_cell(self):
        header = ["Property Address", "Rent/SF /Yr", "Ops Ex /SF"]
        proposal = {"updates": [{"column": "Ops Ex /SF", "value": "0.39"}]}
        rowvals = ["3100 Sirius Ave", "", ""]
        config = {"mappings": {"rent_sf_yr": "Rent/SF /Yr"}}
        conversation = [{
            "direction": "inbound",
            "content": "Asking $9.00/SF/year, NNN $0.39/SF.",
        }]

        augmented = ai_processing._augment_proposal_with_deterministic_extractions(
            proposal, rowvals, header, config, conversation
        )

        self.assertIn(
            {"column": "Rent/SF /Yr", "value": "9.00", "confidence": 0.92,
             "reason": "Deterministic fallback parsed asking rent per SF per year from the latest broker message."},
            augmented["updates"],
        )

    def test_deterministic_rent_fallback_corrects_existing_monthly_llm_update(self):
        header = ["Property Address", "Rent/SF /Yr", "Ops Ex /SF"]
        proposal = {
            "updates": [
                {"column": "Rent/SF /Yr", "value": "1.12", "confidence": 0.92, "reason": "LLM copied monthly rent"},
                {"column": "Ops Ex /SF", "value": "3.24"},
            ]
        }
        rowvals = ["414 Alternate Signal Pkwy", "", ""]
        config = {"mappings": {"rent_sf_yr": "Rent/SF /Yr"}}
        conversation = [{
            "direction": "inbound",
            "content": "Asking rent: $1.12/SF NNN monthly. Ops Ex / NNN: $0.27/SF monthly.",
        }]

        augmented = ai_processing._augment_proposal_with_deterministic_extractions(
            proposal, rowvals, header, config, conversation
        )

        self.assertIn(
            {"column": "Rent/SF /Yr", "value": "13.44", "confidence": 0.92,
             "reason": "Deterministic fallback parsed asking rent per SF per year from the latest broker message."},
            augmented["updates"],
        )


if __name__ == "__main__":
    unittest.main()
