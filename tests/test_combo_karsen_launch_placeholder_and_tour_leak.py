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
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import pending_responses
from email_automation import processing as processing_module
from email_automation.outbound_safety import validate_outbound_body
from email_automation.ai_processing import _augment_events_with_deterministic_signals
from email_automation.campaign_safety import CampaignAutomationDecision
from email_automation.column_config import get_default_column_config


# ---------------------------------------------------------------------------
# Firestore boundary fake (pendingResponses read + deadLetterQueue writes).
# ---------------------------------------------------------------------------
class _FakeDocRef:
    def __init__(self):
        self.deleted = False
        self.update_calls = []

    def delete(self):
        self.deleted = True

    def update(self, data):
        self.update_calls.append(data)


class _FakeDoc:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data
        self.reference = _FakeDocRef()

    def to_dict(self):
        return dict(self._data)


class _FakeCollection:
    def __init__(self, docs=None, client_status=None):
        self.docs = docs or []
        self.add_calls = []
        self.client_status = client_status

    def stream(self):
        return list(self.docs)

    def add(self, data):
        self.add_calls.append(data)
        return _FakeDocRef()

    def document(self, _doc_id):
        status = self.client_status
        return types.SimpleNamespace(
            get=lambda: types.SimpleNamespace(
                exists=status is not None,
                to_dict=lambda: {"status": status} if status is not None else None,
            )
        )


class _FakeFirestore:
    def __init__(self, pending_docs):
        self.collections = {
            "pendingResponses": _FakeCollection(pending_docs),
            "deadLetterQueue": _FakeCollection(),
        }

    def document(self, _name):
        return self

    def collection(self, name):
        if name == "users":
            return self
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

        return fake_fs, recorder, sent_count

    def _dead_letter_for(self, fake_fs, original_doc_id):
        for payload in fake_fs.collections["deadLetterQueue"].add_calls:
            if payload.get("originalDocId") == original_doc_id:
                return payload
        return None

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
            1, len([s for s in op_states if s.get("status") == "healthy"]),
            "exactly one clean reply may reach a real send",
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
        self.assertEqual([], docs["A"].reference.update_calls,
                         "Blocked placeholder doc must not be re-queued for another send.")
        dl_a = self._dead_letter_for(fake_fs, "thread-placeholder-tour")
        self.assertIsNotNone(dl_a)
        self.assertIn("Unresolved outbound placeholder", dl_a["failureReason"])
        self.assertIn("manual review", dl_a["failureReason"])

        # --- mustProve #1 (no tour scheduling email leaves the core lane) via the
        # resolved-name tour-wording doc.
        self.assertTrue(docs["E"].reference.deleted)
        self.assertEqual([], docs["E"].reference.update_calls)
        dl_e = self._dead_letter_for(fake_fs, "thread-tour-only")
        self.assertIsNotNone(dl_e)
        self.assertIn("scheduling language", dl_e["failureReason"])

        # --- graph_accepted_but_index_missing: reconcile the already-sent reply
        # instead of double-sending.
        self.assertTrue(docs["B"].reference.deleted)
        self.assertEqual([], docs["B"].reference.update_calls)
        dl_b = self._dead_letter_for(fake_fs, "thread-reconcile")
        self.assertIsNotNone(dl_b)
        self.assertEqual("needs_reconciliation", dl_b["status"])
        self.assertTrue(dl_b["alreadySent"])
        self.assertEqual("sent-B-1", dl_b["sentMessageId"])
        self.assertEqual("conv-B", dl_b["conversationId"])

        # --- mustProve #3 (manual continuation suppresses retry).
        self.assertTrue(docs["C"].reference.deleted)
        self.assertEqual([], docs["C"].reference.update_calls)
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

        self.assertEqual(
            1, len([s for s in op_states if s.get("status") == "healthy"]),
            "the clean, uncollided reply sends exactly once",
        )
        self.assertEqual([], [s for s in op_states if s.get("status") == "error"])
        self.assertEqual(1, len(recorder.calls))
        self.assertEqual("thread-clean", recorder.calls[0]["thread_id"])
        self.assertTrue(docs["D"].reference.deleted)
        self.assertEqual([], fake_fs.collections["deadLetterQueue"].add_calls,
                         "A clean, uncollided viable reply must not be dead-lettered.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
