import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import StringIO
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch


os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "service-account.json",
    ),
)

from email_automation import operator_replay
from email_automation.utils import b64url_id, normalize_message_id


BAYLOR_UID = "NO7lVYVp6BaplKYEfMlWCgBnpdh2"
CLIENT_ID = "client-bp21"
THREAD_ID = "thread-bp21"
GRAPH_MESSAGE_ID = "graph-inbox-message-1"
INTERNET_MESSAGE_ID = "<bp21-reply-1@example.test>"
CONVERSATION_ID = "graph-conversation-1"
SENDER = "bp21harrison@gmail.com"
OPERATOR_RECIPIENT = "baylor.freelance@outlook.com"


class _Snapshot:
    def __init__(self, store, path):
        self._store = store
        self._path = path
        self.id = path[-1]

    @property
    def exists(self):
        return self._path in self._store.data

    def to_dict(self):
        return deepcopy(self._store.data.get(self._path) or {})

    @property
    def reference(self):
        return _DocRef(self._store, self._path)


class _DocRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def collection(self, name):
        return _CollectionRef(self._store, self._path + (name,))

    @property
    def id(self):
        return self._path[-1]

    def get(self):
        self._store.events.append(("get", self._path))
        return _Snapshot(self._store, self._path)

    def set(self, payload, merge=False):
        self._store.events.append(("set", self._path))
        if merge and self._path in self._store.data:
            self._store.data[self._path].update(deepcopy(payload))
        else:
            self._store.data[self._path] = deepcopy(payload)

    def delete(self):
        self._store.events.append(("delete", self._path))
        self._store.data.pop(self._path, None)


class _CollectionRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def document(self, doc_id):
        return _DocRef(self._store, self._path + (doc_id,))


class _Batch:
    def __init__(self, store):
        self._store = store
        self._operations = []

    def set(self, ref, payload, merge=False):
        self._operations.append(("set", ref._path, deepcopy(payload), merge))

    def create(self, ref, payload):
        self._operations.append(("create", ref._path, deepcopy(payload), False))

    def delete(self, ref):
        self._operations.append(("delete", ref._path, None, False))

    def commit(self):
        self._store.batch_commit_count += 1
        if self._store.fail_batch_commit_number == self._store.batch_commit_count:
            raise RuntimeError("atomic batch commit failed")
        for operation, path, *_ in self._operations:
            if operation == "create" and path in self._store.data:
                raise RuntimeError("document already exists")
        self._store.events.append(
            ("batch_commit", tuple((operation, path) for operation, path, *_ in self._operations))
        )
        for operation, path, payload, merge in self._operations:
            if operation == "create":
                self._store.data[path] = deepcopy(payload)
            elif operation == "set":
                if merge and path in self._store.data:
                    self._store.data[path].update(deepcopy(payload))
                else:
                    self._store.data[path] = deepcopy(payload)
            else:
                self._store.data.pop(path, None)


class _FakeFirestore:
    def __init__(self):
        self.data = {}
        self.events = []
        self.batch_commit_count = 0
        self.fail_batch_commit_number = None

    def collection(self, name):
        return _CollectionRef(self, (name,))

    def batch(self):
        return _Batch(self)


def _request(**overrides):
    values = {
        "uid": BAYLOR_UID,
        "client_id": CLIENT_ID,
        "thread_id": THREAD_ID,
        "graph_message_id": GRAPH_MESSAGE_ID,
        "internet_message_id": INTERNET_MESSAGE_ID,
        "sender": SENDER,
        "operator_recipient": OPERATOR_RECIPIENT,
    }
    values.update(overrides)
    return operator_replay.ReplayRequest(**values)


def _graph_message(**overrides):
    message = {
        "id": GRAPH_MESSAGE_ID,
        "internetMessageId": INTERNET_MESSAGE_ID,
        "conversationId": CONVERSATION_ID,
        "subject": "Re: BP21 property details",
        "receivedDateTime": "2026-07-12T18:00:00Z",
        "from": {"emailAddress": {"address": SENDER}},
        "sender": {"emailAddress": {"address": SENDER}},
        "replyTo": [{"emailAddress": {"address": SENDER}}],
        "toRecipients": [
            {"emailAddress": {"address": OPERATOR_RECIPIENT}},
        ],
        "ccRecipients": [],
        "internetMessageHeaders": [],
        "body": {"contentType": "Text", "content": "The rent is $12/SF."},
        "bodyPreview": "The rent is $12/SF.",
        "hasAttachments": False,
    }
    message.update(overrides)
    return message


def _failure_id():
    return f"{THREAD_ID}__{INTERNET_MESSAGE_ID}"


def _seed_valid_state(fs):
    fs.data[("users", BAYLOR_UID, "clients", CLIENT_ID)] = {
        "status": "live",
        "automationPaused": False,
    }
    fs.data[("systemConfig", "campaignAccess")] = {
        "automationEnabled": False,
        "allowedUids": [BAYLOR_UID],
    }
    fs.data[("users", BAYLOR_UID, "threads", THREAD_ID)] = {
        "clientId": CLIENT_ID,
        "status": "active",
        "email": ["bp21harrison+avery@gmail.com"],
    }
    fs.data[("users", BAYLOR_UID, "processingFailures", _failure_id())] = {
        "clientId": CLIENT_ID,
        "threadId": THREAD_ID,
        "messageId": INTERNET_MESSAGE_ID,
        "graphMessageId": GRAPH_MESSAGE_ID,
        "retryable": True,
        "reason": (
            "Broker asset extraction failed for 1 asset(s); leaving message "
            "unprocessed for retry/manual review"
        ),
        "createdAt": datetime(2026, 7, 12, 18, 1, tzinfo=timezone.utc),
    }
    canonical_id = normalize_message_id(INTERNET_MESSAGE_ID)
    fs.data[("users", BAYLOR_UID, "msgIndex", b64url_id(canonical_id))] = {
        "threadId": THREAD_ID,
    }
    fs.data[("users", BAYLOR_UID, "convIndex", CONVERSATION_ID)] = {
        "threadId": THREAD_ID,
    }
    fs.data[("users", BAYLOR_UID, "processingFailures", "unrelated-failure")] = {
        "clientId": "other-client",
        "threadId": "other-thread",
        "messageId": "<other@example.test>",
        "retryable": True,
    }


