"""A conversation the product has already resolved must never be asked for specs.

LIVE BREAK (2026-08-21, reproduced in production). A broker wrote that a
building had gone under lease and that he had nothing comparable. The product
answered by thanking him for details he never gave and asking him to confirm
the total square footage, asking rent, operating expenses, drive-in doors,
dock-high doors, clear height and electrical service -- of a building that is
off the market. It is the only broker-VISIBLE defect of the six that run found,
and it fires on one of the most common replies in the business.

Internally the product got it completely right: it marked the conversation
finished, recorded the reason as a natural end, and stopped follow-ups. THE
PART THAT DECIDES WHAT TO SAY NEVER CONSULTED THE PART THAT WORKED OUT THE
PROPERTY WAS GONE.

The wiring, precisely. `old_row_became_nonviable` is a RECEIPT FOR A GOOGLE
SHEETS ROW-MOVE. It is set in exactly three places, each the success tail of a
sheet mutation. The reply lane then reads it as though it answered "is this
property still alive?" -- and because the missing-fields lane is gated on its
NEGATION, every route that recognises the property is gone but skips the sheet
write lands in the specification request BY DEFAULT.

Suppressing a model draft cannot fix this. The missing-fields copy is composed
DETERMINISTICALLY and discards the draft outright; the send decision itself has
to hold. That is the same conclusion `_deferral_holds_missing_fields_reply`
reached after three byte-identical requests went out in fourteen minutes, and
it is reached here again by a different road.

WHY THE WHOLE SUITE MISSED IT: grep the tests for `old_row_became_nonviable`
and nothing drives a case where a resolved row leaves that flag False. Every
pre-existing non-viable runtime test happens to take a path that sets it. Two
adversarial audits missed it too. So each test below is named for the ROUTE it
drives, and the routes are the point.

WHICH OF THESE ACTUALLY FAILED BEFORE THE FIX -- stated because a test that
passes either way is not a test, and quietly shipping three of them as though
they proved something would be the same class of mistake as the defect:

  REPRODUCTIONS (red before, green after), verified by running this file
  against the unfixed tree:
    - test_natural_end_close_never_asks_a_dead_building_for_its_specification
    - test_a_draft_asking_a_dead_building_for_specs_is_replaced (both scenarios)

  GUARDS (green either way). These lanes already behave correctly; they are
  pinned because the fix re-routes them through a new predicate and a
  regression there would otherwise be silent:
    - test_already_handled_unavailable_event_still_blocks_the_specification_request
    - test_row_already_below_divider_with_failing_stop_still_sends_no_specification_request
    - test_a_leased_building_is_never_told_that_gives_me_everything_i_need
    - test_an_ordinary_acknowledgement_draft_still_goes_out (the over-fire guard)
"""
import unittest

from tests import test_compound_nonviable_processing as _compound

FakeDocumentRef = _compound.FakeDocumentRef


class _BorrowedHarness(unittest.TestCase):
    """Borrow the runtime harness WITHOUT re-collecting the suite it lives in.

    Two things are deliberate. Subclassing that TestCase would inherit all
    seventy-odd of its cases into this module, and so would importing the class
    NAME into this namespace -- pytest collects any TestCase subclass bound at
    module level, however it got there. Either way the same suite runs twice
    under two names, which is the test-collection poisoning this repo has
    already paid for once. Reaching through the module keeps the harness and
    leaves that suite running exactly once.

    The borrowed instance still needs its own setUp: that is where the campaign
    automation gate is patched, and without it every run dies fail-closed on
    `client_automation_state_malformed` long before any reply is composed.
    """

    def run_reply_processing(self, **kwargs):
        harness = _compound.CompoundNonviableProcessingTests(
            "_run_tour_invite_reply_processing"
        )
        harness.setUp()
        self.addCleanup(harness.tearDown)
        return harness._run_tour_invite_reply_processing(**kwargs)

