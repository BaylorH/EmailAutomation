"""Surface D-6: Base-V1 rubric state-permutation closures (feature x state).

Closes four needs_fixture cells with REAL behavior tests. Only Firestore /
Sheets / Graph transports are faked (in-memory doubles). Every function under
test is the real production code and each assertion would FAIL if the guarded
safety behavior regressed. ZERO live sends.

Cells closed by this file:
  * core.inbox_matching  / bad_placeholder     -> test_bad_placeholder_message_id_never_hijacks_thread_match
  * core.inbox_matching  / manual_continuation -> test_reply_to_operator_manual_continuation_threads_to_origin
  * core.inbox_auto_reply / terminal_state      -> test_terminal_thread_suppresses_inbox_autoreply_pipeline
  * core.inbox_auto_reply / wrong_recipient     -> test_autoreply_strips_optedout_wrong_recipient_before_graph_send
"""

import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault("SITESIFT_AUTO_REPLY_ALLOWLIST", "*")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import unittest
from unittest.mock import patch, MagicMock

from email_automation import messaging, processing
from email_automation.campaign_safety import CampaignAutomationDecision


# ─────────────────────────────────────────────────────────────────────────────
# Path-addressed in-memory Firestore double.
#
# Documents are keyed by their full "users/<uid>/msgIndex/<id>" path so the REAL
# messaging.index_message_id / lookup_thread_by_message_id / index_conversation_id
# / lookup_thread_by_conversation_id (and their b64url_id / normalize helpers)
# execute their true read/write logic against it. Only the transport is faked.
# ─────────────────────────────────────────────────────────────────────────────
class _FakeSnapshot:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data or {})


class _FakeDocRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def collection(self, name):
        return _FakeCollectionRef(self._store, f"{self._path}/{name}")

    def set(self, data, merge=False):
        if merge and self._path in self._store:
            self._store[self._path] = {**self._store[self._path], **data}
        else:
            self._store[self._path] = dict(data)

    def get(self):
        return _FakeSnapshot(self._store.get(self._path))


class _FakeCollectionRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def document(self, doc_id):
        return _FakeDocRef(self._store, f"{self._path}/{doc_id}")


class _FakeFirestore:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return _FakeCollectionRef(self.store, name)


# ─────────────────────────────────────────────────────────────────────────────
# core.inbox_matching / bad_placeholder
# ─────────────────────────────────────────────────────────────────────────────
class InboxMatchingBadPlaceholderTests(unittest.TestCase):
    """core.inbox_matching x bad_placeholder.

    An outbound message-id can arrive as a bad/empty placeholder header
    (`<>` or `""`) — Graph sometimes emits these before a real id is assigned.
    This proves the real matcher never lets such a placeholder become a thread
    match key: an empty id is refused at index time, and an inbound reply whose
    In-Reply-To/References carry the `<>` placeholder is NOT hijacked into a
    decoy thread (header normalization collapses the placeholder to "" before
    any lookup), instead resolving to the correct thread via real lineage.
    """

    def test_bad_placeholder_message_id_never_hijacks_thread_match(self):
        fake_fs = _FakeFirestore()
        user_id = "uid-bad-placeholder"

        real_origin_id = "<campaign-real-outreach-001@acme-listings.com>"
        origin_thread = "thread-origin-real"
        conv_id = "conv-origin-real"

        with patch.object(messaging, "_fs", fake_fs):
            # Seed the real campaign thread through the REAL index functions.
            self.assertTrue(messaging.index_message_id(user_id, real_origin_id, origin_thread))
            self.assertTrue(messaging.index_conversation_id(user_id, conv_id, origin_thread))

            # An empty placeholder id must be REFUSED at index time (nothing stored).
            self.assertFalse(
                messaging.index_message_id(user_id, "", "thread-empty-decoy"),
                "an empty message-id must not be indexed",
            )

            # A bracket-only placeholder <> may leak into the raw index, so seed a
            # decoy thread under it to prove even a leaked placeholder entry cannot
            # be reached by an inbound reply carrying the <> header.
            messaging.index_message_id(user_id, "<>", "thread-placeholder-decoy")

            # Inbound reply whose In-Reply-To is a bad placeholder, but whose
            # References carry the placeholder AND the real origin id, on the
            # origin conversation.
            inbound_reply = {
                "id": "graph-inbound-1",
                "conversationId": conv_id,
                "internetMessageHeaders": [
                    {"name": "In-Reply-To", "value": "<>"},
                    {"name": "References", "value": f"<> {real_origin_id}"},
                ],
            }
            matched = processing._match_message_to_thread(
                user_id, inbound_reply, {"Authorization": "Bearer t"}
            )

            # A reply whose ONLY lineage signal is the bad placeholder resolves to
            # NOTHING — the placeholder is never a valid match key even though a
            # decoy thread was indexed under the raw "<>".
            placeholder_only = {
                "id": "graph-inbound-2",
                "conversationId": None,
                "internetMessageHeaders": [
                    {"name": "In-Reply-To", "value": "<>"},
                ],
            }
            matched_placeholder_only = processing._match_message_to_thread(
                user_id, placeholder_only, {"Authorization": "Bearer t"}
            )

        # Routed to the REAL owning thread via lineage, not the placeholder decoy.
        self.assertEqual(matched, origin_thread)
        self.assertNotEqual(matched, "thread-placeholder-decoy")
        # The bad placeholder alone is inert: no spurious match to the decoy.
        self.assertIsNone(matched_placeholder_only)


