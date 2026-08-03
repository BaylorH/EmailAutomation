"""Combination stress deck: karsen_launch_placeholder_and_tour_leak.

Deck (docs/release-safety/feature-gradebook.json ->
combinationStressDecks.karsen_launch_placeholder_and_tour_leak) chains three
combinationPlaybooks that all fire on the SAME Karsen launch conversation:

  * manual_reply_before_retry
        "User replies manually or continues the thread before pending/dead-letter
         /follow-up retry; autonomous send must suppress or reconcile."
  * graph_accepted_but_index_missing
        "Graph returns/accepts sent message but thread/message indexing fails;
         retry must reconcile Sent Items instead of double-sending."
  * tour_unavailable_but_property_viable
        "Broker says tours are unavailable; classifier must not mark property
         non-viable or stopped unless the property itself is unavailable."

variantsToCross: "missing-name plus tour wording in uploaded template",
"normal-user entitlement plus scheduler retry", "manual user reply before
worker retry".

mustProve:
  1. no tour scheduling email leaves Production V1 core lane
  2. raw name placeholder blocks before Graph
  3. manual continuation suppresses retry

WHY THIS IS A REAL INTEGRATION TEST (not a per-feature unit test)
-----------------------------------------------------------------
It drives the REAL retry send handler
``email_automation.pending_responses.process_pending_responses`` over ONE mixed
queue of Karsen-launch pending responses, through the REAL Sent Items
reconciliation guards
(``sent_mail_guard.find_matching_sent_message_for_retry`` and
``find_sent_conversation_continuation_for_retry``) and the REAL outbound body
validator (``outbound_safety.validate_outbound_body``). The ONLY things faked are
the three external boundaries: Firestore (``clients._fs``), the Microsoft Graph
Sent Items REST call (``sent_mail_guard.requests.get``), and the terminal Graph
send (``processing.send_reply_in_thread``). ZERO live sends, zero live sheet
writes.

The classifier half (``ai_processing._augment_events_with_deterministic_signals``)
is pure and is driven directly on a tours-only-unavailable Karsen broker reply.

The interaction invariant the deck exists to protect: across all of
placeholder-block, tour-leak-block, already-sent reconciliation and
manual-continuation suppression firing on the SAME queue, EXACTLY the one clean,
viable, human-safe response is sent, it carries ITS OWN row/thread anchor (not a
neighbor's), and every unsafe / already-continued / already-sent sibling is
diverted to manual review WITHOUT a second send. Break any single guard and the
concrete assertions below go red (verified fail-ability: the send recorder
asserts an exact call list and exact anchor, not merely "<= 1 send").
"""

import os
import sys
import types
import unittest
from datetime import timedelta
from html import escape
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import pending_responses
from email_automation import processing as processing_module
from email_automation import send_permits
from email_automation.outbound_safety import validate_outbound_body
from email_automation.ai_processing import _augment_events_with_deterministic_signals
from email_automation.campaign_safety import CampaignAutomationDecision
from email_automation.column_config import get_default_column_config


# ---------------------------------------------------------------------------
# Firestore boundary fake (pendingResponses read + deadLetterQueue writes).
# ---------------------------------------------------------------------------
class _FakeDocRef:
    def __init__(self, doc=None, doc_id=None, *, path=None, collection=None):
        self._doc = doc
        self.id = doc_id or getattr(doc, "id", None)
        self.path = path
        self._collection = collection
        self._collections = {}
        self.deleted = False
        self.update_calls = []

    def delete(self):
        self.deleted = True
        if self._doc is not None:
            self._doc.exists = False

    def update(self, data):
        self.update_calls.append(data)
        if self._doc is not None:
            self._doc._data.update(data)
            self._doc.exists = True

    def set(self, data, merge=False):
        if self._doc is None:
            self._doc = _FakeDoc(self.id, {}, reference=self)
            if self._collection is not None:
                self._collection.docs.append(self._doc)
        if merge:
            self._doc._data.update(data)
        else:
            self._doc._data = dict(data)
        self._doc.exists = True
        self.deleted = False
        if (
            self._collection is not None
            and self._collection.path.endswith("/deadLetterQueue")
        ):
            self._collection.add_calls.append(dict(data))

    def create(self, data):
        if self._doc is not None and self._doc.exists:
            raise RuntimeError("document already exists")
        self.set(data)

    def collection(self, name):
        return self._collections.setdefault(
            name,
            _FakeCollection(path=f"{self.path}/{name}"),
        )

    def get(self, transaction=None):
        if transaction is not None:
            return transaction.get(self)
        if self._doc is not None:
            return self._doc
        return types.SimpleNamespace(exists=False, to_dict=lambda: {})


