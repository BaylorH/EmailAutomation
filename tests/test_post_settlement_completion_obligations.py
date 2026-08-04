import copy
import hashlib
import json
import sys
import types
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from email_automation import pending_responses, processing, send_permits
from email_automation.campaign_safety import CAMPAIGN_AUTOMATION_ALLOW
from tests.test_pending_responses import (
    FakeCollection,
    FakeDoc,
    FakeFirestore,
)
from tests import test_terminal_completion_replay as terminal_completion_fixture


_COMPLETION_COLLECTION = "pendingResponseCompletionObligations"
_COMPLETION_VERSION = 1


class _Query:
    def __init__(self, docs, filters=(), query_limit=None):
        self._docs = list(docs)
        self._filters = tuple(filters)
        self._limit = query_limit

    def where(self, *, filter):
        return _Query(
            self._docs,
            (*self._filters, filter),
            self._limit,
        )

    def limit(self, count):
        return _Query(self._docs, self._filters, count)

    def stream(self):
        docs = list(self._docs)
        for field_filter in self._filters:
            docs = [
                doc
                for doc in docs
                if doc.to_dict().get(field_filter.field_path)
                == field_filter.value
            ]
        docs = [doc for doc in docs if doc.exists]
        return docs[:self._limit] if self._limit is not None else docs


class _QueryableCollection(FakeCollection):
    def _all_docs(self):
        docs = list(self.docs)
        for ref in self._missing_refs.values():
            snapshot = ref.get()
            if snapshot.exists and snapshot not in docs:
                docs.append(snapshot)
        return docs

    def stream(self):
        return [doc for doc in self._all_docs() if doc.exists]

    def where(self, *, filter):
        return _Query(self._all_docs(), (filter,))


class _CompletionCrash(BaseException):
    pass


class PendingCompletionObligationSchemaTests(unittest.TestCase):
    def _build(
        self,
        *,
        client_id,
        complete_client_after_reply,
        source_authority_protocol="legacy",
    ):
        return send_permits.pending_completion_obligation_payload(
            user_id="uid-completion-schema",
            client_id=client_id,
            thread_id="thread-completion-schema",
            pending_document_id="pending-completion-schema",
            source_graph_message_id="source-completion-schema",
            pending_envelope_hash_value="a" * 64,
            permit_id="graph-send-completion-schema",
            permit_immutable_hash="b" * 64,
            sent_evidence={"sentMessageId": "sent-completion-schema"},
            complete_client_after_reply=complete_client_after_reply,
            source_authority_protocol=source_authority_protocol,
        )

    def test_builder_persists_versioned_exact_source_protocol(self):
        _legacy_id, legacy = self._build(
            client_id="client-completion-schema",
            complete_client_after_reply=True,
        )
        exact_id, exact = self._build(
            client_id="client-completion-schema",
            complete_client_after_reply=True,
            source_authority_protocol="b1_exact_source",
        )

        self.assertEqual(1, legacy["version"])
        self.assertNotIn("sourceAuthorityProtocol", legacy["immutable"])
        self.assertEqual(2, exact["version"])
        self.assertEqual(2, exact["immutable"]["version"])
        self.assertEqual(
            "b1_exact_source",
            exact["immutable"]["sourceAuthorityProtocol"],
        )
        self.assertEqual(
            exact,
            send_permits.validate_pending_completion_obligation_payload(
                exact,
                document_id=exact_id,
                expected_user_id="uid-completion-schema",
            ),
        )

    def test_builder_rejects_client_binding_when_completion_is_not_required(self):
        with self.assertRaises(send_permits.GraphSendPermitBlocked):
            self._build(
                client_id="client-completion-schema",
                complete_client_after_reply=False,
            )

    def test_validator_rejects_settled_outcome_incompatible_with_binding(self):
        now = datetime(2026, 8, 2, 20, 0, tzinfo=timezone.utc)
        cases = (
            ("client-completion-schema", True, "not_required"),
            ("", False, "client_completed"),
            ("", False, "client_ineligible"),
        )
        for client_id, complete_client_after_reply, outcome in cases:
            with self.subTest(
                complete_client_after_reply=complete_client_after_reply,
                outcome=outcome,
            ):
                obligation_id, payload = self._build(
                    client_id=client_id,
                    complete_client_after_reply=complete_client_after_reply,
                )
                payload.update({
                    "status": "settled",
                    "completionOutcome": outcome,
                    "settledAt": now,
                    "updatedAt": now,
                })
                with self.assertRaises(send_permits.GraphSendPermitBlocked):
                    send_permits.validate_pending_completion_obligation_payload(
                        payload,
                        document_id=obligation_id,
                        expected_user_id="uid-completion-schema",
                    )


