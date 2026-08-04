import os
import unittest
import hashlib
import json
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event, RLock
from unittest.mock import MagicMock, patch
from googleapiclient.errors import HttpError

os.environ.setdefault("E2E_TEST_MODE", "true")

with patch("google.cloud.firestore.Client", return_value=MagicMock()):
    from email_automation import (
        campaign_safety,
        clients,
        pending_responses,
        processing,
        send_permits,
        sheet_operations,
    )
    from email_automation import sheets as sheets_module


class FakeSnapshot:
    def __init__(self, data=None, exists=True):
        self._data = data or {}
        self.exists = exists

    def to_dict(self):
        return dict(self._data)


class FakeDocumentRef:
    def __init__(
        self,
        data=None,
        exists=True,
        update_error=None,
        get_error=None,
        doc_id=None,
    ):
        self._data = data or {}
        self._exists = exists
        self._update_error = update_error
        self._get_error = get_error
        self.id = doc_id
        self.reference = self
        self._version = 0
        self._lock = RLock()
        self._subcollections = {}

    def get(self, transaction=None):
        if self._get_error:
            raise self._get_error
        if transaction is not None:
            return transaction.get(self)
        with self._lock:
            return FakeSnapshot(self._data, self._exists)

    def set(self, data, merge=False):
        with self._lock:
            if merge:
                self._data.update(data)
            else:
                self._data = dict(data)
            self._exists = True
            self._version += 1

    def collection(self, name):
        return self._subcollections.setdefault(
            name,
            FakeCollection(
                FakeDocumentRef({}, exists=False),
                docs={},
                retain_missing=True,
            ),
        )

    def update(self, data):
        with self._lock:
            if self._update_error:
                raise self._update_error
            self._data.update(data)
            self._version += 1

    def delete(self):
        with self._lock:
            self._data = {}
            self._exists = False
            self._version += 1


class FakeQuerySnapshot(FakeSnapshot):
    def __init__(self, doc_id, data=None, exists=True, reference=None):
        super().__init__(data, exists)
        self.id = doc_id
        self.reference = reference


class FakeQuery:
    def __init__(self, docs):
        self.docs = docs or {}

    def limit(self, _count):
        return self

    def stream(self):
        return [
            FakeQuerySnapshot(
                doc_id,
                doc_ref._data,
                doc_ref._exists,
                reference=doc_ref,
            )
            for doc_id, doc_ref in self.docs.items()
            if doc_ref._exists
        ]


class FakeUserRef:
    def __init__(
        self,
        thread_ref,
        client_ref,
        thread_docs=None,
        pending_response_docs=None,
    ):
        self.thread_ref = thread_ref
        self.client_ref = client_ref
        self.thread_docs = thread_docs or {}
        self.pending_response_docs = (
            pending_response_docs if pending_response_docs is not None else {}
        )

    def collection(self, name):
        if name == "threads":
            return FakeCollection(self.thread_ref, docs=self.thread_docs)
        if name == "clients":
            return FakeCollection(self.client_ref)
        if name == "pendingResponses":
            return FakeCollection(
                FakeDocumentRef({}, exists=False),
                docs=self.pending_response_docs,
                retain_missing=True,
            )
        return FakeCollection(FakeDocumentRef({}, exists=False))


class FakeCollection:
    def __init__(self, doc_ref, docs=None, retain_missing=False):
        self.doc_ref = doc_ref
        self.docs = docs if docs is not None else {}
        self.retain_missing = retain_missing

    def document(self, *args):
        doc_id = str(args[0]) if args else ""
        if doc_id and doc_id in self.docs:
            self.docs[doc_id].id = doc_id
            return self.docs[doc_id]
        if doc_id and self.retain_missing:
            ref = FakeDocumentRef({}, exists=False, doc_id=doc_id)
            self.docs[doc_id] = ref
            return ref
        if doc_id and getattr(self.doc_ref, "id", None) is None:
            setattr(self.doc_ref, "id", doc_id)
        return self.doc_ref

    def where(self, *args, **kwargs):
        return FakeQuery(self.docs)

    def stream(self):
        return FakeQuery(self.docs).stream()


class FakeWriteBatch:
    def __init__(self, commit_error=None, *, apply_before_error=False, max_writes=500):
        self._updates = []
        self._commit_error = commit_error
        self._apply_before_error = apply_before_error
        self._max_writes = max_writes

    def update(self, document_ref, data):
        self._updates.append((document_ref, data))

    def commit(self):
        if len(self._updates) > self._max_writes:
            raise RuntimeError(
                f"Firestore batch has {len(self._updates)} writes; limit is {self._max_writes}"
            )
        if self._commit_error and not self._apply_before_error:
            raise self._commit_error
        for document_ref, _data in self._updates:
            if document_ref._update_error:
                raise document_ref._update_error
        for document_ref, data in self._updates:
            document_ref.update(data)
        if self._commit_error:
            raise self._commit_error


class FakeTransaction:
    def __init__(self, firestore, read_barrier=None):
        self._firestore = firestore
        self._read_barrier = read_barrier
        self._barrier_used = False
        self._reads = {}
        self._updates = []
        self._deletes = []
        self._sets = []
        self._before_update_barrier_used = False

    def get(self, document_ref):
        with self._firestore._transaction_lock:
            snapshot = FakeSnapshot(document_ref._data, document_ref._exists)
            self._reads[document_ref] = document_ref._version
        if self._read_barrier is not None and not self._barrier_used:
            self._barrier_used = True
            self._read_barrier.wait()
        return snapshot

    def update(self, document_ref, data):
        events = self._firestore.transaction_before_update_events
        if events is not None and not self._before_update_barrier_used:
            self._before_update_barrier_used = True
            entered, release = events
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("transaction update barrier timed out")
        self._updates.append((document_ref, data))

    def delete(self, document_ref):
        self._deletes.append(document_ref)

    def set(self, document_ref, data):
        self._sets.append((document_ref, data))

    def commit(self):
        write_count = len(self._updates) + len(self._sets) + len(self._deletes)
        self._firestore.transaction_write_counts.append(write_count)
        if write_count > self._firestore.max_transaction_writes:
            raise RuntimeError(
                f"Firestore transaction has {write_count} writes; "
                f"limit is {self._firestore.max_transaction_writes}"
            )
        behavior = self._firestore._take_transaction_commit_behavior(
            self._updates
        )
        commit_error = behavior.get("error") if behavior else None
        apply_before_error = bool(
            behavior and behavior.get("applyBeforeError")
        )
        after_apply = behavior.get("afterApply") if behavior else None
        if commit_error and not apply_before_error:
            raise commit_error
        with self._firestore._transaction_lock:
            for document_ref, read_version in self._reads.items():
                if document_ref._version != read_version:
                    raise RuntimeError("transaction read/write conflict")
            for document_ref, _data in self._updates:
                if document_ref._update_error:
                    raise document_ref._update_error
            for document_ref, data in self._updates:
                document_ref._data.update(data)
                document_ref._version += 1
            for document_ref, data in self._sets:
                document_ref._data = dict(data)
                document_ref._exists = True
                document_ref._version += 1
            for document_ref in self._deletes:
                document_ref._data = {}
                document_ref._exists = False
                document_ref._version += 1
        if after_apply is not None:
            after_apply()
        if commit_error:
            raise commit_error


class FakeFirestore:
    def __init__(
        self,
        thread_ref,
        client_ref,
        thread_docs=None,
        batch_commit_errors=None,
        batch_commit_behaviors=None,
        transaction_read_barrier=None,
        transaction_commit_behaviors_by_field=None,
        transaction_before_update_events=None,
        pending_response_docs=None,
        max_transaction_writes=500,
    ):
        self.thread_ref = thread_ref
        self.client_ref = client_ref
        self.thread_docs = thread_docs or {}
        self.pending_response_docs = (
            pending_response_docs if pending_response_docs is not None else {}
        )
        self.batch_commit_errors = list(batch_commit_errors or [])
        self.batch_commit_behaviors = list(batch_commit_behaviors or [])
        self.transaction_read_barrier = transaction_read_barrier
        self.transaction_commit_behaviors_by_field = {
            field: list(behaviors)
            for field, behaviors in (
                transaction_commit_behaviors_by_field or {}
            ).items()
        }
        self.transaction_before_update_events = transaction_before_update_events
        self.max_transaction_writes = max_transaction_writes
        self.transaction_write_counts = []
        self._transaction_lock = RLock()
        self.client_ref._data.update({
            "status": "live",
            "automationPaused": False,
        })

    def collection(self, name):
        if name == "users":
            return FakeCollection(FakeUserRef(
                self.thread_ref,
                self.client_ref,
                self.thread_docs,
                self.pending_response_docs,
            ))
        if name == "systemConfig":
            return FakeCollection(FakeDocumentRef({
                "automationEnabled": True,
                "allowedUids": [],
            }))
        return FakeCollection(FakeDocumentRef({}, exists=False))

    def batch(self):
        if self.batch_commit_behaviors:
            behavior = self.batch_commit_behaviors.pop(0) or {}
            return FakeWriteBatch(
                commit_error=behavior.get("error"),
                apply_before_error=bool(behavior.get("applyBeforeError")),
            )
        commit_error = self.batch_commit_errors.pop(0) if self.batch_commit_errors else None
        return FakeWriteBatch(commit_error=commit_error)

    def transaction(self):
        return FakeTransaction(self, self.transaction_read_barrier)

    def _take_transaction_commit_behavior(self, updates):
        written_fields = {
            field
            for _document_ref, data in updates
            for field in data
        }
        for field, behaviors in self.transaction_commit_behaviors_by_field.items():
            if field in written_fields and behaviors:
                return behaviors.pop(0) or {}
        return {}


def _prepare_and_consume_test_graph_capability(capability):
    permit = send_permits.read_permit(capability)
    source_id = permit["sourceGraphMessageId"]
    draft_id = f"draft-{capability.permit_id}"
    subject = "RE: Compound processing test subject"
    html_body = "<p>Prepared test reply.</p>"
    recipients = [permit["recipient"]]
    send_permits.begin_graph_draft_creation(capability, source_id)
    send_permits.complete_graph_draft_creation(
        capability,
        draft_id=draft_id,
        outcome="created",
        evidence={
            "httpStatus": 201,
            "phase": "create_reply",
            "draftId": draft_id,
        },
    )
    prepared = send_permits.begin_graph_draft_patch(
        capability,
        source_graph_message_id=source_id,
        draft_id=draft_id,
        subject=subject,
        html_body=html_body,
        to_recipients=recipients,
        cc_recipients=[],
        attachments=[],
    )
    send_permits.complete_graph_draft_patch(
        capability,
        prepared_envelope_hash=prepared["preparedEnvelopeHash"],
        outcome="applied",
        evidence={
            "httpStatus": 204,
            "phase": "patch_draft",
            "draftId": draft_id,
            "preparedEnvelopeHash": prepared["preparedEnvelopeHash"],
        },
    )
    send_permits.finalize_graph_draft_preparation(
        capability,
        prepared_envelope_hash=prepared["preparedEnvelopeHash"],
    )
    return send_permits.consume_graph_send_capability(
        capability,
        source_graph_message_id=source_id,
        draft_id=draft_id,
        subject=subject,
        html_body=html_body,
        to_recipients=recipients,
        cc_recipients=[],
        attachments=[],
    )


def _exact_sent_evidence_for_test(permit):
    prepared = permit["preparedEnvelope"]
    return {
        "id": prepared["draftId"],
        "sentMessageId": prepared["draftId"],
        "internetMessageId": f"<sent-{permit['permitId']}@mock.test>",
        "isDraft": False,
        "subject": prepared["subject"],
        "recipient": permit["recipient"],
        "bodyHash": permit["bodyHash"],
        "conversationId": permit.get("conversationId"),
        "sentDateTime": permit["requestStartedAt"] + timedelta(seconds=1),
        "permitId": permit["permitId"],
        "sourceGraphMessageId": permit["sourceGraphMessageId"],
        "preparedEnvelopeHash": prepared["preparedEnvelopeHash"],
        "toRecipients": [
            {"emailAddress": {"address": address}}
            for address in prepared["toRecipients"]
        ],
        "ccRecipients": [
            {"emailAddress": {"address": address}}
            for address in prepared["ccRecipients"]
        ],
        "bccRecipients": [],
        "body": {
            "contentType": "HTML",
            "content": "<p>Prepared test reply.</p>",
        },
        "attachments": [],
    }


