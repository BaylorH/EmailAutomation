import hashlib
import json
import os
import unittest
from copy import deepcopy
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "service-account.json",
    ),
)

from email_automation import reply_reviews


class FakeSnapshot:
    def __init__(self, data, *, path):
        self._data = deepcopy(data) if data is not None else None
        self.exists = data is not None
        self.id = path[-1]

    def to_dict(self):
        return deepcopy(self._data or {})


class FakeDocument:
    def __init__(self, firestore, path=()):
        self.firestore = firestore
        self.path = tuple(path)
        self.id = self.path[-1] if self.path else None

    def collection(self, name):
        return FakeDocument(self.firestore, self.path + (name,))

    def document(self, doc_id):
        return FakeDocument(self.firestore, self.path + (doc_id,))

    def get(self, transaction=None):
        if transaction is None:
            data = self.firestore.store.get(self.path)
            return FakeSnapshot(data, path=self.path)
        return transaction.get(self)


class FakeTransaction:
    def __init__(self, firestore):
        self.firestore = firestore
        self.reads = []
        self.operations = []
        self._write_started = False

    def get(self, reference):
        if self._write_started:
            raise AssertionError("Firestore transaction read occurred after a write")
        self.reads.append(reference.path)
        data = self.firestore.store.get(reference.path)
        return FakeSnapshot(data, path=reference.path)

    def set(self, reference, data, merge=False):
        self._write_started = True
        self.operations.append(("set", reference.path, deepcopy(data), merge))

    def update(self, reference, data):
        self._write_started = True
        self.operations.append(("update", reference.path, deepcopy(data), True))

    def delete(self, reference):
        self._write_started = True
        self.operations.append(("delete", reference.path, None, False))

    def commit(self):
        if self.firestore.fail_next_commit:
            self.firestore.fail_next_commit = False
            raise RuntimeError("atomic projection commit failed")
        staged = deepcopy(self.firestore.store)
        for operation, path, data, merge in self.operations:
            if operation == "delete":
                staged.pop(path, None)
                continue
            current = deepcopy(staged.get(path) or {}) if merge else {}
            current.update(deepcopy(data))
            staged[path] = current
        self.firestore.store = staged
        self.firestore.committed_operations.extend(deepcopy(self.operations))


class FakeFirestore:
    def __init__(self, store=None):
        self.store = deepcopy(store or {})
        self.transactions = []
        self.committed_operations = []
        self.fail_next_commit = False

    def collection(self, name):
        return FakeDocument(self, (name,))

    def transaction(self):
        transaction = FakeTransaction(self)
        self.transactions.append(transaction)
        return transaction


def fake_transactional(callback):
    def run(transaction, *args, **kwargs):
        result = callback(transaction, *args, **kwargs)
        transaction.commit()
        return result

    return run


