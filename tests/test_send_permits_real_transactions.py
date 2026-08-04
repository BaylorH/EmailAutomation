import ast
import copy
import itertools
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore
from google.cloud.firestore_v1 import _helpers, types

from email_automation import send_permits
from tests import test_send_permits as permit_fixtures


class _FirestoreRpcHarness:
    """Real Firestore references with every RPC intercepted before transport."""

    def __init__(self, documents):
        self.client = firestore.Client(
            project="sitesift-transaction-test",
            credentials=AnonymousCredentials(),
        )
        self.documents = {
            path.strip("/"): self._rpc_safe(copy.deepcopy(data))
            for path, data in documents.items()
        }
        self.transaction_ids = (
            f"transaction-{index}".encode("ascii")
            for index in itertools.count(1)
        )
        self.api = MagicMock(name="firestore_api")
        self.api.begin_transaction.side_effect = self._begin_transaction
        self.api.batch_get_documents.side_effect = self._batch_get_documents
        self.api.commit.return_value = types.CommitResponse()
        self.api.rollback.return_value = None
        # The client lazily resolves this attribute for every RPC. Replacing it
        # before creating references guarantees this test cannot reach network.
        self.client._firestore_api_internal = self.api

    @classmethod
    def _rpc_safe(cls, value):
        if value is send_permits.SERVER_TIMESTAMP:
            return datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
        if isinstance(value, dict):
            return {key: cls._rpc_safe(nested) for key, nested in value.items()}
        if isinstance(value, list):
            return [cls._rpc_safe(nested) for nested in value]
        if isinstance(value, tuple):
            return [cls._rpc_safe(nested) for nested in value]
        return value

    def _begin_transaction(self, *, request, metadata):
        return types.BeginTransactionResponse(
            transaction=next(self.transaction_ids),
        )

    def _batch_get_documents(self, *, request, metadata):
        responses = []
        for document_name in request["documents"]:
            relative_path = document_name.split("/documents/", 1)[1]
            data = self.documents.get(relative_path)
            if data is None:
                responses.append(types.BatchGetDocumentsResponse(
                    missing=document_name,
                ))
            else:
                responses.append(types.BatchGetDocumentsResponse(
                    found=types.Document(
                        name=document_name,
                        fields=_helpers.encode_dict(data),
                    ),
                ))
        return iter(responses)


