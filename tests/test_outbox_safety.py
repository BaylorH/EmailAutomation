import unittest
import os
from unittest.mock import MagicMock, patch
from contextlib import ExitStack

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import email as email_module
from email_automation import notifications as notifications_module
from email_automation.column_config import get_default_column_config


class FakeDocRef:
    def __init__(self, doc_id="outbox-1", data=None):
        self.id = doc_id
        self.deleted = False
        self.set_calls = []
        self.update_calls = []
        self._data = data if data is not None else {}

    def get(self, transaction=None):
        return FakeSnapshot(dict(self._data), exists=not self.deleted)

    def delete(self):
        self.deleted = True

    def set(self, *args, **kwargs):
        self.set_calls.append((args, kwargs))
        if args:
            payload = dict(args[0] or {})
            if kwargs.get("merge"):
                self._data.update(payload)
            else:
                self._data.clear()
                self._data.update(payload)

    def update(self, data):
        self.update_calls.append(data)


class FakeDoc:
    def __init__(self, data, doc_id="outbox-1"):
        self.id = doc_id
        self._data = data
        self.reference = FakeDocRef(doc_id, dict(data))

    def to_dict(self):
        return self._data


class FakeFirestoreNode:
    def __init__(self, root, path=None):
        self.root = root
        self.path = path or []

    def collection(self, name):
        return FakeFirestoreNode(self.root, self.path + ["collection", name])

    def document(self, name):
        return FakeFirestoreNode(self.root, self.path + ["document", name])

    def delete(self):
        self.root.deleted_paths.append(tuple(self.path))
        key = "/".join(self.path[1::2])
        self.root.snapshots[key] = FakeSnapshot({}, exists=False)

    def set(self, data, merge=False):
        error = self.root.set_errors.get("/".join(self.path[1::2]))
        if error:
            raise error
        self.root.set_calls.append((tuple(self.path), data, merge))
        key = "/".join(self.path[1::2])
        current = self.root.snapshots.get(key, FakeSnapshot({}, exists=False))
        current_data = dict(current.to_dict() or {}) if current.exists else {}
        next_data = {**current_data, **data} if merge else dict(data)
        self.root.snapshots[key] = FakeSnapshot(next_data)

    def add(self, data):
        self.root.add_calls.append((tuple(self.path), data))
        return FakeFirestoreNode(self.root, self.path + ["document", "auto-id"])

    def order_by(self, _field):
        return self

    def stream(self):
        key = "/".join(self.path[1::2])
        if key.endswith("/outbox"):
            return list(self.root.outbox_docs)
        return []

    def get(self, transaction=None):
        key = "/".join(self.path[1::2])
        if "/archivedClients/" in f"/{key}/":
            return FakeSnapshot({}, exists=False)
        key_parts = key.split("/")
        if len(key_parts) == 2 and key_parts[0] == "users":
            return self.root.snapshots.get(
                key,
                FakeSnapshot({"email": "baylor.freelance@outlook.com"}),
            )
        if len(key_parts) == 4 and key_parts[2] == "clients":
            return self.root.snapshots.get(key, FakeSnapshot({"status": "live"}))
        return self.root.snapshots.get(key, FakeSnapshot({}, exists=False))


class FakeFirestoreTransaction:
    def __init__(self):
        self._max_attempts = 1
        self._read_only = False
        self._id = b"fake-transaction"

    def _clean_up(self):
        return None

    def _begin(self, retry_id=None):
        self._id = retry_id or b"fake-transaction"

    def _commit(self):
        return None

    def _rollback(self):
        return None

    def set(self, ref, data, merge=False):
        ref.set(data, merge=merge)

    def create(self, ref, data):
        snapshot = ref.get()
        if getattr(snapshot, "exists", False):
            raise AssertionError("document already exists")
        ref.set(data, merge=False)

    def update(self, ref, data):
        ref.update(data)

    def delete(self, ref):
        ref.delete()


class FakeFirestore:
    def __init__(self):
        self.deleted_paths = []
        self.set_calls = []
        self.add_calls = []
        self.set_errors = {}
        self.outbox_docs = []
        # "users/uid-1/threads/thread-1" -> FakeSnapshot; consulted by node.get()
        self.snapshots = {}

    def collection(self, name):
        return FakeFirestoreNode(self, ["collection", name])

    def transaction(self):
        return FakeFirestoreTransaction()


def _seed_open_thread(fake_fs, user_id="uid-1", thread_id="thread-1",
                      client_id="client-1", status="paused", row_number=20,
                      message_id="graph-message-1"):
    """Seed a server-side thread + recorded reply-target message so the
    client-supplied thread binding on outbox docs passes pre-send validation."""
    fake_fs.snapshots[f"users/{user_id}/threads/{thread_id}"] = FakeSnapshot({
        "clientId": client_id,
        "status": status,
        "rowNumber": row_number,
    })
    fake_fs.snapshots[
        f"users/{user_id}/threads/{thread_id}/messages/{message_id}"
    ] = FakeSnapshot({"direction": "inbound"})


def _seed_tour_notification(fake_fs, *, user_id="uid-1", client_id="client-1",
                            notification_id="notification-1", thread_id="thread-1",
                            email="bp21harrison@gmail.com",
                            reply_to_message_id="graph-message-1",
                            reason="tour_requested"):
    fake_fs.snapshots[
        f"users/{user_id}/clients/{client_id}/notifications/{notification_id}"
    ] = FakeSnapshot({
        "kind": "action_needed",
        "email": email,
        "threadId": thread_id,
        "meta": {
            "reason": reason,
            "replyToMessageId": reply_to_message_id,
        },
    })


def _action_audit_payload(fake_fs, user_id, audit_id):
    audit_path = (
        "collection", "users", "document", user_id,
        "collection", "actionAudit", "document", audit_id,
    )
    matches = [
        payload for path, payload, _merge in fake_fs.set_calls
        if path == audit_path
    ]
    if len(matches) != 1:
        raise AssertionError(f"Expected one actionAudit write at {audit_path}, found {len(matches)}")
    return matches[0]


class FakeSnapshot:
    def __init__(self, data=None, exists=True):
        self._data = data or {}
        self.exists = exists

    def to_dict(self):
        return self._data


class FakeOutboxCollection:
    def __init__(self, docs):
        self.docs = docs

    def order_by(self, _field):
        return self

    def stream(self):
        return self.docs


from google.cloud.firestore import Increment as _Increment


class FakeCounterDocRef:
    """Backed by a shared dict so atomic increments persist across the loop."""

    def __init__(self, store, key, raise_on_get=False):
        self.store = store
        self.key = key
        self.raise_on_get = raise_on_get

    def get(self):
        if self.raise_on_get:
            raise RuntimeError("firestore sendCounters read failed")
        exists = self.key in self.store
        return FakeSnapshot({"count": self.store.get(self.key, 0)}, exists=exists)

    def set(self, data, merge=False):
        val = data.get("count")
        cur = self.store.get(self.key, 0)
        if isinstance(val, _Increment):
            self.store[self.key] = cur + val.value
        elif val is not None:
            self.store[self.key] = val


class FakeCounterCollection:
    def __init__(self, store, raise_on_get=False):
        self.store = store
        self.raise_on_get = raise_on_get

    def document(self, key):
        return FakeCounterDocRef(self.store, key, raise_on_get=self.raise_on_get)


class FakeHealthDocRef:
    def __init__(self, sink):
        self.sink = sink

    def set(self, data, merge=False):
        self.sink.append((data, merge))


class FakeHealthCollection:
    def __init__(self, sink):
        self.sink = sink

    def document(self, _doc_id):
        return FakeHealthDocRef(self.sink)


class FakeClientCollection:
    def __init__(self, archived=False):
        self.archived = archived

    def document(self, _client_id):
        return type("ClientRef", (), {
            "get": lambda _self: FakeSnapshot(
                {} if self.archived else {"status": "live"},
                exists=not self.archived,
            )
        })()


class FakeUserNode:
    def __init__(self, docs, user_data=None, counter_store=None, health_sink=None,
                 counter_raise_on_get=False):
        self.docs = docs
        self.user_data = user_data or {"email": "baylor.freelance@outlook.com"}
        self.counter_store = counter_store if counter_store is not None else {}
        self.health_sink = health_sink if health_sink is not None else []
        self.counter_raise_on_get = counter_raise_on_get

    def get(self):
        return FakeSnapshot(self.user_data)

    def collection(self, name):
        if name == "outbox":
            return FakeOutboxCollection(self.docs)
        if name == "sendCounters":
            return FakeCounterCollection(
                self.counter_store, raise_on_get=self.counter_raise_on_get
            )
        if name == "systemHealth":
            return FakeHealthCollection(self.health_sink)
        if name == "clients":
            return FakeClientCollection()
        if name == "archivedClients":
            return FakeClientCollection(archived=True)
        raise AssertionError(f"Unexpected user collection: {name}")


class FakeUsersCollection:
    def __init__(self, docs, user_data=None, counter_store=None, health_sink=None,
                 counter_raise_on_get=False):
        self.docs = docs
        self.user_data = user_data
        # Shared mutable state so every users/<uid> lookup sees the same counter.
        self.counter_store = counter_store if counter_store is not None else {}
        self.health_sink = health_sink if health_sink is not None else []
        self.counter_raise_on_get = counter_raise_on_get

    def document(self, _user_id):
        return FakeUserNode(
            self.docs,
            self.user_data,
            counter_store=self.counter_store,
            health_sink=self.health_sink,
            counter_raise_on_get=self.counter_raise_on_get,
        )


class FakeFirestoreWithOutbox:
    def __init__(self, docs, user_data=None, counter_store=None, health_sink=None,
                 counter_raise_on_get=False):
        self.docs = docs
        self.user_data = user_data
        self.counter_store = counter_store if counter_store is not None else {}
        self.health_sink = health_sink if health_sink is not None else []
        self.counter_raise_on_get = counter_raise_on_get

    def collection(self, name):
        if name != "users":
            raise AssertionError(f"Unexpected root collection: {name}")
        return FakeUsersCollection(
            self.docs,
            self.user_data,
            counter_store=self.counter_store,
            health_sink=self.health_sink,
            counter_raise_on_get=self.counter_raise_on_get,
        )


class FakeSheetsRequest:
    def __init__(self, values):
        self.values = values

    def execute(self):
        return {"values": self.values}


class FakeSheetsValues:
    def __init__(self, row_values):
        self.row_values = row_values
        self.ranges = []

    def get(self, **kwargs):
        range_name = kwargs.get("range")
        self.ranges.append((kwargs.get("spreadsheetId"), range_name))
        if isinstance(self.row_values, dict):
            return FakeSheetsRequest([self.row_values.get(range_name, [])])
        return FakeSheetsRequest([self.row_values])


class FakeSheetsSpreadsheets:
    def __init__(self, row_values):
        self.values_api = FakeSheetsValues(row_values)

    def values(self):
        return self.values_api


class FakeSheetsClient:
    def __init__(self, row_values):
        self.spreadsheets_api = FakeSheetsSpreadsheets(row_values)

    def spreadsheets(self):
        return self.spreadsheets_api


