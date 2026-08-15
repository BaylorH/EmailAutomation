"""Transport-only contract for exact outbox-item processing.

No test in this module may acquire an MSAL token, call Graph, scan a mailbox,
or enumerate an outbox collection.
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "service-account.json",
    ),
)

import main
from email_automation import email as email_module


def _path(*parts):
    return tuple(parts)


class FakeSnapshot:
    def __init__(self, reference, data):
        self.reference = reference
        self.id = reference.id
        self._data = None if data is None else dict(data)
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class FakeNode:
    def __init__(self, root, path):
        self.root = root
        self.path = tuple(path)
        self.id = self.path[-1] if self.path else None

    def collection(self, name):
        return FakeNode(self.root, self.path + ("collection", name))

    def document(self, name):
        return FakeNode(self.root, self.path + ("document", name))

    def get(self, transaction=None):
        if transaction is not None:
            return transaction.get(self)
        self.root.direct_reads.append(self.path)
        snapshot = FakeSnapshot(self, self.root.documents.get(self.path))
        mutation = self.root.after_direct_read
        if mutation is not None:
            self.root.after_direct_read = None
            mutation(self.root, self.path)
        return snapshot

    def order_by(self, _field):
        raise AssertionError("exact-item processing must not order or scan outbox")

    def stream(self):
        raise AssertionError("exact-item processing must not stream outbox")


class FakeTransaction:
    def __init__(self, root):
        self.root = root
        self.pending = []

    def get(self, ref):
        self.root.transaction_reads.append(ref.path)
        return FakeSnapshot(ref, self.root.documents.get(ref.path))

    def delete(self, ref):
        self.pending.append(("delete", ref, None, False))

    def set(self, ref, data, merge=False):
        if self.root.fail_audit_write and "actionAudit" in ref.path:
            raise RuntimeError("audit write failed")
        self.pending.append(("set", ref, dict(data), merge))

    def commit(self):
        if not self.pending:
            return
        for operation, ref, data, merge in self.pending:
            if operation == "delete":
                self.root.documents.pop(ref.path, None)
                continue
            previous = self.root.documents.get(ref.path, {}) if merge else {}
            self.root.documents[ref.path] = {**previous, **data}
        self.root.committed_transactions += 1


class FakeFirestore:
    def __init__(self):
        self.documents = {}
        self.direct_reads = []
        self.transaction_reads = []
        self.committed_transactions = 0
        self.fail_audit_write = False
        self.after_direct_read = None

    def collection(self, name):
        return FakeNode(self, ("collection", name))

    def transaction(self):
        return FakeTransaction(self)


def fake_transactional(callback):
    def run(transaction):
        result = callback(transaction)
        transaction.commit()
        return result

    return run


def _outbox_path(uid="uid-1", outbox_id="outbox-1"):
    return _path(
        "collection", "users", "document", uid,
        "collection", "outbox", "document", outbox_id,
    )


def _audit_path(uid="uid-1", audit_id="audit-1"):
    return _path(
        "collection", "users", "document", uid,
        "collection", "actionAudit", "document", audit_id,
    )


def _manual_item(**overrides):
    data = {
        "source": "dashboard_inline_reply",
        "actionType": "reply",
        "actionAuditId": "audit-1",
        "clientId": "client-1",
        "notificationId": "notification-1",
        "threadId": "thread-1",
        "replyToMessageId": "message-1",
    }
    data.update(overrides)
    return data


def _queued_audit(**overrides):
    data = {
        "status": "queued",
        "actorUid": "uid-1",
        "source": "dashboard_inline_reply",
        "actionType": "reply",
        "clientId": "client-1",
        "notificationId": "notification-1",
        "threadId": "thread-1",
    }
    data.update(overrides)
    return data


class ProcessOutboxMainScopeTests(unittest.TestCase):
    def test_main_hands_only_the_exact_identifiers_to_email_entrypoint(self):
        processor = getattr(main, "process_outbox_item", None)
        self.assertTrue(callable(processor), "main.process_outbox_item is missing")

        downstream = {
            "status": "manual_ready",
            "uid": "must-not-escape",
            "outboxId": "must-not-escape",
            "internal": {"must": "not escape"},
        }
        with patch.object(
            main,
            "process_exact_outbox_item",
            return_value=downstream,
            create=True,
        ) as process_exact, \
                patch.object(main, "send_outboxes") as send_outboxes, \
                patch.object(main, "scan_inbox_against_index") as scan_inbox, \
                patch.object(main, "scan_sent_items_for_manual_replies") as scan_sent, \
                patch.object(main, "process_pending_responses") as pending, \
                patch.object(main, "check_and_send_followups") as followups, \
                patch.object(main, "download_token") as download_token, \
                patch.object(main, "ConfidentialClientApplication") as msal:
            result = processor("uid-1", "outbox-1")

        self.assertEqual({"status": "manual_ready"}, result)
        process_exact.assert_called_once_with("uid-1", "outbox-1")
        send_outboxes.assert_not_called()
        scan_inbox.assert_not_called()
        scan_sent.assert_not_called()
        pending.assert_not_called()
        followups.assert_not_called()
        download_token.assert_not_called()
        msal.assert_not_called()


class ProcessExactOutboxEmailTests(unittest.TestCase):
    def _processor(self):
        processor = getattr(email_module, "process_outbox_item", None)
        self.assertTrue(callable(processor), "email.process_outbox_item is missing")
        return processor

    def test_manual_item_is_handoff_ready_without_sender_or_collection_scan(self):
        fake_fs = FakeFirestore()
        fake_fs.documents[_outbox_path()] = _manual_item()

        with patch("email_automation.clients._fs", fake_fs):
            result = self._processor()("uid-1", "outbox-1")

        self.assertEqual(
            {"status": "manual_ready"},
            result,
        )
        self.assertEqual([_outbox_path()], fake_fs.direct_reads)
        self.assertEqual([], fake_fs.transaction_reads)
        self.assertEqual(0, fake_fs.committed_transactions)

    def test_non_manual_item_fails_closed_without_mutation(self):
        fake_fs = FakeFirestore()
        fake_fs.documents[_outbox_path()] = {
            "source": "dashboard_new_campaign",
            "actionAuditId": "audit-1",
        }

        with patch("email_automation.clients._fs", fake_fs):
            result = self._processor()("uid-1", "outbox-1")

        self.assertEqual(
            {"status": "blocked_non_manual"},
            result,
        )
        self.assertIn(_outbox_path(), fake_fs.documents)
        self.assertEqual([_outbox_path()], fake_fs.direct_reads)
        self.assertEqual(0, fake_fs.committed_transactions)

    def test_manual_ready_requires_exact_deployed_source_and_action_type(self):
        invalid_shapes = [
            _manual_item(source="dashboard_manual_reply"),
            _manual_item(source="Dashboard_inline_reply"),
            _manual_item(source="dashboard_inline_reply "),
            _manual_item(actionType="tour_reply"),
            _manual_item(actionType="Reply"),
            _manual_item(actionType=" reply"),
            _manual_item(source="dashboard_inline_reply", actionType=""),
            _manual_item(source="", actionType="reply"),
        ]

        for data in invalid_shapes:
            with self.subTest(source=data.get("source"), action_type=data.get("actionType")):
                fake_fs = FakeFirestore()
                fake_fs.documents[_outbox_path()] = data
                with patch("email_automation.clients._fs", fake_fs):
                    result = self._processor()("uid-1", "outbox-1")
                self.assertEqual({"status": "blocked_non_manual"}, result)
                self.assertIn(_outbox_path(), fake_fs.documents)
                self.assertEqual(0, fake_fs.committed_transactions)

    def test_missing_exact_item_is_not_replaced_by_an_outbox_scan(self):
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs):
            result = self._processor()("uid-1", "missing")

        self.assertEqual(
            {"status": "not_found"},
            result,
        )
        self.assertEqual([_outbox_path(outbox_id="missing")], fake_fs.direct_reads)

    def test_cancelled_item_deletes_and_terminalizes_linked_audit_atomically(self):
        fake_fs = FakeFirestore()
        fake_fs.documents[_outbox_path()] = _manual_item(
            cancelRequested=True,
            status="cancel_requested",
        )
        fake_fs.documents[_audit_path()] = _queued_audit()

        with patch("email_automation.clients._fs", fake_fs), \
                patch.object(email_module.firestore, "transactional", fake_transactional):
            result = self._processor()("uid-1", "outbox-1")

        self.assertEqual(
            {"status": "cancelled"},
            result,
        )
        self.assertNotIn(_outbox_path(), fake_fs.documents)
        audit = fake_fs.documents[_audit_path()]
        self.assertEqual("cancelled", audit["status"])
        self.assertEqual("outbox-1", audit["outboxId"])
        self.assertEqual("client-1", audit["clientId"])
        self.assertEqual("notification-1", audit["notificationId"])
        self.assertEqual("thread-1", audit["threadId"])
        self.assertEqual(
            [_outbox_path(), _audit_path()],
            fake_fs.transaction_reads,
        )
        self.assertEqual(1, fake_fs.committed_transactions)

    def test_cancel_audit_failure_rolls_back_outbox_delete(self):
        fake_fs = FakeFirestore()
        original_outbox = _manual_item(
            cancelRequested=True,
            status="cancel_requested",
        )
        original_audit = _queued_audit()
        fake_fs.documents[_outbox_path()] = dict(original_outbox)
        fake_fs.documents[_audit_path()] = dict(original_audit)
        fake_fs.fail_audit_write = True

        with patch("email_automation.clients._fs", fake_fs), \
                patch.object(email_module.firestore, "transactional", fake_transactional):
            with self.assertRaisesRegex(RuntimeError, "audit write failed"):
                self._processor()("uid-1", "outbox-1")

        self.assertEqual(original_outbox, fake_fs.documents[_outbox_path()])
        self.assertEqual(original_audit, fake_fs.documents[_audit_path()])
        self.assertEqual(0, fake_fs.committed_transactions)

    def test_cancel_without_linked_action_audit_fails_closed_and_preserves_outbox(self):
        fake_fs = FakeFirestore()
        original_outbox = _manual_item(
            cancelRequested=True,
            status="cancel_requested",
        )
        fake_fs.documents[_outbox_path()] = dict(original_outbox)

        with patch("email_automation.clients._fs", fake_fs), \
                patch.object(email_module.firestore, "transactional", fake_transactional):
            result = self._processor()("uid-1", "outbox-1")

        self.assertEqual({"status": "blocked_missing_action_audit"}, result)
        self.assertEqual(original_outbox, fake_fs.documents[_outbox_path()])
        self.assertEqual(0, fake_fs.committed_transactions)

    def test_cancel_with_invalid_action_audit_id_fails_closed_and_preserves_outbox(self):
        for invalid_id in ("", "   ", "folder/audit"):
            with self.subTest(action_audit_id=invalid_id):
                fake_fs = FakeFirestore()
                original_outbox = _manual_item(
                    actionAuditId=invalid_id,
                    cancelRequested=True,
                    status="cancel_requested",
                )
                fake_fs.documents[_outbox_path()] = dict(original_outbox)

                with patch("email_automation.clients._fs", fake_fs), \
                        patch.object(email_module.firestore, "transactional", fake_transactional):
                    result = self._processor()("uid-1", "outbox-1")

                self.assertEqual({"status": "blocked_invalid_action_audit"}, result)
                self.assertEqual(original_outbox, fake_fs.documents[_outbox_path()])
                self.assertEqual(0, fake_fs.committed_transactions)

    def test_cancel_with_padded_action_audit_id_never_reads_or_mutates_audit(self):
        fake_fs = FakeFirestore()
        original_outbox = _manual_item(
            actionAuditId=" audit-1 ",
            cancelRequested=True,
            status="cancel_requested",
        )
        original_audit = _queued_audit()
        fake_fs.documents[_outbox_path()] = dict(original_outbox)
        fake_fs.documents[_audit_path()] = dict(original_audit)

        with patch("email_automation.clients._fs", fake_fs), \
                patch.object(email_module.firestore, "transactional", fake_transactional):
            result = self._processor()("uid-1", "outbox-1")

        self.assertEqual({"status": "blocked_invalid_action_audit"}, result)
        self.assertEqual(original_outbox, fake_fs.documents[_outbox_path()])
        self.assertEqual(original_audit, fake_fs.documents[_audit_path()])
        self.assertEqual([_outbox_path()], fake_fs.transaction_reads)
        self.assertEqual(0, fake_fs.committed_transactions)

    def test_cancelled_non_manual_item_fails_closed_before_audit_transaction(self):
        fake_fs = FakeFirestore()
        original_outbox = _manual_item(
            source="dashboard_new_campaign",
            actionType="campaign_launch",
            cancelRequested=True,
            status="cancel_requested",
        )
        original_audit = _queued_audit()
        fake_fs.documents[_outbox_path()] = dict(original_outbox)
        fake_fs.documents[_audit_path()] = dict(original_audit)

        with patch("email_automation.clients._fs", fake_fs), \
                patch.object(email_module.firestore, "transactional", fake_transactional):
            result = self._processor()("uid-1", "outbox-1")

        self.assertEqual({"status": "blocked_non_manual"}, result)
        self.assertEqual(original_outbox, fake_fs.documents[_outbox_path()])
        self.assertEqual(original_audit, fake_fs.documents[_audit_path()])
        self.assertEqual([], fake_fs.transaction_reads)
        self.assertEqual(0, fake_fs.committed_transactions)

    def test_cancel_transaction_rechecks_exact_manual_shape_after_outer_read(self):
        fake_fs = FakeFirestore()
        initial_outbox = _manual_item(
            cancelRequested=True,
            status="cancel_requested",
        )
        changed_outbox = {
            **initial_outbox,
            "source": "dashboard_new_campaign",
            "actionType": "campaign_launch",
        }
        original_audit = _queued_audit()
        fake_fs.documents[_outbox_path()] = dict(initial_outbox)
        fake_fs.documents[_audit_path()] = dict(original_audit)

        def change_after_outer_read(store, path):
            if path == _outbox_path():
                store.documents[path] = dict(changed_outbox)

        fake_fs.after_direct_read = change_after_outer_read

        with patch("email_automation.clients._fs", fake_fs), \
                patch.object(email_module.firestore, "transactional", fake_transactional):
            result = self._processor()("uid-1", "outbox-1")

        self.assertEqual({"status": "blocked_non_manual"}, result)
        self.assertEqual(changed_outbox, fake_fs.documents[_outbox_path()])
        self.assertEqual(original_audit, fake_fs.documents[_audit_path()])
        self.assertEqual([_outbox_path()], fake_fs.transaction_reads)
        self.assertEqual(0, fake_fs.committed_transactions)

    def test_cancelled_item_requires_canonical_nonblank_outbox_bindings(self):
        missing = object()
        fields = [
            ("clientId", "client-1", "blocked_invalid_client"),
            ("threadId", "thread-1", "blocked_invalid_thread"),
            ("notificationId", "notification-1", "blocked_invalid_notification"),
        ]
        invalid_values = [
            ("missing", missing),
            ("none", None),
            ("wrong_type", 7),
            ("blank", ""),
            ("whitespace", "   "),
            ("leading_whitespace", " value"),
            ("trailing_whitespace", "value "),
            ("current_path", "."),
            ("parent_path", ".."),
            ("slash", "folder/value"),
            ("control", "value\x00suffix"),
            ("oversize", "x" * 1501),
        ]

        for field, canonical_value, expected_status in fields:
            for variant, invalid_value in invalid_values:
                with self.subTest(field=field, variant=variant):
                    original_outbox = _manual_item(
                        cancelRequested=True,
                        status="cancel_requested",
                    )
                    original_audit = _queued_audit()
                    if invalid_value is missing:
                        original_outbox.pop(field)
                        original_audit.pop(field)
                    else:
                        original_outbox[field] = invalid_value
                        original_audit[field] = invalid_value

                    self.assertNotEqual(canonical_value, original_outbox.get(field))
                    fake_fs = FakeFirestore()
                    fake_fs.documents[_outbox_path()] = dict(original_outbox)
                    fake_fs.documents[_audit_path()] = dict(original_audit)

                    with patch("email_automation.clients._fs", fake_fs), \
                            patch.object(
                                email_module.firestore,
                                "transactional",
                                fake_transactional,
                            ):
                        result = self._processor()("uid-1", "outbox-1")

                    self.assertEqual({"status": expected_status}, result)
                    self.assertEqual(original_outbox, fake_fs.documents[_outbox_path()])
                    self.assertEqual(original_audit, fake_fs.documents[_audit_path()])
                    self.assertEqual([_outbox_path()], fake_fs.transaction_reads)
                    self.assertEqual(0, fake_fs.committed_transactions)

    def test_cancelled_item_requires_exact_queued_audit_binding(self):
        mismatches = [
            ("status", {"status": "sent"}, "blocked_audit_status"),
            ("actor", {"actorUid": "other-user"}, "blocked_audit_actor"),
            ("source", {"source": "dashboard_inline_reply "}, "blocked_audit_source"),
            ("action", {"actionType": "Reply"}, "blocked_audit_action_type"),
            ("client", {"clientId": "other-client"}, "blocked_audit_client"),
            ("thread", {"threadId": "other-thread"}, "blocked_audit_thread"),
            (
                "notification",
                {"notificationId": "other-notification"},
                "blocked_audit_notification",
            ),
            ("outbox", {"outboxId": "other-outbox"}, "blocked_audit_outbox"),
        ]

        for label, audit_override, expected_status in mismatches:
            with self.subTest(binding=label):
                fake_fs = FakeFirestore()
                original_outbox = _manual_item(
                    cancelRequested=True,
                    status="cancel_requested",
                )
                original_audit = _queued_audit(**audit_override)
                fake_fs.documents[_outbox_path()] = dict(original_outbox)
                fake_fs.documents[_audit_path()] = dict(original_audit)

                with patch("email_automation.clients._fs", fake_fs), \
                        patch.object(email_module.firestore, "transactional", fake_transactional):
                    result = self._processor()("uid-1", "outbox-1")

                self.assertEqual({"status": expected_status}, result)
                self.assertEqual(original_outbox, fake_fs.documents[_outbox_path()])
                self.assertEqual(original_audit, fake_fs.documents[_audit_path()])
                self.assertEqual(0, fake_fs.committed_transactions)


if __name__ == "__main__":
    unittest.main()
