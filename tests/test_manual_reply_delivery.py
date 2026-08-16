"""Task 9 RED contracts for the exact dashboard manual-reply sender.

These tests are credential-free.  The HTTP recorder rejects mailbox scans and
records only synthetic ``/me`` requests; no network adapter is imported.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import unittest
from unittest.mock import patch

from google.cloud.firestore import SERVER_TIMESTAMP

from email_automation import manual_reply


IMMUTABLE_PREFER = 'IdType="ImmutableId"'
PRODUCER_TIME = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
CLAIM_COMMIT_TIME = datetime(2026, 8, 15, 12, 1, tzinfo=timezone.utc)
TASK4_REJECTED_BOUNDARY_CODE_POINTS = (
    0x0009,
    0x000A,
    0x000B,
    0x000C,
    0x000D,
    0x0020,
    0x0085,
    0x00A0,
    0x1680,
    0x2000,
    0x2001,
    0x2002,
    0x2003,
    0x2004,
    0x2005,
    0x2006,
    0x2007,
    0x2008,
    0x2009,
    0x200A,
    0x2028,
    0x2029,
    0x202F,
    0x205F,
    0x3000,
    0xFEFF,
)

TASK7_MANUAL_REPLY_ALLOWED_FIELDS = frozenset({
    "manualReplyLaneVersion",
    "source",
    "actionType",
    "status",
    "cancelRequested",
    "assignedEmails",
    "ccEmails",
    "script",
    "clientId",
    "subject",
    "contactName",
    "rowNumber",
    "isPersonalized",
    "createdAt",
    "threadId",
    "replyToMessageId",
    "sourceMessageId",
    "sourceGraphMessageId",
    "sourceInternetMessageId",
    "sourceMessage",
    "notificationId",
    "notificationClientId",
    "deleteNotificationOnSend",
    "sourceDeadLetterId",
    "resumeThreadOnSend",
    "scriptSelectionMode",
    "forceScript",
    "actionReason",
    "actionAuditId",
})


def _required_callable(test_case: unittest.TestCase, name: str):
    function = getattr(manual_reply, name, None)
    test_case.assertTrue(callable(function), f"manual_reply.{name} is missing")
    return function


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return deepcopy(self._payload)


class _RecordingHttp:
    def __init__(
        self,
        *,
        draft_recipient="broker@example.invalid",
        draft_cc=None,
        draft_bcc=None,
        source_graph_id="source-immutable-1",
        source_internet_id="<source-1@example.invalid>",
        source_sender="broker@example.invalid",
        source_from="broker@example.invalid",
        source_conversation_id="conversation-1",
        source_to=None,
        source_cc=None,
        source_bcc=None,
        source_reply_to=None,
        create_reply_draft_id="draft-immutable-1",
        draft_graph_id="draft-immutable-1",
        draft_conversation_id="conversation-1",
        draft_is_draft=True,
        draft_body="Synthetic reviewed reply.",
        draft_body_type="Text",
        patch_status=200,
        draft_get_status=200,
        send_status=202,
        graph_me_id="local-account-1",
        graph_upn="sender@example.invalid",
        graph_mail="mail-alias@example.invalid",
        event_log=None,
    ):
        self.calls = []
        self.event_log = event_log if event_log is not None else []
        self.draft_recipient = draft_recipient
        self.draft_cc = list(draft_cc or [])
        self.draft_bcc = list(draft_bcc or [])
        self.source_graph_id = source_graph_id
        self.source_internet_id = source_internet_id
        self.source_sender = source_sender
        self.source_from = source_from
        self.source_conversation_id = source_conversation_id
        self.source_to = list(
            ["sender@example.invalid"] if source_to is None else source_to
        )
        self.source_cc = list(source_cc or [])
        self.source_bcc = list(source_bcc or [])
        self.source_reply_to = list(source_reply_to or [])
        self.create_reply_draft_id = create_reply_draft_id
        self.draft_graph_id = draft_graph_id
        self.draft_conversation_id = draft_conversation_id
        self.draft_is_draft = draft_is_draft
        self.draft_body = draft_body
        self.draft_body_type = draft_body_type
        self.patch_status = patch_status
        self.draft_get_status = draft_get_status
        self.send_status = send_status
        self.graph_me_id = graph_me_id
        self.graph_upn = graph_upn
        self.graph_mail = graph_mail

    def _record(self, method, path, *, headers, params=None, json=None, retry=None):
        self.assert_exact_path(path)
        if params not in (None, {}):
            raise AssertionError(f"manual reply attempted a Graph query: {params!r}")
        if headers.get("Prefer") != IMMUTABLE_PREFER:
            raise AssertionError(f"{method} omitted the ImmutableId preference")
        self.event_log.append(("graph", method, path))
        self.calls.append({
            "method": method,
            "path": path,
            "headers": dict(headers),
            "params": deepcopy(params),
            "json": deepcopy(json),
            "retry": retry,
        })

    @staticmethod
    def assert_exact_path(path):
        if path != "/me" and not path.startswith("/me/"):
            raise AssertionError(f"manual reply escaped the exact /me surface: {path}")
        lowered = path.lower()
        if (
            "?" in path
            or "sentitems" in lowered
            or "mailfolders" in lowered
            or "$search" in lowered
            or "$filter" in lowered
            or path in {"/me/messages", "/me/sendMail"}
        ):
            raise AssertionError(f"manual reply attempted a mailbox scan: {path}")

    def get(self, path, *, headers, params=None, retry=None):
        self._record("GET", path, headers=headers, params=params, retry=retry)
        if path == "/me":
            return _Response(200, {
                "id": self.graph_me_id,
                "mail": self.graph_mail,
                "userPrincipalName": self.graph_upn,
            })
        if (
            path.startswith("/me/messages/")
            and path.count("/") == 3
            and path != "/me/messages/draft-immutable-1"
        ):
            return _Response(200, {
                "id": self.source_graph_id,
                "internetMessageId": self.source_internet_id,
                "conversationId": self.source_conversation_id,
                "from": {"emailAddress": {"address": self.source_from}},
                "sender": {"emailAddress": {"address": self.source_sender}},
                "toRecipients": [
                    {"emailAddress": {"address": address}}
                    for address in self.source_to
                ],
                "ccRecipients": [
                    {"emailAddress": {"address": address}}
                    for address in self.source_cc
                ],
                "bccRecipients": [
                    {"emailAddress": {"address": address}}
                    for address in self.source_bcc
                ],
                "replyTo": [
                    {"emailAddress": {"address": address}}
                    for address in self.source_reply_to
                ],
                "isDraft": False,
            })
        if path == "/me/messages/draft-immutable-1":
            return _Response(self.draft_get_status, {
                "id": self.draft_graph_id,
                "conversationId": self.draft_conversation_id,
                "isDraft": self.draft_is_draft,
                "toRecipients": [{
                    "emailAddress": {"address": self.draft_recipient},
                }],
                "ccRecipients": [
                    {"emailAddress": {"address": address}}
                    for address in self.draft_cc
                ],
                "bccRecipients": [
                    {"emailAddress": {"address": address}}
                    for address in self.draft_bcc
                ],
                "body": {
                    "contentType": self.draft_body_type,
                    "content": self.draft_body,
                },
            })
        raise AssertionError(f"unexpected exact GET: {path}")

    def post(self, path, *, headers, json=None, retry=None):
        self._record("POST", path, headers=headers, json=json, retry=retry)
        if path == "/me/messages/source-immutable-1/createReply":
            return _Response(201, {"id": self.create_reply_draft_id})
        if path == "/me/messages/draft-immutable-1/send":
            return _Response(self.send_status)
        raise AssertionError(f"unexpected exact POST: {path}")

    def patch(self, path, *, headers, json=None, retry=None):
        self._record("PATCH", path, headers=headers, json=json, retry=retry)
        if path != "/me/messages/draft-immutable-1":
            raise AssertionError(f"unexpected exact PATCH: {path}")
        return _Response(self.patch_status)

    def delete(self, path, *, headers, retry=None):
        self._record("DELETE", path, headers=headers, retry=retry)
        if path != "/me/messages/draft-immutable-1":
            raise AssertionError(f"unexpected exact DELETE: {path}")
        return _Response(204)


def _source_binding():
    return {
        "graphLookupMessageId": SOURCE_ALIAS,
        "immutableGraphMessageId": "source-immutable-1",
        "internetMessageId": "<SOURCE-1@example.invalid>",
        "conversationId": "conversation-1",
        "fromAddress": "broker@example.invalid",
        "senderAddress": "broker@example.invalid",
        "sender": "broker@example.invalid",
        "audience": {
            "to": ["broker@example.invalid"],
            "cc": [],
            "bcc": [],
        },
    }


class ManualReplyGraphDeliveryTests(unittest.TestCase):
    def _prepare(self, http, *, body="Synthetic reviewed reply."):
        prepare = _required_callable(self, "prepare_canonical_manual_reply_draft")
        return prepare(
            http_client=http,
            headers={"Authorization": "Bearer synthetic"},
            source_binding=_source_binding(),
            selected_account={
                "home_account_id": "synthetic-home-account-1",
                "local_account_id": "local-account-1",
                "environment": "login.microsoftonline.com",
                "realm": "synthetic-tenant-1",
                "username": "sender@example.invalid",
            },
            recipient="broker@example.invalid",
            body=body,
        )

    def test_shared_graph_id_contract_rejects_every_task4_boundary_and_invalid_matrix(self):
        canonicalize = _required_callable(self, "canonical_graph_message_id")
        self.assertEqual("source-id", canonicalize("source-id"))

        for code_point in TASK4_REJECTED_BOUNDARY_CODE_POINTS:
            boundary = chr(code_point)
            for position, candidate in (
                ("prefix", f"{boundary}source-id"),
                ("suffix", f"source-id{boundary}"),
            ):
                with self.subTest(
                    code_point=f"U+{code_point:04X}",
                    position=position,
                ):
                    with self.assertRaises((TypeError, ValueError)):
                        canonicalize(candidate)

        invalid_ids = (
            None,
            b"source-id",
            "",
            "x" * 2049,
            "source\x00id",
            "source\x7fid",
            "\ud800",
        )
        for value in invalid_ids:
            with self.subTest(value=repr(value)):
                with self.assertRaises((TypeError, ValueError)):
                    canonicalize(value)

    def test_shared_address_contract_accepts_dotless_and_rejects_invalid_matrix(self):
        canonicalize = _required_callable(self, "canonical_email_address")
        self.assertEqual("broker@localhost", canonicalize("Broker@LOCALHOST"))

        invalid_addresses = (
            None,
            b"broker@localhost",
            "",
            " broker@localhost",
            "broker@localhost ",
            "brokerlocalhost",
            "@localhost",
            "broker@",
            "broker@@localhost",
            "bro ker@localhost",
            "broker@local host",
            "broker\x00@localhost",
            "broker@local\x7fhost",
            "br\N{LATIN SMALL LETTER O WITH ACUTE}ker@localhost",
            "<broker>@localhost",
            "broker>@localhost",
            "bro,ker@localhost",
            "bro;ker@localhost",
        )
        for value in invalid_addresses:
            with self.subTest(value=repr(value)):
                with self.assertRaises((TypeError, ValueError)):
                    canonicalize(value)

    def test_dotless_addresses_prepare_but_graph_me_identity_remains_exact(self):
        prepare = _required_callable(self, "prepare_canonical_manual_reply_draft")
        source_binding = deepcopy(_source_binding())
        source_binding.update({
            "fromAddress": "broker@localhost",
            "senderAddress": "broker@localhost",
            "sender": "broker@localhost",
            "audience": {
                "to": ["broker@localhost"],
                "cc": [],
                "bcc": [],
            },
        })
        selected_account = {
            **_selected_account(),
            "username": "sender@localhost",
        }
        exact_http = _RecordingHttp(
            draft_recipient="broker@localhost",
            source_sender="broker@localhost",
            source_from="broker@localhost",
            source_to=["sender@localhost"],
            graph_upn="sender@localhost",
            graph_mail="mail-alias@localhost",
        )

        result = prepare(
            http_client=exact_http,
            headers={"Authorization": "Bearer synthetic"},
            source_binding=source_binding,
            selected_account=selected_account,
            recipient="broker@localhost",
            body="Synthetic reviewed reply.",
        )

        self.assertEqual("prepared", result["status"])
        self.assertFalse(any(call["path"].endswith("/send") for call in exact_http.calls))

        mismatches = (
            (
                _RecordingHttp(
                    graph_upn="sender@localhost",
                    graph_mail="mail-alias@localhost",
                ),
                {**selected_account, "username": "other@localhost"},
            ),
            (
                _RecordingHttp(
                    graph_me_id="other-local-account",
                    graph_upn="sender@localhost",
                    graph_mail="mail-alias@localhost",
                ),
                selected_account,
            ),
            (
                _RecordingHttp(
                    graph_upn="other@localhost",
                    graph_mail="mail-alias@localhost",
                ),
                selected_account,
            ),
        )
        for http, account in mismatches:
            with self.subTest(
                graph_me_id=http.graph_me_id,
                graph_upn=http.graph_upn,
                username=account["username"],
            ):
                mismatch = prepare(
                    http_client=http,
                    headers={"Authorization": "Bearer synthetic"},
                    source_binding=source_binding,
                    selected_account=account,
                    recipient="broker@localhost",
                    body="Synthetic reviewed reply.",
                )

                self.assertEqual("manual_review", mismatch["status"])
                self.assertEqual(["/me"], [call["path"] for call in http.calls])

    def test_exact_create_reply_prepares_one_recipient_without_sending(self):
        http = _RecordingHttp()

        result = self._prepare(http)

        self.assertEqual("prepared", result["status"])
        paths = [call["path"] for call in http.calls]
        self.assertEqual([
            "/me",
            "/me/messages/source-immutable-1",
            "/me/messages/source-immutable-1/createReply",
            "/me/messages/draft-immutable-1",
            "/me/messages/draft-immutable-1",
        ], paths)
        self.assertFalse(any("createReplyAll" in path for path in paths))

        patch_call = next(call for call in http.calls if call["method"] == "PATCH")
        self.assertEqual(
            [{"emailAddress": {"address": "broker@example.invalid"}}],
            patch_call["json"]["toRecipients"],
        )
        self.assertEqual([], patch_call["json"]["ccRecipients"])
        self.assertEqual([], patch_call["json"]["bccRecipients"])
        self.assertEqual(
            {"contentType": "Text", "content": "Synthetic reviewed reply."},
            patch_call["json"]["body"],
        )
        self.assertTrue(all(
            call["headers"].get("Authorization") == "Bearer synthetic"
            for call in http.calls
        ))

        self.assertFalse(any(call["path"].endswith("/send") for call in http.calls))

    def test_draft_audience_mismatch_deletes_without_send_or_fallback(self):
        cases = (
            {"draft_recipient": "other@example.invalid"},
            {"draft_cc": ["copied@example.invalid"]},
            {"draft_bcc": ["blind@example.invalid"]},
            {"draft_body": "Changed unreviewed body."},
            {"draft_body_type": "HTML"},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                http = _RecordingHttp(**kwargs)

                result = self._prepare(http)

                self.assertEqual("manual_review", result["status"])
                self.assertEqual(
                    ["/me/messages/draft-immutable-1"],
                    [call["path"] for call in http.calls if call["method"] == "DELETE"],
                )
                self.assertFalse(any(call["path"].endswith("/send") for call in http.calls))

    def test_source_pair_sender_and_recipient_must_match_before_create_reply(self):
        cases = (
            _RecordingHttp(source_graph_id="drifted-immutable-id"),
            _RecordingHttp(source_internet_id="<drifted@example.invalid>"),
            _RecordingHttp(source_sender="other@example.invalid"),
            _RecordingHttp(source_from="other@example.invalid"),
            _RecordingHttp(source_conversation_id="other-conversation"),
            _RecordingHttp(source_to=["other-account@example.invalid"]),
            _RecordingHttp(source_cc=["observer@example.invalid"]),
            _RecordingHttp(source_bcc=["observer@example.invalid"]),
            _RecordingHttp(source_reply_to=["alternate@example.invalid"]),
        )
        for http in cases:
            with self.subTest(http=http):
                result = self._prepare(http)

                self.assertEqual("manual_review", result["status"])
                self.assertFalse(any("createReply" in call["path"] for call in http.calls))
                self.assertFalse(any(call["path"].endswith("/send") for call in http.calls))

    def test_normal_multiline_reviewed_body_is_allowed_but_unsafe_controls_are_not(self):
        safe_bodies = (
            "Hello,\n\nThank you for the update.\n\nBest,\nSynthetic Sender",
            "Hello,\r\n\r\nThank you for the update.\r\n\r\nBest,\r\nSynthetic Sender",
        )
        for body in safe_bodies:
            with self.subTest(kind="safe_multiline", body=repr(body)):
                http = _RecordingHttp(draft_body=body)

                result = self._prepare(http, body=body)

                self.assertEqual("prepared", result["status"])
                self.assertFalse(any(call["path"].endswith("/send") for call in http.calls))

        for control in ("\x00", "\x08", "\x7f", "\u0085"):
            with self.subTest(kind="unsafe_control", codepoint=ord(control)):
                body = f"Reviewed prefix{control}reviewed suffix"
                http = _RecordingHttp(draft_body=body)

                result = self._prepare(http, body=body)

                self.assertEqual("manual_review", result["status"])
                self.assertFalse(any("createReply" in call["path"] for call in http.calls))

    def test_exact_draft_id_and_conversation_are_verified_after_create_reply(self):
        cases = (
            {"draft_graph_id": "different-draft-id"},
            {"draft_conversation_id": "different-conversation"},
            {"draft_is_draft": False},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                http = _RecordingHttp(**kwargs)

                result = self._prepare(http)

                self.assertEqual("manual_review", result["status"])
                self.assertEqual(
                    ["/me/messages/draft-immutable-1"],
                    [call["path"] for call in http.calls if call["method"] == "DELETE"],
                )
                self.assertFalse(any(call["path"].endswith("/send") for call in http.calls))

    def test_every_failure_after_a_known_draft_id_attempts_exact_cleanup(self):
        cases = (
            {"patch_status": 500},
            {"draft_get_status": 500},
            {"draft_graph_id": "different-draft-id"},
            {"draft_conversation_id": "different-conversation"},
            {"draft_is_draft": False},
            {"draft_body": "Changed unreviewed body."},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                http = _RecordingHttp(**kwargs)

                result = self._prepare(http)

                self.assertEqual("manual_review", result["status"])
                self.assertEqual(
                    [("DELETE", "/me/messages/draft-immutable-1", False)],
                    [
                        (call["method"], call["path"], call["retry"])
                        for call in http.calls
                        if call["method"] == "DELETE"
                    ],
                )
                self.assertFalse(any(call["path"].endswith("/send") for call in http.calls))

    def test_token_selected_account_must_equal_exact_graph_me_identity(self):
        prepare = _required_callable(self, "prepare_canonical_manual_reply_draft")
        cases = (
            (
                _RecordingHttp(),
                {**_selected_account(), "username": "different-sender@example.invalid"},
            ),
            (
                _RecordingHttp(graph_me_id="other-local-account"),
                _selected_account(),
            ),
            (
                _RecordingHttp(graph_upn="other-sender@example.invalid"),
                _selected_account(),
            ),
        )
        for http, account in cases:
            with self.subTest(account=account, graph_me_id=http.graph_me_id):
                result = prepare(
                    http_client=http,
                    headers={"Authorization": "Bearer synthetic"},
                    source_binding=_source_binding(),
                    selected_account=account,
                    recipient="broker@example.invalid",
                    body="Synthetic reviewed reply.",
                )

                self.assertEqual("manual_review", result["status"])
                self.assertEqual(["/me"], [call["path"] for call in http.calls])

    def test_live_kill_switch_is_reread_immediately_before_low_level_send(self):
        events = []
        http = _RecordingHttp(event_log=events)
        modes = iter(("live", "paused"))
        reads = []

        def read_mode():
            mode = next(modes)
            reads.append(mode)
            events.append(("mode", mode))
            return mode

        transport = _required_callable(self, "send_prepared_manual_reply_once")
        result = transport(
            http_client=http,
            headers={"Authorization": "Bearer synthetic"},
            immutable_draft_id="draft-immutable-1",
            selected_account=_selected_account(),
            outbound_mode_reader=read_mode,
        )

        self.assertEqual(["live", "paused"], reads)
        self.assertEqual("manual_review", result["status"])
        self.assertFalse(any(call["path"].endswith("/send") for call in http.calls))
        self.assertEqual(1, sum(call["method"] == "DELETE" for call in http.calls))
        self.assertEqual([
            ("mode", "live"),
            ("graph", "GET", "/me"),
            ("mode", "paused"),
            ("graph", "DELETE", "/me/messages/draft-immutable-1"),
        ], events)

    def test_prepared_transport_returns_raw_http_observation_for_task10(self):
        transport = _required_callable(self, "send_prepared_manual_reply_once")
        for status_code in (200, 202, 204):
            with self.subTest(status_code=status_code):
                events = []
                http = _RecordingHttp(event_log=events, send_status=status_code)
                reads = []

                def read_mode():
                    reads.append("live")
                    events.append(("mode", "live"))
                    return "live"

                result = transport(
                    http_client=http,
                    headers={"Authorization": "Bearer synthetic"},
                    immutable_draft_id="draft-immutable-1",
                    selected_account=_selected_account(),
                    outbound_mode_reader=read_mode,
                )

                self.assertNotIn(
                    result.get("status"),
                    {"accepted", "finalize_only", "finalized", "processed"},
                )
                self.assertEqual(status_code, result.get("statusCode"))
                self.assertEqual(
                    {"status", "reason", "statusCode"},
                    set(result),
                )
                self.assertEqual([
                    "/me",
                    "/me/messages/draft-immutable-1/send",
                ], [call["path"] for call in http.calls])
                send = http.calls[-1]
                self.assertEqual("POST", send["method"])
                self.assertIs(False, send["retry"])
                self.assertEqual(2, len(reads))
                self.assertEqual([
                    ("mode", "live"),
                    ("graph", "GET", "/me"),
                    ("mode", "live"),
                    ("graph", "POST", "/me/messages/draft-immutable-1/send"),
                ], events)

    def test_task9a_reconciliation_never_finalizes_a_draft(self):
        reconcile = _required_callable(self, "reconcile_canonical_manual_reply")
        http = _RecordingHttp(draft_is_draft=True)

        result = reconcile(
            http_client=http,
            headers={"Authorization": "Bearer synthetic"},
            immutable_draft_id="draft-immutable-1",
            immutable_source_message_id="source-immutable-1",
        )

        self.assertNotIn(
            result.get("status"),
            {"accepted", "finalize_only", "finalized", "processed"},
        )
        self.assertIs(result.get("isDraft"), True)
        self.assertEqual(
            [
                "/me/messages/draft-immutable-1",
                "/me/messages/source-immutable-1",
            ],
            [call["path"] for call in http.calls],
        )

    def test_reconciliation_observation_requires_exact_ids_and_conversation(self):
        reconcile = _required_callable(self, "reconcile_canonical_manual_reply")
        for kwargs in (
            {"draft_graph_id": "different-draft-id"},
            {"source_graph_id": "different-source-id"},
            {"draft_conversation_id": "different-conversation"},
        ):
            with self.subTest(kwargs=kwargs):
                http = _RecordingHttp(**kwargs)

                result = reconcile(
                    http_client=http,
                    headers={"Authorization": "Bearer synthetic"},
                    immutable_draft_id="draft-immutable-1",
                    immutable_source_message_id="source-immutable-1",
                )

                self.assertEqual("manual_review", result["status"])
                self.assertNotIn(
                    result.get("status"),
                    {"accepted", "finalize_only", "finalized", "processed"},
                )

    def test_prepared_transport_rejects_graph_me_drift_without_send(self):
        transport = _required_callable(self, "send_prepared_manual_reply_once")
        for kwargs in (
            {"graph_me_id": "other-local-account"},
            {"graph_upn": "other-sender@example.invalid"},
        ):
            with self.subTest(kwargs=kwargs):
                http = _RecordingHttp(**kwargs)
                result = transport(
                    http_client=http,
                    headers={"Authorization": "Bearer synthetic"},
                    immutable_draft_id="draft-immutable-1",
                    selected_account=_selected_account(),
                    outbound_mode_reader=lambda: "live",
                )

                self.assertEqual("manual_review", result["status"])
                self.assertFalse(any(call["path"].endswith("/send") for call in http.calls))


class _FsSnapshot:
    def __init__(self, reference, data):
        self.reference = reference
        self.id = reference.id
        self.exists = data is not None
        self._data = None if data is None else deepcopy(data)

    def to_dict(self):
        return deepcopy(self._data or {})


class _FsNode:
    def __init__(self, root, path=()):
        self.root = root
        self.path = tuple(path)
        self.id = self.path[-1] if self.path else None

    def collection(self, name):
        return _FsNode(self.root, self.path + (name,))

    def document(self, name):
        return _FsNode(self.root, self.path + (name,))

    def get(self, transaction=None):
        if transaction is not None:
            return transaction.get(self)
        self.root.direct_reads.append(self.path)
        self.root.event_log.append(("firestore_direct_read", self.path))
        return self.root.snapshot(self)

    def set(self, data, merge=False):
        self.root.apply_write("set", self.path, data, merge=merge)

    def update(self, data):
        self.root.apply_write("update", self.path, data, merge=True)

    def delete(self):
        self.root.apply_write("delete", self.path, None)

    def stream(self):
        raise AssertionError("exact manual-reply delivery must never stream a collection")

    def where(self, *_args, **_kwargs):
        raise AssertionError("exact manual-reply delivery must never query by client alias")


class _FsTransaction:
    def __init__(self, root, ordinal, *, speculative=False):
        self.root = root
        self.ordinal = ordinal
        self.speculative = speculative
        self.reads = []
        self.writes = []
        self.committed = False

    def get(self, reference):
        self.reads.append(reference.path)
        self.root.transaction_reads.append((self.ordinal, reference.path))
        self.root.event_log.append(
            ("firestore_transaction_read", self.ordinal, reference.path)
        )
        return self.root.snapshot(reference)

    def set(self, reference, data, merge=False):
        self.root.event_log.append(
            ("firestore_transaction_set", self.ordinal, reference.path)
        )
        self.writes.append(("set", reference.path, deepcopy(data), merge))

    def update(self, reference, data):
        self.root.event_log.append(
            ("firestore_transaction_update", self.ordinal, reference.path)
        )
        self.writes.append(("update", reference.path, deepcopy(data), True))

    def create(self, reference, data):
        if reference.path in self.root.documents:
            raise RuntimeError("document already exists")
        self.root.event_log.append(
            ("firestore_transaction_create", self.ordinal, reference.path)
        )
        self.writes.append(("create", reference.path, deepcopy(data), False))

    def delete(self, reference):
        self.writes.append(("delete", reference.path, None, False))

    def commit(self):
        if self.committed or self.speculative:
            return
        self.root.event_log.append(("firestore_transaction_commit", self.ordinal))
        for operation, path, data, merge in self.writes:
            self.root.apply_write(
                operation,
                path,
                _resolve_server_timestamps(data, self.root.commit_time),
                merge=merge,
            )
        self.committed = True
        self.root.committed_transactions.append(self)
        callback = self.root.after_commit
        if callback is not None:
            self.root.after_commit = None
            callback(self.root)


class _MemoryFirestore:
    def __init__(self):
        self.documents = {}
        self.direct_reads = []
        self.transaction_reads = []
        self.write_log = []
        self.transactions = []
        self.committed_transactions = []
        self.after_commit = None
        self.retry_transaction_ordinal = None
        self.transaction_callback_runs = {}
        self.commit_time = CLAIM_COMMIT_TIME
        self.event_log = []

    def collection(self, name):
        return _FsNode(self, (name,))

    def transaction(self):
        transaction = _FsTransaction(self, len(self.transactions) + 1)
        self.transactions.append(transaction)
        self.event_log.append(("firestore_transaction_start", transaction.ordinal))
        return transaction

    def snapshot(self, reference):
        value = self.documents.get(reference.path)
        if isinstance(value, Exception):
            raise value
        return _FsSnapshot(reference, value)

    def apply_write(self, operation, path, data, *, merge=False):
        self.write_log.append((operation, path, deepcopy(data), merge))
        if operation == "delete":
            self.documents.pop(path, None)
            return
        current = deepcopy(self.documents.get(path) or {}) if merge else {}
        self.documents[path] = {**current, **deepcopy(data or {})}


def _resolve_server_timestamps(value, committed_at):
    if value is SERVER_TIMESTAMP:
        return committed_at
    if type(value) is dict:
        return {
            key: _resolve_server_timestamps(item, committed_at)
            for key, item in value.items()
        }
    if type(value) is list:
        return [_resolve_server_timestamps(item, committed_at) for item in value]
    return deepcopy(value)


def _fake_transactional(callback):
    def run(transaction, *args, **kwargs):
        root = transaction.root
        if root.retry_transaction_ordinal == transaction.ordinal:
            speculative = _FsTransaction(
                root,
                transaction.ordinal,
                speculative=True,
            )
            root.transaction_callback_runs[transaction.ordinal] = (
                root.transaction_callback_runs.get(transaction.ordinal, 0) + 1
            )
            callback(speculative, *args, **kwargs)
        root.transaction_callback_runs[transaction.ordinal] = (
            root.transaction_callback_runs.get(transaction.ordinal, 0) + 1
        )
        result = callback(transaction, *args, **kwargs)
        transaction.commit()
        return result

    return run


UID = "synthetic-user"
CLIENT_ID = "client-1"
THREAD_ID = "thread-1"
OUTBOX_ID = "outbox-1"
AUDIT_ID = "audit-1"
NOTIFICATION_ID = "notification-1"
SOURCE_ALIAS = "client-message-alias-1"
SOURCE_IMMUTABLE_ID = "source-immutable-1"
SOURCE_INTERNET_ID = "<source-1@example.invalid>"
_OMIT = object()


def _path(*parts):
    return tuple(parts)


def _resolution_key():
    return manual_reply.manual_reply_resolution_key(
        uid=UID,
        thread_id=THREAD_ID,
        immutable_graph_message_id=SOURCE_IMMUTABLE_ID,
        internet_message_id=SOURCE_INTERNET_ID,
        source="dashboard_inline_reply",
    )


def _outbox_path(outbox_id=OUTBOX_ID):
    return _path("users", UID, "outbox", outbox_id)


def _resolution_path():
    return _path("users", UID, "manualReplyResolutions", _resolution_key())


def _authority_path():
    return _path(
        "users",
        UID,
        "manualReplyAuthorities",
        manual_reply.manual_reply_authority_key(
            uid=UID,
            client_id=CLIENT_ID,
            notification_id=NOTIFICATION_ID,
        ),
    )


def _authority_key(
    uid=UID,
    client_id=CLIENT_ID,
    notification_id=NOTIFICATION_ID,
):
    digest = hashlib.sha256()
    for member in (
        "sitesift-manual-reply-authority:v1",
        uid,
        client_id,
        notification_id,
    ):
        encoded = member.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _task7_outbox_document(*, source_alias=SOURCE_ALIAS):
    """Mirror the 29-field Task7 allowlist and the real queued reply shape."""

    return {
        "manualReplyLaneVersion": 1,
        "status": "queued",
        "source": "dashboard_inline_reply",
        "actionType": "reply",
        "actionAuditId": AUDIT_ID,
        "clientId": CLIENT_ID,
        "notificationClientId": CLIENT_ID,
        "notificationId": NOTIFICATION_ID,
        "threadId": THREAD_ID,
        "replyToMessageId": source_alias,
        # Task7 stores the raw Internet Message-ID as the thread message doc ID.
        "sourceMessageId": SOURCE_INTERNET_ID,
        # Graph IDs are mutable aliases until the server requests ImmutableId.
        "sourceGraphMessageId": source_alias,
        "sourceInternetMessageId": SOURCE_INTERNET_ID,
        "sourceMessage": {
            "graphMessageId": source_alias,
            "internetMessageId": SOURCE_INTERNET_ID,
        },
        "assignedEmails": ["broker@example.invalid"],
        "ccEmails": [],
        "script": "Synthetic reviewed reply.",
        "subject": "Synthetic subject",
        "contactName": "Synthetic Broker",
        "rowNumber": 42,
        "isPersonalized": True,
        "createdAt": PRODUCER_TIME,
        "sourceDeadLetterId": None,
        "actionReason": "needs_user_input:client_question",
        "forceScript": True,
        "scriptSelectionMode": "exact",
        "deleteNotificationOnSend": True,
        "resumeThreadOnSend": True,
    }


def _task7_action_audit_document(*, outbox_id=_OMIT, source_alias=SOURCE_ALIAS):
    """Mirror Task7's queued audit; the writer normally omits ``outboxId``."""

    audit = {
        "status": "queued",
        "actorUid": UID,
        "source": "dashboard_inline_reply",
        "actionType": "reply",
        "clientId": CLIENT_ID,
        "clientName": "Synthetic Client",
        "threadId": THREAD_ID,
        "notificationId": NOTIFICATION_ID,
        "rowAnchor": "42 Synthetic Way",
        "rowNumber": 42,
        "reason": "needs_user_input:client_question",
        "subject": "Synthetic subject",
        "suggestedBody": "Synthetic suggested reply.",
        "suggestedRecipients": ["broker@example.invalid"],
        "suggestedCcRecipients": [],
        "finalBody": "Synthetic reviewed reply.",
        "finalRecipients": ["broker@example.invalid"],
        "finalCcRecipients": [],
        "replyToMessageId": source_alias,
        "sourceMessageId": SOURCE_INTERNET_ID,
        "sourceGraphMessageId": source_alias,
        "sourceInternetMessageId": SOURCE_INTERNET_ID,
        "sourceMessage": {
            "graphMessageId": source_alias,
            "internetMessageId": SOURCE_INTERNET_ID,
        },
        "createdAt": PRODUCER_TIME,
        "updatedAt": PRODUCER_TIME,
    }
    if outbox_id is not _OMIT:
        audit["outboxId"] = outbox_id
    return audit


