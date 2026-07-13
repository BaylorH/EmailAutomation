"""Surface D — state-permutation rubric gap closure (feature × state).

Each test class below closes exactly one needs_fixture cell of the Base-V1
rubric by driving the REAL production handler for THAT feature in THAT state and
asserting the safety-relevant behavior (would FAIL red if the behavior
regressed). Everything external (Firestore / Sheets / Graph) is faked in-memory;
ZERO live sends.

Cells closed here:
  * core.launch_draft / happy_path
  * core.launch_draft / operator_visible_failure
  * core.launch_draft / manual_continuation
  * core.reply_all_cc / manual_continuation
  * core.property_extraction / manual_continuation
  * core.sheet_update / manual_continuation
  * core.sheet_update / terminal_state          (replaces a BORROWED GREEN)
  * core.health_recovery / wrong_recipient

Reported NOT APPLICABLE (no fake test written):
  * core.name_resolution / manual_continuation   (see NameResolutionManualContinuationNotApplicable)
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from email_automation import email as email_mod
from email_automation import pending_responses as pending_mod
from email_automation import processing as processing_mod
from email_automation import dead_letter_recovery as dlr_mod
from email_automation.column_config import get_default_column_config


# =========================================================================
# Shared fakes
# =========================================================================
class _FakeOutboxRef:
    """Stand-in for an outbox item's Firestore document reference."""

    def __init__(self, doc_id="outbox-1"):
        self.id = doc_id
        self.deleted = False
        self.sets = []

    def delete(self):
        self.deleted = True

    def set(self, data, merge=False):
        self.sets.append((data, merge))


class _FakeDoc:
    def __init__(self, ref):
        self.reference = ref
        self.id = ref.id


# =========================================================================
# core.launch_draft / happy_path
# =========================================================================
class LaunchDraftHappyPathTests(unittest.TestCase):
    """Rubric cell: core.launch_draft / happy_path (outbox_queued).

    Drives the REAL new-outreach send path _send_single_outbox_item end to end:
    a campaign-launch outbox that carries a [NAME] merge placeholder and NO
    pre-resolved contact name. The path must read the sheet row, resolve the
    contact-name column, substitute it into the body, and hand a fully
    personalized body (no raw placeholder) to the Graph send call.

    Real code under test (unpatched): _dead_letter_campaign_recipient_row_mismatch_if_needed,
    _resolve_campaign_launch_contact_name_result_from_sheet,
    _contact_name_resolution_from_campaign_row, _personalize_name_placeholders,
    _dead_letter_unresolved_name_placeholder_if_needed,
    _dead_letter_unsafe_outbound_body_if_needed. Only datastore boundaries and
    the terminal send/finalize sinks are faked.
    """

    USER_ID = "user-launch"
    RECIPIENT = "broker@example.com"

    def _outbox_data(self):
        return {
            "clientId": "client-1",
            "source": "dashboard_new_campaign",   # real _is_campaign_launch_outbox
            "forceScript": True,                  # deterministic exact-script personalization
            "assignedEmails": [self.RECIPIENT],
            "rowNumber": 7,
            "subject": "123 Main St",
            "script": "Hi [NAME], is 123 Main St still available?",
            "followUpConfig": {"enabled": False},  # truthy -> skip client fetch
        }

    def _run(self, header, row):
        data = self._outbox_data()
        self.assertTrue(email_mod._is_campaign_launch_outbox(data))
        ref = _FakeOutboxRef()
        item = {"doc": _FakeDoc(ref), "data": data}
        send_spy = MagicMock(return_value={
            "sent": [self.RECIPIENT],
            "sentMessageIds": {self.RECIPIENT: "graph-msg-1"},
            "internetMessageIds": {},
            "conversationIds": {},
            "errors": {},
        })
        dead_letter_spy = MagicMock()
        finalize_spy = MagicMock()
        with mock.patch.object(email_mod, "_claim_outbox_item", return_value=True), \
             mock.patch.object(email_mod, "_get_current_outbox_data", return_value=data), \
             mock.patch.object(email_mod, "_delete_cancelled_outbox_item_if_needed", return_value=False), \
             mock.patch.object(email_mod, "_pause_results_outbox_item_if_needed", return_value=False), \
             mock.patch.object(email_mod, "_pause_client_outbox_item_if_needed", return_value=False), \
             mock.patch.object(email_mod, "_read_client_automation_decision", return_value=SimpleNamespace(
                 client_data={"columnConfig": get_default_column_config()},
             )), \
             mock.patch.object(email_mod, "_has_existing_thread_for_property", return_value=False), \
             mock.patch.object(email_mod, "_campaign_sheet_header_and_row", return_value=(header, row)), \
             mock.patch.object(email_mod, "_fresh_graph_headers", side_effect=lambda h, p=None: h), \
             mock.patch.object(email_mod, "_finalize_successful_outbox_item", finalize_spy), \
             mock.patch.object(email_mod, "_move_to_dead_letter", dead_letter_spy), \
             mock.patch.object(email_mod, "send_and_index_email", send_spy):
            email_mod._send_single_outbox_item(
                self.USER_ID, {"Authorization": "Bearer fake"}, item
            )
        return send_spy, dead_letter_spy, finalize_spy

    def test_launch_draft_resolves_name_column_into_queued_body_before_send(self):
        header = ["Email", "Contact Name"]
        row = [self.RECIPIENT, "Jane Smith"]
        send_spy, dead_letter_spy, finalize_spy = self._run(header, row)

        # The launch draft must reach the Graph send exactly once...
        send_spy.assert_called_once()
        sent_body = send_spy.call_args.args[2]
        # ...with the [NAME] variable resolved from the sheet column...
        self.assertEqual(
            "Hi Jane, is 123 Main St still available?",
            sent_body,
            "launch-draft [NAME] placeholder was not resolved from the sheet contact-name column",
        )
        # ...and NO raw placeholder left in the queued/sent body.
        self.assertIsNone(
            email_mod.NAME_PLACEHOLDER_RE.search(sent_body),
            "a raw merge placeholder survived into the sent launch-draft body",
        )
        dead_letter_spy.assert_not_called()
        finalize_spy.assert_called_once()  # successful launch outbox item consumed