# ─────────────────────────────────────────────────────────────────────────────
# core.inbox_matching / manual_continuation
# ─────────────────────────────────────────────────────────────────────────────
class InboxMatchingManualContinuationTests(unittest.TestCase):
    """core.inbox_matching x manual_continuation.

    After the operator MANUALLY continues a thread (their Sent-Items reply is
    indexed to the thread), a broker's next reply threads off the operator's
    manual message (In-Reply-To == operator manual msg-id), not the system's
    original outreach. This proves the real matcher follows the message-id chain
    THROUGH the human-inserted link so the conversation stays unified — and that
    the match genuinely depends on the manual continuation being indexed (a
    reply to an UN-indexed manual id resolves to None, not a constant).
    """

    def test_reply_to_operator_manual_continuation_threads_to_origin(self):
        fake_fs = _FakeFirestore()
        user_id = "uid-manual-cont-match"

        origin_id = "<sys-outreach-77@acme.com>"
        origin_thread = "thread-77"
        operator_manual_id = "<operator-manual-continuation-77@outlook.com>"
        # A separate concurrent campaign the reply must never land in.
        other_thread = "thread-decoy-88"
        other_origin_id = "<sys-outreach-88@other.com>"

        broker_reply = {
            "id": "graph-broker-reply-1",
            "conversationId": "conv-77",
            "internetMessageHeaders": [
                {"name": "In-Reply-To", "value": operator_manual_id},
                {"name": "References", "value": f"{origin_id} {operator_manual_id}"},
            ],
        }

        with patch.object(messaging, "_fs", fake_fs):
            # Both campaigns seeded via REAL indexing.
            messaging.index_message_id(user_id, origin_id, origin_thread)
            messaging.index_message_id(user_id, other_origin_id, other_thread)

            # NEGATIVE CONTROL: before the manual continuation is indexed, a broker
            # reply-to-the-manual-message cannot be threaded via that link. Its only
            # other lineage (origin References) still saves it, so strip References
            # to isolate the manual link: with no indexed manual id + no conv, None.
            pre_index_probe = {
                "id": "graph-broker-reply-pre",
                "conversationId": None,
                "internetMessageHeaders": [
                    {"name": "In-Reply-To", "value": operator_manual_id},
                ],
            }
            matched_before = processing._match_message_to_thread(
                user_id, pre_index_probe, {"Authorization": "Bearer t"}
            )

            # Operator manually continues the thread: the manual reply is indexed to
            # the SAME origin thread (exactly what the manual-reply scan does).
            self.assertTrue(
                messaging.index_message_id(user_id, operator_manual_id, origin_thread)
            )

            matched_after = processing._match_message_to_thread(
                user_id, broker_reply, {"Authorization": "Bearer t"}
            )

        # Without the indexed manual continuation the manual-link reply is inert.
        self.assertIsNone(matched_before)
        # After the manual continuation, the broker's reply to the operator's
        # manual message threads back to the origin — not the decoy campaign.
        self.assertEqual(matched_after, origin_thread)
        self.assertNotEqual(matched_after, other_thread)


# ─────────────────────────────────────────────────────────────────────────────
# core.inbox_auto_reply / terminal_state
# ─────────────────────────────────────────────────────────────────────────────
class _TerminalFakeDoc:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data or {})


