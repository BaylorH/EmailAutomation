"""Surface D-2 — core.signature_identity state-permutation matrix.

Closes the needs_fixture cells for feature=core.signature_identity across the
state columns: terminal_state, bad_placeholder, manual_continuation,
duplicate_retry, operator_visible_failure.

Every test drives REAL production code (the signature builder in
email_automation.utils and the outbox send pipeline in email_automation.email)
against in-memory doubles. Faked Firestore/Graph only — ZERO live sends. Each
test asserts a distinct safety-relevant behavior of signature/identity handling
in one lifecycle state and would FAIL if that behavior regressed.
"""

import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import unittest
from unittest.mock import patch, MagicMock

from email_automation import email as email_module
from email_automation.column_config import get_default_column_config
from email_automation.utils import (
    build_professional_signature_html,
    format_email_body_with_footer,
    resolve_signature_settings,
)


# ---------------------------------------------------------------------------
# In-memory Firestore double good enough for the outbox send pipeline. It
# records deadLetterQueue / reconciliation writes so we can assert that a
# signature failure or a terminal thread produces an operator-visible record
# instead of a silent send.
# ---------------------------------------------------------------------------
class _FakeSnap:
    def __init__(self, data=None, exists=False):
        self._data = data or {}
        self.exists = exists

    def to_dict(self):
        return self._data


class _RecNode:
    def __init__(self, root, path):
        self.root = root
        self.path = path

    def collection(self, name):
        return _RecNode(self.root, self.path + [("c", name)])

    def document(self, name):
        return _RecNode(self.root, self.path + [("d", name)])

    def order_by(self, *_a, **_k):
        return self

    def stream(self):
        return list(self.root.outbox_docs)

    def get(self):
        if self.path == [("c", "users"), ("d", self.root.user_id)]:
            return _FakeSnap(self.root.user_data, exists=True)
        # #16 pre-send thread-binding validation re-reads the server-side thread
        # doc and the reply-target message doc under it. Serve seeded ones so a
        # valid dashboard reply passes validation and reaches the manual-
        # continuation guard rather than failing "thread_not_found".
        base = [("c", "users"), ("d", self.root.user_id), ("c", "threads")]
        if len(self.path) == 4 and self.path[:3] == base:
            thread = self.root.threads.get(self.path[3][1])
            return _FakeSnap(thread or {}, exists=thread is not None)
        if (
            len(self.path) == 6
            and self.path[:3] == base
            and self.path[4] == ("c", "messages")
        ):
            key = (self.path[3][1], self.path[5][1])
            return _FakeSnap({}, exists=key in self.root.thread_messages)
        return _FakeSnap(exists=False)

    def add(self, data):
        collection_name = self.path[-1][1]
        self.root.adds.append((collection_name, data))
        return _RecNode(self.root, self.path + [("d", "auto-id")])

    def set(self, data, merge=False):
        self.root.sets.append((tuple(self.path), data, merge))

    def update(self, data):
        self.root.updates.append((tuple(self.path), data))

    def delete(self):
        self.root.deletes.append(tuple(self.path))


class _RecFS:
    def __init__(self, user_id="uid-1", user_data=None, outbox_docs=None,
                 threads=None, thread_messages=None):
        self.user_id = user_id
        self.user_data = user_data or {}
        self.outbox_docs = outbox_docs or []
        # #16 thread-binding validation: seeded server-side threads and the
        # (thread_id, message_id) pairs recorded under them.
        self.threads = threads or {}
        self.thread_messages = set(thread_messages or ())
        self.adds = []
        self.sets = []
        self.updates = []
        self.deletes = []

    def collection(self, name):
        return _RecNode(self, [("c", name)])

    def dead_letters(self):
        return [payload for (col, payload) in self.adds if col == "deadLetterQueue"]


class _FakeDocRef:
    def __init__(self, doc_id="outbox-1"):
        self.id = doc_id
        self.deleted = False
        self.set_calls = []

    def delete(self):
        self.deleted = True

    def set(self, *args, **kwargs):
        self.set_calls.append((args, kwargs))


class _FakeDoc:
    def __init__(self, data, doc_id="outbox-1"):
        self.id = doc_id
        self.reference = _FakeDocRef(doc_id)
        self._data = data

    def to_dict(self):
        return self._data