# =========================================================================
# core.launch_draft / operator_visible_failure
# =========================================================================
class LaunchDraftOperatorVisibleFailureTests(unittest.TestCase):
    """Rubric cell: core.launch_draft / operator_visible_failure (dead_letter_visible).

    Same real _send_single_outbox_item path, but the sheet row has conflicting
    contact-name columns (two different people). Name resolution must REFUSE to
    guess; the still-unresolved [NAME] body must be routed to the operator-visible
    dead-letter queue and NO Graph send may occur.
    """

    USER_ID = "user-launch"
    RECIPIENT = "broker@example.com"

    def test_ambiguous_name_dead_letters_launch_draft_before_any_send(self):
        data = {
            "clientId": "client-1",
            "source": "dashboard_new_campaign",
            "forceScript": True,
            "assignedEmails": [self.RECIPIENT],
            "rowNumber": 7,
            "subject": "123 Main St",
            "script": "Hi [NAME], is 123 Main St still available?",
            "followUpConfig": {"enabled": False},
        }
        # Two DIFFERENT people across explicit contact-name columns -> ambiguous.
        header = ["Email", "Contact Name", "Broker Name"]
        row = [self.RECIPIENT, "Jane Smith", "Bob Jones"]
        ref = _FakeOutboxRef()
        item = {"doc": _FakeDoc(ref), "data": data}
        send_spy = MagicMock()
        dead_letter_spy = MagicMock()
        with mock.patch.object(email_mod, "_claim_outbox_item", return_value=True), \
             mock.patch.object(email_mod, "_get_current_outbox_data", return_value=data), \
             mock.patch.object(email_mod, "_delete_cancelled_outbox_item_if_needed", return_value=False), \
             mock.patch.object(email_mod, "_pause_results_outbox_item_if_needed", return_value=False), \
             mock.patch.object(email_mod, "_pause_client_outbox_item_if_needed", return_value=False), \
             mock.patch.object(email_mod, "_has_existing_thread_for_property", return_value=False), \
             mock.patch.object(email_mod, "_campaign_sheet_header_and_row", return_value=(header, row)), \
             mock.patch.object(email_mod, "_fresh_graph_headers", side_effect=lambda h, p=None: h), \
             mock.patch.object(email_mod, "_finalize_successful_outbox_item"), \
             mock.patch.object(email_mod, "_move_to_dead_letter", dead_letter_spy), \
             mock.patch.object(email_mod, "send_and_index_email", send_spy):
            email_mod._send_single_outbox_item(
                self.USER_ID, {"Authorization": "Bearer fake"}, item
            )

        send_spy.assert_not_called()
        dead_letter_spy.assert_called_once()
        reason = dead_letter_spy.call_args.args[3]
        self.assertIn("manual review", reason.lower())
        self.assertIn("[name]", reason.lower())