def _lease_runs(uid, callback, **kwargs):
    callback()
    return True


def _scheduler_lease_runs(callback, **kwargs):
    callback()
    return True


class OperatorReplayContractTests(unittest.TestCase):
    def setUp(self):
        self.fs = _FakeFirestore()
        _seed_valid_state(self.fs)
        self.fetch_message = MagicMock(return_value=_graph_message())
        self.process_message = MagicMock()
        self.find_existing_artifact = MagicMock(return_value=None)
        self.find_continuation = MagicMock(return_value=None)
        self.find_recipient_continuation = MagicMock(return_value=None)
        self.verify_postcondition = MagicMock(return_value=True)
        self.lease_runner = MagicMock(side_effect=_lease_runs)
        self.scheduler_lease_runner = MagicMock(side_effect=_scheduler_lease_runs)

    def replay(self, request=None, *, apply=False):
        return operator_replay.replay_exact_message(
            request or _request(),
            {"Authorization": "Bearer test-token"},
            apply=apply,
            fs_client=self.fs,
            fetch_message=self.fetch_message,
            process_message=self.process_message,
            find_existing_artifact=self.find_existing_artifact,
            find_manual_continuation=self.find_continuation,
            find_recipient_continuation=self.find_recipient_continuation,
            verify_postcondition=self.verify_postcondition,
            lease_runner=self.lease_runner,
            scheduler_lease_runner=self.scheduler_lease_runner,
        )

    def assert_refused(self, request=None, message=None):
        with self.assertRaisesRegex(operator_replay.ReplayRefused, message or ".+"):
            self.replay(request)
        self.process_message.assert_not_called()

    def test_dry_run_fetches_and_verifies_one_exact_message_without_processing(self):
        result = self.replay()

        self.assertFalse(result.applied)
        self.assertEqual("verified", result.status)
        self.fetch_message.assert_called_once_with(
            {"Authorization": "Bearer test-token"}, GRAPH_MESSAGE_ID
        )
        self.process_message.assert_not_called()
        self.find_continuation.assert_called_once()
        self.lease_runner.assert_called_once()
        self.scheduler_lease_runner.assert_called_once()
        self.assertEqual(30 * 60, self.lease_runner.call_args.kwargs["ttl_seconds"])
        self.assertEqual(
            10 * 60,
            self.scheduler_lease_runner.call_args.kwargs["ttl_seconds"],
        )

    def test_refuses_any_identity_mismatch(self):
        cases = [
            (_request(uid="another-user"), "Baylor UID"),
            (_request(client_id="another-client"), "client"),
            (_request(thread_id="another-thread"), "thread"),
            (_request(graph_message_id="another-graph-id"), "Graph message"),
            (_request(internet_message_id="<another@example.test>"), "RFC"),
            (_request(sender="other@gmail.com"), "BP21 sender"),
            (_request(operator_recipient="other@outlook.com"), "operator recipient"),
        ]

        for request, error in cases:
            with self.subTest(error=error):
                self.fetch_message.reset_mock()
                self.process_message.reset_mock()
                self.find_continuation.reset_mock()
                self.lease_runner.reset_mock()
                self.assert_refused(request, error)

    def test_refuses_graph_payload_identity_mismatches(self):
        cases = [
            ({"id": "wrong-graph"}, "Graph message"),
            ({"internetMessageId": "<wrong@example.test>"}, "RFC"),
            (
                {"from": {"emailAddress": {"address": "other@gmail.com"}}},
                "sender",
            ),
            (
                {
                    "toRecipients": [
                        {"emailAddress": {"address": "other@outlook.com"}}
                    ]
                },
                "recipient",
            ),
            (
                {
                    "ccRecipients": [
                        {"emailAddress": {"address": "third-party@example.com"}}
                    ]
                },
                "safe lane",
            ),
            (
                {
                    "replyTo": [
                        {"emailAddress": {"address": "third-party@example.com"}}
                    ]
                },
                "reply-to",
            ),
        ]

        for overrides, error in cases:
            with self.subTest(error=error):
                self.fetch_message.return_value = _graph_message(**overrides)
                self.assert_refused(message=error)
                self.process_message.reset_mock()

    def test_refuses_missing_or_mismatched_exact_failure(self):
        failure_path = (
            "users",
            BAYLOR_UID,
            "processingFailures",
            _failure_id(),
        )
        del self.fs.data[failure_path]
        self.assert_refused(message="failure")

        _seed_valid_state(self.fs)
        self.fs.data[failure_path]["graphMessageId"] = "wrong-graph"
        self.assert_refused(message="failure")

    def test_legacy_asset_failure_without_graph_id_is_verified_then_backfilled_on_apply(self):
        failure_path = (
            "users",
            BAYLOR_UID,
            "processingFailures",
            _failure_id(),
        )
        self.fs.data[failure_path].pop("graphMessageId")

        result = self.replay()
        self.assertEqual("verified", result.status)
        self.assertNotIn("graphMessageId", self.fs.data[failure_path])

        self.verify_postcondition.return_value = False
        with self.assertRaisesRegex(operator_replay.ReplayRefused, "postcondition"):
            self.replay(apply=True)
        self.assertEqual(
            GRAPH_MESSAGE_ID,
            self.fs.data[failure_path]["graphMessageId"],
        )
        first_commit = [
            event for event in self.fs.events if event[0] == "batch_commit"
        ][0]
        self.assertIn(("set", failure_path), first_commit[1])

    def test_missing_failure_graph_id_is_refused_for_any_other_failure_type(self):
        failure_path = (
            "users",
            BAYLOR_UID,
            "processingFailures",
            _failure_id(),
        )
        self.fs.data[failure_path].pop("graphMessageId")
        self.fs.data[failure_path]["reason"] = "generic processing failure"

        self.assert_refused(message="Graph message ID")

    def test_refuses_processed_graph_or_rfc_marker(self):
        for processed_id in (GRAPH_MESSAGE_ID, INTERNET_MESSAGE_ID):
            with self.subTest(processed_id=processed_id):
                fs = _FakeFirestore()
                _seed_valid_state(fs)
                fs.data[
                    (
                        "users",
                        BAYLOR_UID,
                        "processedMessages",
                        b64url_id(processed_id),
                    )
                ] = {"processedAt": "already"}
                self.fs = fs
                self.assert_refused(message="processed")
                self.process_message.reset_mock()

    def test_refuses_terminal_client_or_thread_state(self):
        client_path = ("users", BAYLOR_UID, "clients", CLIENT_ID)
        self.fs.data[client_path]["status"] = "stopped"
        self.assert_refused(message="client")

        self.fs = _FakeFirestore()
        _seed_valid_state(self.fs)
        thread_path = ("users", BAYLOR_UID, "threads", THREAD_ID)
        self.fs.data[thread_path]["status"] = "completed"
        self.assert_refused(message="thread")

    def test_refuses_wrong_message_or_conversation_index(self):
        msg_index_path = (
            "users",
            BAYLOR_UID,
            "msgIndex",
            b64url_id(normalize_message_id(INTERNET_MESSAGE_ID)),
        )
        self.fs.data[msg_index_path]["threadId"] = "wrong-thread"
        self.assert_refused(message="message index")

        self.fs = _FakeFirestore()
        _seed_valid_state(self.fs)
        conv_index_path = (
            "users",
            BAYLOR_UID,
            "convIndex",
            CONVERSATION_ID,
        )
        self.fs.data[conv_index_path]["threadId"] = "wrong-thread"
        self.assert_refused(message="conversation index")

    def test_refuses_conflicting_in_reply_to_message_index(self):
        outbound_id = "<outbound-root@example.test>"
        self.fetch_message.return_value = _graph_message(
            internetMessageHeaders=[
                {"name": "In-Reply-To", "value": outbound_id},
            ]
        )
        self.fs.data[
            (
                "users",
                BAYLOR_UID,
                "msgIndex",
                b64url_id(normalize_message_id(outbound_id)),
            )
        ] = {"threadId": "wrong-thread"}

        self.assert_refused(message="header message index")

    def test_refuses_sent_items_manual_continuation(self):
        self.find_continuation.return_value = {
            "id": "sent-message-after-failure",
            "conversationId": CONVERSATION_ID,
            "sentDateTime": "2026-07-12T18:05:00Z",
        }

        self.assert_refused(message="Sent Items")

    def test_refuses_manual_continuation_sent_as_new_conversation_to_bp21(self):
        self.find_recipient_continuation.return_value = {
            "id": "manual-new-conversation",
            "conversationId": "different-conversation",
        }

        self.assert_refused(message="recipient continuation")

        self.find_recipient_continuation.assert_called_once()
        self.assertEqual(
            SENDER,
            self.find_recipient_continuation.call_args.kwargs["recipient"],
        )

    def test_sent_items_guard_starts_from_earlier_inbound_time(self):
        inbound_time = datetime(2026, 7, 12, 17, 55, tzinfo=timezone.utc)
        failure_path = (
            "users",
            BAYLOR_UID,
            "processingFailures",
            _failure_id(),
        )
        self.fs.data[failure_path]["createdAt"] = inbound_time + timedelta(minutes=6)
        self.fetch_message.return_value = _graph_message(
            receivedDateTime=inbound_time.isoformat().replace("+00:00", "Z")
        )

        self.replay()

        sent_after = self.find_continuation.call_args.kwargs["sent_after"]
        self.assertEqual(inbound_time - timedelta(seconds=30), sent_after)

    def test_refuses_existing_recovery_or_outbound_artifact(self):
        self.find_existing_artifact.return_value = {
            "collection": "outbox",
            "id": "queued-reply",
            "sourceMessageId": INTERNET_MESSAGE_ID,
        }

        self.assert_refused(message="recovery artifact")
        self.find_continuation.assert_not_called()

    def test_refuses_when_user_lease_is_held(self):
        self.lease_runner.side_effect = None
        self.lease_runner.return_value = False

        self.assert_refused(message="lease")
        self.fetch_message.assert_not_called()

    def test_refuses_when_global_scheduler_lease_is_held(self):
        self.scheduler_lease_runner.side_effect = None
        self.scheduler_lease_runner.return_value = False

        self.assert_refused(message="global scheduler lease")
        self.lease_runner.assert_not_called()
        self.fetch_message.assert_not_called()

    def test_apply_processes_once_then_marks_both_ids_and_deletes_only_exact_failure(self):
        captured = {}

        def record_process(*args, **kwargs):
            self.assertFalse(kwargs["allow_outbound_reply"])
            replay_attempt_id = kwargs["operator_replay_attempt_id"]
            captured["replayAttemptId"] = replay_attempt_id
            for message_id in (GRAPH_MESSAGE_ID, INTERNET_MESSAGE_ID):
                marker_path = (
                    "users",
                    BAYLOR_UID,
                    "processedMessages",
                    b64url_id(message_id),
                )
                self.assertEqual(
                    "operator_replay_in_progress",
                    self.fs.data[marker_path]["status"],
                )
                self.assertEqual(
                    replay_attempt_id,
                    self.fs.data[marker_path]["replayAttemptId"],
                )
            self.fs.events.append(("process", args[2]["id"]))

        self.process_message.side_effect = record_process

        result = self.replay(apply=True)

        self.assertTrue(result.applied)
        self.assertEqual("applied", result.status)
        self.process_message.assert_called_once()
        graph_marker = (
            "users",
            BAYLOR_UID,
            "processedMessages",
            b64url_id(GRAPH_MESSAGE_ID),
        )
        rfc_marker = (
            "users",
            BAYLOR_UID,
            "processedMessages",
            b64url_id(INTERNET_MESSAGE_ID),
        )
        failure_path = (
            "users",
            BAYLOR_UID,
            "processingFailures",
            _failure_id(),
        )
        unrelated_path = (
            "users",
            BAYLOR_UID,
            "processingFailures",
            "unrelated-failure",
        )
        self.assertIn(graph_marker, self.fs.data)
        self.assertIn(rfc_marker, self.fs.data)
        self.assertEqual("processed", self.fs.data[graph_marker]["status"])
        self.assertEqual("processed", self.fs.data[rfc_marker]["status"])
        self.assertNotIn(failure_path, self.fs.data)
        self.assertIn(unrelated_path, self.fs.data)
        history_path = (
            "users",
            BAYLOR_UID,
            "processingFailureHistory",
            _failure_id(),
        )
        self.assertEqual("replayed", self.fs.data[history_path]["status"])
        self.assertEqual(_failure_id(), self.fs.data[history_path]["sourceFailureId"])
        self.assertFalse(self.fs.data[history_path]["retryable"])
        self.assertEqual("replayed", self.fs.data[history_path]["recoveryStatus"])
        self.assertEqual(
            captured["replayAttemptId"],
            self.fs.data[history_path]["replayAttemptId"],
        )
        self.assertEqual(CLIENT_ID, self.fs.data[history_path]["clientId"])
        self.assertEqual(THREAD_ID, self.fs.data[history_path]["threadId"])

        commits = [event for event in self.fs.events if event[0] == "batch_commit"]
        self.assertEqual(2, len(commits))
        final_paths = {path for _, path in commits[-1][1]}
        self.assertEqual(
            {graph_marker, rfc_marker, failure_path, history_path},
            final_paths,
        )

    def test_apply_preserves_failure_when_durable_postcondition_is_missing(self):
        self.verify_postcondition.return_value = False

        with self.assertRaisesRegex(operator_replay.ReplayRefused, "postcondition"):
            self.replay(apply=True)

        graph_marker = (
            "users",
            BAYLOR_UID,
            "processedMessages",
            b64url_id(GRAPH_MESSAGE_ID),
        )
        rfc_marker = (
            "users",
            BAYLOR_UID,
            "processedMessages",
            b64url_id(INTERNET_MESSAGE_ID),
        )
        failure_path = (
            "users",
            BAYLOR_UID,
            "processingFailures",
            _failure_id(),
        )
        self.assertEqual("operator_replay_in_progress", self.fs.data[graph_marker]["status"])
        self.assertEqual("operator_replay_in_progress", self.fs.data[rfc_marker]["status"])
        self.assertIn(failure_path, self.fs.data)

    def test_apply_preserves_failure_when_processing_creates_recovery_artifact(self):
        self.find_existing_artifact.side_effect = [
            None,
            {"collection": "pendingResponses", "id": "pending-after-process"},
        ]

        with self.assertRaisesRegex(operator_replay.ReplayRefused, "post-processing artifact"):
            self.replay(apply=True)

        failure_path = (
            "users",
            BAYLOR_UID,
            "processingFailures",
            _failure_id(),
        )
        self.assertIn(failure_path, self.fs.data)

    def test_post_process_artifact_query_error_marks_failure_visible(self):
        self.find_existing_artifact.side_effect = [
            None,
            RuntimeError("exact artifact query unavailable"),
        ]

        with self.assertRaisesRegex(operator_replay.ReplayRefused, "artifact guard"):
            self.replay(apply=True)

        failure_path = (
            "users",
            BAYLOR_UID,
            "processingFailures",
            _failure_id(),
        )
        self.assertEqual(
            "operator_replay_guard_failed",
            self.fs.data[failure_path]["recoveryStatus"],
        )
        self.assertEqual(
            "RuntimeError",
            self.fs.data[failure_path]["replayErrorClass"],
        )
        for message_id in (GRAPH_MESSAGE_ID, INTERNET_MESSAGE_ID):
            marker_path = (
                "users",
                BAYLOR_UID,
                "processedMessages",
                b64url_id(message_id),
            )
            self.assertEqual(
                "operator_replay_in_progress",
                self.fs.data[marker_path]["status"],
            )

    def test_failed_atomic_completion_leaves_preclaims_and_failure_visible(self):
        self.fs.fail_batch_commit_number = 2

        with self.assertRaisesRegex(RuntimeError, "atomic batch commit failed"):
            self.replay(apply=True)

        for message_id in (GRAPH_MESSAGE_ID, INTERNET_MESSAGE_ID):
            marker_path = (
                "users",
                BAYLOR_UID,
                "processedMessages",
                b64url_id(message_id),
            )
            self.assertEqual(
                "operator_replay_in_progress",
                self.fs.data[marker_path]["status"],
            )
        self.assertIn(
            ("users", BAYLOR_UID, "processingFailures", _failure_id()),
            self.fs.data,
        )

    def test_processing_exception_preserves_failure_and_both_unprocessed_markers(self):
        self.process_message.side_effect = RuntimeError("asset extraction still broken")

        with self.assertRaisesRegex(RuntimeError, "asset extraction still broken"):
            self.replay(apply=True)

        graph_marker = (
            "users",
            BAYLOR_UID,
            "processedMessages",
            b64url_id(GRAPH_MESSAGE_ID),
        )
        rfc_marker = (
            "users",
            BAYLOR_UID,
            "processedMessages",
            b64url_id(INTERNET_MESSAGE_ID),
        )
        failure_path = (
            "users",
            BAYLOR_UID,
            "processingFailures",
            _failure_id(),
        )
        self.assertEqual("operator_replay_in_progress", self.fs.data[graph_marker]["status"])
        self.assertEqual("operator_replay_in_progress", self.fs.data[rfc_marker]["status"])
        self.assertIn(failure_path, self.fs.data)

    def test_default_processor_is_forced_into_no_reply_mode(self):
        from email_automation import processing

        with patch.object(processing, "process_inbox_message") as process_message:
            operator_replay.replay_exact_message(
                _request(),
                {"Authorization": "Bearer test-token"},
                apply=True,
                fs_client=self.fs,
                fetch_message=self.fetch_message,
                find_existing_artifact=self.find_existing_artifact,
                find_manual_continuation=self.find_continuation,
                find_recipient_continuation=self.find_recipient_continuation,
                verify_postcondition=self.verify_postcondition,
                lease_runner=self.lease_runner,
                scheduler_lease_runner=self.scheduler_lease_runner,
            )

        process_message.assert_called_once_with(
            BAYLOR_UID,
            {"Authorization": "Bearer test-token"},
            self.fetch_message.return_value,
            allow_outbound_reply=False,
            operator_replay_attempt_id=ANY,
        )

    def test_injected_processor_is_also_forced_into_no_reply_mode(self):
        self.replay(apply=True)

        self.process_message.assert_called_once_with(
            BAYLOR_UID,
            {"Authorization": "Bearer test-token"},
            self.fetch_message.return_value,
            allow_outbound_reply=False,
            operator_replay_attempt_id=ANY,
        )

    def test_apply_refuses_if_processed_marker_appears_after_preflight(self):
        graph_marker = (
            "users",
            BAYLOR_UID,
            "processedMessages",
            b64url_id(GRAPH_MESSAGE_ID),
        )

        def race_marker_into_place(*args, **kwargs):
            self.fs.data[graph_marker] = {
                "status": "processed",
                "processedAt": datetime.now(timezone.utc),
            }
            return None

        self.find_recipient_continuation.side_effect = race_marker_into_place

        with self.assertRaisesRegex(operator_replay.ReplayRefused, "claim"):
            self.replay(apply=True)

        self.assertEqual("processed", self.fs.data[graph_marker]["status"])
        self.process_message.assert_not_called()


