"""Surface D state-permutation rubric closures for core.event_classifier.

Closes three needs_fixture cells of the Production V1 fixture map
(docs/release-safety/production-v1-fixture-map.json), feature
core.event_classifier:

  * terminal_state           -> a reply the classifier labels property-terminal
                                does NOT re-activate a stopped/completed thread
                                and leaves no live outbound reply.
  * bad_placeholder          -> a classifier-proposed outbound reply carrying an
                                unresolved merge placeholder is blocked at the
                                send/apply gate (never reaches Graph).
  * operator_visible_failure -> when the classifier cannot produce a usable
                                proposal, the failure is persisted as a
                                dashboard-visible, retryable processingFailures
                                record and the message is NOT marked processed.

Every test drives the REAL production functions owned by core.event_classifier
(email_automation/ai_processing.py + email_automation/processing.py per
docs/release-safety/feature-registry.json). Only in-memory doubles are used;
ZERO live Firestore / Sheets / Graph calls, ZERO sends.
"""

import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import unittest
from unittest.mock import patch

from email_automation import ai_processing
from email_automation import processing
from email_automation.messaging import THREAD_STATUS


# ---------------------------------------------------------------------------
# core.event_classifier / terminal_state
# ---------------------------------------------------------------------------
class CoreEventClassifierTerminalStateTests(unittest.TestCase):
    """Rubric cell: core.event_classifier / terminal_state.

    Behavior proven: a broker reply that the REAL deterministic classifier
    labels property-terminal does not re-activate an already stopped/completed
    thread, and the classifier strips any live outbound so a terminalized row
    never keeps chatting with the broker.

    Two production functions run for real and nothing they own is patched:
      * ai_processing._augment_events_with_deterministic_signals (the classifier)
      * processing._should_skip_processing_for_terminal_thread (the terminal gate
        that process_inbox_message consults BEFORE any AI/auto-reply work).
    """

    # A physical non-fit reply (office-heavy / lacks warehouse) -- the classifier
    # must read this as the property itself being non-viable.
    TERMINAL_REPLY = (
        "Hi Jill,\n\n"
        "Unfortunately this unit is mostly office and lacks the warehouse area "
        "your client needs, so it won't work for them."
    )

    def test_terminal_property_reply_does_not_reactivate_stopped_or_completed_thread(self):
        # --- The REAL classifier is genuinely exercised on this reply and must
        # emit the property-terminal event (proving the reply carries a live,
        # reactivation-worthy signal -- not an inert message).
        proposal = {
            "updates": [],
            "events": [],
            # A live outbound the model wanted to send back to the broker.
            "response_email": "Thanks for the update -- happy to keep this one on our list!",
        }
        conversation = [{"direction": "inbound", "content": self.TERMINAL_REPLY}]

        augmented = ai_processing._augment_events_with_deterministic_signals(
            proposal,
            conversation,
        )

        self.assertEqual(
            "property_unavailable",
            augmented["events"][0]["type"],
            "Classifier must flag the office-heavy/no-warehouse reply as property-terminal.",
        )
        self.assertEqual("requirements_mismatch", augmented["events"][0]["reason"])
        # FIX-03: a terminal classification must strip the live outbound so a
        # dead row does not keep replying to the broker.
        self.assertIsNone(
            augmented["response_email"],
            "A property-terminal classification must null the live response_email.",
        )

        # --- Now the SAME classified reply arriving on a terminal thread: the
        # terminal gate must suppress all further processing, so nothing can
        # re-activate the thread.
        self.assertTrue(
            processing._should_skip_processing_for_terminal_thread(
                THREAD_STATUS["completed"],
                thread_data={"status": THREAD_STATUS["completed"]},
                message_text=self.TERMINAL_REPLY,
            ),
            "A completed thread must not be re-activated by a re-classified reply.",
        )
        self.assertTrue(
            processing._should_skip_processing_for_terminal_thread(
                THREAD_STATUS["stopped"],
                thread_data={"status": THREAD_STATUS["stopped"]},
                message_text=self.TERMINAL_REPLY,
            ),
            "A stopped thread with no active replacement must not be re-activated.",
        )

        # --- NEGATIVE CONTROL (discriminating): the identical reply on an ACTIVE
        # thread is NOT skipped -- the classifier's output stays reachable. If the
        # gate returned True for every status the positives would be vacuous.
        self.assertFalse(
            processing._should_skip_processing_for_terminal_thread(
                THREAD_STATUS["active"],
                thread_data={"status": THREAD_STATUS["active"]},
                message_text=self.TERMINAL_REPLY,
            ),
            "An active thread must still process the classified reply.",
        )