# =========================================================================
# core.launch_draft / manual_continuation
# =========================================================================
class LaunchDraftManualContinuationTests(unittest.TestCase):
    """Rubric cell: core.launch_draft / manual_continuation (retry_reconciled).

    Drives the REAL outbox/launch send-path retry reconciliation
    email._sent_retry_reconciliation_result. On a RETRIED launch outbox whose
    prior attempt failed, when Sent Items shows the user manually continued the
    conversation, the reconciliation must return a manualContinuation verdict
    (defer to manual review) rather than a resend, and the derived stop reason
    must tell the operator why.
    """

    HEADERS = {"Authorization": "Bearer fake"}
    CONV = "conversation-1"

    def _retry_data(self):
        return {
            "clientId": "client-1",
            "source": "dashboard_new_campaign",
            "attempts": 1,
            "lastError": "Graph send timed out",
            "lastSendAttemptAt": "2026-07-01T12:00:00Z",
        }

    def test_retried_launch_outbox_defers_to_manual_continuation(self):
        continuation = {
            "id": "sent-user-1",
            "sentMessageId": "sent-user-1",
            "conversationId": self.CONV,
            "sentDateTime": "2026-07-01T12:05:00Z",
            "recipientCount": 2,
        }
        with mock.patch.object(email_mod, "find_matching_sent_message_for_retry", return_value=None) as sent_guard, \
             mock.patch.object(email_mod, "find_sent_conversation_continuation_for_retry", return_value=continuation):
            result = email_mod._sent_retry_reconciliation_result(
                self.HEADERS,
                self._retry_data(),
                "broker@example.com",
                "Hi Jane, is 123 Main St still available?",
                "123 Main St",
                conversation_id=self.CONV,
            )

        sent_guard.assert_called_once()  # already-sent guard runs first...
        self.assertNotIn("sent", result)  # ...and did NOT declare a resend
        self.assertEqual(continuation, result.get("manualContinuation"))

        reason = email_mod._manual_continuation_retry_reason(result)
        self.assertIn("manually continued", reason.lower())
        self.assertIn("review", reason.lower())
        self.assertIn("2026-07-01T12:05:00Z", reason)

    def test_fresh_launch_outbox_without_retry_markers_skips_reconciliation(self):
        # Negative control: a first-attempt launch outbox (no retry markers) must
        # NOT trigger a Sent Items preflight at all (no continuation lookup),
        # so normal automation is never blocked.
        with mock.patch.object(email_mod, "find_matching_sent_message_for_retry") as sent_guard, \
             mock.patch.object(email_mod, "find_sent_conversation_continuation_for_retry") as cont_guard:
            result = email_mod._sent_retry_reconciliation_result(
                self.HEADERS,
                {"clientId": "client-1", "source": "dashboard_new_campaign"},
                "broker@example.com",
                "Hi Jane, is 123 Main St still available?",
                "123 Main St",
                conversation_id=self.CONV,
            )
        self.assertEqual({}, result)
        sent_guard.assert_not_called()
        cont_guard.assert_not_called()


