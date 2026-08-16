import copy
import hashlib
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "service-account.json",
    ),
)

from email_automation import manual_reply, notifications


UID = "synthetic-user"
CLIENT_ID = "client-1"
THREAD_ID = "thread-1"
NOTIFICATION_ID = hashlib.sha1(b"manual-authority:test").hexdigest()
AUTHORITY_DOMAIN = "sitesift-manual-reply-authority:v1"
RESOLVED_COMMIT_TIME = datetime(2026, 8, 15, 12, 34, 56, tzinfo=timezone.utc)


def _path(*parts):
    return tuple(parts)


def _length_framed_sha256(*members):
    digest = hashlib.sha256()
    for member in members:
        encoded = member.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _expected_authority_key(
    uid=UID,
    client_id=CLIENT_ID,
    notification_id=NOTIFICATION_ID,
):
    return _length_framed_sha256(
        AUTHORITY_DOMAIN,
        uid,
        client_id,
        notification_id,
    )


def _notification_path(notification_id=NOTIFICATION_ID):
    return _path(
        "users",
        UID,
        "clients",
        CLIENT_ID,
        "notifications",
        notification_id,
    )


def _authority_path(notification_id=NOTIFICATION_ID):
    return _path(
        "users",
        UID,
        "manualReplyAuthorities",
        _expected_authority_key(notification_id=notification_id),
    )


def _client_path():
    return _path("users", UID, "clients", CLIENT_ID)


class _Snapshot:
    def __init__(self, data):
        self.exists = data is not None
        self._data = copy.deepcopy(data)

    def to_dict(self):
        return copy.deepcopy(self._data)


class _DocumentReference:
    def __init__(self, root, path):
        self.root = root
        self.path = tuple(path)

    @property
    def id(self):
        return self.path[-1]

    def collection(self, name):
        return _CollectionReference(self.root, self.path + (name,))

    def get(self, transaction=None):
        self.root.reads.append(self.path)
        return _Snapshot(self.root.documents.get(self.path))


class _CollectionReference:
    def __init__(self, root, path):
        self.root = root
        self.path = tuple(path)

    def document(self, document_id=None):
        if document_id is None:
            document_id = f"auto-{self.root.next_auto_id}"
            self.root.next_auto_id += 1
        return _DocumentReference(self.root, self.path + (document_id,))


class _Transaction:
    def __init__(self, root, *, speculative=False):
        self.root = root
        self.speculative = speculative
        self.operations = []

    def set(self, ref, data, merge=False):
        self.operations.append(("set", ref.path, copy.deepcopy(data), bool(merge)))

    def delete(self, ref):
        self.operations.append(("delete", ref.path, None, False))

    def commit(self):
        if self.speculative:
            raise AssertionError("a speculative transaction must never commit")

        next_documents = copy.deepcopy(self.root.documents)
        for operation, path, data, merge in self.operations:
            if operation == "delete":
                next_documents.pop(path, None)
                continue
            if merge:
                current = dict(next_documents.get(path) or {})
                current.update(data)
                next_documents[path] = current
            else:
                next_documents[path] = data

        self.root.documents = next_documents
        if self.operations:
            self.root.write_commits.append(copy.deepcopy(self.operations))


class _MemoryFirestore:
    def __init__(self, documents=None, *, retry_once=False):
        self.documents = copy.deepcopy(documents or {})
        self.retry_once = retry_once
        self.next_auto_id = 1
        self.reads = []
        self.callback_runs = 0
        self.write_commits = []

    def collection(self, name):
        return _CollectionReference(self, (name,))

    def transaction(self):
        return _Transaction(self)


def _fake_transactional(callback):
    def run(transaction, *args, **kwargs):
        root = transaction.root
        if root.retry_once:
            root.callback_runs += 1
            callback(
                _Transaction(root, speculative=True),
                *args,
                **kwargs,
            )
        root.callback_runs += 1
        result = callback(transaction, *args, **kwargs)
        transaction.commit()
        return result

    return run