class ReplyReviewProjectionTests(unittest.TestCase):
    USER_ID = "uid-1"
    CLIENT_ID = "client-1"
    THREAD_ID = "thread-1"
    SOURCE_MESSAGE_ID = "message-1"
    RECIPIENT = "contact@example.test"
    RESPONSE_BODY = "Hi,\n\nThanks."
    SUBJECT = "Re: Example"
    CONVERSATION_ID = "conversation-1"

    @property
    def client_path(self):
        return ("users", self.USER_ID, "clients", self.CLIENT_ID)

    @property
    def thread_path(self):
        return ("users", self.USER_ID, "threads", self.THREAD_ID)

    @property
    def review_id(self):
        return hashlib.sha256(
            b"blocked-auto-reply:v1\nthread-1\nmessage-1"
        ).hexdigest()

    @property
    def notification_id(self):
        return hashlib.sha1(
            f"reply-review-required:v1\n{self.review_id}".encode("utf-8")
        ).hexdigest()

    @property
    def review_path(self):
        return ("users", self.USER_ID, "deadLetterQueue", self.review_id)

    @property
    def notification_path(self):
        return self.client_path + ("notifications", self.notification_id)

    def _firestore(self, *, client=True, thread=True):
        store = {}
        if client:
            store[self.client_path] = {
                "status": "live",
                "notificationsUnread": 4,
                "newUpdateCount": 2,
                "notifCounts": {"sheet_update": 2, "action_needed": 1},
            }
        if thread:
            store[self.thread_path] = {
                "clientId": self.CLIENT_ID,
                "status": "active",
                "followUpStatus": "waiting",
            }
        return FakeFirestore(store)

    def _create(self, firestore, **overrides):
        values = {
            "user_id": self.USER_ID,
            "client_id": self.CLIENT_ID,
            "thread_id": self.THREAD_ID,
            "source_message_id": self.SOURCE_MESSAGE_ID,
            "recipient": self.RECIPIENT,
            "response_body": self.RESPONSE_BODY,
            "subject": self.SUBJECT,
            "conversation_id": self.CONVERSATION_ID,
        }
        values.update(overrides)
        with patch.object(reply_reviews, "_fs", firestore), patch.object(
            reply_reviews.firestore, "transactional", fake_transactional
        ):
            return reply_reviews.create_policy_blocked_reply_review(**values)

    def test_builds_stable_review_and_notification_ids(self):
        self.assertEqual(
            self.review_id,
            reply_reviews.build_policy_blocked_reply_review_id(
                thread_id=self.THREAD_ID,
                source_message_id=self.SOURCE_MESSAGE_ID,
            ),
        )
        self.assertEqual(
            self.notification_id,
            reply_reviews.build_policy_blocked_reply_review_notification_id(
                self.review_id
            ),
        )

    def test_builds_canonical_intent_hash_independent_of_mapping_order(self):
        intent = {
            "clientId": self.CLIENT_ID,
            "conversationId": self.CONVERSATION_ID,
            "recipient": self.RECIPIENT,
            "responseBody": self.RESPONSE_BODY,
            "sourceMessageId": self.SOURCE_MESSAGE_ID,
            "subject": self.SUBJECT,
            "terminalDisposition": None,
            "threadId": self.THREAD_ID,
        }
        expected = hashlib.sha256(
            json.dumps(
                intent,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()

        self.assertEqual(
            expected,
            reply_reviews.build_policy_blocked_reply_intent_hash(
                dict(reversed(list(intent.items())))
            ),
        )

    def test_create_writes_closed_review_notification_rollup_and_pause_atomically(self):
        firestore = self._firestore()

        result = self._create(firestore)

        self.assertEqual("created", result.status)
        self.assertEqual(self.review_id, result.review_id)
        self.assertEqual(self.notification_id, result.notification_id)
        self.assertEqual(
            [
                self.client_path,
                self.thread_path,
                self.review_path,
                self.notification_path,
            ],
            firestore.transactions[0].reads,
        )

        review = firestore.store[self.review_path]
        self.assertEqual(
            {
                "recordType",
                "schemaVersion",
                "reviewId",
                "failureCode",
                "status",
                "recoveryStatus",
                "manualActionRequired",
                "automaticRetryAllowed",
                "alreadySent",
                "source",
                "clientId",
                "threadId",
                "sourceMessageId",
                "conversationId",
                "recipient",
                "subject",
                "responseBody",
                "terminalDisposition",
                "draftVersion",
                "intentHash",
                "notificationId",
                "createdAt",
                "updatedAt",
            },
            set(review),
        )
        self.assertEqual("reply_review", review["recordType"])
        self.assertEqual(1, review["schemaVersion"])
        self.assertEqual(self.review_id, review["reviewId"])
        self.assertEqual("blocked_auto_reply_policy", review["failureCode"])
        self.assertEqual("needs_review", review["status"])
        self.assertEqual("needs_review", review["recoveryStatus"])
        self.assertIs(review["manualActionRequired"], True)
        self.assertIs(review["automaticRetryAllowed"], False)
        self.assertIs(review["alreadySent"], False)
        self.assertEqual("autoResponse", review["source"])
        self.assertEqual(self.CLIENT_ID, review["clientId"])
        self.assertEqual(self.THREAD_ID, review["threadId"])
        self.assertEqual(self.SOURCE_MESSAGE_ID, review["sourceMessageId"])
        self.assertEqual(self.CONVERSATION_ID, review["conversationId"])
        self.assertEqual(self.RECIPIENT, review["recipient"])
        self.assertEqual(self.SUBJECT, review["subject"])
        self.assertEqual(self.RESPONSE_BODY, review["responseBody"])
        self.assertIsNone(review["terminalDisposition"])
        self.assertEqual(1, review["draftVersion"])
        self.assertEqual(self.notification_id, review["notificationId"])
        self.assertNotIn("retryable", review)

        notification = firestore.store[self.notification_path]
        self.assertEqual(
            {"kind", "priority", "threadId", "meta", "createdAt"},
            set(notification),
        )
        self.assertEqual("action_needed", notification["kind"])
        self.assertEqual("important", notification["priority"])
        self.assertEqual(self.THREAD_ID, notification["threadId"])
        self.assertEqual(
            {
                "reason": "reply_review_required",
                "failureCode": "blocked_auto_reply_policy",
                "reviewActionMode": "projection_only",
                "reviewId": self.review_id,
                "sourceMessageId": self.SOURCE_MESSAGE_ID,
                "suggestedEmail": {
                    "to": [self.RECIPIENT],
                    "subject": self.SUBJECT,
                    "body": self.RESPONSE_BODY,
                },
            },
            notification["meta"],
        )

        client = firestore.store[self.client_path]
        self.assertEqual(5, client["notificationsUnread"])
        self.assertEqual(2, client["newUpdateCount"])
        self.assertEqual(
            {"sheet_update": 2, "action_needed": 2}, client["notifCounts"]
        )

        thread = firestore.store[self.thread_path]
        self.assertEqual("action_needed", thread["status"])
        self.assertEqual("blocked_auto_reply_policy", thread["statusReason"])
        self.assertEqual("stopped", thread["followUpStatus"])
        self.assertIs(thread["followUpConfig.enabled"], False)
        self.assertIsNone(thread["followUpConfig.nextFollowUpAt"])
        self.assertIsNone(thread["followUpConfig.processingBy"])
        self.assertIsNone(thread["followUpConfig.processingAt"])
        thread_operations = [
            operation
            for operation in firestore.committed_operations
            if operation[1] == self.thread_path
        ]
        self.assertEqual("update", thread_operations[0][0])

        touched_paths = [operation[1] for operation in firestore.committed_operations]
        self.assertFalse(
            any("pendingResponses" in path or "outbox" in path for path in touched_paths)
        )

    def test_exact_replay_is_a_noop_without_counter_increment(self):
        firestore = self._firestore()
        self._create(firestore)
        first_state = deepcopy(firestore.store)
        first_write_count = len(firestore.committed_operations)

        result = self._create(firestore)

        self.assertEqual("existing", result.status)
        self.assertEqual(first_state, firestore.store)
        self.assertEqual(first_write_count, len(firestore.committed_operations))
        self.assertEqual(2, len(firestore.transactions))
        self.assertEqual(
            [
                self.client_path,
                self.thread_path,
                self.review_path,
                self.notification_path,
            ],
            firestore.transactions[-1].reads,
        )

    def test_nullable_subject_is_preserved_without_weakening_projection(self):
        firestore = self._firestore()

        result = self._create(firestore, subject=None)

        self.assertEqual("created", result.status)
        self.assertIsNone(firestore.store[self.review_path]["subject"])
        self.assertIsNone(
            firestore.store[self.notification_path]["meta"]["suggestedEmail"]["subject"]
        )

    def test_completed_terminal_disposition_is_preserved_in_closed_shape(self):
        firestore = self._firestore()
        disposition = {
            "status": "completed",
            "reason": "all_fields_gathered",
            "rowNumber": 7,
        }

        self._create(firestore, terminal_disposition=disposition)

        self.assertEqual(
            disposition,
            firestore.store[self.review_path]["terminalDisposition"],
        )

    def test_invalid_terminal_disposition_fails_before_firestore_access(self):
        invalid_dispositions = (
            {},
            {"status": "completed", "reason": "all_fields_gathered", "rowNumber": 7, "extra": True},
            {"status": "active", "reason": "all_fields_gathered", "rowNumber": 7},
            {"status": "completed", "reason": " ", "rowNumber": 7},
            {"status": "completed", "reason": "all_fields_gathered", "rowNumber": 0},
            {"status": "completed", "reason": "all_fields_gathered", "rowNumber": True},
        )
        for disposition in invalid_dispositions:
            with self.subTest(disposition=disposition):
                firestore = self._firestore()
                with self.assertRaises(ValueError):
                    self._create(firestore, terminal_disposition=disposition)
                self.assertEqual([], firestore.transactions)

    def test_same_identity_with_different_intent_fails_closed_without_writes(self):
        firestore = self._firestore()
        self._create(firestore)
        before = deepcopy(firestore.store)
        write_count = len(firestore.committed_operations)

        with self.assertRaises(reply_reviews.ReplyReviewConflict):
            self._create(firestore, response_body="A different preserved draft")

        self.assertEqual(before, firestore.store)
        self.assertEqual(write_count, len(firestore.committed_operations))

    def test_tampered_existing_review_is_not_accepted_as_exact_replay(self):
        tampered_values = (
            ("responseBody", "tampered body"),
            ("recipient", "different@example.test"),
            ("status", "resolved"),
            ("automaticRetryAllowed", True),
            ("createdAt", None),
        )
        for field, value in tampered_values:
            with self.subTest(field=field):
                firestore = self._firestore()
                self._create(firestore)
                if field == "createdAt":
                    firestore.store[self.review_path].pop(field)
                else:
                    firestore.store[self.review_path][field] = value
                before = deepcopy(firestore.store)
                write_count = len(firestore.committed_operations)

                with self.assertRaises(reply_reviews.ReplyReviewConflict):
                    self._create(firestore)

                self.assertEqual(before, firestore.store)
                self.assertEqual(write_count, len(firestore.committed_operations))

    def test_missing_client_fails_closed_without_writes(self):
        firestore = self._firestore(client=False)
        before = deepcopy(firestore.store)

        with self.assertRaises(reply_reviews.ReplyReviewProjectionError):
            self._create(firestore)

        self.assertEqual(before, firestore.store)
        self.assertEqual([], firestore.committed_operations)

    def test_missing_thread_fails_closed_without_writes(self):
        firestore = self._firestore(thread=False)
        before = deepcopy(firestore.store)

        with self.assertRaises(reply_reviews.ReplyReviewProjectionError):
            self._create(firestore)

        self.assertEqual(before, firestore.store)
        self.assertEqual([], firestore.committed_operations)

    def test_thread_bound_to_another_or_missing_client_fails_closed(self):
        for bound_client_id in ("client-other", None):
            with self.subTest(bound_client_id=bound_client_id):
                firestore = self._firestore()
                if bound_client_id is None:
                    firestore.store[self.thread_path].pop("clientId")
                else:
                    firestore.store[self.thread_path]["clientId"] = bound_client_id
                before = deepcopy(firestore.store)

                with self.assertRaises(reply_reviews.ReplyReviewConflict):
                    self._create(firestore)

                self.assertEqual(before, firestore.store)
                self.assertEqual([], firestore.committed_operations)

    def test_preexisting_notification_without_review_is_a_conflict(self):
        firestore = self._firestore()
        firestore.store[self.notification_path] = {
            "kind": "action_needed",
            "meta": {"reason": "unrelated"},
        }
        before = deepcopy(firestore.store)

        with self.assertRaises(reply_reviews.ReplyReviewConflict):
            self._create(firestore)

        self.assertEqual(before, firestore.store)
        self.assertEqual([], firestore.committed_operations)

    def test_commit_failure_is_wrapped_and_leaves_no_partial_projection(self):
        firestore = self._firestore()
        before = deepcopy(firestore.store)
        firestore.fail_next_commit = True

        with self.assertRaisesRegex(
            reply_reviews.ReplyReviewProjectionError,
            "policy-blocked reply review projection failed",
        ):
            self._create(firestore)

        self.assertEqual(before, firestore.store)
        self.assertEqual([], firestore.committed_operations)

    def test_invalid_required_values_fail_before_firestore_access(self):
        invalid_cases = {
            "user_id": " ",
            "client_id": "",
            "thread_id": None,
            "source_message_id": "\t",
            "recipient": " ",
            "response_body": "\n\t",
        }
        for field, value in invalid_cases.items():
            with self.subTest(field=field):
                firestore = self._firestore()
                with self.assertRaises(ValueError):
                    self._create(firestore, **{field: value})
                self.assertEqual([], firestore.transactions)

    def test_oversized_draft_fields_fail_before_firestore_access(self):
        for field, value in (
            ("recipient", "x" * 321),
            ("subject", "x" * 999),
            ("response_body", "x" * 100001),
        ):
            with self.subTest(field=field):
                firestore = self._firestore()
                with self.assertRaises(ValueError):
                    self._create(firestore, **{field: value})
                self.assertEqual([], firestore.transactions)


if __name__ == "__main__":
    unittest.main()