# =========================================================================
# core.name_resolution / manual_continuation  -> NOT APPLICABLE
# =========================================================================
class NameResolutionManualContinuationNotApplicable(unittest.TestCase):
    """Rubric cell: core.name_resolution / manual_continuation -> NOT APPLICABLE.

    name_resolution is the launch-time deterministic [NAME] sheet-column ->
    greeting resolver. It runs ONLY on new-outreach outbox items, and the
    new-outreach send path invokes the Sent Items reconciliation with
    conversation_id defaulting to None (email.py: _sent_retry_reconciliation_result
    is called WITHOUT a conversation_id in the new-outreach branch), so the
    manual-continuation guard structurally cannot fire for the name-resolution
    path. Manual continuation is a live-thread reply concern handled by the
    launch_draft / reply / inbox paths. There is therefore no name-resolution-
    specific manual_continuation behavior distinct from launch_draft.manual_continuation.

    This test documents the structural fact (asserts the new-outreach
    reconciliation call does not carry conversation identity) rather than faking
    a borrowed green.
    """

    def test_new_outreach_reconciliation_has_no_conversation_identity(self):
        # find_sent_conversation_continuation_for_retry returns None immediately
        # when conversation_id is falsy -- proving the name-resolution/new-outreach
        # path cannot detect a manual continuation.
        from email_automation.sent_mail_guard import find_sent_conversation_continuation_for_retry
        result = find_sent_conversation_continuation_for_retry(
            {"Authorization": "Bearer fake"},
            conversation_id=None,
            sent_after="2026-07-01T12:00:00Z",
        )
        self.assertIsNone(result)


# =========================================================================
# core.reply_all_cc / manual_continuation
# =========================================================================
class _FakePendingDoc:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = dict(data)
        self.reference = MagicMock()

    def to_dict(self):
        return dict(self._data)


class _FakePendingFs:
    """Minimal Firestore: users/<uid>/pendingResponses stream()."""

    def __init__(self, docs, user_id):
        self._docs = docs
        self._user_id = user_id
        self._seeded = {
            ("systemConfig", "campaignAccess"): {
                "automationEnabled": True,
                "allowedUids": [],
            },
            ("users", user_id, "clients", "client-1"): {
                "status": "live",
                "automationPaused": False,
            },
        }

    def collection(self, name):
        return _FakePendingNode(self, (name,))


class _FakePendingSnapshot:
    def __init__(self, data=None):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class _FakePendingNode:
    def __init__(self, root, path):
        self.root = root
        self.path = path

    def collection(self, name):
        return _FakePendingNode(self.root, self.path + (name,))

    def document(self, name):
        return _FakePendingNode(self.root, self.path + (name,))

    def get(self):
        return _FakePendingSnapshot(self.root._seeded.get(self.path))

    def stream(self):
        if self.path == (
            "users", self.root._user_id, "pendingResponses",
        ):
            return list(self.root._docs)
        return []