def _producer_authority_document(*, immutable_graph_message_id=_OMIT):
    """Cross-slice fixture for ``notifications`` authority production."""

    authority = {
        "schemaVersion": 1,
        "status": "eligible",
        "uid": UID,
        "clientId": CLIENT_ID,
        "threadId": THREAD_ID,
        "notificationId": NOTIFICATION_ID,
        "source": "dashboard_inline_reply",
        "graphLookupMessageId": SOURCE_ALIAS,
        "normalizedInternetMessageId": SOURCE_INTERNET_ID,
        "conversationId": "conversation-1",
        "authenticatedMailboxAddress": "sender@example.invalid",
        "fromAddress": "broker@example.invalid",
        "senderAddress": "broker@example.invalid",
        "sourceAudience": {
            "to": ["sender@example.invalid"],
            "cc": [],
            "bcc": [],
            "replyTo": [],
        },
        "audience": {
            "to": ["broker@example.invalid"],
            "cc": [],
            "bcc": [],
        },
        "createdAt": PRODUCER_TIME,
        "updatedAt": PRODUCER_TIME,
    }
    if immutable_graph_message_id is not _OMIT:
        authority["immutableGraphMessageId"] = immutable_graph_message_id
    return authority


def _task7_notification_document(*, source_alias=SOURCE_ALIAS):
    return {
        "kind": "action_needed",
        "priority": "important",
        "email": "broker@example.invalid",
        "threadId": THREAD_ID,
        "rowNumber": 42,
        "rowAnchor": "42 Synthetic Way",
        "createdAt": PRODUCER_TIME,
        "meta": {
            "reason": "needs_user_input:client_question",
            "replyToMessageId": source_alias,
            "sourceMessageId": source_alias,
            "sourceGraphMessageId": source_alias,
            "sourceInternetMessageId": SOURCE_INTERNET_ID,
        },
        "dedupeKey": "manual-authority:test",
        "manualReplyAuthorityKey": _authority_path()[-1],
    }


