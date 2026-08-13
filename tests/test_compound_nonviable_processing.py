import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import ai_processing, campaign_safety, email as email_module, processing


class FakeSnapshot:
    def __init__(self, data=None, exists=True):
        self._data = data or {}
        self.exists = exists

    def to_dict(self):
        return dict(self._data)


class FakeDocumentRef:
    def __init__(self, data=None, exists=True, update_error=None):
        self._data = data or {}
        self._exists = exists
        self._update_error = update_error
        self._collections = {}

    def get(self, transaction=None):
        return FakeSnapshot(self._data, self._exists)

    def set(self, data, merge=False):
        if merge:
            self._data.update(data)
        else:
            self._data = dict(data)
        self._exists = True

    def update(self, data):
        if self._update_error:
            raise self._update_error
        self._data.update(data)

    def delete(self):
        self._exists = False

    def collection(self, name):
        return self._collections.get(
            name,
            FakeCollection(FakeDocumentRef({}, exists=False)),
        )


class FakeQuerySnapshot(FakeSnapshot):
    def __init__(self, doc_id, data=None, exists=True):
        super().__init__(data, exists)
        self.id = doc_id


class FakeQuery:
    def __init__(self, docs):
        self.docs = docs or {}

    def stream(self):
        return [
            FakeQuerySnapshot(doc_id, doc_ref._data, doc_ref._exists)
            for doc_id, doc_ref in self.docs.items()
        ]


class FakeUserRef:
    def __init__(self, thread_ref, client_ref, thread_docs=None):
        self.thread_ref = thread_ref
        self.client_ref = client_ref
        self.thread_docs = thread_docs or {}

    def collection(self, name):
        if name == "threads":
            return FakeCollection(self.thread_ref, docs=self.thread_docs)
        if name == "clients":
            return FakeCollection(self.client_ref)
        if name == "actionResolutions":
            return self.client_ref._collections.get(
                "actionResolutions",
                FakeCollection(FakeDocumentRef({}, exists=False)),
            )
        return FakeCollection(FakeDocumentRef({}, exists=False))


class FakeCollection:
    def __init__(self, doc_ref, docs=None):
        self.doc_ref = doc_ref
        self.docs = docs or {}

    def document(self, *args):
        doc_id = str(args[0]) if args else ""
        if doc_id and doc_id in self.docs:
            return self.docs[doc_id]
        return self.doc_ref

    def where(self, *args, **kwargs):
        return FakeQuery(self.docs)

    def stream(self):
        return FakeQuery(self.docs).stream()


class FakeWriteBatch:
    def __init__(self):
        self._updates = []

    def update(self, document_ref, data):
        self._updates.append((document_ref, data))

    def commit(self):
        for document_ref, _data in self._updates:
            if document_ref._update_error:
                raise document_ref._update_error
        for document_ref, data in self._updates:
            document_ref.update(data)


class FakeTransaction:
    def __init__(self):
        self._max_attempts = 1
        self._read_only = False
        self._id = b"fake-transaction"

    def _clean_up(self):
        return None

    def _begin(self, retry_id=None):
        self._id = retry_id or b"fake-transaction"

    def _commit(self):
        return None

    def _rollback(self):
        return None

    def set(self, ref, data, merge=False):
        ref.set(data, merge=merge)

    def create(self, ref, data):
        if ref.get().exists:
            raise AssertionError("document already exists")
        ref.set(data)

    def update(self, ref, data):
        ref.update(data)

    def delete(self, ref):
        ref.delete()


class FakeFirestore:
    def __init__(self, thread_ref, client_ref, thread_docs=None):
        self.thread_ref = thread_ref
        self.client_ref = client_ref
        self.thread_docs = thread_docs or {}
        self.client_ref._data.update({
            "status": "live",
            "automationPaused": False,
        })

    def collection(self, name):
        if name == "users":
            return FakeCollection(FakeUserRef(self.thread_ref, self.client_ref, self.thread_docs))
        if name == "systemConfig":
            return FakeCollection(FakeDocumentRef({
                "automationEnabled": True,
                "allowedUids": [],
            }))
        return FakeCollection(FakeDocumentRef({}, exists=False))

    def batch(self):
        return FakeWriteBatch()

    def transaction(self):
        return FakeTransaction()