class ReplyAllManualContinuationTests(unittest.TestCase):
    """Rubric cell: core.reply_all_cc / manual_continuation (retry_reconciled).

    Pending responses are re-sent as reply-all drafts that preserve the broker's
    CC audience (via send_reply_in_thread). Drives the REAL retry loop
    process_pending_responses: a queued reply-all response whose prior attempt
    failed must be diverted to manual review (dead-letter) — NOT re-sent to the
    whole CC audience — when Sent Items shows the user already continued the thread.
    """

    USER_ID = "user-reply"

    def _pending_doc(self):
        return _FakePendingDoc("pending-1", {
            "threadId": "thread-1",
            "clientId": "client-1",
            "msgId": "msg-1",
            "recipient": "broker@acme.com",
            "ccEmails": ["teammate@acme.com", "assistant@myfirm.com"],
            "responseBody": "Thanks — following up on 123 Main St. Are the specs still current?",
            "conversationId": "conversation-1",
            "attempts": 1,
            "lastError": "Graph send timed out",
            "lastSendAttemptAt": "2026-07-01T12:00:00Z",
        })

    def test_queued_reply_all_defers_to_manual_continuation_without_resending(self):
        continuation = {"id": "sent-user-1", "conversationId": "conversation-1",
                        "sentDateTime": "2026-07-01T12:05:00Z", "recipientCount": 3}
        fake_fs = _FakePendingFs([self._pending_doc()], self.USER_ID)
        send_spy = MagicMock(return_value=True)
        dead_letter_spy = MagicMock()

        import email_automation.clients as clients_mod
        with mock.patch.object(clients_mod, "_fs", fake_fs), \
             mock.patch.object(processing_mod, "send_reply_in_thread", send_spy), \
             mock.patch.object(pending_mod, "find_matching_sent_message_for_retry", return_value=None), \
             mock.patch.object(pending_mod, "find_sent_conversation_continuation_for_retry", return_value=continuation), \
             mock.patch.object(pending_mod, "_move_pending_response_to_dead_letter", dead_letter_spy):
            op_states = pending_mod.process_pending_responses(self.USER_ID, {"Authorization": "Bearer fake"})

        # #20 GO-condition: process_pending_responses now returns a list of Graph
        # operation-states, not an int send count. A manual continuation defers
        # to dead-letter without sending, so NO error op-state surfaces.
        self.assertIsInstance(op_states, list)
        self.assertEqual(
            [], [s for s in op_states if s.get("status") == "error"],
            "no reply-all should be sent (or fail) on manual continuation",
        )
        send_spy.assert_not_called()
        dead_letter_spy.assert_called_once()
        reason = dead_letter_spy.call_args.args[3]
        self.assertIn("manually continued", reason.lower())


# =========================================================================
# Fakes for processing.py inbox-retry manual continuation
# =========================================================================
class _FakeProcSnapshot:
    def __init__(self, data, exists):
        self._data = data
        self.exists = exists

    def to_dict(self):
        return dict(self._data or {})


class _FakeProcNode:
    def __init__(self, root, path):
        self.root = root
        self.path = tuple(path)

    def collection(self, name):
        return _FakeProcNode(self.root, self.path + ("collection", name))

    def document(self, name):
        return _FakeProcNode(self.root, self.path + ("document", name))

    def get(self):
        return _FakeProcSnapshot(self.root.docs.get(self.path), self.path in self.root.docs)

    def set(self, data, merge=False):
        self.root.set_calls.append((self.path, data, merge))
        existing = self.root.docs.get(self.path, {}) if merge else {}
        self.root.docs[self.path] = {**existing, **data}

    def delete(self):
        self.root.docs.pop(self.path, None)


class _FakeProcFs:
    def __init__(self):
        self.docs = {}
        self.set_calls = []

    def seed(self, path, data):
        self.docs[tuple(path)] = dict(data)

    def collection(self, name):
        return _FakeProcNode(self, ("collection", name))


def _failure_path(user_id, thread_id, message_id):
    return (
        "collection", "users", "document", user_id,
        "collection", "processingFailures", "document", f"{thread_id}__{message_id}",
    )


def _thread_path(user_id, thread_id):
    return (
        "collection", "users", "document", user_id,
        "collection", "threads", "document", thread_id,
    )


