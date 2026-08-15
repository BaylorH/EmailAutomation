"""Task 5: versioned manual replies never enter the broad outbox drain."""

import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "service-account.json",
    ),
)

from email_automation import email as email_module
from tests.test_outbox_safety import (
    FakeDoc,
    FakeDocRef,
    FakeFirestore,
    FakeFirestoreWithOutbox,
)


def immediate_transactional(callback):
    def run(transaction, *args):
        return callback(transaction, *args)

    return run


class ManualReplyGenericDrainTests(unittest.TestCase):
    def _legacy_data(self, **overrides):
        data = {
            "assignedEmails": ["broker@example.invalid"],
            "script": "Synthetic reviewed message",
            "clientId": "client-1",
            "rowNumber": 3,
            "attempts": 0,
            "source": "dashboard_inline_reply",
            "actionType": "reply",
        }
        data.update(overrides)
        return data

    def _run_drain(self, docs, *, single_result=None):
        fake_fs = FakeFirestoreWithOutbox(
            docs,
            user_data={"email": "sender@example.invalid"},
        )
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                "SITESIFT_DAILY_SEND_CAP": "20",
                "SITESIFT_GLOBAL_DAILY_SEND_CAP": "20",
            }))
            stack.enter_context(patch("email_automation.clients._fs", fake_fs))
            cancel = stack.enter_context(patch.object(
                email_module, "_delete_cancelled_outbox_item_if_needed",
                return_value=False,
            ))
            single = stack.enter_context(patch.object(
                email_module, "_send_single_outbox_item",
                return_value=single_result,
            ))
            multi = stack.enter_context(patch.object(
                email_module, "_send_multi_property_email",
            ))
            combined = stack.enter_context(patch.object(
                email_module, "_send_combined_property_email",
            ))
            dead_letter = stack.enter_context(patch.object(
                email_module, "_move_to_dead_letter",
            ))
            read_user_cap = stack.enter_context(patch.object(
                email_module, "_read_daily_send_count", return_value=0,
            ))
            read_global_cap = stack.enter_context(patch.object(
                email_module, "_read_global_send_count", return_value=0,
            ))
            increment_user_cap = stack.enter_context(patch.object(
                email_module, "_increment_daily_send_count",
            ))
            increment_global_cap = stack.enter_context(patch.object(
                email_module, "_increment_global_send_count",
            ))
            stack.enter_context(patch.object(email_module.time, "sleep", return_value=None))
            email_module.send_outboxes(
                "synthetic-user",
                {"Authorization": "Bearer synthetic"},
            )

        return {
            "cancel": cancel,
            "single": single,
            "multi": multi,
            "combined": combined,
            "dead_letter": dead_letter,
            "read_user_cap": read_user_cap,
            "read_global_cap": read_global_cap,
            "increment_user_cap": increment_user_cap,
            "increment_global_cap": increment_global_cap,
        }

    def _assert_zero_generic_effect(self, result):
        result["cancel"].assert_not_called()
        result["single"].assert_not_called()
        result["multi"].assert_not_called()
        result["combined"].assert_not_called()
        result["dead_letter"].assert_not_called()
        result["read_user_cap"].assert_not_called()
        result["read_global_cap"].assert_not_called()
        result["increment_user_cap"].assert_not_called()
        result["increment_global_cap"].assert_not_called()

    def test_any_present_marker_is_invisible_before_cancel_grouping_and_caps(self):
        for marker in (1, 0, 2, "1", None, False, {}, []):
            with self.subTest(marker=marker):
                doc = FakeDoc(
                    self._legacy_data(manualReplyLaneVersion=marker),
                    doc_id="manual-item",
                )
                result = self._run_drain([doc])
                self._assert_zero_generic_effect(result)
                self.assertFalse(doc.reference.deleted)
                self.assertEqual([], doc.reference.set_calls)
                self.assertEqual([], doc.reference.update_calls)

    def test_current_marker_wins_over_stale_discovery_snapshot(self):
        stale = self._legacy_data(attempts=email_module.MAX_OUTBOX_ATTEMPTS)
        doc = FakeDoc(stale, doc_id="stale-discovery")
        doc.reference._data = {**stale, "manualReplyLaneVersion": 1}

        result = self._run_drain([doc])

        self._assert_zero_generic_effect(result)
        self.assertFalse(doc.reference.deleted)

    def test_missing_marker_preserves_the_legacy_route(self):
        doc = FakeDoc(self._legacy_data(), doc_id="legacy-item")

        result = self._run_drain([doc])

        result["single"].assert_called_once()

    def test_marker_skip_return_consumes_zero_planned_caps(self):
        doc = FakeDoc(self._legacy_data(), doc_id="late-marker-cap")

        result = self._run_drain(
            [doc],
            single_result={"manualReplyLaneSkipped": 1},
        )

        result["single"].assert_called_once()
        result["increment_user_cap"].assert_not_called()
        result["increment_global_cap"].assert_not_called()

    def test_claim_transaction_never_mutates_a_marked_item(self):
        data = self._legacy_data(manualReplyLaneVersion=1)
        ref = FakeDocRef("manual-claim", dict(data))
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), patch(
            "google.cloud.firestore.transactional",
            immediate_transactional,
        ), patch.object(
            email_module, "_terminalize_outbox_action_audit",
        ) as terminalize:
            claimed = email_module._claim_outbox_item(
                ref,
                data,
                user_id="synthetic-user",
            )

        self.assertFalse(claimed)
        self.assertFalse(ref.deleted)
        self.assertEqual([], ref.set_calls)
        self.assertEqual([], ref.update_calls)
        terminalize.assert_not_called()

    def test_claim_retry_uses_only_the_committed_marker_outcome(self):
        cancelled = self._legacy_data(
            cancelRequested=True,
            status="cancel_requested",
        )
        marked = self._legacy_data(manualReplyLaneVersion=1)
        ref = FakeDocRef("claim-retry", dict(cancelled))
        fake_fs = FakeFirestore()

        def retry_transactional(callback):
            def run(transaction, doc_ref):
                doc_ref.deleted = False
                doc_ref._data = dict(cancelled)
                callback(transaction, doc_ref)
                doc_ref.deleted = False
                doc_ref._data = dict(marked)
                return callback(transaction, doc_ref)

            return run

        with patch("email_automation.clients._fs", fake_fs), patch(
            "google.cloud.firestore.transactional",
            retry_transactional,
        ), patch.object(
            email_module, "_terminalize_outbox_action_audit",
        ) as terminalize:
            claimed = email_module._claim_outbox_item(
                ref,
                cancelled,
                user_id="synthetic-user",
            )

        self.assertFalse(claimed)
        self.assertFalse(ref.deleted)
        self.assertEqual(marked, ref._data)
        terminalize.assert_not_called()

    def test_non_ready_all_row_gate_releases_surviving_current_worker_claims(self):
        for blocked_status in ("claim_lost", "gone"):
            with self.subTest(blocked_status=blocked_status):
                ours = FakeDocRef("ours", {
                    **self._legacy_data(),
                    "processingBy": email_module.WORKER_ID,
                    "processingAt": "stale-current-worker-claim",
                })
                other = FakeDocRef("other", {
                    **self._legacy_data(),
                    "processingBy": "another-worker",
                    "processingAt": "other-worker-claim",
                })
                if blocked_status == "gone":
                    other.deleted = True

                fake_fs = FakeFirestore()
                with patch("email_automation.clients._fs", fake_fs), patch.object(
                    email_module.firestore,
                    "transactional",
                    immediate_transactional,
                ):
                    result = email_module._gate_generic_provider_unit([ours, other])

                self.assertEqual(blocked_status, result["status"])
                self.assertIsNone(ours._data.get("processingBy"))
                self.assertIsNone(ours._data.get("processingAt"))
                if blocked_status == "claim_lost":
                    self.assertEqual("another-worker", other._data["processingBy"])
                    self.assertEqual("other-worker-claim", other._data["processingAt"])

    def test_generic_cancel_rereads_and_preserves_current_marker(self):
        stale_cancel = self._legacy_data(
            cancelRequested=True,
            status="cancel_requested",
        )
        ref = FakeDocRef(
            "cancel-race",
            {**stale_cancel, "manualReplyLaneVersion": 1},
        )

        with patch.object(
            email_module, "_terminalize_outbox_action_audit",
        ) as terminalize:
            deleted = email_module._delete_cancelled_outbox_item_if_needed(
                ref,
                stale_cancel,
                user_id="synthetic-user",
            )

        self.assertFalse(deleted)
        self.assertFalse(ref.deleted)
        terminalize.assert_not_called()

    def test_single_final_gate_stops_marker_inserted_after_claim(self):
        data = self._legacy_data()
        doc = FakeDoc(data, doc_id="single-final-gate")
        fake_fs = FakeFirestore()
        real_gate = email_module._gate_generic_provider_unit

        def claim_current(*_args, **_kwargs):
            doc.reference._data["processingBy"] = email_module.WORKER_ID
            return True

        def mark_from_retry_guard(*_args, **_kwargs):
            doc.reference._data["manualReplyLaneVersion"] = 1
            return {"sent": []}

        with patch("email_automation.clients._fs", fake_fs), patch.object(
            email_module, "_claim_outbox_item", side_effect=claim_current,
        ), patch.object(
            email_module, "_pause_results_outbox_item_if_needed", return_value=False,
        ), patch.object(
            email_module, "_pause_client_outbox_item_if_needed", return_value=False,
        ), patch.object(
            email_module, "_has_existing_thread_for_property", return_value=False,
        ), patch.object(
            email_module,
            "_dead_letter_campaign_recipient_row_mismatch_if_needed",
            return_value=False,
        ), patch.object(
            email_module,
            "_dead_letter_unresolved_name_placeholder_if_needed",
            return_value=False,
        ), patch.object(
            email_module,
            "_dead_letter_invalid_initial_outreach_column_contract_if_needed",
            return_value=False,
        ), patch.object(
            email_module, "_dead_letter_unsafe_outbound_body_if_needed", return_value=False,
        ), patch.object(
            email_module, "_select_script_for_recipient", return_value=data["script"],
        ), patch.object(
            email_module,
            "_sent_retry_reconciliation_result",
            side_effect=mark_from_retry_guard,
        ), patch.object(
            email_module, "_gate_generic_provider_unit", wraps=real_gate,
        ) as gate_spy, patch.object(
            email_module, "_release_claim", wraps=email_module._release_claim,
        ), patch.object(
            email_module, "send_and_index_email",
        ) as graph_send:
            result = email_module._send_single_outbox_item(
                "synthetic-user",
                {"Authorization": "Bearer synthetic"},
                {"doc": doc, "data": data},
            )

        self.assertEqual({"manualReplyLaneSkipped": 1}, result)
        self.assertEqual(2, gate_spy.call_count)
        graph_send.assert_not_called()
        self.assertIsNone(doc.reference._data.get("processingBy"))

    def _assert_thread_provider_variant_stops_at_second_gate(self, reply_sender):
        data = self._legacy_data(
            subject="RE: Synthetic property",
            threadId="thread-1",
            replyToMessageId="graph-message-1",
            conversationId="conversation-1",
            notificationId="notification-1",
            actionAuditId="audit-1",
        )
        doc = FakeDoc(data, doc_id="thread-final-gate")
        fake_fs = FakeFirestore()
        real_gate = email_module._gate_generic_provider_unit

        def claim_current(*_args, **_kwargs):
            doc.reference._data["processingBy"] = email_module.WORKER_ID
            doc.reference._data["processingAt"] = "synthetic-claim"
            return True

        def mark_from_retry_guard(*_args, **_kwargs):
            doc.reference._data["manualReplyLaneVersion"] = 1
            return {"sent": []}

        with patch("email_automation.clients._fs", fake_fs), patch.object(
            email_module, "_claim_outbox_item", side_effect=claim_current,
        ), patch.object(
            email_module, "_pause_results_outbox_item_if_needed", return_value=False,
        ), patch.object(
            email_module, "_pause_client_outbox_item_if_needed", return_value=False,
        ), patch.object(
            email_module,
            "_validate_outbox_thread_reply_target",
            return_value={
                "ok": True,
                "reason": None,
                "thread": {"clientId": "client-1", "status": "paused", "rowNumber": 3},
            },
        ), patch.object(
            email_module, "_should_preflight_sent_items_retry", return_value=False,
        ), patch.object(
            email_module,
            "_reserve_dashboard_tour_action_resolution",
            return_value={"status": "generic"},
        ), patch.object(
            email_module, "_dead_letter_unsafe_outbound_body_if_needed", return_value=False,
        ), patch.object(
            email_module, "_get_reply_message_sender", return_value=reply_sender,
        ), patch.object(
            email_module,
            "_sent_retry_reconciliation_result",
            side_effect=mark_from_retry_guard,
        ), patch.object(
            email_module, "_gate_generic_provider_unit", wraps=real_gate,
        ) as gate_spy, patch.object(
            email_module, "_send_outbox_as_reply",
        ) as graph_reply, patch.object(
            email_module, "send_and_index_email",
        ) as graph_send:
            result = email_module._send_single_outbox_item(
                "synthetic-user",
                {"Authorization": "Bearer synthetic"},
                {"doc": doc, "data": data},
            )

        self.assertEqual({"manualReplyLaneSkipped": 1}, result)
        self.assertEqual(2, gate_spy.call_count)
        graph_reply.assert_not_called()
        graph_send.assert_not_called()
        self.assertIsNone(doc.reference._data.get("processingBy"))
        self.assertIsNone(doc.reference._data.get("processingAt"))

    def test_graph_reply_variant_stops_at_second_gate(self):
        self._assert_thread_provider_variant_stops_at_second_gate(
            "broker@example.invalid"
        )

    def test_redirected_thread_variant_stops_at_second_gate(self):
        self._assert_thread_provider_variant_stops_at_second_gate(
            "different-sender@example.invalid"
        )

    def test_grouped_final_gate_aborts_on_any_late_marker(self):
        for sender_name, expected_skip in (
            ("_send_multi_property_email", 1),
            ("_send_combined_property_email", 1),
        ):
            with self.subTest(sender=sender_name):
                data = self._legacy_data(subject="Synthetic property")
                doc = FakeDoc(data, doc_id=f"{sender_name}-final-gate")
                fake_fs = FakeFirestore()
                real_gate = email_module._gate_generic_provider_unit

                def claim_current(*_args, **_kwargs):
                    doc.reference._data["processingBy"] = email_module.WORKER_ID
                    return True

                def mark_from_retry_guard(*_args, **_kwargs):
                    doc.reference._data["manualReplyLaneVersion"] = 1
                    return {"sent": []}

                with patch("email_automation.clients._fs", fake_fs), patch(
                    "email_automation.processing.is_contact_opted_out",
                    return_value=None,
                ), patch.object(
                    email_module, "_claim_outbox_item", side_effect=claim_current,
                ), patch.object(
                    email_module, "_pause_client_outbox_item_if_needed", return_value=False,
                ), patch.object(
                    email_module,
                    "_dead_letter_campaign_recipient_row_mismatch_if_needed",
                    return_value=False,
                ), patch.object(
                    email_module, "_has_existing_thread_for_property", return_value=False,
                ), patch.object(
                    email_module,
                    "_sent_retry_reconciliation_result",
                    side_effect=mark_from_retry_guard,
                ), patch.object(
                    email_module, "_gate_generic_provider_unit", wraps=real_gate,
                ) as gate_spy, patch.object(
                    email_module,
                    "_dead_letter_unresolved_name_placeholder_if_needed",
                    return_value=False,
                ), patch.object(
                    email_module,
                    "_dead_letter_invalid_initial_outreach_column_contract_if_needed",
                    return_value=False,
                ), patch.object(
                    email_module, "_dead_letter_unsafe_outbound_body_if_needed", return_value=False,
                ), patch.object(
                    email_module, "send_and_index_email",
                ) as graph_send:
                    result = getattr(email_module, sender_name)(
                        "synthetic-user",
                        {"Authorization": "Bearer synthetic"},
                        "broker@example.invalid",
                        [{"doc": doc, "data": data, "email": "broker@example.invalid"}],
                    )

                self.assertEqual(
                    {"manualReplyLaneSkipped": expected_skip},
                    result,
                )
                self.assertEqual(2, gate_spy.call_count)
                graph_send.assert_not_called()
                self.assertIsNone(doc.reference._data.get("processingBy"))

    def test_combined_final_gate_rejects_all_rows_when_one_late_marker_wins(self):
        first_data = self._legacy_data(
            subject="First synthetic property",
            rowNumber=3,
            sendMode="combined",
        )
        second_data = self._legacy_data(
            subject="Second synthetic property",
            rowNumber=4,
            sendMode="combined",
        )
        first = FakeDoc(first_data, doc_id="combined-first")
        second = FakeDoc(second_data, doc_id="combined-second")
        fake_fs = FakeFirestore()
        real_gate = email_module._gate_generic_provider_unit

        def claim_current(ref, *_args, **_kwargs):
            ref._data["processingBy"] = email_module.WORKER_ID
            ref._data["processingAt"] = "synthetic-claim"
            return True

        def mark_second_from_retry_guard(*_args, **_kwargs):
            second.reference._data["manualReplyLaneVersion"] = 1
            return {"sent": []}

        with patch("email_automation.clients._fs", fake_fs), patch(
            "email_automation.processing.is_contact_opted_out",
            return_value=None,
        ), patch.object(
            email_module, "_claim_outbox_item", side_effect=claim_current,
        ), patch.object(
            email_module, "_pause_client_outbox_item_if_needed", return_value=False,
        ), patch.object(
            email_module,
            "_dead_letter_campaign_recipient_row_mismatch_if_needed",
            return_value=False,
        ), patch.object(
            email_module, "_has_existing_thread_for_property", return_value=False,
        ), patch.object(
            email_module,
            "_dead_letter_unresolved_name_placeholder_if_needed",
            return_value=False,
        ), patch.object(
            email_module,
            "_dead_letter_invalid_initial_outreach_column_contract_if_needed",
            return_value=False,
        ), patch.object(
            email_module, "_dead_letter_unsafe_outbound_body_if_needed", return_value=False,
        ), patch.object(
            email_module,
            "_sent_retry_reconciliation_result",
            side_effect=mark_second_from_retry_guard,
        ), patch.object(
            email_module, "_gate_generic_provider_unit", wraps=real_gate,
        ) as gate_spy, patch.object(
            email_module, "send_and_index_email",
        ) as graph_send:
            result = email_module._send_combined_property_email(
                "synthetic-user",
                {"Authorization": "Bearer synthetic"},
                "broker@example.invalid",
                [
                    {"doc": first, "data": first_data},
                    {"doc": second, "data": second_data},
                ],
            )

        self.assertEqual({"manualReplyLaneSkipped": 1}, result)
        self.assertEqual(3, gate_spy.call_count)
        self.assertEqual(
            [first.reference, second.reference],
            gate_spy.call_args_list[-1].args[0],
        )
        graph_send.assert_not_called()
        for doc in (first, second):
            self.assertIsNone(doc.reference._data.get("processingBy"))
            self.assertIsNone(doc.reference._data.get("processingAt"))

    def test_multi_final_gate_returns_full_unsent_plan_and_leaves_later_row_unclaimed(self):
        first_data = self._legacy_data(subject="First grouped property", rowNumber=3)
        second_data = self._legacy_data(subject="Second grouped property", rowNumber=4)
        first = FakeDoc(first_data, doc_id="multi-first")
        second = FakeDoc(second_data, doc_id="multi-second")
        fake_fs = FakeFirestore()
        real_gate = email_module._gate_generic_provider_unit

        def claim_current(ref, *_args, **_kwargs):
            ref._data["processingBy"] = email_module.WORKER_ID
            ref._data["processingAt"] = "synthetic-claim"
            return True

        def mark_first_from_retry_guard(*_args, **_kwargs):
            first.reference._data["manualReplyLaneVersion"] = 1
            return {"sent": []}

        with patch("email_automation.clients._fs", fake_fs), patch(
            "email_automation.processing.is_contact_opted_out",
            return_value=None,
        ), patch.object(
            email_module, "_claim_outbox_item", side_effect=claim_current,
        ), patch.object(
            email_module, "_pause_client_outbox_item_if_needed", return_value=False,
        ), patch.object(
            email_module,
            "_dead_letter_campaign_recipient_row_mismatch_if_needed",
            return_value=False,
        ), patch.object(
            email_module, "_has_existing_thread_for_property", return_value=False,
        ), patch.object(
            email_module,
            "_dead_letter_unresolved_name_placeholder_if_needed",
            return_value=False,
        ), patch.object(
            email_module,
            "_dead_letter_invalid_initial_outreach_column_contract_if_needed",
            return_value=False,
        ), patch.object(
            email_module, "_dead_letter_unsafe_outbound_body_if_needed", return_value=False,
        ), patch.object(
            email_module,
            "_sent_retry_reconciliation_result",
            side_effect=mark_first_from_retry_guard,
        ), patch.object(
            email_module, "_gate_generic_provider_unit", wraps=real_gate,
        ) as gate_spy, patch.object(
            email_module, "send_and_index_email",
        ) as graph_send:
            result = email_module._send_multi_property_email(
                "synthetic-user",
                {"Authorization": "Bearer synthetic"},
                "broker@example.invalid",
                [
                    {"doc": first, "data": first_data},
                    {"doc": second, "data": second_data},
                ],
            )

        self.assertEqual({"manualReplyLaneSkipped": 2}, result)
        self.assertEqual(2, gate_spy.call_count)
        graph_send.assert_not_called()
        self.assertIsNone(first.reference._data.get("processingBy"))
        self.assertIsNone(first.reference._data.get("processingAt"))
        self.assertNotIn("processingBy", second.reference._data)
        self.assertNotIn("processingAt", second.reference._data)


if __name__ == "__main__":
    unittest.main()