def _task7_optional_variants():
    def remove(field):
        return lambda outbox: outbox.pop(field)

    return {
        "cancel_false_present": lambda outbox: outbox.update(
            cancelRequested=False
        ),
        "subject_absent": remove("subject"),
        "subject_value": lambda outbox: outbox.update(subject="Other subject"),
        "contact_absent": remove("contactName"),
        "contact_value": lambda outbox: outbox.update(contactName="Other Broker"),
        "row_absent": remove("rowNumber"),
        "row_value": lambda outbox: outbox.update(rowNumber=43),
        "personalized_absent": remove("isPersonalized"),
        "personalized_value": lambda outbox: outbox.update(isPersonalized=False),
        "created_absent": remove("createdAt"),
        "created_value": lambda outbox: outbox.update(
            createdAt=PRODUCER_TIME + timedelta(seconds=1)
        ),
        "source_message_absent": remove("sourceMessage"),
        "source_message_value": lambda outbox: outbox["sourceMessage"].update(
            preview="Synthetic preview"
        ),
        "dead_letter_absent": remove("sourceDeadLetterId"),
        "action_reason_absent": remove("actionReason"),
        "action_reason_value": lambda outbox: outbox.update(
            actionReason="needs_user_input:other_question"
        ),
    }


def _seed_delivery_store(
    *, outbox_id=OUTBOX_ID, source_alias=SOURCE_ALIAS, with_server_authority=True
):
    store = _MemoryFirestore()
    store.documents.update({
        _outbox_path(outbox_id): _task7_outbox_document(source_alias=source_alias),
        _path("users", UID, "clients", CLIENT_ID): {
            "status": "live",
        },
        _path("users", UID, "threads", THREAD_ID): {
            "clientId": CLIENT_ID,
            "status": "paused",
            "followUpConfig": {"enabled": False, "processingBy": None},
        },
        _path("users", UID, "threads", THREAD_ID, "messages", SOURCE_INTERNET_ID): {
            "direction": "inbound",
            "from": "broker@example.invalid",
            "sourceMessage": {
                "graphMessageId": source_alias,
                "internetMessageId": SOURCE_INTERNET_ID,
            },
        },
        _path(
            "users", UID, "clients", CLIENT_ID, "notifications", NOTIFICATION_ID
        ): _task7_notification_document(source_alias=source_alias),
        _path("users", UID, "actionAudit", AUDIT_ID): _task7_action_audit_document(
            source_alias=source_alias
        ),
        _path("systemConfig", "campaignAccess"): {
            "automationEnabled": True,
            "allowedUids": [],
        },
    })
    if with_server_authority:
        authority = _producer_authority_document()
        authority["graphLookupMessageId"] = source_alias
        store.documents[_authority_path()] = authority
    return store