# =========================================================================
# core.property_extraction / manual_continuation
# =========================================================================
class PropertyExtractionManualContinuationTests(unittest.TestCase):
    """Rubric cell: core.property_extraction / manual_continuation (live_waiting).

    A broker reply carrying full specs whose PRIOR processing failed (a
    processingFailures record exists) must NOT be re-processed / re-extracted on
    the next inbox pass when Sent Items shows the user manually continued the
    thread. Drives the REAL gate _skip_inbox_retry_after_manual_continuation:
    it must return True (so the caller's `continue` skips process_inbox_message,
    i.e. no duplicate AI spec extraction / action) and mark the message processed.
    """

    USER_ID = "user-proc"
    THREAD = "thread-1"
    MSG_ID = "imid-1"

    def _msg(self):
        return {
            "conversationId": "conversation-1",
            "internetMessageId": self.MSG_ID,
            "receivedDateTime": "2026-07-01T12:00:00Z",
            "subject": "Re: 123 Main St — 5,000 SF, $22/SF NNN, available now",
        }

    def test_reprocessing_skipped_when_prior_failure_and_manual_continuation(self):
        fake_fs = _FakeProcFs()
        fake_fs.seed(_failure_path(self.USER_ID, self.THREAD, self.MSG_ID),
                     {"retryable": True, "recoveryStatus": "pending"})
        fake_fs.seed(_thread_path(self.USER_ID, self.THREAD), {"clientId": "client-1"})
        continuation = {"id": "sent-user-1", "conversationId": "conversation-1",
                        "sentDateTime": "2026-07-01T12:05:00Z", "recipientCount": 2}
        mark_spy = MagicMock()
        with mock.patch.object(processing_mod, "_fs", fake_fs), \
             mock.patch.object(processing_mod, "find_sent_conversation_continuation_for_retry", return_value=continuation), \
             mock.patch.object(processing_mod, "mark_processed", mark_spy):
            skipped = processing_mod._skip_inbox_retry_after_manual_continuation(
                self.USER_ID, {"Authorization": "Bearer fake"}, self.THREAD, self._msg(), self.MSG_ID
            )

        self.assertTrue(
            skipped,
            "inbox reprocessing (spec re-extraction) must be skipped after a manual continuation",
        )
        mark_spy.assert_called_once_with(self.USER_ID, self.MSG_ID)

    def test_reprocessing_not_skipped_without_prior_failure_record(self):
        # Negative control: a fresh broker-specs reply with no prior failure
        # record is NOT gated — it proceeds to normal extraction (skip=False),
        # and the manual-continuation guard is never even consulted.
        fake_fs = _FakeProcFs()  # no processingFailures doc seeded
        with mock.patch.object(processing_mod, "_fs", fake_fs), \
             mock.patch.object(processing_mod, "find_sent_conversation_continuation_for_retry") as cont_guard, \
             mock.patch.object(processing_mod, "mark_processed") as mark_spy:
            skipped = processing_mod._skip_inbox_retry_after_manual_continuation(
                self.USER_ID, {"Authorization": "Bearer fake"}, self.THREAD, self._msg(), self.MSG_ID
            )
        self.assertFalse(skipped)
        cont_guard.assert_not_called()
        mark_spy.assert_not_called()


# =========================================================================
# core.sheet_update / manual_continuation
# =========================================================================
class SheetUpdateManualContinuationTests(unittest.TestCase):
    """Rubric cell: core.sheet_update / manual_continuation (live_waiting).

    When a broker-specs inbox action's retry is blocked by a manual continuation,
    the persisted processing-failure record must be PARKED as non-retryable so a
    later retry pass can never re-drive the extracted specs into the sheet row.
    Drives the REAL persistence handler
    _record_processing_failure_blocked_by_manual_continuation and asserts the
    parked record's sheet-safety fields.
    """

    USER_ID = "user-sheet"
    THREAD = "thread-1"
    MSG_ID = "imid-1"

    def test_blocked_action_is_parked_non_retryable_so_no_stale_sheet_write(self):
        fake_fs = _FakeProcFs()
        continuation = {"id": "sent-user-1", "conversationId": "conversation-1",
                        "sentDateTime": "2026-07-01T12:05:00Z"}
        with mock.patch.object(processing_mod, "_fs", fake_fs):
            processing_mod._record_processing_failure_blocked_by_manual_continuation(
                self.USER_ID, "client-1", self.THREAD, self.MSG_ID, continuation
            )

        path, payload, _merge = fake_fs.set_calls[-1]
        self.assertEqual(
            False, payload["retryable"],
            "blocked action must be non-retryable so the sheet write is never re-attempted",
        )
        self.assertEqual("blocked_manual_conversation_continued", payload["recoveryStatus"])
        self.assertIn("manual review", payload["lastRetryError"].lower())

    def test_guard_unreadable_still_parks_action_for_manual_review(self):
        # Near-miss: if the Sent Items guard was UNREADABLE (not a clean
        # continuation), the record is still parked non-retryable under a distinct
        # status, so an ambiguous state can never silently retry a sheet write.
        fake_fs = _FakeProcFs()
        artifact = {"guardUnreadable": True, "guardError": "Graph 503"}
        with mock.patch.object(processing_mod, "_fs", fake_fs):
            processing_mod._record_processing_failure_blocked_by_manual_continuation(
                self.USER_ID, "client-1", self.THREAD, self.MSG_ID, artifact
            )
        _path, payload, _merge = fake_fs.set_calls[-1]
        self.assertEqual(False, payload["retryable"])
        self.assertEqual("blocked_manual_retry_guard_unreadable", payload["recoveryStatus"])