class PendingSuccessCompletionObligationTests(unittest.TestCase):
    USER_ID = "uid-pending-completion"
    CLIENT_ID = "client-pending-completion"

    def _pending_data(self, suffix, *, client_id=None):
        return {
            "threadId": f"thread-{suffix}",
            "msgId": f"source-{suffix}",
            "recipient": "broker@example.test",
            "responseBody": "Thank you for the update.",
            "clientId": self.CLIENT_ID if client_id is None else client_id,
            "conversationId": f"conversation-{suffix}",
            "attempts": 0,
        }

    def _firestore(self, pending_doc, *, client_status="live"):
        fake_fs = FakeFirestore([pending_doc] if pending_doc is not None else [])
        if pending_doc is not None:
            pending_data = pending_doc.to_dict() or {}
            thread_snapshot = fake_fs.collections["threads"].document(
                pending_data["threadId"]
            ).get()
            thread_snapshot._data["clientId"] = pending_data.get("clientId")
        fake_fs.collections["clients"] = _QueryableCollection([
            FakeDoc(self.CLIENT_ID, {"status": client_status}),
        ])
        fake_fs.collections[_COMPLETION_COLLECTION] = _QueryableCollection()
        return fake_fs

    def _clients_patch(self, fake_fs):
        return patch.dict(
            sys.modules,
            {"email_automation.clients": types.SimpleNamespace(_fs=fake_fs)},
        )

    def _prepare_and_accept(self, capability):
        permit = send_permits.read_permit(capability)
        draft_id = f"draft-{capability.permit_id}"
        subject = "RE: Pending completion durability"
        html_body = "<p>Thank you for the update.</p>"
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
            "subject": subject,
            "sentMessageId": draft_id,
            "recipient": retained["recipient"],
            "bodyHash": retained["bodyHash"],
            "conversationId": retained.get("conversationId"),
            "sentDateTime": retained["requestStartedAt"] + timedelta(seconds=1),
            "permitId": retained["permitId"],
            "sourceGraphMessageId": retained["sourceGraphMessageId"],
            "preparedEnvelopeHash": prepared["preparedEnvelopeHash"],
            "toRecipients": [{"emailAddress": {"address": recipient}}],
            "ccRecipients": [],
            "bccRecipients": [],
            "body": {"contentType": "HTML", "content": html_body},
            "attachments": [],
        }

    def _obligation_payload(
        self,
        data,
        capability,
        sent_evidence,
        *,
        complete=True,
        overrides=None,
    ):
        immutable = {
            "version": _COMPLETION_VERSION,
            "kind": "pending_response_client_completion",
            "userId": self.USER_ID,
            "clientId": data["clientId"],
            "threadId": data["threadId"],
            "pendingDocumentId": data["threadId"],
            "sourceGraphMessageId": data["msgId"],
            "pendingEnvelopeHash": send_permits.pending_envelope_hash(data),
            "permitId": capability.permit_id,
            "permitImmutableHash": capability.immutable_hash,
            "sentEvidenceHash": send_permits._stable_evidence_hash(sent_evidence),
            "completeClientAfterReply": complete,
        }
        immutable.update(dict(overrides or {}))
        immutable_hash = hashlib.sha256(
            json.dumps(immutable, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        obligation_id = f"pending-completion-{immutable_hash}"
        now = datetime(2026, 8, 2, 20, 0, tzinfo=timezone.utc)
        return obligation_id, {
            "version": _COMPLETION_VERSION,
            "obligationId": obligation_id,
            "immutable": immutable,
            "immutableHash": immutable_hash,
            "status": "owed",
            "completionOutcome": None,
            "settledAt": None,
            "createdAt": now,
            "updatedAt": now,
        }

    def _all_obligations(self, fake_fs):
        return fake_fs.collections[_COMPLETION_COLLECTION].stream()

    def _assert_valid_owed_obligation(
        self,
        doc,
        data,
        capability,
        sent_evidence,
    ):
        expected_id, expected = self._obligation_payload(
            data,
            capability,
            sent_evidence,
        )
        actual = doc.to_dict()
        self.assertEqual(expected_id, doc.id)
        self.assertEqual(expected["immutable"], actual.get("immutable"))
        self.assertEqual(expected["immutableHash"], actual.get("immutableHash"))
        self.assertEqual("owed", actual.get("status"))

    def _seed_settled_permit(
        self,
        suffix,
        *,
        client_status="live",
        client_id=None,
    ):
        data = self._pending_data(suffix, client_id=client_id)
        pending_doc = FakeDoc(data["threadId"], copy.deepcopy(data))
        fake_fs = self._firestore(pending_doc, client_status=client_status)
        with self._clients_patch(fake_fs):
            claim = pending_responses._claim_pending_response_for_send(
                self.USER_ID,
                pending_doc,
                copy.deepcopy(data),
            )
            capability = pending_responses._final_pending_response_send_fence(
                self.USER_ID,
                pending_doc,
                copy.deepcopy(data),
                claim,
            )
            sent_evidence = self._prepare_and_accept(capability)
            pending_responses._cas_pending_success(
                self.USER_ID,
                pending_doc,
                copy.deepcopy(data),
                claim,
                capability,
                sent_evidence,
            )
        self.assertTrue(pending_doc.reference.deleted)
        self.assertEqual("settled_sent", send_permits.read_permit(capability)["status"])
        return fake_fs, pending_doc, data, capability, sent_evidence

    def _seed_manual_obligation(
        self,
        suffix,
        *,
        client_status="live",
        client_id=None,
        complete=True,
        overrides=None,
    ):
        fake_fs, pending_doc, data, capability, evidence = (
            self._seed_settled_permit(
                suffix,
                client_status=client_status,
                client_id=client_id,
            )
        )
        obligation_id, payload = self._obligation_payload(
            data,
            capability,
            evidence,
            complete=complete,
            overrides=overrides,
        )
        for existing in self._all_obligations(fake_fs):
            existing.exists = False
        obligation_doc = FakeDoc(obligation_id, payload)
        fake_fs.collections[_COMPLETION_COLLECTION].docs.append(obligation_doc)
        return (
            fake_fs,
            pending_doc,
            data,
            capability,
            evidence,
            obligation_doc,
        )

    def _process(self, fake_fs, *, completion_effect):
        send = MagicMock(name="send_reply_in_thread")
        exact_sent_lookup = MagicMock(name="find_exact_sent_message_by_immutable_id")
        heuristic_sent_lookup = MagicMock(name="find_matching_sent_message_for_retry")
        continuation_lookup = MagicMock(
            name="find_sent_conversation_continuation_for_retry"
        )
        with ExitStack() as stack:
            stack.enter_context(self._clients_patch(fake_fs))
            stack.enter_context(patch.object(processing, "_fs", fake_fs))
            stack.enter_context(patch.object(
                pending_responses,
                "_fs",
                fake_fs,
                create=True,
            ))
            stack.enter_context(patch.object(
                processing,
                "send_reply_in_thread",
                new=send,
            ))
            completion_patch = (
                patch.object(
                    processing,
                    "_maybe_mark_client_completed",
                    side_effect=completion_effect,
                )
                if isinstance(completion_effect, BaseException)
                else patch.object(
                    processing,
                    "_maybe_mark_client_completed",
                    return_value=completion_effect,
                )
            )
            stack.enter_context(completion_patch)
            stack.enter_context(patch.object(
                pending_responses,
                "find_exact_sent_message_by_immutable_id",
                new=exact_sent_lookup,
            ))
            stack.enter_context(patch.object(
                pending_responses,
                "find_matching_sent_message_for_retry",
                new=heuristic_sent_lookup,
            ))
            stack.enter_context(patch.object(
                pending_responses,
                "find_sent_conversation_continuation_for_retry",
                new=continuation_lookup,
            ))
            states = pending_responses.process_pending_responses(
                self.USER_ID,
                {"Authorization": "Bearer local-test"},
            )
        return (
            states,
            send,
            exact_sent_lookup,
            heuristic_sent_lookup,
            continuation_lookup,
        )

    def _last_state(self, states, expected_status):
        self.assertTrue(
            states,
            "pending-absent completion replay emitted no operation state",
        )
        self.assertEqual(expected_status, states[-1]["status"])
        return states[-1]

    def test_success_cas_includes_one_deterministic_completion_tombstone_in_same_transaction(self):
        data = self._pending_data("same-cas")
        pending_doc = FakeDoc(data["threadId"], copy.deepcopy(data))
        fake_fs = self._firestore(pending_doc)
        capability = types.SimpleNamespace(
            permit_id="graph-send-same-cas",
            immutable_hash="a" * 64,
            envelope_hash=send_permits.pending_envelope_hash(data),
            issuer_ref=pending_doc.reference,
        )
        evidence = {
            "sentMessageId": "draft-same-cas",
            "permitId": capability.permit_id,
        }
        settle = MagicMock(return_value=True)
        refs = (
            fake_fs,
            fake_fs,
            fake_fs.collections["threads"].document(data["threadId"]),
            pending_doc.reference,
        )
        with patch.object(
            pending_responses,
            "_pending_claim_refs",
            return_value=refs,
        ), patch.object(
            pending_responses,
            "cas_pending_claim_transition",
            new=settle,
        ):
            pending_responses._cas_pending_success(
                self.USER_ID,
                pending_doc,
                data,
                "pending-owner-same-cas",
                capability,
                evidence,
            )
            pending_responses._cas_pending_success(
                self.USER_ID,
                pending_doc,
                data,
                "pending-owner-same-cas",
                capability,
                evidence,
            )

        first_kwargs = settle.call_args_list[0].kwargs
        second_kwargs = settle.call_args_list[1].kwargs
        self.assertIn("side_documents", first_kwargs)
        self.assertEqual(1, len(first_kwargs["side_documents"]))
        first_ref, first_payload = first_kwargs["side_documents"][0]
        second_ref, second_payload = second_kwargs["side_documents"][0]
        self.assertEqual(first_ref.id, second_ref.id)
        self.assertEqual(first_payload["immutable"], second_payload["immutable"])
        self.assertEqual(first_payload["immutableHash"], second_payload["immutableHash"])
        self.assertEqual("owed", first_payload["status"])
        self.assertTrue(first_payload["immutable"]["completeClientAfterReply"])

    def test_exact_pending_success_stamps_immutable_b1_protocol(self):
        data = {
            **self._pending_data("exact-protocol"),
            "status": "sending",
            "canonicalSourceId": "source-exact-protocol",
            "workKey": "1" * 64,
            "proposalHash": "2" * 64,
            "selectionHash": "3" * 64,
            "pendingRevision": 2,
            "pendingProtocol": {
                "kind": "b1_exact_source",
                "version": 1,
            },
        }
        pending_doc = FakeDoc(data["threadId"], copy.deepcopy(data))
        fake_fs = self._firestore(pending_doc)
        capability = types.SimpleNamespace(
            permit_id="graph-send-exact-protocol",
            immutable_hash="a" * 64,
            envelope_hash=send_permits.pending_envelope_hash(data),
            issuer_ref=pending_doc.reference,
        )
        evidence = {
            "sentMessageId": "draft-exact-protocol",
            "permitId": capability.permit_id,
        }
        settle = MagicMock(return_value=True)
        refs = (
            fake_fs,
            fake_fs,
            fake_fs.collections["threads"].document(data["threadId"]),
            pending_doc.reference,
        )
        with patch.object(
            pending_responses,
            "_pending_claim_refs",
            return_value=refs,
        ), patch.object(
            pending_responses,
            "cas_pending_claim_transition",
            new=settle,
        ):
            pending_responses._cas_pending_success(
                self.USER_ID,
                pending_doc,
                data,
                "pending-response-b1-owner",
                capability,
                evidence,
            )

        _ref, payload = settle.call_args.kwargs["side_documents"][0]
        self.assertEqual(2, payload["version"])
        self.assertEqual(
            "b1_exact_source",
            payload["immutable"]["sourceAuthorityProtocol"],
        )

    def test_false_exception_and_crash_after_success_leave_settled_permit_and_owed_tombstone(self):
        cases = (
            ("false", False, None),
            ("exception", RuntimeError("local completion unavailable"), None),
            ("crash", _CompletionCrash("worker died"), _CompletionCrash),
        )
        for suffix, effect, raised in cases:
            with self.subTest(outcome=suffix):
                data = self._pending_data(f"post-success-{suffix}")
                pending_doc = FakeDoc(data["threadId"], copy.deepcopy(data))
                fake_fs = self._firestore(pending_doc)
                captured = {}

                def accepted_send(**kwargs):
                    capability = kwargs["graph_send_capability"]
                    captured["capability"] = capability
                    evidence = self._prepare_and_accept(capability)
                    captured["evidence"] = evidence
                    processing._set_reply_send_outcome(
                        outcome="sent_indexed",
                        exact_sent_evidence=evidence,
                    )
                    return True

                with ExitStack() as stack:
                    stack.enter_context(self._clients_patch(fake_fs))
                    stack.enter_context(patch.object(processing, "_fs", fake_fs))
                    stack.enter_context(patch.object(
                        pending_responses,
                        "get_client_automation_decision",
                        return_value=types.SimpleNamespace(
                            state=CAMPAIGN_AUTOMATION_ALLOW,
                            reason="",
                            client_data={},
                            metadata={"terminal": False},
                        ),
                    ))
                    stack.enter_context(patch.object(
                        pending_responses,
                        "_pending_response_column_contract_error",
                        return_value=None,
                    ))
                    stack.enter_context(patch.object(
                        processing,
                        "send_reply_in_thread",
                        side_effect=accepted_send,
                    ))
                    completion_patch = (
                        patch.object(
                            processing,
                            "_maybe_mark_client_completed",
                            side_effect=effect,
                        )
                        if isinstance(effect, BaseException)
                        else patch.object(
                            processing,
                            "_maybe_mark_client_completed",
                            return_value=effect,
                        )
                    )
                    stack.enter_context(completion_patch)
                    if raised is None:
                        states = pending_responses.process_pending_responses(
                            self.USER_ID,
                            {"Authorization": "Bearer local-test"},
                        )
                        self._last_state(states, "error")
                    else:
                        with self.assertRaises(raised):
                            pending_responses.process_pending_responses(
                                self.USER_ID,
                                {"Authorization": "Bearer local-test"},
                            )

                capability = captured["capability"]
                self.assertTrue(pending_doc.reference.deleted)
                self.assertEqual(
                    "settled_sent",
                    send_permits.read_permit(capability)["status"],
                )
                obligations = self._all_obligations(fake_fs)
                self.assertEqual(1, len(obligations))
                self._assert_valid_owed_obligation(
                    obligations[0],
                    data,
                    capability,
                    captured["evidence"],
                )

    def test_pending_absent_replay_retries_only_local_completion_until_settled(self):
        fake_fs, pending_doc, _data, capability, _evidence, obligation = (
            self._seed_manual_obligation("replay")
        )
        permit_before = copy.deepcopy(send_permits.read_permit(capability))

        first, send, exact, heuristic, continuation = self._process(
            fake_fs,
            completion_effect=False,
        )
        self._last_state(first, "error")
        self.assertEqual("owed", obligation.to_dict()["status"])
        send.assert_not_called()
        exact.assert_not_called()
        heuristic.assert_not_called()
        continuation.assert_not_called()
        self.assertTrue(pending_doc.reference.deleted)
        self.assertEqual(permit_before, send_permits.read_permit(capability))

        second, send, exact, heuristic, continuation = self._process(
            fake_fs,
            completion_effect=True,
        )
        self._last_state(second, "healthy")
        self.assertEqual("settled", obligation.to_dict()["status"])
        self.assertEqual("client_completed", obligation.to_dict()["completionOutcome"])
        send.assert_not_called()
        exact.assert_not_called()
        heuristic.assert_not_called()
        continuation.assert_not_called()
        self.assertEqual(permit_before, send_permits.read_permit(capability))

    def test_legacy_v1_obligation_replays_after_enforced_rollout(self):
        fake_fs, pending_doc, _data, capability, _evidence, obligation = (
            self._seed_manual_obligation("legacy-rollout")
        )
        self.assertEqual(1, obligation.to_dict()["version"])
        self.assertNotIn(
            "sourceAuthorityProtocol",
            obligation.to_dict()["immutable"],
        )
        permit_before = copy.deepcopy(send_permits.read_permit(capability))

        with patch.dict(
            "os.environ",
            {"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"},
        ):
            states, send, exact, heuristic, continuation = self._process(
                fake_fs,
                completion_effect=True,
            )

        self._last_state(states, "healthy")
        self.assertEqual("settled", obligation.to_dict()["status"])
        self.assertEqual(
            "client_completed",
            obligation.to_dict()["completionOutcome"],
        )
        self.assertTrue(pending_doc.reference.deleted)
        self.assertEqual(permit_before, send_permits.read_permit(capability))
        send.assert_not_called()
        exact.assert_not_called()
        heuristic.assert_not_called()
        continuation.assert_not_called()

    def test_completed_and_ineligible_clients_settle_without_duplicate_completion(self):
        for status in ("completed", "stopping", "stopped", "archived", "deleted"):
            with self.subTest(client_status=status):
                fake_fs, _pending, _data, _cap, _evidence, obligation = (
                    self._seed_manual_obligation(
                        f"control-{status}",
                        client_status=status,
                    )
                )
                states, send, exact, heuristic, continuation = self._process(
                    fake_fs,
                    completion_effect=AssertionError(
                        "completed/ineligible client must not be rewritten"
                    ),
                )
                self._last_state(states, "healthy")
                self.assertEqual("settled", obligation.to_dict()["status"])
                self.assertIn(
                    obligation.to_dict()["completionOutcome"],
                    {"client_completed", "client_ineligible"},
                )
                send.assert_not_called()
                exact.assert_not_called()
                heuristic.assert_not_called()
                continuation.assert_not_called()

    def test_false_completion_obligation_settles_without_local_completion(self):
        fake_fs, _pending, data, _cap, _evidence, obligation = (
            self._seed_manual_obligation(
                "not-required",
                client_id="",
                complete=False,
            )
        )
        thread_snapshot = fake_fs.collections["threads"].document(
            data["threadId"]
        ).get()
        thread_snapshot._data["clientId"] = self.CLIENT_ID
        states, send, exact, heuristic, continuation = self._process(
            fake_fs,
            completion_effect=AssertionError(
                "false completion obligation must not call completion"
            ),
        )
        self._last_state(states, "healthy")
        self.assertEqual("settled", obligation.to_dict()["status"])
        self.assertEqual("not_required", obligation.to_dict()["completionOutcome"])
        send.assert_not_called()
        exact.assert_not_called()
        heuristic.assert_not_called()
        continuation.assert_not_called()

    def test_client_bound_replay_rejects_missing_thread_client_binding(self):
        fake_fs, _pending, data, _cap, _evidence, obligation = (
            self._seed_manual_obligation("missing-thread-client")
        )
        thread_snapshot = fake_fs.collections["threads"].document(
            data["threadId"]
        ).get()
        thread_snapshot._data.pop("clientId")
        completion = MagicMock(return_value=True)

        with self._clients_patch(fake_fs), patch.object(
            processing,
            "_fs",
            fake_fs,
        ), patch.object(
            processing,
            "_maybe_mark_client_completed",
            new=completion,
        ):
            state = pending_responses._replay_pending_completion_obligation(
                self.USER_ID,
                obligation,
                processing,
            )

        self.assertEqual("error", state["status"])
        self.assertEqual("owed", obligation.to_dict()["status"])
        completion.assert_not_called()

    def test_rehashed_user_client_thread_permit_and_evidence_drift_fails_closed(self):
        cases = {
            "user": {"userId": "uid-other"},
            "client": {"clientId": "client-other"},
            "thread": {"threadId": "thread-other"},
            "permit": {
                "permitId": "graph-send-other",
                "permitImmutableHash": "b" * 64,
            },
            "evidence": {"sentEvidenceHash": "c" * 64},
        }
        for suffix, overrides in cases.items():
            with self.subTest(drift=suffix):
                fake_fs, _pending, _data, _cap, _evidence, obligation = (
                    self._seed_manual_obligation(
                        f"drift-{suffix}",
                        overrides=overrides,
                    )
                )
                states, send, exact, heuristic, continuation = self._process(
                    fake_fs,
                    completion_effect=AssertionError(
                        "drifted obligation must fail before completion"
                    ),
                )
                self._last_state(states, "error")
                self.assertEqual("owed", obligation.to_dict()["status"])
                send.assert_not_called()
                exact.assert_not_called()
                heuristic.assert_not_called()
                continuation.assert_not_called()


class TerminalInitialCompletionFailureTests(unittest.TestCase):
    def test_initial_post_cleanup_false_is_retryable_then_exact_tombstone_replays_locally(self):
        fixture = terminal_completion_fixture.TerminalCompletionReplayTests(
            methodName="runTest"
        )
        settlement = fixture._settlement(complete_client_after_reply=True)
        fake_fs, thread_doc, client_doc = fixture._firestore_fixture(
            settlement,
            client_status="live",
        )
        thread_doc._data["terminalSettlements"] = []
        runtime_saga = {
            "version": processing.TERMINAL_SAGA_VERSION,
            "sagaKey": "terminal-saga-initial-completion",
            "sourceMessageKey": fixture.SOURCE_INTERNET_ID,
            "sourceGraphMessageId": fixture.SOURCE_GRAPH_ID,
            "sourceInternetMessageId": fixture.SOURCE_INTERNET_ID,
            "clientId": fixture.CLIENT_ID,
            "replyRecipient": "broker@example.test",
            "completeClientAfterReply": True,
            "phase": "finalized",
            "sourceRow": 3,
            "finalizationPlan": {
                "finalRow": 10,
                "claimThreadId": fixture.THREAD_ID,
                "terminalThreadIds": [fixture.THREAD_ID],
            },
            "sheetId": "sheet-local",
            "tabTitle": "Sheet1",
            "notesColumnIndex": 0,
            "rowAnchor": "951 E FM 646",
        }
        thread_doc._data.update({
            "terminalSaga": copy.deepcopy(runtime_saga),
            "terminalSagaKey": runtime_saga["sagaKey"],
        })

        def settle_and_clear(*args, **kwargs):
            thread_doc._data.update({
                "terminalSaga": None,
                "terminalSagaKey": None,
                "terminalSettlements": [copy.deepcopy(settlement)],
                "terminalReplyOwed": False,
                "terminalNotificationOwed": False,
            })
            return "sent_indexed"

        with ExitStack() as stack:
            stack.enter_context(patch.object(processing, "_fs", fake_fs))
            stack.enter_context(patch.object(
                processing,
                "_validate_terminal_saga_immutable_hash",
            ))
            stack.enter_context(patch.object(
                processing,
                "_terminal_sheet_mutation_geometry_from_saga",
                return_value=(3, 10, "move_with_note"),
            ))
            stack.enter_context(patch.object(
                processing,
                "_validate_terminal_saga_sheet_layout_binding",
            ))
            stack.enter_context(patch.object(
                processing,
                "_claim_existing_terminal_saga_execution",
                return_value=types.SimpleNamespace(owner="owner", fencing_token=1),
            ))
            stack.enter_context(patch.object(processing, "_sheets_client"))
            stack.enter_context(patch.object(
                processing,
                "_get_first_tab_title",
                return_value="Sheet1",
            ))
            stack.enter_context(patch.object(
                processing,
                "_read_header_row2",
                return_value=["Notes"],
            ))
            stack.enter_context(patch.object(
                processing,
                "find_notes_comment_column_index",
                return_value=0,
            ))
            stack.enter_context(patch.object(
                processing,
                "_validate_terminal_saga_sheet_layout",
                return_value=0,
            ))
            stack.enter_context(patch.object(
                processing,
                "_find_row_by_anchor",
                return_value=(10, ["951 E FM 646"]),
            ))
            stack.enter_context(patch.object(
                processing,
                "get_row_anchor",
                return_value="951 E FM 646",
            ))
            stack.enter_context(patch.object(
                processing,
                "_execute_or_reconcile_terminal_sheet_mutation",
                return_value=10,
            ))
            stack.enter_context(patch.object(
                processing,
                "_settle_terminal_notification_obligation",
            ))
            stack.enter_context(patch.object(
                processing,
                "_settle_terminal_reply_obligation",
                side_effect=settle_and_clear,
            ))
            stack.enter_context(patch.object(
                processing,
                "_release_terminal_saga_execution_claim",
            ))
            stack.enter_context(patch.object(
                processing,
                "_maybe_mark_client_completed",
                return_value=False,
            ))

            with self.assertRaises(processing.RetryableProcessingError):
                processing._resume_exact_terminal_saga(
                    fixture.USER_ID,
                    {"Authorization": "Bearer local-test"},
                    fixture.THREAD_ID,
                    copy.deepcopy(thread_doc._data),
                    runtime_saga,
                )

        self.assertEqual([settlement], thread_doc._data["terminalSettlements"])
        self.assertIsNone(thread_doc._data["terminalSaga"])
        effects = fixture._effect_mocks()
        fixture._process_exact_source(fake_fs, effects)
        self.assertEqual("completed", client_doc._data["status"])
        fixture._assert_no_terminal_effect_replay(effects)


if __name__ == "__main__":
    unittest.main()