# The user profile used across the send-pipeline tests. It carries a resolvable
# structured professional signature (John Doe / Example Realty Advisors) so each
# test proves the *state gate* — not an empty signature — is what governs whether
# that identity ever reaches the send surface.
PROFESSIONAL_USER_DATA = {
    "email": "baylor.freelance@outlook.com",
    "signatureMode": "professional",
    "emailSignature": '<div data-sitesift-professional-signature="v1">Jill Ames jill.ames@mohrpartners.com</div>',
    "professionalSignature": {
        "name": "John Doe",
        "title": "Principal",
        "email": "baylor.freelance@outlook.com",
        "company": "Example Realty Advisors",
    },
}


class SignatureIdentityBadPlaceholderTest(unittest.TestCase):
    """feature=core.signature_identity, state=bad_placeholder (outbox_queued).

    An unresolved mail-merge placeholder in a signature field must be blocked
    before the send surface: the literal "[NAME]"/"[COMPANY]" token must not
    survive into the rendered signature, while a resolved real value passes
    through unchanged.
    """

    def test_unresolved_signature_placeholder_is_stripped_before_send_surface(self):
        # Real builder under test. Every human-identity field is an un-substituted
        # template token that the frontend mail-merge failed to resolve.
        bad = build_professional_signature_html({
            "name": "[NAME]",
            "title": "[TITLE]",
            "company": "[COMPANY]",
            "email": "[EMAIL]",
            "phone": "[PHONE]",
        })

        for token in ("[NAME]", "[TITLE]", "[COMPANY]", "[EMAIL]", "[PHONE]"):
            self.assertNotIn(
                token, bad,
                f"unresolved placeholder {token} must not reach the outbound signature",
            )
        # With every identity field a placeholder, nothing identity-bearing remains,
        # so the builder emits no signature at all rather than a broken one.
        self.assertEqual("", bad)

        # Negative control: resolved real identity values pass through unchanged.
        # If the sanitizer were over-broad (or removed), this assertion pins that
        # real content is never stripped.
        good = build_professional_signature_html({
            "name": "Drew Ingram",
            "company": "Example Realty Advisors",
            "email": "drew.ingram@example.com",
        })
        self.assertIn("Drew Ingram", good)
        self.assertIn("Example Realty Advisors", good)

        # End-to-end through the body formatter (the last transform before Graph):
        # a partially-unresolved signature keeps the real company but drops the
        # bracketed name token entirely.
        partial = build_professional_signature_html({
            "name": "[FIRST_NAME]",
            "company": "Example Realty Advisors",
            "email": "drew.ingram@example.com",
        })
        html = format_email_body_with_footer(
            "Hi Avery,\n\nCould you confirm the rate?",
            partial,
            "professional",
            user_email="drew.ingram@example.com",
        )
        self.assertIn("Hi Avery", html)
        self.assertIn("Example Realty Advisors", html)
        self.assertNotIn("[FIRST_NAME]", html)
        self.assertNotIn("[", html.split("Example Realty Advisors")[0][-200:])


class SignatureIdentityTerminalStateTest(unittest.TestCase):
    """feature=core.signature_identity, state=terminal_state (stopped).

    When the client's automation is stopped (a terminal thread), a fully
    resolvable professional identity must NOT be emitted: the outbox item is
    dead-lettered and the signature-bearing send surface is never reached.
    """

    def test_resolved_signature_is_never_emitted_on_stopped_terminal_thread(self):
        # The identity IS available — prove the gate, not an empty signature.
        user_signature, signature_mode, user_email = resolve_signature_settings(PROFESSIONAL_USER_DATA)
        self.assertIn("John Doe", user_signature)
        self.assertEqual("professional", signature_mode)

        doc = _FakeDoc({
            "assignedEmails": ["bp21harrison@gmail.com"],
            "script": "Hi Avery,\n\nFollowing up on the tour.",
            "clientId": "client-stopped",
            "subject": "100 Terminal Way",
            "rowNumber": 7,
        }, doc_id="outbox-terminal")

        fake_fs = _RecFS()
        send_and_index = MagicMock()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_should_pause_results_outbox_for_user", return_value=False), \
             patch.object(
                 email_module, "get_client_automation_pause",
                 return_value=(True, "Client automation stopped by operator", {}),
             ), \
             patch.object(email_module, "send_and_index_email", send_and_index):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
                user_signature,
                signature_mode,
                user_email,
            )

        # No signed outbound was produced on the terminal thread.
        send_and_index.assert_not_called()
        # The stop is operator-visible: the item is moved to the dead-letter queue.
        dead = fake_fs.dead_letters()
        self.assertEqual(1, len(dead))
        self.assertIn("campaign is stopped", dead[0]["failureReason"].lower())
        self.assertIn("stopped by operator", dead[0]["failureReason"].lower())
        # Original outbox item consumed (not left to silently retry a send).
        self.assertTrue(doc.reference.deleted)