class OutboxSafetyTests(unittest.TestCase):
    def test_same_timestamp_campaign_outbox_docs_sort_by_row_number(self):
        created_at = "2026-07-14T14:06:29Z"
        docs = [
            FakeDoc({
                "source": "dashboard_campaign_launch",
                "clientId": "client-1",
                "rowNumber": row_number,
                "createdAt": created_at,
            }, doc_id=doc_id)
            for row_number, doc_id in ((4, "a-doc"), (3, "z-doc"), (5, "m-doc"))
        ]

        ordered = email_module._order_outbox_docs(docs)

        self.assertEqual([3, 4, 5], [doc.to_dict()["rowNumber"] for doc in ordered])

    def setUp(self):
        # These tests isolate outbox behavior. Campaign policy is covered in
        # test_campaign_automation_pause; keep this fixture explicitly active.
        self.client_data = {
            "status": "live",
            "columnConfig": get_default_column_config(),
        }
        self._active_campaign = patch.object(
            email_module,
            "get_client_automation_pause",
            side_effect=lambda *_args: (False, "", self.client_data),
        )
        self._active_campaign.start()

    def tearDown(self):
        self._active_campaign.stop()

    def test_initial_outreach_column_contract_allows_ask_only_launch(self):
        doc_ref = FakeDocRef("ask-only-launch")
        data = {
            "clientId": "client-1",
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
        }

        with patch.object(email_module, "_move_to_dead_letter") as move_to_dead_letter:
            blocked = email_module._dead_letter_invalid_initial_outreach_column_contract_if_needed(
                "uid-1",
                doc_ref,
                data,
                "Could you confirm the asking rent and operating expenses?",
            )

        self.assertFalse(blocked)
        move_to_dead_letter.assert_not_called()

    def test_initial_outreach_column_contract_dead_letters_flyer_note_request(self):
        doc_ref = FakeDocRef("flyer-note-launch")
        data = {
            "clientId": "client-1",
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
        }

        with patch.object(email_module, "_move_to_dead_letter") as move_to_dead_letter:
            blocked = email_module._dead_letter_invalid_initial_outreach_column_contract_if_needed(
                "uid-1",
                doc_ref,
                data,
                "Could you send the flyer or listing link?",
            )

        self.assertTrue(blocked)
        move_to_dead_letter.assert_called_once()
        self.assertIn("non-requestable", move_to_dead_letter.call_args.args[3])
        self.assertIn("manual review", move_to_dead_letter.call_args.args[3])

    def test_initial_outreach_column_contract_dead_letters_malformed_config(self):
        # Do not invent an Ask set for old/incomplete shapes: missing mode data
        # makes Skip semantics unknowable, so the operator must review it.
        self.client_data["columnConfig"] = {"mappings": {}}
        doc_ref = FakeDocRef("malformed-config-launch")
        data = {
            "clientId": "client-1",
            "notificationId": "new-property-notification-1",
            "forceScript": True,
            "scriptSelectionMode": "exact",
            "property": {"address": "100 New Property Way"},
        }

        with patch.object(email_module, "_move_to_dead_letter") as move_to_dead_letter:
            blocked = email_module._dead_letter_invalid_initial_outreach_column_contract_if_needed(
                "uid-1",
                doc_ref,
                data,
                "Could you confirm the asking rent?",
            )

        self.assertTrue(blocked)
        move_to_dead_letter.assert_called_once()
        self.assertIn("invalid persisted columnConfig", move_to_dead_letter.call_args.args[3])

    def test_explicit_initial_outreach_markers_override_stale_thread_metadata(self):
        self.client_data["columnConfig"] = None
        doc_ref = FakeDocRef("explicit-launch-with-stale-thread")
        data = {
            "clientId": "client-1",
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
            "threadId": "stale-thread-1",
            "replyToMessageId": "stale-message-1",
        }

        with patch.object(email_module, "_move_to_dead_letter") as move_to_dead_letter:
            blocked = email_module._dead_letter_invalid_initial_outreach_column_contract_if_needed(
                "uid-1",
                doc_ref,
                data,
                "Could you confirm the asking rent?",
            )

        self.assertTrue(blocked)
        move_to_dead_letter.assert_called_once()
        self.assertIn("invalid persisted columnConfig", move_to_dead_letter.call_args.args[3])

    def test_explicit_initial_outreach_markers_route_away_from_reply_path(self):
        self.assertFalse(email_module._is_outbox_thread_reply({
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
            "threadId": "stale-thread-1",
            "replyToMessageId": "stale-message-1",
        }))
        self.assertTrue(email_module._is_outbox_thread_reply({
            "source": "dashboard",
            "threadId": "thread-1",
            "replyToMessageId": "message-1",
        }))

    def test_initial_outreach_column_contract_does_not_classify_threaded_reply(self):
        self.client_data["columnConfig"] = None
        doc_ref = FakeDocRef("ordinary-dashboard-reply")
        data = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "replyToMessageId": "message-1",
            "source": "dashboard",
        }

        with patch.object(email_module, "_move_to_dead_letter") as move_to_dead_letter:
            blocked = email_module._dead_letter_invalid_initial_outreach_column_contract_if_needed(
                "uid-1",
                doc_ref,
                data,
                "Thanks, here is the flyer you requested.",
            )

        self.assertFalse(blocked)
        move_to_dead_letter.assert_not_called()

    def test_cancel_requested_item_is_deleted_without_sending(self):
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison@gmail.com"],
            "script": "Hi,\n\nPlease disregard.\n\nThanks",
            "clientId": "client-1",
            "subject": "123 Cancel St, Testville",
            "rowNumber": 12,
            "cancelRequested": True,
            "status": "cancel_requested",
        })

        with patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "send_and_index_email") as send_and_index_email, \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)

    def test_exact_or_threaded_dashboard_items_are_not_grouped_with_campaign_outreach(self):
        self.assertTrue(email_module._must_process_outbox_item_individually({
            "threadId": "thread-1",
            "replyToMessageId": "message-1",
            "scriptSelectionMode": "exact",
        }))
        self.assertTrue(email_module._must_process_outbox_item_individually({
            "notificationId": "notification-1",
            "forceScript": True,
        }))
        self.assertTrue(email_module._must_process_outbox_item_individually({
            "source": "dashboard_tour_planner",
        }))
        self.assertTrue(email_module._must_process_outbox_item_individually({
            "actionType": "tour_invite",
        }))
        self.assertFalse(email_module._must_process_outbox_item_individually({
            "assignedEmails": ["bp21harrison@gmail.com"],
            "script": "Campaign first touch",
        }))

    def test_send_outboxes_requests_fresh_headers_for_each_throttled_recipient(self):
        docs = [
            FakeDoc({
                "assignedEmails": ["bp21harrison+one@gmail.com"],
                "script": "Hi Avery",
                "clientId": "client-1",
                "subject": "100 Token Way",
                "rowNumber": 3,
            }, doc_id="outbox-1"),
            FakeDoc({
                "assignedEmails": ["bp21harrison+two@gmail.com"],
                "script": "Hi Blake",
                "clientId": "client-1",
                "subject": "200 Token Way",
                "rowNumber": 4,
            }, doc_id="outbox-2"),
        ]
        provider_calls = []
        send_headers = []

        def headers_provider():
            provider_calls.append(len(provider_calls) + 1)
            return {
                "Authorization": f"Bearer fresh-token-{provider_calls[-1]}",
                "Content-Type": "application/json",
            }

        def record_single_send(_user_id, headers, _item, *_args, **_kwargs):
            send_headers.append(headers["Authorization"])

        with patch("email_automation.clients._fs", FakeFirestoreWithOutbox(docs)), \
             patch.object(email_module, "_send_single_outbox_item", side_effect=record_single_send), \
             patch.object(email_module.time, "sleep", return_value=None) as sleep:
            email_module.send_outboxes(
                "uid-1",
                {"Authorization": "Bearer stale-token"},
                headers_provider=headers_provider,
            )

        self.assertEqual(provider_calls, [1, 2])
        self.assertEqual(send_headers, ["Bearer fresh-token-1", "Bearer fresh-token-2"])
        sleep.assert_called_once_with(120)

    def test_send_outboxes_bounds_each_request_to_four_recipients(self):
        docs = [
            FakeDoc({
                "assignedEmails": [f"bp21harrison+batch-{row_number}@gmail.com"],
                "script": f"Hi Broker {row_number}",
                "clientId": "client-1",
                "subject": f"{row_number} Batch Way",
                "rowNumber": row_number,
            }, doc_id=f"outbox-{row_number}")
            for row_number in range(3, 8)
        ]
        sent_doc_ids = []

        def record_single_send(_user_id, _headers, item, *_args, **_kwargs):
            sent_doc_ids.append(item["doc"].id)

        with patch.dict(os.environ, {}, clear=False), \
             patch("email_automation.clients._fs", FakeFirestoreWithOutbox(docs)), \
             patch.object(email_module, "_send_single_outbox_item", side_effect=record_single_send), \
             patch.object(email_module.time, "sleep", return_value=None) as sleep:
            os.environ.pop("SITESIFT_OUTBOX_RECIPIENTS_PER_RUN", None)
            email_module.send_outboxes("uid-1", {"Authorization": "Bearer token"})

        self.assertEqual(
            ["outbox-3", "outbox-4", "outbox-5", "outbox-6"],
            sent_doc_ids,
        )
        self.assertFalse(docs[-1].reference.deleted)
        self.assertEqual(3, sleep.call_count)

    def test_outbox_batch_override_cannot_exceed_lease_safe_ceiling(self):
        with patch.dict(
            os.environ,
            {"SITESIFT_OUTBOX_RECIPIENTS_PER_RUN": "50"},
        ):
            self.assertEqual(4, email_module._resolve_outbox_recipients_per_run())

    def test_send_outboxes_resolves_structured_professional_signature_before_send(self):
        docs = [
            FakeDoc({
                "assignedEmails": ["bp21harrison@gmail.com"],
                "script": "Hi Avery",
                "clientId": "client-1",
                "subject": "100 Signature Way",
                "rowNumber": 3,
            }, doc_id="outbox-1")
        ]
        captured_signature = {}

        def record_single_send(_user_id, _headers, _item, user_signature=None, signature_mode=None, user_email=None, **_kwargs):
            captured_signature["html"] = user_signature
            captured_signature["mode"] = signature_mode
            captured_signature["email"] = user_email

        with patch(
            "email_automation.clients._fs",
            FakeFirestoreWithOutbox(docs, user_data={
                "email": "baylor.freelance@outlook.com",
                "signatureMode": "professional",
                "emailSignature": '<div data-sitesift-professional-signature="v1">Jill Ames jill.ames@mohrpartners.com</div>',
                "professionalSignature": {
                    "name": "John Doe",
                    "title": "Principal",
                    "email": "baylor.freelance@outlook.com",
                    "company": "Example Realty Advisors",
                },
            }),
        ), patch.object(email_module, "_send_single_outbox_item", side_effect=record_single_send):
            email_module.send_outboxes(
                "uid-1",
                {"Authorization": "Bearer token"},
            )

        self.assertEqual(captured_signature["mode"], "professional")
        self.assertEqual(captured_signature["email"], "baylor.freelance@outlook.com")
        self.assertIn("John Doe", captured_signature["html"])
        self.assertIn("Example Realty Advisors", captured_signature["html"])
        self.assertNotIn("Jill Ames", captured_signature["html"])
        self.assertNotIn("jill.ames@mohrpartners.com", captured_signature["html"])

    def test_campaign_launch_replaces_name_placeholder_before_send_guard(self):
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison@gmail.com"],
            "script": "Hi [NAME],\n\nCould you confirm the SF available?",
            "clientId": "client-1",
            "subject": "100 Name Resolution Way",
            "rowNumber": 3,
            "contactName": "Avery Brooks",
        }, doc_id="outbox-name-resolution")
        captured_body = {}

        def record_send(_user_id, _headers, script, *_args, **_kwargs):
            captured_body["script"] = script
            return {
                "sent": ["bp21harrison@gmail.com"],
                "errors": {},
            }

        with patch("email_automation.clients._fs", FakeFirestore()), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_pause_client_outbox_item_if_needed", return_value=False), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "get_contact_email_count", return_value=0), \
             patch.object(email_module, "_move_to_dead_letter") as move_to_dead_letter, \
             patch.object(email_module, "send_and_index_email", side_effect=record_send) as send_and_index_email, \
             patch.object(email_module, "_finalize_successful_outbox_item"):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        move_to_dead_letter.assert_not_called()
        send_and_index_email.assert_called_once()
        self.assertEqual(
            "Hi Avery,\n\nCould you confirm the SF available?",
            captured_body["script"],
        )

    def test_campaign_launch_recovers_missing_contact_name_from_sheet_row_before_send_guard(self):
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison+sheet-name@gmail.com"],
            "script": "Hi [NAME],\n\nCould you confirm the SF available?",
            "clientId": "client-1",
            "subject": "100 Sheet Name Way",
            "rowNumber": 12,
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
        }, doc_id="outbox-sheet-name")
        fake_fs = FakeFirestore()
        fake_sheets = FakeSheetsClient([
            "Avery Brooks",
            "bp21harrison+sheet-name@gmail.com",
            "100 Sheet Name Way",
        ])
        captured_body = {}

        def record_send(_user_id, _headers, script, *_args, **_kwargs):
            captured_body["script"] = script
            return {
                "sent": ["bp21harrison+sheet-name@gmail.com"],
                "errors": {},
            }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_pause_client_outbox_item_if_needed", return_value=False), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "_sheets_client", return_value=fake_sheets), \
             patch.object(email_module, "_get_first_tab_title", return_value="Campaign"), \
             patch.object(email_module, "_read_header_row2", return_value=["Leasing Contact", "Email", "Address"]), \
             patch.object(email_module, "get_contact_email_count", return_value=0), \
             patch.object(email_module, "_move_to_dead_letter") as move_to_dead_letter, \
             patch.object(email_module, "send_and_index_email", side_effect=record_send) as send_and_index_email, \
             patch.object(email_module, "_finalize_successful_outbox_item"):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        move_to_dead_letter.assert_not_called()
        send_and_index_email.assert_called_once()
        self.assertEqual(
            "Hi Avery,\n\nCould you confirm the SF available?",
            captured_body["script"],
        )

    def test_campaign_launch_exact_script_recovers_missing_contact_name_from_sheet_row(self):
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison+exact-sheet-name@gmail.com"],
            "script": "Hi [NAME],\n\nCould you confirm the SF available?",
            "clientId": "client-1",
            "subject": "101 Exact Sheet Name Way",
            "rowNumber": 12,
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
            "scriptSelectionMode": "exact",
        }, doc_id="outbox-exact-sheet-name")
        fake_fs = FakeFirestore()
        fake_sheets = FakeSheetsClient([
            "Avery Brooks",
            "bp21harrison+exact-sheet-name@gmail.com",
            "101 Exact Sheet Name Way",
        ])
        captured_body = {}

        def record_send(_user_id, _headers, script, *_args, **_kwargs):
            captured_body["script"] = script
            return {
                "sent": ["bp21harrison+exact-sheet-name@gmail.com"],
                "errors": {},
            }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "_sheets_client", return_value=fake_sheets), \
             patch.object(email_module, "_get_first_tab_title", return_value="Campaign"), \
             patch.object(email_module, "_read_header_row2", return_value=["Leasing Contact", "Email", "Address"]), \
             patch.object(email_module, "get_contact_email_count", return_value=0), \
             patch.object(email_module, "_move_to_dead_letter") as move_to_dead_letter, \
             patch.object(email_module, "send_and_index_email", side_effect=record_send) as send_and_index_email, \
             patch.object(email_module, "_finalize_successful_outbox_item"):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        move_to_dead_letter.assert_not_called()
        send_and_index_email.assert_called_once()
        self.assertEqual(
            "Hi Avery,\n\nCould you confirm the SF available?",
            captured_body["script"],
        )

    def test_campaign_launch_refuses_ambiguous_sheet_contact_name_before_graph_send(self):
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison+ambiguous-name@gmail.com"],
            "script": "Hi [NAME],\n\nCould you confirm the SF available?",
            "clientId": "client-1",
            "subject": "100 Ambiguous Name Way",
            "rowNumber": 12,
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
        }, doc_id="outbox-ambiguous-name")
        fake_fs = FakeFirestore()
        fake_sheets = FakeSheetsClient([
            "Avery Brooks",
            "Casey Broker",
            "bp21harrison+ambiguous-name@gmail.com",
        ])

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "_sheets_client", return_value=fake_sheets), \
             patch.object(email_module, "_get_first_tab_title", return_value="Campaign"), \
             patch.object(email_module, "_read_header_row2", return_value=["Leasing Contact", "Broker Name", "Email"]), \
             patch.object(email_module, "get_contact_email_count", return_value=0), \
             patch.object(email_module, "send_and_index_email") as send_and_index_email:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual("dead_lettered", dead_letter_payload["status"])
        self.assertIn("[NAME]", dead_letter_payload["failureReason"])
        self.assertIn("Ambiguous sheet contact/name source", dead_letter_payload["failureReason"])

    def test_campaign_launch_recipient_mismatch_dead_letters_before_graph_send(self):
        doc = FakeDoc({
            "assignedEmails": ["wrong.recipient@example.com"],
            "script": "Hi Casey,\n\nCould you confirm the SF available?",
            "clientId": "client-1",
            "subject": "100 Wrong Recipient Way",
            "rowNumber": 12,
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
            "contactName": "Casey Broker",
        }, doc_id="outbox-wrong-recipient")
        fake_fs = FakeFirestore()
        fake_sheets = FakeSheetsClient(["Casey Broker", "bp21harrison+correct@gmail.com"])

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "_sheets_client", return_value=fake_sheets), \
             patch.object(email_module, "_get_first_tab_title", return_value="Campaign"), \
             patch.object(email_module, "_read_header_row2", return_value=["Leasing Contact", "Email"]), \
             patch.object(email_module, "get_contact_email_count", return_value=0), \
             patch.object(email_module, "send_and_index_email") as send_and_index_email:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual(dead_letter_payload["status"], "dead_lettered")
        self.assertIn("Queued recipient does not match sheet row", dead_letter_payload["failureReason"])

    def test_campaign_launch_missing_row_metadata_dead_letters_before_graph_send(self):
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison+missing-row@gmail.com"],
            "script": "Hi Casey,\n\nCould you confirm the SF available?",
            "clientId": "client-1",
            "subject": "100 Missing Row Way",
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
            "contactName": "Casey Broker",
        }, doc_id="outbox-missing-row")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "get_contact_email_count", return_value=0), \
             patch.object(email_module, "_finalize_successful_outbox_item"), \
             patch.object(email_module, "send_and_index_email", return_value={
                 "sent": ["bp21harrison+missing-row@gmail.com"],
                 "errors": {},
             }) as send_and_index_email:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual(dead_letter_payload["status"], "dead_lettered")
        self.assertIn("missing required campaign launch metadata", dead_letter_payload["failureReason"])
        self.assertIn("rowNumber", dead_letter_payload["failureReason"])

    def test_campaign_launch_sheet_lookup_failure_dead_letters_before_graph_send(self):
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison+lookup-failed@gmail.com"],
            "script": "Hi Casey,\n\nCould you confirm the SF available?",
            "clientId": "client-1",
            "subject": "100 Lookup Failure Way",
            "rowNumber": 12,
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
            "contactName": "Casey Broker",
        }, doc_id="outbox-lookup-failed")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_get_sheet_id_or_fail", side_effect=RuntimeError("sheet unavailable")), \
             patch.object(email_module, "get_contact_email_count", return_value=0), \
             patch.object(email_module, "_finalize_successful_outbox_item"), \
             patch.object(email_module, "send_and_index_email", return_value={
                 "sent": ["bp21harrison+lookup-failed@gmail.com"],
                 "errors": {},
             }) as send_and_index_email:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual(dead_letter_payload["status"], "dead_lettered")
        self.assertIn("Could not verify queued recipient against sheet row 12", dead_letter_payload["failureReason"])
        self.assertIn("sheet unavailable", dead_letter_payload["failureReason"])

    def test_campaign_launch_invalid_row_number_dead_letters_before_graph_send(self):
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison+bad-row@gmail.com"],
            "script": "Hi Casey,\n\nCould you confirm the SF available?",
            "clientId": "client-1",
            "subject": "100 Invalid Row Way",
            "rowNumber": "Row 12",
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
            "contactName": "Casey Broker",
        }, doc_id="outbox-invalid-row")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "get_contact_email_count", return_value=0), \
             patch.object(email_module, "_finalize_successful_outbox_item"), \
             patch.object(email_module, "send_and_index_email", return_value={
                 "sent": ["bp21harrison+bad-row@gmail.com"],
                 "errors": {},
             }) as send_and_index_email:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual(dead_letter_payload["status"], "dead_lettered")
        self.assertIn("invalid rowNumber", dead_letter_payload["failureReason"])
        self.assertIn("Row 12", dead_letter_payload["failureReason"])

    def test_campaign_launch_recipient_guard_accepts_multi_email_row_cell(self):
        recipient = "bp21harrison+correct@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi Casey,\n\nCould you confirm the SF available?",
            "clientId": "client-1",
            "subject": "101 Multi Email Way",
            "rowNumber": 12,
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
            "contactName": "Casey Broker",
        }, doc_id="outbox-multi-email-row")
        fake_fs = FakeFirestore()
        fake_sheets = FakeSheetsClient(["Casey Broker", f"casey@example.com; {recipient}"])

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "_sheets_client", return_value=fake_sheets), \
             patch.object(email_module, "_get_first_tab_title", return_value="Campaign"), \
             patch.object(email_module, "_read_header_row2", return_value=["Leasing Contact", "Email"]), \
             patch.object(email_module, "get_contact_email_count", return_value=0), \
             patch.object(email_module, "_finalize_successful_outbox_item") as finalize, \
             patch.object(email_module, "send_and_index_email", return_value={
                 "sent": [recipient],
                 "errors": {},
             }) as send_and_index_email:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        send_and_index_email.assert_called_once()
        finalize.assert_called_once()
        self.assertFalse(doc.reference.deleted)
        self.assertEqual([], fake_fs.add_calls)

    def test_grouped_campaign_launch_uses_resolved_row_number_before_graph_send(self):
        recipient = "bp21harrison+grouped-row-anchor@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi Casey,\n\nCould you confirm the SF available?",
            "clientId": "client-1",
            "subject": "102 Grouped Row Anchor Way",
            "rowNumber": 12,
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
            "contactName": "Casey Broker",
        }, doc_id="outbox-grouped-row-anchor")
        fresh_without_row_number = {
            "assignedEmails": [recipient],
            "script": "Hi Casey,\n\nCould you confirm the SF available?",
            "clientId": "client-1",
            "subject": "102 Grouped Row Anchor Way",
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
            "contactName": "Casey Broker",
        }
        fake_fs = FakeFirestore()
        fake_sheets = FakeSheetsClient(["Casey Broker", "other.broker@example.com"])

        with patch("email_automation.clients._fs", fake_fs), \
             patch("email_automation.processing.is_contact_opted_out", return_value=None), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_current_outbox_data", return_value=fresh_without_row_number), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "_sheets_client", return_value=fake_sheets), \
             patch.object(email_module, "_get_first_tab_title", return_value="Campaign"), \
             patch.object(email_module, "_read_header_row2", return_value=["Leasing Contact", "Email"]), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "send_and_index_email") as send_and_index_email:
            email_module._send_multi_property_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                recipient,
                [{"doc": doc, "data": doc.to_dict()}],
            )

        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual(dead_letter_payload["status"], "dead_lettered")
        self.assertIn("Queued recipient does not match sheet row 12", dead_letter_payload["failureReason"])

    def test_grouped_campaign_launch_recovers_missing_contact_name_from_sheet_row(self):
        recipient = "bp21harrison+grouped-sheet-name@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi [NAME],\n\nCould you confirm the SF available?",
            "clientId": "client-1",
            "subject": "102 Grouped Sheet Name Way",
            "rowNumber": 12,
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
        }, doc_id="outbox-grouped-sheet-name")
        fake_sheets = FakeSheetsClient(["Avery Brooks", recipient])
        captured_body = {}

        def record_send(_user_id, _headers, script, *_args, **_kwargs):
            captured_body["script"] = script
            return {
                "sent": [recipient],
                "errors": {},
            }

        with patch("email_automation.clients._fs", FakeFirestore()), \
             patch("email_automation.processing.is_contact_opted_out", return_value=None), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "_sheets_client", return_value=fake_sheets), \
             patch.object(email_module, "_get_first_tab_title", return_value="Campaign"), \
             patch.object(email_module, "_read_header_row2", return_value=["Leasing Contact", "Email"]), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_move_to_dead_letter") as move_to_dead_letter, \
             patch.object(email_module, "send_and_index_email", side_effect=record_send) as send_and_index_email, \
             patch.object(email_module, "_finalize_successful_outbox_item"), \
             patch.object(email_module.time, "sleep"):
            email_module._send_multi_property_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                recipient,
                [{"doc": doc, "data": doc.to_dict()}],
            )

        move_to_dead_letter.assert_not_called()
        send_and_index_email.assert_called_once()
        self.assertEqual(
            "Hi Avery,\n\nCould you confirm the SF available?",
            captured_body["script"],
        )

    def test_grouped_campaign_launch_refuses_ambiguous_sheet_contact_name_before_graph_send(self):
        recipient = "bp21harrison+grouped-ambiguous-name@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi [NAME],\n\nCould you confirm the SF available?",
            "clientId": "client-1",
            "subject": "102 Grouped Ambiguous Name Way",
            "rowNumber": 12,
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
        }, doc_id="outbox-grouped-ambiguous-name")
        fake_fs = FakeFirestore()
        fake_sheets = FakeSheetsClient(["Avery Brooks", "Casey Broker", recipient])

        with patch("email_automation.clients._fs", fake_fs), \
             patch("email_automation.processing.is_contact_opted_out", return_value=None), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "_sheets_client", return_value=fake_sheets), \
             patch.object(email_module, "_get_first_tab_title", return_value="Campaign"), \
             patch.object(email_module, "_read_header_row2", return_value=["Leasing Contact", "Broker Name", "Email"]), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "send_and_index_email") as send_and_index_email, \
             patch.object(email_module.time, "sleep"):
            email_module._send_multi_property_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                recipient,
                [{"doc": doc, "data": doc.to_dict()}],
            )

        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual("dead_lettered", dead_letter_payload["status"])
        self.assertIn("[NAME]", dead_letter_payload["failureReason"])
        self.assertIn("Ambiguous sheet contact/name source", dead_letter_payload["failureReason"])

    def test_grouped_campaign_launch_reuses_sheet_metadata_across_rows(self):
        recipient = "bp21harrison+grouped-cache@gmail.com"
        docs = [
            FakeDoc({
                "assignedEmails": [recipient],
                "script": "Hi Casey,\n\nCould you confirm the SF available?",
                "clientId": "client-1",
                "subject": "103 Cache Row A",
                "rowNumber": 12,
                "source": "dashboard_new_campaign",
                "actionType": "campaign_creation",
                "contactName": "Casey Broker",
            }, doc_id="outbox-cache-a"),
            FakeDoc({
                "assignedEmails": [recipient],
                "script": "Hi Casey,\n\nCould you confirm the SF available?",
                "clientId": "client-1",
                "subject": "104 Cache Row B",
                "rowNumber": 13,
                "source": "dashboard_new_campaign",
                "actionType": "campaign_creation",
                "contactName": "Casey Broker",
            }, doc_id="outbox-cache-b"),
        ]
        fake_sheets = FakeSheetsClient({
            "Campaign!12:12": ["Casey Broker", recipient],
            "Campaign!13:13": ["Casey Broker", recipient],
        })

        with patch("email_automation.clients._fs", FakeFirestore()), \
             patch("email_automation.processing.is_contact_opted_out", return_value=None), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "_sheets_client", return_value=fake_sheets), \
             patch.object(email_module, "_get_first_tab_title", return_value="Campaign") as get_first_tab_title, \
             patch.object(email_module, "_read_header_row2", return_value=["Leasing Contact", "Email"]) as read_header, \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "send_and_index_email", return_value={
                 "sent": [recipient],
                 "errors": {},
             }) as send_and_index_email, \
             patch.object(email_module, "_finalize_successful_outbox_item"), \
             patch.object(email_module.time, "sleep"):
            email_module._send_multi_property_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                recipient,
                [{"doc": doc, "data": doc.to_dict()} for doc in docs],
            )

        self.assertEqual(2, send_and_index_email.call_count)
        self.assertEqual(1, get_first_tab_title.call_count)
        self.assertEqual(1, read_header.call_count)
        self.assertEqual(
            [("sheet-1", "Campaign!12:12"), ("sheet-1", "Campaign!13:13")],
            fake_sheets.spreadsheets_api.values_api.ranges,
        )

    def test_name_placeholder_rejects_markup_contact_name(self):
        body = email_module._personalize_name_placeholders(
            "Hi [NAME],\n\nCould you confirm the SF available?",
            "Avery<script>",
        )

        self.assertEqual(
            "Hi [NAME],\n\nCould you confirm the SF available?",
            body,
        )

    def test_name_placeholder_rejects_regex_like_contact_name_without_error(self):
        body = email_module._personalize_name_placeholders(
            "Hi [NAME],\n\nCould you confirm the SF available?",
            r"Avery\1",
        )

        self.assertEqual(
            "Hi [NAME],\n\nCould you confirm the SF available?",
            body,
        )

    def test_generated_fallback_greeting_rejects_markup_contact_name(self):
        primary_script = (
            "Hi [NAME],\n\n"
            "Their requirements are:\n"
            "- 10,000 SF warehouse\n\n"
            "Thanks!"
        )

        with patch.object(email_module, "get_contact_email_count", return_value=1):
            script = email_module._select_script_for_recipient(
                "uid-1",
                "bp21harrison@gmail.com",
                [primary_script],
                contact_name="Avery<script>",
            )

        self.assertTrue(script.startswith("Hi,"))
        self.assertNotIn("Avery<script>", script)

    def test_maintenance_paused_client_outbox_item_stays_queued_without_send(self):
        doc_ref = FakeDocRef("paused-outbox")
        data = {
            "clientId": "client-1",
            "script": "Hi Avery",
            "actionAuditId": "audit-1",
        }

        with patch.object(
            email_module,
            "get_client_automation_pause",
            return_value=(True, "admin_incident_pause", {"automationPaused": True}),
        ), patch.object(email_module, "_move_to_dead_letter") as move_to_dead_letter, \
             patch.object(email_module, "_update_action_audit") as update_action_audit:
            blocked = email_module._pause_client_outbox_item_if_needed(
                "uid-1",
                doc_ref,
                data,
            )

        self.assertTrue(blocked)
        move_to_dead_letter.assert_not_called()
        retry_payload = doc_ref.set_calls[-1][0][0]
        self.assertEqual("queued", retry_payload["status"])
        self.assertEqual("blocked", retry_payload["automationSuppressedState"])
        update_action_audit.assert_called_once()
        audit_payload = update_action_audit.call_args.args[2]
        self.assertEqual("queued", audit_payload["status"])
        self.assertEqual("blocked", audit_payload["automationSuppressedState"])
        self.assertEqual("admin_incident_pause", audit_payload["automationSuppressedReason"])

    def test_stopped_client_outbox_item_moves_to_dead_letter_before_send(self):
        doc_ref = FakeDocRef("stopped-outbox")
        data = {"clientId": "client-1", "script": "Hi Avery"}

        with patch.object(
            email_module,
            "get_client_automation_pause",
            return_value=(True, "client_stopped_by_user", {"status": "stopped"}),
        ), patch.object(email_module, "_move_to_dead_letter") as move_to_dead_letter:
            blocked = email_module._pause_client_outbox_item_if_needed(
                "uid-1",
                doc_ref,
                data,
            )

        self.assertTrue(blocked)
        move_to_dead_letter.assert_called_once()
        self.assertIn("stopped", move_to_dead_letter.call_args.args[3])
        self.assertEqual([], doc_ref.set_calls)

    def test_paused_client_outbox_item_moves_to_dead_letter_before_send(self):
        """Compatibility contract: a terminal stop still dead-letters before send."""
        self.test_stopped_client_outbox_item_moves_to_dead_letter_before_send()

    def test_jill_tour_outbox_item_moves_to_dead_letter_before_send(self):
        doc_ref = FakeDocRef("jill-tour-outbox")
        data = {
            "clientId": "client-1",
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
            "script": "Hi Avery,\n\nPlease confirm the 10:00 AM tour slot.",
        }

        with patch.object(email_module, "_move_to_dead_letter") as move_to_dead_letter:
            blocked = email_module._pause_results_outbox_item_if_needed(
                "C4X3UH1r6QhgP3ivXD1QjyhuGyI2",
                doc_ref,
                data,
            )

        self.assertTrue(blocked)
        move_to_dead_letter.assert_called_once()
        self.assertIn("Tour-planning emails", move_to_dead_letter.call_args.args[3])

    def test_unresolved_name_placeholder_outbox_item_moves_to_dead_letter_before_send(self):
        doc_ref = FakeDocRef("unsafe-outbox")
        data = {"clientId": "client-1", "script": "Hi [NAME],\n\nCould you confirm?"}

        with patch.object(email_module, "_move_to_dead_letter") as move_to_dead_letter:
            blocked = email_module._dead_letter_unsafe_outbound_body_if_needed(
                "uid-1",
                doc_ref,
                data,
                data["script"],
            )

        self.assertTrue(blocked)
        move_to_dead_letter.assert_called_once()
        self.assertIn("[NAME]", move_to_dead_letter.call_args.args[3])

    def test_normal_campaign_tour_language_moves_to_dead_letter_before_send(self):
        doc_ref = FakeDocRef("tour-language-outbox")
        data = {
            "clientId": "client-1",
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
            "script": (
                "Hi Connor,\n\nBefore we proceed with tour scheduling and/or LOIs, "
                "can you please confirm the following?"
            ),
        }

        with patch.object(email_module, "_move_to_dead_letter") as move_to_dead_letter:
            blocked = email_module._dead_letter_unsafe_outbound_body_if_needed(
                "uid-1",
                doc_ref,
                data,
                data["script"],
            )

        self.assertTrue(blocked)
        move_to_dead_letter.assert_called_once()
        self.assertIn("Tour/LOI", move_to_dead_letter.call_args.args[3])

    def test_tour_planner_outbox_uses_reviewed_body_even_for_existing_contact(self):
        reviewed_body = (
            "Property: 555 Geocoded Map Dr\n"
            "Scheduled arrival: 9:00 AM\n"
            "Scheduled departure: 9:30 AM\n"
            "Please confirm whether this tour slot works."
        )
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison@gmail.com"],
            "script": reviewed_body,
            "clientId": "client-1",
            "subject": "Tour slot: 555 Geocoded Map Dr at 9:00 AM",
            "rowNumber": 7,
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
            "actionAuditId": "audit-tour",
        }, doc_id="outbox-tour")

        with patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch("email_automation.clients._fs", FakeFirestore()), \
             patch.object(email_module, "_select_script_for_recipient", return_value="Wrong fallback body") as select_script, \
             patch.object(email_module, "send_and_index_email", return_value={
                 "sent": ["bp21harrison@gmail.com"],
                 "errors": {},
             }) as send_and_index_email, \
             patch.object(email_module, "_finalize_successful_outbox_item"):
            email_module._send_single_outbox_item(
                "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        select_script.assert_not_called()
        send_and_index_email.assert_called_once()
        self.assertEqual(send_and_index_email.call_args.args[2], reviewed_body)

    def test_tour_planner_outbox_preserves_tour_context_on_sent_thread(self):
        tour_context = {
            "propertyId": "row-7",
            "arrivalTime": "10:47 AM",
            "departureTime": "11:17 AM",
            "stopMinutes": 30,
        }
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison@gmail.com"],
            "script": "Hi Avery,\n\nPlease confirm the 10:47 AM tour slot.",
            "clientId": "client-1",
            "subject": "Tour slot: 555 Geocoded Map Dr at 10:47 AM",
            "rowNumber": 7,
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
            "tourInvite": tour_context,
            "actionAuditId": "audit-tour",
        }, doc_id="outbox-tour")

        with patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch("email_automation.clients._fs", FakeFirestore()), \
             patch.object(email_module, "send_and_index_email", return_value={
                 "sent": ["bp21harrison@gmail.com"],
                 "errors": {},
             }) as send_and_index_email, \
             patch.object(email_module, "_finalize_successful_outbox_item"):
            email_module._send_single_outbox_item(
                "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        self.assertEqual(
            {
                "source": "dashboard_tour_planner",
                "actionType": "tour_invite",
                "tourInvite": tour_context,
                "actionAuditId": "audit-tour",
            },
            send_and_index_email.call_args.kwargs["thread_context"],
        )

    def test_successful_dashboard_outbox_finalizes_notification_and_thread_after_send(self):
        outbox_ref = FakeDocRef()
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "delete_notification_and_decrement_counters") as delete_notification:
            email_module._finalize_successful_outbox_item(
                "uid-1",
                outbox_ref,
                {
                    "clientId": "client-1",
                    "notificationClientId": "client-1",
                    "notificationId": "notification-1",
                    "deleteNotificationOnSend": True,
                    "resumeThreadOnSend": True,
                    "threadId": "thread-1",
                },
            )

        self.assertTrue(outbox_ref.deleted)
        delete_notification.assert_called_once_with("uid-1", "client-1", "notification-1")
        thread_set = fake_fs.set_calls[0]
        self.assertEqual(
            thread_set[0],
            ("collection", "users", "document", "uid-1", "collection", "threads", "document", "thread-1"),
        )
        self.assertEqual(thread_set[1]["status"], "active")
        self.assertEqual(thread_set[1]["followUpStatus"], "waiting")
        self.assertTrue(thread_set[2])

    def _dashboard_manual_reply_doc(self, overrides=None, doc_id="outbox-dashboard-reply") -> FakeDoc:
        data = {
            "assignedEmails": ["bp21harrison@gmail.com"],
            "script": "Hi Ron,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "notificationClientId": "client-1",
            "notificationId": "notification-1",
            "deleteNotificationOnSend": True,
            "resumeThreadOnSend": True,
            "subject": "RE: 0 Gemini Ave, Houston",
            "threadId": "thread-1",
            "replyToMessageId": "graph-message-1",
            "conversationId": "conversation-1",
            "rowNumber": 20,
            "actionAuditId": "audit-dashboard-reply",
            "source": "dashboard_manual_reply",
            "followUpConfig": {"enabled": False},
            "processingBy": email_module.WORKER_ID,
        }
        if overrides:
            data.update(overrides)
        return FakeDoc(data, doc_id=doc_id)

    def test_dashboard_manual_reply_cancelled_after_claim_deletes_without_graph_send(self):
        doc = self._dashboard_manual_reply_doc()
        fake_fs = FakeFirestore()
        cancelled_payload = {
            **doc.to_dict(),
            "cancelRequested": True,
            "status": "cancel_requested",
        }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_current_outbox_data", return_value=cancelled_payload), \
             patch.object(email_module, "_get_reply_message_sender") as get_reply_sender, \
             patch.object(email_module, "_send_outbox_as_reply") as send_outbox_as_reply, \
             patch.object(email_module, "send_and_index_email") as send_and_index_email:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        get_reply_sender.assert_not_called()
        send_outbox_as_reply.assert_not_called()
        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual("cancelled", audit_payload["status"])
        self.assertEqual("outbox-dashboard-reply", audit_payload["outboxId"])

    def test_dashboard_manual_reply_success_records_audit_after_graph_reply(self):
        doc = self._dashboard_manual_reply_doc()
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com"), \
             patch.object(email_module, "_send_outbox_as_reply", return_value={
                 "sent": True,
                 "error": None,
                 "sentMessageId": "graph-reply-message-1",
                 "internetMessageId": "<graph-reply-message-1@example.com>",
                 "conversationId": "conversation-1",
                 "toRecipients": ["bp21harrison@gmail.com"],
                 "ccRecipients": [],
                 "sentRecipients": ["bp21harrison@gmail.com"],
             }) as send_outbox_as_reply, \
             patch.object(email_module, "_save_outbox_reply_message") as save_outbox_reply_message, \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "highlight_row"), \
             patch.object(email_module, "delete_notification_and_decrement_counters") as delete_notification:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        send_outbox_as_reply.assert_called_once()
        save_outbox_reply_message.assert_called_once()
        delete_notification.assert_called_once_with("uid-1", "client-1", "notification-1")
        self.assertTrue(doc.reference.deleted)
        audit_payload = _action_audit_payload(fake_fs, "uid-1", "audit-dashboard-reply")
        self.assertEqual("sent", audit_payload["status"])
        self.assertEqual("audit-dashboard-reply", doc.to_dict()["actionAuditId"])
        self.assertEqual("graph-reply-message-1", audit_payload["sentMessageId"])
        self.assertEqual("<graph-reply-message-1@example.com>", audit_payload["internetMessageId"])
        thread_payload = fake_fs.set_calls[-1][1]
        self.assertEqual("active", thread_payload["status"])
        self.assertEqual("waiting", thread_payload["followUpStatus"])

    def test_dashboard_tour_action_reply_persists_invite_context_with_resume(self):
        doc = self._dashboard_manual_reply_doc({
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
        })
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        fake_fs.snapshots["users/uid-1/threads/thread-1"] = FakeSnapshot({
            "clientId": "client-1",
            "status": "paused",
            "rowNumber": 20,
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
        })
        _seed_tour_notification(fake_fs)

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com"), \
             patch.object(email_module, "_send_outbox_as_reply", return_value={
                 "sent": True,
                 "error": None,
                 "sentMessageId": "graph-tour-reply-1",
                 "internetMessageId": "<graph-tour-reply-1@example.com>",
                 "conversationId": "conversation-1",
                 "toRecipients": ["bp21harrison@gmail.com"],
                 "ccRecipients": [],
                 "sentRecipients": ["bp21harrison@gmail.com"],
             }), \
             patch.object(email_module, "_save_outbox_reply_message"), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "highlight_row"), \
             patch.object(email_module, "delete_notification_and_decrement_counters"):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        thread_path = (
            "collection", "users", "document", "uid-1",
            "collection", "threads", "document", "thread-1",
        )
        thread_payloads = [
            payload for path, payload, _merge in fake_fs.set_calls
            if path == thread_path
        ]
        self.assertEqual(1, len(thread_payloads))
        self.assertEqual("active", thread_payloads[0]["status"])
        self.assertEqual("waiting", thread_payloads[0]["followUpStatus"])
        self.assertEqual("awaiting_confirmation", thread_payloads[0]["tourStatus"])
        self.assertEqual("sent", thread_payloads[0]["tourInvite"]["status"])
        self.assertEqual(
            "graph-tour-reply-1",
            thread_payloads[0]["tourInvite"]["sentMessageId"],
        )
        self.assertFalse(any(
            key.startswith("tourInvite.") for key in thread_payloads[0]
        ))
        self.assertNotIn("source", thread_payloads[0])
        self.assertNotIn("actionType", thread_payloads[0])
        resolution = fake_fs.snapshots[
            "users/uid-1/actionResolutions/notification-1"
        ].to_dict()
        self.assertEqual("sent", resolution["status"])
        self.assertEqual("outbox-dashboard-reply", resolution["outboxId"])
        audit = _action_audit_payload(
            fake_fs,
            "uid-1",
            "audit-dashboard-reply",
        )
        self.assertEqual("sent", audit["status"])
        self.assertEqual("graph-tour-reply-1", audit["sentMessageId"])

    def test_server_verified_dashboard_tour_reply_allows_real_suggested_copy(self):
        doc = self._dashboard_manual_reply_doc({
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
            "script": (
                "Hi Ryan,\n\n"
                "Thank you for offering to show me the property. I'd like to schedule a tour.\n\n"
                "Tuesday, August 11 at 2:00 PM would work on my end.\n\n"
                "Could you please confirm what works best?\n\n"
                "Thanks!"
            ),
        }, doc_id="outbox-real-tour-suggested-copy")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        _seed_tour_notification(fake_fs)

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com"), \
             patch.object(email_module, "_send_outbox_as_reply", return_value={
                 "sent": True,
                 "error": None,
                 "sentMessageId": "graph-real-tour-copy-1",
                 "internetMessageId": "<graph-real-tour-copy-1@example.com>",
                 "conversationId": "conversation-1",
                 "toRecipients": ["bp21harrison@gmail.com"],
                 "ccRecipients": [],
                 "sentRecipients": ["bp21harrison@gmail.com"],
             }) as graph_send, \
             patch.object(email_module, "_save_outbox_reply_message", return_value=True), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "highlight_row"):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        graph_send.assert_called_once()
        self.assertEqual([], fake_fs.add_calls)
        self.assertEqual(
            "awaiting_confirmation",
            fake_fs.snapshots["users/uid-1/threads/thread-1"].to_dict()["tourStatus"],
        )

    def test_tour_notification_cannot_authorize_forged_recipient(self):
        doc = self._dashboard_manual_reply_doc({
            "assignedEmails": ["forged@example.com"],
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
            "script": "I'd like to schedule the property tour Tuesday at 2 PM.",
        }, doc_id="outbox-forged-tour-recipient")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        _seed_tour_notification(fake_fs)

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="forged@example.com"), \
             patch.object(email_module, "_send_outbox_as_reply") as graph_send:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        graph_send.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        self.assertEqual(1, len(fake_fs.add_calls))

    def test_inline_action_with_multiple_recipients_fails_closed(self):
        doc = self._dashboard_manual_reply_doc({
            "assignedEmails": [
                "bp21harrison@gmail.com",
                "forged@example.com",
            ],
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
            "script": "Thanks, we'll follow up shortly.",
        }, doc_id="outbox-tour-multiple-recipients")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        _seed_tour_notification(fake_fs)

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com"), \
             patch.object(email_module, "_send_outbox_as_reply") as graph_reply, \
             patch.object(email_module, "send_and_index_email") as graph_new_send:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        graph_reply.assert_not_called()
        graph_new_send.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        self.assertEqual(1, len(fake_fs.add_calls))

    def test_inline_action_with_missing_binding_fails_closed(self):
        doc = self._dashboard_manual_reply_doc({
            "notificationId": "",
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
            "script": "Thanks, we'll follow up shortly.",
        }, doc_id="outbox-tour-missing-notification")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com"), \
             patch.object(email_module, "_send_outbox_as_reply") as graph_reply, \
             patch.object(email_module, "send_and_index_email") as graph_new_send:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        graph_reply.assert_not_called()
        graph_new_send.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        self.assertEqual(1, len(fake_fs.add_calls))

    def test_tour_notification_cannot_authorize_forged_source_message(self):
        doc = self._dashboard_manual_reply_doc({
            "replyToMessageId": "graph-message-2",
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
            "script": "I'd like to schedule the property tour Tuesday at 2 PM.",
        }, doc_id="outbox-forged-tour-message")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs, message_id="graph-message-2")
        _seed_tour_notification(fake_fs, reply_to_message_id="graph-message-1")

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com"), \
             patch.object(email_module, "_send_outbox_as_reply") as graph_send:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        graph_send.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        self.assertEqual(1, len(fake_fs.add_calls))

    def test_client_tour_reason_cannot_upgrade_server_non_tour_notification(self):
        doc = self._dashboard_manual_reply_doc({
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
            "script": "I'd like to schedule the property tour Tuesday at 2 PM.",
        }, doc_id="outbox-spoofed-tour-reason")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        _seed_tour_notification(fake_fs, reason="call_requested")

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com"), \
             patch.object(email_module, "_send_outbox_as_reply") as graph_send:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        graph_send.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        self.assertEqual(1, len(fake_fs.add_calls))

    def test_server_verified_tour_reply_drops_client_supplied_fallback_cc(self):
        doc = self._dashboard_manual_reply_doc({
            "ccEmails": ["forged-cc@example.com"],
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
            "script": "I'd like to schedule the property tour Tuesday at 2 PM.",
        }, doc_id="outbox-forged-tour-cc")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        _seed_tour_notification(fake_fs)

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com"), \
             patch.object(email_module, "_send_outbox_as_reply", return_value={
                 "sent": True,
                 "error": None,
                 "sentMessageId": "graph-tour-no-forged-cc",
                 "toRecipients": ["bp21harrison@gmail.com"],
                 "ccRecipients": [],
                 "sentRecipients": ["bp21harrison@gmail.com"],
             }) as graph_send, \
             patch.object(email_module, "_save_outbox_reply_message", return_value=True), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "highlight_row"):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        graph_send.assert_called_once()
        self.assertEqual([], graph_send.call_args.kwargs["fallback_cc_emails"])

    def test_tour_reservation_observes_cancel_delete_and_claim_owner_races(self):
        for race, expected_status in (
            ("cancel", "cancelled"),
            ("delete", "gone"),
            ("claim_owner", "claim_lost"),
        ):
            with self.subTest(race=race):
                doc = self._dashboard_manual_reply_doc({
                    "actionReason": "tour_requested",
                    "source": "dashboard_inline_reply",
                    "actionType": "reply",
                }, doc_id=f"outbox-tour-{race}-race")
                stale_data = dict(doc.to_dict())
                if race == "cancel":
                    doc.reference._data.update({
                        "cancelRequested": True,
                        "status": "cancel_requested",
                    })
                elif race == "delete":
                    doc.reference.delete()
                else:
                    doc.reference._data["processingBy"] = "other-worker"

                fake_fs = FakeFirestore()
                _seed_open_thread(fake_fs)
                _seed_tour_notification(fake_fs)
                with patch("email_automation.clients._fs", fake_fs):
                    result = email_module._reserve_dashboard_tour_action_resolution(
                        "uid-1",
                        doc.reference,
                        stale_data,
                    )

                self.assertEqual(expected_status, result["status"])
                resolution = fake_fs.snapshots.get(
                    "users/uid-1/actionResolutions/notification-1"
                )
                self.assertFalse(resolution and resolution.exists)

    def test_dashboard_tour_reservation_rejects_terminal_client(self):
        outbox_ref = FakeDocRef("outbox-tour-stopped-client")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        fake_fs.snapshots["users/uid-1/clients/client-1"] = FakeSnapshot({
            "status": "stopped",
            "automationPaused": True,
        })
        _seed_tour_notification(fake_fs)
        data = {
            **self._dashboard_manual_reply_doc().to_dict(),
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
        }
        outbox_ref._data.update(data)

        with patch("email_automation.clients._fs", fake_fs):
            result = email_module._reserve_dashboard_tour_action_resolution(
                "uid-1",
                outbox_ref,
                data,
            )

        self.assertEqual("invalid", result["status"])
        self.assertEqual("client_no_longer_active", result["reason"])

    def test_unverified_dashboard_tour_action_binding_fails_closed_before_send(self):
        doc = self._dashboard_manual_reply_doc({
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
        })
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        fake_fs.snapshots[
            "users/uid-1/clients/client-1/notifications/notification-1"
        ] = FakeSnapshot({
            "kind": "action_needed",
            "threadId": "different-thread",
            "meta": {"reason": "tour_requested"},
        })

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com"), \
             patch.object(email_module, "_send_outbox_as_reply", return_value={
                 "sent": True,
                 "error": None,
                 "sentMessageId": "graph-reply-message-1",
                 "internetMessageId": "<graph-reply-message-1@example.com>",
                 "conversationId": "conversation-1",
                 "toRecipients": ["bp21harrison@gmail.com"],
                 "ccRecipients": [],
                 "sentRecipients": ["bp21harrison@gmail.com"],
             }) as send_reply, \
             patch.object(email_module, "_save_outbox_reply_message"), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "highlight_row"), \
             patch.object(email_module, "delete_notification_and_decrement_counters"):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        send_reply.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        self.assertEqual(1, len(fake_fs.add_calls))

    def test_tour_context_write_failure_keeps_dashboard_action_retryable(self):
        outbox_ref = FakeDocRef("outbox-tour-state-retry")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        _seed_tour_notification(fake_fs)
        fake_fs.set_errors["users/uid-1/threads/thread-1"] = RuntimeError(
            "tour context write unavailable"
        )
        data = {
            **self._dashboard_manual_reply_doc().to_dict(),
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
        }
        outbox_ref._data.update(data)

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "delete_notification_and_decrement_counters") as delete_notification:
            resolution = email_module._reserve_dashboard_tour_action_resolution(
                "uid-1",
                outbox_ref,
                data,
            )
            with self.assertRaisesRegex(RuntimeError, "tour context write unavailable"):
                email_module._finalize_successful_outbox_item(
                    "uid-1",
                    outbox_ref,
                    data,
                    send_result={
                        "sent": ["bp21harrison@gmail.com"],
                        "sentMessageIds": {
                            "bp21harrison@gmail.com": "graph-tour-reply-1",
                        },
                    },
                    dashboard_tour_resolution=resolution,
                )

        self.assertFalse(outbox_ref.deleted)
        delete_notification.assert_not_called()

    def test_dashboard_tour_action_retry_finalizes_without_second_graph_send(self):
        doc = self._dashboard_manual_reply_doc({
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
        }, doc_id="outbox-dashboard-tour-retry")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        fake_fs.snapshots["users/uid-1/threads/thread-1"] = FakeSnapshot({
            "clientId": "client-1",
            "status": "paused",
            "rowNumber": 20,
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
        })
        _seed_tour_notification(fake_fs)
        fake_fs.set_errors["users/uid-1/threads/thread-1"] = RuntimeError(
            "tour context write unavailable"
        )
        fake_fs.outbox_docs = [doc]

        def graph_send(*_args, **_kwargs):
            attempt_payloads = [
                args[0] for args, _kwargs in doc.reference.set_calls
                if args
            ]
            self.assertTrue(any(
                payload.get("requiresSentItemsPreflight") is True
                for payload in attempt_payloads
            ))
            return {
                "sent": True,
                "error": None,
                "sentMessageId": "graph-tour-retry-1",
                "internetMessageId": "<graph-tour-retry-1@example.com>",
                "conversationId": "conversation-1",
                "toRecipients": ["bp21harrison@gmail.com"],
                "ccRecipients": [],
                "sentRecipients": ["bp21harrison@gmail.com"],
            }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com"), \
             patch.object(email_module, "_send_outbox_as_reply", side_effect=graph_send) as send_reply, \
             patch.object(email_module, "_save_outbox_reply_message"), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "highlight_row"), \
             patch.object(email_module, "delete_notification_and_decrement_counters") as delete_notification, \
             patch.object(email_module, "_resolve_daily_send_cap", return_value=1), \
             patch.object(email_module, "_resolve_global_daily_send_cap", return_value=None), \
             patch.object(email_module, "_read_daily_send_count", side_effect=[0, 1]) as read_send_count, \
             patch.object(email_module, "_increment_daily_send_count"), \
             patch.object(
                 email_module,
                 "resolve_outbound_mode",
                 side_effect=[email_module.OUTBOUND_MODE_LIVE, email_module.OUTBOUND_MODE_PAUSED],
             ):
            email_module.send_outboxes(
                "uid-1",
                {"Authorization": "Bearer token"},
            )

            pending_payloads = [
                args[0] for args, _kwargs in doc.reference.set_calls
                if args and args[0].get("status") == "sent_pending_finalization"
            ]
            self.assertEqual(1, len(pending_payloads))
            self.assertTrue(pending_payloads[0]["alreadySent"])
            self.assertTrue(pending_payloads[0]["requiresSentItemsPreflight"])
            self.assertFalse(doc.reference.deleted)
            delete_notification.assert_not_called()

            doc._data.update(pending_payloads[0])
            fake_fs.set_errors.clear()
            failing_headers_provider = MagicMock(
                side_effect=RuntimeError("token refresh unavailable")
            )
            email_module.send_outboxes(
                "uid-1",
                {"Authorization": "Bearer token"},
                headers_provider=failing_headers_provider,
            )

        send_reply.assert_called_once()
        failing_headers_provider.assert_not_called()
        read_send_count.assert_called_once()
        self.assertTrue(doc.reference.deleted)
        delete_notification.assert_not_called()
        self.assertIn(
            (
                "collection", "users", "document", "uid-1",
                "collection", "clients", "document", "client-1",
                "collection", "notifications", "document", "notification-1",
            ),
            fake_fs.deleted_paths,
        )

    def test_matching_non_tour_dashboard_action_remains_generic_reply(self):
        outbox_ref = FakeDocRef("outbox-call-action")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        fake_fs.snapshots[
            "users/uid-1/clients/client-1/notifications/notification-1"
        ] = FakeSnapshot({
            "kind": "action_needed",
            "threadId": "thread-1",
            "meta": {"reason": "call_requested"},
        })
        data = {
            **self._dashboard_manual_reply_doc().to_dict(),
            "actionReason": "call_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
        }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "delete_notification_and_decrement_counters"):
            email_module._finalize_successful_outbox_item(
                "uid-1",
                outbox_ref,
                data,
                send_result={
                    "sent": ["bp21harrison@gmail.com"],
                    "sentMessageIds": {
                        "bp21harrison@gmail.com": "graph-call-reply-1",
                    },
                },
            )

        thread_path = (
            "collection", "users", "document", "uid-1",
            "collection", "threads", "document", "thread-1",
        )
        thread_payloads = [
            payload for path, payload, _merge in fake_fs.set_calls
            if path == thread_path
        ]
        self.assertEqual(1, len(thread_payloads))
        self.assertNotIn("tourStatus", thread_payloads[0])
        self.assertNotIn("tourInvite", thread_payloads[0])

    def test_grouped_campaign_tour_resolution_preserves_core_provenance(self):
        outbox_ref = FakeDocRef("outbox-grouped-tour-action")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        grouped_thread = {
            "clientId": "client-1",
            "status": "paused",
            "rowNumber": 20,
            "source": "dashboard_campaign_launch",
            "actionType": "campaign_launch",
            "rows": [20, 21],
        }
        fake_fs.snapshots["users/uid-1/threads/thread-1"] = FakeSnapshot(grouped_thread)
        _seed_tour_notification(fake_fs)
        data = {
            **self._dashboard_manual_reply_doc().to_dict(),
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
        }
        outbox_ref._data.update(data)

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "delete_notification_and_decrement_counters"):
            resolution = email_module._reserve_dashboard_tour_action_resolution(
                "uid-1",
                outbox_ref,
                data,
            )
            email_module._finalize_successful_outbox_item(
                "uid-1",
                outbox_ref,
                data,
                send_result={
                    "sent": ["bp21harrison@gmail.com"],
                    "sentMessageIds": {
                        "bp21harrison@gmail.com": "graph-grouped-tour-reply-1",
                    },
                },
                dashboard_tour_resolution=resolution,
            )

        thread_path = (
            "collection", "users", "document", "uid-1",
            "collection", "threads", "document", "thread-1",
        )
        thread_payloads = [
            payload for path, payload, _merge in fake_fs.set_calls
            if path == thread_path
        ]
        self.assertEqual("sent", thread_payloads[0]["tourInvite"]["status"])
        self.assertNotIn("source", thread_payloads[0])
        self.assertNotIn("actionType", thread_payloads[0])
        self.assertEqual("dashboard_campaign_launch", grouped_thread["source"])
        self.assertEqual("campaign_launch", grouped_thread["actionType"])

    def test_server_tour_notification_bridges_legacy_reply_without_action_reason(self):
        doc = self._dashboard_manual_reply_doc({
            "source": "dashboard_inline_reply",
            "actionType": "reply",
        }, doc_id="outbox-legacy-tour-action")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        _seed_tour_notification(fake_fs)

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com"), \
             patch.object(email_module, "_send_outbox_as_reply", return_value={
                 "sent": True,
                 "error": None,
                 "sentMessageId": "graph-legacy-tour-1",
                 "internetMessageId": "<graph-legacy-tour-1@example.com>",
                 "conversationId": "conversation-1",
                 "toRecipients": ["bp21harrison@gmail.com"],
                 "ccRecipients": [],
                 "sentRecipients": ["bp21harrison@gmail.com"],
             }), \
             patch.object(email_module, "_save_outbox_reply_message"), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "highlight_row"), \
             patch.object(email_module, "delete_notification_and_decrement_counters"):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        thread = fake_fs.snapshots["users/uid-1/threads/thread-1"].to_dict()
        self.assertEqual("awaiting_confirmation", thread["tourStatus"])
        self.assertEqual("sent", thread["tourInvite"]["status"])

    def test_duplicate_dashboard_tour_outboxes_send_one_logical_reply(self):
        first = self._dashboard_manual_reply_doc({
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
        }, doc_id="outbox-tour-duplicate-a")
        second = self._dashboard_manual_reply_doc({
            "actionAuditId": "audit-tour-duplicate-b",
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
        }, doc_id="outbox-tour-duplicate-b")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        _seed_tour_notification(fake_fs)
        graph_result = {
            "sent": True,
            "error": None,
            "sentMessageId": "graph-tour-deduped-1",
            "internetMessageId": "<graph-tour-deduped-1@example.com>",
            "conversationId": "conversation-1",
            "toRecipients": ["bp21harrison@gmail.com"],
            "ccRecipients": [],
            "sentRecipients": ["bp21harrison@gmail.com"],
        }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com"), \
             patch.object(email_module, "_send_outbox_as_reply", return_value=graph_result) as send_reply, \
             patch.object(email_module, "_save_outbox_reply_message"), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "highlight_row"), \
             patch.object(email_module, "delete_notification_and_decrement_counters"):
            for doc in (first, second):
                email_module._send_single_outbox_item(
                    "uid-1",
                    {"Authorization": "Bearer token"},
                    {"doc": doc, "data": doc.to_dict()},
                )

        send_reply.assert_called_once()
        self.assertTrue(first.reference.deleted)
        self.assertTrue(second.reference.deleted)

    def test_dashboard_tour_graph_success_survives_reply_index_warning(self):
        doc = self._dashboard_manual_reply_doc({
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
        }, doc_id="outbox-tour-index-warning")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        _seed_tour_notification(fake_fs)

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com"), \
             patch.object(email_module, "_send_outbox_as_reply", return_value={
                 "sent": True,
                 "error": None,
                 "sentMessageId": "graph-tour-index-warning-1",
                 "internetMessageId": "<graph-tour-index-warning-1@example.com>",
                 "conversationId": "conversation-1",
                 "toRecipients": ["bp21harrison@gmail.com"],
                 "ccRecipients": [],
                 "sentRecipients": ["bp21harrison@gmail.com"],
             }) as graph_send, \
             patch.object(email_module, "_save_outbox_reply_message", return_value=False), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "highlight_row"), \
             patch.object(email_module, "delete_notification_and_decrement_counters"):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        graph_send.assert_called_once()
        self.assertTrue(doc.reference.deleted)
        self.assertEqual([], fake_fs.add_calls)
        thread = fake_fs.snapshots["users/uid-1/threads/thread-1"].to_dict()
        self.assertEqual("awaiting_confirmation", thread["tourStatus"])
        self.assertEqual("sent", thread["tourInvite"]["status"])
        resolution = fake_fs.snapshots[
            "users/uid-1/actionResolutions/notification-1"
        ].to_dict()
        self.assertIn("Reply history indexing failed", resolution["postSendWarning"])

    def test_dashboard_tour_graph_success_without_identity_still_persists_context(self):
        doc = self._dashboard_manual_reply_doc({
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
        }, doc_id="outbox-tour-identity-warning")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        _seed_tour_notification(fake_fs)

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com"), \
             patch.object(email_module, "_send_outbox_as_reply", return_value={
                 "sent": True,
                 "error": None,
                 "conversationId": "conversation-1",
                 "toRecipients": ["bp21harrison@gmail.com"],
                 "ccRecipients": [],
                 "sentRecipients": ["bp21harrison@gmail.com"],
             }) as graph_send, \
             patch.object(email_module, "_save_outbox_reply_message"), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "highlight_row"), \
             patch.object(email_module, "delete_notification_and_decrement_counters"):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        graph_send.assert_called_once()
        self.assertTrue(doc.reference.deleted)
        self.assertEqual([], fake_fs.add_calls)
        thread = fake_fs.snapshots["users/uid-1/threads/thread-1"].to_dict()
        self.assertEqual("awaiting_confirmation", thread["tourStatus"])
        self.assertEqual("sent", thread["tourInvite"]["status"])

    def test_dashboard_tour_send_terminal_race_never_resurrects_thread(self):
        doc = self._dashboard_manual_reply_doc({
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
        }, doc_id="outbox-tour-terminal-race")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        thread_key = "users/uid-1/threads/thread-1"
        fake_fs.snapshots[thread_key] = FakeSnapshot({
            "clientId": "client-1",
            "status": "paused",
            "rowNumber": 20,
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
        })
        _seed_tour_notification(fake_fs)

        def graph_send(*_args, **_kwargs):
            fake_fs.snapshots[thread_key] = FakeSnapshot({
                "clientId": "client-1",
                "status": "completed",
                "followUpStatus": "stopped",
                "statusReason": "manual_completion",
                "rowNumber": 20,
                "source": "dashboard_new_campaign",
                "actionType": "campaign_creation",
                "tourStatus": "confirmed",
            })
            return {
                "sent": True,
                "error": None,
                "sentMessageId": "graph-tour-terminal-race-1",
                "internetMessageId": "<graph-tour-terminal-race-1@example.com>",
                "conversationId": "conversation-1",
                "toRecipients": ["bp21harrison@gmail.com"],
                "ccRecipients": [],
                "sentRecipients": ["bp21harrison@gmail.com"],
            }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com"), \
             patch.object(email_module, "_send_outbox_as_reply", side_effect=graph_send), \
             patch.object(email_module, "_save_outbox_reply_message"), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "highlight_row"), \
             patch.object(email_module, "delete_notification_and_decrement_counters"):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        thread = fake_fs.snapshots[thread_key].to_dict()
        self.assertEqual("completed", thread["status"])
        self.assertEqual("stopped", thread["followUpStatus"])
        self.assertEqual("confirmed", thread["tourStatus"])
        self.assertEqual("manual_completion", thread["statusReason"])

    def test_dashboard_tour_send_stopped_client_race_never_resumes_thread(self):
        outbox_ref = FakeDocRef("outbox-tour-stopped-client-race")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        client_key = "users/uid-1/clients/client-1"
        thread_key = "users/uid-1/threads/thread-1"
        fake_fs.snapshots[client_key] = FakeSnapshot({"status": "live"})
        _seed_tour_notification(fake_fs)
        data = {
            **self._dashboard_manual_reply_doc().to_dict(),
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
        }
        outbox_ref._data.update(data)

        with patch("email_automation.clients._fs", fake_fs):
            resolution = email_module._reserve_dashboard_tour_action_resolution(
                "uid-1",
                outbox_ref,
                data,
            )
            self.assertEqual("reserved", resolution["status"])
            fake_fs.snapshots[client_key] = FakeSnapshot({
                "status": "stopped",
                "automationPaused": True,
            })
            email_module._finalize_successful_outbox_item(
                "uid-1",
                outbox_ref,
                {**data, "tourActionResolution": resolution["marker"]},
                send_result={
                    "sent": ["bp21harrison@gmail.com"],
                    "sentMessageIds": {
                        "bp21harrison@gmail.com": "graph-stopped-client-race-1",
                    },
                },
                dashboard_tour_resolution=resolution,
            )

        thread = fake_fs.snapshots[thread_key].to_dict()
        self.assertEqual("paused", thread["status"])
        self.assertNotIn("tourStatus", thread)
        client = fake_fs.snapshots[client_key].to_dict()
        self.assertEqual("stopped", client["status"])
        self.assertTrue(client["automationPaused"])

    def test_dashboard_tour_send_deleted_client_race_never_recreates_client(self):
        outbox_ref = FakeDocRef("outbox-tour-deleted-client-race")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        client_key = "users/uid-1/clients/client-1"
        thread_key = "users/uid-1/threads/thread-1"
        fake_fs.snapshots[client_key] = FakeSnapshot({"status": "live"})
        _seed_tour_notification(fake_fs)
        data = {
            **self._dashboard_manual_reply_doc().to_dict(),
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
        }
        outbox_ref._data.update(data)

        with patch("email_automation.clients._fs", fake_fs):
            resolution = email_module._reserve_dashboard_tour_action_resolution(
                "uid-1",
                outbox_ref,
                data,
            )
            self.assertEqual("reserved", resolution["status"])
            fake_fs.snapshots[client_key] = FakeSnapshot({}, exists=False)
            email_module._finalize_successful_outbox_item(
                "uid-1",
                outbox_ref,
                {**data, "tourActionResolution": resolution["marker"]},
                send_result={
                    "sent": ["bp21harrison@gmail.com"],
                    "sentMessageIds": {
                        "bp21harrison@gmail.com": "graph-deleted-client-race-1",
                    },
                },
                dashboard_tour_resolution=resolution,
            )

        thread = fake_fs.snapshots[thread_key].to_dict()
        self.assertEqual("paused", thread["status"])
        self.assertNotIn("tourStatus", thread)
        self.assertFalse(fake_fs.snapshots[client_key].exists)

    def test_dashboard_tour_send_preserves_nested_terminal_invite_race(self):
        outbox_ref = FakeDocRef("outbox-tour-nested-terminal-race")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        thread_key = "users/uid-1/threads/thread-1"
        _seed_tour_notification(fake_fs)
        data = {
            **self._dashboard_manual_reply_doc().to_dict(),
            "actionReason": "tour_requested",
            "source": "dashboard_inline_reply",
            "actionType": "reply",
        }
        outbox_ref._data.update(data)

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "highlight_row"):
            resolution = email_module._reserve_dashboard_tour_action_resolution(
                "uid-1",
                outbox_ref,
                data,
            )
            fake_fs.snapshots[thread_key] = FakeSnapshot({
                "clientId": "client-1",
                "status": "paused",
                "followUpStatus": "paused",
                "rowNumber": 20,
                "source": "dashboard_new_campaign",
                "actionType": "campaign_creation",
                "tourInvite": {"status": "confirmed", "confirmedAt": "later"},
            })
            email_module._finalize_successful_outbox_item(
                "uid-1",
                outbox_ref,
                data,
                send_result={
                    "sent": ["bp21harrison@gmail.com"],
                    "sentMessageIds": {
                        "bp21harrison@gmail.com": "graph-nested-terminal-race-1",
                    },
                },
                dashboard_tour_resolution=resolution,
            )

        thread = fake_fs.snapshots[thread_key].to_dict()
        self.assertEqual("paused", thread["status"])
        self.assertEqual("paused", thread["followUpStatus"])
        self.assertNotIn("tourStatus", thread)
        self.assertEqual("confirmed", thread["tourInvite"]["status"])
        self.assertEqual("later", thread["tourInvite"]["confirmedAt"])

    def test_core_dashboard_tour_bridge_does_not_expand_planner_predicate(self):
        self.assertFalse(email_module._is_tour_invite_outbox({
            "source": "dashboard_inline_reply",
            "actionType": "reply",
            "actionReason": "tour_requested",
        }))
        self.assertTrue(email_module._is_tour_invite_outbox({
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
        }))

    def test_dashboard_manual_reply_preserves_reviewed_reply_all_ccs_in_audit_and_history(self):
        doc = self._dashboard_manual_reply_doc({
            "ccEmails": ["baylor@manifoldengineering.ai"],
        })
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com"), \
             patch.object(email_module, "_send_outbox_as_reply", return_value={
                 "sent": True,
                 "error": None,
                 "sentMessageId": "graph-reply-message-1",
                 "internetMessageId": "<graph-reply-message-1@example.com>",
                 "conversationId": "conversation-1",
                 "toRecipients": ["bp21harrison@gmail.com"],
                 "ccRecipients": ["baylor@manifoldengineering.ai"],
                 "sentRecipients": [
                     "bp21harrison@gmail.com",
                     "baylor@manifoldengineering.ai",
                 ],
             }) as send_outbox_as_reply, \
             patch.object(email_module, "_save_outbox_reply_message") as save_outbox_reply_message, \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "highlight_row"), \
             patch.object(email_module, "delete_notification_and_decrement_counters"):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        send_outbox_as_reply.assert_called_once()
        self.assertEqual(
            ["baylor@manifoldengineering.ai"],
            send_outbox_as_reply.call_args.kwargs["fallback_cc_emails"],
        )
        self.assertEqual(
            ["baylor@manifoldengineering.ai"],
            save_outbox_reply_message.call_args.kwargs["cc_emails"],
        )
        audit_payload = _action_audit_payload(fake_fs, "uid-1", "audit-dashboard-reply")
        self.assertEqual("sent", audit_payload["status"])
        self.assertEqual(
            ["bp21harrison@gmail.com", "baylor@manifoldengineering.ai"],
            audit_payload["sentRecipients"],
        )

    def test_dashboard_manual_reply_recipient_mismatch_uses_reviewed_recipient_not_graph_reply(self):
        doc = self._dashboard_manual_reply_doc({
            "assignedEmails": ["bp21harrison+reviewed@gmail.com"],
        })
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="wrong-broker@example.com"), \
             patch.object(email_module, "_send_outbox_as_reply") as send_outbox_as_reply, \
             patch.object(email_module, "send_and_index_email", return_value={
                 "sent": ["bp21harrison+reviewed@gmail.com"],
                 "errors": {},
                 "sentMessageIds": {"bp21harrison+reviewed@gmail.com": "draft-reviewed"},
                 "internetMessageIds": {"bp21harrison+reviewed@gmail.com": "<reviewed@example.com>"},
                 "threadIds": {"bp21harrison+reviewed@gmail.com": "thread-reviewed"},
                 "conversationIds": {"bp21harrison+reviewed@gmail.com": "conversation-reviewed"},
             }) as send_and_index_email, \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "highlight_row"), \
             patch.object(email_module, "delete_notification_and_decrement_counters"):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        send_outbox_as_reply.assert_not_called()
        send_and_index_email.assert_called_once()
        self.assertEqual(["bp21harrison+reviewed@gmail.com"], send_and_index_email.call_args.args[3])
        self.assertTrue(doc.reference.deleted)
        audit_payload = _action_audit_payload(fake_fs, "uid-1", "audit-dashboard-reply")
        self.assertEqual("sent", audit_payload["status"])
        self.assertEqual("draft-reviewed", audit_payload["sentMessageId"])

    def test_dashboard_manual_reply_placeholder_dead_letters_before_graph_send(self):
        doc = self._dashboard_manual_reply_doc({
            "script": "Hi [NAME],\n\nCan you share details?\n\nThanks",
        })
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender") as get_reply_sender, \
             patch.object(email_module, "_send_outbox_as_reply") as send_outbox_as_reply, \
             patch.object(email_module, "send_and_index_email") as send_and_index_email:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        get_reply_sender.assert_not_called()
        send_outbox_as_reply.assert_not_called()
        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual("dead_lettered", dead_letter_payload["status"])
        self.assertIn("[NAME]", dead_letter_payload["failureReason"])
        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual("dead_lettered", audit_payload["status"])

    def test_dashboard_manual_reply_retry_reconciles_prior_sent_item_without_resending(self):
        doc = self._dashboard_manual_reply_doc({
            "attempts": 1,
            "lastError": "HTTPSConnectionPool read timed out",
            "lastSendAttemptAt": "2026-06-26T12:00:00Z",
        })
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        sent_match = {
            "id": "sent-dashboard-reply-1",
            "internetMessageId": "<sent-dashboard-reply-1@example.com>",
            "conversationId": "conversation-1",
            "subject": "RE: 0 Gemini Ave, Houston",
        }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_fetch_graph_message_metadata", return_value={
                 "conversationId": "conversation-1",
                 "subject": "RE: 0 Gemini Ave, Houston",
             }), \
             patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com"), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=sent_match) as sent_guard, \
             patch.object(email_module, "find_sent_conversation_continuation_for_retry") as continuation_guard, \
             patch.object(email_module, "_send_outbox_as_reply") as send_outbox_as_reply:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        sent_guard.assert_called_once()
        continuation_guard.assert_not_called()
        send_outbox_as_reply.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual("needs_reconciliation", dead_letter_payload["status"])
        self.assertTrue(dead_letter_payload["alreadySent"])
        self.assertEqual(["bp21harrison@gmail.com"], dead_letter_payload["sentRecipients"])
        self.assertEqual("sent-dashboard-reply-1", dead_letter_payload["sentMessageIds"]["bp21harrison@gmail.com"])
        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual("needs_reconciliation", audit_payload["status"])
        self.assertTrue(audit_payload["alreadySent"])

    def test_dashboard_manual_reply_retry_blocks_when_user_manually_continued(self):
        doc = self._dashboard_manual_reply_doc({
            "attempts": 1,
            "lastError": "HTTPSConnectionPool read timed out",
            "lastSendAttemptAt": "2026-06-26T12:00:00Z",
        })
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        manual_continuation = {
            "id": "manual-sent-1",
            "internetMessageId": "<manual-sent-1@example.com>",
            "conversationId": "conversation-1",
            "sentDateTime": "2026-06-26T12:04:00Z",
        }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_fetch_graph_message_metadata", return_value={
                 "conversationId": "conversation-1",
                 "subject": "RE: 0 Gemini Ave, Houston",
             }), \
             patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com"), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=None), \
             patch.object(email_module, "find_sent_conversation_continuation_for_retry", return_value=manual_continuation) as continuation_guard, \
             patch.object(email_module, "_send_outbox_as_reply") as send_outbox_as_reply:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        continuation_guard.assert_called_once()
        send_outbox_as_reply.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual("dead_lettered", dead_letter_payload["status"])
        self.assertIn("manually continued", dead_letter_payload["failureReason"])
        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual("dead_lettered", audit_payload["status"])

    def test_dashboard_manual_reply_failure_remains_visible_for_operator_retry(self):
        doc = self._dashboard_manual_reply_doc(doc_id="outbox-dashboard-retry")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com"), \
             patch.object(email_module, "_send_outbox_as_reply", return_value={
                 "sent": False,
                 "error": "Graph 500",
             }), \
             patch.object(email_module, "delete_notification_and_decrement_counters") as delete_notification:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        delete_notification.assert_not_called()
        self.assertFalse(doc.reference.deleted)
        retry_payload = doc.reference.set_calls[-1][0][0]
        self.assertEqual("retrying", retry_payload["status"])
        self.assertEqual(1, retry_payload["attempts"])
        self.assertEqual(None, retry_payload["processingBy"])
        self.assertIn("Graph 500", retry_payload["lastError"])
        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual("retrying", audit_payload["status"])
        self.assertEqual("outbox-dashboard-retry", audit_payload["outboxId"])
        self.assertIn("Graph 500", audit_payload["lastError"])

    def test_retryable_send_failure_updates_action_audit_with_visible_retry_state(self):
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison+leaguecity-row20@gmail.com"],
            "script": "Hi Ron,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "0 Gemini Ave, Houston",
            "rowNumber": 20,
            "attempts": 1,
            "actionAuditId": "audit-retry",
            "followUpConfig": {"enabled": False},
        }, doc_id="outbox-retry")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_select_script_for_recipient", return_value=doc.to_dict()["script"]), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=None), \
             patch.object(email_module, "send_and_index_email", return_value={
                 "sent": [],
                 "errors": {
                     "bp21harrison+leaguecity-row20@gmail.com": "Request failed after 3 attempts",
                 },
             }):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        self.assertFalse(doc.reference.deleted)
        retry_payload = doc.reference.set_calls[-1][0][0]
        self.assertEqual(retry_payload["attempts"], 2)
        self.assertEqual(retry_payload["processingBy"], None)
        self.assertEqual(retry_payload["processingAt"], None)
        self.assertIn("Request failed after 3 attempts", retry_payload["lastError"])

        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "retrying")
        self.assertEqual(audit_payload["outboxId"], "outbox-retry")
        self.assertEqual(audit_payload["attempts"], 2)
        self.assertEqual(audit_payload["maxAttempts"], email_module.MAX_OUTBOX_ATTEMPTS)
        self.assertIn("Request failed after 3 attempts", audit_payload["lastError"])

    def test_retry_with_matching_sent_item_moves_to_reconciliation_without_resending(self):
        recipient = "bp21harrison+leaguecity-row20@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi Ron,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "0 Gemini Ave, Houston",
            "rowNumber": 20,
            "attempts": 1,
            "lastError": "HTTPSConnectionPool read timed out",
            "lastSendAttemptAt": "2026-06-26T12:00:00Z",
            "actionAuditId": "audit-ambiguous-retry",
            "followUpConfig": {"enabled": False},
        }, doc_id="outbox-ambiguous-retry")
        fake_fs = FakeFirestore()
        sent_match = {
            "id": "sent-graph-1",
            "internetMessageId": "<sent-graph-1@example.com>",
            "conversationId": "conversation-1",
            "subject": "0 Gemini Ave, Houston",
        }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_select_script_for_recipient", return_value=doc.to_dict()["script"]), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=sent_match) as sent_guard, \
             patch.object(email_module, "send_and_index_email") as send_and_index_email:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        sent_guard.assert_called_once()
        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual(dead_letter_payload["status"], "needs_reconciliation")
        self.assertTrue(dead_letter_payload["alreadySent"])
        self.assertEqual(dead_letter_payload["sentRecipients"], [recipient])
        self.assertEqual(dead_letter_payload["sentMessageIds"][recipient], "sent-graph-1")
        self.assertEqual(dead_letter_payload["internetMessageIds"][recipient], "<sent-graph-1@example.com>")

        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "needs_reconciliation")
        self.assertTrue(audit_payload["alreadySent"])
        self.assertEqual(audit_payload["sentRecipients"], [recipient])

    def test_recovered_dead_letter_outbox_checks_sent_items_before_send(self):
        recipient = "bp21harrison+recovered@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi Ron,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "0 Gemini Ave, Houston",
            "rowNumber": 20,
            "attempts": 0,
            "status": "queued",
            "requiresSentItemsPreflight": True,
            "recoveryFromDeadLetterId": "dead-1",
            "recoveredAt": "2026-06-26T12:00:00Z",
            "actionAuditId": "audit-recovered",
            "followUpConfig": {"enabled": False},
        }, doc_id="outbox-recovered")
        fake_fs = FakeFirestore()
        sent_match = {
            "id": "sent-recovered-1",
            "internetMessageId": "<sent-recovered-1@example.com>",
            "conversationId": "conversation-1",
            "subject": "0 Gemini Ave, Houston",
        }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_select_script_for_recipient", return_value=doc.to_dict()["script"]), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=sent_match) as sent_guard, \
             patch.object(email_module, "send_and_index_email") as send_and_index_email:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        sent_guard.assert_called_once()
        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual(dead_letter_payload["status"], "needs_reconciliation")
        self.assertTrue(dead_letter_payload["alreadySent"])
        self.assertEqual(dead_letter_payload["sentMessageIds"][recipient], "sent-recovered-1")

    def test_recovered_dead_letter_outbox_blocks_manual_continuation_before_send(self):
        recipient = "bp21harrison+recovered@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi Ron,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "threadId": "thread-1",
            "replyToMessageId": "graph-message-1",
            "attempts": 0,
            "status": "queued",
            "requiresSentItemsPreflight": True,
            "recoveryFromDeadLetterId": "dead-1",
            "recoveredAt": "2026-06-26T12:00:00Z",
            "actionAuditId": "audit-recovered",
        }, doc_id="outbox-recovered")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        manual_continuation = {
            "id": "manual-sent-1",
            "internetMessageId": "<manual-sent-1@example.com>",
            "conversationId": "conv-thread-1",
            "sentDateTime": "2026-06-26T12:04:00Z",
        }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_thread_row_number", return_value=7), \
             patch.object(email_module, "_get_reply_message_sender", return_value=recipient), \
             patch.object(email_module, "_fetch_graph_message_metadata", return_value={
                 "conversationId": "conv-thread-1",
                 "subject": "RE: 0 Gemini Ave",
             }), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=None), \
             patch.object(email_module, "find_sent_conversation_continuation_for_retry", return_value=manual_continuation) as continuation_guard, \
             patch.object(email_module, "_send_outbox_as_reply") as send_reply:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        continuation_guard.assert_called_once()
        send_reply.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual(dead_letter_payload["status"], "dead_lettered")
        self.assertIn("manually continued", dead_letter_payload["failureReason"])

    def test_thread_reply_retry_passes_conversation_identity_to_sent_guard(self):
        recipient = "bp21harrison+reply@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi Ron,\n\nThat time works.\n\nThanks",
            "clientId": "client-1",
            "threadId": "thread-1",
            "replyToMessageId": "graph-message-1",
            "attempts": 1,
            "lastError": "Read timed out after Graph reply",
            "lastSendAttemptAt": "2026-06-26T12:00:00Z",
            "actionAuditId": "audit-thread-retry",
        }, doc_id="outbox-thread-retry")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        sent_match = {
            "id": "sent-reply-1",
            "internetMessageId": "<sent-reply-1@example.com>",
            "conversationId": "conv-thread-1",
            "subject": "RE: 0 Gemini Ave",
        }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_thread_row_number", return_value=7), \
             patch.object(email_module, "_get_reply_message_sender", return_value=recipient), \
             patch.object(email_module, "_fetch_graph_message_metadata", return_value={
                 "conversationId": "conv-thread-1",
                 "subject": "RE: 0 Gemini Ave",
             }), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=sent_match) as sent_guard, \
             patch.object(email_module, "_send_outbox_as_reply") as send_reply:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        sent_guard.assert_called_once()
        self.assertEqual(sent_guard.call_args.kwargs["conversation_id"], "conv-thread-1")
        self.assertEqual(sent_guard.call_args.kwargs["subject"], "RE: 0 Gemini Ave")
        send_reply.assert_not_called()
        self.assertTrue(doc.reference.deleted)

    def test_thread_reply_retry_blocks_when_conversation_was_manually_continued(self):
        recipient = "bp21harrison+reply@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi Ron,\n\nThat time works.\n\nThanks",
            "clientId": "client-1",
            "threadId": "thread-1",
            "replyToMessageId": "graph-message-1",
            "attempts": 1,
            "lastError": "Read timed out after Graph reply",
            "lastSendAttemptAt": "2026-06-26T12:00:00Z",
            "actionAuditId": "audit-thread-retry",
        }, doc_id="outbox-thread-retry")
        fake_fs = FakeFirestore()
        _seed_open_thread(fake_fs)
        manual_continuation = {
            "id": "manual-sent-1",
            "internetMessageId": "<manual-sent-1@example.com>",
            "conversationId": "conv-thread-1",
            "sentDateTime": "2026-06-26T12:04:00Z",
        }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_thread_row_number", return_value=7), \
             patch.object(email_module, "_get_reply_message_sender", return_value=recipient), \
             patch.object(email_module, "_fetch_graph_message_metadata", return_value={
                 "conversationId": "conv-thread-1",
                 "subject": "RE: 0 Gemini Ave",
             }), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=None), \
             patch.object(email_module, "find_sent_conversation_continuation_for_retry", return_value=manual_continuation) as continuation_guard, \
             patch.object(email_module, "_send_outbox_as_reply") as send_reply:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        continuation_guard.assert_called_once()
        self.assertEqual(continuation_guard.call_args.kwargs["conversation_id"], "conv-thread-1")
        send_reply.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual(dead_letter_payload["status"], "dead_lettered")
        self.assertIn("manually continued", dead_letter_payload["failureReason"])
        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "dead_lettered")
        self.assertIn("manually continued", audit_payload["failureReason"])

    def test_retry_guard_lookup_failure_dead_letters_without_resending(self):
        recipient = "bp21harrison+leaguecity-row20@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi Ron,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "0 Gemini Ave, Houston",
            "rowNumber": 20,
            "attempts": 1,
            "lastError": "HTTPSConnectionPool read timed out",
            "lastSendAttemptAt": "2026-06-26T12:00:00Z",
            "actionAuditId": "audit-guard-failed",
            "followUpConfig": {"enabled": False},
        }, doc_id="outbox-guard-failed")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_select_script_for_recipient", return_value=doc.to_dict()["script"]), \
             patch.object(
                 email_module,
                 "find_matching_sent_message_for_retry",
                 side_effect=email_module.SentMailGuardLookupError("Graph 401"),
             ), \
             patch.object(email_module, "send_and_index_email") as send_and_index_email:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual(dead_letter_payload["status"], "dead_lettered")
        self.assertIn("Sent Items retry guard could not verify prior send", dead_letter_payload["failureReason"])

    def test_partial_send_retry_keeps_only_failed_recipients(self):
        doc = FakeDoc({
            "assignedEmails": [
                "bp21harrison+sent@gmail.com",
                "bp21harrison+failed@gmail.com",
            ],
            "script": "Hi,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "123 Partial Send Way",
            "rowNumber": 21,
            "attempts": 0,
            "actionAuditId": "audit-partial",
            "followUpConfig": {"enabled": False},
        }, doc_id="outbox-partial")
        fake_fs = FakeFirestore()

        def send_result(_user_id, _headers, _script, recipients, **_kwargs):
            recipient = recipients[0]
            if recipient == "bp21harrison+sent@gmail.com":
                return {
                    "sent": [recipient],
                    "errors": {},
                    "sentMessageIds": {recipient: "graph-sent-1"},
                }
            return {
                "sent": [],
                "errors": {recipient: "Graph send failed"},
            }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_pause_client_outbox_item_if_needed", return_value=False), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_select_script_for_recipient", return_value=doc.to_dict()["script"]), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=None), \
             patch.object(email_module, "send_and_index_email", side_effect=send_result):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        retry_payload = doc.reference.set_calls[-1][0][0]
        self.assertEqual(["bp21harrison+failed@gmail.com"], retry_payload["assignedEmails"])
        self.assertEqual(["bp21harrison+sent@gmail.com"], retry_payload["sentRecipients"])
        self.assertIn("Graph send failed", retry_payload["lastError"])

        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "retrying")
        self.assertEqual(audit_payload["sentRecipients"], ["bp21harrison+sent@gmail.com"])
        self.assertEqual(audit_payload["remainingRecipients"], ["bp21harrison+failed@gmail.com"])

    def test_mid_batch_campaign_suppression_never_requeues_already_sent_recipient(self):
        sent_recipient = "bp21harrison+sent@gmail.com"
        waiting_recipient = "bp21harrison+waiting@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [sent_recipient, waiting_recipient],
            "script": "Hi,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "123 Maintenance Boundary Way",
            "rowNumber": 21,
            "attempts": 0,
            "actionAuditId": "audit-maintenance-boundary",
            "followUpConfig": {"enabled": False},
        }, doc_id="outbox-maintenance-boundary")
        fake_fs = FakeFirestore()

        def send_result(_user_id, _headers, _script, recipients, **_kwargs):
            recipient = recipients[0]
            if recipient == sent_recipient:
                return {
                    "sent": [recipient],
                    "errors": {},
                    "sentMessageIds": {recipient: "graph-sent-1"},
                }
            return {
                "sent": [],
                "errors": {recipient: "campaign_maintenance"},
                "campaignAutomationSuppressed": True,
                "campaignAutomationState": "blocked",
                "campaignAutomationReason": "campaign_maintenance",
                "campaignAutomationTerminal": False,
            }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_pause_client_outbox_item_if_needed", return_value=False), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_select_script_for_recipient", return_value=doc.to_dict()["script"]), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=None), \
             patch.object(email_module, "send_and_index_email", side_effect=send_result):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        retry_payload = next(
            call[0][0]
            for call in doc.reference.set_calls
            if call[0][0].get("automationSuppressedState")
        )
        self.assertEqual([waiting_recipient], retry_payload["assignedEmails"])
        self.assertEqual([sent_recipient], retry_payload["sentRecipients"])
        self.assertTrue(retry_payload["partialSend"])

    def test_mid_batch_suppression_preserves_recipient_found_in_sent_items(self):
        reconciled_recipient = "bp21harrison+reconciled@gmail.com"
        waiting_recipient = "bp21harrison+waiting@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [reconciled_recipient, waiting_recipient],
            "script": "Hi,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "123 Reconciliation Boundary Way",
            "rowNumber": 22,
            "attempts": 1,
            "actionAuditId": "audit-reconciliation-boundary",
            "followUpConfig": {"enabled": False},
        }, doc_id="outbox-reconciliation-boundary")
        fake_fs = FakeFirestore()

        def prior_send(_headers, _data, recipient, *_args, **_kwargs):
            if recipient == reconciled_recipient:
                return {
                    "sent": [recipient],
                    "sentMessageIds": {recipient: "graph-reconciled-1"},
                    "internetMessageIds": {recipient: "<graph-reconciled-1@example.com>"},
                }
            return {"sent": []}

        suppressed = {
            "sent": [],
            "errors": {waiting_recipient: "campaign_maintenance"},
            "campaignAutomationSuppressed": True,
            "campaignAutomationState": "blocked",
            "campaignAutomationReason": "campaign_maintenance",
            "campaignAutomationTerminal": False,
        }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_pause_client_outbox_item_if_needed", return_value=False), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_select_script_for_recipient", return_value=doc.to_dict()["script"]), \
             patch.object(email_module, "_sent_retry_reconciliation_result", side_effect=prior_send), \
             patch.object(email_module, "send_and_index_email", return_value=suppressed):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        retry_payload = next(
            call[0][0]
            for call in doc.reference.set_calls
            if call[0][0].get("automationSuppressedState")
        )
        self.assertEqual([waiting_recipient], retry_payload["assignedEmails"])
        self.assertEqual([reconciled_recipient], retry_payload["sentRecipients"])
        self.assertTrue(retry_payload["partialSend"])

    def test_graph_accepted_unindexed_outbox_moves_to_reconciliation_without_retry(self):
        recipient = "bp21harrison+unindexed@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "123 Reconciliation Way",
            "rowNumber": 22,
            "attempts": 0,
            "actionAuditId": "audit-reconciliation",
            "followUpConfig": {"enabled": False},
        }, doc_id="outbox-reconciliation")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_select_script_for_recipient", return_value=doc.to_dict()["script"]), \
             patch.object(email_module, "send_and_index_email", return_value={
                 "sent": [],
                 "errors": {recipient: "CRITICAL: Failed to index message ID after 3 attempts"},
                 "sentMessageIds": {recipient: "graph-accepted-1"},
                 "internetMessageIds": {recipient: "<accepted-1@example.com>"},
                 "threadIds": {recipient: "accepted-thread-1"},
                 "conversationIds": {recipient: "conversation-1"},
             }):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        self.assertTrue(doc.reference.deleted)
        self.assertEqual([], doc.reference.set_calls)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual(dead_letter_payload["status"], "needs_reconciliation")
        self.assertTrue(dead_letter_payload["alreadySent"])
        self.assertEqual(dead_letter_payload["assignedEmails"], [recipient])
        self.assertEqual(dead_letter_payload["sentRecipients"], [recipient])
        self.assertEqual(dead_letter_payload["sentMessageIds"][recipient], "graph-accepted-1")

        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "needs_reconciliation")
        self.assertTrue(audit_payload["alreadySent"])
        self.assertEqual(audit_payload["sentRecipients"], [recipient])
        self.assertEqual(audit_payload["sentMessageIds"][recipient], "graph-accepted-1")

    def test_partial_retry_success_unions_prior_sent_and_clears_partial_audit_state(self):
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison+failed@gmail.com"],
            "sentRecipients": ["bp21harrison+sent@gmail.com"],
            "partialSend": True,
            "remainingRecipients": ["bp21harrison+failed@gmail.com"],
            "script": "Hi,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "123 Partial Send Way",
            "rowNumber": 21,
            "attempts": 1,
            "actionAuditId": "audit-partial",
            "followUpConfig": {"enabled": False},
        }, doc_id="outbox-partial")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_select_script_for_recipient", return_value=doc.to_dict()["script"]), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=None), \
             patch.object(email_module, "send_and_index_email", return_value={
                 "sent": ["bp21harrison+failed@gmail.com"],
                 "errors": {},
                 "sentMessageIds": {"bp21harrison+failed@gmail.com": "graph-sent-2"},
                 "internetMessageIds": {"bp21harrison+failed@gmail.com": "<sent-2@example.com>"},
             }):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        self.assertTrue(doc.reference.deleted)
        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "sent")
        self.assertEqual(
            audit_payload["sentRecipients"],
            ["bp21harrison+sent@gmail.com", "bp21harrison+failed@gmail.com"],
        )
        self.assertEqual(audit_payload["remainingRecipients"], [])
        self.assertFalse(audit_payload["partialSend"])

    def test_decrement_notification_rollups_clamps_counts(self):
        updated = notifications_module._decrement_notification_rollups(
            {
                "notificationsUnread": 1,
                "newUpdateCount": 0,
                "notifCounts": {"action_needed": 1, "sheet_update": 3},
            },
            "action_needed",
        )

        self.assertEqual(updated["notificationsUnread"], 0)
        self.assertEqual(updated["newUpdateCount"], 0)
        self.assertEqual(updated["notifCounts"], {"sheet_update": 3})

    def test_decrement_notification_rollups_handles_sheet_update_count(self):
        updated = notifications_module._decrement_notification_rollups(
            {
                "notificationsUnread": 4,
                "newUpdateCount": 2,
                "notifCounts": {"sheet_update": 2},
            },
            "sheet_update",
        )

        self.assertEqual(updated["notificationsUnread"], 3)
        self.assertEqual(updated["newUpdateCount"], 1)
        self.assertEqual(updated["notifCounts"], {"sheet_update": 1})

    def test_duplicate_suppression_terminalizes_action_audit(self):
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison@gmail.com"],
            "script": "Hi,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "123 Duplicate St, Testville",
            "rowNumber": 12,
            "actionAuditId": "audit-duplicate",
        }, doc_id="outbox-duplicate")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=True), \
             patch.object(email_module, "send_and_index_email") as send_and_index_email:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "duplicate_skipped")
        self.assertEqual(audit_payload["outboxId"], "outbox-duplicate")

    def test_contact_opt_out_terminalizes_action_audit_for_grouped_item(self):
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison@gmail.com"],
            "script": "Hi,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "123 Opt Out St, Testville",
            "rowNumber": 12,
            "actionAuditId": "audit-opt-out",
        }, doc_id="outbox-opt-out")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), \
             patch("email_automation.processing.is_contact_opted_out", return_value={"reason": "unsubscribe"}), \
             patch.object(email_module, "send_and_index_email") as send_and_index_email:
            email_module._send_multi_property_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "bp21harrison@gmail.com",
                [{"doc": doc, "data": doc.to_dict()}],
            )

        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "opt_out_skipped")
        self.assertEqual(audit_payload["outboxId"], "outbox-opt-out")

    def test_grouped_outbox_replaces_name_placeholder_before_send_guard(self):
        recipient = "bp21harrison+grouped@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi [first name],\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "123 Grouped Name Way",
            "rowNumber": 12,
            "contactName": "Morgan Lake",
            "actionAuditId": "audit-grouped-name",
        }, doc_id="outbox-grouped-name")
        captured = {}

        def record_guard(_user_id, _doc_ref, _data, body):
            captured["guard_body"] = body
            return False

        def record_send(_user_id, _headers, script, *_args, **_kwargs):
            captured["send_body"] = script
            return {
                "sent": [recipient],
                "errors": {},
            }

        with patch("email_automation.clients._fs", FakeFirestore()), \
             patch("email_automation.processing.is_contact_opted_out", return_value=None), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_dead_letter_unsafe_outbound_body_if_needed", side_effect=record_guard), \
             patch.object(email_module, "send_and_index_email", side_effect=record_send), \
             patch.object(email_module, "_finalize_successful_outbox_item"):
            email_module._send_multi_property_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                recipient,
                [{"doc": doc, "data": doc.to_dict()}],
            )

        self.assertEqual("Hi Morgan,\n\nCan you share details?\n\nThanks", captured["guard_body"])
        self.assertEqual("Hi Morgan,\n\nCan you share details?\n\nThanks", captured["send_body"])

    def test_grouped_graph_accepted_unindexed_outbox_moves_to_reconciliation_without_retry(self):
        recipient = "bp21harrison+grouped@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "123 Grouped Reconciliation Way",
            "rowNumber": 23,
            "attempts": 0,
            "actionAuditId": "audit-grouped-reconciliation",
            "followUpConfig": {"enabled": False},
        }, doc_id="outbox-grouped-reconciliation")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), \
             patch("email_automation.processing.is_contact_opted_out", return_value=None), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "send_and_index_email", return_value={
                 "sent": [],
                 "errors": {recipient: "CRITICAL: Failed to index message ID after 3 attempts"},
                 "sentMessageIds": {recipient: "graph-grouped-accepted-1"},
                 "internetMessageIds": {recipient: "<grouped-accepted-1@example.com>"},
                 "threadIds": {recipient: "grouped-thread-1"},
                 "conversationIds": {recipient: "grouped-conversation-1"},
             }):
            email_module._send_multi_property_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                recipient,
                [{"doc": doc, "data": doc.to_dict()}],
            )

        self.assertTrue(doc.reference.deleted)
        self.assertEqual([], doc.reference.set_calls)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual(dead_letter_payload["status"], "needs_reconciliation")
        self.assertTrue(dead_letter_payload["alreadySent"])
        self.assertEqual(dead_letter_payload["assignedEmails"], [recipient])
        self.assertEqual(dead_letter_payload["sentMessageIds"][recipient], "graph-grouped-accepted-1")

        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "needs_reconciliation")
        self.assertTrue(audit_payload["alreadySent"])
        self.assertEqual(audit_payload["sentRecipients"], [recipient])