def _selected_account():
    return {
        "home_account_id": "synthetic-home-account-1",
        "local_account_id": "local-account-1",
        "environment": "login.microsoftonline.com",
        "realm": "synthetic-tenant-1",
        "username": "sender@example.invalid",
    }


class _RecordingTokenBroker:
    def __init__(
        self,
        *,
        accounts=None,
        selected_account=None,
        token_account=None,
        token_claims=None,
        cache_path_builder=None,
        event_log=None,
    ):
        self.accounts = (
            [_selected_account()] if accounts is None else deepcopy(accounts)
        )
        self.selected_account = (
            deepcopy(self.accounts[0])
            if selected_account is None and len(self.accounts) == 1
            else deepcopy(selected_account)
        )
        self.token_account = (
            deepcopy(self.selected_account)
            if token_account is None
            else deepcopy(token_account)
        )
        self.token_claims = deepcopy(token_claims or {
            "oid": "local-account-1",
            "tid": "synthetic-tenant-1",
            "preferred_username": "sender@example.invalid",
        })
        self.cache_path_builder = cache_path_builder or (
            lambda attempt_id: f"/synthetic/manual-reply/{attempt_id}.cache"
        )
        self.event_log = event_log if event_log is not None else []
        self.calls = []
        self.contexts = []

    def __call__(self, *, uid, outbox_id, attempt_id):
        call = {
            "uid": uid,
            "outbox_id": outbox_id,
            "attempt_id": attempt_id,
        }
        self.calls.append(call)
        self.event_log.append(("token_context", uid, outbox_id, attempt_id))
        context = {
            "headers": {"Authorization": f"Bearer synthetic-{attempt_id}"},
            "accounts": deepcopy(self.accounts),
            "selected_account": deepcopy(self.selected_account),
            "token_account": deepcopy(self.token_account),
            "token_claims": deepcopy(self.token_claims),
            "token_cache_path": self.cache_path_builder(attempt_id),
        }
        self.contexts.append(deepcopy(context))
        return context