class SignatureIdentityManualContinuationTest(unittest.TestCase):
    """feature=core.signature_identity, state=manual_continuation (retry_reconciled).

    If Sent Items shows the user manually continued the conversation before the
    queued retry fired, the stale signed draft must be dead-lettered — never
    sent — so a bot signature does not stomp the human's manual reply.
    """

    def test_manual_continuation_dead_letters_stale_signed_draft_without_resending(self):
        user_signature, signature_mode, user_email = resolve_signature_settings(PROFESSIONAL_USER_DATA)
        self.assertIn("John Doe", user_signature)

        doc = _FakeDoc({
            "assignedEmails": ["bp21harrison@gmail.com"],
            "script": "Hi Avery,\n\nJust circling back on the rate.",
            "clientId": "client-1",
            "subject": "200 Continuation Way",
            "threadId": "thread-xyz",
            "replyToMessageId": "msg-abc",
            "conversationId": "conv-1",
            "attempts": 1,  # a retry — arms the Sent Items preflight guard
            "lastError": "prior send timed out",
        }, doc_id="outbox-manual-cont")

        # Seed the open server-side thread + recorded reply target so #16's
        # thread-binding validation passes and execution reaches the #15
        # manual-continuation guard (the behavior under test).
        fake_fs = _RecFS(
            threads={"thread-xyz": {"clientId": "client-1", "status": "active"}},
            thread_messages={("thread-xyz", "msg-abc")},
        )
        send_and_index = MagicMock()
        send_reply = MagicMock()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_should_pause_results_outbox_for_user", return_value=False), \
             patch.object(email_module, "get_client_automation_pause", return_value=(False, None, {})), \
             patch.object(email_module, "_fetch_graph_message_metadata", return_value={}), \
             patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com"), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=None), \
             patch.object(
                 email_module, "find_sent_conversation_continuation_for_retry",
                 return_value={"sentDateTime": "2026-07-04T10:00:00Z"},
             ), \
             patch.object(email_module, "_send_outbox_as_reply", send_reply), \
             patch.object(email_module, "send_and_index_email", send_and_index):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
                user_signature,
                signature_mode,
                user_email,
            )

        # No second, signature-bearing message was sent over the human's reply.
        send_reply.assert_not_called()
        send_and_index.assert_not_called()
        # The stale signed draft is surfaced to the operator with a clear reason.
        dead = fake_fs.dead_letters()
        self.assertEqual(1, len(dead))
        self.assertIn("manually continued", dead[0]["failureReason"].lower())
        self.assertTrue(doc.reference.deleted)