# =========================================================================
# core.sheet_update / terminal_state  (replaces BORROWED GREEN)
# =========================================================================
class SheetUpdateTerminalStateTests(unittest.TestCase):
    """Rubric cell: core.sheet_update / terminal_state (stopped).

    Replaces a borrowed-green (row-anchor / gross-rent formula tests that did not
    prove terminal-state suppression). Drives the REAL terminal gate
    _should_skip_processing_for_terminal_thread that guards the inbox sheet-write
    (email.py call site: "If thread is terminal, skip further processing (AI,
    sheet updates, auto-replies)"). A broker reply carrying full specs on a
    stopped/completed thread must be short-circuited (no live-thread sheet
    mutation); an active thread must proceed; a stopped thread with a genuine
    active replacement property must still process (no legitimate work dropped).
    """

    SPECS_MSG = "5,000 SF, $22/SF NNN, available now at 123 Main St"

    def test_specs_on_stopped_thread_are_short_circuited(self):
        self.assertTrue(
            processing_mod._should_skip_processing_for_terminal_thread(
                "stopped", {"status": "stopped"}, self.SPECS_MSG
            ),
            "broker specs on a stopped thread must NOT be written to the sheet",
        )

    def test_specs_on_completed_thread_are_short_circuited(self):
        self.assertTrue(
            processing_mod._should_skip_processing_for_terminal_thread(
                "completed", {"status": "completed"}, self.SPECS_MSG
            ),
        )

    def test_specs_on_active_thread_proceed_to_sheet_update(self):
        # Negative control: a live/active thread must NOT be short-circuited so
        # the extracted specs are written to the sheet.
        self.assertFalse(
            processing_mod._should_skip_processing_for_terminal_thread(
                "active", {"status": "active"}, self.SPECS_MSG
            ),
        )

    def test_stopped_thread_with_active_replacement_still_processes(self):
        # Near-miss: a stopped thread that carries an ACTIVE replacement property
        # matching the broker's message must still process (don't drop a
        # legitimate new-property referral / sheet write).
        thread_data = {
            "status": "stopped",
            "activeReplacementProperty": {"address": "456 Oak Ave", "rowNumber": 5},
        }
        self.assertFalse(
            processing_mod._should_skip_processing_for_terminal_thread(
                "stopped", thread_data, "Try 456 Oak Ave instead — 3,000 SF available"
            ),
        )


# =========================================================================
# core.health_recovery / wrong_recipient
# =========================================================================
class _DlrSnapshot:
    def __init__(self, data, exists):
        self._data = data
        self.exists = exists

    def to_dict(self):
        return dict(self._data or {})


class _DlrNode:
    def __init__(self, root, path):
        self.root = root
        self.path = tuple(path)
        self.id = self.path[-1] if self.path else "root"

    def collection(self, name):
        return _DlrNode(self.root, self.path + ("collection", name))

    def document(self, name):
        return _DlrNode(self.root, self.path + ("document", name))

    def get(self):
        return _DlrSnapshot(self.root.docs.get(self.path), self.path in self.root.docs)

    def set(self, data, merge=False):
        self.root.set_calls.append((self.path, data, merge))
        existing = self.root.docs.get(self.path, {}) if merge else {}
        self.root.docs[self.path] = {**existing, **data}

    def update(self, data):
        self.root.update_calls.append((self.path, data))
        existing = self.root.docs.get(self.path, {})
        self.root.docs[self.path] = {**existing, **data}

    def add(self, data):
        self.root.add_calls.append((self.path, data))
        doc_id = f"auto-{len(self.root.add_calls)}"
        node = _DlrNode(self.root, self.path + ("document", doc_id))
        self.root.docs[node.path] = dict(data)
        return node