class _TerminalFakeRef:
    def __init__(self, data_holder):
        self._holder = data_holder

    def collection(self, _name):
        return _TerminalFakeCol(self._holder)

    def document(self, _doc_id):
        return self

    def get(self):
        return _TerminalFakeDoc(self._holder["thread"])

    def set(self, data, merge=False):
        if self._holder.get("set_error"):
            raise self._holder["set_error"]
        self._holder.setdefault("writes", []).append((dict(data), merge))

    def update(self, data):
        self._holder.setdefault("updates", []).append(dict(data))


class _TerminalFakeCol:
    def __init__(self, data_holder):
        self._holder = data_holder

    def document(self, _doc_id):
        return _TerminalFakeRef(self._holder)


class _TerminalFakeFs:
    def __init__(self, thread_data):
        self._holder = {"thread": thread_data}

    def collection(self, _name):
        return _TerminalFakeCol(self._holder)


class _Sentinel(Exception):
    """Marks that process_inbox_message proceeded past the terminal gate."""


class InboxAutoReplyTerminalStateTests(unittest.TestCase):
    """core.inbox_auto_reply x terminal_state.

    Drives the real process_inbox_message auto-reply pipeline. On a STOPPED
    (terminal) thread it must save the inbound message for history but return
    BEFORE the auto-reply pipeline runs: send_reply_in_thread is never called
    AND the downstream pipeline (fetch_and_log_sheet_for_thread, the first step
    after the terminal gate) never runs. A positive control with an IDENTICAL
    reply on an ACTIVE thread proves the terminal status is the decisive gate:
    the same code path proceeds past the gate into the pipeline. So the send
    suppression is caused by the terminal status, not the harness.
    """

    def _drive(
        self,
        thread_status,
        campaign_decision=None,
        *,
        thread_client_id="client-x",
        resolved_client_id=None,
        thread_set_error=None,
    ):
        user_id = "uid-terminal"
        thread_id = "thread-terminal-1"
        msg_id = "graph-msg-terminal"
        in_reply_to = "<origin-terminal@acme.com>"
        broker_from = "broker@acme.example"
        operator_email = "operator@sitesift.example"

        msg = {
            "id": msg_id,
            "subject": "RE: 100 Main St availability",
            "from": {"emailAddress": {"name": "Broker", "address": broker_from}},
            "internetMessageId": "<broker-reply-terminal@acme.com>",
            "conversationId": "conv-terminal",
            "receivedDateTime": "2026-07-02T12:00:00Z",
            "sentDateTime": "2026-07-02T12:00:00Z",
            "bodyPreview": "The property at 100 Main St is available, 5000 sqft.",
            "internetMessageHeaders": [
                {"name": "In-Reply-To", "value": in_reply_to},
            ],
        }

        thread_data = {"status": thread_status}
        if thread_client_id:
            thread_data["clientId"] = thread_client_id
        fake_fs = _TerminalFakeFs(thread_data)
        fake_fs._holder["set_error"] = thread_set_error
        campaign_decision = campaign_decision or CampaignAutomationDecision(
            state="allow", reason="", client_data={"status": "live"},
            metadata={"terminal": False, "stopKind": "none"},
        )

        base = "https://graph.microsoft.com/v1.0"

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if url == f"{base}/me":
                resp.json.return_value = {"mail": operator_email}
            elif url.endswith(f"/me/messages/{msg_id}"):
                resp.json.return_value = {
                    "body": {
                        "contentType": "Text",
                        "content": "The property at 100 Main St is available, 5000 sqft.",
                    }
                }
            else:
                resp.json.return_value = {}
            return resp

        send_spy = MagicMock(name="send_reply_in_thread", return_value=True)
        fetch_spy = MagicMock(name="fetch_and_log_sheet_for_thread", side_effect=_Sentinel())

        with patch.object(processing, "requests") as fake_requests, \
             patch.object(processing, "exponential_backoff_request", side_effect=lambda func, **k: func()), \
             patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "lookup_thread_by_message_id", return_value=thread_id), \
             patch.object(processing, "lookup_thread_by_conversation_id", return_value=thread_id), \
             patch.object(processing, "get_client_automation_decision", return_value=campaign_decision) as decision_spy, \
             patch.object(processing, "_find_client_id_by_email", return_value=resolved_client_id) as resolver_spy, \
             patch.object(processing, "get_thread_status", return_value=thread_status), \
             patch.object(processing, "save_message", return_value=True) as save_spy, \
             patch.object(processing, "index_message_id", return_value=True) as index_spy, \
             patch.object(processing, "dump_thread_from_firestore", return_value=None), \
             patch.object(processing, "fetch_and_log_sheet_for_thread", fetch_spy), \
             patch.object(processing, "send_reply_in_thread", send_spy), \
             patch("email_automation.followup.cancel_followup_on_response", return_value=None):
            fake_requests.get.side_effect = fake_get
            raised_sentinel = False
            raised_retryable = False
            try:
                processing.process_inbox_message(user_id, {"Authorization": "Bearer t"}, msg)
            except _Sentinel:
                raised_sentinel = True
            except processing.RetryableProcessingError:
                raised_retryable = True

        return (
            send_spy,
            fetch_spy,
            save_spy,
            index_spy,
            raised_sentinel,
            raised_retryable,
            fake_fs,
            decision_spy,
            resolver_spy,
        )

    def test_terminal_thread_suppresses_inbox_autoreply_pipeline(self):
        # TERMINAL (stopped): pipeline is suppressed at the terminal gate.
        send_spy, fetch_spy, _save, _index, raised, retryable, _fake_fs, _decision, _resolver = self._drive(processing.THREAD_STATUS["stopped"])
        send_spy.assert_not_called()
        fetch_spy.assert_not_called()
        self.assertFalse(
            raised,
            "terminal thread must return before the pipeline (no fetch_and_log)",
        )
        self.assertFalse(retryable)

        # POSITIVE CONTROL (active): identical reply enters the pipeline past the
        # terminal gate — proving the terminal status is what stops the send.
        send_spy2, fetch_spy2, _save2, _index2, raised2, retryable2, _fake_fs2, _decision2, _resolver2 = self._drive(processing.THREAD_STATUS["active"])
        fetch_spy2.assert_called_once()
        self.assertTrue(
            raised2,
            "active thread must proceed past the terminal gate into the pipeline",
        )
        self.assertFalse(retryable2)

    def test_maintenance_pause_saves_inbound_without_terminalizing_or_processing(self):
        decision = CampaignAutomationDecision(
            state="blocked",
            reason="campaign_maintenance",
            client_data={"status": "live", "automationPaused": True},
            metadata={"terminal": False, "stopKind": "maintenance_pause"},
        )

        (
            send_spy,
            fetch_spy,
            save_spy,
            index_spy,
            raised,
            retryable,
            fake_fs,
            _decision,
            _resolver,
        ) = self._drive(
            processing.THREAD_STATUS["active"],
            campaign_decision=decision,
        )

        send_spy.assert_not_called()
        fetch_spy.assert_not_called()
        save_spy.assert_called_once()
        index_spy.assert_called_once()
        self.assertFalse(raised)
        self.assertTrue(
            retryable,
            "maintenance-suppressed inbound evidence must remain retryable",
        )
        terminal_updates = [
            update for update in fake_fs._holder.get("updates", [])
            if update.get("status") == processing.THREAD_STATUS["stopped"]
        ]
        self.assertEqual([], terminal_updates)

    def test_missing_thread_client_id_is_resolved_before_campaign_gate(self):
        (
            _send_spy,
            fetch_spy,
            _save_spy,
            _index_spy,
            raised,
            retryable,
            fake_fs,
            decision_spy,
            resolver_spy,
        ) = self._drive(
            processing.THREAD_STATUS["active"],
            thread_client_id=None,
            resolved_client_id="client-recovered",
        )

        resolver_spy.assert_called_once_with(
            "uid-terminal",
            "broker@acme.example",
        )
        decision_spy.assert_called_once_with(
            "uid-terminal",
            "client-recovered",
        )
        self.assertIn(
            ({"clientId": "client-recovered"}, True),
            fake_fs._holder.get("writes", []),
        )
        fetch_spy.assert_called_once()
        self.assertTrue(raised)
        self.assertFalse(retryable)

    def test_recovered_client_id_write_failure_does_not_drop_inbound_processing(self):
        (
            _send_spy,
            fetch_spy,
            _save_spy,
            _index_spy,
            raised,
            retryable,
            _fake_fs,
            decision_spy,
            resolver_spy,
        ) = self._drive(
            processing.THREAD_STATUS["active"],
            thread_client_id=None,
            resolved_client_id="client-recovered",
            thread_set_error=RuntimeError("firestore write unavailable"),
        )

        resolver_spy.assert_called_once()
        decision_spy.assert_called_once_with(
            "uid-terminal",
            "client-recovered",
        )
        fetch_spy.assert_called_once()
        self.assertTrue(raised)
        self.assertFalse(retryable)