class _FakeDoc:
    def __init__(self, doc_id, data, *, reference=None):
        self.id = doc_id
        self._data = data
        self.exists = True
        self.reference = reference or _FakeDocRef(self, doc_id)
        self.reference._doc = self

    def to_dict(self):
        return dict(self._data)


class _FakeCollection:
    def __init__(self, docs=None, client_status=None, *, path=""):
        self.docs = docs if docs is not None else []
        self.add_calls = []
        self.client_status = client_status
        self.path = path
        for doc in self.docs:
            doc.reference.path = f"{path}/{doc.id}" if path else None
            doc.reference._collection = self

    def stream(self):
        return [doc for doc in self.docs if doc.exists]

    def where(self, *, filter):
        return _FakeQuery(self, filters=(filter,))

    def add(self, data):
        self.add_calls.append(data)
        return _FakeDocRef()

    def document(self, doc_id):
        for doc in self.docs:
            if doc.id == doc_id:
                return doc.reference
        status = self.client_status
        if status is not None:
            return types.SimpleNamespace(
                get=lambda: types.SimpleNamespace(
                    exists=True,
                    to_dict=lambda: {"status": status},
                )
            )
        return _FakeDocRef(
            doc_id=doc_id,
            path=f"{self.path}/{doc_id}" if self.path else None,
            collection=self,
        )


class _FakeQuery:
    def __init__(self, collection, *, filters=(), query_limit=None):
        self.collection = collection
        self.filters = tuple(filters)
        self.query_limit = query_limit

    def where(self, *, filter):
        return _FakeQuery(
            self.collection,
            filters=(*self.filters, filter),
            query_limit=self.query_limit,
        )

    def limit(self, count):
        return _FakeQuery(
            self.collection,
            filters=self.filters,
            query_limit=count,
        )

    def stream(self):
        docs = list(self.collection.stream())
        for field_filter in self.filters:
            docs = [
                doc
                for doc in docs
                if doc.to_dict().get(field_filter.field_path)
                == field_filter.value
            ]
        if self.query_limit is not None:
            docs = docs[:self.query_limit]
        return docs


class _FakeTransaction:
    def __init__(self):
        self._operations = []

    def get(self, document_ref):
        return document_ref.get()

    def update(self, document_ref, data):
        self._operations.append(("update", document_ref, dict(data)))

    def set(self, document_ref, data, merge=False):
        self._operations.append(("set", document_ref, dict(data), merge))

    def delete(self, document_ref):
        self._operations.append(("delete", document_ref))

    def commit(self):
        for operation in self._operations:
            kind, document_ref, *payload = operation
            if kind == "update":
                document_ref.update(payload[0])
            elif kind == "set":
                document_ref.set(payload[0], merge=payload[1])
            else:
                document_ref.delete()


class _FakeUserRef:
    def __init__(self, firestore, user_id):
        self._firestore = firestore
        self.id = user_id
        self.path = f"users/{user_id}"

    def collection(self, name):
        collection = self._firestore.collections.setdefault(
            name,
            _FakeCollection(path=f"{self.path}/{name}"),
        )
        if not collection.path:
            collection.path = f"{self.path}/{name}"
        for doc in collection.docs:
            doc.reference.path = f"{collection.path}/{doc.id}"
            doc.reference._collection = collection
        return collection


class _FakeUsersCollection:
    def __init__(self, firestore):
        self._firestore = firestore

    def document(self, user_id):
        return _FakeUserRef(self._firestore, user_id)


