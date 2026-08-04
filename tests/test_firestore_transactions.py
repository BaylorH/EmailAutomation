import unittest
from unittest.mock import MagicMock, patch

from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore
from google.cloud.firestore_v1 import types

from email_automation.firestore_transactions import run_firestore_transaction


class _FakeTransaction:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


class _FakeClient:
    def __init__(self):
        self.transaction_instance = _FakeTransaction()

    def transaction(self):
        return self.transaction_instance


class FirestoreTransactionRunnerTests(unittest.TestCase):
    def test_local_fake_read_only_callback_does_not_commit(self):
        client = _FakeClient()

        result = run_firestore_transaction(
            client,
            lambda _transaction: "read",
            read_only=True,
        )

        self.assertEqual("read", result)
        self.assertEqual(0, client.transaction_instance.commits)

    def test_requested_attempt_bound_is_forwarded_to_real_client_factory(self):
        client = firestore.Client(
            project="local-transaction-attempt-bound-test",
            credentials=AnonymousCredentials(),
        )
        transaction = client.transaction(max_attempts=1)

        def begin(*, retry_id=None):
            transaction._id = b"bounded-transaction-id"

        with patch.object(
            client,
            "transaction",
            return_value=transaction,
        ) as transaction_factory, patch.object(
            transaction,
            "_begin",
            side_effect=begin,
        ), patch.object(
            transaction,
            "_commit",
            return_value=[],
        ), patch.object(
            transaction,
            "_rollback",
        ):
            result = run_firestore_transaction(
                client,
                lambda _active: "bounded",
                max_attempts=1,
            )

        self.assertEqual("bounded", result)
        transaction_factory.assert_called_once_with(max_attempts=1)

    def test_local_fake_callback_is_committed_once(self):
        client = _FakeClient()

        result = run_firestore_transaction(
            client,
            lambda transaction: (transaction, "result"),
        )

        self.assertIs(client.transaction_instance, result[0])
        self.assertEqual("result", result[1])
        self.assertEqual(1, client.transaction_instance.commits)

    def test_real_transaction_is_active_before_callback_reads(self):
        client = firestore.Client(
            project="local-transaction-lifecycle-test",
            credentials=AnonymousCredentials(),
        )
        transaction = client.transaction(max_attempts=1)
        callback_states = []

        class ActiveTransactionRead:
            @staticmethod
            def get(*, transaction):
                if transaction.in_progress is not True:
                    raise ValueError(
                        "Transaction not in progress, cannot be used in API requests."
                    )
                callback_states.append(transaction.id)
                return "snapshot"

        def begin(*, retry_id=None):
            transaction._id = b"local-transaction-id"

        with patch.object(
            client,
            "transaction",
            return_value=transaction,
        ), patch.object(
            transaction,
            "_begin",
            side_effect=begin,
        ) as begin_mock, patch.object(
            transaction,
            "_commit",
            return_value=[],
        ) as commit_mock, patch.object(
            transaction,
            "_rollback",
        ) as rollback_mock:
            result = run_firestore_transaction(
                client,
                lambda active: ActiveTransactionRead.get(
                    transaction=active,
                ),
            )

        self.assertEqual("snapshot", result)
        self.assertEqual([b"local-transaction-id"], callback_states)
        begin_mock.assert_called_once_with(retry_id=None)
        commit_mock.assert_called_once_with()
        rollback_mock.assert_not_called()

    def test_real_read_only_transaction_carries_id_through_read_and_commit(self):
        client = firestore.Client(
            project="local-read-only-transaction-test",
            credentials=AnonymousCredentials(),
        )
        firestore_api = MagicMock(name="firestore_api")
        firestore_api.begin_transaction.return_value = (
            types.BeginTransactionResponse(
                transaction=b"read-only-transaction-id",
            )
        )
        document_ref = client.collection("tests").document("read-only")
        firestore_api.batch_get_documents.return_value = iter((
            types.BatchGetDocumentsResponse(
                missing=document_ref._document_path,
            ),
        ))
        firestore_api.commit.return_value = types.CommitResponse()
        firestore_api.rollback.return_value = None
        client._firestore_api_internal = firestore_api

        snapshot = run_firestore_transaction(
            client,
            lambda transaction: document_ref.get(transaction=transaction),
            max_attempts=1,
            read_only=True,
        )

        self.assertFalse(snapshot.exists)
        firestore_api.begin_transaction.assert_called_once()
        read_request = firestore_api.batch_get_documents.call_args.kwargs[
            "request"
        ]
        self.assertEqual(
            b"read-only-transaction-id",
            read_request["transaction"],
        )
        commit_request = firestore_api.commit.call_args.kwargs["request"]
        self.assertEqual(
            b"read-only-transaction-id",
            commit_request["transaction"],
        )
        self.assertEqual([], commit_request["writes"])


if __name__ == "__main__":
    unittest.main()