# The exact fields the deterministic missing-fields body asks for. If any of
# these words reaches a broker whose conversation is already resolved, this is
# the live defect happening again.
SPEC_REQUEST_MARKERS = (
    "square footage",
    "asking rent",
    "operating expenses",
    "drive-in",
    "dock",
    "clear height",
    "ceiling",
    "power",
    "electrical",
)

# The opening line of the missing-fields body. Thanking a broker for details he
# did not give is half of what made the live reply embarrassing.
FALSE_THANKS = "Thanks for the details."


class ResolvedRowReplyGateTests(_BorrowedHarness):
    """Every route that resolves a row must reach the same reply verdict."""

    def assert_not_a_specification_request(self, sent_body, route):
        self.assertIsNotNone(sent_body, f"{route}: nothing was sent at all")
        lowered = sent_body.lower()
        for marker in SPEC_REQUEST_MARKERS:
            self.assertNotIn(
                marker,
                lowered,
                f"{route}: asked a resolved conversation for {marker!r}.\n"
                f"--- body sent ---\n{sent_body}",
            )
        self.assertNotIn(
            FALSE_THANKS.lower(),
            lowered,
            f"{route}: thanked the broker for details he did not give.\n"
            f"--- body sent ---\n{sent_body}",
        )

    def _sent_body(self, result):
        send = result["sendReply"]
        if not send.call_args:
            return None
        return send.call_args.args[2]

    def _thread(self, **overrides):
        data = {
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "contactName": "Marcus",
            "status": "active",
            "rowNumber": 3,
        }
        data.update(overrides)
        return FakeDocumentRef(data)

    # ------------------------------------------------------------------
    # ROUTE 6 -- the live defect, byte for byte.
    # ------------------------------------------------------------------
    def test_natural_end_close_never_asks_a_dead_building_for_its_specification(self):
        """THE production reply. A close_conversation event with reason
        `natural_end` bypasses the missing-fields veto, stops follow-ups and
        records the close -- and never touches the sheet-move receipt. So the
        reply lane defaults into the deterministic specification request and
        overrides the perfectly good closing note the model wrote.
        """
        body = (
            "Hi Baylor,\n\nUnfortunately I'm going to have to bow out on this "
            "one - the space went under lease last Thursday and the paperwork "
            "is done. It's off the market.\n\nI wish I had something comparable "
            "but our portfolio in that submarket is full at the moment.\n\n"
            "Sorry not to be more help.\n\nMarcus"
        )
        proposal = {
            "updates": [],
            "events": [{"type": "close_conversation", "reason": "natural_end"}],
            "response_email": (
                "Hi Marcus,\n\nThanks for letting me know, and no problem at "
                "all. If anything comparable comes up in that submarket, I'd "
                "appreciate a heads up."
            ),
        }

        result = self.run_reply_processing(
            thread_id="thread-natural-end-dead-building",
            body=body,
            proposal=proposal,
            thread_ref=self._thread(),
            row_anchor="2250 Valwood Parkway",
            contact_name="Marcus",
            missing_required_fields=["Ops Ex / SF", "Total SF"],
        )

        self.assert_not_a_specification_request(
            self._sent_body(result), "route 6 (close_conversation natural_end)"
        )

    # ------------------------------------------------------------------
    # ROUTE 2 -- the event was already stamped handled on an earlier pass.
    # ------------------------------------------------------------------
    def test_already_handled_unavailable_event_still_blocks_the_specification_request(self):
        """A broker restating that the building is leased must not be punished
        for repeating himself.

        The per-thread event dedupe `continue`s out of the loop BEFORE the sheet
        work, so the receipt stays False on this pass even though the row is
        long dead. The handled marker is seeded directly rather than by running
        a first pass: a real first pass moves the row and re-points the thread's
        rowNumber, so a second pass in the same fake store cannot find its own
        thread root and dies in terminal staging -- an artefact of the harness,
        not of the route under test.
        """
        from email_automation import processing

        body = (
            "Baylor - as I mentioned, 818 Ridgepoint is leased and off the "
            "market. Nothing else to add.\n\nMarcus"
        )
        event = {
            "type": "property_unavailable",
            "address": "818 Ridgepoint Road",
            "reason": "property_unavailable",
        }
        thread_id = "thread-already-handled-unavailable"
        event_key = processing.build_event_key(
            "property_unavailable",
            event,
            thread_id=thread_id,
            row_anchor="818 Ridgepoint Road",
        )
        thread_ref = self._thread(
            handledEvents={event_key: {"detectedInMessageId": "msg-earlier"}}
        )

        result = self.run_reply_processing(
            thread_id=thread_id,
            body=body,
            proposal={"updates": [], "events": [event], "response_email": None},
            thread_ref=thread_ref,
            thread_docs={thread_id: thread_ref},
            row_anchor="818 Ridgepoint Road",
            contact_name="Marcus",
            persist_handled_events=True,
            missing_required_fields=["Ops Ex / SF"],
        )

        # GUARD, not a reproduction: this lane already reaches the right copy
        # today, verified by running it against the unfixed tree. It is pinned
        # because the fix re-routes it through a different predicate, and a
        # lane that silently stopped acknowledging would be invisible.
        self.assertEqual(
            1,
            result["sendReply"].call_count,
            "a broker restating that the building is leased got no answer",
        )
        sent = self._sent_body(result)
        self.assertIn("no longer available", sent.lower())
        self.assert_not_a_specification_request(
            sent, "route 2 (event already handled)"
        )

    # ------------------------------------------------------------------
    # ROUTE 3 -- the row is already below the divider and the inner try fails.
    # ------------------------------------------------------------------
    def test_row_already_below_divider_with_failing_stop_still_sends_no_specification_request(self):
        """When the row already sits below NON-VIABLE the handler terminalizes
        without moving the sheet. If anything inside that block raises, the
        surrounding `except` swallows it BEFORE the receipt is set -- and the
        specification request goes out on a row the sheet already marks dead.
        """
        body = (
            "Hi Baylor,\n\n3900 Silverleaf has been leased - it came off the "
            "market a couple of weeks ago.\n\nMarcus"
        )
        proposal = {
            "updates": [],
            "events": [{
                "type": "property_unavailable",
                "address": "3900 Silverleaf Boulevard",
                "reason": "property_unavailable",
            }],
            "response_email": None,
        }

        result = self.run_reply_processing(
            thread_id="thread-below-divider-failing-stop",
            body=body,
            proposal=proposal,
            thread_ref=self._thread(),
            row_anchor="3900 Silverleaf Boulevard",
            contact_name="Marcus",
            row_below_nonviable=True,
            missing_required_fields=["Ops Ex / SF"],
        )

        # GUARD, not a reproduction. Today this lane sets skip_response before
        # the composer runs, so nothing is sent at all -- confirmed against the
        # unfixed tree. Pinned exactly, because "sends nothing" and "sends a
        # specification request" are the two outcomes that matter here and only
        # an exact assertion tells them apart.
        self.assertEqual(
            0,
            result["sendReply"].call_count,
            "a row already below the divider answered anyway:\n"
            + str(self._sent_body(result)),
        )

    # ------------------------------------------------------------------
    # Scenario 4 -- the closing reply is just as wrong on a dead building.
    # ------------------------------------------------------------------
    def test_a_leased_building_is_never_told_that_gives_me_everything_i_need(self):
        """The completion copy is the other half of the same gate. Telling a
        broker whose building is gone that we now have everything we need reads
        exactly as badly as asking him for its dock doors.
        """
        body = (
            "Hi Baylor,\n\n617 Harbourfield went under lease on Friday. "
            "Off the market now.\n\nPriya"
        )
        proposal = {
            "updates": [],
            "events": [{
                "type": "property_unavailable",
                "address": "617 Harbourfield Lane",
                "reason": "property_unavailable",
            }],
            "response_email": None,
        }

        result = self.run_reply_processing(
            thread_id="thread-complete-on-dead-building",
            body=body,
            proposal=proposal,
            thread_ref=self._thread(contactName="Priya"),
            row_anchor="617 Harbourfield Lane",
            contact_name="Priya",
            row_below_nonviable=True,
            missing_required_fields=[],
        )

        # GUARD, not a reproduction, on the same basis as route 3.
        self.assertEqual(
            0,
            result["sendReply"].call_count,
            "a leased building's broker was answered anyway:\n"
            + str(self._sent_body(result)),
        )


    def test_an_opted_out_contact_is_sent_nothing_at_all(self):
        """Silence is the only correct answer to "remove me from your list".

        The opt-out handler suppresses the reply itself, but it does so at the
        END of a try block whose handler only prints -- so one Firestore blip
        loses the suppression and control reaches the reply lane. Before the
        gate that meant a specification request; a gate that merely knows the
        row is resolved would send the non-viable acknowledgement instead and
        ask a man who asked to be removed whether he has other properties.
        Neither is acceptable, so this asserts NOTHING goes out.
        """
        body = (
            "Please remove me from your list and don't contact me again.\n\nMarcus"
        )
        proposal = {
            "updates": [],
            "events": [{"type": "contact_optout", "reason": "unsubscribe"}],
            "response_email": "Hi Marcus,\n\nUnderstood, thanks.",
        }

        result = self.run_reply_processing(
            thread_id="thread-optout-suppression-lost",
            body=body,
            proposal=proposal,
            thread_ref=self._thread(),
            row_anchor="55 Ambergate Way",
            contact_name="Marcus",
            missing_required_fields=["Ops Ex / SF"],
            notification_error=RuntimeError("firestore unavailable"),
        )

        self.assertEqual(
            0,
            result["sendReply"].call_count,
            "answered a contact who asked to be removed:\n"
            + str(self._sent_body(result)),
        )


