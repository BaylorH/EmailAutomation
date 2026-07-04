"""Pressure test for the dashboard_action_resolution event class.

Event: operator resolves a dashboard action -- clicks Send Reply on an
action-needed draft, cancels a queued launch before the worker claims it, or
dismisses a non-viable/new-property action.

The DETERMINISTIC guard that decides whether a resolved/cancelled action still
sends is:

    email_automation.email._is_cancelled_outbox_item(data)   (email.py:1559)

wired into the send loop via:

    _delete_cancelled_outbox_item_if_needed(...)  (deletes + terminalizes audit)
    _claim_outbox_item(...)                        (transaction re-check)

stopIf conditions this test defends:
  * "cancelled action still sends"  -> a cancel encoding the guard MISSES is a
    HIGH false negative (the operator explicitly cancelled, worker sends anyway).
  * "unresolved action disappears"  -> a live action_needed/queued item the guard
    WRONGLY treats as cancelled would be deleted = false positive.

We drive the REAL guard on many encodings of the cancel signal (the dashboard,
not a broker, writes these fields, so the "phrasings" are the messy ways a
cancel can be represented in Firestore: British/American spelling, casing,
whitespace, delimiter variants, loosely-typed booleans, transient in-progress
statuses) plus near-miss controls that must NOT be treated as cancelled.

Only external boundaries (Firestore _fs) are faked. No real sends.
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation import email as email_module


# --------------------------------------------------------------------------
# Fakes for the Firestore boundary (borrowed shape from test_action_audit_backend)
# --------------------------------------------------------------------------
class FakeOutboxRef:
    def __init__(self, doc_id="outbox-1"):
        self.id = doc_id
        self.deleted = False

    def delete(self):
        self.deleted = True


class FakeFirestoreNode:
    def __init__(self, root, path=None):
        self.root = root
        self.path = path or []

    def collection(self, name):
        return FakeFirestoreNode(self.root, self.path + ["collection", name])

    def document(self, name):
        return FakeFirestoreNode(self.root, self.path + ["document", name])

    def set(self, data, merge=False):
        self.root.set_calls.append((tuple(self.path), data, merge))


class FakeFirestore:
    def __init__(self):
        self.set_calls = []

    def collection(self, name):
        return FakeFirestoreNode(self, ["collection", name])


# --------------------------------------------------------------------------
# Phrasing corpus
# --------------------------------------------------------------------------
# Each entry: (label, outbox_data_dict). These are the REAL-THREAT cancel
# encodings -- the operator cancelled/dismissed; the item MUST NOT send.
CANCEL_PHRASINGS_EXPECTED_TRUE = [
    ("bool cancelRequested True",              {"cancelRequested": True}),
    ("status cancel_requested (canonical)",    {"status": "cancel_requested"}),
    ("status cancelled (British)",             {"status": "cancelled"}),
    ("status canceled (American)",             {"status": "canceled"}),
    ("status CANCELLED (all caps)",            {"status": "CANCELLED"}),
    ("status Cancelled (title case)",          {"status": "Cancelled"}),
    ("status '  cancelled  ' (whitespace)",    {"status": "  cancelled  "}),
    ("status CANCEL_REQUESTED (caps)",         {"status": "CANCEL_REQUESTED"}),
    ("status canceled + assignedEmails",       {"status": "canceled",
                                                "assignedEmails": ["broker@x.com"],
                                                "script": "Hi, following up..."}),
    ("bool True but stale status action_needed",
                                               {"cancelRequested": True,
                                                "status": "action_needed"}),
]

# REAL-THREAT cancel encodings the guard currently MISSES (candidate bugs).
# We assert the CORRECT behavior (should be treated as cancelled) so these go RED.
CANCEL_PHRASINGS_BUG_EXPECTED_TRUE = [
    # Loosely-typed boolean writes (JS toggle / Firestore REST / form-encoded).
    ("cancelRequested string 'true'",          {"cancelRequested": "true"}),
    ("cancelRequested int 1",                  {"cancelRequested": 1}),
    # Delimiter variant of the canonical status.
    ("status cancel-requested (hyphen)",       {"status": "cancel-requested"}),
    # Optimistic in-progress cancel status set by the UI on click.
    ("status cancelling (in-progress, BrE)",   {"status": "cancelling"}),
    ("status canceling (in-progress, AmE)",    {"status": "canceling"}),
]

# NEAR-MISS controls -- live/terminal/history actions that MUST NOT be treated
# as cancelled. A True here = false positive = "unresolved action disappears".
NEAR_MISS_EXPECTED_FALSE = [
    ("status action_needed (Send Reply draft)", {"status": "action_needed"}),
    ("status queued (awaiting worker claim)",   {"status": "queued"}),
    ("status pending",                          {"status": "pending"}),
    ("status sent (read-only history)",         {"status": "sent"}),
    ("status scheduled",                        {"status": "scheduled"}),
    ("empty status string",                     {"status": ""}),
    ("no status field at all",                  {"clientId": "c-1"}),
    ("cancelRequested False (bool)",            {"cancelRequested": False}),
    ("cancelRequested string 'false'",          {"cancelRequested": "false"}),
    ("subject mentions cancelled, status live",
                                                {"status": "action_needed",
                                                 "subject": "Re: cancelled tour"}),
    ("new_property action (dismiss target, not send-cancel)",
                                                {"status": "new_property"}),
]


class IsCancelledGuardPhrasingTests(unittest.TestCase):
    """Drive the pure guard on every phrasing."""

    def test_real_cancel_encodings_are_recognized(self):
        for label, data in CANCEL_PHRASINGS_EXPECTED_TRUE:
            with self.subTest(phrasing=label):
                self.assertTrue(
                    email_module._is_cancelled_outbox_item(dict(data)),
                    f"FALSE NEGATIVE: cancel encoding not recognized -> would SEND: {label}",
                )

    def test_near_misses_are_not_treated_as_cancelled(self):
        for label, data in NEAR_MISS_EXPECTED_FALSE:
            with self.subTest(phrasing=label):
                self.assertFalse(
                    email_module._is_cancelled_outbox_item(dict(data)),
                    f"FALSE POSITIVE: live/terminal action wrongly cancelled -> would DISAPPEAR: {label}",
                )

    def test_loosely_typed_and_variant_cancel_encodings_should_be_recognized(self):
        """RED: these represent genuine cancel intent the guard misses.

        Asserting CORRECT behavior (should be cancelled). Failures here are the
        'cancelled action still sends' safety hole.
        """
        for label, data in CANCEL_PHRASINGS_BUG_EXPECTED_TRUE:
            with self.subTest(phrasing=label):
                self.assertTrue(
                    email_module._is_cancelled_outbox_item(dict(data)),
                    f"FALSE NEGATIVE (BUG): cancel intent missed -> would SEND: {label}",
                )


class DeleteCancelledBehaviorTests(unittest.TestCase):
    """Drive the send-loop wiring that turns the guard into a no-send + audit."""

    def test_cancelled_item_is_deleted_before_send(self):
        ref = FakeOutboxRef("outbox-cancel")
        fs = FakeFirestore()
        data = {
            "status": "cancel_requested",
            "clientId": "client-1",
            "actionAuditId": "audit-1",
            "assignedEmails": ["broker@x.com"],
        }
        with patch("email_automation.clients._fs", fs):
            handled = email_module._delete_cancelled_outbox_item_if_needed(
                ref, data, user_id="uid-1"
            )
        self.assertTrue(handled, "cancelled item should be handled (removed) before send")
        self.assertTrue(ref.deleted, "cancelled outbox doc must be deleted -> not sent")
        # audit terminalized as cancelled
        statuses = [call[1].get("status") for call in fs.set_calls]
        self.assertIn(
            "cancelled", statuses,
            "cancelled action must be terminalized as 'cancelled' in actionAudit",
        )

    def test_live_action_needed_item_is_not_deleted(self):
        """A live action_needed item must survive -> unresolved action must not disappear."""
        ref = FakeOutboxRef("outbox-live")
        fs = FakeFirestore()
        data = {
            "status": "action_needed",
            "clientId": "client-1",
            "actionAuditId": "audit-2",
            "assignedEmails": ["broker@x.com"],
        }
        with patch("email_automation.clients._fs", fs):
            handled = email_module._delete_cancelled_outbox_item_if_needed(
                ref, data, user_id="uid-1"
            )
        self.assertFalse(handled, "live action must not be treated as cancelled")
        self.assertFalse(ref.deleted, "live action_needed doc must NOT be deleted")

    def test_loosely_typed_cancel_is_removed_before_send(self):
        """RED: cancelRequested='true' should stop the send; currently it does not.

        This is the concrete 'cancelled action still sends' proof at the
        behavior level: the item is neither recognized nor deleted, so
        send_outboxes proceeds to send an action the operator cancelled.
        """
        ref = FakeOutboxRef("outbox-str-cancel")
        fs = FakeFirestore()
        data = {
            "cancelRequested": "true",
            "clientId": "client-1",
            "actionAuditId": "audit-3",
            "assignedEmails": ["broker@x.com"],
        }
        with patch("email_automation.clients._fs", fs):
            handled = email_module._delete_cancelled_outbox_item_if_needed(
                ref, data, user_id="uid-1"
            )
        self.assertTrue(
            handled,
            "cancelRequested='true' should be treated as cancelled and removed before send",
        )
        self.assertTrue(
            ref.deleted,
            "SAFETY: operator-cancelled item (string bool) must not be sent",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
