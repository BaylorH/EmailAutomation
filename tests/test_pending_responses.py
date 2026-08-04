import ast
import hashlib
import os
import sys
import types
import unittest
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

with patch("google.cloud.firestore.Client", return_value=types.SimpleNamespace()):
    from email_automation import pending_responses, processing, send_permits
    from email_automation.campaign_safety import CampaignAutomationDecision
    from email_automation.column_config import get_default_column_config


EXACT_PENDING_PROTOCOL = {
    "kind": "b1_exact_source",
    "version": 1,
}


def exact_pending_data(data):
    return {
        **data,
        "pendingProtocol": dict(EXACT_PENDING_PROTOCOL),
    }


class FakeDocRef:
    def __init__(self, doc=None, doc_id=None):
        self._doc = doc
        self.id = doc_id or getattr(doc, "id", None)
        self.deleted = False
        self.update_calls = []
        self.set_calls = []
        self.subcollections = {}

    def delete(self):
        self.deleted = True
        if self._doc is not None:
            self._doc.exists = False

    def update(self, data):
        self.update_calls.append(data)
        if self._doc is not None:
            self._doc._data.update(data)

    def get(self, transaction=None):
        if transaction is not None:
            return transaction.get(self)
        if self._doc is not None:
            return self._doc
        return types.SimpleNamespace(exists=False, to_dict=lambda: {})

    def set(self, data, merge=False):
        self.set_calls.append(data)
        if self._doc is None:
            self._doc = FakeDoc(self.id, {})
            self._doc.reference = self
        if merge:
            self._doc._data.update(data)
        else:
            self._doc._data = dict(data)
        self._doc.exists = True

    def collection(self, name):
        return self.subcollections.setdefault(name, FakeCollection())


class FakeDoc:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data
        self.exists = True
        self.reference = FakeDocRef(self, doc_id)

    def to_dict(self):
        return self._data


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or []
        self.add_calls = []
        self._missing_refs = {}

    def stream(self):
        return [doc for doc in self._all_docs() if doc.exists]

    def _all_docs(self):
        docs = list(self.docs)
        for ref in self._missing_refs.values():
            snapshot = ref.get()
            if snapshot.exists and snapshot not in docs:
                docs.append(snapshot)
        return docs

    def where(self, *, filter):
        return FakeQuery(self._all_docs(), filters=(filter,))

    def limit(self, count):
        return FakeQuery(self._all_docs(), query_limit=count)

    def add(self, data):
        self.add_calls.append(data)
        return FakeDocRef()

    def document(self, doc_id):
        for doc in self.docs:
            if doc.id == doc_id:
                return doc.reference
        return self._missing_refs.setdefault(doc_id, FakeDocRef(doc_id=doc_id))


class FakeQuery:
    def __init__(self, docs, *, filters=(), query_limit=None):
        self.docs = list(docs)
        self.filters = tuple(filters)
        self.query_limit = query_limit

    def where(self, *, filter):
        return FakeQuery(
            self.docs,
            filters=(*self.filters, filter),
            query_limit=self.query_limit,
        )

    def limit(self, count):
        return FakeQuery(
            self.docs,
            filters=self.filters,
            query_limit=count,
        )

    def stream(self):
        docs = [doc for doc in self.docs if doc.exists]
        for field_filter in self.filters:
            docs = [
                doc
                for doc in docs
                if doc.to_dict().get(field_filter.field_path)
                == field_filter.value
            ]
        if self.query_limit is not None:
            docs = docs[:self.query_limit]
        return docs


class FakeFirestore:
    def __init__(self, pending_docs):
        thread_ids = {
            str(doc.to_dict().get("threadId") or "")
            for doc in pending_docs
            if doc.to_dict().get("threadId")
        }
        self.collections = {
            "pendingResponses": FakeCollection(pending_docs),
            "deadLetterQueue": FakeCollection(),
            "threads": FakeCollection([
                FakeDoc(thread_id, {}) for thread_id in sorted(thread_ids)
            ]),
        }
        exact_bindings = []
        for doc in pending_docs:
            data = dict(getattr(doc, "_data", {}) or {})
            try:
                protocol = pending_responses._classify_pending_response_protocol(
                    data
                )
            except pending_responses.PendingResponseConflict:
                continue
            if protocol != "b1_exact_source":
                continue
            payload = pending_responses._pending_source_binding_payload(
                user_id="uid-1",
                pending_document_id=doc.id,
                data=data,
                pending_revision=data["pendingRevision"],
            )
            exact_bindings.append(FakeDoc(payload["bindingId"], payload))
        self.collections[
            pending_responses.PENDING_RESPONSE_SOURCE_BINDING_COLLECTION
        ] = FakeCollection(exact_bindings)

    def collection(self, name):
        return self

    def document(self, name):
        return self

    def collection(self, name):
        return self.collections.setdefault(name, FakeCollection()) if name != "users" else self

    def transaction(self):
        return FakeTransaction()


class FakeTransaction:
    def __init__(self):
        self._updates = []
        self._deletes = []

    def get(self, document_ref):
        if document_ref._doc is not None:
            return document_ref._doc
        return types.SimpleNamespace(exists=False, to_dict=lambda: {})

    def update(self, document_ref, data):
        self._updates.append(("update", document_ref, data))

    def set(self, document_ref, data):
        self._updates.append(("set", document_ref, data))

    def delete(self, document_ref):
        self._deletes.append(document_ref)

    def commit(self):
        for operation, document_ref, data in self._updates:
            if operation == "set":
                document_ref.set(data)
            elif document_ref._doc is not None:
                document_ref.update(data)
            else:
                document_ref._doc = FakeDoc(document_ref.id, dict(data))
                document_ref._doc.reference = document_ref
                document_ref.set_calls.append(data)
        for document_ref in self._deletes:
            document_ref.delete()