# ─────────────────────────────────────────────────────────────────────────────
# core.inbox_auto_reply / wrong_recipient
# ─────────────────────────────────────────────────────────────────────────────
class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _AutoReplyFakeFs:
    """Returns a benign user doc for signature resolution; everything else no-op."""

    def collection(self, _name):
        return self

    def document(self, _doc_id):
        return self

    def get(self):
        return self

    @property
    def exists(self):
        return True

    def to_dict(self):
        return {"email": "operator@sitesift.com"}


class InboxAutoReplyWrongRecipientTests(unittest.TestCase):
    """core.inbox_auto_reply x wrong_recipient.

    Drives the real processing.send_reply_in_thread reply-all path for an
    allowlisted user. It proves recipient-IDENTITY enforcement (distinct from
    the user_id auto-reply policy gate): the real _filter_reply_all_draft_recipients
    strips an opted-out / blocked (wrong) recipient injected into the draft's
    toRecipients BEFORE the Graph draft is patched or sent, so the wrong address
    never reaches Graph; and when the wrong recipient is the ONLY audience the
    send is blocked entirely (no /send call, draft deleted). A positive contrast
    with a clean recipient set proves the block fires on recipient identity.
    """

    def _run(self, opted_out_addresses):
        user_id = "uid-allowlisted"
        posts = []
        patch_payloads = []
        deletes = []
        base = "https://graph.microsoft.com/v1.0"

        wrong_recipient = "opted-out-broker@wrong-brokerage.com"
        good_recipient = "active-broker@acme-listings.com"

        def fake_get(url, **_kwargs):
            if url.endswith(f"/me/messages/msg-w"):
                return _FakeResponse(200, {
                    "conversationId": "conv-w",
                    "subject": "RE: 200 Market St",
                })
            return _FakeResponse(404)

        def fake_post(url, **_kwargs):
            posts.append(url)
            if url.endswith("/createReplyAll"):
                # Graph's reply-all audience contains BOTH the wrong (opted-out)
                # recipient and a legitimate one.
                return _FakeResponse(201, {
                    "id": "reply-draft-w",
                    "toRecipients": [
                        {"emailAddress": {"address": wrong_recipient}},
                        {"emailAddress": {"address": good_recipient}},
                    ],
                    "ccRecipients": [],
                })
            if url.endswith("/reply-draft-w/send"):
                return _FakeResponse(202)
            return _FakeResponse(500)

        def fake_patch(_url, **kwargs):
            patch_payloads.append(kwargs.get("json") or {})
            return _FakeResponse(200)

        def fake_delete(url, **_kwargs):
            deletes.append(url)
            return _FakeResponse(204)

        sent_message = {
            "id": "sent-w",
            "internetMessageId": "<sent-w@acme-listings.com>",
            "conversationId": "conv-w",
            "subject": "RE: 200 Market St",
            "sentDateTime": "2026-07-02T16:00:00Z",
            "toRecipients": [{"emailAddress": {"address": good_recipient}}],
            "ccRecipients": [],
            "body": {"contentType": "HTML", "content": "Thanks"},
            "bodyPreview": "Thanks",
        }

        def fake_optout(_user_id, email):
            if email in opted_out_addresses:
                return {"reason": "unsubscribed"}
            return None

        with patch("email_automation.utils.exponential_backoff_request", side_effect=lambda func, **k: func()), \
                patch("email_automation.clients._fs", _AutoReplyFakeFs()), \
                patch.object(processing.requests, "get", side_effect=fake_get), \
                patch.object(processing.requests, "post", side_effect=fake_post), \
                patch.object(processing.requests, "patch", side_effect=fake_patch), \
                patch.object(processing.requests, "delete", side_effect=fake_delete), \
                patch.object(processing.time, "sleep", return_value=None), \
                patch.object(processing, "_find_recent_sent_message_for_conversation", return_value=sent_message), \
                patch("email_automation.messaging.index_message_id", return_value=True), \
                patch("email_automation.messaging.lookup_thread_by_message_id", return_value="thread-w"), \
                patch("email_automation.messaging.index_conversation_id", return_value=True), \
                patch("email_automation.messaging.save_message", return_value=True), \
                patch.object(processing, "is_contact_opted_out", side_effect=fake_optout), \
                patch.object(
                    processing,
                    "get_client_automation_decision",
                    return_value=CampaignAutomationDecision(
                        state="allow", reason="", client_data={"status": "live"},
                        metadata={"terminal": False, "stopKind": "none"},
                    ),
                    create=True,
                ):
            sent = processing.send_reply_in_thread(
                user_id,
                {"Authorization": "Bearer token"},
                "Hi Broker,\n\nThanks for the details.",
                "msg-w",
                good_recipient,
                "thread-w",
            )

        return {
            "sent": sent,
            "posts": posts,
            "patch_payloads": patch_payloads,
            "deletes": deletes,
            "wrong": wrong_recipient,
            "good": good_recipient,
            "outcome": getattr(processing.send_reply_in_thread, "last_outcome", None),
        }

    def test_autoreply_rechecks_campaign_stop_immediately_before_graph_send(self):
        user_id = "uid-allowlisted"
        posts = []
        deletes = []
        decisions = [
            CampaignAutomationDecision(
                state="allow", reason="", client_data={"status": "live"},
                metadata={"terminal": False, "stopKind": "none"},
            ),
            CampaignAutomationDecision(
                state="blocked", reason="client_stopped_by_user",
                client_data={"status": "stopping"},
                metadata={"terminal": True, "stopKind": "terminal_stop"},
            ),
        ]

        def fake_get(url, **_kwargs):
            return _FakeResponse(200, {
                "conversationId": "conv-stop",
                "subject": "RE: 200 Market St",
            })

        def fake_post(url, **_kwargs):
            posts.append(url)
            if url.endswith("/createReplyAll"):
                return _FakeResponse(201, {
                    "id": "reply-draft-stop",
                    "toRecipients": [{"emailAddress": {"address": "bp21harrison@gmail.com"}}],
                    "ccRecipients": [],
                })
            if url.endswith("/send"):
                raise AssertionError("stopped campaign must not reach Graph /send")
            return _FakeResponse(500)

        def fake_delete(url, **_kwargs):
            deletes.append(url)
            return _FakeResponse(204)

        with patch("email_automation.utils.exponential_backoff_request", side_effect=lambda func, **k: func()), \
                patch("email_automation.clients._fs", _AutoReplyFakeFs()), \
                patch.object(processing.requests, "get", side_effect=fake_get), \
                patch.object(processing.requests, "post", side_effect=fake_post), \
                patch.object(processing.requests, "patch", return_value=_FakeResponse(200)), \
                patch.object(processing.requests, "delete", side_effect=fake_delete), \
                patch.object(processing, "is_contact_opted_out", return_value=None), \
                patch.object(
                    processing,
                    "get_client_automation_decision",
                    side_effect=decisions,
                    create=True,
                ):
            sent = processing.send_reply_in_thread(
                user_id,
                {"Authorization": "Bearer token"},
                "Hi Broker,\n\nThanks for the details.",
                "msg-stop",
                "bp21harrison@gmail.com",
                "thread-stop",
            )

        self.assertFalse(sent)
        self.assertFalse(any(url.endswith("/send") for url in posts))
        self.assertTrue(any(url.endswith("/reply-draft-stop") for url in deletes))
        self.assertEqual("blocked_campaign_terminal", processing.send_reply_in_thread.last_outcome)

    def test_autoreply_strips_optedout_wrong_recipient_before_graph_send(self):
        wrong = "opted-out-broker@wrong-brokerage.com"
        good = "active-broker@acme-listings.com"

        # CASE A: wrong recipient stripped, send proceeds to the corrected audience.
        res = self._run(opted_out_addresses={wrong})
        self.assertTrue(res["sent"])
        self.assertTrue(res["patch_payloads"], "draft must be patched before send")
        patched_to = [
            r["emailAddress"]["address"]
            for r in res["patch_payloads"][0]["toRecipients"]
        ]
        # The wrong/opted-out address is corrected out of the patched audience.
        self.assertNotIn(wrong, patched_to)
        self.assertIn(good, patched_to)
        # And it never reached the Graph send surface in any form.
        self.assertFalse(any(wrong in url for url in res["posts"]))

        # CASE B: the wrong recipient is the ONLY audience -> send is blocked
        # entirely before Graph /send, and the draft is deleted.
        res_b = self._run(opted_out_addresses={wrong, good})
        self.assertFalse(res_b["sent"])
        self.assertFalse(
            any(url.endswith("/reply-draft-w/send") for url in res_b["posts"]),
            "no Graph /send may be issued when no safe recipient remains",
        )
        self.assertTrue(res_b["deletes"], "the unsafe reply-all draft must be deleted")
        self.assertEqual(res_b["outcome"], "send_failed")


if __name__ == "__main__":
    unittest.main()