def _record_successful_test_graph_send(*args, **kwargs):
    capability = kwargs.get("graph_send_capability")
    if not isinstance(capability, send_permits.GraphSendCapability):
        raise AssertionError("test send requires a typed Graph send capability")
    _prepare_and_consume_test_graph_capability(capability)
    send_permits.resolve_graph_send_permit(
        capability,
        "accepted",
        evidence={"httpStatus": 202, "phase": "send"},
    )
    permit = send_permits.read_permit(capability)
    processing._set_reply_send_outcome(
        outcome="sent_indexed",
        conversation_id=permit.get("conversationId"),
        exact_sent_evidence=_exact_sent_evidence_for_test(permit),
    )
    return True


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
        self.campaign_decision = self._campaign_gate.start()

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
        contact_name="Ryan",
        from_email="bp21harrison@gmail.com",
        row_below_nonviable=False,
        sheet_id_override=None,
        tab_title_override=None,
        extra_header_before_notes=None,
        reply_recipient_override=None,
        campaign_status="live",
        campaign_automation_paused=False,
        ensure_divider_side_effect=None,
        divider_preview_exists=True,
        divider_preview_side_effect=None,
        move_row_side_effect=None,
        notes_header="Notes",
        existing_note="",
        note_read_error=None,
        note_write_error=None,
        sync_thread_count=None,
        stop_thread_count=None,
        mark_event_handled_result=True,
        update_thread_status_result=True,
        sheet_attempt_error=None,
        finalization_error=None,
        finalization_apply_then_error=None,
        notification_errors=None,
        reply_outcome_update_error=None,
        queue_outcome_update_error=None,
        sent_reply_match=None,
        recovery_external_error=None,
        msg_id_override=None,
        internet_message_id_override=None,
        send_result=True,
        pending_response_docs=None,
        honor_handled_events=False,
        capture=None,
    ):
        client_id = "client-1"
        msg = self._common_graph_message(
            msg_id=msg_id_override or f"msg-{thread_id}",
            subject=f"RE: Tour slot: {row_anchor}",
            from_email=from_email,
            body=body,
            internet_message_id=(
                internet_message_id_override or f"<{thread_id}@mock.test>"
            ),
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
        if notes_header is not None:
            if extra_header_before_notes is not None:
                header.append(extra_header_before_notes)
                rowvals.append("")
            header.append(notes_header)
            rowvals.append(existing_note)
        sheet_id = sheet_id_override or "sheet-1"
        tab_title = tab_title_override or "Sheet1"
        client_ref = FakeDocumentRef({"criteria": "Industrial search"})
        full_body_response = MagicMock()
        full_body_response.json.return_value = {
            "body": {"content": body, "contentType": "Text"},
            "hasAttachments": False,
        }
        me_response = MagicMock(status_code=200)
        me_response.json.return_value = {"mail": "baylor.freelance@outlook.com"}

        notifications = []
        notification_attempts = []
        handled_events = []
        status_updates = []
        pending_notification_errors = list(notification_errors or [])

        def fake_write_notification(*args, **kwargs):
            attempt = {
                "args": args,
                "kwargs": kwargs,
                "threadStatus": thread_ref._data.get("status"),
            }
            notification_attempts.append(attempt)
            if pending_notification_errors:
                error = pending_notification_errors.pop(0)
                if error is not None:
                    attempt["error"] = str(error)
                    raise error
            notif_id = f"notif-{len(notifications) + 1}"
            notifications.append({"args": args, "kwargs": kwargs, "id": notif_id})
            return notif_id

        def fake_mark_event_handled(_user_id, _thread_id, event_key, _msg_id, notif_id):
            handled_events.append({"eventKey": event_key, "notifId": notif_id})
            return mark_event_handled_result

        def fake_update_thread_status(_user_id, _thread_id, status, reason):
            status_updates.append({"status": status, "reason": reason})
            return update_thread_status_result

        # move_row_below_divider returns the moved row after the source-row
        # deletion shifts both the divider and copied row up by one.
        move_row = MagicMock(return_value=10)
        if move_row_side_effect is not None:
            move_row.side_effect = move_row_side_effect
        ensure_divider = MagicMock(return_value=10)
        if ensure_divider_side_effect is not None:
            ensure_divider.side_effect = ensure_divider_side_effect
        call_trace = []
        pending_reply_outcome_errors = (
            [reply_outcome_update_error] if reply_outcome_update_error else []
        )

        def fake_send_reply(*args, **kwargs):
            call_trace.append("send")
            graph_capability = kwargs.get("graph_send_capability")
            if graph_capability is not None:
                if send_result:
                    _prepare_and_consume_test_graph_capability(graph_capability)
                processing.resolve_graph_send_permit(
                    graph_capability,
                    "accepted" if send_result else "definitely_not_sent",
                    evidence=(
                        {"phase": "send", "httpStatus": 202}
                        if send_result
                        else {
                            "phase": "preflight",
                            "reason": "definite test send failure",
                        }
                    ),
                )
                if send_result:
                    retained_permit = send_permits.read_permit(
                        graph_capability
                    )
                    prepared_envelope = retained_permit[
                        "preparedEnvelope"
                    ]
                    processing._set_reply_send_outcome(
                        outcome="sent_indexed",
                        conversation_id=retained_permit.get(
                            "conversationId"
                        ),
                        exact_sent_evidence={
                            "id": prepared_envelope["draftId"],
                            "sentMessageId": prepared_envelope["draftId"],
                            "isDraft": False,
                            "subject": prepared_envelope["subject"],
                            "recipient": retained_permit["recipient"],
                            "bodyHash": retained_permit["bodyHash"],
                            "conversationId": retained_permit.get(
                                "conversationId"
                            ),
                            "sentDateTime": (
                                retained_permit["requestStartedAt"]
                                + timedelta(seconds=1)
                            ),
                            "permitId": retained_permit["permitId"],
                            "sourceGraphMessageId": retained_permit[
                                "sourceGraphMessageId"
                            ],
                            "preparedEnvelopeHash": prepared_envelope[
                                "preparedEnvelopeHash"
                            ],
                            "toRecipients": [
                                {
                                    "emailAddress": {
                                        "address": address,
                                    },
                                }
                                for address in prepared_envelope[
                                    "toRecipients"
                                ]
                            ],
                            "ccRecipients": [],
                            "bccRecipients": [],
                            "body": {
                                "contentType": "HTML",
                                "content": "<p>Prepared test reply.</p>",
                            },
                            "attachments": [],
                        },
                    )
            if pending_reply_outcome_errors:
                thread_ref._update_error = pending_reply_outcome_errors.pop(0)
            if not send_result:
                processing._set_reply_send_outcome(
                    error="definite test send failure",
                    outcome="send_failed",
                    sent_but_unindexed=False,
                    conversation_id=f"conv-{thread_id}",
                )
            return send_result

        def fake_mark_client_completed(*args, **kwargs):
            call_trace.append("complete")
            return True

        send_reply = MagicMock(side_effect=fake_send_reply)
        mark_client_completed = MagicMock(side_effect=fake_mark_client_completed)
        propose_updates = MagicMock(return_value=proposal)
        apply_proposal = MagicMock(return_value={
            "applied": list(proposal.get("updates") or []),
            "skipped": [],
        })
        sent_reply_lookup = MagicMock(return_value=sent_reply_match)
        message_id_lookup = MagicMock(return_value=thread_id)
        thread_status_lookup = MagicMock(
            return_value=processing.THREAD_STATUS["active"]
        )
        save_inbound_message = MagicMock(return_value=True)
        index_inbound_message = MagicMock(return_value=True)
        dump_thread = MagicMock()
        cancel_followup = MagicMock()
        find_client_by_email = MagicMock(return_value=None)
        resume_manual_continuation = MagicMock(return_value=False)
        fetch_pdfs = MagicMock(return_value=[])
        fetch_linked_assets = MagicMock(return_value=[])
        fetch_url = MagicMock(return_value=None)
        write_order = MagicMock()
        read_header = MagicMock(return_value=header)
        find_row_anchor = MagicMock(return_value=(rownum, rowvals))
        fetch_sheet = MagicMock(
            return_value=(client_id, sheet_id, header, rownum, rowvals, None, [])
        )
        if recovery_external_error is not None:
            fetch_pdfs.side_effect = recovery_external_error
            fetch_linked_assets.side_effect = recovery_external_error
            fetch_url.side_effect = recovery_external_error
            write_order.side_effect = recovery_external_error
        pending_queue_outcome_errors = (
            [queue_outcome_update_error] if queue_outcome_update_error else []
        )

        pending_response_docs = pending_response_docs if pending_response_docs is not None else {}

        def fake_queue_pending(*args, **kwargs):
            (
                queued_user_id,
                queued_thread_id,
                queued_msg_id,
                queued_recipient,
                queued_body,
                *queued_rest,
            ) = args
            queued_client_id = queued_rest[0] if queued_rest else None
            pending_response_docs[queued_thread_id] = FakeDocumentRef({
                "threadId": queued_thread_id,
                "msgId": queued_msg_id,
                "recipient": queued_recipient,
                "responseBody": queued_body,
                "clientId": queued_client_id,
                "conversationId": kwargs.get("conversation_id"),
                "attempts": 1,
            })
            if pending_queue_outcome_errors:
                thread_ref._update_error = pending_queue_outcome_errors.pop(0)
            return thread_id

        queue_pending = MagicMock(side_effect=fake_queue_pending)

        def fake_is_event_handled(_user_id, _thread_id, event_key):
            nested = thread_ref._data.get("handledEvents") or {}
            return (
                isinstance(nested, dict) and event_key in nested
            ) or f"handledEvents.{event_key}" in thread_ref._data

        thread_docs = thread_docs or {thread_id: thread_ref}
        expected_thread_count = len(thread_docs)
        sync_threads = MagicMock(
            return_value=expected_thread_count if sync_thread_count is None else sync_thread_count
        )
        stop_threads = MagicMock(
            return_value=expected_thread_count if stop_thread_count is None else stop_thread_count
        )

        sheets = MagicMock()
        values_api = sheets.spreadsheets.return_value.values.return_value
        note_get_request = MagicMock()
        if note_read_error is not None:
            note_get_request.execute.side_effect = note_read_error
        elif existing_note:
            note_get_request.execute.return_value = {"values": [[existing_note]]}
        else:
            note_get_request.execute.return_value = {"values": []}
        values_api.get.return_value = note_get_request

        note_update_request = MagicMock()
        if note_write_error is not None:
            note_update_request.execute.side_effect = note_write_error
        else:
            note_update_request.execute.return_value = {}
        values_api.update.return_value = note_update_request

        if capture is not None:
            capture.update({
                "handledEvents": handled_events,
                "notifications": notifications,
                "notificationAttempts": notification_attempts,
                "statusUpdates": status_updates,
                "moveRow": move_row,
                "ensureDivider": ensure_divider,
                "syncThreads": sync_threads,
                "stopThreads": stop_threads,
                "sendReply": send_reply,
                "proposeUpdates": propose_updates,
                "applyProposal": apply_proposal,
                "sentReplyLookup": sent_reply_lookup,
                "messageIdLookup": message_id_lookup,
                "threadStatusLookup": thread_status_lookup,
                "saveInboundMessage": save_inbound_message,
                "indexInboundMessage": index_inbound_message,
                "dumpThread": dump_thread,
                "cancelFollowup": cancel_followup,
                "findClientByEmail": find_client_by_email,
                "resumeManualContinuation": resume_manual_continuation,
                "fetchPdfs": fetch_pdfs,
                "fetchLinkedAssets": fetch_linked_assets,
                "fetchUrl": fetch_url,
                "writeOrder": write_order,
                "queuePending": queue_pending,
                "readHeader": read_header,
                "findRowAnchor": find_row_anchor,
                "fetchSheet": fetch_sheet,
                "markClientCompleted": mark_client_completed,
                "callTrace": call_trace,
                "noteGet": values_api.get,
                "noteUpdate": values_api.update,
                "threadRef": thread_ref,
            })
        transaction_behaviors = {}
        if sheet_attempt_error is not None:
            transaction_behaviors["terminalSheetMutationAttempt"] = [{
                "error": sheet_attempt_error,
                "applyBeforeError": False,
            }]
        if finalization_error is not None:
            transaction_behaviors["terminalNotificationOwed"] = [{
                "error": finalization_error,
                "applyBeforeError": False,
            }]
        elif finalization_apply_then_error is not None:
            transaction_behaviors["terminalNotificationOwed"] = [{
                "error": finalization_apply_then_error,
                "applyBeforeError": True,
            }]
        if queue_outcome_update_error is not None and not send_result:
            transaction_behaviors["terminalReplyAttempt"] = [
                {},
                {},
                {},
                {
                    "error": queue_outcome_update_error,
                    "applyBeforeError": False,
                },
            ]
        firestore = FakeFirestore(
            thread_ref,
            client_ref,
            thread_docs=thread_docs,
            transaction_commit_behaviors_by_field=(
                transaction_behaviors or None
            ),
            pending_response_docs=pending_response_docs,
        )
        if capture is not None:
            capture["firestore"] = firestore
            capture["pendingResponseDocs"] = pending_response_docs
        client_ref._data.update({
            "status": campaign_status,
            "automationPaused": campaign_automation_paused,
        })
        patches = [
            patch.object(processing, "_fs", firestore),
            patch.object(processing, "exponential_backoff_request", return_value=full_body_response),
            patch.object(processing.requests, "get", return_value=me_response),
            patch.object(
                processing,
                "lookup_thread_by_message_id",
                side_effect=message_id_lookup,
            ),
            patch.object(processing, "lookup_thread_by_conversation_id", return_value=None),
            patch.object(processing, "get_thread_status", side_effect=thread_status_lookup),
            patch.object(processing, "save_message", side_effect=save_inbound_message),
            patch.object(processing, "index_message_id", side_effect=index_inbound_message),
            patch.object(processing, "dump_thread_from_firestore", side_effect=dump_thread),
            patch(
                "email_automation.followup.cancel_followup_on_response",
                side_effect=cancel_followup,
            ),
            patch.object(
                processing,
                "_find_client_id_by_email",
                side_effect=find_client_by_email,
            ),
            patch.object(
                processing,
                "_resume_paused_thread_after_manual_continuation",
                side_effect=resume_manual_continuation,
            ),
            patch.object(
                processing,
                "fetch_and_log_sheet_for_thread",
                side_effect=fetch_sheet,
            ),
            patch.object(
                processing,
                "_resolve_reply_identity",
                return_value={
                    "recipient_email": reply_recipient_override or from_email,
                    "contact_name": contact_name,
                    "original_email": from_email,
                    "source": "test",
                },
            ),
            patch.object(processing, "fetch_and_process_pdfs", side_effect=fetch_pdfs),
            patch.object(
                processing,
                "fetch_and_process_linked_assets",
                side_effect=fetch_linked_assets,
            ),
            patch.object(processing, "write_message_order_test", side_effect=write_order),
            patch.object(processing, "fetch_url_as_text", side_effect=fetch_url),
            patch.object(processing, "propose_sheet_updates", side_effect=propose_updates),
            patch.object(processing, "apply_proposal_to_sheet", side_effect=apply_proposal),
            patch.object(
                processing,
                "find_exact_sent_message_by_immutable_id",
                side_effect=sent_reply_lookup,
            ),
            patch.object(processing, "_sheets_client", return_value=sheets),
            patch.object(processing, "_get_first_tab_title", return_value=tab_title),
            patch.object(processing, "_read_header_row2", side_effect=read_header),
            patch.object(processing, "_find_row_by_anchor", side_effect=find_row_anchor),
            patch.object(
                processing,
                "is_event_handled",
                side_effect=(
                    fake_is_event_handled
                    if honor_handled_events
                    else lambda *_args, **_kwargs: False
                ),
            ),
            patch.object(processing, "write_notification", side_effect=fake_write_notification),
            patch.object(processing, "mark_event_handled", side_effect=fake_mark_event_handled),
            patch.object(processing, "_is_row_below_nonviable", return_value=row_below_nonviable),
            patch.object(
                processing,
                "_preview_nonviable_divider",
                return_value={
                    "dividerRow": 10,
                    "exists": divider_preview_exists,
                },
                side_effect=divider_preview_side_effect,
                create=True,
            ),
            patch.object(processing, "ensure_nonviable_divider", new=ensure_divider),
            patch.object(processing, "move_row_below_divider", side_effect=move_row),
            patch.object(
                processing,
                "move_row_below_new_divider_atomic",
                side_effect=move_row,
            ),
            patch.object(processing, "sync_thread_row_numbers_after_move", side_effect=sync_threads),
            patch.object(processing, "stop_threads_for_row", side_effect=stop_threads),
            patch.object(processing, "format_sheet_columns_autosize_with_exceptions"),
            patch.object(processing, "clear_row_highlight"),
            patch.object(processing, "highlight_row"),
            patch.object(processing, "send_reply_in_thread", side_effect=send_reply),
            patch.object(processing, "queue_pending_response", side_effect=queue_pending),
            patch.object(processing, "update_thread_status", side_effect=fake_update_thread_status),
            patch.object(processing, "complete_threads_for_row", return_value=1),
            patch.object(processing, "_clear_thread_action_notifications"),
            patch.object(processing, "_maybe_mark_client_completed", side_effect=mark_client_completed),
            patch.object(processing, "check_missing_required_fields", return_value=[]),
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

        return {
            "notifications": notifications,
            "notificationAttempts": notification_attempts,
            "handledEvents": handled_events,
            "statusUpdates": status_updates,
            "moveRow": move_row,
            "ensureDivider": ensure_divider,
            "syncThreads": sync_threads,
            "stopThreads": stop_threads,
            "sendReply": send_reply,
            "proposeUpdates": propose_updates,
            "applyProposal": apply_proposal,
            "sentReplyLookup": sent_reply_lookup,
            "messageIdLookup": message_id_lookup,
            "threadStatusLookup": thread_status_lookup,
            "saveInboundMessage": save_inbound_message,
            "indexInboundMessage": index_inbound_message,
            "dumpThread": dump_thread,
            "cancelFollowup": cancel_followup,
            "findClientByEmail": find_client_by_email,
            "resumeManualContinuation": resume_manual_continuation,
            "fetchPdfs": fetch_pdfs,
            "fetchLinkedAssets": fetch_linked_assets,
            "fetchUrl": fetch_url,
            "writeOrder": write_order,
            "queuePending": queue_pending,
            "readHeader": read_header,
            "findRowAnchor": find_row_anchor,
            "fetchSheet": fetch_sheet,
            "markClientCompleted": mark_client_completed,
            "callTrace": call_trace,
            "threadRef": thread_ref,
        }

    def _finalized_terminal_execution_fixture(
        self,
        thread_id,
        *,
        owner="terminal-owner-a",
        fencing_token=1,
        lease_until=None,
        terminal_reply_owed=True,
        terminal_reply_attempt=None,
        pending_response_docs=None,
    ):
        frozen_header = [
            "Property Address",
            "City",
            "Leasing Contact",
            "Email",
            "Total SF",
            "Rent/SF/Yr",
            "Ops Ex / SF",
            "Notes",
        ]
        immutable = {
            "version": processing.TERMINAL_SAGA_VERSION,
            "settlementOrdinal": 1,
            "sagaKey": f"terminal-saga-{thread_id}",
            "sourceMessageKey": f"<{thread_id}@mock.test>",
            "sourceGraphMessageId": f"msg-{thread_id}",
            "sourceInternetMessageId": f"<{thread_id}@mock.test>",
            "sourceConversationId": f"conv-{thread_id}",
            "sourceReceivedAt": "2026-06-19T19:12:39Z",
            "reason": "no_longer_available",
            "note": "[06/19/2026] Property marked unavailable",
            "eventKey": "property_unavailable",
            "sourceRow": 3,
            "rowAnchor": "951 E FM 646",
            "responseScenario": "nonviable",
            "responseBody": "Thank you for letting me know.",
            "completeClientAfterReply": True,
            "replyRecipient": "bp21harrison@gmail.com",
            "notificationRequired": True,
            "eventAddress": "951 E FM 646",
            "eventCity": "Houston",
            "clientId": "client-1",
            "sheetId": "sheet-1",
            "tabTitle": "Sheet1",
            "notesColumnIndex": 8,
            "notesColumnHeader": "Notes",
            "sheetHeaderFingerprint": (
                processing._terminal_sheet_header_fingerprint(frozen_header)
            ),
            "finalizationPlan": {
                "dividerRow": 10,
                "finalRow": 10,
                "claimThreadId": thread_id,
                "terminalThreadIds": [thread_id],
                "rowShifts": [],
                "writeCount": 1,
            },
        }
        immutable_hash = hashlib.sha256(
            json.dumps(immutable, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        saga = {
            **immutable,
            "immutableHash": immutable_hash,
            "phase": "finalized",
            "finalRow": 10,
        }
        claim = {
            "sagaKey": saga["sagaKey"],
            "immutableHash": immutable_hash,
            "sourceMessageKey": saga["sourceMessageKey"],
            "currentThreadId": thread_id,
            "owner": owner,
            "fencingToken": fencing_token,
            "claimedAt": datetime.now(timezone.utc),
            "leaseUntil": lease_until or (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ),
            "status": "processing",
        }
        root_data = {
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["stopped"],
            "rowNumber": 10,
            "terminalSaga": saga,
            "terminalSagaKey": saga["sagaKey"],
            "pendingTerminalReason": saga["reason"],
            "terminalSagaClaim": claim,
            "terminalSagaFence": fencing_token,
            "terminalNotificationOwed": False,
            "terminalNotificationOutcome": "created",
            "terminalReplyOwed": terminal_reply_owed,
        }
        root_data["terminalSheetMutationAttempt"] = (
            self._test_terminal_sheet_attempt(
                saga,
                claim,
                "move_with_note",
                status="applied",
            )
        )
        root_data["terminalSheetMutationHistory"] = []
        root_data["terminalSheetMutationReview"] = None
        if terminal_reply_attempt is None and not terminal_reply_owed:
            terminal_reply_attempt = {
                "sagaKey": saga["sagaKey"],
                "sourceMessageKey": saga["sourceMessageKey"],
                "sourceGraphMessageId": saga["sourceGraphMessageId"],
                "conversationId": saga["sourceConversationId"],
                "recipient": saga["replyRecipient"],
                "responseBodyHash": hashlib.sha256(
                    saga["responseBody"].encode("utf-8")
                ).hexdigest(),
                "status": "committed",
                "outcome": "sent_indexed",
                "startedAt": datetime.now(timezone.utc) - timedelta(minutes=1),
                "committedAt": datetime.now(timezone.utc),
            }
        if terminal_reply_attempt is not None:
            root_data["terminalReplyAttempt"] = terminal_reply_attempt
        root = FakeDocumentRef(root_data)
        client_ref = FakeDocumentRef({"criteria": "Industrial search"})
        pending_response_docs = (
            pending_response_docs if pending_response_docs is not None else {}
        )
        firestore = FakeFirestore(
            root,
            client_ref,
            thread_docs={thread_id: root},
            pending_response_docs=pending_response_docs,
        )
        return root, firestore, saga, pending_response_docs

    def _archived_terminal_projection_for_test(
        self,
        base_saga,
        ordinal,
        *,
        source_suffix=None,
        with_reply=False,
    ):
        suffix = source_suffix or str(ordinal)
        immutable_saga = {
            key: copy.deepcopy(value)
            for key, value in base_saga.items()
            if key not in {"immutableHash", "phase", "finalRow"}
        }
        immutable_saga.update({
            "settlementOrdinal": ordinal,
            "sagaKey": f"archived-saga-{suffix}",
            "sourceMessageKey": f"<archived-{suffix}@mock.test>",
            "sourceGraphMessageId": f"archived-msg-{suffix}",
            "sourceInternetMessageId": f"<archived-{suffix}@mock.test>",
            "sourceConversationId": f"archived-conv-{suffix}",
            "responseScenario": (
                "nonviable" if with_reply else "none"
            ),
            "responseBody": (
                "Thank you for letting me know." if with_reply else None
            ),
            "notificationRequired": False,
            "completeClientAfterReply": False,
        })
        immutable_hash = hashlib.sha256(
            json.dumps(
                immutable_saga,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        saga_snapshot = {
            **immutable_saga,
            "immutableHash": immutable_hash,
            "phase": "finalized",
            "finalRow": immutable_saga["finalizationPlan"]["finalRow"],
        }
        claim = {
            "owner": f"archived-owner-{suffix}",
            "fencingToken": 1,
        }
        sheet_attempt = self._test_terminal_sheet_attempt(
            saga_snapshot,
            claim,
            "move_with_note",
            status="applied",
        )
        reply_attempt = None
        reply_outcome = "not_required"
        if with_reply:
            reply_outcome = "sent_indexed"
            reply_attempt = {
                "sagaKey": saga_snapshot["sagaKey"],
                "sourceMessageKey": saga_snapshot["sourceMessageKey"],
                "sourceGraphMessageId": saga_snapshot[
                    "sourceGraphMessageId"
                ],
                "conversationId": saga_snapshot["sourceConversationId"],
                "recipient": saga_snapshot["replyRecipient"],
                "responseBodyHash": hashlib.sha256(
                    saga_snapshot["responseBody"].encode("utf-8")
                ).hexdigest(),
                "status": "committed",
                "outcome": reply_outcome,
                "startedAt": datetime.now(timezone.utc) - timedelta(minutes=1),
                "committedAt": datetime.now(timezone.utc),
            }
        immutable_projection = {
            "version": processing.TERMINAL_SETTLEMENT_VERSION,
            "settlementOrdinal": ordinal,
            "sagaKey": saga_snapshot["sagaKey"],
            "sagaImmutableHash": saga_snapshot["immutableHash"],
            "sourceMessageKey": saga_snapshot["sourceMessageKey"],
            "sourceGraphMessageId": saga_snapshot["sourceGraphMessageId"],
            "sourceInternetMessageId": saga_snapshot[
                "sourceInternetMessageId"
            ],
            "finalRow": saga_snapshot["finalRow"],
            "notificationOutcome": "not_required",
            "replyOutcome": reply_outcome,
            "sagaSnapshot": saga_snapshot,
            "terminalReplyAttempt": reply_attempt,
            "terminalReplyAttemptHash": (
                processing._terminal_reply_attempt_archive_hash(reply_attempt)
            ),
            "sheetMutationAttempt": sheet_attempt,
            "sheetMutationHistory": [],
            "sheetMutationReview": None,
            "settledAt": f"2026-06-{ordinal:02d}T00:00:00Z",
        }
        return {
            **immutable_projection,
            "projectionHash": hashlib.sha256(
                json.dumps(
                    immutable_projection,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest(),
        }

    def _terminal_reply_attempt(self, saga, status, *, response_body=None):
        body = saga["responseBody"] if response_body is None else response_body
        return {
            "sagaKey": saga["sagaKey"],
            "sourceMessageKey": saga["sourceMessageKey"],
            "sourceGraphMessageId": saga["sourceGraphMessageId"],
            "conversationId": saga["sourceConversationId"],
            "recipient": saga["replyRecipient"],
            "responseBodyHash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "status": status,
            "startedAt": datetime.now(timezone.utc) - timedelta(minutes=1),
        }

    def _rehash_test_terminal_sheet_attempt(self, attempt):
        immutable_fields = (
            "version",
            "sagaKey",
            "sagaImmutableHash",
            "attemptId",
            "ordinal",
            "previousAttemptId",
            "previousAttemptHash",
            "mutationKind",
            "sourceRow",
            "finalRow",
            "rowAnchor",
            "noteHash",
            "owner",
            "fencingToken",
            "requestStartedAt",
            "providerDeadline",
        )
        immutable = {field: attempt.get(field) for field in immutable_fields}
        attempt["attemptImmutableHash"] = hashlib.sha256(
            json.dumps(immutable, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        attempt.pop("attemptHash", None)
        attempt["attemptHash"] = hashlib.sha256(
            json.dumps(attempt, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return attempt

    def _test_terminal_sheet_attempt(
        self,
        saga,
        claim,
        mutation_kind,
        *,
        status="request_started",
        ordinal=1,
        previous_attempt=None,
        now=None,
    ):
        now = now or datetime.now(timezone.utc)
        immutable = {
            "version": 2,
            "sagaKey": saga["sagaKey"],
            "sagaImmutableHash": saga["immutableHash"],
            "attemptId": f"sheet-attempt-{saga['sagaKey']}-{ordinal}",
            "ordinal": ordinal,
            "previousAttemptId": (
                previous_attempt["attemptId"] if previous_attempt else None
            ),
            "previousAttemptHash": (
                previous_attempt["attemptHash"] if previous_attempt else None
            ),
            "mutationKind": mutation_kind,
            "sourceRow": saga["sourceRow"],
            "finalRow": saga["finalizationPlan"]["finalRow"],
            "rowAnchor": saga["rowAnchor"],
            "noteHash": hashlib.sha256(saga["note"].encode("utf-8")).hexdigest(),
            "owner": claim["owner"],
            "fencingToken": claim["fencingToken"],
            "requestStartedAt": now,
            "providerDeadline": now + timedelta(seconds=60),
        }
        attempt = {**immutable, "status": status}
        if status == "applied":
            attempt.update({
                "appliedByOwner": claim["owner"],
                "appliedByFencingToken": claim["fencingToken"],
                "providerCompletedAt": now + timedelta(seconds=1),
                "operatorReviewRequired": False,
            })
        elif status == "reconciled_applied":
            attempt.update({
                "reconciledByOwner": claim["owner"],
                "reconciledByFencingToken": claim["fencingToken"],
                "reconciledAt": now + timedelta(seconds=1),
                "reconciliationEvidence": (
                    "exact final row and terminal note are present"
                ),
                "operatorReviewRequired": False,
            })
        elif status == "needs_operator_review":
            attempt.update({
                "operatorReviewRequired": True,
                "reviewReason": "persisted Sheet effect was absent",
                "reviewEvidence": "absent",
                "providerError": None,
                "reviewedByOwner": claim["owner"],
                "reviewedByFencingToken": claim["fencingToken"],
                "reviewedAt": now + timedelta(seconds=1),
            })
        elif status == "definitely_not_applied":
            attempt.update({
                "providerStatusCode": 429,
                "providerError": "provider rejected before acceptance",
                "definitelyNotAppliedAt": now + timedelta(seconds=1),
                "operatorReviewRequired": False,
            })
        return self._rehash_test_terminal_sheet_attempt(attempt)

    def _staged_terminal_sheet_fixture(self, thread_id):
        current_root, firestore, finalized_saga, pending_docs = (
            self._finalized_terminal_execution_fixture(thread_id)
        )
        saga = {**finalized_saga, "phase": "staged"}
        saga.pop("finalRow", None)
        current_root._data.update({
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": saga["sourceRow"],
            "terminalSaga": saga,
            "terminalSagaKey": saga["sagaKey"],
            "terminalNotificationOwed": False,
            "terminalReplyOwed": False,
            "terminalSheetMutationAttempt": None,
            "terminalSheetMutationHistory": [],
            "terminalSheetMutationReview": None,
        })
        owner = processing.TerminalSagaExecution(
            owner=current_root._data["terminalSagaClaim"]["owner"],
            fencing_token=current_root._data["terminalSagaClaim"][
                "fencingToken"
            ],
        )
        return current_root, firestore, saga, owner, pending_docs

    def _test_terminal_sheet_outcome_fields(
        self,
        saga,
        claim,
        mutation_kind,
        status,
        *,
        now=None,
    ):
        attempt = self._test_terminal_sheet_attempt(
            saga,
            claim,
            mutation_kind,
            status=status,
            now=now,
        )
        identity_and_hash_fields = {
            "version",
            "sagaKey",
            "sagaImmutableHash",
            "attemptId",
            "ordinal",
            "previousAttemptId",
            "previousAttemptHash",
            "mutationKind",
            "sourceRow",
            "finalRow",
            "rowAnchor",
            "noteHash",
            "owner",
            "fencingToken",
            "requestStartedAt",
            "providerDeadline",
            "attemptImmutableHash",
            "attemptHash",
            "status",
        }
        return {
            key: value
            for key, value in attempt.items()
            if key not in identity_and_hash_fields
        }

    def _test_terminal_sheet_review(self, saga, attempt):
        return {
            "sagaKey": saga["sagaKey"],
            "attemptId": attempt["attemptId"],
            "attemptHash": attempt["attemptHash"],
            "reason": attempt["reviewReason"],
            "observedByOwner": attempt["reviewedByOwner"],
            "observedByFencingToken": attempt["reviewedByFencingToken"],
            "requestedAt": attempt["reviewedAt"],
        }

    def _terminal_pending_response(self, thread_id, saga, *, response_body=None):
        return FakeDocumentRef({
            "threadId": thread_id,
            "msgId": saga["sourceGraphMessageId"],
            "recipient": saga["replyRecipient"],
            "responseBody": (
                saga["responseBody"] if response_body is None else response_body
            ),
            "clientId": saga["clientId"],
            "conversationId": saga["sourceConversationId"],
            "attempts": 1,
        })

    def _attach_accepted_terminal_permit(self, thread_id, root, firestore, saga):
        root.id = thread_id
        root._data["terminalReplyAttempt"] = self._terminal_reply_attempt(
            saga,
            "sending",
        )
        claim = root._data["terminalSagaClaim"]
        capability = send_permits.issue_terminal_graph_send_permit(
            firestore,
            root,
            root,
            saga,
            claim["owner"],
            claim["fencingToken"],
        )
        _prepare_and_consume_test_graph_capability(capability)
        send_permits.resolve_graph_send_permit(
            capability,
            "accepted",
            evidence={"httpStatus": 202, "phase": "send"},
        )
        return capability, _exact_sent_evidence_for_test(
            send_permits.read_permit(capability)
        )

    def _attach_definitely_unsent_queue_attempt(
        self,
        thread_id,
        root,
        firestore,
        saga,
        pending_docs,
    ):
        root.id = thread_id
        root._data["terminalReplyAttempt"] = self._terminal_reply_attempt(
            saga,
            "sending",
        )
        claim = root._data["terminalSagaClaim"]
        capability = send_permits.issue_terminal_graph_send_permit(
            firestore,
            root,
            root,
            saga,
            claim["owner"],
            claim["fencingToken"],
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "definitely_not_sent",
            evidence={"reason": "definite pre-send failure", "phase": "preflight"},
        )
        attempt = {
            **root._data["terminalReplyAttempt"],
            "status": "queueing_response_retry",
            "queueDocumentId": thread_id,
            "definiteUnsentAt": datetime.now(timezone.utc),
        }
        pending_ref = (
            firestore.collection("users").document("test-user")
            .collection("pendingResponses").document(thread_id)
        )
        send_permits.cas_terminal_reply_transition(
            firestore,
            root,
            root,
            saga,
            claim["owner"],
            claim["fencingToken"],
            expected_attempt_status="sending",
            thread_patch={
                "terminalReplyAttempt": attempt,
                "updatedAt": datetime.now(timezone.utc),
            },
            permit_settlement="settled_definitely_not_sent",
            capability=capability,
            pending_upsert=(
                pending_ref,
                processing._terminal_pending_response_payload(
                    thread_id,
                    saga["clientId"],
                    saga["replyRecipient"],
                    saga["responseBody"],
                    saga,
                    error="definite test send failure",
                    conversation_id=saga["sourceConversationId"],
                ),
            ),
        )
        pending_docs[thread_id] = pending_ref
        return capability

    def _settle_terminal_reply_direct(
        self,
        thread_id,
        firestore,
        saga,
        *,
        sent_match=None,
        sent_side_effect=None,
    ):
        sent_lookup = MagicMock(return_value=sent_match)
        if sent_side_effect is not None:
            sent_lookup.side_effect = sent_side_effect
        send_reply = MagicMock()

        def fake_queue_pending(
            _user_id,
            queued_thread_id,
            queued_msg_id,
            queued_recipient,
            queued_body,
            queued_client_id,
            **kwargs,
        ):
            firestore.pending_response_docs[queued_thread_id] = FakeDocumentRef({
                "threadId": queued_thread_id,
                "msgId": queued_msg_id,
                "recipient": queued_recipient,
                "responseBody": queued_body,
                "clientId": queued_client_id,
                "conversationId": kwargs.get("conversation_id"),
                "attempts": 1,
            })
            return queued_thread_id

        queue_pending = MagicMock(side_effect=fake_queue_pending)
        owner = processing.TerminalSagaExecution(
            owner=firestore.thread_ref._data["terminalSagaClaim"]["owner"],
            fencing_token=firestore.thread_ref._data["terminalSagaClaim"][
                "fencingToken"
            ],
        )
        with patch.object(processing, "_fs", firestore), \
             patch.object(
                 processing,
                 "find_exact_sent_message_by_immutable_id",
                 side_effect=sent_lookup,
             ), \
             patch.object(processing, "send_reply_in_thread", side_effect=send_reply), \
             patch.object(
                 processing,
                 "queue_pending_response",
                 side_effect=queue_pending,
             ):
            outcome = processing._settle_terminal_reply_obligation(
                "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
                saga["clientId"],
                thread_id,
                {"Authorization": "Bearer test-token"},
                saga["replyRecipient"],
                saga,
                terminal_saga_owner=owner,
            )
        return outcome, sent_lookup, send_reply, queue_pending

    def test_terminal_sheet_provider_window_bounds_timeout_and_disables_429_replay(self):
        response = MagicMock(status=429, reason="Too Many Requests")
        quota_error = HttpError(response, b"rate limited")

        class BoundedRequest:
            def __init__(self):
                self.http = MagicMock()
                self.http.http = MagicMock()
                self.http.http.timeout = 99
                self.calls = 0
                self.observed_timeouts = []

            def execute(self):
                self.calls += 1
                self.observed_timeouts.append(self.http.http.timeout)
                raise quota_error

        request = BoundedRequest()
        with patch.object(sheets_module.time, "sleep") as sleep:
            with self.assertRaises(HttpError):
                with sheets_module.terminal_sheets_provider_window(1.0):
                    sheets_module._execute_with_retry(
                        request,
                        "terminal Sheet mutation",
                    )

        self.assertEqual(1, request.calls)
        sleep.assert_not_called()
        self.assertGreater(request.observed_timeouts[0], 0)
        self.assertLessEqual(request.observed_timeouts[0], 1.0)
        self.assertEqual(99, request.http.http.timeout)

    def test_existing_terminal_reply_attempt_reconciles_sent_before_any_other_action(self):
        thread_id = "thread-sent-first-active-permit"
        root, firestore, saga, _pending_docs = (
            self._finalized_terminal_execution_fixture(thread_id)
        )
        _capability, sent_match = self._attach_accepted_terminal_permit(
            thread_id,
            root,
            firestore,
            saga,
        )

        outcome, sent_lookup, send_reply, queue_pending = (
            self._settle_terminal_reply_direct(
                thread_id,
                firestore,
                saga,
                sent_match=sent_match,
            )
        )

        self.assertEqual("sent_reconciled", outcome)
        sent_lookup.assert_called_once()
        send_reply.assert_not_called()
        queue_pending.assert_not_called()
        self.assertEqual("sent_reconciled", root._data["terminalReplyOutcome"])
        self.assertFalse(root._data["terminalReplyOwed"])
        self.assertIsNone(root._data.get("terminalSaga"))

    def test_queueing_response_retry_without_definite_unsent_permit_fails_closed(self):
        thread_id = "thread-terminal-queue-without-permit"
        root, firestore, saga, pending_docs = (
            self._finalized_terminal_execution_fixture(thread_id)
        )
        root._data["terminalReplyAttempt"] = self._terminal_reply_attempt(
            saga,
            "queueing_response_retry",
        )
        pending_docs[thread_id] = self._terminal_pending_response(thread_id, saga)
        before = copy.deepcopy(root._data)

        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "terminal unissued reply attempt is not exact",
        ):
            self._settle_terminal_reply_direct(
                thread_id,
                firestore,
                saga,
                sent_side_effect=AssertionError("Sent lookup must not run"),
            )

        self.assertTrue(root._data["terminalReplyOwed"])
        self.assertTrue(pending_docs[thread_id]._exists)
        self.assertEqual(before, root._data)

    def test_cross_saga_definitely_unsent_permit_cannot_authorize_current_queue(self):
        thread_id = "thread-terminal-cross-saga-queue-permit"
        root, firestore, saga_a, pending_docs = (
            self._finalized_terminal_execution_fixture(thread_id)
        )
        claim_a = copy.deepcopy(root._data["terminalSagaClaim"])

        immutable_b = {
            key: copy.deepcopy(value)
            for key, value in saga_a.items()
            if key not in {"immutableHash", "phase", "finalRow"}
        }
        immutable_b.update({
            "sagaKey": f"{saga_a['sagaKey']}-foreign-b",
            "sourceMessageKey": f"<{thread_id}-foreign-b@mock.test>",
            "sourceGraphMessageId": f"msg-{thread_id}-foreign-b",
            "sourceInternetMessageId": f"<{thread_id}-foreign-b@mock.test>",
            "sourceConversationId": f"conv-{thread_id}-foreign-b",
        })
        saga_b = {
            **immutable_b,
            "immutableHash": hashlib.sha256(
                json.dumps(
                    immutable_b,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest(),
            "phase": "finalized",
            "finalRow": 10,
        }
        claim_b = {
            **claim_a,
            "sagaKey": saga_b["sagaKey"],
            "immutableHash": saga_b["immutableHash"],
            "sourceMessageKey": saga_b["sourceMessageKey"],
            "owner": "terminal-owner-b",
            "fencingToken": 2,
            "leaseUntil": datetime.now(timezone.utc) + timedelta(minutes=5),
        }
        root._data.update({
            "terminalSaga": saga_b,
            "terminalSagaKey": saga_b["sagaKey"],
            "terminalSagaClaim": claim_b,
            "terminalSagaFence": 2,
            "terminalReplyOwed": True,
        })
        capability_b = self._attach_definitely_unsent_queue_attempt(
            thread_id,
            root,
            firestore,
            saga_b,
            pending_docs,
        )
        permit_pointer_b = copy.deepcopy(root._data["activeGraphSendPermit"])

        root._data.update({
            "terminalSaga": saga_a,
            "terminalSagaKey": saga_a["sagaKey"],
            "terminalSagaClaim": claim_a,
            "terminalSagaFence": claim_a["fencingToken"],
            "terminalReplyOwed": True,
            "activeGraphSendPermit": permit_pointer_b,
            "terminalReplyAttempt": {
                **self._terminal_reply_attempt(
                    saga_a,
                    "queueing_response_retry",
                ),
                "graphSendPermitId": capability_b.permit_id,
                "graphSendPermitHash": capability_b.immutable_hash,
            },
        })
        pending_docs[thread_id] = self._terminal_pending_response(
            thread_id,
            saga_a,
        )

        sent_lookup = MagicMock(
            side_effect=AssertionError("Sent lookup must not run")
        )
        send_reply = MagicMock()
        queue_pending = MagicMock()
        owner_a = processing.TerminalSagaExecution(
            owner=claim_a["owner"],
            fencing_token=claim_a["fencingToken"],
        )
        with patch.object(processing, "_fs", firestore), \
             patch.object(
                 processing,
                 "find_exact_sent_message_by_immutable_id",
                 sent_lookup,
             ), \
             patch.object(processing, "send_reply_in_thread", send_reply), \
             patch.object(processing, "queue_pending_response", queue_pending):
            with self.assertRaisesRegex(
                processing.RetryableProcessingError,
                "retained permit validation failed",
            ):
                processing._settle_terminal_reply_obligation(
                    "test-user",
                    saga_a["clientId"],
                    thread_id,
                    {"Authorization": "Bearer fake"},
                    saga_a["replyRecipient"],
                    saga_a,
                    terminal_saga_owner=owner_a,
                )

        sent_lookup.assert_not_called()
        send_reply.assert_not_called()
        queue_pending.assert_not_called()
        self.assertTrue(root._data["terminalReplyOwed"])
        self.assertTrue(pending_docs[thread_id]._exists)

    def test_campaign_queue_intent_is_proven_pre_send_without_sent_lookup(self):
        thread_id = "thread-terminal-campaign-queue-pre-send"
        root, firestore, saga, pending_docs = (
            self._finalized_terminal_execution_fixture(thread_id)
        )
        root._data["terminalReplyAttempt"] = self._terminal_reply_attempt(
            saga,
            "queueing_campaign_suppression",
        )
        pending_docs[thread_id] = self._terminal_pending_response(thread_id, saga)

        outcome, sent_lookup, send_reply, queue_pending = (
            self._settle_terminal_reply_direct(
                thread_id,
                firestore,
                saga,
                sent_side_effect=AssertionError("Sent lookup must not run"),
            )
        )

        self.assertEqual("queued_retry", outcome)
        sent_lookup.assert_not_called()
        send_reply.assert_not_called()
        queue_pending.assert_not_called()

    def test_loaded_pending_worker_cannot_send_after_terminal_sent_reconciliation(self):
        thread_id = "thread-terminal-pending-worker-race"
        root, firestore, saga, pending_docs = (
            self._finalized_terminal_execution_fixture(thread_id)
        )
        _capability, exact_sent = self._attach_accepted_terminal_permit(
            thread_id,
            root,
            firestore,
            saga,
        )
        pending_docs[thread_id] = self._terminal_pending_response(thread_id, saga)
        pending_docs[thread_id]._data["attempts"] = 0

        pending_loaded = Event()
        resume_pending_worker = Event()
        pending_graph_send = MagicMock(return_value=True)
        original_claim = pending_responses._claim_pending_response_for_send

        def claim_after_recovery(*args, **kwargs):
            pending_loaded.set()
            if not resume_pending_worker.wait(timeout=5):
                raise AssertionError("pending worker barrier timed out")
            return original_claim(*args, **kwargs)

        allow_decision = campaign_safety.CampaignAutomationDecision(
            state="allow",
            reason="",
            client_data={"status": "live"},
            metadata={"terminal": False, "stopKind": "none"},
        )
        with patch.object(clients, "_fs", firestore), \
             patch.object(
                 pending_responses,
                 "get_client_automation_decision",
                 return_value=allow_decision,
             ), \
             patch.object(pending_responses, "_gate_pending_response", return_value=False), \
             patch.object(
                 processing,
                 "send_reply_in_thread",
                 side_effect=pending_graph_send,
             ), \
             patch.object(
                 pending_responses,
                 "_claim_pending_response_for_send",
                 side_effect=claim_after_recovery,
             ):
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending_future = executor.submit(
                    pending_responses.process_pending_responses,
                    "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
                    {"Authorization": "Bearer test-token"},
                )
                self.assertTrue(pending_loaded.wait(timeout=5))
                try:
                    outcome, sent_lookup, terminal_send, terminal_queue = (
                        self._settle_terminal_reply_direct(
                            thread_id,
                            firestore,
                            saga,
                            sent_match=exact_sent,
                        )
                    )
                finally:
                    resume_pending_worker.set()
                pending_states = pending_future.result(timeout=5)

        self.assertEqual("sent_reconciled", outcome)
        sent_lookup.assert_called_once()
        terminal_send.assert_not_called()
        terminal_queue.assert_not_called()
        pending_graph_send.assert_not_called()
        self.assertEqual([], pending_states)
        self.assertFalse(pending_docs[thread_id]._exists)
        self.assertIsNone(root._data.get("terminalSaga"))

    def test_terminal_sent_reconciliation_does_not_delete_active_pending_send_claim(self):
        thread_id = "thread-terminal-active-pending-claim"
        root, firestore, saga, pending_docs = (
            self._finalized_terminal_execution_fixture(thread_id)
        )
        _capability, exact_sent = self._attach_accepted_terminal_permit(
            thread_id,
            root,
            firestore,
            saga,
        )
        pending_docs[thread_id] = self._terminal_pending_response(thread_id, saga)
        pending_docs[thread_id]._data.update({
            "processingBy": "active-pending-worker",
            "processingLeaseUntil": datetime.now(timezone.utc) + timedelta(minutes=2),
        })

        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "terminal Sent reconciliation found active pending work",
        ):
            self._settle_terminal_reply_direct(
                thread_id,
                firestore,
                saga,
                sent_match=exact_sent,
            )

        self.assertTrue(root._data["terminalReplyOwed"])
        self.assertTrue(pending_docs[thread_id]._exists)
        self.assertNotIn("terminalReplyOutcome", root._data)

    def test_terminal_queue_intent_reuses_exact_pending_or_creates_absent_once(self):
        for status, pending_variants in (
            ("queueing_response_retry", (True,)),
            ("queueing_campaign_suppression", (True, False)),
        ):
            for pending_exists in pending_variants:
                with self.subTest(status=status, pending_exists=pending_exists):
                    thread_id = f"thread-queue-idempotent-{status}-{pending_exists}"
                    root, firestore, saga, pending_docs = (
                        self._finalized_terminal_execution_fixture(thread_id)
                    )
                    if status == "queueing_response_retry":
                        self._attach_definitely_unsent_queue_attempt(
                            thread_id,
                            root,
                            firestore,
                            saga,
                            pending_docs,
                        )
                    else:
                        root._data["terminalReplyAttempt"] = self._terminal_reply_attempt(
                            saga,
                            status,
                        )
                    if pending_exists and thread_id not in pending_docs:
                        pending_docs[thread_id] = self._terminal_pending_response(
                            thread_id,
                            saga,
                        )

                    outcome, sent_lookup, send_reply, queue_pending = (
                        self._settle_terminal_reply_direct(
                            thread_id,
                            firestore,
                            saga,
                            sent_match=None,
                        )
                    )

                    self.assertEqual("queued_retry", outcome)
                    sent_lookup.assert_not_called()
                    send_reply.assert_not_called()
                    self.assertEqual(0 if pending_exists else 1, queue_pending.call_count)
                    self.assertEqual("queued_retry", root._data["terminalReplyOutcome"])

    def test_terminal_queue_intent_rejects_corrupt_attempt_even_when_pending_matches_it(self):
        thread_id = "thread-terminal-corrupt-attempt-body"
        root, firestore, saga, pending_docs = (
            self._finalized_terminal_execution_fixture(thread_id)
        )
        wrong_body = "This is not the immutable saga response."
        root._data["terminalReplyAttempt"] = self._terminal_reply_attempt(
            saga,
            "queueing_response_retry",
            response_body=wrong_body,
        )
        pending_docs[thread_id] = self._terminal_pending_response(
            thread_id,
            saga,
            response_body=wrong_body,
        )

        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "attempt body hash.*immutable saga response body",
        ):
            self._settle_terminal_reply_direct(
                thread_id,
                firestore,
                saga,
                sent_match=None,
            )

        self.assertTrue(root._data["terminalReplyOwed"])
        self.assertTrue(pending_docs[thread_id]._exists)
        self.assertNotIn("terminalReplyOutcome", root._data)

    def test_initial_campaign_queue_intent_reuses_exact_pending_and_rejects_drift(self):
        for pending_matches in (True, False):
            with self.subTest(pending_matches=pending_matches):
                thread_id = f"thread-campaign-initial-pending-{pending_matches}"
                root, firestore, saga, pending_docs = (
                    self._finalized_terminal_execution_fixture(thread_id)
                )
                pending_docs[thread_id] = self._terminal_pending_response(
                    thread_id,
                    saga,
                    response_body=(
                        None if pending_matches else "unrelated queued response"
                    ),
                )
                firestore.client_ref._data.update({
                    "status": "live",
                    "automationPaused": True,
                })

                if pending_matches:
                    outcome, sent_lookup, send_reply, queue_pending = (
                        self._settle_terminal_reply_direct(
                            thread_id,
                            firestore,
                            saga,
                        )
                    )
                    self.assertEqual("queued_retry", outcome)
                    sent_lookup.assert_not_called()
                    send_reply.assert_not_called()
                    queue_pending.assert_not_called()
                else:
                    with self.assertRaisesRegex(
                        processing.RetryableProcessingError,
                        "pending response evidence does not match",
                    ):
                        self._settle_terminal_reply_direct(
                            thread_id,
                            firestore,
                            saga,
                        )
                    self.assertTrue(root._data["terminalReplyOwed"])
                    self.assertNotIn("terminalReplyOutcome", root._data)

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
                divider_preview_exists=False,
                move_row_side_effect=fail_sheet_mutation,
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

    def test_property_unavailable_atomic_move_failure_has_no_terminal_side_effects(self):
        body = "Hi Baylor,\n\n951 E FM 646 is no longer available.\n\nBest,\nRyan"
        thread_id = "thread-atomic-sheet-failure"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "followUpStatus": "waiting",
        })
        sibling_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "followUpStatus": "waiting",
        })
        proposal = {
            "updates": [],
            "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
            "response_email": "Thank you for letting me know.",
        }
        capture = {}

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
                    "thread-atomic-sheet-sibling": sibling_root,
                },
                row_anchor="951 E FM 646",
                existing_note="Broker supplied availability details",
                move_row_side_effect=RuntimeError("atomic Sheet batch failed"),
                capture=capture,
            )

        capture["noteGet"].assert_called_once()
        move_kwargs = capture["moveRow"].call_args.kwargs
        self.assertEqual(8, move_kwargs["notes_column_index"])
        self.assertEqual(
            "Broker supplied availability details | "
            "[06/19/2026] Property marked unavailable - contact said: 'no longer available'",
            move_kwargs["notes_value"],
        )
        capture["syncThreads"].assert_not_called()
        capture["stopThreads"].assert_not_called()
        self.assertEqual([], capture["statusUpdates"])
        self.assertEqual([], capture["handledEvents"])
        self.assertEqual([], capture["notifications"])
        capture["sendReply"].assert_not_called()
        capture["noteUpdate"].assert_not_called()
        for root in (current_root, sibling_root):
            self.assertEqual("stopped", root._data["followUpStatus"])
            self.assertEqual("no_longer_available", root._data["pendingTerminalReason"])
            self.assertEqual(processing.THREAD_STATUS["active"], root._data["status"])

    def test_property_unavailable_missing_notes_column_fails_closed(self):
        body = "Hi Baylor,\n\n951 E FM 646 is no longer available.\n\nBest,\nRyan"
        thread_id = "thread-missing-notes-column"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "followUpStatus": "waiting",
        })
        proposal = {
            "updates": [],
            "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
            "response_email": "Thank you for letting me know.",
        }
        capture = {}

        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "Notes/Comments column is required",
        ):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body=body,
                proposal=proposal,
                thread_ref=thread_ref,
                row_anchor="951 E FM 646",
                notes_header=None,
                capture=capture,
            )

        self.assertEqual("waiting", thread_ref._data["followUpStatus"])
        self.assertNotIn("pendingTerminalReason", thread_ref._data)
        self.assertNotIn("terminalSaga", thread_ref._data)
        capture["ensureDivider"].assert_not_called()
        capture["moveRow"].assert_not_called()
        capture["syncThreads"].assert_not_called()
        capture["stopThreads"].assert_not_called()
        self.assertEqual([], capture["statusUpdates"])
        self.assertEqual([], capture["handledEvents"])
        capture["sendReply"].assert_not_called()

    def test_legacy_saga_with_missing_notes_coordinate_never_adopts_later_column(self):
        thread_id = "thread-legacy-missing-notes-coordinate"
        thread_ref, _firestore, fixture_saga, _pending = (
            self._finalized_terminal_execution_fixture(thread_id)
        )
        immutable = {
            key: value
            for key, value in fixture_saga.items()
            if key not in {"immutableHash", "phase", "finalRow"}
        }
        immutable["notesColumnIndex"] = None
        saga = {
            **immutable,
            "immutableHash": hashlib.sha256(
                json.dumps(immutable, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest(),
            "phase": "staged",
        }
        before_saga = copy.deepcopy(saga)
        thread_ref._data.update({
            "terminalSaga": copy.deepcopy(saga),
            "terminalSagaKey": saga["sagaKey"],
        })
        owner = processing.TerminalSagaExecution(
            owner="terminal-owner-recovery",
            fencing_token=2,
        )
        find_row = MagicMock()
        mutate_sheet = MagicMock()
        finalize = MagicMock()
        settle_notification = MagicMock()
        settle_reply = MagicMock()

        with patch.object(
            processing,
            "_claim_existing_terminal_saga_execution",
            return_value=owner,
        ) as claim_saga, patch.object(
            processing,
            "_sheets_client",
            return_value=MagicMock(),
        ) as sheets_client, patch.object(
            processing,
            "_get_first_tab_title",
            return_value=saga["tabTitle"],
        ), patch.object(
            processing,
            "_read_header_row2",
            return_value=["Property Address", "Notes"],
        ), patch.object(
            processing,
            "_find_row_by_anchor",
            side_effect=find_row,
        ), patch.object(
            processing,
            "_execute_or_reconcile_terminal_sheet_mutation",
            side_effect=mutate_sheet,
        ), patch.object(
            processing,
            "_finalize_terminal_thread_roots",
            side_effect=finalize,
        ), patch.object(
            processing,
            "_settle_terminal_notification_obligation",
            side_effect=settle_notification,
        ), patch.object(
            processing,
            "_settle_terminal_reply_obligation",
            side_effect=settle_reply,
        ), self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "persisted no Notes/Comments column",
        ):
            processing._resume_exact_terminal_saga(
                "user-1",
                {"Authorization": "Bearer token"},
                thread_id,
                dict(thread_ref._data),
                saga,
            )

        self.assertEqual(before_saga, saga)
        claim_saga.assert_not_called()
        sheets_client.assert_not_called()
        find_row.assert_not_called()
        mutate_sheet.assert_not_called()
        finalize.assert_not_called()
        settle_notification.assert_not_called()
        settle_reply.assert_not_called()

    def test_property_unavailable_existing_stable_note_is_not_duplicated(self):
        stable_note = (
            "[06/19/2026] Property marked unavailable - contact said: "
            "'no longer available'"
        )
        thread_id = "thread-existing-terminal-note"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
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
            body="This property is no longer available.",
            proposal=proposal,
            thread_ref=thread_ref,
            row_anchor="951 E FM 646",
            existing_note=stable_note,
        )

        persisted_note = result["moveRow"].call_args.kwargs["notes_value"]
        self.assertEqual(stable_note, persisted_note)
        self.assertEqual(1, persisted_note.count(stable_note))

    def test_property_unavailable_already_below_note_failure_blocks_terminalization(self):
        thread_id = "thread-already-below-note-failure"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 11,
        })
        proposal = {
            "updates": [],
            "events": [{"type": "property_unavailable", "reason": "requirements_mismatch"}],
            "response_email": "Understood, thank you.",
        }
        capture = {}

        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "operator review after an ambiguous provider outcome",
        ):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This space would not be a good fit for your client.",
                proposal=proposal,
                thread_ref=thread_ref,
                row_anchor="951 Tristar Dr",
                rownum=11,
                row_below_nonviable=True,
                existing_note="Legacy operator note",
                note_write_error=RuntimeError("already-below note repair failed"),
                capture=capture,
            )

        capture["moveRow"].assert_not_called()
        capture["noteUpdate"].assert_called_once()
        capture["stopThreads"].assert_not_called()
        self.assertEqual([], capture["statusUpdates"])
        self.assertEqual([], capture["handledEvents"])
        capture["sendReply"].assert_not_called()
        self.assertEqual(processing.THREAD_STATUS["active"], thread_ref._data["status"])
        self.assertEqual(
            "needs_operator_review",
            thread_ref._data["terminalSheetMutationAttempt"]["status"],
        )

    def test_move_attempt_is_durable_before_single_atomic_missing_divider_move(self):
        thread_id = "thread-terminal-sheet-attempt-order"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        observations = []

        def observe_provider_call(*_args, **_kwargs):
            observations.append({
                "attempt": copy.deepcopy(
                    thread_ref._data.get("terminalSheetMutationAttempt")
                ),
                "claim": copy.deepcopy(thread_ref._data.get("terminalSagaClaim")),
            })
            return 10

        result = self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="This property is no longer available.",
            proposal={
                "updates": [],
                "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                "response_email": None,
            },
            thread_ref=thread_ref,
            row_anchor="951 E FM 646",
            divider_preview_exists=False,
            ensure_divider_side_effect=observe_provider_call,
            move_row_side_effect=observe_provider_call,
        )

        self.assertEqual(1, len(observations))
        for observation in observations:
            attempt = observation["attempt"]
            claim = observation["claim"]
            self.assertIsInstance(attempt, dict)
            self.assertEqual("request_started", attempt["status"])
            self.assertEqual("move_with_note", attempt["mutationKind"])
            self.assertEqual(claim["owner"], attempt["owner"])
            self.assertEqual(claim["fencingToken"], attempt["fencingToken"])
            self.assertLess(attempt["providerDeadline"], claim["leaseUntil"])
        self.assertEqual(
            hashlib.sha256(
                result["moveRow"].call_args.kwargs["notes_value"].encode("utf-8")
            ).hexdigest(),
            observations[0]["attempt"]["noteHash"],
        )
        result["ensureDivider"].assert_not_called()
        result["moveRow"].assert_called_once()

    def test_missing_divider_then_move_429_partial_lane_is_eliminated(self):
        thread_id = "thread-missing-divider-atomic-429"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        provider_state = {"legacyDividerApplied": False}

        def legacy_divider_write(*_args, **_kwargs):
            provider_state["legacyDividerApplied"] = True
            return 10

        quota_response = MagicMock(status=429, reason="Too Many Requests")
        quota_error = HttpError(quota_response, b"atomic batch rejected")
        capture = {}

        with self.assertRaises(processing.RetryableProcessingError):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [{
                        "type": "property_unavailable",
                        "reason": "no_longer_available",
                    }],
                    "response_email": None,
                },
                thread_ref=thread_ref,
                row_anchor="951 E FM 646",
                divider_preview_exists=False,
                ensure_divider_side_effect=legacy_divider_write,
                move_row_side_effect=quota_error,
                capture=capture,
            )

        self.assertFalse(provider_state["legacyDividerApplied"])
        capture["ensureDivider"].assert_not_called()
        capture["moveRow"].assert_called_once()
        attempt = thread_ref._data["terminalSheetMutationAttempt"]
        self.assertEqual("definitely_not_applied", attempt["status"])
        self.assertEqual(429, attempt["providerStatusCode"])

    def test_late_missing_divider_plan_drift_blocks_atomic_batch(self):
        thread_id = "thread-missing-divider-late-drift"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        capture = {}
        previews = [
            {"dividerRow": 10, "exists": False},
            # Another actor added a divider after immutable staging.
            {"dividerRow": 11, "exists": True},
        ]

        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "operator review|plan drifted",
        ):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [{
                        "type": "property_unavailable",
                        "reason": "no_longer_available",
                    }],
                    "response_email": None,
                },
                thread_ref=thread_ref,
                row_anchor="951 E FM 646",
                divider_preview_exists=False,
                divider_preview_side_effect=previews,
                capture=capture,
            )

        capture["ensureDivider"].assert_not_called()
        capture["moveRow"].assert_not_called()
        self.assertEqual(
            "needs_operator_review",
            thread_ref._data["terminalSheetMutationAttempt"]["status"],
        )

    def test_header_drift_after_staging_blocks_every_sheet_batch(self):
        thread_id = "thread-terminal-header-race"
        current_root, firestore, finalized_saga, _pending = (
            self._finalized_terminal_execution_fixture(thread_id)
        )
        saga = {**finalized_saga, "phase": "staged"}
        saga.pop("finalRow", None)
        current_root._data.update({
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "terminalSaga": saga,
            "terminalSagaKey": saga["sagaKey"],
            "terminalSheetMutationAttempt": None,
            "terminalSheetMutationHistory": None,
            "terminalSheetMutationReview": None,
        })
        owner = processing.TerminalSagaExecution(
            owner=current_root._data["terminalSagaClaim"]["owner"],
            fencing_token=current_root._data["terminalSagaClaim"]["fencingToken"],
        )
        frozen_header = [
            "Property Address",
            "City",
            "Leasing Contact",
            "Email",
            "Total SF",
            "Rent/SF/Yr",
            "Ops Ex / SF",
            "Notes",
        ]
        drifted_header = ["Inserted Column", *frozen_header]
        existing_move = MagicMock()
        missing_divider_move = MagicMock()

        with patch.object(processing, "_fs", firestore), patch.object(
            processing,
            "_read_header_row2",
            return_value=drifted_header,
        ), patch.object(
            processing,
            "move_row_below_divider",
            existing_move,
        ), patch.object(
            processing,
            "move_row_below_new_divider_atomic",
            missing_divider_move,
        ), patch.object(
            processing,
            "_read_terminal_sheet_mutation_effect",
            return_value=("unreadable", "layout drift blocked readback"),
        ), self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "operator review",
        ):
            processing._execute_or_reconcile_terminal_sheet_mutation(
                "user-1",
                thread_id,
                MagicMock(),
                saga["sheetId"],
                saga["tabTitle"],
                frozen_header,
                saga["notesColumnIndex"],
                saga,
                owner,
                "move_with_note",
            )

        existing_move.assert_not_called()
        missing_divider_move.assert_not_called()
        self.assertEqual(
            "needs_operator_review",
            current_root._data["terminalSheetMutationAttempt"]["status"],
        )

    def test_existing_divider_path_reads_without_divider_creation_mutation(self):
        thread_id = "thread-terminal-existing-divider-read-only"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        result = self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="This property is no longer available.",
            proposal={
                "updates": [],
                "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                "response_email": None,
            },
            thread_ref=thread_ref,
            row_anchor="951 E FM 646",
            divider_preview_exists=True,
        )

        result["ensureDivider"].assert_not_called()
        result["moveRow"].assert_called_once()

    def test_already_below_note_attempt_is_durable_before_note_write(self):
        thread_id = "thread-terminal-ensure-note-attempt-order"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 11,
        })
        observations = []

        def observe_note_write():
            observations.append({
                "attempt": copy.deepcopy(
                    thread_ref._data.get("terminalSheetMutationAttempt")
                ),
                "claim": copy.deepcopy(thread_ref._data.get("terminalSagaClaim")),
            })
            return {}

        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="This space would not be a good fit for your client.",
            proposal={
                "updates": [],
                "events": [{"type": "property_unavailable", "reason": "requirements_mismatch"}],
                "response_email": None,
            },
            thread_ref=thread_ref,
            row_anchor="951 Tristar Dr",
            rownum=11,
            row_below_nonviable=True,
            existing_note="Legacy operator note",
            note_write_error=observe_note_write,
        )

        self.assertEqual(1, len(observations))
        attempt = observations[0]["attempt"]
        claim = observations[0]["claim"]
        self.assertIsInstance(attempt, dict)
        self.assertEqual("request_started", attempt["status"])
        self.assertEqual("ensure_note", attempt["mutationKind"])
        self.assertEqual(11, attempt["sourceRow"])
        self.assertEqual(11, attempt["finalRow"])
        self.assertLess(attempt["providerDeadline"], claim["leaseUntil"])

    def test_new_owner_reconciles_applied_move_attempt_without_second_mutation(self):
        thread_id = "thread-terminal-sheet-attempt-applied-reconcile"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        provider_claim = {}

        def apply_then_raise(*_args, **_kwargs):
            provider_claim.update(copy.deepcopy(thread_ref._data["terminalSagaClaim"]))
            raise RuntimeError("Sheet applied move then response was lost")

        with self.assertRaises(processing.RetryableProcessingError):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                    "response_email": None,
                },
                thread_ref=thread_ref,
                row_anchor="951 E FM 646",
                move_row_side_effect=apply_then_raise,
            )

        saga = dict(thread_ref._data["terminalSaga"])
        thread_ref._data.setdefault(
            "terminalSheetMutationAttempt",
            self._test_terminal_sheet_attempt(saga, provider_claim, "move_with_note"),
        )
        second_capture = {}
        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="This property is no longer available.",
            proposal={"updates": [], "events": [], "response_email": None},
            thread_ref=thread_ref,
            row_anchor="951 E FM 646",
            rownum=10,
            row_below_nonviable=True,
            existing_note=saga["note"],
            capture=second_capture,
        )

        second_capture["ensureDivider"].assert_not_called()
        second_capture["moveRow"].assert_not_called()
        second_capture["noteUpdate"].assert_not_called()
        self.assertIsNone(thread_ref._data.get("terminalSheetMutationAttempt"))
        attempt = thread_ref._data["terminalSettlements"][-1][
            "sheetMutationAttempt"
        ]
        self.assertEqual("reconciled_applied", attempt["status"])
        self.assertEqual(provider_claim["owner"], attempt["owner"])
        self.assertEqual(provider_claim["fencingToken"], attempt["fencingToken"])
        self.assertNotEqual(provider_claim["owner"], attempt["reconciledByOwner"])

    def test_absent_or_partial_move_evidence_requires_review_without_second_mutation(self):
        for evidence_kind, rownum, row_below, existing_note in (
            ("absent", 3, False, ""),
            ("partial_note_missing", 10, True, "Legacy operator note"),
        ):
            with self.subTest(evidence=evidence_kind):
                thread_id = f"thread-terminal-sheet-{evidence_kind}"
                thread_ref = FakeDocumentRef({
                    "clientId": "client-1",
                    "email": ["bp21harrison@gmail.com"],
                    "status": processing.THREAD_STATUS["active"],
                    "rowNumber": 3,
                })
                provider_claim = {}

                def ambiguous_move(*_args, **_kwargs):
                    provider_claim.update(copy.deepcopy(thread_ref._data["terminalSagaClaim"]))
                    raise RuntimeError("ambiguous Sheet move")

                with self.assertRaises(processing.RetryableProcessingError):
                    self._run_tour_invite_reply_processing(
                        thread_id=thread_id,
                        body="This property is no longer available.",
                        proposal={
                            "updates": [],
                            "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                            "response_email": None,
                        },
                        thread_ref=thread_ref,
                        row_anchor="951 E FM 646",
                        move_row_side_effect=ambiguous_move,
                    )
                saga = dict(thread_ref._data["terminalSaga"])
                thread_ref._data.setdefault(
                    "terminalSheetMutationAttempt",
                    self._test_terminal_sheet_attempt(
                        saga, provider_claim, "move_with_note"
                    ),
                )
                second_capture = {}
                with self.assertRaisesRegex(
                    processing.RetryableProcessingError,
                    "operator review",
                ):
                    self._run_tour_invite_reply_processing(
                        thread_id=thread_id,
                        body="This property is no longer available.",
                        proposal={"updates": [], "events": [], "response_email": None},
                        thread_ref=thread_ref,
                        row_anchor="951 E FM 646",
                        rownum=rownum,
                        row_below_nonviable=row_below,
                        existing_note=existing_note,
                        capture=second_capture,
                    )

                second_capture["ensureDivider"].assert_not_called()
                second_capture["moveRow"].assert_not_called()
                second_capture["noteUpdate"].assert_not_called()
                attempt = thread_ref._data["terminalSheetMutationAttempt"]
                self.assertEqual("needs_operator_review", attempt["status"])
                self.assertTrue(attempt["operatorReviewRequired"])

    def test_unreadable_or_malformed_move_attempt_fails_closed_without_second_mutation(self):
        for evidence_kind in ("unreadable", "malformed_attempt"):
            with self.subTest(evidence=evidence_kind):
                thread_id = f"thread-terminal-sheet-{evidence_kind}"
                thread_ref = FakeDocumentRef({
                    "clientId": "client-1",
                    "email": ["bp21harrison@gmail.com"],
                    "status": processing.THREAD_STATUS["active"],
                    "rowNumber": 3,
                })
                provider_claim = {}

                def ambiguous_move(*_args, **_kwargs):
                    provider_claim.update(copy.deepcopy(thread_ref._data["terminalSagaClaim"]))
                    raise RuntimeError("ambiguous Sheet move")

                with self.assertRaises(processing.RetryableProcessingError):
                    self._run_tour_invite_reply_processing(
                        thread_id=thread_id,
                        body="This property is no longer available.",
                        proposal={
                            "updates": [],
                            "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                            "response_email": None,
                        },
                        thread_ref=thread_ref,
                        row_anchor="951 E FM 646",
                        move_row_side_effect=ambiguous_move,
                    )
                saga = dict(thread_ref._data["terminalSaga"])
                attempt = thread_ref._data.setdefault(
                    "terminalSheetMutationAttempt",
                    self._test_terminal_sheet_attempt(
                        saga, provider_claim, "move_with_note"
                    ),
                )
                if evidence_kind == "malformed_attempt":
                    attempt["noteHash"] = "tampered-note-hash"
                second_capture = {}
                with self.assertRaises(processing.RetryableProcessingError):
                    self._run_tour_invite_reply_processing(
                        thread_id=thread_id,
                        body="This property is no longer available.",
                        proposal={"updates": [], "events": [], "response_email": None},
                        thread_ref=thread_ref,
                        row_anchor="951 E FM 646",
                        rownum=10 if evidence_kind == "unreadable" else 3,
                        row_below_nonviable=evidence_kind == "unreadable",
                        existing_note="",
                        note_read_error=(
                            RuntimeError("Sheet readback unavailable")
                            if evidence_kind == "unreadable"
                            else None
                        ),
                        capture=second_capture,
                    )

                second_capture["ensureDivider"].assert_not_called()
                second_capture["moveRow"].assert_not_called()
                second_capture["noteUpdate"].assert_not_called()
                if evidence_kind == "unreadable":
                    self.assertEqual(
                        "needs_operator_review",
                        thread_ref._data["terminalSheetMutationAttempt"]["status"],
                    )

    def test_new_owner_reconciles_applied_ensure_note_without_second_note_write(self):
        thread_id = "thread-terminal-ensure-note-applied-reconcile"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 11,
        })
        provider_claim = {}

        def note_apply_then_raise():
            provider_claim.update(copy.deepcopy(thread_ref._data["terminalSagaClaim"]))
            raise RuntimeError("Sheet applied note then response was lost")

        with self.assertRaises(processing.RetryableProcessingError):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This space would not be a good fit for your client.",
                proposal={
                    "updates": [],
                    "events": [{"type": "property_unavailable", "reason": "requirements_mismatch"}],
                    "response_email": None,
                },
                thread_ref=thread_ref,
                row_anchor="951 Tristar Dr",
                rownum=11,
                row_below_nonviable=True,
                existing_note="Legacy operator note",
                note_write_error=note_apply_then_raise,
            )
        saga = dict(thread_ref._data["terminalSaga"])
        thread_ref._data.setdefault(
            "terminalSheetMutationAttempt",
            self._test_terminal_sheet_attempt(saga, provider_claim, "ensure_note"),
        )
        second_capture = {}
        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="This space would not be a good fit for your client.",
            proposal={"updates": [], "events": [], "response_email": None},
            thread_ref=thread_ref,
            row_anchor="951 Tristar Dr",
            rownum=11,
            row_below_nonviable=True,
            existing_note=saga["note"],
            capture=second_capture,
        )

        second_capture["noteUpdate"].assert_not_called()
        self.assertIsNone(thread_ref._data.get("terminalSheetMutationAttempt"))
        self.assertEqual(
            "reconciled_applied",
            thread_ref._data["terminalSettlements"][-1][
                "sheetMutationAttempt"
            ]["status"],
        )

    def test_requirements_mismatch_full_path_persists_truthful_stable_note(self):
        thread_id = "thread-requirements-mismatch-note"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        proposal = {
            "updates": [],
            "events": [{"type": "property_unavailable", "reason": "requirements_mismatch"}],
            "response_email": None,
        }

        result = self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="This space would not be a good fit for your client.",
            proposal=proposal,
            thread_ref=thread_ref,
            row_anchor="951 Tristar Dr",
            existing_note="Reviewed loading configuration",
        )

        persisted_note = result["moveRow"].call_args.kwargs["notes_value"]
        self.assertIn("[06/19/2026]", persisted_note)
        self.assertIn("does not meet client requirements", persisted_note.lower())
        self.assertNotIn("marked unavailable", persisted_note.lower())

    def test_property_unavailable_finalization_batch_failure_retries_exact_staged_roots(self):
        thread_id = "thread-finalization-power-loss"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        sibling_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        proposal = {
            "updates": [],
            "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
            "response_email": "Thank you for letting me know.",
        }
        first_capture = {}

        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "terminal Firestore finalization failed",
        ):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal=proposal,
                thread_ref=current_root,
                thread_docs={
                    thread_id: current_root,
                    "thread-finalization-power-loss-sibling": sibling_root,
                },
                row_anchor="951 E FM 646",
                finalization_error=RuntimeError("simulated power loss before Firestore commit"),
                capture=first_capture,
            )

        first_capture["moveRow"].assert_called_once()
        first_capture["sendReply"].assert_not_called()
        stable_note = first_capture["moveRow"].call_args.kwargs["notes_value"]
        for root in (current_root, sibling_root):
            self.assertEqual(processing.THREAD_STATUS["active"], root._data["status"])
            self.assertEqual(3, root._data["rowNumber"])
            self.assertEqual("stopped", root._data["followUpStatus"])
            self.assertEqual("no_longer_available", root._data["pendingTerminalReason"])
            self.assertEqual(3, root._data["pendingTerminalSourceRow"])

        second_capture = {}
        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="This property is no longer available.",
            proposal=proposal,
            thread_ref=current_root,
            thread_docs={
                thread_id: current_root,
                "thread-finalization-power-loss-sibling": sibling_root,
            },
            row_anchor="951 E FM 646",
            rownum=10,
            row_below_nonviable=True,
            existing_note=stable_note,
            capture=second_capture,
        )

        second_capture["moveRow"].assert_not_called()
        second_capture["noteUpdate"].assert_not_called()
        second_capture["sendReply"].assert_called_once()
        self.assertEqual(["send", "complete"], second_capture["callTrace"])
        for root in (current_root, sibling_root):
            self.assertEqual(processing.THREAD_STATUS["stopped"], root._data["status"])
            self.assertEqual(10, root._data["rowNumber"])
            self.assertEqual("no_longer_available", root._data["nonViableReason"])
            self.assertIsNone(root._data["pendingTerminalReason"])
            self.assertIsNone(root._data["pendingTerminalSourceRow"])
        self.assertTrue(
            any(key.startswith("handledEvents.property_unavailable") for key in current_root._data)
        )

    def test_terminal_saga_resumes_same_source_without_fresh_terminal_event(self):
        thread_id = "thread-terminal-saga-no-event"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        first_proposal = {
            "updates": [],
            "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
            "response_email": "Thank you for letting me know.",
        }
        first_capture = {}

        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "terminal Firestore finalization failed",
        ):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal=first_proposal,
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                finalization_error=RuntimeError("definite finalization failure"),
                capture=first_capture,
            )

        saga = dict(current_root._data["terminalSaga"])
        original_note = saga["note"]
        original_reason = saga["reason"]
        original_response_body = saga["responseBody"]
        self.assertEqual([], first_capture["notifications"])
        first_capture["sendReply"].assert_not_called()

        second_capture = {}
        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="This property is no longer available.",
            proposal={"updates": [], "events": [], "response_email": None},
            thread_ref=current_root,
            row_anchor="951 E FM 646",
            rownum=10,
            row_below_nonviable=True,
            existing_note=original_note,
            capture=second_capture,
        )

        second_capture["proposeUpdates"].assert_not_called()
        second_capture["moveRow"].assert_not_called()
        second_capture["noteUpdate"].assert_not_called()
        second_capture["sendReply"].assert_called_once()
        self.assertEqual(original_response_body, second_capture["sendReply"].call_args.args[2])
        self.assertEqual(original_reason, current_root._data["nonViableReason"])
        self.assertIsNone(current_root._data.get("terminalSaga"))
        self.assertFalse(current_root._data.get("terminalReplyOwed"))
        self.assertFalse(current_root._data.get("terminalNotificationOwed"))
        self.assertEqual(["send", "complete"], second_capture["callTrace"])

    def test_terminal_saga_ignores_changed_reason_on_same_source_retry(self):
        thread_id = "thread-terminal-saga-reason-drift"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        first_capture = {}

        with self.assertRaises(processing.RetryableProcessingError):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                    "response_email": None,
                },
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                finalization_error=RuntimeError("definite finalization failure"),
                capture=first_capture,
            )

        original_saga = dict(current_root._data["terminalSaga"])
        second_capture = {}
        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="This property is no longer available.",
            proposal={
                "updates": [],
                "events": [{"type": "property_unavailable", "reason": "requirements_mismatch"}],
                "response_email": "The requirements changed.",
            },
            thread_ref=current_root,
            row_anchor="951 E FM 646",
            rownum=10,
            row_below_nonviable=True,
            existing_note=original_saga["note"],
            capture=second_capture,
        )

        second_capture["proposeUpdates"].assert_not_called()
        second_capture["noteUpdate"].assert_not_called()
        self.assertEqual("no_longer_available", current_root._data["nonViableReason"])
        sent_body = second_capture["sendReply"].call_args.args[2]
        self.assertEqual(original_saga["responseBody"], sent_body)
        self.assertNotIn("does not meet the requirements", sent_body.lower())

    def test_terminal_saga_ignores_changed_alternate_context_and_note_on_retry(self):
        thread_id = "thread-terminal-saga-note-drift"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        first_capture = {}

        with self.assertRaises(processing.RetryableProcessingError):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                    "response_email": None,
                },
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                finalization_error=RuntimeError("definite finalization failure"),
                capture=first_capture,
            )

        original_saga = dict(current_root._data["terminalSaga"])
        self.assertNotIn("alternate", original_saga["note"].lower())
        second_capture = {}
        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="This property is no longer available.",
            proposal={
                "updates": [],
                "events": [
                    {
                        "type": "new_property",
                        "notes": "A new alternate context invented on retry",
                    },
                    {"type": "property_unavailable", "reason": "no_longer_available"},
                ],
                "response_email": "I will review the new alternate.",
            },
            thread_ref=current_root,
            row_anchor="951 E FM 646",
            rownum=10,
            row_below_nonviable=True,
            existing_note=original_saga["note"],
            capture=second_capture,
        )

        second_capture["proposeUpdates"].assert_not_called()
        second_capture["noteUpdate"].assert_not_called()
        self.assertEqual(original_saga["responseBody"], second_capture["sendReply"].call_args.args[2])
        self.assertEqual("no_longer_available", current_root._data["nonViableReason"])

    def test_terminal_saga_rejects_persisted_immutable_payload_tampering(self):
        thread_id = "thread-terminal-saga-tamper"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })

        with self.assertRaises(processing.RetryableProcessingError):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                    "response_email": None,
                },
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                finalization_error=RuntimeError("definite finalization failure"),
            )

        current_root._data["terminalSaga"]["reason"] = "requirements_mismatch"
        capture = {}
        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "immutable terminal saga hash",
        ):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={"updates": [], "events": [], "response_email": None},
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                rownum=10,
                row_below_nonviable=True,
                existing_note=current_root._data["terminalSaga"]["note"],
                capture=capture,
            )

        capture["proposeUpdates"].assert_not_called()
        capture["sendReply"].assert_not_called()
        self.assertEqual(processing.THREAD_STATUS["active"], current_root._data["status"])

    def test_terminal_saga_recovers_apply_then_raise_and_only_exact_source_bypasses_stop(self):
        thread_id = "thread-terminal-saga-ambiguous-commit"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        first_capture = {}

        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "terminal Firestore finalization failed",
        ):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                    "response_email": None,
                },
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                finalization_apply_then_error=RuntimeError("commit applied then connection reset"),
                capture=first_capture,
            )

        self.assertEqual(processing.THREAD_STATUS["stopped"], current_root._data["status"])
        self.assertTrue(current_root._data["terminalReplyOwed"])
        self.assertTrue(current_root._data["terminalNotificationOwed"])
        self.assertEqual([], first_capture["notificationAttempts"])
        first_capture["sendReply"].assert_not_called()

        unrelated_capture = {}
        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "terminal saga transition is pending for a different source message",
        ):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="A different later message.",
                proposal={"updates": [], "events": [], "response_email": None},
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                rownum=10,
                row_below_nonviable=True,
                msg_id_override="msg-unrelated-source",
                internet_message_id_override="<unrelated-source@mock.test>",
                capture=unrelated_capture,
            )
        unrelated_capture["proposeUpdates"].assert_not_called()
        unrelated_capture["sendReply"].assert_not_called()
        self.assertTrue(current_root._data["terminalReplyOwed"])

        recovery_capture = {}
        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="This property is no longer available.",
            proposal={"updates": [], "events": [], "response_email": None},
            thread_ref=current_root,
            row_anchor="951 E FM 646",
            rownum=10,
            row_below_nonviable=True,
            existing_note=current_root._data["terminalSaga"]["note"],
            capture=recovery_capture,
        )

        recovery_capture["proposeUpdates"].assert_not_called()
        recovery_capture["sendReply"].assert_called_once()
        self.assertEqual(1, len(recovery_capture["notifications"]))
        self.assertFalse(current_root._data.get("terminalReplyOwed"))
        self.assertFalse(current_root._data.get("terminalNotificationOwed"))
        self.assertIsNone(current_root._data.get("terminalSaga"))

    def test_terminal_notification_is_post_finalization_and_recovers_strictly(self):
        thread_id = "thread-terminal-notification-recovery"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        first_capture = {}

        with self.assertRaises(processing.RetryableProcessingError):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                    "response_email": None,
                },
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                notification_errors=[RuntimeError("notification write unavailable")],
                capture=first_capture,
            )

        self.assertEqual(processing.THREAD_STATUS["stopped"], current_root._data["status"])
        self.assertTrue(current_root._data["terminalNotificationOwed"])
        self.assertTrue(current_root._data["terminalReplyOwed"])
        self.assertEqual([], first_capture["notifications"])
        self.assertEqual(
            processing.THREAD_STATUS["stopped"],
            first_capture["notificationAttempts"][0]["threadStatus"],
        )
        self.assertFalse(
            any(key.startswith("handledEvents.") for key in current_root._data)
        )
        first_capture["sendReply"].assert_not_called()

        recovery_capture = {}
        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="This property is no longer available.",
            proposal={"updates": [], "events": [], "response_email": None},
            thread_ref=current_root,
            row_anchor="951 E FM 646",
            rownum=10,
            row_below_nonviable=True,
            existing_note=current_root._data["terminalSaga"]["note"],
            capture=recovery_capture,
        )

        self.assertEqual(1, len(recovery_capture["notifications"]))
        self.assertTrue(
            any(key.startswith("handledEvents.") for key in current_root._data)
        )
        recovery_capture["sendReply"].assert_called_once()
        self.assertIsNone(current_root._data.get("terminalSaga"))

    def test_terminal_reply_send_success_reconciles_after_outcome_write_failure(self):
        thread_id = "thread-terminal-reply-outcome-ambiguity"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        first_capture = {}

        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "terminal reply outcome persistence failed",
        ):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                    "response_email": None,
                },
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                reply_outcome_update_error=RuntimeError(
                    "reply outcome write failed after Graph success"
                ),
                capture=first_capture,
            )

        first_capture["sendReply"].assert_called_once()
        self.assertTrue(current_root._data["terminalReplyOwed"])
        self.assertFalse(current_root._data["terminalNotificationOwed"])
        self.assertIsNotNone(current_root._data.get("terminalSaga"))

        current_root._update_error = None
        current_root._data["terminalSagaClaim"] = {
            **current_root._data["terminalSagaClaim"],
            "leaseUntil": datetime.now(timezone.utc) - timedelta(seconds=1),
        }
        permit_id = current_root._data["activeGraphSendPermit"]["permitId"]
        retained_permit = current_root.collection(
            "graphSendPermits"
        ).document(permit_id)._data
        sent_match = _exact_sent_evidence_for_test(retained_permit)
        recovery_capture = {}
        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="This property is no longer available.",
            proposal={"updates": [], "events": [], "response_email": None},
            thread_ref=current_root,
            row_anchor="951 E FM 646",
            rownum=10,
            row_below_nonviable=True,
            existing_note=current_root._data["terminalSaga"]["note"],
            sent_reply_match=sent_match,
            capture=recovery_capture,
        )

        recovery_capture["sentReplyLookup"].assert_called_once()
        recovery_capture["sendReply"].assert_not_called()
        recovery_capture["queuePending"].assert_not_called()
        self.assertEqual("sent_reconciled", current_root._data["terminalReplyOutcome"])
        self.assertFalse(current_root._data["terminalReplyOwed"])
        self.assertIsNone(current_root._data.get("terminalSaga"))

    def test_different_source_on_pending_terminal_root_is_history_only_and_retryable(self):
        thread_id = "thread-terminal-pending-different-source"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        first_capture = {}

        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "property_unavailable event failed",
        ):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                    "response_email": None,
                },
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                sheet_attempt_error=RuntimeError(
                    "Sheet mutation intent temporarily unavailable"
                ),
                capture=first_capture,
            )

        original_saga = dict(current_root._data["terminalSaga"])
        different_source_capture = {}
        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "terminal saga transition is pending for a different source message",
        ):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="A later reply with an unrelated attachment and a different property.",
                proposal={
                    "updates": [{"column": "Total SF", "value": "9999"}],
                    "events": [{"type": "new_property", "address": "Different Property"}],
                    "response_email": "This must not send.",
                },
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                msg_id_override="msg-different-source",
                internet_message_id_override="<different-source@mock.test>",
                capture=different_source_capture,
            )

        for side_effect in (
            "fetchSheet",
            "fetchPdfs",
            "fetchLinkedAssets",
            "fetchUrl",
            "writeOrder",
            "proposeUpdates",
            "applyProposal",
            "ensureDivider",
            "moveRow",
            "sendReply",
            "queuePending",
        ):
            different_source_capture[side_effect].assert_not_called()
        self.assertEqual([], different_source_capture["notifications"])
        self.assertEqual(original_saga, current_root._data["terminalSaga"])
        self.assertEqual(
            original_saga["sagaKey"],
            current_root._data["terminalSagaKey"],
        )

        exact_source_capture = {}
        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="This property is no longer available.",
            proposal={"updates": [], "events": [], "response_email": None},
            thread_ref=current_root,
            row_anchor="951 E FM 646",
            capture=exact_source_capture,
        )

        exact_source_capture["proposeUpdates"].assert_not_called()
        exact_source_capture["moveRow"].assert_called_once()
        exact_source_capture["sendReply"].assert_called_once()
        self.assertIsNone(current_root._data.get("terminalSaga"))

    def test_different_source_admission_precedes_campaign_and_thread_mutation_matrix(self):
        campaign_states = (
            ("active", "live", False),
            ("paused", "live", True),
            ("stopped", "stopped", False),
        )
        for phase in ("staged", "finalized"):
            for state_name, campaign_status, automation_paused in campaign_states:
                with self.subTest(phase=phase, campaign_state=state_name):
                    thread_id = f"thread-other-source-{phase}-{state_name}"
                    current_root = FakeDocumentRef({
                        "clientId": "client-1",
                        "email": ["bp21harrison@gmail.com"],
                        "status": processing.THREAD_STATUS["active"],
                        "rowNumber": 3,
                        "followUpStatus": "waiting",
                    })
                    initial_failure = (
                        {
                            "sheet_attempt_error": RuntimeError("stage only"),
                        }
                        if phase == "staged"
                        else {
                            "finalization_apply_then_error": RuntimeError(
                                "finalized but acknowledgement was lost"
                            )
                        }
                    )
                    with self.assertRaises(processing.RetryableProcessingError):
                        self._run_tour_invite_reply_processing(
                            thread_id=thread_id,
                            body="This property is no longer available.",
                            proposal={
                                "updates": [],
                                "events": [{
                                    "type": "property_unavailable",
                                    "reason": "no_longer_available",
                                }],
                                "response_email": None,
                            },
                            thread_ref=current_root,
                            row_anchor="951 E FM 646",
                            **initial_failure,
                        )
                    self.assertEqual(phase, current_root._data["terminalSaga"]["phase"])

                    root_before = copy.deepcopy(current_root._data)
                    self.campaign_decision.reset_mock()
                    different_capture = {}
                    with self.assertRaisesRegex(
                        processing.RetryableProcessingError,
                        "terminal saga transition is pending for a different source message",
                    ):
                        self._run_tour_invite_reply_processing(
                            thread_id=thread_id,
                            body="A later reply that must remain history-only.",
                            proposal={
                                "updates": [{"column": "Total SF", "value": "9999"}],
                                "events": [{
                                    "type": "new_property",
                                    "address": "Different Property",
                                }],
                                "response_email": "This must not send.",
                            },
                            thread_ref=current_root,
                            row_anchor="951 E FM 646",
                            msg_id_override=f"msg-different-{phase}-{state_name}",
                            internet_message_id_override=(
                                f"<different-{phase}-{state_name}@mock.test>"
                            ),
                            campaign_status=campaign_status,
                            campaign_automation_paused=automation_paused,
                            capture=different_capture,
                        )

                    self.campaign_decision.assert_not_called()
                    different_capture["threadStatusLookup"].assert_not_called()
                    different_capture["findClientByEmail"].assert_not_called()
                    different_capture["resumeManualContinuation"].assert_not_called()
                    different_capture["cancelFollowup"].assert_not_called()
                    different_capture["dumpThread"].assert_not_called()
                    different_capture["saveInboundMessage"].assert_called_once()
                    different_capture["indexInboundMessage"].assert_called_once()
                    for side_effect in (
                        "fetchSheet",
                        "fetchPdfs",
                        "fetchLinkedAssets",
                        "fetchUrl",
                        "writeOrder",
                        "proposeUpdates",
                        "applyProposal",
                        "ensureDivider",
                        "moveRow",
                        "sendReply",
                        "queuePending",
                    ):
                        different_capture[side_effect].assert_not_called()
                    self.assertEqual([], different_capture["notifications"])
                    self.assertNotIn("processed", current_root._data)

                    root_after_without_envelope = {
                        key: value
                        for key, value in current_root._data.items()
                        if key not in {"updatedAt", "lastInboundEnvelope"}
                    }
                    root_before_without_envelope = {
                        key: value
                        for key, value in root_before.items()
                        if key not in {"updatedAt", "lastInboundEnvelope"}
                    }
                    self.assertEqual(
                        root_before_without_envelope,
                        root_after_without_envelope,
                    )

                    exact_capture = {}
                    exact_kwargs = (
                        {
                            "rownum": 10,
                            "row_below_nonviable": True,
                            "existing_note": current_root._data["terminalSaga"]["note"],
                        }
                        if phase == "finalized"
                        else {}
                    )
                    self._run_tour_invite_reply_processing(
                        thread_id=thread_id,
                        body="This property is no longer available.",
                        proposal={"updates": [], "events": [], "response_email": None},
                        thread_ref=current_root,
                        row_anchor="951 E FM 646",
                        capture=exact_capture,
                        **exact_kwargs,
                    )
                    exact_capture["proposeUpdates"].assert_not_called()
                    self.assertIsNone(current_root._data.get("terminalSaga"))

    def test_definite_unsent_terminal_reply_reconciles_exact_pending_doc_without_requeue(self):
        thread_id = "thread-terminal-definite-unsent-pending"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        pending_response_docs = {}
        first_capture = {}

        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "terminal reply outcome persistence failed",
        ):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                    "response_email": None,
                },
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                send_result=False,
                queue_outcome_update_error=RuntimeError(
                    "pending response committed but terminal outcome write failed"
                ),
                pending_response_docs=pending_response_docs,
                capture=first_capture,
            )

        first_capture["sendReply"].assert_called_once()
        first_capture["queuePending"].assert_not_called()
        self.assertIn(thread_id, pending_response_docs)
        self.assertEqual(
            "queueing_response_retry",
            current_root._data["terminalReplyAttempt"]["status"],
        )
        self.assertTrue(current_root._data["terminalReplyOwed"])

        current_root._update_error = None
        current_root._data["terminalSagaClaim"] = {
            **current_root._data["terminalSagaClaim"],
            "leaseUntil": datetime.now(timezone.utc) - timedelta(seconds=1),
        }
        recovery_capture = {}
        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="This property is no longer available.",
            proposal={"updates": [], "events": [], "response_email": None},
            thread_ref=current_root,
            row_anchor="951 E FM 646",
            rownum=10,
            row_below_nonviable=True,
            existing_note=current_root._data["terminalSaga"]["note"],
            pending_response_docs=pending_response_docs,
            capture=recovery_capture,
        )

        recovery_capture["sentReplyLookup"].assert_not_called()
        recovery_capture["sendReply"].assert_not_called()
        recovery_capture["queuePending"].assert_not_called()
        self.assertEqual("queued_retry", current_root._data["terminalReplyOutcome"])
        self.assertFalse(current_root._data["terminalReplyOwed"])
        self.assertIsNone(current_root._data.get("terminalSaga"))

    def test_definite_unsent_terminal_reply_rejects_mismatched_pending_doc(self):
        thread_id = "thread-terminal-definite-unsent-pending-drift"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        pending_response_docs = {}

        with self.assertRaises(processing.RetryableProcessingError):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                    "response_email": None,
                },
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                send_result=False,
                queue_outcome_update_error=RuntimeError("owed clear failed"),
                pending_response_docs=pending_response_docs,
            )

        current_root._update_error = None
        current_root._data["terminalSagaClaim"] = {
            **current_root._data["terminalSagaClaim"],
            "leaseUntil": datetime.now(timezone.utc) - timedelta(seconds=1),
        }
        pending_response_docs[thread_id]._data["responseBody"] = "mismatched body"
        recovery_capture = {}
        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "terminal pending response evidence does not match immutable reply intent",
        ):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={"updates": [], "events": [], "response_email": None},
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                rownum=10,
                row_below_nonviable=True,
                existing_note=current_root._data["terminalSaga"]["note"],
                pending_response_docs=pending_response_docs,
                capture=recovery_capture,
            )

        recovery_capture["sentReplyLookup"].assert_not_called()
        recovery_capture["sendReply"].assert_not_called()
        recovery_capture["queuePending"].assert_not_called()
        self.assertTrue(current_root._data["terminalReplyOwed"])
        self.assertIsNotNone(current_root._data.get("terminalSaga"))

    def test_terminal_recovery_does_not_steal_active_lease_for_stale_reply_attempt(self):
        thread_id = "thread-terminal-active-lease-stale-attempt"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        capture = {}
        with self.assertRaises(processing.RetryableProcessingError):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                    "response_email": None,
                },
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                sheet_attempt_error=RuntimeError("pause staged owner"),
                capture=capture,
            )

        saga = dict(current_root._data["terminalSaga"])
        current_root._data["terminalSagaClaim"] = {
            **current_root._data["terminalSagaClaim"],
            "owner": "still-running-original-worker",
            "leaseUntil": datetime.now(timezone.utc) + timedelta(minutes=5),
            "status": "processing",
        }
        current_root._data["terminalReplyAttempt"] = {
            "sagaKey": "stale-prior-saga",
            "responseBodyHash": "stale-prior-body",
            "status": "committed",
        }

        with patch.object(processing, "_fs", capture["firestore"]):
            with self.assertRaisesRegex(
                processing.RetryableProcessingError,
                "terminal saga is already owned by another active worker",
            ):
                processing._claim_existing_terminal_saga_execution(
                    "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
                    thread_id,
                    dict(current_root._data),
                    saga,
                )

        self.assertEqual(
            "still-running-original-worker",
            current_root._data["terminalSagaClaim"]["owner"],
        )

    def test_terminal_execution_rejects_missing_or_malformed_owned_lease(self):
        thread_id = "thread-terminal-malformed-owned-lease"
        current_root, firestore, saga, _pending_docs = (
            self._finalized_terminal_execution_fixture(thread_id)
        )
        terminal_saga_owner = processing.TerminalSagaExecution(
            owner="terminal-owner-a",
            fencing_token=1,
        )

        for malformed_lease in (None, "not-a-timestamp"):
            with self.subTest(lease=malformed_lease):
                current_root._data["terminalSagaClaim"]["leaseUntil"] = malformed_lease
                with patch.object(processing, "_fs", firestore):
                    with self.assertRaisesRegex(
                        processing.RetryableProcessingError,
                        "terminal saga execution lease is missing, malformed, or expired",
                    ):
                        processing._renew_terminal_saga_execution(
                            "user-1",
                            saga,
                            terminal_saga_owner,
                        )

    def test_stale_owner_cannot_finalize_after_new_owner_acquires_higher_fence(self):
        thread_id = "thread-terminal-stale-finalizer"
        current_root, firestore, saga, _pending_docs = (
            self._finalized_terminal_execution_fixture(thread_id)
        )
        owner_a = processing.TerminalSagaExecution(
            owner="terminal-owner-a",
            fencing_token=1,
        )
        staged_saga = {**saga, "phase": "staged"}
        staged_saga.pop("finalRow", None)
        current_root._data.update({
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "terminalSaga": staged_saga,
        })
        current_root._data["terminalSagaClaim"]["leaseUntil"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )

        with patch.object(processing, "_fs", firestore):
            owner_b = processing._claim_existing_terminal_saga_execution(
                "user-1",
                thread_id,
                dict(current_root._data),
                staged_saga,
            )
            before_finalize = copy.deepcopy(current_root._data)
            with self.assertRaisesRegex(
                processing.RetryableProcessingError,
                "owner/fence changed|ownership changed",
            ):
                processing._finalize_terminal_thread_roots(
                    "user-1",
                    "client-1",
                    thread_id,
                    staged_saga,
                    final_row=10,
                    terminal_saga_owner=owner_a,
                )

        self.assertGreater(owner_b.fencing_token, owner_a.fencing_token)
        self.assertEqual(before_finalize, current_root._data)
        self.assertEqual(processing.THREAD_STATUS["active"], current_root._data["status"])
        self.assertEqual(owner_b.owner, current_root._data["terminalSagaClaim"]["owner"])

    def test_finalization_transaction_conflicts_on_sibling_root_drift(self):
        current_id = "thread-terminal-finalize-current"
        sibling_id = "thread-terminal-finalize-sibling"
        current_root, _firestore, original_saga, _pending_docs = (
            self._finalized_terminal_execution_fixture(current_id)
        )
        sibling_root = FakeDocumentRef({
            "clientId": "client-1",
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        immutable = {
            key: value
            for key, value in original_saga.items()
            if key not in {"immutableHash", "phase", "finalRow"}
        }
        immutable["finalizationPlan"] = {
            "dividerRow": 10,
            "finalRow": 10,
            "claimThreadId": current_id,
            "terminalThreadIds": [current_id, sibling_id],
            "rowShifts": [],
            "writeCount": 2,
        }
        immutable_hash = hashlib.sha256(
            json.dumps(immutable, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        saga = {
            **immutable,
            "immutableHash": immutable_hash,
            "phase": "staged",
        }
        current_root._data.update({
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "terminalSaga": saga,
            "terminalSagaKey": saga["sagaKey"],
            "terminalSagaClaim": {
                **current_root._data["terminalSagaClaim"],
                "immutableHash": immutable_hash,
            },
        })
        sibling_root._data["terminalSagaKey"] = saga["sagaKey"]
        entered = Event()
        release = Event()
        firestore = FakeFirestore(
            current_root,
            FakeDocumentRef({"status": "live"}),
            thread_docs={current_id: current_root, sibling_id: sibling_root},
            transaction_before_update_events=(entered, release),
        )
        owner = processing.TerminalSagaExecution(
            owner=current_root._data["terminalSagaClaim"]["owner"],
            fencing_token=current_root._data["terminalSagaClaim"]["fencingToken"],
        )
        current_root._data["terminalSheetMutationAttempt"] = (
            self._test_terminal_sheet_attempt(
                saga,
                current_root._data["terminalSagaClaim"],
                "move_with_note",
                status="applied",
            )
        )

        with patch.object(processing, "_fs", firestore):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    processing._finalize_terminal_thread_roots,
                    "user-1",
                    "client-1",
                    current_id,
                    saga,
                    final_row=10,
                    terminal_saga_owner=owner,
                )
                self.assertTrue(
                    entered.wait(timeout=5),
                    "finalization did not reach its transactional write barrier",
                )
                sibling_root.update({"rowNumber": 99})
                release.set()
                with self.assertRaisesRegex(
                    processing.RetryableProcessingError,
                    "terminal Firestore finalization failed",
                ):
                    future.result(timeout=5)

        self.assertEqual(3, current_root._data["rowNumber"])
        self.assertEqual(99, sibling_root._data["rowNumber"])
        self.assertEqual(processing.THREAD_STATUS["active"], sibling_root._data["status"])

    def test_sheet_attempt_interleave_has_one_mutation_and_new_owner_alone_finalizes(self):
        thread_id = "thread-terminal-sheet-real-interleave"
        current_root, firestore, finalized_saga, _pending_docs = (
            self._finalized_terminal_execution_fixture(thread_id)
        )
        saga = {**finalized_saga, "phase": "staged"}
        saga.pop("finalRow", None)
        current_root._data.update({
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "terminalSaga": saga,
            "terminalSagaKey": saga["sagaKey"],
            "terminalNotificationOwed": False,
            "terminalReplyOwed": False,
            "terminalSheetMutationAttempt": None,
            "terminalSheetMutationHistory": None,
            "terminalSheetMutationReview": None,
        })
        owner_a = processing.TerminalSagaExecution(
            owner=current_root._data["terminalSagaClaim"]["owner"],
            fencing_token=current_root._data["terminalSagaClaim"]["fencingToken"],
        )
        provider_entered = Event()
        allow_provider = Event()
        sheet_state = {"applied": False, "mutationRequests": 0}
        rowvals = [
            saga["rowAnchor"],
            "",
            "Ryan",
            saga["replyRecipient"],
            "4531",
            "10.00",
            "3.31",
            saga["note"],
        ]
        header = [
            "Property Address",
            "City",
            "Leasing Contact",
            "Email",
            "Total SF",
            "Rent/SF/Yr",
            "Ops Ex / SF",
            "Notes",
        ]

        def one_use_provider_request(*_args, **_kwargs):
            sheet_state["mutationRequests"] += 1
            provider_entered.set()
            if not allow_provider.wait(timeout=5):
                raise RuntimeError("provider interleave barrier timed out")
            sheet_state["applied"] = True
            return 10

        def read_row(*_args, **_kwargs):
            return (10 if sheet_state["applied"] else 3, rowvals)

        sheets = MagicMock()
        move = MagicMock(side_effect=one_use_provider_request)
        user_id = "user-1"

        with patch.object(processing, "_fs", firestore), \
             patch.object(processing, "_read_header_row2", return_value=header), \
             patch.object(processing, "_preview_nonviable_divider", return_value={"dividerRow": 10, "exists": True}), \
             patch.object(processing, "move_row_below_divider", side_effect=move), \
             patch.object(processing, "_find_row_by_anchor", side_effect=read_row), \
             patch.object(processing, "_is_row_below_nonviable", side_effect=lambda *_args, **_kwargs: sheet_state["applied"]), \
             patch.object(processing, "_read_terminal_note", side_effect=lambda *_args, **_kwargs: saga["note"] if sheet_state["applied"] else ""):
            with ThreadPoolExecutor(max_workers=1) as executor:
                owner_a_future = executor.submit(
                    processing._execute_or_reconcile_terminal_sheet_mutation,
                    user_id,
                    thread_id,
                    sheets,
                    saga["sheetId"],
                    saga["tabTitle"],
                    header,
                    saga["notesColumnIndex"],
                    saga,
                    owner_a,
                    "move_with_note",
                )
                self.assertTrue(
                    provider_entered.wait(timeout=5),
                    "owner A did not persist request_started and reach provider",
                )
                persisted_attempt = copy.deepcopy(
                    current_root._data["terminalSheetMutationAttempt"]
                )
                current_root.update({
                    "terminalSagaClaim": {
                        **current_root._data["terminalSagaClaim"],
                        "leaseUntil": datetime.now(timezone.utc) - timedelta(seconds=1),
                    },
                })
                owner_b = processing._claim_existing_terminal_saga_execution(
                    user_id,
                    thread_id,
                    dict(current_root._data),
                    saga,
                )

                with self.assertRaisesRegex(
                    processing.RetryableProcessingError,
                    "operator review",
                ):
                    processing._execute_or_reconcile_terminal_sheet_mutation(
                        user_id,
                        thread_id,
                        sheets,
                        saga["sheetId"],
                        saga["tabTitle"],
                        header,
                        saga["notesColumnIndex"],
                        saga,
                        owner_b,
                        "move_with_note",
                    )
                self.assertEqual(1, sheet_state["mutationRequests"])

                allow_provider.set()
                with self.assertRaisesRegex(
                    processing.RetryableProcessingError,
                    "terminal saga execution ownership changed",
                ):
                    owner_a_future.result(timeout=5)

            reconciled_row = processing._execute_or_reconcile_terminal_sheet_mutation(
                user_id,
                thread_id,
                sheets,
                saga["sheetId"],
                saga["tabTitle"],
                header,
                saga["notesColumnIndex"],
                saga,
                owner_b,
                "move_with_note",
            )
            finalized = processing._finalize_terminal_thread_roots(
                user_id,
                saga["clientId"],
                thread_id,
                saga,
                final_row=reconciled_row,
                terminal_saga_owner=owner_b,
            )
            with self.assertRaisesRegex(
                processing.RetryableProcessingError,
                "terminal saga execution ownership changed",
            ):
                processing._finalize_terminal_thread_roots(
                    user_id,
                    saga["clientId"],
                    thread_id,
                    saga,
                    final_row=10,
                    terminal_saga_owner=owner_a,
                )

        self.assertEqual(1, sheet_state["mutationRequests"])
        final_attempt = current_root._data["terminalSheetMutationAttempt"]
        self.assertEqual(persisted_attempt["attemptId"], final_attempt["attemptId"])
        self.assertEqual(persisted_attempt["owner"], final_attempt["owner"])
        self.assertEqual("reconciled_applied", final_attempt["status"])
        self.assertEqual(owner_b.owner, final_attempt["reconciledByOwner"])
        self.assertEqual("finalized", finalized["phase"])
        self.assertEqual(processing.THREAD_STATUS["stopped"], current_root._data["status"])

    def test_definite_429_allows_one_linked_attempt_for_new_fenced_owner(self):
        thread_id = "thread-terminal-sheet-definite-429-lineage"
        current_root, firestore, finalized_saga, _pending_docs = (
            self._finalized_terminal_execution_fixture(thread_id)
        )
        saga = {**finalized_saga, "phase": "staged"}
        saga.pop("finalRow", None)
        current_root._data.update({
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "terminalSaga": saga,
            "terminalSagaKey": saga["sagaKey"],
            "terminalNotificationOwed": False,
            "terminalReplyOwed": False,
            "terminalSheetMutationAttempt": None,
            "terminalSheetMutationHistory": None,
            "terminalSheetMutationReview": None,
        })
        owner_a = processing.TerminalSagaExecution(
            owner=current_root._data["terminalSagaClaim"]["owner"],
            fencing_token=current_root._data["terminalSagaClaim"]["fencingToken"],
        )
        quota_response = MagicMock(status=429, reason="Too Many Requests")
        quota_error = HttpError(quota_response, b"rate limited before acceptance")
        provider_calls = []

        def quota_then_apply(*_args, **_kwargs):
            provider_calls.append(current_root._data["terminalSagaClaim"]["owner"])
            if len(provider_calls) == 1:
                raise quota_error
            return 10

        rowvals = [
            saga["rowAnchor"],
            "",
            "Ryan",
            saga["replyRecipient"],
            "4531",
            "10.00",
            "3.31",
            saga["note"],
        ]
        header = [
            "Property Address",
            "City",
            "Leasing Contact",
            "Email",
            "Total SF",
            "Rent/SF/Yr",
            "Ops Ex / SF",
            "Notes",
        ]
        sheets = MagicMock()

        with patch.object(processing, "_fs", firestore), \
             patch.object(processing, "_read_header_row2", return_value=header), \
             patch.object(processing, "_preview_nonviable_divider", return_value={"dividerRow": 10, "exists": True}), \
             patch.object(processing, "move_row_below_divider", side_effect=quota_then_apply), \
             patch.object(processing, "_find_row_by_anchor", return_value=(3, rowvals)), \
             patch.object(processing, "_is_row_below_nonviable", return_value=False), \
             patch.object(processing, "_read_terminal_note", return_value=""):
            with self.assertRaises(processing.RetryableProcessingError):
                processing._execute_or_reconcile_terminal_sheet_mutation(
                    "user-1",
                    thread_id,
                    sheets,
                    saga["sheetId"],
                    saga["tabTitle"],
                    header,
                    saga["notesColumnIndex"],
                    saga,
                    owner_a,
                    "move_with_note",
                )

            first_attempt = copy.deepcopy(
                current_root._data["terminalSheetMutationAttempt"]
            )
            self.assertEqual("definitely_not_applied", first_attempt["status"])
            self.assertEqual(429, first_attempt["providerStatusCode"])
            self.assertEqual(1, first_attempt["ordinal"])
            self.assertIsNone(first_attempt["previousAttemptId"])
            self.assertEqual(2, first_attempt["version"])
            self.assertNotEqual(
                first_attempt["attemptImmutableHash"],
                first_attempt["attemptHash"],
            )
            self.assertEqual(
                processing._terminal_sheet_attempt_full_hash(first_attempt),
                first_attempt["attemptHash"],
            )
            self.assertEqual(1, len(provider_calls))

            with self.assertRaisesRegex(
                processing.RetryableProcessingError,
                "new fenced owner",
            ):
                processing._begin_terminal_sheet_mutation_attempt(
                    "user-1",
                    saga,
                    owner_a,
                    "move_with_note",
                )
            self.assertEqual(
                first_attempt,
                current_root._data["terminalSheetMutationAttempt"],
            )
            self.assertFalse(current_root._data.get("terminalSheetMutationHistory"))

            current_root.update({
                "terminalSagaClaim": {
                    **current_root._data["terminalSagaClaim"],
                    "leaseUntil": datetime.now(timezone.utc) - timedelta(seconds=1),
                },
            })
            owner_b = processing._claim_existing_terminal_saga_execution(
                "user-1",
                thread_id,
                dict(current_root._data),
                saga,
            )
            final_row = processing._execute_or_reconcile_terminal_sheet_mutation(
                "user-1",
                thread_id,
                sheets,
                saga["sheetId"],
                saga["tabTitle"],
                header,
                saga["notesColumnIndex"],
                saga,
                owner_b,
                "move_with_note",
            )

        second_attempt = current_root._data["terminalSheetMutationAttempt"]
        history = current_root._data["terminalSheetMutationHistory"]
        self.assertEqual(10, final_row)
        self.assertEqual(2, len(provider_calls))
        self.assertEqual("applied", second_attempt["status"])
        self.assertEqual(2, second_attempt["ordinal"])
        self.assertEqual(first_attempt["attemptId"], second_attempt["previousAttemptId"])
        self.assertEqual(first_attempt["attemptHash"], second_attempt["previousAttemptHash"])
        self.assertNotEqual(
            first_attempt["attemptImmutableHash"],
            second_attempt["previousAttemptHash"],
        )
        self.assertEqual([first_attempt], history)

    def test_terminal_sheet_provider_429_must_be_exact_integer(self):
        current_root, firestore, saga, owner, _pending = (
            self._staged_terminal_sheet_fixture(
                "thread-terminal-sheet-non-integer-429"
            )
        )
        header = [
            "Property Address",
            "City",
            "Leasing Contact",
            "Email",
            "Total SF",
            "Rent/SF/Yr",
            "Ops Ex / SF",
            "Notes",
        ]
        response = MagicMock(status=429.0, reason="non-integer status")
        provider_error = HttpError(response, b"ambiguous non-integer status")
        provider_mutation = MagicMock(side_effect=provider_error)

        with patch.object(processing, "_fs", firestore), patch.object(
            processing,
            "_read_header_row2",
            return_value=header,
        ), patch.object(
            processing,
            "_preview_nonviable_divider",
            return_value={"dividerRow": 10, "exists": True},
        ), patch.object(
            processing,
            "move_row_below_divider",
            provider_mutation,
        ), patch.object(
            processing,
            "_read_terminal_sheet_mutation_effect",
            return_value=("absent", "exact effect is absent"),
        ), self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "operator review",
        ):
            processing._execute_or_reconcile_terminal_sheet_mutation(
                "user-1",
                saga["finalizationPlan"]["claimThreadId"],
                MagicMock(),
                saga["sheetId"],
                saga["tabTitle"],
                header,
                saga["notesColumnIndex"],
                saga,
                owner,
                "move_with_note",
            )

        attempt = current_root._data["terminalSheetMutationAttempt"]
        self.assertEqual("needs_operator_review", attempt["status"])
        self.assertNotIn("providerStatusCode", attempt)
        provider_mutation.assert_called_once()

    def test_terminal_sheet_active_v1_attempt_fails_closed_before_provider(self):
        current_root, firestore, saga, owner, _pending = (
            self._staged_terminal_sheet_fixture(
                "thread-terminal-sheet-active-v1"
            )
        )
        claim = current_root._data["terminalSagaClaim"]
        v1_attempt = self._test_terminal_sheet_attempt(
            saga,
            claim,
            "move_with_note",
        )
        v1_attempt["version"] = 1
        self._rehash_test_terminal_sheet_attempt(v1_attempt)
        current_root._data["terminalSheetMutationAttempt"] = v1_attempt
        provider_mutation = MagicMock(return_value=10)
        effect_readback = MagicMock(return_value=("applied", "forged"))

        with patch.object(processing, "_fs", firestore), patch.object(
            processing,
            "move_row_below_divider",
            provider_mutation,
        ), patch.object(
            processing,
            "_read_terminal_sheet_mutation_effect",
            effect_readback,
        ), self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "malformed terminal Sheet mutation attempt",
        ):
            processing._execute_or_reconcile_terminal_sheet_mutation(
                "user-1",
                saga["finalizationPlan"]["claimThreadId"],
                MagicMock(),
                saga["sheetId"],
                saga["tabTitle"],
                [],
                saga["notesColumnIndex"],
                saga,
                owner,
                "move_with_note",
            )

        self.assertEqual(
            v1_attempt,
            current_root._data["terminalSheetMutationAttempt"],
        )
        self.assertEqual(
            v1_attempt["attemptHash"],
            current_root._data["terminalSheetMutationReview"]["attemptHash"],
        )
        provider_mutation.assert_not_called()
        effect_readback.assert_not_called()

    def test_terminal_sheet_status_tamper_with_stale_full_hash_cannot_authorize_next_ordinal(self):
        thread_id = "thread-terminal-sheet-stale-full-hash"
        current_root, firestore, finalized_saga, _pending_docs = (
            self._finalized_terminal_execution_fixture(thread_id)
        )
        saga = {**finalized_saga, "phase": "staged"}
        saga.pop("finalRow", None)
        current_root._data.update({
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "terminalSaga": saga,
            "terminalSagaKey": saga["sagaKey"],
            "terminalNotificationOwed": False,
            "terminalReplyOwed": False,
            "terminalSheetMutationAttempt": None,
            "terminalSheetMutationHistory": [],
            "terminalSheetMutationReview": None,
        })
        owner_a = processing.TerminalSagaExecution(
            owner=current_root._data["terminalSagaClaim"]["owner"],
            fencing_token=current_root._data["terminalSagaClaim"]["fencingToken"],
        )
        header = [
            "Property Address",
            "City",
            "Leasing Contact",
            "Email",
            "Total SF",
            "Rent/SF/Yr",
            "Ops Ex / SF",
            "Notes",
        ]
        sheets = MagicMock()
        provider_header_read = MagicMock(return_value=header)
        provider_divider_preview = MagicMock(
            return_value={"dividerRow": 10, "exists": True}
        )
        provider_mutation = MagicMock(return_value=10)

        with patch.object(processing, "_fs", firestore), \
             patch.object(processing, "_read_header_row2", provider_header_read), \
             patch.object(
                 processing,
                 "_preview_nonviable_divider",
                 provider_divider_preview,
             ), \
             patch.object(processing, "move_row_below_divider", provider_mutation):
            request_started, created = (
                processing._begin_terminal_sheet_mutation_attempt(
                    "user-1",
                    saga,
                    owner_a,
                    "move_with_note",
                )
            )
            self.assertTrue(created)
            self.assertEqual("request_started", request_started["status"])
            self.assertEqual(1, request_started["ordinal"])
            stale_full_hash = request_started["attemptHash"]

            tampered_attempt = {
                **copy.deepcopy(request_started),
                "attemptHash": stale_full_hash,
                "status": "definitely_not_applied",
                "providerStatusCode": 429,
                "providerError": "forged provider rejection",
                "definitelyNotAppliedAt": datetime.now(timezone.utc),
                "operatorReviewRequired": False,
            }
            current_root.update({
                "terminalSheetMutationAttempt": tampered_attempt,
                "terminalSagaClaim": {
                    **current_root._data["terminalSagaClaim"],
                    "leaseUntil": datetime.now(timezone.utc) - timedelta(seconds=1),
                },
            })
            forged_attempt_snapshot = copy.deepcopy(
                current_root._data["terminalSheetMutationAttempt"]
            )
            owner_b = processing._claim_existing_terminal_saga_execution(
                "user-1",
                thread_id,
                dict(current_root._data),
                saga,
            )
            persisted_claim = current_root._data["terminalSagaClaim"]
            self.assertNotEqual(owner_a.owner, owner_b.owner)
            self.assertGreater(owner_b.fencing_token, owner_a.fencing_token)
            self.assertEqual(owner_b.owner, persisted_claim["owner"])
            self.assertEqual(
                owner_b.fencing_token,
                persisted_claim["fencingToken"],
            )

            rejection = None
            try:
                processing._execute_or_reconcile_terminal_sheet_mutation(
                    "user-1",
                    thread_id,
                    sheets,
                    saga["sheetId"],
                    saga["tabTitle"],
                    header,
                    saga["notesColumnIndex"],
                    saga,
                    owner_b,
                    "move_with_note",
                )
            except processing.RetryableProcessingError as exc:
                rejection = exc

        persisted_attempt = current_root._data["terminalSheetMutationAttempt"]
        self.assertEqual(1, persisted_attempt["ordinal"])
        self.assertEqual(forged_attempt_snapshot, persisted_attempt)
        self.assertEqual(request_started["attemptId"], persisted_attempt["attemptId"])
        self.assertEqual(stale_full_hash, persisted_attempt["attemptHash"])
        self.assertEqual("definitely_not_applied", persisted_attempt["status"])
        self.assertEqual(429, persisted_attempt["providerStatusCode"])
        self.assertEqual("forged provider rejection", persisted_attempt["providerError"])
        self.assertEqual(
            forged_attempt_snapshot["definitelyNotAppliedAt"],
            persisted_attempt["definitelyNotAppliedAt"],
        )
        self.assertIs(False, persisted_attempt["operatorReviewRequired"])
        self.assertEqual([], current_root._data["terminalSheetMutationHistory"])
        provider_header_read.assert_not_called()
        provider_divider_preview.assert_not_called()
        provider_mutation.assert_not_called()
        self.assertIsNotNone(rejection, "the stale full hash authorized a new attempt")
        self.assertRegex(
            str(rejection),
            "malformed terminal Sheet mutation attempt",
        )
        hash_rejection = rejection.__cause__
        self.assertIsInstance(
            hash_rejection,
            processing.RetryableProcessingError,
        )
        self.assertIn(
            "terminal Sheet mutation attempt",
            str(hash_rejection),
        )
        self.assertRegex(str(hash_rejection), r"(?i)(?:full[- ]?)?hash")
        review = current_root._data["terminalSheetMutationReview"]
        self.assertIsInstance(review, dict)
        self.assertEqual(request_started["attemptId"], review["attemptId"])
        self.assertEqual(stale_full_hash, review["attemptHash"])

    def test_terminal_sheet_v2_exact_status_schemas_reject_drift(self):
        current_root, _firestore, saga, _owner, _pending = (
            self._staged_terminal_sheet_fixture(
                "thread-terminal-sheet-v2-exact-schemas"
            )
        )
        claim = current_root._data["terminalSagaClaim"]
        statuses = (
            "request_started",
            "applied",
            "reconciled_applied",
            "needs_operator_review",
            "definitely_not_applied",
        )

        for status in statuses:
            with self.subTest(valid_status=status):
                attempt = self._test_terminal_sheet_attempt(
                    saga,
                    claim,
                    "move_with_note",
                    status=status,
                )
                self.assertEqual(
                    attempt,
                    processing._validate_terminal_sheet_mutation_attempt(
                        attempt,
                        saga,
                        mutation_kind="move_with_note",
                    ),
                )

        stale_hash_tampers = (
            ("request_started", "status", "applied"),
            ("applied", "providerCompletedAt", "forged"),
            ("reconciled_applied", "reconciliationEvidence", "forged"),
            ("needs_operator_review", "reviewReason", "forged"),
            ("definitely_not_applied", "providerError", "forged"),
        )
        for status, field, forged_value in stale_hash_tampers:
            with self.subTest(stale_full_hash_status=status, field=field):
                attempt = self._test_terminal_sheet_attempt(
                    saga,
                    claim,
                    "move_with_note",
                    status=status,
                )
                attempt[field] = forged_value
                with self.assertRaisesRegex(
                    processing.RetryableProcessingError,
                    "full hash|schema",
                ):
                    processing._validate_terminal_sheet_mutation_attempt(
                        attempt,
                        saga,
                        mutation_kind="move_with_note",
                    )

        invalid_attempts = []

        missing = self._test_terminal_sheet_attempt(
            saga,
            claim,
            "move_with_note",
            status="applied",
        )
        missing.pop("providerCompletedAt")
        invalid_attempts.append(("missing", self._rehash_test_terminal_sheet_attempt(missing)))

        extra = self._test_terminal_sheet_attempt(
            saga,
            claim,
            "move_with_note",
        )
        extra["unexpectedOutcome"] = "forged"
        invalid_attempts.append(("extra", self._rehash_test_terminal_sheet_attempt(extra)))

        cross_status = self._test_terminal_sheet_attempt(
            saga,
            claim,
            "move_with_note",
            status="applied",
        )
        cross_status["reviewReason"] = "belongs to needs_operator_review"
        invalid_attempts.append((
            "cross_status",
            self._rehash_test_terminal_sheet_attempt(cross_status),
        ))

        wrong_429_type = self._test_terminal_sheet_attempt(
            saga,
            claim,
            "move_with_note",
            status="definitely_not_applied",
        )
        wrong_429_type["providerStatusCode"] = 429.0
        invalid_attempts.append((
            "non_integer_429",
            self._rehash_test_terminal_sheet_attempt(wrong_429_type),
        ))

        wrong_fence_type = self._test_terminal_sheet_attempt(
            saga,
            claim,
            "move_with_note",
            status="needs_operator_review",
        )
        wrong_fence_type["reviewedByFencingToken"] = True
        invalid_attempts.append((
            "boolean_review_fence",
            self._rehash_test_terminal_sheet_attempt(wrong_fence_type),
        ))

        malformed_timestamp = self._test_terminal_sheet_attempt(
            saga,
            claim,
            "move_with_note",
            status="reconciled_applied",
        )
        malformed_timestamp["reconciledAt"] = "not-a-timestamp"
        invalid_attempts.append((
            "malformed_timestamp",
            self._rehash_test_terminal_sheet_attempt(malformed_timestamp),
        ))

        wrong_attempt_id_type = self._test_terminal_sheet_attempt(
            saga,
            claim,
            "move_with_note",
        )
        wrong_attempt_id_type["attemptId"] = 123
        invalid_attempts.append((
            "non_string_attempt_id",
            self._rehash_test_terminal_sheet_attempt(wrong_attempt_id_type),
        ))

        wrong_owner_type = self._test_terminal_sheet_attempt(
            saga,
            claim,
            "move_with_note",
        )
        wrong_owner_type["owner"] = 123
        invalid_attempts.append((
            "non_string_owner",
            self._rehash_test_terminal_sheet_attempt(wrong_owner_type),
        ))

        first_lineage_attempt = self._test_terminal_sheet_attempt(
            saga,
            claim,
            "move_with_note",
            status="definitely_not_applied",
        )
        wrong_previous_id_type = self._test_terminal_sheet_attempt(
            saga,
            {**claim, "owner": "terminal-owner-b", "fencingToken": 2},
            "move_with_note",
            ordinal=2,
            previous_attempt=first_lineage_attempt,
        )
        wrong_previous_id_type["previousAttemptId"] = 123
        invalid_attempts.append((
            "non_string_previous_attempt_id",
            self._rehash_test_terminal_sheet_attempt(
                wrong_previous_id_type
            ),
        ))

        boolean_timestamps = self._test_terminal_sheet_attempt(
            saga,
            claim,
            "move_with_note",
            status="definitely_not_applied",
        )
        boolean_timestamps.update({
            "requestStartedAt": True,
            "providerDeadline": 2,
            "definitelyNotAppliedAt": True,
        })
        invalid_attempts.append((
            "boolean_numeric_timestamps",
            self._rehash_test_terminal_sheet_attempt(boolean_timestamps),
        ))

        string_timestamps = self._test_terminal_sheet_attempt(
            saga,
            claim,
            "move_with_note",
            status="applied",
        )
        string_timestamps.update({
            "requestStartedAt": "2026-08-02T00:00:00Z",
            "providerDeadline": "2026-08-02T00:01:00Z",
            "providerCompletedAt": "2026-08-02T00:00:01Z",
        })
        invalid_attempts.append((
            "string_timestamps",
            self._rehash_test_terminal_sheet_attempt(string_timestamps),
        ))

        float_version = self._test_terminal_sheet_attempt(
            saga,
            claim,
            "move_with_note",
        )
        float_version["version"] = 2.0
        invalid_attempts.append((
            "float_version",
            self._rehash_test_terminal_sheet_attempt(float_version),
        ))

        float_source_row = self._test_terminal_sheet_attempt(
            saga,
            claim,
            "move_with_note",
        )
        float_source_row["sourceRow"] = float(float_source_row["sourceRow"])
        invalid_attempts.append((
            "float_source_row",
            self._rehash_test_terminal_sheet_attempt(float_source_row),
        ))

        float_final_row = self._test_terminal_sheet_attempt(
            saga,
            claim,
            "move_with_note",
        )
        float_final_row["finalRow"] = float(float_final_row["finalRow"])
        invalid_attempts.append((
            "float_final_row",
            self._rehash_test_terminal_sheet_attempt(float_final_row),
        ))

        float_applied_fence = self._test_terminal_sheet_attempt(
            saga,
            claim,
            "move_with_note",
            status="applied",
        )
        float_applied_fence["appliedByFencingToken"] = float(
            float_applied_fence["appliedByFencingToken"]
        )
        invalid_attempts.append((
            "float_applied_fence",
            self._rehash_test_terminal_sheet_attempt(float_applied_fence),
        ))

        for failure_kind, attempt in invalid_attempts:
            with self.subTest(invalid=failure_kind), self.assertRaisesRegex(
                processing.RetryableProcessingError,
                "schema|field|malformed|429",
            ):
                processing._validate_terminal_sheet_mutation_attempt(
                    attempt,
                    saga,
                    mutation_kind="move_with_note",
                )

    def test_terminal_sheet_legal_transition_matrix_and_exact_replay(self):
        statuses = (
            "request_started",
            "applied",
            "reconciled_applied",
            "needs_operator_review",
            "definitely_not_applied",
        )
        legal_cross_transitions = {
            ("request_started", "applied"),
            ("request_started", "reconciled_applied"),
            ("request_started", "needs_operator_review"),
            ("request_started", "definitely_not_applied"),
            ("needs_operator_review", "reconciled_applied"),
        }

        for current_status in statuses:
            for target_status in statuses:
                thread_id = (
                    "thread-sheet-transition-"
                    f"{current_status}-to-{target_status}"
                )
                current_root, firestore, saga, owner, _pending = (
                    self._staged_terminal_sheet_fixture(thread_id)
                )
                claim = current_root._data["terminalSagaClaim"]
                current = self._test_terminal_sheet_attempt(
                    saga,
                    claim,
                    "move_with_note",
                    status=current_status,
                )
                current_root._data["terminalSheetMutationAttempt"] = current
                current_root._data["terminalSheetMutationReview"] = (
                    self._test_terminal_sheet_review(saga, current)
                    if current_status == "needs_operator_review"
                    else None
                )
                if target_status == current_status:
                    outcome_fields = {
                        key: value
                        for key, value in current.items()
                        if key not in {
                            *processing._TERMINAL_SHEET_ATTEMPT_IMMUTABLE_FIELDS,
                            "attemptImmutableHash",
                            "attemptHash",
                            "status",
                        }
                    }
                else:
                    outcome_fields = self._test_terminal_sheet_outcome_fields(
                        saga,
                        claim,
                        "move_with_note",
                        target_status,
                        now=current["requestStartedAt"],
                    )
                write_count_before = len(firestore.transaction_write_counts)
                review_before = copy.deepcopy(
                    current_root._data.get("terminalSheetMutationReview")
                )

                is_legal = (
                    current_status == target_status
                    or (current_status, target_status)
                    in legal_cross_transitions
                )
                with self.subTest(
                    current=current_status,
                    target=target_status,
                ), patch.object(processing, "_fs", firestore):
                    if not is_legal:
                        with self.assertRaisesRegex(
                            processing.RetryableProcessingError,
                            "transition|terminal|rewrite",
                        ):
                            processing._record_terminal_sheet_mutation_state(
                                "user-1",
                                saga,
                                owner,
                                current,
                                target_status,
                                **outcome_fields,
                            )
                        self.assertEqual(
                            current,
                            current_root._data["terminalSheetMutationAttempt"],
                        )
                        continue

                    updated = processing._record_terminal_sheet_mutation_state(
                        "user-1",
                        saga,
                        owner,
                        current,
                        target_status,
                        **outcome_fields,
                    )
                    if current_status == target_status:
                        self.assertEqual(current, updated)
                        # The supported Firestore runner closes this
                        # read/validate no-op with an empty commit. It must
                        # never enqueue a document write.
                        self.assertEqual(
                            write_count_before + 1,
                            len(firestore.transaction_write_counts),
                        )
                        self.assertEqual(
                            0,
                            firestore.transaction_write_counts[-1],
                        )
                        self.assertEqual(
                            review_before,
                            current_root._data.get(
                                "terminalSheetMutationReview"
                            ),
                        )
                    else:
                        self.assertEqual(target_status, updated["status"])
                        self.assertNotEqual(
                            current["attemptHash"],
                            updated["attemptHash"],
                        )
                        expected_shape = self._test_terminal_sheet_attempt(
                            saga,
                            claim,
                            "move_with_note",
                            status=target_status,
                            now=current["requestStartedAt"],
                        )
                        self.assertEqual(set(expected_shape), set(updated))
                        if target_status == "needs_operator_review":
                            review = current_root._data[
                                "terminalSheetMutationReview"
                            ]
                            self.assertEqual(
                                updated["attemptHash"],
                                review["attemptHash"],
                            )
                        else:
                            self.assertIsNone(
                                current_root._data.get(
                                    "terminalSheetMutationReview"
                                )
                            )

    def test_terminal_sheet_history_and_tombstones_require_exact_v2_lineage(self):
        current_root, _firestore, saga, _owner, _pending = (
            self._staged_terminal_sheet_fixture(
                "thread-terminal-sheet-v2-lineage"
            )
        )
        claim_a = current_root._data["terminalSagaClaim"]
        first = self._test_terminal_sheet_attempt(
            saga,
            claim_a,
            "move_with_note",
            status="definitely_not_applied",
        )
        claim_b = {**claim_a, "owner": "terminal-owner-b", "fencingToken": 2}
        second = self._test_terminal_sheet_attempt(
            saga,
            claim_b,
            "move_with_note",
            status="applied",
            ordinal=2,
            previous_attempt=first,
        )
        self.assertEqual(
            [first],
            processing._validate_terminal_sheet_mutation_history(
                [first],
                saga,
                mutation_kind="move_with_note",
                active_attempt=second,
            ),
        )
        self.assertEqual(first["attemptHash"], second["previousAttemptHash"])
        self.assertNotEqual(
            first["attemptImmutableHash"],
            first["attemptHash"],
        )

        invalid_lineages = []
        duplicate_id = copy.deepcopy(second)
        duplicate_id["attemptId"] = first["attemptId"]
        invalid_lineages.append((
            "duplicate_attempt_id",
            [first],
            self._rehash_test_terminal_sheet_attempt(duplicate_id),
        ))

        same_owner = self._test_terminal_sheet_attempt(
            saga,
            {**claim_a, "fencingToken": 2},
            "move_with_note",
            status="applied",
            ordinal=2,
            previous_attempt=first,
        )
        invalid_lineages.append(("same_owner", [first], same_owner))

        nonincreasing_fence = self._test_terminal_sheet_attempt(
            saga,
            {**claim_b, "fencingToken": 1},
            "move_with_note",
            status="applied",
            ordinal=2,
            previous_attempt=first,
        )
        invalid_lineages.append((
            "nonincreasing_fence",
            [first],
            nonincreasing_fence,
        ))

        stale_previous_full_hash = copy.deepcopy(second)
        stale_previous_full_hash["previousAttemptHash"] = first[
            "attemptImmutableHash"
        ]
        invalid_lineages.append((
            "immutable_hash_used_as_lineage",
            [first],
            self._rehash_test_terminal_sheet_attempt(
                stale_previous_full_hash
            ),
        ))

        v1_history = copy.deepcopy(first)
        v1_history["version"] = 1
        invalid_lineages.append((
            "v1_history",
            [self._rehash_test_terminal_sheet_attempt(v1_history)],
            second,
        ))

        tampered_history = copy.deepcopy(first)
        tampered_history["providerError"] = "tampered with stale full hash"
        invalid_lineages.append((
            "tampered_history",
            [tampered_history],
            second,
        ))

        for failure_kind, history, active in invalid_lineages:
            with self.subTest(lineage=failure_kind), self.assertRaises(
                processing.RetryableProcessingError
            ):
                processing._validate_terminal_sheet_mutation_history(
                    history,
                    saga,
                    mutation_kind="move_with_note",
                    active_attempt=active,
                )

        projection = self._archived_terminal_projection_for_test(saga, 1)
        nested_v1 = copy.deepcopy(projection)
        nested_v1["sheetMutationAttempt"]["version"] = 1
        self._rehash_test_terminal_sheet_attempt(
            nested_v1["sheetMutationAttempt"]
        )
        nested_v1.pop("projectionHash")
        nested_v1["projectionHash"] = hashlib.sha256(
            json.dumps(nested_v1, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        with self.assertRaises(processing.RetryableProcessingError):
            processing._validate_terminal_settlement_projection(nested_v1)

        nested_tamper = copy.deepcopy(projection)
        nested_tamper["sheetMutationAttempt"][
            "providerCompletedAt"
        ] = "tampered with stale full hash"
        nested_tamper.pop("projectionHash")
        nested_tamper["projectionHash"] = hashlib.sha256(
            json.dumps(
                nested_tamper,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        with self.assertRaises(processing.RetryableProcessingError):
            processing._validate_terminal_settlement_projection(nested_tamper)

    def test_terminal_sheet_settlement_rejects_rehashed_mutation_kind_flip(self):
        _current_root, _firestore, saga, _owner, _pending = (
            self._staged_terminal_sheet_fixture(
                "thread-terminal-sheet-settlement-kind-flip"
            )
        )
        projection = self._archived_terminal_projection_for_test(saga, 1)
        forged_projection = copy.deepcopy(projection)
        forged_attempt = forged_projection["sheetMutationAttempt"]
        self.assertEqual("move_with_note", forged_attempt["mutationKind"])
        forged_attempt["mutationKind"] = "ensure_note"
        self._rehash_test_terminal_sheet_attempt(forged_attempt)
        forged_projection.pop("projectionHash")
        forged_projection["projectionHash"] = hashlib.sha256(
            json.dumps(
                forged_projection,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()

        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "kind|geometry|drifted",
        ):
            processing._validate_terminal_settlement_projection(
                forged_projection
            )

    def test_terminal_sheet_applied_states_are_execution_idempotent(self):
        for status in ("applied", "reconciled_applied"):
            with self.subTest(status=status):
                current_root, firestore, saga, owner, _pending = (
                    self._staged_terminal_sheet_fixture(
                        f"thread-terminal-sheet-idempotent-{status}"
                    )
                )
                claim = current_root._data["terminalSagaClaim"]
                attempt = self._test_terminal_sheet_attempt(
                    saga,
                    claim,
                    "move_with_note",
                    status=status,
                )
                current_root._data["terminalSheetMutationAttempt"] = attempt
                readback = MagicMock(
                    return_value=(
                        "applied",
                        "exact final row and terminal note are present",
                    )
                )
                provider_mutation = MagicMock(return_value=10)

                with patch.object(processing, "_fs", firestore), patch.object(
                    processing,
                    "_read_terminal_sheet_mutation_effect",
                    readback,
                ), patch.object(
                    processing,
                    "move_row_below_divider",
                    provider_mutation,
                ):
                    final_row = (
                        processing._execute_or_reconcile_terminal_sheet_mutation(
                            "user-1",
                            saga["finalizationPlan"]["claimThreadId"],
                            MagicMock(),
                            saga["sheetId"],
                            saga["tabTitle"],
                            [],
                            saga["notesColumnIndex"],
                            saga,
                            owner,
                            "move_with_note",
                        )
                    )

                self.assertEqual(
                    saga["finalizationPlan"]["finalRow"],
                    final_row,
                )
                self.assertEqual(
                    attempt,
                    current_root._data["terminalSheetMutationAttempt"],
                )
                readback.assert_not_called()
                provider_mutation.assert_not_called()

    def test_finalized_saga_missing_sheet_attempt_fails_closed_without_reconstruction(self):
        current_root, firestore, saga, _pending = (
            self._finalized_terminal_execution_fixture(
                "thread-finalized-missing-sheet-attempt"
            )
        )
        owner = processing.TerminalSagaExecution(
            current_root._data["terminalSagaClaim"]["owner"],
            current_root._data["terminalSagaClaim"]["fencingToken"],
        )
        evidence_fields = (
            "terminalSheetMutationAttempt",
            "terminalSheetMutationHistory",
            "terminalSheetMutationReview",
        )
        for field in evidence_fields:
            current_root._data.pop(field, None)
        applied_readback = MagicMock(
            return_value=(
                "applied",
                "exact final row and terminal note are present",
            )
        )
        move_row = MagicMock(return_value=saga["finalRow"])
        move_with_divider = MagicMock(return_value=saga["finalRow"])
        ensure_note = MagicMock()

        with patch.object(processing, "_fs", firestore), patch.object(
            processing,
            "_read_terminal_sheet_mutation_effect",
            applied_readback,
        ), patch.object(
            processing,
            "move_row_below_divider",
            move_row,
        ), patch.object(
            processing,
            "move_row_below_new_divider_atomic",
            move_with_divider,
        ), patch.object(
            processing,
            "_ensure_terminal_note",
            ensure_note,
        ), self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "missing|reconstruct|creation|finalized",
        ):
            processing._execute_or_reconcile_terminal_sheet_mutation(
                "user-1",
                saga["finalizationPlan"]["claimThreadId"],
                MagicMock(),
                saga["sheetId"],
                saga["tabTitle"],
                [],
                saga["notesColumnIndex"],
                saga,
                owner,
                "move_with_note",
                allow_provider_mutation=False,
            )

        self.assertEqual([], firestore.transaction_write_counts)
        for field in evidence_fields:
            self.assertNotIn(field, current_root._data)
        applied_readback.assert_not_called()
        move_row.assert_not_called()
        move_with_divider.assert_not_called()
        ensure_note.assert_not_called()

    def test_terminal_sheet_begin_commit_readback_requires_exact_state(self):
        for outcome in ("apply_then_raise", "no_apply", "fabricated"):
            with self.subTest(outcome=outcome):
                current_root, firestore, saga, owner, _pending = (
                    self._staged_terminal_sheet_fixture(
                        f"thread-terminal-sheet-begin-readback-{outcome}"
                    )
                )

                def fabricate_committed_state():
                    fabricated = copy.deepcopy(
                        current_root._data["terminalSheetMutationAttempt"]
                    )
                    fabricated.update({
                        "status": "applied",
                        "appliedByOwner": fabricated["owner"],
                        "appliedByFencingToken": fabricated["fencingToken"],
                        "providerCompletedAt": (
                            fabricated["requestStartedAt"]
                            + timedelta(seconds=1)
                        ),
                        "operatorReviewRequired": False,
                    })
                    current_root._data["terminalSheetMutationAttempt"] = (
                        self._rehash_test_terminal_sheet_attempt(fabricated)
                    )

                firestore.transaction_commit_behaviors_by_field = {
                    "terminalSheetMutationAttempt": [{
                        "error": RuntimeError(
                            f"ambiguous begin commit: {outcome}"
                        ),
                        "applyBeforeError": outcome != "no_apply",
                        "afterApply": (
                            fabricate_committed_state
                            if outcome == "fabricated"
                            else None
                        ),
                    }],
                }
                with patch.object(processing, "_fs", firestore):
                    if outcome == "apply_then_raise":
                        attempt, created = (
                            processing._begin_terminal_sheet_mutation_attempt(
                                "user-1",
                                saga,
                                owner,
                                "move_with_note",
                            )
                        )
                        self.assertTrue(created)
                        self.assertEqual("request_started", attempt["status"])
                        self.assertEqual(
                            attempt,
                            current_root._data[
                                "terminalSheetMutationAttempt"
                            ],
                        )
                    else:
                        with self.assertRaisesRegex(
                            processing.RetryableProcessingError,
                            "intent persistence failed",
                        ):
                            processing._begin_terminal_sheet_mutation_attempt(
                                "user-1",
                                saga,
                                owner,
                                "move_with_note",
                            )

    def test_terminal_sheet_wrapper_no_apply_begin_retry_is_not_poisoned(self):
        current_root, firestore, saga, owner, _pending = (
            self._staged_terminal_sheet_fixture(
                "thread-terminal-sheet-wrapper-no-apply"
            )
        )
        header = [
            "Property Address",
            "City",
            "Leasing Contact",
            "Email",
            "Total SF",
            "Rent/SF/Yr",
            "Ops Ex / SF",
            "Notes",
        ]
        firestore.transaction_commit_behaviors_by_field = {
            "terminalSheetMutationAttempt": [{
                "error": RuntimeError("ambiguous begin commit without apply"),
                "applyBeforeError": False,
            }],
        }
        provider_mutation = MagicMock(
            return_value=saga["finalizationPlan"]["finalRow"]
        )

        with patch.object(processing, "_fs", firestore), patch.object(
            processing,
            "_read_header_row2",
            return_value=header,
        ), patch.object(
            processing,
            "_preview_nonviable_divider",
            return_value={
                "dividerRow": saga["finalizationPlan"]["dividerRow"],
                "exists": True,
            },
        ), patch.object(
            processing,
            "move_row_below_divider",
            provider_mutation,
        ):
            with self.assertRaisesRegex(
                processing.RetryableProcessingError,
                "intent persistence failed",
            ):
                processing._execute_or_reconcile_terminal_sheet_mutation(
                    "user-1",
                    saga["finalizationPlan"]["claimThreadId"],
                    MagicMock(),
                    saga["sheetId"],
                    saga["tabTitle"],
                    header,
                    saga["notesColumnIndex"],
                    saga,
                    owner,
                    "move_with_note",
                )
            state_after_no_apply = copy.deepcopy(current_root._data)
            second_error = None
            final_row = None
            try:
                final_row = (
                    processing._execute_or_reconcile_terminal_sheet_mutation(
                        "user-1",
                        saga["finalizationPlan"]["claimThreadId"],
                        MagicMock(),
                        saga["sheetId"],
                        saga["tabTitle"],
                        header,
                        saga["notesColumnIndex"],
                        saga,
                        owner,
                        "move_with_note",
                    )
                )
            except Exception as exc:
                second_error = exc

        with self.subTest(boundary="first no-apply remains empty"):
            self.assertIsNone(
                state_after_no_apply.get("terminalSheetMutationAttempt")
            )
            self.assertEqual(
                [],
                state_after_no_apply.get("terminalSheetMutationHistory"),
            )
            self.assertIsNone(
                state_after_no_apply.get("terminalSheetMutationReview")
            )
        with self.subTest(boundary="second invocation is not poisoned"):
            self.assertIsNone(second_error)
            self.assertEqual(saga["finalizationPlan"]["finalRow"], final_row)
            self.assertEqual(
                "applied",
                current_root._data["terminalSheetMutationAttempt"]["status"],
            )
            self.assertIsNone(
                current_root._data.get("terminalSheetMutationReview")
            )
            provider_mutation.assert_called_once()

    def test_terminal_sheet_geometry_drift_fails_before_attempt_or_provider(self):
        header = [
            "Property Address",
            "City",
            "Leasing Contact",
            "Email",
            "Total SF",
            "Rent/SF/Yr",
            "Ops Ex / SF",
            "Notes",
        ]
        cases = (
            ("wrong-caller-kind", "ensure_note", False),
            ("staged-final-row", "move_with_note", True),
        )
        for suffix, caller_kind, add_staged_final_row in cases:
            with self.subTest(case=suffix):
                current_root, firestore, saga, owner, _pending = (
                    self._staged_terminal_sheet_fixture(
                        f"thread-terminal-sheet-geometry-{suffix}"
                    )
                )
                if add_staged_final_row:
                    saga["finalRow"] = saga["sourceRow"]
                ensure_note = MagicMock()
                move_row = MagicMock(
                    return_value=saga["finalizationPlan"]["finalRow"]
                )
                rejection = None

                with patch.object(processing, "_fs", firestore), patch.object(
                    processing,
                    "_read_header_row2",
                    return_value=header,
                ), patch.object(
                    processing,
                    "_preview_nonviable_divider",
                    return_value={
                        "dividerRow": saga["finalizationPlan"]["dividerRow"],
                        "exists": True,
                    },
                ), patch.object(
                    processing,
                    "_ensure_terminal_note",
                    ensure_note,
                ), patch.object(
                    processing,
                    "move_row_below_divider",
                    move_row,
                ), patch.object(
                    processing,
                    "_read_terminal_sheet_mutation_effect",
                    return_value=("absent", "exact effect is absent"),
                ):
                    try:
                        processing._execute_or_reconcile_terminal_sheet_mutation(
                            "user-1",
                            saga["finalizationPlan"]["claimThreadId"],
                            MagicMock(),
                            saga["sheetId"],
                            saga["tabTitle"],
                            header,
                            saga["notesColumnIndex"],
                            saga,
                            owner,
                            caller_kind,
                        )
                    except processing.RetryableProcessingError as exc:
                        rejection = exc

                self.assertIsNotNone(rejection)
                self.assertRegex(
                    str(rejection),
                    "kind|geometry|phase|finalRow|final row",
                )
                self.assertIsNone(
                    current_root._data.get("terminalSheetMutationAttempt")
                )
                self.assertEqual(
                    [],
                    current_root._data.get("terminalSheetMutationHistory"),
                )
                self.assertIsNone(
                    current_root._data.get("terminalSheetMutationReview")
                )
                ensure_note.assert_not_called()
                move_row.assert_not_called()

    def test_terminal_sheet_outcome_commit_readback_requires_exact_state(self):
        for outcome in ("apply_then_raise", "no_apply", "fabricated"):
            with self.subTest(outcome=outcome):
                current_root, firestore, saga, owner, _pending = (
                    self._staged_terminal_sheet_fixture(
                        f"thread-terminal-sheet-outcome-readback-{outcome}"
                    )
                )
                with patch.object(processing, "_fs", firestore):
                    attempt, _created = (
                        processing._begin_terminal_sheet_mutation_attempt(
                            "user-1",
                            saga,
                            owner,
                            "move_with_note",
                        )
                    )
                outcome_fields = self._test_terminal_sheet_outcome_fields(
                    saga,
                    current_root._data["terminalSagaClaim"],
                    "move_with_note",
                    "applied",
                    now=attempt["requestStartedAt"],
                )

                def fabricate_committed_state():
                    fabricated = copy.deepcopy(
                        current_root._data["terminalSheetMutationAttempt"]
                    )
                    fabricated["providerCompletedAt"] = (
                        fabricated["providerCompletedAt"]
                        + timedelta(seconds=1)
                    )
                    current_root._data["terminalSheetMutationAttempt"] = (
                        self._rehash_test_terminal_sheet_attempt(fabricated)
                    )

                firestore.transaction_commit_behaviors_by_field = {
                    "terminalSheetMutationAttempt": [{
                        "error": RuntimeError(
                            f"ambiguous outcome commit: {outcome}"
                        ),
                        "applyBeforeError": outcome != "no_apply",
                        "afterApply": (
                            fabricate_committed_state
                            if outcome == "fabricated"
                            else None
                        ),
                    }],
                }
                with patch.object(processing, "_fs", firestore):
                    if outcome == "apply_then_raise":
                        applied = processing._record_terminal_sheet_mutation_state(
                            "user-1",
                            saga,
                            owner,
                            attempt,
                            "applied",
                            **outcome_fields,
                        )
                        self.assertEqual(
                            applied,
                            current_root._data[
                                "terminalSheetMutationAttempt"
                            ],
                        )
                    else:
                        with self.assertRaisesRegex(
                            processing.RetryableProcessingError,
                            "outcome persistence failed",
                        ):
                            processing._record_terminal_sheet_mutation_state(
                                "user-1",
                                saga,
                                owner,
                                attempt,
                                "applied",
                                **outcome_fields,
                            )

    def test_terminal_sheet_review_hash_rotates_once_and_replay_is_noop(self):
        current_root, firestore, saga, owner, _pending = (
            self._staged_terminal_sheet_fixture(
                "thread-terminal-sheet-review-rotation"
            )
        )
        with patch.object(processing, "_fs", firestore):
            request_started, _created = (
                processing._begin_terminal_sheet_mutation_attempt(
                    "user-1",
                    saga,
                    owner,
                    "move_with_note",
                )
            )
            review_fields = {
                "operatorReviewRequired": True,
                "reviewReason": "persisted Sheet effect was absent",
                "reviewEvidence": "absent",
                "providerError": None,
                "reviewedByOwner": owner.owner,
                "reviewedByFencingToken": owner.fencing_token,
                "reviewedAt": (
                    request_started["requestStartedAt"]
                    + timedelta(seconds=1)
                ),
            }
            needs_review = processing._record_terminal_sheet_mutation_state(
                "user-1",
                saga,
                owner,
                request_started,
                "needs_operator_review",
                **review_fields,
            )
            first_review = copy.deepcopy(
                current_root._data["terminalSheetMutationReview"]
            )
            self.assertNotEqual(
                request_started["attemptHash"],
                needs_review["attemptHash"],
            )
            self.assertEqual(
                needs_review["attemptHash"],
                first_review["attemptHash"],
            )

            write_count_before_replay = len(
                firestore.transaction_write_counts
            )
            replayed = processing._record_terminal_sheet_mutation_state(
                "user-1",
                saga,
                owner,
                needs_review,
                "needs_operator_review",
                **review_fields,
            )
            self.assertEqual(needs_review, replayed)
            # The supported Firestore runner closes this read/validate no-op
            # with an empty commit; the persisted attempt/review stay exact.
            self.assertEqual(
                write_count_before_replay + 1,
                len(firestore.transaction_write_counts),
            )
            self.assertEqual(
                0,
                firestore.transaction_write_counts[-1],
            )
            self.assertEqual(
                first_review,
                current_root._data["terminalSheetMutationReview"],
            )

            reconciliation_fields = {
                "reconciledByOwner": owner.owner,
                "reconciledByFencingToken": owner.fencing_token,
                "reconciledAt": (
                    request_started["requestStartedAt"]
                    + timedelta(seconds=2)
                ),
                "reconciliationEvidence": (
                    "exact final row and terminal note are present"
                ),
                "operatorReviewRequired": False,
            }
            reconciled = processing._record_terminal_sheet_mutation_state(
                "user-1",
                saga,
                owner,
                needs_review,
                "reconciled_applied",
                **reconciliation_fields,
            )
        self.assertNotEqual(
            needs_review["attemptHash"],
            reconciled["attemptHash"],
        )
        self.assertIsNone(
            current_root._data["terminalSheetMutationReview"]
        )
        self.assertFalse(
            {
                "reviewReason",
                "reviewEvidence",
                "reviewedAt",
                "reviewedByOwner",
                "reviewedByFencingToken",
                "providerError",
            }
            & set(reconciled)
        )

    def test_terminal_finalization_rejects_v1_nested_sheet_history(self):
        current_root, firestore, saga, _owner, _pending = (
            self._staged_terminal_sheet_fixture(
                "thread-terminal-finalize-v1-sheet-history"
            )
        )
        claim_a = current_root._data["terminalSagaClaim"]
        first = self._test_terminal_sheet_attempt(
            saga,
            claim_a,
            "move_with_note",
            status="definitely_not_applied",
        )
        first["version"] = 1
        self._rehash_test_terminal_sheet_attempt(first)
        claim_b = {
            **claim_a,
            "owner": "terminal-owner-b",
            "fencingToken": claim_a["fencingToken"] + 1,
        }
        active = self._test_terminal_sheet_attempt(
            saga,
            claim_b,
            "move_with_note",
            status="applied",
            ordinal=2,
            previous_attempt=first,
        )
        current_root._data.update({
            "terminalSagaClaim": claim_b,
            "terminalSagaFence": claim_b["fencingToken"],
            "terminalSheetMutationAttempt": active,
            "terminalSheetMutationHistory": [first],
            "terminalSheetMutationReview": None,
        })
        owner_b = processing.TerminalSagaExecution(
            claim_b["owner"],
            claim_b["fencingToken"],
        )

        with patch.object(processing, "_fs", firestore), self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "Sheet mutation|history|version|drifted",
        ):
            processing._finalize_terminal_thread_roots(
                "user-1",
                saga["clientId"],
                saga["finalizationPlan"]["claimThreadId"],
                saga,
                final_row=saga["finalizationPlan"]["finalRow"],
                terminal_saga_owner=owner_b,
            )

        self.assertEqual(
            processing.THREAD_STATUS["active"],
            current_root._data["status"],
        )

    def test_terminal_finalization_rejects_rehashed_mutation_kind_flip(self):
        current_root, firestore, saga, owner, _pending = (
            self._staged_terminal_sheet_fixture(
                "thread-terminal-finalize-kind-flip"
            )
        )
        claim = current_root._data["terminalSagaClaim"]
        forged_attempt = self._test_terminal_sheet_attempt(
            saga,
            claim,
            "move_with_note",
            status="applied",
        )
        forged_attempt["mutationKind"] = "ensure_note"
        self._rehash_test_terminal_sheet_attempt(forged_attempt)
        current_root._data["terminalSheetMutationAttempt"] = forged_attempt

        with patch.object(processing, "_fs", firestore), self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "kind|geometry|drifted",
        ):
            processing._finalize_terminal_thread_roots(
                "user-1",
                saga["clientId"],
                saga["finalizationPlan"]["claimThreadId"],
                saga,
                final_row=saga["finalizationPlan"]["finalRow"],
                terminal_saga_owner=owner,
            )

        self.assertEqual([], firestore.transaction_write_counts)
        self.assertEqual(
            processing.THREAD_STATUS["active"],
            current_root._data["status"],
        )

    def test_ambiguous_timeout_never_creates_second_sheet_attempt(self):
        thread_id = "thread-terminal-sheet-ambiguous-timeout-no-lineage"
        current_root, firestore, finalized_saga, _pending_docs = (
            self._finalized_terminal_execution_fixture(thread_id)
        )
        saga = {**finalized_saga, "phase": "staged"}
        saga.pop("finalRow", None)
        current_root._data.update({
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "terminalSaga": saga,
            "terminalSagaKey": saga["sagaKey"],
            "terminalNotificationOwed": False,
            "terminalReplyOwed": False,
            "terminalSheetMutationAttempt": None,
            "terminalSheetMutationHistory": None,
            "terminalSheetMutationReview": None,
        })
        owner_a = processing.TerminalSagaExecution(
            owner=current_root._data["terminalSagaClaim"]["owner"],
            fencing_token=current_root._data["terminalSagaClaim"]["fencingToken"],
        )
        rowvals = [
            saga["rowAnchor"],
            "",
            "Ryan",
            saga["replyRecipient"],
            "4531",
            "10.00",
            "3.31",
            "",
        ]
        header = [
            "Property Address",
            "City",
            "Leasing Contact",
            "Email",
            "Total SF",
            "Rent/SF/Yr",
            "Ops Ex / SF",
            "Notes",
        ]
        move = MagicMock(side_effect=TimeoutError("provider outcome unknown"))
        sheets = MagicMock()

        with patch.object(processing, "_fs", firestore), \
             patch.object(processing, "_read_header_row2", return_value=header), \
             patch.object(processing, "_preview_nonviable_divider", return_value={"dividerRow": 10, "exists": True}), \
             patch.object(processing, "move_row_below_divider", side_effect=move), \
             patch.object(processing, "_find_row_by_anchor", return_value=(3, rowvals)), \
             patch.object(processing, "_is_row_below_nonviable", return_value=False), \
             patch.object(processing, "_read_terminal_note", return_value=""):
            with self.assertRaises(processing.RetryableProcessingError):
                processing._execute_or_reconcile_terminal_sheet_mutation(
                    "user-1", thread_id, sheets, saga["sheetId"], saga["tabTitle"],
                    header, saga["notesColumnIndex"], saga, owner_a, "move_with_note",
                )
            first_attempt = copy.deepcopy(
                current_root._data["terminalSheetMutationAttempt"]
            )
            current_root.update({
                "terminalSagaClaim": {
                    **current_root._data["terminalSagaClaim"],
                    "leaseUntil": datetime.now(timezone.utc) - timedelta(seconds=1),
                },
            })
            owner_b = processing._claim_existing_terminal_saga_execution(
                "user-1", thread_id, dict(current_root._data), saga,
            )
            with self.assertRaisesRegex(
                processing.RetryableProcessingError,
                "operator review",
            ):
                processing._execute_or_reconcile_terminal_sheet_mutation(
                    "user-1", thread_id, sheets, saga["sheetId"], saga["tabTitle"],
                    header, saga["notesColumnIndex"], saga, owner_b, "move_with_note",
                )

        self.assertEqual(1, move.call_count)
        self.assertEqual(first_attempt["attemptId"], current_root._data["terminalSheetMutationAttempt"]["attemptId"])
        self.assertEqual(1, current_root._data["terminalSheetMutationAttempt"]["ordinal"])
        self.assertEqual("needs_operator_review", current_root._data["terminalSheetMutationAttempt"]["status"])
        self.assertFalse(current_root._data.get("terminalSheetMutationHistory"))

    def test_stale_terminal_owner_cannot_queue_outcome_or_cleanup_newer_claim(self):
        thread_id = "thread-terminal-stale-owner-queue-cleanup"
        current_root, firestore, saga, pending_docs = (
            self._finalized_terminal_execution_fixture(
                thread_id,
                owner="terminal-owner-b",
                fencing_token=2,
            )
        )
        self._attach_definitely_unsent_queue_attempt(
            thread_id,
            current_root,
            firestore,
            saga,
            pending_docs,
        )
        stale_owner = processing.TerminalSagaExecution(
            owner="terminal-owner-a",
            fencing_token=1,
        )

        with patch.object(processing, "_fs", firestore), \
             patch.object(processing, "queue_pending_response") as queue_pending, \
             patch.object(processing, "send_reply_in_thread") as send_reply:
            with self.assertRaisesRegex(
                processing.RetryableProcessingError,
                "terminal saga execution ownership changed",
            ):
                processing._settle_terminal_reply_obligation(
                    "user-1",
                    "client-1",
                    thread_id,
                    {"Authorization": "Bearer fake"},
                    "bp21harrison@gmail.com",
                    saga,
                    terminal_saga_owner=stale_owner,
                )

            current_root._data["terminalReplyOwed"] = False
            with self.assertRaisesRegex(
                processing.RetryableProcessingError,
                "terminal saga execution ownership changed",
            ):
                processing._clear_resolved_terminal_saga(
                    "user-1",
                    thread_id,
                    saga,
                    terminal_saga_owner=stale_owner,
                )

        queue_pending.assert_not_called()
        send_reply.assert_not_called()
        self.assertEqual("terminal-owner-b", current_root._data["terminalSagaClaim"]["owner"])
        self.assertEqual(2, current_root._data["terminalSagaClaim"]["fencingToken"])
        self.assertEqual(saga["sagaKey"], current_root._data["terminalSagaKey"])
        self.assertIsNotNone(current_root._data["terminalSaga"])

    def test_cleanup_apply_then_raise_reconciles_and_exact_retry_uses_settlement(self):
        thread_id = "thread-terminal-cleanup-ambiguous-settlement"
        current_root, firestore, saga, _pending_docs = (
            self._finalized_terminal_execution_fixture(
                thread_id,
                terminal_reply_owed=False,
            )
        )
        current_root._data.update({
            "terminalNotificationOwed": False,
            "terminalNotificationOutcome": "created",
            "terminalReplyOutcome": "sent_indexed",
        })
        firestore.transaction_commit_behaviors_by_field = {
            "terminalSagaKey": [{
                "error": RuntimeError("cleanup applied then transport failed"),
                "applyBeforeError": True,
            }],
        }
        owner = processing.TerminalSagaExecution(
            owner=current_root._data["terminalSagaClaim"]["owner"],
            fencing_token=current_root._data["terminalSagaClaim"]["fencingToken"],
        )

        with patch.object(processing, "_fs", firestore):
            try:
                processing._clear_resolved_terminal_saga(
                    "user-1",
                    thread_id,
                    saga,
                    terminal_saga_owner=owner,
                )
            except processing.RetryableProcessingError as exc:
                self.fail(
                    "cleanup apply-then-raise must reconcile exact committed state: "
                    f"{exc}"
                )

        self.assertIsNone(current_root._data.get("terminalSaga"))
        self.assertIsNone(current_root._data.get("terminalSagaKey"))
        self.assertIsNone(current_root._data.get("terminalSagaClaim"))
        self.assertEqual(1, len(current_root._data.get("terminalSettlements") or []))

        retry_capture = {}
        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="This property is no longer available.",
            proposal={
                "updates": [{"field": "askingRent", "value": "99.99"}],
                "events": [{"type": "new_property", "address": "Should not run"}],
                "response_email": "This must not send.",
            },
            thread_ref=current_root,
            row_anchor="951 E FM 646",
            rownum=10,
            row_below_nonviable=True,
            existing_note=saga["note"],
            capture=retry_capture,
        )

        retry_capture["saveInboundMessage"].assert_not_called()
        retry_capture["cancelFollowup"].assert_not_called()
        retry_capture["fetchSheet"].assert_not_called()
        retry_capture["proposeUpdates"].assert_not_called()
        retry_capture["sendReply"].assert_not_called()

    def test_two_terminal_generations_keep_exact_tombstones_and_reset_active_sheet_state(self):
        thread_id = "thread-terminal-two-generations"
        current_root, firestore, saga_a, _pending_docs = (
            self._finalized_terminal_execution_fixture(
                thread_id,
                terminal_reply_owed=False,
            )
        )
        owner_a = processing.TerminalSagaExecution(
            owner=current_root._data["terminalSagaClaim"]["owner"],
            fencing_token=current_root._data["terminalSagaClaim"]["fencingToken"],
        )
        attempt_a = self._test_terminal_sheet_attempt(
            saga_a,
            current_root._data["terminalSagaClaim"],
            "move_with_note",
            status="applied",
        )
        current_root._data.update({
            "terminalNotificationOwed": False,
            "terminalNotificationOutcome": "created",
            "terminalReplyOutcome": "sent_indexed",
            "terminalSheetMutationAttempt": attempt_a,
            "terminalSheetMutationHistory": [],
            "terminalSheetMutationReview": None,
        })

        with patch.object(processing, "_fs", firestore):
            processing._clear_resolved_terminal_saga(
                "user-1",
                thread_id,
                saga_a,
                terminal_saga_owner=owner_a,
            )

            self.assertIsNone(current_root._data.get("terminalSheetMutationAttempt"))
            self.assertIsNone(current_root._data.get("terminalSheetMutationHistory"))
            self.assertIsNone(current_root._data.get("terminalSheetMutationReview"))

            immutable_b = {
                key: copy.deepcopy(value)
                for key, value in saga_a.items()
                if key not in {"immutableHash", "phase", "finalRow"}
            }
            immutable_b.update({
                "sagaKey": f"terminal-saga-{thread_id}-generation-b",
                "settlementOrdinal": 2,
                "sourceMessageKey": f"<{thread_id}-b@mock.test>",
                "sourceGraphMessageId": f"msg-{thread_id}-b",
                "sourceInternetMessageId": f"<{thread_id}-b@mock.test>",
                "sourceReceivedAt": "2026-06-20T19:12:39Z",
                "sourceRow": 3,
            })
            saga_b = {
                **immutable_b,
                "immutableHash": hashlib.sha256(
                    json.dumps(
                        immutable_b,
                        sort_keys=True,
                        default=str,
                    ).encode("utf-8")
                ).hexdigest(),
                "phase": "staged",
            }
            current_root.update({
                "status": processing.THREAD_STATUS["active"],
                "rowNumber": 3,
            })
            staged_b, owner_b = processing._stage_terminal_saga(
                "user-1",
                saga_b["clientId"],
                thread_id,
                saga_b,
            )
            attempt_b, created_b = processing._begin_terminal_sheet_mutation_attempt(
                "user-1",
                staged_b,
                owner_b,
                "move_with_note",
            )
            self.assertTrue(created_b)
            self.assertEqual(1, attempt_b["ordinal"])
            self.assertEqual(saga_b["sagaKey"], attempt_b["sagaKey"])
            processing._record_terminal_sheet_mutation_state(
                "user-1",
                staged_b,
                owner_b,
                attempt_b,
                "applied",
                appliedByOwner=owner_b.owner,
                appliedByFencingToken=owner_b.fencing_token,
                providerCompletedAt=(
                    attempt_b["requestStartedAt"] + timedelta(seconds=1)
                ),
                operatorReviewRequired=False,
            )
            finalized_b = processing._finalize_terminal_thread_roots(
                "user-1",
                saga_b["clientId"],
                thread_id,
                staged_b,
                final_row=10,
                terminal_saga_owner=owner_b,
            )
            current_root.update({
                "terminalNotificationOwed": False,
                "terminalNotificationOutcome": "created",
                "terminalReplyOwed": False,
                "terminalReplyOutcome": "sent_indexed",
                "terminalReplyAttempt": {
                    **self._terminal_reply_attempt(
                        finalized_b,
                        "committed",
                    ),
                    "outcome": "sent_indexed",
                    "committedAt": datetime.now(timezone.utc),
                },
            })
            processing._clear_resolved_terminal_saga(
                "user-1",
                thread_id,
                finalized_b,
                terminal_saga_owner=owner_b,
            )

        settlements = current_root._data.get("terminalSettlements")
        self.assertIsInstance(settlements, list)
        self.assertEqual(2, len(settlements))
        self.assertEqual(
            saga_a["sagaKey"],
            processing._terminal_settlement_for_source(
                current_root._data,
                saga_a["sourceGraphMessageId"],
                saga_a["sourceInternetMessageId"],
            )["sagaKey"],
        )
        self.assertEqual(
            saga_b["sagaKey"],
            processing._terminal_settlement_for_source(
                current_root._data,
                saga_b["sourceGraphMessageId"],
                saga_b["sourceInternetMessageId"],
            )["sagaKey"],
        )
        self.assertEqual(
            [attempt_a["attemptId"], attempt_b["attemptId"]],
            [
                settlement["sheetMutationAttempt"]["attemptId"]
                for settlement in settlements
            ],
        )
        self.assertIsNone(current_root._data.get("terminalSheetMutationAttempt"))
        self.assertIsNone(current_root._data.get("terminalSheetMutationHistory"))
        self.assertIsNone(current_root._data.get("terminalSheetMutationReview"))

        for source_saga in (saga_a, saga_b):
            retry_capture = {}
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [{"field": "askingRent", "value": "99.99"}],
                    "events": [{"type": "new_property", "address": "Must not run"}],
                    "response_email": "This must not send.",
                },
                thread_ref=current_root,
                row_anchor=source_saga["rowAnchor"],
                rownum=10,
                row_below_nonviable=True,
                existing_note=source_saga["note"],
                msg_id_override=source_saga["sourceGraphMessageId"],
                internet_message_id_override=source_saga["sourceInternetMessageId"],
                capture=retry_capture,
            )
            retry_capture["saveInboundMessage"].assert_not_called()
            retry_capture["fetchSheet"].assert_not_called()
            retry_capture["proposeUpdates"].assert_not_called()
            retry_capture["sendReply"].assert_not_called()

    def test_real_event_loop_allows_reactivated_later_terminal_source_generation_and_recovers_both_settlements(self):
        thread_id = "thread-terminal-real-event-loop-generations"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "followUpStatus": "waiting",
        })
        terminal_proposal = {
            "updates": [],
            "events": [{
                "type": "property_unavailable",
                "reason": "no_longer_available",
            }],
            "response_email": None,
        }
        sources = (
            ("msg-generation-a", "<generation-a@mock.test>"),
            ("msg-generation-b", "<generation-b@mock.test>"),
        )

        for index, (message_id, internet_message_id) in enumerate(sources):
            if index:
                # Model the explicit operator/reviewed row rebound that makes a
                # later source a legitimate new terminal generation.
                thread_ref.update({
                    "status": processing.THREAD_STATUS["active"],
                    "statusReason": None,
                    "statusUpdatedAt": None,
                    "nonViableAt": None,
                    "nonViableReason": None,
                    "rowNumber": 3,
                    "followUpStatus": "waiting",
                })
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal=terminal_proposal,
                thread_ref=thread_ref,
                row_anchor="951 E FM 646",
                rownum=3,
                row_below_nonviable=False,
                msg_id_override=message_id,
                internet_message_id_override=internet_message_id,
                honor_handled_events=True,
            )
            if index == 0:
                first_settlement = thread_ref._data["terminalSettlements"][0]
                self.assertEqual(
                    "committed",
                    first_settlement["terminalReplyAttempt"]["status"],
                )
                self.assertIsNone(thread_ref._data.get("terminalReplyAttempt"))
                self.assertFalse(
                    pending_responses._has_terminal_pending_send_marker(
                        thread_ref._data
                    )
                )

        settlements = thread_ref._data.get("terminalSettlements") or []
        self.assertEqual(2, len(settlements))
        self.assertEqual(
            {"<generation-a@mock.test>", "<generation-b@mock.test>"},
            {item["sourceMessageKey"] for item in settlements},
        )
        self.assertEqual(2, len({item["sagaKey"] for item in settlements}))

        for message_id, internet_message_id in sources:
            retry_capture = {}
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [{"field": "askingRent", "value": "99.99"}],
                    "events": [{"type": "new_property", "address": "Must not run"}],
                    "response_email": "This must not send.",
                },
                thread_ref=thread_ref,
                row_anchor="951 E FM 646",
                rownum=10,
                row_below_nonviable=True,
                msg_id_override=message_id,
                internet_message_id_override=internet_message_id,
                honor_handled_events=True,
                capture=retry_capture,
            )
            retry_capture["saveInboundMessage"].assert_not_called()
            retry_capture["fetchSheet"].assert_not_called()
            retry_capture["proposeUpdates"].assert_not_called()
            retry_capture["sendReply"].assert_not_called()

    def test_legacy_thread_wide_terminal_marker_is_source_scoped_after_reactivation(self):
        source_a = "msg-legacy-a"
        source_b = "msg-legacy-b"
        key_a = processing._terminal_event_key_for_source(
            "property_unavailable",
            source_a,
            "<legacy-a@mock.test>",
        )
        key_b = processing._terminal_event_key_for_source(
            "property_unavailable",
            source_b,
            "<legacy-b@mock.test>",
        )
        legacy_data = {
            "status": processing.THREAD_STATUS["active"],
            "handledEvents": {
                "property_unavailable": {
                    "detectedInMessageId": source_a,
                },
            },
        }

        self.assertTrue(processing._terminal_event_is_handled_for_source(
            legacy_data,
            "property_unavailable",
            key_a,
            source_a,
            "<legacy-a@mock.test>",
        ))
        self.assertFalse(processing._terminal_event_is_handled_for_source(
            legacy_data,
            "property_unavailable",
            key_b,
            source_b,
            "<legacy-b@mock.test>",
        ))
        legacy_data["status"] = processing.THREAD_STATUS["stopped"]
        self.assertTrue(processing._terminal_event_is_handled_for_source(
            legacy_data,
            "property_unavailable",
            key_b,
            source_b,
            "<legacy-b@mock.test>",
        ))

    def test_same_contact_reactivation_stages_new_terminal_source_despite_stale_legacy_terminal_fields(self):
        thread_id = "thread-replacement-stale-legacy-marker"
        current_message_id = "msg-replacement-terminal-b"
        current_internet_message_id = "<replacement-terminal-b@mock.test>"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["stopped"],
            "statusReason": "no_longer_available",
            "rowNumber": 10,
            "followUpStatus": "stopped",
            "nonViableAt": datetime(2026, 6, 18, tzinfo=timezone.utc),
            "nonViableReason": "no_longer_available",
            "activeReplacementProperty": {
                "address": "414 Alternate Signal Pkwy",
                "city": "North Las Vegas",
                "rowNumber": 7,
            },
            "handledEvents": {
                "property_unavailable": {
                    "detectedInMessageId": "msg-original-terminal-a",
                    "sourceInternetMessageId": "<original-terminal-a@mock.test>",
                },
            },
        })
        capture = {}

        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "property_unavailable event failed",
        ):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="414 Alternate Signal Pkwy is no longer available.",
                proposal={
                    "updates": [],
                    "events": [{
                        "type": "property_unavailable",
                        "reason": "no_longer_available",
                    }],
                    "response_email": None,
                },
                thread_ref=thread_ref,
                row_anchor="414 Alternate Signal Pkwy",
                rownum=7,
                msg_id_override=current_message_id,
                internet_message_id_override=current_internet_message_id,
                move_row_side_effect=RuntimeError(
                    "stop after proving new terminal work was staged"
                ),
                capture=capture,
            )

        self.assertEqual(processing.THREAD_STATUS["active"], thread_ref._data["status"])
        self.assertEqual(7, thread_ref._data["rowNumber"])
        self.assertEqual(
            "same_contact_replacement_reply",
            thread_ref._data["statusReason"],
        )
        self.assertEqual("stopped", thread_ref._data["followUpStatus"])
        self.assertEqual(
            "no_longer_available",
            thread_ref._data["pendingTerminalReason"],
        )
        self.assertEqual(
            current_message_id,
            thread_ref._data["terminalSaga"]["sourceGraphMessageId"],
        )
        self.assertEqual(
            current_internet_message_id,
            thread_ref._data["terminalSaga"]["sourceInternetMessageId"],
        )
        capture["moveRow"].assert_called_once()

    def test_same_contact_reactivation_still_honors_exact_source_legacy_terminal_marker(self):
        thread_id = "thread-replacement-exact-legacy-marker"
        message_id = "msg-replacement-terminal-exact"
        internet_message_id = "<replacement-terminal-exact@mock.test>"
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["stopped"],
            "statusReason": "no_longer_available",
            "rowNumber": 10,
            "followUpStatus": "stopped",
            "nonViableAt": datetime(2026, 6, 18, tzinfo=timezone.utc),
            "nonViableReason": "no_longer_available",
            "activeReplacementProperty": {
                "address": "414 Alternate Signal Pkwy",
                "city": "North Las Vegas",
                "rowNumber": 7,
            },
            "handledEvents": {
                "property_unavailable": {
                    "detectedInMessageId": message_id,
                    "sourceInternetMessageId": internet_message_id,
                },
            },
        })
        capture = {}

        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="414 Alternate Signal Pkwy is no longer available.",
            proposal={
                "updates": [],
                "events": [{
                    "type": "property_unavailable",
                    "reason": "no_longer_available",
                }],
                "response_email": None,
            },
            thread_ref=thread_ref,
            row_anchor="414 Alternate Signal Pkwy",
            rownum=7,
            msg_id_override=message_id,
            internet_message_id_override=internet_message_id,
            capture=capture,
        )

        self.assertEqual(processing.THREAD_STATUS["active"], thread_ref._data["status"])
        self.assertEqual(7, thread_ref._data["rowNumber"])
        self.assertEqual(
            "same_contact_replacement_reply",
            thread_ref._data["statusReason"],
        )
        self.assertIsNone(thread_ref._data.get("terminalSaga"))
        self.assertIsNone(thread_ref._data.get("terminalSagaKey"))
        capture["moveRow"].assert_not_called()
        capture["ensureDivider"].assert_not_called()
        self.assertEqual([], capture["notificationAttempts"])

        unsourced_legacy = copy.deepcopy(thread_ref._data)
        unsourced_legacy["handledEvents"]["property_unavailable"] = {
            "notificationId": "legacy-terminal-notification",
        }
        unsourced_message_id = "msg-replacement-terminal-unsourced"
        unsourced_internet_message_id = (
            "<replacement-terminal-unsourced@mock.test>"
        )
        unsourced_event_key = processing._terminal_event_key_for_source(
            "property_unavailable",
            unsourced_message_id,
            unsourced_internet_message_id,
        )
        self.assertTrue(
            processing._terminal_event_is_handled_for_source(
                unsourced_legacy,
                "property_unavailable",
                unsourced_event_key,
                unsourced_message_id,
                unsourced_internet_message_id,
            ),
            "unsourced legacy terminal evidence must remain fail-closed",
        )

    def test_terminal_settlement_retention_limit_fails_closed_without_eviction(self):
        thread_id = "thread-terminal-settlement-retention-limit"
        current_root, firestore, saga, _pending_docs = (
            self._finalized_terminal_execution_fixture(
                thread_id,
                terminal_reply_owed=False,
            )
        )
        current_root._data.update({
            "terminalNotificationOwed": False,
            "terminalReplyOwed": False,
        })
        prior_settlements = []
        for ordinal in range(1, processing.TERMINAL_SETTLEMENT_HISTORY_LIMIT + 1):
            prior_settlements.append(
                self._archived_terminal_projection_for_test(
                    saga,
                    ordinal,
                    source_suffix=f"prior-{ordinal}",
                )
            )
        current_root._data["terminalSettlements"] = copy.deepcopy(prior_settlements)
        owner = processing.TerminalSagaExecution(
            owner=current_root._data["terminalSagaClaim"]["owner"],
            fencing_token=current_root._data["terminalSagaClaim"]["fencingToken"],
        )

        with patch.object(processing, "_fs", firestore):
            with self.assertRaisesRegex(
                processing.RetryableProcessingError,
                "retention limit",
            ):
                processing._persist_terminal_settlement_projection(
                    "user-1",
                    thread_id,
                    saga,
                    owner,
                )

        self.assertEqual(prior_settlements, current_root._data["terminalSettlements"])

    def test_invalid_terminal_outcome_is_rejected_before_settlement_transaction_writes(self):
        thread_id = "thread-terminal-invalid-outcome-precommit"
        current_root, firestore, saga, _pending = (
            self._finalized_terminal_execution_fixture(
                thread_id,
                terminal_reply_owed=False,
            )
        )
        current_root._data["terminalNotificationOutcome"] = "created"
        current_root._data["terminalReplyOutcome"] = "forged_outcome"
        current_root._data["terminalReplyAttempt"] = {
            **current_root._data["terminalReplyAttempt"],
            "outcome": "forged_outcome",
        }
        before = copy.deepcopy(current_root._data)
        owner = processing.TerminalSagaExecution(
            owner=current_root._data["terminalSagaClaim"]["owner"],
            fencing_token=current_root._data["terminalSagaClaim"][
                "fencingToken"
            ],
        )

        with patch.object(processing, "_fs", firestore), self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "exact resolved reply attempt",
        ):
            processing._persist_terminal_settlement_projection(
                "user-1",
                thread_id,
                saga,
                owner,
            )

        self.assertEqual(before, current_root._data)
        self.assertEqual([], firestore.transaction_write_counts)

    def test_terminal_settlement_rejects_nested_tamper_even_with_recomputed_outer_hash(self):
        _root, _firestore, base_saga, _pending = (
            self._finalized_terminal_execution_fixture(
                "thread-terminal-settlement-tamper",
                terminal_reply_owed=False,
            )
        )
        projection = self._archived_terminal_projection_for_test(
            base_saga,
            1,
            source_suffix="tamper",
            with_reply=True,
        )
        processing._validate_terminal_settlement_history([projection])

        def rehash_outer(candidate):
            immutable = {
                key: value
                for key, value in candidate.items()
                if key != "projectionHash"
            }
            candidate["projectionHash"] = hashlib.sha256(
                json.dumps(
                    immutable,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()

        tamper_cases = {
            "sheet attempt": lambda value: value["sheetMutationAttempt"].update({
                "noteHash": "tampered-note-hash",
            }),
            "reply body": lambda value: value["terminalReplyAttempt"].update({
                "responseBodyHash": "tampered-reply-hash",
            }),
            "reply status": lambda value: value["terminalReplyAttempt"].update({
                "status": "sending",
            }),
            "reply outcome": lambda value: value["terminalReplyAttempt"].update({
                "outcome": "queued_retry",
            }),
            "saga context": lambda value: value["sagaSnapshot"][
                "finalizationPlan"
            ].update({"finalRow": 99}),
            "notification outcome": lambda value: value.update({
                "notificationOutcome": "created",
            }),
        }
        for label, tamper in tamper_cases.items():
            with self.subTest(label=label):
                candidate = copy.deepcopy(projection)
                tamper(candidate)
                rehash_outer(candidate)
                with self.assertRaises(processing.RetryableProcessingError):
                    processing._validate_terminal_settlement_history(
                        [candidate]
                    )
                with self.assertRaises(processing.RetryableProcessingError):
                    processing._terminal_settlement_for_source(
                        {"terminalSettlements": [candidate]},
                        projection["sourceGraphMessageId"],
                        projection["sourceInternetMessageId"],
                    )

        for field, forged_value in (
            ("sourceMessageKey", "<forged-source@mock.test>"),
            ("sourceGraphMessageId", "forged-graph-source"),
            ("conversationId", "forged-conversation"),
            ("recipient", "forged-recipient@example.test"),
        ):
            with self.subTest(rehashed_reply_binding=field):
                candidate = copy.deepcopy(projection)
                candidate["terminalReplyAttempt"][field] = forged_value
                candidate["terminalReplyAttemptHash"] = (
                    processing._terminal_reply_attempt_archive_hash(
                        candidate["terminalReplyAttempt"]
                    )
                )
                rehash_outer(candidate)
                with self.assertRaisesRegex(
                    processing.RetryableProcessingError,
                    "source binding",
                ):
                    processing._validate_terminal_settlement_history(
                        [candidate]
                    )

    def test_retry_disposition_classifies_seven_settled_and_new_active_generations(self):
        thread_id = "thread-terminal-retry-disposition-eight-generations"
        root, firestore, base_saga, _pending = (
            self._finalized_terminal_execution_fixture(thread_id)
        )
        settlements = [
            self._archived_terminal_projection_for_test(
                base_saga,
                ordinal,
                source_suffix=f"generation-{ordinal}",
            )
            for ordinal in range(1, 8)
        ]
        active_immutable = {
            key: copy.deepcopy(value)
            for key, value in base_saga.items()
            if key not in {"immutableHash", "phase", "finalRow"}
        }
        active_immutable.update({
            "settlementOrdinal": 8,
            "sagaKey": "active-generation-8",
            "sourceMessageKey": "<active-generation-8@mock.test>",
            "sourceGraphMessageId": "active-generation-8",
            "sourceInternetMessageId": "<active-generation-8@mock.test>",
            "sourceConversationId": "active-conversation-8",
        })
        active_saga = {
            **active_immutable,
            "immutableHash": hashlib.sha256(
                json.dumps(
                    active_immutable,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest(),
            "phase": "finalized",
            "finalRow": active_immutable["finalizationPlan"]["finalRow"],
        }
        root._data.update({
            "terminalSettlements": settlements,
            "terminalSaga": active_saga,
            "terminalSagaKey": active_saga["sagaKey"],
        })

        with patch.object(processing, "_fs", firestore):
            for settlement in settlements:
                disposition = processing._terminal_retry_disposition(
                    "user-1",
                    thread_id,
                    graph_message_id=settlement["sourceGraphMessageId"],
                    internet_message_id=settlement[
                        "sourceInternetMessageId"
                    ],
                )
                self.assertEqual("settled", disposition["kind"])
                self.assertEqual(
                    settlement["sagaKey"],
                    disposition["settlement"]["sagaKey"],
                )
            active = processing._terminal_retry_disposition(
                "user-1",
                thread_id,
                graph_message_id=active_saga["sourceGraphMessageId"],
                internet_message_id=active_saga[
                    "sourceInternetMessageId"
                ],
            )
            ordinary = processing._terminal_retry_disposition(
                "user-1",
                thread_id,
                graph_message_id="unseen-generation",
                internet_message_id="<unseen-generation@mock.test>",
            )
            with self.assertRaisesRegex(
                processing.RetryableProcessingError,
                "contradictory source aliases",
            ):
                processing._terminal_retry_disposition(
                    "user-1",
                    thread_id,
                    graph_message_id=active_saga[
                        "sourceGraphMessageId"
                    ],
                    internet_message_id=settlements[0][
                        "sourceInternetMessageId"
                    ],
                )

        self.assertEqual("active", active["kind"])
        self.assertEqual(active_saga["sagaKey"], active["saga"]["sagaKey"])
        self.assertEqual("ordinary", ordinary["kind"])

    def test_ninth_terminal_source_stops_before_generic_or_provider_effects(self):
        thread_id = "thread-terminal-settlement-ninth-source"
        fixture_root, _firestore, base_saga, _pending = (
            self._finalized_terminal_execution_fixture(
                "thread-terminal-settlement-ninth-fixture",
                terminal_reply_owed=False,
            )
        )
        thread_ref = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "followUpStatus": "waiting",
            "terminalSettlements": [
                self._archived_terminal_projection_for_test(
                    base_saga,
                    ordinal,
                    source_suffix=f"retained-{ordinal}",
                )
                for ordinal in range(
                    1,
                    processing.TERMINAL_SETTLEMENT_HISTORY_LIMIT + 1,
                )
            ],
        })
        before = copy.deepcopy(thread_ref._data)
        capture = {}
        proposal = {
            "updates": [{
                "column": "Rent/SF/Yr",
                "value": "12.50",
                "source": "inbound_email",
            }],
            "events": [
                {
                    "type": "tour_proposed",
                    "date": "2026-08-05",
                    "time": "10:00 AM",
                },
                {
                    "type": "property_unavailable",
                    "reason": "no_longer_available",
                },
            ],
            "response_email": "Thank you for the update.",
        }

        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "retention limit reached before generic source admission",
        ):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body=(
                    "The rent is $12.50. We can tour August 5 at 10 AM, "
                    "but this property is no longer available."
                ),
                proposal=proposal,
                thread_ref=thread_ref,
                row_anchor="951 E FM 646",
                capture=capture,
            )

        self.assertEqual(before, thread_ref._data)
        for name in (
            "saveInboundMessage",
            "indexInboundMessage",
            "dumpThread",
            "cancelFollowup",
            "findClientByEmail",
            "resumeManualContinuation",
            "fetchSheet",
            "fetchPdfs",
            "fetchLinkedAssets",
            "fetchUrl",
            "writeOrder",
            "proposeUpdates",
            "applyProposal",
            "readHeader",
            "findRowAnchor",
            "ensureDivider",
            "moveRow",
            "syncThreads",
            "stopThreads",
            "sendReply",
            "queuePending",
            "markClientCompleted",
        ):
            with self.subTest(effect=name):
                capture[name].assert_not_called()
        self.assertEqual([], capture["notificationAttempts"])
        self.assertEqual([], capture["handledEvents"])
        self.assertEqual([], capture["statusUpdates"])

    def test_accepted_terminal_send_then_new_owner_reconciles_without_second_send(self):
        thread_id = "thread-terminal-accepted-send-owner-takeover"
        current_root, firestore, saga, _pending_docs = (
            self._finalized_terminal_execution_fixture(thread_id)
        )
        owner_a = processing.TerminalSagaExecution(
            owner="terminal-owner-a",
            fencing_token=1,
        )
        owner_b = processing.TerminalSagaExecution(
            owner="terminal-owner-b",
            fencing_token=2,
        )
        send_calls = []

        def accepted_send_then_takeover(*_args, **kwargs):
            send_calls.append("accepted")
            capability = kwargs["graph_send_capability"]
            _prepare_and_consume_test_graph_capability(capability)
            retained_permit = send_permits.read_permit(capability)
            sent_match["sentDateTime"] = (
                retained_permit["requestStartedAt"] + timedelta(seconds=1)
            )
            send_permits.resolve_graph_send_permit(
                capability,
                "accepted",
                evidence={"httpStatus": 202, "phase": "send"},
            )
            retained_permit = send_permits.read_permit(capability)
            prepared_envelope = retained_permit["preparedEnvelope"]
            exact_evidence = {
                "id": prepared_envelope["draftId"],
                "sentMessageId": prepared_envelope["draftId"],
                "isDraft": False,
                "subject": prepared_envelope["subject"],
                "recipient": retained_permit["recipient"],
                "bodyHash": retained_permit["bodyHash"],
                "conversationId": retained_permit.get("conversationId"),
                "sentDateTime": (
                    retained_permit["requestStartedAt"]
                    + timedelta(seconds=1)
                ),
                "permitId": retained_permit["permitId"],
                "sourceGraphMessageId": retained_permit[
                    "sourceGraphMessageId"
                ],
                "preparedEnvelopeHash": prepared_envelope[
                    "preparedEnvelopeHash"
                ],
                "toRecipients": [
                    {"emailAddress": {"address": address}}
                    for address in prepared_envelope["toRecipients"]
                ],
                "ccRecipients": [],
                "bccRecipients": [],
                "body": {
                    "contentType": "HTML",
                    "content": "<p>Prepared test reply.</p>",
                },
                "attachments": [],
            }
            sent_match.clear()
            sent_match.update(exact_evidence)
            processing._set_reply_send_outcome(
                outcome="sent_indexed",
                conversation_id=retained_permit.get("conversationId"),
                exact_sent_evidence=exact_evidence,
            )
            current_root._data["terminalSagaClaim"] = {
                **current_root._data["terminalSagaClaim"],
                "owner": owner_b.owner,
                "fencingToken": owner_b.fencing_token,
                "claimedAt": datetime.now(timezone.utc),
                "leaseUntil": datetime.now(timezone.utc) + timedelta(minutes=5),
                "status": "recovering",
            }
            current_root._data["terminalSagaFence"] = owner_b.fencing_token
            current_root._version += 1
            return True

        sent_match = {
            "id": "sent-terminal-owner-interleave",
            "internetMessageId": "<sent-terminal-owner-interleave@mock.test>",
            "conversationId": saga["sourceConversationId"],
            "sentDateTime": "2026-06-19T19:13:00Z",
        }

        with patch.object(processing, "_fs", firestore), \
             patch.object(
                 processing,
                 "send_reply_in_thread",
                 side_effect=accepted_send_then_takeover,
             ) as send_reply, \
             patch.object(processing, "queue_pending_response") as queue_pending, \
             patch.object(
                 processing,
                 "find_exact_sent_message_by_immutable_id",
                 return_value=sent_match,
            ) as sent_lookup:
            with self.assertRaisesRegex(
                processing.RetryableProcessingError,
                "owner/fence changed|ownership changed",
            ):
                processing._settle_terminal_reply_obligation(
                    "user-1",
                    "client-1",
                    thread_id,
                    {"Authorization": "Bearer fake"},
                    "bp21harrison@gmail.com",
                    saga,
                    terminal_saga_owner=owner_a,
                )

            self.assertTrue(current_root._data["terminalReplyOwed"])
            self.assertEqual("terminal-owner-b", current_root._data["terminalSagaClaim"]["owner"])

            outcome = processing._settle_terminal_reply_obligation(
                "user-1",
                "client-1",
                thread_id,
                {"Authorization": "Bearer fake"},
                "bp21harrison@gmail.com",
                saga,
                terminal_saga_owner=owner_b,
            )

        self.assertEqual("sent_reconciled", outcome)
        self.assertEqual(["accepted"], send_calls)
        send_reply.assert_called_once()
        sent_lookup.assert_called_once()
        queue_pending.assert_not_called()
        self.assertFalse(current_root._data["terminalReplyOwed"])
        self.assertIsNone(current_root._data.get("terminalSaga"))
        self.assertIsNone(current_root._data.get("terminalSagaClaim"))

    def test_terminal_thread_read_outage_blocks_exact_and_different_source_before_effects(self):
        for source_kind in ("exact", "different"):
            with self.subTest(source_kind=source_kind):
                thread_id = f"thread-terminal-read-outage-{source_kind}"
                current_root = FakeDocumentRef({
                    "clientId": "client-1",
                    "email": ["bp21harrison@gmail.com"],
                    "status": processing.THREAD_STATUS["active"],
                    "rowNumber": 3,
                })
                with self.assertRaises(processing.RetryableProcessingError):
                    self._run_tour_invite_reply_processing(
                        thread_id=thread_id,
                        body="This property is no longer available.",
                        proposal={
                            "updates": [],
                            "events": [{
                                "type": "property_unavailable",
                                "reason": "no_longer_available",
                            }],
                            "response_email": None,
                        },
                        thread_ref=current_root,
                        row_anchor="951 E FM 646",
                        finalization_error=RuntimeError("stage for read outage"),
                    )

                current_root._get_error = RuntimeError("authoritative root read unavailable")
                self.campaign_decision.reset_mock()
                capture = {}
                call_kwargs = {}
                if source_kind == "different":
                    call_kwargs = {
                        "msg_id_override": f"msg-{thread_id}-different",
                        "internet_message_id_override": (
                            f"<{thread_id}-different@mock.test>"
                        ),
                    }
                with self.assertRaisesRegex(
                    processing.RetryableProcessingError,
                    "authoritative terminal thread read failed",
                ):
                    self._run_tour_invite_reply_processing(
                        thread_id=thread_id,
                        body="This property is no longer available.",
                        proposal={"updates": [], "events": [], "response_email": None},
                        thread_ref=current_root,
                        row_anchor="951 E FM 646",
                        capture=capture,
                        **call_kwargs,
                    )

                self.campaign_decision.assert_not_called()
                for side_effect in (
                    "fetchSheet",
                    "fetchPdfs",
                    "fetchLinkedAssets",
                    "fetchUrl",
                    "writeOrder",
                    "proposeUpdates",
                    "applyProposal",
                    "ensureDivider",
                    "moveRow",
                    "sendReply",
                    "queuePending",
                ):
                    capture[side_effect].assert_not_called()
                self.assertEqual([], capture["notifications"])

    def test_missing_matched_authoritative_thread_root_fails_closed_before_effects(self):
        thread_id = "thread-matched-authoritative-root-missing"
        missing_root = FakeDocumentRef({}, exists=False)
        capture = {}
        self.campaign_decision.reset_mock()

        with patch.object(
            processing,
            "mark_processed",
        ) as mark_processed, patch.object(
            processing,
            "_record_ai_processing_failure",
        ) as record_failure, self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "matched authoritative thread root is missing",
        ):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [],
                    "response_email": None,
                },
                thread_ref=missing_root,
                row_anchor="951 E FM 646",
                capture=capture,
            )

        self.campaign_decision.assert_not_called()
        for side_effect in (
            "threadStatusLookup",
            "saveInboundMessage",
            "indexInboundMessage",
            "findClientByEmail",
            "resumeManualContinuation",
            "fetchSheet",
            "fetchPdfs",
            "fetchLinkedAssets",
            "fetchUrl",
            "writeOrder",
            "proposeUpdates",
            "applyProposal",
            "ensureDivider",
            "moveRow",
            "noteGet",
            "noteUpdate",
            "dumpThread",
            "cancelFollowup",
            "sendReply",
            "queuePending",
        ):
            capture[side_effect].assert_not_called()
        self.assertEqual([], capture["notifications"])
        mark_processed.assert_not_called()
        record_failure.assert_not_called()
        self.assertFalse(missing_root._exists)
        self.assertEqual({}, missing_root._data)
        self.assertEqual(0, missing_root._version)

    def test_exact_terminal_saga_resumes_when_retry_body_is_blank(self):
        thread_id = "thread-terminal-blank-body-recovery"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })

        with self.assertRaises(processing.RetryableProcessingError):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                    "response_email": None,
                },
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                finalization_error=RuntimeError("finalization unavailable"),
            )

        recovery_capture = {}
        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="",
            proposal={"updates": [], "events": [], "response_email": None},
            thread_ref=current_root,
            row_anchor="951 E FM 646",
            rownum=10,
            row_below_nonviable=True,
            existing_note=current_root._data["terminalSaga"]["note"],
            capture=recovery_capture,
        )

        recovery_capture["proposeUpdates"].assert_not_called()
        recovery_capture["sendReply"].assert_called_once()
        self.assertFalse(current_root._data["terminalReplyOwed"])
        self.assertIsNone(current_root._data.get("terminalSaga"))

    def test_exact_terminal_saga_recovery_skips_unrelated_external_pipeline(self):
        thread_id = "thread-terminal-narrow-recovery"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })

        with self.assertRaises(processing.RetryableProcessingError):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                    "response_email": None,
                },
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                finalization_error=RuntimeError("finalization unavailable"),
            )

        recovery_capture = {}
        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body=(
                "This property is no longer available. "
                "https://example.test/unrelated-flyer.pdf"
            ),
            proposal={"updates": [], "events": [], "response_email": None},
            thread_ref=current_root,
            row_anchor="951 E FM 646",
            rownum=10,
            row_below_nonviable=True,
            existing_note=current_root._data["terminalSaga"]["note"],
            recovery_external_error=AssertionError(
                "exact saga recovery entered unrelated external pipeline"
            ),
            capture=recovery_capture,
        )

        recovery_capture["fetchPdfs"].assert_not_called()
        recovery_capture["fetchLinkedAssets"].assert_not_called()
        recovery_capture["fetchUrl"].assert_not_called()
        recovery_capture["writeOrder"].assert_not_called()
        recovery_capture["proposeUpdates"].assert_not_called()
        recovery_capture["sendReply"].assert_called_once()

    def test_exact_terminal_saga_settles_campaign_suppression_without_send(self):
        for campaign_status, paused, expected_outcome, expect_queue in (
            ("stopped", False, "campaign_stopped", False),
            ("live", True, "queued_retry", True),
        ):
            with self.subTest(campaign_status=campaign_status, paused=paused):
                thread_id = f"thread-terminal-campaign-{campaign_status}-{paused}"
                current_root = FakeDocumentRef({
                    "clientId": "client-1",
                    "email": ["bp21harrison@gmail.com"],
                    "status": processing.THREAD_STATUS["active"],
                    "rowNumber": 3,
                })

                with self.assertRaises(processing.RetryableProcessingError):
                    self._run_tour_invite_reply_processing(
                        thread_id=thread_id,
                        body="This property is no longer available.",
                        proposal={
                            "updates": [],
                            "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                            "response_email": None,
                        },
                        thread_ref=current_root,
                        row_anchor="951 E FM 646",
                        finalization_apply_then_error=RuntimeError(
                            "finalization applied then transport failed"
                        ),
                    )

                recovery_capture = {}
                self._run_tour_invite_reply_processing(
                    thread_id=thread_id,
                    body="This property is no longer available.",
                    proposal={"updates": [], "events": [], "response_email": None},
                    thread_ref=current_root,
                    row_anchor="951 E FM 646",
                    rownum=10,
                    row_below_nonviable=True,
                    existing_note=current_root._data["terminalSaga"]["note"],
                    campaign_status=campaign_status,
                    campaign_automation_paused=paused,
                    capture=recovery_capture,
                )

                recovery_capture["sendReply"].assert_not_called()
                if expect_queue:
                    recovery_capture["queuePending"].assert_called_once()
                else:
                    recovery_capture["queuePending"].assert_not_called()
                self.assertEqual(expected_outcome, current_root._data["terminalReplyOutcome"])
                self.assertFalse(current_root._data["terminalReplyOwed"])
                self.assertIsNone(current_root._data.get("terminalSaga"))

    def test_paused_terminal_reply_queue_reconciles_after_outcome_write_failure(self):
        thread_id = "thread-terminal-paused-queue-ambiguity"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        pending_response_docs = {}
        with self.assertRaises(processing.RetryableProcessingError):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                    "response_email": None,
                },
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                finalization_apply_then_error=RuntimeError(
                    "finalization applied then transport failed"
                ),
                pending_response_docs=pending_response_docs,
            )

        first_recovery = {}
        with self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "suppression outcome persistence failed",
        ):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={"updates": [], "events": [], "response_email": None},
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                rownum=10,
                row_below_nonviable=True,
                existing_note=current_root._data["terminalSaga"]["note"],
                campaign_status="live",
                campaign_automation_paused=True,
                queue_outcome_update_error=RuntimeError(
                    "queue created but owed clear failed"
                ),
                pending_response_docs=pending_response_docs,
                capture=first_recovery,
            )

        first_recovery["queuePending"].assert_called_once()
        first_recovery["sendReply"].assert_not_called()
        self.assertTrue(current_root._data["terminalReplyOwed"])

        current_root._update_error = None
        current_root._data["terminalSagaClaim"] = {
            **current_root._data["terminalSagaClaim"],
            "leaseUntil": datetime.now(timezone.utc) - timedelta(seconds=1),
        }
        second_recovery = {}
        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="This property is no longer available.",
            proposal={"updates": [], "events": [], "response_email": None},
            thread_ref=current_root,
            row_anchor="951 E FM 646",
            rownum=10,
            row_below_nonviable=True,
            existing_note=current_root._data["terminalSaga"]["note"],
            campaign_status="live",
            campaign_automation_paused=False,
            pending_response_docs=pending_response_docs,
            capture=second_recovery,
        )

        second_recovery["sendReply"].assert_not_called()
        second_recovery["sentReplyLookup"].assert_not_called()
        second_recovery["queuePending"].assert_not_called()
        self.assertEqual("queued_retry", current_root._data["terminalReplyOutcome"])
        self.assertFalse(current_root._data["terminalReplyOwed"])
        self.assertIsNone(current_root._data.get("terminalSaga"))

    def test_exact_terminal_saga_rejects_tab_or_notes_context_drift(self):
        drift_cases = (
            {"tab_title_override": "Renamed Sheet"},
            {"extra_header_before_notes": "Unexpected Column"},
        )
        for case_index, drift in enumerate(drift_cases):
            with self.subTest(drift=drift):
                thread_id = f"thread-terminal-context-drift-{case_index}"
                current_root = FakeDocumentRef({
                    "clientId": "client-1",
                    "email": ["bp21harrison@gmail.com"],
                    "status": processing.THREAD_STATUS["active"],
                    "rowNumber": 3,
                })
                with self.assertRaises(processing.RetryableProcessingError):
                    self._run_tour_invite_reply_processing(
                        thread_id=thread_id,
                        body="This property is no longer available.",
                        proposal={
                            "updates": [],
                            "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                            "response_email": None,
                        },
                        thread_ref=current_root,
                        row_anchor="951 E FM 646",
                        finalization_error=RuntimeError("finalization unavailable"),
                    )

                recovery_capture = {}
                with self.assertRaisesRegex(
                    processing.RetryableProcessingError,
                    "context drift",
                ):
                    self._run_tour_invite_reply_processing(
                        thread_id=thread_id,
                        body="This property is no longer available.",
                        proposal={"updates": [], "events": [], "response_email": None},
                        thread_ref=current_root,
                        row_anchor="951 E FM 646",
                        rownum=10,
                        row_below_nonviable=True,
                        existing_note=current_root._data["terminalSaga"]["note"],
                        capture=recovery_capture,
                        **drift,
                    )

                recovery_capture["moveRow"].assert_not_called()
                recovery_capture["noteUpdate"].assert_not_called()
                recovery_capture["sendReply"].assert_not_called()
                self.assertTrue(current_root._data.get("terminalReplyOwed", False) is False)
                self.assertIsNotNone(current_root._data.get("terminalSaga"))

    def test_exact_terminal_saga_ignores_fresh_client_sheet_drift(self):
        thread_id = "thread-terminal-client-sheet-drift"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        first_capture = {}
        with self.assertRaises(processing.RetryableProcessingError):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                    "response_email": None,
                },
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                finalization_error=RuntimeError("finalization unavailable"),
                capture=first_capture,
            )

        first_capture["sendReply"].assert_not_called()
        self.assertEqual(
            "applied",
            current_root._data["terminalSheetMutationAttempt"]["status"],
        )
        self.assertEqual(
            "sheet-1",
            current_root._data["terminalSaga"]["sheetId"],
        )
        recovery_capture = {}
        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="This property is no longer available.",
            proposal={"updates": [], "events": [], "response_email": None},
            thread_ref=current_root,
            row_anchor="951 E FM 646",
            rownum=10,
            row_below_nonviable=True,
            existing_note=current_root._data["terminalSaga"]["note"],
            sheet_id_override="fresh-client-sheet-must-be-ignored",
            capture=recovery_capture,
        )

        recovery_capture["fetchSheet"].assert_not_called()
        recovery_capture["noteGet"].assert_not_called()
        recovery_capture["noteUpdate"].assert_not_called()
        recovery_capture["moveRow"].assert_not_called()
        recovery_capture["ensureDivider"].assert_not_called()
        recovery_capture["readHeader"].assert_called_once()
        self.assertEqual(
            "sheet-1",
            recovery_capture["readHeader"].call_args.args[1],
        )
        recovery_capture["findRowAnchor"].assert_called_once()
        self.assertEqual(
            "sheet-1",
            recovery_capture["findRowAnchor"].call_args.args[3],
        )
        recovery_capture["sendReply"].assert_called_once()
        self.assertEqual(
            "sent_indexed",
            current_root._data["terminalReplyOutcome"],
        )
        self.assertFalse(current_root._data["terminalReplyOwed"])
        self.assertFalse(current_root._data["terminalNotificationOwed"])
        self.assertEqual(
            processing.THREAD_STATUS["stopped"],
            current_root._data["status"],
        )
        self.assertEqual(10, current_root._data["rowNumber"])
        self.assertIsNone(current_root._data.get("terminalSaga"))
        self.assertIsNone(
            current_root._data.get("terminalSheetMutationAttempt")
        )

    def test_exact_terminal_saga_uses_persisted_recipient_on_recovery(self):
        thread_id = "thread-terminal-recipient-drift"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        with self.assertRaises(processing.RetryableProcessingError):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                    "response_email": None,
                },
                thread_ref=current_root,
                row_anchor="951 E FM 646",
                finalization_apply_then_error=RuntimeError(
                    "finalization applied then transport failed"
                ),
            )

        original_recipient = current_root._data["terminalSaga"]["replyRecipient"]
        recovery_capture = {}
        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="This property is no longer available.",
            proposal={"updates": [], "events": [], "response_email": None},
            thread_ref=current_root,
            row_anchor="951 E FM 646",
            rownum=10,
            row_below_nonviable=True,
            existing_note=current_root._data["terminalSaga"]["note"],
            reply_recipient_override="fresh-drift@example.test",
            capture=recovery_capture,
        )

        self.assertEqual(
            original_recipient,
            recovery_capture["notificationAttempts"][0]["kwargs"]["email"],
        )
        self.assertEqual(
            original_recipient,
            recovery_capture["sendReply"].call_args.args[4],
        )

    def _terminal_staging_plan_fixture(self, suffix):
        current_id = f"thread-stage-{suffix}-current"
        sibling_id = f"thread-stage-{suffix}-sibling"
        shift_id = f"thread-stage-{suffix}-shift"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "followUpStatus": "waiting",
        }, doc_id=current_id)
        sibling_root = FakeDocumentRef({
            "clientId": "client-1",
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "followUpStatus": "waiting",
        }, doc_id=sibling_id)
        shift_root = FakeDocumentRef({
            "clientId": "client-1",
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 5,
            "followUpStatus": "waiting",
        }, doc_id=shift_id)
        roots = {
            current_id: current_root,
            sibling_id: sibling_root,
            shift_id: shift_root,
        }
        firestore = FakeFirestore(
            current_root,
            FakeDocumentRef({"status": "live"}),
            thread_docs=roots,
        )
        with patch.object(processing, "_fs", firestore):
            plan = processing._build_terminal_finalization_plan(
                "user-1",
                "client-1",
                current_id,
                source_row=3,
                divider_row=10,
            )
        immutable = {
            "version": processing.TERMINAL_SAGA_VERSION,
            "settlementOrdinal": 1,
            "sagaKey": f"saga-stage-{suffix}",
            "sourceMessageKey": f"<stage-{suffix}@mock.test>",
            "sourceGraphMessageId": f"msg-stage-{suffix}",
            "sourceInternetMessageId": f"<stage-{suffix}@mock.test>",
            "sourceConversationId": f"conversation-stage-{suffix}",
            "sourceReceivedAt": "2026-08-02T10:00:00Z",
            "reason": "no_longer_available",
            "note": "Property is no longer available.",
            "eventKey": "property_unavailable",
            "sourceRow": 3,
            "rowAnchor": "951 E FM 646",
            "responseScenario": "none",
            "responseBody": None,
            "completeClientAfterReply": True,
            "replyRecipient": "broker@example.test",
            "notificationRequired": False,
            "eventAddress": "951 E FM 646",
            "eventCity": "Houston",
            "clientId": "client-1",
            "sheetId": "sheet-1",
            "tabTitle": "Sheet1",
            "notesColumnIndex": 8,
            "notesColumnHeader": "Notes",
            "sheetHeaderFingerprint": "test-header-fingerprint",
            "dividerExists": True,
            "finalizationPlan": plan,
        }
        saga = {
            **immutable,
            "immutableHash": hashlib.sha256(
                json.dumps(immutable, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest(),
            "phase": "staged",
        }
        return current_id, sibling_id, shift_id, roots, firestore, saga

    def test_terminal_stage_rejects_frozen_plan_root_row_drift_before_any_marker_write(self):
        drift_cases = (
            ("terminal_root", "sibling", 4),
            ("planned_shift_root", "shift", 6),
        )
        for case_name, target_kind, drifted_row in drift_cases:
            with self.subTest(case=case_name):
                (
                    current_id,
                    sibling_id,
                    shift_id,
                    roots,
                    firestore,
                    saga,
                ) = self._terminal_staging_plan_fixture(case_name)
                target_id = sibling_id if target_kind == "sibling" else shift_id
                roots[target_id].update({"rowNumber": drifted_row})
                before = {
                    thread_id: copy.deepcopy(root._data)
                    for thread_id, root in roots.items()
                }

                with patch.object(processing, "_fs", firestore):
                    with self.assertRaises(processing.RetryableProcessingError):
                        processing._stage_terminal_saga(
                            "user-1",
                            "client-1",
                            current_id,
                            saga,
                        )

                self.assertEqual(
                    before,
                    {
                        thread_id: root._data
                        for thread_id, root in roots.items()
                    },
                )
                self.assertEqual([], firestore.transaction_write_counts)

    def test_terminal_stage_accepts_exact_unchanged_frozen_plan(self):
        (
            current_id,
            sibling_id,
            shift_id,
            roots,
            firestore,
            saga,
        ) = self._terminal_staging_plan_fixture("unchanged-control")
        shift_before = copy.deepcopy(roots[shift_id]._data)

        with patch.object(processing, "_fs", firestore):
            staged, owner = processing._stage_terminal_saga(
                "user-1",
                "client-1",
                current_id,
                saga,
            )

        self.assertEqual(saga["sagaKey"], staged["sagaKey"])
        self.assertIsInstance(owner, processing.TerminalSagaExecution)
        for terminal_id in (current_id, sibling_id):
            self.assertEqual("stopped", roots[terminal_id]._data["followUpStatus"])
            self.assertEqual(
                "no_longer_available",
                roots[terminal_id]._data["pendingTerminalReason"],
            )
            self.assertEqual(
                saga["sagaKey"],
                roots[terminal_id]._data["terminalSagaKey"],
            )
        self.assertEqual(saga, roots[current_id]._data["terminalSaga"])
        self.assertIsNone(roots[sibling_id]._data.get("terminalSaga"))
        self.assertEqual(shift_before, roots[shift_id]._data)
        self.assertEqual([2], firestore.transaction_write_counts)

    def test_terminal_stage_ambiguous_commit_returns_exact_readback_winner(self):
        (
            current_id,
            sibling_id,
            shift_id,
            roots,
            firestore,
            saga,
        ) = self._terminal_staging_plan_fixture("ambiguous-commit")
        shift_before = copy.deepcopy(roots[shift_id]._data)
        firestore.transaction_commit_behaviors_by_field = {
            "terminalSagaKey": [{
                "error": RuntimeError("staging acknowledgement was lost"),
                "applyBeforeError": True,
            }],
        }

        with patch.object(processing, "_fs", firestore):
            result = processing._stage_terminal_saga(
                "user-1",
                "client-1",
                current_id,
                saga,
            )

        self.assertIsNotNone(result)
        staged, owner = result
        self.assertEqual(saga, staged)
        self.assertIsInstance(owner, processing.TerminalSagaExecution)
        self.assertEqual(
            owner.owner,
            roots[saga["finalizationPlan"]["claimThreadId"]]
            ._data["terminalSagaClaim"]["owner"],
        )
        self.assertEqual(saga, roots[current_id]._data["terminalSaga"])
        self.assertEqual(
            saga["sagaKey"],
            roots[sibling_id]._data["terminalSagaKey"],
        )
        self.assertEqual(shift_before, roots[shift_id]._data)
        self.assertEqual([2], firestore.transaction_write_counts)

    def test_terminal_saga_two_workers_have_one_immutable_winner(self):
        root_a_id = "thread-terminal-two-worker-a"
        root_b_id = "thread-terminal-two-worker-b"
        root_a = FakeDocumentRef({
            "clientId": "client-1",
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        root_b = FakeDocumentRef({
            "clientId": "client-1",
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        transaction_read_barrier = Barrier(2)
        firestore = FakeFirestore(
            root_a,
            FakeDocumentRef({"status": "live"}),
            thread_docs={root_a_id: root_a, root_b_id: root_b},
            transaction_read_barrier=transaction_read_barrier,
        )

        def saga(source_suffix, reason, response_body):
            immutable = {
                "version": processing.TERMINAL_SAGA_VERSION,
                "settlementOrdinal": 1,
                "sagaKey": f"saga-{source_suffix}",
                "sourceMessageKey": f"<source-{source_suffix}@mock.test>",
                "sourceGraphMessageId": f"graph-source-{source_suffix}",
                "sourceInternetMessageId": f"<source-{source_suffix}@mock.test>",
                "sourceConversationId": "conversation-same-source",
                "sourceReceivedAt": "2026-06-19T19:12:39Z",
                "reason": reason,
                "note": f"immutable note: {reason}",
                "eventKey": "property_unavailable",
                "sourceRow": 3,
                "rowAnchor": "951 E FM 646",
                "responseScenario": "nonviable",
                "responseBody": response_body,
                "completeClientAfterReply": True,
                "replyRecipient": "bp21harrison@gmail.com",
                "notificationRequired": True,
                "eventAddress": "951 E FM 646",
                "eventCity": "Houston",
                "clientId": "client-1",
                "sheetId": "sheet-1",
                "tabTitle": "Sheet1",
                "notesColumnIndex": 8,
                "finalizationPlan": {
                    "dividerRow": 10,
                    "finalRow": 10,
                    "claimThreadId": root_a_id,
                    "terminalThreadIds": [root_a_id, root_b_id],
                    "rowShifts": [],
                    "writeCount": 2,
                },
            }
            return {
                **immutable,
                "immutableHash": hashlib.sha256(
                    json.dumps(immutable, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest(),
                "phase": "staged",
            }

        candidates = (
            (root_a_id, saga("a", "no_longer_available", "Original response body.")),
            (root_b_id, saga("b", "requirements_mismatch", "Drifted response body.")),
        )
        start = Barrier(2)
        acquired = []
        would_send = []

        def worker(current_thread_id, candidate):
            start.wait()
            result = processing._stage_terminal_saga(
                "user-1",
                "client-1",
                current_thread_id,
                candidate,
            )
            claimed_saga, _owner = result
            acquired.append(claimed_saga)
            would_send.append(claimed_saga["responseBody"])

        with patch.object(processing, "_fs", firestore):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(worker, current_thread_id, candidate)
                    for current_thread_id, candidate in candidates
                ]
                worker_errors = []
                for future in futures:
                    try:
                        future.result()
                    except processing.RetryableProcessingError as exc:
                        worker_errors.append(exc)

        self.assertEqual(1, len(acquired))
        self.assertEqual(1, len(would_send))
        self.assertEqual(1, len(worker_errors))
        saga_roots = [
            root for root in (root_a, root_b)
            if isinstance(root._data.get("terminalSaga"), dict)
        ]
        self.assertEqual(1, len(saga_roots))
        persisted = saga_roots[0]._data["terminalSaga"]
        processing._validate_terminal_saga_immutable_hash(persisted)
        self.assertIn(
            persisted["reason"],
            {candidate["reason"] for _thread_id, candidate in candidates},
        )
        self.assertEqual(
            persisted["responseBody"],
            next(
                candidate["responseBody"]
                for _thread_id, candidate in candidates
                if candidate["reason"] == persisted["reason"]
            ),
        )
        self.assertEqual(persisted["sagaKey"], root_a._data["terminalSagaKey"])
        self.assertEqual(persisted["sagaKey"], root_b._data["terminalSagaKey"])

    def test_terminal_stage_checks_graph_permit_on_every_frozen_plan_root(self):
        root_a_id = "thread-permit-alias-a"
        root_b_id = "thread-terminal-current-b"
        root_a = FakeDocumentRef({
            "clientId": "client-1",
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        }, doc_id=root_a_id)
        root_b = FakeDocumentRef({
            "clientId": "client-1",
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        }, doc_id=root_b_id)
        firestore = FakeFirestore(
            root_b,
            FakeDocumentRef({"status": "live"}),
            thread_docs={root_a_id: root_a, root_b_id: root_b},
        )
        now = datetime.now(timezone.utc)
        pending_data = {
            "threadId": root_a_id,
            "msgId": "pending-source-a",
            "recipient": "broker@example.test",
            "responseBody": "Earlier pending reply.",
            "clientId": "client-1",
            "conversationId": "conversation-a",
            "processingBy": "pending-worker-a",
            "processingLeaseUntil": now + timedelta(minutes=5),
            "status": "sending",
        }
        pending_ref = FakeDocumentRef(
            dict(pending_data),
            doc_id="pending-a",
        )
        capability = send_permits.issue_pending_graph_send_permit(
            firestore,
            root_a,
            pending_ref,
            dict(pending_data),
            "pending-worker-a",
        )
        immutable = {
            "version": processing.TERMINAL_SAGA_VERSION,
            "settlementOrdinal": 1,
            "sagaKey": "saga-current-b",
            "sourceMessageKey": "<source-b@mock.test>",
            "sourceGraphMessageId": "graph-source-b",
            "sourceInternetMessageId": "<source-b@mock.test>",
            "sourceConversationId": "conversation-b",
            "sourceReceivedAt": "2026-08-02T10:00:00Z",
            "reason": "no_longer_available",
            "note": "Property is no longer available.",
            "eventKey": "event-b",
            "sourceRow": 3,
            "rowAnchor": "951 E FM 646",
            "responseScenario": "nonviable",
            "responseBody": "Thank you for the update.",
            "completeClientAfterReply": True,
            "replyRecipient": "broker@example.test",
            "notificationRequired": True,
            "eventAddress": "951 E FM 646",
            "eventCity": "Houston",
            "clientId": "client-1",
            "sheetId": "sheet-1",
            "tabTitle": "Sheet1",
            "notesColumnIndex": 8,
            "dividerExists": True,
            "finalizationPlan": {
                "dividerRow": 10,
                "finalRow": 10,
                "claimThreadId": sorted([root_a_id, root_b_id])[0],
                "terminalThreadIds": sorted([root_a_id, root_b_id]),
                "rowShifts": [],
                "writeCount": 2,
            },
        }
        saga = {
            **immutable,
            "immutableHash": hashlib.sha256(
                json.dumps(immutable, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest(),
            "phase": "staged",
        }

        with patch.object(processing, "_fs", firestore):
            with self.assertRaisesRegex(
                processing.RetryableProcessingError,
                "Graph send permit|unresolved",
            ):
                processing._stage_terminal_saga(
                    "user-1",
                    "client-1",
                    root_b_id,
                    saga,
                )

            self.assertIsNone(root_a._data.get("terminalSagaKey"))
            self.assertIsNone(root_b._data.get("terminalSagaKey"))

            _prepare_and_consume_test_graph_capability(capability)
            send_permits.resolve_graph_send_permit(
                capability,
                "accepted",
                evidence={"httpStatus": 202, "phase": "send"},
            )
            retained_permit = send_permits.read_permit(capability)
            sent_evidence = _exact_sent_evidence_for_test(retained_permit)
            completion_id, completion_payload = (
                send_permits.pending_completion_obligation_payload(
                    user_id="user-1",
                    client_id=pending_data["clientId"],
                    thread_id=pending_data["threadId"],
                    pending_document_id=pending_ref.id,
                    source_graph_message_id=pending_data["msgId"],
                    pending_envelope_hash_value=(
                        send_permits.pending_envelope_hash(pending_data)
                    ),
                    permit_id=capability.permit_id,
                    permit_immutable_hash=capability.immutable_hash,
                    sent_evidence=sent_evidence,
                    complete_client_after_reply=True,
                )
            )
            completion_ref = FakeDocumentRef(
                {},
                exists=False,
                doc_id=completion_id,
            )
            send_permits.cas_pending_claim_transition(
                firestore,
                root_a,
                pending_ref,
                pending_data,
                "pending-worker-a",
                delete_pending=True,
                capability=capability,
                permit_settlement="settled_sent",
                sent_evidence=sent_evidence,
                side_documents=((completion_ref, completion_payload),),
            )
            staged, _owner = processing._stage_terminal_saga(
                "user-1",
                "client-1",
                root_b_id,
                saga,
            )

        self.assertEqual(saga["sagaKey"], staged["sagaKey"])
        self.assertEqual(saga["sagaKey"], root_a._data["terminalSagaKey"])
        self.assertEqual(saga["sagaKey"], root_b._data["terminalSagaKey"])

    def test_terminal_stage_blocks_malformed_permit_pointer_on_noncurrent_root(self):
        root_a_id = "thread-malformed-permit-a"
        root_b_id = "thread-malformed-current-b"
        root_a = FakeDocumentRef({
            "clientId": "client-1",
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
            "activeGraphSendPermit": {
                "version": send_permits.GRAPH_SEND_PERMIT_VERSION,
                "permitId": "missing-permit",
                "permitImmutableHash": "missing-hash",
            },
        }, doc_id=root_a_id)
        root_b = FakeDocumentRef({
            "clientId": "client-1",
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        }, doc_id=root_b_id)
        firestore = FakeFirestore(
            root_b,
            FakeDocumentRef({"status": "live"}),
            thread_docs={root_a_id: root_a, root_b_id: root_b},
        )
        immutable = {
            "version": processing.TERMINAL_SAGA_VERSION,
            "settlementOrdinal": 1,
            "sagaKey": "saga-malformed-pointer",
            "sourceMessageKey": "<malformed@mock.test>",
            "sourceGraphMessageId": "graph-malformed",
            "sourceInternetMessageId": "<malformed@mock.test>",
            "sourceConversationId": "conversation-malformed",
            "reason": "no_longer_available",
            "note": "Unavailable.",
            "eventKey": "event-malformed",
            "sourceRow": 3,
            "rowAnchor": "951 E FM 646",
            "responseScenario": "none",
            "responseBody": None,
            "completeClientAfterReply": True,
            "replyRecipient": "broker@example.test",
            "notificationRequired": False,
            "eventAddress": "951 E FM 646",
            "eventCity": "Houston",
            "clientId": "client-1",
            "sheetId": "sheet-1",
            "tabTitle": "Sheet1",
            "notesColumnIndex": 8,
            "dividerExists": True,
            "finalizationPlan": {
                "dividerRow": 10,
                "finalRow": 10,
                "claimThreadId": sorted([root_a_id, root_b_id])[0],
                "terminalThreadIds": sorted([root_a_id, root_b_id]),
                "rowShifts": [],
                "writeCount": 2,
            },
        }
        saga = {
            **immutable,
            "immutableHash": hashlib.sha256(
                json.dumps(immutable, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest(),
            "phase": "staged",
        }

        with patch.object(processing, "_fs", firestore), self.assertRaisesRegex(
            processing.RetryableProcessingError,
            "Graph send permit|missing",
        ):
            processing._stage_terminal_saga(
                "user-1", "client-1", root_b_id, saga
            )

        self.assertIsNone(root_a._data.get("terminalSagaKey"))
        self.assertIsNone(root_b._data.get("terminalSagaKey"))

    def test_terminal_finalization_over_500_writes_fails_before_sheet_mutation(self):
        thread_id = "thread-terminal-preflight-limit"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        thread_docs = {thread_id: current_root}
        for index in range(500):
            thread_docs[f"shifted-root-{index}"] = FakeDocumentRef({
                "clientId": "client-1",
                "status": processing.THREAD_STATUS["active"],
                "rowNumber": 4 + (index % 6),
            })
        capture = {}

        with self.assertRaisesRegex(processing.RetryableProcessingError, "500|limit"):
            self._run_tour_invite_reply_processing(
                thread_id=thread_id,
                body="This property is no longer available.",
                proposal={
                    "updates": [],
                    "events": [{"type": "property_unavailable", "reason": "no_longer_available"}],
                    "response_email": None,
                },
                thread_ref=current_root,
                thread_docs=thread_docs,
                row_anchor="951 E FM 646",
                capture=capture,
            )

        capture["ensureDivider"].assert_not_called()
        capture["moveRow"].assert_not_called()
        capture["noteUpdate"].assert_not_called()
        capture["sendReply"].assert_not_called()
        self.assertEqual([], capture["notificationAttempts"])
        self.assertIsNone(current_root._data.get("terminalSaga"))

    def test_terminal_staging_and_finalization_accept_exactly_500_writes(self):
        thread_id = "thread-terminal-exact-500"
        current_root = FakeDocumentRef({
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "status": processing.THREAD_STATUS["active"],
            "rowNumber": 3,
        })
        thread_docs = {thread_id: current_root}
        for index in range(499):
            thread_docs[f"terminal-root-{index}"] = FakeDocumentRef({
                "clientId": "client-1",
                "status": processing.THREAD_STATUS["active"],
                "rowNumber": 3,
            })
        capture = {}

        self._run_tour_invite_reply_processing(
            thread_id=thread_id,
            body="This property is no longer available.",
            proposal={
                "updates": [],
                "events": [{
                    "type": "property_unavailable",
                    "reason": "no_longer_available",
                }],
                "response_email": None,
            },
            thread_ref=current_root,
            thread_docs=thread_docs,
            row_anchor="951 E FM 646",
            capture=capture,
        )

        self.assertGreaterEqual(
            capture["firestore"].transaction_write_counts.count(500),
            2,
            "both staging and finalization must fit the exact 500-write boundary",
        )
        capture["moveRow"].assert_called_once()
        self.assertEqual(
            processing.THREAD_STATUS["stopped"],
            current_root._data["status"],
        )

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
        result["stopThreads"].assert_not_called()
        self.assertEqual(
            processing.THREAD_STATUS["stopped"],
            thread_ref._data["status"],
        )
        self.assertEqual("requirements_mismatch", thread_ref._data["statusReason"])
        self.assertEqual("requirements_mismatch", thread_ref._data["nonViableReason"])
        self.assertEqual("stopped", thread_ref._data["followUpStatus"])
        self.assertTrue(
            any(key.startswith("handledEvents.property_unavailable") for key in thread_ref._data)
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
        result["markClientCompleted"].assert_called_once()
        completion_call = result["markClientCompleted"].call_args
        self.assertEqual(
            (
                "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
                "client-1",
            ),
            completion_call.args,
        )
        self.assertEqual(
            "live",
            completion_call.kwargs["client_ref"]._data["status"],
        )

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

    def test_non_allowlisted_user_tour_offer_creates_no_tour_action(self):
        body = "Hi Baylor,\n\nThe space is available. Let me know if you would like to schedule a tour.\n\nBest,\nBP21"
        thread_id = "thread-normal-tour-offer"
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
                    "type": "tour_requested",
                    "question": "Let me know if you would like to schedule a tour.",
                    "suggestedEmail": "Hi Ryan,\n\nCan we tour Tuesday morning?",
                }
            ],
            "response_email": None,
        }

        result = self._run_tour_invite_reply_processing(
            user_id="regular-user",
            thread_id=thread_id,
            body=body,
            proposal=proposal,
            thread_ref=thread_ref,
        )

        self.assertEqual([], result["notifications"])
        result["sendReply"].assert_not_called()
        self.assertFalse(
            any(update["reason"] == "tour_requested" for update in result["statusUpdates"])
        )
        self.assertTrue(
            any(
                handled["eventKey"] == "tour_requested"
                and handled["notifId"] is None
                for handled in result["handledEvents"]
            )
        )

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
            patch.object(processing, "_read_header_row2", return_value=header),
            patch.object(processing, "is_event_handled", return_value=False),
            patch.object(processing, "write_notification", side_effect=fake_write_notification),
            patch.object(processing, "mark_event_handled", side_effect=fake_mark_event_handled),
            patch.object(
                processing,
                "_preview_nonviable_divider",
                return_value={"dividerRow": 10, "exists": True},
            ),
            patch.object(processing, "ensure_nonviable_divider", return_value=10),
            patch.object(processing, "move_row_below_divider", return_value=10),
            patch.object(processing, "sync_thread_row_numbers_after_move"),
            patch.object(processing, "stop_threads_for_row", return_value=1),
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
            patch.object(
                processing,
                "send_reply_in_thread",
                side_effect=_record_successful_test_graph_send,
            ),
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


class TerminalNoteSheetBatchTests(unittest.TestCase):
    def test_missing_divider_move_note_and_source_delete_share_one_atomic_batch(self):
        sheets = MagicMock()
        spreadsheets = sheets.spreadsheets.return_value
        metadata_request = MagicMock()
        metadata_request.execute.return_value = {
            "sheets": [{"properties": {"sheetId": 123, "title": "Sheet1"}}]
        }
        spreadsheets.get.return_value = metadata_request
        header_request = MagicMock()
        header_request.execute.return_value = {
            "values": [[f"Column {index}" for index in range(1, 9)]]
        }
        spreadsheets.values.return_value.get.return_value = header_request
        batch_request = MagicMock()
        batch_request.execute.return_value = {}
        spreadsheets.batchUpdate.return_value = batch_request

        final_row = sheet_operations.move_row_below_new_divider_atomic(
            sheets,
            "sheet-1",
            "Sheet1",
            3,
            10,
            notes_column_index=8,
            notes_value="[06/19/2026] Property does not meet requirements",
        )

        self.assertEqual(10, final_row)
        spreadsheets.batchUpdate.assert_called_once()
        requests = spreadsheets.batchUpdate.call_args.kwargs["body"]["requests"]
        self.assertEqual(
            [
                "updateCells",
                "addConditionalFormatRule",
                "insertDimension",
                "copyPaste",
                "updateCells",
                "deleteDimension",
            ],
            [next(iter(request)) for request in requests],
        )
        self.assertEqual(
            "NON-VIABLE",
            requests[0]["updateCells"]["rows"][0]["values"][0]
            ["userEnteredValue"]["stringValue"],
        )
        self.assertEqual(
            "[06/19/2026] Property does not meet requirements",
            requests[4]["updateCells"]["rows"][0]["values"][0]
            ["userEnteredValue"]["stringValue"],
        )

    def test_move_row_batch_copies_note_and_deletes_source_atomically(self):
        sheets = MagicMock()
        spreadsheets = sheets.spreadsheets.return_value

        metadata_request = MagicMock()
        metadata_request.execute.return_value = {
            "sheets": [{"properties": {"sheetId": 123, "title": "Broker's Sheet"}}]
        }
        spreadsheets.get.return_value = metadata_request

        header_request = MagicMock()
        header_request.execute.return_value = {
            "values": [[f"Column {index}" for index in range(1, 30)]]
        }
        spreadsheets.values.return_value.get.return_value = header_request

        batch_request = MagicMock()
        batch_request.execute.return_value = {}
        spreadsheets.batchUpdate.return_value = batch_request

        new_row = sheet_operations.move_row_below_divider(
            sheets,
            "sheet-1",
            "Broker's Sheet",
            3,
            10,
            notes_column_index=27,
            notes_value="Existing note | [06/19/2026] Property does not meet client requirements",
        )

        self.assertEqual(10, new_row)
        spreadsheets.batchUpdate.assert_called_once()
        requests = spreadsheets.batchUpdate.call_args.kwargs["body"]["requests"]
        self.assertEqual(
            ["insertDimension", "copyPaste", "updateCells", "deleteDimension"],
            [next(iter(request)) for request in requests],
        )
        note_request = requests[2]["updateCells"]
        self.assertEqual(
            {
                "sheetId": 123,
                "startRowIndex": 10,
                "endRowIndex": 11,
                "startColumnIndex": 26,
                "endColumnIndex": 27,
            },
            note_request["range"],
        )
        self.assertEqual(
            "Existing note | [06/19/2026] Property does not meet client requirements",
            note_request["rows"][0]["values"][0]["userEnteredValue"]["stringValue"],
        )
        self.assertEqual("userEnteredValue", note_request["fields"])


if __name__ == "__main__":
    unittest.main()