class ResolvedScenarioModelDraftTests(unittest.TestCase):
    """A model draft must not smuggle a specification request past the gate.

    `_response_requests_nonrequestable_fields` only guards Note/Skip/formula
    columns, so a draft that politely asks a dead building for its square
    footage is returned VERBATIM for the non-viable scenarios. Closing the
    deterministic hole while leaving this one open would move the defect rather
    than fix it.
    """

    def setUp(self):
        from email_automation import processing
        from email_automation.column_config import get_default_column_config

        self.processing = processing
        # The real shape. A hand-rolled {"columns": {...}} dict is REJECTED by
        # get_column_config_error, and response_requests_nonrequestable_fields
        # fails closed on a rejected config -- so every draft reads as blocked
        # and the test proves nothing about the guard.
        self.column_config = get_default_column_config()

    def test_a_draft_asking_a_dead_building_for_specs_is_replaced(self):
        draft = (
            "Hi Marcus,\n\nSorry to hear that. Before I close this out, could "
            "you send over the total square footage and the clear height for "
            "my records?"
        )
        for scenario in ("nonviable", "nonviable_with_alternative"):
            with self.subTest(scenario=scenario):
                body = self.processing._select_automatic_response_body(
                    scenario,
                    draft,
                    self.column_config,
                    "Marcus",
                )
                self.assertNotEqual(
                    body,
                    draft,
                    f"{scenario}: a model draft asked a dead building for its "
                    "square footage and went out verbatim",
                )
                lowered = body.lower()
                self.assertNotIn("square footage", lowered)
                self.assertNotIn("clear height", lowered)

    def test_an_ordinary_acknowledgement_draft_still_goes_out(self):
        """The gate must not flatten every non-viable reply into boilerplate.

        Without this, 'block drafts on resolved rows' quietly becomes 'never
        use a draft again', and the product loses the natural voice that the
        deterministic copy cannot produce.
        """
        draft = (
            "Hi Marcus,\n\nUnderstood, and thanks for the quick reply. If "
            "anything comparable comes up in that submarket, I'd appreciate a "
            "heads up."
        )
        body = self.processing._select_automatic_response_body(
            "nonviable",
            draft,
            self.column_config,
            "Marcus",
        )
        self.assertEqual(body, draft)


if __name__ == "__main__":
    unittest.main()