def _source_type():
    source_type = getattr(notifications, "ManualReplySource", None)
    if source_type is None:
        raise AssertionError(
            "notifications.ManualReplySource must be the explicit typed authority input"
        )
    return source_type


def _manual_reply_source(**overrides):
    values = {
        "graph_lookup_message_id": "source-alias-1",
        # Notification-time authority may precede Graph ImmutableId
        # canonicalization. The lookup ID + normalized Internet Message ID are
        # the trusted pair that the send lane must later resolve and re-check.
        "immutable_graph_message_id": None,
        "internet_message_id": "<Source-1@Example.INVALID>",
        "conversation_id": "conversation-1",
        "authenticated_mailbox_address": "Sender@Example.INVALID",
        "from_addresses": ("Broker@Example.INVALID",),
        "sender_addresses": ("broker@example.invalid",),
        "reply_to_addresses": (),
        "to_addresses": ("sender@example.invalid",),
        "cc_addresses": (),
        "bcc_addresses": (),
    }
    values.update(overrides)
    return _source_type()(**values)


def _meta(**overrides):
    value = {
        "reason": "needs_user_input:client_question",
        "replyToMessageId": "source-alias-1",
        "sourceMessageId": "source-alias-1",
        "sourceGraphMessageId": "source-alias-1",
        "sourceInternetMessageId": "<Source-1@Example.INVALID>",
    }
    value.update(overrides)
    return value


_NO_SOURCE_ARGUMENT = object()


def _write(
    store,
    *,
    kind="action_needed",
    email="Broker@Example.INVALID",
    meta=None,
    source_marker=_NO_SOURCE_ARGUMENT,
    dedupe_key="manual-authority:test",
):
    kwargs = {
        "kind": kind,
        "priority": "important",
        "email": email,
        "thread_id": THREAD_ID,
        "row_number": 42,
        "row_anchor": "123 Synthetic Ave",
        "meta": _meta() if meta is None else meta,
        "dedupe_key": dedupe_key,
    }
    if source_marker is not _NO_SOURCE_ARGUMENT:
        kwargs["manual_reply_source"] = source_marker

    with patch.object(notifications, "_fs", store), patch.object(
        notifications.firestore,
        "transactional",
        _fake_transactional,
    ):
        return notifications.write_notification(
            UID,
            CLIENT_ID,
            **kwargs,
        )


def _expected_notification_doc(*, created_at=notifications.SERVER_TIMESTAMP, meta=None):
    return {
        "kind": "action_needed",
        "priority": "important",
        "email": "Broker@Example.INVALID",
        "threadId": THREAD_ID,
        "rowNumber": 42,
        "rowAnchor": "123 Synthetic Ave",
        "createdAt": created_at,
        "meta": _meta() if meta is None else meta,
        "dedupeKey": "manual-authority:test",
        "manualReplyAuthorityKey": _expected_authority_key(),
    }


def _expected_authority_doc(
    *,
    created_at=notifications.SERVER_TIMESTAMP,
    updated_at=notifications.SERVER_TIMESTAMP,
):
    return {
        "schemaVersion": 1,
        "status": "eligible",
        "uid": UID,
        "clientId": CLIENT_ID,
        "threadId": THREAD_ID,
        "notificationId": NOTIFICATION_ID,
        "source": "dashboard_inline_reply",
        "graphLookupMessageId": "source-alias-1",
        "normalizedInternetMessageId": "<source-1@example.invalid>",
        "conversationId": "conversation-1",
        "authenticatedMailboxAddress": "sender@example.invalid",
        "fromAddress": "broker@example.invalid",
        "senderAddress": "broker@example.invalid",
        "sourceAudience": {
            "to": ["sender@example.invalid"],
            "cc": [],
            "bcc": [],
            "replyTo": [],
        },
        "audience": {
            "to": ["broker@example.invalid"],
            "cc": [],
            "bcc": [],
        },
        "createdAt": created_at,
        "updatedAt": updated_at,
    }