class _FakeFirestore:
    def __init__(self, pending_docs):
        thread_clients = {
            str(doc.to_dict().get("threadId") or ""): str(
                doc.to_dict().get("clientId") or ""
            )
            for doc in pending_docs
            if doc.to_dict().get("threadId")
        }
        client_ids = sorted({
            client_id
            for client_id in thread_clients.values()
            if client_id
        })
        self.collections = {
            "pendingResponses": _FakeCollection(pending_docs),
            "deadLetterQueue": _FakeCollection(),
            "threads": _FakeCollection([
                _FakeDoc(
                    thread_id,
                    {"clientId": thread_clients[thread_id]},
                )
                for thread_id in sorted(thread_clients)
            ]),
            "clients": _FakeCollection([
                _FakeDoc(client_id, {"status": "live"})
                for client_id in client_ids
            ]),
        }

    def collection(self, name):
        if name == "users":
            return _FakeUsersCollection(self)
        if name == "systemConfig":
            return types.SimpleNamespace(
                document=lambda _doc_id: types.SimpleNamespace(
                    get=lambda: types.SimpleNamespace(
                        exists=True,
                        to_dict=lambda: {
                            "automationEnabled": True,
                            "allowedUids": [],
                        },
                    )
                )
            )
        if name in {"clients", "archivedClients"}:
            return _FakeCollection(client_status="live" if name == "clients" else None)
        return self.collections.setdefault(name, _FakeCollection())

    def transaction(self):
        return _FakeTransaction()


# ---------------------------------------------------------------------------
# Microsoft Graph Sent Items boundary fake. A single global Sent Items store is
# served to BOTH real guards; each guard applies its own real server/client
# filters (conversationId, recipient, body, sentDateTime) against it.
# ---------------------------------------------------------------------------
class _FakeGraphResponse:
    def __init__(self, value):
        self._value = value
        self.status_code = 200

    def json(self):
        return {"value": list(self._value)}


class _FakeSentItems:
    def __init__(self, messages):
        self.messages = messages

    def get(self, url, headers=None, params=None, timeout=None):
        # Both guards hit /me/mailFolders/SentItems/messages. We return the whole
        # store and let the REAL guard code do its own filtering, so this fake
        # never encodes the pass/fail decision itself.
        return _FakeGraphResponse(self.messages)


def _sent_message(*, conv, recipient, body, sent_iso, mid, imid):
    return {
        "id": mid,
        "internetMessageId": imid,
        "conversationId": conv,
        "subject": "RE: 4200 Karsen Launch Blvd",
        "toRecipients": [{"emailAddress": {"address": recipient}}],
        "sentDateTime": sent_iso,
        "body": {"contentType": "text", "content": body},
        "bodyPreview": body[:200],
    }


# ---------------------------------------------------------------------------
# Terminal Graph send boundary fake — records every send so we can assert the
# EXACT set of anchors that reached a real send (must be only the clean one).
# ---------------------------------------------------------------------------
class _SendRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        capability = kwargs.get("graph_send_capability")
        if not isinstance(capability, send_permits.GraphSendCapability):
            raise AssertionError("combo send requires a typed Graph capability")
        body = kwargs.get("body") or ""
        recipient = kwargs.get("recipient")
        source_id = kwargs.get("current_msg_id")
        draft_id = f"draft-{capability.permit_id}"
        subject = "RE: Combo terminal test subject"
        html_body = f"<p>{escape(body).replace(chr(10), '<br>')}</p>"
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
            source_graph_message_id=source_id,
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
        permit = send_permits.read_permit(capability)
        envelope = permit["preparedEnvelope"]
        processing_module._set_reply_send_outcome(
            outcome="sent_indexed",
            conversation_id=permit.get("conversationId"),
            exact_sent_evidence={
                "id": draft_id,
                "sentMessageId": draft_id,
                "internetMessageId": f"<sent-{capability.permit_id}@mock.test>",
                "isDraft": False,
                "subject": envelope["subject"],
                "recipient": permit["recipient"],
                "bodyHash": permit["bodyHash"],
                "conversationId": permit.get("conversationId"),
                "sentDateTime": permit["requestStartedAt"] + timedelta(seconds=1),
                "permitId": permit["permitId"],
                "sourceGraphMessageId": permit["sourceGraphMessageId"],
                "preparedEnvelopeHash": envelope["preparedEnvelopeHash"],
                "toRecipients": [
                    {"emailAddress": {"address": address}}
                    for address in envelope["toRecipients"]
                ],
                "ccRecipients": [],
                "bccRecipients": [],
                "body": {"contentType": "HTML", "content": html_body},
                "attachments": [],
            },
        )
        self.calls.append({
            "user_id": kwargs.get("user_id"),
            "thread_id": kwargs.get("thread_id"),
            "recipient": kwargs.get("recipient"),
            "current_msg_id": kwargs.get("current_msg_id"),
            "body": kwargs.get("body"),
        })
        return True  # a clean, viable reply sends successfully