class OperatorReplayPostconditionTests(unittest.TestCase):
    def _firestore_with(self, warnings, changes):
        fs = MagicMock()
        user_ref = MagicMock()
        fs.collection.return_value.document.return_value = user_ref
        collections = {
            "assetWarnings": MagicMock(),
            "sheetChangeLog": MagicMock(),
        }
        warning_query = MagicMock()
        warning_query.limit.return_value = warning_query
        warning_query.stream.return_value = [
            SimpleNamespace(to_dict=lambda value=value: deepcopy(value))
            for value in warnings
        ]
        collections["assetWarnings"].where.return_value = warning_query
        change_query = MagicMock()
        change_query.limit.return_value = change_query
        change_query.stream.return_value = [
            SimpleNamespace(to_dict=lambda value=value: deepcopy(value))
            for value in changes
        ]
        collections["sheetChangeLog"].where.return_value = change_query
        user_ref.collection.side_effect = lambda name: collections[name]
        return fs

    def test_postcondition_rejects_stale_warning_from_prior_attempt(self):
        started = datetime(2026, 7, 12, 18, 0, tzinfo=timezone.utc)
        stale = started - timedelta(minutes=10)
        fresh = started + timedelta(seconds=1)
        fs = self._firestore_with(
            [{
                "clientId": CLIENT_ID,
                "threadId": THREAD_ID,
                "messageId": INTERNET_MESSAGE_ID,
                "status": "degraded_text_processed",
                "updatedAt": stale,
            }],
            [{
                "clientId": CLIENT_ID,
                "threadId": THREAD_ID,
                "status": "applied",
                "applied": {"applied": [{"column": "Rent", "newValue": "12"}]},
                "createdAt": fresh,
            }],
        )

        with patch(
            "email_automation.processing._get_reply_send_outcome",
            return_value=SimpleNamespace(
                error=None,
                sent_but_unindexed=False,
                outcome="suppressed_operator_replay_no_send",
            ),
        ):
            verified = operator_replay._verify_degraded_asset_postcondition(
                _request(), fs, started, "attempt-123"
            )

        self.assertFalse(verified)

    def test_postcondition_requires_fresh_sheet_warning_and_no_send(self):
        started = datetime(2026, 7, 12, 18, 0, tzinfo=timezone.utc)
        fresh = started + timedelta(seconds=1)
        attempt_id = "attempt-123"
        fs = self._firestore_with(
            [{
                "clientId": CLIENT_ID,
                "threadId": THREAD_ID,
                "messageId": INTERNET_MESSAGE_ID,
                "status": "degraded_text_processed",
                "updatedAt": fresh,
            }],
            [{
                "clientId": CLIENT_ID,
                "threadId": THREAD_ID,
                "sourceGraphMessageId": GRAPH_MESSAGE_ID,
                "sourceInternetMessageId": INTERNET_MESSAGE_ID,
                "replayAttemptId": attempt_id,
                "status": "applied",
                "applied": {"applied": [{"column": "Rent", "newValue": "12"}]},
                "createdAt": fresh,
            }],
        )

        with patch(
            "email_automation.processing._get_reply_send_outcome",
            return_value=SimpleNamespace(
                error=None,
                sent_but_unindexed=False,
                outcome="suppressed_operator_replay_no_send",
            ),
        ):
            self.assertTrue(
                operator_replay._verify_degraded_asset_postcondition(
                    _request(), fs, started, attempt_id
                )
            )

        with patch(
            "email_automation.processing._get_reply_send_outcome",
            return_value=SimpleNamespace(
                error=None,
                sent_but_unindexed=False,
                outcome="sent_indexed",
            ),
        ):
            self.assertFalse(
                operator_replay._verify_degraded_asset_postcondition(
                    _request(), fs, started, attempt_id
                )
            )

    def test_postcondition_rejects_sheet_evidence_from_another_message_or_attempt(self):
        started = datetime(2026, 7, 12, 18, 0, tzinfo=timezone.utc)
        fresh = started + timedelta(seconds=1)
        attempt_id = "attempt-123"
        warning = {
            "clientId": CLIENT_ID,
            "threadId": THREAD_ID,
            "messageId": INTERNET_MESSAGE_ID,
            "status": "degraded_text_processed",
            "updatedAt": fresh,
        }
        base_change = {
            "clientId": CLIENT_ID,
            "threadId": THREAD_ID,
            "sourceGraphMessageId": GRAPH_MESSAGE_ID,
            "sourceInternetMessageId": INTERNET_MESSAGE_ID,
            "replayAttemptId": attempt_id,
            "status": "applied",
            "applied": {"applied": [{"column": "Rent", "newValue": "12"}]},
            "createdAt": fresh,
        }

        with patch(
            "email_automation.processing._get_reply_send_outcome",
            return_value=SimpleNamespace(
                error=None,
                sent_but_unindexed=False,
                outcome="suppressed_operator_replay_no_send",
            ),
        ):
            for changed_field, wrong_value in (
                ("sourceGraphMessageId", "different-graph-message"),
                ("sourceInternetMessageId", "<different@example.test>"),
                ("replayAttemptId", "different-attempt"),
            ):
                with self.subTest(changed_field=changed_field):
                    change = dict(base_change)
                    change[changed_field] = wrong_value
                    fs = self._firestore_with([warning], [change])
                    self.assertFalse(
                        operator_replay._verify_degraded_asset_postcondition(
                            _request(), fs, started, attempt_id
                        )
                    )