class RealFirestorePendingTransactionLifecycleTests(unittest.TestCase):
    USER_ID = "u-real-transaction"

    def test_send_permits_has_no_manual_firestore_transaction_lifecycle(self):
        source_path = Path(send_permits.__file__)
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        unsupported = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not isinstance(function, ast.Attribute):
                continue
            if function.attr == "transaction":
                unsupported.append((node.lineno, "manual transaction factory"))
            elif function.attr == "commit":
                unsupported.append((node.lineno, "manual transaction commit"))

        self.assertEqual([], unsupported)

    def test_exact_pending_issue_begins_transaction_before_reads_and_callback(self):
        thread_id = "thread-real-issue"
        claim_token = "pending-response-b1-" + "a" * 64 + "-" + "b" * 32
        loaded = {
            **permit_fixtures._pending_data(thread_id, token=claim_token),
            "pendingProtocol": {"kind": "b1_exact_source", "version": 1},
            "canonicalSourceId": "source-real-issue",
            "workKey": "1" * 64,
            "proposalHash": "2" * 64,
            "selectionHash": "3" * 64,
            "pendingRevision": 2,
        }
        harness = _FirestoreRpcHarness({
            f"users/{self.USER_ID}/threads/{thread_id}": {
                "clientId": loaded["clientId"],
            },
            f"users/{self.USER_ID}/pendingResponses/{thread_id}": loaded,
        })
        user_ref = harness.client.collection("users").document(self.USER_ID)
        thread_ref = user_ref.collection("threads").document(thread_id)
        pending_ref = user_ref.collection("pendingResponses").document(thread_id)
        callback_transaction_ids = []

        def exact_claim_validator(transaction, current, current_claim_token):
            callback_transaction_ids.append(transaction.id)
            self.assertEqual(loaded, current)
            self.assertEqual(claim_token, current_claim_token)

        capability = send_permits.issue_pending_graph_send_permit(
            harness.client,
            thread_ref,
            pending_ref,
            loaded,
            claim_token,
            require_exact_client_binding=True,
            exact_claim_validator=exact_claim_validator,
        )

        self.assertIsNotNone(capability)
        self.assertEqual([b"transaction-1"], callback_transaction_ids)
        self.assertEqual(1, harness.api.begin_transaction.call_count)
        for call in harness.api.batch_get_documents.call_args_list:
            self.assertEqual(b"transaction-1", call.kwargs["request"]["transaction"])
        commit_request = harness.api.commit.call_args.kwargs["request"]
        self.assertEqual(b"transaction-1", commit_request["transaction"])

    def test_settled_sent_completion_uses_one_active_transaction(self):
        fixture = permit_fixtures.SendPermitTests(methodName="runTest")
        fixture.setUp()
        prepared = fixture._prepare_pending_draft(
            thread_id="thread-real-settled-sent",
            canonical_user_id=self.USER_ID,
        )
        send_permits.consume_graph_send_capability(
            prepared["capability"],
            source_graph_message_id=prepared["source_id"],
            draft_id=prepared["draft_id"],
            subject=prepared["subject"],
            html_body=prepared["html_body"],
            to_recipients=prepared["to_recipients"],
            cc_recipients=prepared["cc_recipients"],
            attachments=prepared["attachments"],
        )
        send_permits.resolve_graph_send_permit(
            prepared["capability"],
            "accepted",
            evidence={"httpStatus": 202, "phase": "send"},
        )
        sent_evidence = permit_fixtures._exact_sent_evidence(
            prepared["capability"],
            html_body=prepared["html_body"],
        )

        fake_capability = prepared["capability"]
        thread_path = prepared["thread_ref"].path
        pending_path = prepared["pending_ref"].path
        permit_path = fake_capability.permit_ref.path
        harness = _FirestoreRpcHarness({
            thread_path: prepared["thread_ref"].data,
            pending_path: prepared["pending_ref"].data,
            permit_path: fake_capability.permit_ref.data,
        })
        thread_ref = harness.client.document(thread_path)
        pending_ref = harness.client.document(pending_path)
        permit_ref = harness.client.document(permit_path)
        capability = send_permits.GraphSendCapability(
            permit_id=fake_capability.permit_id,
            immutable_hash=fake_capability.immutable_hash,
            issuer_kind=fake_capability.issuer_kind,
            issuer_owner=fake_capability.issuer_owner,
            issuer_fence=fake_capability.issuer_fence,
            envelope_hash=fake_capability.envelope_hash,
            capability=fake_capability.capability,
            firestore_client=harness.client,
            thread_ref=thread_ref,
            permit_ref=permit_ref,
            issuer_ref=pending_ref,
        )
        obligation_id, obligation_payload = (
            send_permits.pending_completion_obligation_payload(
                user_id=self.USER_ID,
                client_id=prepared["loaded"]["clientId"],
                thread_id=prepared["loaded"]["threadId"],
                pending_document_id=pending_ref.id,
                source_graph_message_id=prepared["loaded"]["msgId"],
                pending_envelope_hash_value=capability.envelope_hash,
                permit_id=capability.permit_id,
                permit_immutable_hash=capability.immutable_hash,
                sent_evidence=sent_evidence,
                complete_client_after_reply=True,
            )
        )
        completion_ref = (
            harness.client.collection("users").document(self.USER_ID)
            .collection(send_permits.PENDING_COMPLETION_OBLIGATION_COLLECTION)
            .document(obligation_id)
        )

        result = send_permits.cas_pending_claim_transition(
            harness.client,
            thread_ref,
            pending_ref,
            prepared["loaded"],
            prepared["loaded"]["processingBy"],
            delete_pending=True,
            capability=capability,
            permit_settlement="settled_sent",
            sent_evidence=sent_evidence,
            side_documents=((completion_ref, obligation_payload),),
        )

        self.assertTrue(result)
        self.assertEqual(1, harness.api.begin_transaction.call_count)
        for call in harness.api.batch_get_documents.call_args_list:
            self.assertEqual(b"transaction-1", call.kwargs["request"]["transaction"])
        commit_request = harness.api.commit.call_args.kwargs["request"]
        self.assertEqual(b"transaction-1", commit_request["transaction"])
        write_paths = {
            write.update.name or write.delete
            for write in commit_request["writes"]
        }
        self.assertIn(pending_ref._document_path, write_paths)
        self.assertIn(completion_ref._document_path, write_paths)
        self.assertIn(permit_ref._document_path, write_paths)


if __name__ == "__main__":
    unittest.main()