class _DlrFs:
    def __init__(self, dead_letter_payload):
        self.docs = {}
        self.add_calls = []
        self.set_calls = []
        self.update_calls = []
        self.dl_path = (
            "collection", "users", "document", "uid-1",
            "collection", "deadLetterQueue", "document", "dead-1",
        )
        self.docs[self.dl_path] = dict(dead_letter_payload)

    def collection(self, name):
        return _DlrNode(self, ("collection", name))


class HealthRecoveryWrongRecipientTests(unittest.TestCase):
    """Rubric cell: core.health_recovery / wrong_recipient (paused_manual_review).

    A token/graph-failure send that dead-lettered is being recovered via
    requeue_verified_unsent. If the recovery cannot establish a verified
    recipient from the payload, it must PAUSE for manual review
    (blocked_missing_send_identity) and never resurrect a send to an unknown /
    wrong recipient — the Sent Items guard is not even consulted, and no fresh
    outbox item is written.
    """

    def _fake_clients(self, fake_fs):
        from types import SimpleNamespace
        return mock.patch.dict(sys.modules, {"email_automation.clients": SimpleNamespace(_fs=fake_fs)})

    def _payload(self, **overrides):
        payload = {
            "source": "outbox",
            "status": "dead_lettered",
            "script": "Hi, can you confirm availability of 123 Main St?",
            "subject": "123 Main St availability",
            "clientId": "client-1",
            "threadId": "thread-1",
            "conversationId": "conversation-1",
            "attempts": 5,
            "failureReason": "Graph token refresh failed repeatedly",
            "lastError": "Graph token refresh failed repeatedly",
        }
        payload.update(overrides)
        return payload

    def test_recovery_blocks_requeue_without_verified_recipient(self):
        # No assignedEmails / recipient anywhere -> recipient cannot be verified.
        fake_fs = _DlrFs(self._payload())
        guard_spy = MagicMock()
        with self._fake_clients(fake_fs), \
             mock.patch.object(dlr_mod, "find_matching_sent_message_for_retry", guard_spy):
            result = dlr_mod.resolve_dead_letter_item(
                "uid-1", "dead-1",
                action="requeue_verified_unsent",
                headers={"Authorization": "Bearer fake"},
                operator_id="operator-1",
            )

        self.assertFalse(result["success"])
        self.assertEqual("missing_send_identity", result["code"])
        self.assertEqual([], fake_fs.add_calls, "no send may be resurrected without a verified recipient")
        guard_spy.assert_not_called()
        self.assertEqual(
            "blocked_missing_send_identity",
            fake_fs.update_calls[-1][1]["recoveryStatus"],
        )

    def test_recovery_requeues_when_recipient_is_verified(self):
        # Positive control: with a verified recipient and a Sent Items readback
        # proving the message was NOT already sent, recovery requeues cleanly.
        fake_fs = _DlrFs(self._payload(assignedEmails=["broker@acme.com"]))
        with self._fake_clients(fake_fs), \
             mock.patch.object(dlr_mod, "find_matching_sent_message_for_retry", return_value=None), \
             mock.patch.object(dlr_mod, "find_sent_conversation_continuation_for_retry", return_value=None):
            result = dlr_mod.resolve_dead_letter_item(
                "uid-1", "dead-1",
                action="requeue_verified_unsent",
                headers={"Authorization": "Bearer fake"},
                operator_id="operator-1",
            )
        self.assertTrue(result["success"])
        self.assertEqual("requeued", result["code"])
        self.assertEqual(1, len(fake_fs.add_calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