# ---------------------------------------------------------------------------
# core.event_classifier / bad_placeholder
# ---------------------------------------------------------------------------
class CoreEventClassifierBadPlaceholderTests(unittest.TestCase):
    """Rubric cell: core.event_classifier / bad_placeholder.

    Behavior proven: an outbound reply proposed by the classifier
    (proposal['response_email']) that still carries an UNRESOLVED merge
    placeholder is blocked at the send/apply gate send_reply_in_thread BEFORE any
    Graph call -- the reply never leaves the building. A clean body of the exact
    same shape gets PAST the placeholder guard (blocked later, only by auto-reply
    policy), proving the block is caused by the placeholder specifically, not by
    an unconditional refusal.

    Real function under test: processing.send_reply_in_thread. It calls the real
    outbound_safety.validate_outbound_body first; the whole thing runs offline
    because it returns before the Graph section on both branches.
    """

    def _attempt_send(self, body, user_id):
        # Force a deterministic, non-matching auto-reply allowlist so the clean
        # body is blocked by POLICY (not a send) -- guarantees zero network.
        with patch.dict(os.environ, {"SITESIFT_AUTO_REPLY_ALLOWLIST": "some-other-uid"}):
            return processing.send_reply_in_thread(
                user_id,
                {"Authorization": "Bearer fake"},
                body,
                current_msg_id="graph-msg-1",
                recipient="broker@example.test",
                thread_id="thread-1",
            )

    def test_placeholder_response_email_is_blocked_at_send_gate(self):
        # --- MAIN CASE: the classifier's proposed reply still has "[FIRST_NAME]".
        placeholder_body = (
            "Hi [FIRST_NAME],\n\nThanks for the details on the warehouse -- "
            "we'll review and circle back shortly."
        )
        sent = self._attempt_send(placeholder_body, "uid-anything")

        self.assertFalse(sent, "A reply carrying an unresolved placeholder must not be sent.")
        self.assertEqual(
            "blocked_unsafe_body",
            processing.send_reply_in_thread.last_outcome,
            "The placeholder body must be blocked by the unsafe-body guard, not by policy.",
        )
        self.assertIsNotNone(processing.send_reply_in_thread.last_error)
        self.assertIn("placeholder", processing.send_reply_in_thread.last_error.lower())

        # --- NEGATIVE CONTROL: the SAME reply with the placeholder resolved to a
        # real name gets PAST the placeholder guard. With a non-allowlisted user
        # it is then blocked by auto-reply POLICY -- a DIFFERENT outcome -- which
        # proves the first block was the placeholder guard, not a blanket refusal,
        # and still performs no Graph send.
        clean_body = (
            "Hi Dana,\n\nThanks for the details on the warehouse -- "
            "we'll review and circle back shortly."
        )
        sent_clean = self._attempt_send(clean_body, "uid-anything")

        self.assertFalse(sent_clean)
        self.assertEqual(
            "blocked_auto_reply_policy",
            processing.send_reply_in_thread.last_outcome,
            "A clean body must clear the placeholder guard and only stop at auto-reply policy.",
        )


# ---------------------------------------------------------------------------
# core.event_classifier / operator_visible_failure
# ---------------------------------------------------------------------------
class _RecordingDocRef:
    """A document handle that is both chainable (.collection) and writable
    (.set). It only registers itself in the store when .set() is actually
    called, so intermediate path nodes (users/{uid}) never count as writes."""

    def __init__(self, store, key):
        self._store = store
        self._key = key
        self.written = None

    def collection(self, name):
        return _RecordingNode(self._store, key_prefix=(self._key, name))

    def set(self, data, merge=False):
        if self._key not in self._store.docs:
            self._store.docs[self._key] = self
            self._store.order.append(self._key)
        if self.written is None or not merge:
            self.written = dict(data)
        else:
            self.written.update(data)