class ManualReplyAuthorityNotificationTests(unittest.TestCase):
    def test_authority_key_is_length_framed_and_shared_with_manual_reply(self):
        key_function = getattr(manual_reply, "manual_reply_authority_key", None)
        self.assertIsNotNone(
            key_function,
            "manual_reply.manual_reply_authority_key is the one canonical key function",
        )
        expected = _expected_authority_key()
        actual = key_function(
            uid=UID,
            client_id=CLIENT_ID,
            notification_id=NOTIFICATION_ID,
        )

        self.assertEqual(expected, actual)
        self.assertNotEqual(hashlib.sha256(NOTIFICATION_ID.encode()).hexdigest(), actual)
        self.assertNotEqual(
            actual,
            key_function(
                uid=UID,
                client_id=f"{CLIENT_ID}:notification-1",
                notification_id="suffix",
            ),
        )

    def test_eligible_action_notification_and_authority_share_one_commit(self):
        store = _MemoryFirestore({_client_path(): {}})

        created_id = _write(store, source_marker=_manual_reply_source())

        self.assertEqual(NOTIFICATION_ID, created_id)
        self.assertEqual(_expected_notification_doc(), store.documents[_notification_path()])
        self.assertEqual(_expected_authority_doc(), store.documents[_authority_path()])
        self.assertEqual(
            {
                "notificationsUnread": 1,
                "newUpdateCount": 0,
                "notifCounts": {"action_needed": 1},
            },
            store.documents[_client_path()],
        )
        self.assertEqual(1, len(store.write_commits))
        self.assertEqual(
            {_notification_path(), _authority_path(), _client_path()},
            {operation[1] for operation in store.write_commits[0]},
        )

    def test_initial_authority_has_no_future_outbox_audit_body_or_claim_fields(self):
        store = _MemoryFirestore({_client_path(): {}})
        _write(store, source_marker=_manual_reply_source())
        authority = store.documents[_authority_path()]

        forbidden = {
            "outboxId",
            "ownerOutboxId",
            "auditId",
            "actionAuditId",
            "body",
            "reviewedBody",
            "reviewedBodyHash",
            "snapshotHash",
            "fence",
            "claimedAt",
        }
        self.assertTrue(forbidden.isdisjoint(authority))
        self.assertEqual("eligible", authority["status"])

    def test_meta_cannot_bootstrap_authority_without_typed_source(self):
        store = _MemoryFirestore({_client_path(): {}})
        _write(
            store,
            source_marker=_NO_SOURCE_ARGUMENT,
            meta={
                **_meta(),
                "immutableGraphMessageId": "source-immutable-1",
                "conversationId": "conversation-1",
                "fromAddress": "broker@example.invalid",
                "senderAddress": "broker@example.invalid",
                "audience": {
                    "to": ["broker@example.invalid"],
                    "cc": [],
                    "bcc": [],
                },
            },
        )

        self.assertIn(_notification_path(), store.documents)
        self.assertNotIn(_authority_path(), store.documents)
        self.assertNotIn(
            "manualReplyAuthorityKey",
            store.documents[_notification_path()],
        )

    def test_meta_cannot_choose_the_server_authority_marker(self):
        attacker_marker = "attacker-selected-authority"
        poisoned_meta = {
            **_meta(),
            "manualReplyAuthorityKey": attacker_marker,
        }
        store = _MemoryFirestore({_client_path(): {}})

        _write(
            store,
            meta=poisoned_meta,
            source_marker=_manual_reply_source(),
        )

        notification = store.documents[_notification_path()]
        self.assertEqual(
            _expected_authority_key(),
            notification["manualReplyAuthorityKey"],
        )
        self.assertNotEqual(
            attacker_marker,
            notification["manualReplyAuthorityKey"],
        )
        self.assertEqual(attacker_marker, notification["meta"]["manualReplyAuthorityKey"])

    def test_untyped_source_dict_is_rejected_before_any_write(self):
        store = _MemoryFirestore({_client_path(): {}})
        with self.assertRaises(TypeError):
            _write(
                store,
                source_marker={
                    "graph_lookup_message_id": "source-alias-1",
                    "internet_message_id": "<source-1@example.invalid>",
                },
            )

        self.assertEqual({_client_path(): {}}, store.documents)
        self.assertEqual([], store.write_commits)

    def test_typed_authority_requires_a_deterministic_dedupe_key(self):
        store = _MemoryFirestore({_client_path(): {}})

        with self.assertRaisesRegex(
            ValueError,
            "manual_reply_source requires dedupe_key",
        ):
            _write(
                store,
                source_marker=_manual_reply_source(),
                dedupe_key=None,
            )

        self.assertEqual({_client_path(): {}}, store.documents)
        self.assertEqual([], store.reads)
        self.assertEqual([], store.write_commits)

    def test_nonqualifying_typed_source_without_dedupe_remains_notification_only(self):
        store = _MemoryFirestore({_client_path(): {}})

        try:
            created_id = _write(
                store,
                kind="property_unavailable",
                source_marker=_manual_reply_source(),
                dedupe_key=None,
            )
        except ValueError as error:
            self.fail(f"nonqualifying source changed legacy behavior: {error}")

        self.assertEqual("auto-1", created_id)
        self.assertFalse(
            any("manualReplyAuthorities" in path for path in store.documents),
        )
        self.assertEqual(1, store.documents[_client_path()]["notificationsUnread"])

    def test_nonqualifying_or_ambiguous_paths_create_notification_only(self):
        cases = {
            "non_action_kind": {
                "kind": "property_unavailable",
                "source": _manual_reply_source(),
            },
            "non_sendable_action_reason": {
                "source": _manual_reply_source(),
                "meta": _meta(reason="contact_optout:do_not_contact"),
            },
            "missing_source": {
                "source": _NO_SOURCE_ARGUMENT,
            },
            "missing_source_identity": {
                "source": _manual_reply_source(conversation_id=""),
            },
            "multiple_from": {
                "source": _manual_reply_source(
                    from_addresses=(
                        "broker@example.invalid",
                        "second@example.invalid",
                    )
                ),
            },
            "multiple_sender": {
                "source": _manual_reply_source(
                    sender_addresses=(
                        "broker@example.invalid",
                        "second@example.invalid",
                    )
                ),
            },
            "cc_present": {
                "source": _manual_reply_source(
                    cc_addresses=("observer@example.invalid",)
                ),
            },
            "bcc_present": {
                "source": _manual_reply_source(
                    bcc_addresses=("observer@example.invalid",)
                ),
            },
            "alternate_reply_to": {
                "source": _manual_reply_source(
                    reply_to_addresses=("alternate@example.invalid",)
                ),
            },
            "alternate_notification_recipient": {
                "source": _manual_reply_source(),
                "email": "alternate@example.invalid",
            },
            "source_to_not_authenticated_mailbox": {
                "source": _manual_reply_source(
                    to_addresses=("other-mailbox@example.invalid",)
                ),
            },
            "multiple_source_to": {
                "source": _manual_reply_source(
                    to_addresses=(
                        "sender@example.invalid",
                        "other-mailbox@example.invalid",
                    )
                ),
            },
            "from_sender_mismatch": {
                "source": _manual_reply_source(
                    sender_addresses=("different-sender@example.invalid",)
                ),
            },
        }

        for label, case in cases.items():
            with self.subTest(label=label):
                store = _MemoryFirestore({_client_path(): {}})
                _write(
                    store,
                    kind=case.get("kind", "action_needed"),
                    email=case.get("email", "Broker@Example.INVALID"),
                    meta=case.get("meta"),
                    source_marker=case["source"],
                )
                self.assertIn(_notification_path(), store.documents)
                self.assertFalse(
                    any("manualReplyAuthorities" in path for path in store.documents),
                )
                self.assertNotIn(
                    "manualReplyAuthorityKey",
                    store.documents[_notification_path()],
                )

    def test_transaction_retry_commits_one_pair_and_one_counter_increment(self):
        store = _MemoryFirestore({_client_path(): {}}, retry_once=True)

        _write(store, source_marker=_manual_reply_source())

        self.assertEqual(2, store.callback_runs)
        self.assertEqual(1, len(store.write_commits))
        self.assertIn(_notification_path(), store.documents)
        self.assertIn(_authority_path(), store.documents)
        self.assertEqual(1, store.documents[_client_path()]["notificationsUnread"])
        self.assertEqual(
            1,
            store.documents[_client_path()]["notifCounts"]["action_needed"],
        )

    def test_exact_existing_pair_is_an_idempotent_no_op_that_reads_both(self):
        store = _MemoryFirestore(
            {
                _client_path(): {
                    "notificationsUnread": 1,
                    "newUpdateCount": 0,
                    "notifCounts": {"action_needed": 1},
                },
                _notification_path(): _expected_notification_doc(
                    created_at=RESOLVED_COMMIT_TIME,
                ),
                _authority_path(): _expected_authority_doc(
                    created_at=RESOLVED_COMMIT_TIME,
                    updated_at=RESOLVED_COMMIT_TIME,
                ),
            }
        )

        created_id = _write(store, source_marker=_manual_reply_source())

        self.assertEqual(NOTIFICATION_ID, created_id)
        self.assertIn(_notification_path(), store.reads)
        self.assertIn(_authority_path(), store.reads)
        self.assertEqual([], store.write_commits)
        self.assertEqual(1, store.documents[_client_path()]["notificationsUnread"])

    def test_existing_partial_or_mismatched_pair_is_a_conflict(self):
        exact_notification = _expected_notification_doc(
            created_at=RESOLVED_COMMIT_TIME,
        )
        exact_authority = _expected_authority_doc(
            created_at=RESOLVED_COMMIT_TIME,
            updated_at=RESOLVED_COMMIT_TIME,
        )
        cases = {
            "notification_without_authority": {
                _notification_path(): exact_notification,
            },
            "authority_without_notification": {
                _authority_path(): exact_authority,
            },
            "notification_drift": {
                _notification_path(): {
                    **exact_notification,
                    "email": "alternate@example.invalid",
                },
                _authority_path(): exact_authority,
            },
            "authority_drift": {
                _notification_path(): exact_notification,
                _authority_path(): {
                    **exact_authority,
                    "conversationId": "different-conversation",
                },
            },
        }

        for label, documents in cases.items():
            with self.subTest(label=label):
                store = _MemoryFirestore(
                    {
                        _client_path(): {
                            "notificationsUnread": 1,
                            "newUpdateCount": 0,
                            "notifCounts": {"action_needed": 1},
                        },
                        **documents,
                    }
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "manual reply authority conflict",
                ):
                    _write(store, source_marker=_manual_reply_source())
                self.assertEqual([], store.write_commits)

    def test_existing_pair_requires_the_exact_server_authority_marker(self):
        exact_notification = _expected_notification_doc(
            created_at=RESOLVED_COMMIT_TIME,
        )
        exact_authority = _expected_authority_doc(
            created_at=RESOLVED_COMMIT_TIME,
            updated_at=RESOLVED_COMMIT_TIME,
        )
        cases = {
            "missing": {
                key: value
                for key, value in exact_notification.items()
                if key != "manualReplyAuthorityKey"
            },
            "wrong": {
                **exact_notification,
                "manualReplyAuthorityKey": "wrong-authority-key",
            },
        }

        for label, notification in cases.items():
            with self.subTest(label=label):
                store = _MemoryFirestore(
                    {
                        _client_path(): {
                            "notificationsUnread": 1,
                            "newUpdateCount": 0,
                            "notifCounts": {"action_needed": 1},
                        },
                        _notification_path(): notification,
                        _authority_path(): exact_authority,
                    }
                )

                with self.assertRaisesRegex(
                    ValueError,
                    "manual reply authority conflict",
                ):
                    _write(store, source_marker=_manual_reply_source())
                self.assertEqual([], store.write_commits)

    def test_existing_pair_rejects_non_datetime_or_naive_timestamps(self):
        exact_notification = _expected_notification_doc(
            created_at=RESOLVED_COMMIT_TIME,
        )
        exact_authority = _expected_authority_doc(
            created_at=RESOLVED_COMMIT_TIME,
            updated_at=RESOLVED_COMMIT_TIME,
        )
        cases = {
            "notification_unresolved_sentinel": (
                {**exact_notification, "createdAt": notifications.SERVER_TIMESTAMP},
                exact_authority,
            ),
            "notification_string": (
                {**exact_notification, "createdAt": "2026-08-15T12:34:56Z"},
                exact_authority,
            ),
            "notification_naive": (
                {**exact_notification, "createdAt": RESOLVED_COMMIT_TIME.replace(tzinfo=None)},
                exact_authority,
            ),
            "authority_created_string": (
                exact_notification,
                {**exact_authority, "createdAt": "2026-08-15T12:34:56Z"},
            ),
            "authority_updated_naive": (
                exact_notification,
                {
                    **exact_authority,
                    "updatedAt": RESOLVED_COMMIT_TIME.replace(tzinfo=None),
                },
            ),
        }

        for label, (notification, authority) in cases.items():
            with self.subTest(label=label):
                store = _MemoryFirestore(
                    {
                        _client_path(): {
                            "notificationsUnread": 1,
                            "newUpdateCount": 0,
                            "notifCounts": {"action_needed": 1},
                        },
                        _notification_path(): notification,
                        _authority_path(): authority,
                    }
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "manual reply authority conflict",
                ):
                    _write(store, source_marker=_manual_reply_source())
                self.assertEqual([], store.write_commits)

    def test_existing_pair_requires_one_shared_commit_timestamp(self):
        exact_notification = _expected_notification_doc(
            created_at=RESOLVED_COMMIT_TIME,
        )
        exact_authority = _expected_authority_doc(
            created_at=RESOLVED_COMMIT_TIME,
            updated_at=RESOLVED_COMMIT_TIME,
        )
        later = RESOLVED_COMMIT_TIME + timedelta(microseconds=1)
        cases = {
            "authority_created_at_drift": {
                **exact_authority,
                "createdAt": later,
            },
            "authority_updated_at_drift": {
                **exact_authority,
                "updatedAt": later,
            },
        }

        for label, authority in cases.items():
            with self.subTest(label=label):
                store = _MemoryFirestore(
                    {
                        _client_path(): {
                            "notificationsUnread": 1,
                            "newUpdateCount": 0,
                            "notifCounts": {"action_needed": 1},
                        },
                        _notification_path(): exact_notification,
                        _authority_path(): authority,
                    }
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "manual reply authority conflict",
                ):
                    _write(store, source_marker=_manual_reply_source())
                self.assertEqual([], store.write_commits)

    def test_legacy_notification_call_remains_unchanged(self):
        store = _MemoryFirestore({_client_path(): {}})

        created_id = _write(
            store,
            kind="sheet_update",
            source_marker=_NO_SOURCE_ARGUMENT,
        )

        self.assertEqual(NOTIFICATION_ID, created_id)
        self.assertIn(_notification_path(), store.documents)
        self.assertFalse(
            any("manualReplyAuthorities" in path for path in store.documents),
        )
        self.assertNotIn(
            "manualReplyAuthorityKey",
            store.documents[_notification_path()],
        )
        self.assertEqual(1, store.documents[_client_path()]["notificationsUnread"])
        self.assertEqual(1, store.documents[_client_path()]["newUpdateCount"])
        self.assertEqual(
            {"sheet_update": 1},
            store.documents[_client_path()]["notifCounts"],
        )


if __name__ == "__main__":
    unittest.main()