class OperatorReplayExactArtifactQueryTests(unittest.TestCase):
    def test_exact_replay_guard_does_not_stream_the_whole_collection(self):
        from email_automation import processing

        collection = MagicMock()
        query_builder = MagicMock()
        targeted_query = MagicMock()
        collection.where.return_value = query_builder
        query_builder.limit.return_value = targeted_query
        targeted_query.stream.return_value = []

        docs = processing._candidate_artifact_docs(
            collection,
            {GRAPH_MESSAGE_ID, INTERNET_MESSAGE_ID},
            processing.PROCESSING_RETRY_SOURCE_MESSAGE_FIELDS,
            THREAD_ID,
            allow_broad_scan=False,
        )

        self.assertEqual([], docs)
        collection.stream.assert_not_called()
        expected_filters = {
            (field, candidate)
            for field in processing.PROCESSING_RETRY_SOURCE_MESSAGE_FIELDS
            for candidate in {GRAPH_MESSAGE_ID, INTERNET_MESSAGE_ID}
        }
        actual_filters = {
            (
                call.kwargs["filter"].field_path,
                call.kwargs["filter"].value,
            )
            for call in collection.where.call_args_list
        }
        self.assertEqual(expected_filters, actual_filters)
        self.assertEqual(len(expected_filters), query_builder.limit.call_count)
        for call in query_builder.limit.call_args_list:
            self.assertEqual((11,), call.args)

    def test_exact_replay_guard_fails_closed_when_targeted_query_is_truncated(self):
        from email_automation import processing

        collection = MagicMock()
        query_builder = MagicMock()
        targeted_query = MagicMock()
        collection.where.return_value = query_builder
        query_builder.limit.return_value = targeted_query
        targeted_query.stream.return_value = [
            SimpleNamespace(
                id=f"artifact-{index}",
                to_dict=lambda: {
                    "threadId": "different-thread",
                    "sourceMessageId": GRAPH_MESSAGE_ID,
                },
            )
            for index in range(11)
        ]

        artifact = processing._scan_retry_artifact_collection(
            collection,
            "outbox",
            {GRAPH_MESSAGE_ID, INTERNET_MESSAGE_ID},
            THREAD_ID,
            allow_broad_scan=False,
        )

        self.assertTrue(artifact["guardUnreadable"])
        self.assertEqual("guard_scan_failed", artifact["status"])
        first_filter = collection.where.call_args_list[0].kwargs["filter"]
        self.assertIn(first_filter.field_path, processing.PROCESSING_RETRY_SOURCE_MESSAGE_FIELDS)
        self.assertIn(first_filter.value, {GRAPH_MESSAGE_ID, INTERNET_MESSAGE_ID})
        query_builder.limit.assert_called_once_with(11)

    def test_exact_replay_guard_fails_closed_when_query_api_is_unavailable(self):
        from email_automation import processing

        collection = MagicMock()
        collection.where = None

        artifact = processing._scan_retry_artifact_collection(
            collection,
            "outbox",
            {GRAPH_MESSAGE_ID, INTERNET_MESSAGE_ID},
            THREAD_ID,
            allow_broad_scan=False,
        )

        self.assertTrue(artifact["guardUnreadable"])
        self.assertEqual("guard_scan_failed", artifact["status"])

    def test_exact_replay_guard_fails_closed_on_aggregate_ambiguity(self):
        from email_automation import processing

        collection = MagicMock()
        query_builders = []

        def targeted_query(*, filter):
            query_builder = MagicMock()
            query = MagicMock()
            query_builder.limit.return_value = query
            index = len(query_builders)
            query.stream.return_value = [
                SimpleNamespace(
                    id=f"aggregate-artifact-{index}",
                    to_dict=lambda filter=filter: {
                        "threadId": "different-thread",
                        filter.field_path: filter.value,
                    },
                )
            ]
            query_builders.append(query_builder)
            return query_builder

        collection.where.side_effect = targeted_query

        artifact = processing._scan_retry_artifact_collection(
            collection,
            "outbox",
            {GRAPH_MESSAGE_ID, INTERNET_MESSAGE_ID},
            THREAD_ID,
            allow_broad_scan=False,
        )

        self.assertTrue(artifact["guardUnreadable"])
        self.assertEqual("guard_scan_failed", artifact["status"])
        self.assertEqual(11, len(query_builders))
        for query_builder in query_builders:
            query_builder.limit.assert_called_once_with(11)