class _RecordingNode:
    def __init__(self, store, key_prefix=None):
        self._store = store
        self._key_prefix = key_prefix  # (parent_key, collection_name)

    def collection(self, name):
        return _RecordingNode(self._store, key_prefix=(None, name))

    def document(self, doc_id):
        collection_name = self._key_prefix[1] if self._key_prefix else None
        return _RecordingDocRef(self._store, (collection_name, doc_id))


class _RecordingFirestore(_RecordingNode):
    """Captures every processingFailures write while running the REAL recorder."""

    def __init__(self):
        self.docs = {}
        self.order = []
        super().__init__(self)


class CoreEventClassifierOperatorVisibleFailureTests(unittest.TestCase):
    """Rubric cell: core.event_classifier / operator_visible_failure.

    Behavior proven: when the reply classifier cannot produce a usable proposal
    (the production `No proposal generated` branch), the failure is persisted as
    a dashboard-VISIBLE processingFailures record flagged retryable=True (keyed
    threadId__messageId) AND the message is NOT marked processed -- so an
    operator sees it and it is retried, never silently swallowed. A successful
    classification (error is None) marks processed and needs no failure record,
    proving the visibility+retry wiring is the failure branch firing, not an
    unconditional record.

    Real functions under test: processing._record_ai_processing_failure and
    processing._should_mark_processed_after_error (both core.event_classifier
    owners; processingFailures is a declared dataWrite of the feature). Only the
    Firestore boundary is an in-memory double.
    """

    CLASSIFIER_FAILURE_REASON = "OpenAI proposal was unavailable or invalid JSON"

    def test_unclassifiable_reply_is_visible_and_retryable_not_marked_processed(self):
        fake_fs = _RecordingFirestore()

        # --- FAILURE BRANCH: the classifier could not produce a proposal.
        with patch.object(processing, "_fs", fake_fs):
            processing._record_ai_processing_failure(
                "uid-1",
                "client-1",
                "thread-1",
                "<msg-1@broker.test>",
                self.CLASSIFIER_FAILURE_REASON,
            )

        # Exactly one processingFailures doc was written, keyed threadId__messageId.
        self.assertEqual(1, len(fake_fs.order), "One visible failure record must be written.")
        (collection_name, doc_id) = fake_fs.order[0]
        self.assertEqual("processingFailures", collection_name)
        self.assertEqual("thread-1__<msg-1@broker.test>", doc_id)

        written = fake_fs.docs[(collection_name, doc_id)].written
        # Operator-visible: it carries the classifier failure reason + linkage.
        self.assertEqual(self.CLASSIFIER_FAILURE_REASON, written["reason"])
        self.assertEqual("thread-1", written["threadId"])
        self.assertEqual("<msg-1@broker.test>", written["messageId"])
        self.assertEqual("client-1", written["clientId"])
        # Retryable: the dashboard/retry sweep is allowed to pick it back up.
        self.assertTrue(written["retryable"], "A classifier failure must be flagged retryable.")

        # The same failure keeps the message UNPROCESSED (stays visible for retry).
        self.assertFalse(
            processing._should_mark_processed_after_error(
                processing.RetryableProcessingError(self.CLASSIFIER_FAILURE_REASON)
            ),
            "An unclassifiable reply must NOT be marked processed.",
        )

        # --- NEGATIVE CONTROL (discriminating): a SUCCESSFUL classification
        # (error is None) marks processed -- proving the visible+retryable state
        # above is the failure branch, not an unconditional 'always stuck'.
        self.assertTrue(
            processing._should_mark_processed_after_error(None),
            "A successful classification must mark the message processed.",
        )


if __name__ == "__main__":
    unittest.main()
