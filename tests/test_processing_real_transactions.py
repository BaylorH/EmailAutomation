import ast
import itertools
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore
from google.cloud.firestore_v1 import types as firestore_types


os.environ.setdefault("E2E_TEST_MODE", "true")
with patch("google.cloud.firestore.Client", return_value=SimpleNamespace()):
    from email_automation import processing


class _FirestoreRpcHarness:
    """Real Firestore references with every RPC intercepted before transport."""

    def __init__(self):
        self.client = firestore.Client(
            project="sitesift-processing-transaction-test",
            credentials=AnonymousCredentials(),
        )
        self.transaction_ids = (
            f"processing-transaction-{index}".encode("ascii")
            for index in itertools.count(1)
        )
        self.api = MagicMock(name="firestore_api")
        self.api.begin_transaction.side_effect = self._begin_transaction
        self.api.batch_get_documents.side_effect = self._batch_get_documents
        self.api.commit.return_value = firestore_types.CommitResponse()
        self.api.rollback.return_value = None
        self.client._firestore_api_internal = self.api

    def _begin_transaction(self, *, request, metadata):
        return firestore_types.BeginTransactionResponse(
            transaction=next(self.transaction_ids),
        )

    def _batch_get_documents(self, *, request, metadata):
        return iter(
            firestore_types.BatchGetDocumentsResponse(missing=document_name)
            for document_name in request["documents"]
        )


class RealFirestoreProcessingTransactionLifecycleTests(unittest.TestCase):
    def test_processing_failure_write_begins_real_transaction_before_reads(self):
        harness = _FirestoreRpcHarness()

        with patch.object(processing, "_fs", harness.client):
            recorded = processing._record_ai_processing_failure(
                "user-real-transaction",
                "client-real-transaction",
                "thread-real-transaction",
                "message-real-transaction",
                "retryable processing failure",
                graph_message_id="message-real-transaction",
            )

        self.assertTrue(recorded)
        self.assertEqual(1, harness.api.begin_transaction.call_count)
        self.assertGreaterEqual(
            harness.api.batch_get_documents.call_count,
            1,
        )
        for call in harness.api.batch_get_documents.call_args_list:
            self.assertEqual(
                b"processing-transaction-1",
                call.kwargs["request"]["transaction"],
            )
        commit_request = harness.api.commit.call_args.kwargs["request"]
        self.assertEqual(
            b"processing-transaction-1",
            commit_request["transaction"],
        )
        self.assertEqual(1, len(commit_request["writes"]))

    def test_processing_has_no_manual_firestore_transaction_lifecycle(self):
        source_path = Path(processing.__file__)
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


if __name__ == "__main__":
    unittest.main()