class ManualReplyFirestoreDeliveryTests(unittest.TestCase):
    def _claim(
        self,
        store,
        *,
        outbox_id=OUTBOX_ID,
        worker_id="worker-1",
        graph_lookup_message_id=SOURCE_ALIAS,
        authority_key=None,
    ):
        claim = _required_callable(self, "claim_manual_reply_item")
        with patch("google.cloud.firestore.transactional", _fake_transactional):
            return claim(
                firestore_client=store,
                uid=UID,
                outbox_id=outbox_id,
                worker_id=worker_id,
                authority_key=authority_key or _authority_path()[-1],
                canonical_source={
                    "graphLookupMessageId": graph_lookup_message_id,
                    "immutableGraphMessageId": SOURCE_IMMUTABLE_ID,
                    "internetMessageId": SOURCE_INTERNET_ID,
                    "conversationId": "conversation-1",
                    "fromAddress": "broker@example.invalid",
                    "senderAddress": "broker@example.invalid",
                    "sender": "broker@example.invalid",
                    "audience": {
                        "to": ["broker@example.invalid"],
                        "cc": [],
                        "bcc": [],
                    },
                },
                selected_account=_selected_account(),
                graph_me={
                    "id": "local-account-1",
                    "mail": "mail-alias@example.invalid",
                    "userPrincipalName": "sender@example.invalid",
                },
            )

    def _prepare_item(
        self,
        store,
        *,
        outbox_id=OUTBOX_ID,
        worker_id="worker-1",
        http=None,
        accounts=None,
        mode_reader=lambda: "live",
        token_broker=None,
    ):
        prepare = _required_callable(self, "prepare_manual_reply_item")
        http = http or _RecordingHttp(event_log=store.event_log)
        token_broker = token_broker or _RecordingTokenBroker(
            accounts=accounts,
            event_log=store.event_log,
        )
        with patch("google.cloud.firestore.transactional", _fake_transactional):
            result = prepare(
                firestore_client=store,
                uid=UID,
                outbox_id=outbox_id,
                worker_id=worker_id,
                graph_context_provider=token_broker,
                http_client=http,
                outbound_mode_reader=mode_reader,
            )
        return result, http

    def test_authority_key_is_domain_separated_and_tuple_bound(self):
        key = _required_callable(self, "manual_reply_authority_key")
        baseline = key(
            uid=UID,
            client_id=CLIENT_ID,
            notification_id=NOTIFICATION_ID,
        )
        self.assertEqual(_authority_key(), baseline)
        self.assertRegex(baseline, r"^[0-9a-f]{64}$")
        self.assertNotEqual(
            baseline,
            key(
                uid=UID,
                client_id=f"{CLIENT_ID}:{NOTIFICATION_ID}",
                notification_id="suffix",
            ),
        )

    def test_cross_slice_fixture_matches_task7_and_authority_producer_shapes(self):
        store = _seed_delivery_store()
        outbox = store.documents[_outbox_path()]
        audit = store.documents[_path("users", UID, "actionAudit", AUDIT_ID)]
        notification = store.documents[
            _path(
                "users", UID, "clients", CLIENT_ID,
                "notifications", NOTIFICATION_ID,
            )
        ]
        authority = store.documents[_authority_path()]
        message_path = _path(
            "users", UID, "threads", THREAD_ID, "messages", SOURCE_INTERNET_ID
        )

        self.assertEqual(
            TASK7_MANUAL_REPLY_ALLOWED_FIELDS - {"cancelRequested"},
            set(outbox),
        )
        self.assertNotIn("outboxId", audit)
        self.assertNotIn("clientId", notification)
        self.assertEqual(_authority_path()[-1], notification["manualReplyAuthorityKey"])
        self.assertEqual(SOURCE_INTERNET_ID, outbox["sourceMessageId"])
        self.assertIn(message_path, store.documents)
        self.assertEqual(
            SOURCE_ALIAS,
            store.documents[message_path]["sourceMessage"]["graphMessageId"],
        )
        self.assertEqual(_producer_authority_document(), authority)
        self.assertNotIn("immutableGraphMessageId", authority)

    def test_authority_consumer_accepts_the_producer_dotless_address_contract(self):
        outbox = _task7_outbox_document()
        outbox["assignedEmails"] = ["broker@localhost"]
        authority = _producer_authority_document()
        authority.update({
            "authenticatedMailboxAddress": "sender@localhost",
            "fromAddress": "broker@localhost",
            "senderAddress": "broker@localhost",
            "sourceAudience": {
                "to": ["sender@localhost"],
                "cc": [],
                "bcc": [],
                "replyTo": [],
            },
            "audience": {
                "to": ["broker@localhost"],
                "cc": [],
                "bcc": [],
            },
        })

        canonical = manual_reply._eligible_authority_source(
            authority,
            uid=UID,
            outbox=outbox,
        )

        self.assertEqual("sender@localhost", canonical["authenticatedMailboxAddress"])
        self.assertEqual("broker@localhost", canonical["fromAddress"])
        self.assertEqual("broker@localhost", canonical["senderAddress"])
        self.assertEqual(["broker@localhost"], canonical["audience"]["to"])

    def test_task7_audit_outbox_id_is_optional_but_mismatch_blocks(self):
        for label, audit_outbox_id, expected_status in (
            ("omitted", _OMIT, "claimed"),
            ("exact", OUTBOX_ID, "claimed"),
            ("mismatch", "other-outbox", "manual_review"),
        ):
            with self.subTest(label=label):
                store = _seed_delivery_store()
                store.documents[
                    _path("users", UID, "actionAudit", AUDIT_ID)
                ] = _task7_action_audit_document(outbox_id=audit_outbox_id)

                result = self._claim(store)

                self.assertEqual(expected_status, result["status"])
                if expected_status == "claimed":
                    self.assertEqual(3, len(store.write_log))
                else:
                    self.assertEqual([], store.write_log)

    def test_final_reread_accepts_omitted_or_exact_audit_outbox_id(self):
        for label, audit_outbox_id, expected_status in (
            ("omitted", _OMIT, "prepared"),
            ("exact", OUTBOX_ID, "prepared"),
            ("mismatch", "other-outbox", "manual_review"),
        ):
            with self.subTest(label=label):
                store = _seed_delivery_store()
                store.documents[
                    _path("users", UID, "actionAudit", AUDIT_ID)
                ] = _task7_action_audit_document(outbox_id=audit_outbox_id)

                result, http = self._prepare_item(store)

                self.assertEqual(expected_status, result["status"])
                self.assertFalse(any(call["path"].endswith("/send") for call in http.calls))

    def test_task7_outbox_schema_is_closed_and_initial_server_fields_are_forbidden(self):
        base = _task7_outbox_document()
        self.assertEqual(
            TASK7_MANUAL_REPLY_ALLOWED_FIELDS - {"cancelRequested"},
            set(base),
        )

        for label, field, value in (
            ("unknown", "unexpectedClientField", "value"),
            ("processing_owner", "processingBy", None),
            ("processing_time", "processingAt", PRODUCER_TIME),
            ("server_route", "serverRoute", {"kind": "manual_reply"}),
            ("stored_permit", "permit", True),
            ("provider_outcome", "providerOutcome", {"status": "accepted"}),
        ):
            with self.subTest(label=label):
                store = _seed_delivery_store()
                store.documents[_outbox_path()][field] = value

                result = self._claim(store)

                self.assertIn(result["status"], {"invalid", "manual_review"})
                self.assertEqual([], store.write_log)

        for label, marker, expected_status in (
            ("absent", _OMIT, "claimed"),
            ("false", False, "claimed"),
            ("true", True, "manual_review"),
        ):
            with self.subTest(cancel=label):
                store = _seed_delivery_store()
                if marker is not _OMIT:
                    store.documents[_outbox_path()]["cancelRequested"] = marker

                result = self._claim(store)

                self.assertEqual(expected_status, result["status"])
                if expected_status == "claimed":
                    self.assertEqual(3, len(store.write_log))
                else:
                    self.assertEqual([], store.write_log)

    def test_every_accepted_task7_optional_field_presence_and_value_is_hash_bound(self):
        baseline_store = _seed_delivery_store()
        baseline = self._claim(baseline_store)
        self.assertEqual("claimed", baseline["status"])
        baseline_hash = baseline.get("snapshotHash")
        self.assertRegex(baseline_hash or "", r"^[0-9a-f]{64}$")

        for label, mutate in _task7_optional_variants().items():
            with self.subTest(label=label):
                store = _seed_delivery_store()
                mutate(store.documents[_outbox_path()])

                result = self._claim(store)

                self.assertEqual("claimed", result["status"])
                self.assertNotEqual(baseline_hash, result.get("snapshotHash"))

    def test_final_canonical_snapshot_hash_binds_task7_optional_presence_and_value(self):
        original_hash = manual_reply.manual_reply_snapshot_hash

        def prepare_and_capture(store):
            captured = []

            def record(snapshot):
                digest = original_hash(snapshot)
                captured.append((deepcopy(snapshot), digest))
                return digest

            with patch.object(
                manual_reply,
                "manual_reply_snapshot_hash",
                side_effect=record,
            ):
                result, http = self._prepare_item(store)
            self.assertEqual("prepared", result["status"])
            self.assertEqual(1, len(captured))
            self.assertFalse(any(call["path"].endswith("/send") for call in http.calls))
            return captured[0]

        baseline_snapshot, baseline_hash = prepare_and_capture(_seed_delivery_store())
        self.assertRegex(baseline_hash, r"^[0-9a-f]{64}$")
        self.assertEqual(
            TASK7_MANUAL_REPLY_ALLOWED_FIELDS - {"cancelRequested"},
            set(baseline_snapshot["outbox"]) - {
                "id", "processingBy", "processingAt", "serverRoute"
            },
        )
        self.assertEqual(
            {"processingBy", "processingAt", "serverRoute"},
            {
                field
                for field in ("processingBy", "processingAt", "serverRoute")
                if field in baseline_snapshot["outbox"]
            },
        )
        self.assertEqual(
            CLAIM_COMMIT_TIME,
            baseline_snapshot["outbox"]["processingAt"],
        )

        for label, mutate in _task7_optional_variants().items():
            with self.subTest(label=label):
                store = _seed_delivery_store()
                mutate(store.documents[_outbox_path()])

                _snapshot, digest = prepare_and_capture(store)

                self.assertNotEqual(baseline_hash, digest)

    def test_prepared_result_carries_final_snapshot_hash_and_claim_fence(self):
        store = _seed_delivery_store()
        claim_snapshot_hash = "c" * 64
        final_snapshot_hash = "f" * 64
        canonical_source = {
            **_source_binding(),
            "internetMessageId": SOURCE_INTERNET_ID.lower(),
        }

        with patch.object(
            manual_reply,
            "_validate_task7_outbox",
            side_effect=lambda data, _outbox_id: data,
        ), patch.object(
            manual_reply,
            "_eligible_authority_source",
            return_value={key: value for key, value in canonical_source.items()
                          if key != "immutableGraphMessageId"},
        ), patch.object(
            manual_reply,
            "_resolve_canonical_graph_source",
            return_value=(canonical_source, {
                "id": "local-account-1",
                "mail": "mail-alias@example.invalid",
                "userPrincipalName": "sender@example.invalid",
            }),
        ), patch.object(
            manual_reply,
            "claim_manual_reply_item",
            return_value={
                "status": "claimed",
                "reason": "claim_created",
                "resolutionKey": "resolution-1",
                "authorityKey": _authority_path()[-1],
                "snapshotHash": claim_snapshot_hash,
                "fence": "fence-1",
            },
        ), patch.object(
            manual_reply,
            "_final_manual_reply_snapshot",
            return_value={
                "status": "ready",
                "reason": "snapshot_bound",
                "snapshotHash": final_snapshot_hash,
            },
        ), patch.object(
            manual_reply,
            "prepare_canonical_manual_reply_draft",
            return_value={
                "status": "prepared",
                "reason": "draft_prepared",
                "immutableDraftId": "draft-immutable-1",
            },
        ):
            result, http = self._prepare_item(store)

        self.assertEqual("prepared", result["status"])
        self.assertEqual(final_snapshot_hash, result.get("snapshotHash"))
        self.assertEqual("fence-1", result.get("fence"))
        self.assertEqual("resolution-1", result.get("resolutionKey"))
        self.assertFalse(any(call["path"].endswith("/send") for call in http.calls))

    def test_initial_claim_atomically_canonicalizes_the_exact_outbox_document(self):
        store = _seed_delivery_store(source_alias="client-alias-key")

        result = self._claim(store, graph_lookup_message_id="client-alias-key")

        self.assertEqual("claimed", result["status"])
        self.assertEqual(_resolution_key(), result["resolutionKey"])
        claimed = store.documents[_outbox_path()]
        self.assertEqual("manual_reply_claimed", claimed["status"])
        self.assertEqual("worker-1", claimed["processingBy"])
        self.assertEqual(CLAIM_COMMIT_TIME, claimed["processingAt"])
        self.assertIsNotNone(claimed["processingAt"].utcoffset())
        self.assertEqual("manual_reply", claimed["serverRoute"]["kind"])
        self.assertEqual(_resolution_key(), claimed["serverRoute"]["resolutionKey"])
        self.assertTrue(claimed["serverRoute"]["fence"])
        self.assertNotIn("permit", claimed)
        authority = store.documents[_authority_path()]
        self.assertEqual(_authority_path()[-1], result["authorityKey"])
        expected_authority = _producer_authority_document()
        expected_authority["graphLookupMessageId"] = "client-alias-key"
        expected_authority.update({
            "status": "claimed",
            "ownerOutboxId": OUTBOX_ID,
            "actionAuditId": AUDIT_ID,
            "immutableGraphMessageId": SOURCE_IMMUTABLE_ID,
            "internetMessageId": SOURCE_INTERNET_ID,
            "reviewedBodyHash": hashlib.sha256(
                b"Synthetic reviewed reply."
            ).hexdigest(),
            "snapshotHash": result.get("snapshotHash"),
            "fence": claimed["serverRoute"]["fence"],
            "claimedAt": CLAIM_COMMIT_TIME,
            "updatedAt": CLAIM_COMMIT_TIME,
        })
        self.assertRegex(result.get("snapshotHash", ""), r"^[0-9a-f]{64}$")
        self.assertEqual(expected_authority, authority)
        resolution = store.documents[_resolution_path()]
        self.assertEqual("claimed", resolution["status"])
        self.assertEqual(authority["fence"], resolution["fence"])
        self.assertEqual(1, len(store.committed_transactions))
        self.assertEqual([], store.direct_reads)
        transaction = store.committed_transactions[0]
        self.assertEqual({
            _outbox_path(),
            _path("users", UID, "actionAudit", AUDIT_ID),
            _authority_path(),
            _resolution_path(),
        }, set(transaction.reads))
        self.assertEqual({
            _outbox_path(),
            _authority_path(),
            _resolution_path(),
        }, {path for _operation, path, _data, _merge in transaction.writes})

    def test_claim_uses_server_timestamps_and_retry_commits_once(self):
        store = _seed_delivery_store()
        store.retry_transaction_ordinal = 1

        result = self._claim(store)

        self.assertEqual("claimed", result["status"])
        self.assertEqual({1: 2}, store.transaction_callback_runs)
        self.assertEqual(1, len(store.committed_transactions))
        self.assertEqual(3, len(store.write_log))
        claimed = store.documents[_outbox_path()]
        authority = store.documents[_authority_path()]
        for value in (
            claimed.get("processingAt"),
            authority.get("claimedAt"),
            authority.get("updatedAt"),
        ):
            with self.subTest(value=value):
                self.assertIs(type(value), datetime)
                self.assertIsNotNone(value.utcoffset())

        transaction = store.committed_transactions[0]
        writes = {
            path: data
            for _operation, path, data, _merge in transaction.writes
        }
        self.assertIs(writes[_outbox_path()]["processingAt"], SERVER_TIMESTAMP)
        self.assertIs(writes[_authority_path()]["claimedAt"], SERVER_TIMESTAMP)
        self.assertIs(writes[_authority_path()]["updatedAt"], SERVER_TIMESTAMP)

    def test_claim_rejects_each_malformed_task7_v1_field_before_any_write(self):
        malformed = {
            "status": ("status", "cancel_requested"),
            "lane_version": ("manualReplyLaneVersion", "1"),
            "source": ("source", "dashboard"),
            "action": ("actionType", "send"),
            "audit_id": ("actionAuditId", ""),
            "client_id": ("clientId", ""),
            "notification_client": ("notificationClientId", "other-client"),
            "notification_id": ("notificationId", ""),
            "thread_id": ("threadId", ""),
            "reply_alias": ("replyToMessageId", "other-alias"),
            "source_alias": ("sourceMessageId", "other-alias"),
            "source_graph": ("sourceGraphMessageId", ""),
            "source_internet": ("sourceInternetMessageId", ""),
            "recipient_type": ("assignedEmails", "broker@example.invalid"),
            "recipient_count": ("assignedEmails", ["one@example.invalid", "two@example.invalid"]),
            "cc": ("ccEmails", ["copied@example.invalid"]),
            "script": ("script", ""),
            "force_script": ("forceScript", False),
            "selection": ("scriptSelectionMode", "generated"),
            "delete_flag": ("deleteNotificationOnSend", False),
            "resume_flag": ("resumeThreadOnSend", False),
        }
        for name, (field, value) in malformed.items():
            with self.subTest(name=name):
                store = _seed_delivery_store()
                store.documents[_outbox_path()][field] = value

                result = self._claim(store)

                self.assertIn(result["status"], {"invalid", "manual_review"})
                self.assertEqual([], store.write_log)

    def test_cloned_outbox_cannot_reclaim_one_authority_or_resolution(self):
        store = _seed_delivery_store(outbox_id="clone-a")
        clone = deepcopy(store.documents[_outbox_path("clone-a")])
        clone.update(actionAuditId="audit-2")
        store.documents[_outbox_path("clone-b")] = clone
        clone_audit = deepcopy(
            store.documents[_path("users", UID, "actionAudit", AUDIT_ID)]
        )
        clone_audit.update(outboxId="clone-b")
        store.documents[_path("users", UID, "actionAudit", "audit-2")] = clone_audit

        first = self._claim(store, outbox_id="clone-a")
        write_count = len(store.write_log)
        second = self._claim(store, outbox_id="clone-b")

        self.assertEqual("claimed", first["status"])
        self.assertIn(second["status"], {"manual_review", "already_claimed"})
        self.assertEqual(write_count, len(store.write_log))
        self.assertEqual("clone-a", store.documents[_authority_path()]["ownerOutboxId"])
        self.assertEqual("clone-a", store.documents[_resolution_path()]["outboxId"])
        self.assertEqual("queued", store.documents[_outbox_path("clone-b")]["status"])

    def test_wrong_authority_key_fails_before_any_write(self):
        store = _seed_delivery_store()

        result = self._claim(store, authority_key="0" * 64)

        self.assertIn(result["status"], {"invalid", "manual_review"})
        self.assertEqual([], store.write_log)

    def test_authority_must_be_eligible_and_match_canonical_graph_projection(self):
        def move_authenticated_mailbox(authority):
            authority["authenticatedMailboxAddress"] = "other-account@example.invalid"
            authority["sourceAudience"]["to"] = ["other-account@example.invalid"]

        mutators = (
            lambda authority: authority.update(status="revoked"),
            lambda authority: authority.update(uid="other-user"),
            lambda authority: authority.update(clientId="other-client"),
            lambda authority: authority.update(notificationId="other-notification"),
            lambda authority: authority.update(graphLookupMessageId="other-alias"),
            lambda authority: authority.update(
                normalizedInternetMessageId="<other@example.invalid>"
            ),
            lambda authority: authority.update(conversationId="other-conversation"),
            lambda authority: authority.update(
                authenticatedMailboxAddress="other-account@example.invalid"
            ),
            move_authenticated_mailbox,
            lambda authority: authority.update(fromAddress="other@example.invalid"),
            lambda authority: authority.update(senderAddress="other@example.invalid"),
            lambda authority: authority["sourceAudience"].update(
                to=["other-account@example.invalid"]
            ),
            lambda authority: authority["sourceAudience"].update(
                cc=["observer@example.invalid"]
            ),
            lambda authority: authority["sourceAudience"].update(
                bcc=["observer@example.invalid"]
            ),
            lambda authority: authority["sourceAudience"].update(
                replyTo=["alternate@example.invalid"]
            ),
            lambda authority: authority["audience"].update(
                to=["other@example.invalid"]
            ),
            lambda authority: authority["audience"].update(
                cc=["copied@example.invalid"]
            ),
            lambda authority: authority.update(createdAt=datetime(2026, 8, 15)),
            lambda authority: authority.update(updatedAt="client-clock"),
        )
        for mutate in mutators:
            with self.subTest(mutate=mutate):
                store = _seed_delivery_store()
                mutate(store.documents[_authority_path()])

                result = self._claim(store)

                self.assertIn(result["status"], {"invalid", "manual_review"})
                self.assertEqual([], store.write_log)

    def test_optional_producer_immutable_id_must_match_graph_resolution(self):
        for immutable_id, expected_status in (
            (SOURCE_IMMUTABLE_ID, "claimed"),
            ("different-immutable-id", "manual_review"),
        ):
            with self.subTest(immutable_id=immutable_id):
                store = _seed_delivery_store()
                store.documents[_authority_path()] = _producer_authority_document(
                    immutable_graph_message_id=immutable_id
                )

                result = self._claim(store)

                self.assertEqual(expected_status, result["status"])
                if expected_status == "claimed":
                    self.assertEqual(3, len(store.write_log))
                else:
                    self.assertEqual([], store.write_log)

    def test_prepare_resolves_graph_alias_before_claim_transaction(self):
        store = _seed_delivery_store()

        result, http = self._prepare_item(store)

        self.assertEqual("prepared", result["status"])
        alias_event = (
            "graph",
            "GET",
            f"/me/messages/{SOURCE_ALIAS}",
        )
        self.assertIn(alias_event, store.event_log)
        claim_read = next(
            index
            for index, event in enumerate(store.event_log)
            if event[:2] == ("firestore_transaction_read", 1)
        )
        self.assertLess(store.event_log.index(alias_event), claim_read)
        self.assertFalse(any(call["path"].endswith("/send") for call in http.calls))

    def test_every_final_snapshot_input_is_reread_and_mutation_blocks_effects(self):
        mutations = {
            "outbox": lambda store: store.documents[_outbox_path()].update(
                script="Changed body"
            ),
            "outbox_processing_time": lambda store: store.documents[
                _outbox_path()
            ].update(processingAt=CLAIM_COMMIT_TIME + timedelta(seconds=1)),
            "active_client": lambda store: store.documents[
                _path("users", UID, "clients", CLIENT_ID)
            ].update(status="stopped"),
            "archived_client": lambda store: store.documents.__setitem__(
                _path("users", UID, "archivedClients", CLIENT_ID),
                {"status": "archived"},
            ),
            "thread": lambda store: store.documents[
                _path("users", UID, "threads", THREAD_ID)
            ].update(status="stopped"),
            "follow_up": lambda store: store.documents[
                _path("users", UID, "threads", THREAD_ID)
            ]["followUpConfig"].update(enabled=True),
            "notification": lambda store: store.documents[
                _path("users", UID, "clients", CLIENT_ID, "notifications", NOTIFICATION_ID)
            ].update(kind="dismissed"),
            "notification_authority_key": lambda store: store.documents[
                _path("users", UID, "clients", CLIENT_ID, "notifications", NOTIFICATION_ID)
            ].update(manualReplyAuthorityKey="0" * 64),
            "action_audit": lambda store: store.documents[
                _path("users", UID, "actionAudit", AUDIT_ID)
            ].update(status="cancelled"),
            "source_binding": lambda store: store.documents[
                _path(
                    "users", UID, "threads", THREAD_ID, "messages", SOURCE_INTERNET_ID
                )
            ]["sourceMessage"].update(
                internetMessageId="<drifted@example.invalid>"
            ),
            "server_authority": lambda store: store.documents[
                _authority_path()
            ]["audience"].update(to=["other@example.invalid"]),
            "authority_claim_hash": lambda store: store.documents[
                _authority_path()
            ].update(snapshotHash="0" * 64),
            "authority_body_hash": lambda store: store.documents[
                _authority_path()
            ].update(reviewedBodyHash="0" * 64),
            "authority_claim_time": lambda store: store.documents[
                _authority_path()
            ].update(claimedAt=CLAIM_COMMIT_TIME + timedelta(seconds=1)),
            "logical_resolution": lambda store: store.documents[
                _resolution_path()
            ].update(fence="different-fence"),
            "global_access": lambda store: store.documents[
                _path("systemConfig", "campaignAccess")
            ].update(automationEnabled=False),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                store = _seed_delivery_store()
                store.after_commit = mutate

                result, http = self._prepare_item(store)

                self.assertEqual("manual_review", result["status"])
                self.assertFalse(any("createReply" in call["path"] for call in http.calls))
                self.assertFalse(any(call["path"].endswith("/send") for call in http.calls))
                resolution_writes = [
                    entry for entry in store.write_log
                    if "manualReplyResolutions" in entry[1]
                ]
                self.assertEqual(1, len(resolution_writes))
                self.assertFalse(any(
                    "sendCounters" in path
                    for _operation, path, _data, _merge in store.write_log
                ))

    def test_global_disabled_or_read_error_is_not_hidden_by_client_maintenance(self):
        for global_value in (
            {"automationEnabled": False, "allowedUids": []},
            RuntimeError("synthetic global read error"),
        ):
            with self.subTest(global_value=global_value):
                store = _seed_delivery_store()
                store.documents[_path("users", UID, "clients", CLIENT_ID)].update({
                    "automationPaused": True,
                    "automationPauseReason": "maintenance_window",
                })

                def mutate(root):
                    root.documents[_path("systemConfig", "campaignAccess")] = global_value

                store.after_commit = mutate
                result, http = self._prepare_item(store)

                self.assertEqual("manual_review", result["status"])
                self.assertIn(
                    result.get("reason"),
                    {"global_access_disabled", "global_access_unavailable"},
                )
                self.assertFalse(any("createReply" in call["path"] for call in http.calls))
                self.assertFalse(any("sendCounters" in path for _, path, _, _ in store.write_log))
                global_path = _path("systemConfig", "campaignAccess")
                self.assertTrue(any(
                    event[-1] == global_path
                    for event in store.event_log
                    if event[0] in {
                        "firestore_direct_read",
                        "firestore_transaction_read",
                    }
                ))

    def test_transaction_callback_retry_prepares_once_without_provider_send(self):
        store = _seed_delivery_store()
        store.retry_transaction_ordinal = 2

        result, http = self._prepare_item(store)

        self.assertEqual("prepared", result["status"])
        self.assertEqual(
            1,
            sum("createReply" in call["path"] for call in http.calls),
        )
        self.assertFalse(any(call["path"].endswith("/send") for call in http.calls))
        self.assertFalse(any("sendCounters" in path for _, path, _, _ in store.write_log))

    def test_malformed_authority_accounts_and_untrusted_source_stop_without_effect(self):
        cases = (
            (_RecordingTokenBroker(accounts=[]), None),
            (_RecordingTokenBroker(accounts=[
                _selected_account(),
                {**_selected_account(), "home_account_id": "other"},
            ]), None),
            (_RecordingTokenBroker(
                token_account={**_selected_account(), "realm": "other-tenant"},
            ), None),
            (_RecordingTokenBroker(
                token_claims={
                    "oid": "other-local-account",
                    "tid": "synthetic-tenant-1",
                    "preferred_username": "sender@example.invalid",
                },
            ), None),
            (_RecordingTokenBroker(), {"automationEnabled": "true", "allowedUids": []}),
            (_RecordingTokenBroker(), {"automationEnabled": True, "allowedUids": "all"}),
        )
        for token_broker, malformed_global in cases:
            with self.subTest(
                accounts=token_broker.accounts,
                malformed_global=malformed_global,
            ):
                store = _seed_delivery_store()
                token_broker.event_log = store.event_log
                if malformed_global is not None:
                    store.documents[_path("systemConfig", "campaignAccess")] = malformed_global
                result, http = self._prepare_item(
                    store,
                    token_broker=token_broker,
                )
                self.assertEqual("manual_review", result["status"])
                self.assertFalse(any("createReply" in call["path"] for call in http.calls))
                self.assertFalse(any("sendCounters" in path for _, path, _, _ in store.write_log))

        store = _seed_delivery_store(with_server_authority=False)
        store.documents[_outbox_path()]["sourceAuthority"] = {
            "kind": "owner_writable",
            "schemaVersion": 1,
        }
        result, http = self._prepare_item(store)
        self.assertEqual("manual_review", result["status"])
        self.assertEqual([], http.calls)

    def test_token_broker_is_item_scoped_and_uses_distinct_attempt_cache_paths(self):
        contexts = []
        for outbox_id, worker_id in (("outbox-a", "worker-a"), ("outbox-b", "worker-b")):
            store = _seed_delivery_store(outbox_id=outbox_id)
            broker = _RecordingTokenBroker(event_log=store.event_log)

            _result, _http = self._prepare_item(
                store,
                outbox_id=outbox_id,
                worker_id=worker_id,
                token_broker=broker,
            )

            self.assertEqual(1, len(broker.calls))
            call = broker.calls[0]
            self.assertEqual(UID, call["uid"])
            self.assertEqual(outbox_id, call["outbox_id"])
            contexts.append(deepcopy(broker.contexts[0]))

        self.assertNotEqual(
            contexts[0]["token_cache_path"],
            contexts[1]["token_cache_path"],
        )
        self.assertTrue(all(
            context["selected_account"] == context["token_account"]
            for context in contexts
        ))

    def test_same_item_retries_use_fresh_attempt_identities(self):
        observed = []

        def stop_after_attempt(**context):
            observed.append(deepcopy(context))
            raise RuntimeError("synthetic stop after attempt allocation")

        with patch.object(
            manual_reply,
            "_validate_task7_outbox",
            side_effect=lambda data, _outbox_id: data,
        ), patch.object(
            manual_reply,
            "_eligible_authority_source",
            return_value={},
        ):
            for _ordinal in range(2):
                store = _seed_delivery_store()

                result, http = self._prepare_item(
                    store,
                    token_broker=stop_after_attempt,
                )

                self.assertEqual("manual_review", result["status"])
                self.assertEqual("source_validation_failed", result["reason"])
                self.assertEqual([], http.calls)

        self.assertEqual(2, len(observed))
        for context in observed:
            self.assertEqual(UID, context["uid"])
            self.assertEqual(OUTBOX_ID, context["outbox_id"])
            self.assertRegex(context["attempt_id"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(observed[0]["attempt_id"], observed[1]["attempt_id"])

    def test_token_cache_binding_is_exact_not_attempt_id_substring(self):
        builders = (
            lambda attempt_id: f"/synthetic/manual-reply/prefix-{attempt_id}.cache",
            lambda attempt_id: f"/synthetic/manual-reply/{attempt_id}-suffix.cache",
            lambda attempt_id: f"/synthetic/{attempt_id}/manual-reply.cache",
            lambda attempt_id: f"/synthetic/manual-reply/{attempt_id}.cache.backup",
        )
        for builder in builders:
            with self.subTest(path_shape=builder("attempt")):
                store = _seed_delivery_store()
                broker = _RecordingTokenBroker(
                    cache_path_builder=builder,
                    event_log=store.event_log,
                )

                with patch.object(
                    manual_reply,
                    "_validate_task7_outbox",
                    side_effect=lambda data, _outbox_id: data,
                ), patch.object(
                    manual_reply,
                    "_eligible_authority_source",
                    return_value={
                        "graphLookupMessageId": SOURCE_ALIAS,
                        "internetMessageId": SOURCE_INTERNET_ID.lower(),
                        "conversationId": "conversation-1",
                        "fromAddress": "broker@example.invalid",
                        "senderAddress": "broker@example.invalid",
                        "sender": "broker@example.invalid",
                        "audience": {
                            "to": ["broker@example.invalid"],
                            "cc": [],
                            "bcc": [],
                        },
                    },
                ):
                    result, http = self._prepare_item(store, token_broker=broker)

                self.assertEqual("manual_review", result["status"])
                self.assertEqual("source_validation_failed", result["reason"])
                self.assertEqual(1, len(broker.calls))
                self.assertEqual([], http.calls)
                self.assertEqual([], store.write_log)

    def test_canonical_source_pair_and_cross_document_thread_bindings_cannot_drift(self):
        mutators = (
            lambda store: store.documents[
                _path(
                    "users", UID, "threads", THREAD_ID, "messages", SOURCE_INTERNET_ID
                )
            ]["sourceMessage"].update(graphMessageId="drifted-immutable-id"),
            lambda store: store.documents[
                _path("users", UID, "threads", THREAD_ID)
            ].update(clientId="other-client"),
            lambda store: store.documents[
                _path("users", UID, "clients", CLIENT_ID, "notifications", NOTIFICATION_ID)
            ].update(threadId="other-thread"),
            lambda store: store.documents[_outbox_path()].update(
                assignedEmails=["other@example.invalid"]
            ),
            lambda store: store.documents[
                _path("users", UID, "actionAudit", AUDIT_ID)
            ].update(finalBody="Changed reviewed body"),
            lambda store: store.documents[
                _path("users", UID, "actionAudit", AUDIT_ID)
            ].update(finalRecipients=["other@example.invalid"]),
        )
        for mutate in mutators:
            with self.subTest(mutate=mutate):
                store = _seed_delivery_store()
                mutate(store)

                result, http = self._prepare_item(store)

                self.assertEqual("manual_review", result["status"])
                self.assertFalse(any(call["path"].endswith("/send") for call in http.calls))


if __name__ == "__main__":
    unittest.main()
