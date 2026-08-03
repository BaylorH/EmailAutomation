import copy
from contextlib import ExitStack
import hashlib
import os
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import email as email_module
from email_automation import processing, send_permits, sent_mail_guard


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeSnapshot:
    def __init__(self, data=None, *, exists=True):
        self._data = data or {}
        self.exists = exists

    def to_dict(self):
        return dict(self._data)


class _FakeDocument:
    def __init__(self, user_data, collection_name=None):
        self._user_data = user_data
        self._collection_name = collection_name

    def get(self):
        if self._collection_name == "threads":
            return _FakeSnapshot({"clientId": "client-1", "status": "active"})
        return _FakeSnapshot(self._user_data)

    def collection(self, name):
        return _FakeCollection(self._user_data, name)


class _FakeCollection:
    def __init__(self, user_data, name):
        self._user_data = user_data
        self._name = name

    def document(self, _document_id):
        return _FakeDocument(self._user_data, self._name)


class _FakeFirestore:
    def __init__(self, user_data):
        self._user_data = user_data

    def collection(self, name):
        if name != "users":
            raise AssertionError(f"Unexpected Firestore collection {name}")
        return _FakeCollection(self._user_data, name)


class GraphSubjectBindingTests(unittest.TestCase):
    SUBJECT = "AW: Deal Ref AbC-42"
    SOURCE_SUBJECT = "Deal Ref AbC-42"
    DRIFTED_SUBJECT = "RE: Deal Ref AbC-42"
    RECIPIENT = "broker@example.test"
    SOURCE_ID = "source-subject-1"
    DRAFT_ID = "draft-subject-1"
    CONVERSATION_ID = "conversation-subject-1"
    HTML_BODY = "<p>Thanks for the update.</p>"

    def tearDown(self):
        processing._reset_reply_send_outcome()

    def _sent_message(self, *, subject=None):
        return {
            "id": self.DRAFT_ID,
            "isDraft": False,
            "internetMessageId": "<subject-binding@example.test>",
            "conversationId": self.CONVERSATION_ID,
            "subject": self.SUBJECT if subject is None else subject,
            "sentDateTime": "2026-08-02T18:00:00Z",
            "toRecipients": [
                {"emailAddress": {"address": self.RECIPIENT}}
            ],
            "ccRecipients": [],
            "bccRecipients": [],
            "body": {"contentType": "HTML", "content": self.HTML_BODY},
            "bodyPreview": "Thanks for the update.",
            "attachments": [],
        }

    def _prepared_envelope(self):
        immutable = {
            "version": send_permits.GRAPH_DRAFT_PREPARATION_VERSION,
            "parentPermitId": "permit-subject-1",
            "parentPermitImmutableHash": "permit-immutable-hash-subject-1",
            "sourceGraphMessageId": self.SOURCE_ID,
            "draftId": self.DRAFT_ID,
            "subject": self.SUBJECT,
            "htmlBodyHash": sent_mail_guard.canonical_graph_body_hash(
                self.HTML_BODY
            ),
            "toRecipients": [self.RECIPIENT],
            "ccRecipients": [],
            "attachments": [],
        }
        return {
            **immutable,
            "preparedEnvelopeHash": send_permits._hash(immutable),
        }

    def _permit_for_terminal_sent_validation(self):
        prepared = self._prepared_envelope()
        body_hash = hashlib.sha256(b"Thanks for the update.").hexdigest()
        permit = {
            "permitId": prepared["parentPermitId"],
            "sourceGraphMessageId": self.SOURCE_ID,
            "conversationId": self.CONVERSATION_ID,
            "recipient": self.RECIPIENT,
            "bodyHash": body_hash,
            "requestStartedAt": datetime(2026, 8, 2, 17, 59, tzinfo=timezone.utc),
            "sendPreparedEnvelopeHash": prepared["preparedEnvelopeHash"],
            "preparedEnvelope": prepared,
        }
        evidence = {
            **self._sent_message(),
            "sentMessageId": self.DRAFT_ID,
            "recipient": self.RECIPIENT,
            "bodyHash": body_hash,
            "permitId": permit["permitId"],
            "sourceGraphMessageId": self.SOURCE_ID,
            "preparedEnvelopeHash": prepared["preparedEnvelopeHash"],
        }
        return permit, evidence

    def test_exact_immutable_sent_lookup_accepts_exact_subject(self):
        response = MagicMock(status_code=200)
        response.json.return_value = self._sent_message()

        with patch.object(sent_mail_guard.requests, "get", return_value=response):
            match = sent_mail_guard.find_exact_sent_message_by_immutable_id(
                {"Authorization": "Bearer test"},
                self.DRAFT_ID,
                subject=self.SUBJECT,
                attempts=1,
            )

        self.assertEqual(self.SUBJECT, match["subject"])

    def test_exact_immutable_sent_lookup_rejects_prefix_changed_subject(self):
        response = MagicMock(status_code=200)
        response.json.return_value = self._sent_message(
            subject=self.DRIFTED_SUBJECT
        )

        with patch.object(sent_mail_guard.requests, "get", return_value=response):
            with self.assertRaisesRegex(
                sent_mail_guard.SentMailGuardLookupError,
                "subject",
            ):
                sent_mail_guard.find_exact_sent_message_by_immutable_id(
                    {"Authorization": "Bearer test"},
                    self.DRAFT_ID,
                    subject=self.SUBJECT,
                    attempts=1,
                )

    def test_terminal_exact_sent_evidence_rejects_subject_drift(self):
        permit, evidence = self._permit_for_terminal_sent_validation()

        exact = send_permits._validate_exact_terminal_sent_evidence(
            permit,
            evidence,
        )
        self.assertEqual(self.SUBJECT, exact["subject"])

        drifted = {**evidence, "subject": self.DRIFTED_SUBJECT}
        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "subject|exact Sent evidence",
        ):
            send_permits._validate_exact_terminal_sent_evidence(
                permit,
                drifted,
            )

    def test_prepared_envelope_hash_binds_exact_subject(self):
        capability = SimpleNamespace(
            permit_id="permit-subject-1",
            immutable_hash="permit-immutable-hash-subject-1",
        )
        common = {
            "source_graph_message_id": self.SOURCE_ID,
            "draft_id": self.DRAFT_ID,
            "html_body": self.HTML_BODY,
            "to_recipients": [self.RECIPIENT],
            "cc_recipients": [],
            "attachments": [],
        }

        exact = send_permits._prepared_envelope(
            capability,
            subject=self.SUBJECT,
            **common,
        )
        changed = send_permits._prepared_envelope(
            capability,
            subject=self.DRIFTED_SUBJECT,
            **common,
        )

        self.assertEqual(self.SUBJECT, exact["subject"])
        self.assertNotEqual(
            exact["preparedEnvelopeHash"],
            changed["preparedEnvelopeHash"],
        )

    def test_prepared_envelope_schema_rejects_omitted_or_tampered_subject(self):
        envelope = self._prepared_envelope()
        permit = {
            "permitId": envelope["parentPermitId"],
            "immutableHash": envelope["parentPermitImmutableHash"],
            "sourceGraphMessageId": self.SOURCE_ID,
            "recipient": self.RECIPIENT,
            "draftPreparation": {"plannedAttachmentCount": 0},
        }

        send_permits._validate_prepared_envelope_state(permit, envelope)

        omitted = dict(envelope)
        omitted.pop("subject")
        omitted_core = {
            key: value
            for key, value in omitted.items()
            if key != "preparedEnvelopeHash"
        }
        omitted["preparedEnvelopeHash"] = send_permits._hash(omitted_core)
        with self.assertRaisesRegex(
            send_permits.GraphSendPermitError,
            "prepared envelope schema|subject",
        ):
            send_permits._validate_prepared_envelope_state(permit, omitted)

        tampered = {**envelope, "subject": self.DRIFTED_SUBJECT}
        with self.assertRaisesRegex(
            send_permits.GraphSendPermitError,
            "prepared envelope schema|subject",
        ):
            send_permits._validate_prepared_envelope_state(permit, tampered)

    def _run_runtime_subject_binding(self, *, sparse_create_response):
        patch_payloads = []
        draft_get_params = []
        recipient_payload = {
            "payload": {
                "toRecipients": [
                    {"emailAddress": {"address": self.RECIPIENT}}
                ],
                "ccRecipients": [],
            },
            "skipped": {},
        }

        def fake_get(url, **kwargs):
            if url.endswith(f"/me/messages/{self.SOURCE_ID}"):
                return _FakeResponse(
                    200,
                    {
                        "conversationId": self.CONVERSATION_ID,
                        "subject": self.SOURCE_SUBJECT,
                    },
                )
            if url.endswith(f"/me/messages/{self.DRAFT_ID}"):
                draft_get_params.append(dict(kwargs.get("params") or {}))
                return _FakeResponse(
                    200,
                    {
                        "id": self.DRAFT_ID,
                        "subject": self.SUBJECT,
                        "toRecipients": recipient_payload["payload"][
                            "toRecipients"
                        ],
                        "ccRecipients": [],
                    },
                )
            raise AssertionError(f"Unexpected Graph GET {url}")

        def fake_post(url, **_kwargs):
            if url.endswith("/createReplyAll"):
                payload = {"id": self.DRAFT_ID}
                if not sparse_create_response:
                    payload.update({
                        "subject": self.SUBJECT,
                        "toRecipients": recipient_payload["payload"][
                            "toRecipients"
                        ],
                        "ccRecipients": [],
                    })
                return _FakeResponse(201, payload)
            if url.endswith(f"/{self.DRAFT_ID}/send"):
                return _FakeResponse(202)
            raise AssertionError(f"Unexpected Graph POST {url}")

        def fake_patch(_url, **kwargs):
            patch_payloads.append(copy.deepcopy(kwargs.get("json") or {}))
            return _FakeResponse(204)

        allow = SimpleNamespace(denies_autonomous_work=False)
        capability = SimpleNamespace(permit_id="permit-subject-1")
        prepared = {
            "preparedEnvelopeHash": "prepared-envelope-hash-subject-1",
            "htmlBodyHash": sent_mail_guard.canonical_graph_body_hash(
                self.HTML_BODY
            ),
            "subject": self.SUBJECT,
            "timeoutSeconds": 30,
        }

        with ExitStack() as stack:
            stack.enter_context(patch.dict(
                os.environ,
                {
                    "SITESIFT_AUTO_REPLY_ALLOWLIST": "uid-1",
                    "SITESIFT_OUTBOUND_MODE": "live",
                },
            ))
            stack.enter_context(patch.object(
                processing,
                "get_client_automation_decision",
                return_value=allow,
            ))
            stack.enter_context(patch.object(
                processing,
                "format_email_body_with_footer",
                return_value=self.HTML_BODY,
            ))
            stack.enter_context(patch(
                "email_automation.clients._fs",
                _FakeFirestore({"email": "sender@example.test"}),
            ))
            stack.enter_context(patch(
                "email_automation.utils.exponential_backoff_request",
                side_effect=lambda operation, **_kwargs: operation(),
            ))
            stack.enter_context(patch.object(
                email_module,
                "exponential_backoff_request",
                side_effect=lambda operation, **_kwargs: operation(),
            ))
            stack.enter_context(patch(
                "email_automation.utils.resolve_signature_settings",
                return_value=(None, None, "sender@example.test"),
            ))
            stack.enter_context(patch(
                "email_automation.utils.needs_signature_attachments",
                return_value=False,
            ))
            stack.enter_context(patch.object(
                processing,
                "validate_graph_draft_attachment_plan",
                return_value=0,
            ))
            stack.enter_context(patch.object(
                processing,
                "begin_graph_draft_creation",
                return_value=30,
            ))
            stack.enter_context(patch.object(
                processing,
                "complete_graph_draft_creation",
                return_value={},
            ))
            freeze_envelope = stack.enter_context(patch.object(
                processing,
                "begin_graph_draft_patch",
                return_value=prepared,
            ))
            stack.enter_context(patch.object(
                processing,
                "complete_graph_draft_patch",
                return_value={},
            ))
            stack.enter_context(patch.object(
                processing,
                "finalize_graph_draft_preparation",
                return_value={},
            ))
            consume_envelope = stack.enter_context(patch.object(
                processing,
                "consume_graph_send_capability",
                return_value=30,
            ))
            stack.enter_context(patch.object(
                processing,
                "resolve_graph_send_permit",
                return_value={},
            ))
            stack.enter_context(patch.object(
                processing.requests,
                "get",
                side_effect=fake_get,
            ))
            stack.enter_context(patch.object(
                processing.requests,
                "post",
                side_effect=fake_post,
            ))
            stack.enter_context(patch.object(
                processing.requests,
                "patch",
                side_effect=fake_patch,
            ))
            stack.enter_context(patch.object(
                email_module,
                "_source_message_reply_all_fallback",
                side_effect=lambda draft, _source: draft,
            ))
            stack.enter_context(patch.object(
                email_module,
                "_reviewed_recipient_reply_all_fallback",
                side_effect=lambda draft, to_emails=None: draft,
            ))
            stack.enter_context(patch.object(
                email_module,
                "_filter_reply_all_draft_recipients",
                return_value=recipient_payload,
            ))
            exact_sent_lookup = stack.enter_context(patch.object(
                processing,
                "find_exact_sent_message_by_immutable_id",
                return_value=self._sent_message(),
            ))
            stack.enter_context(patch.object(
                processing.time,
                "sleep",
                return_value=None,
            ))
            stack.enter_context(patch(
                "email_automation.messaging.index_message_id",
                return_value=True,
            ))
            stack.enter_context(patch(
                "email_automation.messaging.lookup_thread_by_message_id",
                return_value="thread-1",
            ))
            stack.enter_context(patch(
                "email_automation.messaging.index_conversation_id",
                return_value=True,
            ))
            stack.enter_context(patch(
                "email_automation.messaging.save_message",
            ))
            sent = processing.send_reply_in_thread(
                user_id="uid-1",
                headers={"Authorization": "Bearer test"},
                body="Thanks for the update.",
                current_msg_id=self.SOURCE_ID,
                recipient=self.RECIPIENT,
                thread_id="thread-1",
                graph_send_capability=capability,
            )

        self.assertTrue(sent)
        self.assertEqual(1, len(patch_payloads))
        with self.subTest(binding="PATCH exact provider subject"):
            self.assertEqual(self.SUBJECT, patch_payloads[0].get("subject"))
        with self.subTest(binding="freeze exact provider subject"):
            self.assertEqual(
                self.SUBJECT,
                freeze_envelope.call_args.kwargs.get("subject"),
            )
        with self.subTest(binding="send consumes exact provider subject"):
            self.assertEqual(
                self.SUBJECT,
                consume_envelope.call_args.kwargs.get("subject"),
            )
        with self.subTest(binding="Sent reconciliation exact provider subject"):
            self.assertEqual(
                self.SUBJECT,
                exact_sent_lookup.call_args.kwargs.get("subject"),
            )
        if sparse_create_response:
            self.assertEqual(1, len(draft_get_params))
            with self.subTest(binding="sparse draft hydration requests subject"):
                self.assertIn(
                    "subject",
                    draft_get_params[0].get("$select", "").split(","),
                )

    def test_capability_runtime_full_create_reply_all_binds_provider_subject(self):
        self._run_runtime_subject_binding(sparse_create_response=False)

    def test_capability_runtime_sparse_create_reply_all_binds_provider_subject(self):
        self._run_runtime_subject_binding(sparse_create_response=True)


if __name__ == "__main__":
    unittest.main()
