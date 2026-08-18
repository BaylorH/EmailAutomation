import os
import unittest
from copy import deepcopy
from contextvars import copy_context
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import requests
from google.api_core.datetime_helpers import DatetimeWithNanoseconds

def _record_delete(sink):
    """Record a Graph draft deletion instead of performing one.

    These lanes used to delete an abandoned draft through a helper the tests
    mocked by name. Once the call moved behind the delivery transport that mock
    stopped intercepting anything, and the real verb underneath would have
    reached a live mailbox. Asserting the DELETE itself is both stronger and
    safe.
    """

    class _Deleted:
        status_code = 204

        def json(self):
            return {}

        def raise_for_status(self):
            return None

    def _delete(url, **_kwargs):
        sink.append(url)
        return _Deleted()

    return _delete



os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import followup
from email_automation.campaign_safety import CampaignAutomationDecision
from email_automation.column_config import get_default_column_config


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)


class FakeThreadRef:
    def __init__(self, data=None):
        self.updates = []
        self._data = data or {}

    def update(self, data):
        self.updates.append(data)

    def get(self, transaction=None):
        return FakeThreadSnapshot(self._data)


class FakeThreadSnapshot:
    def __init__(self, data):
        self.exists = True
        self._data = data

    def to_dict(self):
        return self._data


class FakeFirestore:
    def __init__(self, thread_ref):
        self.thread_ref = thread_ref

    def collection(self, _name):
        return self

    def document(self, _name):
        return self

    def update(self, data):
        self.thread_ref.update(data)

    def get(self, transaction=None):
        return self.thread_ref.get(transaction=transaction)

    def transaction(self):
        return FakeTransaction()


class FakeTransaction:
    def __init__(self):
        self.updates = []

    def update(self, ref, data):
        payload = dict(data)
        self.updates.append((ref, payload))
        ref.update(payload)


class StaleWaitingThreads:
    def __init__(self, stale_data, backing_ref):
        self.backing_ref = backing_ref
        self.transaction = FakeTransaction()
        self.stale_doc = type("StaleWaitingDoc", (), {
            "id": "thread-stale",
            "reference": backing_ref,
            "to_dict": lambda self: dict(stale_data),
        })()

    def where(self, *_args, **_kwargs):
        return self

    def stream(self):
        return [self.stale_doc]

    def document(self, _thread_id):
        return self.backing_ref


class StaleWaitingFirestore:
    def __init__(self, stale_data, current_data):
        self.backing_ref = FakeThreadRef(current_data)
        self.threads = StaleWaitingThreads(stale_data, self.backing_ref)

    def document(self, _user_id):
        return self

    def collection(self, name):
        if name == "users":
            return self
        if name == "threads":
            return self.threads
        raise AssertionError(f"Unexpected collection: {name}")

    def transaction(self):
        return self.threads.transaction


def _fixed_datetime(value):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None) -> datetime:
            return value.astimezone(tz) if tz else value

    return FixedDateTime


def _sealed_followup_attempt(
    thread_data,
    followup_config,
    *,
    attempt_id,
    owner,
    state="sending",
    reconciliation_owner=None,
    followup_index=0,
    send_started_at=None,
    subject="Follow-up",
    conversation_id=None,
    draft_id=None,
    to_recipients=None,
    cc_recipients=None,
):
    """Build a production-shaped active marker for recovery-path tests."""
    send_started_at = send_started_at or datetime.now(timezone.utc)
    identity = followup._followup_send_identity(
        thread_data,
        followup_config,
        followup_index,
    )
    recipient = identity["recipient"]
    marker = {
        "id": attempt_id,
        "state": state,
        "owner": owner,
        "index": followup_index,
        "createdAt": send_started_at,
        "sendStartedAt": send_started_at,
        "leaseUntil": send_started_at + followup.timedelta(minutes=10),
        "sendIdentity": identity,
        "configFingerprint": identity["configFingerprint"],
        "clientId": identity["clientId"],
        "recipient": recipient,
        "body": followup._resolve_followup_message(
            followup_config,
            followup_index,
            thread_data.get("contactName"),
        ),
        "subject": subject,
        "conversationId": conversation_id,
        "draftId": draft_id,
        "toRecipients": followup._normalize_followup_recipients(
            to_recipients if to_recipients is not None else [recipient]
        ),
        "ccRecipients": followup._normalize_followup_recipients(
            cc_recipients
            if cc_recipients is not None
            else identity.get("ccRecipients")
        ),
    }
    if reconciliation_owner is not None:
        marker["reconciliationOwner"] = reconciliation_owner
    return followup._seal_followup_send_envelope(marker)


class FakeMessageDoc:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


