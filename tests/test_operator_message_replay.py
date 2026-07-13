import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
from io import StringIO
from unittest.mock import MagicMock, patch


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


class _FakeFirestore:
    def __init__(self):
        self.data = {}
        self.events = []

    def collection(self, name):
        return _CollectionRef(self, (name,))


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
        "email": [SENDER],
    }
    fs.data[("users", BAYLOR_UID, "processingFailures", _failure_id())] = {
        "clientId": CLIENT_ID,
        "threadId": THREAD_ID,
        "messageId": INTERNET_MESSAGE_ID,
        "graphMessageId": GRAPH_MESSAGE_ID,
        "retryable": True,
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


class OperatorReplayContractTests(unittest.TestCase):
    def setUp(self):
        self.fs = _FakeFirestore()
        _seed_valid_state(self.fs)
        self.fetch_message = MagicMock(return_value=_graph_message())
        self.process_message = MagicMock()
        self.find_continuation = MagicMock(return_value=None)
        self.lease_runner = MagicMock(side_effect=_lease_runs)

    def replay(self, request=None, *, apply=False):
        return operator_replay.replay_exact_message(
            request or _request(),
            {"Authorization": "Bearer test-token"},
            apply=apply,
            fs_client=self.fs,
            fetch_message=self.fetch_message,
            process_message=self.process_message,
            find_manual_continuation=self.find_continuation,
            lease_runner=self.lease_runner,
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

    def test_refuses_sent_items_manual_continuation(self):
        self.find_continuation.return_value = {
            "id": "sent-message-after-failure",
            "conversationId": CONVERSATION_ID,
            "sentDateTime": "2026-07-12T18:05:00Z",
        }

        self.assert_refused(message="Sent Items")

    def test_refuses_when_user_lease_is_held(self):
        self.lease_runner.side_effect = None
        self.lease_runner.return_value = False

        self.assert_refused(message="lease")
        self.fetch_message.assert_not_called()

    def test_apply_processes_once_then_marks_both_ids_and_deletes_only_exact_failure(self):
        def record_process(*args):
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
        self.assertNotIn(failure_path, self.fs.data)
        self.assertIn(unrelated_path, self.fs.data)

        process_position = self.fs.events.index(("process", GRAPH_MESSAGE_ID))
        graph_set_position = self.fs.events.index(("set", graph_marker))
        rfc_set_position = self.fs.events.index(("set", rfc_marker))
        delete_position = self.fs.events.index(("delete", failure_path))
        self.assertLess(process_position, graph_set_position)
        self.assertLess(process_position, rfc_set_position)
        self.assertLess(graph_set_position, delete_position)
        self.assertLess(rfc_set_position, delete_position)

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
        self.assertNotIn(graph_marker, self.fs.data)
        self.assertNotIn(rfc_marker, self.fs.data)
        self.assertIn(failure_path, self.fs.data)


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
