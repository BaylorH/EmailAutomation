import unittest

from email_automation import ai_processing, processing


class ProcessingCompletionGuardTests(unittest.TestCase):
    def test_closing_copy_does_not_satisfy_missing_field_response(self):
        body = "Thanks for sending this over. This covers everything I needed."

        self.assertFalse(processing._response_mentions_missing_fields(body, ["Rail Access"]))

    def test_missing_field_response_must_reference_requested_detail(self):
        body = "Thanks for the info. Could you also confirm whether the building has rail access?"

        self.assertTrue(processing._response_mentions_missing_fields(body, ["Rail Access"]))

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

    def test_completion_cleanup_deletes_thread_action_notifications(self):
        class FakeReference:
            def __init__(self):
                self.deleted = False

            def delete(self):
                self.deleted = True

        class FakeDoc:
            def __init__(self):
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

        deleted = processing._clear_thread_action_notifications(
            "uid-1",
            "client-1",
            "thread-1",
            notifications_ref=notifications_ref,
        )

        self.assertEqual(1, deleted)
        self.assertTrue(stale_action.reference.deleted)
        self.assertEqual(2, len(notifications_ref.filters))

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
        )

        self.assertTrue(completed)
        self.assertEqual("completed", client_ref._data["status"])
        self.assertEqual(
            {
                "terminalThreads": 2,
                "activeThreads": 0,
                "pendingOutbox": 0,
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
        )

        self.assertFalse(completed)
        self.assertEqual([], client_ref.set_calls)

    def test_does_not_mark_client_completed_with_current_action_or_pending_outbox(self):
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
        ))
        self.assertEqual([], with_outbox.set_calls)

    def test_deterministic_rent_fallback_extracts_asking_rent_not_nnn(self):
        value = ai_processing._extract_rent_sf_yr_from_text(
            "Asking $9.00/SF/year, NNN $0.39/SF, power is 200 amps."
        )

        self.assertEqual(value, "9.00")

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