class DailySendCapTests(unittest.TestCase):
    """Rail 2 — aggregate daily send cap (fail-closed, off-by-default-SAFE)."""

    def _two_recipient_docs(self):
        return [
            FakeDoc({
                "assignedEmails": ["bp21harrison+one@gmail.com"],
                "script": "Hi Avery",
                "clientId": "client-1",
                "subject": "100 Cap Way",
                "rowNumber": 3,
            }, doc_id="outbox-1"),
            FakeDoc({
                "assignedEmails": ["bp21harrison+two@gmail.com"],
                "script": "Hi Blake",
                "clientId": "client-1",
                "subject": "200 Cap Way",
                "rowNumber": 4,
            }, doc_id="outbox-2"),
        ]

    def _same_recipient_docs(self, count):
        return [
            FakeDoc({
                "assignedEmails": ["broker@example.invalid"],
                "script": f"Hi Broker, property {index}",
                "clientId": "client-1",
                "subject": f"{100 + index} Cap Way",
                "rowNumber": 3 + index,
            }, doc_id=f"outbox-batch-{index}")
            for index in range(count)
        ]

    def _direct_multi_recipient_doc(self):
        return FakeDoc({
            "assignedEmails": [
                "first-broker@example.invalid",
                "second-broker@example.invalid",
            ],
            "script": "Hi Broker, can you share the property details?",
            "scriptSelectionMode": "exact",
            "clientId": "client-1",
            "subject": "100 Direct Cap Way",
            "rowNumber": 3,
        }, doc_id="outbox-direct-multi-recipient")

    def _combined_docs(self, count=3):
        docs = self._same_recipient_docs(count)
        for doc in docs:
            doc._data["sendMode"] = "combined"
        return docs

    def _run_provider_send_cap_case(self, docs, *, scope, current, cap=20):
        day_key = email_module._send_counter_day_key()
        health_sink = []
        fake_fs = FakeFirestoreWithOutbox(
            docs,
            counter_store={day_key: current} if scope == "user" else {},
            health_sink=health_sink,
        )
        env = {
            "SITESIFT_DAILY_SEND_CAP": str(cap) if scope == "user" else "0",
            "SITESIFT_GLOBAL_DAILY_SEND_CAP": str(cap) if scope == "global" else "0",
        }

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, env))
            stack.enter_context(patch("email_automation.clients._fs", fake_fs))
            if scope == "global":
                stack.enter_context(patch.object(
                    email_module,
                    "_read_global_send_count",
                    return_value=current,
                ))
            single_send = stack.enter_context(
                patch.object(email_module, "_send_single_outbox_item")
            )
            separate_send = stack.enter_context(
                patch.object(email_module, "_send_multi_property_email")
            )
            combined_send = stack.enter_context(
                patch.object(email_module, "_send_combined_property_email")
            )
            increment_user = stack.enter_context(
                patch.object(email_module, "_increment_daily_send_count")
            )
            increment_global = stack.enter_context(
                patch.object(email_module, "_increment_global_send_count")
            )
            stack.enter_context(patch.object(email_module.time, "sleep", return_value=None))
            email_module.send_outboxes("uid-1", {"Authorization": "Bearer t"})

        return {
            "day_key": day_key,
            "fake_fs": fake_fs,
            "health_sink": health_sink,
            "single_send": single_send,
            "separate_send": separate_send,
            "combined_send": combined_send,
            "increment_user": increment_user,
            "increment_global": increment_global,
        }

    def test_direct_multi_recipient_overflow_blocks_user_and_global_caps(self):
        for scope in ("user", "global"):
            with self.subTest(scope=scope):
                doc = self._direct_multi_recipient_doc()
                result = self._run_provider_send_cap_case(
                    [doc],
                    scope=scope,
                    current=19,
                )

                result["single_send"].assert_not_called()
                result["increment_user"].assert_not_called()
                result["increment_global"].assert_not_called()
                self.assertFalse(doc.reference.deleted)
                self.assertEqual(len(result["health_sink"]), 1)
                self.assertEqual(
                    result["health_sink"][0][0]["sendCap"]["reason"],
                    email_module.DAILY_CAP_REACHED_REASON,
                )

    def test_direct_multi_recipient_exact_fill_counts_two_provider_sends(self):
        for scope in ("user", "global"):
            with self.subTest(scope=scope):
                result = self._run_provider_send_cap_case(
                    [self._direct_multi_recipient_doc()],
                    scope=scope,
                    current=18,
                )

                result["single_send"].assert_called_once()
                expected_increment = (
                    result["increment_user"]
                    if scope == "user"
                    else result["increment_global"]
                )
                expected_args = (
                    (result["fake_fs"], "uid-1", result["day_key"], 2)
                    if scope == "user"
                    else (result["fake_fs"], result["day_key"], 2)
                )
                expected_increment.assert_called_once_with(*expected_args)
                self.assertEqual(result["health_sink"], [])

    def test_combined_mode_exact_fill_counts_one_provider_send(self):
        for scope in ("user", "global"):
            with self.subTest(scope=scope):
                result = self._run_provider_send_cap_case(
                    self._combined_docs(),
                    scope=scope,
                    current=19,
                )

                result["combined_send"].assert_called_once()
                result["separate_send"].assert_not_called()
                expected_increment = (
                    result["increment_user"]
                    if scope == "user"
                    else result["increment_global"]
                )
                expected_args = (
                    (result["fake_fs"], "uid-1", result["day_key"], 1)
                    if scope == "user"
                    else (result["fake_fs"], result["day_key"], 1)
                )
                expected_increment.assert_called_once_with(*expected_args)
                self.assertEqual(result["health_sink"], [])

    def test_combined_mode_overflow_blocks_user_and_global_caps(self):
        for scope in ("user", "global"):
            with self.subTest(scope=scope):
                docs = self._combined_docs()
                result = self._run_provider_send_cap_case(
                    docs,
                    scope=scope,
                    current=20,
                )

                result["combined_send"].assert_not_called()
                result["increment_user"].assert_not_called()
                result["increment_global"].assert_not_called()
                self.assertTrue(all(not doc.reference.deleted for doc in docs))
                self.assertEqual(len(result["health_sink"]), 1)
                self.assertEqual(
                    result["health_sink"][0][0]["sendCap"]["reason"],
                    email_module.DAILY_CAP_REACHED_REASON,
                )

    def test_user_cap_blocks_group_when_batch_would_exceed_remaining_capacity(self):
        docs = self._same_recipient_docs(10)
        day_key = email_module._send_counter_day_key()
        health_sink = []
        fake_fs = FakeFirestoreWithOutbox(
            docs,
            counter_store={day_key: 18},
            health_sink=health_sink,
        )

        with patch.dict(os.environ, {
            "SITESIFT_DAILY_SEND_CAP": "20",
            "SITESIFT_GLOBAL_DAILY_SEND_CAP": "0",
        }), patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_send_multi_property_email") as grouped_send, \
             patch.object(email_module, "_send_single_outbox_item") as single_send, \
             patch.object(email_module, "_increment_daily_send_count") as increment, \
             patch.object(email_module.time, "sleep", return_value=None):
            email_module.send_outboxes("uid-1", {"Authorization": "Bearer t"})

        grouped_send.assert_not_called()
        single_send.assert_not_called()
        increment.assert_not_called()
        self.assertTrue(all(not doc.reference.deleted for doc in docs))
        self.assertEqual(fake_fs.counter_store[day_key], 18)
        self.assertEqual(len(health_sink), 1)
        self.assertEqual(health_sink[0][0]["sendCap"]["status"], "warning")
        self.assertEqual(
            health_sink[0][0]["sendCap"]["reason"],
            email_module.DAILY_CAP_REACHED_REASON,
        )
        self.assertEqual(health_sink[0][0]["sendCap"]["count"], 18)

    def test_user_cap_allows_group_that_exactly_fills_remaining_capacity(self):
        docs = self._same_recipient_docs(2)
        day_key = email_module._send_counter_day_key()
        health_sink = []
        fake_fs = FakeFirestoreWithOutbox(
            docs,
            counter_store={day_key: 18},
            health_sink=health_sink,
        )

        with patch.dict(os.environ, {
            "SITESIFT_DAILY_SEND_CAP": "20",
            "SITESIFT_GLOBAL_DAILY_SEND_CAP": "0",
        }), patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_send_multi_property_email") as grouped_send, \
             patch.object(email_module, "_increment_daily_send_count") as increment, \
             patch.object(email_module.time, "sleep", return_value=None):
            email_module.send_outboxes("uid-1", {"Authorization": "Bearer t"})

        grouped_send.assert_called_once()
        increment.assert_called_once_with(fake_fs, "uid-1", day_key, 2)
        self.assertEqual(health_sink, [])

    def test_global_cap_blocks_group_when_batch_would_exceed_remaining_capacity(self):
        docs = self._same_recipient_docs(3)
        day_key = email_module._send_counter_day_key()
        health_sink = []
        fake_fs = FakeFirestoreWithOutbox(docs, health_sink=health_sink)

        with patch.dict(os.environ, {
            "SITESIFT_DAILY_SEND_CAP": "0",
            "SITESIFT_GLOBAL_DAILY_SEND_CAP": "20",
        }), patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_read_global_send_count", return_value=18), \
             patch.object(email_module, "_send_multi_property_email") as grouped_send, \
             patch.object(email_module, "_send_single_outbox_item") as single_send, \
             patch.object(email_module, "_increment_global_send_count") as increment, \
             patch.object(email_module.time, "sleep", return_value=None):
            email_module.send_outboxes("uid-1", {"Authorization": "Bearer t"})

        grouped_send.assert_not_called()
        single_send.assert_not_called()
        increment.assert_not_called()
        self.assertTrue(all(not doc.reference.deleted for doc in docs))
        self.assertEqual(len(health_sink), 1)
        self.assertEqual(health_sink[0][0]["sendCap"]["status"], "warning")
        self.assertEqual(
            health_sink[0][0]["sendCap"]["reason"],
            email_module.DAILY_CAP_REACHED_REASON,
        )
        self.assertEqual(health_sink[0][0]["sendCap"]["count"], 18)
        self.assertEqual(health_sink[0][0]["sendCap"]["scope"], "global")

    def test_global_cap_allows_group_that_exactly_fills_remaining_capacity(self):
        docs = self._same_recipient_docs(2)
        day_key = email_module._send_counter_day_key()
        health_sink = []
        fake_fs = FakeFirestoreWithOutbox(docs, health_sink=health_sink)

        with patch.dict(os.environ, {
            "SITESIFT_DAILY_SEND_CAP": "0",
            "SITESIFT_GLOBAL_DAILY_SEND_CAP": "20",
        }), patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_read_global_send_count", return_value=18), \
             patch.object(email_module, "_send_multi_property_email") as grouped_send, \
             patch.object(email_module, "_increment_global_send_count") as increment, \
             patch.object(email_module.time, "sleep", return_value=None):
            email_module.send_outboxes("uid-1", {"Authorization": "Bearer t"})

        grouped_send.assert_called_once()
        increment.assert_called_once_with(fake_fs, day_key, 2)
        self.assertEqual(health_sink, [])

    def test_pending_finalization_repairs_at_cap_without_graph_or_counter_access(self):
        day_key = email_module._send_counter_day_key()
        doc = FakeDoc({
            "assignedEmails": ["broker@example.invalid"],
            "clientId": "client-1",
            "rowNumber": 3,
            "notificationId": "notification-1",
            "status": email_module.DASHBOARD_TOUR_PENDING_STATUS,
            "alreadySent": True,
            "pendingSendResult": {
                "sent": ["broker@example.invalid"],
                "sentMessageIds": {"broker@example.invalid": "graph-message-1"},
            },
            "tourActionResolution": {
                "kind": email_module.DASHBOARD_TOUR_RESOLUTION_KIND,
                "notificationId": "notification-1",
            },
        }, doc_id="outbox-pending-finalization")
        fake_fs = FakeFirestoreWithOutbox(
            [doc],
            counter_store={day_key: 20},
        )

        def finalize(_uid, doc_ref, *_args, **_kwargs):
            doc_ref.delete()

        with patch.dict(os.environ, {
            "SITESIFT_DAILY_SEND_CAP": "20",
            "SITESIFT_GLOBAL_DAILY_SEND_CAP": "20",
        }), patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_current_outbox_data", return_value=doc.to_dict()), \
             patch.object(email_module, "_finalize_successful_outbox_item", side_effect=finalize) as finalizer, \
             patch.object(email_module, "_read_daily_send_count") as read_user_count, \
             patch.object(email_module, "_read_global_send_count") as read_global_count, \
             patch.object(email_module, "_increment_daily_send_count") as increment_user, \
             patch.object(email_module, "_increment_global_send_count") as increment_global, \
             patch.object(email_module, "resolve_outbound_mode") as outbound_mode, \
             patch.object(email_module, "_send_outbox_as_reply") as graph_reply, \
             patch.object(email_module, "send_and_index_email") as graph_send:
            email_module.send_outboxes("uid-1", {"Authorization": "Bearer t"})

        finalizer.assert_called_once()
        self.assertTrue(doc.reference.deleted)
        read_user_count.assert_not_called()
        read_global_count.assert_not_called()
        increment_user.assert_not_called()
        increment_global.assert_not_called()
        outbound_mode.assert_not_called()
        graph_reply.assert_not_called()
        graph_send.assert_not_called()

    def test_send_is_suppressed_and_queue_retained_when_daily_cap_reached(self):
        """The cap+1 send must be blocked and the outbox retained, not drained."""
        docs = self._two_recipient_docs()
        day_key = email_module._send_counter_day_key()
        health_sink = []
        fake_fs = FakeFirestoreWithOutbox(
            docs,
            counter_store={day_key: 1},  # already at the cap
            health_sink=health_sink,
        )
        sends = []

        def record_single_send(_uid, _headers, item, *_a, **_k):
            sends.append(item)

        with patch.dict(os.environ, {"SITESIFT_DAILY_SEND_CAP": "1"}), \
             patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_send_single_outbox_item", side_effect=record_single_send), \
             patch.object(email_module, "_send_multi_property_email", side_effect=record_single_send), \
             patch.object(email_module.time, "sleep", return_value=None) as sleep:
            email_module.send_outboxes("uid-1", {"Authorization": "Bearer t"})

        self.assertEqual(sends, [], "cap-reached: no email may be sent")
        self.assertFalse(docs[0].reference.deleted, "queue must be retained")
        self.assertFalse(docs[1].reference.deleted, "queue must be retained")
        sleep.assert_not_called()
        self.assertEqual(fake_fs.counter_store[day_key], 1, "counter must not advance")
        reasons = [p.get("sendCap", {}).get("reason") for p, _m in health_sink]
        self.assertIn(email_module.DAILY_CAP_REACHED_REASON, reasons)
        statuses = [p.get("sendCap", {}).get("status") for p, _m in health_sink]
        self.assertIn("warning", statuses)

    def test_sends_proceed_under_cap_and_counter_increments(self):
        docs = self._two_recipient_docs()
        day_key = email_module._send_counter_day_key()
        fake_fs = FakeFirestoreWithOutbox(docs, counter_store={})
        sends = []

        def record_single_send(_uid, _headers, item, *_a, **_k):
            sends.append(item)

        with patch.dict(os.environ, {"SITESIFT_DAILY_SEND_CAP": "5"}), \
             patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_send_single_outbox_item", side_effect=record_single_send), \
             patch.object(email_module.time, "sleep", return_value=None):
            email_module.send_outboxes("uid-1", {"Authorization": "Bearer t"})

        self.assertEqual(len(sends), 2, "both under-cap recipients should send")
        self.assertEqual(fake_fs.counter_store[day_key], 2, "counter must reflect 2 sends")

    def test_partial_drain_stops_exactly_at_cap(self):
        """First send allowed, counter hits cap, second recipient is retained."""
        docs = self._two_recipient_docs()
        day_key = email_module._send_counter_day_key()
        fake_fs = FakeFirestoreWithOutbox(docs, counter_store={})
        sends = []

        def record_single_send(_uid, _headers, item, *_a, **_k):
            sends.append(item)

        with patch.dict(os.environ, {"SITESIFT_DAILY_SEND_CAP": "1"}), \
             patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_send_single_outbox_item", side_effect=record_single_send), \
             patch.object(email_module.time, "sleep", return_value=None):
            email_module.send_outboxes("uid-1", {"Authorization": "Bearer t"})

        self.assertEqual(len(sends), 1, "exactly one send before hitting the cap")
        self.assertEqual(fake_fs.counter_store[day_key], 1)
        self.assertFalse(docs[1].reference.deleted, "second recipient must be retained")

    def test_fails_closed_when_counter_unreadable(self):
        """If the shared counter cannot be read, draining STOPS (fail-closed)."""
        docs = self._two_recipient_docs()
        health_sink = []
        fake_fs = FakeFirestoreWithOutbox(
            docs, counter_store={}, health_sink=health_sink, counter_raise_on_get=True
        )
        sends = []

        def record_single_send(_uid, _headers, item, *_a, **_k):
            sends.append(item)

        with patch.dict(os.environ, {"SITESIFT_DAILY_SEND_CAP": "500"}), \
             patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_send_single_outbox_item", side_effect=record_single_send), \
             patch.object(email_module.time, "sleep", return_value=None):
            email_module.send_outboxes("uid-1", {"Authorization": "Bearer t"})

        self.assertEqual(sends, [], "unreadable counter must block all sends")
        reasons = [p.get("sendCap", {}).get("reason") for p, _m in health_sink]
        self.assertIn(email_module.DAILY_CAP_COUNTER_UNAVAILABLE_REASON, reasons)

    def test_explicit_zero_disables_rail_and_never_touches_counter(self):
        """Explicit opt-out ('0') sends unbounded and does not read the counter."""
        docs = self._two_recipient_docs()
        day_key = email_module._send_counter_day_key()
        # Counter preset absurdly high; if the rail read it, sends would stop.
        fake_fs = FakeFirestoreWithOutbox(
            docs, counter_store={day_key: 10_000}, counter_raise_on_get=True
        )
        sends = []

        def record_single_send(_uid, _headers, item, *_a, **_k):
            sends.append(item)

        with patch.dict(os.environ, {"SITESIFT_DAILY_SEND_CAP": "0"}), \
             patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_send_single_outbox_item", side_effect=record_single_send), \
             patch.object(email_module.time, "sleep", return_value=None):
            email_module.send_outboxes("uid-1", {"Authorization": "Bearer t"})

        self.assertEqual(len(sends), 2, "explicit disable must send unbounded")

    def test_unset_env_keeps_rail_on_at_default_ceiling(self):
        """Absence of config must NOT silently disable the rail."""
        self.assertEqual(email_module._resolve_daily_send_cap(),
                         email_module.DEFAULT_DAILY_SEND_CAP)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SITESIFT_DAILY_SEND_CAP", None)
            self.assertEqual(email_module._resolve_daily_send_cap(),
                             email_module.DEFAULT_DAILY_SEND_CAP)

    def test_unparseable_cap_falls_back_to_default_not_disabled(self):
        with patch.dict(os.environ, {"SITESIFT_DAILY_SEND_CAP": "banana"}):
            self.assertEqual(email_module._resolve_daily_send_cap(),
                             email_module.DEFAULT_DAILY_SEND_CAP)