# A Karsen-launch conversation body that is clean and human-safe (no placeholder,
# no tour/LOI scheduling language, no confidential disclosure).
CLEAN_VIABLE_BODY = (
    "Hi Karsen,\n\nThanks for the note on 4200 Karsen Launch Blvd. Happy to send "
    "over the flyer and current asking rate whenever useful.\n\nBest,\nAvery"
)


class KarsenLaunchPlaceholderAndTourLeakComboTests(unittest.TestCase):
    UID = "karsen-uid"
    HEADERS = {"Authorization": "Bearer token"}

    # ---- Playbook 3: tour_unavailable_but_property_viable (pure classifier) ---
    def test_tours_only_reply_keeps_property_viable_terminal_classification(self):
        """A tours-only-unavailable Karsen broker reply must classify as
        still-viable: no property_unavailable / terminal event, and the tour is
        re-requested rather than dropped. This is the terminal-vs-viable half of
        the deck (a false 'property_unavailable' here would stop a live listing).
        """
        thread = [
            {"direction": "outbound",
             "content": "Can you confirm a tour date and requested arrival time for 4200 Karsen Launch Blvd?"},
            {"direction": "inbound",
             "content": ("No tours right now while the current tenant is still in "
                         "place, but the suite is very much still on the market.")},
        ]
        # Simulate the LLM mislabeling the tours-only reply as a terminal
        # property_unavailable; the deterministic guard must scrub it.
        proposal = {"events": [{"type": "property_unavailable", "reason": "misread"}]}
        out = _augment_events_with_deterministic_signals(proposal, thread)
        types_out = [(e or {}).get("type") for e in out.get("events", [])]

        self.assertNotIn(
            "property_unavailable", types_out,
            "TERMINAL-VS-VIABLE VIOLATION: a tours-only broker reply left the "
            "Karsen property marked non-viable/terminal.",
        )
        self.assertIn(
            "tour_requested", types_out,
            "A tours-only reply must re-request the tour, not silently drop it.",
        )

        # Cross-layer tie: even though the property stays viable, an outbound body
        # that carries tour scheduling wording must STILL be blocked from the send
        # lane (mustProve #1: no tour scheduling email leaves the core lane).
        tour_body = "Hi Karsen, we can set up a showing of the suite this Friday at 2pm."
        v = validate_outbound_body(tour_body)
        self.assertFalse(
            v.is_safe,
            "TOUR LEAK: tour scheduling wording passed the outbound guard.",
        )

    # ---- Full chained retry lane (all three playbooks on one queue) ----------
    def _build_queue(self):
        placeholder_tour_doc = _FakeDoc("thread-placeholder-tour", {
            # variant: "missing-name plus tour wording in uploaded template"
            "threadId": "thread-placeholder-tour",
            "msgId": "msg-A",
            "recipient": "broker.a@karsen-cre.com",
            "responseBody": ("Hi [NAME],\n\nGreat news — we can schedule a tour of "
                             "the space this Friday. Let me know.\n\nBest,\nAvery"),
            "clientId": "karsen",
            "attempts": 1,
            "lastError": "Graph 500 on first send",
            "lastSendAttemptAt": "2026-07-02T11:00:00Z",
            "conversationId": "conv-A",
            "subject": "4200 Karsen Launch Blvd",
        })
        tour_only_doc = _FakeDoc("thread-tour-only", {
            # resolved name, but tour scheduling wording -> mustProve #1
            "threadId": "thread-tour-only",
            "msgId": "msg-E",
            "recipient": "broker.e@karsen-cre.com",
            "responseBody": ("Hi Karsen,\n\nLet's book a tour of the suite Tuesday "
                             "at 2pm.\n\nBest,\nAvery"),
            "clientId": "karsen",
            "attempts": 1,
            "lastError": "Graph 500 on first send",
            "lastSendAttemptAt": "2026-07-02T11:00:00Z",
            "conversationId": "conv-E",
            "subject": "4200 Karsen Launch Blvd",
        })
        reconcile_doc = _FakeDoc("thread-reconcile", {
            # graph_accepted_but_index_missing: prior attempt is already in Sent
            "threadId": "thread-reconcile",
            "msgId": "msg-B",
            "recipient": "broker.b@karsen-cre.com",
            "responseBody": ("Hi Karsen,\n\nAttaching the current rent roll and "
                             "asking rate for 4200 Karsen Launch Blvd as "
                             "requested.\n\nBest,\nAvery"),
            "clientId": "karsen",
            "attempts": 1,
            "lastError": "Read timed out after Graph accepted the reply",
            "lastSendAttemptAt": "2026-07-02T11:00:00Z",
            "conversationId": "conv-B",
            "subject": "4200 Karsen Launch Blvd",
        })
        manual_doc = _FakeDoc("thread-manual", {
            # manual_reply_before_retry: user already continued in Sent Items
            "threadId": "thread-manual",
            "msgId": "msg-C",
            "recipient": "broker.c@karsen-cre.com",
            "responseBody": ("Hi Karsen,\n\nFollowing up on 4200 Karsen Launch "
                             "Blvd — happy to answer any questions.\n\nBest,\nAvery"),
            "clientId": "karsen",
            "attempts": 1,
            "lastError": "Read timed out after Graph reply",
            "lastSendAttemptAt": "2026-07-02T11:00:00Z",
            "conversationId": "conv-C",
            "subject": "4200 Karsen Launch Blvd",
        })
        clean_doc = _FakeDoc("thread-clean", {
            # the one viable reply that SHOULD send, with its OWN anchor
            "threadId": "thread-clean",
            "msgId": "msg-D",
            "recipient": "broker.d@karsen-cre.com",
            "responseBody": CLEAN_VIABLE_BODY,
            "clientId": "karsen",
            "attempts": 1,
            "lastError": "Transient network blip",
            "lastSendAttemptAt": "2026-07-02T11:00:00Z",
            "conversationId": "conv-D",
            "subject": "4200 Karsen Launch Blvd",
        })
        return {
            "A": placeholder_tour_doc,
            "E": tour_only_doc,
            "B": reconcile_doc,
            "C": manual_doc,
            "D": clean_doc,
        }

    def _run_queue(self, docs):
        fake_fs = _FakeFirestore([docs[k] for k in ("A", "E", "B", "C", "D")])

        # Sent Items store: the reconcile conversation already has our exact reply
        # (index-missing already-sent); the manual conversation has a NEWER human
        # send whose body differs from our queued draft (so the strong-identity
        # reconciliation guard does NOT match it, but the continuation guard does).
        sent_items = [
            _sent_message(
                conv="conv-B",
                recipient="broker.b@karsen-cre.com",
                body=docs["B"].to_dict()["responseBody"],
                sent_iso="2026-07-02T12:00:00Z",
                mid="sent-B-1",
                imid="<sent-B-1@karsen-cre.com>",
            ),
            _sent_message(
                conv="conv-C",
                recipient="broker.c@karsen-cre.com",
                body="Quick manual note from the broker's rep — ignore the draft, I've got this.",
                sent_iso="2026-07-02T12:05:00Z",
                mid="manual-C-1",
                imid="<manual-C-1@karsen-cre.com>",
            ),
        ]
        fake_graph = _FakeSentItems(sent_items)
        recorder = _SendRecorder()

        with patch.dict(sys.modules, {
            "email_automation.clients": types.SimpleNamespace(_fs=fake_fs),
        }), \
             patch.object(processing_module, "send_reply_in_thread", new=recorder), \
             patch.object(
                 processing_module,
                 "_maybe_mark_client_completed",
                 return_value=True,
             ) as maybe_mark_completed, \
             patch.object(
                 pending_responses,
                 "get_client_automation_decision",
                 return_value=CampaignAutomationDecision(
                     state="allow",
                     reason="",
                     client_data={"columnConfig": get_default_column_config()},
                     metadata={"terminal": False, "stopKind": "none"},
                 ),
             ), \
             patch("email_automation.sent_mail_guard.requests.get", fake_graph.get), \
             patch("email_automation.sent_mail_guard.exponential_backoff_request",
                   side_effect=lambda fn: fn()):
            sent_count = pending_responses.process_pending_responses(self.UID, self.HEADERS)

        maybe_mark_completed.assert_called_once_with(self.UID, "karsen")
        return fake_fs, recorder, sent_count

    def _dead_letter_for(self, fake_fs, original_doc_id):
        for payload in fake_fs.collections["deadLetterQueue"].add_calls:
            if payload.get("originalDocId") == original_doc_id:
                return payload
        return None

    def _assert_only_send_claim(self, doc, message):
        """A diverted item may be claimed, but it must never be re-queued."""
        self.assertEqual(1, len(doc.reference.update_calls), message)
        claim = doc.reference.update_calls[0]
        self.assertEqual("sending", claim.get("status"), message)
        self.assertRegex(
            str(claim.get("processingBy") or ""),
            r"^pending-response-[0-9a-f]{32}$",
            message,
        )

    def test_only_the_clean_viable_reply_sends_across_the_whole_deck(self):
        docs = self._build_queue()
        fake_fs, recorder, op_states = self._run_queue(docs)

        # --- CORE INTERACTION INVARIANT: exactly ONE real send, and it is the
        # clean/viable doc carrying its OWN anchor (thread/recipient/msg). If a
        # placeholder, tour body, already-sent, or manually-continued sibling had
        # leaked into the send lane -- or the clean send borrowed a neighbor's
        # anchor -- this assertion goes red. (#20: process_pending_responses now
        # returns a Graph op-state list; exactly one HEALTHY send op-state.)
        self.assertEqual(
            1,
            len([
                state
                for state in op_states
                if state.get("status") == "healthy"
                and state.get("operation") == "pending_response_send"
            ]),
            "exactly one clean reply may reach a real send",
        )
        self.assertEqual(
            1,
            len([
                state
                for state in op_states
                if state.get("status") == "healthy"
                and state.get("operation") == "pending_response_completion"
            ]),
            "the clean reply must settle its durable completion obligation",
        )
        self.assertEqual([], [s for s in op_states if s.get("status") == "error"])
        self.assertEqual(1, len(recorder.calls),
                         "Exactly one reply may reach a real Graph send across the deck.")
        call = recorder.calls[0]
        self.assertEqual("thread-clean", call["thread_id"])
        self.assertEqual("broker.d@karsen-cre.com", call["recipient"])
        self.assertEqual("msg-D", call["current_msg_id"])
        self.assertEqual(CLEAN_VIABLE_BODY, call["body"])
        self.assertTrue(docs["D"].reference.deleted, "Sent clean doc must be cleared.")

        # --- mustProve #2 (placeholder blocks BEFORE Graph) + the placeholder side
        # of the "missing-name plus tour wording" variant.
        self.assertTrue(docs["A"].reference.deleted)
        self._assert_only_send_claim(
            docs["A"],
            "Blocked placeholder doc must not be re-queued for another send.",
        )
        dl_a = self._dead_letter_for(fake_fs, "thread-placeholder-tour")
        self.assertIsNotNone(dl_a)
        self.assertIn("Unresolved outbound placeholder", dl_a["failureReason"])
        self.assertIn("manual review", dl_a["failureReason"])

        # --- mustProve #1 (no tour scheduling email leaves the core lane) via the
        # resolved-name tour-wording doc.
        self.assertTrue(docs["E"].reference.deleted)
        self._assert_only_send_claim(
            docs["E"],
            "Blocked tour doc must not be re-queued for another send.",
        )
        dl_e = self._dead_letter_for(fake_fs, "thread-tour-only")
        self.assertIsNotNone(dl_e)
        self.assertIn("scheduling language", dl_e["failureReason"])

        # --- graph_accepted_but_index_missing: reconcile the already-sent reply
        # instead of double-sending.
        self.assertTrue(docs["B"].reference.deleted)
        self._assert_only_send_claim(
            docs["B"],
            "Already-sent doc must not be re-queued after reconciliation.",
        )
        dl_b = self._dead_letter_for(fake_fs, "thread-reconcile")
        self.assertIsNotNone(dl_b)
        self.assertEqual("needs_reconciliation", dl_b["status"])
        self.assertTrue(dl_b["alreadySent"])
        self.assertEqual("sent-B-1", dl_b["sentMessageId"])
        self.assertEqual("conv-B", dl_b["conversationId"])

        # --- mustProve #3 (manual continuation suppresses retry).
        self.assertTrue(docs["C"].reference.deleted)
        self._assert_only_send_claim(
            docs["C"],
            "Manually continued doc must not be re-queued after diversion.",
        )
        dl_c = self._dead_letter_for(fake_fs, "thread-manual")
        self.assertIsNotNone(dl_c)
        self.assertIn("manually continued", dl_c["failureReason"])

        # --- No sibling's anchor was ever handed to the send lane.
        sent_threads = [c["thread_id"] for c in recorder.calls]
        for leaked in ("thread-placeholder-tour", "thread-tour-only",
                       "thread-reconcile", "thread-manual"):
            self.assertNotIn(leaked, sent_threads,
                             f"Diverted doc {leaked} must never reach a real send.")

    def test_negative_control_clean_only_queue_sends_and_no_dead_letter(self):
        """Fail-ability / detection control: with ONLY the clean viable doc in the
        queue (and no Sent Items collisions), the same handler sends exactly once
        and dead-letters nothing -- proving the diversions above are the guards
        firing on the hostile inputs, not an unconditional block.
        """
        docs = self._build_queue()
        fake_fs = _FakeFirestore([docs["D"]])
        fake_graph = _FakeSentItems([])  # empty Sent Items -> no reconcile/continuation
        recorder = _SendRecorder()

        with patch.dict(sys.modules, {
            "email_automation.clients": types.SimpleNamespace(_fs=fake_fs),
        }), \
             patch.object(processing_module, "send_reply_in_thread", new=recorder), \
             patch.object(
                 processing_module,
                 "_maybe_mark_client_completed",
                 return_value=True,
             ) as maybe_mark_completed, \
             patch.object(
                 pending_responses,
                 "get_client_automation_decision",
                 return_value=CampaignAutomationDecision(
                     state="allow",
                     reason="",
                     client_data={"columnConfig": get_default_column_config()},
                     metadata={"terminal": False, "stopKind": "none"},
                 ),
             ), \
             patch("email_automation.sent_mail_guard.requests.get", fake_graph.get), \
             patch("email_automation.sent_mail_guard.exponential_backoff_request",
                   side_effect=lambda fn: fn()):
            op_states = pending_responses.process_pending_responses(self.UID, self.HEADERS)

        maybe_mark_completed.assert_called_once_with(self.UID, "karsen")
        self.assertEqual(
            1,
            len([
                state
                for state in op_states
                if state.get("status") == "healthy"
                and state.get("operation") == "pending_response_send"
            ]),
            "the clean, uncollided reply sends exactly once",
        )
        self.assertEqual(
            1,
            len([
                state
                for state in op_states
                if state.get("status") == "healthy"
                and state.get("operation") == "pending_response_completion"
            ]),
            "the clean reply settles its durable completion obligation",
        )
        self.assertEqual([], [s for s in op_states if s.get("status") == "error"])
        self.assertEqual(1, len(recorder.calls))
        self.assertEqual("thread-clean", recorder.calls[0]["thread_id"])
        self.assertTrue(docs["D"].reference.deleted)
        self.assertEqual([], fake_fs.collections["deadLetterQueue"].add_calls,
                         "A clean, uncollided viable reply must not be dead-lettered.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
