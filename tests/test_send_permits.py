import ast
import copy
import hashlib
import json
import os
import types
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from unittest.mock import MagicMock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")

with patch("google.cloud.firestore.Client", return_value=MagicMock()):
    from email_automation import email as email_module
    from email_automation import pending_responses, processing, send_permits
    from email_automation.campaign_safety import CampaignAutomationDecision


PROVIDER_DRAFT_SUBJECT = "AW: Provider Subject AbC-42"


class _Snapshot:
    def __init__(self, ref):
        self.exists = ref.exists
        self._data = dict(ref.data)
        self.id = ref.id
        self.reference = ref

    def to_dict(self):
        return dict(self._data)


class _DocRef:
    def __init__(self, data=None, *, exists=True, doc_id=None, path=None):
        self.data = dict(data or {})
        self.exists = exists
        self.id = doc_id
        self.path = path
        self.version = 0
        self.collections = {}

    def get(self, transaction=None):
        return transaction.get(self) if transaction else _Snapshot(self)

    def collection(self, name):
        return self.collections.setdefault(
            name,
            _Collection(
                base_path=(f"{self.path}/{name}" if self.path else None),
            ),
        )

    def __deepcopy__(self, memo):
        # Firestore DocumentReference values are immutable path identities.
        memo[id(self)] = self
        return self


class _AliasDocRef:
    def __init__(self, target, path):
        self._target = target
        self.path = path
        self.id = target.id

    @property
    def data(self):
        return self._target.data

    @data.setter
    def data(self, value):
        self._target.data = value

    @property
    def exists(self):
        return self._target.exists

    @exists.setter
    def exists(self, value):
        self._target.exists = value

    @property
    def version(self):
        return self._target.version

    @version.setter
    def version(self, value):
        self._target.version = value

    def get(self, transaction=None):
        return transaction.get(self) if transaction else _Snapshot(self)

    def collection(self, name):
        return self._target.collection(name)


class _Collection:
    def __init__(self, *, base_path=None):
        self.docs = {}
        self.base_path = base_path

    def document(self, doc_id):
        return self.docs.setdefault(
            doc_id,
            _DocRef(
                {},
                exists=False,
                doc_id=doc_id,
                path=(
                    f"{self.base_path}/{doc_id}"
                    if self.base_path
                    else None
                ),
            ),
        )

    def stream(self):
        return [
            _Snapshot(ref)
            for ref in self.docs.values()
            if ref.exists
        ]

    def where(self, *, filter):
        return _CollectionQuery(
            list(self.docs.values()),
            filters=(filter,),
        )

    def limit(self, count):
        return _CollectionQuery(
            list(self.docs.values()),
            query_limit=count,
        )


class _CollectionQuery:
    def __init__(self, refs, *, filters=(), query_limit=None):
        self.refs = list(refs)
        self.filters = tuple(filters)
        self.query_limit = query_limit

    def where(self, *, filter):
        return _CollectionQuery(
            self.refs,
            filters=(*self.filters, filter),
            query_limit=self.query_limit,
        )

    def limit(self, count):
        return _CollectionQuery(
            self.refs,
            filters=self.filters,
            query_limit=count,
        )

    def stream(self):
        refs = [ref for ref in self.refs if ref.exists]
        for field_filter in self.filters:
            refs = [
                ref
                for ref in refs
                if ref.data.get(field_filter.field_path)
                == field_filter.value
            ]
        if self.query_limit is not None:
            refs = refs[:self.query_limit]
        return [_Snapshot(ref) for ref in refs]


class _Transaction:
    def __init__(self, firestore):
        self.firestore = firestore
        self.reads = {}
        self.writes = []

    def get(self, ref):
        with self.firestore.lock:
            self.reads[ref] = ref.version
            return _Snapshot(ref)

    def update(self, ref, data):
        self.writes.append(("update", ref, dict(data)))

    def set(self, ref, data):
        self.writes.append(("set", ref, dict(data)))

    def delete(self, ref):
        self.writes.append(("delete", ref, None))

    def commit(self):
        with self.firestore.lock:
            for ref, version in self.reads.items():
                if ref.version != version:
                    raise RuntimeError("transaction conflict")
            for operation, ref, data in self.writes:
                if operation == "set":
                    ref.data = dict(data)
                    ref.exists = True
                elif operation == "update":
                    if not ref.exists:
                        raise RuntimeError("cannot update missing document")
                    ref.data.update(data)
                else:
                    ref.data = {}
                    ref.exists = False
                ref.version += 1


class _Firestore:
    def __init__(self):
        self.lock = RLock()

    def transaction(self):
        return _Transaction(self)


class _CommitOutcomeTransaction(_Transaction):
    def commit(self):
        self.firestore.commit_attempts += 1
        commit_payloads = getattr(self.firestore, "commit_payloads", None)
        if isinstance(commit_payloads, list):
            deepcopy_memo = {
                id(send_permits.SERVER_TIMESTAMP): (
                    send_permits.SERVER_TIMESTAMP
                ),
            }
            commit_payloads.append([
                (
                    operation,
                    getattr(ref, "path", None),
                    getattr(ref, "id", None),
                    copy.deepcopy(data, deepcopy_memo),
                )
                for operation, ref, data in self.writes
            ])
        outcome = (
            self.firestore.commit_outcomes.pop(0)
            if self.firestore.commit_outcomes
            else None
        )
        if outcome == "no_apply":
            raise RuntimeError("terminal permit commit did not apply")
        super().commit()
        if outcome == "apply_then_raise":
            if self.firestore.after_apply is not None:
                self.firestore.after_apply()
            raise RuntimeError("terminal permit commit applied then raised")


class _CommitOutcomeFirestore(_Firestore):
    def __init__(self, *commit_outcomes):
        super().__init__()
        self.commit_outcomes = list(commit_outcomes)
        self.commit_attempts = 0
        self.commit_payloads = []
        self.after_apply = None

    def transaction(self):
        return _CommitOutcomeTransaction(self)


class _ReadBeforeWriteTransaction(_CommitOutcomeTransaction):
    def get(self, ref):
        if self.writes:
            raise RuntimeError("READ_AFTER_WRITE_ERROR")
        return super().get(ref)


class _ReadBeforeWriteFirestore(_CommitOutcomeFirestore):
    def transaction(self):
        return _ReadBeforeWriteTransaction(self)


class _UserRoot:
    def __init__(self, *, user_data=None):
        self.user_document = _DocRef(user_data or {"email": "sender@example.test"})
        self.user_id = None
        self.collections = {
            "threads": _Collection(),
            "pendingResponses": _Collection(),
            "terminalGraphSendReviews": _Collection(),
        }

    def get(self):
        return self.user_document.get()

    def collection(self, name):
        collection = self.collections.setdefault(name, _Collection())
        if self.user_id:
            collection.base_path = f"users/{self.user_id}/{name}"
        return collection


class _UsersCollection:
    def __init__(self, user_root):
        self.user_root = user_root

    def document(self, _user_id):
        self.user_root.user_id = _user_id
        return self.user_root


class _RootedFirestore(_Firestore):
    def __init__(self, *, user_data=None):
        super().__init__()
        self.user_root = _UserRoot(user_data=user_data)

    def collection(self, name):
        if name != "users":
            raise KeyError(name)
        return _UsersCollection(self.user_root)

    def add_thread(self, thread_ref):
        self.user_root.collection("threads").docs[thread_ref.id] = thread_ref

    def add_pending(self, pending_ref):
        self.user_root.collection("pendingResponses").docs[
            pending_ref.id
        ] = pending_ref


class _CommitOutcomeRootedFirestore(_RootedFirestore):
    def __init__(self, *commit_outcomes, user_data=None):
        super().__init__(user_data=user_data)
        self.commit_outcomes = list(commit_outcomes)
        self.commit_attempts = 0
        self.commit_payloads = []
        self.after_apply = None

    def transaction(self):
        return _CommitOutcomeTransaction(self)


class _ReadBeforeWriteRootedFirestore(_CommitOutcomeRootedFirestore):
    def transaction(self):
        return _ReadBeforeWriteTransaction(self)


class _GraphResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = dict(payload or {})

    def json(self):
        return dict(self._payload)


def _pending_data(thread_id, *, token, body="Thank you for the update."):
    now = datetime.now(timezone.utc)
    return {
        "threadId": thread_id,
        "msgId": f"msg-{thread_id}",
        "recipient": "broker@example.test",
        "responseBody": body,
        "clientId": "client-1",
        "conversationId": f"conv-{thread_id}",
        "processingBy": token,
        "processingLeaseUntil": now + timedelta(minutes=5),
        "status": "sending",
    }


def _terminal_saga(thread_id):
    body = "Thank you. We will close this property review."
    return {
        "sagaKey": f"saga-{thread_id}",
        "immutableHash": f"saga-hash-{thread_id}",
        "sourceMessageKey": f"source-{thread_id}",
        "clientId": "client-1",
        "sourceGraphMessageId": f"msg-{thread_id}",
        "sourceConversationId": f"conv-{thread_id}",
        "replyRecipient": "broker@example.test",
        "responseBody": body,
        "finalizationPlan": {"claimThreadId": thread_id},
    }


def _exact_unissued_terminal_attempt(saga):
    return {
        "sagaKey": saga["sagaKey"],
        "sourceMessageKey": saga["sourceMessageKey"],
        "sourceGraphMessageId": saga["sourceGraphMessageId"],
        "conversationId": saga["sourceConversationId"],
        "recipient": str(saga["replyRecipient"]).strip().lower(),
        "responseBodyHash": hashlib.sha256(
            saga["responseBody"].encode("utf-8")
        ).hexdigest(),
        "status": "sending",
        "startedAt": datetime.now(timezone.utc),
    }


def _invalid_unissued_terminal_attempts(saga):
    exact = _exact_unissued_terminal_attempt(saga)
    cases = {}
    for field_name in exact:
        missing = copy.deepcopy(exact)
        missing.pop(field_name)
        cases[f"missing_{field_name}"] = missing
    drift_values = {
        "sagaKey": "other-saga",
        "sourceMessageKey": "other-source",
        "sourceGraphMessageId": "other-graph-message",
        "conversationId": "other-conversation",
        "recipient": "other@example.test",
        "responseBodyHash": "f" * 64,
        "status": "needs_reconciliation",
    }
    for field_name, value in drift_values.items():
        drifted = copy.deepcopy(exact)
        drifted[field_name] = value
        cases[f"drifted_{field_name}"] = drifted
    for label, field_name, value in (
        ("malformed_sourceMessageKey", "sourceMessageKey", None),
        ("malformed_sourceGraphMessageId", "sourceGraphMessageId", []),
        ("malformed_conversationId", "conversationId", {}),
        ("unnormalized_recipient", "recipient", " Broker@Example.Test "),
        ("naive_startedAt", "startedAt", datetime.now()),
        ("string_startedAt", "startedAt", "2026-08-02T12:00:00Z"),
    ):
        malformed = copy.deepcopy(exact)
        malformed[field_name] = value
        cases[label] = malformed
    extra = copy.deepcopy(exact)
    extra["unexpectedField"] = "unexpected"
    cases["extra_field"] = extra
    linked_none = copy.deepcopy(exact)
    linked_none["graphSendPermitId"] = None
    linked_none["graphSendPermitHash"] = None
    cases["permit_fields_present_none"] = linked_none
    return cases


def _terminal_refs(thread_id, *, owner="terminal-owner-a", fence=1):
    saga = _terminal_saga(thread_id)
    now = datetime.now(timezone.utc)
    attempt = _exact_unissued_terminal_attempt(saga)
    thread_ref = _DocRef({
        "terminalSaga": dict(saga),
        "terminalReplyOwed": True,
        "terminalReplyAttempt": attempt,
        "terminalSagaClaim": {
            "sagaKey": saga["sagaKey"],
            "immutableHash": saga["immutableHash"],
            "owner": owner,
            "fencingToken": fence,
            "leaseUntil": now + timedelta(minutes=5),
        },
    }, doc_id=thread_id)
    return saga, thread_ref, thread_ref


def _prepare_capability_for_send(capability, source_id, recipient):
    draft_id = f"draft-{capability.permit_id}"
    html_body = "<p>Prepared reply body.</p>"
    to_recipients = [recipient]
    cc_recipients = []
    attachments = []
    send_permits.begin_graph_draft_creation(capability, source_id)
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
        source_graph_message_id=source_id,
        draft_id=draft_id,
        subject=PROVIDER_DRAFT_SUBJECT,
        html_body=html_body,
        to_recipients=to_recipients,
        cc_recipients=cc_recipients,
        attachments=attachments,
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
    return {
        "source_graph_message_id": source_id,
        "draft_id": draft_id,
        "subject": PROVIDER_DRAFT_SUBJECT,
        "html_body": html_body,
        "to_recipients": to_recipients,
        "cc_recipients": cc_recipients,
        "attachments": attachments,
    }


def _actual_sent_envelope(
    permit,
    *,
    html_body="<p>Prepared reply body.</p>",
    attachments=None,
):
    prepared_envelope = permit["preparedEnvelope"]
    return {
        "isDraft": False,
        "subject": prepared_envelope["subject"],
        "toRecipients": [
            {"emailAddress": {"address": address}}
            for address in prepared_envelope["toRecipients"]
        ],
        "ccRecipients": [
            {"emailAddress": {"address": address}}
            for address in prepared_envelope["ccRecipients"]
        ],
        "bccRecipients": [],
        "body": {"contentType": "HTML", "content": html_body},
        "attachments": list(attachments or []),
    }


def _exact_sent_evidence(
    capability,
    *,
    html_body="<p>Prepared reply body.</p>",
    attachments=None,
):
    permit = send_permits.read_permit(capability)
    prepared_envelope = permit["preparedEnvelope"]
    return {
        "sentMessageId": prepared_envelope["draftId"],
        "recipient": permit["recipient"],
        "bodyHash": permit["bodyHash"],
        "conversationId": permit.get("conversationId"),
        "sentDateTime": permit["requestStartedAt"] + timedelta(seconds=1),
        "permitId": permit["permitId"],
        "sourceGraphMessageId": permit["sourceGraphMessageId"],
        "preparedEnvelopeHash": prepared_envelope["preparedEnvelopeHash"],
        **_actual_sent_envelope(
            permit,
            html_body=html_body,
            attachments=attachments,
        ),
    }


def _pending_completion_document(
    thread_ref,
    pending_ref,
    loaded,
    capability,
    sent_evidence,
):
    thread_path = str(getattr(thread_ref, "path", None) or "").strip("/")
    path_parts = thread_path.split("/") if thread_path else []
    user_id = (
        path_parts[-3]
        if len(path_parts) >= 4 and path_parts[-4] == "users"
        else "u"
    )
    permit = send_permits.read_permit(capability)
    obligation_id, payload = (
        send_permits.pending_completion_obligation_payload(
            user_id=user_id,
            client_id=str((loaded or {}).get("clientId") or ""),
            thread_id=str((loaded or {}).get("threadId") or ""),
            pending_document_id=str(getattr(pending_ref, "id", None) or ""),
            source_graph_message_id=str((loaded or {}).get("msgId") or ""),
            pending_envelope_hash_value=(
                send_permits.pending_envelope_hash(loaded)
            ),
            permit_id=capability.permit_id,
            permit_immutable_hash=permit["immutableHash"],
            sent_evidence=sent_evidence,
            complete_client_after_reply=bool(
                str((loaded or {}).get("clientId") or "")
            ),
        )
    )
    completion_path = (
        f"users/{user_id}/pendingResponseCompletionObligations/"
        f"{obligation_id}"
        if thread_path
        else None
    )
    return _DocRef(
        {},
        exists=False,
        doc_id=obligation_id,
        path=completion_path,
    ), payload


class SendPermitTests(unittest.TestCase):
    def setUp(self):
        self.firestore = _Firestore()

    def _graph_202_unconfirmed_stack(
        self,
        rooted_firestore,
        *,
        source_id,
        conversation_id,
        recipient,
        draft_id,
        exact_get="404",
    ):
        stack = ExitStack()
        send_posts = []
        html_body = "<p>Durable 202 reconciliation body.</p>"
        decision = CampaignAutomationDecision(
            state="allow",
            reason="",
            client_data={"status": "live"},
            metadata={"terminal": False, "stopKind": "none"},
        )

        def fake_get(url, **_kwargs):
            if url.endswith(f"/me/messages/{source_id}"):
                return _GraphResponse(200, {
                    "conversationId": conversation_id,
                    "subject": "RE: Durable provider ambiguity",
                })
            if url.endswith(f"/me/messages/{draft_id}"):
                if exact_get == "draft":
                    return _GraphResponse(200, {
                        "id": draft_id,
                        "isDraft": True,
                    })
                return _GraphResponse(404)
            return _GraphResponse(404)

        def fake_post(url, **_kwargs):
            if url.endswith("/createReplyAll"):
                return _GraphResponse(201, {
                    "id": draft_id,
                    "subject": PROVIDER_DRAFT_SUBJECT,
                    "toRecipients": [
                        {"emailAddress": {"address": recipient}},
                    ],
                    "ccRecipients": [],
                })
            if url.endswith(f"/{draft_id}/send"):
                send_posts.append(url)
                return _GraphResponse(202)
            return _GraphResponse(500)

        stack.enter_context(
            patch("email_automation.clients._fs", rooted_firestore)
        )
        stack.enter_context(
            patch.object(
                processing,
                "get_client_automation_decision",
                return_value=decision,
            )
        )
        stack.enter_context(
            patch.object(processing, "resolve_outbound_mode", return_value="live")
        )
        stack.enter_context(
            patch.object(
                processing,
                "_automatic_inbox_replies_allowed",
                return_value=True,
            )
        )
        stack.enter_context(
            patch.object(
                processing,
                "format_email_body_with_footer",
                return_value=html_body,
            )
        )
        stack.enter_context(
            patch(
                "email_automation.utils.exponential_backoff_request",
                side_effect=lambda callback, *args, **kwargs: callback(),
            )
        )
        stack.enter_context(
            patch.object(processing.requests, "get", side_effect=fake_get)
        )
        stack.enter_context(
            patch.object(processing.requests, "post", side_effect=fake_post)
        )
        stack.enter_context(
            patch.object(
                processing.requests,
                "patch",
                return_value=_GraphResponse(204),
            )
        )
        stack.enter_context(
            patch(
                "email_automation.email._hydrate_reply_all_draft_recipients",
                side_effect=lambda _headers, draft, base=None: draft,
            )
        )
        stack.enter_context(
            patch(
                "email_automation.email._source_message_reply_all_fallback",
                side_effect=lambda draft, _source: draft,
            )
        )
        stack.enter_context(
            patch(
                "email_automation.email._reviewed_recipient_reply_all_fallback",
                side_effect=lambda draft, to_emails=None: draft,
            )
        )
        stack.enter_context(
            patch(
                "email_automation.email._filter_reply_all_draft_recipients",
                return_value={
                    "payload": {
                        "toRecipients": [
                            {"emailAddress": {"address": recipient}},
                        ],
                        "ccRecipients": [],
                    },
                    "skipped": {},
                },
            )
        )
        stack.enter_context(patch.object(processing.time, "sleep", return_value=None))
        return stack, send_posts

    def _local_transition_processing_stack(
        self,
        rooted_firestore,
        *,
        source_id: str,
        conversation_id: str,
        recipient: str,
        draft_id: str,
        attachment: dict,
    ):
        stack = ExitStack()
        calls = []
        decision = CampaignAutomationDecision(
            state="allow",
            reason="",
            client_data={"status": "live"},
            metadata={"terminal": False, "stopKind": "none"},
        )

        def fake_get(url, **_kwargs):
            calls.append(("get", url))
            if url.endswith(f"/me/messages/{source_id}"):
                return _GraphResponse(200, {
                    "conversationId": conversation_id,
                    "subject": "RE: Local transition recovery",
                })
            return _GraphResponse(404)

        def fake_post(url, **_kwargs):
            calls.append(("post", url))
            if url.endswith("/createReplyAll"):
                return _GraphResponse(201, {
                    "id": draft_id,
                    "subject": PROVIDER_DRAFT_SUBJECT,
                    "toRecipients": [{
                        "emailAddress": {"address": recipient},
                    }],
                    "ccRecipients": [],
                })
            if url.endswith(f"/{draft_id}/attachments"):
                return _GraphResponse(201, {"id": "provider-attachment-1"})
            if url.endswith(f"/{draft_id}/send"):
                return _GraphResponse(202)
            return _GraphResponse(500)

        stack.enter_context(
            patch("email_automation.clients._fs", rooted_firestore)
        )
        stack.enter_context(patch.object(
            processing,
            "get_client_automation_decision",
            return_value=decision,
        ))
        stack.enter_context(
            patch.object(processing, "resolve_outbound_mode", return_value="live")
        )
        stack.enter_context(patch.object(
            processing,
            "_automatic_inbox_replies_allowed",
            return_value=True,
        ))
        stack.enter_context(patch.object(
            processing,
            "format_email_body_with_footer",
            return_value="<p>Local transition recovery.</p>",
        ))
        stack.enter_context(patch(
            "email_automation.utils.resolve_signature_settings",
            return_value=("signature", "html", "sender@example.test"),
        ))
        stack.enter_context(patch(
            "email_automation.utils.needs_signature_attachments",
            return_value=True,
        ))
        stack.enter_context(patch(
            "email_automation.utils.get_signature_attachments",
            return_value=[attachment],
        ))
        stack.enter_context(patch(
            "email_automation.utils.exponential_backoff_request",
            side_effect=lambda callback, *args, **kwargs: callback(),
        ))
        stack.enter_context(
            patch.object(processing.requests, "get", side_effect=fake_get)
        )
        stack.enter_context(
            patch.object(processing.requests, "post", side_effect=fake_post)
        )
        stack.enter_context(patch.object(
            processing.requests,
            "patch",
            side_effect=lambda url, **_kwargs: (
                calls.append(("patch", url)) or _GraphResponse(204)
            ),
        ))
        stack.enter_context(patch(
            "email_automation.email._hydrate_reply_all_draft_recipients",
            side_effect=lambda _headers, draft, base=None: draft,
        ))
        stack.enter_context(patch(
            "email_automation.email._source_message_reply_all_fallback",
            side_effect=lambda draft, _source: draft,
        ))
        stack.enter_context(patch(
            "email_automation.email._reviewed_recipient_reply_all_fallback",
            side_effect=lambda draft, to_emails=None: draft,
        ))
        stack.enter_context(patch(
            "email_automation.email._filter_reply_all_draft_recipients",
            return_value={
                "payload": {
                    "toRecipients": [{
                        "emailAddress": {"address": recipient},
                    }],
                    "ccRecipients": [],
                },
                "skipped": {},
            },
        ))
        return stack, calls

    def _issue_pending(self, thread_ref, pending_ref, loaded, token):
        return send_permits.issue_pending_graph_send_permit(
            self.firestore,
            thread_ref,
            pending_ref,
            loaded,
            token,
        )

    def _prepare_pending_draft(
        self,
        *,
        thread_id="thread-prepared",
        token="pending-worker-a",
        attachments=None,
        canonical_user_id=None,
    ):
        thread_ref = _DocRef(
            {},
            doc_id=thread_id,
            path=(
                f"users/{canonical_user_id}/threads/{thread_id}"
                if canonical_user_id
                else None
            ),
        )
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(
            loaded,
            doc_id="pending-a",
            path=(
                f"users/{canonical_user_id}/pendingResponses/pending-a"
                if canonical_user_id
                else None
            ),
        )
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )
        source_id = loaded["msgId"]
        draft_id = f"draft-{thread_id}"
        html_body = "<p>Thank you for the update.</p>"
        to_recipients = ["broker@example.test"]
        cc_recipients = ["asset-manager@example.test"]
        attachments = list(attachments or [])
        send_permits.begin_graph_draft_creation(
            capability,
            source_id,
            planned_attachment_count=len(attachments),
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
            source_graph_message_id=source_id,
            draft_id=draft_id,
            subject=PROVIDER_DRAFT_SUBJECT,
            html_body=html_body,
            to_recipients=to_recipients,
            cc_recipients=cc_recipients,
            attachments=attachments,
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
        for attachment_index, attachment in enumerate(attachments):
            send_permits.begin_graph_draft_attachment(
                capability,
                prepared_envelope_hash=prepared["preparedEnvelopeHash"],
                attachment_index=attachment_index,
                attachment=attachment,
            )
            send_permits.complete_graph_draft_attachment(
                capability,
                prepared_envelope_hash=prepared["preparedEnvelopeHash"],
                attachment_index=attachment_index,
                outcome="applied",
                evidence={
                    "httpStatus": 201,
                    "phase": "attach_draft",
                    "draftId": draft_id,
                    "attachmentIndex": attachment_index,
                    "attachmentHash": send_permits._attachment_projection(
                        attachment,
                        attachment_index,
                    )["attachmentHash"],
                    "providerAttachmentId": f"provider-{attachment_index}",
                },
            )
        send_permits.finalize_graph_draft_preparation(
            capability,
            prepared_envelope_hash=prepared["preparedEnvelopeHash"],
        )
        return {
            "thread_ref": thread_ref,
            "pending_ref": pending_ref,
            "loaded": loaded,
            "capability": capability,
            "source_id": source_id,
            "draft_id": draft_id,
            "subject": PROVIDER_DRAFT_SUBJECT,
            "html_body": html_body,
            "to_recipients": to_recipients,
            "cc_recipients": cc_recipients,
            "attachments": attachments,
            "prepared": prepared,
        }

    def _local_transition_scenario(
        self,
        *,
        firestore,
        issuer: str,
        operation: str,
    ):
        label = f"{issuer}-{operation}"
        attachment = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": f"{label}.png",
            "contentType": "image/png",
            "contentBytes": "bG9zdC1jb21taXQtYWNr",
            "contentId": f"{label}-content",
            "isInline": True,
        }
        if issuer == "pending":
            token = f"pending-{label}"
            thread_ref = _DocRef({}, doc_id=f"thread-{label}")
            loaded = _pending_data(thread_ref.id, token=token)
            pending_ref = _DocRef(loaded, doc_id=f"pending-{label}")
            capability = send_permits.issue_pending_graph_send_permit(
                firestore,
                thread_ref,
                pending_ref,
                dict(loaded),
                token,
            )
            source_id = loaded["msgId"]
            recipient = loaded["recipient"]
        else:
            saga, thread_ref, claim_ref = _terminal_refs(
                f"thread-{label}"
            )
            capability = send_permits.issue_terminal_graph_send_permit(
                firestore,
                thread_ref,
                claim_ref,
                saga,
                "terminal-owner-a",
                1,
            )
            source_id = saga["sourceGraphMessageId"]
            recipient = saga["replyRecipient"]

        draft_id = f"draft-{label}"
        html_body = f"<p>{label} exact local transition.</p>"
        envelope_args = {
            "source_graph_message_id": source_id,
            "draft_id": draft_id,
            "subject": PROVIDER_DRAFT_SUBJECT,
            "html_body": html_body,
            "to_recipients": [recipient],
            "cc_recipients": [],
            "attachments": [attachment],
        }
        if operation == "begin_create":
            invoke = lambda: send_permits.begin_graph_draft_creation(
                capability,
                source_id,
                planned_attachment_count=1,
            )
            expected_status = "issued"
            expected_state = "create_request_started"
        else:
            send_permits.begin_graph_draft_creation(
                capability,
                source_id,
                planned_attachment_count=1,
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
            if operation == "begin_patch":
                invoke = lambda: send_permits.begin_graph_draft_patch(
                    capability,
                    **envelope_args,
                )
                expected_status = "issued"
                expected_state = "patch_request_started"
            else:
                prepared = send_permits.begin_graph_draft_patch(
                    capability,
                    **envelope_args,
                )
                send_permits.complete_graph_draft_patch(
                    capability,
                    prepared_envelope_hash=prepared[
                        "preparedEnvelopeHash"
                    ],
                    outcome="applied",
                    evidence={
                        "httpStatus": 204,
                        "phase": "patch_draft",
                        "draftId": draft_id,
                        "preparedEnvelopeHash": prepared[
                            "preparedEnvelopeHash"
                        ],
                    },
                )
                if operation == "begin_attachment":
                    invoke = lambda: send_permits.begin_graph_draft_attachment(
                        capability,
                        prepared_envelope_hash=prepared[
                            "preparedEnvelopeHash"
                        ],
                        attachment_index=0,
                        attachment=attachment,
                    )
                    expected_status = "issued"
                    expected_state = "attachment_request_started"
                else:
                    attachment_operation = (
                        send_permits.begin_graph_draft_attachment(
                            capability,
                            prepared_envelope_hash=prepared[
                                "preparedEnvelopeHash"
                            ],
                            attachment_index=0,
                            attachment=attachment,
                        )
                    )
                    send_permits.complete_graph_draft_attachment(
                        capability,
                        prepared_envelope_hash=prepared[
                            "preparedEnvelopeHash"
                        ],
                        attachment_index=0,
                        outcome="applied",
                        evidence={
                            "httpStatus": 201,
                            "phase": "attach_draft",
                            "draftId": draft_id,
                            "attachmentIndex": 0,
                            "attachmentHash": attachment_operation[
                                "attachmentHash"
                            ],
                            "providerAttachmentId": f"provider-{label}",
                        },
                    )
                    if operation == "finalize":
                        invoke = lambda: (
                            send_permits.finalize_graph_draft_preparation(
                                capability,
                                prepared_envelope_hash=prepared[
                                    "preparedEnvelopeHash"
                                ],
                            )
                        )
                        expected_status = "issued"
                        expected_state = "prepared"
                    else:
                        send_permits.finalize_graph_draft_preparation(
                            capability,
                            prepared_envelope_hash=prepared[
                                "preparedEnvelopeHash"
                            ],
                        )
                        invoke = lambda: (
                            send_permits.consume_graph_send_capability(
                                capability,
                                **envelope_args,
                            )
                        )
                        expected_status = "request_started"
                        expected_state = "prepared"

        return {
            "capability": capability,
            "firestore": firestore,
            "invoke": invoke,
            "expected_status": expected_status,
            "expected_state": expected_state,
        }

    def _orphaned_draft_request_scenario(
        self,
        *,
        firestore,
        issuer: str,
        request_state: str,
        user_id: str,
    ):
        label = f"{issuer}-{request_state}"
        thread_id = f"thread-orphan-{label}"
        thread_path = f"users/{user_id}/threads/{thread_id}"
        if issuer == "terminal":
            saga, thread_ref, claim_ref = _terminal_refs(thread_id)
            saga["finalizationPlan"]["terminalThreadIds"] = [thread_id]
            saga["immutableHash"] = hashlib.sha256(
                json.dumps(
                    {
                        key: value
                        for key, value in saga.items()
                        if key not in {"immutableHash", "phase", "finalRow"}
                    },
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            thread_ref.data["terminalSaga"] = dict(saga)
            thread_ref.data["terminalSagaClaim"]["immutableHash"] = saga[
                "immutableHash"
            ]
            thread_ref.path = thread_path
            thread_ref.data["terminalReplyAttempt"].update({
                "sourceGraphMessageId": saga["sourceGraphMessageId"],
                "conversationId": saga["sourceConversationId"],
                "recipient": saga["replyRecipient"],
            })
            firestore.add_thread(thread_ref)
            capability = send_permits.issue_terminal_graph_send_permit(
                firestore,
                thread_ref,
                claim_ref,
                saga,
                "terminal-owner-a",
                1,
            )
            source_id = saga["sourceGraphMessageId"]
            recipient = saga["replyRecipient"]
            pending_ref = None
            loaded = None
        else:
            saga = None
            claim_ref = None
            token = f"pending-owner-{label}"
            thread_ref = _DocRef(
                {"clientId": "client-1"},
                doc_id=thread_id,
                path=thread_path,
            )
            loaded = _pending_data(thread_id, token=token)
            pending_id = f"pending-orphan-{label}"
            pending_ref = _DocRef(
                loaded,
                doc_id=pending_id,
                path=f"users/{user_id}/pendingResponses/{pending_id}",
            )
            firestore.add_thread(thread_ref)
            firestore.add_pending(pending_ref)
            capability = send_permits.issue_pending_graph_send_permit(
                firestore,
                thread_ref,
                pending_ref,
                dict(loaded),
                token,
            )
            source_id = loaded["msgId"]
            recipient = loaded["recipient"]

        attachment = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": f"orphan-{label}.png",
            "contentType": "image/png",
            "contentBytes": "b3JwaGFuZWQtZHJhZnQ=",
            "contentId": f"orphan-{label}-content",
            "isInline": True,
        }
        draft_id = f"draft-orphan-{label}"
        send_permits.begin_graph_draft_creation(
            capability,
            source_id,
            planned_attachment_count=1,
        )
        if request_state != "create_request_started":
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
                source_graph_message_id=source_id,
                draft_id=draft_id,
                subject=PROVIDER_DRAFT_SUBJECT,
                html_body="<p>Orphaned provider draft request.</p>",
                to_recipients=[recipient],
                cc_recipients=[],
                attachments=[attachment],
            )
            if request_state != "patch_request_started":
                send_permits.complete_graph_draft_patch(
                    capability,
                    prepared_envelope_hash=prepared[
                        "preparedEnvelopeHash"
                    ],
                    outcome="applied",
                    evidence={
                        "httpStatus": 204,
                        "phase": "patch_draft",
                        "draftId": draft_id,
                        "preparedEnvelopeHash": prepared[
                            "preparedEnvelopeHash"
                        ],
                    },
                )
                send_permits.begin_graph_draft_attachment(
                    capability,
                    prepared_envelope_hash=prepared[
                        "preparedEnvelopeHash"
                    ],
                    attachment_index=0,
                    attachment=attachment,
                )

        permit_ref = capability.permit_ref
        permit = permit_ref.data
        now = datetime.now(timezone.utc)
        if issuer == "terminal":
            thread_ref.data["terminalSagaClaim"]["leaseUntil"] = (
                now + timedelta(minutes=20)
            )
        else:
            pending_ref.data["processingLeaseUntil"] = (
                now - timedelta(minutes=5)
            )
            pending_ref.version += 1
        thread_ref.version += 1

        real_datetime = datetime

        class FutureDateTimeMeta(type):
            def __instancecheck__(cls, value):
                return isinstance(value, real_datetime)

        class FutureDateTime(real_datetime, metaclass=FutureDateTimeMeta):
            @classmethod
            def now(cls, tz=None):
                future = real_datetime.now(timezone.utc) + timedelta(
                    minutes=10
                )
                if tz is None:
                    return future.replace(tzinfo=None)
                return future.astimezone(tz)

        self.assertEqual(
            request_state,
            send_permits._validate_permit(permit)["draftPreparation"][
                "state"
            ],
        )
        return {
            "capability": capability,
            "permit_ref": permit_ref,
            "thread_ref": thread_ref,
            "claim_ref": claim_ref,
            "pending_ref": pending_ref,
            "loaded": loaded,
            "saga": saga,
            "future_datetime": FutureDateTime,
            "draft_id": (
                None if request_state == "create_request_started" else draft_id
            ),
        }

    def _lost_capability_pre_send_scenario(
        self,
        *,
        firestore,
        issuer: str,
        state: str,
        user_id: str,
        terminal_response_body=None,
    ):
        """Build an exact expired pre-send source without retaining its secret."""
        label = f"{issuer}-{state}"
        thread_id = f"thread-lost-capability-{label}"
        thread_path = f"users/{user_id}/threads/{thread_id}"
        if issuer == "terminal":
            saga, thread_ref, claim_ref = _terminal_refs(thread_id)
            if terminal_response_body is not None:
                saga["responseBody"] = terminal_response_body
                canonical_body = str(terminal_response_body or "").strip()
                thread_ref.data["terminalReplyAttempt"][
                    "responseBodyHash"
                ] = hashlib.sha256(
                    canonical_body.encode("utf-8")
                ).hexdigest()
            saga["finalizationPlan"]["terminalThreadIds"] = [thread_id]
            saga["immutableHash"] = hashlib.sha256(
                json.dumps(
                    {
                        key: value
                        for key, value in saga.items()
                        if key not in {"immutableHash", "phase", "finalRow"}
                    },
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            thread_ref.data["terminalSaga"] = dict(saga)
            thread_ref.data["terminalSagaClaim"]["immutableHash"] = saga[
                "immutableHash"
            ]
            thread_ref.path = thread_path
            thread_ref.data["terminalReplyAttempt"].update({
                "sourceGraphMessageId": saga["sourceGraphMessageId"],
                "conversationId": saga["sourceConversationId"],
                "recipient": saga["replyRecipient"],
            })
            firestore.add_thread(thread_ref)
            capability = send_permits.issue_terminal_graph_send_permit(
                firestore,
                thread_ref,
                claim_ref,
                saga,
                "terminal-owner-a",
                1,
            )
            source_id = saga["sourceGraphMessageId"]
            recipient = saga["replyRecipient"]
            pending_ref = None
            loaded = None
        else:
            saga = None
            claim_ref = None
            token = f"pending-owner-{label}"
            thread_ref = _DocRef(
                {"clientId": "client-1"},
                doc_id=thread_id,
                path=thread_path,
            )
            loaded = _pending_data(thread_id, token=token)
            pending_id = f"pending-lost-capability-{label}"
            pending_ref = _DocRef(
                loaded,
                doc_id=pending_id,
                path=f"users/{user_id}/pendingResponses/{pending_id}",
            )
            firestore.add_thread(thread_ref)
            firestore.add_pending(pending_ref)
            capability = send_permits.issue_pending_graph_send_permit(
                firestore,
                thread_ref,
                pending_ref,
                dict(loaded),
                token,
            )
            source_id = loaded["msgId"]
            recipient = loaded["recipient"]

        attachment = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": f"lost-capability-{label}.png",
            "contentType": "image/png",
            "contentBytes": "bG9zdC1jYXBhYmlsaXR5",
            "contentId": f"lost-capability-{label}-content",
            "isInline": True,
        }
        draft_id = f"draft-lost-capability-{label}"
        prepared = None
        if state != "issued_no_preparation":
            attachment_count = (
                0 if state == "create_definitely_not_created" else 1
            )
            send_permits.begin_graph_draft_creation(
                capability,
                source_id,
                planned_attachment_count=attachment_count,
            )
            if state == "create_definitely_not_created":
                definite_evidence = {
                    "reason": "provider rejected before creating a draft",
                    "phase": "create_reply",
                    "draftId": None,
                    "providerSendStarted": False,
                    "automaticDeleteAttempted": False,
                }
                send_permits.complete_graph_draft_creation(
                    capability,
                    outcome="definitely_not_created",
                    evidence=definite_evidence,
                )
            else:
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
                if state != "draft_created":
                    prepared = send_permits.begin_graph_draft_patch(
                        capability,
                        source_graph_message_id=source_id,
                        draft_id=draft_id,
                        subject=PROVIDER_DRAFT_SUBJECT,
                        html_body="<p>Retained pre-send provider draft.</p>",
                        to_recipients=[recipient],
                        cc_recipients=[],
                        attachments=[attachment],
                    )
                    send_permits.complete_graph_draft_patch(
                        capability,
                        prepared_envelope_hash=prepared[
                            "preparedEnvelopeHash"
                        ],
                        outcome="applied",
                        evidence={
                            "httpStatus": 204,
                            "phase": "patch_draft",
                            "draftId": draft_id,
                            "preparedEnvelopeHash": prepared[
                                "preparedEnvelopeHash"
                            ],
                        },
                    )
                    if state not in {"patch_applied"}:
                        attachment_operation = (
                            send_permits.begin_graph_draft_attachment(
                                capability,
                                prepared_envelope_hash=prepared[
                                    "preparedEnvelopeHash"
                                ],
                                attachment_index=0,
                                attachment=attachment,
                            )
                        )
                        send_permits.complete_graph_draft_attachment(
                            capability,
                            prepared_envelope_hash=prepared[
                                "preparedEnvelopeHash"
                            ],
                            attachment_index=0,
                            outcome="applied",
                            evidence={
                                "httpStatus": 201,
                                "phase": "attach_draft",
                                "draftId": draft_id,
                                "attachmentIndex": 0,
                                "attachmentHash": attachment_operation[
                                    "attachmentHash"
                                ],
                                "providerAttachmentId": (
                                    f"provider-lost-capability-{label}"
                                ),
                            },
                        )
                        if state == "prepared":
                            send_permits.finalize_graph_draft_preparation(
                                capability,
                                prepared_envelope_hash=prepared[
                                    "preparedEnvelopeHash"
                                ],
                            )

        permit_ref = capability.permit_ref
        now = datetime.now(timezone.utc)
        if issuer == "terminal":
            thread_ref.data["terminalSagaClaim"]["leaseUntil"] = (
                now + timedelta(minutes=20)
            )
        else:
            pending_ref.data["processingLeaseUntil"] = (
                now - timedelta(minutes=5)
            )
            pending_ref.version += 1
        thread_ref.version += 1

        real_datetime = datetime

        class FutureDateTimeMeta(type):
            def __instancecheck__(cls, value):
                return isinstance(value, real_datetime)

        class FutureDateTime(real_datetime, metaclass=FutureDateTimeMeta):
            @classmethod
            def now(cls, tz=None):
                future = real_datetime.now(timezone.utc) + timedelta(
                    minutes=10
                )
                if tz is None:
                    return future.replace(tzinfo=None)
                return future.astimezone(tz)

        permit = send_permits._validate_permit(permit_ref.data)
        expected_draft_state = (
            None if state == "issued_no_preparation" else state
        )
        self.assertEqual(
            expected_draft_state,
            (permit.get("draftPreparation") or {}).get("state"),
        )
        return {
            "capability": capability,
            "permit_ref": permit_ref,
            "thread_ref": thread_ref,
            "claim_ref": claim_ref,
            "pending_ref": pending_ref,
            "loaded": loaded,
            "saga": saga,
            "future_datetime": FutureDateTime,
            "draft_id": (
                None
                if state in {
                    "issued_no_preparation",
                    "create_definitely_not_created",
                }
                else draft_id
            ),
        }

    def _lost_capability_side_ref(
        self,
        *,
        firestore,
        scenario,
        issuer: str,
        state: str,
        user_id: str,
    ):
        stable_states = {
            "draft_created",
            "patch_applied",
            "attachment_applied",
            "prepared",
        }
        permit = send_permits._validate_permit(
            scenario["permit_ref"].data
        )
        if issuer == "terminal":
            if state in stable_states:
                side_path = send_permits._terminal_review_path(
                    scenario["thread_ref"],
                    scenario["saga"],
                    permit,
                    kind="draft_needs_review",
                )
                side_ref = firestore.user_root.collection(
                    "terminalGraphSendReviews"
                ).document(side_path.rsplit("/", 1)[-1])
            else:
                side_path = (
                    f"users/{user_id}/pendingResponses/"
                    f"{scenario['thread_ref'].id}"
                )
                side_ref = firestore.user_root.collection(
                    "pendingResponses"
                ).document(scenario["thread_ref"].id)
        elif state in stable_states:
            side_path = send_permits._pending_draft_review_path(
                scenario["thread_ref"],
                permit["permitId"],
            )
            side_ref = firestore.user_root.collection(
                "graphSendDraftReviews"
            ).document(f"pending-{permit['permitId']}")
        else:
            side_path = send_permits._pending_dead_letter_path(
                scenario["thread_ref"],
                scenario["pending_ref"],
                permit,
            )
            side_ref = firestore.user_root.collection(
                "deadLetterQueue"
            ).document(side_path.rsplit("/", 1)[-1])
        side_ref.path = side_path
        return side_ref

    def _terminal_pre_resolved_definitely_unsent_scenario(
        self,
        *,
        firestore,
        shape: str,
        user_id: str,
        terminal_response_body=None,
    ):
        state = (
            "issued_no_preparation"
            if shape == "preflight_no_preparation"
            else "create_definitely_not_created"
        )
        scenario = self._lost_capability_pre_send_scenario(
            firestore=firestore,
            issuer="terminal",
            state=state,
            user_id=user_id,
            terminal_response_body=terminal_response_body,
        )
        if shape == "preflight_no_preparation":
            send_permits.resolve_graph_send_permit(
                scenario["capability"],
                "definitely_not_sent",
                evidence={
                    "reason": "campaign gate closed before provider work",
                    "phase": "preflight_campaign_gate",
                    "providerSendStarted": False,
                },
            )
        elif shape != "create_definitely_not_created":
            raise AssertionError(f"unsupported definite-unsent shape: {shape}")

        permit = send_permits._validate_permit(
            scenario["permit_ref"].data
        )
        self.assertEqual("definitely_not_sent", permit["status"])
        self.assertIsNone(permit.get("requestStartedAt"))
        self.assertEqual(
            "definitely_not_sent",
            send_permits.expired_graph_send_pre_send_recovery_kind(
                permit,
                now=scenario["future_datetime"].now(timezone.utc),
            ),
        )
        if shape == "preflight_no_preparation":
            self.assertFalse(permit.get("draftPreparation"))
            self.assertTrue(
                send_permits._exact_preflight_definitely_not_sent_resolution(
                    permit
                )
            )
        else:
            self.assertEqual(
                "create_definitely_not_created",
                permit["draftPreparation"]["state"],
            )
            self.assertTrue(
                send_permits._exact_definitely_not_created_resolution(permit)
            )
        return scenario, state

    def _invoke_lost_capability_pre_send_recovery(
        self,
        *,
        firestore,
        scenario,
        issuer: str,
        state: str,
        user_id: str,
    ):
        side_ref = self._lost_capability_side_ref(
            firestore=firestore,
            scenario=scenario,
            issuer=issuer,
            state=state,
            user_id=user_id,
        )

        with patch.object(
            send_permits,
            "datetime",
            scenario["future_datetime"],
        ), patch.object(
            processing,
            "_fs",
            firestore,
        ), patch.object(
            processing,
            "_renew_terminal_saga_execution",
            return_value=datetime.now(timezone.utc),
        ), patch.object(
            processing,
            "_clear_resolved_terminal_saga",
        ), patch.object(
            pending_responses,
            "_pending_claim_refs",
            return_value=(
                firestore,
                firestore.user_root,
                scenario["thread_ref"],
                scenario["pending_ref"],
            ),
        ), patch.object(
            processing,
            "find_exact_sent_message_by_immutable_id",
            side_effect=AssertionError(
                "lost capability recovery cannot search Sent"
            ),
        ), patch.object(
            pending_responses,
            "find_exact_sent_message_by_immutable_id",
            side_effect=AssertionError(
                "lost capability recovery cannot search Sent"
            ),
        ), patch.object(
            processing.requests,
            "post",
            side_effect=AssertionError(
                "lost capability recovery cannot replay provider work"
            ),
        ), patch.object(
            processing.requests,
            "patch",
            side_effect=AssertionError(
                "lost capability recovery cannot replay provider work"
            ),
        ), patch.object(
            email_module,
            "_delete_graph_reply_draft",
            side_effect=AssertionError(
                "lost capability recovery cannot delete a draft"
            ),
        ):
            if issuer == "terminal":
                outcome = processing._settle_terminal_reply_obligation(
                    user_id,
                    "client-1",
                    scenario["thread_ref"].id,
                    {"Authorization": "Bearer test"},
                    scenario["saga"]["replyRecipient"],
                    scenario["saga"],
                    terminal_saga_owner=processing.TerminalSagaExecution(
                        owner="terminal-owner-a",
                        fencing_token=1,
                    ),
                )
            else:
                doc = types.SimpleNamespace(
                    id=scenario["pending_ref"].id,
                    reference=scenario["pending_ref"],
                )
                outcome = pending_responses._reconcile_expired_pending_permit(
                    user_id,
                    {"Authorization": "Bearer test"},
                    doc,
                    scenario["loaded"],
                )
        return outcome, side_ref

    def _invoke_orphaned_draft_atomic_settlement(
        self,
        *,
        firestore,
        scenario,
        issuer: str,
        user_id: str,
    ):
        permit = send_permits._validate_permit(
            scenario["permit_ref"].data
        )
        if issuer == "terminal":
            saga = scenario["saga"]
            review_path = send_permits._terminal_review_path(
                scenario["thread_ref"],
                saga,
                permit,
                kind="draft_needs_review",
            )
            with patch.object(processing, "_fs", firestore):
                review_ref, review_payload = (
                    processing._terminal_reply_reconciliation_document(
                        user_id,
                        scenario["thread_ref"].id,
                        saga,
                        permit,
                        kind="draft_needs_review",
                        already_sent=False,
                        provider_send_started=False,
                        reason="orphaned draft request recovery",
                    )
                )
            review_ref.path = review_path
            attempt = dict(
                scenario["thread_ref"].data["terminalReplyAttempt"]
            )
            send_permits.cas_terminal_reply_transition(
                firestore,
                scenario["thread_ref"],
                scenario["claim_ref"],
                saga,
                "terminal-owner-a",
                1,
                expected_attempt_status="sending",
                thread_patch={
                    "terminalReplyOwed": False,
                    "terminalReplyOutcome": "draft_needs_review",
                    "terminalReplyResolvedAt": send_permits.SERVER_TIMESTAMP,
                    "terminalReplyAttempt": {
                        **attempt,
                        "status": "committed",
                        "outcome": "draft_needs_review",
                        "committedAt": send_permits.SERVER_TIMESTAMP,
                    },
                    "updatedAt": send_permits.SERVER_TIMESTAMP,
                },
                permit_settlement="settled_draft_needs_review",
                side_documents=((review_ref, review_payload),),
            )
            return review_ref

        review_id = f"pending-{permit['permitId']}"
        review_ref = scenario["thread_ref"].collection(
            "graphSendReviews"
        ).document(review_id)
        review_ref.path = send_permits._pending_draft_review_path(
            scenario["thread_ref"],
            permit["permitId"],
        )
        preparation = dict(permit.get("draftPreparation") or {})
        send_permits.reconcile_pending_graph_send_permit(
            firestore,
            scenario["thread_ref"],
            scenario["pending_ref"],
            scenario["loaded"],
            outcome="draft_needs_review",
            evidence_document=(
                review_ref,
                {
                    "status": "manual_review",
                    "source": "pendingGraphSendProtocol",
                    "authoritative": True,
                    "alreadySent": False,
                    "providerSendStarted": False,
                    "sendOutcomeUnknown": False,
                    "retryAllowed": False,
                    "automaticDeleteAttempted": False,
                    "failureReason": "orphaned draft request recovery",
                    "draftId": preparation.get("draftId"),
                    "draftMutationState": preparation.get("state"),
                    "draftResolutionEvidenceHash": permit.get(
                        "resolutionEvidenceHash"
                    ),
                },
            ),
        )
        return review_ref

    def _settle_terminal_draft_review(
        self,
        *,
        thread_id: str,
        canonical_user_id: str = "uid-draft-review-integrity",
    ):
        saga, thread_ref, claim_ref = _terminal_refs(thread_id)
        thread_ref.path = (
            f"users/{canonical_user_id}/threads/{thread_id}"
        )
        capability = send_permits.issue_terminal_graph_send_permit(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
        )
        prepared = _prepare_capability_for_send(
            capability,
            saga["sourceGraphMessageId"],
            saga["replyRecipient"],
        )
        resolution_evidence = {
            "reason": "campaign stopped after exact draft preparation",
            "phase": "final_campaign_gate",
            "draftId": prepared["draft_id"],
            "providerSendStarted": False,
            "automaticDeleteAttempted": False,
        }
        send_permits.resolve_graph_send_permit(
            capability,
            "needs_reconciliation",
            evidence=resolution_evidence,
        )
        permit = send_permits.read_permit(capability)
        review_path = send_permits._terminal_review_path(
            thread_ref,
            saga,
            permit,
            kind="draft_needs_review",
        )
        review_ref = _DocRef(
            {},
            exists=False,
            doc_id=review_path.rsplit("/", 1)[-1],
            path=review_path,
        )
        review_payload = {
            "threadId": thread_id,
            "sourceGraphMessageId": saga["sourceGraphMessageId"],
            "status": "manual_review",
            "source": "terminalGraphSendProtocol",
            "authoritative": True,
            "alreadySent": False,
            "providerSendStarted": False,
            "sendOutcomeUnknown": False,
            "retryAllowed": False,
            "automaticDeleteAttempted": False,
            "failureReason": resolution_evidence["reason"],
            "graphSendPermitId": permit["permitId"],
            "graphSendPermitHash": permit["immutableHash"],
            "preparedEnvelopeHash": permit["preparedEnvelope"][
                "preparedEnvelopeHash"
            ],
            "draftId": prepared["draft_id"],
            "draftMutationState": "prepared",
            "draftResolutionEvidenceHash": permit[
                "resolutionEvidenceHash"
            ],
            "createdAt": send_permits.SERVER_TIMESTAMP,
            "updatedAt": send_permits.SERVER_TIMESTAMP,
        }
        attempt = dict(thread_ref.data["terminalReplyAttempt"])
        committed_attempt = {
            **attempt,
            "status": "committed",
            "outcome": "draft_needs_review",
            "committedAt": send_permits.SERVER_TIMESTAMP,
        }
        send_permits.cas_terminal_reply_transition(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
            expected_attempt_status="sending",
            thread_patch={
                "terminalReplyOwed": False,
                "terminalReplyOutcome": "draft_needs_review",
                "terminalReplyResolvedAt": send_permits.SERVER_TIMESTAMP,
                "terminalReplyAttempt": committed_attempt,
                "updatedAt": send_permits.SERVER_TIMESTAMP,
            },
            permit_settlement="settled_draft_needs_review",
            capability=capability,
            side_documents=((review_ref, review_payload),),
        )
        return {
            "capability": capability,
            "thread_ref": thread_ref,
            "review_ref": review_ref,
        }

    def _settle_terminal_draft_mutation_review(
        self,
        *,
        thread_id: str,
        ambiguity: str,
    ):
        saga, thread_ref, claim_ref = _terminal_refs(thread_id)
        thread_ref.path = f"users/uid-draft-mutation/threads/{thread_id}"
        capability = send_permits.issue_terminal_graph_send_permit(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
        )
        source_id = saga["sourceGraphMessageId"]
        draft_id = f"draft-{thread_id}"
        attachment = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": "review-proof.png",
            "contentType": "image/png",
            "contentBytes": "cmV2aWV3LXByb29m",
            "contentId": "review-proof-1",
            "isInline": True,
        }
        attachments = [attachment] if ambiguity == "attachment" else []
        send_permits.begin_graph_draft_creation(
            capability,
            source_id,
            planned_attachment_count=len(attachments),
        )
        if ambiguity == "create":
            send_permits.complete_graph_draft_creation(
                capability,
                outcome="needs_reconciliation",
                evidence={
                    "reason": "create timeout",
                    "phase": "create_reply",
                },
            )
        else:
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
                source_graph_message_id=source_id,
                draft_id=draft_id,
                subject=PROVIDER_DRAFT_SUBJECT,
                html_body="<p>Draft mutation review.</p>",
                to_recipients=[saga["replyRecipient"]],
                cc_recipients=[],
                attachments=attachments,
            )
            if ambiguity == "patch":
                send_permits.complete_graph_draft_patch(
                    capability,
                    prepared_envelope_hash=prepared[
                        "preparedEnvelopeHash"
                    ],
                    outcome="needs_reconciliation",
                    evidence={
                        "reason": "patch timeout",
                        "phase": "patch_draft",
                    },
                )
            else:
                send_permits.complete_graph_draft_patch(
                    capability,
                    prepared_envelope_hash=prepared[
                        "preparedEnvelopeHash"
                    ],
                    outcome="applied",
                    evidence={
                        "httpStatus": 204,
                        "phase": "patch_draft",
                        "draftId": draft_id,
                        "preparedEnvelopeHash": prepared[
                            "preparedEnvelopeHash"
                        ],
                    },
                )
                send_permits.begin_graph_draft_attachment(
                    capability,
                    prepared_envelope_hash=prepared[
                        "preparedEnvelopeHash"
                    ],
                    attachment_index=0,
                    attachment=attachment,
                )
                send_permits.complete_graph_draft_attachment(
                    capability,
                    prepared_envelope_hash=prepared[
                        "preparedEnvelopeHash"
                    ],
                    attachment_index=0,
                    outcome="needs_reconciliation",
                    evidence={
                        "reason": "attachment timeout",
                        "phase": "attach_draft",
                    },
                )

        permit = send_permits.read_permit(capability)
        review_path = send_permits._terminal_review_path(
            thread_ref,
            saga,
            permit,
            kind="draft_needs_review",
        )
        review_ref = _DocRef(
            {},
            exists=False,
            doc_id=review_path.rsplit("/", 1)[-1],
            path=review_path,
        )
        with patch.object(processing, "_fs") as firestore_mock:
            (
                firestore_mock.collection.return_value.document.return_value
                .collection.return_value.document.return_value
            ) = review_ref
            evidence_document = (
                processing._terminal_reply_reconciliation_document(
                    "uid-draft-mutation",
                    thread_id,
                    saga,
                    permit,
                    kind="draft_needs_review",
                    already_sent=False,
                    provider_send_started=False,
                    reason=permit["resolutionEvidence"]["reason"],
                )
            )
        attempt = dict(thread_ref.data["terminalReplyAttempt"])
        send_permits.cas_terminal_reply_transition(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
            expected_attempt_status="sending",
            thread_patch={
                "terminalReplyOwed": False,
                "terminalReplyOutcome": "draft_needs_review",
                "terminalReplyResolvedAt": send_permits.SERVER_TIMESTAMP,
                "terminalReplyAttempt": {
                    **attempt,
                    "status": "committed",
                    "outcome": "draft_needs_review",
                    "committedAt": send_permits.SERVER_TIMESTAMP,
                },
                "updatedAt": send_permits.SERVER_TIMESTAMP,
            },
            permit_settlement="settled_draft_needs_review",
            capability=capability,
            side_documents=(evidence_document,),
        )
        return {
            "capability": capability,
            "permit": send_permits.read_permit(capability),
            "thread_ref": thread_ref,
            "review_ref": review_ref,
        }

    def _settle_pending_draft_mutation_review(
        self,
        *,
        thread_id: str,
        ambiguity: str,
        commit_outcome: str = None,
    ):
        token = f"pending-{ambiguity}-owner"
        thread_ref = _DocRef(
            {},
            doc_id=thread_id,
            path=f"users/uid-pending-mutation/threads/{thread_id}",
        )
        loaded = _pending_data(thread_id, token=token)
        pending_ref = _DocRef(
            loaded,
            doc_id=f"pending-{ambiguity}",
            path=(
                "users/uid-pending-mutation/pendingResponses/"
                f"pending-{ambiguity}"
            ),
        )
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )
        source_id = loaded["msgId"]
        draft_id = f"draft-{thread_id}"
        attachment = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": "pending-review-proof.png",
            "contentType": "image/png",
            "contentBytes": "cGVuZGluZy1yZXZpZXctcHJvb2Y=",
            "contentId": "pending-review-proof-1",
            "isInline": True,
        }
        attachments = [attachment] if ambiguity == "attachment" else []
        send_permits.begin_graph_draft_creation(
            capability,
            source_id,
            planned_attachment_count=len(attachments),
        )
        if ambiguity == "create":
            send_permits.complete_graph_draft_creation(
                capability,
                outcome="needs_reconciliation",
                evidence={
                    "reason": "pending create timeout",
                    "phase": "create_reply",
                },
            )
        else:
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
                source_graph_message_id=source_id,
                draft_id=draft_id,
                subject=PROVIDER_DRAFT_SUBJECT,
                html_body="<p>Pending draft mutation review.</p>",
                to_recipients=[loaded["recipient"]],
                cc_recipients=[],
                attachments=attachments,
            )
            if ambiguity == "patch":
                send_permits.complete_graph_draft_patch(
                    capability,
                    prepared_envelope_hash=prepared[
                        "preparedEnvelopeHash"
                    ],
                    outcome="needs_reconciliation",
                    evidence={
                        "reason": "pending patch timeout",
                        "phase": "patch_draft",
                    },
                )
            else:
                send_permits.complete_graph_draft_patch(
                    capability,
                    prepared_envelope_hash=prepared[
                        "preparedEnvelopeHash"
                    ],
                    outcome="applied",
                    evidence={
                        "httpStatus": 204,
                        "phase": "patch_draft",
                        "draftId": draft_id,
                        "preparedEnvelopeHash": prepared[
                            "preparedEnvelopeHash"
                        ],
                    },
                )
                send_permits.begin_graph_draft_attachment(
                    capability,
                    prepared_envelope_hash=prepared[
                        "preparedEnvelopeHash"
                    ],
                    attachment_index=0,
                    attachment=attachment,
                )
                send_permits.complete_graph_draft_attachment(
                    capability,
                    prepared_envelope_hash=prepared[
                        "preparedEnvelopeHash"
                    ],
                    attachment_index=0,
                    outcome="needs_reconciliation",
                    evidence={
                        "reason": "pending attachment timeout",
                        "phase": "attach_draft",
                    },
                )

        permit = send_permits.read_permit(capability)
        review_id = f"pending-{permit['permitId']}"
        user_ref = _DocRef(
            {},
            doc_id="uid-pending-mutation",
            path="users/uid-pending-mutation",
        )
        review_ref = _DocRef(
            {},
            exists=False,
            doc_id=review_id,
            path=(
                "users/uid-pending-mutation/graphSendDraftReviews/"
                f"{review_id}"
            ),
        )
        user_ref.collection("graphSendDraftReviews").docs[
            review_id
        ] = review_ref
        doc = types.SimpleNamespace(id=pending_ref.id, reference=pending_ref)
        settlement_attempts_before = getattr(
            self.firestore,
            "commit_attempts",
            0,
        )
        if commit_outcome is not None:
            self.firestore.commit_outcomes = [commit_outcome]
        with patch.object(
            pending_responses,
            "_pending_claim_refs",
            return_value=(self.firestore, user_ref, thread_ref, pending_ref),
        ):
            pending_responses._cas_pending_draft_review(
                "uid-pending-mutation",
                doc,
                loaded,
                token,
                capability,
                permit["resolutionEvidence"]["reason"],
            )
        return {
            "capability": capability,
            "permit": send_permits.read_permit(capability),
            "thread_ref": thread_ref,
            "review_ref": review_ref,
            "user_ref": user_ref,
            "settlementCommitAttempts": (
                getattr(self.firestore, "commit_attempts", 0)
                - settlement_attempts_before
            ),
        }

    def _pending_draft_review_resolution_case(
        self,
        *,
        firestore,
        suffix: str,
    ):
        prior_firestore = self.firestore
        self.firestore = firestore
        try:
            settled = self._settle_pending_draft_mutation_review(
                thread_id=f"thread-draft-resolution-{suffix}",
                ambiguity="patch",
            )
        finally:
            self.firestore = prior_firestore
        permit = send_permits.read_permit(settled["capability"])
        settlement_id = f"draft-review-settlement-{suffix}"
        audit_ref = _DocRef(
            {},
            exists=False,
            doc_id=settlement_id,
            path=(
                "users/uid-pending-mutation/"
                "graphSendDraftReviewSettlements/"
                f"{settlement_id}"
            ),
        )
        return settled, {
            "expected_permit_id": permit["permitId"],
            "expected_permit_hash": permit["immutableHash"],
            "expected_review_evidence_hash": permit[
                "draftReviewEvidenceHash"
            ],
            "review_ref": settled["review_ref"],
            "action": "confirm_retained_draft_not_actionable",
            "operator_id": "authenticated-operator-uid",
            "operator_reason": "The retained provider draft was manually discarded.",
            "settlement_id": settlement_id,
            "audit_ref": audit_ref,
        }

    def test_local_transition_apply_then_raise_recovers_exact_target(self):
        operations = (
            "begin_create",
            "begin_patch",
            "begin_attachment",
            "finalize",
            "consume",
        )
        for issuer in ("terminal", "pending"):
            for operation in operations:
                with self.subTest(issuer=issuer, operation=operation):
                    firestore = _CommitOutcomeFirestore()
                    scenario = self._local_transition_scenario(
                        firestore=firestore,
                        issuer=issuer,
                        operation=operation,
                    )
                    attempts_before = firestore.commit_attempts
                    firestore.commit_outcomes = ["apply_then_raise"]
                    result = scenario["invoke"]()

                    self.assertEqual(
                        attempts_before + 1,
                        firestore.commit_attempts,
                    )
                    permit = send_permits.read_permit(
                        scenario["capability"]
                    )
                    self.assertEqual(
                        scenario["expected_status"], permit["status"]
                    )
                    self.assertEqual(
                        scenario["expected_state"],
                        permit["draftPreparation"]["state"],
                    )
                    if operation in {"begin_create", "consume"}:
                        self.assertGreater(result, 0)
                    elif operation in {"begin_patch", "begin_attachment"}:
                        self.assertGreater(result["timeoutSeconds"], 0)
                    else:
                        self.assertEqual(
                            permit["preparedEnvelope"][
                                "preparedEnvelopeHash"
                            ],
                            result["preparedEnvelopeHash"],
                        )
                    if operation == "consume":
                        self.assertLessEqual(
                            result,
                            permit["providerTimeoutSeconds"],
                        )

    def test_local_transition_no_apply_retries_same_bytes_once(self):
        operations = (
            "begin_create",
            "begin_patch",
            "begin_attachment",
            "finalize",
            "consume",
        )
        for issuer in ("terminal", "pending"):
            for operation in operations:
                with self.subTest(issuer=issuer, operation=operation):
                    firestore = _CommitOutcomeFirestore()
                    scenario = self._local_transition_scenario(
                        firestore=firestore,
                        issuer=issuer,
                        operation=operation,
                    )
                    attempts_before = firestore.commit_attempts
                    payloads_before = len(firestore.commit_payloads)
                    firestore.commit_outcomes = ["no_apply"]

                    scenario["invoke"]()

                    self.assertEqual(
                        attempts_before + 2,
                        firestore.commit_attempts,
                    )
                    retry_payloads = firestore.commit_payloads[
                        payloads_before:
                    ]
                    self.assertEqual(2, len(retry_payloads))
                    self.assertEqual(retry_payloads[0], retry_payloads[1])
                    permit = send_permits.read_permit(
                        scenario["capability"]
                    )
                    self.assertEqual(
                        scenario["expected_status"], permit["status"]
                    )
                    self.assertEqual(
                        scenario["expected_state"],
                        permit["draftPreparation"]["state"],
                    )

    def test_local_transition_repeated_no_apply_is_typed_and_retryable(self):
        for operation in (
            "begin_create",
            "begin_patch",
            "begin_attachment",
            "finalize",
            "consume",
        ):
            with self.subTest(operation=operation):
                firestore = _CommitOutcomeFirestore()
                scenario = self._local_transition_scenario(
                    firestore=firestore,
                    issuer="pending",
                    operation=operation,
                )
                before = copy.deepcopy(
                    scenario["capability"].permit_ref.data
                )
                firestore.commit_outcomes = ["no_apply", "no_apply"]

                with self.assertRaises(
                    send_permits.GraphSendPermitError
                ) as raised:
                    scenario["invoke"]()

                self.assertEqual(
                    "GraphSendPermitLocalRetryable",
                    type(raised.exception).__name__,
                )
                self.assertEqual(
                    before,
                    scenario["capability"].permit_ref.data,
                )

    def test_processing_repeated_local_no_apply_preserves_exact_provider_boundary(self):
        operations = {
            "begin_create": {
                "prior_commits": 0,
                "state": None,
                "status": "definitely_not_sent",
                "outcome": "local_transition_definitely_not_started",
                "provider": (0, 0, 0, 0),
            },
            "begin_patch": {
                "prior_commits": 2,
                "state": "draft_created",
                "status": "needs_reconciliation",
                "outcome": "draft_mutation_needs_reconciliation",
                "provider": (1, 0, 0, 0),
            },
            "begin_attachment": {
                "prior_commits": 4,
                "state": "patch_applied",
                "status": "needs_reconciliation",
                "outcome": "draft_mutation_needs_reconciliation",
                "provider": (1, 1, 0, 0),
            },
            "finalize": {
                "prior_commits": 6,
                "state": "attachment_applied",
                "status": "needs_reconciliation",
                "outcome": "draft_mutation_needs_reconciliation",
                "provider": (1, 1, 1, 0),
            },
            "consume": {
                "prior_commits": 7,
                "state": "prepared",
                "status": "needs_reconciliation",
                "outcome": "draft_mutation_needs_reconciliation",
                "provider": (1, 1, 1, 0),
            },
        }
        user_id = "uid-local-transition-processing"
        for issuer in ("terminal", "pending"):
            for operation, expected in operations.items():
                with self.subTest(issuer=issuer, operation=operation):
                    label = f"{issuer}-{operation}"
                    rooted = _CommitOutcomeRootedFirestore()
                    thread_id = f"thread-processing-{label}"
                    thread_path = f"users/{user_id}/threads/{thread_id}"
                    if issuer == "terminal":
                        saga, thread_ref, claim_ref = _terminal_refs(thread_id)
                        thread_ref.path = thread_path
                        rooted.add_thread(thread_ref)
                        capability = send_permits.issue_terminal_graph_send_permit(
                            rooted,
                            thread_ref,
                            claim_ref,
                            saga,
                            "terminal-owner-a",
                            1,
                        )
                        source_id = saga["sourceGraphMessageId"]
                        conversation_id = saga["sourceConversationId"]
                        recipient = saga["replyRecipient"]
                        body = saga["responseBody"]
                    else:
                        token = f"pending-owner-{label}"
                        thread_ref = _DocRef(
                            {"clientId": "client-1"},
                            doc_id=thread_id,
                            path=thread_path,
                        )
                        loaded = _pending_data(thread_id, token=token)
                        pending_id = f"pending-processing-{label}"
                        pending_ref = _DocRef(
                            loaded,
                            doc_id=pending_id,
                            path=(
                                f"users/{user_id}/pendingResponses/"
                                f"{pending_id}"
                            ),
                        )
                        rooted.add_thread(thread_ref)
                        rooted.add_pending(pending_ref)
                        capability = send_permits.issue_pending_graph_send_permit(
                            rooted,
                            thread_ref,
                            pending_ref,
                            dict(loaded),
                            token,
                        )
                        source_id = loaded["msgId"]
                        conversation_id = loaded["conversationId"]
                        recipient = loaded["recipient"]
                        body = loaded["responseBody"]

                    attachment = {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": f"{label}.png",
                        "contentType": "image/png",
                        "contentBytes": "ZXhhY3QtbG9jYWwtcmV0cnk=",
                        "contentId": f"{label}-content",
                        "isInline": True,
                    }
                    draft_id = f"draft-processing-{label}"
                    stack, calls = self._local_transition_processing_stack(
                        rooted,
                        source_id=source_id,
                        conversation_id=conversation_id,
                        recipient=recipient,
                        draft_id=draft_id,
                        attachment=attachment,
                    )
                    attempts_before = rooted.commit_attempts
                    rooted.commit_outcomes = [
                        *([None] * expected["prior_commits"]),
                        "no_apply",
                        "no_apply",
                    ]

                    with stack, patch.object(
                        email_module,
                        "_delete_graph_reply_draft",
                        side_effect=AssertionError(
                            "local transition recovery cannot delete a draft"
                        ),
                    ) as delete_draft:
                        sent = processing.send_reply_in_thread(
                            user_id,
                            {"Authorization": "Bearer test"},
                            body,
                            source_id,
                            recipient,
                            thread_id,
                            graph_send_capability=capability,
                        )

                    self.assertFalse(sent)
                    delete_draft.assert_not_called()
                    self.assertEqual([], rooted.commit_outcomes)
                    self.assertEqual(
                        attempts_before + expected["prior_commits"] + 3,
                        rooted.commit_attempts,
                    )
                    permit = send_permits.read_permit(capability)
                    self.assertEqual(expected["status"], permit["status"])
                    preparation = dict(permit.get("draftPreparation") or {})
                    self.assertEqual(expected["state"], preparation.get("state"))
                    self.assertIsNone(permit.get("requestStartedAt"))
                    self.assertEqual(
                        expected["outcome"],
                        processing.send_reply_in_thread.last_outcome,
                    )
                    if expected["state"] is not None:
                        self.assertEqual(draft_id, preparation.get("draftId"))
                        self.assertEqual(
                            {
                                "draftId",
                                "reason",
                                "phase",
                                "providerSendStarted",
                                "automaticDeleteAttempted",
                            },
                            set(permit["resolutionEvidence"]),
                        )
                        self.assertFalse(
                            permit["resolutionEvidence"]["providerSendStarted"]
                        )
                        self.assertFalse(
                            permit["resolutionEvidence"][
                                "automaticDeleteAttempted"
                            ]
                        )

                    create_count = sum(
                        kind == "post" and url.endswith("/createReplyAll")
                        for kind, url in calls
                    )
                    patch_count = sum(kind == "patch" for kind, _url in calls)
                    attachment_count = sum(
                        kind == "post" and url.endswith("/attachments")
                        for kind, url in calls
                    )
                    send_count = sum(
                        kind == "post" and url.endswith("/send")
                        for kind, url in calls
                    )
                    self.assertEqual(
                        expected["provider"],
                        (
                            create_count,
                            patch_count,
                            attachment_count,
                            send_count,
                        ),
                    )

    def test_processing_expired_consume_retains_prepared_draft_for_review(self):
        user_id = "uid-expired-consume-processing"
        real_remaining = send_permits._remaining_provider_seconds
        for issuer in ("terminal", "pending"):
            with self.subTest(issuer=issuer):
                rooted = _CommitOutcomeRootedFirestore()
                thread_id = f"thread-expired-consume-{issuer}"
                thread_path = f"users/{user_id}/threads/{thread_id}"
                if issuer == "terminal":
                    saga, thread_ref, claim_ref = _terminal_refs(thread_id)
                    thread_ref.path = thread_path
                    rooted.add_thread(thread_ref)
                    capability = send_permits.issue_terminal_graph_send_permit(
                        rooted,
                        thread_ref,
                        claim_ref,
                        saga,
                        "terminal-owner-a",
                        1,
                    )
                    source_id = saga["sourceGraphMessageId"]
                    conversation_id = saga["sourceConversationId"]
                    recipient = saga["replyRecipient"]
                    body = saga["responseBody"]
                else:
                    token = f"pending-owner-expired-consume-{issuer}"
                    thread_ref = _DocRef(
                        {"clientId": "client-1"},
                        doc_id=thread_id,
                        path=thread_path,
                    )
                    loaded = _pending_data(thread_id, token=token)
                    pending_id = f"pending-expired-consume-{issuer}"
                    pending_ref = _DocRef(
                        loaded,
                        doc_id=pending_id,
                        path=(
                            f"users/{user_id}/pendingResponses/{pending_id}"
                        ),
                    )
                    rooted.add_thread(thread_ref)
                    rooted.add_pending(pending_ref)
                    capability = send_permits.issue_pending_graph_send_permit(
                        rooted,
                        thread_ref,
                        pending_ref,
                        dict(loaded),
                        token,
                    )
                    source_id = loaded["msgId"]
                    conversation_id = loaded["conversationId"]
                    recipient = loaded["recipient"]
                    body = loaded["responseBody"]

                attachment = {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": f"expired-consume-{issuer}.png",
                    "contentType": "image/png",
                    "contentBytes": "ZXhwaXJlZC1jb25zdW1l",
                    "contentId": f"expired-consume-{issuer}-content",
                    "isInline": True,
                }
                draft_id = f"draft-expired-consume-{issuer}"
                stack, calls = self._local_transition_processing_stack(
                    rooted,
                    source_id=source_id,
                    conversation_id=conversation_id,
                    recipient=recipient,
                    draft_id=draft_id,
                    attachment=attachment,
                )

                def expire_prepared_permit(permit, now, **kwargs):
                    preparation = dict(permit.get("draftPreparation") or {})
                    if preparation.get("state") == "prepared":
                        raise send_permits.GraphSendPermitBlocked(
                            "Graph provider plan expired before provider mutation"
                        )
                    return real_remaining(permit, now, **kwargs)

                with stack, patch.object(
                    send_permits,
                    "_remaining_provider_seconds",
                    side_effect=expire_prepared_permit,
                ), patch.object(
                    email_module,
                    "_delete_graph_reply_draft",
                    side_effect=AssertionError(
                        "expired consume cannot delete a retained draft"
                    ),
                ) as delete_draft:
                    sent = processing.send_reply_in_thread(
                        user_id,
                        {"Authorization": "Bearer test"},
                        body,
                        source_id,
                        recipient,
                        thread_id,
                        graph_send_capability=capability,
                    )

                self.assertFalse(sent)
                delete_draft.assert_not_called()
                permit = send_permits.read_permit(capability)
                self.assertEqual("needs_reconciliation", permit["status"])
                self.assertEqual(
                    "prepared",
                    permit["draftPreparation"]["state"],
                )
                self.assertIsNone(permit.get("requestStartedAt"))
                self.assertEqual(
                    "draft_mutation_needs_reconciliation",
                    processing.send_reply_in_thread.last_outcome,
                )
                self.assertEqual(
                    {
                        "draftId",
                        "reason",
                        "phase",
                        "providerSendStarted",
                        "automaticDeleteAttempted",
                    },
                    set(permit["resolutionEvidence"]),
                )
                self.assertEqual(
                    draft_id,
                    permit["resolutionEvidence"]["draftId"],
                )
                self.assertFalse(
                    permit["resolutionEvidence"]["providerSendStarted"]
                )
                self.assertFalse(
                    permit["resolutionEvidence"]["automaticDeleteAttempted"]
                )
                self.assertEqual(
                    (1, 1, 1, 0),
                    (
                        sum(
                            kind == "post" and url.endswith("/createReplyAll")
                            for kind, url in calls
                        ),
                        sum(kind == "patch" for kind, _url in calls),
                        sum(
                            kind == "post" and url.endswith("/attachments")
                            for kind, url in calls
                        ),
                        sum(
                            kind == "post" and url.endswith("/send")
                            for kind, url in calls
                        ),
                    ),
                )

    def test_orphaned_draft_request_is_atomically_settled_to_exact_review(self):
        user_id = "uid-orphaned-draft-request"
        request_states = {
            "create_request_started": (
                "draft_create_needs_reconciliation",
                "create_reply",
                "createReplyAll",
            ),
            "patch_request_started": (
                "draft_patch_needs_reconciliation",
                "patch_draft",
                "PATCH",
            ),
            "attachment_request_started": (
                "draft_attachment_0_needs_reconciliation",
                "attach_draft",
                "attachment",
            ),
        }
        for issuer in ("terminal", "pending"):
            for request_state, recovery in request_states.items():
                with self.subTest(issuer=issuer, request_state=request_state):
                    recovery_event, expected_phase, reason_fragment = recovery
                    rooted = _CommitOutcomeRootedFirestore()
                    scenario = self._orphaned_draft_request_scenario(
                        firestore=rooted,
                        issuer=issuer,
                        request_state=request_state,
                        user_id=user_id,
                    )
                    permit_before = send_permits._validate_permit(
                        scenario["permit_ref"].data
                    )
                    history_before = list(permit_before["stateHistory"])
                    expected_review_id = (
                        f"pending-{permit_before['permitId']}"
                    )
                    if issuer == "terminal":
                        saga = scenario["saga"]
                        review_path = send_permits._terminal_review_path(
                            scenario["thread_ref"],
                            saga,
                            permit_before,
                            kind="draft_needs_review",
                        )
                        review_ref = rooted.user_root.collection(
                            "terminalGraphSendReviews"
                        ).document(review_path.rsplit("/", 1)[-1])
                        review_ref.path = review_path
                        owner = processing.TerminalSagaExecution(
                            owner="terminal-owner-a",
                            fencing_token=1,
                        )
                        with patch.object(
                            send_permits,
                            "datetime",
                            scenario["future_datetime"],
                        ), patch.object(
                            processing,
                            "_fs",
                            rooted,
                        ), patch.object(
                            processing,
                            "_renew_terminal_saga_execution",
                            return_value=datetime.now(timezone.utc),
                        ), patch.object(
                            processing,
                            "_clear_resolved_terminal_saga",
                        ), patch.object(
                            processing,
                            "find_exact_sent_message_by_immutable_id",
                            side_effect=AssertionError(
                                "orphan draft recovery cannot search Sent"
                            ),
                        ) as sent_lookup, patch.object(
                            processing,
                            "send_reply_in_thread",
                            side_effect=AssertionError(
                                "orphan draft recovery cannot replay provider work"
                            ),
                        ) as resend, patch.object(
                            email_module,
                            "_delete_graph_reply_draft",
                            side_effect=AssertionError(
                                "orphan draft recovery cannot delete provider work"
                            ),
                        ) as delete_draft:
                            outcome = processing._settle_terminal_reply_obligation(
                                user_id,
                                "client-1",
                                scenario["thread_ref"].id,
                                {"Authorization": "Bearer test"},
                                saga["replyRecipient"],
                                saga,
                                terminal_saga_owner=owner,
                            )
                        self.assertEqual("draft_needs_review", outcome)
                        self.assertFalse(
                            scenario["thread_ref"].data["terminalReplyOwed"]
                        )
                        sent_lookup.assert_not_called()
                        resend.assert_not_called()
                        delete_draft.assert_not_called()
                        settlement_event = (
                            "terminal_settled_draft_needs_review"
                        )
                    else:
                        review_path = send_permits._pending_draft_review_path(
                            scenario["thread_ref"],
                            permit_before["permitId"],
                        )
                        review_ref = rooted.user_root.collection(
                            "graphSendDraftReviews"
                        ).document(expected_review_id)
                        review_ref.path = review_path
                        doc = types.SimpleNamespace(
                            id=scenario["pending_ref"].id,
                            reference=scenario["pending_ref"],
                        )
                        with patch.object(
                            send_permits,
                            "datetime",
                            scenario["future_datetime"],
                        ), patch.object(
                            pending_responses,
                            "_pending_claim_refs",
                            return_value=(
                                rooted,
                                rooted.user_root,
                                scenario["thread_ref"],
                                scenario["pending_ref"],
                            ),
                        ), patch.object(
                            pending_responses,
                            "find_exact_sent_message_by_immutable_id",
                            side_effect=AssertionError(
                                "orphan draft recovery cannot search Sent"
                            ),
                        ) as sent_lookup, patch.object(
                            processing,
                            "send_reply_in_thread",
                            side_effect=AssertionError(
                                "orphan draft recovery cannot replay provider work"
                            ),
                        ) as resend, patch.object(
                            email_module,
                            "_delete_graph_reply_draft",
                            side_effect=AssertionError(
                                "orphan draft recovery cannot delete provider work"
                            ),
                        ) as delete_draft:
                            outcome = pending_responses._reconcile_expired_pending_permit(
                                user_id,
                                {"Authorization": "Bearer test"},
                                doc,
                                scenario["loaded"],
                            )
                        self.assertTrue(outcome)
                        self.assertFalse(scenario["pending_ref"].exists)
                        sent_lookup.assert_not_called()
                        resend.assert_not_called()
                        delete_draft.assert_not_called()
                        settlement_event = (
                            "pending_reconcile_draft_needs_review"
                        )

                    permit = send_permits._validate_permit(
                        scenario["permit_ref"].data
                    )
                    self.assertEqual(
                        "settled_draft_needs_review", permit["status"]
                    )
                    self.assertEqual(
                        "draft_mutation_needs_reconciliation",
                        permit["draftPreparation"]["state"],
                    )
                    self.assertIsNone(permit.get("requestStartedAt"))
                    self.assertEqual(
                        {
                            "reason",
                            "phase",
                            "draftId",
                            "providerSendStarted",
                            "automaticDeleteAttempted",
                        },
                        set(permit["resolutionEvidence"]),
                    )
                    self.assertEqual(
                        scenario["draft_id"],
                        permit["resolutionEvidence"]["draftId"],
                    )
                    self.assertEqual(
                        expected_phase,
                        permit["resolutionEvidence"]["phase"],
                    )
                    reason = permit["resolutionEvidence"]["reason"]
                    self.assertIn(reason_fragment, reason)
                    self.assertIn("orphaned", reason)
                    self.assertIn("lease expired", reason)
                    self.assertEqual(
                        send_permits._hash(permit["resolutionEvidence"]),
                        permit["resolutionEvidenceHash"],
                    )
                    self.assertFalse(
                        permit["resolutionEvidence"]["providerSendStarted"]
                    )
                    self.assertFalse(
                        permit["resolutionEvidence"][
                            "automaticDeleteAttempted"
                        ]
                    )
                    self.assertEqual(
                        len(history_before) + 2,
                        len(permit["stateHistory"]),
                    )
                    self.assertEqual(
                        [recovery_event, settlement_event],
                        [
                            event["event"]
                            for event in permit["stateHistory"][-2:]
                        ],
                    )
                    review = review_ref.get().to_dict()
                    self.assertTrue(review_ref.exists)
                    self.assertEqual("manual_review", review["status"])
                    self.assertTrue(review["authoritative"])
                    self.assertFalse(review["alreadySent"])
                    self.assertFalse(review["providerSendStarted"])
                    self.assertFalse(review["sendOutcomeUnknown"])
                    self.assertFalse(review["retryAllowed"])
                    self.assertEqual(
                        permit["resolutionEvidenceHash"],
                        review["draftResolutionEvidenceHash"],
                    )
                    self.assertIs(
                        review_ref,
                        permit["draftReviewEvidenceRef"],
                    )
                    self.assertEqual(
                        send_permits._stable_evidence_hash(review),
                        permit["draftReviewEvidenceHash"],
                    )

    def test_orphaned_draft_atomic_settlement_recovers_exact_commit_outcome(self):
        user_id = "uid-orphaned-draft-commit-recovery"
        request_states = (
            "create_request_started",
            "patch_request_started",
            "attachment_request_started",
        )
        for commit_outcome in ("no_apply", "apply_then_raise"):
            for issuer in ("terminal", "pending"):
                for request_state in request_states:
                    with self.subTest(
                        commit_outcome=commit_outcome,
                        issuer=issuer,
                        request_state=request_state,
                    ):
                        rooted = _CommitOutcomeRootedFirestore()
                        scenario = self._orphaned_draft_request_scenario(
                            firestore=rooted,
                            issuer=issuer,
                            request_state=request_state,
                            user_id=user_id,
                        )
                        permit_before = send_permits._validate_permit(
                            scenario["permit_ref"].data
                        )
                        history_before = len(permit_before["stateHistory"])
                        if issuer == "terminal":
                            saga = scenario["saga"]
                            review_path = send_permits._terminal_review_path(
                                scenario["thread_ref"],
                                saga,
                                permit_before,
                                kind="draft_needs_review",
                            )
                            review_ref = rooted.user_root.collection(
                                "terminalGraphSendReviews"
                            ).document(review_path.rsplit("/", 1)[-1])
                            review_ref.path = review_path
                        else:
                            review_path = send_permits._pending_draft_review_path(
                                scenario["thread_ref"],
                                permit_before["permitId"],
                            )
                            review_ref = rooted.user_root.collection(
                                "graphSendDraftReviews"
                            ).document(f"pending-{permit_before['permitId']}")
                            review_ref.path = review_path

                        attempts_before = rooted.commit_attempts
                        payloads_before = len(rooted.commit_payloads)
                        rooted.commit_outcomes = [commit_outcome]
                        if issuer == "terminal":
                            owner = processing.TerminalSagaExecution(
                                owner="terminal-owner-a",
                                fencing_token=1,
                            )
                            with patch.object(
                                send_permits,
                                "datetime",
                                scenario["future_datetime"],
                            ), patch.object(
                                processing,
                                "_fs",
                                rooted,
                            ), patch.object(
                                processing,
                                "_renew_terminal_saga_execution",
                                return_value=datetime.now(timezone.utc),
                            ), patch.object(
                                processing,
                                "_clear_resolved_terminal_saga",
                            ), patch.object(
                                processing,
                                "find_exact_sent_message_by_immutable_id",
                                side_effect=AssertionError(
                                    "orphan commit recovery cannot search Sent"
                                ),
                            ) as sent_lookup, patch.object(
                                processing,
                                "send_reply_in_thread",
                                side_effect=AssertionError(
                                    "orphan commit recovery cannot replay provider work"
                                ),
                            ) as resend, patch.object(
                                email_module,
                                "_delete_graph_reply_draft",
                                side_effect=AssertionError(
                                    "orphan commit recovery cannot delete a draft"
                                ),
                            ) as delete_draft:
                                outcome = processing._settle_terminal_reply_obligation(
                                    user_id,
                                    "client-1",
                                    scenario["thread_ref"].id,
                                    {"Authorization": "Bearer test"},
                                    saga["replyRecipient"],
                                    saga,
                                    terminal_saga_owner=owner,
                                )
                            self.assertEqual("draft_needs_review", outcome)
                        else:
                            doc = types.SimpleNamespace(
                                id=scenario["pending_ref"].id,
                                reference=scenario["pending_ref"],
                            )
                            with patch.object(
                                send_permits,
                                "datetime",
                                scenario["future_datetime"],
                            ), patch.object(
                                pending_responses,
                                "_pending_claim_refs",
                                return_value=(
                                    rooted,
                                    rooted.user_root,
                                    scenario["thread_ref"],
                                    scenario["pending_ref"],
                                ),
                            ), patch.object(
                                pending_responses,
                                "find_exact_sent_message_by_immutable_id",
                                side_effect=AssertionError(
                                    "orphan commit recovery cannot search Sent"
                                ),
                            ) as sent_lookup, patch.object(
                                processing,
                                "send_reply_in_thread",
                                side_effect=AssertionError(
                                    "orphan commit recovery cannot replay provider work"
                                ),
                            ) as resend, patch.object(
                                email_module,
                                "_delete_graph_reply_draft",
                                side_effect=AssertionError(
                                    "orphan commit recovery cannot delete a draft"
                                ),
                            ) as delete_draft:
                                outcome = pending_responses._reconcile_expired_pending_permit(
                                    user_id,
                                    {"Authorization": "Bearer test"},
                                    doc,
                                    scenario["loaded"],
                                )
                            self.assertTrue(outcome)

                        expected_attempts = (
                            2 if commit_outcome == "no_apply" else 1
                        )
                        self.assertEqual(
                            attempts_before + expected_attempts,
                            rooted.commit_attempts,
                        )
                        commit_payloads = rooted.commit_payloads[
                            payloads_before:
                        ]
                        self.assertEqual(
                            expected_attempts,
                            len(commit_payloads),
                        )
                        if commit_outcome == "no_apply":
                            self.assertEqual(
                                commit_payloads[0],
                                commit_payloads[1],
                            )
                        for payload in commit_payloads:
                            permit_writes = [
                                data
                                for operation, _path, document_id, data in payload
                                if operation == "update"
                                and document_id == permit_before["permitId"]
                            ]
                            self.assertEqual(1, len(permit_writes))
                            self.assertEqual(
                                "settled_draft_needs_review",
                                permit_writes[0]["status"],
                            )
                            self.assertEqual(
                                history_before + 2,
                                len(permit_writes[0]["stateHistory"]),
                            )
                        permit = send_permits._validate_permit(
                            scenario["permit_ref"].data
                        )
                        self.assertEqual(
                            "settled_draft_needs_review",
                            permit["status"],
                        )
                        self.assertEqual(
                            history_before + 2,
                            len(permit["stateHistory"]),
                        )
                        self.assertTrue(review_ref.exists)
                        self.assertEqual(
                            send_permits._stable_evidence_hash(
                                review_ref.data
                            ),
                            permit["draftReviewEvidenceHash"],
                        )
                        sent_lookup.assert_not_called()
                        resend.assert_not_called()
                        delete_draft.assert_not_called()

    def test_orphaned_draft_repeated_no_apply_retains_exact_source(self):
        user_id = "uid-orphaned-draft-repeated-no-apply"
        for issuer in ("terminal", "pending"):
            with self.subTest(issuer=issuer):
                rooted = _CommitOutcomeRootedFirestore()
                scenario = self._orphaned_draft_request_scenario(
                    firestore=rooted,
                    issuer=issuer,
                    request_state="patch_request_started",
                    user_id=user_id,
                )
                permit_before = copy.deepcopy(scenario["permit_ref"].data)
                thread_before = copy.deepcopy(scenario["thread_ref"].data)
                pending_before = (
                    copy.deepcopy(scenario["pending_ref"].data)
                    if scenario["pending_ref"] is not None
                    else None
                )
                attempts_before = rooted.commit_attempts
                payloads_before = len(rooted.commit_payloads)
                rooted.commit_outcomes = ["no_apply", "no_apply"]
                with patch.object(
                    send_permits,
                    "datetime",
                    scenario["future_datetime"],
                ), patch.object(
                    processing,
                    "find_exact_sent_message_by_immutable_id",
                    side_effect=AssertionError(
                        "repeated no-apply cannot search Sent"
                    ),
                ) as sent_lookup, patch.object(
                    processing.requests,
                    "post",
                    side_effect=AssertionError(
                        "repeated no-apply cannot replay provider work"
                    ),
                ) as provider_post, patch.object(
                    processing.requests,
                    "patch",
                    side_effect=AssertionError(
                        "repeated no-apply cannot replay provider work"
                    ),
                ) as provider_patch, patch.object(
                    email_module,
                    "_delete_graph_reply_draft",
                    side_effect=AssertionError(
                        "repeated no-apply cannot delete a draft"
                    ),
                ) as delete_draft:
                    with self.assertRaises(
                        send_permits.GraphSendPermitLocalRetryable
                    ):
                        self._invoke_orphaned_draft_atomic_settlement(
                            firestore=rooted,
                            scenario=scenario,
                            issuer=issuer,
                            user_id=user_id,
                        )

                self.assertEqual(
                    attempts_before + 2,
                    rooted.commit_attempts,
                )
                retry_payloads = rooted.commit_payloads[payloads_before:]
                self.assertEqual(2, len(retry_payloads))
                self.assertEqual(retry_payloads[0], retry_payloads[1])
                self.assertEqual(permit_before, scenario["permit_ref"].data)
                self.assertEqual(thread_before, scenario["thread_ref"].data)
                if issuer == "pending":
                    self.assertTrue(scenario["pending_ref"].exists)
                    self.assertEqual(
                        pending_before,
                        scenario["pending_ref"].data,
                    )
                    reviews = scenario["thread_ref"].collection(
                        "graphSendReviews"
                    ).docs.values()
                else:
                    reviews = rooted.user_root.collection(
                        "terminalGraphSendReviews"
                    ).docs.values()
                self.assertFalse(any(ref.exists for ref in reviews))
                self.assertEqual(
                    len(permit_before["stateHistory"]),
                    len(scenario["permit_ref"].data["stateHistory"]),
                )
                sent_lookup.assert_not_called()
                provider_post.assert_not_called()
                provider_patch.assert_not_called()
                delete_draft.assert_not_called()

    def test_orphaned_draft_apply_then_raise_corrupt_readback_fails_drift(self):
        user_id = "uid-orphaned-draft-corrupt-readback"
        for issuer in ("terminal", "pending"):
            with self.subTest(issuer=issuer):
                rooted = _CommitOutcomeRootedFirestore()
                scenario = self._orphaned_draft_request_scenario(
                    firestore=rooted,
                    issuer=issuer,
                    request_state="attachment_request_started",
                    user_id=user_id,
                )
                attempts_before = rooted.commit_attempts
                rooted.commit_outcomes = ["apply_then_raise"]

                def corrupt_settled_permit():
                    scenario["permit_ref"].data[
                        "stateHeadHash"
                    ] = "forged-orphan-settlement-head"
                    scenario["permit_ref"].version += 1

                rooted.after_apply = corrupt_settled_permit
                with patch.object(
                    send_permits,
                    "datetime",
                    scenario["future_datetime"],
                ), patch.object(
                    processing,
                    "find_exact_sent_message_by_immutable_id",
                    side_effect=AssertionError(
                        "corrupt readback cannot search Sent"
                    ),
                ) as sent_lookup, patch.object(
                    processing.requests,
                    "post",
                    side_effect=AssertionError(
                        "corrupt readback cannot replay provider work"
                    ),
                ) as provider_post, patch.object(
                    processing.requests,
                    "patch",
                    side_effect=AssertionError(
                        "corrupt readback cannot replay provider work"
                    ),
                ) as provider_patch, patch.object(
                    email_module,
                    "_delete_graph_reply_draft",
                    side_effect=AssertionError(
                        "corrupt readback cannot delete a draft"
                    ),
                ) as delete_draft:
                    with self.assertRaises(
                        send_permits.GraphSendPermitError
                    ) as raised:
                        self._invoke_orphaned_draft_atomic_settlement(
                            firestore=rooted,
                            scenario=scenario,
                            issuer=issuer,
                            user_id=user_id,
                        )

                self.assertNotIsInstance(
                    raised.exception,
                    send_permits.GraphSendPermitLocalRetryable,
                )
                self.assertEqual(
                    attempts_before + 1,
                    rooted.commit_attempts,
                )
                sent_lookup.assert_not_called()
                provider_post.assert_not_called()
                provider_patch.assert_not_called()
                delete_draft.assert_not_called()

    def test_orphaned_terminal_apply_then_raise_recovers_distinct_same_path_claim_ref(self):
        user_id = "uid-orphaned-terminal-distinct-claim-ref"
        rooted = _CommitOutcomeRootedFirestore()
        scenario = self._orphaned_draft_request_scenario(
            firestore=rooted,
            issuer="terminal",
            request_state="attachment_request_started",
            user_id=user_id,
        )
        thread_ref = scenario["thread_ref"]
        distinct_claim_ref = _AliasDocRef(thread_ref, thread_ref.path)
        self.assertIsNot(distinct_claim_ref, thread_ref)
        self.assertEqual(distinct_claim_ref.path, thread_ref.path)
        scenario["claim_ref"] = distinct_claim_ref
        history_before = len(scenario["permit_ref"].data["stateHistory"])
        attempts_before = rooted.commit_attempts
        rooted.commit_outcomes = ["apply_then_raise"]

        with patch.object(
            send_permits,
            "datetime",
            scenario["future_datetime"],
        ), patch.object(
            processing,
            "find_exact_sent_message_by_immutable_id",
            side_effect=AssertionError(
                "same-path claim recovery cannot search Sent"
            ),
        ) as sent_lookup, patch.object(
            processing.requests,
            "post",
            side_effect=AssertionError(
                "same-path claim recovery cannot replay provider work"
            ),
        ) as provider_post, patch.object(
            processing.requests,
            "patch",
            side_effect=AssertionError(
                "same-path claim recovery cannot replay provider work"
            ),
        ) as provider_patch, patch.object(
            email_module,
            "_delete_graph_reply_draft",
            side_effect=AssertionError(
                "same-path claim recovery cannot delete a draft"
            ),
        ) as delete_draft:
            review_ref = self._invoke_orphaned_draft_atomic_settlement(
                firestore=rooted,
                scenario=scenario,
                issuer="terminal",
                user_id=user_id,
            )

        permit = send_permits._validate_permit(
            scenario["permit_ref"].data
        )
        self.assertEqual(
            "settled_draft_needs_review",
            permit["status"],
        )
        self.assertEqual(
            history_before + 2,
            len(permit["stateHistory"]),
        )
        self.assertFalse(thread_ref.data["terminalReplyOwed"])
        self.assertEqual(
            "draft_needs_review",
            thread_ref.data["terminalReplyOutcome"],
        )
        self.assertTrue(review_ref.exists)
        self.assertEqual(attempts_before + 1, rooted.commit_attempts)
        sent_lookup.assert_not_called()
        provider_post.assert_not_called()
        provider_patch.assert_not_called()
        delete_draft.assert_not_called()

    def test_expired_lost_capability_pre_send_state_matrix_settles_without_provider_work(self):
        user_id = "uid-expired-lost-capability-pre-send-matrix"
        stable_states = (
            "draft_created",
            "patch_applied",
            "attachment_applied",
            "prepared",
        )
        matrix = (
            *(
                ("terminal", state)
                for state in ("issued_no_preparation", *stable_states)
            ),
            *(
                ("pending", state)
                for state in (
                    "issued_no_preparation",
                    *stable_states,
                    "create_definitely_not_created",
                )
            ),
        )
        for issuer, state in matrix:
            with self.subTest(issuer=issuer, state=state):
                rooted = _CommitOutcomeRootedFirestore()
                scenario = self._lost_capability_pre_send_scenario(
                    firestore=rooted,
                    issuer=issuer,
                    state=state,
                    user_id=user_id,
                )
                permit_before = send_permits._validate_permit(
                    scenario["permit_ref"].data
                )
                history_before = len(permit_before["stateHistory"])
                review_ref = None
                if issuer == "terminal":
                    if state in stable_states:
                        review_path = send_permits._terminal_review_path(
                            scenario["thread_ref"],
                            scenario["saga"],
                            permit_before,
                            kind="draft_needs_review",
                        )
                        review_ref = rooted.user_root.collection(
                            "terminalGraphSendReviews"
                        ).document(review_path.rsplit("/", 1)[-1])
                        review_ref.path = review_path
                    else:
                        terminal_pending_ref = rooted.user_root.collection(
                            "pendingResponses"
                        ).document(scenario["thread_ref"].id)
                        terminal_pending_ref.path = (
                            f"users/{user_id}/pendingResponses/"
                            f"{scenario['thread_ref'].id}"
                        )
                elif state in stable_states:
                    review_path = send_permits._pending_draft_review_path(
                        scenario["thread_ref"],
                        permit_before["permitId"],
                    )
                    review_ref = rooted.user_root.collection(
                        "graphSendDraftReviews"
                    ).document(f"pending-{permit_before['permitId']}")
                    review_ref.path = review_path
                else:
                    dead_letter_path = send_permits._pending_dead_letter_path(
                        scenario["thread_ref"],
                        scenario["pending_ref"],
                        permit_before,
                    )
                    dead_letter_ref = rooted.user_root.collection(
                        "deadLetterQueue"
                    ).document(dead_letter_path.rsplit("/", 1)[-1])
                    dead_letter_ref.path = dead_letter_path

                with patch.object(
                    send_permits,
                    "datetime",
                    scenario["future_datetime"],
                ), patch.object(
                    processing,
                    "_fs",
                    rooted,
                ), patch.object(
                    processing,
                    "_renew_terminal_saga_execution",
                    return_value=datetime.now(timezone.utc),
                ), patch.object(
                    processing,
                    "_clear_resolved_terminal_saga",
                ), patch.object(
                    pending_responses,
                    "_pending_claim_refs",
                    return_value=(
                        rooted,
                        rooted.user_root,
                        scenario["thread_ref"],
                        scenario["pending_ref"],
                    ),
                ), patch.object(
                    processing,
                    "find_exact_sent_message_by_immutable_id",
                    side_effect=AssertionError(
                        "lost capability recovery cannot search Sent"
                    ),
                ) as terminal_sent_lookup, patch.object(
                    pending_responses,
                    "find_exact_sent_message_by_immutable_id",
                    side_effect=AssertionError(
                        "lost capability recovery cannot search Sent"
                    ),
                ) as pending_sent_lookup, patch.object(
                    processing.requests,
                    "post",
                    side_effect=AssertionError(
                        "lost capability recovery cannot replay provider work"
                    ),
                ) as provider_post, patch.object(
                    processing.requests,
                    "patch",
                    side_effect=AssertionError(
                        "lost capability recovery cannot replay provider work"
                    ),
                ) as provider_patch, patch.object(
                    email_module,
                    "_delete_graph_reply_draft",
                    side_effect=AssertionError(
                        "lost capability recovery cannot delete a draft"
                    ),
                ) as delete_draft:
                    if issuer == "terminal":
                        outcome = processing._settle_terminal_reply_obligation(
                            user_id,
                            "client-1",
                            scenario["thread_ref"].id,
                            {"Authorization": "Bearer test"},
                            scenario["saga"]["replyRecipient"],
                            scenario["saga"],
                            terminal_saga_owner=(
                                processing.TerminalSagaExecution(
                                    owner="terminal-owner-a",
                                    fencing_token=1,
                                )
                            ),
                        )
                    else:
                        doc = types.SimpleNamespace(
                            id=scenario["pending_ref"].id,
                            reference=scenario["pending_ref"],
                        )
                        outcome = (
                            pending_responses._reconcile_expired_pending_permit(
                                user_id,
                                {"Authorization": "Bearer test"},
                                doc,
                                scenario["loaded"],
                            )
                        )

                permit = send_permits._validate_permit(
                    scenario["permit_ref"].data
                )
                if state in stable_states:
                    if issuer == "terminal":
                        self.assertEqual("draft_needs_review", outcome)
                    else:
                        self.assertTrue(outcome)
                    self.assertEqual(
                        "settled_draft_needs_review",
                        permit["status"],
                    )
                    self.assertEqual(
                        history_before + 2,
                        len(permit["stateHistory"]),
                    )
                    self.assertTrue(review_ref.exists)
                    self.assertEqual(
                        send_permits._stable_evidence_hash(review_ref.data),
                        permit["draftReviewEvidenceHash"],
                    )
                else:
                    if issuer == "terminal":
                        self.assertEqual("queued_retry", outcome)
                        self.assertTrue(terminal_pending_ref.exists)
                        self.assertFalse(
                            scenario["thread_ref"].data[
                                "terminalReplyOwed"
                            ]
                        )
                    else:
                        self.assertTrue(outcome)
                        self.assertTrue(scenario["pending_ref"].exists)
                        self.assertEqual(
                            "queued",
                            scenario["pending_ref"].data["status"],
                        )
                    self.assertEqual(
                        "settled_definitely_not_sent",
                        permit["status"],
                    )
                    expected_history_delta = (
                        2
                        if issuer == "terminal"
                        and state == "issued_no_preparation"
                        else 1
                    )
                    self.assertEqual(
                        history_before + expected_history_delta,
                        len(permit["stateHistory"]),
                    )
                terminal_sent_lookup.assert_not_called()
                pending_sent_lookup.assert_not_called()
                provider_post.assert_not_called()
                provider_patch.assert_not_called()
                delete_draft.assert_not_called()

    def test_lost_capability_pre_send_atomic_commit_recovers_no_apply_and_apply_then_raise(self):
        user_id = "uid-lost-capability-atomic-commit"
        stable_states = (
            "draft_created",
            "patch_applied",
            "attachment_applied",
            "prepared",
        )
        matrix = (
            *(
                ("terminal", state)
                for state in ("issued_no_preparation", *stable_states)
            ),
            *(
                ("pending", state)
                for state in (
                    "issued_no_preparation",
                    *stable_states,
                    "create_definitely_not_created",
                )
            ),
        )
        for commit_outcome in ("no_apply", "apply_then_raise"):
            for issuer, state in matrix:
                with self.subTest(
                    commit_outcome=commit_outcome,
                    issuer=issuer,
                    state=state,
                ):
                    rooted = _CommitOutcomeRootedFirestore()
                    scenario = self._lost_capability_pre_send_scenario(
                        firestore=rooted,
                        issuer=issuer,
                        state=state,
                        user_id=user_id,
                    )
                    permit_before = copy.deepcopy(
                        scenario["permit_ref"].data
                    )
                    history_before = len(permit_before["stateHistory"])
                    payloads_before = len(rooted.commit_payloads)
                    rooted.commit_outcomes = [commit_outcome]

                    outcome, side_ref = (
                        self._invoke_lost_capability_pre_send_recovery(
                            firestore=rooted,
                            scenario=scenario,
                            issuer=issuer,
                            state=state,
                            user_id=user_id,
                        )
                    )

                    permit = send_permits._validate_permit(
                        scenario["permit_ref"].data
                    )
                    expected_status = (
                        "settled_draft_needs_review"
                        if state in stable_states
                        else "settled_definitely_not_sent"
                    )
                    expected_history_delta = (
                        2
                        if (
                            state in stable_states
                            or (
                                issuer == "terminal"
                                and state == "issued_no_preparation"
                            )
                        )
                        else 1
                    )
                    self.assertEqual(expected_status, permit["status"])
                    self.assertEqual(
                        history_before + expected_history_delta,
                        len(permit["stateHistory"]),
                    )
                    self.assertTrue(side_ref.exists)
                    if issuer == "terminal":
                        self.assertEqual(
                            (
                                "draft_needs_review"
                                if state in stable_states
                                else "queued_retry"
                            ),
                            outcome,
                        )
                    else:
                        self.assertTrue(outcome)

                    settlement_payloads = []
                    for payload in rooted.commit_payloads[
                        payloads_before:
                    ]:
                        if any(
                            operation == "update"
                            and document_id == permit_before["permitId"]
                            and data.get("status") == expected_status
                            for operation, _path, document_id, data in payload
                        ):
                            settlement_payloads.append(payload)
                    expected_atomic_attempts = (
                        2 if commit_outcome == "no_apply" else 1
                    )
                    self.assertEqual(
                        expected_atomic_attempts,
                        len(settlement_payloads),
                    )
                    if commit_outcome == "no_apply":
                        self.assertEqual(
                            settlement_payloads[0],
                            settlement_payloads[1],
                        )

    def test_lost_capability_pre_send_repeated_no_apply_retains_exact_source(self):
        user_id = "uid-lost-capability-repeated-no-apply"
        cases = (
            ("terminal", "issued_no_preparation"),
            ("terminal", "prepared"),
            ("pending", "issued_no_preparation"),
            ("pending", "prepared"),
            ("pending", "create_definitely_not_created"),
        )
        for issuer, state in cases:
            with self.subTest(issuer=issuer, state=state):
                rooted = _CommitOutcomeRootedFirestore()
                scenario = self._lost_capability_pre_send_scenario(
                    firestore=rooted,
                    issuer=issuer,
                    state=state,
                    user_id=user_id,
                )
                side_ref = self._lost_capability_side_ref(
                    firestore=rooted,
                    scenario=scenario,
                    issuer=issuer,
                    state=state,
                    user_id=user_id,
                )
                permit_before = copy.deepcopy(
                    scenario["permit_ref"].data
                )
                thread_before = copy.deepcopy(
                    scenario["thread_ref"].data
                )
                pending_before = (
                    copy.deepcopy(scenario["pending_ref"].data)
                    if scenario["pending_ref"] is not None
                    else None
                )
                payloads_before = len(rooted.commit_payloads)
                rooted.commit_outcomes = ["no_apply", "no_apply"]

                if issuer == "terminal":
                    with self.assertRaises(
                        processing.RetryableProcessingError
                    ) as raised:
                        self._invoke_lost_capability_pre_send_recovery(
                            firestore=rooted,
                            scenario=scenario,
                            issuer=issuer,
                            state=state,
                            user_id=user_id,
                        )
                    self.assertIsInstance(
                        raised.exception.__cause__,
                        send_permits.GraphSendPermitLocalRetryable,
                    )
                else:
                    with self.assertRaises(
                        send_permits.GraphSendPermitLocalRetryable
                    ):
                        self._invoke_lost_capability_pre_send_recovery(
                            firestore=rooted,
                            scenario=scenario,
                            issuer=issuer,
                            state=state,
                            user_id=user_id,
                        )

                retry_payloads = rooted.commit_payloads[payloads_before:]
                self.assertEqual(2, len(retry_payloads))
                self.assertEqual(retry_payloads[0], retry_payloads[1])
                self.assertEqual(
                    permit_before,
                    scenario["permit_ref"].data,
                )
                self.assertEqual(
                    thread_before,
                    scenario["thread_ref"].data,
                )
                if issuer == "pending":
                    self.assertTrue(scenario["pending_ref"].exists)
                    self.assertEqual(
                        pending_before,
                        scenario["pending_ref"].data,
                    )
                self.assertFalse(side_ref.exists)

    def test_lost_capability_pre_send_corrupt_target_readback_fails_closed(self):
        user_id = "uid-lost-capability-corrupt-readback"
        cases = (
            ("terminal", "issued_no_preparation"),
            ("terminal", "prepared"),
            ("pending", "issued_no_preparation"),
            ("pending", "prepared"),
            ("pending", "create_definitely_not_created"),
        )
        for issuer, state in cases:
            with self.subTest(issuer=issuer, state=state):
                rooted = _CommitOutcomeRootedFirestore()
                scenario = self._lost_capability_pre_send_scenario(
                    firestore=rooted,
                    issuer=issuer,
                    state=state,
                    user_id=user_id,
                )
                side_ref = self._lost_capability_side_ref(
                    firestore=rooted,
                    scenario=scenario,
                    issuer=issuer,
                    state=state,
                    user_id=user_id,
                )
                attempts_before = rooted.commit_attempts
                rooted.commit_outcomes = ["apply_then_raise"]

                def corrupt_atomic_side_document():
                    side_ref.data["status"] = "forged-settlement-target"
                    side_ref.version += 1

                rooted.after_apply = corrupt_atomic_side_document
                if issuer == "terminal":
                    with self.assertRaises(
                        processing.RetryableProcessingError
                    ) as raised:
                        self._invoke_lost_capability_pre_send_recovery(
                            firestore=rooted,
                            scenario=scenario,
                            issuer=issuer,
                            state=state,
                            user_id=user_id,
                        )
                    self.assertIsInstance(
                        raised.exception.__cause__,
                        send_permits.GraphSendPermitError,
                    )
                    self.assertNotIsInstance(
                        raised.exception.__cause__,
                        send_permits.GraphSendPermitLocalRetryable,
                    )
                else:
                    with self.assertRaises(
                        send_permits.GraphSendPermitError
                    ) as raised:
                        self._invoke_lost_capability_pre_send_recovery(
                            firestore=rooted,
                            scenario=scenario,
                            issuer=issuer,
                            state=state,
                            user_id=user_id,
                        )
                    self.assertNotIsInstance(
                        raised.exception,
                        send_permits.GraphSendPermitLocalRetryable,
                    )
                self.assertEqual(
                    attempts_before + 1,
                    rooted.commit_attempts,
                )

    def test_terminal_no_prep_existing_pending_updated_at_drift_is_not_exact_target(self):
        user_id = "uid-terminal-no-prep-existing-pending-drift"
        rooted = _CommitOutcomeRootedFirestore()
        scenario = self._lost_capability_pre_send_scenario(
            firestore=rooted,
            issuer="terminal",
            state="issued_no_preparation",
            user_id=user_id,
        )
        pending_ref = self._lost_capability_side_ref(
            firestore=rooted,
            scenario=scenario,
            issuer="terminal",
            state="issued_no_preparation",
            user_id=user_id,
        )
        prior_updated_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        pending_ref.data = {
            "threadId": scenario["thread_ref"].id,
            "msgId": scenario["saga"]["sourceGraphMessageId"],
            "recipient": scenario["saga"]["replyRecipient"],
            "responseBody": scenario["saga"]["responseBody"],
            "clientId": scenario["saga"]["clientId"],
            "conversationId": scenario["saga"]["sourceConversationId"],
            "attempts": 1,
            "createdAt": prior_updated_at - timedelta(minutes=1),
            "updatedAt": prior_updated_at,
        }
        pending_ref.exists = True
        pending_ref.version += 1
        attempts_before = rooted.commit_attempts
        rooted.commit_outcomes = ["apply_then_raise"]

        def drift_untouched_pending_timestamp():
            pending_ref.data["updatedAt"] = prior_updated_at + timedelta(
                seconds=1
            )
            pending_ref.version += 1

        rooted.after_apply = drift_untouched_pending_timestamp
        with self.assertRaises(
            processing.RetryableProcessingError
        ) as raised:
            self._invoke_lost_capability_pre_send_recovery(
                firestore=rooted,
                scenario=scenario,
                issuer="terminal",
                state="issued_no_preparation",
                user_id=user_id,
            )

        self.assertIsInstance(
            raised.exception.__cause__,
            send_permits.GraphSendPermitError,
        )
        self.assertEqual(attempts_before + 1, rooted.commit_attempts)

    def test_pending_preflight_definitely_unsent_lost_ack_requeues_without_provider_work(self):
        user_id = "uid-pending-preflight-definitely-unsent-lost-ack"
        rooted = _CommitOutcomeRootedFirestore()
        scenario = self._lost_capability_pre_send_scenario(
            firestore=rooted,
            issuer="pending",
            state="issued_no_preparation",
            user_id=user_id,
        )
        rooted.commit_outcomes = ["apply_then_raise"]
        with self.assertRaises(RuntimeError):
            send_permits.resolve_graph_send_permit(
                scenario["capability"],
                "definitely_not_sent",
                evidence={
                    "reason": "campaign gate closed before provider work",
                    "phase": "preflight_campaign_gate",
                    "providerSendStarted": False,
                },
            )
        permit_before = send_permits._validate_permit(
            scenario["permit_ref"].data
        )
        self.assertEqual("definitely_not_sent", permit_before["status"])
        self.assertIsNone(permit_before.get("draftPreparation"))
        self.assertIsNone(permit_before.get("requestStartedAt"))
        history_before = len(permit_before["stateHistory"])

        outcome, evidence_ref = (
            self._invoke_lost_capability_pre_send_recovery(
                firestore=rooted,
                scenario=scenario,
                issuer="pending",
                state="issued_no_preparation",
                user_id=user_id,
            )
        )

        permit = send_permits._validate_permit(
            scenario["permit_ref"].data
        )
        self.assertTrue(outcome)
        self.assertEqual("settled_definitely_not_sent", permit["status"])
        self.assertEqual(history_before + 1, len(permit["stateHistory"]))
        self.assertTrue(scenario["pending_ref"].exists)
        self.assertEqual("queued", scenario["pending_ref"].data["status"])
        self.assertTrue(evidence_ref.exists)
        self.assertFalse(
            any(
                ref.exists
                for ref in scenario["thread_ref"].collection(
                    "graphSendReviews"
                ).docs.values()
            )
        )

    def test_terminal_pre_resolved_definitely_unsent_recovers_commit_outcomes_and_replay(self):
        shapes = (
            "preflight_no_preparation",
            "create_definitely_not_created",
        )
        for shape in shapes:
            for commit_outcome, expected_atomic_attempts in (
                ("no_apply", 2),
                ("apply_then_raise", 1),
            ):
                with self.subTest(
                    shape=shape,
                    commit_outcome=commit_outcome,
                ):
                    rooted = _CommitOutcomeRootedFirestore()
                    scenario, state = (
                        self._terminal_pre_resolved_definitely_unsent_scenario(
                            firestore=rooted,
                            shape=shape,
                            user_id="uid-terminal-pre-resolved-recovery",
                        )
                    )
                    source_permit = copy.deepcopy(
                        scenario["permit_ref"].data
                    )
                    source_revision = source_permit["stateRevision"]
                    source_history = copy.deepcopy(
                        source_permit["stateHistory"]
                    )
                    source_pointer = copy.deepcopy(
                        scenario["thread_ref"].data[
                            "activeGraphSendPermit"
                        ]
                    )
                    source_claim = copy.deepcopy(
                        scenario["thread_ref"].data["terminalSagaClaim"]
                    )
                    payloads_before = len(rooted.commit_payloads)
                    rooted.commit_outcomes = [commit_outcome]

                    outcome, pending_ref = (
                        self._invoke_lost_capability_pre_send_recovery(
                            firestore=rooted,
                            scenario=scenario,
                            issuer="terminal",
                            state=state,
                            user_id="uid-terminal-pre-resolved-recovery",
                        )
                    )

                    permit = send_permits._validate_permit(
                        scenario["permit_ref"].data
                    )
                    self.assertEqual("queued_retry", outcome)
                    self.assertEqual(
                        "settled_definitely_not_sent",
                        permit["status"],
                    )
                    self.assertEqual(
                        source_revision + 1,
                        permit["stateRevision"],
                    )
                    self.assertEqual(
                        source_history,
                        permit["stateHistory"][:-1],
                    )
                    for field in (
                        "permitId",
                        "immutableHash",
                        "issuerKind",
                        "issuerOwner",
                        "issuerFence",
                        "issuerDocumentId",
                        "issuerDocumentPath",
                    ):
                        self.assertEqual(
                            source_permit[field],
                            permit[field],
                        )
                    self.assertEqual(
                        source_pointer,
                        scenario["thread_ref"].data[
                            "activeGraphSendPermit"
                        ],
                    )
                    self.assertEqual(
                        source_claim,
                        scenario["thread_ref"].data["terminalSagaClaim"],
                    )
                    committed_attempt = scenario["thread_ref"].data[
                        "terminalReplyAttempt"
                    ]
                    self.assertEqual(
                        source_permit["permitId"],
                        committed_attempt["graphSendPermitId"],
                    )
                    self.assertEqual(
                        source_permit["immutableHash"],
                        committed_attempt["graphSendPermitHash"],
                    )
                    self.assertTrue(pending_ref.exists)

                    settlement_payloads = [
                        payload
                        for payload in rooted.commit_payloads[
                            payloads_before:
                        ]
                        if any(
                            operation == "update"
                            and document_id == source_permit["permitId"]
                            and data.get("status")
                            == "settled_definitely_not_sent"
                            for operation, _path, document_id, data in payload
                        )
                    ]
                    self.assertEqual(
                        expected_atomic_attempts,
                        len(settlement_payloads),
                    )
                    if commit_outcome == "no_apply":
                        self.assertEqual(
                            settlement_payloads[0],
                            settlement_payloads[1],
                        )

                    target_permit = copy.deepcopy(
                        scenario["permit_ref"].data
                    )
                    target_thread = copy.deepcopy(
                        scenario["thread_ref"].data
                    )
                    target_pending = copy.deepcopy(pending_ref.data)
                    attempts_before_replay = rooted.commit_attempts
                    replay_outcome, replay_pending_ref = (
                        self._invoke_lost_capability_pre_send_recovery(
                            firestore=rooted,
                            scenario=scenario,
                            issuer="terminal",
                            state=state,
                            user_id="uid-terminal-pre-resolved-recovery",
                        )
                    )
                    self.assertEqual("queued_retry", replay_outcome)
                    self.assertEqual(
                        attempts_before_replay,
                        rooted.commit_attempts,
                    )
                    self.assertEqual(
                        target_permit,
                        scenario["permit_ref"].data,
                    )
                    self.assertEqual(
                        target_thread,
                        scenario["thread_ref"].data,
                    )
                    self.assertEqual(target_pending, replay_pending_ref.data)

    def test_terminal_padded_definite_unsent_queues_canonical_once_and_replays(self):
        rooted = _ReadBeforeWriteRootedFirestore()
        canonical_body = "Thank you. We will close this property review."
        padded_body = f" \n  {canonical_body}\t "
        scenario, state = (
            self._terminal_pre_resolved_definitely_unsent_scenario(
                firestore=rooted,
                shape="preflight_no_preparation",
                user_id="uid-terminal-padded-definite-unsent",
                terminal_response_body=padded_body,
            )
        )

        outcome, pending_ref = (
            self._invoke_lost_capability_pre_send_recovery(
                firestore=rooted,
                scenario=scenario,
                issuer="terminal",
                state=state,
                user_id="uid-terminal-padded-definite-unsent",
            )
        )

        self.assertEqual("queued_retry", outcome)
        self.assertTrue(pending_ref.exists)
        self.assertEqual(canonical_body, pending_ref.data["responseBody"])
        pending_hash = send_permits.pending_envelope_hash(pending_ref.data)
        self.assertEqual(1, len([
            ref
            for ref in rooted.user_root.collection(
                "pendingResponses"
            ).docs.values()
            if ref.exists
        ]))
        permit_before_replay = copy.deepcopy(
            scenario["permit_ref"].data
        )
        thread_before_replay = copy.deepcopy(
            scenario["thread_ref"].data
        )
        pending_before_replay = copy.deepcopy(pending_ref.data)
        attempts_before_replay = rooted.commit_attempts

        replay_outcome, replay_pending_ref = (
            self._invoke_lost_capability_pre_send_recovery(
                firestore=rooted,
                scenario=scenario,
                issuer="terminal",
                state=state,
                user_id="uid-terminal-padded-definite-unsent",
            )
        )

        self.assertEqual("queued_retry", replay_outcome)
        self.assertEqual(attempts_before_replay, rooted.commit_attempts)
        self.assertEqual(permit_before_replay, scenario["permit_ref"].data)
        self.assertEqual(thread_before_replay, scenario["thread_ref"].data)
        self.assertEqual(pending_before_replay, replay_pending_ref.data)
        self.assertEqual(
            pending_hash,
            send_permits.pending_envelope_hash(replay_pending_ref.data),
        )
        self.assertEqual(1, len([
            ref
            for ref in rooted.user_root.collection(
                "pendingResponses"
            ).docs.values()
            if ref.exists
        ]))

    def test_terminal_pre_resolved_definitely_unsent_repeated_no_apply_is_typed(self):
        for shape in (
            "preflight_no_preparation",
            "create_definitely_not_created",
        ):
            with self.subTest(shape=shape):
                rooted = _CommitOutcomeRootedFirestore()
                scenario, state = (
                    self._terminal_pre_resolved_definitely_unsent_scenario(
                        firestore=rooted,
                        shape=shape,
                        user_id="uid-terminal-pre-resolved-no-apply",
                    )
                )
                pending_ref = self._lost_capability_side_ref(
                    firestore=rooted,
                    scenario=scenario,
                    issuer="terminal",
                    state=state,
                    user_id="uid-terminal-pre-resolved-no-apply",
                )
                source_permit = copy.deepcopy(
                    scenario["permit_ref"].data
                )
                source_thread = copy.deepcopy(
                    scenario["thread_ref"].data
                )
                payloads_before = len(rooted.commit_payloads)
                rooted.commit_outcomes = ["no_apply", "no_apply"]

                with self.assertRaises(
                    processing.RetryableProcessingError
                ) as raised:
                    self._invoke_lost_capability_pre_send_recovery(
                        firestore=rooted,
                        scenario=scenario,
                        issuer="terminal",
                        state=state,
                        user_id="uid-terminal-pre-resolved-no-apply",
                    )

                self.assertIsInstance(
                    raised.exception.__cause__,
                    send_permits.GraphSendPermitLocalRetryable,
                )
                retry_payloads = rooted.commit_payloads[payloads_before:]
                self.assertEqual(2, len(retry_payloads))
                self.assertEqual(retry_payloads[0], retry_payloads[1])
                self.assertEqual(
                    source_permit,
                    scenario["permit_ref"].data,
                )
                self.assertEqual(
                    source_thread,
                    scenario["thread_ref"].data,
                )
                self.assertFalse(pending_ref.exists)

    def test_terminal_pre_resolved_definitely_unsent_corrupt_target_fails_closed(self):
        for shape in (
            "preflight_no_preparation",
            "create_definitely_not_created",
        ):
            with self.subTest(shape=shape):
                rooted = _CommitOutcomeRootedFirestore()
                scenario, state = (
                    self._terminal_pre_resolved_definitely_unsent_scenario(
                        firestore=rooted,
                        shape=shape,
                        user_id="uid-terminal-pre-resolved-drift",
                    )
                )
                pending_ref = self._lost_capability_side_ref(
                    firestore=rooted,
                    scenario=scenario,
                    issuer="terminal",
                    state=state,
                    user_id="uid-terminal-pre-resolved-drift",
                )
                attempts_before = rooted.commit_attempts
                rooted.commit_outcomes = ["apply_then_raise"]

                def corrupt_pending_target():
                    pending_ref.data["status"] = "forged-target"
                    pending_ref.version += 1

                rooted.after_apply = corrupt_pending_target
                with self.assertRaises(
                    processing.RetryableProcessingError
                ) as raised:
                    self._invoke_lost_capability_pre_send_recovery(
                        firestore=rooted,
                        scenario=scenario,
                        issuer="terminal",
                        state=state,
                        user_id="uid-terminal-pre-resolved-drift",
                    )

                self.assertIsInstance(
                    raised.exception.__cause__,
                    send_permits.GraphSendPermitError,
                )
                self.assertNotIsInstance(
                    raised.exception.__cause__,
                    send_permits.GraphSendPermitLocalRetryable,
                )
                self.assertEqual(
                    attempts_before + 1,
                    rooted.commit_attempts,
                )

    def test_orphaned_draft_recovery_requires_expired_permit_and_current_fence(self):
        user_id = "uid-orphaned-draft-negative-lease"
        for issuer in ("terminal", "pending"):
            with self.subTest(issuer=issuer, condition="active_permit"):
                rooted = _CommitOutcomeRootedFirestore()
                scenario = self._orphaned_draft_request_scenario(
                    firestore=rooted,
                    issuer=issuer,
                    request_state="patch_request_started",
                    user_id=user_id,
                )
                before = copy.deepcopy(scenario["permit_ref"].data)
                if issuer == "terminal":
                    saga = scenario["saga"]
                    owner = processing.TerminalSagaExecution(
                        owner="terminal-owner-a",
                        fencing_token=1,
                    )
                    with patch.object(
                        processing,
                        "_fs",
                        rooted,
                    ), patch.object(
                        processing,
                        "_renew_terminal_saga_execution",
                        return_value=datetime.now(timezone.utc),
                    ), patch.object(
                        processing,
                        "find_exact_sent_message_by_immutable_id",
                        side_effect=AssertionError(
                            "active orphan candidate cannot search Sent"
                        ),
                    ) as sent_lookup, patch.object(
                        processing,
                        "send_reply_in_thread",
                        side_effect=AssertionError(
                            "active orphan candidate cannot replay provider work"
                        ),
                    ) as resend, patch.object(
                        email_module,
                        "_delete_graph_reply_draft",
                        side_effect=AssertionError(
                            "active orphan candidate cannot delete a draft"
                        ),
                    ) as delete_draft:
                        with self.assertRaisesRegex(
                            processing.RetryableProcessingError,
                            "duplicate send|unresolved",
                        ):
                            processing._settle_terminal_reply_obligation(
                                user_id,
                                "client-1",
                                scenario["thread_ref"].id,
                                {"Authorization": "Bearer test"},
                                saga["replyRecipient"],
                                saga,
                                terminal_saga_owner=owner,
                            )
                else:
                    doc = types.SimpleNamespace(
                        id=scenario["pending_ref"].id,
                        reference=scenario["pending_ref"],
                    )
                    with patch.object(
                        pending_responses,
                        "_pending_claim_refs",
                        return_value=(
                            rooted,
                            rooted.user_root,
                            scenario["thread_ref"],
                            scenario["pending_ref"],
                        ),
                    ), patch.object(
                        pending_responses,
                        "find_exact_sent_message_by_immutable_id",
                        side_effect=AssertionError(
                            "active orphan candidate cannot search Sent"
                        ),
                    ) as sent_lookup, patch.object(
                        processing,
                        "send_reply_in_thread",
                        side_effect=AssertionError(
                            "active orphan candidate cannot replay provider work"
                        ),
                    ) as resend, patch.object(
                        email_module,
                        "_delete_graph_reply_draft",
                        side_effect=AssertionError(
                            "active orphan candidate cannot delete a draft"
                        ),
                    ) as delete_draft:
                        self.assertFalse(
                            pending_responses._reconcile_expired_pending_permit(
                                user_id,
                                {"Authorization": "Bearer test"},
                                doc,
                                scenario["loaded"],
                            )
                        )
                self.assertEqual(before, scenario["permit_ref"].data)
                self.assertFalse(
                    any(
                        ref.exists
                        for ref in scenario["thread_ref"].collection(
                            "graphSendReviews"
                        ).docs.values()
                    )
                )
                sent_lookup.assert_not_called()
                resend.assert_not_called()
                delete_draft.assert_not_called()

        rooted = _CommitOutcomeRootedFirestore()
        scenario = self._orphaned_draft_request_scenario(
            firestore=rooted,
            issuer="terminal",
            request_state="attachment_request_started",
            user_id=user_id,
        )
        before = copy.deepcopy(scenario["permit_ref"].data)
        claim = scenario["thread_ref"].data["terminalSagaClaim"]
        claim.update({"owner": "terminal-owner-b", "fencingToken": 2})
        scenario["thread_ref"].version += 1
        old_owner = processing.TerminalSagaExecution(
            owner="terminal-owner-a",
            fencing_token=1,
        )
        with patch.object(
            send_permits,
            "datetime",
            scenario["future_datetime"],
        ), patch.object(
            processing,
            "_fs",
            rooted,
        ), patch.object(
            processing,
            "_renew_terminal_saga_execution",
            return_value=datetime.now(timezone.utc),
        ), patch.object(
            processing,
            "find_exact_sent_message_by_immutable_id",
            side_effect=AssertionError("stale fence cannot search Sent"),
        ) as sent_lookup, patch.object(
            processing,
            "send_reply_in_thread",
            side_effect=AssertionError("stale fence cannot replay provider work"),
        ) as resend, patch.object(
            email_module,
            "_delete_graph_reply_draft",
            side_effect=AssertionError("stale fence cannot delete a draft"),
        ) as delete_draft:
            with self.assertRaisesRegex(
                processing.RetryableProcessingError,
                "ownership|claim|CAS failed",
            ):
                processing._settle_terminal_reply_obligation(
                    user_id,
                    "client-1",
                    scenario["thread_ref"].id,
                    {"Authorization": "Bearer test"},
                    scenario["saga"]["replyRecipient"],
                    scenario["saga"],
                    terminal_saga_owner=old_owner,
                )
        self.assertEqual(before, scenario["permit_ref"].data)
        sent_lookup.assert_not_called()
        resend.assert_not_called()
        delete_draft.assert_not_called()

        rooted = _CommitOutcomeRootedFirestore()
        scenario = self._orphaned_draft_request_scenario(
            firestore=rooted,
            issuer="terminal",
            request_state="attachment_request_started",
            user_id=user_id,
        )
        claim = scenario["thread_ref"].data["terminalSagaClaim"]
        claim.update({"owner": "terminal-owner-b", "fencingToken": 2})
        scenario["thread_ref"].version += 1
        successor = processing.TerminalSagaExecution(
            owner="terminal-owner-b",
            fencing_token=2,
        )
        permit_before = send_permits._validate_permit(
            scenario["permit_ref"].data
        )
        review_path = send_permits._terminal_review_path(
            scenario["thread_ref"],
            scenario["saga"],
            permit_before,
            kind="draft_needs_review",
        )
        review_ref = rooted.user_root.collection(
            "terminalGraphSendReviews"
        ).document(review_path.rsplit("/", 1)[-1])
        review_ref.path = review_path
        with patch.object(
            send_permits,
            "datetime",
            scenario["future_datetime"],
        ), patch.object(
            processing,
            "_fs",
            rooted,
        ), patch.object(
            processing,
            "_renew_terminal_saga_execution",
            return_value=datetime.now(timezone.utc),
        ), patch.object(
            processing,
            "_clear_resolved_terminal_saga",
        ), patch.object(
            processing,
            "find_exact_sent_message_by_immutable_id",
            side_effect=AssertionError("successor cannot search Sent"),
        ) as sent_lookup, patch.object(
            processing,
            "send_reply_in_thread",
            side_effect=AssertionError("successor cannot replay provider work"),
        ) as resend, patch.object(
            email_module,
            "_delete_graph_reply_draft",
            side_effect=AssertionError("successor cannot delete a draft"),
        ) as delete_draft:
            outcome = processing._settle_terminal_reply_obligation(
                user_id,
                "client-1",
                scenario["thread_ref"].id,
                {"Authorization": "Bearer test"},
                scenario["saga"]["replyRecipient"],
                scenario["saga"],
                terminal_saga_owner=successor,
            )
        self.assertEqual("draft_needs_review", outcome)
        self.assertEqual(
            "settled_draft_needs_review",
            send_permits._validate_permit(
                scenario["permit_ref"].data
            )["status"],
        )
        self.assertTrue(review_ref.exists)
        sent_lookup.assert_not_called()
        resend.assert_not_called()
        delete_draft.assert_not_called()

    def test_orphaned_draft_recovery_rejects_typed_hash_and_issuer_drift(self):
        user_id = "uid-orphaned-draft-negative-drift"
        corruptions = {
            "create_request_started": lambda permit: permit[
                "draftPreparation"
            ].update({"createRequestHash": "forged-create-request"}),
            "patch_request_started": lambda permit: permit[
                "preparedEnvelope"
            ].update({"preparedEnvelopeHash": "forged-prepared-envelope"}),
            "attachment_request_started": lambda permit: permit[
                "draftPreparation"
            ]["activeAttachment"].update({
                "requestHash": "forged-attachment-request"
            }),
        }
        for issuer in ("terminal", "pending"):
            for request_state, corrupt in corruptions.items():
                with self.subTest(issuer=issuer, request_state=request_state):
                    rooted = _CommitOutcomeRootedFirestore()
                    scenario = self._orphaned_draft_request_scenario(
                        firestore=rooted,
                        issuer=issuer,
                        request_state=request_state,
                        user_id=user_id,
                    )
                    corrupt(scenario["permit_ref"].data)
                    scenario["permit_ref"].version += 1
                    corrupted = copy.deepcopy(scenario["permit_ref"].data)
                    with patch.object(
                        send_permits,
                        "datetime",
                        scenario["future_datetime"],
                    ):
                        with self.assertRaises(
                            send_permits.GraphSendPermitBlocked
                        ):
                            if issuer == "terminal":
                                send_permits.read_active_terminal_reply_permit(
                                    rooted,
                                    scenario["thread_ref"],
                                    scenario["thread_ref"].data[
                                        "terminalReplyAttempt"
                                    ],
                                    scenario["saga"],
                                )
                            else:
                                send_permits.read_expired_pending_graph_send_permit(
                                    rooted,
                                    scenario["thread_ref"],
                                    scenario["pending_ref"],
                                    scenario["loaded"],
                                )
                    self.assertEqual(
                        corrupted,
                        scenario["permit_ref"].data,
                    )
                    self.assertFalse(
                        any(
                            ref.exists
                            for ref in scenario["thread_ref"].collection(
                                "graphSendReviews"
                            ).docs.values()
                        )
                    )

        for issuer in ("terminal", "pending"):
            with self.subTest(issuer=issuer, drift="issuer_link"):
                rooted = _CommitOutcomeRootedFirestore()
                scenario = self._orphaned_draft_request_scenario(
                    firestore=rooted,
                    issuer=issuer,
                    request_state="create_request_started",
                    user_id=user_id,
                )
                if issuer == "terminal":
                    scenario["thread_ref"].data["terminalReplyAttempt"][
                        "graphSendPermitHash"
                    ] = "forged-terminal-permit-link"
                    scenario["thread_ref"].version += 1
                else:
                    scenario["pending_ref"].data[
                        "graphSendPermitHash"
                    ] = "forged-pending-permit-link"
                    scenario["pending_ref"].version += 1
                before = copy.deepcopy(scenario["permit_ref"].data)
                with patch.object(
                    send_permits,
                    "datetime",
                    scenario["future_datetime"],
                ):
                    with self.assertRaises(
                        send_permits.GraphSendPermitBlocked
                    ):
                        if issuer == "terminal":
                            send_permits.read_active_terminal_reply_permit(
                                rooted,
                                scenario["thread_ref"],
                                scenario["thread_ref"].data[
                                    "terminalReplyAttempt"
                                ],
                                scenario["saga"],
                            )
                        else:
                            send_permits.read_expired_pending_graph_send_permit(
                                rooted,
                                scenario["thread_ref"],
                                scenario["pending_ref"],
                                scenario["loaded"],
                            )
                self.assertEqual(before, scenario["permit_ref"].data)

    def test_local_transition_malformed_readback_fails_closed(self):
        for operation in (
            "begin_create",
            "begin_patch",
            "begin_attachment",
            "finalize",
            "consume",
        ):
            with self.subTest(operation=operation):
                firestore = _CommitOutcomeFirestore()
                scenario = self._local_transition_scenario(
                    firestore=firestore,
                    issuer="pending",
                    operation=operation,
                )

                def corrupt_readback():
                    permit_ref = scenario["capability"].permit_ref
                    permit_ref.data["stateHeadHash"] = "forged-state-head"
                    permit_ref.version += 1

                firestore.commit_outcomes = ["apply_then_raise"]
                firestore.after_apply = corrupt_readback

                with self.assertRaisesRegex(
                    send_permits.GraphSendPermitError,
                    "commit|readback|ambiguous|malformed|state",
                ):
                    scenario["invoke"]()

    def test_pending_permit_issue_apply_then_raise_recovers_usable_capability_once(self):
        firestore = _CommitOutcomeFirestore("apply_then_raise")
        thread_ref = _DocRef(
            {"clientId": "client-1"},
            doc_id="thread-pending-issue-apply-then-raise",
        )
        token = "pending-owner-apply-then-raise"
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(
            loaded,
            doc_id="pending-issue-apply-then-raise",
        )

        capability = send_permits.issue_pending_graph_send_permit(
            firestore,
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )

        existing_permits = [
            ref
            for ref in thread_ref.collection("graphSendPermits").docs.values()
            if ref.exists
        ]
        self.assertEqual([capability.permit_ref], existing_permits)
        self.assertEqual(1, firestore.commit_attempts)
        send_permits.begin_graph_draft_creation(
            capability,
            loaded["msgId"],
            planned_attachment_count=0,
        )
        permit = send_permits.read_permit(capability)
        self.assertEqual(
            "create_request_started",
            permit["draftPreparation"]["state"],
        )

    def test_pending_permit_issue_no_apply_retries_same_permit_once(self):
        firestore = _CommitOutcomeFirestore("no_apply")
        thread_ref = _DocRef(
            {"clientId": "client-1"},
            doc_id="thread-pending-issue-no-apply",
        )
        token = "pending-owner-no-apply"
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-issue-no-apply")

        capability = send_permits.issue_pending_graph_send_permit(
            firestore,
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )

        self.assertEqual(2, firestore.commit_attempts)
        self.assertEqual(2, len(firestore.commit_payloads))
        self.assertEqual(
            firestore.commit_payloads[0],
            firestore.commit_payloads[1],
        )
        existing_permits = [
            ref
            for ref in thread_ref.collection("graphSendPermits").docs.values()
            if ref.exists
        ]
        self.assertEqual([capability.permit_ref], existing_permits)
        send_permits.begin_graph_draft_creation(
            capability,
            loaded["msgId"],
            planned_attachment_count=0,
        )
        self.assertEqual(
            "create_request_started",
            send_permits.read_permit(capability)["draftPreparation"]["state"],
        )

    def test_pending_permit_issue_repeated_no_apply_is_typed_exact_source(self):
        firestore = _CommitOutcomeFirestore("no_apply", "no_apply")
        thread_ref = _DocRef(
            {"clientId": "client-1"},
            doc_id="thread-pending-issue-repeated-no-apply",
        )
        token = "pending-owner-repeated-no-apply"
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(
            loaded,
            doc_id="pending-issue-repeated-no-apply",
        )
        original_thread = copy.deepcopy(thread_ref.data)
        original_pending = copy.deepcopy(pending_ref.data)

        with self.assertRaises(
            send_permits.GraphSendPermitLocalRetryable
        ):
            send_permits.issue_pending_graph_send_permit(
                firestore,
                thread_ref,
                pending_ref,
                dict(loaded),
                token,
            )

        self.assertEqual(original_thread, thread_ref.data)
        self.assertEqual(original_pending, pending_ref.data)
        self.assertEqual(2, firestore.commit_attempts)
        self.assertEqual(2, len(firestore.commit_payloads))
        self.assertEqual(
            firestore.commit_payloads[0],
            firestore.commit_payloads[1],
        )
        self.assertFalse(any(
            ref.exists
            for ref in thread_ref.collection("graphSendPermits").docs.values()
        ))

    def test_pending_permit_issue_corrupt_applied_target_fails_closed(self):
        firestore = _CommitOutcomeFirestore("apply_then_raise")
        thread_ref = _DocRef(
            {"clientId": "client-1"},
            doc_id="thread-pending-issue-corrupt-target",
        )
        token = "pending-owner-corrupt-target"
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-issue-corrupt-target")

        def corrupt_target():
            pending_ref.data["graphSendPermitHash"] = "forged-permit-hash"
            pending_ref.version += 1

        firestore.after_apply = corrupt_target
        with self.assertRaisesRegex(
            send_permits.GraphSendPermitError,
            "source/target|drift|exact",
        ):
            send_permits.issue_pending_graph_send_permit(
                firestore,
                thread_ref,
                pending_ref,
                dict(loaded),
                token,
            )

        self.assertEqual(1, firestore.commit_attempts)
        self.assertEqual(1, len([
            ref
            for ref in thread_ref.collection("graphSendPermits").docs.values()
            if ref.exists
        ]))

    def test_pending_permit_issue_wrong_thread_id_is_write_free(self):
        firestore = _CommitOutcomeFirestore()
        thread_ref = _DocRef(
            {"clientId": "client-1"},
            doc_id="thread-canonical-pending-issue",
        )
        token = "pending-owner-wrong-thread"
        loaded = _pending_data("thread-wrong-pending-issue", token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-wrong-thread")
        original_thread = copy.deepcopy(thread_ref.data)
        original_pending = copy.deepcopy(pending_ref.data)
        permit_collection = thread_ref.collection("graphSendPermits")
        capability = None

        with patch.object(
            send_permits,
            "uuid4",
            side_effect=AssertionError(
                "wrong-thread pending source cannot generate a capability"
            ),
        ) as generate_uuid:
            with self.assertRaisesRegex(
                send_permits.GraphSendPermitBlocked,
                "thread|canonical|issuer",
            ):
                capability = send_permits.issue_pending_graph_send_permit(
                    firestore,
                    thread_ref,
                    pending_ref,
                    dict(loaded),
                    token,
                )

        self.assertIsNone(capability)
        generate_uuid.assert_not_called()
        self.assertEqual(original_thread, thread_ref.data)
        self.assertEqual(original_pending, pending_ref.data)
        self.assertEqual({}, permit_collection.docs)
        self.assertEqual(0, firestore.commit_attempts)

    def test_pending_caller_cannot_capabilitylessly_release_linked_permit(self):
        rooted = _RootedFirestore()
        thread_ref = _DocRef(
            {"clientId": "client-1"},
            doc_id="thread-pending-linked-release",
        )
        token = "pending-owner-linked-release"
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-linked-release")
        rooted.add_thread(thread_ref)
        rooted.add_pending(pending_ref)
        capability = send_permits.issue_pending_graph_send_permit(
            rooted,
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )
        before = copy.deepcopy(pending_ref.data)
        doc = types.SimpleNamespace(id=pending_ref.id, reference=pending_ref)

        with patch.object(
            pending_responses,
            "_pending_claim_refs",
            return_value=(rooted, rooted.user_root, thread_ref, pending_ref),
        ), self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "capability|linked|permit",
        ):
            pending_responses._cas_pending_update(
                "uid-linked-release",
                doc,
                loaded,
                token,
                {
                    "status": "queued",
                    "processingBy": None,
                    "processingAt": None,
                    "processingLeaseUntil": None,
                    "updatedAt": send_permits.SERVER_TIMESTAMP,
                },
            )

        self.assertEqual(before, pending_ref.data)
        self.assertEqual(
            "issued",
            send_permits.read_permit(capability)["status"],
        )

    def test_terminal_unissued_attempt_schema_matrix_blocks_before_recovery(self):
        allow = CampaignAutomationDecision(
            state="allow",
            reason="",
            client_data={"status": "live"},
            metadata={"terminal": False, "stopKind": "none"},
        )
        for label, invalid_attempt in _invalid_unissued_terminal_attempts(
            _terminal_saga("schema-matrix-template")
        ).items():
            with self.subTest(label=label):
                rooted = _CommitOutcomeRootedFirestore()
                thread_id = f"thread-terminal-schema-{label}"
                saga, thread_ref, _claim_ref = _terminal_refs(thread_id)
                immutable_payload = {
                    key: value
                    for key, value in saga.items()
                    if key not in {"immutableHash", "phase", "finalRow"}
                }
                immutable_hash = hashlib.sha256(
                    json.dumps(
                        immutable_payload,
                        sort_keys=True,
                        default=str,
                    ).encode("utf-8")
                ).hexdigest()
                saga["immutableHash"] = immutable_hash
                thread_ref.data["terminalSaga"]["immutableHash"] = immutable_hash
                thread_ref.data["terminalSagaClaim"][
                    "immutableHash"
                ] = immutable_hash
                template_saga = _terminal_saga("schema-matrix-template")
                translated_attempt = copy.deepcopy(invalid_attempt)
                for field_name, template_field, actual_field in (
                    ("sagaKey", "sagaKey", "sagaKey"),
                    ("sourceMessageKey", "sourceMessageKey", "sourceMessageKey"),
                    (
                        "sourceGraphMessageId",
                        "sourceGraphMessageId",
                        "sourceGraphMessageId",
                    ),
                    ("conversationId", "sourceConversationId", "sourceConversationId"),
                    ("recipient", "replyRecipient", "replyRecipient"),
                ):
                    if (
                        field_name in translated_attempt
                        and translated_attempt[field_name]
                        == template_saga[template_field]
                    ):
                        translated_attempt[field_name] = saga[actual_field]
                exact_template_hash = hashlib.sha256(
                    template_saga["responseBody"].encode("utf-8")
                ).hexdigest()
                if translated_attempt.get("responseBodyHash") == exact_template_hash:
                    translated_attempt["responseBodyHash"] = hashlib.sha256(
                        saga["responseBody"].encode("utf-8")
                    ).hexdigest()
                thread_ref.data["terminalReplyAttempt"] = translated_attempt
                rooted.add_thread(thread_ref)
                before = copy.deepcopy(thread_ref.data)
                owner = processing.TerminalSagaExecution(
                    owner="terminal-owner-a",
                    fencing_token=1,
                )

                with patch.object(
                    processing,
                    "_fs",
                    rooted,
                ), patch.object(
                    processing,
                    "get_client_automation_decision",
                    return_value=allow,
                ), patch.object(
                    processing,
                    "send_reply_in_thread",
                    side_effect=AssertionError(
                        "invalid unissued attempt cannot reach provider"
                    ),
                ) as provider:
                    with self.assertRaises(
                        processing.RetryableProcessingError
                    ):
                        processing._settle_terminal_reply_obligation(
                            "uid-terminal-schema",
                            "client-1",
                            thread_id,
                            {"Authorization": "Bearer test"},
                            saga["replyRecipient"],
                            saga,
                            terminal_saga_owner=owner,
                        )

                provider.assert_not_called()
                self.assertEqual(0, rooted.commit_attempts)
                self.assertEqual(before, thread_ref.data)
                self.assertFalse(any(
                    ref.exists
                    for ref in thread_ref.collection(
                        "graphSendPermits"
                    ).docs.values()
                ))

    def test_terminal_issuance_revalidates_unissued_attempt_schema(self):
        template_saga = _terminal_saga("issuance-schema-template")
        for label, invalid_attempt in _invalid_unissued_terminal_attempts(
            template_saga
        ).items():
            with self.subTest(label=label):
                firestore = _CommitOutcomeFirestore()
                thread_id = f"thread-terminal-issuance-schema-{label}"
                saga, thread_ref, claim_ref = _terminal_refs(thread_id)
                translated_attempt = copy.deepcopy(invalid_attempt)
                translations = {
                    "sagaKey": (template_saga["sagaKey"], saga["sagaKey"]),
                    "sourceMessageKey": (
                        template_saga["sourceMessageKey"],
                        saga["sourceMessageKey"],
                    ),
                    "sourceGraphMessageId": (
                        template_saga["sourceGraphMessageId"],
                        saga["sourceGraphMessageId"],
                    ),
                    "conversationId": (
                        template_saga["sourceConversationId"],
                        saga["sourceConversationId"],
                    ),
                    "recipient": (
                        template_saga["replyRecipient"],
                        saga["replyRecipient"],
                    ),
                }
                for field_name, (template_value, saga_value) in translations.items():
                    if translated_attempt.get(field_name) == template_value:
                        translated_attempt[field_name] = saga_value
                thread_ref.data["terminalReplyAttempt"] = translated_attempt
                before = copy.deepcopy(thread_ref.data)

                with self.assertRaisesRegex(
                    send_permits.GraphSendPermitBlocked,
                    "intent|attempt|schema",
                ):
                    send_permits.issue_terminal_graph_send_permit(
                        firestore,
                        thread_ref,
                        claim_ref,
                        saga,
                        "terminal-owner-a",
                        1,
                    )

                self.assertEqual(before, thread_ref.data)
                self.assertFalse(any(
                    ref.exists
                    for ref in thread_ref.collection(
                        "graphSendPermits"
                    ).docs.values()
                ))
                self.assertEqual(0, firestore.commit_attempts)

    def test_terminal_padded_body_exact_attempt_issues_canonical_permit(self):
        firestore = _CommitOutcomeFirestore()
        thread_id = "thread-terminal-padded-body-issue"
        saga, thread_ref, claim_ref = _terminal_refs(thread_id)
        canonical_body = "Thank you. We will close this property review."
        saga["responseBody"] = f" \n{canonical_body}\t "
        thread_ref.data["terminalSaga"] = dict(saga)
        thread_ref.data["terminalReplyAttempt"] = (
            _exact_unissued_terminal_attempt(saga)
        )
        canonical_hash = hashlib.sha256(
            canonical_body.encode("utf-8")
        ).hexdigest()
        thread_ref.data["terminalReplyAttempt"][
            "responseBodyHash"
        ] = canonical_hash

        capability = send_permits.issue_terminal_graph_send_permit(
            firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
        )

        permit = send_permits.read_permit(capability)
        self.assertEqual(canonical_hash, permit["bodyHash"])
        self.assertEqual(
            canonical_hash,
            thread_ref.data["terminalReplyAttempt"]["responseBodyHash"],
        )
        self.assertEqual(1, firestore.commit_attempts)

    def test_terminal_whitespace_only_body_is_blocked_before_uuid(self):
        firestore = _CommitOutcomeFirestore()
        thread_id = "thread-terminal-empty-body-issue"
        saga, thread_ref, claim_ref = _terminal_refs(thread_id)
        saga["responseBody"] = " \n\t "
        thread_ref.data["terminalSaga"] = dict(saga)
        thread_ref.data["terminalReplyAttempt"] = (
            _exact_unissued_terminal_attempt(saga)
        )
        before = copy.deepcopy(thread_ref.data)

        with patch.object(
            send_permits,
            "uuid4",
            side_effect=AssertionError("empty body cannot reach UUID generation"),
        ) as uuid_factory:
            with self.assertRaisesRegex(
                send_permits.GraphSendPermitBlocked,
                "body|empty|blank|intent",
            ):
                send_permits.issue_terminal_graph_send_permit(
                    firestore,
                    thread_ref,
                    claim_ref,
                    saga,
                    "terminal-owner-a",
                    1,
                )

        uuid_factory.assert_not_called()
        self.assertEqual(before, thread_ref.data)
        self.assertEqual(0, firestore.commit_attempts)

    def test_terminal_padded_body_unissued_attempt_crash_replay_does_not_resend(self):
        allow = CampaignAutomationDecision(
            state="allow",
            reason="",
            client_data={"status": "live"},
            metadata={"terminal": False, "stopKind": "none"},
        )
        rooted = _CommitOutcomeRootedFirestore()
        thread_id = "thread-terminal-padded-body-crash-replay"
        saga, thread_ref, _claim_ref = _terminal_refs(thread_id)
        canonical_body = "Thank you. We will close this property review."
        saga["responseBody"] = f"\n  {canonical_body} \t"
        thread_ref.data["terminalSaga"] = dict(saga)
        thread_ref.data["terminalReplyAttempt"] = (
            _exact_unissued_terminal_attempt(saga)
        )
        canonical_hash = hashlib.sha256(
            canonical_body.encode("utf-8")
        ).hexdigest()
        thread_ref.data["terminalReplyAttempt"][
            "responseBodyHash"
        ] = canonical_hash
        rooted.add_thread(thread_ref)
        owner = processing.TerminalSagaExecution(
            owner="terminal-owner-a",
            fencing_token=1,
        )
        provider_capabilities = []

        def crash_at_provider_boundary(*args, **kwargs):
            self.assertEqual(canonical_body, args[2])
            provider_capabilities.append(kwargs["graph_send_capability"])
            raise RuntimeError("simulated padded-body provider-boundary crash")

        with patch.object(
            processing,
            "_fs",
            rooted,
        ), patch.object(
            processing,
            "_renew_terminal_saga_execution",
            return_value=datetime.now(timezone.utc),
        ), patch.object(
            processing,
            "get_client_automation_decision",
            return_value=allow,
        ), patch.object(
            processing,
            "send_reply_in_thread",
            side_effect=crash_at_provider_boundary,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "padded-body provider-boundary crash",
            ):
                processing._settle_terminal_reply_obligation(
                    "uid-terminal-padded-body",
                    "client-1",
                    thread_id,
                    {"Authorization": "Bearer test"},
                    saga["replyRecipient"],
                    saga,
                    terminal_saga_owner=owner,
                )

        self.assertEqual(1, len(provider_capabilities))
        permit = send_permits.read_permit(provider_capabilities[0])
        self.assertEqual(canonical_hash, permit["bodyHash"])

        with patch.object(
            processing,
            "_fs",
            rooted,
        ), patch.object(
            processing,
            "_renew_terminal_saga_execution",
            return_value=datetime.now(timezone.utc),
        ), patch.object(
            processing,
            "find_exact_sent_message_by_immutable_id",
            return_value=None,
        ), patch.object(
            processing,
            "send_reply_in_thread",
            side_effect=AssertionError("padded-body retry cannot resend"),
        ) as resend:
            with self.assertRaisesRegex(
                processing.RetryableProcessingError,
                "duplicate send|unresolved",
            ):
                processing._settle_terminal_reply_obligation(
                    "uid-terminal-padded-body",
                    "client-1",
                    thread_id,
                    {"Authorization": "Bearer test"},
                    saga["replyRecipient"],
                    saga,
                    terminal_saga_owner=owner,
                )

        resend.assert_not_called()
        self.assertEqual(1, len([
            ref
            for ref in thread_ref.collection("graphSendPermits").docs.values()
            if ref.exists
        ]))

    def test_terminal_unissued_attempt_recovers_once_across_issue_commit_boundary(self):
        allow = CampaignAutomationDecision(
            state="allow",
            reason="",
            client_data={"status": "live"},
            metadata={"terminal": False, "stopKind": "none"},
        )
        for commit_outcome in (None, "apply_then_raise"):
            with self.subTest(commit_outcome=commit_outcome):
                rooted = _CommitOutcomeRootedFirestore(
                    *([commit_outcome] if commit_outcome else [])
                )
                label = commit_outcome or "normal"
                thread_id = f"thread-terminal-unissued-{label}"
                saga, thread_ref, _claim_ref = _terminal_refs(thread_id)
                attempt = thread_ref.data["terminalReplyAttempt"]
                attempt.update({
                    "sourceMessageKey": saga.get("sourceMessageKey"),
                    "sourceGraphMessageId": saga["sourceGraphMessageId"],
                    "conversationId": saga["sourceConversationId"],
                    "recipient": saga["replyRecipient"],
                    "startedAt": datetime.now(timezone.utc),
                })
                rooted.add_thread(thread_ref)
                owner = processing.TerminalSagaExecution(
                    owner="terminal-owner-a",
                    fencing_token=1,
                )
                provider_capabilities = []

                def crash_at_provider_boundary(*_args, **kwargs):
                    provider_capabilities.append(
                        kwargs["graph_send_capability"]
                    )
                    raise RuntimeError("simulated provider-boundary crash")

                with patch.object(
                    processing,
                    "_fs",
                    rooted,
                ), patch.object(
                    processing,
                    "_renew_terminal_saga_execution",
                    return_value=datetime.now(timezone.utc),
                ), patch.object(
                    processing,
                    "get_client_automation_decision",
                    return_value=allow,
                ), patch.object(
                    processing,
                    "send_reply_in_thread",
                    side_effect=crash_at_provider_boundary,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "simulated provider-boundary crash",
                    ):
                        processing._settle_terminal_reply_obligation(
                            "uid-terminal-unissued",
                            "client-1",
                            thread_id,
                            {"Authorization": "Bearer test"},
                            saga["replyRecipient"],
                            saga,
                            terminal_saga_owner=owner,
                        )

                self.assertEqual(1, len(provider_capabilities))
                existing_permits = [
                    ref
                    for ref in thread_ref.collection(
                        "graphSendPermits"
                    ).docs.values()
                    if ref.exists
                ]
                self.assertEqual(1, len(existing_permits))
                capability = provider_capabilities[0]
                self.assertEqual(
                    capability.permit_id,
                    thread_ref.data["terminalReplyAttempt"][
                        "graphSendPermitId"
                    ],
                )
                self.assertEqual(
                    capability.immutable_hash,
                    thread_ref.data["terminalReplyAttempt"][
                        "graphSendPermitHash"
                    ],
                )
                self.assertEqual(1, rooted.commit_attempts)

                with patch.object(
                    processing,
                    "_fs",
                    rooted,
                ), patch.object(
                    processing,
                    "_renew_terminal_saga_execution",
                    return_value=datetime.now(timezone.utc),
                ), patch.object(
                    processing,
                    "find_exact_sent_message_by_immutable_id",
                    return_value=None,
                ), patch.object(
                    processing,
                    "send_reply_in_thread",
                    side_effect=AssertionError("retry cannot resend"),
                ) as resend:
                    with self.assertRaisesRegex(
                        processing.RetryableProcessingError,
                        "duplicate send|unresolved",
                    ):
                        processing._settle_terminal_reply_obligation(
                            "uid-terminal-unissued",
                            "client-1",
                            thread_id,
                            {"Authorization": "Bearer test"},
                            saga["replyRecipient"],
                            saga,
                            terminal_saga_owner=owner,
                        )

                resend.assert_not_called()
                self.assertEqual(1, len([
                    ref
                    for ref in thread_ref.collection(
                        "graphSendPermits"
                    ).docs.values()
                    if ref.exists
                ]))

    def test_terminal_unissued_attempt_stale_owner_or_fence_fails_closed(self):
        allow = CampaignAutomationDecision(
            state="allow",
            reason="",
            client_data={"status": "live"},
            metadata={"terminal": False, "stopKind": "none"},
        )
        for drift in ("owner", "fence"):
            with self.subTest(drift=drift):
                rooted = _CommitOutcomeRootedFirestore()
                thread_id = f"thread-terminal-unissued-stale-{drift}"
                saga, thread_ref, _claim_ref = _terminal_refs(thread_id)
                thread_ref.data["terminalReplyAttempt"].update({
                    "sourceGraphMessageId": saga["sourceGraphMessageId"],
                    "conversationId": saga["sourceConversationId"],
                    "recipient": saga["replyRecipient"],
                    "startedAt": datetime.now(timezone.utc),
                })
                claim = thread_ref.data["terminalSagaClaim"]
                if drift == "owner":
                    claim["owner"] = "terminal-owner-new"
                else:
                    claim["fencingToken"] = 2
                rooted.add_thread(thread_ref)
                owner = processing.TerminalSagaExecution(
                    owner="terminal-owner-a",
                    fencing_token=1,
                )

                with patch.object(
                    processing,
                    "_fs",
                    rooted,
                ), patch.object(
                    processing,
                    "_renew_terminal_saga_execution",
                    return_value=datetime.now(timezone.utc),
                ), patch.object(
                    processing,
                    "get_client_automation_decision",
                    return_value=allow,
                ), patch.object(
                    processing,
                    "send_reply_in_thread",
                    side_effect=AssertionError("stale issuer cannot send"),
                ) as provider:
                    with self.assertRaisesRegex(
                        send_permits.GraphSendPermitBlocked,
                        "ownership changed",
                    ):
                        processing._settle_terminal_reply_obligation(
                            "uid-terminal-stale",
                            "client-1",
                            thread_id,
                            {"Authorization": "Bearer test"},
                            saga["replyRecipient"],
                            saga,
                            terminal_saga_owner=owner,
                        )

                provider.assert_not_called()
                self.assertFalse(any(
                    ref.exists
                    for ref in thread_ref.collection(
                        "graphSendPermits"
                    ).docs.values()
                ))

    def test_terminal_unissued_attempt_partial_or_active_permit_fails_closed(self):
        for state in ("partial_link", "active_pointer"):
            with self.subTest(state=state):
                rooted = _CommitOutcomeRootedFirestore()
                thread_id = f"thread-terminal-unissued-{state}"
                saga, thread_ref, claim_ref = _terminal_refs(thread_id)
                thread_ref.data["terminalReplyAttempt"].update({
                    "sourceGraphMessageId": saga["sourceGraphMessageId"],
                    "conversationId": saga["sourceConversationId"],
                    "recipient": saga["replyRecipient"],
                    "startedAt": datetime.now(timezone.utc),
                })
                rooted.add_thread(thread_ref)
                if state == "partial_link":
                    thread_ref.data["terminalReplyAttempt"][
                        "graphSendPermitId"
                    ] = "partial-permit"
                    expected_permits = 0
                else:
                    send_permits.issue_terminal_graph_send_permit(
                        rooted,
                        thread_ref,
                        claim_ref,
                        saga,
                        "terminal-owner-a",
                        1,
                    )
                    thread_ref.data["terminalReplyAttempt"].pop(
                        "graphSendPermitId"
                    )
                    thread_ref.data["terminalReplyAttempt"].pop(
                        "graphSendPermitHash"
                    )
                    thread_ref.version += 1
                    expected_permits = 1
                owner = processing.TerminalSagaExecution(
                    owner="terminal-owner-a",
                    fencing_token=1,
                )

                with patch.object(
                    processing,
                    "_fs",
                    rooted,
                ), patch.object(
                    processing,
                    "_renew_terminal_saga_execution",
                    return_value=datetime.now(timezone.utc),
                ), patch.object(
                    processing,
                    "send_reply_in_thread",
                    side_effect=AssertionError("unsafe source cannot send"),
                ) as provider:
                    with self.assertRaisesRegex(
                        processing.RetryableProcessingError,
                        "partial retained permit|permit evidence|retained Graph",
                    ):
                        processing._settle_terminal_reply_obligation(
                            "uid-terminal-unsafe-source",
                            "client-1",
                            thread_id,
                            {"Authorization": "Bearer test"},
                            saga["replyRecipient"],
                            saga,
                            terminal_saga_owner=owner,
                        )

                provider.assert_not_called()
                self.assertEqual(expected_permits, len([
                    ref
                    for ref in thread_ref.collection(
                        "graphSendPermits"
                    ).docs.values()
                    if ref.exists
                ]))

    def test_terminal_permit_issue_apply_then_raise_recovers_exact_capability_without_duplicate(self):
        firestore = _CommitOutcomeFirestore("apply_then_raise")
        saga, thread_ref, claim_ref = _terminal_refs(
            "thread-terminal-permit-apply-then-raise"
        )
        original_attempt = copy.deepcopy(
            thread_ref.data["terminalReplyAttempt"]
        )
        original_claim = copy.deepcopy(thread_ref.data["terminalSagaClaim"])
        generated_permit_suffix = "terminal-permit-apply-then-raise"
        generated_capability = "terminal-capability-apply-then-raise"

        with patch.object(
            send_permits,
            "uuid4",
            side_effect=[
                types.SimpleNamespace(hex=generated_permit_suffix),
                types.SimpleNamespace(hex=generated_capability),
            ],
        ):
            capability = send_permits.issue_terminal_graph_send_permit(
                firestore,
                thread_ref,
                claim_ref,
                saga,
                "terminal-owner-a",
                1,
            )

        permit_id = f"graph-send-{generated_permit_suffix}"
        permit_ref = thread_ref.collection("graphSendPermits").docs[permit_id]
        self.assertIs(True, permit_ref.exists)
        permit = send_permits._validate_permit(copy.deepcopy(permit_ref.data))
        self.assertEqual(permit_id, capability.permit_id)
        self.assertEqual(generated_capability, capability.capability)
        self.assertEqual(permit["immutableHash"], capability.immutable_hash)
        self.assertEqual(
            hashlib.sha256(generated_capability.encode("utf-8")).hexdigest(),
            permit["capabilityHash"],
        )
        self.assertEqual(
            send_permits._pointer(permit),
            thread_ref.data["activeGraphSendPermit"],
        )
        self.assertEqual(
            {
                **original_attempt,
                "graphSendPermitId": permit_id,
                "graphSendPermitHash": permit["immutableHash"],
            },
            thread_ref.data["terminalReplyAttempt"],
        )
        issuer_document_id, issuer_document_path = (
            send_permits._issuer_document_identity(
                claim_ref,
                issuer_kind="terminal_saga",
            )
        )
        self.assertEqual(issuer_document_id, permit["issuerDocumentId"])
        self.assertEqual(issuer_document_path, permit["issuerDocumentPath"])
        self.assertEqual("terminal-owner-a", permit["issuerOwner"])
        self.assertEqual(1, permit["issuerFence"])
        self.assertEqual(original_claim, thread_ref.data["terminalSagaClaim"])
        self.assertGreater(
            send_permits._utc(
                thread_ref.data["terminalSagaClaim"]["leaseUntil"]
            ),
            datetime.now(timezone.utc),
        )
        existing_permits = [
            ref
            for ref in thread_ref.collection("graphSendPermits").docs.values()
            if ref.exists
        ]
        self.assertEqual([permit_ref], existing_permits)
        self.assertEqual(1, firestore.commit_attempts)

    def test_terminal_permit_issue_no_apply_is_typed_and_later_retry_issues_once(self):
        firestore = _CommitOutcomeFirestore("no_apply", None)
        saga, thread_ref, claim_ref = _terminal_refs(
            "thread-terminal-permit-no-apply"
        )
        original_thread = copy.deepcopy(thread_ref.data)
        first_permit_suffix = "terminal-permit-no-apply-first"
        first_capability = "terminal-capability-no-apply-first"
        retry_permit_suffix = "terminal-permit-no-apply-retry"
        retry_capability = "terminal-capability-no-apply-retry"

        with patch.object(
            send_permits,
            "uuid4",
            side_effect=[
                types.SimpleNamespace(hex=first_permit_suffix),
                types.SimpleNamespace(hex=first_capability),
                types.SimpleNamespace(hex=retry_permit_suffix),
                types.SimpleNamespace(hex=retry_capability),
            ],
        ):
            with self.assertRaisesRegex(
                send_permits.GraphSendPermitError,
                "did not commit|retry",
            ):
                send_permits.issue_terminal_graph_send_permit(
                    firestore,
                    thread_ref,
                    claim_ref,
                    saga,
                    "terminal-owner-a",
                    1,
                )

            first_permit_id = f"graph-send-{first_permit_suffix}"
            first_permit_ref = thread_ref.collection(
                "graphSendPermits"
            ).docs[first_permit_id]
            self.assertIs(False, first_permit_ref.exists)
            self.assertEqual(original_thread, thread_ref.data)
            self.assertNotIn("activeGraphSendPermit", thread_ref.data)
            self.assertEqual(
                original_thread["terminalReplyAttempt"],
                thread_ref.data["terminalReplyAttempt"],
            )
            self.assertEqual(
                original_thread["terminalSagaClaim"],
                thread_ref.data["terminalSagaClaim"],
            )
            self.assertEqual(1, firestore.commit_attempts)

            capability = send_permits.issue_terminal_graph_send_permit(
                firestore,
                thread_ref,
                claim_ref,
                saga,
                "terminal-owner-a",
                1,
            )

        retry_permit_id = f"graph-send-{retry_permit_suffix}"
        retry_permit_ref = thread_ref.collection(
            "graphSendPermits"
        ).docs[retry_permit_id]
        retry_permit = send_permits._validate_permit(
            copy.deepcopy(retry_permit_ref.data)
        )
        self.assertEqual(retry_permit_id, capability.permit_id)
        self.assertEqual(retry_capability, capability.capability)
        self.assertIs(False, first_permit_ref.exists)
        self.assertIs(True, retry_permit_ref.exists)
        self.assertEqual(
            send_permits._pointer(retry_permit),
            thread_ref.data["activeGraphSendPermit"],
        )
        self.assertEqual(
            {
                **original_thread["terminalReplyAttempt"],
                "graphSendPermitId": retry_permit_id,
                "graphSendPermitHash": retry_permit["immutableHash"],
            },
            thread_ref.data["terminalReplyAttempt"],
        )
        self.assertEqual(
            original_thread["terminalSagaClaim"],
            thread_ref.data["terminalSagaClaim"],
        )
        existing_permits = [
            ref
            for ref in thread_ref.collection("graphSendPermits").docs.values()
            if ref.exists
        ]
        self.assertEqual([retry_permit_ref], existing_permits)
        self.assertEqual(2, firestore.commit_attempts)

    def test_terminal_permit_issue_partial_readback_is_typed_ambiguous(self):
        firestore = _CommitOutcomeFirestore("apply_then_raise")
        saga, thread_ref, claim_ref = _terminal_refs(
            "thread-terminal-permit-partial-readback"
        )
        generated_permit_suffix = "terminal-permit-partial-readback"

        def remove_committed_updated_at():
            thread_ref.data.pop("updatedAt", None)
            thread_ref.version += 1

        firestore.after_apply = remove_committed_updated_at
        with patch.object(
            send_permits,
            "uuid4",
            side_effect=[
                types.SimpleNamespace(hex=generated_permit_suffix),
                types.SimpleNamespace(
                    hex="terminal-capability-partial-readback"
                ),
            ],
        ):
            with self.assertRaisesRegex(
                send_permits.GraphSendPermitError,
                "ambiguous|exact readback",
            ):
                send_permits.issue_terminal_graph_send_permit(
                    firestore,
                    thread_ref,
                    claim_ref,
                    saga,
                    "terminal-owner-a",
                    1,
                )

        permit_id = f"graph-send-{generated_permit_suffix}"
        permit_ref = thread_ref.collection("graphSendPermits").docs[permit_id]
        self.assertIs(True, permit_ref.exists)
        send_permits._validate_permit(copy.deepcopy(permit_ref.data))
        self.assertNotIn("updatedAt", thread_ref.data)
        self.assertEqual(1, firestore.commit_attempts)

    def test_capability_draft_exit_matrix_retains_review_without_delete_or_send(self):
        cases = {
            "recipient_optout": {
                "filter": {
                    "payload": {"toRecipients": [], "ccRecipients": []},
                    "skipped": {
                        "optedOut": [{"email": "broker@example.test"}],
                    },
                },
                "reason": "recipient_optout",
                "phase": "draft",
                "preparation_state": "draft_created",
                "patch_count": 0,
            },
            "no_safe_recipients": {
                "filter": {
                    "payload": {"toRecipients": [], "ccRecipients": []},
                    "skipped": {},
                },
                "reason": "no_safe_recipients",
                "phase": "draft",
                "preparation_state": "draft_created",
                "patch_count": 0,
            },
            "final_campaign_gate": {
                "reason": "campaign_stopped_after_draft",
                "phase": "final_campaign_gate",
                "preparation_state": "prepared",
                "patch_count": 1,
            },
            "final_kill_switch": {
                "reason": (
                    "suppressed_by_kill_switch "
                    "(SITESIFT_OUTBOUND_MODE=paused)"
                ),
                "phase": "final_kill_switch",
                "preparation_state": "prepared",
                "patch_count": 1,
            },
        }
        allow = CampaignAutomationDecision(
            state="allow",
            reason="",
            client_data={"status": "live"},
            metadata={"terminal": False, "stopKind": "none"},
        )
        deny = CampaignAutomationDecision(
            state="blocked",
            reason="campaign_stopped_after_draft",
            client_data={"status": "stopped"},
            metadata={"terminal": True, "stopKind": "terminal_stop"},
        )

        for case_name, case in cases.items():
            with self.subTest(case=case_name):
                rooted = _RootedFirestore()
                token = f"pending-worker-{case_name}"
                thread_ref = _DocRef(
                    {"clientId": "client-1"},
                    doc_id=f"thread-{case_name}",
                )
                loaded = _pending_data(thread_ref.id, token=token)
                pending_ref = _DocRef(
                    loaded,
                    doc_id=f"pending-{case_name}",
                )
                rooted.add_thread(thread_ref)
                rooted.add_pending(pending_ref)
                capability = send_permits.issue_pending_graph_send_permit(
                    rooted,
                    thread_ref,
                    pending_ref,
                    dict(loaded),
                    token,
                )
                draft_id = f"draft-{case_name}"
                post_urls = []
                decision_calls = 0
                mode_calls = 0

                def campaign_decision(*_args, **_kwargs):
                    nonlocal decision_calls
                    decision_calls += 1
                    if case_name == "final_campaign_gate" and decision_calls > 1:
                        return deny
                    return allow

                def outbound_mode():
                    nonlocal mode_calls
                    mode_calls += 1
                    if case_name == "final_kill_switch" and mode_calls > 1:
                        return "paused"
                    return "live"

                def fake_get(url, **_kwargs):
                    if url.endswith(f"/me/messages/{loaded['msgId']}"):
                        return _GraphResponse(200, {
                            "conversationId": loaded["conversationId"],
                            "subject": "RE: Capability draft review",
                        })
                    return _GraphResponse(404)

                def fake_post(url, **_kwargs):
                    post_urls.append(url)
                    if url.endswith("/createReplyAll"):
                        return _GraphResponse(201, {
                            "id": draft_id,
                            "subject": PROVIDER_DRAFT_SUBJECT,
                            "toRecipients": [{
                                "emailAddress": {
                                    "address": loaded["recipient"],
                                },
                            }],
                            "ccRecipients": [],
                        })
                    if url.endswith(f"/{draft_id}/send"):
                        return _GraphResponse(202)
                    return _GraphResponse(500)

                filter_result = case.get("filter") or {
                    "payload": {
                        "toRecipients": [{
                            "emailAddress": {
                                "address": loaded["recipient"],
                            },
                        }],
                        "ccRecipients": [],
                    },
                    "skipped": {},
                }
                with ExitStack() as stack:
                    stack.enter_context(
                        patch("email_automation.clients._fs", rooted)
                    )
                    stack.enter_context(
                        patch.object(
                            processing,
                            "get_client_automation_decision",
                            side_effect=campaign_decision,
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            processing,
                            "resolve_outbound_mode",
                            side_effect=outbound_mode,
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            processing,
                            "_automatic_inbox_replies_allowed",
                            return_value=True,
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            processing,
                            "format_email_body_with_footer",
                            return_value="<p>Prepared capability draft.</p>",
                        )
                    )
                    stack.enter_context(
                        patch(
                            "email_automation.utils.resolve_signature_settings",
                            return_value=(None, None, "sender@example.test"),
                        )
                    )
                    stack.enter_context(
                        patch(
                            "email_automation.utils.needs_signature_attachments",
                            return_value=False,
                        )
                    )
                    stack.enter_context(
                        patch(
                            "email_automation.utils.exponential_backoff_request",
                            side_effect=lambda callback, *args, **kwargs: callback(),
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            processing.requests,
                            "get",
                            side_effect=fake_get,
                        )
                    )
                    stack.enter_context(
                        patch.object(
                            processing.requests,
                            "post",
                            side_effect=fake_post,
                        )
                    )
                    patch_request = stack.enter_context(
                        patch.object(
                            processing.requests,
                            "patch",
                            return_value=_GraphResponse(204),
                        )
                    )
                    request_delete = stack.enter_context(
                        patch.object(
                            processing.requests,
                            "delete",
                            return_value=_GraphResponse(204),
                        )
                    )
                    stack.enter_context(
                        patch(
                            "email_automation.email._hydrate_reply_all_draft_recipients",
                            side_effect=lambda _headers, draft, base=None: draft,
                        )
                    )
                    stack.enter_context(
                        patch(
                            "email_automation.email._source_message_reply_all_fallback",
                            side_effect=lambda draft, _source: draft,
                        )
                    )
                    stack.enter_context(
                        patch(
                            "email_automation.email._reviewed_recipient_reply_all_fallback",
                            side_effect=lambda draft, to_emails=None: draft,
                        )
                    )
                    stack.enter_context(
                        patch(
                            "email_automation.email._filter_reply_all_draft_recipients",
                            return_value=filter_result,
                        )
                    )
                    delete_draft = stack.enter_context(
                        patch.object(
                            email_module,
                            "_delete_graph_reply_draft",
                            wraps=email_module._delete_graph_reply_draft,
                        )
                    )
                    sent = processing.send_reply_in_thread(
                        user_id="uid-1",
                        headers={"Authorization": "Bearer test"},
                        body=loaded["responseBody"],
                        current_msg_id=loaded["msgId"],
                        recipient=loaded["recipient"],
                        thread_id=thread_ref.id,
                        graph_send_capability=capability,
                    )

                self.assertFalse(sent)
                self.assertEqual(
                    "draft_mutation_needs_reconciliation",
                    processing.send_reply_in_thread.last_outcome,
                )
                self.assertEqual(
                    1,
                    len([
                        url for url in post_urls
                        if url.endswith("/createReplyAll")
                    ]),
                )
                self.assertFalse(
                    any(url.endswith("/send") for url in post_urls)
                )
                self.assertEqual(case["patch_count"], patch_request.call_count)
                delete_draft.assert_not_called()
                request_delete.assert_not_called()

                permit = send_permits.read_permit(capability)
                expected_evidence = {
                    "reason": case["reason"],
                    "phase": case["phase"],
                    "draftId": draft_id,
                    "providerSendStarted": False,
                    "automaticDeleteAttempted": False,
                }
                self.assertEqual("needs_reconciliation", permit["status"])
                self.assertIsNone(permit.get("requestStartedAt"))
                self.assertEqual(
                    draft_id,
                    permit["draftPreparation"]["draftId"],
                )
                self.assertEqual(
                    case["preparation_state"],
                    permit["draftPreparation"]["state"],
                )
                self.assertEqual(expected_evidence, permit["resolutionEvidence"])
                self.assertEqual(
                    send_permits._hash(expected_evidence),
                    permit["resolutionEvidenceHash"],
                )
                with self.assertRaises(send_permits.GraphSendPermitBlocked):
                    send_permits.issue_pending_graph_send_permit(
                        rooted,
                        thread_ref,
                        pending_ref,
                        dict(loaded),
                        token,
                    )
                with self.assertRaises(send_permits.GraphSendPermitBlocked):
                    send_permits.consume_graph_send_capability(
                        capability,
                        source_graph_message_id=loaded["msgId"],
                        draft_id=draft_id,
                        subject=PROVIDER_DRAFT_SUBJECT,
                        html_body="<p>Prepared capability draft.</p>",
                        to_recipients=[loaded["recipient"]],
                        cc_recipients=[],
                        attachments=[],
                    )

    def test_capability_send_uses_immutable_id_for_all_draft_operations(self):
        thread_ref = _DocRef({}, doc_id="thread-immutable-send")
        loaded = _pending_data(thread_ref.id, token="pending-worker-a")
        loaded["responseBody"] = "Stable reply body for immutable identity."
        pending_ref = _DocRef(loaded, doc_id="pending-immutable-send")
        capability = self._issue_pending(
            thread_ref, pending_ref, dict(loaded), "pending-worker-a"
        )

        class StaticSnapshot:
            def __init__(self, data, exists=True):
                self._data = dict(data)
                self.exists = exists

            def to_dict(self):
                return dict(self._data)

        class AppRef:
            def __init__(self, path=()):
                self.path = tuple(path)

            def collection(self, name):
                return AppRef(self.path + (name,))

            def document(self, doc_id):
                return AppRef(self.path + (doc_id,))

            def get(self):
                if len(self.path) == 2 and self.path[0] == "users":
                    return StaticSnapshot({"email": "sender@example.test"})
                if len(self.path) == 4 and self.path[2] == "threads":
                    return StaticSnapshot({"clientId": "client-1"})
                return StaticSnapshot({}, exists=False)

        class AppFirestore:
            def collection(self, name):
                return AppRef((name,))

        def graph_response(status, payload=None):
            response = MagicMock(status_code=status)
            response.json.return_value = dict(payload or {})
            return response

        calls = []
        draft_id = "immutable/draft+1"
        encoded_draft_id = "immutable%2Fdraft%2B1"
        source_id = loaded["msgId"]
        recipient = loaded["recipient"]
        conversation_id = loaded["conversationId"]
        html_body = "<p>Stable reply body for immutable identity.</p>"

        def fake_get(url, **kwargs):
            calls.append(("get", url, dict(kwargs.get("headers") or {})))
            if url.endswith(f"/me/messages/{source_id}"):
                return graph_response(200, {
                    "conversationId": conversation_id,
                    "subject": "RE: Immutable identity",
                })
            if url.endswith(
                f"/me/messages/{encoded_draft_id}/attachments"
            ):
                return graph_response(200, {"value": []})
            if url.endswith(f"/me/messages/{encoded_draft_id}"):
                return graph_response(200, {
                    "id": draft_id,
                    "isDraft": False,
                    "internetMessageId": "<immutable-sent@example.test>",
                    "conversationId": conversation_id,
                    "subject": PROVIDER_DRAFT_SUBJECT,
                    "toRecipients": [
                        {"emailAddress": {"address": recipient}},
                    ],
                    "ccRecipients": [],
                    "sentDateTime": "2026-08-02T10:00:00Z",
                    "body": {"contentType": "HTML", "content": html_body},
                    "bodyPreview": loaded["responseBody"],
                })
            return graph_response(404)

        def fake_post(url, **kwargs):
            calls.append(("post", url, dict(kwargs.get("headers") or {})))
            if url.endswith("/createReplyAll"):
                return graph_response(201, {
                    "id": draft_id,
                    "subject": PROVIDER_DRAFT_SUBJECT,
                    "toRecipients": [
                        {"emailAddress": {"address": recipient}},
                    ],
                    "ccRecipients": [],
                })
            if url.endswith(f"/{encoded_draft_id}/send"):
                return graph_response(202)
            return graph_response(500)

        def fake_patch(url, **kwargs):
            calls.append(("patch", url, dict(kwargs.get("headers") or {})))
            return graph_response(204)

        original_headers = {
            "Authorization": "Bearer test",
            "Prefer": 'outlook.body-content-type="text"',
        }
        with patch("email_automation.clients._fs", AppFirestore()), patch.object(
            processing,
            "get_client_automation_decision",
            return_value=MagicMock(denies_autonomous_work=False),
        ), patch.object(
            processing, "resolve_outbound_mode", return_value="live"
        ), patch.object(
            processing, "_automatic_inbox_replies_allowed", return_value=True
        ), patch.object(
            processing, "format_email_body_with_footer", return_value=html_body
        ), patch(
            "email_automation.utils.exponential_backoff_request",
            side_effect=lambda callback, *args, **kwargs: callback(),
        ), patch.object(
            processing.requests, "get", side_effect=fake_get
        ), patch.object(
            processing.requests, "post", side_effect=fake_post
        ), patch.object(
            processing.requests, "patch", side_effect=fake_patch
        ), patch(
            "email_automation.email._hydrate_reply_all_draft_recipients",
            side_effect=lambda _headers, draft, base=None: draft,
        ), patch(
            "email_automation.email._source_message_reply_all_fallback",
            side_effect=lambda draft, _source: draft,
        ), patch(
            "email_automation.email._reviewed_recipient_reply_all_fallback",
            side_effect=lambda draft, to_emails=None: draft,
        ), patch(
            "email_automation.email._filter_reply_all_draft_recipients",
            return_value={
                "payload": {
                    "toRecipients": [
                        {"emailAddress": {"address": recipient}},
                    ],
                    "ccRecipients": [],
                },
                "skipped": {},
            },
        ), patch(
            "email_automation.messaging.index_message_id", return_value=True
        ), patch(
            "email_automation.messaging.lookup_thread_by_message_id",
            return_value=thread_ref.id,
        ), patch(
            "email_automation.messaging.index_conversation_id", return_value=True
        ), patch(
            "email_automation.messaging.save_message"
        ), patch.object(processing.time, "sleep", return_value=None):
            sent = processing.send_reply_in_thread(
                "uid-1",
                original_headers,
                loaded["responseBody"],
                source_id,
                recipient,
                thread_ref.id,
                graph_send_capability=capability,
            )

        self.assertTrue(sent)
        self.assertEqual(
            'outlook.body-content-type="text"', original_headers["Prefer"]
        )
        expected_draft_calls = {
            (
                "patch",
                "https://graph.microsoft.com/v1.0/me/messages/"
                f"{encoded_draft_id}",
            ),
            (
                "get",
                "https://graph.microsoft.com/v1.0/me/messages/"
                f"{encoded_draft_id}/attachments",
            ),
            (
                "post",
                "https://graph.microsoft.com/v1.0/me/messages/"
                f"{encoded_draft_id}/send",
            ),
        }
        self.assertTrue(
            expected_draft_calls.issubset(
                {(operation, url) for operation, url, _headers in calls}
            )
        )
        self.assertFalse(any(draft_id in url for _operation, url, _headers in calls))
        draft_calls = [
            call for call in calls
            if (
                "/createReplyAll" in call[1]
                or encoded_draft_id in call[1]
            )
        ]
        self.assertGreaterEqual(len(draft_calls), 4)
        for operation, url, operation_headers in draft_calls:
            with self.subTest(operation=operation, url=url):
                self.assertIn(
                    'IdType="ImmutableId"', operation_headers.get("Prefer", "")
                )
        settled_permit = send_permits.read_permit(capability)
        self.assertEqual("accepted", settled_permit["status"])
        self.assertEqual(
            draft_id,
            settled_permit["preparedEnvelope"]["draftId"],
        )
        self.assertEqual(
            draft_id,
            processing.send_reply_in_thread.last_exact_sent_evidence[
                "sentMessageId"
            ],
        )

    def test_pending_202_without_exact_sent_copy_retains_work_and_never_resends(self):
        for exact_get in ("404", "draft"):
            with self.subTest(exact_get=exact_get):
                rooted = _RootedFirestore()
                token = "pending-worker-202"
                thread_ref = _DocRef({}, doc_id=f"thread-pending-{exact_get}")
                loaded = _pending_data(thread_ref.id, token=token)
                loaded.update({
                    "responseBody": "Durable 202 reconciliation body.",
                    "conversationId": f"conv-pending-{exact_get}",
                    "attempts": 0,
                })
                pending_ref = _DocRef(
                    loaded,
                    doc_id=f"pending-pending-{exact_get}",
                )
                rooted.add_thread(thread_ref)
                rooted.add_pending(pending_ref)
                capability = send_permits.issue_pending_graph_send_permit(
                    rooted,
                    thread_ref,
                    pending_ref,
                    dict(loaded),
                    token,
                )
                doc = types.SimpleNamespace(
                    id=pending_ref.id,
                    reference=pending_ref,
                )
                stack, send_posts = self._graph_202_unconfirmed_stack(
                    rooted,
                    source_id=loaded["msgId"],
                    conversation_id=loaded["conversationId"],
                    recipient=loaded["recipient"],
                    draft_id=f"draft-pending-{exact_get}",
                    exact_get=exact_get,
                )
                allow = CampaignAutomationDecision(
                    state="allow",
                    reason="",
                    client_data={"status": "live"},
                    metadata={"terminal": False, "stopKind": "none"},
                )
                with stack, patch.object(
                    pending_responses,
                    "get_pending_responses",
                    return_value=[{"doc": doc, "data": dict(loaded)}],
                ), patch.object(
                    pending_responses,
                    "_claim_pending_response_for_send",
                    return_value=token,
                ), patch.object(
                    pending_responses,
                    "_final_pending_response_send_fence",
                    return_value=capability,
                ), patch.object(
                    pending_responses,
                    "_pending_claim_refs",
                    return_value=(
                        rooted,
                        rooted.user_root,
                        thread_ref,
                        pending_ref,
                    ),
                ), patch.object(
                    pending_responses,
                    "get_client_automation_decision",
                    return_value=allow,
                ), patch.object(
                    pending_responses,
                    "_pending_response_column_contract_error",
                    return_value=None,
                ):
                    states = pending_responses.process_pending_responses(
                        "uid-1",
                        {"Authorization": "Bearer test"},
                    )

                self.assertEqual([], states)
                self.assertEqual(1, len(send_posts))
                self.assertTrue(pending_ref.exists)
                self.assertEqual(
                    "needs_reconciliation", pending_ref.data["status"]
                )
                self.assertEqual(token, pending_ref.data["processingBy"])
                self.assertEqual(
                    capability.permit_id,
                    pending_ref.data["graphSendPermitId"],
                )
                retained = send_permits.read_permit(capability)
                self.assertEqual("needs_reconciliation", retained["status"])
                self.assertTrue(retained["pendingSendReviewRequired"])
                review_ref = thread_ref.collection("graphSendReviews").document(
                    f"pending-{capability.permit_id}"
                )
                review = review_ref.get().to_dict()
                self.assertIsNone(review["alreadySent"])
                self.assertTrue(review["sendOutcomeUnknown"])
                self.assertFalse(review["retryAllowed"])
                self.assertTrue(review["authoritative"])
                self.assertEqual("pendingGraphSendProtocol", review["source"])

                pending_ref.data["processingLeaseUntil"] = (
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                )
                pending_ref.version += 1
                with patch("email_automation.clients._fs", rooted), patch.object(
                    pending_responses,
                    "get_pending_responses",
                    return_value=[
                        {"doc": doc, "data": dict(pending_ref.data)}
                    ],
                ), patch.object(
                    pending_responses,
                    "find_exact_sent_message_by_immutable_id",
                    return_value=None,
                ), patch.object(
                    processing,
                    "send_reply_in_thread",
                    side_effect=AssertionError("takeover must not resend"),
                ) as second_send:
                    takeover_states = pending_responses.process_pending_responses(
                        "uid-1",
                        {"Authorization": "Bearer test"},
                    )

                self.assertEqual([], takeover_states)
                second_send.assert_not_called()
                self.assertTrue(pending_ref.exists)
                self.assertEqual(
                    "needs_reconciliation",
                    send_permits.read_permit(capability)["status"],
                )

    def test_terminal_202_without_exact_sent_copy_stays_owed_and_never_resends(self):
        for exact_get in ("404", "draft"):
            with self.subTest(exact_get=exact_get):
                rooted = _RootedFirestore()
                thread_id = f"thread-terminal-{exact_get}"
                saga = _terminal_saga(thread_id)
                owner = processing.TerminalSagaExecution(
                    owner="terminal-owner-202",
                    fencing_token=1,
                )
                thread_ref = _DocRef({
                    "terminalSaga": dict(saga),
                    "terminalReplyOwed": True,
                    "terminalSagaClaim": {
                        "sagaKey": saga["sagaKey"],
                        "immutableHash": saga["immutableHash"],
                        "owner": owner.owner,
                        "fencingToken": owner.fencing_token,
                        "leaseUntil": datetime.now(timezone.utc)
                        + timedelta(minutes=5),
                    },
                }, doc_id=thread_id)
                rooted.add_thread(thread_ref)

                def fenced_update(
                    _user_id,
                    current_thread_id,
                    _saga,
                    _owner,
                    state_patch,
                    **_kwargs,
                ):
                    target = rooted.user_root.collection("threads").document(
                        current_thread_id
                    )
                    target.data.update(dict(state_patch))
                    target.version += 1

                stack, send_posts = self._graph_202_unconfirmed_stack(
                    rooted,
                    source_id=saga["sourceGraphMessageId"],
                    conversation_id=saga["sourceConversationId"],
                    recipient=saga["replyRecipient"],
                    draft_id=f"draft-terminal-{exact_get}",
                    exact_get=exact_get,
                )
                with stack, patch.object(processing, "_fs", rooted), patch.object(
                    processing,
                    "_renew_terminal_saga_execution",
                    return_value=datetime.now(timezone.utc),
                ), patch.object(
                    processing,
                    "_fenced_terminal_thread_update",
                    side_effect=fenced_update,
                ):
                    with self.assertRaisesRegex(
                        processing.RetryableProcessingError,
                        "reconciliation-only|duplicate send",
                    ):
                        processing._settle_terminal_reply_obligation(
                            "uid-1",
                            "client-1",
                            thread_id,
                            {"Authorization": "Bearer test"},
                            saga["replyRecipient"],
                            saga,
                            terminal_saga_owner=owner,
                        )

                self.assertEqual(1, len(send_posts))
                self.assertTrue(thread_ref.data["terminalReplyOwed"])
                self.assertEqual(
                    "needs_reconciliation",
                    thread_ref.data["terminalReplyAttempt"]["status"],
                )
                permit_id = thread_ref.data["terminalReplyAttempt"][
                    "graphSendPermitId"
                ]
                permit_ref = thread_ref.collection("graphSendPermits").document(
                    permit_id
                )
                permit = send_permits._validate_permit(permit_ref.data)
                self.assertEqual("needs_reconciliation", permit["status"])
                self.assertTrue(permit["terminalSendReviewRequired"])
                reviews = rooted.user_root.collection(
                    "terminalGraphSendReviews"
                ).docs
                self.assertEqual(1, len(reviews))
                review = next(iter(reviews.values())).get().to_dict()
                self.assertIsNone(review["alreadySent"])
                self.assertTrue(review["sendOutcomeUnknown"])
                self.assertFalse(review["retryAllowed"])
                self.assertTrue(review["authoritative"])
                self.assertEqual("terminalGraphSendProtocol", review["source"])

                with patch.object(processing, "_fs", rooted), patch.object(
                    processing,
                    "_renew_terminal_saga_execution",
                    return_value=datetime.now(timezone.utc),
                ), patch.object(
                    processing,
                    "find_exact_sent_message_by_immutable_id",
                    return_value=None,
                ), patch.object(
                    processing,
                    "send_reply_in_thread",
                    side_effect=AssertionError("takeover must not resend"),
                ) as second_send:
                    with self.assertRaisesRegex(
                        processing.RetryableProcessingError,
                        "reconciliation-only|duplicate send",
                    ):
                        processing._settle_terminal_reply_obligation(
                            "uid-1",
                            "client-1",
                            thread_id,
                            {"Authorization": "Bearer test"},
                            saga["replyRecipient"],
                            saga,
                            terminal_saga_owner=owner,
                        )

                second_send.assert_not_called()
                self.assertTrue(thread_ref.data["terminalReplyOwed"])

    def test_terminal_capability_draft_exit_settles_exact_review_and_never_retries(self):
        rooted = _RootedFirestore()
        thread_id = "thread-terminal-draft-review"
        saga = _terminal_saga(thread_id)
        owner = processing.TerminalSagaExecution(
            owner="terminal-owner-draft-review",
            fencing_token=1,
        )
        thread_ref = _DocRef({
            "terminalSaga": dict(saga),
            "terminalReplyOwed": True,
            "terminalSagaClaim": {
                "sagaKey": saga["sagaKey"],
                "immutableHash": saga["immutableHash"],
                "owner": owner.owner,
                "fencingToken": owner.fencing_token,
                "leaseUntil": datetime.now(timezone.utc)
                + timedelta(minutes=5),
            },
        }, doc_id=thread_id)
        rooted.add_thread(thread_ref)
        draft_id = "draft-terminal-draft-review"
        stack, send_posts = self._graph_202_unconfirmed_stack(
            rooted,
            source_id=saga["sourceGraphMessageId"],
            conversation_id=saga["sourceConversationId"],
            recipient=saga["replyRecipient"],
            draft_id=draft_id,
        )
        captured_capabilities = []

        def fenced_update(
            _user_id,
            current_thread_id,
            _saga,
            _owner,
            state_patch,
            **_kwargs,
        ):
            target = rooted.user_root.collection("threads").document(
                current_thread_id
            )
            target.data.update(dict(state_patch))
            target.version += 1

        def issue_and_capture(*args, **kwargs):
            capability = send_permits.issue_terminal_graph_send_permit(
                *args,
                **kwargs,
            )
            captured_capabilities.append(capability)
            return capability

        with stack, patch.object(processing, "_fs", rooted), patch.object(
            processing,
            "resolve_outbound_mode",
            side_effect=["live", "paused"],
        ), patch.object(
            processing,
            "_renew_terminal_saga_execution",
            return_value=datetime.now(timezone.utc),
        ), patch.object(
            processing,
            "_fenced_terminal_thread_update",
            side_effect=fenced_update,
        ), patch.object(
            processing,
            "issue_terminal_graph_send_permit",
            side_effect=issue_and_capture,
        ), patch.object(
            processing,
            "_clear_resolved_terminal_saga",
        ) as clear_saga, patch.object(
            email_module,
            "_delete_graph_reply_draft",
            wraps=email_module._delete_graph_reply_draft,
        ) as delete_draft, patch.object(
            processing.requests,
            "delete",
            return_value=_GraphResponse(204),
        ) as request_delete:
            outcome = processing._settle_terminal_reply_obligation(
                "uid-1",
                "client-1",
                thread_id,
                {"Authorization": "Bearer test"},
                saga["replyRecipient"],
                saga,
                terminal_saga_owner=owner,
            )

        self.assertEqual("draft_needs_review", outcome)
        self.assertEqual([], send_posts)
        delete_draft.assert_not_called()
        request_delete.assert_not_called()
        clear_saga.assert_called_once()
        self.assertEqual(1, len(captured_capabilities))
        capability = captured_capabilities[0]
        permit = send_permits.read_permit(capability)
        self.assertEqual("settled_draft_needs_review", permit["status"])
        self.assertTrue(permit["draftReviewRequired"])
        self.assertIsNone(permit.get("requestStartedAt"))
        self.assertFalse(thread_ref.data["terminalReplyOwed"])
        self.assertEqual(
            "draft_needs_review",
            thread_ref.data["terminalReplyOutcome"],
        )
        self.assertEqual(
            "draft_needs_review",
            thread_ref.data["terminalReplyAttempt"]["outcome"],
        )
        reviews = rooted.user_root.collection("terminalGraphSendReviews").docs
        self.assertEqual(1, len(reviews))
        review_ref = next(iter(reviews.values()))
        review = review_ref.get().to_dict()
        self.assertEqual("manual_review", review["status"])
        self.assertIs(review_ref, permit["draftReviewEvidenceRef"])
        self.assertEqual(
            send_permits._stable_evidence_hash(review),
            permit["draftReviewEvidenceHash"],
        )
        self.assertFalse(review["alreadySent"])
        self.assertFalse(review["providerSendStarted"])
        self.assertFalse(review["sendOutcomeUnknown"])
        self.assertFalse(review["retryAllowed"])
        self.assertFalse(review["automaticDeleteAttempted"])
        self.assertTrue(review["authoritative"])
        self.assertEqual(capability.permit_id, review["graphSendPermitId"])
        self.assertEqual(capability.immutable_hash, review["graphSendPermitHash"])
        self.assertEqual(draft_id, review["draftId"])
        self.assertEqual(
            "prepared",
            review["draftMutationState"],
        )
        self.assertEqual(
            permit["resolutionEvidenceHash"],
            review["draftResolutionEvidenceHash"],
        )
        transaction = rooted.transaction()
        send_permits.assert_terminal_reply_permit_settled(
            transaction,
            thread_ref,
        )

        with patch.object(
            processing,
            "_fs",
            rooted,
        ), patch.object(
            processing,
            "_renew_terminal_saga_execution",
            return_value=datetime.now(timezone.utc),
        ), patch.object(
            processing,
            "_clear_resolved_terminal_saga",
        ), patch.object(
            processing,
            "send_reply_in_thread",
            side_effect=AssertionError("settled draft review must not resend"),
        ) as second_send, patch.object(
            email_module,
            "_delete_graph_reply_draft",
        ) as second_delete:
            retry_outcome = processing._settle_terminal_reply_obligation(
                "uid-1",
                "client-1",
                thread_id,
                {"Authorization": "Bearer test"},
                saga["replyRecipient"],
                saga,
                terminal_saga_owner=owner,
            )

        self.assertEqual("draft_needs_review", retry_outcome)
        second_send.assert_not_called()
        second_delete.assert_not_called()
        with self.assertRaises(send_permits.GraphSendPermitBlocked):
            send_permits.issue_terminal_graph_send_permit(
                rooted,
                thread_ref,
                thread_ref,
                saga,
                owner.owner,
                owner.fencing_token,
            )

    def test_terminal_draft_review_cas_lost_ack_recovers_without_provider_work(self):
        for commit_outcome in ("no_apply", "apply_then_raise"):
            with self.subTest(commit_outcome=commit_outcome):
                rooted = _CommitOutcomeRootedFirestore()
                thread_id = f"thread-terminal-draft-cas-{commit_outcome}"
                saga, thread_ref, claim_ref = _terminal_refs(thread_id)
                owner = processing.TerminalSagaExecution(
                    owner="terminal-owner-a",
                    fencing_token=1,
                )
                rooted.add_thread(thread_ref)
                thread_ref.data["terminalReplyAttempt"].update({
                    "sourceGraphMessageId": saga[
                        "sourceGraphMessageId"
                    ],
                    "conversationId": saga["sourceConversationId"],
                    "recipient": saga["replyRecipient"],
                })
                capability = send_permits.issue_terminal_graph_send_permit(
                    rooted,
                    thread_ref,
                    claim_ref,
                    saga,
                    owner.owner,
                    owner.fencing_token,
                )
                prepared = _prepare_capability_for_send(
                    capability,
                    saga["sourceGraphMessageId"],
                    saga["replyRecipient"],
                )
                resolution_evidence = {
                    "reason": "campaign stopped after prepared draft",
                    "phase": "final_campaign_gate",
                    "draftId": prepared["draft_id"],
                    "providerSendStarted": False,
                    "automaticDeleteAttempted": False,
                }
                send_permits.resolve_graph_send_permit(
                    capability,
                    "needs_reconciliation",
                    evidence=resolution_evidence,
                )
                permit = send_permits.read_permit(capability)
                attempt = dict(thread_ref.data["terminalReplyAttempt"])
                committed_attempt = {
                    **attempt,
                    "status": "committed",
                    "outcome": "draft_needs_review",
                    "committedAt": send_permits.SERVER_TIMESTAMP,
                }
                with patch.object(processing, "_fs", rooted):
                    evidence_document = (
                        processing._terminal_reply_reconciliation_document(
                            "uid-1",
                            thread_id,
                            saga,
                            permit,
                            kind="draft_needs_review",
                            already_sent=False,
                            provider_send_started=False,
                            reason=resolution_evidence["reason"],
                        )
                    )
                rooted.commit_outcomes = [commit_outcome]
                with self.assertRaises(RuntimeError):
                    send_permits.cas_terminal_reply_transition(
                        rooted,
                        thread_ref,
                        claim_ref,
                        saga,
                        owner.owner,
                        owner.fencing_token,
                        expected_attempt_status="sending",
                        thread_patch={
                            "terminalReplyOwed": False,
                            "terminalReplyOutcome": "draft_needs_review",
                            "terminalReplyResolvedAt": (
                                send_permits.SERVER_TIMESTAMP
                            ),
                            "terminalReplyAttempt": committed_attempt,
                            "updatedAt": send_permits.SERVER_TIMESTAMP,
                        },
                        permit_settlement="settled_draft_needs_review",
                        capability=capability,
                        side_documents=(evidence_document,),
                    )

                rooted.commit_outcomes = []
                with patch.object(processing, "_fs", rooted), patch.object(
                    processing,
                    "_renew_terminal_saga_execution",
                    return_value=datetime.now(timezone.utc),
                ), patch.object(
                    processing,
                    "_clear_resolved_terminal_saga",
                ), patch.object(
                    processing,
                    "find_exact_sent_message_by_immutable_id",
                    side_effect=AssertionError("pre-send review cannot search Sent"),
                ) as sent_lookup, patch.object(
                    processing,
                    "send_reply_in_thread",
                    side_effect=AssertionError("pre-send review cannot resend"),
                ) as resend, patch.object(
                    email_module,
                    "_delete_graph_reply_draft",
                    side_effect=AssertionError("pre-send review cannot delete"),
                ) as delete_draft:
                    outcome = processing._settle_terminal_reply_obligation(
                        "uid-1",
                        "client-1",
                        thread_id,
                        {"Authorization": "Bearer test"},
                        saga["replyRecipient"],
                        saga,
                        terminal_saga_owner=owner,
                    )

                self.assertEqual("draft_needs_review", outcome)
                sent_lookup.assert_not_called()
                resend.assert_not_called()
                delete_draft.assert_not_called()
                self.assertFalse(thread_ref.data["terminalReplyOwed"])
                self.assertEqual(
                    "settled_draft_needs_review",
                    send_permits.read_permit(capability)["status"],
                )

    def test_pending_worker_immediately_settles_capability_draft_exit_to_exact_review(self):
        rooted = _RootedFirestore()
        token = "pending-worker-draft-review"
        thread_ref = _DocRef(
            {"clientId": "client-1"},
            doc_id="thread-pending-draft-review",
        )
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(
            loaded,
            doc_id="pending-draft-review",
        )
        rooted.add_thread(thread_ref)
        rooted.add_pending(pending_ref)
        capability = send_permits.issue_pending_graph_send_permit(
            rooted,
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )
        doc = types.SimpleNamespace(id=pending_ref.id, reference=pending_ref)
        draft_id = "draft-pending-draft-review"
        stack, send_posts = self._graph_202_unconfirmed_stack(
            rooted,
            source_id=loaded["msgId"],
            conversation_id=loaded["conversationId"],
            recipient=loaded["recipient"],
            draft_id=draft_id,
        )
        allow = CampaignAutomationDecision(
            state="allow",
            reason="",
            client_data={"status": "live"},
            metadata={"terminal": False, "stopKind": "none"},
        )
        review_commit_attempts = []
        settle_draft_review = pending_responses._cas_pending_draft_review

        def settle_with_lost_commit_ack(*args, **kwargs):
            rooted.commit_outcomes = ["apply_then_raise"]
            rooted.commit_attempts = 0
            rooted.after_apply = None
            with patch.object(
                rooted,
                "transaction",
                side_effect=lambda: _CommitOutcomeTransaction(rooted),
            ):
                result = settle_draft_review(*args, **kwargs)
            review_commit_attempts.append(rooted.commit_attempts)
            return result

        with stack, patch.object(
            processing,
            "resolve_outbound_mode",
            side_effect=["live", "paused"],
        ), patch.object(
            pending_responses,
            "get_pending_responses",
            return_value=[{"doc": doc, "data": dict(loaded)}],
        ), patch.object(
            pending_responses,
            "_claim_pending_response_for_send",
            return_value=token,
        ), patch.object(
            pending_responses,
            "_final_pending_response_send_fence",
            return_value=capability,
        ), patch.object(
            pending_responses,
            "_pending_claim_refs",
            return_value=(rooted, rooted.user_root, thread_ref, pending_ref),
        ), patch.object(
            pending_responses,
            "get_client_automation_decision",
            return_value=allow,
        ), patch.object(
            pending_responses,
            "_pending_response_column_contract_error",
            return_value=None,
        ), patch.object(
            pending_responses,
            "_cas_pending_draft_review",
            side_effect=settle_with_lost_commit_ack,
        ), patch.object(
            email_module,
            "_delete_graph_reply_draft",
            wraps=email_module._delete_graph_reply_draft,
        ) as delete_draft, patch.object(
            processing.requests,
            "delete",
            return_value=_GraphResponse(204),
        ) as request_delete:
            states = pending_responses.process_pending_responses(
                "uid-1",
                {"Authorization": "Bearer test"},
            )

        self.assertEqual([], states)
        self.assertEqual([], send_posts)
        self.assertEqual([1], review_commit_attempts)
        delete_draft.assert_not_called()
        request_delete.assert_not_called()
        self.assertIs(False, pending_ref.exists)
        permit = send_permits.read_permit(capability)
        self.assertEqual("settled_draft_needs_review", permit["status"])
        self.assertTrue(permit["draftReviewRequired"])
        self.assertIsNone(permit.get("requestStartedAt"))
        reviews = rooted.user_root.collection("graphSendDraftReviews").docs
        existing_reviews = [ref for ref in reviews.values() if ref.exists]
        self.assertEqual(1, len(existing_reviews))
        review_ref = existing_reviews[0]
        review = review_ref.get().to_dict()
        self.assertIs(review_ref, permit["draftReviewEvidenceRef"])
        self.assertEqual(
            send_permits._stable_evidence_hash(review),
            permit["draftReviewEvidenceHash"],
        )
        self.assertEqual("manual_review", review["status"])
        self.assertEqual("pendingGraphSendProtocol", review["source"])
        self.assertTrue(review["authoritative"])
        self.assertFalse(review["alreadySent"])
        self.assertFalse(review["providerSendStarted"])
        self.assertFalse(review["sendOutcomeUnknown"])
        self.assertFalse(review["retryAllowed"])
        self.assertFalse(review["automaticDeleteAttempted"])
        self.assertEqual(capability.permit_id, review["graphSendPermitId"])
        self.assertEqual(capability.immutable_hash, review["graphSendPermitHash"])
        self.assertEqual(draft_id, review["draftId"])
        self.assertEqual("prepared", review["draftMutationState"])
        self.assertEqual(
            permit["resolutionEvidenceHash"],
            review["draftResolutionEvidenceHash"],
        )
        self.assertEqual(
            permit["draftReviewEvidenceHash"],
            permit["pendingReconciliationEvidenceHash"],
        )
        self.assertIsNone(
            send_permits.issue_pending_graph_send_permit(
                rooted,
                thread_ref,
                pending_ref,
                dict(loaded),
                token,
            )
        )

    def test_stable_evidence_hash_uses_canonical_firestore_reference_paths(self):
        ref_a = _DocRef(
            {},
            doc_id="review-1",
            path="users/u/threads/t/graphSendReviews/review-1",
        )
        ref_b = _DocRef(
            {},
            doc_id="review-1",
            path="users/u/threads/t/graphSendReviews/review-1",
        )
        other_ref = _DocRef(
            {},
            doc_id="review-1",
            path="users/other/threads/t/graphSendReviews/review-1",
        )
        payload_a = {"reviewRef": ref_a, "nested": [{"auditRef": ref_a}]}
        payload_b = {"reviewRef": ref_b, "nested": [{"auditRef": ref_b}]}

        self.assertEqual(
            send_permits._stable_evidence_hash(payload_a),
            send_permits._stable_evidence_hash(payload_b),
        )
        self.assertNotEqual(
            send_permits._stable_evidence_hash(payload_a),
            send_permits._stable_evidence_hash({
                "reviewRef": other_ref,
                "nested": [{"auditRef": other_ref}],
            }),
        )

    def test_every_retained_permit_update_appends_validated_state_history(self):
        source = Path(send_permits.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        direct_updates = []
        issuance_sets = []
        exact_helper_updates = []
        exact_helper_calls = []
        orphan_helper_updates = []
        orphan_helper_calls = []
        for function in (
            node for node in tree.body if isinstance(node, ast.FunctionDef)
        ):
            stateful_patch_names = {
                target.id
                for node in ast.walk(function)
                if isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "_stateful_permit_patch"
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_commit_exact_local_transition"
                ):
                    state_patch = node.args[3] if len(node.args) > 3 else None
                    exact_helper_calls.append((
                        node.lineno,
                        isinstance(state_patch, ast.Name)
                        and state_patch.id in stateful_patch_names,
                    ))
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id
                    == "_enqueue_validated_orphaned_draft_permit_write"
                ):
                    orphan_helper_calls.append((
                        function.name,
                        node.lineno,
                        len(node.args) == 5
                        and isinstance(node.args[2], ast.Name)
                        and node.args[2].id
                        in {
                            "orphan_source_permit",
                            "capabilityless_source_permit",
                        }
                        and isinstance(node.args[3], ast.Name)
                        and node.args[3].id
                        in {
                            "orphan_state_patch",
                            "pre_settlement_state_patch",
                        }
                        and isinstance(node.args[4], ast.Name)
                        and node.args[4].id == "settlement_state_patch",
                    ))
                if (
                    not isinstance(node, ast.Call)
                    or not isinstance(node.func, ast.Attribute)
                    or node.func.attr not in {"update", "set", "delete"}
                    or not node.args
                ):
                    continue
                target = node.args[0]
                target_name = (
                    target.id
                    if isinstance(target, ast.Name)
                    else target.attr
                    if isinstance(target, ast.Attribute)
                    else ""
                )
                if not target_name.endswith("permit_ref"):
                    continue
                if node.func.attr == "set":
                    issuance_sets.append(node)
                    continue
                state_patch = node.args[1] if len(node.args) > 1 else None
                if (
                    function.name == "_commit_exact_local_transition"
                    and node.func.attr == "update"
                    and isinstance(state_patch, ast.Name)
                    and state_patch.id == "state_patch"
                ):
                    exact_helper_updates.append(node.lineno)
                elif (
                    function.name
                    == "_enqueue_validated_orphaned_draft_permit_write"
                    and node.func.attr == "update"
                    and isinstance(state_patch, ast.Dict)
                    and len(state_patch.values) == 2
                    and state_patch.keys == [None, None]
                    and all(
                        isinstance(value, ast.Name)
                        and value.id
                        in {
                            "orphan_state_patch",
                            "settlement_state_patch",
                        }
                        for value in state_patch.values
                    )
                ):
                    orphan_helper_updates.append(node.lineno)
                elif not (
                    node.func.attr == "update"
                    and (
                        (
                            isinstance(state_patch, ast.Call)
                            and isinstance(state_patch.func, ast.Name)
                            and state_patch.func.id
                            == "_stateful_permit_patch"
                        )
                        or (
                            isinstance(state_patch, ast.Name)
                            and state_patch.id in stateful_patch_names
                        )
                    )
                ):
                    direct_updates.append(node.lineno)

        self.assertEqual([], direct_updates)
        self.assertEqual(2, len(exact_helper_updates))
        self.assertTrue(exact_helper_calls)
        self.assertTrue(all(valid for _line, valid in exact_helper_calls))
        self.assertEqual(1, len(orphan_helper_updates))
        self.assertEqual(4, len(orphan_helper_calls))
        self.assertEqual(
            {
                "cas_terminal_reply_transition",
                "reconcile_pending_graph_send_permit",
            },
            {function for function, _line, _valid in orphan_helper_calls},
        )
        self.assertTrue(
            all(valid for _function, _line, valid in orphan_helper_calls)
        )
        self.assertEqual(2, len(issuance_sets))
        for issuance in issuance_sets:
            self.assertIsInstance(issuance.args[1], ast.Name)
            self.assertEqual("permit", issuance.args[1].id)

    def test_raw_status_forgery_cannot_invent_accepted_provider_outcome(self):
        prepared = self._prepare_pending_draft(thread_id="thread-forged-accepted")
        permit_ref = prepared["capability"].permit_ref
        permit_ref.data.update({
            "status": "accepted",
            "requestStartedAt": datetime.now(timezone.utc),
            "capabilityConsumedAt": datetime.now(timezone.utc),
            "sendPreparedEnvelopeHash": prepared["prepared"][
                "preparedEnvelopeHash"
            ],
            "resolvedAt": datetime.now(timezone.utc),
            "resolutionEvidence": {"httpStatus": 202},
            "resolutionEvidenceHash": send_permits._hash({"httpStatus": 202}),
        })
        permit_ref.version += 1

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitError,
            "state|transition|accepted",
        ):
            send_permits.read_permit(prepared["capability"])

    def test_raw_prepared_forgery_cannot_skip_draft_operation_chain(self):
        token = "pending-worker-a"
        thread_ref = _DocRef({}, doc_id="thread-forged-prepared")
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-forged-prepared")
        capability = self._issue_pending(
            thread_ref, pending_ref, dict(loaded), token
        )
        envelope = send_permits._prepared_envelope(
            capability,
            source_graph_message_id=loaded["msgId"],
            draft_id="forged-draft",
            subject=PROVIDER_DRAFT_SUBJECT,
            html_body="<p>Forged prepared body.</p>",
            to_recipients=[loaded["recipient"]],
            cc_recipients=[],
            attachments=[],
        )
        capability.permit_ref.data.update({
            "draftPreparation": {
                "version": send_permits.GRAPH_DRAFT_PREPARATION_VERSION,
                "state": "prepared",
                "preparedAt": datetime.now(timezone.utc),
            },
            "preparedEnvelope": envelope,
        })
        capability.permit_ref.version += 1

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitError,
            "draft|state|transition|chain",
        ):
            send_permits.read_permit(capability)

    def test_definitely_unsent_cannot_be_forged_after_send_boundary(self):
        prepared = self._prepare_pending_draft(thread_id="thread-forged-unsent")
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
        permit_ref = prepared["capability"].permit_ref
        forged_evidence = {"phase": "forged_after_send"}
        permit_ref.data.update({
            "status": "definitely_not_sent",
            "resolvedAt": datetime.now(timezone.utc),
            "resolutionEvidence": forged_evidence,
            "resolutionEvidenceHash": send_permits._hash(forged_evidence),
        })
        permit_ref.version += 1

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitError,
            "definitely|state|transition|send",
        ):
            send_permits.read_permit(prepared["capability"])

    def test_settled_sent_rejects_actual_sent_envelope_drift(self):
        def add_extra_to(evidence):
            evidence["toRecipients"].append(
                {"emailAddress": {"address": "outsider@example.test"}}
            )

        def change_cc(evidence):
            evidence["ccRecipients"] = [
                {"emailAddress": {"address": "outsider@example.test"}}
            ]

        def remove_cc(evidence):
            evidence.pop("ccRecipients")

        def add_bcc(evidence):
            evidence["bccRecipients"] = [
                {"emailAddress": {"address": "hidden@example.test"}}
            ]

        def change_body(evidence):
            evidence["body"] = {
                "contentType": "HTML",
                "content": "<p>Wrong actual provider body.</p>",
            }

        def remove_body(evidence):
            evidence.pop("body")

        def mark_as_draft(evidence):
            evidence["isDraft"] = True

        def remove_draft_state(evidence):
            evidence.pop("isDraft")

        cases = {
            "extra_to": add_extra_to,
            "changed_cc": change_cc,
            "missing_cc": remove_cc,
            "nonempty_bcc": add_bcc,
            "wrong_body": change_body,
            "missing_body": remove_body,
            "still_draft": mark_as_draft,
            "missing_draft_state": remove_draft_state,
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                prepared = self._prepare_pending_draft(
                    thread_id=f"thread-drifted-sent-envelope-{label}"
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
                evidence = _exact_sent_evidence(
                    prepared["capability"],
                    html_body=prepared["html_body"],
                )
                mutate(evidence)

                with self.assertRaisesRegex(
                    send_permits.GraphSendPermitBlocked,
                    "exact Sent|envelope|recipient|body",
                ):
                    send_permits.cas_pending_claim_transition(
                        self.firestore,
                        prepared["thread_ref"],
                        prepared["pending_ref"],
                        prepared["loaded"],
                        prepared["loaded"]["processingBy"],
                        delete_pending=True,
                        capability=prepared["capability"],
                        permit_settlement="settled_sent",
                        sent_evidence=evidence,
                    )

                self.assertTrue(prepared["pending_ref"].exists)
                self.assertEqual(
                    "accepted",
                    send_permits.read_permit(prepared["capability"])["status"],
                )

    def test_settled_sent_accepts_exact_actual_sent_envelope(self):
        prepared = self._prepare_pending_draft(
            thread_id="thread-exact-sent-envelope"
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

        settled = send_permits.cas_pending_claim_transition(
            self.firestore,
            prepared["thread_ref"],
            prepared["pending_ref"],
            prepared["loaded"],
            prepared["loaded"]["processingBy"],
            delete_pending=True,
            capability=prepared["capability"],
            permit_settlement="settled_sent",
            sent_evidence=_exact_sent_evidence(
                prepared["capability"],
                html_body=prepared["html_body"],
            ),
            side_documents=(_pending_completion_document(
                prepared["thread_ref"],
                prepared["pending_ref"],
                prepared["loaded"],
                prepared["capability"],
                _exact_sent_evidence(
                    prepared["capability"],
                    html_body=prepared["html_body"],
                ),
            ),),
        )

        self.assertTrue(settled)
        self.assertFalse(prepared["pending_ref"].exists)
        self.assertEqual(
            "settled_sent",
            send_permits.read_permit(prepared["capability"])["status"],
        )

    def test_settled_sent_requires_exact_actual_attachment_projection(self):
        planned_attachment = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": "sitesift-logo.png",
            "contentType": "image/png",
            "contentBytes": "bG9nby1ieXRlcw==",
            "contentId": "sitesift-logo-1",
            "isInline": True,
        }
        actual_attachment = {
            **planned_attachment,
            "id": "provider-attachment-1",
        }

        def missing_planned(evidence):
            evidence["attachments"] = []

        def add_extra(evidence):
            evidence["attachments"].append({
                **actual_attachment,
                "id": "provider-attachment-extra",
                "name": "extra.png",
            })

        def change(field, value):
            def mutate(evidence):
                evidence["attachments"][0][field] = value

            return mutate

        def missing_actual(evidence):
            evidence.pop("attachments")

        def malformed_actual(evidence):
            evidence["attachments"] = "not-a-list"

        cases = {
            "exact": None,
            "missing_planned": missing_planned,
            "extra": add_extra,
            "content_bytes": change("contentBytes", "dGFtcGVyZWQ="),
            "name": change("name", "other-logo.png"),
            "content_type": change("contentType", "image/jpeg"),
            "content_id": change("contentId", "other-cid"),
            "inline": change("isInline", False),
            "missing_actual": missing_actual,
            "malformed_actual": malformed_actual,
        }

        for label, mutate in cases.items():
            with self.subTest(label=label):
                prepared = self._prepare_pending_draft(
                    thread_id=f"thread-sent-attachment-{label}",
                    attachments=[planned_attachment],
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
                evidence = _exact_sent_evidence(
                    prepared["capability"],
                    html_body=prepared["html_body"],
                    attachments=[copy.deepcopy(actual_attachment)],
                )
                if mutate is not None:
                    mutate(evidence)

                transition = lambda: send_permits.cas_pending_claim_transition(
                    self.firestore,
                    prepared["thread_ref"],
                    prepared["pending_ref"],
                    prepared["loaded"],
                    prepared["loaded"]["processingBy"],
                    delete_pending=True,
                    capability=prepared["capability"],
                    permit_settlement="settled_sent",
                    sent_evidence=evidence,
                    side_documents=(_pending_completion_document(
                        prepared["thread_ref"],
                        prepared["pending_ref"],
                        prepared["loaded"],
                        prepared["capability"],
                        evidence,
                    ),),
                )
                if label == "exact":
                    self.assertTrue(transition())
                    self.assertFalse(prepared["pending_ref"].exists)
                else:
                    with self.assertRaisesRegex(
                        send_permits.GraphSendPermitBlocked,
                        "exact Sent|attachment",
                    ):
                        transition()
                    self.assertTrue(prepared["pending_ref"].exists)

    def test_exact_actual_attachment_proof_is_an_order_independent_multiset(self):
        planned = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": "logo.png",
                "contentType": "image/png",
                "contentBytes": "bG9nby1ieXRlcw==",
                "contentId": "Logo-A",
                "isInline": True,
            },
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": "terms.pdf",
                "contentType": "application/pdf",
                "contentBytes": "dGVybXMtYnl0ZXM=",
                "contentId": None,
                "isInline": False,
            },
        ]
        prepared = self._prepare_pending_draft(
            thread_id="thread-sent-attachment-order",
            attachments=planned,
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
        actual = [
            {**planned[1], "id": "provider-terms"},
            {**planned[0], "id": "provider-logo"},
        ]

        settled = send_permits.cas_pending_claim_transition(
            self.firestore,
            prepared["thread_ref"],
            prepared["pending_ref"],
            prepared["loaded"],
            prepared["loaded"]["processingBy"],
            delete_pending=True,
            capability=prepared["capability"],
            permit_settlement="settled_sent",
            sent_evidence=_exact_sent_evidence(
                prepared["capability"],
                html_body=prepared["html_body"],
                attachments=actual,
            ),
            side_documents=(_pending_completion_document(
                prepared["thread_ref"],
                prepared["pending_ref"],
                prepared["loaded"],
                prepared["capability"],
                _exact_sent_evidence(
                    prepared["capability"],
                    html_body=prepared["html_body"],
                    attachments=actual,
                ),
            ),),
        )

        self.assertTrue(settled)
        self.assertFalse(prepared["pending_ref"].exists)

    def test_exact_sent_timestamp_does_not_depend_on_provider_clock_order(self):
        for label, delta in (
            ("subsecond", timedelta(microseconds=1)),
            ("material_clock_drift", timedelta(hours=24)),
        ):
            with self.subTest(label=label):
                prepared = self._prepare_pending_draft(
                    thread_id=f"thread-sent-clock-skew-{label}"
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
                evidence = _exact_sent_evidence(
                    prepared["capability"],
                    html_body=prepared["html_body"],
                )
                permit = send_permits.read_permit(prepared["capability"])
                evidence["sentDateTime"] = permit["requestStartedAt"] - delta
                transition = lambda: send_permits.cas_pending_claim_transition(
                    self.firestore,
                    prepared["thread_ref"],
                    prepared["pending_ref"],
                    prepared["loaded"],
                    prepared["loaded"]["processingBy"],
                    delete_pending=True,
                    capability=prepared["capability"],
                    permit_settlement="settled_sent",
                    sent_evidence=evidence,
                    side_documents=(_pending_completion_document(
                        prepared["thread_ref"],
                        prepared["pending_ref"],
                        prepared["loaded"],
                        prepared["capability"],
                        evidence,
                    ),),
                )
                self.assertTrue(transition())
                self.assertFalse(prepared["pending_ref"].exists)

    def test_unknown_permit_state_key_is_rejected(self):
        prepared = self._prepare_pending_draft(thread_id="thread-forged-key")
        prepared["capability"].permit_ref.data["clientCanApproveSend"] = True
        prepared["capability"].permit_ref.version += 1

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitError,
            "unknown|schema|state",
        ):
            send_permits.read_permit(prepared["capability"])

    def test_settled_draft_review_requires_original_resolution_evidence_and_hash(self):
        mutations = {
            "missing_evidence": lambda permit: permit.pop(
                "resolutionEvidence", None
            ),
            "tampered_evidence": lambda permit: permit.update({
                "resolutionEvidence": {
                    **dict(permit["resolutionEvidence"]),
                    "reason": "forged retained-draft reason",
                },
            }),
            "missing_hash": lambda permit: permit.pop(
                "resolutionEvidenceHash", None
            ),
            "tampered_hash": lambda permit: permit.update({
                "resolutionEvidenceHash": "forged-resolution-hash",
            }),
        }

        for label, mutate in mutations.items():
            with self.subTest(label=label):
                settled = self._settle_terminal_draft_review(
                    thread_id=f"thread-settled-draft-resolution-{label}"
                )
                permit_ref = settled["capability"].permit_ref
                mutate(permit_ref.data)
                permit_ref.version += 1

                with self.assertRaisesRegex(
                    send_permits.GraphSendPermitError,
                    "draft|resolution|evidence|history|state",
                ):
                    send_permits.read_permit(settled["capability"])

    def test_terminal_draft_mutation_review_compatibility_matrix(self):
        for ambiguity in ("create", "patch", "attachment"):
            with self.subTest(ambiguity=ambiguity):
                settled = self._settle_terminal_draft_mutation_review(
                    thread_id=f"thread-terminal-{ambiguity}-ambiguity",
                    ambiguity=ambiguity,
                )
                self.assertEqual(
                    "settled_draft_needs_review",
                    settled["permit"]["status"],
                )

    def test_pending_draft_mutation_review_compatibility_matrix(self):
        for ambiguity in ("create", "patch", "attachment"):
            with self.subTest(ambiguity=ambiguity):
                settled = self._settle_pending_draft_mutation_review(
                    thread_id=f"thread-pending-{ambiguity}-ambiguity",
                    ambiguity=ambiguity,
                )
                self.assertEqual(
                    "settled_draft_needs_review",
                    settled["permit"]["status"],
                )

    def test_pending_draft_review_settlement_recovers_both_commit_outcomes(self):
        original_firestore = self.firestore
        try:
            for commit_outcome, expected_attempts in (
                ("no_apply", 2),
                ("apply_then_raise", 1),
            ):
                with self.subTest(commit_outcome=commit_outcome):
                    self.firestore = _CommitOutcomeFirestore()
                    settled = self._settle_pending_draft_mutation_review(
                        thread_id=(
                            "thread-pending-draft-review-"
                            f"{commit_outcome}"
                        ),
                        ambiguity="patch",
                        commit_outcome=commit_outcome,
                    )
                    self.assertEqual(
                        expected_attempts,
                        settled["settlementCommitAttempts"],
                    )
                    self.assertEqual(
                        "settled_draft_needs_review",
                        settled["permit"]["status"],
                    )
                    self.assertFalse(
                        settled["capability"].issuer_ref.exists
                    )
                    self.assertTrue(settled["review_ref"].exists)
                    self.assertEqual(
                        send_permits._stable_evidence_hash(
                            settled["review_ref"].data
                        ),
                        settled["permit"]["draftReviewEvidenceHash"],
                    )
        finally:
            self.firestore = original_firestore

    def _assert_settled_attachment_ambiguity_tamper_is_blocked(
        self,
        *,
        issuer: str,
        settle,
    ):
        def set_active(preparation, value):
            preparation["activeAttachment"] = value

        def drift_active_field(preparation, field, value):
            preparation["activeAttachment"][field] = value

        mutations = {
            "missing_active_attachment": lambda preparation: set_active(
                preparation,
                None,
            ),
            "incomplete_active_attachment": lambda preparation: set_active(
                preparation,
                {},
            ),
            "attachment_index_drift": lambda preparation: drift_active_field(
                preparation,
                "index",
                1,
            ),
            "attachment_hash_drift": lambda preparation: drift_active_field(
                preparation,
                "attachmentHash",
                "forged-attachment-hash",
            ),
            "attachment_request_hash_drift": (
                lambda preparation: drift_active_field(
                    preparation,
                    "requestHash",
                    "forged-request-hash",
                )
            ),
        }
        gates = {
            "validate": lambda settled: send_permits._validate_permit(
                settled["capability"].permit_ref.data
            ),
            "read": lambda settled: send_permits.read_permit(
                settled["capability"]
            ),
            "staging": lambda settled: send_permits.assert_terminal_staging_allowed(
                self.firestore.transaction(),
                settled["thread_ref"],
            ),
            "cleanup": lambda settled: send_permits.assert_terminal_reply_permit_settled(
                self.firestore.transaction(),
                settled["thread_ref"],
            ),
        }

        for mutation_label, mutate in mutations.items():
            for gate_label, gate in gates.items():
                with self.subTest(
                    issuer=issuer,
                    mutation=mutation_label,
                    gate=gate_label,
                ):
                    settled = settle(
                        thread_id=(
                            f"thread-{issuer}-attachment-tamper-"
                            f"{mutation_label}-{gate_label}"
                        ),
                        ambiguity="attachment",
                    )
                    permit_ref = settled["capability"].permit_ref
                    mutate(permit_ref.data["draftPreparation"])
                    permit_ref.version += 1
                    expected_error = (
                        send_permits.GraphSendPermitError
                        if gate_label in {"validate", "read"}
                        else send_permits.GraphSendPermitBlocked
                    )
                    with self.assertRaises(expected_error):
                        gate(settled)

    def test_terminal_settled_attachment_ambiguity_tamper_is_blocked(self):
        self._assert_settled_attachment_ambiguity_tamper_is_blocked(
            issuer="terminal",
            settle=self._settle_terminal_draft_mutation_review,
        )

    def test_pending_settled_attachment_ambiguity_tamper_is_blocked(self):
        self._assert_settled_attachment_ambiguity_tamper_is_blocked(
            issuer="pending",
            settle=self._settle_pending_draft_mutation_review,
        )

    def test_settled_draft_review_marker_ref_and_hash_drift_is_rejected(self):
        def drift_ref(permit):
            retained_ref = permit["draftReviewEvidenceRef"]
            permit["draftReviewEvidenceRef"] = _AliasDocRef(
                retained_ref,
                (
                    "users/other/terminalGraphSendReviews/"
                    f"{retained_ref.id}"
                ),
            )

        mutations = {
            "missing_marker": lambda permit: permit.pop(
                "draftReviewRequired", None
            ),
            "false_marker": lambda permit: permit.update({
                "draftReviewRequired": False,
            }),
            "missing_ref": lambda permit: permit.pop(
                "draftReviewEvidenceRef", None
            ),
            "canonical_ref_path_drift": drift_ref,
            "missing_hash": lambda permit: permit.pop(
                "draftReviewEvidenceHash", None
            ),
            "tampered_hash": lambda permit: permit.update({
                "draftReviewEvidenceHash": "forged-review-hash",
            }),
        }

        for label, mutate in mutations.items():
            with self.subTest(label=label):
                settled = self._settle_terminal_draft_review(
                    thread_id=f"thread-settled-draft-linkage-{label}"
                )
                permit_ref = settled["capability"].permit_ref
                mutate(permit_ref.data)
                permit_ref.version += 1

                with self.assertRaisesRegex(
                    send_permits.GraphSendPermitError,
                    "draft|review|history|state|linkage",
                ):
                    send_permits.read_permit(settled["capability"])

    def test_settled_draft_review_ref_path_drift_blocks_read_staging_and_cleanup(self):
        def drifted_settlement(label):
            settled = self._settle_terminal_draft_review(
                thread_id=f"thread-settled-draft-gate-{label}"
            )
            permit_ref = settled["capability"].permit_ref
            retained_ref = permit_ref.data["draftReviewEvidenceRef"]
            permit_ref.data["draftReviewEvidenceRef"] = _AliasDocRef(
                retained_ref,
                (
                    "users/other/terminalGraphSendReviews/"
                    f"{retained_ref.id}"
                ),
            )
            permit_ref.version += 1
            return settled

        gates = {
            "read": lambda settled: send_permits.read_permit(
                settled["capability"]
            ),
            "staging": lambda settled: send_permits.assert_terminal_staging_allowed(
                self.firestore.transaction(),
                settled["thread_ref"],
            ),
            "cleanup": lambda settled: send_permits.assert_terminal_reply_permit_settled(
                self.firestore.transaction(),
                settled["thread_ref"],
            ),
        }

        for label, gate in gates.items():
            with self.subTest(gate=label):
                settled = drifted_settlement(label)
                expected_error = (
                    send_permits.GraphSendPermitError
                    if label == "read"
                    else send_permits.GraphSendPermitBlocked
                )
                with self.assertRaises(expected_error):
                    gate(settled)

    def test_settled_draft_review_document_drift_blocks_read_staging_and_cleanup(self):
        def mutate_missing(settled):
            settled["review_ref"].data = {}
            settled["review_ref"].exists = False
            settled["review_ref"].version += 1

        def mutate_tampered(settled):
            settled["review_ref"].data["failureReason"] = (
                "forged durable review reason"
            )
            settled["review_ref"].version += 1

        def mutate_nonliteral_exists(settled):
            settled["review_ref"].exists = 1
            settled["review_ref"].version += 1

        def mutate_redirected_after_delete(settled):
            retained_ref = settled["review_ref"]
            retained_ref.data = {}
            retained_ref.exists = False
            retained_ref.version += 1
            redirected_ref = _DocRef(
                {},
                exists=False,
                doc_id=retained_ref.id,
                path=(
                    "users/other/terminalGraphSendReviews/"
                    f"{retained_ref.id}"
                ),
            )
            permit_ref = settled["capability"].permit_ref
            permit_ref.data.update({
                "draftReviewEvidenceRef": redirected_ref,
                "draftReviewEvidenceHash": "forged-review-hash",
            })
            permit_ref.version += 1

        mutations = {
            "missing": mutate_missing,
            "tampered": mutate_tampered,
            "nonliteral_exists": mutate_nonliteral_exists,
            "redirected_after_delete": mutate_redirected_after_delete,
        }
        gates = {
            "read": lambda settled: send_permits.read_permit(
                settled["capability"]
            ),
            "staging": lambda settled: send_permits.assert_terminal_staging_allowed(
                self.firestore.transaction(),
                settled["thread_ref"],
            ),
            "cleanup": lambda settled: send_permits.assert_terminal_reply_permit_settled(
                self.firestore.transaction(),
                settled["thread_ref"],
            ),
        }

        for mutation_label, mutate in mutations.items():
            for gate_label, gate in gates.items():
                with self.subTest(
                    mutation=mutation_label,
                    gate=gate_label,
                ):
                    settled = self._settle_terminal_draft_review(
                        thread_id=(
                            "thread-settled-draft-document-"
                            f"{mutation_label}-{gate_label}"
                        )
                    )
                    mutate(settled)
                    expected_error = (
                        send_permits.GraphSendPermitError
                        if gate_label == "read"
                        else send_permits.GraphSendPermitBlocked
                    )
                    with self.assertRaises(expected_error):
                        gate(settled)

    def test_unresolved_settled_draft_review_blocks_pending_claim_and_reissue(self):
        settled = self._settle_pending_draft_mutation_review(
            thread_id="thread-valid-settled-draft-reissue",
            ambiguity="patch",
        )
        thread_ref = settled["thread_ref"]
        pointer_before = copy.deepcopy(
            thread_ref.data["activeGraphSendPermit"]
        )
        permits_before = [
            ref
            for ref in thread_ref.collection(
                "graphSendPermits"
            ).docs.values()
            if ref.exists
        ]
        token = "pending-valid-review-reissue"
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(
            loaded,
            doc_id="pending-valid-review-reissue",
            path=(
                "users/uid-pending-mutation/pendingResponses/"
                "pending-valid-review-reissue"
            ),
        )

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "draft review|operator|unresolved",
        ):
            send_permits.assert_pending_claim_allowed(
                self.firestore.transaction(),
                thread_ref,
            )
        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "draft review|operator|unresolved",
        ):
            send_permits.issue_pending_graph_send_permit(
                self.firestore,
                thread_ref,
                pending_ref,
                dict(loaded),
                token,
            )

        self.assertEqual(
            pointer_before,
            thread_ref.data["activeGraphSendPermit"],
        )
        self.assertNotIn("graphSendPermitId", pending_ref.data)
        self.assertNotIn("graphSendPermitHash", pending_ref.data)
        permits_after = [
            ref
            for ref in thread_ref.collection(
                "graphSendPermits"
            ).docs.values()
            if ref.exists
        ]
        self.assertEqual(permits_before, permits_after)

    def test_authenticated_operator_resolution_unlocks_exact_reissue_without_deleting_review(self):
        settled = self._settle_pending_draft_mutation_review(
            thread_id="thread-explicit-draft-review-resolution",
            ambiguity="patch",
        )
        thread_ref = settled["thread_ref"]
        review_ref = settled["review_ref"]
        permit_before = send_permits.read_permit(settled["capability"])
        original_review_hash = permit_before["draftReviewEvidenceHash"]
        settlement_id = "draft-review-settlement-1"
        audit_ref = _DocRef(
            {},
            exists=False,
            doc_id=settlement_id,
            path=(
                "users/uid-pending-mutation/graphSendDraftReviewSettlements/"
                f"{settlement_id}"
            ),
        )

        resolved = send_permits.operator_resolve_pending_graph_draft_review(
            self.firestore,
            thread_ref,
            expected_permit_id=permit_before["permitId"],
            expected_permit_hash=permit_before["immutableHash"],
            expected_review_evidence_hash=original_review_hash,
            review_ref=review_ref,
            action="confirm_retained_draft_not_actionable",
            operator_id="authenticated-operator-uid",
            operator_reason="The retained provider draft was manually discarded.",
            settlement_id=settlement_id,
            audit_ref=audit_ref,
        )

        self.assertTrue(resolved)
        self.assertTrue(review_ref.exists)
        self.assertTrue(audit_ref.exists)
        permit_after = send_permits.read_permit(settled["capability"])
        review_after = review_ref.get().to_dict()
        audit_after = audit_ref.get().to_dict()
        self.assertEqual("settled_draft_review_resolved", permit_after["status"])
        self.assertFalse(permit_after["draftReviewRequired"])
        self.assertEqual(
            original_review_hash,
            permit_after["operatorOriginalReconciliationEvidenceHash"],
        )
        self.assertEqual(
            send_permits._stable_evidence_hash(review_after),
            permit_after["draftReviewEvidenceHash"],
        )
        self.assertEqual(
            permit_after["draftReviewEvidenceHash"],
            permit_after["operatorResolvedReviewEvidenceHash"],
        )
        self.assertEqual(
            send_permits._stable_evidence_hash(audit_after),
            permit_after["operatorSettlementAuditHash"],
        )
        self.assertEqual("resolved_not_actionable", review_after["status"])
        self.assertEqual(
            "retained_draft_not_actionable",
            review_after["resolution"],
        )
        self.assertEqual(original_review_hash, review_after["originalReviewEvidenceHash"])
        self.assertTrue(review_after["retryAllowed"])
        self.assertFalse(review_after["automaticDeleteAttempted"])
        self.assertEqual(
            "retained_draft_not_actionable",
            audit_after["resolution"],
        )
        self.assertFalse(audit_after["providerSendStarted"])
        self.assertFalse(audit_after["automaticDeleteAttempted"])

        token = "pending-after-explicit-review-resolution"
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(
            loaded,
            doc_id="pending-after-explicit-review-resolution",
            path=(
                "users/uid-pending-mutation/pendingResponses/"
                "pending-after-explicit-review-resolution"
            ),
        )
        replacement = send_permits.issue_pending_graph_send_permit(
            self.firestore,
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )
        self.assertIsNotNone(replacement)
        self.assertEqual(
            replacement.permit_id,
            thread_ref.data["activeGraphSendPermit"]["permitId"],
        )

    def test_operator_draft_review_resolution_exact_replay_is_revision_stable(self):
        rooted = _CommitOutcomeFirestore()
        settled, resolution = self._pending_draft_review_resolution_case(
            firestore=rooted,
            suffix="stable-replay",
        )
        self.assertTrue(
            send_permits.operator_resolve_pending_graph_draft_review(
                rooted,
                settled["thread_ref"],
                **resolution,
            )
        )
        permit_after = copy.deepcopy(settled["capability"].permit_ref.data)
        commit_attempts_after = rooted.commit_attempts

        self.assertTrue(
            send_permits.operator_resolve_pending_graph_draft_review(
                rooted,
                settled["thread_ref"],
                **resolution,
            )
        )

        self.assertEqual(permit_after, settled["capability"].permit_ref.data)
        self.assertEqual(
            permit_after["stateRevision"],
            settled["capability"].permit_ref.data["stateRevision"],
        )
        self.assertEqual(
            permit_after["stateHistory"],
            settled["capability"].permit_ref.data["stateHistory"],
        )
        self.assertEqual(commit_attempts_after, rooted.commit_attempts)

    def test_operator_draft_review_resolution_recovers_exact_commit_outcomes(self):
        for commit_outcome, expected_attempts in (
            ("no_apply", 2),
            ("apply_then_raise", 1),
        ):
            with self.subTest(commit_outcome=commit_outcome):
                rooted = _CommitOutcomeFirestore()
                settled, resolution = self._pending_draft_review_resolution_case(
                    firestore=rooted,
                    suffix=commit_outcome,
                )
                payloads_before = len(rooted.commit_payloads)
                attempts_before = rooted.commit_attempts
                rooted.commit_outcomes = [commit_outcome]

                self.assertTrue(
                    send_permits.operator_resolve_pending_graph_draft_review(
                        rooted,
                        settled["thread_ref"],
                        **resolution,
                    )
                )

                self.assertEqual(
                    attempts_before + expected_attempts,
                    rooted.commit_attempts,
                )
                payloads = rooted.commit_payloads[payloads_before:]
                self.assertEqual(expected_attempts, len(payloads))
                if commit_outcome == "no_apply":
                    self.assertEqual(payloads[0], payloads[1])
                permit = send_permits.read_permit(settled["capability"])
                self.assertEqual(
                    "settled_draft_review_resolved",
                    permit["status"],
                )
                self.assertTrue(resolution["review_ref"].exists)
                self.assertTrue(resolution["audit_ref"].exists)

    def test_operator_draft_review_resolution_repeated_no_apply_is_typed_retry(self):
        rooted = _CommitOutcomeFirestore()
        settled, resolution = self._pending_draft_review_resolution_case(
            firestore=rooted,
            suffix="repeated-no-apply",
        )
        source_permit = copy.deepcopy(settled["capability"].permit_ref.data)
        source_review = copy.deepcopy(resolution["review_ref"].data)
        attempts_before = rooted.commit_attempts
        payloads_before = len(rooted.commit_payloads)
        rooted.commit_outcomes = ["no_apply", "no_apply"]

        with self.assertRaises(send_permits.GraphSendPermitLocalRetryable):
            send_permits.operator_resolve_pending_graph_draft_review(
                rooted,
                settled["thread_ref"],
                **resolution,
            )

        self.assertEqual(attempts_before + 2, rooted.commit_attempts)
        self.assertEqual(
            rooted.commit_payloads[payloads_before],
            rooted.commit_payloads[payloads_before + 1],
        )
        self.assertEqual(source_permit, settled["capability"].permit_ref.data)
        self.assertEqual(source_review, resolution["review_ref"].data)
        self.assertFalse(resolution["audit_ref"].exists)

    def test_operator_draft_review_resolution_corrupt_target_and_replay_drift_fail_closed(self):
        rooted = _CommitOutcomeFirestore()
        settled, resolution = self._pending_draft_review_resolution_case(
            firestore=rooted,
            suffix="corrupt-target",
        )
        rooted.commit_outcomes = ["apply_then_raise"]

        def corrupt_target():
            resolution["review_ref"].data["resolution"] = "drifted"
            resolution["review_ref"].version += 1

        rooted.after_apply = corrupt_target
        with self.assertRaises(send_permits.GraphSendPermitError) as raised:
            send_permits.operator_resolve_pending_graph_draft_review(
                rooted,
                settled["thread_ref"],
                **resolution,
            )
        self.assertNotIsInstance(
            raised.exception,
            send_permits.GraphSendPermitLocalRetryable,
        )

        replay_root = _CommitOutcomeFirestore()
        replay_settled, replay_resolution = (
            self._pending_draft_review_resolution_case(
                firestore=replay_root,
                suffix="drifted-replay",
            )
        )
        self.assertTrue(
            send_permits.operator_resolve_pending_graph_draft_review(
                replay_root,
                replay_settled["thread_ref"],
                **replay_resolution,
            )
        )
        revision = replay_settled["capability"].permit_ref.data[
            "stateRevision"
        ]
        replay_resolution["audit_ref"].data["operatorReason"] = "drifted"
        replay_resolution["audit_ref"].version += 1
        with self.assertRaises(send_permits.GraphSendPermitBlocked):
            send_permits.operator_resolve_pending_graph_draft_review(
                replay_root,
                replay_settled["thread_ref"],
                **replay_resolution,
            )
        self.assertEqual(
            revision,
            replay_settled["capability"].permit_ref.data["stateRevision"],
        )

    def test_operator_draft_review_resolution_rejects_ambiguous_send_review_path(self):
        rooted = _CommitOutcomeFirestore()
        settled, resolution = self._pending_draft_review_resolution_case(
            firestore=rooted,
            suffix="ambiguous-path",
        )
        source_permit = copy.deepcopy(settled["capability"].permit_ref.data)
        resolution["review_ref"] = _AliasDocRef(
            settled["review_ref"],
            (
                f"{settled['thread_ref'].path}/graphSendReviews/"
                f"pending-{resolution['expected_permit_id']}"
            ),
        )

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "draft review.*path|canonical",
        ):
            send_permits.operator_resolve_pending_graph_draft_review(
                rooted,
                settled["thread_ref"],
                **resolution,
            )

        self.assertEqual(source_permit, settled["capability"].permit_ref.data)

    def test_settled_draft_review_document_drift_blocks_reissue_and_completion(self):
        def mutate_missing(settled):
            settled["review_ref"].data = {}
            settled["review_ref"].exists = False
            settled["review_ref"].version += 1

        def mutate_tampered(settled):
            settled["review_ref"].data["failureReason"] = (
                "forged durable review reason"
            )
            settled["review_ref"].version += 1

        for mutation_label, mutate in {
            "missing": mutate_missing,
            "tampered": mutate_tampered,
        }.items():
            with self.subTest(mutation=mutation_label):
                settled = self._settle_pending_draft_mutation_review(
                    thread_id=(
                        "thread-settled-draft-reissue-completion-"
                        f"{mutation_label}"
                    ),
                    ambiguity="patch",
                )
                thread_ref = settled["thread_ref"]
                thread_doc = types.SimpleNamespace(reference=thread_ref)
                self.assertTrue(
                    processing._terminal_thread_blocks_client_completion(
                        thread_doc,
                        thread_ref.data,
                    )
                )
                pointer_before = copy.deepcopy(
                    thread_ref.data["activeGraphSendPermit"]
                )
                permits_before = [
                    ref
                    for ref in thread_ref.collection(
                        "graphSendPermits"
                    ).docs.values()
                    if ref.exists
                ]

                mutate(settled)

                self.assertTrue(
                    processing._terminal_thread_blocks_client_completion(
                        thread_doc,
                        thread_ref.data,
                    )
                )
                token = f"pending-reissue-{mutation_label}"
                loaded = _pending_data(thread_ref.id, token=token)
                pending_id = f"pending-reissue-{mutation_label}"
                pending_ref = _DocRef(
                    loaded,
                    doc_id=pending_id,
                    path=(
                        "users/uid-pending-mutation/pendingResponses/"
                        f"{pending_id}"
                    ),
                )

                with self.assertRaises(send_permits.GraphSendPermitBlocked):
                    send_permits.issue_pending_graph_send_permit(
                        self.firestore,
                        thread_ref,
                        pending_ref,
                        dict(loaded),
                        token,
                    )

                self.assertEqual(
                    pointer_before,
                    thread_ref.data["activeGraphSendPermit"],
                )
                self.assertEqual(
                    permits_before,
                    [
                        ref
                        for ref in thread_ref.collection(
                            "graphSendPermits"
                        ).docs.values()
                        if ref.exists
                    ],
                )
                self.assertNotIn("graphSendPermitId", pending_ref.data)
                self.assertNotIn("graphSendPermitHash", pending_ref.data)

    def test_draft_created_rejects_fields_from_later_attachment_state(self):
        token = "pending-worker-a"
        thread_ref = _DocRef({}, doc_id="thread-forged-nested-state")
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-forged-nested-state")
        capability = self._issue_pending(
            thread_ref, pending_ref, dict(loaded), token
        )
        send_permits.begin_graph_draft_creation(capability, loaded["msgId"])
        send_permits.complete_graph_draft_creation(
            capability,
            draft_id="draft-forged-nested-state",
            outcome="created",
            evidence={
                "httpStatus": 201,
                "phase": "create_reply",
                "draftId": "draft-forged-nested-state",
            },
        )
        capability.permit_ref.data["draftPreparation"].update({
            "attachmentOutcomes": [],
            "preparedAt": datetime.now(timezone.utc),
        })
        capability.permit_ref.version += 1

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitError,
            "draft|state|schema|later",
        ):
            send_permits.read_permit(capability)

    def test_rehashed_history_cannot_skip_from_issued_to_prepared(self):
        prepared = self._prepare_pending_draft(
            thread_id="thread-forged-history-sequence"
        )
        permit_ref = prepared["capability"].permit_ref
        permit = dict(permit_ref.data)
        genesis = dict(permit["stateHistory"][0])
        forged = send_permits._state_event(
            revision=1,
            prior_head_hash=genesis["headHash"],
            event="draft_prepared",
            state=send_permits._permit_state_projection(permit),
            occurred_at=datetime.now(timezone.utc),
        )
        permit_ref.data.update({
            "stateRevision": 1,
            "stateHeadHash": forged["headHash"],
            "stateHistory": [genesis, forged],
        })
        permit_ref.version += 1

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitError,
            "state|transition|history|sequence",
        ):
            send_permits.read_permit(prepared["capability"])

    def test_draft_creation_is_one_use_before_provider_post(self):
        token = "pending-worker-a"
        thread_ref = _DocRef({}, doc_id="thread-one-draft")
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-one-draft")
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )

        timeout = send_permits.begin_graph_draft_creation(
            capability,
            loaded["msgId"],
        )
        self.assertGreater(timeout, 0)
        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "create_request_started|draft creation|one-use",
        ):
            send_permits.begin_graph_draft_creation(
                capability,
                loaded["msgId"],
            )

    def test_stale_pending_owner_cannot_begin_draft_after_takeover(self):
        token = "pending-worker-a"
        thread_ref = _DocRef({}, doc_id="thread-stale-draft-owner")
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-stale-draft-owner")
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )
        pending_ref.data.update({
            "processingBy": "pending-worker-b",
            "processingLeaseUntil": datetime.now(timezone.utc) + timedelta(minutes=5),
        })
        pending_ref.version += 1

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "issuer changed|owner",
        ):
            send_permits.begin_graph_draft_creation(
                capability,
                loaded["msgId"],
            )

        self.assertIsNone(
            send_permits.read_permit(capability).get("draftPreparation")
        )

    def test_ambiguous_draft_creation_is_never_replayed(self):
        token = "pending-worker-a"
        thread_ref = _DocRef({}, doc_id="thread-ambiguous-create")
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-ambiguous-create")
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )
        send_permits.begin_graph_draft_creation(capability, loaded["msgId"])
        send_permits.complete_graph_draft_creation(
            capability,
            outcome="needs_reconciliation",
            evidence={"reason": "timeout", "phase": "create_reply"},
        )

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "needs_reconciliation|draft creation|one-use",
        ):
            send_permits.begin_graph_draft_creation(
                capability,
                loaded["msgId"],
            )
        self.assertEqual(
            "needs_reconciliation",
            send_permits.read_permit(capability)["status"],
        )

    def test_send_consumption_rejects_every_prepared_envelope_drift(self):
        drift_cases = {
            "source": {"source_graph_message_id": "other-source"},
            "draft": {"draft_id": "other-draft"},
            "html": {"html_body": "<p>Changed body.</p>"},
            "to": {"to_recipients": ["other@example.test"]},
            "cc": {"cc_recipients": ["other-cc@example.test"]},
        }
        for label, drift in drift_cases.items():
            with self.subTest(drift=label):
                prepared = self._prepare_pending_draft(
                    thread_id=f"thread-envelope-drift-{label}",
                )
                actual = {
                    "source_graph_message_id": prepared["source_id"],
                    "draft_id": prepared["draft_id"],
                    "subject": prepared["subject"],
                    "html_body": prepared["html_body"],
                    "to_recipients": prepared["to_recipients"],
                    "cc_recipients": prepared["cc_recipients"],
                    "attachments": prepared["attachments"],
                }
                actual.update(drift)

                with self.assertRaisesRegex(
                    send_permits.GraphSendPermitBlocked,
                    "prepared envelope|drift",
                ):
                    send_permits.consume_graph_send_capability(
                        prepared["capability"],
                        **actual,
                    )

                self.assertEqual(
                    "issued",
                    send_permits.read_permit(prepared["capability"])["status"],
                )

    def test_two_attachments_and_exact_completion_replays_are_idempotent(self):
        token = "pending-worker-a"
        thread_ref = _DocRef({}, doc_id="thread-two-attachments")
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-two-attachments")
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )
        source_id = loaded["msgId"]
        draft_id = "draft-two-attachments"
        attachments = [
            {
                "name": "logo.png",
                "contentType": "image/png",
                "contentBytes": "bG9nby1ieXRlcw==",
            },
            {
                "name": "terms.pdf",
                "contentType": "application/pdf",
                "contentBytes": "dGVybXMtYnl0ZXM=",
            },
        ]

        send_permits.begin_graph_draft_creation(
            capability,
            source_id,
            planned_attachment_count=len(attachments),
        )
        for _ in range(2):
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
            source_graph_message_id=source_id,
            draft_id=draft_id,
            subject=PROVIDER_DRAFT_SUBJECT,
            html_body="<p>Prepared body.</p>",
            to_recipients=["broker@example.test"],
            cc_recipients=[],
            attachments=attachments,
        )
        for _ in range(2):
            send_permits.complete_graph_draft_patch(
                capability,
                prepared_envelope_hash=prepared["preparedEnvelopeHash"],
                outcome="applied",
                evidence={
                    "httpStatus": 204,
                    "phase": "patch_draft",
                    "draftId": draft_id,
                    "preparedEnvelopeHash": prepared[
                        "preparedEnvelopeHash"
                    ],
                },
            )
        for index, attachment in enumerate(attachments):
            send_permits.begin_graph_draft_attachment(
                capability,
                prepared_envelope_hash=prepared["preparedEnvelopeHash"],
                attachment_index=index,
                attachment=attachment,
            )
            for _ in range(2):
                send_permits.complete_graph_draft_attachment(
                    capability,
                    prepared_envelope_hash=prepared["preparedEnvelopeHash"],
                    attachment_index=index,
                    outcome="applied",
                    evidence={
                        "httpStatus": 201,
                        "phase": "attach_draft",
                        "draftId": draft_id,
                        "attachmentIndex": index,
                        "attachmentHash": send_permits._attachment_projection(
                            attachment,
                            index,
                        )["attachmentHash"],
                        "providerAttachmentId": f"provider-{index}",
                    },
                )

        envelope = send_permits.finalize_graph_draft_preparation(
            capability,
            prepared_envelope_hash=prepared["preparedEnvelopeHash"],
        )

        self.assertEqual(2, len(envelope["attachments"]))
        self.assertNotEqual(
            envelope["attachments"][0]["attachmentHash"],
            envelope["attachments"][1]["attachmentHash"],
        )

    def test_successful_draft_operations_require_typed_exact_evidence(self):
        token = "pending-worker-typed-evidence"
        thread_ref = _DocRef({}, doc_id="thread-typed-draft-evidence")
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-typed-draft-evidence")
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )
        source_id = loaded["msgId"]
        draft_id = "draft-typed-evidence"
        attachment = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": "logo.png",
            "contentType": "image/png",
            "contentBytes": "bG9nby1ieXRlcw==",
            "contentId": "logo-typed-1",
            "isInline": True,
        }

        send_permits.begin_graph_draft_creation(
            capability,
            source_id,
            planned_attachment_count=1,
        )
        invalid_create = (
            {},
            {
                "phase": "patch_draft",
                "httpStatus": 201,
                "draftId": draft_id,
            },
            {
                "phase": "create_reply",
                "httpStatus": 202,
                "draftId": draft_id,
            },
            {
                "phase": "create_reply",
                "httpStatus": 201,
                "draftId": "other-draft",
            },
        )
        for evidence in invalid_create:
            with self.subTest(operation="create", evidence=evidence):
                with self.assertRaisesRegex(
                    send_permits.GraphSendPermitBlocked,
                    "evidence|create",
                ):
                    send_permits.complete_graph_draft_creation(
                        capability,
                        draft_id=draft_id,
                        outcome="created",
                        evidence=evidence,
                    )
        send_permits.complete_graph_draft_creation(
            capability,
            draft_id=draft_id,
            outcome="created",
            evidence={
                "phase": "create_reply",
                "httpStatus": 201,
                "draftId": draft_id,
            },
        )
        prepared = send_permits.begin_graph_draft_patch(
            capability,
            source_graph_message_id=source_id,
            draft_id=draft_id,
            subject=PROVIDER_DRAFT_SUBJECT,
            html_body="<p>Typed evidence.</p>",
            to_recipients=[loaded["recipient"]],
            cc_recipients=[],
            attachments=[attachment],
        )
        envelope_hash = prepared["preparedEnvelopeHash"]
        invalid_patch = (
            {},
            {
                "phase": "create_reply",
                "httpStatus": 204,
                "draftId": draft_id,
                "preparedEnvelopeHash": envelope_hash,
            },
            {
                "phase": "patch_draft",
                "httpStatus": 201,
                "draftId": draft_id,
                "preparedEnvelopeHash": envelope_hash,
            },
            {
                "phase": "patch_draft",
                "httpStatus": 204,
                "draftId": draft_id,
                "preparedEnvelopeHash": "wrong-hash",
            },
        )
        for evidence in invalid_patch:
            with self.subTest(operation="patch", evidence=evidence):
                with self.assertRaisesRegex(
                    send_permits.GraphSendPermitBlocked,
                    "evidence|patch",
                ):
                    send_permits.complete_graph_draft_patch(
                        capability,
                        prepared_envelope_hash=envelope_hash,
                        outcome="applied",
                        evidence=evidence,
                    )
        send_permits.complete_graph_draft_patch(
            capability,
            prepared_envelope_hash=envelope_hash,
            outcome="applied",
            evidence={
                "phase": "patch_draft",
                "httpStatus": 204,
                "draftId": draft_id,
                "preparedEnvelopeHash": envelope_hash,
            },
        )
        send_permits.begin_graph_draft_attachment(
            capability,
            prepared_envelope_hash=envelope_hash,
            attachment_index=0,
            attachment=attachment,
        )
        attachment_hash = send_permits._attachment_projection(
            attachment,
            0,
        )["attachmentHash"]
        valid_attachment_evidence = {
            "phase": "attach_draft",
            "httpStatus": 201,
            "draftId": draft_id,
            "attachmentIndex": 0,
            "attachmentHash": attachment_hash,
            "providerAttachmentId": "provider-attachment-typed-1",
        }
        invalid_attachment = (
            {},
            {**valid_attachment_evidence, "phase": "patch_draft"},
            {**valid_attachment_evidence, "httpStatus": 204},
            {**valid_attachment_evidence, "attachmentIndex": 1},
            {**valid_attachment_evidence, "attachmentHash": "wrong-hash"},
            {**valid_attachment_evidence, "providerAttachmentId": ""},
        )
        for evidence in invalid_attachment:
            with self.subTest(operation="attachment", evidence=evidence):
                with self.assertRaisesRegex(
                    send_permits.GraphSendPermitBlocked,
                    "evidence|attachment",
                ):
                    send_permits.complete_graph_draft_attachment(
                        capability,
                        prepared_envelope_hash=envelope_hash,
                        attachment_index=0,
                        outcome="applied",
                        evidence=evidence,
                    )
        send_permits.complete_graph_draft_attachment(
            capability,
            prepared_envelope_hash=envelope_hash,
            attachment_index=0,
            outcome="applied",
            evidence=valid_attachment_evidence,
        )

        envelope = send_permits.finalize_graph_draft_preparation(
            capability,
            prepared_envelope_hash=envelope_hash,
        )
        self.assertEqual(attachment_hash, envelope["attachments"][0]["attachmentHash"])

    def test_attachment_history_budget_is_reserved_before_draft_creation(self):
        token = "pending-worker-history-budget"
        thread_ref = _DocRef({}, doc_id="thread-history-budget")
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-history-budget")
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )
        attachment = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": "logo.png",
            "contentType": "image/png",
            "contentBytes": "bG9nby1ieXRlcw==",
            "contentId": "logo-budget-1",
            "isInline": True,
        }
        oversized = [
            {**attachment, "name": f"logo-{index}.png"}
            for index in range(send_permits.GRAPH_DRAFT_ATTACHMENT_LIMIT + 1)
        ]

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "attachment|history|bound",
        ):
            send_permits.validate_graph_draft_attachment_plan(oversized)
        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "attachment|history|bound",
        ):
            send_permits.begin_graph_draft_creation(
                capability,
                loaded["msgId"],
                planned_attachment_count=len(oversized),
            )
        self.assertIsNone(
            send_permits.read_permit(capability).get("draftPreparation")
        )

        send_permits.validate_graph_draft_attachment_plan([attachment])
        send_permits.begin_graph_draft_creation(
            capability,
            loaded["msgId"],
            planned_attachment_count=1,
        )
        send_permits.complete_graph_draft_creation(
            capability,
            draft_id="draft-history-budget",
            outcome="created",
            evidence={
                "phase": "create_reply",
                "httpStatus": 201,
                "draftId": "draft-history-budget",
            },
        )
        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "attachment plan|planned attachment",
        ):
            send_permits.begin_graph_draft_patch(
                capability,
                source_graph_message_id=loaded["msgId"],
                draft_id="draft-history-budget",
                subject=PROVIDER_DRAFT_SUBJECT,
                html_body="<p>History budget.</p>",
                to_recipients=[loaded["recipient"]],
                cc_recipients=[],
                attachments=[],
            )

    def test_prepared_recipient_projection_fails_closed_before_patch(self):
        cases = {
            "malformed_to": (
                [{"emailAddress": {}}],
                [],
            ),
            "duplicate_to": (
                [
                    "broker@example.test",
                    {"emailAddress": {"address": " BROKER@example.test "}},
                ],
                [],
            ),
            "malformed_cc": (
                ["broker@example.test"],
                [{"emailAddress": "asset-manager@example.test"}],
            ),
            "duplicate_cc": (
                ["broker@example.test"],
                [
                    "asset-manager@example.test",
                    {"emailAddress": {"address": "ASSET-MANAGER@example.test"}},
                ],
            ),
            "to_cc_overlap": (
                ["broker@example.test"],
                [{"emailAddress": {"address": "BROKER@example.test"}}],
            ),
        }

        for label, (to_recipients, cc_recipients) in cases.items():
            with self.subTest(label=label):
                token = f"pending-worker-recipient-{label}"
                thread_ref = _DocRef({}, doc_id=f"thread-recipient-{label}")
                loaded = _pending_data(thread_ref.id, token=token)
                pending_ref = _DocRef(
                    loaded,
                    doc_id=f"pending-recipient-{label}",
                )
                capability = self._issue_pending(
                    thread_ref,
                    pending_ref,
                    dict(loaded),
                    token,
                )
                draft_id = f"draft-recipient-{label}"
                send_permits.begin_graph_draft_creation(
                    capability,
                    loaded["msgId"],
                )
                send_permits.complete_graph_draft_creation(
                    capability,
                    draft_id=draft_id,
                    outcome="created",
                    evidence={
                        "phase": "create_reply",
                        "httpStatus": 201,
                        "draftId": draft_id,
                    },
                )
                before = send_permits.read_permit(capability)

                with self.assertRaisesRegex(
                    send_permits.GraphSendPermitBlocked,
                    "recipient|To|Cc|overlap|duplicate|malformed",
                ):
                    send_permits.begin_graph_draft_patch(
                        capability,
                        source_graph_message_id=loaded["msgId"],
                        draft_id=draft_id,
                        subject=PROVIDER_DRAFT_SUBJECT,
                        html_body="<p>Recipient projection.</p>",
                        to_recipients=to_recipients,
                        cc_recipients=cc_recipients,
                        attachments=[],
                    )

                after = send_permits.read_permit(capability)
                self.assertEqual("draft_created", after["draftPreparation"]["state"])
                self.assertNotIn("preparedEnvelope", after)
                self.assertEqual(before, after)

    def test_pending_definitely_not_started_event_matches_emitted_outcome(self):
        prior = {
            "status": "issued",
            "draftState": None,
            "preparedEnvelopeHash": None,
            "requestStartedAt": None,
            "resolutionEvidenceHash": None,
            "issuerSettledAt": None,
            "draftReviewRequired": None,
            "draftReviewEvidencePath": None,
            "draftReviewEvidenceHash": None,
            "pendingReconciliationEvidenceHash": None,
            "terminalSendReviewEvidenceHash": None,
            "pendingSendReviewRequired": None,
            "terminalSendReviewRequired": None,
        }
        current = {
            **prior,
            "status": "settled_definitely_not_sent",
            "issuerSettledAt": datetime.now(timezone.utc).isoformat(),
            "pendingReconciliationEvidenceHash": "a" * 64,
        }

        self.assertTrue(
            send_permits._valid_state_event_transition(
                prior,
                current,
                "pending_reconcile_definitely_not_started",
            )
        )

    def test_expired_issued_pending_permit_requeues_as_definitely_not_started(self):
        token = "pending-worker-definitely-not-started"
        thread_id = "thread-definitely-not-started"
        pending_id = "pending-definitely-not-started"
        thread_ref = _DocRef(
            {},
            doc_id=thread_id,
            path=f"users/u/threads/{thread_id}",
        )
        loaded = _pending_data(thread_id, token=token)
        pending_ref = _DocRef(
            loaded,
            doc_id=pending_id,
            path=f"users/u/pendingResponses/{pending_id}",
        )
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )
        pending_ref.data["processingLeaseUntil"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        pending_ref.version += 1
        permit = send_permits.read_permit(capability)
        identity = hashlib.sha256(
            (
                f"{pending_id}:graph_permit_{permit['permitId']}:"
                f"{permit['envelopeHash']}"
            ).encode("utf-8")
        ).hexdigest()
        evidence_ref = _DocRef(
            {},
            exists=False,
            doc_id=f"pending-exit-{identity}",
            path=f"users/u/deadLetterQueue/pending-exit-{identity}",
        )

        real_datetime = datetime

        class FutureDateTimeMeta(type):
            def __instancecheck__(cls, value):
                return isinstance(value, real_datetime)

        class FutureDateTime(real_datetime, metaclass=FutureDateTimeMeta):
            @classmethod
            def now(cls, tz=None):
                future = real_datetime.now(timezone.utc) + timedelta(
                    minutes=10
                )
                if tz is None:
                    return future.replace(tzinfo=None)
                return future.astimezone(tz)

        with patch.object(send_permits, "datetime", FutureDateTime):
            reconciled = send_permits.reconcile_pending_graph_send_permit(
                self.firestore,
                thread_ref,
                pending_ref,
                loaded,
                outcome="definitely_not_started",
                evidence_document=(
                    evidence_ref,
                    {
                        "status": "retryable",
                        "alreadySent": False,
                        "providerSendStarted": False,
                    },
                ),
            )

        self.assertTrue(reconciled)
        self.assertTrue(pending_ref.exists)
        self.assertEqual("queued", pending_ref.data["status"])
        self.assertIsNone(pending_ref.data["processingBy"])
        self.assertIsNone(pending_ref.data["graphSendPermitId"])
        self.assertTrue(evidence_ref.exists)
        self.assertEqual(
            "settled_definitely_not_sent",
            send_permits.read_permit(capability)["status"],
        )

    def test_processing_freezes_attachment_plan_before_create_reply_all(self):
        source = Path(processing.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        send_function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "send_reply_in_thread"
        )
        calls = [
            node
            for node in ast.walk(send_function)
            if isinstance(node, ast.Call)
        ]

        def call_name(call):
            return (
                call.func.id
                if isinstance(call.func, ast.Name)
                else getattr(call.func, "attr", None)
            )

        signature_line = min(
            call.lineno
            for call in calls
            if call_name(call) == "get_signature_attachments"
        )
        validation_line = min(
            call.lineno
            for call in calls
            if call_name(call) == "validate_graph_draft_attachment_plan"
        )
        create_call = min(
            (
                call
                for call in calls
                if call_name(call) == "begin_graph_draft_creation"
            ),
            key=lambda call: call.lineno,
        )
        self.assertLess(signature_line, validation_line)
        self.assertLess(validation_line, create_call.lineno)
        self.assertIn(
            "planned_attachment_count",
            {keyword.arg for keyword in create_call.keywords},
        )

    def test_same_completion_state_rejects_different_evidence(self):
        token = "pending-worker-a"
        thread_ref = _DocRef({}, doc_id="thread-evidence-idempotency")
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-evidence-idempotency")
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )
        send_permits.begin_graph_draft_creation(capability, loaded["msgId"])
        send_permits.complete_graph_draft_creation(
            capability,
            draft_id="draft-evidence",
            outcome="created",
            evidence={
                "httpStatus": 201,
                "phase": "create_reply",
                "draftId": "draft-evidence",
            },
        )

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "different evidence",
        ):
            send_permits.complete_graph_draft_creation(
                capability,
                draft_id="draft-evidence",
                outcome="created",
                evidence={
                    "httpStatus": 200,
                    "phase": "create_reply",
                    "draftId": "draft-evidence",
                },
            )

    def test_same_send_resolution_requires_identical_evidence_hash(self):
        prepared = self._prepare_pending_draft(
            thread_id="thread-send-evidence-idempotency",
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
        for _ in range(2):
            send_permits.resolve_graph_send_permit(
                prepared["capability"],
                "accepted",
                evidence={
                    "httpStatus": 202,
                    "requestId": "request-one",
                    "phase": "send",
                },
            )

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "different evidence",
        ):
            send_permits.resolve_graph_send_permit(
                prepared["capability"],
                "accepted",
                evidence={
                    "httpStatus": 202,
                    "requestId": "request-two",
                    "phase": "send",
                },
            )

    def test_older_identical_sent_item_cannot_settle_new_terminal_generation(self):
        saga, thread_ref, claim_ref = _terminal_refs("thread-old-identical-sent")
        capability = send_permits.issue_terminal_graph_send_permit(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
        )
        send_arguments = _prepare_capability_for_send(
            capability,
            saga["sourceGraphMessageId"],
            saga["replyRecipient"],
        )
        send_permits.consume_graph_send_capability(
            capability,
            **send_arguments,
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "accepted",
            evidence={"httpStatus": 202, "phase": "send"},
        )
        retained_permit = send_permits.read_permit(capability)
        claim_ref.data["terminalSagaClaim"].update({
            "owner": "terminal-owner-b",
            "fencingToken": 2,
            "leaseUntil": datetime.now(timezone.utc) + timedelta(minutes=5),
        })
        claim_ref.version += 1
        attempt = dict(thread_ref.data["terminalReplyAttempt"])

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "Sent evidence|evidence",
        ):
            send_permits.cas_terminal_reply_transition(
                self.firestore,
                thread_ref,
                claim_ref,
                saga,
                "terminal-owner-b",
                2,
                expected_attempt_status="sending",
                thread_patch={
                    "terminalReplyOwed": False,
                    "terminalReplyOutcome": "sent_reconciled",
                    "terminalReplyAttempt": {
                        **attempt,
                        "status": "reconciled",
                        "outcome": "sent_reconciled",
                    },
                },
                permit_settlement="settled_sent",
                sent_evidence={
                    **_actual_sent_envelope(retained_permit),
                    "sentMessageId": "older-identical-send",
                    "recipient": saga["replyRecipient"],
                    "bodyHash": attempt["responseBodyHash"],
                    "conversationId": saga["sourceConversationId"],
                    "sentDateTime": (
                        retained_permit["requestStartedAt"]
                        - timedelta(seconds=1)
                    ),
                    "permitId": capability.permit_id,
                    "sourceGraphMessageId": saga["sourceGraphMessageId"],
                    "preparedEnvelopeHash": retained_permit[
                        "sendPreparedEnvelopeHash"
                    ],
                },
            )

        self.assertTrue(thread_ref.data["terminalReplyOwed"])
        self.assertEqual("accepted", send_permits.read_permit(capability)["status"])

    def test_pending_takeover_settles_exact_sent_without_plaintext_capability(self):
        prepared = self._prepare_pending_draft(
            thread_id="thread-pending-sent-takeover",
        )
        capability = prepared["capability"]
        send_permits.consume_graph_send_capability(
            capability,
            source_graph_message_id=prepared["source_id"],
            draft_id=prepared["draft_id"],
            subject=prepared["subject"],
            html_body=prepared["html_body"],
            to_recipients=prepared["to_recipients"],
            cc_recipients=prepared["cc_recipients"],
            attachments=prepared["attachments"],
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "accepted",
            evidence={"httpStatus": 202, "phase": "send"},
        )
        retained = send_permits.read_permit(capability)
        prepared["pending_ref"].data["processingLeaseUntil"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        prepared["pending_ref"].version += 1
        evidence_ref = _DocRef({}, exists=False, doc_id="pending-sent-evidence")
        sent_evidence = {
            **_actual_sent_envelope(
                retained,
                html_body=prepared["html_body"],
            ),
            "sentMessageId": prepared["draft_id"],
            "recipient": retained["recipient"],
            "bodyHash": retained["bodyHash"],
            "conversationId": retained["conversationId"],
            "sentDateTime": retained["requestStartedAt"] + timedelta(seconds=1),
            "permitId": retained["permitId"],
            "sourceGraphMessageId": retained["sourceGraphMessageId"],
            "preparedEnvelopeHash": retained["sendPreparedEnvelopeHash"],
        }
        completion_document = _pending_completion_document(
            prepared["thread_ref"],
            prepared["pending_ref"],
            prepared["loaded"],
            capability,
            sent_evidence,
        )

        for _ in range(2):
            send_permits.reconcile_pending_graph_send_permit(
                self.firestore,
                prepared["thread_ref"],
                prepared["pending_ref"],
                prepared["loaded"],
                outcome="sent",
                sent_evidence=sent_evidence,
                evidence_document=(
                    evidence_ref,
                    {"alreadySent": True, "permitId": capability.permit_id},
                ),
                completion_document=completion_document,
            )

        self.assertFalse(prepared["pending_ref"].exists)
        self.assertTrue(evidence_ref.exists)
        self.assertTrue(evidence_ref.data["alreadySent"])
        self.assertTrue(completion_document[0].exists)
        self.assertEqual("owed", completion_document[0].data["status"])
        self.assertEqual("settled_sent", send_permits.read_permit(capability)["status"])

    def test_pending_draft_ambiguity_records_not_sent_evidence_without_replay(self):
        token = "pending-worker-a"
        thread_ref = _DocRef({}, doc_id="thread-pending-draft-ambiguity")
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-draft-ambiguity")
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )
        send_permits.begin_graph_draft_creation(capability, loaded["msgId"])
        send_permits.complete_graph_draft_creation(
            capability,
            outcome="needs_reconciliation",
            evidence={"reason": "create timeout", "phase": "create_reply"},
        )
        pending_ref.data["processingLeaseUntil"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        pending_ref.version += 1
        evidence_ref = _DocRef({}, exists=False, doc_id="draft-review-evidence")
        retained = send_permits.read_permit(capability)

        send_permits.reconcile_pending_graph_send_permit(
            self.firestore,
            thread_ref,
            pending_ref,
            loaded,
            outcome="draft_needs_review",
            evidence_document=(
                evidence_ref,
                {
                    "status": "manual_review",
                    "source": "pendingGraphSendProtocol",
                    "authoritative": True,
                    "alreadySent": False,
                    "providerSendStarted": False,
                    "sendOutcomeUnknown": False,
                    "retryAllowed": False,
                    "automaticDeleteAttempted": False,
                    "failureReason": "create timeout",
                    "draftId": None,
                    "draftMutationState": (
                        retained["draftPreparation"]["state"]
                    ),
                    "draftResolutionEvidenceHash": retained[
                        "resolutionEvidenceHash"
                    ],
                },
            ),
        )

        self.assertFalse(pending_ref.exists)
        self.assertFalse(evidence_ref.data["alreadySent"])
        self.assertFalse(evidence_ref.data["providerSendStarted"])
        self.assertEqual(
            "settled_draft_needs_review",
            send_permits.read_permit(capability)["status"],
        )

    def test_pending_send_ambiguity_retains_exact_work_for_capped_rechecks(self):
        prepared = self._prepare_pending_draft(
            thread_id="thread-pending-send-review",
        )
        capability = prepared["capability"]
        send_permits.consume_graph_send_capability(
            capability,
            source_graph_message_id=prepared["source_id"],
            draft_id=prepared["draft_id"],
            subject=prepared["subject"],
            html_body=prepared["html_body"],
            to_recipients=prepared["to_recipients"],
            cc_recipients=prepared["cc_recipients"],
            attachments=prepared["attachments"],
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "needs_reconciliation",
            evidence={"reason": "provider timeout", "phase": "send"},
        )
        prepared["pending_ref"].data["processingLeaseUntil"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        prepared["pending_ref"].version += 1
        evidence_ref = _DocRef(
            {},
            exists=False,
            doc_id="pending-send-review-evidence",
            path="users/u/threads/t/graphSendReviews/pending-send-review-evidence",
        )

        send_permits.reconcile_pending_graph_send_permit(
            self.firestore,
            prepared["thread_ref"],
            prepared["pending_ref"],
            prepared["loaded"],
            outcome="send_needs_review",
            evidence_document=(
                evidence_ref,
                {
                    "status": "needs_reconciliation",
                    "alreadySent": None,
                    "providerSendStarted": True,
                    "sendOutcomeUnknown": True,
                },
            ),
        )

        retained_pending = prepared["pending_ref"].data
        retained_permit = send_permits.read_permit(capability)
        self.assertTrue(prepared["pending_ref"].exists)
        self.assertEqual("needs_reconciliation", retained_pending["status"])
        self.assertEqual(capability.permit_id, retained_pending["graphSendPermitId"])
        self.assertEqual(1, retained_pending["graphSendSentRecheckCount"])
        self.assertIs(
            evidence_ref,
            retained_pending["graphSendReviewEvidenceRef"],
        )
        self.assertEqual("needs_reconciliation", retained_permit["status"])
        self.assertNotIn("issuerSettledAt", retained_permit)

    def test_pending_send_review_can_later_settle_only_exact_sent_evidence(self):
        prepared = self._prepare_pending_draft(
            thread_id="thread-pending-review-then-sent",
        )
        capability = prepared["capability"]
        send_permits.consume_graph_send_capability(
            capability,
            source_graph_message_id=prepared["source_id"],
            draft_id=prepared["draft_id"],
            subject=prepared["subject"],
            html_body=prepared["html_body"],
            to_recipients=prepared["to_recipients"],
            cc_recipients=prepared["cc_recipients"],
            attachments=prepared["attachments"],
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "needs_reconciliation",
            evidence={"reason": "provider timeout", "phase": "send"},
        )
        retained = send_permits.read_permit(capability)
        prepared["pending_ref"].data["processingLeaseUntil"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        prepared["pending_ref"].version += 1
        evidence_ref = _DocRef(
            {},
            exists=False,
            doc_id="pending-review-then-sent-evidence",
            path="users/u/threads/t/graphSendReviews/pending-review-then-sent-evidence",
        )
        send_permits.reconcile_pending_graph_send_permit(
            self.firestore,
            prepared["thread_ref"],
            prepared["pending_ref"],
            prepared["loaded"],
            outcome="send_needs_review",
            evidence_document=(
                evidence_ref,
                {
                    "status": "needs_reconciliation",
                    "alreadySent": None,
                    "providerSendStarted": True,
                    "sendOutcomeUnknown": True,
                },
            ),
        )
        sent_evidence = {
            **_actual_sent_envelope(
                retained,
                html_body=prepared["html_body"],
            ),
            "sentMessageId": prepared["draft_id"],
            "recipient": retained["recipient"],
            "bodyHash": retained["bodyHash"],
            "conversationId": retained["conversationId"],
            "sentDateTime": retained["requestStartedAt"] + timedelta(seconds=1),
            "permitId": retained["permitId"],
            "sourceGraphMessageId": retained["sourceGraphMessageId"],
            "preparedEnvelopeHash": retained["sendPreparedEnvelopeHash"],
        }

        send_permits.reconcile_pending_graph_send_permit(
            self.firestore,
            prepared["thread_ref"],
            prepared["pending_ref"],
            prepared["loaded"],
            outcome="sent",
            sent_evidence=sent_evidence,
            evidence_document=(
                evidence_ref,
                {
                    "status": "reconciled_sent",
                    "alreadySent": True,
                    "providerSendStarted": True,
                    "sentMessageId": prepared["draft_id"],
                },
            ),
            completion_document=_pending_completion_document(
                prepared["thread_ref"],
                prepared["pending_ref"],
                prepared["loaded"],
                capability,
                sent_evidence,
            ),
        )

        self.assertFalse(prepared["pending_ref"].exists)
        self.assertEqual("settled_sent", send_permits.read_permit(capability)["status"])

    def test_operator_exact_sent_settlement_atomically_preserves_provenance(self):
        prepared = self._prepare_pending_draft(
            thread_id="thread-pending-operator-exact-sent",
        )
        capability = prepared["capability"]
        send_permits.consume_graph_send_capability(
            capability,
            source_graph_message_id=prepared["source_id"],
            draft_id=prepared["draft_id"],
            subject=prepared["subject"],
            html_body=prepared["html_body"],
            to_recipients=prepared["to_recipients"],
            cc_recipients=prepared["cc_recipients"],
            attachments=prepared["attachments"],
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "needs_reconciliation",
            evidence={"reason": "provider timeout", "phase": "send"},
        )
        prepared["pending_ref"].data["processingLeaseUntil"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        prepared["pending_ref"].version += 1
        evidence_ref = _DocRef(
            {},
            exists=False,
            doc_id="pending-operator-exact-sent-evidence",
            path=(
                "users/u/threads/t/graphSendReviews/"
                "pending-operator-exact-sent-evidence"
            ),
        )
        send_permits.reconcile_pending_graph_send_permit(
            self.firestore,
            prepared["thread_ref"],
            prepared["pending_ref"],
            prepared["loaded"],
            outcome="send_needs_review",
            evidence_document=(
                evidence_ref,
                {
                    "status": "needs_reconciliation",
                    "alreadySent": None,
                    "providerSendStarted": True,
                    "sendOutcomeUnknown": True,
                },
            ),
        )
        permit = send_permits.read_permit(capability)
        original_evidence_hash = permit[
            "pendingReconciliationEvidenceHash"
        ]
        sent_evidence = {
            **_actual_sent_envelope(
                permit,
                html_body=prepared["html_body"],
            ),
            "sentMessageId": prepared["draft_id"],
            "recipient": permit["recipient"],
            "bodyHash": permit["bodyHash"],
            "conversationId": permit["conversationId"],
            "sentDateTime": permit["requestStartedAt"] + timedelta(seconds=1),
            "permitId": permit["permitId"],
            "sourceGraphMessageId": permit["sourceGraphMessageId"],
            "preparedEnvelopeHash": permit["sendPreparedEnvelopeHash"],
        }
        lookup_completed_at = datetime.now(timezone.utc)
        audit_ref = _DocRef(
            {},
            exists=False,
            doc_id="operator-exact-sent-settlement-1",
            path=(
                "users/u/graphSendOperatorSettlements/"
                "operator-exact-sent-settlement-1"
            ),
        )
        audit_payload = {
            "version": 1,
            "settlementId": "operator-exact-sent-settlement-1",
            "action": "acknowledge_ambiguous_no_retry",
            "requestedAction": "acknowledge_ambiguous_no_retry",
            "operatorId": "authenticated-operator-uid",
            "operatorReason": "Fresh Sent evidence won precedence.",
            "reconciliationEvidenceHash": original_evidence_hash,
            "resolution": "exact_sent",
            "alreadySent": True,
            "retryAllowed": False,
            "freshSentLookupCompletedAt": lookup_completed_at,
            "resolvedAt": lookup_completed_at,
            "sentMessageId": prepared["draft_id"],
        }
        resolved_review = {
            **evidence_ref.data,
            "status": "reconciled_sent",
            "alreadySent": True,
            "sendOutcomeUnknown": False,
            "retryAllowed": False,
            "sentMessageId": prepared["draft_id"],
            "freshSentLookupCompletedAt": lookup_completed_at,
            "resolvedBy": "authenticated-operator-uid",
        }
        completion_document = _pending_completion_document(
            prepared["thread_ref"],
            prepared["pending_ref"],
            prepared["loaded"],
            capability,
            sent_evidence,
        )

        for _ in range(2):
            send_permits.reconcile_pending_graph_send_permit(
                self.firestore,
                prepared["thread_ref"],
                prepared["pending_ref"],
                prepared["loaded"],
                outcome="sent",
                sent_evidence=sent_evidence,
                evidence_document=(evidence_ref, resolved_review),
                operator_audit_document=(audit_ref, audit_payload),
                completion_document=completion_document,
            )

        self.assertFalse(prepared["pending_ref"].exists)
        settled = send_permits.read_permit(capability)
        self.assertEqual("settled_sent", settled["status"])
        self.assertIs(audit_ref, settled["operatorSettlementAuditRef"])
        self.assertTrue(audit_ref.exists)
        self.assertTrue(completion_document[0].exists)
        self.assertEqual("owed", completion_document[0].data["status"])
        self.assertEqual(
            "operator-exact-sent-settlement-1",
            audit_ref.data["settlementId"],
        )
        self.assertEqual(
            "acknowledge_ambiguous_no_retry",
            audit_ref.data["action"],
        )
        self.assertEqual("exact_sent", audit_ref.data["resolution"])
        self.assertEqual(
            original_evidence_hash,
            audit_ref.data["reconciliationEvidenceHash"],
        )
        self.assertIs(
            audit_ref,
            evidence_ref.data["operatorSettlementAuditRef"],
        )
        self.assertEqual(
            "operator-exact-sent-settlement-1",
            evidence_ref.data["operatorSettlementId"],
        )

    def test_authenticated_operator_acknowledges_ambiguous_send_without_retry_claim(self):
        prepared = self._prepare_pending_draft(
            thread_id="thread-pending-operator-ack",
        )
        capability = prepared["capability"]
        send_permits.consume_graph_send_capability(
            capability,
            source_graph_message_id=prepared["source_id"],
            draft_id=prepared["draft_id"],
            subject=prepared["subject"],
            html_body=prepared["html_body"],
            to_recipients=prepared["to_recipients"],
            cc_recipients=prepared["cc_recipients"],
            attachments=prepared["attachments"],
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "needs_reconciliation",
            evidence={"reason": "provider timeout", "phase": "send"},
        )
        prepared["pending_ref"].data["processingLeaseUntil"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        prepared["pending_ref"].version += 1
        evidence_ref = _DocRef(
            {},
            exists=False,
            doc_id="pending-operator-evidence",
            path="users/u/threads/t/graphSendReviews/pending-operator-evidence",
        )
        send_permits.reconcile_pending_graph_send_permit(
            self.firestore,
            prepared["thread_ref"],
            prepared["pending_ref"],
            prepared["loaded"],
            outcome="send_needs_review",
            evidence_document=(
                evidence_ref,
                {
                    "status": "needs_reconciliation",
                    "alreadySent": None,
                    "providerSendStarted": True,
                    "sendOutcomeUnknown": True,
                },
            ),
        )
        permit = send_permits.read_permit(capability)
        audit_ref = _DocRef(
            {},
            exists=False,
            doc_id="operator-settlement-1",
            path="users/u/graphSendOperatorSettlements/operator-settlement-1",
        )
        lookup_completed_at = datetime.now(timezone.utc)

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "evidence|exact",
        ):
            send_permits.operator_settle_pending_graph_send_review(
                self.firestore,
                prepared["thread_ref"],
                prepared["pending_ref"],
                prepared["loaded"],
                expected_permit_id=permit["permitId"],
                expected_permit_hash=permit["immutableHash"],
                expected_reconciliation_evidence_hash="wrong-hash",
                reconciliation_evidence_ref=evidence_ref,
                action="acknowledge_ambiguous_no_retry",
                operator_id="authenticated-operator-uid",
                operator_reason="Mailbox evidence cannot determine acceptance.",
                settlement_id="operator-settlement-1",
                audit_ref=audit_ref,
                sent_lookup_completed_at=lookup_completed_at,
            )

        for _ in range(2):
            send_permits.operator_settle_pending_graph_send_review(
                self.firestore,
                prepared["thread_ref"],
                prepared["pending_ref"],
                prepared["loaded"],
                expected_permit_id=permit["permitId"],
                expected_permit_hash=permit["immutableHash"],
                expected_reconciliation_evidence_hash=permit[
                    "pendingReconciliationEvidenceHash"
                ],
                reconciliation_evidence_ref=evidence_ref,
                action="acknowledge_ambiguous_no_retry",
                operator_id="authenticated-operator-uid",
                operator_reason="Mailbox evidence cannot determine acceptance.",
                settlement_id="operator-settlement-1",
                audit_ref=audit_ref,
                sent_lookup_completed_at=lookup_completed_at,
            )

        self.assertFalse(prepared["pending_ref"].exists)
        settled = send_permits.read_permit(capability)
        self.assertEqual("settled_ambiguous_no_retry", settled["status"])
        self.assertTrue(audit_ref.exists)
        self.assertEqual("authenticated-operator-uid", audit_ref.data["operatorId"])
        self.assertFalse(audit_ref.data["retryAllowed"])
        self.assertIsNone(audit_ref.data["alreadySent"])
        self.assertEqual("settled_ambiguous_no_retry", evidence_ref.data["status"])
        self.assertFalse(evidence_ref.data["retryAllowed"])
        self.assertIsNone(evidence_ref.data["alreadySent"])
        user_ref = _DocRef({}, doc_id="u")
        user_ref.collection("threads").docs[
            prepared["thread_ref"].id
        ] = prepared["thread_ref"]
        user_ref.collection("graphSendOperatorSettlements").docs[
            audit_ref.id
        ] = audit_ref
        replayed = (
            send_permits.read_pending_graph_send_operator_settlement_replay(
                self.firestore,
                user_ref,
                audit_ref,
                pending_document_id=prepared["pending_ref"].id,
                expected_permit_id=permit["permitId"],
                expected_permit_hash=permit["immutableHash"],
                expected_reconciliation_evidence_hash=permit[
                    "pendingReconciliationEvidenceHash"
                ],
                operator_id="authenticated-operator-uid",
                operator_reason=(
                    "Mailbox evidence cannot determine acceptance."
                ),
                settlement_id="operator-settlement-1",
            )
        )
        self.assertEqual("settled_ambiguous_no_retry", replayed)
        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "exact request",
        ):
            send_permits.read_pending_graph_send_operator_settlement_replay(
                self.firestore,
                user_ref,
                audit_ref,
                pending_document_id=prepared["pending_ref"].id,
                expected_permit_id=permit["permitId"],
                expected_permit_hash=permit["immutableHash"],
                expected_reconciliation_evidence_hash=permit[
                    "pendingReconciliationEvidenceHash"
                ],
                operator_id="authenticated-operator-uid",
                operator_reason="Different replay request.",
                settlement_id="operator-settlement-1",
            )

    def test_terminal_stage_before_pending_permit_yields_zero_graph_send(self):
        token = "pending-worker-a"
        thread_ref = _DocRef({
            "terminalSagaKey": "terminal-generation-b",
        }, doc_id="thread-ordering-one")
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-a")

        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )

        self.assertIsNone(capability)
        self.assertIsNone(thread_ref.data.get("activeGraphSendPermit"))
        self.assertEqual({}, thread_ref.collection("graphSendPermits").docs)
        self.assertEqual("queued", pending_ref.data["status"])
        self.assertIsNone(pending_ref.data["processingBy"])

    def test_pending_permit_before_terminal_stage_blocks_all_terminal_effects_until_exact_outcome(self):
        token = "pending-worker-a"
        thread_ref = _DocRef({}, doc_id="thread-ordering-two")
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-a")
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )

        transaction = self.firestore.transaction()
        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "unresolved",
        ):
            send_permits.assert_terminal_staging_allowed(
                transaction,
                thread_ref,
            )
        self.assertEqual("issued", send_permits.read_permit(capability)["status"])

    def test_request_started_capability_is_one_use(self):
        token = "pending-worker-a"
        thread_ref = _DocRef({}, doc_id="thread-one-use")
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-a")
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )

        send_arguments = _prepare_capability_for_send(
            capability,
            loaded["msgId"],
            loaded["recipient"],
        )
        timeout = send_permits.consume_graph_send_capability(
            capability,
            **send_arguments,
        )
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, send_permits.GRAPH_SEND_HTTP_MAX_SECONDS)
        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "request_started|one-use",
        ):
            send_permits.consume_graph_send_capability(
                capability,
                **send_arguments,
            )

    def test_original_request_started_worker_can_settle_after_claim_lease_expires(self):
        token = "pending-worker-a"
        thread_ref = _DocRef({}, doc_id="thread-expired-after-post")
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-a")
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )

        send_permits.consume_graph_send_capability(
            capability,
            **_prepare_capability_for_send(
                capability,
                loaded["msgId"],
                loaded["recipient"],
            ),
        )
        pending_ref.data["processingLeaseUntil"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        pending_ref.version += 1
        send_permits.resolve_graph_send_permit(
            capability,
            "accepted",
            evidence={"httpStatus": 202, "phase": "send"},
        )

        settled = send_permits.cas_pending_claim_transition(
            self.firestore,
            thread_ref,
            pending_ref,
            loaded,
            token,
            delete_pending=True,
            capability=capability,
            permit_settlement="settled_sent",
            sent_evidence=_exact_sent_evidence(capability),
            side_documents=(_pending_completion_document(
                thread_ref,
                pending_ref,
                loaded,
                capability,
                _exact_sent_evidence(capability),
            ),),
        )

        self.assertTrue(settled)
        self.assertFalse(pending_ref.exists)
        self.assertEqual(
            "settled_sent",
            send_permits.read_permit(capability)["status"],
        )

    def test_expired_claim_takeover_is_blocked_by_request_started_permit(self):
        token = "pending-worker-a"
        thread_ref = _DocRef({}, doc_id="thread-takeover-barrier")
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-a")
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )
        send_permits.consume_graph_send_capability(
            capability,
            **_prepare_capability_for_send(
                capability,
                loaded["msgId"],
                loaded["recipient"],
            ),
        )
        pending_ref.data["processingLeaseUntil"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        pending_ref.version += 1

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "unresolved|reconciliation",
        ):
            send_permits.assert_pending_claim_allowed(
                self.firestore.transaction(),
                thread_ref,
            )

        self.assertEqual(token, pending_ref.data["processingBy"])
        self.assertEqual(
            "request_started",
            send_permits.read_permit(capability)["status"],
        )

    def test_terminal_accepted_permit_and_reply_outcome_settle_atomically(self):
        saga, thread_ref, claim_ref = _terminal_refs("thread-terminal-direct")
        capability = send_permits.issue_terminal_graph_send_permit(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
        )
        send_permits.consume_graph_send_capability(
            capability,
            **_prepare_capability_for_send(
                capability,
                saga["sourceGraphMessageId"],
                saga["replyRecipient"],
            ),
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "accepted",
            evidence={"httpStatus": 202, "phase": "send"},
        )
        attempt = dict(thread_ref.data["terminalReplyAttempt"])

        send_permits.cas_terminal_reply_transition(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
            expected_attempt_status="sending",
            thread_patch={
                "terminalReplyOwed": False,
                "terminalReplyOutcome": "sent_indexed",
                "terminalReplyAttempt": {
                    **attempt,
                    "status": "committed",
                    "outcome": "sent_indexed",
                },
            },
            permit_settlement="settled_sent",
            capability=capability,
            sent_evidence=_exact_sent_evidence(capability),
        )

        self.assertFalse(thread_ref.data["terminalReplyOwed"])
        self.assertEqual("sent_indexed", thread_ref.data["terminalReplyOutcome"])
        self.assertEqual(
            "settled_sent",
            send_permits.read_permit(capability)["status"],
        )

    def test_terminal_cas_accepts_separate_refs_for_same_canonical_path(self):
        saga, thread_ref, claim_ref = _terminal_refs("thread-ref-path")
        thread_ref.path = "users/user-1/threads/thread-ref-path"
        capability = send_permits.issue_terminal_graph_send_permit(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
        )
        send_permits.consume_graph_send_capability(
            capability,
            **_prepare_capability_for_send(
                capability,
                saga["sourceGraphMessageId"],
                saga["replyRecipient"],
            ),
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "accepted",
            evidence={"httpStatus": 202, "phase": "send"},
        )
        alias = _AliasDocRef(thread_ref, thread_ref.path)
        attempt = dict(thread_ref.data["terminalReplyAttempt"])

        send_permits.cas_terminal_reply_transition(
            self.firestore,
            alias,
            alias,
            saga,
            "terminal-owner-a",
            1,
            expected_attempt_status="sending",
            thread_patch={
                "terminalReplyOwed": False,
                "terminalReplyOutcome": "sent_indexed",
                "terminalReplyAttempt": {
                    **attempt,
                    "status": "committed",
                    "outcome": "sent_indexed",
                },
            },
            permit_settlement="settled_sent",
            capability=capability,
            sent_evidence=_exact_sent_evidence(capability),
        )

        self.assertEqual("settled_sent", send_permits.read_permit(capability)["status"])

    def test_terminal_cas_rejects_same_id_under_different_parent_path(self):
        saga, thread_ref, claim_ref = _terminal_refs("thread-ref-collision")
        thread_ref.path = "users/user-1/threads/thread-ref-collision"
        capability = send_permits.issue_terminal_graph_send_permit(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
        )
        send_permits.consume_graph_send_capability(
            capability,
            **_prepare_capability_for_send(
                capability,
                saga["sourceGraphMessageId"],
                saga["replyRecipient"],
            ),
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "accepted",
            evidence={"httpStatus": 202, "phase": "send"},
        )
        wrong_parent_alias = _AliasDocRef(
            thread_ref,
            "users/user-2/threads/thread-ref-collision",
        )
        attempt = dict(thread_ref.data["terminalReplyAttempt"])

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "capability.*owner/fence|does not belong",
        ):
            send_permits.cas_terminal_reply_transition(
                self.firestore,
                wrong_parent_alias,
                wrong_parent_alias,
                saga,
                "terminal-owner-a",
                1,
                expected_attempt_status="sending",
                thread_patch={
                    "terminalReplyOwed": False,
                    "terminalReplyOutcome": "sent_indexed",
                    "terminalReplyAttempt": {
                        **attempt,
                        "status": "committed",
                        "outcome": "sent_indexed",
                    },
                },
                permit_settlement="settled_sent",
                capability=capability,
            )

        self.assertEqual("accepted", send_permits.read_permit(capability)["status"])

    def test_terminal_takeover_reconciles_exact_sent_evidence_without_capability(self):
        saga, thread_ref, claim_ref = _terminal_refs("thread-terminal-takeover")
        capability = send_permits.issue_terminal_graph_send_permit(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
        )
        send_permits.consume_graph_send_capability(
            capability,
            **_prepare_capability_for_send(
                capability,
                saga["sourceGraphMessageId"],
                saga["replyRecipient"],
            ),
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "accepted",
            evidence={"httpStatus": 202, "phase": "send"},
        )
        claim_ref.data["terminalSagaClaim"].update({
            "owner": "terminal-owner-b",
            "fencingToken": 2,
            "leaseUntil": datetime.now(timezone.utc) + timedelta(minutes=5),
        })
        claim_ref.version += 1
        attempt = dict(thread_ref.data["terminalReplyAttempt"])
        body_hash = attempt["responseBodyHash"]
        retained_permit = send_permits.read_permit(capability)

        send_permits.cas_terminal_reply_transition(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-b",
            2,
            expected_attempt_status="sending",
            thread_patch={
                "terminalReplyOwed": False,
                "terminalReplyOutcome": "sent_reconciled",
                "terminalReplyAttempt": {
                    **attempt,
                    "status": "reconciled",
                    "outcome": "sent_reconciled",
                },
            },
            permit_settlement="settled_sent",
            sent_evidence={
                **_actual_sent_envelope(retained_permit),
                "sentMessageId": retained_permit["preparedEnvelope"]["draftId"],
                "recipient": "broker@example.test",
                "bodyHash": body_hash,
                "conversationId": saga["sourceConversationId"],
                "sentDateTime": (
                    retained_permit["requestStartedAt"] + timedelta(seconds=1)
                ),
                "permitId": capability.permit_id,
                "sourceGraphMessageId": saga["sourceGraphMessageId"],
                "preparedEnvelopeHash": retained_permit[
                    "sendPreparedEnvelopeHash"
                ],
            },
        )

        self.assertEqual("sent_reconciled", thread_ref.data["terminalReplyOutcome"])
        self.assertEqual(
            "settled_sent",
            send_permits.read_permit(capability)["status"],
        )

    def test_terminal_takeover_without_exact_sent_evidence_stays_unresolved(self):
        saga, thread_ref, claim_ref = _terminal_refs("thread-terminal-no-evidence")
        capability = send_permits.issue_terminal_graph_send_permit(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
        )
        send_permits.consume_graph_send_capability(
            capability,
            **_prepare_capability_for_send(
                capability,
                saga["sourceGraphMessageId"],
                saga["replyRecipient"],
            ),
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "needs_reconciliation",
            evidence={"reason": "timeout", "phase": "send"},
        )
        claim_ref.data["terminalSagaClaim"].update({
            "owner": "terminal-owner-b",
            "fencingToken": 2,
            "leaseUntil": datetime.now(timezone.utc) + timedelta(minutes=5),
        })
        claim_ref.version += 1
        attempt = dict(thread_ref.data["terminalReplyAttempt"])

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "Sent evidence|evidence",
        ):
            send_permits.cas_terminal_reply_transition(
                self.firestore,
                thread_ref,
                claim_ref,
                saga,
                "terminal-owner-b",
                2,
                expected_attempt_status="sending",
                thread_patch={
                    "terminalReplyOwed": False,
                    "terminalReplyOutcome": "sent_reconciled",
                    "terminalReplyAttempt": {
                        **attempt,
                        "status": "reconciled",
                    },
                },
                permit_settlement="settled_sent",
            )

        self.assertTrue(thread_ref.data["terminalReplyOwed"])
        self.assertEqual(
            "needs_reconciliation",
            send_permits.read_permit(capability)["status"],
        )

    def test_terminal_ambiguous_review_remains_linked_until_exact_sent_settlement(self):
        saga, thread_ref, claim_ref = _terminal_refs("thread-terminal-review")
        capability = send_permits.issue_terminal_graph_send_permit(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
        )
        send_permits.consume_graph_send_capability(
            capability,
            **_prepare_capability_for_send(
                capability,
                saga["sourceGraphMessageId"],
                saga["replyRecipient"],
            ),
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "needs_reconciliation",
            evidence={
                "reason": "Graph /send response was ambiguous",
                "phase": "send",
            },
        )
        attempt = dict(thread_ref.data["terminalReplyAttempt"])
        review_ref = _DocRef({}, exists=False, doc_id="terminal-review-1")
        review_payload = {
            "threadId": thread_ref.id,
            "clientId": saga["clientId"],
            "source": "terminalGraphSendProtocol",
            "authoritative": True,
            "status": "needs_reconciliation",
            "alreadySent": None,
            "providerSendStarted": True,
            "sendOutcomeUnknown": True,
            "retryAllowed": False,
            "graphSendPermitId": capability.permit_id,
            "graphSendPermitHash": capability.immutable_hash,
        }

        send_permits.cas_terminal_reply_transition(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
            expected_attempt_status="sending",
            thread_patch={
                "terminalReplyOwed": True,
                "terminalReplyAttempt": {
                    **attempt,
                    "status": "needs_reconciliation",
                },
            },
            permit_settlement="reconciliation_recorded",
            side_documents=[(review_ref, review_payload)],
        )

        retained = send_permits.read_permit(capability)
        self.assertTrue(retained["terminalSendReviewRequired"])
        self.assertIs(review_ref, retained["terminalSendReviewEvidenceRef"])
        self.assertIsNone(review_ref.data["alreadySent"])
        self.assertFalse(review_ref.data["retryAllowed"])

        retained_attempt = dict(thread_ref.data["terminalReplyAttempt"])
        sent_at = retained["requestStartedAt"] + timedelta(seconds=1)
        send_permits.cas_terminal_reply_transition(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
            expected_attempt_status="needs_reconciliation",
            thread_patch={
                "terminalReplyOwed": False,
                "terminalReplyOutcome": "sent_reconciled",
                "terminalReplyAttempt": {
                    **retained_attempt,
                    "status": "reconciled",
                    "outcome": "sent_reconciled",
                },
            },
            permit_settlement="settled_sent",
            sent_evidence={
                **_actual_sent_envelope(retained),
                "sentMessageId": retained["preparedEnvelope"]["draftId"],
                "recipient": saga["replyRecipient"],
                "bodyHash": retained_attempt["responseBodyHash"],
                "conversationId": saga["sourceConversationId"],
                "sentDateTime": sent_at,
                "permitId": capability.permit_id,
                "sourceGraphMessageId": saga["sourceGraphMessageId"],
                "preparedEnvelopeHash": retained["sendPreparedEnvelopeHash"],
            },
        )

        settled = send_permits.read_permit(capability)
        self.assertFalse(settled["terminalSendReviewRequired"])
        self.assertEqual("reconciled_sent", review_ref.data["status"])
        self.assertTrue(review_ref.data["alreadySent"])
        self.assertFalse(review_ref.data["sendOutcomeUnknown"])
        self.assertEqual(
            retained["preparedEnvelope"]["draftId"],
            review_ref.data["sentMessageId"],
        )

    def test_terminal_review_exact_sent_reads_pending_before_atomic_writes(self):
        for pending_exists in (False, True):
            with self.subTest(pending_exists=pending_exists):
                firestore = _ReadBeforeWriteFirestore()
                thread_id = f"thread-terminal-review-pending-{pending_exists}"
                saga, thread_ref, claim_ref = _terminal_refs(thread_id)
                capability = send_permits.issue_terminal_graph_send_permit(
                    firestore,
                    thread_ref,
                    claim_ref,
                    saga,
                    "terminal-owner-a",
                    1,
                )
                send_permits.consume_graph_send_capability(
                    capability,
                    **_prepare_capability_for_send(
                        capability,
                        saga["sourceGraphMessageId"],
                        saga["replyRecipient"],
                    ),
                )
                send_permits.resolve_graph_send_permit(
                    capability,
                    "needs_reconciliation",
                    evidence={
                        "reason": "Graph /send response was ambiguous",
                        "phase": "send",
                    },
                )
                attempt = dict(thread_ref.data["terminalReplyAttempt"])
                review_ref = _DocRef(
                    {},
                    exists=False,
                    doc_id=f"terminal-review-pending-{pending_exists}",
                )
                review_payload = {
                    "threadId": thread_id,
                    "clientId": saga["clientId"],
                    "source": "terminalGraphSendProtocol",
                    "authoritative": True,
                    "status": "needs_reconciliation",
                    "alreadySent": None,
                    "providerSendStarted": True,
                    "sendOutcomeUnknown": True,
                    "retryAllowed": False,
                    "graphSendPermitId": capability.permit_id,
                    "graphSendPermitHash": capability.immutable_hash,
                }
                send_permits.cas_terminal_reply_transition(
                    firestore,
                    thread_ref,
                    claim_ref,
                    saga,
                    "terminal-owner-a",
                    1,
                    expected_attempt_status="sending",
                    thread_patch={
                        "terminalReplyOwed": True,
                        "terminalReplyAttempt": {
                            **attempt,
                            "status": "needs_reconciliation",
                        },
                    },
                    permit_settlement="reconciliation_recorded",
                    side_documents=[(review_ref, review_payload)],
                )

                retained = send_permits.read_permit(capability)
                retained_attempt = dict(
                    thread_ref.data["terminalReplyAttempt"]
                )
                pending_ref = _DocRef(
                    {
                        "threadId": thread_id,
                        "msgId": saga["sourceGraphMessageId"],
                        "recipient": saga["replyRecipient"],
                        "responseBody": saga["responseBody"],
                        "clientId": saga["clientId"],
                        "conversationId": saga["sourceConversationId"],
                        "processingBy": None,
                        "processingLeaseUntil": None,
                    }
                    if pending_exists
                    else {},
                    exists=pending_exists,
                    doc_id=thread_id,
                )
                commits_before_settlement = firestore.commit_attempts

                send_permits.cas_terminal_reply_transition(
                    firestore,
                    thread_ref,
                    claim_ref,
                    saga,
                    "terminal-owner-a",
                    1,
                    expected_attempt_status="needs_reconciliation",
                    thread_patch={
                        "terminalReplyOwed": False,
                        "terminalReplyOutcome": "sent_reconciled",
                        "terminalReplyAttempt": {
                            **retained_attempt,
                            "status": "reconciled",
                            "outcome": "sent_reconciled",
                        },
                    },
                    permit_settlement="settled_sent",
                    sent_evidence=_exact_sent_evidence(capability),
                    pending_delete_ref=pending_ref,
                )

                settled = send_permits.read_permit(capability)
                self.assertEqual(
                    commits_before_settlement + 1,
                    firestore.commit_attempts,
                )
                self.assertEqual("settled_sent", settled["status"])
                self.assertFalse(settled["terminalSendReviewRequired"])
                self.assertEqual("reconciled_sent", review_ref.data["status"])
                self.assertTrue(review_ref.data["alreadySent"])
                self.assertFalse(review_ref.data["sendOutcomeUnknown"])
                self.assertFalse(thread_ref.data["terminalReplyOwed"])
                self.assertEqual(
                    "sent_reconciled",
                    thread_ref.data["terminalReplyOutcome"],
                )
                self.assertFalse(pending_ref.exists)
                expected_operations = ["set", "update", "update"]
                if pending_exists:
                    expected_operations.append("delete")
                self.assertCountEqual(
                    expected_operations,
                    [
                        operation
                        for operation, _path, _doc_id, _data
                        in firestore.commit_payloads[-1]
                    ],
                )

    def test_terminal_takeover_settles_definite_unsent_before_queue_intent(self):
        saga, thread_ref, claim_ref = _terminal_refs("thread-terminal-unsent")
        capability = send_permits.issue_terminal_graph_send_permit(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "definitely_not_sent",
            evidence={"phase": "final_campaign_gate"},
        )
        claim_ref.data["terminalSagaClaim"].update({
            "owner": "terminal-owner-b",
            "fencingToken": 2,
            "leaseUntil": datetime.now(timezone.utc) + timedelta(minutes=5),
        })
        claim_ref.version += 1
        attempt = dict(thread_ref.data["terminalReplyAttempt"])
        pending_ref = _DocRef({}, exists=False, doc_id=thread_ref.id)
        pending_payload = {
            "threadId": thread_ref.id,
            "msgId": saga["sourceGraphMessageId"],
            "recipient": saga["replyRecipient"],
            "responseBody": saga["responseBody"],
            "clientId": saga["clientId"],
            "conversationId": saga["sourceConversationId"],
            "attempts": 1,
        }

        send_permits.cas_terminal_reply_transition(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-b",
            2,
            expected_attempt_status="sending",
            thread_patch={
                "terminalReplyAttempt": {
                    **attempt,
                    "status": "queueing_response_retry",
                    "queueDocumentId": thread_ref.id,
                },
            },
            permit_settlement="settled_definitely_not_sent",
            pending_upsert=(pending_ref, pending_payload),
        )

        self.assertTrue(thread_ref.data["terminalReplyOwed"])
        self.assertEqual(
            "queueing_response_retry",
            thread_ref.data["terminalReplyAttempt"]["status"],
        )
        self.assertEqual(
            "settled_definitely_not_sent",
            send_permits.read_permit(capability)["status"],
        )
        self.assertTrue(pending_ref.exists)
        self.assertEqual(saga["responseBody"], pending_ref.data["responseBody"])

    def test_accepted_before_exact_pending_settlement_still_blocks_terminal_stage(self):
        token = "pending-worker-a"
        thread_ref = _DocRef({}, doc_id="thread-accepted-gap")
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-a")
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )
        send_permits.consume_graph_send_capability(
            capability,
            **_prepare_capability_for_send(
                capability,
                loaded["msgId"],
                loaded["recipient"],
            ),
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "accepted",
            evidence={"httpStatus": 202, "phase": "send"},
        )

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "unresolved",
        ):
            send_permits.assert_terminal_staging_allowed(
                self.firestore.transaction(),
                thread_ref,
            )

        send_permits.cas_pending_claim_transition(
            self.firestore,
            thread_ref,
            pending_ref,
            loaded,
            token,
            delete_pending=True,
            capability=capability,
            permit_settlement="settled_sent",
            sent_evidence=_exact_sent_evidence(capability),
            side_documents=(_pending_completion_document(
                thread_ref,
                pending_ref,
                loaded,
                capability,
                _exact_sent_evidence(capability),
            ),),
        )
        transaction = self.firestore.transaction()
        send_permits.assert_terminal_staging_allowed(transaction, thread_ref)

    def test_stale_pending_worker_cannot_settle_accepted_permit_or_delete_replacement(self):
        token = "pending-worker-a"
        thread_ref = _DocRef({}, doc_id="thread-stale-replacement")
        loaded = _pending_data(thread_ref.id, token=token, body="Body A")
        pending_ref = _DocRef(loaded, doc_id="pending-a")
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )
        send_permits.consume_graph_send_capability(
            capability,
            **_prepare_capability_for_send(
                capability,
                loaded["msgId"],
                loaded["recipient"],
            ),
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "accepted",
            evidence={"httpStatus": 202, "phase": "send"},
        )
        replacement = _pending_data(
            thread_ref.id,
            token="pending-worker-b",
            body="Body B",
        )
        pending_ref.data = replacement
        pending_ref.version += 1

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "token|envelope",
        ):
            send_permits.cas_pending_claim_transition(
                self.firestore,
                thread_ref,
                pending_ref,
                loaded,
                token,
                delete_pending=True,
                capability=capability,
                permit_settlement="settled_sent",
            )

        self.assertTrue(pending_ref.exists)
        self.assertEqual("Body B", pending_ref.data["responseBody"])
        self.assertEqual("accepted", send_permits.read_permit(capability)["status"])

    def test_accepted_is_illegal_before_request_started(self):
        token = "pending-worker-a"
        thread_ref = _DocRef({}, doc_id="thread-illegal-transition")
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-a")
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "illegal.*issued -> accepted",
        ):
            send_permits.resolve_graph_send_permit(
                capability,
                "accepted",
                evidence={"httpStatus": 202, "phase": "send"},
            )

    def test_resolved_prior_permit_allows_new_generation_and_retains_old_doc(self):
        thread_ref = _DocRef({}, doc_id="thread-generations")
        token_a = "pending-worker-a"
        loaded_a = _pending_data(thread_ref.id, token=token_a, body="Body A")
        pending_a = _DocRef(loaded_a, doc_id="pending-a")
        capability_a = self._issue_pending(
            thread_ref,
            pending_a,
            dict(loaded_a),
            token_a,
        )
        send_permits.consume_graph_send_capability(
            capability_a,
            **_prepare_capability_for_send(
                capability_a,
                loaded_a["msgId"],
                loaded_a["recipient"],
            ),
        )
        send_permits.resolve_graph_send_permit(
            capability_a,
            "accepted",
            evidence={"httpStatus": 202, "phase": "send"},
        )
        send_permits.cas_pending_claim_transition(
            self.firestore,
            thread_ref,
            pending_a,
            loaded_a,
            token_a,
            delete_pending=True,
            capability=capability_a,
            permit_settlement="settled_sent",
            sent_evidence=_exact_sent_evidence(capability_a),
            side_documents=(_pending_completion_document(
                thread_ref,
                pending_a,
                loaded_a,
                capability_a,
                _exact_sent_evidence(capability_a),
            ),),
        )

        token_b = "pending-worker-b"
        loaded_b = _pending_data(thread_ref.id, token=token_b, body="Body B")
        pending_b = _DocRef(loaded_b, doc_id="pending-b")
        capability_b = self._issue_pending(
            thread_ref,
            pending_b,
            dict(loaded_b),
            token_b,
        )

        self.assertNotEqual(capability_a.permit_id, capability_b.permit_id)
        permit_docs = thread_ref.collection("graphSendPermits").docs
        self.assertEqual({capability_a.permit_id, capability_b.permit_id}, set(permit_docs))
        self.assertEqual(
            capability_b.permit_id,
            thread_ref.data["activeGraphSendPermit"]["permitId"],
        )
        self.assertEqual(
            "settled_sent",
            permit_docs[capability_a.permit_id].data["status"],
        )

    def test_expired_unresolved_permit_blocks_new_issue_and_resend(self):
        thread_ref = _DocRef({}, doc_id="thread-expired")
        token_a = "pending-worker-a"
        loaded_a = _pending_data(thread_ref.id, token=token_a)
        pending_a = _DocRef(loaded_a, doc_id="pending-a")
        capability_a = self._issue_pending(
            thread_ref,
            pending_a,
            dict(loaded_a),
            token_a,
        )
        permit_a = thread_ref.collection("graphSendPermits").document(
            capability_a.permit_id
        )
        permit_a.data["leaseUntil"] = datetime.now(timezone.utc) - timedelta(seconds=1)

        token_b = "pending-worker-b"
        loaded_b = _pending_data(thread_ref.id, token=token_b, body="replacement")
        pending_b = _DocRef(loaded_b, doc_id="pending-b")
        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "unresolved|expired",
        ):
            self._issue_pending(
                thread_ref,
                pending_b,
                dict(loaded_b),
                token_b,
            )

        self.assertEqual(1, len(thread_ref.collection("graphSendPermits").docs))
        self.assertEqual("issued", permit_a.data["status"])

    def _prepare_canonical_pending_ambiguity(self, thread_id):
        prepared = self._prepare_pending_draft(
            thread_id=thread_id,
            canonical_user_id="u",
        )
        capability = prepared["capability"]
        send_permits.consume_graph_send_capability(
            capability,
            source_graph_message_id=prepared["source_id"],
            draft_id=prepared["draft_id"],
            subject=prepared["subject"],
            html_body=prepared["html_body"],
            to_recipients=prepared["to_recipients"],
            cc_recipients=prepared["cc_recipients"],
            attachments=prepared["attachments"],
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "needs_reconciliation",
            evidence={"reason": "provider timeout", "phase": "send"},
        )
        prepared["pending_ref"].data["processingLeaseUntil"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        prepared["pending_ref"].version += 1
        evidence_ref = _DocRef(
            {},
            exists=False,
            doc_id=f"pending-{capability.permit_id}",
            path=(
                f"users/u/threads/{thread_id}/graphSendReviews/"
                f"pending-{capability.permit_id}"
            ),
        )
        send_permits.reconcile_pending_graph_send_permit(
            self.firestore,
            prepared["thread_ref"],
            prepared["pending_ref"],
            prepared["loaded"],
            outcome="send_needs_review",
            evidence_document=(
                evidence_ref,
                {
                    "status": "needs_reconciliation",
                    "alreadySent": None,
                    "providerSendStarted": True,
                    "sendOutcomeUnknown": True,
                },
            ),
        )
        return prepared, evidence_ref, send_permits.read_permit(capability)

    def test_provider_resolution_revalidates_current_terminal_issuer(self):
        saga, thread_ref, claim_ref = _terminal_refs(
            "thread-stale-provider-resolution"
        )
        thread_ref.path = "users/u/threads/thread-stale-provider-resolution"
        capability = send_permits.issue_terminal_graph_send_permit(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
        )
        claim_ref.data["terminalSagaClaim"].update({
            "owner": "terminal-owner-b",
            "fencingToken": 2,
        })
        claim_ref.version += 1

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "owner|fence|issuer",
        ):
            send_permits.resolve_graph_send_permit(
                capability,
                "definitely_not_sent",
                evidence={"phase": "provider_response"},
            )

        self.assertEqual("issued", send_permits.read_permit(capability)["status"])

    def test_terminal_sent_cas_rejects_lookalike_pending_delete_path(self):
        thread_id = "thread-wrong-pending-delete"
        saga, thread_ref, claim_ref = _terminal_refs(thread_id)
        thread_ref.path = f"users/u/threads/{thread_id}"
        capability = send_permits.issue_terminal_graph_send_permit(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
        )
        send_permits.consume_graph_send_capability(
            capability,
            **_prepare_capability_for_send(
                capability,
                saga["sourceGraphMessageId"],
                saga["replyRecipient"],
            ),
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "accepted",
            evidence={"httpStatus": 202, "phase": "send"},
        )
        lookalike_pending = _DocRef(
            {
                "threadId": thread_id,
                "msgId": saga["sourceGraphMessageId"],
                "recipient": saga["replyRecipient"],
                "responseBody": saga["responseBody"],
                "clientId": saga["clientId"],
                "conversationId": saga["sourceConversationId"],
                "processingBy": None,
                "processingLeaseUntil": (
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                ),
            },
            doc_id=thread_id,
            path=f"users/u/lookalikePending/{thread_id}",
        )
        attempt = dict(thread_ref.data["terminalReplyAttempt"])

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "pending.*path|canonical.*pending",
        ):
            send_permits.cas_terminal_reply_transition(
                self.firestore,
                thread_ref,
                claim_ref,
                saga,
                "terminal-owner-a",
                1,
                expected_attempt_status="sending",
                thread_patch={
                    "terminalReplyOwed": False,
                    "terminalReplyOutcome": "sent_indexed",
                    "terminalReplyAttempt": {
                        **attempt,
                        "status": "committed",
                        "outcome": "sent_indexed",
                    },
                },
                permit_settlement="settled_sent",
                capability=capability,
                sent_evidence=_exact_sent_evidence(capability),
                pending_delete_ref=lookalike_pending,
            )

        self.assertTrue(lookalike_pending.exists)
        self.assertEqual("accepted", send_permits.read_permit(capability)["status"])

    def test_terminal_definite_unsent_rejects_lookalike_pending_upsert_path(self):
        thread_id = "thread-wrong-pending-upsert"
        saga, thread_ref, claim_ref = _terminal_refs(thread_id)
        thread_ref.path = f"users/u/threads/{thread_id}"
        capability = send_permits.issue_terminal_graph_send_permit(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "definitely_not_sent",
            evidence={"phase": "pre_send_gate"},
        )
        wrong_pending_ref = _DocRef(
            {},
            exists=False,
            doc_id=thread_id,
            path=f"users/u/lookalikePending/{thread_id}",
        )
        pending_payload = {
            "threadId": thread_id,
            "msgId": saga["sourceGraphMessageId"],
            "recipient": saga["replyRecipient"],
            "responseBody": saga["responseBody"],
            "clientId": saga["clientId"],
            "conversationId": saga["sourceConversationId"],
        }
        attempt = dict(thread_ref.data["terminalReplyAttempt"])

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "pending.*path|canonical.*pending",
        ):
            send_permits.cas_terminal_reply_transition(
                self.firestore,
                thread_ref,
                claim_ref,
                saga,
                "terminal-owner-a",
                1,
                expected_attempt_status="sending",
                thread_patch={
                    "terminalReplyAttempt": {
                        **attempt,
                        "status": "queueing_response_retry",
                        "queueDocumentId": thread_id,
                    },
                },
                permit_settlement="settled_definitely_not_sent",
                capability=capability,
                pending_upsert=(wrong_pending_ref, pending_payload),
            )

        self.assertFalse(wrong_pending_ref.exists)
        self.assertEqual(
            "definitely_not_sent",
            send_permits.read_permit(capability)["status"],
        )

    def test_terminal_reconciliation_rejects_noncanonical_review_path(self):
        thread_id = "thread-wrong-terminal-review"
        saga, thread_ref, claim_ref = _terminal_refs(thread_id)
        thread_ref.path = f"users/u/threads/{thread_id}"
        capability = send_permits.issue_terminal_graph_send_permit(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
        )
        send_permits.consume_graph_send_capability(
            capability,
            **_prepare_capability_for_send(
                capability,
                saga["sourceGraphMessageId"],
                saga["replyRecipient"],
            ),
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "needs_reconciliation",
            evidence={"reason": "timeout", "phase": "send"},
        )
        attempt = dict(thread_ref.data["terminalReplyAttempt"])
        wrong_review_ref = _DocRef(
            {},
            exists=False,
            doc_id="review-lookalike",
            path="users/u/deadLetterQueue/review-lookalike",
        )
        review_payload = {
            "source": "terminalGraphSendProtocol",
            "authoritative": True,
            "alreadySent": None,
            "providerSendStarted": True,
            "sendOutcomeUnknown": True,
            "retryAllowed": False,
            "graphSendPermitId": capability.permit_id,
            "graphSendPermitHash": capability.immutable_hash,
        }

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "review.*path|canonical.*review",
        ):
            send_permits.cas_terminal_reply_transition(
                self.firestore,
                thread_ref,
                claim_ref,
                saga,
                "terminal-owner-a",
                1,
                expected_attempt_status="sending",
                thread_patch={
                    "terminalReplyAttempt": {
                        **attempt,
                        "status": "needs_reconciliation",
                    },
                },
                permit_settlement="reconciliation_recorded",
                capability=capability,
                side_documents=((wrong_review_ref, review_payload),),
            )

        self.assertFalse(wrong_review_ref.exists)
        self.assertEqual(
            "needs_reconciliation",
            send_permits.read_permit(capability)["status"],
        )

    def test_pending_ambiguity_rejects_noncanonical_review_path(self):
        prepared = self._prepare_pending_draft(
            thread_id="thread-wrong-pending-review",
            canonical_user_id="u",
        )
        capability = prepared["capability"]
        send_permits.consume_graph_send_capability(
            capability,
            source_graph_message_id=prepared["source_id"],
            draft_id=prepared["draft_id"],
            subject=prepared["subject"],
            html_body=prepared["html_body"],
            to_recipients=prepared["to_recipients"],
            cc_recipients=prepared["cc_recipients"],
            attachments=prepared["attachments"],
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "needs_reconciliation",
            evidence={"reason": "timeout", "phase": "send"},
        )
        prepared["pending_ref"].data["processingLeaseUntil"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        prepared["pending_ref"].version += 1
        wrong_review_ref = _DocRef(
            {},
            exists=False,
            doc_id=f"pending-{capability.permit_id}",
            path=f"users/u/deadLetterQueue/pending-{capability.permit_id}",
        )

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "review.*path|canonical.*review",
        ):
            send_permits.reconcile_pending_graph_send_permit(
                self.firestore,
                prepared["thread_ref"],
                prepared["pending_ref"],
                prepared["loaded"],
                outcome="send_needs_review",
                evidence_document=(
                    wrong_review_ref,
                    {
                        "status": "needs_reconciliation",
                        "alreadySent": None,
                        "providerSendStarted": True,
                        "sendOutcomeUnknown": True,
                    },
                ),
            )

        self.assertFalse(wrong_review_ref.exists)
        self.assertTrue(prepared["pending_ref"].exists)

    def test_pending_direct_ambiguity_patch_cannot_erase_claim_or_envelope(self):
        prepared = self._prepare_pending_draft(
            thread_id="thread-malicious-ambiguity-patch",
            canonical_user_id="u",
        )
        capability = prepared["capability"]
        send_permits.consume_graph_send_capability(
            capability,
            source_graph_message_id=prepared["source_id"],
            draft_id=prepared["draft_id"],
            subject=prepared["subject"],
            html_body=prepared["html_body"],
            to_recipients=prepared["to_recipients"],
            cc_recipients=prepared["cc_recipients"],
            attachments=prepared["attachments"],
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "needs_reconciliation",
            evidence={"reason": "timeout", "phase": "send"},
        )
        review_ref = _DocRef(
            {},
            exists=False,
            doc_id=f"pending-{capability.permit_id}",
            path=(
                "users/u/threads/thread-malicious-ambiguity-patch/"
                f"graphSendReviews/pending-{capability.permit_id}"
            ),
        )

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "patch|claim|envelope|immutable",
        ):
            send_permits.cas_pending_claim_transition(
                self.firestore,
                prepared["thread_ref"],
                prepared["pending_ref"],
                prepared["loaded"],
                "pending-worker-a",
                pending_patch={
                    "status": "needs_reconciliation",
                    "processingBy": "replacement-worker",
                    "graphSendPermitId": None,
                    "responseBody": "tampered body",
                },
                side_documents=((
                    review_ref,
                    {
                        "alreadySent": None,
                        "providerSendStarted": True,
                        "sendOutcomeUnknown": True,
                        "retryAllowed": False,
                        "graphSendPermitId": capability.permit_id,
                        "graphSendPermitHash": capability.immutable_hash,
                    },
                ),),
                capability=capability,
                permit_settlement="reconciliation_recorded",
            )

        self.assertEqual(
            "pending-worker-a",
            prepared["pending_ref"].data["processingBy"],
        )
        self.assertEqual(
            capability.permit_id,
            prepared["pending_ref"].data["graphSendPermitId"],
        )

    def test_pending_exact_sent_must_resolve_the_retained_review_document(self):
        prepared, retained_review_ref, permit = (
            self._prepare_canonical_pending_ambiguity(
                "thread-retained-review-resolution"
            )
        )
        wrong_evidence_ref = _DocRef(
            {},
            exists=False,
            doc_id="lookalike-sent-evidence",
            path="users/u/deadLetterQueue/lookalike-sent-evidence",
        )
        sent_evidence = _exact_sent_evidence(
            prepared["capability"],
            html_body=prepared["html_body"],
        )

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "retained.*review|evidence.*path|canonical",
        ):
            send_permits.reconcile_pending_graph_send_permit(
                self.firestore,
                prepared["thread_ref"],
                prepared["pending_ref"],
                prepared["loaded"],
                outcome="sent",
                sent_evidence=sent_evidence,
                evidence_document=(
                    wrong_evidence_ref,
                    {
                        "status": "reconciled_sent",
                        "alreadySent": True,
                        "providerSendStarted": True,
                    },
                ),
                completion_document=_pending_completion_document(
                    prepared["thread_ref"],
                    prepared["pending_ref"],
                    prepared["loaded"],
                    prepared["capability"],
                    sent_evidence,
                ),
            )

        self.assertTrue(prepared["pending_ref"].exists)
        self.assertTrue(retained_review_ref.exists)
        self.assertTrue(permit["pendingSendReviewRequired"])

    def test_operator_lookup_must_be_recent_and_not_future_dated(self):
        for label, lookup_completed_at in (
            (
                "stale",
                datetime.now(timezone.utc) - timedelta(minutes=10),
            ),
            (
                "future",
                datetime.now(timezone.utc) + timedelta(minutes=10),
            ),
        ):
            with self.subTest(label=label):
                prepared, evidence_ref, permit = (
                    self._prepare_canonical_pending_ambiguity(
                        f"thread-operator-lookup-{label}"
                    )
                )
                settlement_id = f"settlement-{label}"
                audit_ref = _DocRef(
                    {},
                    exists=False,
                    doc_id=settlement_id,
                    path=(
                        "users/u/graphSendOperatorSettlements/"
                        f"{settlement_id}"
                    ),
                )

                with self.assertRaisesRegex(
                    send_permits.GraphSendPermitBlocked,
                    "fresh|lookup",
                ):
                    send_permits.operator_settle_pending_graph_send_review(
                        self.firestore,
                        prepared["thread_ref"],
                        prepared["pending_ref"],
                        prepared["loaded"],
                        expected_permit_id=permit["permitId"],
                        expected_permit_hash=permit["immutableHash"],
                        expected_reconciliation_evidence_hash=permit[
                            "pendingReconciliationEvidenceHash"
                        ],
                        reconciliation_evidence_ref=evidence_ref,
                        action="acknowledge_ambiguous_no_retry",
                        operator_id="authenticated-operator-uid",
                        operator_reason="No exact Sent match after fresh lookup.",
                        settlement_id=settlement_id,
                        audit_ref=audit_ref,
                        sent_lookup_completed_at=lookup_completed_at,
                    )

                self.assertTrue(prepared["pending_ref"].exists)
                self.assertFalse(audit_ref.exists)

    def test_operator_settlement_rejects_noncanonical_audit_path(self):
        prepared, evidence_ref, permit = (
            self._prepare_canonical_pending_ambiguity(
                "thread-wrong-operator-audit"
            )
        )
        settlement_id = "settlement-wrong-audit-path"
        wrong_audit_ref = _DocRef(
            {},
            exists=False,
            doc_id=settlement_id,
            path=f"users/u/deadLetterQueue/{settlement_id}",
        )

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "audit.*path|canonical.*audit",
        ):
            send_permits.operator_settle_pending_graph_send_review(
                self.firestore,
                prepared["thread_ref"],
                prepared["pending_ref"],
                prepared["loaded"],
                expected_permit_id=permit["permitId"],
                expected_permit_hash=permit["immutableHash"],
                expected_reconciliation_evidence_hash=permit[
                    "pendingReconciliationEvidenceHash"
                ],
                reconciliation_evidence_ref=evidence_ref,
                action="acknowledge_ambiguous_no_retry",
                operator_id="authenticated-operator-uid",
                operator_reason="No exact Sent match after fresh lookup.",
                settlement_id=settlement_id,
                audit_ref=wrong_audit_ref,
                sent_lookup_completed_at=datetime.now(timezone.utc),
            )

        self.assertTrue(prepared["pending_ref"].exists)
        self.assertFalse(wrong_audit_ref.exists)

    def test_terminal_cas_rejects_unowned_thread_patch_fields(self):
        thread_id = "thread-terminal-patch-allowlist"
        saga, thread_ref, claim_ref = _terminal_refs(thread_id)
        thread_ref.path = f"users/u/threads/{thread_id}"
        capability = send_permits.issue_terminal_graph_send_permit(
            self.firestore,
            thread_ref,
            claim_ref,
            saga,
            "terminal-owner-a",
            1,
        )
        send_permits.consume_graph_send_capability(
            capability,
            **_prepare_capability_for_send(
                capability,
                saga["sourceGraphMessageId"],
                saga["replyRecipient"],
            ),
        )
        send_permits.resolve_graph_send_permit(
            capability,
            "accepted",
            evidence={"httpStatus": 202, "phase": "send"},
        )
        attempt = dict(thread_ref.data["terminalReplyAttempt"])

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "thread patch|allowlist|unowned",
        ):
            send_permits.cas_terminal_reply_transition(
                self.firestore,
                thread_ref,
                claim_ref,
                saga,
                "terminal-owner-a",
                1,
                expected_attempt_status="sending",
                thread_patch={
                    "terminalReplyOwed": False,
                    "terminalReplyOutcome": "sent_indexed",
                    "terminalReplyAttempt": {
                        **attempt,
                        "status": "committed",
                        "outcome": "sent_indexed",
                    },
                    "terminalSaga": {"hijacked": True},
                },
                permit_settlement="settled_sent",
                capability=capability,
                sent_evidence=_exact_sent_evidence(capability),
            )

        self.assertEqual(saga, thread_ref.data["terminalSaga"])
        self.assertEqual("accepted", send_permits.read_permit(capability)["status"])

    def test_operator_replay_rejects_drifted_resolved_review_document(self):
        prepared, evidence_ref, permit = (
            self._prepare_canonical_pending_ambiguity(
                "thread-replay-review-drift"
            )
        )
        settlement_id = "settlement-replay-review-drift"
        audit_ref = _DocRef(
            {},
            exists=False,
            doc_id=settlement_id,
            path=(
                "users/u/graphSendOperatorSettlements/"
                f"{settlement_id}"
            ),
        )
        operator_reason = "Fresh lookup could not prove provider acceptance."
        send_permits.operator_settle_pending_graph_send_review(
            self.firestore,
            prepared["thread_ref"],
            prepared["pending_ref"],
            prepared["loaded"],
            expected_permit_id=permit["permitId"],
            expected_permit_hash=permit["immutableHash"],
            expected_reconciliation_evidence_hash=permit[
                "pendingReconciliationEvidenceHash"
            ],
            reconciliation_evidence_ref=evidence_ref,
            action="acknowledge_ambiguous_no_retry",
            operator_id="authenticated-operator-uid",
            operator_reason=operator_reason,
            settlement_id=settlement_id,
            audit_ref=audit_ref,
            sent_lookup_completed_at=datetime.now(timezone.utc),
        )
        evidence_ref.data["resolution"] = "drifted-after-settlement"
        evidence_ref.version += 1
        user_ref = _DocRef({}, doc_id="u", path="users/u")
        user_ref.collection("threads").docs[
            prepared["thread_ref"].id
        ] = prepared["thread_ref"]

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "review|evidence|linkage",
        ):
            send_permits.read_pending_graph_send_operator_settlement_replay(
                self.firestore,
                user_ref,
                audit_ref,
                pending_document_id=prepared["pending_ref"].id,
                expected_permit_id=permit["permitId"],
                expected_permit_hash=permit["immutableHash"],
                expected_reconciliation_evidence_hash=permit[
                    "pendingReconciliationEvidenceHash"
                ],
                operator_id="authenticated-operator-uid",
                operator_reason=operator_reason,
                settlement_id=settlement_id,
            )

    def test_expired_pending_permit_read_rejects_lookalike_pending_path(self):
        thread_id = "thread-expired-pending-path"
        thread_ref = _DocRef(
            {},
            doc_id=thread_id,
            path=f"users/u/threads/{thread_id}",
        )
        token = "pending-worker-a"
        loaded = _pending_data(thread_id, token=token)
        pending_ref = _DocRef(
            loaded,
            doc_id="pending-a",
            path="users/u/pendingResponses/pending-a",
        )
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )
        pending_ref.data["processingLeaseUntil"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        pending_ref.version += 1
        lookalike_ref = _AliasDocRef(
            pending_ref,
            "users/u/lookalikePending/pending-a",
        )

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "pending.*path|canonical.*pending|issuer",
        ):
            send_permits.read_expired_pending_graph_send_permit(
                self.firestore,
                thread_ref,
                lookalike_ref,
                loaded,
            )

        self.assertEqual("issued", send_permits.read_permit(capability)["status"])

    def test_settled_ambiguous_requires_complete_operator_resolution_schema(self):
        prepared, evidence_ref, permit = (
            self._prepare_canonical_pending_ambiguity(
                "thread-operator-schema"
            )
        )
        original_evidence_hash = permit["pendingReconciliationEvidenceHash"]
        settlement_id = "settlement-operator-schema"
        audit_ref = _DocRef(
            {},
            exists=False,
            doc_id=settlement_id,
            path=(
                "users/u/graphSendOperatorSettlements/"
                f"{settlement_id}"
            ),
        )
        send_permits.operator_settle_pending_graph_send_review(
            self.firestore,
            prepared["thread_ref"],
            prepared["pending_ref"],
            prepared["loaded"],
            expected_permit_id=permit["permitId"],
            expected_permit_hash=permit["immutableHash"],
            expected_reconciliation_evidence_hash=original_evidence_hash,
            reconciliation_evidence_ref=evidence_ref,
            action="acknowledge_ambiguous_no_retry",
            operator_id="authenticated-operator-uid",
            operator_reason="Fresh lookup could not prove provider acceptance.",
            settlement_id=settlement_id,
            audit_ref=audit_ref,
            sent_lookup_completed_at=datetime.now(timezone.utc),
        )
        settled = send_permits.read_permit(prepared["capability"])
        self.assertEqual(
            original_evidence_hash,
            settled["operatorOriginalReconciliationEvidenceHash"],
        )
        self.assertEqual("unknown_no_retry", settled["operatorResolution"])
        self.assertIs(audit_ref, settled["operatorSettlementAuditRef"])
        self.assertTrue(settled["operatorResolvedReviewEvidenceHash"])

        for missing_field in (
            "operatorSettlementAuditRef",
            "operatorSettlementAuditHash",
            "operatorOriginalReconciliationEvidenceHash",
            "operatorResolvedReviewEvidenceHash",
            "operatorResolution",
        ):
            with self.subTest(missing_field=missing_field):
                malformed = dict(settled)
                malformed.pop(missing_field)
                with self.assertRaisesRegex(
                    send_permits.GraphSendPermitError,
                    "ambiguous-no-retry|operator.*settlement",
                ):
                    send_permits._validate_permit(malformed)

    def test_permit_binds_exact_canonical_issuer_document_path(self):
        thread_id = "thread-canonical-issuer"
        thread_ref = _DocRef(
            {},
            doc_id=thread_id,
            path=f"users/u/threads/{thread_id}",
        )
        token = "pending-worker-a"
        loaded = _pending_data(thread_id, token=token)
        pending_ref = _DocRef(
            loaded,
            doc_id="pending-a",
            path="users/u/pendingResponses/pending-a",
        )
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )
        permit = send_permits.read_permit(capability)
        self.assertEqual("pending-a", permit["issuerDocumentId"])
        self.assertEqual(
            "users/u/pendingResponses/pending-a",
            permit["issuerDocumentPath"],
        )

        malformed = dict(permit)
        malformed["issuerDocumentPath"] = "users/u/deadLetterQueue/pending-a"
        malformed["immutableHash"] = send_permits._hash({
            field: malformed.get(field)
            for field in send_permits._PERMIT_IMMUTABLE_FIELDS
        })
        with self.assertRaisesRegex(
            send_permits.GraphSendPermitError,
            "issuer.*path|issuer.*identity",
        ):
            send_permits._validate_permit(malformed)

    def test_pending_permit_issue_rejects_same_id_under_wrong_user_path(self):
        thread_id = "thread-wrong-issuer-user"
        thread_ref = _DocRef(
            {},
            doc_id=thread_id,
            path=f"users/u/threads/{thread_id}",
        )
        token = "pending-worker-a"
        loaded = _pending_data(thread_id, token=token)
        wrong_pending_ref = _DocRef(
            loaded,
            doc_id="pending-a",
            path="users/other-user/pendingResponses/pending-a",
        )

        with self.assertRaisesRegex(
            send_permits.GraphSendPermitBlocked,
            "issuer.*path|canonical.*path",
        ):
            self._issue_pending(
                thread_ref,
                wrong_pending_ref,
                dict(loaded),
                token,
            )

        self.assertNotIn("activeGraphSendPermit", thread_ref.data)
        self.assertNotIn("graphSendPermitId", wrong_pending_ref.data)

    def test_persisted_permit_never_contains_plaintext_capability(self):
        token = "pending-worker-a"
        thread_ref = _DocRef({}, doc_id="thread-no-plaintext")
        loaded = _pending_data(thread_ref.id, token=token)
        pending_ref = _DocRef(loaded, doc_id="pending-a")
        capability = self._issue_pending(
            thread_ref,
            pending_ref,
            dict(loaded),
            token,
        )
        permit = send_permits.read_permit(capability)

        self.assertNotIn(capability.capability, repr(permit))
        self.assertEqual(
            hashlib.sha256(capability.capability.encode("utf-8")).hexdigest(),
            permit["capabilityHash"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