class OperatorReplayProcessorClaimTests(unittest.TestCase):
    def test_processor_claim_requires_both_exact_message_markers(self):
        from email_automation import processing

        attempt_id = "attempt-123"
        fs = MagicMock()
        docs = {
            b64url_id(GRAPH_MESSAGE_ID): {
                "status": "operator_replay_in_progress",
                "replayAttemptId": attempt_id,
            },
            b64url_id(INTERNET_MESSAGE_ID): {
                "status": "operator_replay_in_progress",
                "replayAttemptId": attempt_id,
            },
        }

        def document(doc_id):
            snapshot = MagicMock()
            snapshot.exists = doc_id in docs
            snapshot.to_dict.return_value = docs.get(doc_id, {})
            ref = MagicMock()
            ref.get.return_value = snapshot
            return ref

        fs.collection.return_value.document.return_value.collection.return_value.document.side_effect = document

        with patch.object(processing, "_fs", fs):
            processing._validate_operator_replay_claims(
                BAYLOR_UID,
                GRAPH_MESSAGE_ID,
                INTERNET_MESSAGE_ID,
                attempt_id,
            )

            docs[b64url_id(INTERNET_MESSAGE_ID)]["replayAttemptId"] = "wrong"
            with self.assertRaisesRegex(processing.RetryableProcessingError, "claim"):
                processing._validate_operator_replay_claims(
                    BAYLOR_UID,
                    GRAPH_MESSAGE_ID,
                    INTERNET_MESSAGE_ID,
                    attempt_id,
                )

            docs[b64url_id(INTERNET_MESSAGE_ID)]["replayAttemptId"] = attempt_id
            for missing_message_id in (GRAPH_MESSAGE_ID, INTERNET_MESSAGE_ID):
                with self.subTest(missing_message_id=missing_message_id):
                    missing_claim = docs.pop(b64url_id(missing_message_id))
                    try:
                        with self.assertRaisesRegex(
                            processing.RetryableProcessingError,
                            "claim",
                        ):
                            processing._validate_operator_replay_claims(
                                BAYLOR_UID,
                                GRAPH_MESSAGE_ID,
                                INTERNET_MESSAGE_ID,
                                attempt_id,
                            )
                    finally:
                        docs[b64url_id(missing_message_id)] = missing_claim