class PendingResponsesTests(unittest.TestCase):
    def test_pending_draft_review_is_written_to_user_root_operator_queue(self):
        firestore = MagicMock(name="firestore")
        user_ref = MagicMock(name="user_ref")
        thread_ref = MagicMock(name="thread_ref")
        pending_ref = MagicMock(name="pending_ref")
        review_ref = MagicMock(name="review_ref")
        user_ref.collection.return_value.document.return_value = review_ref
        capability = types.SimpleNamespace(
            permit_id="graph-send-permit-1",
            immutable_hash="a" * 64,
        )
        doc = types.SimpleNamespace(id="pending-1")
        data = {
            "threadId": "thread-1",
            "clientId": "client-1",
        }
        permit = {
            "permitId": capability.permit_id,
            "immutableHash": capability.immutable_hash,
            "sourceGraphMessageId": "source-1",
            "draftPreparation": {
                "draftId": "draft-1",
                "state": "prepared",
            },
            "preparedEnvelope": {"preparedEnvelopeHash": "b" * 64},
            "resolutionEvidence": {
                "automaticDeleteAttempted": False,
            },
            "resolutionEvidenceHash": "c" * 64,
        }

        with patch.object(
            pending_responses,
            "_pending_claim_refs",
            return_value=(firestore, user_ref, thread_ref, pending_ref),
        ), patch.object(
            pending_responses,
            "read_permit",
            return_value=permit,
        ), patch.object(
            pending_responses,
            "cas_pending_claim_transition",
            return_value=True,
        ) as settle:
            resolved = pending_responses._cas_pending_draft_review(
                "uid-1",
                doc,
                data,
                "pending-owner-1",
                capability,
                "Retained draft requires operator review.",
            )

        self.assertTrue(resolved)
        user_ref.collection.assert_called_once_with("graphSendDraftReviews")
        user_ref.collection.return_value.document.assert_called_once_with(
            f"pending-{capability.permit_id}"
        )
        thread_ref.collection.assert_not_called()
        side_documents = settle.call_args.kwargs["side_documents"]
        self.assertIs(review_ref, side_documents[0][0])
        self.assertEqual("manual_review", side_documents[0][1]["status"])
        self.assertFalse(side_documents[0][1]["retryAllowed"])

    def test_every_pending_exit_wrapper_raises_when_exact_cas_loses_ownership(self):
        doc = types.SimpleNamespace(id="pending-exit-owner-loss")
        loaded = {
            "threadId": "thread-exit-owner-loss",
            "msgId": "message-exit-owner-loss",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Thank you for the update.",
            "clientId": "client-1",
        }
        fake_refs = (
            MagicMock(name="firestore"),
            MagicMock(name="user_ref"),
            MagicMock(name="thread_ref"),
            MagicMock(name="pending_ref"),
        )
        wrappers = (
            (
                "update",
                lambda: pending_responses._cas_pending_update(
                    "uid-1",
                    doc,
                    loaded,
                    "owner-a",
                    {"status": "queued"},
                ),
            ),
            (
                "delete_with_side_document",
                lambda: pending_responses._cas_pending_dead_letter(
                    "uid-1",
                    doc,
                    loaded,
                    "owner-a",
                    "manual review",
                ),
            ),
            (
                "accepted_success",
                lambda: pending_responses._cas_pending_success(
                    "uid-1",
                    doc,
                    loaded,
                    "owner-a",
                    types.SimpleNamespace(
                        permit_id="graph-send-owner-loss",
                        immutable_hash="a" * 64,
                    ),
                    {"sentMessageId": "immutable-sent"},
                ),
            ),
            (
                "definitely_unsent_update",
                lambda: pending_responses._cas_pending_update(
                    "uid-1",
                    doc,
                    loaded,
                    "owner-a",
                    {"status": "queued"},
                    capability=MagicMock(name="definitely_unsent_capability"),
                    permit_settlement="settled_definitely_not_sent",
                ),
            ),
            (
                "reconciliation_delete_with_side_document",
                lambda: pending_responses._cas_pending_dead_letter(
                    "uid-1",
                    doc,
                    loaded,
                    "owner-a",
                    "ambiguous provider outcome",
                    capability=MagicMock(name="ambiguous_capability"),
                    permit_settlement="reconciliation_recorded",
                    already_sent=True,
                ),
            ),
        )

        for label, invoke in wrappers:
            with self.subTest(exit=label), patch.object(
                pending_responses,
                "_pending_claim_refs",
                return_value=fake_refs,
            ), patch.object(
                pending_responses,
                "_pending_exit_document_ref",
                return_value=MagicMock(name="exit_ref"),
            ), patch.object(
                pending_responses,
                "cas_pending_claim_transition",
                return_value=False,
            ):
                with self.assertRaisesRegex(
                    send_permits.GraphSendPermitBlocked,
                    "ownership",
                ):
                    invoke()

    def test_successful_retry_rechecks_campaign_completion_after_deleting_pending_work(self):
        pending_doc = FakeDoc("thread-final", {
            "threadId": "thread-final",
            "msgId": "message-final",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi Ryan,\n\nThank you for letting me know.",
            "clientId": "client-1",
            "attempts": 0,
        })
        fake_fs = FakeFirestore([pending_doc])
        fake_fs.collections["threads"].document("thread-final").get()._data[
            "clientId"
        ] = "client-1"
        fake_fs.collections["clients"] = FakeCollection([
            FakeDoc("client-1", {"status": "live"}),
        ])

        def accepted_send(**kwargs):
            exact_sent_evidence = self._prepare_and_accept_capability(
                kwargs["graph_send_capability"]
            )
            processing._set_reply_send_outcome(
                outcome="sent_indexed",
                exact_sent_evidence=exact_sent_evidence,
            )
            return True

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", side_effect=accepted_send
        ), patch.object(
            processing, "_maybe_mark_client_completed", return_value=True
        ) as mark_client_completed:
            states = pending_responses.process_pending_responses(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertTrue(pending_doc.reference.deleted)
        self.assertEqual("healthy", states[0]["status"])
        self.assertEqual(2, len(states))
        self.assertEqual("healthy", states[1]["status"])
        mark_client_completed.assert_called_once_with("uid-1", "client-1")

    def test_failed_send_without_local_outcome_keeps_current_retry(self):
        active_doc = FakeDoc("thread-active", {
            "threadId": "thread-active",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nCould you confirm the asking rent?",
            "clientId": "client-1",
            "attempts": 0,
        })
        fake_fs = FakeFirestore([active_doc])
        def fake_send_reply_in_thread(**kwargs):
            self._mark_capability_definitely_unsent(
                kwargs["graph_send_capability"]
            )
            processing._set_reply_send_outcome(
                error="send_reply_in_thread returned False",
                outcome="send_failed",
            )
            return False

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", new=fake_send_reply_in_thread
        ), patch.object(
            pending_responses, "find_matching_sent_message_for_retry", return_value=None
        ):
            states = pending_responses.process_pending_responses(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertFalse(active_doc.reference.deleted)
        self.assertEqual([], fake_fs.collections["deadLetterQueue"].add_calls)
        self.assertEqual(1, active_doc.reference.update_calls[-1]["attempts"])
        self.assertEqual("send_reply_in_thread returned False", states[0]["error"])

    def test_reply_campaign_suppression_outcome_is_isolated_per_execution_context(self):
        terminal = CampaignAutomationDecision(
            state="blocked",
            reason="campaign_stopped",
            client_data={},
            metadata={"terminal": True},
        )
        maintenance = CampaignAutomationDecision(
            state="blocked",
            reason="campaign_maintenance",
            client_data={},
            metadata={"terminal": False},
        )
        terminal_context = copy_context()
        maintenance_context = copy_context()

        terminal_context.run(processing._set_reply_campaign_suppression, terminal)
        maintenance_context.run(processing._set_reply_campaign_suppression, maintenance)

        self.assertEqual(
            "terminal",
            terminal_context.run(processing._get_reply_campaign_suppression)[0],
        )
        self.assertEqual(
            "maintenance",
            maintenance_context.run(processing._get_reply_campaign_suppression)[0],
        )

    def test_pending_suppression_ignores_stale_shared_send_attributes(self):
        token = processing._REPLY_SEND_OUTCOME.set(processing.ReplySendOutcome())
        self.addCleanup(processing._REPLY_SEND_OUTCOME.reset, token)
        stale_decision = CampaignAutomationDecision(
            state="blocked",
            reason="other_campaign_stopped",
            client_data={},
            metadata={"terminal": True},
        )

        with patch.object(
            processing.send_reply_in_thread,
            "last_outcome",
            "blocked_campaign_terminal",
            create=True,
        ), patch.object(
            processing.send_reply_in_thread,
            "last_campaign_decision",
            stale_decision,
            create=True,
        ):
            kind, decision = pending_responses._get_local_campaign_suppression()

        self.assertIsNone(kind)
        self.assertIsNone(decision)

    def setUp(self):
        self._campaign_decision_patch = patch.object(
            pending_responses,
            "get_client_automation_decision",
            return_value=CampaignAutomationDecision(
                state="allow",
                reason="",
                client_data={
                    "status": "live",
                    "columnConfig": get_default_column_config(),
                },
                metadata={"terminal": False, "stopKind": "none"},
            ),
            create=True,
        )
        self.campaign_decision = self._campaign_decision_patch.start()
        self.addCleanup(self._campaign_decision_patch.stop)

    def _mock_clients_module(self, fake_fs):
        return patch.dict(
            sys.modules,
            {"email_automation.clients": types.SimpleNamespace(_fs=fake_fs)},
        )

    def _dead_letter_payloads(self, fake_fs):
        collection = fake_fs.collections["deadLetterQueue"]
        deterministic = [
            ref.get().to_dict()
            for ref in collection._missing_refs.values()
            if ref.get().exists
        ]
        return [*collection.add_calls, *deterministic]

    def _prepare_and_accept_capability(self, capability):
        permit = send_permits.read_permit(capability)
        draft_id = f"draft-{capability.permit_id}"
        subject = "RE: Pending response test subject"
        html_body = "<p>Pending reply accepted by Graph.</p>"
        recipient = permit["recipient"]
        send_permits.begin_graph_draft_creation(
            capability,
            permit["sourceGraphMessageId"],
        )
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
            source_graph_message_id=permit["sourceGraphMessageId"],
            draft_id=draft_id,
            subject=subject,
            html_body=html_body,
            to_recipients=[recipient],
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
        send_permits.consume_graph_send_capability(
            capability,
            source_graph_message_id=permit["sourceGraphMessageId"],
            draft_id=draft_id,
            subject=subject,
            html_body=html_body,
            to_recipients=[recipient],
            cc_recipients=[],
            attachments=[],
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "accepted",
            evidence={"httpStatus": 202, "phase": "send"},
        )
        retained = send_permits.read_permit(capability)
        return {
            "isDraft": False,
            "subject": retained["preparedEnvelope"]["subject"],
            "sentMessageId": draft_id,
            "recipient": retained["recipient"],
            "bodyHash": retained["bodyHash"],
            "conversationId": retained.get("conversationId"),
            "sentDateTime": retained["requestStartedAt"]
            + timedelta(seconds=1),
            "permitId": retained["permitId"],
            "sourceGraphMessageId": retained["sourceGraphMessageId"],
            "preparedEnvelopeHash": prepared["preparedEnvelopeHash"],
            "toRecipients": [
                {"emailAddress": {"address": recipient}},
            ],
            "ccRecipients": [],
            "bccRecipients": [],
            "body": {"contentType": "HTML", "content": html_body},
            "attachments": [],
        }

    def _mark_capability_definitely_unsent(self, capability):
        send_permits.resolve_graph_send_permit(
            capability,
            "definitely_not_sent",
            evidence={"reason": "mocked pre-send failure", "phase": "preflight"},
        )

    def test_pending_send_claim_fails_closed_for_owned_malformed_lease(self):
        active_doc = FakeDoc("thread-owned-malformed", {
            "threadId": "thread-owned-malformed",
            "msgId": "message-1",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Thanks for the update.",
            "clientId": "client-1",
            "attempts": 0,
            "processingBy": "other-pending-worker",
        })
        fake_fs = FakeFirestore([active_doc])

        with self._mock_clients_module(fake_fs):
            claim = pending_responses._claim_pending_response_for_send(
                "uid-1",
                active_doc,
                active_doc.to_dict(),
            )

        self.assertIsNone(claim)
        self.assertEqual([], active_doc.reference.update_calls)

    def test_expired_accepted_permit_reconciles_sent_without_second_send(self):
        active_doc = FakeDoc("thread-expired-accepted", {
            "threadId": "thread-expired-accepted",
            "msgId": "message-expired-accepted",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Thank you for the update.",
            "clientId": "client-1",
            "conversationId": "conversation-expired-accepted",
            "attempts": 0,
            "status": "sending",
            "processingBy": "pending-worker-a",
            "processingLeaseUntil": datetime.now(timezone.utc) + timedelta(minutes=5),
        })
        fake_fs = FakeFirestore([active_doc])
        thread_ref = fake_fs.collections["threads"].document(active_doc.id)

        with self._mock_clients_module(fake_fs):
            capability = send_permits.issue_pending_graph_send_permit(
                fake_fs,
                thread_ref,
                active_doc.reference,
                dict(active_doc.to_dict()),
                "pending-worker-a",
            )
            send_permits.begin_graph_draft_creation(
                capability,
                active_doc.to_dict()["msgId"],
            )
            send_permits.complete_graph_draft_creation(
                capability,
                draft_id="draft-expired-accepted",
                outcome="created",
                evidence={
                    "httpStatus": 201,
                    "phase": "create_reply",
                    "draftId": "draft-expired-accepted",
                },
            )
            prepared = send_permits.begin_graph_draft_patch(
                capability,
                source_graph_message_id=active_doc.to_dict()["msgId"],
                draft_id="draft-expired-accepted",
                subject="RE: Expired accepted pending subject",
                html_body="<p>Thank you for the update.</p>",
                to_recipients=[active_doc.to_dict()["recipient"]],
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
                    "draftId": "draft-expired-accepted",
                    "preparedEnvelopeHash": prepared[
                        "preparedEnvelopeHash"
                    ],
                },
            )
            send_permits.finalize_graph_draft_preparation(
                capability,
                prepared_envelope_hash=prepared["preparedEnvelopeHash"],
            )
            send_permits.consume_graph_send_capability(
                capability,
                source_graph_message_id=active_doc.to_dict()["msgId"],
                draft_id="draft-expired-accepted",
                subject="RE: Expired accepted pending subject",
                html_body="<p>Thank you for the update.</p>",
                to_recipients=[active_doc.to_dict()["recipient"]],
                cc_recipients=[],
                attachments=[],
            )
            send_permits.resolve_graph_send_permit(
                capability,
                "accepted",
                evidence={"httpStatus": 202, "phase": "send"},
            )
            retained = send_permits.read_permit(capability)
            active_doc._data["processingLeaseUntil"] = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            )
            sent_match = {
                "id": "draft-expired-accepted",
                "isDraft": False,
                "subject": "RE: Expired accepted pending subject",
                "internetMessageId": "<sent-expired-accepted@example.test>",
                "conversationId": active_doc.to_dict()["conversationId"],
                "sentDateTime": retained["requestStartedAt"] + timedelta(seconds=1),
                "toRecipients": [
                    {
                        "emailAddress": {
                            "address": active_doc.to_dict()["recipient"],
                        },
                    },
                ],
                "ccRecipients": [],
                "bccRecipients": [],
                "body": {
                    "contentType": "HTML",
                    "content": "<p>Thank you for the update.</p>",
                },
                "attachments": [],
            }

            with patch.object(
                processing,
                "send_reply_in_thread",
                side_effect=AssertionError("takeover must not send"),
            ) as send_reply, patch.object(
            pending_responses,
                "find_exact_sent_message_by_immutable_id",
                return_value=sent_match,
            ):
                states = pending_responses.process_pending_responses(
                    "uid-1",
                    {"Authorization": "Bearer token"},
                )

        self.assertEqual([], states)
        send_reply.assert_not_called()
        self.assertTrue(active_doc.reference.deleted)
        self.assertEqual(
            "settled_sent",
            send_permits.read_permit(capability)["status"],
        )
        completion_refs = list(
            fake_fs.collections[
                send_permits.PENDING_COMPLETION_OBLIGATION_COLLECTION
            ]._missing_refs.values()
        )
        self.assertEqual(1, len(completion_refs))
        completion_snapshot = completion_refs[0].get()
        self.assertTrue(completion_snapshot.exists)
        self.assertEqual("owed", completion_snapshot.to_dict()["status"])
        self.assertEqual(
            capability.permit_id,
            completion_snapshot.to_dict()["immutable"]["permitId"],
        )

    def test_expired_ambiguous_send_uses_server_owned_review_not_dead_letter(self):
        active_doc = FakeDoc("thread-expired-ambiguous", {
            "threadId": "thread-expired-ambiguous",
            "msgId": "message-expired-ambiguous",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Thank you for the update.",
            "clientId": "client-1",
            "conversationId": "conversation-expired-ambiguous",
            "attempts": 0,
            "status": "sending",
            "processingBy": "pending-worker-a",
            "processingLeaseUntil": datetime.now(timezone.utc) - timedelta(seconds=1),
            "graphSendPermitId": "graph-send-ambiguous",
            "graphSendPermitHash": "a" * 64,
        })
        fake_fs = FakeFirestore([active_doc])
        permit = {
            "permitId": "graph-send-ambiguous",
            "immutableHash": "a" * 64,
            "status": "needs_reconciliation",
            "requestStartedAt": datetime.now(timezone.utc) - timedelta(minutes=1),
            "sourceGraphMessageId": active_doc.to_dict()["msgId"],
            "sendPreparedEnvelopeHash": "b" * 64,
        }

        with self._mock_clients_module(fake_fs), patch.object(
            pending_responses,
            "read_expired_pending_graph_send_permit",
            return_value=permit,
        ), patch.object(
            pending_responses,
            "find_exact_sent_message_by_immutable_id",
            return_value=None,
        ), patch.object(
            pending_responses,
            "reconcile_pending_graph_send_permit",
            return_value=True,
        ) as reconcile:
            handled = pending_responses._reconcile_expired_pending_permit(
                "uid-1",
                {"Authorization": "Bearer token"},
                active_doc,
                active_doc.to_dict(),
            )

        self.assertTrue(handled)
        evidence_ref, evidence = reconcile.call_args.kwargs["evidence_document"]
        thread_ref = fake_fs.collections["threads"].document(active_doc.id)
        self.assertIs(
            evidence_ref,
            thread_ref.collection("graphSendReviews").document(
                "pending-graph-send-ambiguous"
            ),
        )
        self.assertIsNone(evidence["alreadySent"])
        self.assertTrue(evidence["sendOutcomeUnknown"])
        self.assertEqual([], fake_fs.collections["deadLetterQueue"].add_calls)

    def test_expired_ambiguous_send_stops_sent_reads_at_exact_cap(self):
        active_doc = FakeDoc("thread-recheck-cap", {
            "threadId": "thread-recheck-cap",
            "msgId": "message-recheck-cap",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Thank you for the update.",
            "clientId": "client-1",
            "conversationId": "conversation-recheck-cap",
            "status": "needs_reconciliation",
            "processingBy": "pending-worker-a",
            "processingLeaseUntil": datetime.now(timezone.utc) - timedelta(seconds=1),
            "graphSendPermitId": "graph-send-recheck-cap",
            "graphSendPermitHash": "a" * 64,
            "graphSendSentRecheckCount": (
                send_permits.PENDING_GRAPH_SENT_RECHECK_LIMIT
            ),
        })
        fake_fs = FakeFirestore([active_doc])
        permit = {
            "permitId": "graph-send-recheck-cap",
            "immutableHash": "a" * 64,
            "status": "needs_reconciliation",
            "requestStartedAt": datetime.now(timezone.utc) - timedelta(minutes=1),
        }

        with self._mock_clients_module(fake_fs), patch.object(
            pending_responses,
            "read_expired_pending_graph_send_permit",
            return_value=permit,
        ), patch.object(
            pending_responses,
            "find_exact_sent_message_by_immutable_id",
        ) as sent_lookup, patch.object(
            pending_responses,
            "reconcile_pending_graph_send_permit",
        ) as reconcile:
            handled = pending_responses._reconcile_expired_pending_permit(
                "uid-1",
                {"Authorization": "Bearer token"},
                active_doc,
                active_doc.to_dict(),
            )

        self.assertTrue(handled)
        sent_lookup.assert_not_called()
        reconcile.assert_not_called()
        self.assertTrue(active_doc.exists)
        self.assertEqual("needs_reconciliation", active_doc.to_dict()["status"])

    def test_operator_ack_fresh_exact_sent_takes_precedence_over_unknown(self):
        request_started_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        review_doc = FakeDoc("review-1", {
            "status": "needs_reconciliation",
            "alreadySent": None,
            "sendOutcomeUnknown": True,
        })
        active_doc = FakeDoc("pending-operator-sent", {
            "threadId": "thread-operator-sent",
            "msgId": "message-operator-sent",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Thank you for the update.",
            "clientId": "client-1",
            "conversationId": "conversation-operator-sent",
            "processingBy": "pending-worker-a",
            "processingLeaseUntil": datetime.now(timezone.utc) - timedelta(seconds=1),
            "graphSendPermitId": "graph-send-operator-sent",
            "graphSendPermitHash": "a" * 64,
            "graphSendReviewEvidenceRef": review_doc.reference,
        })
        fake_fs = FakeFirestore([active_doc])
        permit = {
            "permitId": "graph-send-operator-sent",
            "immutableHash": "a" * 64,
            "pendingReconciliationEvidenceHash": "b" * 64,
            "requestStartedAt": request_started_at,
            "recipient": "bp21harrison@gmail.com",
            "bodyHash": "c" * 64,
            "conversationId": "conversation-operator-sent",
            "sourceGraphMessageId": "message-operator-sent",
            "sendPreparedEnvelopeHash": "d" * 64,
            "preparedEnvelope": {"draftId": "immutable-operator-sent"},
        }
        sent_match = {
            "id": "immutable-operator-sent",
            "conversationId": "conversation-operator-sent",
            "sentDateTime": request_started_at + timedelta(seconds=5),
        }

        with self._mock_clients_module(fake_fs), patch.object(
            pending_responses,
            "read_expired_pending_graph_send_permit",
            return_value=permit,
        ), patch.object(
            pending_responses,
            "find_exact_sent_message_by_immutable_id",
            return_value=sent_match,
        ) as sent_lookup, patch.object(
            pending_responses,
            "reconcile_pending_graph_send_permit",
            return_value=True,
        ) as reconcile, patch.object(
            pending_responses,
            "operator_settle_pending_graph_send_review",
        ) as acknowledge_unknown:
            outcome = pending_responses.acknowledge_pending_graph_send_ambiguity(
                "uid-1",
                active_doc.id,
                headers={"Authorization": "Bearer server-token"},
                expected_permit_id=permit["permitId"],
                expected_permit_hash=permit["immutableHash"],
                expected_reconciliation_evidence_hash=permit[
                    "pendingReconciliationEvidenceHash"
                ],
                operator_id="uid-1",
                operator_reason="Fresh lookup completed.",
                settlement_id="settlement-1",
            )

        self.assertEqual("settled_sent", outcome)
        self.assertEqual(
            permit["preparedEnvelope"]["draftId"],
            sent_lookup.call_args.args[1],
        )
        self.assertEqual("sent", reconcile.call_args.kwargs["outcome"])
        self.assertTrue(
            reconcile.call_args.kwargs["evidence_document"][1]["alreadySent"]
        )
        completion_ref, completion_payload = reconcile.call_args.kwargs[
            "completion_document"
        ]
        self.assertEqual(completion_payload["obligationId"], completion_ref.id)
        self.assertEqual("owed", completion_payload["status"])
        self.assertEqual(
            permit["permitId"],
            completion_payload["immutable"]["permitId"],
        )
        self.assertTrue(
            completion_payload["immutable"]["completeClientAfterReply"]
        )
        audit_ref, audit_payload = reconcile.call_args.kwargs[
            "operator_audit_document"
        ]
        self.assertEqual("settlement-1", audit_ref.id)
        self.assertEqual("settlement-1", audit_payload["settlementId"])
        self.assertEqual(
            "acknowledge_ambiguous_no_retry",
            audit_payload["requestedAction"],
        )
        self.assertEqual("uid-1", audit_payload["operatorId"])
        self.assertEqual(
            "Fresh lookup completed.",
            audit_payload["operatorReason"],
        )
        self.assertEqual("exact_sent", audit_payload["resolution"])
        self.assertTrue(audit_payload["alreadySent"])
        self.assertFalse(audit_payload["retryAllowed"])
        acknowledge_unknown.assert_not_called()

    def test_operator_ack_replays_exact_prior_audit_before_pending_or_mailbox(self):
        fake_fs = FakeFirestore([])
        for stored_status in (
            "settled_sent",
            "settled_ambiguous_no_retry",
        ):
            with self.subTest(status=stored_status), self._mock_clients_module(
                fake_fs
            ), patch.object(
                pending_responses,
                "read_pending_graph_send_operator_settlement_replay",
                return_value=stored_status,
            ) as replay, patch.object(
                pending_responses,
                "find_exact_sent_message_by_immutable_id",
            ) as sent_lookup:
                mailbox = MagicMock(
                    return_value={"Authorization": "Bearer server-token"}
                )
                outcome = (
                    pending_responses.acknowledge_pending_graph_send_ambiguity(
                        "uid-1",
                        "pending-operator-replay",
                        headers_factory=mailbox,
                        expected_permit_id="graph-send-operator-replay",
                        expected_permit_hash="a" * 64,
                        expected_reconciliation_evidence_hash="b" * 64,
                        operator_id="uid-1",
                        operator_reason="Exact same operator request.",
                        settlement_id="settlement-replay-1",
                    )
                )

            self.assertEqual(stored_status, outcome)
            replay.assert_called_once()
            mailbox.assert_not_called()
            sent_lookup.assert_not_called()

    def test_operator_ack_refuses_unknown_settlement_when_sent_lookup_unreadable(self):
        active_doc = FakeDoc("pending-operator-unreadable", {
            "threadId": "thread-operator-unreadable",
            "msgId": "message-operator-unreadable",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Thank you for the update.",
            "clientId": "client-1",
            "conversationId": "conversation-operator-unreadable",
            "processingBy": "pending-worker-a",
            "processingLeaseUntil": datetime.now(timezone.utc) - timedelta(seconds=1),
            "graphSendPermitId": "graph-send-operator-unreadable",
            "graphSendPermitHash": "a" * 64,
            "graphSendReviewEvidenceRef": FakeDoc("review-unreadable", {}).reference,
        })
        fake_fs = FakeFirestore([active_doc])
        permit = {
            "permitId": "graph-send-operator-unreadable",
            "immutableHash": "a" * 64,
            "pendingReconciliationEvidenceHash": "b" * 64,
            "requestStartedAt": datetime.now(timezone.utc) - timedelta(minutes=1),
        }

        with self._mock_clients_module(fake_fs), patch.object(
            pending_responses,
            "read_expired_pending_graph_send_permit",
            return_value=permit,
        ), patch.object(
            pending_responses,
            "find_exact_sent_message_by_immutable_id",
            side_effect=pending_responses.SentMailGuardLookupError(
                "mailbox unavailable"
            ),
        ), patch.object(
            pending_responses,
            "operator_settle_pending_graph_send_review",
        ) as settle, self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "fresh readable Sent Items",
        ):
            pending_responses.acknowledge_pending_graph_send_ambiguity(
                "uid-1",
                active_doc.id,
                headers={"Authorization": "Bearer server-token"},
                expected_permit_id=permit["permitId"],
                expected_permit_hash=permit["immutableHash"],
                expected_reconciliation_evidence_hash=permit[
                    "pendingReconciliationEvidenceHash"
                ],
                operator_id="uid-1",
                operator_reason="Fresh lookup completed.",
                settlement_id="settlement-1",
            )
        settle.assert_not_called()

    def test_each_partial_terminal_marker_blocks_early_claim_before_graph(self):
        marker_cases = (
            ("terminal_saga_key", {"terminalSagaKey": "partial-saga-key"}),
            ("terminal_reply_owed", {"terminalReplyOwed": True}),
            ("terminal_notification_owed", {"terminalNotificationOwed": True}),
            ("pending_terminal_reason", {"pendingTerminalReason": "partial"}),
            ("terminal_saga_dict", {"terminalSaga": {}}),
            ("terminal_saga_claim_dict", {"terminalSagaClaim": {}}),
            ("terminal_reply_attempt_dict", {"terminalReplyAttempt": {}}),
        )
        for label, marker in marker_cases:
            with self.subTest(marker=label):
                active_doc = FakeDoc(f"thread-early-{label}", {
                    "threadId": f"thread-early-{label}",
                    "msgId": f"message-early-{label}",
                    "recipient": "bp21harrison@gmail.com",
                    "responseBody": "Thank you for the update.",
                    "clientId": "client-1",
                    "attempts": 0,
                })
                fake_fs = FakeFirestore([active_doc])
                fake_fs.collections["threads"].docs[0]._data.update(marker)

                with self._mock_clients_module(fake_fs), patch.object(
                    processing,
                    "send_reply_in_thread",
                    return_value=True,
                ) as send_reply:
                    states = pending_responses.process_pending_responses(
                        "uid-1",
                        {"Authorization": "Bearer token"},
                    )

                self.assertEqual([], states)
                send_reply.assert_not_called()
                self.assertFalse(active_doc.reference.deleted)
                self.assertIsNone(active_doc.to_dict().get("processingBy"))
                self.assertEqual([], active_doc.reference.update_calls)

    def test_each_partial_terminal_marker_after_claim_blocks_final_graph_fence(self):
        marker_cases = (
            ("terminal_saga_key", {"terminalSagaKey": "partial-saga-key"}),
            ("terminal_reply_owed", {"terminalReplyOwed": True}),
            ("terminal_notification_owed", {"terminalNotificationOwed": True}),
            ("pending_terminal_reason", {"pendingTerminalReason": "partial"}),
            ("terminal_saga_dict", {"terminalSaga": {}}),
            ("terminal_saga_claim_dict", {"terminalSagaClaim": {}}),
            ("terminal_reply_attempt_dict", {"terminalReplyAttempt": {}}),
        )
        allow_decision = self.campaign_decision.return_value
        for label, marker in marker_cases:
            with self.subTest(marker=label):
                active_doc = FakeDoc(f"thread-final-{label}", {
                    "threadId": f"thread-final-{label}",
                    "msgId": f"message-final-{label}",
                    "recipient": "bp21harrison@gmail.com",
                    "responseBody": "Thank you for the update.",
                    "clientId": "client-1",
                    "attempts": 0,
                })
                fake_fs = FakeFirestore([active_doc])
                campaign_gate_entered = Event()
                terminal_marker_committed = Event()

                def pause_after_early_claim(*_args, **_kwargs):
                    campaign_gate_entered.set()
                    if not terminal_marker_committed.wait(timeout=5):
                        raise AssertionError("terminal marker barrier timed out")
                    return allow_decision

                self.campaign_decision.side_effect = pause_after_early_claim
                try:
                    with self._mock_clients_module(fake_fs), patch.object(
                        processing,
                        "send_reply_in_thread",
                        return_value=True,
                    ) as send_reply:
                        with ThreadPoolExecutor(max_workers=1) as executor:
                            future = executor.submit(
                                pending_responses.process_pending_responses,
                                "uid-1",
                                {"Authorization": "Bearer token"},
                            )
                            self.assertTrue(
                                campaign_gate_entered.wait(timeout=5),
                                "pending worker did not complete its early claim",
                            )
                            self.assertTrue(active_doc.to_dict().get("processingBy"))
                            fake_fs.collections["threads"].docs[0]._data.update(marker)
                            terminal_marker_committed.set()
                            states = future.result(timeout=5)
                finally:
                    self.campaign_decision.side_effect = None

                self.assertEqual([], states)
                send_reply.assert_not_called()
                self.assertFalse(active_doc.reference.deleted)
                self.assertIsNone(active_doc.to_dict().get("processingBy"))
                self.assertIsNone(active_doc.to_dict().get("processingLeaseUntil"))

    def test_terminal_stage_after_early_claim_blocks_final_graph_send(self):
        active_doc = FakeDoc("thread-terminal-race", {
            "threadId": "thread-terminal-race",
            "msgId": "message-terminal-race",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Thank you for the update.",
            "clientId": "client-1",
            "attempts": 0,
        })
        fake_fs = FakeFirestore([active_doc])
        campaign_gate_entered = Event()
        terminal_stage_committed = Event()
        allow_decision = self.campaign_decision.return_value

        def pause_after_early_claim(*_args, **_kwargs):
            campaign_gate_entered.set()
            if not terminal_stage_committed.wait(timeout=5):
                raise AssertionError("terminal stage barrier timed out")
            return allow_decision

        self.campaign_decision.side_effect = pause_after_early_claim

        with self._mock_clients_module(fake_fs), patch.object(
            processing,
            "send_reply_in_thread",
            return_value=True,
        ) as send_reply:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    pending_responses.process_pending_responses,
                    "uid-1",
                    {"Authorization": "Bearer token"},
                )
                self.assertTrue(
                    campaign_gate_entered.wait(timeout=5),
                    "pending worker did not complete its early claim",
                )
                self.assertTrue(active_doc.to_dict().get("processingBy"))
                thread_doc = fake_fs.collections["threads"].docs[0]
                thread_doc._data.update({
                    "terminalSagaKey": "terminal-saga-won-race",
                    "terminalReplyOwed": True,
                })
                terminal_stage_committed.set()
                states = future.result(timeout=5)

        self.assertEqual([], states)
        send_reply.assert_not_called()
        self.assertFalse(active_doc.reference.deleted)
        self.assertIsNone(active_doc.to_dict().get("processingBy"))

    def test_max_attempt_pending_response_moves_to_dead_letter_queue(self):
        stale_doc = FakeDoc("thread-stale", {
            "threadId": "thread-stale",
            "msgId": "message-1",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nThanks",
            "clientId": "client-1",
            "attempts": pending_responses.MAX_RESPONSE_ATTEMPTS,
            "lastError": "Graph failed repeatedly",
        })
        active_doc = FakeDoc("thread-active", {
            "threadId": "thread-active",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nCould you confirm the asking rent?",
            "clientId": "client-1",
            "attempts": 1,
            "lastError": "Temporary failure",
        })
        fake_fs = FakeFirestore([stale_doc, active_doc])

        with self._mock_clients_module(fake_fs):
            valid = pending_responses.get_pending_responses("uid-1")

        self.assertEqual([item["doc"].id for item in valid], ["thread-active"])
        self.assertTrue(stale_doc.reference.deleted)
        dead_letter = self._dead_letter_payloads(fake_fs)[-1]
        self.assertEqual(dead_letter["source"], "pendingResponses")
        self.assertEqual(dead_letter["originalDocId"], "thread-stale")
        self.assertEqual(dead_letter["threadId"], "thread-stale")
        self.assertEqual(dead_letter["recipient"], "bp21harrison@gmail.com")
        self.assertEqual(dead_letter["failureReason"], "Graph failed repeatedly")

    def test_failed_retry_preserves_detailed_send_error_when_available(self):
        active_doc = FakeDoc("thread-active", {
            "threadId": "thread-active",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nCould you confirm the asking rent?",
            "clientId": "client-1",
            "attempts": 1,
            "lastError": "Temporary failure",
        })
        fake_fs = FakeFirestore([active_doc])

        def fake_send_reply_in_thread(**kwargs):
            self._mark_capability_definitely_unsent(
                kwargs["graph_send_capability"]
            )
            processing._set_reply_send_outcome(
                error="HTTP 429 rate limited after 3 attempts",
                outcome="send_failed",
            )
            return False

        fake_send_reply_in_thread.last_error = "HTTP 429 rate limited after 3 attempts"

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", new=fake_send_reply_in_thread
        ), patch.object(pending_responses, "find_matching_sent_message_for_retry", return_value=None):
            sent = pending_responses.process_pending_responses("uid-1", {"Authorization": "Bearer token"})

        # A swallowed per-item Graph send failure must surface exactly one
        # "error" op-state to the health rail (not merely "no healthy state").
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["status"], "error")
        self.assertEqual(sent[0]["operation"], "pending_response_send")
        self.assertEqual(sent[0]["recipient"], "bp21harrison@gmail.com")
        self.assertEqual(sent[0]["error"], "HTTP 429 rate limited after 3 attempts")
        retry_payload = active_doc.reference.update_calls[-1]
        self.assertEqual(retry_payload["attempts"], 2)
        self.assertEqual(retry_payload["lastError"], "HTTP 429 rate limited after 3 attempts")

    def test_unsafe_pending_response_moves_to_dead_letter_without_sending(self):
        active_doc = FakeDoc("thread-active", {
            "threadId": "thread-active",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi [NAME],\n\nCan you confirm the availability?",
            "clientId": "client-1",
            "attempts": 0,
        })
        fake_fs = FakeFirestore([active_doc])

        def fake_send_reply_in_thread(**_kwargs):
            raise AssertionError("unsafe pending response should stop before Graph send")

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", new=fake_send_reply_in_thread
        ):
            sent = pending_responses.process_pending_responses("uid-1", {"Authorization": "Bearer token"})

        # Handled outcome (dead-letter / reconciliation): no send was attempted,
        # so no op-state escalates the health rail. Assert the exact empty shape.
        self.assertEqual(sent, [])
        self.assertTrue(active_doc.reference.deleted)
        self.assertEqual(1, len(active_doc.reference.update_calls))
        self.assertEqual("sending", active_doc.reference.update_calls[0]["status"])
        self.assertTrue(active_doc.reference.update_calls[0]["processingBy"])
        dead_letter = self._dead_letter_payloads(fake_fs)[-1]
        self.assertEqual(dead_letter["source"], "pendingResponses")
        self.assertEqual(dead_letter["originalDocId"], "thread-active")
        self.assertIn("Unresolved outbound placeholder", dead_letter["failureReason"])
        self.assertIn("manual review", dead_letter["failureReason"])

    def test_note_request_pending_response_moves_to_manual_review_before_graph(self):
        active_doc = FakeDoc("thread-note-request", {
            "threadId": "thread-note-request",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "What about the brochure?",
            "clientId": "client-1",
            "attempts": 0,
        })
        fake_fs = FakeFirestore([active_doc])

        def fail_send(**_kwargs):
            raise AssertionError("Note request must stop before Graph retry")

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", new=fail_send
        ):
            states = pending_responses.process_pending_responses(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertEqual([], states)
        self.assertTrue(active_doc.reference.deleted)
        self.campaign_decision.assert_called_once_with("uid-1", "client-1")
        dead_letter = self._dead_letter_payloads(fake_fs)[-1]
        self.assertIn("non-requestable", dead_letter["failureReason"])
        self.assertIn("manual review", dead_letter["failureReason"])

    def test_invalid_config_pending_response_moves_to_manual_review_before_graph(self):
        active_doc = FakeDoc("thread-invalid-config", {
            "threadId": "thread-invalid-config",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Could you confirm the asking rent?",
            "clientId": "client-1",
            "attempts": 0,
        })
        fake_fs = FakeFirestore([active_doc])
        self.campaign_decision.return_value = CampaignAutomationDecision(
            state="allow",
            reason="",
            client_data={"status": "live", "columnConfig": {"mappings": {}}},
            metadata={"terminal": False, "stopKind": "none"},
        )

        def fail_send(**_kwargs):
            raise AssertionError("Invalid config must stop before Graph retry")

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", new=fail_send
        ):
            states = pending_responses.process_pending_responses(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertEqual([], states)
        self.assertTrue(active_doc.reference.deleted)
        self.campaign_decision.assert_called_once_with("uid-1", "client-1")
        dead_letter = self._dead_letter_payloads(fake_fs)[-1]
        self.assertIn("invalid persisted columnConfig", dead_letter["failureReason"])
        self.assertIn("manual review", dead_letter["failureReason"])

    def test_maintenance_pause_preserves_pending_response_without_sending(self):
        active_doc = FakeDoc("thread-maintenance", {
            "threadId": "thread-maintenance",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nCould you confirm the asking rent?",
            "clientId": "client-1",
            "attempts": 2,
            "lastError": "Temporary failure",
        })
        fake_fs = FakeFirestore([active_doc])
        self.campaign_decision.return_value = CampaignAutomationDecision(
            state="blocked",
            reason="campaign_maintenance",
            client_data={"status": "live", "automationPaused": True},
            metadata={"terminal": False, "stopKind": "maintenance_pause"},
        )

        def fail_send(**_kwargs):
            raise AssertionError("maintenance-paused pending response must not send")

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", new=fail_send
        ):
            states = pending_responses.process_pending_responses(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertEqual([], states)
        self.assertFalse(active_doc.reference.deleted)
        self.assertEqual([], fake_fs.collections["deadLetterQueue"].add_calls)
        payload = active_doc.reference.update_calls[-1]
        self.assertEqual("queued", payload["status"])
        self.assertEqual("blocked", payload["automationSuppressedState"])
        self.assertEqual(2, active_doc.to_dict()["attempts"])

    def test_unknown_campaign_state_preserves_pending_response_without_sending(self):
        active_doc = FakeDoc("thread-unknown", {
            "threadId": "thread-unknown",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nCould you confirm the asking rent?",
            "clientId": "client-1",
            "attempts": pending_responses.MAX_RESPONSE_ATTEMPTS,
        })
        fake_fs = FakeFirestore([active_doc])
        self.campaign_decision.return_value = CampaignAutomationDecision(
            state="unknown",
            reason="client_automation_state_read_error",
            client_data={},
            metadata={"terminal": False, "stopKind": "none"},
        )

        def fail_send(**_kwargs):
            raise AssertionError("unknown campaign state must not send")

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", new=fail_send
        ):
            states = pending_responses.process_pending_responses(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertEqual([], states)
        self.assertFalse(active_doc.reference.deleted)
        self.assertEqual([], fake_fs.collections["deadLetterQueue"].add_calls)
        payload = active_doc.reference.update_calls[-1]
        self.assertEqual("queued", payload["status"])
        self.assertEqual("unknown", payload["automationSuppressedState"])
        self.assertEqual(
            "client_automation_state_read_error",
            payload["automationSuppressedReason"],
        )
        self.assertIsNone(payload["processingBy"])
        self.assertIsNone(payload["processingAt"])
        self.assertNotIn("attempts", payload)

    def test_sent_but_unindexed_retry_moves_to_reconciliation_without_resending(self):
        active_doc = FakeDoc("thread-active", {
            "threadId": "thread-active",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nCould you confirm the asking rent?",
            "clientId": "client-1",
            "attempts": 1,
            "lastError": "Temporary failure",
        })
        fake_fs = FakeFirestore([active_doc])

        def fake_send_reply_in_thread(**kwargs):
            self._prepare_and_accept_capability(
                kwargs["graph_send_capability"]
            )
            processing._set_reply_send_outcome(
                error="Graph accepted reply but Sent Items lookup failed",
                sent_but_unindexed=True,
                outcome="sent_but_unindexed",
            )
            return False

        fake_send_reply_in_thread.last_error = "Graph accepted reply but Sent Items lookup failed"
        fake_send_reply_in_thread.sent_but_unindexed = True

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", new=fake_send_reply_in_thread
        ), patch.object(pending_responses, "find_matching_sent_message_for_retry", return_value=None):
            sent = pending_responses.process_pending_responses("uid-1", {"Authorization": "Bearer token"})

        # Accepted without immutable Sent evidence is tri-state ambiguity.  It
        # retains the exact issuer/permit and never authorizes another send.
        self.assertEqual(sent, [])
        self.assertEqual(3, len(active_doc.reference.update_calls))
        self.assertEqual(
            "sending",
            active_doc.reference.update_calls[0]["status"],
        )
        self.assertNotIn("attempts", active_doc.reference.update_calls[0])
        self.assertIn(
            "processingLeaseUntil",
            active_doc.reference.update_calls[1],
        )
        self.assertNotIn("attempts", active_doc.reference.update_calls[1])
        self.assertFalse(active_doc.reference.deleted)
        self.assertEqual("needs_reconciliation", active_doc.to_dict()["status"])
        self.assertEqual([], self._dead_letter_payloads(fake_fs))
        thread_ref = fake_fs.collections["threads"].document("thread-active")
        reviews = thread_ref.collection("graphSendReviews")._missing_refs
        self.assertEqual(1, len(reviews))
        review = next(iter(reviews.values())).get().to_dict()
        self.assertIsNone(review["alreadySent"])
        self.assertTrue(review["sendOutcomeUnknown"])
        self.assertFalse(review["retryAllowed"])

    def test_retry_with_matching_sent_item_moves_to_reconciliation_without_resending(self):
        active_doc = FakeDoc("thread-active", {
            "threadId": "thread-active",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nCould you confirm the asking rent?",
            "clientId": "client-1",
            "attempts": 1,
            "lastError": "Read timed out after Graph reply",
            "lastSendAttemptAt": "2026-06-26T12:00:00Z",
            "subject": "0 Gemini Ave, Houston",
            "conversationId": "conv-1",
        })
        fake_fs = FakeFirestore([active_doc])
        sent_match = {
            "id": "sent-reply-1",
            "internetMessageId": "<sent-reply-1@example.com>",
            "conversationId": "conversation-1",
            "subject": "RE: 0 Gemini Ave",
        }

        def fake_send_reply_in_thread(**_kwargs):
            raise AssertionError("retry guard should stop before resending")

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", new=fake_send_reply_in_thread
        ), patch.object(
            pending_responses,
            "find_matching_sent_message_for_retry",
            return_value=sent_match,
            create=True,
        ) as sent_guard:
            sent = pending_responses.process_pending_responses("uid-1", {"Authorization": "Bearer token"})

        # Handled outcome (dead-letter / reconciliation): no send was attempted,
        # so no op-state escalates the health rail. Assert the exact empty shape.
        self.assertEqual(sent, [])
        sent_guard.assert_called_once()
        self.assertEqual(sent_guard.call_args.kwargs["subject"], "0 Gemini Ave, Houston")
        self.assertEqual(sent_guard.call_args.kwargs["conversation_id"], "conv-1")
        self.assertEqual(1, len(active_doc.reference.update_calls))
        self.assertEqual("sending", active_doc.reference.update_calls[0]["status"])
        self.assertTrue(active_doc.reference.update_calls[0]["processingBy"])
        self.assertTrue(active_doc.reference.deleted)
        dead_letter = self._dead_letter_payloads(fake_fs)[-1]
        self.assertEqual(dead_letter["source"], "pendingResponses")
        self.assertEqual(dead_letter["status"], "needs_reconciliation")
        self.assertTrue(dead_letter["alreadySent"])
        self.assertEqual(dead_letter["sentMessageId"], "sent-reply-1")
        self.assertEqual(dead_letter["internetMessageId"], "<sent-reply-1@example.com>")

    def test_retry_blocks_when_conversation_was_manually_continued(self):
        active_doc = FakeDoc("thread-active", {
            "threadId": "thread-active",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nCould you confirm the asking rent?",
            "clientId": "client-1",
            "attempts": 1,
            "lastError": "Read timed out after Graph reply",
            "lastSendAttemptAt": "2026-06-26T12:00:00Z",
            "subject": "0 Gemini Ave, Houston",
            "conversationId": "conv-1",
        })
        fake_fs = FakeFirestore([active_doc])
        manual_continuation = {
            "id": "manual-sent-1",
            "internetMessageId": "<manual-sent-1@example.com>",
            "conversationId": "conv-1",
            "sentDateTime": "2026-06-26T12:04:00Z",
        }

        def fake_send_reply_in_thread(**_kwargs):
            raise AssertionError("manual continuation guard should stop before resending")

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", new=fake_send_reply_in_thread
        ), patch.object(
            pending_responses,
            "find_matching_sent_message_for_retry",
            return_value=None,
            create=True,
        ), patch.object(
            pending_responses,
            "find_sent_conversation_continuation_for_retry",
            return_value=manual_continuation,
            create=True,
        ) as continuation_guard:
            sent = pending_responses.process_pending_responses("uid-1", {"Authorization": "Bearer token"})

        # Handled outcome (dead-letter / reconciliation): no send was attempted,
        # so no op-state escalates the health rail. Assert the exact empty shape.
        self.assertEqual(sent, [])
        continuation_guard.assert_called_once()
        self.assertEqual(continuation_guard.call_args.kwargs["conversation_id"], "conv-1")
        self.assertTrue(active_doc.reference.deleted)
        self.assertEqual(1, len(active_doc.reference.update_calls))
        self.assertEqual("sending", active_doc.reference.update_calls[0]["status"])
        self.assertTrue(active_doc.reference.update_calls[0]["processingBy"])
        dead_letter = self._dead_letter_payloads(fake_fs)[-1]
        self.assertEqual(dead_letter["source"], "pendingResponses")
        self.assertIn("manually continued", dead_letter["failureReason"])

    def test_retry_guard_lookup_failure_dead_letters_without_resending(self):
        active_doc = FakeDoc("thread-active", {
            "threadId": "thread-active",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nCould you confirm the asking rent?",
            "clientId": "client-1",
            "attempts": 1,
            "lastError": "Read timed out after Graph reply",
            "lastSendAttemptAt": "2026-06-26T12:00:00Z",
        })
        fake_fs = FakeFirestore([active_doc])

        def fake_send_reply_in_thread(**_kwargs):
            raise AssertionError("retry guard should stop before resending")

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", new=fake_send_reply_in_thread
        ), patch.object(
            pending_responses,
            "find_matching_sent_message_for_retry",
            side_effect=pending_responses.SentMailGuardLookupError("Graph 401"),
        ):
            sent = pending_responses.process_pending_responses("uid-1", {"Authorization": "Bearer token"})

        # Handled outcome (dead-letter / reconciliation): no send was attempted,
        # so no op-state escalates the health rail. Assert the exact empty shape.
        self.assertEqual(sent, [])
        self.assertTrue(active_doc.reference.deleted)
        self.assertEqual(1, len(active_doc.reference.update_calls))
        self.assertEqual("sending", active_doc.reference.update_calls[0]["status"])
        self.assertTrue(active_doc.reference.update_calls[0]["processingBy"])
        dead_letter = self._dead_letter_payloads(fake_fs)[-1]
        self.assertEqual(dead_letter["source"], "pendingResponses")
        self.assertIn("Sent Items retry guard could not verify prior send", dead_letter["failureReason"])

    def test_enforced_enqueue_rejects_same_thread_source_overwrite(self):
        fake_fs = FakeFirestore([])
        source_a = "source-A"
        work_a = "1" * 64
        proposal_a = "2" * 64
        selection_a = "3" * 64

        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"},
        ), self._mock_clients_module(fake_fs):
            queued = pending_responses.queue_pending_response(
                "uid-1",
                "thread-shared",
                "message-A",
                "broker@example.com",
                "Source A body",
                "client-1",
                error="definitely unsent",
                canonical_source_id=source_a,
                work_key=work_a,
                proposal_hash=proposal_a,
                selection_hash=selection_a,
            )
            before = dict(
                fake_fs.collections["pendingResponses"]
                .document("thread-shared")
                .get()
                .to_dict()
            )

            with self.assertRaises(pending_responses.PendingResponseConflict):
                pending_responses.queue_pending_response(
                    "uid-1",
                    "thread-shared",
                    "message-B",
                    "broker@example.com",
                    "Source B body",
                    "client-1",
                    error="definitely unsent",
                    canonical_source_id="source-B",
                    work_key="4" * 64,
                    proposal_hash="5" * 64,
                    selection_hash="6" * 64,
                )

        after = (
            fake_fs.collections["pendingResponses"]
            .document("thread-shared")
            .get()
            .to_dict()
        )
        self.assertIsInstance(queued, pending_responses.PendingResponseRecord)
        self.assertEqual(source_a, queued.canonical_source_id)
        self.assertEqual(1, queued.pending_revision)
        self.assertEqual(before, after)
        self.assertEqual("Source A body", after["responseBody"])
        self.assertEqual(source_a, after["canonicalSourceId"])
        self.assertEqual(work_a, after["workKey"])
        envelope_hash = send_permits.pending_envelope_hash(after)
        source_binding = (
            fake_fs.collections["pendingResponseSourceBindings"]
            .document(f"pending-source-{envelope_hash}")
            .get()
        )
        self.assertTrue(source_binding.exists)
        self.assertEqual(
            source_a,
            source_binding.to_dict()["canonicalSourceId"],
        )

    def test_enforced_enqueue_requires_both_hashes_before_firestore_access(self):
        exact = {
            "canonical_source_id": "source-A",
            "work_key": "1" * 64,
            "proposal_hash": "2" * 64,
            "selection_hash": "3" * 64,
        }

        for omitted in ("proposal_hash", "selection_hash"):
            with self.subTest(omitted=omitted):
                fake_fs = MagicMock(name=f"firestore_without_{omitted}")
                arguments = {**exact}
                arguments.pop(omitted)
                with patch.dict(
                    os.environ,
                    {"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"},
                ), self._mock_clients_module(fake_fs), self.assertRaises(
                    pending_responses.PendingResponseConflict
                ):
                    pending_responses.queue_pending_response(
                        "uid-1",
                        "thread-A",
                        "message-A",
                        "broker@example.com",
                        "Source A body",
                        "client-1",
                        error="definitely unsent",
                        **arguments,
                    )

                fake_fs.collection.assert_not_called()

    def test_enforced_sent_unindexed_record_requires_and_persists_binding(self):
        binding = {
            "canonical_source_id": "source-A",
            "work_key": "1" * 64,
            "proposal_hash": "2" * 64,
            "selection_hash": "3" * 64,
        }
        inaccessible_fs = MagicMock(name="unbound_reconciliation_firestore")
        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"},
        ), self._mock_clients_module(inaccessible_fs), self.assertRaises(
            pending_responses.PendingResponseConflict
        ):
            pending_responses.record_sent_unindexed_response(
                "uid-1",
                "thread-A",
                "message-A",
                "broker@example.com",
                "Source A body",
                "client-1",
                "Sent indexing failed",
            )
        inaccessible_fs.collection.assert_not_called()

        fake_fs = FakeFirestore([])
        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"},
        ), self._mock_clients_module(fake_fs):
            pending_responses.record_sent_unindexed_response(
                "uid-1",
                "thread-A",
                "message-A",
                "broker@example.com",
                "Source A body",
                "client-1",
                "Sent indexing failed",
                **binding,
            )

        payload = fake_fs.collections["deadLetterQueue"].add_calls[-1]
        self.assertEqual("source-A", payload["canonicalSourceId"])
        self.assertEqual("1" * 64, payload["workKey"])
        self.assertEqual("2" * 64, payload["proposalHash"])
        self.assertEqual("3" * 64, payload["selectionHash"])

    def test_enforced_exact_source_retry_validates_every_binding(self):
        fake_fs = FakeFirestore([])
        exact = {
            "canonical_source_id": "source-A",
            "work_key": "1" * 64,
            "proposal_hash": "2" * 64,
            "selection_hash": "3" * 64,
        }

        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"},
        ), self._mock_clients_module(fake_fs):
            first = pending_responses.queue_pending_response(
                "uid-1",
                "thread-A",
                "message-A",
                "broker@example.com",
                "Source A body",
                "client-1",
                error="first failure",
                **exact,
            )
            retried = pending_responses.queue_pending_response(
                "uid-1",
                "thread-A",
                "message-A",
                "broker@example.com",
                "Source A body",
                "client-1",
                error="second failure",
                **exact,
            )

            self.assertEqual(1, first.pending_revision)
            self.assertEqual(2, retried.pending_revision)
            self.assertEqual(2, retried.data["attempts"])
            self.assertEqual("second failure", retried.data["lastError"])

            for field, replacement in (
                ("canonical_source_id", "source-B"),
                ("work_key", "4" * 64),
                ("proposal_hash", "5" * 64),
                ("selection_hash", "6" * 64),
            ):
                with self.subTest(field=field):
                    drifted = {**exact, field: replacement}
                    with self.assertRaises(
                        pending_responses.PendingResponseConflict
                    ):
                        pending_responses.queue_pending_response(
                            "uid-1",
                            "thread-A",
                            "message-A",
                            "broker@example.com",
                            "Source A body",
                            "client-1",
                            error="drifted retry",
                            **drifted,
                        )

        stored = (
            fake_fs.collections["pendingResponses"]
            .document("thread-A")
            .get()
            .to_dict()
        )
        self.assertEqual(2, stored["pendingRevision"])
        self.assertEqual("second failure", stored["lastError"])
        binding_id = (
            f"pending-source-{send_permits.pending_envelope_hash(stored)}"
        )
        binding = (
            fake_fs.collections["pendingResponseSourceBindings"]
            .document(binding_id)
            .get()
            .to_dict()
        )
        self.assertEqual(2, binding["pendingRevision"])
        self.assertIsNone(binding["claimTokenHash"])

    def test_require_pending_response_exact_is_typed_and_fails_closed(self):
        exact_doc = FakeDoc("thread-A", exact_pending_data({
            "threadId": "thread-A",
            "msgId": "message-A",
            "recipient": "broker@example.com",
            "responseBody": "Source A body",
            "clientId": "client-1",
            "attempts": 1,
            "canonicalSourceId": "source-A",
            "workKey": "1" * 64,
            "proposalHash": "2" * 64,
            "selectionHash": "3" * 64,
            "pendingRevision": 7,
        }))
        malformed_doc = FakeDoc("thread-malformed", {
            "threadId": "thread-malformed",
            "canonicalSourceId": "source-malformed",
            "workKey": "4" * 64,
            "proposalHash": "5" * 64,
            "pendingRevision": 1,
        })
        fake_fs = FakeFirestore([exact_doc, malformed_doc])

        with self._mock_clients_module(fake_fs):
            record = pending_responses.require_pending_response_exact(
                "uid-1", "thread-A", "source-A", "1" * 64
            )
            self.assertIsInstance(record, pending_responses.PendingResponseRecord)
            self.assertEqual(7, record.pending_revision)
            self.assertEqual("2" * 64, record.proposal_hash)
            self.assertEqual("3" * 64, record.selection_hash)

            for source_id, work_key in (
                ("source-B", "1" * 64),
                ("source-A", "9" * 64),
            ):
                with self.subTest(source_id=source_id, work_key=work_key):
                    with self.assertRaises(
                        pending_responses.PendingResponseConflict
                    ):
                        pending_responses.require_pending_response_exact(
                            "uid-1", "thread-A", source_id, work_key
                        )

            with self.assertRaises(pending_responses.PendingResponseConflict):
                pending_responses.require_pending_response_exact(
                    "uid-1",
                    "thread-malformed",
                    "source-malformed",
                    "4" * 64,
                )
            with self.assertRaises(pending_responses.PendingResponseRetryable):
                pending_responses.require_pending_response_exact(
                    "uid-1", "thread-missing", "source-A", "1" * 64
                )

    def test_exact_claim_requires_revision_and_returns_bound_claim(self):
        pending_doc = FakeDoc("thread-A", exact_pending_data({
            "threadId": "thread-A",
            "msgId": "message-A",
            "recipient": "broker@example.com",
            "responseBody": "Source A body",
            "clientId": "client-1",
            "attempts": 1,
            "canonicalSourceId": "source-A",
            "workKey": "1" * 64,
            "proposalHash": "2" * 64,
            "selectionHash": "3" * 64,
            "pendingRevision": 4,
            "status": "queued",
        }))
        fake_fs = FakeFirestore([pending_doc])
        fake_fs.collections["threads"].document("thread-A").get()._data[
            "clientId"
        ] = "client-1"

        with self._mock_clients_module(fake_fs):
            with self.assertRaises(pending_responses.PendingResponseConflict):
                pending_responses.claim_pending_response_for_send_exact(
                    "uid-1", "thread-A", "source-B", "9" * 64, 4
                )
            self.assertEqual([], pending_doc.reference.update_calls)

            with self.assertRaises(pending_responses.PendingResponseRetryable):
                pending_responses.claim_pending_response_for_send_exact(
                    "uid-1", "thread-A", "source-A", "1" * 64, 3
                )
            self.assertEqual([], pending_doc.reference.update_calls)

            claim = pending_responses.claim_pending_response_for_send_exact(
                "uid-1", "thread-A", "source-A", "1" * 64, 4
            )

        self.assertIsInstance(claim, pending_responses.PendingResponseClaim)
        self.assertEqual("source-A", claim.canonical_source_id)
        self.assertEqual("1" * 64, claim.work_key)
        self.assertEqual(5, claim.pending_revision)
        self.assertTrue(claim.claim_token.startswith("pending-response-"))
        self.assertEqual("sending", pending_doc.to_dict()["status"])
        self.assertEqual(claim.claim_token, pending_doc.to_dict()["processingBy"])
        self.assertEqual(5, pending_doc.to_dict()["pendingRevision"])
        binding_id = (
            "pending-source-"
            f"{send_permits.pending_envelope_hash(pending_doc.to_dict())}"
        )
        binding = (
            fake_fs.collections["pendingResponseSourceBindings"]
            .document(binding_id)
            .get()
            .to_dict()
        )
        self.assertEqual(5, binding["pendingRevision"])
        self.assertEqual(
            hashlib.sha256(claim.claim_token.encode("utf-8")).hexdigest(),
            binding["claimTokenHash"],
        )

    def test_enforced_worker_rejects_cross_client_pending_before_any_effect(self):
        pending_doc = FakeDoc("thread-client-A", exact_pending_data({
            "threadId": "thread-client-A",
            "msgId": "message-A",
            "recipient": "broker@example.com",
            "responseBody": "Source A body",
            "clientId": "client-B",
            "attempts": 0,
            "canonicalSourceId": "source-A",
            "workKey": "1" * 64,
            "proposalHash": "2" * 64,
            "selectionHash": "3" * 64,
            "pendingRevision": 1,
            "status": "queued",
        }))
        fake_fs = FakeFirestore([pending_doc])
        fake_fs.collections["threads"].document("thread-client-A").get()._data[
            "clientId"
        ] = "client-A"
        binding_id = (
            "pending-source-"
            f"{send_permits.pending_envelope_hash(pending_doc.to_dict())}"
        )
        binding_doc = (
            fake_fs.collections["pendingResponseSourceBindings"]
            .document(binding_id)
            .get()
        )
        pending_before = dict(pending_doc.to_dict())
        binding_before = dict(binding_doc.to_dict())
        self.campaign_decision.reset_mock()

        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"},
        ), self._mock_clients_module(fake_fs), patch.object(
            pending_responses,
            "_reconcile_expired_pending_permit",
        ) as reconcile_expired, patch.object(
            pending_responses,
            "_final_pending_response_send_fence",
        ) as final_fence, patch.object(
            processing,
            "send_reply_in_thread",
        ) as graph_send:
            states = pending_responses.process_pending_responses(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertEqual(1, len(states))
        self.assertEqual("error", states[0]["status"])
        self.assertIn("client", states[0]["error"])
        self.assertEqual(pending_before, pending_doc.to_dict())
        self.assertEqual([], pending_doc.reference.update_calls)
        self.assertEqual(binding_before, binding_doc.to_dict())
        self.assertEqual([], binding_doc.reference.update_calls)
        self.campaign_decision.assert_not_called()
        reconcile_expired.assert_not_called()
        final_fence.assert_not_called()
        graph_send.assert_not_called()

    def test_exact_claim_preserves_clientless_pending_behavior(self):
        pending_doc = FakeDoc("thread-clientless", exact_pending_data({
            "threadId": "thread-clientless",
            "msgId": "message-clientless",
            "recipient": "broker@example.com",
            "responseBody": "Clientless response body",
            "clientId": None,
            "attempts": 0,
            "canonicalSourceId": "source-clientless",
            "workKey": "4" * 64,
            "proposalHash": "5" * 64,
            "selectionHash": "6" * 64,
            "pendingRevision": 1,
            "status": "queued",
        }))
        fake_fs = FakeFirestore([pending_doc])

        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"},
        ), self._mock_clients_module(fake_fs):
            claim = pending_responses.claim_pending_response_for_send_exact(
                "uid-1",
                "thread-clientless",
                "source-clientless",
                "4" * 64,
                1,
            )
            capability = pending_responses._final_pending_response_send_fence(
                "uid-1",
                pending_doc,
                claim.data,
                claim.claim_token,
            )

        self.assertIsNone(claim.data["clientId"])
        self.assertEqual("sending", claim.data["status"])
        self.assertEqual(claim.claim_token, claim.data["processingBy"])
        self.assertIsNotNone(capability)

    def test_exact_final_permit_rejects_client_rebind_after_claim(self):
        pending_doc = FakeDoc("thread-client-rebind", exact_pending_data({
            "threadId": "thread-client-rebind",
            "msgId": "message-client-rebind",
            "recipient": "broker@example.com",
            "responseBody": "Client-bound response body",
            "clientId": "client-A",
            "attempts": 0,
            "canonicalSourceId": "source-client-rebind",
            "workKey": "7" * 64,
            "proposalHash": "8" * 64,
            "selectionHash": "9" * 64,
            "pendingRevision": 1,
            "status": "queued",
        }))
        fake_fs = FakeFirestore([pending_doc])
        thread_doc = (
            fake_fs.collections["threads"]
            .document("thread-client-rebind")
            .get()
        )
        thread_doc._data["clientId"] = "client-A"

        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"},
        ), self._mock_clients_module(fake_fs):
            claim = pending_responses.claim_pending_response_for_send_exact(
                "uid-1",
                "thread-client-rebind",
                "source-client-rebind",
                "7" * 64,
                1,
            )
            binding_id = (
                "pending-source-"
                f"{send_permits.pending_envelope_hash(claim.data)}"
            )
            binding_doc = (
                fake_fs.collections["pendingResponseSourceBindings"]
                .document(binding_id)
                .get()
            )
            pending_after_claim = dict(pending_doc.to_dict())
            binding_after_claim = dict(binding_doc.to_dict())
            pending_update_count = len(pending_doc.reference.update_calls)
            binding_update_count = len(binding_doc.reference.update_calls)

            thread_doc._data["clientId"] = "client-B"
            with self.assertRaisesRegex(RuntimeError, "client"):
                pending_responses._final_pending_response_send_fence(
                    "uid-1",
                    pending_doc,
                    claim.data,
                    claim.claim_token,
                )

        self.assertEqual(pending_after_claim, pending_doc.to_dict())
        self.assertEqual(
            pending_update_count,
            len(pending_doc.reference.update_calls),
        )
        self.assertEqual(binding_after_claim, binding_doc.to_dict())
        self.assertEqual(
            binding_update_count,
            len(binding_doc.reference.update_calls),
        )
        self.assertNotIn("activeGraphSendPermit", thread_doc.to_dict())
        self.assertEqual(
            [],
            thread_doc.reference.collection("graphSendPermits").stream(),
        )

    def test_exact_final_permit_rejects_deleted_binding_after_claim(self):
        pending_doc = FakeDoc("thread-binding-deleted", exact_pending_data({
            "threadId": "thread-binding-deleted",
            "msgId": "message-binding-deleted",
            "recipient": "broker@example.com",
            "responseBody": "Exact source response body",
            "clientId": "client-A",
            "attempts": 0,
            "canonicalSourceId": "source-binding-deleted",
            "workKey": "1" * 64,
            "proposalHash": "2" * 64,
            "selectionHash": "3" * 64,
            "pendingRevision": 1,
            "status": "queued",
        }))
        fake_fs = FakeFirestore([pending_doc])
        thread_doc = (
            fake_fs.collections["threads"]
            .document("thread-binding-deleted")
            .get()
        )
        thread_doc._data["clientId"] = "client-A"
        provider_effect = MagicMock(name="graph_provider_effect")

        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"},
        ), self._mock_clients_module(fake_fs):
            claim = pending_responses.claim_pending_response_for_send_exact(
                "uid-1",
                "thread-binding-deleted",
                "source-binding-deleted",
                "1" * 64,
                1,
            )
            binding_id = (
                "pending-source-"
                f"{send_permits.pending_envelope_hash(claim.data)}"
            )
            binding_doc = (
                fake_fs.collections["pendingResponseSourceBindings"]
                .document(binding_id)
                .get()
            )
            binding_doc.exists = False
            pending_before_fence = dict(pending_doc.to_dict())
            thread_before_fence = dict(thread_doc.to_dict())
            pending_update_count = len(pending_doc.reference.update_calls)
            thread_update_count = len(thread_doc.reference.update_calls)

            with patch.dict(
                os.environ,
                {"SITESIFT_SOURCE_COORDINATOR_MODE": "disabled"},
            ), self.assertRaisesRegex(RuntimeError, "source binding"):
                capability = pending_responses._final_pending_response_send_fence(
                    "uid-1",
                    pending_doc,
                    claim.data,
                    claim.claim_token,
                )
                provider_effect(capability)

        provider_effect.assert_not_called()
        self.assertEqual(pending_before_fence, pending_doc.to_dict())
        self.assertEqual(
            pending_update_count,
            len(pending_doc.reference.update_calls),
        )
        self.assertEqual(thread_before_fence, thread_doc.to_dict())
        self.assertEqual(
            thread_update_count,
            len(thread_doc.reference.update_calls),
        )
        self.assertFalse(binding_doc.exists)
        self.assertEqual(
            [],
            thread_doc.reference.collection("graphSendPermits").stream(),
        )

    def test_exact_final_permit_rejects_recomputed_binding_drift_after_claim(self):
        pending_doc = FakeDoc("thread-binding-drift", exact_pending_data({
            "threadId": "thread-binding-drift",
            "msgId": "message-binding-drift",
            "recipient": "broker@example.com",
            "responseBody": "Exact source response body",
            "clientId": "client-A",
            "attempts": 0,
            "canonicalSourceId": "source-A",
            "workKey": "4" * 64,
            "proposalHash": "5" * 64,
            "selectionHash": "6" * 64,
            "pendingRevision": 1,
            "status": "queued",
        }))
        fake_fs = FakeFirestore([pending_doc])
        thread_doc = (
            fake_fs.collections["threads"]
            .document("thread-binding-drift")
            .get()
        )
        thread_doc._data["clientId"] = "client-A"
        provider_effect = MagicMock(name="graph_provider_effect")

        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"},
        ), self._mock_clients_module(fake_fs):
            claim = pending_responses.claim_pending_response_for_send_exact(
                "uid-1",
                "thread-binding-drift",
                "source-A",
                "4" * 64,
                1,
            )
            binding_id = (
                "pending-source-"
                f"{send_permits.pending_envelope_hash(claim.data)}"
            )
            binding_doc = (
                fake_fs.collections["pendingResponseSourceBindings"]
                .document(binding_id)
                .get()
            )
            drift = {
                "canonicalSourceId": "source-B",
                "workKey": "7" * 64,
                "proposalHash": "8" * 64,
                "selectionHash": "9" * 64,
            }
            pending_doc._data.update(drift)
            binding_doc._data.update(drift)
            immutable = {
                field: binding_doc._data[field]
                for field in pending_responses._PENDING_RESPONSE_SOURCE_BINDING_IMMUTABLE_FIELDS
            }
            binding_doc._data["immutableHash"] = (
                send_permits._stable_evidence_hash(immutable)
            )
            binding_doc._data["claimBindingHash"] = (
                send_permits._stable_evidence_hash({
                    "immutableHash": binding_doc._data["immutableHash"],
                    "pendingRevision": binding_doc._data["pendingRevision"],
                })
            )
            pending_before_fence = dict(pending_doc.to_dict())
            binding_before_fence = dict(binding_doc.to_dict())
            thread_before_fence = dict(thread_doc.to_dict())
            pending_update_count = len(pending_doc.reference.update_calls)
            binding_update_count = len(binding_doc.reference.update_calls)
            thread_update_count = len(thread_doc.reference.update_calls)

            with self.assertRaisesRegex(RuntimeError, "claim binding"):
                capability = pending_responses._final_pending_response_send_fence(
                    "uid-1",
                    pending_doc,
                    claim.data,
                    claim.claim_token,
                )
                provider_effect(capability)

        provider_effect.assert_not_called()
        self.assertEqual(pending_before_fence, pending_doc.to_dict())
        self.assertEqual(
            pending_update_count,
            len(pending_doc.reference.update_calls),
        )
        self.assertEqual(binding_before_fence, binding_doc.to_dict())
        self.assertEqual(
            binding_update_count,
            len(binding_doc.reference.update_calls),
        )
        self.assertEqual(thread_before_fence, thread_doc.to_dict())
        self.assertEqual(
            thread_update_count,
            len(thread_doc.reference.update_calls),
        )
        self.assertEqual(
            [],
            thread_doc.reference.collection("graphSendPermits").stream(),
        )

    def test_exact_clear_never_deletes_different_source_or_revision(self):
        pending_doc = FakeDoc("thread-shared", exact_pending_data({
            "threadId": "thread-shared",
            "msgId": "message-B",
            "recipient": "broker@example.com",
            "responseBody": "Source B body",
            "clientId": "client-1",
            "attempts": 1,
            "canonicalSourceId": "source-B",
            "workKey": "4" * 64,
            "proposalHash": "5" * 64,
            "selectionHash": "6" * 64,
            "pendingRevision": 9,
            "status": "queued",
        }))
        fake_fs = FakeFirestore([pending_doc])

        with self._mock_clients_module(fake_fs):
            with self.assertRaises(pending_responses.PendingResponseConflict):
                pending_responses.clear_pending_response_exact(
                    "uid-1", "thread-shared", "source-A", "1" * 64, 9
                )
            self.assertTrue(pending_doc.exists)
            self.assertFalse(pending_doc.reference.deleted)

            with self.assertRaises(pending_responses.PendingResponseRetryable):
                pending_responses.clear_pending_response_exact(
                    "uid-1", "thread-shared", "source-B", "4" * 64, 8
                )
            self.assertTrue(pending_doc.exists)
            self.assertFalse(pending_doc.reference.deleted)

            result = pending_responses.clear_pending_response_exact(
                "uid-1", "thread-shared", "source-B", "4" * 64, 9
            )

        self.assertIsInstance(result, pending_responses.PendingResponseClearResult)
        self.assertTrue(result.cleared)
        self.assertEqual("source-B", result.canonical_source_id)
        self.assertEqual("4" * 64, result.work_key)
        self.assertEqual(9, result.pending_revision)
        self.assertFalse(pending_doc.exists)
        binding_id = (
            "pending-source-"
            f"{send_permits.pending_envelope_hash(pending_doc.to_dict())}"
        )
        self.assertFalse(
            fake_fs.collections["pendingResponseSourceBindings"]
            .document(binding_id)
            .get()
            .exists
        )

    def test_enforced_legacy_clear_raises_before_firestore_access(self):
        fake_fs = MagicMock(name="firestore")
        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"},
        ), self._mock_clients_module(fake_fs):
            with self.assertRaises(pending_responses.PendingResponseConflict):
                pending_responses.clear_pending_response(
                    "uid-1", "thread-A"
                )

        fake_fs.collection.assert_not_called()

    def test_disabled_queue_and_thread_clear_preserve_legacy_contract(self):
        fake_fs = FakeFirestore([])
        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "disabled"},
        ), self._mock_clients_module(fake_fs):
            document_id = pending_responses.queue_pending_response(
                "uid-1",
                "thread-legacy",
                "message-legacy",
                "broker@example.com",
                "Legacy body",
                "client-1",
                error="legacy failure",
            )
            stored = (
                fake_fs.collections["pendingResponses"]
                .document("thread-legacy")
                .get()
                .to_dict()
            )
            cleared = pending_responses.clear_pending_response(
                "uid-1", "thread-legacy"
            )

        self.assertEqual("thread-legacy", document_id)
        self.assertNotIn("canonicalSourceId", stored)
        self.assertTrue(cleared)
        self.assertFalse(
            fake_fs.collections["pendingResponses"]
            .document("thread-legacy")
            .get()
            .exists
        )

    def test_persisted_pending_protocol_classifier_is_strict_and_reusable(self):
        classifier = getattr(
            pending_responses,
            "_classify_pending_response_protocol",
            None,
        )
        self.assertTrue(callable(classifier))
        legacy = {
            "threadId": "thread-legacy",
            "msgId": "message-legacy",
            "recipient": "broker@example.com",
            "responseBody": "Legacy body",
            "attempts": 1,
        }
        exact = exact_pending_data({
            **legacy,
            "canonicalSourceId": "source-A",
            "workKey": "1" * 64,
            "proposalHash": "2" * 64,
            "selectionHash": "3" * 64,
            "pendingRevision": 1,
        })
        self.assertEqual("legacy", classifier(legacy))
        self.assertEqual("b1_exact_source", classifier(exact))
        for malformed in (
            {**legacy, "canonicalSourceId": "source-A"},
            {**legacy, "pendingProtocol": dict(EXACT_PENDING_PROTOCOL)},
            {**exact, "pendingProtocol": {"kind": "wrong", "version": 1}},
        ):
            with self.subTest(record=malformed), self.assertRaises(
                pending_responses.PendingResponseConflict
            ):
                classifier(malformed)

    def test_disabled_queue_cannot_overwrite_exact_pending_or_orphan_binding(self):
        fake_fs = FakeFirestore([])
        exact_arguments = {
            "canonical_source_id": "source-A",
            "work_key": "1" * 64,
            "proposal_hash": "2" * 64,
            "selection_hash": "3" * 64,
        }
        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"},
        ), self._mock_clients_module(fake_fs):
            pending_responses.queue_pending_response(
                "uid-1",
                "thread-rollback",
                "message-A",
                "broker@example.com",
                "Source A body",
                "client-1",
                error="source A failed",
                **exact_arguments,
            )

        pending_doc = (
            fake_fs.collections["pendingResponses"]
            .document("thread-rollback")
            .get()
        )
        pending_before = deepcopy(pending_doc.to_dict())
        envelope_hash = send_permits.pending_envelope_hash(pending_before)
        binding_doc = (
            fake_fs.collections["pendingResponseSourceBindings"]
            .document(f"pending-source-{envelope_hash}")
            .get()
        )
        binding_before = deepcopy(binding_doc.to_dict())

        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "disabled"},
        ), self._mock_clients_module(fake_fs), self.assertRaises(
            pending_responses.PendingResponseConflict
        ):
            pending_responses.queue_pending_response(
                "uid-1",
                "thread-rollback",
                "message-legacy",
                "broker@example.com",
                "Legacy rollback body",
                "client-1",
                error="legacy rollback",
            )

        self.assertEqual(EXACT_PENDING_PROTOCOL, pending_before["pendingProtocol"])
        self.assertEqual(pending_before, pending_doc.to_dict())
        self.assertEqual("source-A", pending_doc.to_dict()["canonicalSourceId"])
        self.assertTrue(binding_doc.exists)
        self.assertEqual(binding_before, binding_doc.to_dict())
        self.assertEqual(
            EXACT_PENDING_PROTOCOL,
            binding_doc.to_dict()["pendingProtocol"],
        )
        drifted_protocol = {
            **pending_before,
            "pendingProtocol": {"kind": "different", "version": 1},
        }
        self.assertNotEqual(
            envelope_hash,
            send_permits.pending_envelope_hash(drifted_protocol),
        )

    def test_disabled_legacy_mutations_abort_if_exact_work_wins_after_read(self):
        class AbortAfterExactCommit(FakeTransaction):
            def __init__(self, target_ref, exact_data):
                super().__init__()
                self._target_ref = target_ref
                self._exact_data = deepcopy(exact_data)

            def commit(self):
                exact_doc = FakeDoc(
                    self._target_ref.id,
                    deepcopy(self._exact_data),
                )
                exact_doc.reference = self._target_ref
                self._target_ref._doc = exact_doc
                raise RuntimeError("ABORTED: document changed after transaction read")

        exact = exact_pending_data({
            "threadId": "thread-mixed-race",
            "msgId": "message-exact",
            "recipient": "broker@example.com",
            "responseBody": "Exact source body",
            "clientId": "client-1",
            "attempts": 1,
            "status": "queued",
            "canonicalSourceId": "source-A",
            "workKey": "1" * 64,
            "proposalHash": "2" * 64,
            "selectionHash": "3" * 64,
            "pendingRevision": 1,
        })

        enqueue_fs = FakeFirestore([])
        enqueue_ref = enqueue_fs.collections["pendingResponses"].document(
            "thread-mixed-race"
        )
        enqueue_fs.transaction = lambda: AbortAfterExactCommit(
            enqueue_ref,
            exact,
        )
        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "disabled"},
        ), self._mock_clients_module(enqueue_fs), self.assertRaisesRegex(
            RuntimeError,
            "ABORTED",
        ):
            pending_responses.queue_pending_response(
                "uid-1",
                "thread-mixed-race",
                "message-legacy",
                "broker@example.com",
                "Legacy body",
                "client-1",
            )
        self.assertEqual(exact, enqueue_ref.get().to_dict())

        legacy = {
            "threadId": "thread-mixed-race",
            "msgId": "message-legacy",
            "recipient": "broker@example.com",
            "responseBody": "Legacy body",
            "clientId": "client-1",
            "attempts": 1,
        }
        clear_doc = FakeDoc("thread-mixed-race", legacy)
        clear_fs = FakeFirestore([clear_doc])
        clear_ref = clear_doc.reference
        clear_fs.transaction = lambda: AbortAfterExactCommit(
            clear_ref,
            exact,
        )
        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "disabled"},
        ), self._mock_clients_module(clear_fs), self.assertRaisesRegex(
            RuntimeError,
            "ABORTED",
        ):
            pending_responses.clear_pending_response(
                "uid-1",
                "thread-mixed-race",
            )
        self.assertTrue(clear_ref.get().exists)
        self.assertEqual(exact, clear_ref.get().to_dict())

    def test_disabled_clear_activates_real_firestore_transaction_before_read(self):
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import firestore
        from google.cloud.firestore_v1.types import firestore as firestore_types

        real_fs = firestore.Client(
            project="pending-transaction-regression",
            credentials=AnonymousCredentials(),
        )
        firestore_api = MagicMock(name="firestore_api")
        firestore_api.begin_transaction.return_value = types.SimpleNamespace(
            transaction=b"pending-transaction-id",
        )
        firestore_api.batch_get_documents.return_value = iter((
            firestore_types.BatchGetDocumentsResponse(
                missing=(
                    "projects/pending-transaction-regression/databases/"
                    "(default)/documents/users/uid-real-transaction/"
                    "pendingResponses/thread-real-transaction"
                ),
            ),
        ))
        firestore_api.commit.return_value = types.SimpleNamespace(
            write_results=(),
            commit_time=None,
        )
        real_fs._firestore_api_internal = firestore_api

        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "disabled"},
        ), self._mock_clients_module(real_fs):
            cleared = pending_responses.clear_pending_response(
                "uid-real-transaction",
                "thread-real-transaction",
            )

        self.assertFalse(cleared)
        firestore_api.begin_transaction.assert_called_once()
        firestore_api.batch_get_documents.assert_called_once()
        read_request = firestore_api.batch_get_documents.call_args.kwargs[
            "request"
        ]
        self.assertEqual(
            b"pending-transaction-id",
            read_request["transaction"],
        )
        firestore_api.commit.assert_called_once()

    def test_pending_response_has_no_manual_firestore_transaction_lifecycle(self):
        source_path = Path(pending_responses.__file__)
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        unsupported = [
            (node.lineno, node.func.attr)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"transaction", "commit"}
        ]

        self.assertEqual([], unsupported)

    def test_disabled_queue_fails_closed_on_partial_or_malformed_b1_markers(self):
        base = {
            "threadId": "thread-partial",
            "msgId": "message-partial",
            "recipient": "broker@example.com",
            "responseBody": "Original body",
            "clientId": "client-1",
            "attempts": 1,
        }
        cases = {
            "source marker only": {"canonicalSourceId": "source-A"},
            "protocol only": {"pendingProtocol": dict(EXACT_PENDING_PROTOCOL)},
            "full markers without protocol": {
                "canonicalSourceId": "source-A",
                "workKey": "1" * 64,
                "proposalHash": "2" * 64,
                "selectionHash": "3" * 64,
                "pendingRevision": 1,
            },
            "malformed protocol": exact_pending_data({
                "canonicalSourceId": "source-A",
                "workKey": "1" * 64,
                "proposalHash": "2" * 64,
                "selectionHash": "3" * 64,
                "pendingRevision": 1,
            }) | {"pendingProtocol": {"kind": "b1_exact_source", "version": 2}},
        }

        for label, markers in cases.items():
            with self.subTest(case=label):
                pending_doc = FakeDoc(
                    "thread-partial",
                    {**base, **markers},
                )
                fake_fs = FakeFirestore([pending_doc])
                before = deepcopy(pending_doc.to_dict())
                with patch.dict(
                    os.environ,
                    {"SITESIFT_SOURCE_COORDINATOR_MODE": "disabled"},
                ), self._mock_clients_module(fake_fs), self.assertRaises(
                    pending_responses.PendingResponseConflict
                ):
                    pending_responses.queue_pending_response(
                        "uid-1",
                        "thread-partial",
                        "message-replacement",
                        "broker@example.com",
                        "Replacement body",
                        "client-1",
                    )

                self.assertEqual(before, pending_doc.to_dict())
                self.assertEqual([], pending_doc.reference.set_calls)

    def test_disabled_legacy_clear_refuses_exact_partial_and_malformed_records(self):
        base = {
            "threadId": "thread-clear",
            "msgId": "message-clear",
            "recipient": "broker@example.com",
            "responseBody": "Retained body",
            "clientId": "client-1",
            "attempts": 1,
        }
        cases = {
            "exact": exact_pending_data({
                **base,
                "canonicalSourceId": "source-A",
                "workKey": "1" * 64,
                "proposalHash": "2" * 64,
                "selectionHash": "3" * 64,
                "pendingRevision": 1,
            }),
            "partial": {**base, "canonicalSourceId": "source-A"},
            "malformed": {
                **base,
                "pendingProtocol": {"kind": "b1_exact_source", "version": 99},
                "canonicalSourceId": "source-A",
                "workKey": "1" * 64,
                "proposalHash": "2" * 64,
                "selectionHash": "3" * 64,
                "pendingRevision": 1,
            },
        }

        for label, data in cases.items():
            with self.subTest(case=label):
                pending_doc = FakeDoc("thread-clear", data)
                fake_fs = FakeFirestore([pending_doc])
                before = deepcopy(pending_doc.to_dict())
                binding_before = {
                    doc.id: deepcopy(doc.to_dict())
                    for doc in fake_fs.collections[
                        "pendingResponseSourceBindings"
                    ].stream()
                }
                with patch.dict(
                    os.environ,
                    {"SITESIFT_SOURCE_COORDINATOR_MODE": "disabled"},
                ), self._mock_clients_module(fake_fs), self.assertRaises(
                    pending_responses.PendingResponseConflict
                ):
                    pending_responses.clear_pending_response(
                        "uid-1",
                        "thread-clear",
                    )

                self.assertTrue(pending_doc.exists)
                self.assertFalse(pending_doc.reference.deleted)
                self.assertEqual(before, pending_doc.to_dict())
                self.assertEqual(
                    binding_before,
                    {
                        doc.id: deepcopy(doc.to_dict())
                        for doc in fake_fs.collections[
                            "pendingResponseSourceBindings"
                        ].stream()
                    },
                )

    def test_disabled_processing_has_zero_effects_on_exact_or_malformed_records(self):
        base = {
            "threadId": "thread-rollback-worker",
            "msgId": "message-A",
            "recipient": "broker@example.com",
            "responseBody": "Retained body",
            "clientId": "client-1",
            "attempts": 1,
            "status": "queued",
        }
        cases = {
            "exact": exact_pending_data({
                **base,
                "canonicalSourceId": "source-A",
                "workKey": "1" * 64,
                "proposalHash": "2" * 64,
                "selectionHash": "3" * 64,
                "pendingRevision": 1,
            }),
            "partial": {**base, "canonicalSourceId": "source-A"},
            "full markers without protocol": {
                **base,
                "canonicalSourceId": "source-A",
                "workKey": "1" * 64,
                "proposalHash": "2" * 64,
                "selectionHash": "3" * 64,
                "pendingRevision": 1,
            },
            "malformed protocol": {
                **base,
                "pendingProtocol": {"kind": "wrong", "version": 1},
                "canonicalSourceId": "source-A",
                "workKey": "1" * 64,
                "proposalHash": "2" * 64,
                "selectionHash": "3" * 64,
                "pendingRevision": 1,
            },
        }

        for label, data in cases.items():
            with self.subTest(case=label):
                pending_doc = FakeDoc("thread-rollback-worker", data)
                fake_fs = FakeFirestore([pending_doc])
                before = deepcopy(pending_doc.to_dict())
                self.campaign_decision.reset_mock()
                with patch.dict(
                    os.environ,
                    {"SITESIFT_SOURCE_COORDINATOR_MODE": "disabled"},
                ), self._mock_clients_module(fake_fs), patch.object(
                    pending_responses,
                    "_process_pending_completion_obligations",
                    return_value=[],
                ) as completion_reconciliation, patch.object(
                    pending_responses,
                    "_claim_pending_response_for_send",
                    return_value=None,
                ) as legacy_claim, patch.object(
                    pending_responses,
                    "_reconcile_expired_pending_permit",
                    return_value=False,
                ) as permit_reconciliation, patch.object(
                    pending_responses,
                    "_final_pending_response_send_fence",
                ) as permit_issue, patch.object(
                    pending_responses,
                    "find_matching_sent_message_for_retry",
                ) as provider_read, patch.object(
                    pending_responses,
                    "_cas_pending_update",
                ) as pending_update, patch.object(
                    pending_responses,
                    "_cas_pending_dead_letter",
                ) as pending_delete, patch.object(
                    processing,
                    "send_reply_in_thread",
                ) as provider_send:
                    states = pending_responses.process_pending_responses(
                        "uid-1",
                        {"Authorization": "Bearer token"},
                    )

                self.assertEqual(1, len(states))
                self.assertEqual("error", states[0]["status"])
                self.assertIn("protocol", states[0]["error"])
                completion_reconciliation.assert_not_called()
                legacy_claim.assert_not_called()
                self.campaign_decision.assert_not_called()
                permit_reconciliation.assert_not_called()
                permit_issue.assert_not_called()
                provider_read.assert_not_called()
                provider_send.assert_not_called()
                pending_update.assert_not_called()
                pending_delete.assert_not_called()
                self.assertEqual(before, pending_doc.to_dict())
                self.assertEqual([], pending_doc.reference.update_calls)
                self.assertFalse(pending_doc.reference.deleted)

    def test_disabled_processing_preserves_true_legacy_path(self):
        pending_doc = FakeDoc("thread-legacy-worker", {
            "threadId": "thread-legacy-worker",
            "msgId": "message-legacy",
            "recipient": "broker@example.com",
            "responseBody": "Legacy body",
            "clientId": "client-1",
            "attempts": 1,
            "status": "queued",
        })
        fake_fs = FakeFirestore([pending_doc])
        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "disabled"},
        ), self._mock_clients_module(fake_fs), patch.object(
            pending_responses,
            "_process_pending_completion_obligations",
            return_value=[],
        ) as completion_reconciliation, patch.object(
            pending_responses,
            "_claim_pending_response_for_send",
            return_value=None,
        ) as legacy_claim, patch.object(
            pending_responses,
            "_reconcile_expired_pending_permit",
            return_value=False,
        ) as permit_reconciliation:
            states = pending_responses.process_pending_responses(
                "uid-1",
                {"Authorization": "Bearer token"},
            )

        self.assertEqual([], states)
        completion_reconciliation.assert_called_once()
        legacy_claim.assert_called_once_with(
            "uid-1",
            pending_doc,
            pending_doc.to_dict(),
        )
        permit_reconciliation.assert_called_once()

    def test_disabled_legacy_claim_rechecks_current_persisted_protocol(self):
        legacy_loaded = {
            "threadId": "thread-protocol-race",
            "msgId": "message-A",
            "recipient": "broker@example.com",
            "responseBody": "Same envelope body",
            "clientId": "client-1",
            "attempts": 1,
            "status": "queued",
        }
        exact_current = exact_pending_data({
            **legacy_loaded,
            "canonicalSourceId": "source-A",
            "workKey": "1" * 64,
            "proposalHash": "2" * 64,
            "selectionHash": "3" * 64,
            "pendingRevision": 1,
        })
        pending_doc = FakeDoc("thread-protocol-race", exact_current)
        fake_fs = FakeFirestore([pending_doc])
        before = deepcopy(pending_doc.to_dict())

        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "disabled"},
        ), self._mock_clients_module(fake_fs), self.assertRaises(
            pending_responses.PendingResponseConflict
        ):
            pending_responses._claim_pending_response_for_send(
                "uid-1",
                pending_doc,
                legacy_loaded,
            )

        self.assertEqual(before, pending_doc.to_dict())
        self.assertEqual([], pending_doc.reference.update_calls)

    def test_enforced_malformed_pending_binding_blocks_provider_work(self):
        pending_doc = FakeDoc("thread-legacy", {
            "threadId": "thread-legacy",
            "msgId": "message-legacy",
            "recipient": "broker@example.com",
            "responseBody": "Legacy unbound body",
            "clientId": "client-1",
            "attempts": 1,
        })
        fake_fs = FakeFirestore([pending_doc])
        self.campaign_decision.reset_mock()

        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"},
        ), self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread"
        ) as graph_send, patch.object(
            pending_responses, "find_matching_sent_message_for_retry"
        ) as sent_lookup, patch.object(
            pending_responses, "_final_pending_response_send_fence"
        ) as final_fence:
            states = pending_responses.process_pending_responses(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertEqual(1, len(states))
        self.assertEqual("error", states[0]["status"])
        self.assertIn("binding", states[0]["error"])
        self.campaign_decision.assert_not_called()
        sent_lookup.assert_not_called()
        final_fence.assert_not_called()
        graph_send.assert_not_called()
        self.assertEqual([], pending_doc.reference.update_calls)

    def test_enforced_hash_drift_after_scan_blocks_before_claim_or_provider(self):
        class HashDriftDoc(FakeDoc):
            def __init__(self, doc_id, scanned, current):
                super().__init__(doc_id, scanned)
                self._scanned = dict(scanned)
                self._current = dict(current)
                self.read_count = 0

            def to_dict(self):
                self.read_count += 1
                if self.read_count == 1:
                    return dict(self._scanned)
                return dict(self._current)

        scanned = exact_pending_data({
            "threadId": "thread-hash-drift",
            "msgId": "message-A",
            "recipient": "broker@example.com",
            "responseBody": "Source A body",
            "clientId": "client-1",
            "attempts": 0,
            "status": "queued",
            "canonicalSourceId": "source-A",
            "workKey": "1" * 64,
            "proposalHash": "2" * 64,
            "selectionHash": "3" * 64,
            "pendingRevision": 4,
        })
        pending_doc = HashDriftDoc(
            "thread-hash-drift",
            scanned,
            {**scanned, "proposalHash": "4" * 64},
        )
        fake_fs = FakeFirestore([pending_doc])
        pending_doc.read_count = 0
        self.campaign_decision.reset_mock()

        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"},
        ), self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread"
        ) as graph_send:
            states = pending_responses.process_pending_responses(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertEqual(1, len(states))
        self.assertIn("binding changed", states[0]["error"])
        self.assertEqual([], pending_doc.reference.update_calls)
        self.campaign_decision.assert_not_called()
        graph_send.assert_not_called()

    def test_enforced_exact_retry_preserves_graph_send_permit_path(self):
        pending_doc = FakeDoc("thread-exact-send", exact_pending_data({
            "threadId": "thread-exact-send",
            "msgId": "message-exact-send",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi Ryan,\n\nThank you for the exact update.",
            "clientId": "client-1",
            "attempts": 0,
            "status": "queued",
            "canonicalSourceId": "source-exact-send",
            "workKey": "1" * 64,
            "proposalHash": "2" * 64,
            "selectionHash": "3" * 64,
            "pendingRevision": 1,
        }))
        fake_fs = FakeFirestore([pending_doc])
        fake_fs.collections["threads"].document("thread-exact-send").get()._data[
            "clientId"
        ] = "client-1"
        fake_fs.collections["clients"] = FakeCollection([
            FakeDoc("client-1", {"status": "live"}),
        ])

        def accepted_send(**kwargs):
            exact_sent_evidence = self._prepare_and_accept_capability(
                kwargs["graph_send_capability"]
            )
            processing._set_reply_send_outcome(
                outcome="sent_indexed",
                exact_sent_evidence=exact_sent_evidence,
            )
            return True

        source_b_attempt = {
            "canonical_source_id": "source-B",
            "work_key": "4" * 64,
            "proposal_hash": "5" * 64,
            "selection_hash": "6" * 64,
        }

        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"},
        ), self._mock_clients_module(fake_fs), self.assertRaises(
            pending_responses.PendingResponseConflict
        ):
            pending_responses.queue_pending_response(
                "uid-1",
                "thread-exact-send",
                "message-B",
                "bp21harrison@gmail.com",
                "Source B must never replace A.",
                "client-1",
                error="source B retry",
                **source_b_attempt,
            )

        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"},
        ), self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", side_effect=accepted_send
        ) as graph_send, patch.object(
            processing, "_maybe_mark_client_completed", return_value=True
        ):
            states = pending_responses.process_pending_responses(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertTrue(pending_doc.reference.deleted)
        self.assertEqual("healthy", states[0]["status"])
        self.assertEqual(
            "Hi Ryan,\n\nThank you for the exact update.",
            graph_send.call_args.kwargs["body"],
        )
        obligations = fake_fs.collections[
            send_permits.PENDING_COMPLETION_OBLIGATION_COLLECTION
        ].stream()
        self.assertEqual(1, len(obligations))
        self.assertEqual(2, obligations[0].to_dict()["version"])
        self.assertEqual(
            "b1_exact_source",
            obligations[0].to_dict()["immutable"][
                "sourceAuthorityProtocol"
            ],
        )
        envelope_hash = obligations[0].to_dict()["immutable"][
            "pendingEnvelopeHash"
        ]
        binding = (
            fake_fs.collections["pendingResponseSourceBindings"]
            .document(f"pending-source-{envelope_hash}")
            .get()
        )
        self.assertTrue(binding.exists)
        binding_data = binding.to_dict()
        self.assertEqual("source-exact-send", binding_data["canonicalSourceId"])
        self.assertEqual("1" * 64, binding_data["workKey"])
        self.assertEqual("2" * 64, binding_data["proposalHash"])
        self.assertEqual("3" * 64, binding_data["selectionHash"])
        self.assertEqual(2, binding_data["pendingRevision"])
        self.assertRegex(binding_data["claimTokenHash"], r"^[0-9a-f]{64}$")
        self.assertRegex(binding_data["claimBindingHash"], r"^[0-9a-f]{64}$")
        self.assertNotEqual("source-B", binding_data["canonicalSourceId"])

        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"},
        ), self._mock_clients_module(fake_fs):
            linkage = pending_responses._pending_completion_linkage(
                "uid-1",
                fake_fs.collection("users").document("uid-1"),
                obligations[0],
            )
        self.assertEqual(
            "source-exact-send",
            linkage["sourceBinding"]["canonicalSourceId"],
        )
        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "disabled"},
        ), self._mock_clients_module(fake_fs):
            rollback_linkage = pending_responses._pending_completion_linkage(
                "uid-1",
                fake_fs.collection("users").document("uid-1"),
                obligations[0],
            )
        self.assertEqual(
            "source-exact-send",
            rollback_linkage["sourceBinding"]["canonicalSourceId"],
        )
        downgraded = deepcopy(obligations[0].to_dict())
        downgraded["version"] = 1
        downgraded["immutable"]["version"] = 1
        downgraded["immutable"].pop("sourceAuthorityProtocol")
        downgraded_hash = send_permits._hash(downgraded["immutable"])
        downgraded_id = f"pending-completion-{downgraded_hash}"
        downgraded["obligationId"] = downgraded_id
        downgraded["immutableHash"] = downgraded_hash
        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "disabled"},
        ), self._mock_clients_module(fake_fs), self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "cannot downgrade",
        ):
            pending_responses._pending_completion_linkage(
                "uid-1",
                fake_fs.collection("users").document("uid-1"),
                FakeDoc(downgraded_id, downgraded),
            )

        binding._data.update({
            "canonicalSourceId": "source-B",
            "workKey": "4" * 64,
            "proposalHash": "5" * 64,
            "selectionHash": "6" * 64,
            "pendingRevision": 999,
        })
        immutable = {
            field: binding._data[field]
            for field in pending_responses._PENDING_RESPONSE_SOURCE_BINDING_IMMUTABLE_FIELDS
        }
        binding._data["immutableHash"] = send_permits._stable_evidence_hash(
            immutable
        )
        binding._data["claimBindingHash"] = send_permits._stable_evidence_hash({
            "immutableHash": binding._data["immutableHash"],
            "pendingRevision": binding._data["pendingRevision"],
        })
        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "disabled"},
        ), self._mock_clients_module(fake_fs), self.assertRaises(
            send_permits.GraphSendPermitBlocked
        ):
            pending_responses._pending_completion_linkage(
                "uid-1",
                fake_fs.collection("users").document("uid-1"),
                obligations[0],
            )

    def test_shadow_pending_paths_have_zero_firestore_or_provider_effects(self):
        fake_fs = MagicMock(name="firestore")
        with patch.dict(
            os.environ,
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "shadow"},
        ), self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread"
        ) as graph_send:
            with self.assertRaises(pending_responses.PendingResponseRetryable):
                pending_responses.queue_pending_response(
                    "uid-1",
                    "thread-A",
                    "message-A",
                    "broker@example.com",
                    "Source A body",
                    "client-1",
                    canonical_source_id="source-A",
                    work_key="1" * 64,
                    proposal_hash="2" * 64,
                    selection_hash="3" * 64,
                )
            with self.assertRaises(pending_responses.PendingResponseRetryable):
                pending_responses.record_sent_unindexed_response(
                    "uid-1",
                    "thread-A",
                    "message-A",
                    "broker@example.com",
                    "Source A body",
                    "client-1",
                    "Sent indexing failed",
                    canonical_source_id="source-A",
                    work_key="1" * 64,
                    proposal_hash="2" * 64,
                    selection_hash="3" * 64,
                )
            with self.assertRaises(pending_responses.PendingResponseRetryable):
                pending_responses.require_pending_response_exact(
                    "uid-1",
                    "thread-A",
                    "source-A",
                    "1" * 64,
                )
            states = pending_responses.process_pending_responses(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertEqual([], states)
        fake_fs.collection.assert_not_called()
        graph_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