class CompoundNonviableProcessingTests(unittest.TestCase):
    def setUp(self):
        self._campaign_gate = patch.object(
            processing,
            "get_client_automation_decision",
            side_effect=lambda user_id, client_id: campaign_safety.get_client_automation_decision(
                user_id,
                client_id,
                firestore_client=processing._fs,
            ),
        )
        self._campaign_gate.start()

    def tearDown(self):
        self._campaign_gate.stop()

    def _common_graph_message(self, *, msg_id, subject, from_email, body, internet_message_id, conversation_id):
        return {
            "id": msg_id,
            "subject": subject,
            "from": {"emailAddress": {"address": from_email, "name": "BP21"}},
            "toRecipients": [{"emailAddress": {"address": "baylor.freelance@outlook.com"}}],
            "internetMessageId": internet_message_id,
            "conversationId": conversation_id,
            "receivedDateTime": "2026-06-19T19:12:39Z",
            "bodyPreview": body[:200],
            "hasAttachments": False,
            "internetMessageHeaders": [
                {"name": "In-Reply-To", "value": "<tour-invite@mock.test>"},
            ],
        }

    def _run_tour_invite_reply_processing(
        self,
        *,
        thread_id,
        body,
        proposal,
        thread_ref,
        user_id="NO7lVYVp6BaplKYEfMlWCgBnpdh2",
        thread_docs=None,
        row_anchor="912-930 Gemini St",
        rownum=3,
        property_name=None,
        contact_name="Ryan",
        from_email="bp21harrison@gmail.com",
        row_below_nonviable=False,
        ensure_divider_side_effect=None,
        cancel_followup_side_effect=None,
        msg_id=None,
        internet_message_id=None,
        persist_handled_events=False,
        notification_error=None,
        send_reply_mock=None,
        complete_threads_mock=None,
        mark_client_completed_mock=None,
        missing_required_fields=None,
        apply_proposal_result=None,
        create_reply_review_mock=None,
        queue_pending_mock=None,
    ):
        client_id = "client-1"
        msg = self._common_graph_message(
            msg_id=msg_id or f"msg-{thread_id}",
            subject=f"RE: Tour slot: {row_anchor}",
            from_email=from_email,
            body=body,
            internet_message_id=internet_message_id or f"<{thread_id}@mock.test>",
            conversation_id=f"conv-{thread_id}",
        )
        header = [
            "Property Address",
            "City",
            "Leasing Contact",
            "Email",
            "Total SF",
            "Rent/SF/Yr",
            "Ops Ex / SF",
        ]
        rowvals = [
            row_anchor,
            "Houston",
            contact_name,
            from_email,
            "4531",
            "10.00",
            "3.31",
        ]
        if property_name is not None:
            header.insert(2, "Property Name")
            rowvals.insert(2, property_name)
        client_ref = FakeDocumentRef({"criteria": "Industrial search"})
        full_body_response = MagicMock()
        full_body_response.json.return_value = {
            "body": {"content": body, "contentType": "Text"},
            "hasAttachments": False,
        }
        me_response = MagicMock(status_code=200)
        me_response.json.return_value = {"mail": "baylor.freelance@outlook.com"}

        notifications = []
        handled_events = []
        status_updates = []

        def fake_write_notification(*args, **kwargs):
            if notification_error:
                raise notification_error
            notif_id = f"notif-{len(notifications) + 1}"
            notifications.append({"args": args, "kwargs": kwargs, "id": notif_id})
            return notif_id

        def fake_mark_event_handled(_user_id, _thread_id, event_key, _msg_id, notif_id):
            handled_events.append({"eventKey": event_key, "notifId": notif_id})
            if persist_handled_events:
                thread_ref._data.setdefault("handledEvents", {})[event_key] = {
                    "detectedInMessageId": _msg_id,
                    "notificationId": notif_id,
                }
            return True

        def fake_is_event_handled(_user_id, _thread_id, event_key):
            if not persist_handled_events:
                return False
            return event_key in thread_ref._data.get("handledEvents", {})

        build_event_key = MagicMock(side_effect=processing.build_event_key)
        is_event_handled = MagicMock(side_effect=fake_is_event_handled)
        mark_event_handled = MagicMock(side_effect=fake_mark_event_handled)

        def fake_update_thread_status(_user_id, _thread_id, status, reason):
            status_updates.append({"status": status, "reason": reason})
            return True

        move_row = MagicMock(return_value=11)
        ensure_divider = MagicMock(return_value=10)
        if ensure_divider_side_effect is not None:
            ensure_divider.side_effect = ensure_divider_side_effect
        stop_threads = MagicMock(return_value=1)
        call_trace = []

        def fake_send_reply(*args, **kwargs):
            call_trace.append("send")
            return True

        def fake_mark_client_completed(*args, **kwargs):
            call_trace.append("complete")
            return True

        send_reply = send_reply_mock or MagicMock(side_effect=fake_send_reply)
        mark_client_completed = mark_client_completed_mock or MagicMock(
            side_effect=fake_mark_client_completed
        )
        complete_threads = complete_threads_mock or MagicMock(return_value=1)
        apply_proposal = (
            MagicMock(return_value=apply_proposal_result)
            if apply_proposal_result is not None
            else None
        )
        cancel_followup = MagicMock(side_effect=cancel_followup_side_effect)
        create_reply_review = create_reply_review_mock or MagicMock()
        queue_pending = queue_pending_mock or MagicMock()
        thread_docs = thread_docs or {thread_id: thread_ref}
        patches = [
            patch.object(processing, "_fs", FakeFirestore(thread_ref, client_ref, thread_docs=thread_docs)),
            patch.object(processing, "exponential_backoff_request", return_value=full_body_response),
            patch.object(processing.requests, "get", return_value=me_response),
            patch.object(processing, "lookup_thread_by_message_id", return_value=thread_id),
            patch.object(processing, "lookup_thread_by_conversation_id", return_value=None),
            patch.object(processing, "get_thread_status", return_value=processing.THREAD_STATUS["active"]),
            patch.object(processing, "save_message", return_value=True),
            patch.object(processing, "index_message_id", return_value=True),
            patch.object(processing, "dump_thread_from_firestore"),
            patch(
                "email_automation.followup.cancel_followup_on_response",
                new=cancel_followup,
            ),
            patch.object(
                processing,
                "fetch_and_log_sheet_for_thread",
                return_value=(client_id, "sheet-1", header, rownum, rowvals, None, []),
            ),
            patch.object(
                processing,
                "_resolve_reply_identity",
                return_value={
                    "recipient_email": from_email,
                    "contact_name": contact_name,
                    "original_email": from_email,
                    "source": "test",
                },
            ),
            patch.object(processing, "fetch_and_process_pdfs", return_value=[]),
            patch.object(processing, "write_message_order_test"),
            patch.object(processing, "fetch_url_as_text", return_value=None),
            patch.object(processing, "propose_sheet_updates", return_value=proposal),
            patch.object(processing, "_sheets_client", return_value=MagicMock()),
            patch.object(processing, "_get_first_tab_title", return_value="Sheet1"),
            patch.object(processing, "build_event_key", new=build_event_key),
            patch.object(processing, "is_event_handled", new=is_event_handled),
            patch.object(processing, "write_notification", side_effect=fake_write_notification),
            patch.object(processing, "mark_event_handled", new=mark_event_handled),
            patch.object(processing, "_is_row_below_nonviable", return_value=row_below_nonviable),
            patch.object(processing, "ensure_nonviable_divider", new=ensure_divider),
            patch.object(processing, "move_row_below_divider", side_effect=move_row),
            patch.object(processing, "sync_thread_row_numbers_after_move"),
            patch.object(processing, "stop_threads_for_row", side_effect=stop_threads),
            patch.object(processing, "find_notes_comment_column_index", return_value=None),
            patch.object(processing, "format_sheet_columns_autosize_with_exceptions"),
            patch.object(processing, "clear_row_highlight"),
            patch.object(processing, "highlight_row"),
            patch.object(processing, "send_reply_in_thread", side_effect=send_reply),
            patch.object(processing, "update_thread_status", side_effect=fake_update_thread_status),
            patch.object(processing, "complete_threads_for_row", new=complete_threads),
            patch.object(processing, "_clear_thread_action_notifications"),
            patch.object(processing, "_maybe_mark_client_completed", side_effect=mark_client_completed),
            patch.object(
                processing,
                "create_policy_blocked_reply_review",
                new=create_reply_review,
                create=True,
            ),
            patch.object(processing, "queue_pending_response", new=queue_pending),
            patch.object(
                processing,
                "check_missing_required_fields",
                return_value=list(missing_required_fields or []),
            ),
        ]
        if apply_proposal is not None:
            patches.append(
                patch.object(processing, "apply_proposal_to_sheet", new=apply_proposal)
            )

        for patcher in patches:
            patcher.start()
        try:
            processing.process_inbox_message(
                user_id,
                {"Authorization": "Bearer test-token"},
                msg,
            )
        finally:
            for patcher in reversed(patches):
                patcher.stop()

        return {
            "notifications": notifications,
            "handledEvents": handled_events,
            "buildEventKey": build_event_key,
            "isEventHandled": is_event_handled,
            "markEventHandled": mark_event_handled,
            "statusUpdates": status_updates,
            "moveRow": move_row,
            "ensureDivider": ensure_divider,
            "stopThreads": stop_threads,
            "sendReply": send_reply,
            "markClientCompleted": mark_client_completed,
            "completeThreads": complete_threads,
            "applyProposal": apply_proposal,
            "cancelFollowup": cancel_followup,
            "callTrace": call_trace,
            "threadRef": thread_ref,
            "createReplyReview": create_reply_review,
            "queuePending": queue_pending,
        }

    @staticmethod
    def _policy_blocked_send(*_args, **_kwargs):
        processing._set_reply_send_outcome(
            error=(
                "Automatic inbox replies are disabled for this user; "
                "manual review required before auto-reply"
            ),
            outcome="blocked_auto_reply_policy",
        )
        return False

    def test_reply_review_projection_failure_escapes_auto_response_retry_boundary(self):
        thread_id = "thread-policy-projection-failure"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "followUpStatus": "waiting",
        })
        create_review = MagicMock(
            side_effect=processing.RetryableProcessingError(
                "policy-blocked reply review projection failed"
            )
        )

        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "policy-blocked reply review projection failed",
        ):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="The property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [
                        {"type": "property_unavailable", "reason": "no_longer_available"}
                    ],
                    "response_email": "Thanks for the update.",
                },
                thread_ref=thread_ref,
                send_reply_mock=MagicMock(side_effect=self._policy_blocked_send),
                create_reply_review_mock=create_review,
            )

    def test_oversized_policy_review_draft_stays_retryable_without_second_effect(self):
        self.addCleanup(processing._reset_reply_send_outcome)
        processing._reset_reply_send_outcome()
        thread_id = "thread-policy-oversized-review"
        oversized_draft = "x" * 100_001
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "followUpStatus": "waiting",
        })
        send_reply = MagicMock(side_effect=self._policy_blocked_send)
        create_review = MagicMock(
            side_effect=ValueError("response_body exceeds maximum length 100000")
        )
        queue_pending = MagicMock()
        mark_processed = MagicMock()
        complete_client = MagicMock(return_value=True)

        with patch.object(
            processing,
            "_select_automatic_response_body",
            return_value=oversized_draft,
        ), patch.object(processing, "mark_processed", new=mark_processed):
            with self.assertRaisesRegex(
                processing.RetryableProcessingError,
                "policy-blocked reply review projection failed",
            ):
                self._run_tour_invite_reply_processing(
                    thread_id=thread_id,
                    body="The property is no longer available.",
                    proposal={
                        "updates": [],
                        "events": [
                            {
                                "type": "property_unavailable",
                                "reason": "no_longer_available",
                            }
                        ],
                        "response_email": oversized_draft,
                    },
                    thread_ref=thread_ref,
                    send_reply_mock=send_reply,
                    create_reply_review_mock=create_review,
                    queue_pending_mock=queue_pending,
                    mark_client_completed_mock=complete_client,
                )

        send_reply.assert_called_once()
        create_review.assert_called_once()
        self.assertEqual(
            oversized_draft,
            create_review.call_args.kwargs["response_body"],
        )
        queue_pending.assert_not_called()
        mark_processed.assert_not_called()
        complete_client.assert_not_called()

    def test_reply_review_stops_second_response_and_does_not_complete_client(self):
        thread_id = "thread-policy-single-review"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "followUpStatus": "waiting",
        })
        send_reply = MagicMock(side_effect=self._policy_blocked_send)
        create_review = MagicMock(return_value=MagicMock(status="created"))
        complete_client = MagicMock(return_value=True)

        result = self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body=(
                "The property is no longer available. Please call me so we can discuss."
            ),
            proposal={
                "updates": [],
                "events": [
                    {"type": "property_unavailable", "reason": "no_longer_available"},
                    {"type": "call_requested"},
                ],
                "response_email": "Thanks for the update.",
            },
            thread_ref=thread_ref,
            send_reply_mock=send_reply,
            create_reply_review_mock=create_review,
            mark_client_completed_mock=complete_client,
        )

        send_reply.assert_called_once()
        create_review.assert_called_once()
        complete_client.assert_not_called()
        result["queuePending"].assert_not_called()

    def test_inbound_marker_write_failure_escapes_to_retry_boundary(self):
        body = "The property is available and has 600A power."
        thread_id = "thread-marker-failure"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "followUpConfig": {"enabled": False},
        })

        with self.assertRaisesRegex(RuntimeError, "marker write unavailable"):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body=body,
                proposal={"updates": [], "events": [], "response_email": None},
                thread_ref=thread_ref,
                cancel_followup_side_effect=RuntimeError("marker write unavailable"),
            )

    def test_nonviable_sheet_failure_stages_all_split_roots_before_mutation(self):
        body = "Hi Baylor,\n\n951 E FM 646 is no longer available.\n\nBest,\nRyan"
        thread_id = "thread-current-root"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "followUpStatus": "waiting",
            "followUpConfig.processingBy": "worker-current",
            "followUpConfig.processingAt": "claimed-current",
            "followUpConfig.nextFollowUpAt": "scheduled-current",
        })
        sibling_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "followUpStatus": "waiting",
            "followUpConfig.processingBy": "worker-sibling",
            "followUpConfig.processingAt": "claimed-sibling",
            "followUpConfig.nextFollowUpAt": "scheduled-sibling",
        })
        mutation_observations = []

        def fail_sheet_mutation(*_args, **_kwargs):
            mutation_observations.append([
                {
                    "pendingTerminalReason": root._data.get("pendingTerminalReason"),
                    "followUpStatus": root._data.get("followUpStatus"),
                    "nextFollowUpAt": root._data.get("followUpConfig.nextFollowUpAt"),
                    "processingBy": root._data.get("followUpConfig.processingBy"),
                    "processingAt": root._data.get("followUpConfig.processingAt"),
                }
                for root in (current_root, sibling_root)
            ])
            raise RuntimeError("nonviable Sheet mutation failed")

        proposal = {
            "updates": [],
            "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
            "response_email": None,
        }

        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "property_unavailable event failed",
        ):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body=body,
                proposal=proposal,
                thread_ref=current_root,
                thread_docs={
                    thread_id: current_root,
                    "thread-sibling-root": sibling_root,
                },
                row_anchor="951 E FM 646",
                ensure_divider_side_effect=fail_sheet_mutation,
            )

        self.assertEqual(1, len(mutation_observations))
        for staged_root in mutation_observations[0]:
            self.assertEqual("no_longer_available", staged_root["pendingTerminalReason"])
            self.assertEqual("stopped", staged_root["followUpStatus"])
            self.assertIsNone(staged_root["nextFollowUpAt"])
            self.assertIsNone(staged_root["processingBy"])
            self.assertIsNone(staged_root["processingAt"])
        self.assertEqual(processing.THREAD_STATUS["active"], current_root._data["status"])
        self.assertEqual(processing.THREAD_STATUS["active"], sibling_root._data["status"])

    def test_nonviable_sibling_staging_failure_blocks_sheet_mutation(self):
        body = "Hi Baylor,\n\n951 E FM 646 is no longer available.\n\nBest,\nRyan"
        thread_id = "thread-current-root"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "followUpStatus": "waiting",
        })
        sibling_root = FakeDocumentRef(
            {
                "clientId": "client-1",
                "email": ["bp21harrison@gmail.com"],
                "status": processing.THREAD_STATUS["active"],
                "rowNumber": 3,
                "followUpStatus": "waiting",
            },
            update_error=RuntimeError("sibling stage write failed"),
        )
        sheet_mutations = []
        proposal = {
            "updates": [],
            "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
            "response_email": None,
        }

        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "terminal staging failed",
        ):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body=body,
                proposal=proposal,
                thread_ref=current_root,
                thread_docs={
                    thread_id: current_root,
                    "thread-sibling-root": sibling_root,
                },
                row_anchor="951 E FM 646",
                ensure_divider_side_effect=lambda *_args, **_kwargs: sheet_mutations.append(True),
            )

        self.assertEqual([], sheet_mutations)

    def test_property_unavailable_already_below_nonviable_stops_thread_without_moving_row(self):
        body = "Hi Baylor,\n\nThis space would not be a good fit for your client.\n\nBest,\nBP21"
        thread_id = "thread-already-nonviable"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 11,
        })
        proposal = {
            "updates": [],
            "events": [{"type": "property_unavailable", "reason": "requirements_mismatch"}],
            "response_email": None,
        }

        result = self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body=body,
            proposal=proposal,
            thread_ref=thread_ref,
            row_anchor="951 Tristar Dr",
            rownum=11,
            row_below_nonviable=True,
        )

        result["moveRow"].assert_not_called()
        result["stopThreads"].assert_called_once_with(
            "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
            11,
            client_id="client-1",
            reason="requirements_mismatch",
        )
        self.assertIn(
            {"status": processing.THREAD_STATUS["stopped"], "reason": "requirements_mismatch"},
            result["statusUpdates"],
        )
        self.assertTrue(
            any(
                handled["eventKey"].startswith("property_unavailable")
                and handled["notifId"] is None
                for handled in result["handledEvents"]
            )
        )

    def test_property_unavailable_acknowledges_before_campaign_completion(self):
        body = "Hi Baylor,\n\n951 E FM 646 is no longer available.\n\nBest,\nRyan"
        thread_id = "thread-unavailable-final-row"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "contactName": "Ryan Young",
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        proposal = {
            "updates": [],
            "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
            "response_email": None,
        }

        result = self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body=body,
            proposal=proposal,
            thread_ref=thread_ref,
            row_anchor="951 E FM 646",
            contact_name="Ryan Young",
        )

        self.assertEqual(result["callTrace"], ["send", "complete"])
        result["markClientCompleted"].assert_called_once_with(
            "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
            "client-1",
        )

    def test_property_unavailable_named_by_current_row_property_name_moves_row(self):
        body = (
            "Hi Avery, Olive Commerce Park is no longer available; "
            "the owner signed another tenant yesterday. Best, Jordan"
        )
        thread_id = "thread-current-property-name-unavailable"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "contactName": "Avery Cole",
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 4,
        })
        proposal = {
            "updates": [],
            "events": [{
                "type": "property_unavailable",
                "reason": "no_longer_available",
                "address": "",
                "city": "",
            }],
            "response_email": "Thank you. Do you have another comparable property?",
        }

        result = self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body=body,
            proposal=proposal,
            thread_ref=thread_ref,
            row_anchor="10675 W Olive Ave",
            rownum=4,
            property_name="Olive Commerce Park",
            contact_name="Avery Cole",
        )

        result["moveRow"].assert_called_once()
        result["stopThreads"].assert_called_once_with(
            "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
            11,
            client_id="client-1",
            reason="no_longer_available",
        )
        self.assertEqual("stopped", result["threadRef"]._data["followUpStatus"])
        self.assertEqual("no_longer_available", result["threadRef"]._data["nonViableReason"])
        self.assertEqual(1, len(result["notifications"]))
        self.assertEqual(
            "property_unavailable",
            result["notifications"][0]["kwargs"]["kind"],
        )
        self.assertTrue(
            any(
                handled["eventKey"].startswith("property_unavailable")
                and handled["notifId"] is not None
                for handled in result["handledEvents"]
            )
        )

    def test_quoted_property_name_unavailability_does_not_move_fresh_viable_row(self):
        fresh_messages = (
            "Thanks, I attached the current specs.",
            "Oak Commerce Center is no longer available.",
        )
        for index, fresh_message in enumerate(fresh_messages):
            with self.subTest(fresh_message=fresh_message):
                body = (
                    f"{fresh_message}\n\n"
                    "On Fri, Aug 7, 2026 at 1:00 PM Avery wrote:\n"
                    "Olive Commerce Park is no longer available."
                )
                thread_id = f"thread-quoted-property-name-unavailable-{index}"
                thread_ref = FakeDocumentRef({
                    "clientId": "client-1",
                    "email": ["bp21harrison@gmail.com"],
                    "status": processing.THREAD_STATUS["active"],
                    "rowNumber": 4,
                })
                proposal = {
                    "updates": [],
                    "events": [{
                        "type": "property_unavailable",
                        "reason": "no_longer_available",
                        "address": "",
                        "city": "",
                    }],
                    "skip_response": True,
                    "response_email": None,
                }

                result = self._run_tour_invite_reply_processing(
                    thread_id=thread_id,
                    body=body,
                    proposal=proposal,
                    thread_ref=thread_ref,
                    row_anchor="10675 W Olive Ave",
                    rownum=4,
                    property_name="Olive Commerce Park",
                )

                result["moveRow"].assert_not_called()
                result["stopThreads"].assert_not_called()
                self.assertNotEqual("stopped", thread_ref._data.get("followUpStatus"))
                self.assertFalse(any(
                    handled["eventKey"].startswith("property_unavailable")
                    for handled in result["handledEvents"]
                ))

    def test_rejected_competitor_event_does_not_poison_later_target_event(self):
        thread_id = "thread-competitor-then-target-unavailable"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 4,
        })
        event = {
            "type": "property_unavailable",
            "reason": "no_longer_available",
            "address": "",
            "city": "",
        }

        rejected = self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body=(
                "Oak Commerce Center is no longer available, but specs for "
                "Olive Commerce Park are attached."
            ),
            proposal={
                "updates": [],
                "events": [event],
                "skip_response": True,
                "response_email": None,
            },
            thread_ref=thread_ref,
            row_anchor="10675 W Olive Ave",
            rownum=4,
            property_name="Olive Commerce Park",
            persist_handled_events=True,
            msg_id="msg-competitor-unavailable",
            internet_message_id="<competitor-unavailable@mock.test>",
        )

        rejected["moveRow"].assert_not_called()
        self.assertFalse(any(
            event_key.startswith("property_unavailable")
            for event_key in thread_ref._data.get("handledEvents", {})
        ))

        accepted = self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="Olive Commerce Park is no longer available.",
            proposal={
                "updates": [],
                "events": [event],
                "skip_response": True,
                "response_email": None,
            },
            thread_ref=thread_ref,
            row_anchor="10675 W Olive Ave",
            rownum=4,
            property_name="Olive Commerce Park",
            persist_handled_events=True,
            msg_id="msg-target-unavailable",
            internet_message_id="<target-unavailable@mock.test>",
        )

        accepted["moveRow"].assert_called_once()
        self.assertTrue(any(
            event_key.startswith("property_unavailable")
            and handled["notificationId"] is not None
            for event_key, handled in thread_ref._data.get("handledEvents", {}).items()
        ))

    def test_property_name_unavailable_has_one_terminal_reply_and_no_generic_completion(self):
        body = (
            "Hi Avery, Olive Commerce Park is no longer available; "
            "the owner signed another tenant yesterday. Best, Jordan"
        )
        thread_id = "thread-property-name-terminal-response"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "contactName": "Avery Cole",
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 4,
        })
        proposal = {
            "updates": [],
            "events": [{
                "type": "property_unavailable",
                "reason": "no_longer_available",
                "address": "",
                "city": "",
            }],
            "response_email": "Thank you. Do you have another comparable property?",
        }

        result = self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body=body,
            proposal=proposal,
            thread_ref=thread_ref,
            row_anchor="10675 W Olive Ave",
            rownum=4,
            property_name="Olive Commerce Park",
            contact_name="Avery Cole",
        )

        result["sendReply"].assert_called_once()
        result["completeThreads"].assert_not_called()
        self.assertEqual(
            ["property_unavailable"],
            [notification["kwargs"]["kind"] for notification in result["notifications"]],
        )
        self.assertNotIn(
            {"status": processing.THREAD_STATUS["completed"], "reason": "all_fields_gathered"},
            result["statusUpdates"],
        )
        self.assertTrue(
            any(
                handled["eventKey"].startswith("property_unavailable")
                and handled["notifId"] is not None
                for handled in result["handledEvents"]
            )
        )

    def test_negated_or_superseded_property_name_unavailable_does_not_move_row(self):
        messages = (
            "Olive Commerce Park is not leased.",
            "Olive Commerce Park is not unavailable.",
            "Olive Commerce Park was no longer available, but is now still available.",
            "Olive Commerce Park is not leased; it remains available.",
            "Olive Commerce Park is not not leased.",
        )
        for index, body in enumerate(messages):
            with self.subTest(body=body):
                thread_ref = FakeDocumentRef({
                    "clientId": "client-1",
                    "email": ["bp21harrison@gmail.com"],
                    "status": processing.THREAD_STATUS["active"],
                    "rowNumber": 4,
                })
                result = self._run_tour_invite_reply_processing(
                    thread_id=f"thread-negated-property-name-unavailable-{index}",
                    body=body,
                    proposal={
                        "updates": [],
                        "events": [{
                            "type": "property_unavailable",
                            "reason": "no_longer_available",
                            "address": "",
                            "city": "",
                        }],
                        "skip_response": True,
                        "response_email": None,
                    },
                    thread_ref=thread_ref,
                    row_anchor="10675 W Olive Ave",
                    rownum=4,
                    property_name="Olive Commerce Park",
                    msg_id=f"msg-negated-property-name-unavailable-{index}",
                    internet_message_id=f"<negated-property-name-{index}@mock.test>",
                )

                result["moveRow"].assert_not_called()
                result["stopThreads"].assert_not_called()
                result["sendReply"].assert_not_called()
                self.assertNotEqual("stopped", thread_ref._data.get("followUpStatus"))
                self.assertNotIn("nonViableReason", thread_ref._data)
                self.assertFalse(any(
                    handled["eventKey"].startswith("property_unavailable")
                    for handled in result["handledEvents"]
                ))

    def test_close_conversation_sends_terminal_reply_before_campaign_completion(self):
        body = (
            "Hi Baylor,\n\nThe suite is 42,000 SF at $10.50/SF with $3.25/SF "
            "operating expenses, two drive-ins, 24-foot clear height, and 480V "
            "three-phase power.\n\nBest,\nTram"
        )
        thread_id = "thread-complete-final-row"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "contactName": "Tram Kim",
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        proposal = {
            "updates": [],
            "events": [{"type": "close_conversation", "reason": "all_information_received"}],
            "response_email": "Hi,\n\nPerfect, thank you. This covers everything I needed.",
        }

        result = self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body=body,
            proposal=proposal,
            thread_ref=thread_ref,
            row_anchor="1561 Live Oak St",
            contact_name="Tram Kim",
        )

        self.assertEqual(result["callTrace"], ["send", "complete"])
        result["markClientCompleted"].assert_called_once_with(
            "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
            "client-1",
        )
        sent_body = result["sendReply"].call_args.args[2]
        self.assertTrue(sent_body.startswith("Hi Tram,\n\n"))
        self.assertNotIn("[NAME]", sent_body)

    def test_tour_invite_alternate_reply_processes_schedule_decision_without_auto_send(self):
        body = "Hi Baylor,\n\n10:15 AM does not work for us. Could we do 11:45 AM instead?\n\nBest,\nBP21"
        thread_id = "thread-tour-alt"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
            "tourInvite": {
                "tourDate": "2026-06-23",
                "arrivalTime": "10:15 AM",
                "departureTime": "10:45 AM",
                "travelBufferMinutes": 5,
            },
        })
        busy_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison+busy@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 4,
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
            "tourInvite": {
                "tourDate": "2026-06-23",
                "arrivalTime": "10:00 AM",
                "departureTime": "10:30 AM",
                "travelBufferMinutes": 5,
            },
        })
        proposal = {
            "updates": [],
            "events": [
                {
                    "type": "tour_requested",
                    "question": "10:15 AM does not work. Could we do 11:45 AM instead?",
                    "suggestedEmail": "Let me check and get back to you.",
                }
            ],
            "response_email": None,
        }

        result = self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body=body,
            proposal=proposal,
            thread_ref=thread_ref,
            thread_docs={thread_id: thread_ref, "thread-busy": busy_ref},
        )

        self.assertEqual(1, len(result["notifications"]))
        meta = result["notifications"][0]["kwargs"]["meta"]
        self.assertEqual("tour_reschedule_requested", meta["reason"])
        classification = meta["tourReplyClassification"]
        self.assertEqual("alternate_requested", classification["outcome"])
        self.assertEqual("fits", classification["scheduleDecision"]["feasibility"])
        self.assertIn("Tuesday, June 23, 2026 at 11:45 AM works on our end", meta["suggestedEmail"]["body"])
        self.assertNotIn("Let me check", meta["suggestedEmail"]["body"])
        result["sendReply"].assert_not_called()
        self.assertEqual("alternate_requested", thread_ref._data["tourInvite.status"])
        self.assertEqual("fits", thread_ref._data["tourInvite.requestedAlternate"]["feasibility"])
        self.assertIn(
            {"status": processing.THREAD_STATUS["paused"], "reason": "tour_reschedule_requested"},
            result["statusUpdates"],
        )

    def test_passive_tour_courtesy_does_not_poison_later_real_tour_request(self):
        thread_id = "thread-tour-courtesy-then-real-request"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })

        courtesy_result = self._run_tour_invite_reply_processing(
            user_id="regular-user",
            thread_id=thread_id,
            body="Please let me know if you need a tour.",
            proposal={
                "updates": [],
                "events": [{
                    "type": "tour_requested",
                    "question": "Need a tour?",
                    "suggestedEmail": "",
                }],
                "response_email": None,
            },
            thread_ref=thread_ref,
            msg_id="msg-tour-courtesy",
            internet_message_id="<tour-courtesy@mock.test>",
            persist_handled_events=True,
            missing_required_fields=["Ops Ex / SF"],
        )

        self.assertEqual([], courtesy_result["notifications"])
        self.assertFalse(any(
            update["status"] == processing.THREAD_STATUS["paused"]
            or update["reason"] == "tour_requested"
            for update in courtesy_result["statusUpdates"]
        ))
        self.assertEqual({}, thread_ref._data.get("handledEvents", {}))
        courtesy_result["buildEventKey"].assert_not_called()
        courtesy_result["isEventHandled"].assert_not_called()
        courtesy_result["markEventHandled"].assert_not_called()
        courtesy_result["sendReply"].assert_called_once()
        self.assertIn("Ops Ex / SF", courtesy_result["sendReply"].call_args.args[2])
        courtesy_result["completeThreads"].assert_not_called()
        courtesy_result["markClientCompleted"].assert_not_called()
        self.assertEqual(processing.THREAD_STATUS["active"], thread_ref._data["status"])
        self.assertFalse(any(
            update["status"] == processing.THREAD_STATUS["completed"]
            for update in courtesy_result["statusUpdates"]
        ))

        real_result = self._run_tour_invite_reply_processing(
            user_id="regular-user",
            thread_id=thread_id,
            body="Let me know if your client wants to schedule a tour Tuesday at 2 PM.",
            proposal={
                "updates": [],
                "events": [{
                    "type": "tour_requested",
                    "question": "Let me know if your client wants to schedule a tour Tuesday at 2 PM.",
                    "suggestedEmail": "Tuesday at 2 PM works for us.",
                }],
                "response_email": None,
            },
            thread_ref=thread_ref,
            msg_id="msg-real-tour-request",
            internet_message_id="<real-tour-request@mock.test>",
            persist_handled_events=True,
        )

        self.assertEqual(1, len(real_result["notifications"]))
        self.assertEqual(
            "tour_requested",
            real_result["notifications"][0]["kwargs"]["meta"]["reason"],
        )
        real_result["sendReply"].assert_not_called()
        self.assertTrue(
            any(update["reason"] == "tour_requested" for update in real_result["statusUpdates"])
        )
        self.assertTrue(
            any(
                handled["eventKey"] == "tour_requested"
                and handled["notifId"] == real_result["notifications"][0]["id"]
                for handled in real_result["handledEvents"]
            )
        )

    def test_production_missing_opex_then_dated_tour_offer_pauses_before_completion(self):
        first_body = (
            "Hi John, the suite is still available. It contains 18,750 SF and rent is "
            "$14.40/SF/year NNN. I need to confirm the current operating expenses. "
            "Please let me know if you need a tour. Best, Jordan"
        )
        missing_opex_followup = (
            "Hi Jordan, thanks for the update. Could you confirm the current operating "
            "expenses per square foot? Best, John"
        )

        for index, (tour_window, next_sentence) in enumerate((
            ("Tuesday, August 11 at 2:00 PM", ""),
            ("Tuesday, August 11, 2026 at 2:00 PM", ""),
            ("Tuesday, Aug. 11 at 2 PM", ""),
            ("Tue., Aug. 11, 2026 at 2 p.m", ""),
            (
                "Tue., Aug. 11, 2026 at 2 p.m",
                " In the meantime, the rent schedule is attached.",
            ),
            (
                "Tue., Aug. 11, 2026 at 2 p.m",
                " in the meantime, the rent schedule is attached.",
            ),
            (
                "Tue., Aug. 11, 2026 at 2 p.m",
                " online pricing is available.",
            ),
            (
                "Tue., Aug. 11, 2026 at 2 p.m",
                " via email, I sent the flyer.",
            ),
        )):
            with self.subTest(tour_window=tour_window):
                thread_id = f"thread-production-dated-tour-{index}"
                thread_ref = FakeDocumentRef({
                    "clientId": "client-1",
                    "email": ["bp21harrison@gmail.com"],
                    "status": processing.THREAD_STATUS["active"],
                    "rowNumber": 3,
                })
                first_conversation = [{"direction": "inbound", "content": first_body}]
                first_proposal = ai_processing._augment_events_with_deterministic_signals(
                    {
                        "updates": [
                            {"column": "Total SF", "value": "18750", "confidence": 1.0},
                            {"column": "Rent/SF/Yr", "value": "14.40", "confidence": 1.0},
                        ],
                        "events": [{
                            "type": "tour_requested",
                            "reason": "tour_offer",
                            "question": "Please let me know if you need a tour.",
                            "suggestedEmail": "",
                        }],
                        "response_email": missing_opex_followup,
                    },
                    first_conversation,
                    target_anchor="912-930 Gemini St",
                )
                first_result = self._run_tour_invite_reply_processing(
                    user_id="regular-user",
                    thread_id=thread_id,
                    body=first_body,
                    proposal=first_proposal,
                    thread_ref=thread_ref,
                    msg_id=f"msg-production-passive-{index}",
                    internet_message_id=f"<production-passive-{index}@mock.test>",
                    persist_handled_events=True,
                    missing_required_fields=["Ops Ex / SF"],
                    apply_proposal_result={
                        "applied": [
                            {"column": "Total SF", "newValue": "18750"},
                            {"column": "Rent/SF/Yr", "newValue": "14.40"},
                        ],
                        "skipped": [],
                    },
                )

                self.assertFalse(any(
                    event.get("type") == "tour_requested"
                    for event in first_proposal["events"]
                ))
                self.assertEqual([], first_result["notifications"])
                first_result["sendReply"].assert_called_once()
                first_result["completeThreads"].assert_not_called()
                first_result["markClientCompleted"].assert_not_called()
                self.assertEqual(processing.THREAD_STATUS["active"], thread_ref._data["status"])

                tour_sentence = (
                    "Let me know if your client wants to schedule a tour "
                    f"{tour_window}."
                )
                second_body = (
                    "Hi John, operating expenses are $3.10/SF/year. "
                    f"{tour_sentence}{next_sentence} Best, Jordan"
                )
                second_proposal = ai_processing._augment_events_with_deterministic_signals(
                    {
                        "updates": [{
                            "column": "Ops Ex / SF",
                            "value": "3.10",
                            "confidence": 1.0,
                        }],
                        "events": [{
                            "type": "tour_requested",
                            "reason": "tour_offer",
                            "question": tour_sentence,
                            "suggestedEmail": "",
                        }],
                        "response_email": None,
                    },
                    [
                        {"direction": "inbound", "content": first_body},
                        {"direction": "outbound", "content": missing_opex_followup},
                        {"direction": "inbound", "content": second_body},
                    ],
                    target_anchor="912-930 Gemini St",
                )
                second_result = self._run_tour_invite_reply_processing(
                    user_id="regular-user",
                    thread_id=thread_id,
                    body=second_body,
                    proposal=second_proposal,
                    thread_ref=thread_ref,
                    msg_id=f"msg-production-dated-tour-{index}",
                    internet_message_id=f"<production-dated-tour-{index}@mock.test>",
                    persist_handled_events=True,
                    missing_required_fields=[],
                    apply_proposal_result={
                        "applied": [{"column": "Ops Ex / SF", "newValue": "3.10"}],
                        "skipped": [],
                    },
                )

                second_result["applyProposal"].assert_called_once()
                second_result["completeThreads"].assert_not_called()
                second_result["markClientCompleted"].assert_not_called()
                second_result["sendReply"].assert_not_called()
                self.assertEqual(1, len(second_result["notifications"]))
                self.assertEqual(
                    "tour_requested",
                    second_result["notifications"][0]["kwargs"]["meta"]["reason"],
                )
                self.assertTrue(any(
                    update["status"] == processing.THREAD_STATUS["paused"]
                    and update["reason"] == "tour_requested"
                    for update in second_result["statusUpdates"]
                ))
                self.assertEqual(1, len(second_result["handledEvents"]))
                self.assertTrue(any(
                    event.get("type") == "tour_requested"
                    for event in second_proposal["events"]
                ))

    def test_subject_bound_tour_clauses_control_process_outcome(self):
        offer_thread = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
            "tourInvite": {
                "tourDate": "2026-06-23",
                "arrivalTime": "2:00 PM",
                "departureTime": "2:30 PM",
                "status": "sent",
            },
        })
        offer_body = "Can I show you the property Tuesday? The rent schedule is confirmed."
        offer_result = self._run_tour_invite_reply_processing(
            thread_id="thread-tour-offer-rent-confirmed",
            body=offer_body,
            proposal={
                "updates": [],
                "events": [{
                    "type": "tour_requested",
                    "question": offer_body,
                    "suggestedEmail": "",
                }],
                "response_email": None,
            },
            thread_ref=offer_thread,
            missing_required_fields=["Ops Ex / SF"],
        )

        self.assertEqual(1, len(offer_result["notifications"]))
        offer_meta = offer_result["notifications"][0]["kwargs"]["meta"]
        self.assertEqual("tour_requested", offer_meta["reason"])
        self.assertEqual(
            "tour_offer_or_request",
            offer_meta["tourReplyClassification"]["outcome"],
        )
        offer_result["completeThreads"].assert_not_called()
        self.assertEqual(1, len(offer_result["handledEvents"]))

        confirmation_thread = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
            "tourInvite": {
                "tourDate": "2026-06-23",
                "arrivalTime": "2:00 PM",
                "departureTime": "2:30 PM",
                "status": "sent",
            },
        })
        confirmation_body = "Tour confirmed for Tuesday at 2 PM. The rent schedule doesn't work."
        confirmation_result = self._run_tour_invite_reply_processing(
            thread_id="thread-tour-confirmed-rent-declined",
            body=confirmation_body,
            proposal={
                "updates": [],
                "events": [{
                    "type": "tour_requested",
                    "question": confirmation_body,
                    "suggestedEmail": "",
                }],
                "response_email": None,
            },
            thread_ref=confirmation_thread,
        )

        self.assertEqual("confirmed", confirmation_thread._data["tourInvite.status"])
        self.assertEqual("confirmed", confirmation_thread._data["tourStatus"])
        confirmation_result["completeThreads"].assert_called_once()
        self.assertFalse(any(
            notification["kwargs"]["meta"]["reason"] == "tour_reschedule_requested"
            for notification in confirmation_result["notifications"]
        ))

    def test_offer_questions_do_not_complete_established_tour_invite(self):
        messages = (
            "Happy to show you the space, when works for you?",
            "Happy to show you the property if Tuesday works for you.",
            "Happy to show you the property whenever works for you.",
        )
        for index, body in enumerate(messages):
            with self.subTest(body=body):
                thread_ref = FakeDocumentRef({
                    "clientId": "client-1",
                    "email": ["bp21harrison@gmail.com"],
                    "status": processing.THREAD_STATUS["active"],
                    "rowNumber": 3,
                    "source": "dashboard_tour_planner",
                    "actionType": "tour_invite",
                    "tourInvite": {
                        "tourDate": "2026-06-23",
                        "arrivalTime": "2:00 PM",
                        "departureTime": "2:30 PM",
                        "status": "sent",
                    },
                })
                result = self._run_tour_invite_reply_processing(
                    thread_id=f"thread-tour-offer-question-{index}",
                    body=body,
                    proposal={
                        "updates": [],
                        "events": [{
                            "type": "tour_requested",
                            "question": body,
                            "suggestedEmail": "",
                        }],
                        "response_email": None,
                    },
                    thread_ref=thread_ref,
                    missing_required_fields=["Ops Ex / SF"],
                )

                self.assertEqual(1, len(result["notifications"]))
                meta = result["notifications"][0]["kwargs"]["meta"]
                self.assertEqual("tour_requested", meta["reason"])
                self.assertEqual(
                    "tour_offer_or_request",
                    meta["tourReplyClassification"]["outcome"],
                )
                self.assertEqual(1, len(result["handledEvents"]))
                result["completeThreads"].assert_not_called()
                self.assertNotEqual("confirmed", thread_ref._data.get("tourStatus"))

    def test_fresh_bare_time_model_reason_does_not_pause_normal_campaign(self):
        body = "2 PM."
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "actionType": "campaign_creation",
        })
        result = self._run_tour_invite_reply_processing(
            thread_id="thread-fresh-bare-time-no-tour-context",
            body=body,
            proposal={
                "updates": [],
                "events": [{
                    "type": "tour_requested",
                    "reason": "tour_slot_reply",
                    "question": body,
                    "suggestedEmail": "",
                }],
                "response_email": None,
            },
            thread_ref=thread_ref,
            missing_required_fields=["Ops Ex / SF"],
        )

        self.assertEqual([], result["notifications"])
        self.assertEqual([], result["handledEvents"])
        self.assertFalse(any(
            update["status"] == processing.THREAD_STATUS["paused"]
            for update in result["statusUpdates"]
        ))
        result["completeThreads"].assert_not_called()

    def test_non_tour_subjects_do_not_mutate_established_tour_process(self):
        messages = (
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
            "Let me know if your client wants to schedule a tour Tuesday, August 11 at 2:00 PM via Zoom.",
            "Let me know if your client wants to schedule a tour Tuesday, August 11 at 2:00 PM online.",
            "Let me know if your client wants to schedule a tour Tuesday, August 11 at 2:00 PM in the financial model.",
            "The flyer is dated Tuesday, August 11 at 2:00 PM. Please let me know if you need a tour.",
            "A tour video is available Tuesday, August 11 at 2:00 PM.",
            "Let me know if your client wants to schedule a tour Tue., Aug. 11, 2026 at 2 p.m. via Zoom.",
            "Let me know if your client wants to schedule a tour Tue., Aug. 11, 2026 at 2 p.m. online.",
            "Let me know if your client wants to schedule a tour Tue., Aug. 11, 2026 at 2 p.m. in the financial model.",
        )
        for index, body in enumerate(messages):
            with self.subTest(body=body):
                thread_ref = FakeDocumentRef({
                    "clientId": "client-1",
                    "email": ["bp21harrison@gmail.com"],
                    "status": processing.THREAD_STATUS["active"],
                    "rowNumber": 3,
                    "source": "dashboard_tour_planner",
                    "actionType": "tour_invite",
                    "tourInvite": {
                        "tourDate": "2026-06-23",
                        "arrivalTime": "2:00 PM",
                        "departureTime": "2:30 PM",
                        "status": "sent",
                    },
                })
                result = self._run_tour_invite_reply_processing(
                    thread_id=f"thread-non-tour-subject-{index}",
                    body=body,
                    proposal={
                        "updates": [],
                        "events": [{
                            "type": "tour_requested",
                            "question": body,
                            "suggestedEmail": "",
                        }],
                        "response_email": None,
                    },
                    thread_ref=thread_ref,
                    missing_required_fields=["Ops Ex / SF"],
                )

                self.assertEqual([], result["notifications"])
                self.assertEqual([], result["handledEvents"])
                self.assertFalse(any(
                    update["status"] == processing.THREAD_STATUS["paused"]
                    for update in result["statusUpdates"]
                ))
                result["completeThreads"].assert_not_called()
                self.assertEqual("sent", thread_ref._data["tourInvite"]["status"])

    def test_physical_antecedents_and_mixed_virtual_offers_pause_once(self):
        messages = (
            "The property is available, and I can show it Tuesday.",
            "The suite is ready. I can show it Tuesday.",
            "The building is open and I can walk through it Tuesday.",
            "Would your client like a tour?",
            "Do you want a tour?",
            "Does your client want a tour?",
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
            "Let me know if your client wants to schedule a tour Tuesday at 2 PM.",
        )
        for index, body in enumerate(messages):
            with self.subTest(body=body):
                thread_ref = FakeDocumentRef({
                    "clientId": "client-1",
                    "email": ["bp21harrison@gmail.com"],
                    "status": processing.THREAD_STATUS["active"],
                    "rowNumber": 3,
                })
                result = self._run_tour_invite_reply_processing(
                    thread_id=f"thread-physical-tour-offer-{index}",
                    body=body,
                    proposal={
                        "updates": [],
                        "events": [{
                            "type": "tour_requested",
                            "question": body,
                            "suggestedEmail": "Please share a time that works.",
                        }],
                        "response_email": None,
                    },
                    thread_ref=thread_ref,
                    missing_required_fields=["Ops Ex / SF"],
                )

                self.assertEqual(1, len(result["notifications"]))
                self.assertEqual("tour_requested", result["notifications"][0]["kwargs"]["meta"]["reason"])
                self.assertEqual(1, len(result["handledEvents"]))
                self.assertEqual(1, len([
                    update for update in result["statusUpdates"]
                    if update["status"] == processing.THREAD_STATUS["paused"]
                    and update["reason"] == "tour_requested"
                ]))
                result["completeThreads"].assert_not_called()

    def test_cannot_show_then_proposed_day_reschedules_process(self):
        cases = (
            ("I cannot show Tuesday; could we do Wednesday?", "Wednesday"),
            ("I cannot show Tuesday; could we do Wednesday at 2 PM?", "Wednesday at 2 PM"),
            (
                "The rent schedule is attached. That time no longer works; "
                "could we do Wednesday at 2 PM for the tour?",
                "Wednesday at 2 PM",
            ),
        )
        for index, (body, expected_alternate) in enumerate(cases):
            with self.subTest(body=body):
                thread_ref = FakeDocumentRef({
                    "clientId": "client-1",
                    "email": ["bp21harrison@gmail.com"],
                    "status": processing.THREAD_STATUS["active"],
                    "rowNumber": 3,
                    "source": "dashboard_tour_planner",
                    "actionType": "tour_invite",
                    "tourInvite": {
                        "tourDate": "2026-06-23",
                        "arrivalTime": "2:00 PM",
                        "departureTime": "2:30 PM",
                        "status": "sent",
                    },
                })
                result = self._run_tour_invite_reply_processing(
                    thread_id=f"thread-tour-day-reschedule-{index}",
                    body=body,
                    proposal={
                        "updates": [],
                        "events": [{
                            "type": "tour_requested",
                            "question": body,
                            "suggestedEmail": "",
                        }],
                        "response_email": None,
                    },
                    thread_ref=thread_ref,
                )

                self.assertEqual(1, len(result["notifications"]))
                meta = result["notifications"][0]["kwargs"]["meta"]
                self.assertEqual("tour_reschedule_requested", meta["reason"])
                self.assertEqual("alternate_requested", meta["tourReplyClassification"]["outcome"])
                self.assertIn(expected_alternate, meta["tourReplyClassification"]["alternateTimes"])
                self.assertEqual("alternate_requested", thread_ref._data["tourInvite.status"])
                self.assertEqual(1, len(result["handledEvents"]))
                result["sendReply"].assert_not_called()

    def test_negative_tour_slot_without_alternate_pauses_as_declined(self):
        body = "That no longer works."
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
            "tourInvite": {
                "tourDate": "2026-06-23",
                "arrivalTime": "2:00 PM",
                "departureTime": "2:30 PM",
                "status": "sent",
            },
        })
        result = self._run_tour_invite_reply_processing(
            thread_id="thread-tour-slot-declined-no-alternate",
            body=body,
            proposal={
                "updates": [],
                "events": [{
                    "type": "tour_requested",
                    "question": body,
                    "suggestedEmail": "",
                }],
                "response_email": None,
            },
            thread_ref=thread_ref,
        )

        self.assertEqual(1, len(result["notifications"]))
        meta = result["notifications"][0]["kwargs"]["meta"]
        self.assertEqual("tour_slot_declined", meta["reason"])
        self.assertEqual("declined", meta["tourReplyClassification"]["outcome"])
        self.assertEqual("declined", thread_ref._data["tourInvite.status"])
        self.assertEqual(1, len(result["handledEvents"]))
        result["completeThreads"].assert_not_called()
        result["sendReply"].assert_not_called()

    def test_requested_tour_slot_works_confirms_and_closes_without_draft(self):
        body = "The requested tour slot works."
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
            "tourInvite": {
                "tourDate": "2026-06-23",
                "arrivalTime": "2:00 PM",
                "departureTime": "2:30 PM",
                "status": "sent",
            },
        })
        result = self._run_tour_invite_reply_processing(
            thread_id="thread-requested-tour-slot-confirmed",
            body=body,
            proposal={
                "updates": [],
                "events": [{
                    "type": "tour_requested",
                    "question": body,
                    "suggestedEmail": "",
                }],
                "response_email": None,
            },
            thread_ref=thread_ref,
        )

        self.assertEqual([], result["notifications"])
        self.assertEqual("confirmed", thread_ref._data["tourInvite.status"])
        self.assertEqual("confirmed", thread_ref._data["tourStatus"])
        self.assertEqual(1, len(result["handledEvents"]))
        self.assertTrue(result["handledEvents"][0]["eventKey"].startswith(
            "tour_requested:confirmed:"
        ))
        result["completeThreads"].assert_called_once()
        result["sendReply"].assert_not_called()

    def test_reviewed_tour_invite_confirmation_survives_persisted_offer_dedupe(self):
        thread_id = "thread-tour-sequence-confirmed"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        offer = {
            "updates": [],
            "events": [{
                "type": "tour_requested",
                "question": "I can show the property Tuesday morning.",
                "suggestedEmail": "Could we tour at 10:15 AM?",
            }],
            "response_email": "Thanks for the offer.",
        }

        offer_result = self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="I can show the property Tuesday morning.",
            proposal=offer,
            thread_ref=thread_ref,
            msg_id="msg-tour-offer",
            internet_message_id="<tour-offer@mock.test>",
            persist_handled_events=True,
        )

        self.assertIn("tour_requested", thread_ref._data["handledEvents"])
        offer_result["sendReply"].assert_not_called()

        # The operator reviewed the first action and sent a tour invite. The
        # original offer's handled-event record remains on the same thread.
        thread_ref._data.update({
            "status": processing.THREAD_STATUS["active"],
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
            "tourInvite": {
                "tourDate": "2026-06-23",
                "arrivalTime": "10:15 AM",
                "departureTime": "10:45 AM",
                "status": "sent",
            },
        })
        confirmation_result = self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body=(
                "Tuesday at 10:15 AM works for us. Confirmed. "
                "Let me know if you need directions for the tour."
            ),
            proposal={
                "updates": [],
                "events": [{
                    "type": "tour_requested",
                    "question": "Tuesday at 10:15 AM works for us. Confirmed.",
                    "suggestedEmail": "",
                }],
                "response_email": "Great, thanks for confirming.",
            },
            thread_ref=thread_ref,
            msg_id="msg-tour-confirmation",
            internet_message_id="<tour-confirmation@mock.test>",
            persist_handled_events=True,
        )

        confirmation_result["sendReply"].assert_not_called()
        self.assertEqual("confirmed", thread_ref._data["tourInvite.status"])
        self.assertEqual("confirmed", thread_ref._data["tourStatus"])
        confirmation_result["completeThreads"].assert_called_once()
        self.assertTrue(any(
            key.startswith("tour_requested:confirmed:")
            for key in thread_ref._data["handledEvents"]
        ))

    def test_dashboard_tour_action_handoff_closes_exact_final_confirmation(self):
        thread_id = "thread-production-dashboard-tour-handoff"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["paused"],
            "followUpStatus": "paused",
            "rowNumber": 3,
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
            "handledEvents": {
                "tour_requested": {
                    "detectedInMessageId": "msg-full-date-offer",
                    "notificationId": "notification-tour-1",
                },
            },
        })
        notification_ref = FakeDocumentRef({
            "kind": "action_needed",
            "email": "bp21harrison@gmail.com",
            "threadId": thread_id,
            "meta": {
                "reason": "tour_requested",
                "replyToMessageId": "msg-full-date-offer",
            },
        })
        client_ref = FakeDocumentRef({"status": "live"})
        client_ref._collections["notifications"] = FakeCollection(
            notification_ref,
            docs={"notification-tour-1": notification_ref},
        )
        action_resolution_ref = FakeDocumentRef({}, exists=False)
        client_ref._collections["actionResolutions"] = FakeCollection(
            action_resolution_ref,
            docs={"notification-tour-1": action_resolution_ref},
        )
        outbox_ref = FakeDocumentRef()
        outbox_ref.id = "outbox-dashboard-tour-reply"
        user_id = "NO7lVYVp6BaplKYEfMlWCgBnpdh2"
        outbox_data = {
            "assignedEmails": ["bp21harrison@gmail.com"],
            "clientId": "client-1",
            "notificationClientId": "client-1",
            "notificationId": "notification-tour-1",
            "deleteNotificationOnSend": True,
            "resumeThreadOnSend": True,
            "threadId": thread_id,
            "replyToMessageId": "msg-full-date-offer",
            "actionAuditId": "audit-dashboard-tour-reply",
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
            "processingBy": email_module.WORKER_ID,
        }
        outbox_ref._data.update(outbox_data)
        fake_fs = FakeFirestore(
            thread_ref,
            client_ref,
            thread_docs={thread_id: thread_ref},
        )

        with patch(
            "email_automation.clients._fs",
            fake_fs,
        ), patch.object(
            email_module,
            "delete_notification_and_decrement_counters",
        ), patch.object(
            email_module,
            "_get_sheet_id_or_fail",
            return_value="sheet-1",
        ), patch.object(email_module, "highlight_row"):
            resolution = email_module._reserve_dashboard_tour_action_resolution(
                user_id,
                outbox_ref,
                outbox_data,
            )
            self.assertEqual("reserved", resolution["status"])
            email_module._finalize_successful_outbox_item(
                user_id,
                outbox_ref,
                outbox_data,
                row_number=3,
                client_id="client-1",
                send_result={
                    "sent": ["bp21harrison@gmail.com"],
                    "sentMessageIds": {
                        "bp21harrison@gmail.com": "graph-dashboard-tour-reply",
                    },
                    "internetMessageIds": {
                        "bp21harrison@gmail.com": "<graph-dashboard-tour-reply@example.com>",
                    },
                    "conversationIds": {
                        "bp21harrison@gmail.com": "conv-dashboard-tour",
                    },
                },
                dashboard_tour_resolution=resolution,
            )

        final_body = (
            "Tuesday, August 11 at 2:00 PM works. I'll meet you at the front "
            "entrance. Confirmed. Let me know if you need directions for the tour."
        )
        final_proposal = ai_processing._augment_events_with_deterministic_signals(
            {
                "updates": [],
                "events": [{
                    "type": "tour_requested",
                    "reason": "tour_slot_reply",
                    "question": final_body,
                    "suggestedEmail": "",
                }],
                "response_email": None,
            },
            [{"direction": "inbound", "content": final_body}],
            target_anchor="912-930 Gemini St",
        )
        result = self._run_tour_invite_reply_processing(
            user_id=user_id,
            thread_id=thread_id,
            body=final_body,
            proposal=final_proposal,
            thread_ref=thread_ref,
            msg_id="msg-exact-final-tour-confirmation",
            internet_message_id="<exact-final-tour-confirmation@mock.test>",
            persist_handled_events=True,
            missing_required_fields=[],
        )

        self.assertEqual([], result["notifications"])
        result["sendReply"].assert_not_called()
        result["completeThreads"].assert_called_once_with(
            user_id,
            3,
            client_id="client-1",
            reason="tour_confirmed",
        )
        self.assertTrue(any(
            update["status"] == processing.THREAD_STATUS["completed"]
            and update["reason"] == "tour_confirmed"
            for update in result["statusUpdates"]
        ))
        self.assertEqual("confirmed", thread_ref._data["tourStatus"])
        self.assertEqual(1, len(result["handledEvents"]))
        self.assertTrue(result["handledEvents"][0]["eventKey"].startswith(
            "tour_requested:confirmed:"
        ))

    def test_reviewed_tour_invite_reschedule_survives_persisted_offer_dedupe(self):
        thread_id = "thread-tour-sequence-reschedule"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="We can show the property Tuesday morning.",
            proposal={
                "updates": [],
                "events": [{
                    "type": "tour_requested",
                    "question": "We can show the property Tuesday morning.",
                    "suggestedEmail": "Could we tour at 10:15 AM?",
                }],
                "response_email": None,
            },
            thread_ref=thread_ref,
            msg_id="msg-tour-offer",
            internet_message_id="<tour-offer@mock.test>",
            persist_handled_events=True,
        )
        thread_ref._data.update({
            "status": processing.THREAD_STATUS["active"],
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
            "tourInvite": {
                "tourDate": "2026-06-23",
                "arrivalTime": "10:15 AM",
                "departureTime": "10:45 AM",
                "status": "sent",
            },
        })

        result = self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="10:15 AM will not work. Can we do 11:45 AM instead?",
            proposal={
                "updates": [],
                "events": [{
                    "type": "tour_requested",
                    "question": "10:15 AM will not work. Can we do 11:45 AM instead?",
                    "suggestedEmail": "Let me check.",
                }],
                "response_email": "Thanks, I will make that change.",
            },
            thread_ref=thread_ref,
            msg_id="msg-tour-reschedule",
            internet_message_id="<tour-reschedule@mock.test>",
            persist_handled_events=True,
        )

        result["sendReply"].assert_not_called()
        self.assertEqual(1, len(result["notifications"]))
        self.assertEqual(
            "tour_reschedule_requested",
            result["notifications"][0]["kwargs"]["meta"]["reason"],
        )
        self.assertEqual("alternate_requested", thread_ref._data["tourInvite.status"])
        self.assertTrue(any(
            key.startswith("tour_requested:alternate_requested:")
            for key in thread_ref._data["handledEvents"]
        ))

    def test_tour_notification_failure_is_retryable_without_send_or_completion(self):
        send_reply = MagicMock(return_value=True)
        complete_threads = MagicMock(return_value=1)
        mark_client_completed = MagicMock(return_value=True)
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })

        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "tour handoff",
        ):
            self._run_tour_invite_reply_processing(
                thread_id="thread-tour-notification-failure",
                body="I can show the property Tuesday morning.",
                proposal={
                    "updates": [],
                    "events": [{
                        "type": "tour_requested",
                        "question": "I can show the property Tuesday morning.",
                        "suggestedEmail": "Could we tour at 10:15 AM?",
                    }],
                    "response_email": "Thanks, Tuesday should work.",
                },
                thread_ref=thread_ref,
                notification_error=RuntimeError("notification create unavailable"),
                send_reply_mock=send_reply,
                complete_threads_mock=complete_threads,
                mark_client_completed_mock=mark_client_completed,
            )

        send_reply.assert_not_called()
        complete_threads.assert_not_called()
        mark_client_completed.assert_not_called()

    def test_tour_thread_state_failure_is_retryable_without_send_or_completion(self):
        send_reply = MagicMock(return_value=True)
        complete_threads = MagicMock(return_value=1)
        mark_client_completed = MagicMock(return_value=True)
        thread_ref = FakeDocumentRef(
            {
                "clientId": "client-1",
                "email": ["bp21harrison@gmail.com"],
                "status": processing.THREAD_STATUS["active"],
                "rowNumber": 3,
                "source": "dashboard_tour_planner",
                "actionType": "tour_invite",
                "tourInvite": {
                    "tourDate": "2026-06-23",
                    "arrivalTime": "10:15 AM",
                    "departureTime": "10:45 AM",
                    "status": "sent",
                },
            },
            update_error=RuntimeError("thread handoff update unavailable"),
        )

        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "tour handoff",
        ):
            self._run_tour_invite_reply_processing(
                thread_id="thread-tour-state-failure",
                body="10:15 AM will not work. Can we do 11:45 AM instead?",
                proposal={
                    "updates": [],
                    "events": [{
                        "type": "tour_requested",
                        "question": "10:15 AM will not work. Can we do 11:45 AM instead?",
                        "suggestedEmail": "Let me check.",
                    }],
                    "response_email": "Thanks, I will make that change.",
                },
                thread_ref=thread_ref,
                send_reply_mock=send_reply,
                complete_threads_mock=complete_threads,
                mark_client_completed_mock=mark_client_completed,
            )

        send_reply.assert_not_called()
        complete_threads.assert_not_called()
        mark_client_completed.assert_not_called()

    def test_three_event_inbound_surfaces_every_human_action_and_never_auto_sends(self):
        body = (
            "What use does your client have in mind, and how much power do they need? "
            "We can set a tour for Tuesday at 10:30. "
            "Please call me at 713-555-0186."
        )
        thread_id = "thread-three-event-production"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        proposal = {
            "updates": [],
            "events": [
                {
                    "type": "needs_user_input",
                    "reason": "client_question",
                    "question": "What use and power does the client need?",
                },
                {
                    "type": "tour_requested",
                    "question": "We can set a tour for Tuesday at 10:30.",
                    "suggestedEmail": "Hi Ryan, Tuesday at 10:30 works.",
                },
                {
                    "type": "call_requested",
                    "question": "Please call me at 713-555-0186.",
                },
            ],
            "response_email": "Hi Ryan,\n\nThanks. I will follow up.",
        }

        result = self._run_tour_invite_reply_processing(
            user_id="regular-user",
            thread_id=thread_id,
            body=body,
            proposal=proposal,
            thread_ref=thread_ref,
        )

        reasons = [
            notification["kwargs"]["meta"]["reason"]
            for notification in result["notifications"]
        ]
        self.assertCountEqual(
            [
                "needs_user_input:client_question",
                "tour_requested",
                "call_requested",
            ],
            reasons,
        )
        self.assertEqual(3, len(result["notifications"]))
        self.assertEqual(3, len(result["handledEvents"]))
        self.assertEqual(3, len(result["statusUpdates"]))
        self.assertTrue(all(
            update["status"] == processing.THREAD_STATUS["paused"]
            for update in result["statusUpdates"]
        ))
        result["sendReply"].assert_not_called()

    def test_tour_invite_unavailable_process_does_not_move_row_or_stop_property(self):
        body = "Hi Baylor,\n\nThe space is still available, but tours are no longer available for this property.\n\nBest,\nBP21"
        thread_id = "thread-tour-unavailable"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
            "tourInvite": {
                "tourDate": "2026-06-23",
                "arrivalTime": "9:00 AM",
                "departureTime": "9:30 AM",
            },
        })
        proposal = {
            "updates": [],
            "events": [
                {"type": "property_unavailable", "reason": "leased"},
                {
                    "type": "tour_requested",
                    "question": "Tours are no longer available for this property.",
                    "suggestedEmail": "",
                },
            ],
            "response_email": None,
        }

        result = self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body=body,
            proposal=proposal,
            thread_ref=thread_ref,
        )

        result["moveRow"].assert_not_called()
        result["stopThreads"].assert_not_called()
        self.assertEqual(1, len(result["notifications"]))
        meta = result["notifications"][0]["kwargs"]["meta"]
        self.assertEqual("tour_unavailable", meta["reason"])
        self.assertEqual("tour_unavailable", meta["tourReplyClassification"]["outcome"])
        self.assertIn("tours are unavailable", meta["suggestedEmail"]["body"].lower())
        self.assertEqual("tour_unavailable", thread_ref._data["tourInvite.status"])
        self.assertEqual("2026-06-23", thread_ref._data["tourInvite.tourDate"])

    def test_tour_invite_declined_reply_preserves_date_and_drafts_operator_hold(self):
        body = "Hi Baylor,\n\nWe can't show the space at that time anymore.\n\nBest,\nBP21"
        thread_id = "thread-tour-declined"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
            "tourInvite": {
                "tourDate": "2026-06-23",
                "arrivalTime": "9:00 AM",
                "departureTime": "9:30 AM",
            },
        })
        proposal = {
            "updates": [],
            "events": [
                {
                    "type": "tour_requested",
                    "question": "We can't show the space at that time anymore.",
                    "suggestedEmail": "",
                }
            ],
            "response_email": None,
        }

        result = self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body=body,
            proposal=proposal,
            thread_ref=thread_ref,
        )

        self.assertEqual(1, len(result["notifications"]))
        meta = result["notifications"][0]["kwargs"]["meta"]
        self.assertEqual("tour_slot_declined", meta["reason"])
        self.assertEqual("declined", meta["tourReplyClassification"]["outcome"])
        self.assertIn("Tuesday, June 23, 2026", meta["suggestedEmail"]["body"])
        result["sendReply"].assert_not_called()
        self.assertEqual("declined", thread_ref._data["tourInvite.status"])
        self.assertEqual("2026-06-23", thread_ref._data["tourInvite.tourDate"])

    def test_tour_invite_confirmation_does_not_send_generic_completion_reply(self):
        user_id = "NO7lVYVp6BaplKYEfMlWCgBnpdh2"
        client_id = "client-1"
        thread_id = "thread-tour-confirmed"
        from_email = "bp21harrison@gmail.com"
        body = "Hi John,\n\n10:16 AM works for 1561 Live Oak St. Confirmed.\n\nBest,\nBP21"
        msg = {
            "id": "msg-tour-confirmed",
            "subject": "RE: Tour slot: 1561 Live Oak St at 10:16 AM",
            "from": {"emailAddress": {"address": from_email, "name": "BP21"}},
            "toRecipients": [{"emailAddress": {"address": "baylor.freelance@outlook.com"}}],
            "internetMessageId": "<tour-confirmed@mock.test>",
            "conversationId": "conv-tour-confirmed",
            "receivedDateTime": "2026-06-19T19:12:39Z",
            "bodyPreview": body,
            "hasAttachments": False,
            "internetMessageHeaders": [
                {"name": "In-Reply-To", "value": "<tour-invite@mock.test>"},
            ],
        }
        header = [
            "Property Address",
            "City",
            "Leasing Contact",
            "Email",
            "Total SF",
            "Rent/SF/Yr",
            "Ops Ex / SF",
            "Drive Ins",
            "Ceiling Ht",
            "Power",
        ]
        rowvals = [
            "1561 Live Oak St",
            "Webster",
            "Tram Kim",
            "bp21harrison+leaguecity-row05@gmail.com",
            "5000",
            "12.00",
            "3.84",
            "2",
            "20",
            "480V 3-phase",
        ]
        proposal = {
            "updates": [],
            "events": [
                {
                    "type": "tour_requested",
                    "question": "10:16 AM works for 1561 Live Oak St. Confirmed.",
                    "suggestedEmail": "Hi Tram,\n\n10:16 AM works for 1561 Live Oak St. Confirmed.\n\nThanks,",
                }
            ],
            "response_email": None,
        }
        thread_ref = FakeDocumentRef(
            {
                "clientId": client_id,
                "email": [from_email],
                "status": processing.THREAD_STATUS["active"],
                "rowNumber": 5,
                "source": "dashboard_tour_planner",
                "actionType": "tour_invite",
                "tourInvite": {"arrivalTime": "10:16 AM", "departureTime": "10:46 AM"},
            }
        )
        client_ref = FakeDocumentRef({"criteria": "Industrial search"})

        class FakeExecute:
            def __init__(self, payload):
                self.payload = payload

            def execute(self):
                return self.payload

        class FakeValues:
            def get(self, spreadsheetId=None, range=None):
                if range and range.endswith("A:A"):
                    return FakeExecute({"values": [["Property Address"], ["1561 Live Oak St"]]})
                return FakeExecute({"values": [rowvals]})

        class FakeSpreadsheets:
            def values(self):
                return FakeValues()

        class FakeSheets:
            def spreadsheets(self):
                return FakeSpreadsheets()

        full_body_response = MagicMock()
        full_body_response.json.return_value = {
            "body": {"content": body, "contentType": "Text"},
            "hasAttachments": False,
        }
        me_response = MagicMock(status_code=200)
        me_response.json.return_value = {"mail": "baylor.freelance@outlook.com"}

        send_reply_patcher = patch.object(processing, "send_reply_in_thread", return_value=True)
        patches = [
            patch.object(processing, "_fs", FakeFirestore(thread_ref, client_ref)),
            patch.object(processing, "exponential_backoff_request", return_value=full_body_response),
            patch.object(processing.requests, "get", return_value=me_response),
            patch.object(processing, "lookup_thread_by_message_id", return_value=thread_id),
            patch.object(processing, "lookup_thread_by_conversation_id", return_value=None),
            patch.object(processing, "get_thread_status", return_value=processing.THREAD_STATUS["active"]),
            patch.object(processing, "save_message", return_value=True),
            patch.object(processing, "index_message_id", return_value=True),
            patch.object(processing, "dump_thread_from_firestore"),
            patch("email_automation.followup.cancel_followup_on_response"),
            patch.object(
                processing,
                "fetch_and_log_sheet_for_thread",
                return_value=(client_id, "sheet-1", header, 5, rowvals, None, []),
            ),
            patch.object(
                processing,
                "_resolve_reply_identity",
                return_value={
                    "recipient_email": from_email,
                    "contact_name": "Tram",
                    "original_email": from_email,
                    "source": "test",
                },
            ),
            patch.object(processing, "fetch_and_process_pdfs", return_value=[]),
            patch.object(processing, "write_message_order_test"),
            patch.object(processing, "fetch_url_as_text", return_value=None),
            patch.object(processing, "propose_sheet_updates", return_value=proposal),
            patch.object(processing, "_sheets_client", return_value=FakeSheets()),
            patch.object(processing, "_get_first_tab_title", return_value="Sheet1"),
            patch.object(processing, "is_event_handled", return_value=False),
            patch.object(processing, "mark_event_handled"),
            patch.object(processing, "update_thread_status"),
            patch.object(processing, "complete_threads_for_row", return_value=1),
            patch.object(processing, "_clear_thread_action_notifications"),
            patch.object(processing, "_maybe_mark_client_completed"),
            patch.object(processing, "check_missing_required_fields", return_value=[]),
            patch.object(processing, "write_notification"),
            send_reply_patcher,
        ]

        started = [patcher.start() for patcher in patches]
        send_reply = started[-1]
        try:
            processing.process_inbox_message(
                user_id,
                {"Authorization": "Bearer test-token"},
                msg,
            )
        finally:
            for patcher in reversed(patches):
                patcher.stop()

        send_reply.assert_not_called()
        self.assertEqual("confirmed", thread_ref._data["tourStatus"])
        self.assertEqual("confirmed", thread_ref._data["tourInvite.status"])
        self.assertEqual(processing.SERVER_TIMESTAMP, thread_ref._data["tourInvite.confirmedAt"])
        self.assertEqual("Broker confirmed the requested tour slot.", thread_ref._data["tourInvite.lastReplyDetails"])

    def test_quote_only_blank_reply_is_saved_without_ai_or_followup_side_effects(self):
        user_id = "test-user"
        client_id = "client-1"
        thread_id = "thread-tour-invite"
        from_email = "bp21harrison@gmail.com"
        quoted_original = (
            "On Fri, Jun 19, 2026 at 10:58 AM Baylor Harrison "
            "<baylor.freelance@outlook.com> wrote:\n\n"
            "Hi Ryan,\n\n"
            "I am planning a tour for 912-930 Gemini St.\n"
            "Requested arrival: 9:38 AM\n"
            "Expected departure: 10:08 AM\n"
            "Tour length: 30 minutes\n\n"
            "Please confirm whether this tour slot works, or reply with the closest available alternate."
        )
        msg = {
            "id": "msg-blank-reply",
            "subject": "RE: Tour slot: 912-930 Gemini St at 9:38 AM",
            "from": {"emailAddress": {"address": from_email, "name": "BP21"}},
            "toRecipients": [{"emailAddress": {"address": "baylor.freelance@outlook.com"}}],
            "internetMessageId": "<blank-reply@mock.test>",
            "conversationId": "conv-tour",
            "receivedDateTime": "2026-06-19T18:38:00Z",
            "bodyPreview": quoted_original[:200],
            "hasAttachments": False,
            "internetMessageHeaders": [
                {"name": "In-Reply-To", "value": "<tour-invite@mock.test>"},
            ],
        }
        thread_ref = FakeDocumentRef(
            {
                "clientId": client_id,
                "email": [from_email],
                "status": processing.THREAD_STATUS["active"],
                "rowNumber": 3,
                "source": "dashboard_tour_planner",
                "actionType": "tour_invite",
            }
        )
        client_ref = FakeDocumentRef({"criteria": "Industrial search"})
        full_body_response = MagicMock()
        full_body_response.json.return_value = {
            "body": {"content": quoted_original, "contentType": "Text"},
            "hasAttachments": False,
        }
        me_response = MagicMock(status_code=200)
        me_response.json.return_value = {"mail": "baylor.freelance@outlook.com"}

        with patch.object(processing, "_fs", FakeFirestore(thread_ref, client_ref)), \
             patch.object(processing, "exponential_backoff_request", return_value=full_body_response), \
             patch.object(processing.requests, "get", return_value=me_response), \
             patch.object(processing, "lookup_thread_by_message_id", return_value=thread_id), \
             patch.object(processing, "lookup_thread_by_conversation_id", return_value=None), \
             patch.object(processing, "get_thread_status", return_value=processing.THREAD_STATUS["active"]), \
             patch.object(processing, "save_message", return_value=True) as save_message, \
             patch.object(processing, "index_message_id", return_value=True), \
             patch.object(processing, "dump_thread_from_firestore") as dump_thread, \
             patch("email_automation.followup.cancel_followup_on_response") as cancel_followup, \
             patch.object(processing, "fetch_and_log_sheet_for_thread") as fetch_sheet, \
             patch.object(processing, "propose_sheet_updates") as propose_sheet_updates:
            processing.process_inbox_message(
                user_id,
                {"Authorization": "Bearer test-token"},
                msg,
            )

        save_message.assert_called_once()
        cancel_followup.assert_not_called()
        dump_thread.assert_not_called()
        fetch_sheet.assert_not_called()
        propose_sheet_updates.assert_not_called()

    def test_nonviable_with_replacement_and_tour_does_not_pause_old_row_for_tour(self):
        user_id = "test-user"
        client_id = "client-1"
        thread_id = "thread-19241"
        from_email = "bp21harrison+19241@gmail.com"
        body = (
            "This space wouldn't be a good fit for your client as it is more "
            "office heavy as opposed to a true warehouse with drive in space. "
            "27610 Commerce Oaks Dr could work and I can tour it Wednesday."
        )
        msg = {
            "id": "msg-1",
            "subject": "RE: 19241 David Memorial Dr, The Woodlands",
            "from": {"emailAddress": {"address": from_email, "name": "BP21 Broker"}},
            "toRecipients": [{"emailAddress": {"address": "baylor.freelance@outlook.com"}}],
            "internetMessageId": "<inbound-msg-1@mock.test>",
            "conversationId": "conv-19241",
            "receivedDateTime": "2026-06-17T08:00:00Z",
            "sentDateTime": "2026-06-17T08:00:00Z",
            "bodyPreview": body[:200],
            "internetMessageHeaders": [
                {"name": "In-Reply-To", "value": "<outbound-msg-1@mock.test>"},
            ],
        }
        header = [
            "Property Address",
            "City",
            "Leasing Contact",
            "Leasing Company",
            "Comments",
        ]
        rowvals = [
            "19241 David Memorial Dr",
            "The Woodlands",
            "BP21 Broker",
            "Example Brokerage",
            "",
        ]
        proposal = {
            "updates": [],
            "events": [
                {"type": "property_unavailable", "reason": "requirements_mismatch"},
                {
                    "type": "new_property",
                    "address": "27610 Commerce Oaks Dr",
                    "city": None,
                    "email": None,
                    "contactName": None,
                    "notes": None,
                },
                {
                    "type": "tour_requested",
                    "question": "I can tour it Wednesday.",
                    "suggestedEmail": "Wednesday works for us.",
                },
            ],
            "response_email": "Thanks for the update. I will review the alternate.",
        }
        thread_ref = FakeDocumentRef(
            {
                "clientId": client_id,
                "email": [from_email],
                "status": processing.THREAD_STATUS["active"],
                "rowNumber": 3,
            }
        )
        client_ref = FakeDocumentRef({"criteria": "Industrial search"})

        full_body_response = MagicMock()
        full_body_response.json.return_value = {
            "body": {"content": body, "contentType": "Text"}
        }
        me_response = MagicMock(status_code=200)
        me_response.json.return_value = {"mail": "baylor.freelance@outlook.com"}

        notifications = []
        handled_events = []
        status_updates = []
        send_reply = MagicMock(return_value=True)

        def fake_write_notification(*args, **kwargs):
            notif_id = f"notif-{len(notifications) + 1}"
            notifications.append({"args": args, "kwargs": kwargs, "id": notif_id})
            return notif_id

        def fake_mark_event_handled(_user_id, _thread_id, event_key, _msg_id, notif_id):
            handled_events.append({"eventKey": event_key, "notifId": notif_id})

        def fake_update_thread_status(_user_id, _thread_id, status, reason):
            status_updates.append({"status": status, "reason": reason})

        patches = [
            patch.object(
                processing,
                "_fs",
                FakeFirestore(
                    thread_ref,
                    client_ref,
                    thread_docs={thread_id: thread_ref},
                ),
            ),
            patch.object(processing, "exponential_backoff_request", return_value=full_body_response),
            patch.object(processing.requests, "get", return_value=me_response),
            patch.object(processing, "lookup_thread_by_message_id", return_value=thread_id),
            patch.object(processing, "lookup_thread_by_conversation_id", return_value=None),
            patch.object(processing, "get_thread_status", return_value=processing.THREAD_STATUS["active"]),
            patch.object(processing, "save_message", return_value=True),
            patch.object(processing, "index_message_id", return_value=True),
            patch.object(processing, "dump_thread_from_firestore"),
            patch("email_automation.followup.cancel_followup_on_response"),
            patch.object(
                processing,
                "fetch_and_log_sheet_for_thread",
                return_value=(client_id, "sheet-1", header, 3, rowvals, None, []),
            ),
            patch.object(
                processing,
                "_resolve_reply_identity",
                return_value={
                    "recipient_email": from_email,
                    "contact_name": "BP21 Broker",
                    "original_email": from_email,
                    "source": "test",
                },
            ),
            patch.object(processing, "fetch_and_process_pdfs", return_value=[]),
            patch.object(processing, "write_message_order_test"),
            patch.object(processing, "fetch_url_as_text", return_value=None),
            patch.object(processing, "propose_sheet_updates", return_value=proposal),
            patch.object(processing, "_sheets_client", return_value=MagicMock()),
            patch.object(processing, "_get_first_tab_title", return_value="Sheet1"),
            patch.object(processing, "is_event_handled", return_value=False),
            patch.object(processing, "write_notification", side_effect=fake_write_notification),
            patch.object(processing, "mark_event_handled", side_effect=fake_mark_event_handled),
            patch.object(processing, "ensure_nonviable_divider", return_value=10),
            patch.object(processing, "move_row_below_divider", return_value=11),
            patch.object(processing, "sync_thread_row_numbers_after_move"),
            patch.object(processing, "stop_threads_for_row", return_value=1),
            patch.object(processing, "find_notes_comment_column_index", return_value=None),
            patch.object(processing, "format_sheet_columns_autosize_with_exceptions"),
            patch.object(processing, "clear_row_highlight"),
            patch.object(processing, "_property_exists_in_sheet", return_value=False),
            patch.object(
                processing,
                "build_new_property_suggested_email",
                return_value={
                    "to": [from_email],
                    "subject": "27610 Commerce Oaks Dr",
                    "body": "Hi BP21 Broker, can you send details?",
                },
            ),
            patch.object(processing, "send_reply_in_thread", new=send_reply),
            patch.object(processing, "update_thread_status", side_effect=fake_update_thread_status),
            patch.object(processing, "_maybe_mark_client_completed"),
        ]

        for patcher in patches:
            patcher.start()
        try:
            processing.process_inbox_message(
                user_id,
                {"Authorization": "Bearer test-token"},
                msg,
            )
        finally:
            for patcher in reversed(patches):
                patcher.stop()

        notification_kinds = [
            item["kwargs"].get("kind") for item in notifications
        ]
        action_reasons = [
            (item["kwargs"].get("meta") or {}).get("reason")
            for item in notifications
            if item["kwargs"].get("kind") == "action_needed"
        ]

        self.assertIn("property_unavailable", notification_kinds)
        self.assertIn("new_property_pending_approval", action_reasons)
        self.assertNotIn("tour_requested", action_reasons)
        send_reply.assert_called_once()
        self.assertFalse(
            any(
                update["status"] == processing.THREAD_STATUS["paused"]
                and update["reason"] == "tour_requested"
                for update in status_updates
            )
        )
        self.assertTrue(
            any(
                handled["eventKey"] == "tour_requested"
                or handled["eventKey"].startswith("tour_requested:")
                and handled["notifId"] is None
                for handled in handled_events
            ),
            "stale tour event should be marked handled without a dashboard notification",
        )


if __name__ == "__main__":
    unittest.main()