def _cli_args(**overrides):
    values = {
        "--uid": BAYLOR_UID,
        "--client-id": CLIENT_ID,
        "--thread-id": THREAD_ID,
        "--graph-message-id": GRAPH_MESSAGE_ID,
        "--internet-message-id": INTERNET_MESSAGE_ID,
        "--sender": SENDER,
        "--operator-recipient": OPERATOR_RECIPIENT,
    }
    values.update(overrides)
    args = []
    for flag, value in values.items():
        if value is not None:
            args.extend([flag, value])
    return args


def _verified_result(*, applied=False):
    return operator_replay.ReplayResult(
        status="applied" if applied else "verified",
        applied=applied,
        uid=BAYLOR_UID,
        client_id=CLIENT_ID,
        thread_id=THREAD_ID,
        graph_message_id=GRAPH_MESSAGE_ID,
        internet_message_id=INTERNET_MESSAGE_ID,
        sender=SENDER,
        operator_recipient=OPERATOR_RECIPIENT,
        conversation_id=CONVERSATION_ID,
        failure_id=_failure_id(),
        client_status="live",
        thread_status="active",
    )


class OperatorReplayCliTests(unittest.TestCase):
    def setUp(self):
        from scripts import replay_exact_message

        self.cli = replay_exact_message

    def test_all_exact_identity_arguments_are_required(self):
        parser = self.cli.build_parser()
        for missing_flag in (
            "--uid",
            "--client-id",
            "--thread-id",
            "--graph-message-id",
            "--internet-message-id",
            "--sender",
            "--operator-recipient",
        ):
            with self.subTest(missing_flag=missing_flag), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(_cli_args(**{missing_flag: None}))

    def test_help_does_not_require_runtime_credentials(self):
        clean_env = dict(os.environ)
        for name in (
            "E2E_TEST_MODE",
            "AZURE_API_APP_ID",
            "AZURE_API_CLIENT_SECRET",
            "FIREBASE_API_KEY",
        ):
            clean_env.pop(name, None)

        completed = subprocess.run(
            [sys.executable, self.cli.__file__, "--help"],
            cwd=os.path.dirname(os.path.dirname(self.cli.__file__)),
            env=clean_env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("--graph-message-id", completed.stdout)

    def test_default_is_dry_run_and_prints_redacted_preflight(self):
        fake_headers = {"Authorization": "Bearer secret-token-value"}
        with patch.object(
            self.cli, "acquire_graph_headers", return_value=fake_headers
        ) as acquire, patch.object(
            self.cli, "replay_exact_message", return_value=_verified_result()
        ) as replay, redirect_stdout(StringIO()) as stdout:
            exit_code = self.cli.main(_cli_args())

        self.assertEqual(0, exit_code)
        acquire.assert_called_once_with(BAYLOR_UID, OPERATOR_RECIPIENT)
        request = replay.call_args.args[0]
        self.assertEqual(GRAPH_MESSAGE_ID, request.graph_message_id)
        self.assertEqual(INTERNET_MESSAGE_ID, request.internet_message_id)
        self.assertEqual(fake_headers, replay.call_args.args[1])
        self.assertFalse(replay.call_args.kwargs["apply"])
        output = stdout.getvalue()
        self.assertIn("DRY RUN", output)
        self.assertIn("verified", output)
        self.assertNotIn("secret-token-value", output)
        self.assertNotIn(SENDER, output)
        self.assertNotIn(OPERATOR_RECIPIENT, output)

    def test_apply_flag_is_the_only_mutation_switch(self):
        with patch.object(
            self.cli,
            "acquire_graph_headers",
            return_value={"Authorization": "Bearer secret"},
        ), patch.object(
            self.cli,
            "replay_exact_message",
            return_value=_verified_result(applied=True),
        ) as replay, redirect_stdout(StringIO()) as stdout:
            exit_code = self.cli.main([*_cli_args(), "--apply"])

        self.assertEqual(0, exit_code)
        self.assertTrue(replay.call_args.kwargs["apply"])
        self.assertIn("APPLY", stdout.getvalue())

    def test_unsafe_lane_is_rejected_before_token_acquisition(self):
        with patch.object(self.cli, "acquire_graph_headers") as acquire, patch.object(
            self.cli, "replay_exact_message"
        ) as replay, redirect_stderr(StringIO()) as stderr:
            exit_code = self.cli.main(_cli_args(**{"--sender": "broker@example.com"}))

        self.assertEqual(2, exit_code)
        acquire.assert_not_called()
        replay.assert_not_called()
        self.assertIn("BP21", stderr.getvalue())

    def test_graph_auth_returns_headers_without_printing_token(self):
        secret = "never-print-this-token"
        fake_cache = MagicMock()
        fake_cache.has_state_changed = False
        fake_app = MagicMock()
        fake_app.get_accounts.return_value = [
            {"username": OPERATOR_RECIPIENT},
        ]
        fake_app.acquire_token_silent.return_value = {
            "access_token": secret,
            "expires_in": 3600,
        }

        def fake_download(_api_key, *, output_file, user_id):
            self.assertEqual(BAYLOR_UID, user_id)
            with open(output_file, "w", encoding="utf-8") as cache_file:
                cache_file.write("serialized-cache")

        with patch.object(
            self.cli, "SerializableTokenCache", return_value=fake_cache
        ), patch.object(
            self.cli,
            "ConfidentialClientApplication",
            return_value=fake_app,
        ), patch.object(
            self.cli, "download_token", side_effect=fake_download
        ), redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
            headers = self.cli.acquire_graph_headers(
                BAYLOR_UID,
                OPERATOR_RECIPIENT,
            )

        self.assertEqual(f"Bearer {secret}", headers["Authorization"])
        fake_cache.deserialize.assert_called_once_with("serialized-cache")
        self.assertNotIn(secret, stdout.getvalue())
        self.assertNotIn(secret, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