class SignatureIdentityDuplicateRetryTest(unittest.TestCase):
    """feature=core.signature_identity, state=duplicate_retry (retry_reconciled).

    A retry whose prior attempt is found already delivered in Sent Items must
    reconcile — NOT re-send — so the signed message is never appended and
    delivered a second time.
    """

    def test_retry_finding_prior_send_reconciles_without_double_appending_signature(self):
        user_signature, signature_mode, user_email = resolve_signature_settings(PROFESSIONAL_USER_DATA)
        self.assertIn("John Doe", user_signature)

        doc = _FakeDoc({
            "assignedEmails": ["bp21harrison@gmail.com"],
            "script": "Hi Avery,\n\nConfirming the tour time.",
            "clientId": "client-1",
            "subject": "300 Duplicate Way",
            "rowNumber": 9,
            "scriptSelectionMode": "exact",  # avoid contact-history sheet lookups
            "contactName": "Avery",
            "attempts": 1,  # a retry — arms the Sent Items preflight guard
            "lastError": "prior send uncertain",
        }, doc_id="outbox-dup-retry")

        fake_fs = _RecFS()
        send_and_index = MagicMock()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_should_pause_results_outbox_for_user", return_value=False), \
             patch.object(email_module, "get_client_automation_pause", return_value=(
                 False,
                 None,
                 {"columnConfig": get_default_column_config()},
             )), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(
                 email_module, "find_matching_sent_message_for_retry",
                 return_value={"internetMessageId": "<already-sent@graph>", "conversationId": "conv-dup"},
             ), \
             patch.object(email_module, "send_and_index_email", send_and_index):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
                user_signature,
                signature_mode,
                user_email,
            )

        # The retry did NOT re-send — no second signature-bearing copy delivered.
        send_and_index.assert_not_called()
        # Instead the already-sent message is exposed as a visible reconciliation
        # record so an operator can confirm the single delivery.
        dead = fake_fs.dead_letters()
        self.assertEqual(1, len(dead))
        self.assertTrue(dead[0].get("alreadySent"))
        self.assertEqual("needs_reconciliation", dead[0].get("status"))
        self.assertTrue(doc.reference.deleted)


class SignatureIdentityOperatorVisibleFailureTest(unittest.TestCase):
    """feature=core.signature_identity, state=operator_visible_failure (dead_letter_visible).

    When Graph accepts a *signed* message but the Sent Items identity index is
    missing (graph_accepted_but_index_missing), the item must surface as a
    visible needs_reconciliation record — never deleted as a clean success —
    and the send must have carried the resolved professional identity.
    """

    def test_signed_send_accepted_but_unindexed_surfaces_visible_reconciliation(self):
        user_signature, signature_mode, user_email = resolve_signature_settings(PROFESSIONAL_USER_DATA)
        self.assertIn("John Doe", user_signature)

        doc = _FakeDoc({
            "assignedEmails": ["bp21harrison@gmail.com"],
            "script": "Hi Avery,\n\nConfirming the details below.",
            "clientId": "client-1",
            "subject": "400 Unindexed Way",
            "rowNumber": 11,
            "scriptSelectionMode": "exact",
            "contactName": "Avery",
        }, doc_id="outbox-unindexed")

        captured = {}

        def fake_send(_user_id, _headers, _script, recipients, *_args, **kwargs):
            # Graph accepted the send (identity id present) but returned an error:
            # the Sent Items lookup that indexes the message for reply-tracking failed.
            captured["user_signature"] = kwargs.get("user_signature")
            captured["signature_mode"] = kwargs.get("signature_mode")
            return {
                "sent": [],
                "errors": {
                    recipients[0]: "Graph accepted send but Sent Items identity lookup failed; "
                                   "operator reconciliation required",
                },
                "internetMessageIds": {recipients[0]: "<accepted-no-index@graph>"},
            }

        fake_fs = _RecFS()
        finalize = MagicMock()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_should_pause_results_outbox_for_user", return_value=False), \
             patch.object(email_module, "get_client_automation_pause", return_value=(
                 False,
                 None,
                 {"columnConfig": get_default_column_config()},
             )), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_finalize_successful_outbox_item", finalize), \
             patch.object(email_module, "send_and_index_email", side_effect=fake_send):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
                user_signature,
                signature_mode,
                user_email,
            )

        # The failing send actually carried the resolved professional identity.
        self.assertIn("John Doe", captured.get("user_signature") or "")
        self.assertEqual("professional", captured.get("signature_mode"))
        # It was NOT treated as a clean success…
        finalize.assert_not_called()
        # …but surfaced as an operator-visible reconciliation record.
        dead = fake_fs.dead_letters()
        self.assertEqual(1, len(dead))
        self.assertTrue(dead[0].get("alreadySent"))
        self.assertEqual("needs_reconciliation", dead[0].get("status"))
        self.assertIn("reconciliation", (dead[0].get("failureReason") or "").lower())
        self.assertTrue(doc.reference.deleted)


if __name__ == "__main__":
    unittest.main()