class SendModeCombineTests(unittest.TestCase):
    """sendMode='combined' — one email per broker, atomic across the broker's rows."""

    RECIPIENT = "bp21harrison+golden@gmail.com"
    ADDRS = ["100 Dashboard Way", "200 Interference Rd", "300 Drip Feed Dr"]

    def _same_broker_docs(self, *, send_mode=None, count=3):
        docs = []
        for i in range(count):
            data = {
                "assignedEmails": [self.RECIPIENT],
                "script": "Hi Avery,\n\n- 100 Dashboard Way\n- 200 Interference Rd\n- 300 Drip Feed Dr",
                "clientId": "client-1",
                "subject": self.ADDRS[i],
                "rowNumber": 3 + i,
                "contactName": "Avery Rep",
                "combinedSubject": "100 Dashboard Way (+2 more)",
            }
            if send_mode is not None:
                data["sendMode"] = send_mode
            docs.append(FakeDoc(data, doc_id=f"outbox-{i + 1}"))
        return docs

    @staticmethod
    def _items(docs):
        return [{"doc": d, "data": d.to_dict()} for d in docs]

    def _combined_patches(self, stack, *, existing=False):
        """Enter the common combined-send patches; return (finalize, dead_letter) mocks."""
        def p(name, **kw):
            return stack.enter_context(patch.object(email_module, name, **kw))

        stack.enter_context(patch("email_automation.clients._fs", FakeFirestore()))
        stack.enter_context(patch("email_automation.processing.is_contact_opted_out", return_value=None))
        stack.enter_context(patch.object(
            email_module,
            "get_client_automation_pause",
            return_value=(False, "", {
                "status": "live",
                "columnConfig": get_default_column_config(),
            }),
        ))
        p("_claim_outbox_item", return_value=True)
        p("_get_current_outbox_data", return_value={})
        p("_pause_client_outbox_item_if_needed", return_value=False)
        p("_dead_letter_campaign_recipient_row_mismatch_if_needed", return_value=False)
        if callable(existing):
            p("_has_existing_thread_for_property", side_effect=existing)
        else:
            p("_has_existing_thread_for_property", return_value=bool(existing))
        p("_dead_letter_unresolved_name_placeholder_if_needed", return_value=False)
        p("_dead_letter_unsafe_outbound_body_if_needed", return_value=False)
        p("_sent_retry_reconciliation_result", return_value={"sent": []})
        p("_fresh_graph_headers", side_effect=lambda h, prov=None: h)
        p("_send_identity_recipients", return_value=[])
        p("_terminalize_outbox_action_audit", return_value=None)
        p("_mark_outbox_action_audit_retrying", return_value=None)
        finalize = p("_finalize_successful_outbox_item")
        dead_letter = p("_move_to_dead_letter")
        return finalize, dead_letter

    # --- routing (Step 1 branch) -------------------------------------------
    def test_send_outboxes_defaults_to_separate_when_sendmode_absent(self):
        docs = self._same_broker_docs(send_mode=None, count=2)
        fake_fs = FakeFirestoreWithOutbox(docs, counter_store={})
        calls = {"multi": 0, "combined": 0}
        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_send_multi_property_email",
                          side_effect=lambda *a, **k: calls.__setitem__("multi", calls["multi"] + 1)), \
             patch.object(email_module, "_send_combined_property_email",
                          side_effect=lambda *a, **k: calls.__setitem__("combined", calls["combined"] + 1)), \
             patch.object(email_module.time, "sleep", return_value=None):
            email_module.send_outboxes("uid-1", {"Authorization": "Bearer t"})
        self.assertEqual(calls["multi"], 1, "absent sendMode → separate (multi) path")
        self.assertEqual(calls["combined"], 0, "must not combine without opt-in")

    def test_send_outboxes_routes_to_combined_when_sendmode_combined(self):
        docs = self._same_broker_docs(send_mode="combined", count=2)
        fake_fs = FakeFirestoreWithOutbox(docs, counter_store={})
        calls = {"multi": 0, "combined": 0}
        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_send_multi_property_email",
                          side_effect=lambda *a, **k: calls.__setitem__("multi", calls["multi"] + 1)), \
             patch.object(email_module, "_send_combined_property_email",
                          side_effect=lambda *a, **k: calls.__setitem__("combined", calls["combined"] + 1)), \
             patch.object(email_module.time, "sleep", return_value=None):
            email_module.send_outboxes("uid-1", {"Authorization": "Bearer t"})
        self.assertEqual(calls["combined"], 1, "sendMode=combined → combined path")
        self.assertEqual(calls["multi"], 0, "must not also run the separate path")

    # --- combined sender behavior ------------------------------------------
    def test_combined_send_collapses_to_one_email_and_finalizes_all_rows(self):
        docs = self._same_broker_docs(send_mode="combined", count=3)
        captured = {}

        def record_send(_uid, _headers, _script, recipients, **kwargs):
            captured["recipients"] = recipients
            captured["thread_context"] = kwargs.get("thread_context")
            captured["subject"] = kwargs.get("subject_override")
            return {"sent": recipients, "errors": {}}

        with ExitStack() as stack:
            finalize, dead_letter = self._combined_patches(stack)
            send = stack.enter_context(
                patch.object(email_module, "send_and_index_email", side_effect=record_send)
            )
            email_module._send_combined_property_email(
                "uid-1", {"Authorization": "Bearer t"}, self.RECIPIENT, self._items(docs),
            )

        send.assert_called_once()
        self.assertEqual(captured["recipients"], [self.RECIPIENT], "ONE send to the broker")
        self.assertEqual(finalize.call_count, 3, "every row finalized off the single send")
        dead_letter.assert_not_called()
        self.assertEqual(captured["thread_context"]["propertyAddresses"], self.ADDRS)
        self.assertEqual(captured["thread_context"]["rows"], [3, 4, 5])
        self.assertEqual(captured["subject"], "100 Dashboard Way (+2 more)")

    def test_combined_send_failure_bumps_all_rows_atomically(self):
        docs = self._same_broker_docs(send_mode="combined", count=3)
        with ExitStack() as stack:
            finalize, dead_letter = self._combined_patches(stack)
            send = stack.enter_context(patch.object(
                email_module, "send_and_index_email",
                return_value={"sent": [], "errors": {"_all": "graph boom"}},
            ))
            email_module._send_combined_property_email(
                "uid-1", {"Authorization": "Bearer t"}, self.RECIPIENT, self._items(docs),
            )

        send.assert_called_once()
        finalize.assert_not_called()
        dead_letter.assert_not_called()  # attempts=1 < MAX → retry, not dead-letter
        for d in docs:
            self.assertFalse(d.reference.deleted, "no row deleted on a failed combined send")
            statuses = [args[0].get("status") for (args, _kw) in d.reference.set_calls if args]
            self.assertIn("retrying", statuses, "each row released + bumped for a single retry")

    def test_combined_send_drops_duplicate_property_then_sends_remaining(self):
        docs = self._same_broker_docs(send_mode="combined", count=3)
        captured = {}

        def already_sent(_uid, _rcpt, prop_addr, **_k):
            return prop_addr == "100 Dashboard Way"

        def record_send(_uid, _headers, _script, recipients, **kwargs):
            captured["thread_context"] = kwargs.get("thread_context")
            return {"sent": recipients, "errors": {}}

        with ExitStack() as stack:
            finalize, _dead_letter = self._combined_patches(stack, existing=already_sent)
            send = stack.enter_context(
                patch.object(email_module, "send_and_index_email", side_effect=record_send)
            )
            email_module._send_combined_property_email(
                "uid-1", {"Authorization": "Bearer t"}, self.RECIPIENT, self._items(docs),
            )

        self.assertTrue(docs[0].reference.deleted, "already-sent property dropped from the group")
        send.assert_called_once()
        self.assertEqual(captured["thread_context"]["propertyAddresses"],
                         ["200 Interference Rd", "300 Drip Feed Dr"])
        self.assertEqual(finalize.call_count, 2, "only the surviving rows finalize")


if __name__ == "__main__":
    unittest.main()