class FakeMessagesCollection:
    def __init__(self, docs):
        self.docs = docs

    def where(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def stream(self):
        return self.docs


class FakeFollowupThreadNode:
    def __init__(self, updates, messages, thread_data=None):
        self.updates = updates
        self.messages = messages
        self.thread_data = thread_data or {}

    def collection(self, name):
        if name != "messages":
            raise AssertionError(f"Unexpected thread collection: {name}")
        return FakeMessagesCollection(self.messages)

    def update(self, data):
        self.updates.append(data)
        if isinstance(self.thread_data, dict):
            for path, value in data.items():
                target = self.thread_data
                parts = path.split(".")
                for part in parts[:-1]:
                    target = target.setdefault(part, {})
                target[parts[-1]] = value

    def get(self, transaction=None):
        thread_data = self.thread_data() if callable(self.thread_data) else self.thread_data
        return FakeThreadSnapshot(thread_data)


class FakeFollowupThreadsCollection:
    def __init__(self, updates, messages, thread_data=None):
        self.updates = updates
        self.messages = messages
        self.thread_data = thread_data or {}

    def document(self, _thread_id):
        return FakeFollowupThreadNode(self.updates, self.messages, self.thread_data)


class FakeFollowupUserNode:
    def __init__(self, updates, messages, thread_data=None):
        self.updates = updates
        self.messages = messages
        self.thread_data = thread_data or {}

    def get(self):
        return FakeThreadSnapshot({"email": "baylor.freelance@outlook.com"})

    def collection(self, name):
        if name != "threads":
            raise AssertionError(f"Unexpected user collection: {name}")
        return FakeFollowupThreadsCollection(self.updates, self.messages, self.thread_data)


class FakeFollowupFirestore:
    def __init__(self, messages, thread_data=None):
        self.updates = []
        self.messages = messages
        self.thread_data = thread_data or {}

    def collection(self, name):
        if name != "users":
            raise AssertionError(f"Unexpected root collection: {name}")
        return self

    def document(self, _user_id):
        return FakeFollowupUserNode(self.updates, self.messages, self.thread_data)

    def transaction(self):
        return FakeTransaction()


class FollowupTerminalStateTests(unittest.TestCase):
    def test_other_context_fail_closed_outcome_cannot_disable_current_followup(self):
        past = datetime.now(timezone.utc) - followup.timedelta(hours=1)

        class WaitingQuery:
            def __init__(self, docs):
                self.docs = docs

            def where(self, *_args, **_kwargs):
                return self

            def stream(self):
                return self.docs

        thread_ref = FakeThreadRef()
        thread_doc = type("ThreadDoc", (), {
            "id": "thread-current",
            "reference": thread_ref,
            "to_dict": lambda self: {
                "clientId": "client-1",
                "followUpStatus": "waiting",
                "followUpConfig": {
                    "enabled": True,
                    "nextFollowUpAt": past,
                    "currentFollowUpIndex": 0,
                    "followUps": [{"message": "Following up."}],
                },
                "hasInboundReply": False,
            },
        })()
        fake_fs = WaitingQuery([thread_doc])
        fake_fs.collection = lambda _name: fake_fs
        fake_fs.document = lambda _name: fake_fs
        other_context = copy_context()

        def fake_send_followup(**_kwargs):
            other_context.run(
                followup._set_followup_send_outcome,
                error="other request guard failed closed",
                guard_failed_closed=True,
            )
            return False

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup, "_next_business_followup_time", side_effect=lambda now, _cfg: now
        ), patch.object(followup, "_claim_followup", return_value=True), patch.object(
            followup, "_release_followup_claim"
        ) as release, patch.object(
            followup, "_send_followup_email", new=fake_send_followup
        ):
            states = followup.check_and_send_followups(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertFalse(release.call_args.kwargs["fail_closed"])
        self.assertEqual("follow-up send failed", states[0]["error"])
        self.assertFalse(any(
            update.get("followUpConfig.enabled") is False
            for update in thread_ref.updates
        ))

    def test_campaign_suppression_outcome_is_isolated_per_execution_context(self):
        terminal = CampaignAutomationDecision(
            state="blocked",
            reason="campaign_stopped",
            client_data={},
            metadata={"terminal": True},
        )
        maintenance = CampaignAutomationDecision(
            state="blocked",
            reason="campaign_maintenance",
            client_data={},
            metadata={"terminal": False},
        )
        terminal_context = copy_context()
        maintenance_context = copy_context()

        terminal_context.run(followup._set_followup_campaign_suppression, terminal)
        maintenance_context.run(followup._set_followup_campaign_suppression, maintenance)

        self.assertEqual(
            "terminal",
            terminal_context.run(followup._get_followup_campaign_suppression)[0],
        )
        self.assertEqual(
            "maintenance",
            maintenance_context.run(followup._get_followup_campaign_suppression)[0],
        )

    def test_terminal_followup_persists_the_clean_campaign_reason(self):
        past = datetime.now(timezone.utc) - followup.timedelta(hours=1)
        thread_data = {
            "clientId": "client-1",
            "status": "active",
            "followUpStatus": "waiting",
            "followUpConfig": {
                "enabled": True,
                "nextFollowUpAt": past,
                "currentFollowUpIndex": 0,
                "processingBy": "claim-owner",
                "followUps": [{"message": "Following up."}],
            },
            "hasInboundReply": False,
        }
        fake_fs = StaleWaitingFirestore(thread_data, thread_data)
        claim = followup.FollowupClaim(
            owner="claim-owner",
            index=0,
            thread_data=thread_data,
            followup_config=thread_data["followUpConfig"],
        )
        terminal = CampaignAutomationDecision(
            state="blocked",
            reason="client_stopped_by_user",
            client_data={"status": "stopped"},
            metadata={"terminal": True, "stopKind": "terminal_stop"},
        )

        def suppressed_send(**_kwargs):
            followup._set_followup_campaign_suppression(terminal)
            return False

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup, "_next_business_followup_time", side_effect=lambda now, _cfg: now
        ), patch.object(followup, "_claim_followup", return_value=claim), patch.object(
            followup, "_send_followup_email", side_effect=suppressed_send
        ), patch("google.cloud.firestore.transactional", lambda fn: fn):
            followup.check_and_send_followups(
                "uid-1", {"Authorization": "Bearer token"}
            )

        update = fake_fs.backing_ref.updates[-1]
        self.assertEqual("stopped", update["followUpStatus"])
        self.assertEqual("client_stopped_by_user", update["statusReason"])

    def test_terminal_campaign_suppression_does_not_overwrite_new_owner(self):
        past = datetime.now(timezone.utc) - followup.timedelta(hours=1)
        claimed_data = {
            "clientId": "client-1",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": {
                "enabled": True,
                "nextFollowUpAt": past,
                "currentFollowUpIndex": 0,
                "processingBy": "old-owner",
                "followUps": [{"message": "Following up."}],
            },
        }
        current_data = {
            **claimed_data,
            "followUpConfig": {
                **claimed_data["followUpConfig"],
                "processingBy": "new-owner",
            },
        }
        fake_fs = StaleWaitingFirestore(claimed_data, current_data)
        claim = followup.FollowupClaim(
            owner="old-owner",
            index=0,
            thread_data=claimed_data,
            followup_config=claimed_data["followUpConfig"],
        )
        terminal = CampaignAutomationDecision(
            state="blocked",
            reason="client_stopped_by_user",
            client_data={"status": "stopped"},
            metadata={"terminal": True, "stopKind": "terminal_stop"},
        )

        def suppressed_send(**_kwargs):
            followup._set_followup_campaign_suppression(terminal)
            return False

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup, "_next_business_followup_time", side_effect=lambda now, _cfg: now
        ), patch.object(
            followup, "_claim_followup", return_value=claim
        ), patch.object(
            followup, "_send_followup_email", side_effect=suppressed_send
        ), patch("google.cloud.firestore.transactional", lambda fn: fn):
            states = followup.check_and_send_followups(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertEqual([], fake_fs.backing_ref.updates)
        self.assertEqual("error", states[0]["status"])
        self.assertIn("ownership changed", states[0]["error"])

    def test_terminal_campaign_suppression_rejects_reassigned_client(self):
        current_data = {
            "clientId": "client-current",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": {
                "enabled": True,
                "currentFollowUpIndex": 0,
                "processingBy": "claim-owner",
                "followUps": [{"message": "Following up."}],
            },
        }
        thread_ref = FakeThreadRef(current_data)

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            terminalized, error = followup._terminalize_owned_followup(
                "uid-1",
                "thread-1",
                reason="client_stopped_by_user",
                current_index=0,
                claim_owner="claim-owner",
                expected_client_id="client-claimed",
            )

        self.assertFalse(terminalized)
        self.assertIn("client", error)
        self.assertEqual([], thread_ref.updates)

    def test_followup_suppression_ignores_stale_shared_send_attributes(self):
        followup._clear_followup_campaign_suppression()
        followup._send_followup_email.campaign_suppression_kind = "terminal"

        kind, decision = followup._get_local_followup_campaign_suppression()

        self.assertIsNone(kind)
        self.assertIsNone(decision)

    def setUp(self):
        self._campaign_decision_patch = patch.object(
            followup,
            "get_client_automation_decision",
            return_value=CampaignAutomationDecision(
                state="allow",
                reason="",
                client_data={
                    "status": "live",
                    "columnConfig": get_default_column_config(),
                },
                metadata={"terminal": False, "stopKind": "none"},
            ),
            create=True,
        )
        self.campaign_decision = self._campaign_decision_patch.start()
        self.addCleanup(self._campaign_decision_patch.stop)
        self._optout_patch = patch(
            "email_automation.processing.is_contact_opted_out",
            return_value=None,
        )
        self._optout_patch.start()
        self.addCleanup(self._optout_patch.stop)

    def test_claim_rechecks_stale_waiting_query_and_rejects_terminal_reply(self):
        past = datetime.now(timezone.utc) - followup.timedelta(hours=1)
        stale_data = {
            "clientId": "client-1",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": {
                "enabled": True,
                "nextFollowUpAt": past,
                "currentFollowUpIndex": 0,
                "followUps": [{"message": "Following up."}],
            },
        }
        current_data = {
            **stale_data,
            "status": "stopped",
            "statusReason": "requirements_mismatch",
            "followUpStatus": "stopped",
            "pendingTerminalReason": "requirements_mismatch",
            "hasInboundReply": True,
        }
        fake_fs = StaleWaitingFirestore(stale_data, current_data)

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup, "_next_business_followup_time", side_effect=lambda now, _cfg: now
        ), patch("google.cloud.firestore.transactional", lambda fn: fn), patch.object(
            followup, "_send_followup_email"
        ) as send_followup:
            states = followup.check_and_send_followups(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertEqual([], states)
        send_followup.assert_not_called()
        self.assertEqual([], fake_fs.threads.transaction.updates)
        self.assertEqual([], fake_fs.backing_ref.updates)

    def test_followup_query_failures_surface_health_without_processing(self):
        waiting_doc = type("WaitingDoc", (), {
            "id": "thread-waiting",
            "to_dict": lambda self: {
                "followUpStatus": "waiting",
                "followUpConfig": {
                    "enabled": True,
                    "nextFollowUpAt": (
                        datetime.now(timezone.utc) - followup.timedelta(hours=1)
                    ),
                    "currentFollowUpIndex": 0,
                    "followUps": [{"message": "Following up."}],
                },
            },
        })()

        class QueryResult:
            def __init__(self, docs, error=None):
                self.docs = docs
                self.error = error

            def stream(self):
                if self.error is not None:
                    raise self.error
                return list(self.docs)

        class QueryThreads:
            def __init__(
                self,
                waiting_docs,
                *,
                waiting_error=None,
                recovery_error=None,
            ):
                self.waiting_docs = waiting_docs
                self.waiting_error = waiting_error
                self.recovery_error = recovery_error
                self.where_calls = []

            def where(self, field, _operator, value):
                self.where_calls.append((field, value))
                if field == "followUpStatus":
                    return QueryResult(self.waiting_docs, self.waiting_error)
                if field == "followUpSendAttempt.state":
                    return QueryResult([], self.recovery_error)
                raise AssertionError(f"Unexpected query field: {field}")

        class QueryFirestore:
            def __init__(self, threads):
                self.threads = threads

            def collection(self, name):
                if name == "users":
                    return self
                if name == "threads":
                    return self.threads
                raise AssertionError(f"Unexpected collection: {name}")

            def document(self, _document_id):
                return self

        cases = (
            (
                "recovery_failure_empty_waiting",
                [],
                None,
                RuntimeError("recovery index unavailable"),
                "followup_recovery_query",
                "followup_recovery_query_failed",
                2,
            ),
            (
                "recovery_failure_nonempty_waiting",
                [waiting_doc],
                None,
                RuntimeError("recovery unavailable: permission denied"),
                "followup_recovery_query",
                "followup_recovery_query_failed",
                2,
            ),
            (
                "waiting_failure_stops_before_recovery",
                [waiting_doc],
                RuntimeError("waiting index unavailable"),
                RuntimeError("recovery query must not run"),
                "followup_waiting_query",
                "followup_waiting_query_failed",
                1,
            ),
        )

        for (
            name,
            waiting_docs,
            waiting_error,
            recovery_error,
            expected_operation,
            expected_code,
            expected_query_count,
        ) in cases:
            with self.subTest(name=name):
                threads = QueryThreads(
                    waiting_docs,
                    waiting_error=waiting_error,
                    recovery_error=recovery_error,
                )
                with patch.object(
                    followup,
                    "_fs",
                    QueryFirestore(threads),
                ), patch.object(
                    followup,
                    "_claim_followup",
                    return_value=None,
                ) as claim, patch.object(
                    followup,
                    "_send_followup_email",
                ) as send, patch.object(
                    followup,
                    "_release_followup_claim",
                ) as release:
                    states = followup.check_and_send_followups(
                        "uid-1",
                        {"Authorization": "Bearer token"},
                    )

                self.assertEqual(1, len(states))
                self.assertEqual("error", states[0]["status"])
                self.assertEqual(expected_operation, states[0]["operation"])
                self.assertEqual(expected_code, states[0]["code"])
                self.assertIn("unavailable", states[0]["error"])
                self.assertEqual(expected_query_count, len(threads.where_calls))
                claim.assert_not_called()
                send.assert_not_called()
                release.assert_not_called()

    def test_followup_empty_query_health_isolated_from_prior_query_error(self):
        class QueryResult:
            def __init__(self, error=None):
                self.error = error

            def stream(self):
                if self.error is not None:
                    raise self.error
                return []

        class QueryThreads:
            def __init__(self, recovery_error=None):
                self.recovery_error = recovery_error

            def where(self, field, _operator, _value):
                if field == "followUpStatus":
                    return QueryResult()
                if field == "followUpSendAttempt.state":
                    return QueryResult(self.recovery_error)
                raise AssertionError(f"Unexpected query field: {field}")

        class QueryFirestore:
            def __init__(self, recovery_error=None):
                self.threads = QueryThreads(recovery_error)

            def collection(self, name):
                return self if name == "users" else self.threads

            def document(self, _document_id):
                return self

        with patch.object(
            followup,
            "_fs",
            QueryFirestore(RuntimeError("transient recovery failure")),
        ):
            failed_states = followup.check_and_send_followups(
                "uid-1",
                {"Authorization": "Bearer token"},
            )

        with patch.object(followup, "_fs", QueryFirestore()):
            healthy_states = followup.check_and_send_followups(
                "uid-1",
                {"Authorization": "Bearer token"},
            )

        self.assertEqual("error", failed_states[0]["status"])
        self.assertEqual([], healthy_states)

    def test_scheduler_recovers_unresolved_attempt_from_paused_thread(self):
        expired_at = datetime.now(timezone.utc) - followup.timedelta(hours=1)
        current_data = {
            "clientId": "client-1",
            "email": ["broker@example.com"],
            "status": "paused",
            "statusReason": "manual_continuation",
            "followUpStatus": "paused",
            "hasInboundReply": True,
            "followUpSendAttempt": {
                "id": "attempt-unresolved",
                "state": "uncertain",
                "owner": "crashed-owner",
                "index": 0,
                "recipient": "broker@example.com",
                "body": "Following up.",
                "subject": "Subject",
                "sendStartedAt": expired_at,
                "leaseUntil": expired_at,
            },
            "followUpConfig": {
                "enabled": False,
                "nextFollowUpAt": None,
                "currentFollowUpIndex": 0,
                "processingBy": None,
                "processingAt": None,
                "followUps": [{"message": "Following up."}],
            },
        }
        thread_ref = FakeThreadRef(current_data)
        thread_doc = type("RecoveryDoc", (), {
            "id": "thread-recovery",
            "reference": thread_ref,
            "to_dict": lambda self: dict(current_data),
        })()

        class RecoveryQuery:
            def __init__(self, docs):
                self.docs = docs

            def stream(self):
                return self.docs

        class RecoveryThreads:
            def where(self, field, _operator, _value):
                if field == "followUpStatus":
                    return RecoveryQuery([])
                if field == "followUpSendAttempt.state":
                    return RecoveryQuery([thread_doc])
                raise AssertionError(f"Unexpected query field: {field}")

            def document(self, _thread_id):
                return thread_ref

        class RecoveryFirestore:
            def __init__(self):
                self.threads = RecoveryThreads()

            def collection(self, name):
                if name == "users":
                    return self
                if name == "threads":
                    return self.threads
                raise AssertionError(f"Unexpected collection: {name}")

            def document(self, _document_id):
                return self

            def transaction(self):
                return FakeTransaction()

        with patch.object(followup, "_fs", RecoveryFirestore()), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ), patch.object(
            followup, "_send_followup_email", return_value=False
        ) as send_followup, patch.object(
            followup, "_release_followup_claim", return_value=True
        ):
            states = followup.check_and_send_followups(
                "uid-1", {"Authorization": "Bearer token"}
            )

        send_followup.assert_called_once()
        send_kwargs = send_followup.call_args.kwargs
        self.assertNotEqual("crashed-owner", send_kwargs["claim_owner"])
        self.assertEqual("paused", send_kwargs["thread_data"]["followUpStatus"])
        self.assertTrue(send_kwargs["thread_data"]["hasInboundReply"])
        self.assertEqual("error", states[0]["status"])

    def test_claim_returns_the_owner_written_by_its_transaction(self):
        past = datetime.now(timezone.utc) - followup.timedelta(hours=1)
        thread_ref = FakeThreadRef({
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": {
                "enabled": True,
                "nextFollowUpAt": past,
                "currentFollowUpIndex": 0,
                "followUps": [{"message": "Following up."}],
            },
        })

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            claim = followup._claim_followup("uid-1", "thread-1", 0)

        self.assertIsInstance(claim, followup.FollowupClaim)
        self.assertEqual(
            claim.owner,
            thread_ref.updates[-1]["followUpConfig.processingBy"],
        )

    def test_claim_returns_authoritative_snapshot_with_owner(self):
        past = datetime.now(timezone.utc) - followup.timedelta(hours=1)
        current_data = {
            "clientId": "client-current",
            "email": ["current@example.com"],
            "contactName": "Current Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": {
                "enabled": True,
                "nextFollowUpAt": past,
                "currentFollowUpIndex": 0,
                "lastSendError": "prior ambiguous send",
                "lastSendAttemptAt": "2026-06-26T12:05:00Z",
                "lastSendAttemptIndex": 0,
                "followUps": [{"message": "Current body"}],
            },
        }
        thread_ref = FakeThreadRef(current_data)

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            claim = followup._claim_followup("uid-1", "thread-1", 0)

        self.assertEqual(
            thread_ref.updates[-1]["followUpConfig.processingBy"],
            getattr(claim, "owner", None),
        )
        self.assertEqual(
            "current@example.com",
            getattr(claim, "thread_data", {}).get("email", [None])[0],
        )
        self.assertEqual(
            "prior ambiguous send",
            getattr(claim, "followup_config", {}).get("lastSendError"),
        )

    def test_claim_does_not_reclaim_committed_attempt_at_same_index(self):
        past = datetime.now(timezone.utc) - followup.timedelta(hours=1)
        thread_ref = FakeThreadRef({
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpSendAttempt": {
                "id": "attempt-committed",
                "state": "committed",
                "owner": "prior-owner",
                "index": 0,
            },
            "followUpConfig": {
                "enabled": True,
                "nextFollowUpAt": past,
                "currentFollowUpIndex": 0,
                "followUps": [{"message": "Following up."}],
            },
        })

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            claim = followup._claim_followup("uid-1", "thread-1", 0)

        self.assertIsNone(claim)
        self.assertEqual([], thread_ref.updates)

    def test_claim_does_not_resume_an_ambiguous_attempt_awaiting_review(self):
        past = datetime.now(timezone.utc) - followup.timedelta(hours=1)
        thread_ref = FakeThreadRef({
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpSendAttempt": {
                "id": "attempt-needs-review",
                "state": "needs_review",
                "resolution": "ambiguous",
                "owner": "prior-owner",
                "index": 0,
            },
            "followUpConfig": {
                "enabled": True,
                "nextFollowUpAt": past,
                "currentFollowUpIndex": 0,
                "followUps": [{"message": "Following up."}],
            },
        })

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            claim = followup._claim_followup("uid-1", "thread-1", 0)

        self.assertIsNone(claim)
        self.assertEqual([], thread_ref.updates)

    def test_claim_blocks_unresolved_attempt_from_another_index(self):
        past = datetime.now(timezone.utc) - followup.timedelta(hours=1)
        thread_ref = FakeThreadRef({
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpSendAttempt": {
                "id": "attempt-unresolved",
                "state": "uncertain",
                "owner": "prior-owner",
                "index": 0,
            },
            "followUpConfig": {
                "enabled": True,
                "nextFollowUpAt": past,
                "currentFollowUpIndex": 1,
                "followUps": [
                    {"message": "First"},
                    {"message": "Second"},
                ],
            },
        })

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            claim = followup._claim_followup("uid-1", "thread-1", 1)

        self.assertIsNone(claim)
        update = thread_ref.updates[-1]
        self.assertEqual("needs_review", update["followUpStatus"])
        self.assertEqual("action_needed", update["status"])
        self.assertEqual("followup_send_guard_failed", update["statusReason"])
        self.assertIn("different index", update["followUpConfig.lastSendError"])
        self.assertEqual(
            "attempt-unresolved",
            update["followUpSendAttempt"]["id"],
        )
        self.assertEqual(
            "needs_review",
            update["followUpSendAttempt"]["state"],
        )
        self.assertEqual(
            "ambiguous",
            update["followUpSendAttempt"]["resolution"],
        )

    def test_check_uses_authoritative_claim_snapshot_and_retry_state(self):
        past = datetime.now(timezone.utc) - followup.timedelta(hours=1)
        stale_data = {
            "clientId": "client-stale",
            "email": ["stale@example.com"],
            "contactName": "Stale Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": {
                "enabled": True,
                "nextFollowUpAt": past,
                "currentFollowUpIndex": 0,
                "followUps": [{"message": "Stale body"}],
            },
        }
        current_data = {
            **stale_data,
            "clientId": "client-current",
            "email": ["current@example.com"],
            "contactName": "Current Broker",
            "followUpConfig": {
                **stale_data["followUpConfig"],
                "lastSendError": "prior ambiguous send",
                "lastSendAttemptAt": "2026-06-26T12:05:00Z",
                "lastSendAttemptIndex": 0,
                "followUps": [{"message": "Current body"}],
            },
        }
        claim = type("AuthoritativeClaim", (), {
            "owner": "claim-owner",
            "index": 0,
            "thread_data": current_data,
            "followup_config": current_data["followUpConfig"],
        })()
        fake_fs = StaleWaitingFirestore(stale_data, current_data)

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup, "_next_business_followup_time", side_effect=lambda now, _cfg: now
        ), patch.object(
            followup, "_claim_followup", return_value=claim
        ), patch.object(
            followup, "_send_followup_email", return_value=False
        ) as send, patch.object(
            followup, "_release_followup_claim"
        ):
            followup.check_and_send_followups(
                "uid-1", {"Authorization": "Bearer token"}
            )

        send_kwargs = send.call_args.kwargs
        self.assertEqual("claim-owner", send_kwargs["claim_owner"])
        self.assertEqual("client-current", send_kwargs["thread_data"]["clientId"])
        self.assertEqual("current@example.com", send_kwargs["thread_data"]["email"][0])
        self.assertEqual(
            "Current body",
            send_kwargs["followup_config"]["followUps"][0]["message"],
        )
        self.assertEqual(
            "prior ambiguous send",
            send_kwargs["followup_config"]["lastSendError"],
        )

    def test_post_send_schedule_failure_fails_closed_and_surfaces_error(self):
        past = datetime.now(timezone.utc) - followup.timedelta(hours=1)
        attempted_at = datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc)
        stale_data = {
            "clientId": "client-1",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": {
                "enabled": True,
                "nextFollowUpAt": past,
                "currentFollowUpIndex": 0,
                "followUps": [
                    {"waitTime": 1, "waitUnit": "hours", "message": "Following up."},
                ],
            },
        }
        fake_fs = StaleWaitingFirestore(stale_data, stale_data)

        def sent_followup(**_kwargs):
            followup._set_followup_send_outcome(
                attempt_at=attempted_at,
                attempt_id="attempt-sent",
            )
            return True

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup, "_next_business_followup_time", side_effect=lambda now, _cfg: now
        ), patch.object(
            followup, "_claim_followup", return_value="claim-owner"
        ), patch.object(
            followup, "_send_followup_email", side_effect=sent_followup
        ), patch.object(
            followup,
            "_schedule_next_followup",
            side_effect=RuntimeError("transaction unavailable"),
        ), patch.object(
            followup, "_release_followup_claim", return_value=True
        ) as release:
            states = followup.check_and_send_followups(
                "uid-1", {"Authorization": "Bearer token"}
            )

        release.assert_called_once()
        self.assertEqual("claim-owner", release.call_args.kwargs["claim_owner"])
        self.assertEqual(0, release.call_args.kwargs["current_index"])
        self.assertEqual(attempted_at, release.call_args.kwargs["attempted_at"])
        self.assertEqual("attempt-sent", release.call_args.kwargs["send_attempt_id"])
        self.assertTrue(release.call_args.kwargs["fail_closed"])
        self.assertIn("post-send scheduling failed", release.call_args.kwargs["reason"])
        self.assertEqual("error", states[0]["status"])
        self.assertIn("post-send scheduling failed", states[0]["error"])

    def test_post_send_false_schedule_outcome_is_not_reported_healthy(self):
        past = datetime.now(timezone.utc) - followup.timedelta(hours=1)
        thread_data = {
            "clientId": "client-1",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": {
                "enabled": True,
                "nextFollowUpAt": past,
                "currentFollowUpIndex": 0,
                "followUps": [{"message": "Following up."}],
            },
        }
        fake_fs = StaleWaitingFirestore(thread_data, thread_data)

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup, "_next_business_followup_time", side_effect=lambda now, _cfg: now
        ), patch.object(
            followup, "_claim_followup", return_value="claim-owner"
        ), patch.object(
            followup, "_send_followup_email", return_value=True
        ), patch.object(
            followup, "_schedule_next_followup", return_value=False
        ), patch.object(
            followup, "_release_followup_claim", return_value=True
        ):
            states = followup.check_and_send_followups(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertEqual("error", states[0]["status"])
        self.assertIn("ambiguous", states[0]["error"].lower())

    def test_followup_blocks_note_field_request_before_graph(self):
        self.campaign_decision.return_value = CampaignAutomationDecision(
            state="allow",
            reason="",
            client_data={
                "status": "live",
                "columnConfig": get_default_column_config(),
            },
            metadata={"terminal": False, "stopKind": "none"},
        )

        with patch.object(requests, "get") as get, patch.object(requests, "post") as post:
            sent = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                {"clientId": "client-1", "email": ["broker@example.com"]},
                {
                    "currentFollowUpIndex": 0,
                    "followUps": [{"message": "Is there a flyer available?"}],
                },
                0,
            )

        self.assertFalse(sent)
        get.assert_not_called()
        post.assert_not_called()
        self.assertTrue(followup._send_followup_email.guard_failed_closed)
        self.assertIn("non-requestable", followup._send_followup_email.last_error)
        self.assertIn("manual review", followup._send_followup_email.last_error)

    def test_followup_blocks_incomplete_persisted_config_before_graph(self):
        self.campaign_decision.return_value = CampaignAutomationDecision(
            state="allow",
            reason="",
            client_data={"status": "live", "columnConfig": {"mappings": {}}},
            metadata={"terminal": False, "stopKind": "none"},
        )

        with patch.object(requests, "get") as get, patch.object(requests, "post") as post:
            sent = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                {"clientId": "client-1", "email": ["broker@example.com"]},
                {
                    "currentFollowUpIndex": 0,
                    "followUps": [{"message": "Could you confirm the asking rent?"}],
                },
                0,
            )

        self.assertFalse(sent)
        get.assert_not_called()
        post.assert_not_called()
        self.assertTrue(followup._send_followup_email.guard_failed_closed)
        self.assertIn("invalid persisted columnConfig", followup._send_followup_email.last_error)
        self.assertIn("manual review", followup._send_followup_email.last_error)

    def test_weekend_followup_window_defers_to_monday_business_start(self):
        sunday = datetime(2026, 6, 21, 17, 1, tzinfo=timezone.utc)

        deferred = followup._next_business_followup_time(sunday)

        self.assertEqual(
            deferred.isoformat(),
            "2026-06-22T13:00:00+00:00",
        )

    def test_weekday_followup_window_is_unchanged(self):
        monday = datetime(2026, 6, 22, 15, 1, tzinfo=timezone.utc)

        self.assertEqual(followup._next_business_followup_time(monday), monday)

    def test_initial_followup_schedule_defers_weekend_due_time(self):
        thread_ref = FakeThreadRef()
        followup_config = {
            "enabled": True,
            "timeZone": "America/New_York",
            "followUps": [
                {
                    "waitTime": 24,
                    "waitUnit": "hours",
                    "message": "Hi Alex,\n\nJust following up.",
                }
            ],
        }

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), \
             patch.object(
                 followup,
                 "datetime",
                 _fixed_datetime(datetime(2026, 6, 19, 22, 0, tzinfo=timezone.utc)),
             ):
            followup.schedule_followup_for_thread("uid-1", "thread-1", followup_config)

        update = thread_ref.updates[-1]
        scheduled_at = update["followUpConfig"]["nextFollowUpAt"]
        self.assertEqual(
            scheduled_at.isoformat(),
            "2026-06-22T13:00:00+00:00",
        )

    def test_initial_followup_schedule_preserves_business_day_due_time(self):
        thread_ref = FakeThreadRef()
        followup_config = {
            "enabled": True,
            "timeZone": "America/New_York",
            "followUps": [
                {
                    "waitTime": 24,
                    "waitUnit": "hours",
                    "message": "Hi Alex,\n\nJust following up.",
                }
            ],
        }

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), \
             patch.object(
                 followup,
                 "datetime",
                 _fixed_datetime(datetime(2026, 6, 22, 15, 0, tzinfo=timezone.utc)),
             ):
            followup.schedule_followup_for_thread("uid-1", "thread-1", followup_config)

        update = thread_ref.updates[-1]
        scheduled_at = update["followUpConfig"]["nextFollowUpAt"]
        self.assertEqual(
            scheduled_at.isoformat(),
            "2026-06-23T15:00:00+00:00",
        )

    @patch.object(followup, "_clear_followup_row_highlight", create=True)
    def test_max_reached_stops_thread_and_clears_highlight(self, clear_highlight):
        thread_ref = FakeThreadRef()

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)):
            followup._mark_followup_complete("uid-1", "thread-1", "max_reached")

        self.assertEqual(thread_ref.updates[-1]["followUpStatus"], "max_reached")
        self.assertEqual(thread_ref.updates[-1]["status"], "stopped")
        self.assertEqual(thread_ref.updates[-1]["statusReason"], "max_followups_reached")
        clear_highlight.assert_called_once_with("uid-1", "thread-1")

    def test_reply_anchor_skips_synthetic_followup_history(self):
        synthetic_latest = FakeMessageDoc({
            "direction": "outbound",
            "source": "followup_scheduler",
            "headers": {"internetMessageId": "followup-thread-123"},
            "sentDateTime": "2026-05-06T09:00:00Z",
        })
        graph_backed_original = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<real-message@example.com>"},
            "sentDateTime": "2026-05-06T08:00:00Z",
        })

        selected = followup._select_reply_anchor_message([synthetic_latest, graph_backed_original])

        self.assertEqual(selected["headers"]["internetMessageId"], "<real-message@example.com>")

    def test_reply_anchor_returns_none_when_only_synthetic_history_exists(self):
        selected = followup._select_reply_anchor_message([
            FakeMessageDoc({
                "direction": "outbound",
                "source": "dashboard_outbox_reply",
                "headers": {"internetMessageId": "dashboard-reply-123"},
            })
        ])

        self.assertIsNone(selected)

    def test_auto_response_reschedules_paused_active_thread(self):
        thread_ref = FakeThreadRef({
            "status": "active",
            "followUpStatus": "paused",
            "hasInboundReply": True,
            "followUpConfig": {
                "enabled": True,
                "currentFollowUpIndex": 1,
                "followUps": [
                    {"waitTime": 1, "waitUnit": "hours", "message": "First"},
                    {"waitTime": 2, "waitUnit": "hours", "message": "Second"},
                ],
            },
        })

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)):
            result = followup.schedule_followup_after_auto_response("uid-1", "thread-1")

        self.assertTrue(result)
        update = thread_ref.updates[-1]
        self.assertEqual(update["followUpStatus"], "waiting")
        self.assertFalse(update["hasInboundReply"])
        self.assertIsNone(update["followUpConfig.pausedAt"])
        self.assertIn("followUpConfig.nextFollowUpAt", update)

    def test_auto_response_does_not_restart_exhausted_followups(self):
        thread_ref = FakeThreadRef({
            "status": "active",
            "followUpStatus": "max_reached",
            "hasInboundReply": True,
            "followUpConfig": {
                "enabled": True,
                "currentFollowUpIndex": 0,
                "followUps": [
                    {"waitTime": 1, "waitUnit": "days", "message": "Only follow-up"},
                ],
            },
        })

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)):
            result = followup.schedule_followup_after_auto_response("uid-1", "thread-1")

        self.assertFalse(result)
        self.assertEqual(thread_ref.updates, [])

    def test_auto_response_does_not_restart_pending_terminal_followups(self):
        thread_ref = FakeThreadRef({
            "status": "active",
            "followUpStatus": "stopped",
            "pendingTerminalReason": "requirements_mismatch",
            "followUpConfig": {
                "enabled": True,
                "currentFollowUpIndex": 0,
                "followUps": [
                    {"waitTime": 1, "waitUnit": "days", "message": "Follow-up"},
                ],
            },
        })

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)):
            result = followup.schedule_followup_after_auto_response("uid-1", "thread-1")

        self.assertFalse(result)
        self.assertEqual(thread_ref.updates, [])

    def test_pending_terminal_decision_blocks_followup_send(self):
        reason = followup._followup_terminal_block_reason(
            {
                "status": "active",
                "followUpStatus": "waiting",
                "pendingTerminalReason": "requirements_mismatch",
            },
            {"enabled": True, "currentFollowUpIndex": 0, "followUps": [{}]},
            0,
        )

        self.assertIn("requirements_mismatch", reason)

    def test_followup_blocks_malformed_recipient_before_graph_send(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
        })
        fake_fs = FakeFollowupFirestore([outbound])
        followup_config = {
            "currentFollowUpIndex": 0,
            "followUps": [{"message": "Hi Riley,\n\nJust following up."}],
        }
        thread_data = {
            "email": ["not an email"],
            "contactName": "Riley Broker",
        }

        with patch.object(followup, "_fs", fake_fs), \
             patch.object(followup, "exponential_backoff_request", return_value=FakeResponse(200, {
                 "value": [{"id": "graph-root", "subject": "0 Gemini Ave", "conversationId": "conv-1"}]
             })), \
             patch.object(requests, "post") as post:
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
            )

        self.assertFalse(result)
        post.assert_not_called()
        self.assertIn("Invalid follow-up recipient", followup._send_followup_email.last_error)
        self.assertTrue(followup._send_followup_email.guard_failed_closed)

    def test_followup_blocks_opted_out_recipient_before_graph_send(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
        })
        fake_fs = FakeFollowupFirestore([outbound])
        followup_config = {
            "currentFollowUpIndex": 0,
            "followUps": [{"message": "Hi Riley,\n\nJust following up."}],
        }
        thread_data = {
            "email": ["optout@example.com"],
            "contactName": "Riley Broker",
        }

        with patch.object(followup, "_fs", fake_fs), \
             patch.object(followup, "exponential_backoff_request", return_value=FakeResponse(200, {
                 "value": [{"id": "graph-root", "subject": "0 Gemini Ave", "conversationId": "conv-1"}]
             })), \
             patch("email_automation.processing.is_contact_opted_out", return_value={"reason": "unsubscribe"}), \
             patch.object(requests, "post") as post:
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
            )

        self.assertFalse(result)
        post.assert_not_called()
        self.assertIn("opted out", followup._send_followup_email.last_error)
        self.assertTrue(followup._send_followup_email.guard_failed_closed)

    def test_followup_preserves_safe_ccs_with_reply_all_draft(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
            "to": ["bp21harrison@gmail.com"],
            "cc": ["assistant@example.com", "baylor.freelance@outlook.com"],
        })
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "followUps": [{"message": "Hi Riley,\n\nJust following up."}],
        }
        thread_data = {
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "contactName": "Riley Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": followup_config,
        }
        fake_fs = FakeFollowupFirestore([outbound], thread_data=thread_data)
        post_urls = []
        patched_payloads = []

        def run_request(callback, *args, **kwargs):
            return callback()

        def fake_get(url, **kwargs):
            self.assertIn("/me/messages", url)
            return FakeResponse(200, {
                "value": [{
                    "id": "graph-root",
                    "subject": "0 Gemini Ave",
                    "conversationId": "conv-1",
                }]
            })

        def fake_post(url, **kwargs):
            post_urls.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {
                    "id": "reply-draft-1",
                    "toRecipients": [
                        {"emailAddress": {"address": "bp21harrison@gmail.com"}},
                        {"emailAddress": {"address": "teammate@example.com"}},
                    ],
                    "ccRecipients": [
                        {"emailAddress": {"address": "assistant@example.com"}},
                        {"emailAddress": {"address": "baylor.freelance@outlook.com"}},
                    ],
                })
            if url.endswith("/send"):
                return FakeResponse(202, {})
            raise AssertionError(f"Follow-up used non reply-all endpoint: {url}")

        def fake_patch(url, **kwargs):
            patched_payloads.append(kwargs.get("json") or {})
            return FakeResponse(200, {})

        with patch.object(followup, "_fs", fake_fs), \
             patch.object(followup, "exponential_backoff_request", side_effect=run_request), \
             patch.object(requests, "get", side_effect=fake_get), \
             patch.object(requests, "post", side_effect=fake_post), \
             patch.object(requests, "patch", side_effect=fake_patch), \
             patch.object(followup, "_save_followup_message") as save_followup, \
             patch("email_automation.processing.is_contact_opted_out", return_value=None), \
             patch("google.cloud.firestore.transactional", lambda fn: fn), \
             patch("email_automation.email._delete_graph_reply_draft"):
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
                claim_owner="claim-owner",
            )

        self.assertTrue(result)
        self.assertTrue(any(url.endswith("/createReplyAll") for url in post_urls))
        self.assertTrue(any(url.endswith("/send") for url in post_urls))
        patch_payload = patched_payloads[-1]
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["toRecipients"]],
            ["bp21harrison@gmail.com", "teammate@example.com"],
        )
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["ccRecipients"]],
            ["assistant@example.com"],
        )
        save_followup.assert_called_once()
        self.assertEqual(
            save_followup.call_args.kwargs["cc_recipients"],
            ["assistant@example.com"],
        )
        self.assertEqual(
            save_followup.call_args.kwargs["to_recipients"],
            ["bp21harrison@gmail.com", "teammate@example.com"],
        )
        self.assertEqual(
            thread_data["followUpSendAttempt"]["id"],
            save_followup.call_args.kwargs["attempt_id"],
        )
        self.assertEqual(
            ["bp21harrison@gmail.com", "teammate@example.com"],
            thread_data["followUpSendAttempt"]["toRecipients"],
        )
        self.assertEqual(
            ["assistant@example.com"],
            thread_data["followUpSendAttempt"]["ccRecipients"],
        )

    def test_personalized_followup_persists_raw_template_and_sends_resolved_body(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
            "to": ["broker@example.com"],
        })
        raw_template = "Hi [NAME],\n\nJust following up."
        resolved_body = "Hi Ryan,\n\nJust following up."
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "followUps": [{"message": raw_template}],
        }
        thread_data = {
            "clientId": "client-1",
            "email": ["broker@example.com"],
            "contactName": "Ryan Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": followup_config,
        }
        fake_fs = FakeFollowupFirestore([outbound], thread_data=thread_data)
        post_urls = []
        patched_payloads = []

        def run_request(callback, *args, **kwargs):
            return callback()

        def fake_get(url, **kwargs):
            self.assertIn("/me/messages", url)
            return FakeResponse(200, {
                "value": [{
                    "id": "graph-root",
                    "subject": "Current subject",
                    "conversationId": "conv-current",
                }]
            })

        def fake_post(url, **kwargs):
            post_urls.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {
                    "id": "reply-draft-personalized",
                    "toRecipients": [
                        {"emailAddress": {"address": "broker@example.com"}},
                    ],
                    "ccRecipients": [],
                })
            if url.endswith("/send"):
                marker = thread_data.get("followUpSendAttempt")
                self.assertIsNotNone(marker)
                self.assertEqual("sending", marker["state"])
                self.assertEqual(raw_template, marker["sendIdentity"]["rawMessage"])
                self.assertEqual(resolved_body, marker["body"])
                self.assertEqual("broker@example.com", marker["recipient"])
                return FakeResponse(202, {})
            raise AssertionError(f"Unexpected POST: {url}")

        def fake_patch(url, **kwargs):
            patched_payloads.append(kwargs.get("json") or {})
            return FakeResponse(200, {})

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup, "exponential_backoff_request", side_effect=run_request
        ), patch.object(requests, "get", side_effect=fake_get), patch.object(
            requests, "post", side_effect=fake_post
        ), patch.object(
            requests, "patch", side_effect=fake_patch
        ), patch.object(
            followup, "_save_followup_message", return_value=True
        ), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ), patch.object(
            # Patch the verb, not the old helper name: the lane deletes through
            # the transport now, and a name-level mock stopped intercepting it.
            requests,
            "delete",
            return_value=FakeResponse(204),
        ):
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
                claim_owner="claim-owner",
            )

        self.assertTrue(result)
        self.assertTrue(any(url.endswith("/send") for url in post_urls))
        patched_html = patched_payloads[-1]["body"]["content"]
        self.assertIn("Hi Ryan", patched_html)
        self.assertNotIn("[NAME]", patched_html)

    def test_followup_name_resolution_rejects_malformed_contact_names(self):
        followup_config = {
            "followUps": [{"message": "Hi [NAME],\n\nJust following up."}],
        }

        for contact_name in (
            {"first": "Ryan"},
            ["Ryan", "Broker"],
            b"Ryan",
            123,
            True,
            "   ",
        ):
            with self.subTest(contact_name=contact_name):
                self.assertEqual(
                    "Hi [NAME],\n\nJust following up.",
                    followup._resolve_followup_message(
                        followup_config,
                        0,
                        contact_name,
                    ),
                )

    def test_default_followup_durably_captures_sheet_name_before_graph_send(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
            "to": ["broker@example.com"],
        })
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "followUps": [{"message": ""}],
        }
        thread_data = {
            "clientId": "client-1",
            "email": ["broker@example.com"],
            "contactName": "",
            "rowNumber": 12,
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": followup_config,
        }
        fake_fs = FakeFollowupFirestore([outbound], thread_data=thread_data)
        sheets = Mock()
        sheets.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [["", "", "", "", "Ryan Broker"]],
        }
        post_urls = []
        patched_payloads = []

        def run_request(callback, *args, **kwargs):
            return callback()

        def fake_get(url, **kwargs):
            self.assertIn("/me/messages", url)
            return FakeResponse(200, {
                "value": [{
                    "id": "graph-root",
                    "subject": "Current subject",
                    "conversationId": "conv-current",
                }]
            })

        def fake_post(url, **kwargs):
            post_urls.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {
                    "id": "reply-draft-default",
                    "toRecipients": [
                        {"emailAddress": {"address": "broker@example.com"}},
                    ],
                    "ccRecipients": [],
                })
            if url.endswith("/send"):
                marker = thread_data.get("followUpSendAttempt")
                self.assertIsNotNone(marker)
                self.assertEqual("sending", marker["state"])
                self.assertEqual("", marker["sendIdentity"]["rawMessage"])
                self.assertEqual("Ryan Broker", marker["sendIdentity"]["contactName"])
                self.assertTrue(marker["body"].startswith("Hi Ryan,"))
                self.assertEqual("broker@example.com", marker["recipient"])
                self.assertEqual("Ryan Broker", thread_data["contactName"])
                self.assertEqual(
                    followup._followup_config_fingerprint(followup_config),
                    marker["configFingerprint"],
                )
                return FakeResponse(202, {})
            raise AssertionError(f"Unexpected POST: {url}")

        def fake_patch(url, **kwargs):
            patched_payloads.append(kwargs.get("json") or {})
            return FakeResponse(200, {})

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup, "exponential_backoff_request", side_effect=run_request
        ), patch.object(requests, "get", side_effect=fake_get), patch.object(
            requests, "post", side_effect=fake_post
        ), patch.object(
            requests, "patch", side_effect=fake_patch
        ), patch.object(
            followup, "_save_followup_message", return_value=True
        ), patch(
            "email_automation.clients._get_sheet_id_or_fail",
            return_value="sheet-1",
        ), patch(
            "email_automation.clients._sheets_client",
            return_value=sheets,
        ), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ), patch.object(
            # Patch the verb, not the old helper name: the lane deletes through
            # the transport now, and a name-level mock stopped intercepting it.
            requests,
            "delete",
            return_value=FakeResponse(204),
        ):
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
                claim_owner="claim-owner",
            )

        self.assertTrue(result)
        self.assertTrue(any(url.endswith("/send") for url in post_urls))
        patched_html = patched_payloads[-1]["body"]["content"]
        self.assertIn("Hi Ryan", patched_html)
        self.assertNotIn("[NAME]", patched_html)

    def test_followup_graph_send_requires_claim_owner(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
            "to": ["broker@example.com"],
        })
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "followUps": [{"message": "Hi Alex, following up."}],
        }
        thread_data = {
            "clientId": "client-1",
            "email": ["broker@example.com"],
            "contactName": "Alex Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": followup_config,
        }
        fake_fs = FakeFollowupFirestore([outbound], thread_data=thread_data)
        post_urls = []

        def run_request(callback, *args, **kwargs):
            return callback()

        def fake_get(_url, **_kwargs):
            return FakeResponse(200, {
                "value": [{
                    "id": "graph-root",
                    "subject": "Current subject",
                    "conversationId": "conv-current",
                }]
            })

        def fake_post(url, **_kwargs):
            post_urls.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {
                    "id": "reply-draft-no-owner",
                    "toRecipients": [
                        {"emailAddress": {"address": "broker@example.com"}},
                    ],
                    "ccRecipients": [],
                })
            if url.endswith("/send"):
                return FakeResponse(202, {})
            raise AssertionError(f"Unexpected POST: {url}")

        deleted_urls = []
        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup, "exponential_backoff_request", side_effect=run_request
        ), patch.object(requests, "get", side_effect=fake_get), patch.object(
            requests, "post", side_effect=fake_post
        ), patch.object(
            requests, "patch", return_value=FakeResponse(200)
        ), patch(
            "email_automation.processing.is_contact_opted_out", return_value=None
        ), patch(
            "email_automation.email._delete_graph_reply_draft"
        ), patch("requests.delete", side_effect=_record_delete(deleted_urls)):
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
            )

        self.assertFalse(result)
        self.assertFalse(any(url.endswith("/send") for url in post_urls))
        self.assertEqual(len(deleted_urls), 1, f"draft not deleted: {deleted_urls}")
        self.assertIn("claim owner", followup._send_followup_email.last_error)
        self.assertTrue(followup._send_followup_email.guard_failed_closed)

    def test_graph_acceptance_does_not_commit_when_history_save_fails(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
            "to": ["broker@example.com"],
        })
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "followUps": [{"message": "Hi Alex, following up."}],
        }
        thread_data = {
            "clientId": "client-1",
            "email": ["broker@example.com"],
            "contactName": "Alex Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": followup_config,
        }
        fake_fs = FakeFollowupFirestore([outbound], thread_data=thread_data)
        post_urls = []

        def run_request(callback, *_args, **_kwargs):
            return callback()

        def fake_get(_url, **_kwargs):
            return FakeResponse(200, {
                "value": [{
                    "id": "graph-root",
                    "subject": "Current subject",
                    "conversationId": "conv-current",
                }]
            })

        def fake_post(url, **_kwargs):
            post_urls.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {
                    "id": "reply-draft-history-failure",
                    "toRecipients": [
                        {"emailAddress": {"address": "broker@example.com"}},
                    ],
                    "ccRecipients": [],
                })
            if url.endswith("/send"):
                return FakeResponse(202, {})
            raise AssertionError(f"Unexpected POST: {url}")

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup, "exponential_backoff_request", side_effect=run_request
        ), patch.object(requests, "get", side_effect=fake_get), patch.object(
            requests, "post", side_effect=fake_post
        ), patch.object(
            requests, "patch", return_value=FakeResponse(200)
        ), patch.object(
            followup, "_save_followup_message", return_value=False
        ), patch(
            "email_automation.processing.is_contact_opted_out", return_value=None
        ), patch("google.cloud.firestore.transactional", lambda fn: fn):
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
                claim_owner="claim-owner",
            )

        self.assertFalse(result)
        self.assertTrue(any(url.endswith("/send") for url in post_urls))
        marker = thread_data["followUpSendAttempt"]
        self.assertEqual("sending", marker["state"])
        outcome = followup._get_followup_send_outcome()
        self.assertEqual(marker["id"], outcome.attempt_id)
        self.assertIn("history persistence failed", outcome.error)
        self.assertFalse(outcome.guard_failed_closed)

    def test_send_intent_is_durable_before_graph_acceptance_crash(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
            "to": ["current@example.com"],
        })
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "nextFollowUpAt": datetime.now(timezone.utc) - followup.timedelta(hours=1),
            "processingBy": "claim-owner",
            "processingAt": datetime.now(timezone.utc),
            "followUps": [{"message": "Current body"}],
        }
        thread_state = {
            "clientId": "client-current",
            "email": ["current@example.com"],
            "contactName": "Current Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": followup_config,
        }
        fake_fs = FakeFollowupFirestore([outbound], thread_data=thread_state)

        def run_request(callback, *args, **kwargs):
            return callback()

        def fake_get(_url, **_kwargs):
            return FakeResponse(200, {
                "value": [{
                    "id": "graph-root",
                    "subject": "Current subject",
                    "conversationId": "conv-current",
                }]
            })

        def fake_post(url, **_kwargs):
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {
                    "id": "reply-draft-crash",
                    "toRecipients": [
                        {"emailAddress": {"address": "current@example.com"}},
                    ],
                    "ccRecipients": [],
                })
            if url.endswith("/send"):
                marker = thread_state.get("followUpSendAttempt")
                self.assertIsInstance(marker, dict)
                self.assertEqual("claim-owner", marker["owner"])
                self.assertEqual(0, marker["index"])
                self.assertEqual("current@example.com", marker["recipient"])
                self.assertEqual("Current body", marker["body"])
                self.assertEqual(
                    0,
                    thread_state["followUpConfig"]["lastSendAttemptIndex"],
                )
                self.assertIsNotNone(
                    thread_state["followUpConfig"]["lastSendAttemptAt"]
                )
                raise SystemExit("simulated hard exit after Graph acceptance")
            raise AssertionError(f"Unexpected POST: {url}")

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup, "exponential_backoff_request", side_effect=run_request
        ), patch.object(requests, "get", side_effect=fake_get), patch.object(
            requests, "post", side_effect=fake_post
        ), patch.object(
            requests, "patch", return_value=FakeResponse(200)
        ), patch(
            "email_automation.processing.is_contact_opted_out", return_value=None
        ), patch(
            "email_automation.email._hydrate_reply_all_draft_recipients",
            side_effect=lambda _headers, draft, **_kwargs: draft,
        ), patch(
            "email_automation.email._source_message_reply_all_fallback",
            side_effect=lambda draft, _source: draft,
        ), patch(
            "email_automation.email._reviewed_recipient_reply_all_fallback",
            side_effect=lambda draft, **_kwargs: draft,
        ), patch(
            "email_automation.email._filter_reply_all_draft_recipients",
            return_value={
                "payload": {
                    "toRecipients": [
                        {"emailAddress": {"address": "current@example.com"}},
                    ],
                    "ccRecipients": [],
                },
                "sentRecipients": ["current@example.com"],
            },
        ), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ), patch(
            "email_automation.email._delete_graph_reply_draft"
        ), self.assertRaisesRegex(SystemExit, "simulated hard exit"):
            followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_state,
                followup_config,
                0,
                claim_owner="claim-owner",
            )

    def test_send_intent_cas_rejects_exact_input_and_retry_changes(self):
        base_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "lastSendError": None,
            "lastSendAttemptAt": None,
            "lastSendAttemptIndex": None,
            "followUps": [{"message": "Hi [NAME], following up."}],
        }
        claimed_thread = {
            "clientId": "client-claimed",
            "email": ["claimed@example.com"],
            "contactName": "Claimed Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": base_config,
        }
        cases = {
            "owner": {"config": {"processingBy": "new-owner"}},
            "index": {"config": {"currentFollowUpIndex": 1}},
            "retry": {"config": {"lastSendError": "new ambiguous attempt"}},
            "body": {"followups": [{"message": "Changed body"}]},
            "send_body": {"body_arg": "Injected body"},
            "contact_name": {"thread": {"contactName": "Changed Broker"}},
            "recipient": {"thread": {"email": ["changed@example.com"]}},
            "client": {"thread": {"clientId": "client-changed"}},
        }

        for name, mutation in cases.items():
            with self.subTest(name=name):
                current_config = {
                    **base_config,
                    **mutation.get("config", {}),
                }
                if "followups" in mutation:
                    current_config["followUps"] = mutation["followups"]
                current_thread = {
                    **claimed_thread,
                    **mutation.get("thread", {}),
                    "followUpConfig": current_config,
                }
                thread_ref = FakeThreadRef(current_thread)

                with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    marker, reason = followup._persist_followup_send_intent(
                        "uid-1",
                        "thread-1",
                        claim_owner="claim-owner",
                        followup_index=0,
                        expected_thread_data=claimed_thread,
                        expected_followup_config=base_config,
                        recipient="claimed@example.com",
                        body=mutation.get("body_arg", "Hi Claimed, following up."),
                        subject="Claimed subject",
                        conversation_id="conv-claimed",
                        draft_id="draft-claimed",
                    )

                self.assertIsNone(marker)
                self.assertTrue(reason)
                self.assertEqual([], thread_ref.updates)

    def test_send_intent_cas_rejects_typed_contact_name_mutations(self):
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "followUps": [{"message": "Plain follow-up body."}],
        }
        missing = object()
        mutations = (
            (missing, None),
            (None, missing),
            (None, {}),
            (None, []),
            (None, 0),
            (None, False),
            ({}, None),
            ([], None),
            (0, None),
            (False, None),
            ({}, []),
            ([], {}),
            (0, False),
            (False, 0),
            ([0], [False]),
            ([False], [0]),
            ({"first": 0}, {"first": False}),
            ({"first": False}, {"first": 0}),
        )

        for claimed_name, current_name in mutations:
            with self.subTest(
                claimed_type=type(claimed_name).__name__,
                current_type=type(current_name).__name__,
            ):
                claimed_thread = {
                    "clientId": "client-claimed",
                    "email": ["claimed@example.com"],
                    "status": "active",
                    "followUpStatus": "waiting",
                    "hasInboundReply": False,
                    "followUpConfig": followup_config,
                }
                if claimed_name is not missing:
                    claimed_thread["contactName"] = claimed_name
                current_thread = dict(claimed_thread)
                if current_name is missing:
                    current_thread.pop("contactName", None)
                else:
                    current_thread["contactName"] = current_name
                thread_ref = FakeThreadRef(current_thread)

                with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    marker, reason = followup._persist_followup_send_intent(
                        "uid-1",
                        "thread-1",
                        claim_owner="claim-owner",
                        followup_index=0,
                        expected_thread_data=claimed_thread,
                        expected_followup_config=followup_config,
                        recipient="claimed@example.com",
                        body="Plain follow-up body.",
                        subject="Claimed subject",
                        conversation_id="conv-claimed",
                        draft_id="draft-claimed",
                    )

                self.assertIsNone(marker)
                self.assertTrue(reason)
                self.assertEqual([], thread_ref.updates)

    def test_send_intent_cas_rejects_full_identity_and_retry_type_mutations(self):
        base_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "lastSendError": None,
            "lastSendAttemptAt": None,
            "lastSendAttemptIndex": None,
            "followUps": [{"message": "Plain follow-up body."}],
        }
        base_thread = {
            "clientId": "client-claimed",
            "email": ["claimed@example.com"],
            "contactName": None,
            "ccEmails": [],
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
        }
        cases = (
            ("client_scalar", {"clientId": 0}, {"clientId": False}, {}, {}),
            ("client_nested", {"clientId": [0]}, {"clientId": [False]}, {}, {}),
            ("cc_scalar", {"ccEmails": [0]}, {"ccEmails": [False]}, {}, {}),
            (
                "cc_nested",
                {"ccEmails": [{"address": [0]}]},
                {"ccEmails": [{"address": [False]}]},
                {},
                {},
            ),
            (
                "retry_index",
                {},
                {},
                {"lastSendAttemptIndex": 0},
                {"lastSendAttemptIndex": False},
            ),
            (
                "retry_error",
                {},
                {},
                {"lastSendError": [0]},
                {"lastSendError": [False]},
            ),
            (
                "retry_attempt_at",
                {},
                {},
                {"lastSendAttemptAt": [0]},
                {"lastSendAttemptAt": [False]},
            ),
            (
                "retry_attempt_marker",
                {"followUpSendAttempt": {"nested": [0]}},
                {"followUpSendAttempt": {"nested": [False]}},
                {},
                {},
            ),
            (
                "raw_message",
                {},
                {},
                {"followUps": [{"message": [0]}]},
                {"followUps": [{"message": [False]}]},
            ),
            (
                "config_nested",
                {},
                {},
                {
                    "followUps": [{
                        "message": "Plain follow-up body.",
                        "metadata": [0],
                    }],
                },
                {
                    "followUps": [{
                        "message": "Plain follow-up body.",
                        "metadata": [False],
                    }],
                },
            ),
        )

        for name, claimed_patch, current_patch, claimed_retry, current_retry in cases:
            with self.subTest(name=name):
                claimed_config = {**base_config, **claimed_retry}
                current_config = {**base_config, **current_retry}
                claimed_thread = {
                    **base_thread,
                    **claimed_patch,
                    "followUpConfig": claimed_config,
                }
                current_thread = {
                    **base_thread,
                    **current_patch,
                    "followUpConfig": current_config,
                }
                thread_ref = FakeThreadRef(current_thread)

                with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    marker, reason = followup._persist_followup_send_intent(
                        "uid-1",
                        "thread-1",
                        claim_owner="claim-owner",
                        followup_index=0,
                        expected_thread_data=claimed_thread,
                        expected_followup_config=claimed_config,
                        recipient="claimed@example.com",
                        body="Plain follow-up body.",
                        subject="Claimed subject",
                        conversation_id="conv-claimed",
                        draft_id="draft-claimed",
                    )

                self.assertIsNone(marker)
                self.assertTrue(reason)
                self.assertEqual([], thread_ref.updates)

    def test_send_cas_rejects_source_presence_and_canonical_type_races(self):
        base_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "followUps": [{"message": "Plain follow-up body."}],
        }
        base_thread = {
            "clientId": "client-claimed",
            "email": ["claimed@example.com"],
            "contactName": None,
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
        }
        missing = object()
        attempted_at = datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc)
        cases = (
            ("client_absent_to_none", "thread", "clientId", missing, None),
            ("client_none_to_absent", "thread", "clientId", None, missing),
            ("cc_emails_absent_to_none", "thread", "ccEmails", missing, None),
            ("cc_emails_none_to_absent", "thread", "ccEmails", None, missing),
            (
                "cc_recipients_absent_to_none",
                "thread",
                "ccRecipients",
                missing,
                None,
            ),
            (
                "cc_recipients_none_to_absent",
                "thread",
                "ccRecipients",
                None,
                missing,
            ),
            ("row_absent_to_none", "thread", "rowNumber", missing, None),
            ("row_none_to_absent", "thread", "rowNumber", None, missing),
            (
                "email_string_to_list",
                "thread",
                "email",
                "claimed@example.com",
                ["claimed@example.com"],
            ),
            (
                "email_list_to_string",
                "thread",
                "email",
                ["claimed@example.com"],
                "claimed@example.com",
            ),
            (
                "config_datetime_to_iso_string",
                "config",
                "auditMetadata",
                attempted_at,
                attempted_at.isoformat(),
            ),
            (
                "config_iso_string_to_datetime",
                "config",
                "auditMetadata",
                attempted_at.isoformat(),
                attempted_at,
            ),
            (
                "index_absent_to_zero",
                "config",
                "currentFollowUpIndex",
                missing,
                0,
            ),
            (
                "index_zero_to_absent",
                "config",
                "currentFollowUpIndex",
                0,
                missing,
            ),
            (
                "owner_absent_to_claimed",
                "config",
                "processingBy",
                missing,
                "claim-owner",
            ),
            (
                "owner_none_to_claimed",
                "config",
                "processingBy",
                None,
                "claim-owner",
            ),
        )

        def apply_value(target, key, value):
            if value is missing:
                target.pop(key, None)
            else:
                target[key] = value

        for name, scope, key, claimed_value, current_value in cases:
            with self.subTest(name=name):
                claimed_config = dict(base_config)
                current_config = dict(base_config)
                claimed_thread = dict(base_thread)
                current_thread = dict(base_thread)
                if scope == "config":
                    apply_value(claimed_config, key, claimed_value)
                    apply_value(current_config, key, current_value)
                else:
                    apply_value(claimed_thread, key, claimed_value)
                    apply_value(current_thread, key, current_value)
                claimed_thread["followUpConfig"] = claimed_config
                current_thread["followUpConfig"] = current_config

                send_ref = FakeThreadRef(current_thread)
                with patch.object(
                    followup,
                    "_fs",
                    FakeFirestore(send_ref),
                ), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    marker, reason = followup._persist_followup_send_intent(
                        "uid-1",
                        "thread-1",
                        claim_owner="claim-owner",
                        followup_index=0,
                        expected_thread_data=claimed_thread,
                        expected_followup_config=claimed_config,
                        recipient="claimed@example.com",
                        body="Plain follow-up body.",
                        subject="Claimed subject",
                        conversation_id="conv-claimed",
                        draft_id="draft-claimed",
                    )

                self.assertIsNone(marker)
                self.assertTrue(reason)
                self.assertEqual([], send_ref.updates)

                migrate_ref = FakeThreadRef(current_thread)
                with patch.object(
                    followup,
                    "_fs",
                    FakeFirestore(migrate_ref),
                ), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    migrated, migrate_reason, _attempt_id = (
                        followup._migrate_legacy_sent_match(
                            "uid-1",
                            "thread-1",
                            claim_owner="claim-owner",
                            followup_index=0,
                            expected_thread_data=claimed_thread,
                            expected_followup_config=claimed_config,
                            recipient="claimed@example.com",
                            body="Plain follow-up body.",
                            subject="Claimed subject",
                            conversation_id="conv-claimed",
                            sent_match={
                                "id": "sent-legacy",
                                "sentDateTime": "2026-06-26T12:05:02Z",
                            },
                        )
                    )

                self.assertIsNone(migrated)
                self.assertTrue(migrate_reason)
                self.assertEqual([], migrate_ref.updates)

    def test_cross_index_type_mismatches_fail_at_every_irreversible_fence(self):
        cases = (
            ("bool_config_int_arg", False, 0),
            ("int_config_bool_arg", 0, False),
            ("float_config_int_arg", 0.0, 0),
            ("int_config_float_arg", 0, 0.0),
            ("string_config_int_arg", "0", 0),
            ("int_config_string_arg", 0, "0"),
            ("list_config_int_arg", [0], 0),
            ("int_config_list_arg", 0, [0]),
            ("dict_config_int_arg", {"nested": 0}, 0),
            ("int_config_dict_arg", 0, {"nested": 0}),
        )

        for name, config_index, argument_index in cases:
            followup_config = {
                "enabled": True,
                "currentFollowUpIndex": config_index,
                "processingBy": "claim-owner",
                "followUps": [
                    {"message": "Plain follow-up body."},
                    {"message": "Second follow-up body."},
                ],
            }
            thread_data = {
                "clientId": "client-claimed",
                "email": ["claimed@example.com"],
                "contactName": "Claimed Broker",
                "status": "active",
                "followUpStatus": "waiting",
                "hasInboundReply": False,
                "followUpConfig": followup_config,
            }

            with self.subTest(name=name, stage="send_intent"):
                send_ref = FakeThreadRef(thread_data)
                with patch.object(
                    followup,
                    "_fs",
                    FakeFirestore(send_ref),
                ), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    marker, reason = followup._persist_followup_send_intent(
                        "uid-1",
                        "thread-1",
                        claim_owner="claim-owner",
                        followup_index=argument_index,
                        expected_thread_data=thread_data,
                        expected_followup_config=followup_config,
                        recipient="claimed@example.com",
                        body="Plain follow-up body.",
                        subject="Claimed subject",
                        conversation_id="conv-claimed",
                        draft_id="draft-claimed",
                    )

                self.assertIsNone(marker)
                self.assertTrue(reason)
                self.assertEqual([], send_ref.updates)

            identity_index = (
                argument_index
                if type(argument_index) in {int, bool}
                else 0
            )
            accepted_identity = followup._followup_send_identity(
                thread_data,
                followup_config,
                identity_index,
            )
            attempt = {
                "id": f"attempt-{name}",
                "state": "uncertain",
                "owner": "claim-owner",
                "index": argument_index,
                "sendIdentity": accepted_identity,
            }

            with self.subTest(name=name, stage="post_accept"):
                post_thread = {
                    **thread_data,
                    "followUpSendAttempt": attempt,
                }
                post_ref = FakeThreadRef(post_thread)
                with patch.object(
                    followup,
                    "_fs",
                    FakeFirestore(post_ref),
                ), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    outcome = followup._schedule_next_followup(
                        "uid-1",
                        "thread-1",
                        followup_config,
                        just_sent_index=argument_index,
                        claim_owner="claim-owner",
                        send_attempt_id=attempt["id"],
                    )

                self.assertEqual(
                    "ambiguous",
                    getattr(outcome, "value", outcome),
                )
                self.assertEqual([], post_ref.updates)

            with self.subTest(name=name, stage="reconciliation"):
                reconcile_thread = {
                    **thread_data,
                    "followUpSendAttempt": attempt,
                }
                reconcile_ref = FakeThreadRef(reconcile_thread)
                with patch.object(
                    followup,
                    "_fs",
                    FakeFirestore(reconcile_ref),
                ), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    reconciled, reconcile_reason = (
                        followup._record_reconciled_followup_attempt(
                            "uid-1",
                            "thread-1",
                            claim_owner="claim-owner",
                            followup_index=argument_index,
                            expected_attempt=attempt,
                            expected_identity=accepted_identity,
                            expected_retry=followup._followup_retry_signature(
                                reconcile_thread,
                                followup_config,
                            ),
                            sent_match={
                                "id": "sent-index-mismatch",
                                "sentDateTime": "2026-06-26T12:05:02Z",
                            },
                        )
                    )

                self.assertIsNone(reconciled)
                self.assertTrue(reconcile_reason)
                self.assertEqual([], reconcile_ref.updates)

    def test_complete_identity_proof_rejects_cross_field_inconsistencies(self):
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "followUps": [{"message": "Plain follow-up body."}],
            "auditMetadata": {"nested": [0, False, "0"]},
        }
        thread_data = {
            "clientId": "client-proof",
            "email": [" Proof@Example.com "],
            "contactName": "Proof Broker",
            "ccEmails": [],
            "ccRecipients": ["cc@example.com"],
            "rowNumber": 7,
        }
        identity = followup._followup_send_identity(
            thread_data,
            followup_config,
            0,
        )
        self.assertTrue(followup._followup_send_identity_has_complete_proof(identity))

        def replace_durable_config(proof, config):
            proof["inputSignatures"]["config"]["durable"]["value"] = (
                followup._typed_followup_identity_value(config)
            )

        cases = {
            "recipient_vs_raw_email": lambda proof: proof.update(
                recipient="other@example.com"
            ),
            "raw_message_vs_followups": lambda proof: proof.update(
                rawMessage="Tampered follow-up body."
            ),
            "client_vs_raw_client": lambda proof: proof.update(
                clientId="other-client"
            ),
            "contact_type_vs_raw_contact": lambda proof: proof.update(
                contactNameType="builtins.list"
            ),
            "effective_cc_vs_raw_sources": lambda proof: proof.update(
                ccRecipients=["other@example.com"]
            ),
            "fingerprint_vs_durable_config": lambda proof: proof.update(
                configFingerprint="0" * 64
            ),
            "config_index_vs_argument": lambda proof: proof["inputSignatures"][
                "config"
            ]["currentFollowUpIndex"].update(
                value=followup._typed_followup_identity_value(False)
            ),
            "durable_index_vs_field": lambda proof: replace_durable_config(
                proof,
                {**followup_config, "currentFollowUpIndex": False},
            ),
            "durable_followups_vs_field": lambda proof: replace_durable_config(
                proof,
                {**followup_config, "followUps": [{"message": "Changed body"}]},
            ),
            "row_bool_is_not_positive_int": lambda proof: proof[
                "inputSignatures"
            ]["thread"]["rowNumber"].update(
                value=followup._typed_followup_identity_value(True)
            ),
        }

        for name, mutate in cases.items():
            with self.subTest(name=name):
                tampered = deepcopy(identity)
                mutate(tampered)
                self.assertFalse(
                    followup._followup_send_identity_has_complete_proof(tampered)
                )

    def test_send_envelope_tampering_fails_at_every_recovery_fence(self):
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "followUps": [{"message": "Plain follow-up body."}],
        }
        thread_data = {
            "clientId": "client-envelope",
            "email": ["claimed@example.com"],
            "contactName": "Claimed Broker",
            "ccEmails": ["cc@example.com"],
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": followup_config,
        }
        intent_fs = FakeFollowupFirestore([], thread_data=thread_data)
        with patch.object(followup, "_fs", intent_fs), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            marker, reason = followup._persist_followup_send_intent(
                "uid-1",
                "thread-1",
                claim_owner="claim-owner",
                followup_index=0,
                expected_thread_data=thread_data,
                expected_followup_config=followup_config,
                recipient="claimed@example.com",
                body="Plain follow-up body.",
                subject="Claimed subject",
                conversation_id="conv-claimed",
                draft_id="draft-claimed",
                to_recipients=["claimed@example.com"],
                cc_recipients=["cc@example.com"],
            )

        self.assertIsNone(reason)
        self.assertIsNotNone(marker)
        base_thread = deepcopy(thread_data)

        def remove_field(field):
            return lambda attempt: attempt.pop(field, None)

        def change_all(attempt):
            attempt.update({
                "recipient": "other@example.com",
                "body": "Other body",
                "subject": "Other subject",
                "conversationId": "conv-other",
                "draftId": "draft-other",
                "toRecipients": ["other@example.com"],
                "ccRecipients": ["other-cc@example.com"],
            })

        def forge_self_consistent_envelope(attempt):
            payload = followup._followup_send_envelope_payload(attempt)
            proved_fields = dict(payload)
            send_identity = proved_fields.pop("sendIdentity")
            proof = followup._typed_followup_identity_value({
                "fields": proved_fields,
                "sendIdentityHash": followup._followup_typed_value_hash(
                    send_identity
                ),
            })
            attempt["envelopeProof"] = proof
            attempt["inputHash"] = followup._followup_send_envelope_hash(proof)

        validation_now = datetime.now(timezone.utc) + followup.timedelta(seconds=1)
        ordered_timestamp = datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc)
        firestore_timestamp = DatetimeWithNanoseconds(
            2026,
            6,
            26,
            12,
            5,
            tzinfo=timezone.utc,
        )

        cases = (
            ("exact", None),
            (
                "exact_timestamp_now",
                lambda attempt: attempt.update(
                    createdAt=validation_now,
                    sendStartedAt=validation_now,
                ),
            ),
            (
                "exact_timestamp_iso_z",
                lambda attempt: attempt.update(
                    createdAt="2026-06-26T12:05:00Z",
                    sendStartedAt="2026-06-26T12:05:00Z",
                ),
            ),
            (
                "exact_timestamp_iso_offset",
                lambda attempt: attempt.update(
                    createdAt="2026-06-26T05:05:00-07:00",
                    sendStartedAt="2026-06-26T05:05:00-07:00",
                ),
            ),
            (
                "exact_timestamp_firestore",
                lambda attempt: attempt.update(
                    createdAt=firestore_timestamp,
                    sendStartedAt=firestore_timestamp,
                ),
            ),
            ("attempt_id", lambda attempt: attempt.update(id="attempt-other")),
            ("owner", lambda attempt: attempt.update(owner="other-owner")),
            (
                "created_at",
                lambda attempt: attempt.update(createdAt="2026-06-26T12:00:00Z"),
            ),
            (
                "send_started_at",
                lambda attempt: attempt.update(
                    sendStartedAt="2026-06-26T12:00:00Z"
                ),
            ),
            (
                "config_fingerprint",
                lambda attempt: attempt.update(configFingerprint="0" * 64),
            ),
            (
                "client_id",
                lambda attempt: attempt.update(clientId="client-other"),
            ),
            (
                "recipient",
                lambda attempt: attempt.update(recipient="other@example.com"),
            ),
            ("body", lambda attempt: attempt.update(body="Other body")),
            ("subject", lambda attempt: attempt.update(subject="Other subject")),
            (
                "conversation",
                lambda attempt: attempt.update(conversationId="conv-other"),
            ),
            ("draft", lambda attempt: attempt.update(draftId="draft-other")),
            (
                "to_recipients",
                lambda attempt: attempt.update(toRecipients=["other@example.com"]),
            ),
            (
                "cc_recipients",
                lambda attempt: attempt.update(
                    ccRecipients=["other-cc@example.com"]
                ),
            ),
            ("combined", change_all),
            ("missing_envelope_proof", remove_field("envelopeProof")),
            (
                "malformed_envelope_proof",
                lambda attempt: attempt.update(
                    envelopeProof=followup._typed_followup_identity_value(
                        {"body": "Other body"}
                    )
                ),
            ),
            ("missing_input_hash", remove_field("inputHash")),
            ("changed_input_hash", lambda attempt: attempt.update(inputHash="0" * 64)),
            (
                "timestamp_empty_created",
                lambda attempt: attempt.update(createdAt=""),
            ),
            (
                "timestamp_garbage_started",
                lambda attempt: attempt.update(sendStartedAt="not-a-timestamp"),
            ),
            (
                "timestamp_bool_created",
                lambda attempt: attempt.update(createdAt=False),
            ),
            (
                "timestamp_nan_started",
                lambda attempt: attempt.update(sendStartedAt=float("nan")),
            ),
            (
                "timestamp_naive_datetime",
                lambda attempt: attempt.update(
                    createdAt=datetime(2026, 6, 26, 12, 5),
                ),
            ),
            (
                "timestamp_naive_iso",
                lambda attempt: attempt.update(
                    sendStartedAt="2026-06-26T12:05:00",
                ),
            ),
            (
                "timestamp_invalid_timezone",
                lambda attempt: attempt.update(
                    sendStartedAt="2026-06-26T12:05:00+25:00",
                ),
            ),
            (
                "timestamp_reversed_order",
                lambda attempt: attempt.update(
                    createdAt=ordered_timestamp,
                    sendStartedAt=ordered_timestamp - followup.timedelta(seconds=1),
                ),
            ),
            ("timestamp_missing_lease", remove_field("leaseUntil")),
            (
                "timestamp_garbage_lease",
                lambda attempt: attempt.update(leaseUntil="not-a-timestamp"),
            ),
            (
                "timestamp_lease_before_send",
                lambda attempt: attempt.update(leaseUntil=ordered_timestamp),
            ),
            (
                "timestamp_future_1_second",
                lambda attempt: attempt.update(
                    createdAt=validation_now + followup.timedelta(seconds=1),
                    sendStartedAt=validation_now + followup.timedelta(seconds=1),
                ),
            ),
            (
                "timestamp_future_60_seconds",
                lambda attempt: attempt.update(
                    createdAt=validation_now + followup.timedelta(seconds=60),
                    sendStartedAt=validation_now + followup.timedelta(seconds=60),
                ),
            ),
            (
                "timestamp_future_299_seconds",
                lambda attempt: attempt.update(
                    createdAt=validation_now + followup.timedelta(seconds=299),
                    sendStartedAt=validation_now + followup.timedelta(seconds=299),
                ),
            ),
            (
                "timestamp_future_1_day",
                lambda attempt: attempt.update(
                    createdAt=validation_now + followup.timedelta(days=1),
                    sendStartedAt=validation_now + followup.timedelta(days=1),
                ),
            ),
        )

        sent_match = {
            "id": "sent-envelope",
            "sentDateTime": "2026-06-26T12:05:02Z",
        }
        for name, mutate in cases:
            attempt = deepcopy(marker)
            if mutate is not None:
                mutate(attempt)
            is_timestamp_case = name.startswith("timestamp_")
            is_exact_case = name.startswith("exact")
            if is_timestamp_case or name.startswith("exact_timestamp_"):
                forge_self_consistent_envelope(attempt)

            with self.subTest(name=name, stage="prelookup_reconciliation"):
                current_thread = deepcopy(base_thread)
                current_thread["followUpSendAttempt"] = deepcopy(attempt)
                reconcile_fs = FakeFollowupFirestore(
                    [],
                    thread_data=current_thread,
                )
                with patch.object(followup, "_fs", reconcile_fs), patch.object(
                    followup,
                    "datetime",
                    _fixed_datetime(validation_now),
                ), patch.object(
                    followup,
                    "find_matching_sent_message_for_retry",
                    return_value=sent_match,
                ) as sent_guard, patch.object(
                    followup,
                    "_save_followup_message",
                    return_value=True,
                ) as save_followup, patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    result = followup._reconcile_durable_followup_attempt(
                        "uid-1",
                        "thread-1",
                        {"Authorization": "Bearer token"},
                        current_thread,
                        0,
                        "claim-owner",
                    )

                if is_exact_case:
                    self.assertTrue(result)
                    sent_guard.assert_called_once()
                    save_followup.assert_called_once()
                    self.assertTrue(reconcile_fs.updates)
                else:
                    self.assertFalse(result)
                    sent_guard.assert_not_called()
                    save_followup.assert_not_called()
                    self.assertEqual([], reconcile_fs.updates)

            with self.subTest(name=name, stage="transactional_reconciliation"):
                record_thread = deepcopy(base_thread)
                record_thread["followUpSendAttempt"] = deepcopy(attempt)
                record_ref = FakeThreadRef(record_thread)
                with patch.object(
                    followup,
                    "_fs",
                    FakeFirestore(record_ref),
                ), patch.object(
                    followup,
                    "datetime",
                    _fixed_datetime(validation_now),
                ), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    reconciled, record_reason = (
                        followup._record_reconciled_followup_attempt(
                            "uid-1",
                            "thread-1",
                            claim_owner="claim-owner",
                            followup_index=0,
                            expected_attempt=attempt,
                            expected_identity=followup._followup_send_identity(
                                record_thread,
                                record_thread["followUpConfig"],
                                0,
                            ),
                            expected_retry=followup._followup_retry_signature(
                                record_thread,
                                record_thread["followUpConfig"],
                            ),
                            sent_match=sent_match,
                        )
                    )

                if is_exact_case:
                    self.assertIsNotNone(reconciled)
                    self.assertIsNone(record_reason)
                    self.assertTrue(record_ref.updates)
                else:
                    self.assertIsNone(reconciled)
                    self.assertTrue(record_reason)
                    self.assertEqual([], record_ref.updates)

            with self.subTest(name=name, stage="postaccept"):
                schedule_thread = deepcopy(base_thread)
                schedule_thread["followUpSendAttempt"] = deepcopy(attempt)
                schedule_ref = FakeThreadRef(schedule_thread)
                with patch.object(
                    followup,
                    "_fs",
                    FakeFirestore(schedule_ref),
                ), patch.object(
                    followup,
                    "datetime",
                    _fixed_datetime(validation_now),
                ), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    outcome = followup._schedule_next_followup(
                        "uid-1",
                        "thread-1",
                        schedule_thread["followUpConfig"],
                        just_sent_index=0,
                        claim_owner="claim-owner",
                        send_attempt_id=attempt["id"],
                        send_attempt_marker=attempt,
                    )

                if is_exact_case:
                    self.assertNotEqual(
                        "ambiguous",
                        getattr(outcome, "value", outcome),
                    )
                    self.assertTrue(schedule_ref.updates)
                else:
                    self.assertEqual(
                        "ambiguous",
                        getattr(outcome, "value", outcome),
                    )
                    self.assertEqual([], schedule_ref.updates)

            if is_timestamp_case:
                with self.subTest(name=name, stage="sealing"):
                    unsealed = deepcopy(attempt)
                    unsealed.pop("envelopeProof", None)
                    unsealed.pop("inputHash", None)
                    with patch.object(
                        followup,
                        "datetime",
                        _fixed_datetime(validation_now),
                    ), self.assertRaises(ValueError):
                        followup._seal_followup_send_envelope(unsealed)

    def test_legacy_migration_validates_attempt_timestamp_schema(self):
        missing = object()
        validation_now = datetime.now(timezone.utc)
        cases = (
            ("exact_now", validation_now, True),
            (
                "datetime",
                datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc),
                True,
            ),
            ("iso_z", "2026-06-26T12:05:00Z", True),
            ("iso_offset", "2026-06-26T05:05:00-07:00", True),
            (
                "firestore",
                DatetimeWithNanoseconds(
                    2026,
                    6,
                    26,
                    12,
                    5,
                    tzinfo=timezone.utc,
                ),
                True,
            ),
            ("missing", missing, False),
            ("none", None, False),
            ("empty", "", False),
            ("garbage", "not-a-timestamp", False),
            ("bool", False, False),
            ("nan", float("nan"), False),
            ("naive_datetime", datetime(2026, 6, 26, 12, 5), False),
            ("naive_iso", "2026-06-26T12:05:00", False),
            (
                "future_1_second",
                validation_now + followup.timedelta(seconds=1),
                False,
            ),
            (
                "future_60_seconds",
                validation_now + followup.timedelta(seconds=60),
                False,
            ),
            (
                "future_299_seconds",
                validation_now + followup.timedelta(seconds=299),
                False,
            ),
            (
                "future_1_day",
                validation_now + followup.timedelta(days=1),
                False,
            ),
        )

        for name, attempted_at, is_valid in cases:
            with self.subTest(name=name):
                followup_config = {
                    "enabled": True,
                    "currentFollowUpIndex": 0,
                    "processingBy": "claim-owner",
                    "lastSendError": "ambiguous Graph result",
                    "lastSendAttemptIndex": 0,
                    "followUps": [{"message": "Plain follow-up body."}],
                }
                if attempted_at is not missing:
                    followup_config["lastSendAttemptAt"] = attempted_at
                thread_data = {
                    "clientId": "client-legacy-time",
                    "email": ["legacy@example.com"],
                    "contactName": "Legacy Broker",
                    "status": "active",
                    "followUpStatus": "waiting",
                    "hasInboundReply": False,
                    "followUpConfig": followup_config,
                }
                thread_ref = FakeThreadRef(thread_data)

                with patch.object(
                    followup,
                    "_fs",
                    FakeFirestore(thread_ref),
                ), patch.object(
                    followup,
                    "datetime",
                    _fixed_datetime(validation_now),
                ), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    marker, reason, _attempt_id = (
                        followup._migrate_legacy_sent_match(
                            "uid-1",
                            "thread-1",
                            claim_owner="claim-owner",
                            followup_index=0,
                            expected_thread_data=thread_data,
                            expected_followup_config=followup_config,
                            recipient="legacy@example.com",
                            body="Plain follow-up body.",
                            subject="Legacy subject",
                            conversation_id="conv-legacy",
                            sent_match={
                                "id": "sent-legacy",
                                "sentDateTime": "2026-06-26T12:05:02Z",
                            },
                        )
                    )

                if is_valid:
                    expected_timestamp = followup._followup_utc_timestamp(
                        attempted_at
                    )
                    self.assertIsNotNone(marker)
                    self.assertIsNone(reason)
                    self.assertEqual(expected_timestamp, marker["createdAt"])
                    self.assertEqual(expected_timestamp, marker["sendStartedAt"])
                    self.assertEqual(1, len(thread_ref.updates))
                else:
                    self.assertIsNone(marker)
                    self.assertTrue(reason)
                    self.assertEqual([], thread_ref.updates)

    def test_legacy_migration_rejects_full_identity_and_retry_type_mutations(self):
        attempted_at = datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc)
        base_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "lastSendError": None,
            "lastSendAttemptAt": attempted_at,
            "lastSendAttemptIndex": None,
            "followUps": [{"message": "Plain follow-up body."}],
        }
        base_thread = {
            "clientId": "client-claimed",
            "email": ["claimed@example.com"],
            "contactName": None,
            "ccEmails": [],
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
        }
        cases = (
            ("client_scalar", {"clientId": 0}, {"clientId": False}, {}, {}),
            ("client_nested", {"clientId": [0]}, {"clientId": [False]}, {}, {}),
            ("cc_scalar", {"ccEmails": [0]}, {"ccEmails": [False]}, {}, {}),
            (
                "cc_nested",
                {"ccEmails": [{"address": [0]}]},
                {"ccEmails": [{"address": [False]}]},
                {},
                {},
            ),
            (
                "retry_index",
                {},
                {},
                {"lastSendAttemptIndex": 0},
                {"lastSendAttemptIndex": False},
            ),
            (
                "retry_error",
                {},
                {},
                {"lastSendError": [0]},
                {"lastSendError": [False]},
            ),
            (
                "retry_attempt_at",
                {},
                {},
                {"lastSendAttemptAt": [0]},
                {"lastSendAttemptAt": [False]},
            ),
            (
                "retry_attempt_marker",
                {"followUpSendAttempt": {"nested": [0]}},
                {"followUpSendAttempt": {"nested": [False]}},
                {},
                {},
            ),
            (
                "raw_message",
                {},
                {},
                {"followUps": [{"message": [0]}]},
                {"followUps": [{"message": [False]}]},
            ),
            (
                "config_nested",
                {},
                {},
                {
                    "followUps": [{
                        "message": "Plain follow-up body.",
                        "metadata": [0],
                    }],
                },
                {
                    "followUps": [{
                        "message": "Plain follow-up body.",
                        "metadata": [False],
                    }],
                },
            ),
        )

        for name, claimed_patch, current_patch, claimed_retry, current_retry in cases:
            with self.subTest(name=name):
                claimed_config = {**base_config, **claimed_retry}
                current_config = {**base_config, **current_retry}
                claimed_thread = {
                    **base_thread,
                    **claimed_patch,
                    "followUpConfig": claimed_config,
                }
                current_thread = {
                    **base_thread,
                    **current_patch,
                    "followUpConfig": current_config,
                }
                thread_ref = FakeThreadRef(current_thread)

                with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    marker, reason, _attempt_id = followup._migrate_legacy_sent_match(
                        "uid-1",
                        "thread-1",
                        claim_owner="claim-owner",
                        followup_index=0,
                        expected_thread_data=claimed_thread,
                        expected_followup_config=claimed_config,
                        recipient="claimed@example.com",
                        body="Plain follow-up body.",
                        subject="Claimed subject",
                        conversation_id="conv-claimed",
                        sent_match={
                            "id": "sent-legacy",
                            "sentDateTime": "2026-06-26T12:05:02Z",
                        },
                    )

                self.assertIsNone(marker)
                self.assertTrue(reason)
                self.assertEqual([], thread_ref.updates)

    def test_full_typed_fences_allow_stable_exact_values(self):
        attempted_at = datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc)
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "lastSendError": [0],
            "lastSendAttemptAt": attempted_at,
            "lastSendAttemptIndex": False,
            "followUps": [{"message": "Plain follow-up body."}],
        }
        thread_data = {
            "clientId": [0],
            "email": ["claimed@example.com"],
            "contactName": None,
            "ccEmails": [{"address": [False]}],
            "followUpSendAttempt": {"metadata": [0]},
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": followup_config,
        }

        send_ref = FakeThreadRef(dict(thread_data))
        with patch.object(followup, "_fs", FakeFirestore(send_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            marker, reason = followup._persist_followup_send_intent(
                "uid-1",
                "thread-1",
                claim_owner="claim-owner",
                followup_index=0,
                expected_thread_data=thread_data,
                expected_followup_config=followup_config,
                recipient="claimed@example.com",
                body="Plain follow-up body.",
                subject="Claimed subject",
                conversation_id="conv-claimed",
                draft_id="draft-claimed",
            )

        self.assertIsNotNone(marker)
        self.assertIsNone(reason)
        self.assertEqual(1, len(send_ref.updates))

        migrate_ref = FakeThreadRef(dict(thread_data))
        with patch.object(followup, "_fs", FakeFirestore(migrate_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            migrated, migrate_reason, _attempt_id = (
                followup._migrate_legacy_sent_match(
                    "uid-1",
                    "thread-1",
                    claim_owner="claim-owner",
                    followup_index=0,
                    expected_thread_data=thread_data,
                    expected_followup_config=followup_config,
                    recipient="claimed@example.com",
                    body="Plain follow-up body.",
                    subject="Claimed subject",
                    conversation_id="conv-claimed",
                    sent_match={
                        "id": "sent-legacy",
                        "sentDateTime": "2026-06-26T12:05:02Z",
                    },
                )
            )

        self.assertIsNotNone(migrated)
        self.assertIsNone(migrate_reason)
        self.assertEqual(1, len(migrate_ref.updates))

    def test_retry_signature_rejects_absence_none_races(self):
        base_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "followUps": [{"message": "Plain follow-up body."}],
        }
        base_thread = {
            "clientId": "client-claimed",
            "email": ["claimed@example.com"],
            "contactName": None,
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
        }
        retry_fields = (
            "lastSendError",
            "lastSendAttemptAt",
            "lastSendAttemptIndex",
            "followUpSendAttempt",
        )

        for field in retry_fields:
            for claimed_present, current_present in ((False, True), (True, False)):
                with self.subTest(
                    field=field,
                    claimed_present=claimed_present,
                    current_present=current_present,
                ):
                    claimed_config = dict(base_config)
                    current_config = dict(base_config)
                    claimed_thread = dict(base_thread)
                    current_thread = dict(base_thread)
                    if field == "followUpSendAttempt":
                        if claimed_present:
                            claimed_thread[field] = None
                        if current_present:
                            current_thread[field] = None
                    else:
                        if claimed_present:
                            claimed_config[field] = None
                        if current_present:
                            current_config[field] = None
                    claimed_thread["followUpConfig"] = claimed_config
                    current_thread["followUpConfig"] = current_config

                    send_ref = FakeThreadRef(current_thread)
                    with patch.object(
                        followup,
                        "_fs",
                        FakeFirestore(send_ref),
                    ), patch(
                        "google.cloud.firestore.transactional", lambda fn: fn
                    ):
                        marker, reason = followup._persist_followup_send_intent(
                            "uid-1",
                            "thread-1",
                            claim_owner="claim-owner",
                            followup_index=0,
                            expected_thread_data=claimed_thread,
                            expected_followup_config=claimed_config,
                            recipient="claimed@example.com",
                            body="Plain follow-up body.",
                            subject="Claimed subject",
                            conversation_id="conv-claimed",
                            draft_id="draft-claimed",
                        )

                    self.assertIsNone(marker)
                    self.assertTrue(reason)
                    self.assertEqual([], send_ref.updates)

                    migrate_ref = FakeThreadRef(current_thread)
                    with patch.object(
                        followup,
                        "_fs",
                        FakeFirestore(migrate_ref),
                    ), patch(
                        "google.cloud.firestore.transactional", lambda fn: fn
                    ):
                        migrated, migrate_reason, _attempt_id = (
                            followup._migrate_legacy_sent_match(
                                "uid-1",
                                "thread-1",
                                claim_owner="claim-owner",
                                followup_index=0,
                                expected_thread_data=claimed_thread,
                                expected_followup_config=claimed_config,
                                recipient="claimed@example.com",
                                body="Plain follow-up body.",
                                subject="Claimed subject",
                                conversation_id="conv-claimed",
                                sent_match={
                                    "id": "sent-legacy",
                                    "sentDateTime": "2026-06-26T12:05:02Z",
                                },
                            )
                        )

                    self.assertIsNone(migrated)
                    self.assertTrue(migrate_reason)
                    self.assertEqual([], migrate_ref.updates)

    def test_new_intent_allows_stable_absence_but_legacy_migration_requires_time(self):
        for explicit_none in (False, True):
            with self.subTest(explicit_none=explicit_none):
                followup_config = {
                    "enabled": True,
                    "currentFollowUpIndex": 0,
                    "processingBy": "claim-owner",
                    "followUps": [{"message": "Plain follow-up body."}],
                }
                thread_data = {
                    "clientId": "client-claimed",
                    "email": ["claimed@example.com"],
                    "contactName": None,
                    "status": "active",
                    "followUpStatus": "waiting",
                    "hasInboundReply": False,
                    "followUpConfig": followup_config,
                }
                if explicit_none:
                    followup_config.update({
                        "lastSendError": None,
                        "lastSendAttemptAt": None,
                        "lastSendAttemptIndex": None,
                    })
                    thread_data["followUpSendAttempt"] = None

                send_ref = FakeThreadRef(dict(thread_data))
                with patch.object(
                    followup,
                    "_fs",
                    FakeFirestore(send_ref),
                ), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    marker, reason = followup._persist_followup_send_intent(
                        "uid-1",
                        "thread-1",
                        claim_owner="claim-owner",
                        followup_index=0,
                        expected_thread_data=thread_data,
                        expected_followup_config=followup_config,
                        recipient="claimed@example.com",
                        body="Plain follow-up body.",
                        subject="Claimed subject",
                        conversation_id="conv-claimed",
                        draft_id="draft-claimed",
                    )

                self.assertIsNotNone(marker)
                self.assertIsNone(reason)
                self.assertEqual(1, len(send_ref.updates))

                migrate_ref = FakeThreadRef(dict(thread_data))
                with patch.object(
                    followup,
                    "_fs",
                    FakeFirestore(migrate_ref),
                ), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    migrated, migrate_reason, _attempt_id = (
                        followup._migrate_legacy_sent_match(
                            "uid-1",
                            "thread-1",
                            claim_owner="claim-owner",
                            followup_index=0,
                            expected_thread_data=thread_data,
                            expected_followup_config=followup_config,
                            recipient="claimed@example.com",
                            body="Plain follow-up body.",
                            subject="Claimed subject",
                            conversation_id="conv-claimed",
                            sent_match={
                                "id": "sent-legacy",
                                "sentDateTime": "2026-06-26T12:05:02Z",
                            },
                        )
                    )

                self.assertIsNone(migrated)
                self.assertIn("lastSendAttemptAt", migrate_reason)
                self.assertEqual([], migrate_ref.updates)

    def test_reconciliation_rejects_inexact_legacy_contact_identities(self):
        attempted_at = datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc)
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "reconcile-owner",
            "lastSendError": "Graph send outcome pending reconciliation",
            "lastSendAttemptAt": attempted_at,
            "lastSendAttemptIndex": 0,
            "followUps": [{"message": "Plain follow-up body."}],
        }
        missing = object()
        cases = (
            ([0], [False]),
            ([False], [0]),
            ({"nested": [0]}, {"nested": [False]}),
            ({"nested": {"value": False}}, {"nested": {"value": 0}}),
            (None, False),
            (None, 0),
            (None, {}),
            (None, missing),
            (missing, None),
            (missing, missing),
        )

        for accepted_name, current_name in cases:
            with self.subTest(
                accepted_type=type(accepted_name).__name__,
                current_type=type(current_name).__name__,
            ):
                accepted_thread = {
                    "clientId": "client-current",
                    "email": ["current@example.com"],
                    "status": "active",
                    "followUpStatus": "waiting",
                    "hasInboundReply": False,
                    "followUpConfig": followup_config,
                }
                if accepted_name is not missing:
                    accepted_thread["contactName"] = accepted_name
                legacy_identity = followup._followup_send_identity(
                    accepted_thread,
                    followup_config,
                    0,
                )
                legacy_identity.pop("contactNameExact")
                legacy_identity.pop("contactNamePresent")
                legacy_identity.pop("contactNameType")
                legacy_identity.pop("inputSignatures")
                legacy_identity["contactName"] = (
                    legacy_identity.get("contactName") or ""
                )

                marker = {
                    "id": "attempt-legacy",
                    "state": "uncertain",
                    "owner": "reconcile-owner",
                    "index": 0,
                    "sendIdentity": legacy_identity,
                }
                current_thread = {
                    "clientId": "client-current",
                    "email": ["current@example.com"],
                    "status": "active",
                    "followUpStatus": "waiting",
                    "hasInboundReply": False,
                    "followUpSendAttempt": marker,
                    "followUpConfig": followup_config,
                }
                if current_name is not missing:
                    current_thread["contactName"] = current_name
                thread_ref = FakeThreadRef(current_thread)

                with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    reconciled, reason = followup._record_reconciled_followup_attempt(
                        "uid-1",
                        "thread-1",
                        claim_owner="reconcile-owner",
                        followup_index=0,
                        expected_attempt=followup._canonical_followup_value(marker),
                        expected_identity=legacy_identity,
                        expected_retry=followup._followup_retry_signature(
                            current_thread,
                            followup_config,
                        ),
                        sent_match={
                            "id": "sent-legacy",
                            "sentDateTime": "2026-06-26T12:05:02Z",
                        },
                    )

                self.assertIsNone(reconciled)
                self.assertTrue(reason)
                self.assertEqual([], thread_ref.updates)

    def test_reconciliation_rejects_attempt_datetime_string_race(self):
        attempted_at = datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc)
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "reconcile-owner",
            "lastSendError": "Graph send outcome pending reconciliation",
            "lastSendAttemptAt": attempted_at,
            "lastSendAttemptIndex": 0,
            "followUps": [{"message": "Plain follow-up body."}],
        }
        claimed_thread = {
            "clientId": "client-current",
            "email": ["current@example.com"],
            "contactName": "Ryan Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": followup_config,
        }
        claimed_attempt = {
            "id": "attempt-claimed",
            "state": "uncertain",
            "owner": "reconcile-owner",
            "index": 0,
            "createdAt": attempted_at,
        }
        current_attempt = {
            **claimed_attempt,
            "createdAt": attempted_at.isoformat(),
        }
        current_thread = {
            **claimed_thread,
            "followUpSendAttempt": current_attempt,
        }
        thread_ref = FakeThreadRef(current_thread)

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            reconciled, reason = followup._record_reconciled_followup_attempt(
                "uid-1",
                "thread-1",
                claim_owner="reconcile-owner",
                followup_index=0,
                expected_attempt=claimed_attempt,
                expected_identity=followup._followup_send_identity(
                    claimed_thread,
                    followup_config,
                    0,
                ),
                expected_retry=followup._followup_retry_signature(
                    current_thread,
                    followup_config,
                ),
                sent_match={
                    "id": "sent-claimed",
                    "sentDateTime": "2026-06-26T12:05:02Z",
                },
            )

        self.assertIsNone(reconciled)
        self.assertTrue(reason)
        self.assertEqual([], thread_ref.updates)

    def test_reconciliation_rejects_mixed_schema_config_type_drift(self):
        attempted_at = datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc)
        accepted_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "reconcile-owner",
            "lastSendError": "Graph send outcome pending reconciliation",
            "lastSendAttemptAt": attempted_at,
            "lastSendAttemptIndex": 0,
            "followUps": [{"message": "Plain follow-up body."}],
            "auditMetadata": attempted_at,
        }
        accepted_thread = {
            "clientId": "client-current",
            "email": ["current@example.com"],
            "contactName": "Ryan Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": accepted_config,
        }
        legacy_identity = followup._followup_send_identity(
            accepted_thread,
            accepted_config,
            0,
        )
        legacy_identity.pop("inputSignatures")
        legacy_identity.pop("contactNameExact")
        legacy_identity.pop("contactNamePresent")
        legacy_identity.pop("contactNameType")

        current_config = {
            **accepted_config,
            "auditMetadata": attempted_at.isoformat(),
        }
        marker = {
            "id": "attempt-legacy-config",
            "state": "uncertain",
            "owner": "reconcile-owner",
            "index": 0,
            "sendIdentity": legacy_identity,
        }
        current_thread = {
            **accepted_thread,
            "followUpConfig": current_config,
            "followUpSendAttempt": marker,
        }
        thread_ref = FakeThreadRef(current_thread)

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            reconciled, reason = followup._record_reconciled_followup_attempt(
                "uid-1",
                "thread-1",
                claim_owner="reconcile-owner",
                followup_index=0,
                expected_attempt=marker,
                expected_identity=legacy_identity,
                expected_retry=followup._followup_retry_signature(
                    current_thread,
                    current_config,
                ),
                sent_match={
                    "id": "sent-legacy-config",
                    "sentDateTime": "2026-06-26T12:05:02Z",
                },
            )

        self.assertIsNone(reconciled)
        self.assertTrue(reason)
        self.assertEqual([], thread_ref.updates)

    def test_reconciliation_rejects_legacy_identities_without_raw_input_proofs(self):
        attempted_at = datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc)
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "reconcile-owner",
            "lastSendError": "Graph send outcome pending reconciliation",
            "lastSendAttemptAt": attempted_at,
            "lastSendAttemptIndex": 0,
            "followUps": [{"message": "Plain follow-up body."}],
        }

        for contact_name in (
            "Ryan Broker",
            "",
            [0],
            {"nested": [False]},
        ):
            with self.subTest(contact_name=contact_name):
                thread_data = {
                    "clientId": "client-current",
                    "email": ["current@example.com"],
                    "contactName": contact_name,
                    "status": "active",
                    "followUpStatus": "waiting",
                    "hasInboundReply": False,
                    "followUpConfig": followup_config,
                }
                legacy_identity = followup._followup_send_identity(
                    thread_data,
                    followup_config,
                    0,
                )
                legacy_identity.pop("contactNameExact")
                legacy_identity.pop("contactNamePresent")
                legacy_identity.pop("contactNameType")
                legacy_identity.pop("inputSignatures")
                legacy_identity["contactName"] = (
                    legacy_identity.get("contactName") or ""
                )
                marker = {
                    "id": "attempt-legacy-stable",
                    "state": "uncertain",
                    "owner": "reconcile-owner",
                    "index": 0,
                    "sendIdentity": legacy_identity,
                }
                thread_data["followUpSendAttempt"] = marker
                thread_ref = FakeThreadRef(thread_data)

                with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    reconciled, reason = followup._record_reconciled_followup_attempt(
                        "uid-1",
                        "thread-1",
                        claim_owner="reconcile-owner",
                        followup_index=0,
                        expected_attempt=followup._canonical_followup_value(marker),
                        expected_identity=legacy_identity,
                        expected_retry=followup._followup_retry_signature(
                            thread_data,
                            followup_config,
                        ),
                        sent_match={
                            "id": "sent-legacy",
                            "sentDateTime": "2026-06-26T12:05:02Z",
                        },
                    )

                self.assertIsNone(reconciled)
                self.assertTrue(reason)
                self.assertEqual([], thread_ref.updates)

    def test_send_intent_allows_stable_missing_contact_for_plain_body(self):
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "followUps": [{"message": "Plain follow-up body."}],
        }

        for contact_name_present in (False, True):
            with self.subTest(contact_name_present=contact_name_present):
                thread_data = {
                    "clientId": "client-claimed",
                    "email": ["claimed@example.com"],
                    "status": "active",
                    "followUpStatus": "waiting",
                    "hasInboundReply": False,
                    "followUpConfig": followup_config,
                }
                if contact_name_present:
                    thread_data["contactName"] = None
                thread_ref = FakeThreadRef(thread_data)

                with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    marker, reason = followup._persist_followup_send_intent(
                        "uid-1",
                        "thread-1",
                        claim_owner="claim-owner",
                        followup_index=0,
                        expected_thread_data=thread_data,
                        expected_followup_config=followup_config,
                        recipient="claimed@example.com",
                        body="Plain follow-up body.",
                        subject="Claimed subject",
                        conversation_id="conv-claimed",
                        draft_id="draft-claimed",
                    )

                self.assertIsNotNone(marker)
                self.assertIsNone(reason)
                self.assertEqual(1, len(thread_ref.updates))

    def test_send_intent_rejects_unresolved_name_for_missing_or_malformed_contact(self):
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "followUps": [{"message": "Hi [NAME], following up."}],
        }

        for contact_name in (None, {}, [], 0, False, "   "):
            with self.subTest(contact_name=contact_name):
                thread_data = {
                    "clientId": "client-claimed",
                    "email": ["claimed@example.com"],
                    "contactName": contact_name,
                    "status": "active",
                    "followUpStatus": "waiting",
                    "hasInboundReply": False,
                    "followUpConfig": followup_config,
                }
                thread_ref = FakeThreadRef(thread_data)

                with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    marker, reason = followup._persist_followup_send_intent(
                        "uid-1",
                        "thread-1",
                        claim_owner="claim-owner",
                        followup_index=0,
                        expected_thread_data=thread_data,
                        expected_followup_config=followup_config,
                        recipient="claimed@example.com",
                        body="Hi [NAME], following up.",
                        subject="Claimed subject",
                        conversation_id="conv-claimed",
                        draft_id="draft-claimed",
                    )

                self.assertIsNone(marker)
                self.assertTrue(reason)
                self.assertEqual([], thread_ref.updates)

    def test_send_intent_cas_rejects_ambiguous_attempt_awaiting_review(self):
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "followUps": [{"message": "Claimed body"}],
        }
        thread_data = {
            "clientId": "client-claimed",
            "email": ["claimed@example.com"],
            "contactName": "Claimed Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpSendAttempt": {
                "id": "attempt-needs-review",
                "state": "needs_review",
                "resolution": "ambiguous",
                "owner": "prior-owner",
                "index": 0,
            },
            "followUpConfig": followup_config,
        }
        thread_ref = FakeThreadRef(thread_data)

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            marker, reason = followup._persist_followup_send_intent(
                "uid-1",
                "thread-1",
                claim_owner="claim-owner",
                followup_index=0,
                expected_thread_data=thread_data,
                expected_followup_config=followup_config,
                recipient="claimed@example.com",
                body="Claimed body",
                subject="Claimed subject",
                conversation_id="conv-claimed",
                draft_id="draft-claimed",
            )

        self.assertIsNone(marker)
        self.assertIn("needs_review", reason)
        self.assertEqual([], thread_ref.updates)

    def test_pending_send_intent_extends_claim_past_sixty_seconds(self):
        claim_started = datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc)
        thread_ref = FakeThreadRef({
            "clientId": "client-current",
            "email": ["current@example.com"],
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpSendAttempt": {
                "id": "attempt-crashed",
                "state": "sending",
                "owner": "crashed-owner",
                "index": 0,
                "recipient": "current@example.com",
                "body": "Current body",
                "subject": "Current subject",
                "conversationId": "conv-current",
                "sendStartedAt": claim_started,
                "leaseUntil": claim_started + followup.timedelta(minutes=10),
            },
            "followUpConfig": {
                "enabled": True,
                "nextFollowUpAt": claim_started - followup.timedelta(hours=1),
                "currentFollowUpIndex": 0,
                "processingBy": "crashed-owner",
                "processingAt": claim_started,
                "processingLeaseUntil": claim_started + followup.timedelta(minutes=10),
                "lastSendAttemptAt": claim_started,
                "lastSendAttemptIndex": 0,
                "followUps": [{"message": "Current body"}],
            },
        })

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch.object(
            followup,
            "datetime",
            _fixed_datetime(claim_started + followup.timedelta(seconds=61)),
        ), patch("google.cloud.firestore.transactional", lambda fn: fn):
            reclaimed = followup._claim_followup("uid-1", "thread-1", 0)

        self.assertIsNone(reclaimed)
        self.assertEqual([], thread_ref.updates)

    def test_graph_failure_release_keeps_attempt_lease_then_forces_reconciliation(self):
        started_at = datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc)
        followup_config = {
            "enabled": True,
            "nextFollowUpAt": started_at - followup.timedelta(hours=1),
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "processingAt": started_at,
            "followUps": [{"message": "Current body"}],
        }
        thread_state = {
            "clientId": "client-current",
            "email": ["current@example.com"],
            "contactName": "Current Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": followup_config,
        }
        fake_fs = FakeFollowupFirestore([], thread_data=thread_state)

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup, "datetime", _fixed_datetime(started_at)
        ), patch("google.cloud.firestore.transactional", lambda fn: fn):
            marker, error = followup._persist_followup_send_intent(
                "uid-1",
                "thread-1",
                claim_owner="claim-owner",
                followup_index=0,
                expected_thread_data=thread_state,
                expected_followup_config=followup_config,
                recipient="current@example.com",
                body="Current body",
                subject="Current subject",
                conversation_id="conv-current",
                draft_id="draft-current",
            )
            released = followup._release_followup_claim(
                "uid-1",
                "thread-1",
                reason="Follow-up Graph send returned HTTP 500",
                attempted_at=marker["sendStartedAt"],
                current_index=0,
                claim_owner="claim-owner",
                send_attempt_id=marker["id"],
                fail_closed=False,
            )

        self.assertIsNone(error)
        self.assertTrue(released)
        self.assertEqual("sending", thread_state["followUpSendAttempt"]["state"])
        self.assertIsNone(thread_state["followUpConfig"]["processingBy"])

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup,
            "datetime",
            _fixed_datetime(started_at + followup.timedelta(seconds=61)),
        ), patch("google.cloud.firestore.transactional", lambda fn: fn):
            early_claim = followup._claim_followup("uid-1", "thread-1", 0)

        self.assertIsNone(early_claim)

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup,
            "datetime",
            _fixed_datetime(started_at + followup.timedelta(minutes=11)),
        ), patch("google.cloud.firestore.transactional", lambda fn: fn):
            recovery_claim = followup._claim_followup("uid-1", "thread-1", 0)

        self.assertIsInstance(recovery_claim, followup.FollowupClaim)
        self.assertTrue(recovery_claim.reconciliation_required)
        self.assertEqual(marker["id"], recovery_claim.thread_data["followUpSendAttempt"]["id"])

    def test_durable_send_intent_without_sent_match_never_posts_again(self):
        attempted_at = datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc)
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
        })
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "reconcile-owner",
            "lastSendError": "send outcome unknown",
            "lastSendAttemptAt": attempted_at,
            "lastSendAttemptIndex": 0,
            "followUps": [{"message": "Current body"}],
        }
        thread_data = {
            "clientId": "client-current",
            "email": ["current@example.com"],
            "contactName": "Current Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": followup_config,
        }
        thread_data["followUpSendAttempt"] = _sealed_followup_attempt(
            thread_data,
            followup_config,
            attempt_id="attempt-crashed",
            owner="crashed-owner",
            send_started_at=attempted_at,
            subject="Current subject",
            conversation_id="conv-current",
        )
        fake_fs = FakeFollowupFirestore([outbound], thread_data=thread_data)

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup,
            "exponential_backoff_request",
            return_value=FakeResponse(200, {
                "value": [{
                    "id": "graph-root",
                    "subject": "Current subject",
                    "conversationId": "conv-current",
                }],
            }),
        ), patch.object(
            followup, "find_matching_sent_message_for_retry", return_value=None
        ) as sent_guard, patch.object(
            followup,
            "find_sent_conversation_continuation_for_retry",
            return_value=None,
        ), patch.object(requests, "post") as post, patch(
            "email_automation.processing.is_contact_opted_out", return_value=None
        ):
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
                claim_owner="reconcile-owner",
            )

        self.assertFalse(result)
        sent_guard.assert_called_once()
        post.assert_not_called()
        self.assertIn("manual review", followup._send_followup_email.last_error)
        self.assertTrue(followup._send_followup_email.guard_failed_closed)

    def test_durable_send_intent_with_sent_match_reconciles_without_posting(self):
        attempted_at = datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc)
        accepted_config = {
            "enabled": True,
            "nextFollowUpAt": attempted_at,
            "currentFollowUpIndex": 0,
            "processingBy": "reconcile-owner",
            "lastSendError": "Graph send outcome pending reconciliation",
            "lastSendAttemptAt": attempted_at,
            "lastSendAttemptIndex": 0,
            "followUps": [{"message": "Original body"}],
        }
        accepted_thread = {
            "clientId": "client-current",
            "email": ["original@example.com"],
            "contactName": "Original Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": accepted_config,
        }
        marker = _sealed_followup_attempt(
            accepted_thread,
            accepted_config,
            attempt_id="attempt-crashed",
            owner="crashed-owner",
            state="uncertain",
            reconciliation_owner="reconcile-owner",
            send_started_at=attempted_at,
            subject="Original subject",
            conversation_id="conv-original",
            to_recipients=[
                "original@example.com",
                "teammate@example.com",
            ],
            cc_recipients=["assistant@example.com"],
        )
        followup_config = {
            **accepted_config,
            "enabled": False,
            "nextFollowUpAt": None,
        }
        thread_data = {
            **accepted_thread,
            "status": "paused",
            "statusReason": "manual_continuation",
            "followUpStatus": "paused",
            "hasInboundReply": True,
            "followUpSendAttempt": marker,
            "followUpConfig": followup_config,
        }
        fake_fs = FakeFollowupFirestore([], thread_data=thread_data)
        sent_match = {
            "id": "sent-followup",
            "sentDateTime": "2026-06-26T12:05:02Z",
            "conversationId": "conv-original",
        }

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup,
            "find_matching_sent_message_for_retry",
            return_value=sent_match,
        ) as sent_guard, patch.object(
            followup, "_save_followup_message"
        ) as save_followup, patch.object(
            followup, "resolve_outbound_mode", return_value="paused"
        ) as outbound_mode, patch.object(requests, "post") as post, patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
                claim_owner="reconcile-owner",
            )

        self.assertTrue(result)
        sent_guard.assert_called_once_with(
            {"Authorization": "Bearer token"},
            recipient="original@example.com",
            body="Original body",
            subject="Original subject",
            conversation_id="conv-original",
            sent_after=attempted_at - followup.timedelta(seconds=30),
        )
        post.assert_not_called()
        outbound_mode.assert_not_called()
        self.campaign_decision.assert_not_called()
        save_followup.assert_called_once()
        self.assertEqual(
            "attempt-crashed",
            save_followup.call_args.kwargs["attempt_id"],
        )
        self.assertEqual(
            ["original@example.com", "teammate@example.com"],
            save_followup.call_args.kwargs["to_recipients"],
        )
        self.assertEqual(
            ["assistant@example.com"],
            save_followup.call_args.kwargs["cc_recipients"],
        )
        outcome = followup._get_followup_send_outcome()
        self.assertEqual("attempt-crashed", outcome.attempt_id)
        self.assertFalse(outcome.guard_failed_closed)

    def test_durable_sent_match_rejects_malformed_identity_without_audit_write(self):
        attempted_at = datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc)
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "reconcile-owner",
            "lastSendError": "Graph send outcome pending reconciliation",
            "lastSendAttemptAt": attempted_at,
            "lastSendAttemptIndex": 0,
            "followUps": [{"message": "Original body"}],
        }
        base_thread = {
            "clientId": "client-current",
            "email": ["original@example.com"],
            "contactName": "Original Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": followup_config,
        }
        valid_identity = followup._followup_send_identity(
            base_thread,
            followup_config,
            0,
        )
        missing = object()
        identity_cases = (
            ("missing", missing),
            ("none", None),
            ("string", "not-an-identity"),
            ("list", []),
            ("empty_dict", {}),
            (
                "partial_dict",
                {"inputSignatures": valid_identity["inputSignatures"]},
            ),
        )

        for name, malformed_identity in identity_cases:
            with self.subTest(name=name):
                marker = {
                    "id": f"attempt-malformed-{name}",
                    "state": "uncertain",
                    "owner": "crashed-owner",
                    "reconciliationOwner": "reconcile-owner",
                    "index": 0,
                    "recipient": "original@example.com",
                    "body": "Original body",
                    "subject": "Original subject",
                    "conversationId": "conv-original",
                    "sendStartedAt": attempted_at,
                }
                if malformed_identity is not missing:
                    marker["sendIdentity"] = malformed_identity
                thread_data = {
                    **base_thread,
                    "followUpSendAttempt": marker,
                }
                fake_fs = FakeFollowupFirestore([], thread_data=thread_data)

                with patch.object(followup, "_fs", fake_fs), patch.object(
                    followup,
                    "find_matching_sent_message_for_retry",
                    return_value={
                        "id": "sent-followup",
                        "sentDateTime": "2026-06-26T12:05:02Z",
                    },
                ), patch.object(
                    followup,
                    "_save_followup_message",
                ) as save_followup, patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    result = followup._send_followup_email(
                        "uid-1",
                        {"Authorization": "Bearer token"},
                        "thread-1",
                        thread_data,
                        followup_config,
                        0,
                        claim_owner="reconcile-owner",
                    )

                self.assertFalse(result)
                self.assertEqual([], fake_fs.updates)
                save_followup.assert_not_called()
                outcome = followup._get_followup_send_outcome()
                self.assertTrue(outcome.guard_failed_closed)
                self.assertIn("manual review", outcome.error)

    def test_sent_match_history_failure_keeps_attempt_recoverable(self):
        attempted_at = datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc)
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "reconcile-owner",
            "lastSendError": "Graph send outcome pending reconciliation",
            "lastSendAttemptAt": attempted_at,
            "lastSendAttemptIndex": 0,
            "followUps": [{"message": "Original body"}],
        }
        thread_data = {
            "clientId": "client-1",
            "email": ["original@example.com"],
            "contactName": "Original Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": followup_config,
        }
        marker = _sealed_followup_attempt(
            thread_data,
            followup_config,
            attempt_id="attempt-history-failure",
            owner="crashed-owner",
            state="uncertain",
            reconciliation_owner="reconcile-owner",
            send_started_at=attempted_at,
            subject="Original subject",
            conversation_id="conv-original",
        )
        thread_data["followUpSendAttempt"] = marker
        fake_fs = FakeFollowupFirestore([], thread_data=thread_data)

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup,
            "find_matching_sent_message_for_retry",
            return_value={
                "id": "sent-followup",
                "sentDateTime": "2026-06-26T12:05:02Z",
            },
        ), patch.object(
            followup, "_save_followup_message", return_value=False
        ), patch("google.cloud.firestore.transactional", lambda fn: fn):
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
                claim_owner="reconcile-owner",
            )

        self.assertFalse(result)
        self.assertEqual("uncertain", thread_data["followUpSendAttempt"]["state"])
        outcome = followup._get_followup_send_outcome()
        self.assertEqual("attempt-history-failure", outcome.attempt_id)
        self.assertIn("history persistence failed", outcome.error)
        self.assertFalse(outcome.guard_failed_closed)

    def test_sent_match_reconciliation_cas_rejects_newer_attempt(self):
        attempted_at = datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc)
        claimed_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "reconcile-owner",
            "followUps": [{"message": "Original body"}],
        }
        claimed_thread = {
            "clientId": "client-1",
            "email": ["original@example.com"],
            "contactName": "Original Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": claimed_config,
        }
        claimed_marker = _sealed_followup_attempt(
            claimed_thread,
            claimed_config,
            attempt_id="attempt-original",
            owner="original-owner",
            state="uncertain",
            reconciliation_owner="reconcile-owner",
            send_started_at=attempted_at,
            subject="Original subject",
            conversation_id="conv-original",
        )
        claimed_thread["followUpSendAttempt"] = claimed_marker
        current_config = dict(claimed_config)
        current_thread = {
            **claimed_thread,
            "followUpConfig": current_config,
            "followUpSendAttempt": dict(claimed_marker),
        }
        fake_fs = FakeFollowupFirestore([], thread_data=current_thread)

        def replace_attempt_before_match(*_args, **_kwargs):
            current_config["processingBy"] = "new-owner"
            current_thread["followUpSendAttempt"] = {
                **claimed_marker,
                "id": "attempt-new",
                "owner": "new-owner",
                "reconciliationOwner": "new-owner",
            }
            return {
                "id": "sent-followup",
                "sentDateTime": "2026-06-26T12:05:02Z",
            }

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup,
            "find_matching_sent_message_for_retry",
            side_effect=replace_attempt_before_match,
        ), patch.object(
            followup, "_save_followup_message"
        ) as save_followup, patch.object(requests, "post") as post, patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                claimed_thread,
                claimed_config,
                0,
                claim_owner="reconcile-owner",
            )

        self.assertFalse(result)
        save_followup.assert_not_called()
        post.assert_not_called()
        self.assertEqual([], fake_fs.updates)
        self.assertIn("reconciliation state changed", followup._send_followup_email.last_error)
        self.assertTrue(followup._send_followup_email.guard_failed_closed)

    def test_followup_history_id_is_deterministic_for_attempt(self):
        with patch.object(followup, "save_message", return_value=True) as save_message:
            for _ in range(2):
                saved = followup._save_followup_message(
                    "uid-1",
                    "thread-1",
                    "broker@example.com",
                    "Subject",
                    "Body",
                    attempt_id="attempt-stable",
                )
                self.assertTrue(saved)

        message_ids = [call.args[2] for call in save_message.call_args_list]
        self.assertEqual(message_ids[0], message_ids[1])
        self.assertIn("attempt-stable", message_ids[0])

    def test_followup_does_not_readd_primary_recipient_removed_by_reply_all_filter(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
            "to": ["bp21harrison@gmail.com"],
        })
        fake_fs = FakeFollowupFirestore([outbound])
        followup_config = {
            "currentFollowUpIndex": 0,
            "followUps": [{"message": "Hi Riley,\n\nJust following up."}],
        }
        thread_data = {
            "email": ["bp21harrison@gmail.com"],
            "contactName": "Riley Broker",
        }
        deleted_urls = []
        send_urls = []

        def run_request(callback, *args, **kwargs):
            return callback()

        def fake_get(url, **kwargs):
            return FakeResponse(200, {
                "value": [{
                    "id": "graph-root",
                    "subject": "0 Gemini Ave",
                    "conversationId": "conv-1",
                }]
            })

        def fake_post(url, **kwargs):
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {"id": "reply-draft-1", "toRecipients": [], "ccRecipients": []})
            if url.endswith("/send"):
                send_urls.append(url)
                return FakeResponse(202, {})
            raise AssertionError(f"Unexpected post: {url}")

        def fake_delete(url, **kwargs):
            deleted_urls.append(url)
            return FakeResponse(204, {})

        with patch.object(followup, "_fs", fake_fs), \
             patch.object(followup, "exponential_backoff_request", side_effect=run_request), \
             patch.object(requests, "get", side_effect=fake_get), \
             patch.object(requests, "post", side_effect=fake_post), \
             patch.object(requests, "delete", side_effect=fake_delete), \
             patch("email_automation.email._filter_reply_all_draft_recipients", return_value={
                 "payload": {"toRecipients": [], "ccRecipients": []},
                 "sentRecipients": [],
             }):
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
            )

        self.assertFalse(result)
        self.assertFalse(send_urls)
        self.assertTrue(any(url.endswith("/reply-draft-1") for url in deleted_urls))
        self.assertIn("did not pass reply-all safety filtering", followup._send_followup_email.last_error)
        self.assertTrue(followup._send_followup_email.guard_failed_closed)

    def test_followup_rechecks_terminal_state_before_reply_all_draft(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
        })
        fake_fs = FakeFollowupFirestore(
            [outbound],
            thread_data={
                "status": "completed",
                "followUpStatus": "waiting",
                "followUpConfig": {"enabled": True, "currentFollowUpIndex": 0},
            },
        )
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "followUps": [{"message": "Hi Riley,\n\nJust following up."}],
        }
        thread_data = {
            "email": ["bp21harrison@gmail.com"],
            "contactName": "Riley Broker",
        }

        with patch.object(followup, "_fs", fake_fs), \
             patch.object(followup, "exponential_backoff_request", return_value=FakeResponse(200, {
                 "value": [{"id": "graph-root", "subject": "0 Gemini Ave", "conversationId": "conv-1"}]
             })), \
             patch.object(requests, "post") as post:
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
            )

        self.assertFalse(result)
        post.assert_not_called()
        self.assertIn("completed", followup._send_followup_email.last_error)
        self.assertTrue(followup._send_followup_email.guard_failed_closed)

    def test_followup_rechecks_campaign_stop_immediately_before_graph_send(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
        })
        fake_fs = FakeFollowupFirestore(
            [outbound],
            thread_data={
                "clientId": "client-1",
                "status": "active",
                "followUpStatus": "waiting",
                "followUpConfig": {"enabled": True, "currentFollowUpIndex": 0},
            },
        )
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "followUps": [{"message": "Hi Riley,\n\nJust following up."}],
        }
        thread_data = {
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "contactName": "Riley Broker",
        }
        posts = []

        def run_request(func, **_kwargs):
            return func()

        def fake_get(url, **_kwargs):
            if "/me/messages?" in url:
                return FakeResponse(200, {"value": []})
            return FakeResponse(200, {
                "value": [{"id": "graph-root", "subject": "0 Gemini Ave", "conversationId": "conv-1"}]
            })

        def fake_post(url, **_kwargs):
            posts.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {
                    "id": "reply-draft-stop",
                    "toRecipients": [{"emailAddress": {"address": "bp21harrison@gmail.com"}}],
                    "ccRecipients": [],
                })
            if url.endswith("/send"):
                raise AssertionError("stopped campaign must not reach Graph /send")
            return FakeResponse(201)

        self.campaign_decision.side_effect = [
            CampaignAutomationDecision(
                state="allow", reason="", client_data={
                    "status": "live",
                    "columnConfig": get_default_column_config(),
                },
                metadata={"terminal": False, "stopKind": "none"},
            ),
            CampaignAutomationDecision(
                state="blocked", reason="client_stopped_by_user",
                client_data={"status": "stopping"},
                metadata={"terminal": True, "stopKind": "terminal_stop"},
            ),
        ]

        deleted_urls = []
        with patch.object(followup, "_fs", fake_fs), \
             patch.object(followup, "exponential_backoff_request", side_effect=run_request), \
             patch.object(requests, "get", side_effect=fake_get), \
             patch.object(requests, "post", side_effect=fake_post), \
             patch.object(requests, "patch", return_value=FakeResponse(200)), \
             patch("email_automation.processing.is_contact_opted_out", return_value=None), \
             patch("requests.delete", side_effect=_record_delete(deleted_urls)):
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
            )

        self.assertFalse(result)
        self.assertFalse(any(url.endswith("/send") for url in posts))
        self.assertEqual(len(deleted_urls), 1, f"draft not deleted: {deleted_urls}")
        self.assertEqual("terminal", followup._send_followup_email.campaign_suppression_kind)
        self.assertIn("client_stopped_by_user", followup._send_followup_email.last_error)

    def test_followup_rechecks_thread_state_immediately_before_graph_send(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
        })
        snapshots = iter([
            {
                "clientId": "client-1",
                "status": "active",
                "followUpStatus": "waiting",
                "hasInboundReply": False,
                "followUpConfig": {"enabled": True, "currentFollowUpIndex": 0},
            },
            {
                "clientId": "client-1",
                "status": "stopped",
                "statusReason": "requirements_mismatch",
                "followUpStatus": "stopped",
                "pendingTerminalReason": "requirements_mismatch",
                "hasInboundReply": True,
                "followUpConfig": {"enabled": False, "currentFollowUpIndex": 0},
            },
        ])
        fake_fs = FakeFollowupFirestore([outbound], thread_data=lambda: next(snapshots))
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "followUps": [{"message": "Hi Riley,\n\nJust following up."}],
        }
        thread_data = {
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "contactName": "Riley Broker",
        }
        posts = []

        def run_request(func, **_kwargs):
            return func()

        def fake_get(url, **_kwargs):
            if "/me/messages?" in url:
                return FakeResponse(200, {"value": []})
            return FakeResponse(200, {
                "value": [{"id": "graph-root", "subject": "0 Gemini Ave", "conversationId": "conv-1"}]
            })

        def fake_post(url, **_kwargs):
            posts.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {
                    "id": "reply-draft-terminal",
                    "toRecipients": [{"emailAddress": {"address": "bp21harrison@gmail.com"}}],
                    "ccRecipients": [],
                })
            if url.endswith("/send"):
                return FakeResponse(202, {})
            return FakeResponse(201, {})

        deleted_urls = []
        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup, "exponential_backoff_request", side_effect=run_request
        ), patch.object(requests, "get", side_effect=fake_get), patch.object(
            requests, "post", side_effect=fake_post
        ), patch.object(requests, "patch", return_value=FakeResponse(200)), patch(
            "email_automation.processing.is_contact_opted_out", return_value=None
        ), patch(
            "email_automation.email._hydrate_reply_all_draft_recipients",
            side_effect=lambda _headers, draft, **_kwargs: draft,
        ), patch(
            "email_automation.email._source_message_reply_all_fallback",
            side_effect=lambda draft, _source: draft,
        ), patch(
            "email_automation.email._reviewed_recipient_reply_all_fallback",
            side_effect=lambda draft, **_kwargs: draft,
        ), patch(
            "email_automation.email._filter_reply_all_draft_recipients",
            return_value={
                "payload": {
                    "toRecipients": [{"emailAddress": {"address": "bp21harrison@gmail.com"}}],
                    "ccRecipients": [],
                },
                "sentRecipients": ["bp21harrison@gmail.com"],
            },
        ), patch.object(
            followup, "_save_followup_message", return_value=True
        ), patch("requests.delete", side_effect=_record_delete(deleted_urls)):
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
            )

        self.assertFalse(result)
        self.assertFalse(any(url.endswith("/send") for url in posts))
        self.assertEqual(len(deleted_urls), 1, f"draft not deleted: {deleted_urls}")
        self.assertIn("requirements_mismatch", followup._send_followup_email.last_error)
        self.assertTrue(followup._send_followup_email.guard_failed_closed)

    def test_followup_rechecks_action_needed_state_before_reply_all_draft(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
        })
        fake_fs = FakeFollowupFirestore(
            [outbound],
            thread_data={
                "status": "action_needed",
                "followUpStatus": "waiting",
                "followUpConfig": {"enabled": True, "currentFollowUpIndex": 0},
            },
        )
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "followUps": [{"message": "Hi Riley,\n\nJust following up."}],
        }
        thread_data = {
            "email": ["bp21harrison@gmail.com"],
            "contactName": "Riley Broker",
        }

        with patch.object(followup, "_fs", fake_fs), \
             patch.object(followup, "exponential_backoff_request", return_value=FakeResponse(200, {
                 "value": [{"id": "graph-root", "subject": "0 Gemini Ave", "conversationId": "conv-1"}]
             })), \
             patch.object(requests, "post") as post:
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
            )

        self.assertFalse(result)
        post.assert_not_called()
        self.assertIn("action_needed", followup._send_followup_email.last_error)
        self.assertTrue(followup._send_followup_email.guard_failed_closed)

    def test_followup_reply_all_filter_failure_deletes_draft_and_fails_closed(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
            "to": ["bp21harrison@gmail.com"],
        })
        fake_fs = FakeFollowupFirestore([outbound])
        followup_config = {
            "currentFollowUpIndex": 0,
            "followUps": [{"message": "Hi Riley,\n\nJust following up."}],
        }
        thread_data = {
            "email": ["bp21harrison@gmail.com"],
            "contactName": "Riley Broker",
        }
        deleted_urls = []

        def run_request(callback, *args, **kwargs):
            return callback()

        def fake_get(url, **kwargs):
            return FakeResponse(200, {
                "value": [{
                    "id": "graph-root",
                    "subject": "0 Gemini Ave",
                    "conversationId": "conv-1",
                }]
            })

        def fake_post(url, **kwargs):
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {"id": "reply-draft-1", "toRecipients": [], "ccRecipients": []})
            raise AssertionError(f"Unexpected post after filtering failure: {url}")

        def fake_delete(url, **kwargs):
            deleted_urls.append(url)
            return FakeResponse(204, {})

        with patch.object(followup, "_fs", fake_fs), \
             patch.object(followup, "exponential_backoff_request", side_effect=run_request), \
             patch.object(requests, "get", side_effect=fake_get), \
             patch.object(requests, "post", side_effect=fake_post), \
             patch.object(requests, "delete", side_effect=fake_delete), \
             patch("email_automation.processing.is_contact_opted_out", side_effect=[None, RuntimeError("opt-out service down")]):
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
            )

        self.assertFalse(result)
        self.assertTrue(any(url.endswith("/reply-draft-1") for url in deleted_urls))
        self.assertIn("Could not filter reply-all recipients", followup._send_followup_email.last_error)
        self.assertTrue(followup._send_followup_email.guard_failed_closed)

    def test_followup_signature_attachment_failure_deletes_draft_and_fails_closed(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
            "to": ["bp21harrison@gmail.com"],
        })
        fake_fs = FakeFollowupFirestore([outbound])
        followup_config = {
            "currentFollowUpIndex": 0,
            "followUps": [{"message": "Hi Riley,\n\nJust following up."}],
        }
        thread_data = {
            "email": ["bp21harrison@gmail.com"],
            "contactName": "Riley Broker",
        }
        deleted_urls = []

        def run_request(callback, *args, **kwargs):
            return callback()

        def fake_get(url, **kwargs):
            return FakeResponse(200, {
                "value": [{
                    "id": "graph-root",
                    "subject": "0 Gemini Ave",
                    "conversationId": "conv-1",
                }]
            })

        def fake_post(url, **kwargs):
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {"id": "reply-draft-1", "toRecipients": [], "ccRecipients": []})
            if url.endswith("/attachments"):
                return FakeResponse(500, {})
            raise AssertionError(f"Unexpected send after attachment failure: {url}")

        def fake_patch(url, **kwargs):
            return FakeResponse(200, {})

        def fake_delete(url, **kwargs):
            deleted_urls.append(url)
            return FakeResponse(204, {})

        with patch.object(followup, "_fs", fake_fs), \
             patch.object(followup, "exponential_backoff_request", side_effect=run_request), \
             patch.object(followup, "needs_signature_attachments", return_value=True), \
             patch.object(followup, "get_signature_attachments", return_value=[{"name": "logo.png"}]), \
             patch.object(requests, "get", side_effect=fake_get), \
             patch.object(requests, "post", side_effect=fake_post), \
             patch.object(requests, "patch", side_effect=fake_patch), \
             patch.object(requests, "delete", side_effect=fake_delete), \
             patch("email_automation.processing.is_contact_opted_out", return_value=None):
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
            )

        self.assertFalse(result)
        self.assertTrue(any(url.endswith("/reply-draft-1") for url in deleted_urls))
        self.assertIn("Could not attach required signature asset", followup._send_followup_email.last_error)
        self.assertTrue(followup._send_followup_email.guard_failed_closed)

    def test_legacy_retry_invalid_timestamp_blocks_before_guards_or_intent(self):
        missing = object()
        validation_now = datetime.now(timezone.utc)
        cases = (
            ("missing", missing),
            ("garbage", "not-a-timestamp"),
            ("bool", False),
            (
                "future_1_second",
                validation_now + followup.timedelta(seconds=1),
            ),
            (
                "future_60_seconds",
                validation_now + followup.timedelta(seconds=60),
            ),
            (
                "future_299_seconds",
                validation_now + followup.timedelta(seconds=299),
            ),
            (
                "future_1_day",
                validation_now + followup.timedelta(days=1),
            ),
        )

        for name, attempted_at in cases:
            with self.subTest(name=name):
                outbound = FakeMessageDoc({
                    "direction": "outbound",
                    "headers": {"internetMessageId": "<root@example.com>"},
                    "sentDateTime": "2026-06-26T12:00:00Z",
                })
                followup_config = {
                    "enabled": True,
                    "currentFollowUpIndex": 0,
                    "processingBy": "claim-owner",
                    "lastSendError": "Read timed out after Graph accepted send",
                    "lastSendAttemptIndex": 0,
                    "followUps": [{"message": "Retry body"}],
                }
                if attempted_at is not missing:
                    followup_config["lastSendAttemptAt"] = attempted_at
                thread_data = {
                    "clientId": "client-1",
                    "email": ["broker@example.com"],
                    "contactName": "Ryan Broker",
                    "status": "active",
                    "followUpStatus": "waiting",
                    "hasInboundReply": False,
                    "followUpConfig": followup_config,
                }
                fake_fs = FakeFollowupFirestore([outbound], thread_data=thread_data)

                def run_request(callback, *_args, **_kwargs):
                    return callback()

                def fake_get(_url, **_kwargs):
                    return FakeResponse(200, {
                        "value": [{
                            "id": "graph-root",
                            "subject": "Retry subject",
                            "conversationId": "conv-retry",
                        }],
                    })

                def fake_post(url, **_kwargs):
                    if url.endswith("/createReplyAll"):
                        return FakeResponse(201, {
                            "id": "reply-draft-retry",
                            "toRecipients": [{
                                "emailAddress": {"address": "broker@example.com"},
                            }],
                            "ccRecipients": [],
                        })
                    raise AssertionError(f"Unexpected POST: {url}")

                with patch.object(followup, "_fs", fake_fs), patch.object(
                    followup,
                    "datetime",
                    _fixed_datetime(validation_now),
                ), patch.object(
                    followup,
                    "exponential_backoff_request",
                    side_effect=run_request,
                ), patch.object(
                    requests,
                    "get",
                    side_effect=fake_get,
                ), patch.object(
                    requests,
                    "post",
                    side_effect=fake_post,
                ) as post, patch.object(
                    requests,
                    "patch",
                    return_value=FakeResponse(200),
                ), patch.object(
                    followup,
                    "find_matching_sent_message_for_retry",
                    return_value=None,
                ) as sent_guard, patch.object(
                    followup,
                    "find_sent_conversation_continuation_for_retry",
                    return_value=None,
                ) as continuation_guard, patch.object(
                    followup,
                    "_persist_followup_send_intent",
                    return_value=(None, "test stop"),
                ) as persist_intent, patch.object(
                    # The lane deletes an abandoned draft through the transport
                    # now, so the verb has to be patched - mocking the old helper
                    # name let the real DELETE reach a live mailbox.
                    requests,
                    "delete",
                    return_value=FakeResponse(204),
                ):
                    result = followup._send_followup_email(
                        "uid-1",
                        {"Authorization": "Bearer token"},
                        "thread-1",
                        thread_data,
                        followup_config,
                        0,
                        claim_owner="claim-owner",
                    )

                self.assertFalse(result)
                sent_guard.assert_not_called()
                continuation_guard.assert_not_called()
                persist_intent.assert_not_called()
                post.assert_not_called()
                outcome = followup._get_followup_send_outcome()
                self.assertTrue(outcome.guard_failed_closed)
                self.assertIn("timestamp", outcome.error)

    def test_legacy_retry_valid_timestamps_reach_guards_with_captured_signature(self):
        validation_now = datetime.now(timezone.utc)
        firestore_timestamp = DatetimeWithNanoseconds(
            2026,
            6,
            26,
            12,
            5,
            tzinfo=timezone.utc,
        )
        cases = (
            ("exact_now", validation_now),
            ("datetime", datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc)),
            ("iso_z", "2026-06-26T12:05:00Z"),
            ("iso_offset", "2026-06-26T05:05:00-07:00"),
            ("firestore", firestore_timestamp),
        )

        for name, attempted_at in cases:
            with self.subTest(name=name):
                outbound = FakeMessageDoc({
                    "direction": "outbound",
                    "headers": {"internetMessageId": "<root@example.com>"},
                    "sentDateTime": "2026-06-26T12:00:00Z",
                })
                followup_config = {
                    "enabled": True,
                    "currentFollowUpIndex": 0,
                    "processingBy": "claim-owner",
                    "lastSendError": "Read timed out after Graph accepted send",
                    "lastSendAttemptAt": attempted_at,
                    "lastSendAttemptIndex": 0,
                    "followUps": [{"message": "Retry body"}],
                }
                thread_data = {
                    "clientId": "client-1",
                    "email": ["broker@example.com"],
                    "contactName": "Ryan Broker",
                    "status": "active",
                    "followUpStatus": "waiting",
                    "hasInboundReply": False,
                    "followUpConfig": followup_config,
                }
                fake_fs = FakeFollowupFirestore([outbound], thread_data=thread_data)

                def run_request(callback, *_args, **_kwargs):
                    return callback()

                def fake_get(_url, **_kwargs):
                    return FakeResponse(200, {
                        "value": [{
                            "id": "graph-root",
                            "subject": "Retry subject",
                            "conversationId": "conv-retry",
                        }],
                    })

                def fake_post(url, **_kwargs):
                    if url.endswith("/createReplyAll"):
                        return FakeResponse(201, {
                            "id": "reply-draft-retry",
                            "toRecipients": [{
                                "emailAddress": {"address": "broker@example.com"},
                            }],
                            "ccRecipients": [],
                        })
                    raise AssertionError(f"Unexpected POST: {url}")

                with patch.object(followup, "_fs", fake_fs), patch.object(
                    followup,
                    "datetime",
                    _fixed_datetime(validation_now),
                ), patch.object(
                    followup,
                    "exponential_backoff_request",
                    side_effect=run_request,
                ), patch.object(
                    requests,
                    "get",
                    side_effect=fake_get,
                ), patch.object(
                    requests,
                    "post",
                    side_effect=fake_post,
                ), patch.object(
                    requests,
                    "patch",
                    return_value=FakeResponse(200),
                ), patch.object(
                    followup,
                    "find_matching_sent_message_for_retry",
                    return_value=None,
                ) as sent_guard, patch.object(
                    followup,
                    "find_sent_conversation_continuation_for_retry",
                    return_value=None,
                ) as continuation_guard, patch.object(
                    followup,
                    "_persist_followup_send_intent",
                    return_value=(None, "test stop"),
                ) as persist_intent, patch.object(
                    # The lane deletes an abandoned draft through the transport
                    # now, so the verb has to be patched - mocking the old helper
                    # name let the real DELETE reach a live mailbox.
                    requests,
                    "delete",
                    return_value=FakeResponse(204),
                ):
                    result = followup._send_followup_email(
                        "uid-1",
                        {"Authorization": "Bearer token"},
                        "thread-1",
                        thread_data,
                        followup_config,
                        0,
                        claim_owner="claim-owner",
                    )

                self.assertFalse(result)
                expected_sent_after = (
                    followup._followup_utc_timestamp(attempted_at)
                    - followup.timedelta(seconds=30)
                )
                self.assertEqual(
                    expected_sent_after,
                    sent_guard.call_args.kwargs["sent_after"],
                )
                self.assertEqual(
                    expected_sent_after,
                    continuation_guard.call_args.kwargs["sent_after"],
                )
                self.assertEqual(
                    followup._followup_field_signature(
                        followup_config,
                        "lastSendAttemptAt",
                    ),
                    persist_intent.call_args.kwargs[
                        "expected_retry_timestamp_signature"
                    ],
                )

    def test_legacy_retry_timestamp_change_after_guard_blocks_new_send_intent(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
        })
        original_attempted_at = "2026-06-26T12:05:00Z"
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "lastSendError": "Read timed out after Graph accepted send",
            "lastSendAttemptAt": original_attempted_at,
            "lastSendAttemptIndex": 0,
            "followUps": [{"message": "Retry body"}],
        }
        thread_data = {
            "clientId": "client-1",
            "email": ["broker@example.com"],
            "contactName": "Ryan Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": followup_config,
        }
        fake_fs = FakeFollowupFirestore([outbound], thread_data=thread_data)
        post_urls = []

        def run_request(callback, *_args, **_kwargs):
            return callback()

        def fake_get(_url, **_kwargs):
            return FakeResponse(200, {
                "value": [{
                    "id": "graph-root",
                    "subject": "Retry subject",
                    "conversationId": "conv-retry",
                }],
            })

        def change_timestamp_after_lookup(*_args, **_kwargs):
            followup_config["lastSendAttemptAt"] = "2026-06-26T12:06:00Z"
            return None

        def fake_post(url, **_kwargs):
            post_urls.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {
                    "id": "reply-draft-retry-race",
                    "toRecipients": [{
                        "emailAddress": {"address": "broker@example.com"},
                    }],
                    "ccRecipients": [],
                })
            if url.endswith("/send"):
                return FakeResponse(202, {})
            raise AssertionError(f"Unexpected POST: {url}")

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup,
            "exponential_backoff_request",
            side_effect=run_request,
        ), patch.object(
            requests,
            "get",
            side_effect=fake_get,
        ), patch.object(
            requests,
            "post",
            side_effect=fake_post,
        ), patch.object(
            requests,
            "patch",
            return_value=FakeResponse(200),
        ), patch.object(
            followup,
            "find_matching_sent_message_for_retry",
            side_effect=change_timestamp_after_lookup,
        ), patch.object(
            followup,
            "find_sent_conversation_continuation_for_retry",
            return_value=None,
        ) as continuation_guard, patch.object(
            followup,
            "_save_followup_message",
            return_value=True,
        ), patch(
            "google.cloud.firestore.transactional",
            lambda fn: fn,
        ), patch.object(
            # Patch the verb, not the old helper name: the lane deletes through
            # the transport now, and a name-level mock stopped intercepting it.
            requests,
            "delete",
            return_value=FakeResponse(204),
        ):
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
                claim_owner="claim-owner",
            )

        self.assertFalse(result)
        self.assertFalse(any(url.endswith("/send") for url in post_urls))
        self.assertNotIn("followUpSendAttempt", thread_data)
        self.assertEqual(
            datetime(2026, 6, 26, 12, 4, 30, tzinfo=timezone.utc),
            continuation_guard.call_args.kwargs["sent_after"],
        )
        self.assertIn("timestamp", followup._send_followup_email.last_error)
        self.assertTrue(followup._send_followup_email.guard_failed_closed)

    def test_failed_followup_retry_uses_sent_items_match_without_resending(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
        })
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "lastSendError": "Read timed out after Graph accepted send",
            "lastSendAttemptAt": "2026-06-26T12:05:00Z",
            "lastSendAttemptIndex": 0,
            "followUps": [{"message": "Hi [NAME],\n\nJust following up."}],
        }
        thread_data = {
            "clientId": "client-1",
            "email": ["bp21harrison@gmail.com"],
            "contactName": "Ryan Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": followup_config,
        }
        fake_fs = FakeFollowupFirestore([outbound], thread_data=thread_data)
        expected_retry_timestamp_signature = followup._followup_field_signature(
            followup_config,
            "lastSendAttemptAt",
        )

        def assert_durable_attempt_before_history(*_args, **kwargs):
            marker = thread_data.get("followUpSendAttempt")
            self.assertIsInstance(marker, dict)
            self.assertEqual("uncertain", marker["state"])
            self.assertEqual(marker["id"], kwargs["attempt_id"])
            return True

        with patch.object(followup, "_fs", fake_fs), \
             patch.object(followup, "exponential_backoff_request", return_value=FakeResponse(200, {
                 "value": [{"id": "graph-root", "subject": "0 Gemini Ave", "conversationId": "conv-1"}]
             })), \
             patch.object(followup, "find_matching_sent_message_for_retry", return_value={
                 "id": "sent-followup-1",
                 "internetMessageId": "<sent-followup-1@example.com>",
                 "conversationId": "conv-1",
                 "sentDateTime": "2026-06-26T12:05:02Z",
             }) as sent_guard, \
             patch.object(
                 followup,
                 "_save_followup_message",
                 side_effect=assert_durable_attempt_before_history,
             ) as save_followup, \
             patch.object(
                 followup,
                 "_migrate_legacy_sent_match",
                 wraps=followup._migrate_legacy_sent_match,
             ) as migrate_legacy, \
             patch.object(requests, "post") as post, \
             patch("google.cloud.firestore.transactional", lambda fn: fn):
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
                claim_owner="claim-owner",
            )

        self.assertTrue(result)
        sent_guard.assert_called_once()
        self.assertEqual(
            expected_retry_timestamp_signature,
            migrate_legacy.call_args.kwargs[
                "expected_retry_timestamp_signature"
            ],
        )
        post.assert_not_called()
        save_followup.assert_called_once()
        self.assertIsNone(fake_fs.updates[-1]["followUpConfig.lastSendError"])
        self.assertEqual(
            thread_data["followUpSendAttempt"]["id"],
            followup._get_followup_send_outcome().attempt_id,
        )

    def test_legacy_sent_match_reconciliation_cas_rejects_new_owner(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
        })
        claimed_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "lastSendError": "Read timed out after Graph accepted send",
            "lastSendAttemptAt": "2026-06-26T12:05:00Z",
            "lastSendAttemptIndex": 0,
            "followUps": [{"message": "Hi Ryan, just following up."}],
        }
        claimed_thread = {
            "clientId": "client-1",
            "email": ["broker@example.com"],
            "contactName": "Ryan Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": claimed_config,
        }
        current_config = dict(claimed_config)
        current_thread = {**claimed_thread, "followUpConfig": current_config}
        fake_fs = FakeFollowupFirestore([outbound], thread_data=current_thread)

        def replace_owner_before_match(*_args, **_kwargs):
            current_config["processingBy"] = "new-owner"
            return {
                "id": "sent-followup-1",
                "sentDateTime": "2026-06-26T12:05:02Z",
                "conversationId": "conv-1",
            }

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup,
            "exponential_backoff_request",
            return_value=FakeResponse(200, {
                "value": [{
                    "id": "graph-root",
                    "subject": "0 Gemini Ave",
                    "conversationId": "conv-1",
                }],
            }),
        ), patch.object(
            followup,
            "find_matching_sent_message_for_retry",
            side_effect=replace_owner_before_match,
        ), patch.object(
            followup, "_save_followup_message"
        ) as save_followup, patch.object(requests, "post") as post, patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                claimed_thread,
                claimed_config,
                0,
                claim_owner="claim-owner",
            )

        self.assertFalse(result)
        save_followup.assert_not_called()
        post.assert_not_called()
        self.assertEqual([], fake_fs.updates)
        self.assertIn(
            "legacy reconciliation state changed",
            followup._send_followup_email.last_error,
        )
        self.assertTrue(followup._send_followup_email.guard_failed_closed)
        outcome = followup._get_followup_send_outcome()
        self.assertTrue(outcome.attempt_id.startswith("followup-legacy-"))
        self.assertEqual(outcome.attempt_id, outcome.attempt_marker["id"])
        self.assertTrue(outcome.attempt_expected_absent)

    def test_legacy_sent_match_terminal_drift_stays_blocked_after_resume(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
        })
        claimed_config = {
            "enabled": True,
            "nextFollowUpAt": datetime.now(timezone.utc) - followup.timedelta(hours=1),
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "lastSendError": "Read timed out after Graph accepted send",
            "lastSendAttemptAt": "2026-06-26T12:05:00Z",
            "lastSendAttemptIndex": 0,
            "followUps": [{"message": "Original follow-up"}],
        }
        claimed_thread = {
            "clientId": "client-1",
            "email": ["broker@example.com"],
            "contactName": "Ryan Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": claimed_config,
        }
        current_config = {
            **claimed_config,
            "followUps": [{"message": "Original follow-up"}],
        }
        current_thread = {
            **claimed_thread,
            "followUpConfig": current_config,
        }
        fake_fs = FakeFollowupFirestore([outbound], thread_data=current_thread)

        def change_body_before_match(*_args, **_kwargs):
            current_config["followUps"] = [{"message": "Changed follow-up"}]
            current_config["enabled"] = False
            current_config["nextFollowUpAt"] = None
            current_thread["status"] = "paused"
            current_thread["statusReason"] = "manual_continuation"
            current_thread["followUpStatus"] = "paused"
            current_thread["hasInboundReply"] = True
            return {
                "id": "sent-followup-1",
                "sentDateTime": "2026-06-26T12:05:02Z",
                "conversationId": "conv-1",
            }

        with patch.object(followup, "_fs", fake_fs), patch.object(
            followup,
            "exponential_backoff_request",
            return_value=FakeResponse(200, {
                "value": [{
                    "id": "graph-root",
                    "subject": "0 Gemini Ave",
                    "conversationId": "conv-1",
                }],
            }),
        ), patch.object(
            followup,
            "find_matching_sent_message_for_retry",
            side_effect=change_body_before_match,
        ), patch.object(
            followup, "_save_followup_message"
        ) as save_followup, patch.object(requests, "post") as post, patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                claimed_thread,
                claimed_config,
                0,
                claim_owner="claim-owner",
            )
            outcome = followup._get_followup_send_outcome()
            released = followup._release_followup_claim(
                "uid-1",
                "thread-1",
                reason=outcome.error,
                attempted_at=outcome.attempt_at,
                current_index=0,
                claim_owner="claim-owner",
                send_attempt_id=outcome.attempt_id,
                send_attempt_marker=outcome.attempt_marker,
                expected_no_send_attempt=outcome.attempt_expected_absent,
                fail_closed=outcome.guard_failed_closed,
            )
            preserved_business_state = (
                current_thread["status"],
                current_thread["statusReason"],
                current_thread["followUpStatus"],
                current_thread["hasInboundReply"],
            )
            current_thread.update({
                "status": "active",
                "statusReason": None,
                "followUpStatus": "waiting",
                "hasInboundReply": False,
            })
            current_config.update({
                "enabled": True,
                "nextFollowUpAt": datetime.now(timezone.utc) - followup.timedelta(hours=1),
                "processingBy": None,
                "processingAt": None,
            })
            retry_claim = followup._claim_followup("uid-1", "thread-1", 0)

        self.assertFalse(result)
        self.assertTrue(released)
        self.assertIsNone(retry_claim)
        save_followup.assert_not_called()
        post.assert_not_called()
        self.assertEqual(
            ("paused", "manual_continuation", "paused", True),
            preserved_business_state,
        )
        self.assertEqual(
            "needs_review",
            current_thread["followUpSendAttempt"]["state"],
        )

    def test_failed_followup_retry_blocks_when_sent_items_lookup_fails(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
        })
        fake_fs = FakeFollowupFirestore([outbound])
        followup_config = {
            "currentFollowUpIndex": 0,
            "lastSendError": "Read timed out after Graph accepted send",
            "lastSendAttemptAt": "2026-06-26T12:05:00Z",
            "followUps": [{"message": "Hi [NAME],\n\nJust following up."}],
        }
        thread_data = {
            "email": ["bp21harrison@gmail.com"],
            "contactName": "Ryan Broker",
        }

        with patch.object(followup, "_fs", fake_fs), \
             patch.object(followup, "exponential_backoff_request", return_value=FakeResponse(200, {
                 "value": [{"id": "graph-root", "subject": "0 Gemini Ave", "conversationId": "conv-1"}]
             })), \
             patch.object(
                 followup,
                 "find_matching_sent_message_for_retry",
                 side_effect=followup.SentMailGuardLookupError("Graph 401"),
             ), \
             patch.object(requests, "post") as post:
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
            )

        self.assertFalse(result)
        post.assert_not_called()
        self.assertIn("Sent Items retry guard failed", followup._send_followup_email.last_error)
        self.assertTrue(followup._send_followup_email.guard_failed_closed)

    def test_failed_followup_retry_blocks_when_conversation_was_manually_continued(self):
        outbound = FakeMessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-06-26T12:00:00Z",
        })
        fake_fs = FakeFollowupFirestore([outbound])
        followup_config = {
            "currentFollowUpIndex": 0,
            "lastSendError": "Read timed out after Graph accepted send",
            "lastSendAttemptAt": "2026-06-26T12:05:00Z",
            "followUps": [{"message": "Hi [NAME],\n\nJust following up."}],
        }
        thread_data = {
            "email": ["bp21harrison@gmail.com"],
            "contactName": "Ryan Broker",
        }
        manual_continuation = {
            "id": "manual-sent-1",
            "internetMessageId": "<manual-sent-1@example.com>",
            "conversationId": "conv-1",
            "sentDateTime": "2026-06-26T12:08:00Z",
        }

        with patch.object(followup, "_fs", fake_fs), \
             patch.object(followup, "exponential_backoff_request", return_value=FakeResponse(200, {
                 "value": [{"id": "graph-root", "subject": "0 Gemini Ave", "conversationId": "conv-1"}]
             })), \
             patch.object(followup, "find_matching_sent_message_for_retry", return_value=None), \
             patch.object(followup, "find_sent_conversation_continuation_for_retry", return_value=manual_continuation, create=True) as continuation_guard, \
             patch.object(requests, "post") as post:
            result = followup._send_followup_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "thread-1",
                thread_data,
                followup_config,
                0,
            )

        self.assertFalse(result)
        continuation_guard.assert_called_once()
        self.assertEqual(continuation_guard.call_args.kwargs["conversation_id"], "conv-1")
        post.assert_not_called()
        self.assertIn("manually continued", followup._send_followup_email.last_error)
        self.assertTrue(followup._send_followup_email.guard_failed_closed)

    def test_guard_lookup_failure_release_marks_manual_review(self):
        thread_ref = FakeThreadRef({
            "status": "active",
            "followUpStatus": "waiting",
            "followUpConfig": {
                "enabled": True,
                "currentFollowUpIndex": 1,
            },
        })
        attempted_at = datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc)

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            followup._release_followup_claim(
                "uid-1",
                "thread-1",
                reason="Sent Items retry guard failed: Graph 401",
                attempted_at=attempted_at,
                current_index=1,
                fail_closed=True,
            )

        update = thread_ref.updates[-1]
        self.assertEqual(update["followUpStatus"], "needs_review")
        self.assertEqual(update["status"], "action_needed")
        self.assertEqual(update["statusReason"], "followup_send_guard_failed")
        self.assertFalse(update["followUpConfig.enabled"])
        self.assertEqual(update["followUpConfig.lastSendAttemptIndex"], 1)

    def test_fail_closed_release_preserves_terminal_status_and_reason(self):
        terminal_data = {
            "status": "stopped",
            "statusReason": "requirements_mismatch",
            "followUpStatus": "stopped",
            "pendingTerminalReason": "requirements_mismatch",
            "hasInboundReply": True,
            "followUpConfig": {
                "enabled": False,
                "currentFollowUpIndex": 1,
                "processingBy": "followup-worker",
            },
        }
        thread_ref = FakeThreadRef(terminal_data)

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            followup._release_followup_claim(
                "uid-1",
                "thread-1",
                reason="Follow-up stopped because the broker replied",
                current_index=1,
                fail_closed=True,
            )

        update = thread_ref.updates[-1]
        self.assertNotIn("status", update)
        self.assertNotIn("statusReason", update)
        self.assertNotIn("followUpStatus", update)
        self.assertIsNone(update["followUpConfig.processingBy"])
        self.assertIsNone(update["followUpConfig.processingAt"])

    def test_release_does_not_clear_a_claim_owned_by_another_worker(self):
        thread_ref = FakeThreadRef({
            "status": "active",
            "followUpStatus": "waiting",
            "followUpConfig": {
                "enabled": True,
                "currentFollowUpIndex": 1,
                "processingBy": "new-owner",
            },
        })

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            released = followup._release_followup_claim(
                "uid-1",
                "thread-1",
                reason="old worker failed",
                current_index=1,
                claim_owner="old-owner",
                fail_closed=True,
            )

        self.assertFalse(released)
        self.assertEqual([], thread_ref.updates)

    def test_release_does_not_overwrite_a_newer_send_attempt(self):
        thread_ref = FakeThreadRef({
            "status": "active",
            "followUpStatus": "waiting",
            "followUpSendAttempt": {
                "id": "attempt-new",
                "state": "sending",
                "owner": "claim-owner",
                "index": 1,
            },
            "followUpConfig": {
                "enabled": True,
                "currentFollowUpIndex": 1,
                "processingBy": "claim-owner",
            },
        })

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            released = followup._release_followup_claim(
                "uid-1",
                "thread-1",
                reason="stale worker failed",
                current_index=1,
                claim_owner="claim-owner",
                send_attempt_id="attempt-old",
                fail_closed=True,
            )

        self.assertFalse(released)
        self.assertEqual([], thread_ref.updates)

    def test_schedule_next_followup_clears_previous_retry_guard_state(self):
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "lastSendError": "Read timed out",
            "lastSendAttemptAt": "2026-06-26T12:05:00Z",
            "lastSendAttemptIndex": 0,
            "followUps": [
                {"waitTime": 1, "waitUnit": "hours", "message": "First"},
                {"waitTime": 2, "waitUnit": "hours", "message": "Second"},
            ],
        }
        thread_ref = FakeThreadRef({
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": dict(followup_config),
        })

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            advanced = followup._schedule_next_followup(
                "uid-1",
                "thread-1",
                followup_config,
                just_sent_index=0,
                claim_owner="claim-owner",
            )

        self.assertEqual("scheduled", getattr(advanced, "value", advanced))
        update = thread_ref.updates[-1]
        self.assertEqual(update["followUpConfig.currentFollowUpIndex"], 1)
        self.assertIsNone(update["followUpConfig.lastSendError"])
        self.assertIsNone(update["followUpConfig.lastSendAttemptAt"])
        self.assertIsNone(update["followUpConfig.lastSendAttemptIndex"])

    def test_schedule_next_followup_propagates_transaction_failure(self):
        class ExplodingFirestore(FakeFirestore):
            def transaction(self):
                raise RuntimeError("transaction unavailable")

        current_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "followUps": [
                {"waitTime": 1, "waitUnit": "hours", "message": "Only"},
            ],
        }
        thread_ref = FakeThreadRef({
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": current_config,
        })

        with patch.object(followup, "_fs", ExplodingFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ), self.assertRaisesRegex(RuntimeError, "transaction unavailable"):
            followup._schedule_next_followup(
                "uid-1",
                "thread-1",
                current_config,
                just_sent_index=0,
                claim_owner="claim-owner",
            )

    def test_post_send_schedule_preserves_reply_terminal_and_manual_pause(self):
        followups = [
            {"waitTime": 1, "waitUnit": "hours", "message": "First"},
            {"waitTime": 2, "waitUnit": "hours", "message": "Second"},
        ]
        cases = {
            "inbound_reply": ({
                "status": "active",
                "followUpStatus": "waiting",
                "hasInboundReply": True,
            }, "inbound_preserved"),
            "terminal": ({
                "status": "stopped",
                "statusReason": "requirements_mismatch",
                "followUpStatus": "stopped",
                "pendingTerminalReason": "requirements_mismatch",
                "hasInboundReply": False,
            }, "terminal_preserved"),
            "manual_pause": ({
                "status": "paused",
                "statusReason": "manual_continuation",
                "followUpStatus": "paused",
                "hasInboundReply": False,
            }, "paused_preserved"),
        }

        for name, (state, expected_outcome) in cases.items():
            with self.subTest(name=name):
                current_config = {
                    "enabled": True,
                    "currentFollowUpIndex": 0,
                    "processingBy": "claim-owner",
                    "processingAt": "2026-06-26T12:05:00Z",
                    "processingLeaseUntil": "2026-06-26T12:15:00Z",
                    "lastSendError": "Graph send outcome pending reconciliation",
                    "lastSendAttemptAt": "2026-06-26T12:05:00Z",
                    "lastSendAttemptIndex": 0,
                    "followUps": followups,
                }
                current_thread = {
                    "clientId": "client-1",
                    "email": ["broker@example.com"],
                    "contactName": "Broker",
                    **state,
                    "followUpConfig": current_config,
                }
                current_thread["followUpSendAttempt"] = _sealed_followup_attempt(
                    current_thread,
                    current_config,
                    attempt_id="attempt-sent",
                    owner="claim-owner",
                    subject="Subject",
                    conversation_id="conv-current",
                )
                thread_ref = FakeThreadRef(current_thread)

                with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    advanced = followup._schedule_next_followup(
                        "uid-1",
                        "thread-1",
                        current_config,
                        just_sent_index=0,
                        claim_owner="claim-owner",
                        send_attempt_id="attempt-sent",
                    )

                self.assertEqual(
                    expected_outcome,
                    getattr(advanced, "value", advanced),
                )
                update = thread_ref.updates[-1]
                self.assertEqual(1, update["followUpConfig.currentFollowUpIndex"])
                self.assertIsNone(update["followUpConfig.processingBy"])
                self.assertIsNone(update["followUpConfig.processingAt"])
                self.assertIsNone(update["followUpConfig.processingLeaseUntil"])
                self.assertIsNone(update["followUpConfig.lastSendError"])
                self.assertIsNone(update["followUpConfig.lastSendAttemptAt"])
                self.assertIsNone(update["followUpConfig.lastSendAttemptIndex"])
                self.assertEqual("committed", update["followUpSendAttempt"]["state"])
                self.assertNotIn("status", update)
                self.assertNotIn("followUpStatus", update)

    def test_post_send_schedule_preserves_pause_after_scheduling_is_disabled(self):
        accepted_config = {
            "enabled": True,
            "nextFollowUpAt": "2026-06-26T12:05:00Z",
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "processingAt": "2026-06-26T12:05:00Z",
            "processingLeaseUntil": "2026-06-26T12:15:00Z",
            "lastSendError": "Graph send outcome pending reconciliation",
            "lastSendAttemptAt": "2026-06-26T12:05:00Z",
            "lastSendAttemptIndex": 0,
            "followUps": [
                {"waitTime": 1, "waitUnit": "hours", "message": "First"},
                {"waitTime": 2, "waitUnit": "hours", "message": "Second"},
            ],
        }
        accepted_thread = {
            "clientId": "client-1",
            "email": ["broker@example.com"],
            "contactName": "Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": accepted_config,
        }
        marker = _sealed_followup_attempt(
            accepted_thread,
            accepted_config,
            attempt_id="attempt-sent",
            owner="claim-owner",
            subject="Subject",
            conversation_id="conv-current",
        )
        current_config = {
            **accepted_config,
            "enabled": False,
            "nextFollowUpAt": None,
        }
        current_thread = {
            **accepted_thread,
            "status": "paused",
            "statusReason": "manual_continuation",
            "followUpStatus": "paused",
            "hasInboundReply": True,
            "followUpConfig": current_config,
            "followUpSendAttempt": marker,
        }
        thread_ref = FakeThreadRef(current_thread)

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            outcome = followup._schedule_next_followup(
                "uid-1",
                "thread-1",
                accepted_config,
                just_sent_index=0,
                claim_owner="claim-owner",
                send_attempt_id="attempt-sent",
            )

        self.assertEqual("inbound_preserved", getattr(outcome, "value", outcome))
        update = thread_ref.updates[-1]
        self.assertEqual(1, update["followUpConfig.currentFollowUpIndex"])
        self.assertEqual("committed", update["followUpSendAttempt"]["state"])
        self.assertNotIn("status", update)
        self.assertNotIn("followUpStatus", update)

    def test_post_send_schedule_rejects_same_id_attempt_in_manual_review(self):
        current_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "followUps": [
                {"waitTime": 1, "waitUnit": "hours", "message": "First"},
                {"waitTime": 2, "waitUnit": "hours", "message": "Second"},
            ],
        }
        current_thread = {
            "clientId": "client-1",
            "email": ["broker@example.com"],
            "contactName": "Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": current_config,
        }
        current_thread["followUpSendAttempt"] = {
            "id": "attempt-sent",
            "state": "needs_review",
            "resolution": "ambiguous",
            "owner": "claim-owner",
            "index": 0,
            "sendIdentity": followup._followup_send_identity(
                current_thread,
                current_config,
                0,
            ),
        }
        thread_ref = FakeThreadRef(current_thread)

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            outcome = followup._schedule_next_followup(
                "uid-1",
                "thread-1",
                current_config,
                just_sent_index=0,
                claim_owner="claim-owner",
                send_attempt_id="attempt-sent",
            )

        self.assertEqual("ambiguous", getattr(outcome, "value", outcome))
        self.assertEqual([], thread_ref.updates)

    def test_post_send_schedule_does_not_report_unclassified_block_as_preserved(self):
        current_config = {
            "enabled": False,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "followUps": [
                {"waitTime": 1, "waitUnit": "hours", "message": "First"},
                {"waitTime": 2, "waitUnit": "hours", "message": "Second"},
            ],
        }
        current_thread = {
            "clientId": "client-1",
            "email": ["broker@example.com"],
            "contactName": "Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": current_config,
        }
        marker = {
            "id": "attempt-sent",
            "state": "sending",
            "owner": "claim-owner",
            "index": 0,
            "sendIdentity": followup._followup_send_identity(
                current_thread,
                current_config,
                0,
            ),
        }
        current_thread["followUpSendAttempt"] = marker
        thread_ref = FakeThreadRef(current_thread)

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            outcome = followup._schedule_next_followup(
                "uid-1",
                "thread-1",
                current_config,
                just_sent_index=0,
                claim_owner="claim-owner",
                send_attempt_id="attempt-sent",
                send_attempt_marker=marker,
            )

        self.assertEqual("ambiguous", getattr(outcome, "value", outcome))
        self.assertEqual([], thread_ref.updates)

    def test_legacy_preservation_does_not_hide_claim_conflict(self):
        sent_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "lastSendError": "legacy ambiguous send",
            "lastSendAttemptAt": "2026-06-26T12:05:00Z",
            "lastSendAttemptIndex": 0,
            "followUps": [
                {"waitTime": 1, "waitUnit": "hours", "message": "First"},
                {"waitTime": 2, "waitUnit": "hours", "message": "Second"},
            ],
        }
        current_config = {**sent_config, "processingBy": "new-owner"}
        thread_ref = FakeThreadRef({
            "status": "active",
            "followUpStatus": "paused",
            "hasInboundReply": True,
            "followUpConfig": current_config,
        })

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            outcome = followup._schedule_next_followup(
                "uid-1",
                "thread-1",
                sent_config,
                just_sent_index=0,
                claim_owner="claim-owner",
            )

        self.assertEqual("ambiguous", getattr(outcome, "value", outcome))
        self.assertEqual([], thread_ref.updates)

    def test_post_send_schedule_preserves_newer_owner_and_index(self):
        followups = [
            {"waitTime": 1, "waitUnit": "hours", "message": "First"},
            {"waitTime": 2, "waitUnit": "hours", "message": "Second"},
        ]
        stale_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "followUps": followups,
        }
        cases = {
            "new_owner": {"currentFollowUpIndex": 0, "processingBy": "new-owner"},
            "new_index": {"currentFollowUpIndex": 1, "processingBy": "claim-owner"},
        }

        for name, claim_state in cases.items():
            with self.subTest(name=name):
                current_config = {
                    "enabled": True,
                    "followUps": followups,
                    **claim_state,
                }
                thread_ref = FakeThreadRef({
                    "status": "active",
                    "followUpStatus": "waiting",
                    "hasInboundReply": False,
                    "followUpConfig": current_config,
                })

                with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    advanced = followup._schedule_next_followup(
                        "uid-1",
                        "thread-1",
                        stale_config,
                        just_sent_index=0,
                        claim_owner="claim-owner",
                    )

                self.assertEqual("ambiguous", getattr(advanced, "value", advanced))
                self.assertEqual([], thread_ref.updates)

    def test_post_send_schedule_treats_changed_config_as_ambiguous(self):
        sent_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "followUps": [
                {"waitTime": 1, "waitUnit": "hours", "message": "Sent body"},
                {"waitTime": 2, "waitUnit": "hours", "message": "Second"},
            ],
        }
        current_config = {
            **sent_config,
            "followUps": [
                {"waitTime": 1, "waitUnit": "hours", "message": "Changed body"},
                {"waitTime": 2, "waitUnit": "hours", "message": "Second"},
            ],
        }
        thread_ref = FakeThreadRef({
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": current_config,
        })

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            outcome = followup._schedule_next_followup(
                "uid-1",
                "thread-1",
                sent_config,
                just_sent_index=0,
                claim_owner="claim-owner",
            )

        self.assertEqual("ambiguous", getattr(outcome, "value", outcome))
        self.assertEqual([], thread_ref.updates)

    def test_reconciled_send_attempt_rejects_new_owner_input_conflicts(self):
        sent_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "old-owner",
            "followUps": [
                {"waitTime": 1, "waitUnit": "hours", "message": "Sent body"},
                {"waitTime": 2, "waitUnit": "hours", "message": "Second"},
            ],
        }
        sent_thread = {
            "clientId": "client-sent",
            "email": ["sent@example.com"],
            "contactName": "Sent Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": sent_config,
        }
        sent_identity = followup._followup_send_identity(
            sent_thread,
            sent_config,
            0,
        )
        cases = {
            "changed_body": {
                "thread": {
                    **sent_thread,
                    "followUpConfig": {
                        **sent_config,
                        "processingBy": "new-owner",
                        "followUps": [
                            {
                                "waitTime": 1,
                                "waitUnit": "hours",
                                "message": "Changed body",
                            },
                            sent_config["followUps"][1],
                        ],
                    },
                },
            },
            "changed_recipient_and_client": {
                "thread": {
                    **sent_thread,
                    "clientId": "client-current",
                    "email": ["current@example.com"],
                    "followUpConfig": {
                        **sent_config,
                        "processingBy": "new-owner",
                    },
                },
            },
        }

        for name, case in cases.items():
            with self.subTest(name=name):
                current_thread = case["thread"]
                current_thread["followUpSendAttempt"] = {
                    "id": "attempt-sent",
                    "state": "uncertain",
                    "owner": "old-owner",
                    "index": 0,
                    "sendIdentity": sent_identity,
                    "recipient": "sent@example.com",
                    "body": "Sent body",
                }
                thread_ref = FakeThreadRef(current_thread)

                with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    outcome = followup._schedule_next_followup(
                        "uid-1",
                        "thread-1",
                        current_thread["followUpConfig"],
                        just_sent_index=0,
                        claim_owner="new-owner",
                        send_attempt_id="attempt-sent",
                    )

                self.assertEqual("ambiguous", getattr(outcome, "value", outcome))
                self.assertEqual([], thread_ref.updates)

    def test_post_accept_rejects_unprovable_mixed_schema_input_types(self):
        attempted_at = datetime(2026, 6, 26, 12, 5, tzinfo=timezone.utc)
        cases = (
            (
                "row_int_to_bool",
                {"rowNumber": 0},
                {"rowNumber": False},
                {},
                {},
            ),
            (
                "email_list_to_string",
                {"email": ["sent@example.com"]},
                {"email": "sent@example.com"},
                {},
                {},
            ),
            (
                "cc_source_swap",
                {"ccEmails": ["cc@example.com"]},
                {"ccRecipients": ["cc@example.com"]},
                {},
                {},
            ),
            (
                "config_datetime_to_iso_string",
                {},
                {},
                {"auditMetadata": attempted_at},
                {"auditMetadata": attempted_at.isoformat()},
            ),
        )

        for name, sent_patch, current_patch, sent_config_patch, current_config_patch in cases:
            with self.subTest(name=name):
                sent_config = {
                    "enabled": True,
                    "currentFollowUpIndex": 0,
                    "processingBy": "claim-owner",
                    "followUps": [
                        {"message": "Sent body"},
                        {"message": "Second body"},
                    ],
                    **sent_config_patch,
                }
                current_config = {
                    **sent_config,
                    **current_config_patch,
                }
                sent_thread = {
                    "clientId": "client-sent",
                    "email": ["sent@example.com"],
                    "contactName": "Sent Broker",
                    "status": "active",
                    "followUpStatus": "waiting",
                    "hasInboundReply": False,
                    **sent_patch,
                    "followUpConfig": sent_config,
                }
                legacy_identity = followup._followup_send_identity(
                    sent_thread,
                    sent_config,
                    0,
                )
                legacy_identity.pop("inputSignatures")
                legacy_identity.pop("contactNameExact")
                legacy_identity.pop("contactNamePresent")
                legacy_identity.pop("contactNameType")

                current_thread = {
                    **sent_thread,
                    **current_patch,
                    "followUpConfig": current_config,
                    "followUpSendAttempt": {
                        "id": "attempt-mixed-schema",
                        "state": "sending",
                        "owner": "claim-owner",
                        "index": 0,
                        "sendIdentity": legacy_identity,
                    },
                }
                if name == "cc_source_swap":
                    current_thread.pop("ccEmails", None)
                thread_ref = FakeThreadRef(current_thread)

                with patch.object(
                    followup,
                    "_fs",
                    FakeFirestore(thread_ref),
                ), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    outcome = followup._schedule_next_followup(
                        "uid-1",
                        "thread-1",
                        sent_config,
                        just_sent_index=0,
                        claim_owner="claim-owner",
                        send_attempt_id="attempt-mixed-schema",
                    )

                self.assertEqual(
                    "ambiguous",
                    getattr(outcome, "value", outcome),
                )
                self.assertEqual([], thread_ref.updates)

    def test_post_accept_rejects_missing_or_malformed_send_identity(self):
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "followUps": [
                {"message": "Sent body"},
                {"message": "Second body"},
            ],
        }
        thread_data = {
            "clientId": "client-sent",
            "email": ["sent@example.com"],
            "contactName": "Sent Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": followup_config,
        }
        valid_identity = followup._followup_send_identity(
            thread_data,
            followup_config,
            0,
        )
        missing = object()
        identity_cases = (
            ("missing", missing),
            ("none", None),
            ("string", "not-an-identity"),
            ("list", []),
            ("empty_dict", {}),
            (
                "partial_dict",
                {"inputSignatures": valid_identity["inputSignatures"]},
            ),
        )

        for name, malformed_identity in identity_cases:
            with self.subTest(name=name):
                attempt = {
                    "id": f"attempt-malformed-{name}",
                    "state": "sending",
                    "owner": "claim-owner",
                    "index": 0,
                }
                if malformed_identity is not missing:
                    attempt["sendIdentity"] = malformed_identity
                current_thread = {
                    **thread_data,
                    "followUpSendAttempt": attempt,
                }
                thread_ref = FakeThreadRef(current_thread)

                with patch.object(
                    followup,
                    "_fs",
                    FakeFirestore(thread_ref),
                ), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    outcome = followup._schedule_next_followup(
                        "uid-1",
                        "thread-1",
                        followup_config,
                        just_sent_index=0,
                        claim_owner="claim-owner",
                        send_attempt_id=attempt["id"],
                    )

                self.assertEqual(
                    "ambiguous",
                    getattr(outcome, "value", outcome),
                )
                self.assertEqual([], thread_ref.updates)

    def test_terminal_state_does_not_hide_accepted_send_conflicts(self):
        sent_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "followUps": [
                {"waitTime": 1, "waitUnit": "hours", "message": "Sent body"},
                {"waitTime": 2, "waitUnit": "hours", "message": "Second"},
            ],
        }
        sent_thread = {
            "clientId": "client-sent",
            "email": ["sent@example.com"],
            "contactName": "Sent Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": sent_config,
        }
        sent_identity = followup._followup_send_identity(sent_thread, sent_config, 0)
        base_attempt = {
            "id": "attempt-sent",
            "state": "sending",
            "owner": "claim-owner",
            "index": 0,
            "sendIdentity": sent_identity,
        }
        cases = {
            "inbound_with_new_owner": {
                "state": {"hasInboundReply": True},
                "config": {"processingBy": "new-owner"},
            },
            "pause_with_new_index": {
                "state": {"status": "paused", "followUpStatus": "paused"},
                "config": {"currentFollowUpIndex": 1},
            },
            "terminal_with_changed_config": {
                "state": {"status": "stopped", "followUpStatus": "stopped"},
                "config": {
                    "followUps": [
                        {
                            "waitTime": 1,
                            "waitUnit": "hours",
                            "message": "Changed body",
                        },
                        sent_config["followUps"][1],
                    ],
                },
            },
            "inbound_with_replaced_attempt": {
                "state": {"hasInboundReply": True},
                "attempt": {"id": "attempt-new"},
            },
        }

        for name, changes in cases.items():
            with self.subTest(name=name):
                current_config = {**sent_config, **changes.get("config", {})}
                current_thread = {
                    **sent_thread,
                    **changes["state"],
                    "followUpConfig": current_config,
                    "followUpSendAttempt": {
                        **base_attempt,
                        **changes.get("attempt", {}),
                    },
                }
                thread_ref = FakeThreadRef(current_thread)

                with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
                    "google.cloud.firestore.transactional", lambda fn: fn
                ):
                    outcome = followup._schedule_next_followup(
                        "uid-1",
                        "thread-1",
                        sent_config,
                        just_sent_index=0,
                        claim_owner="claim-owner",
                        send_attempt_id="attempt-sent",
                    )

                self.assertEqual("ambiguous", getattr(outcome, "value", outcome))
                self.assertEqual([], thread_ref.updates)

    def test_reconciled_send_attempt_commits_with_index_advance(self):
        current_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "reconcile-owner",
            "lastSendError": "Graph send outcome pending reconciliation",
            "lastSendAttemptAt": "2026-06-26T12:05:00Z",
            "lastSendAttemptIndex": 0,
            "followUps": [
                {"waitTime": 1, "waitUnit": "hours", "message": "Sent body"},
                {"waitTime": 2, "waitUnit": "hours", "message": "Second"},
            ],
        }
        current_thread = {
            "clientId": "client-sent",
            "email": ["sent@example.com"],
            "contactName": "Sent Broker",
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": current_config,
        }
        current_thread["followUpSendAttempt"] = _sealed_followup_attempt(
            current_thread,
            current_config,
            attempt_id="attempt-sent",
            owner="old-owner",
            state="uncertain",
            reconciliation_owner="reconcile-owner",
            subject="Subject",
            conversation_id="conv-current",
        )
        thread_ref = FakeThreadRef(current_thread)

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            outcome = followup._schedule_next_followup(
                "uid-1",
                "thread-1",
                current_config,
                just_sent_index=0,
                claim_owner="reconcile-owner",
                send_attempt_id="attempt-sent",
            )

        self.assertEqual("scheduled", getattr(outcome, "value", outcome))
        update = thread_ref.updates[-1]
        self.assertEqual(1, update["followUpConfig.currentFollowUpIndex"])
        self.assertEqual("committed", update["followUpSendAttempt"]["state"])
        self.assertEqual("sent", update["followUpSendAttempt"]["resolution"])

    @patch.object(followup, "_clear_followup_row_highlight", create=True)
    def test_post_send_schedule_marks_max_reached_for_exact_active_claim(
        self,
        clear_highlight,
    ):
        current_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "processingBy": "claim-owner",
            "followUps": [
                {"waitTime": 1, "waitUnit": "hours", "message": "Only"},
            ],
        }
        thread_ref = FakeThreadRef({
            "status": "active",
            "followUpStatus": "waiting",
            "hasInboundReply": False,
            "followUpConfig": current_config,
        })

        with patch.object(followup, "_fs", FakeFirestore(thread_ref)), patch(
            "google.cloud.firestore.transactional", lambda fn: fn
        ):
            advanced = followup._schedule_next_followup(
                "uid-1",
                "thread-1",
                current_config,
                just_sent_index=0,
                claim_owner="claim-owner",
            )

        self.assertEqual("max_reached", getattr(advanced, "value", advanced))
        update = thread_ref.updates[-1]
        self.assertEqual("max_reached", update["followUpStatus"])
        self.assertEqual("stopped", update["status"])
        self.assertEqual("max_followups_reached", update["statusReason"])
        self.assertIsNone(update["followUpConfig.processingBy"])
        clear_highlight.assert_called_once_with("uid-1", "thread-1")


if __name__ == "__main__":
    unittest.main()
