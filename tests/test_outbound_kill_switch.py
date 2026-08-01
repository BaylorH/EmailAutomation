"""Rail 3 — global outbound kill switch (SITESIFT_OUTBOUND_MODE).

A single fail-closed lever that halts (or downgrades to dry-run) ALL outbound
Graph sends without a code deploy. Absence of the env var must preserve normal
"live" behavior (so the existing suite is unaffected); an unrecognized value must
fail CLOSED to "paused" so a typo can never keep blasting outbound.

The hard guarantee under test: when the mode is not "live", the send functions
must NOT hit Microsoft Graph (no requests.post), and must report suppression.
"""
import os
import unittest
from unittest.mock import MagicMock, Mock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import email as email_module
from email_automation import followup, processing
from email_automation.campaign_safety import CampaignAutomationDecision
from email_automation.column_config import get_default_column_config


OUTBOUND_MODE_ENV = "SITESIFT_OUTBOUND_MODE"
CLIENT_ID = "client-kill-switch-live"


class _GateSnapshot:
    def __init__(self, data=None):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class _GateNode:
    def __init__(self, docs, path=()):
        self.docs = docs
        self.path = path

    def collection(self, name):
        return _GateNode(self.docs, self.path + (name,))

    def document(self, name):
        return _GateNode(self.docs, self.path + (name,))

    def get(self):
        return _GateSnapshot(self.docs.get(self.path))


def _live_gate_firestore():
    return _GateNode({
        ("users", "user-1", "clients", CLIENT_ID): {
            "status": "live",
            "automationPaused": False,
        },
        ("systemConfig", "campaignAccess"): {
            "automationEnabled": True,
            "allowedUids": [],
        },
    })


def _clear_outbound_mode(env):
    env.pop(OUTBOUND_MODE_ENV, None)


def _make_graph_response():
    resp = Mock(status_code=200)
    resp.json.return_value = {
        "id": "draft-1",
        "internetMessageId": "<mid-1@example.com>",
        "conversationId": "conv-1",
        "subject": "Subject",
        "toRecipients": [],
    }
    resp.raise_for_status = Mock()
    resp.headers = {}
    return resp


class _GraphResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"Unexpected HTTP status {self.status_code}")


def _fake_requests():
    resp = _make_graph_response()
    fake = MagicMock(name="requests")
    fake.post = Mock(return_value=resp)
    fake.get = Mock(return_value=resp)
    fake.patch = Mock(return_value=resp)
    return fake


def _allow_campaign_decision():
    return CampaignAutomationDecision(
        state="allow",
        reason="",
        client_data={
            "status": "live",
            "columnConfig": get_default_column_config(),
        },
        metadata={"terminal": False, "stopKind": "none"},
    )


class _MessageDoc:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _DirectSendFirestoreNode:
    """Minimal Firestore double for direct reply/follow-up send tests."""

    def __init__(self, docs, messages, updates, path=()):
        self.docs = docs
        self.messages = messages
        self.updates = updates
        self.path = path

    def collection(self, name):
        return _DirectSendFirestoreNode(
            self.docs,
            self.messages,
            self.updates,
            self.path + (name,),
        )

    def document(self, name):
        return _DirectSendFirestoreNode(
            self.docs,
            self.messages,
            self.updates,
            self.path + (name,),
        )

    def get(self):
        return _GateSnapshot(self.docs.get(self.path))

    def where(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def stream(self):
        if self.path[-1:] == ("messages",):
            return list(self.messages)
        return []

    def update(self, data):
        self.updates.append(dict(data))


def _direct_send_firestore():
    thread_data = {
        "clientId": CLIENT_ID,
        "status": "active",
        "followUpStatus": "waiting",
        "email": ["broker@example.com"],
    }
    docs = {
        ("users", "user-1"): {
            "email": "sender@example.com",
            "signatureMode": "none",
        },
        ("users", "user-1", "threads", "thread-1"): thread_data,
    }
    messages = [
        _MessageDoc({
            "direction": "outbound",
            "headers": {"internetMessageId": "<root@example.com>"},
            "sentDateTime": "2026-08-01T12:00:00Z",
        })
    ]
    return _DirectSendFirestoreNode(docs, messages, [])


class ResolveOutboundModeTests(unittest.TestCase):
    """The resolver is the single source of truth; it must fail closed."""

    def setUp(self):
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        _clear_outbound_mode(os.environ)

    def tearDown(self):
        self._env.stop()

    def test_unset_defaults_to_live(self):
        _clear_outbound_mode(os.environ)
        self.assertEqual(email_module.resolve_outbound_mode(), "live")
        self.assertTrue(email_module.outbound_sending_enabled())

    def test_empty_string_defaults_to_live(self):
        os.environ[OUTBOUND_MODE_ENV] = "   "
        self.assertEqual(email_module.resolve_outbound_mode(), "live")

    def test_explicit_live(self):
        os.environ[OUTBOUND_MODE_ENV] = "live"
        self.assertEqual(email_module.resolve_outbound_mode(), "live")

    def test_dry_run_recognized(self):
        os.environ[OUTBOUND_MODE_ENV] = "dry_run"
        self.assertEqual(email_module.resolve_outbound_mode(), "dry_run")
        self.assertFalse(email_module.outbound_sending_enabled())

    def test_paused_recognized(self):
        os.environ[OUTBOUND_MODE_ENV] = "paused"
        self.assertEqual(email_module.resolve_outbound_mode(), "paused")
        self.assertFalse(email_module.outbound_sending_enabled())

    def test_case_and_whitespace_normalized(self):
        os.environ[OUTBOUND_MODE_ENV] = "  DRY_RUN  "
        self.assertEqual(email_module.resolve_outbound_mode(), "dry_run")

    def test_unrecognized_value_fails_closed_to_paused(self):
        # A typo ("off", "true", "Live!", "stop") must NEVER resolve to live.
        for bad in ("off", "true", "stop", "enabled", "Live!", "1", "yes"):
            os.environ[OUTBOUND_MODE_ENV] = bad
            self.assertEqual(
                email_module.resolve_outbound_mode(),
                "paused",
                f"Unrecognized mode {bad!r} must fail closed to 'paused'",
            )
            self.assertFalse(email_module.outbound_sending_enabled())


class SendAndIndexEmailKillSwitchTests(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def _run(self, mode):
        os.environ[OUTBOUND_MODE_ENV] = mode
        fake = _fake_requests()
        with patch.object(email_module, "requests", fake), \
             patch("email_automation.clients._fs", _live_gate_firestore()), \
             patch("email_automation.processing.is_contact_opted_out", return_value=None), \
             patch.object(email_module, "save_thread_root", return_value=True), \
             patch.object(email_module, "save_message", return_value=True), \
             patch.object(email_module, "index_message_id", return_value=True), \
             patch.object(email_module, "index_conversation_id", return_value=True), \
             patch.object(
                 email_module,
                 "lookup_thread_by_message_id",
                 return_value=email_module.normalize_message_id("<mid-1@example.com>"),
             ):
            result = email_module.send_and_index_email(
                user_id="user-1",
                headers={"Authorization": "Bearer x"},
                script="Hello, this is a clean outreach message about available space.",
                recipients=["broker@example.com"],
                client_id_or_none=CLIENT_ID,
                signature_mode="none",
            )
        return result, fake

    def test_paused_mode_does_not_hit_graph(self):
        result, fake = self._run("paused")
        fake.post.assert_not_called()
        self.assertEqual(result.get("sent"), [])
        self.assertTrue(result.get("suppressedByKillSwitch"))
        self.assertEqual(result.get("outboundMode"), "paused")

    def test_dry_run_mode_does_not_hit_graph(self):
        result, fake = self._run("dry_run")
        fake.post.assert_not_called()
        self.assertEqual(result.get("sent"), [])
        self.assertTrue(result.get("suppressedByKillSwitch"))
        self.assertEqual(result.get("outboundMode"), "dry_run")

    def test_unrecognized_mode_fails_closed_no_graph(self):
        result, fake = self._run("totally-bogus")
        fake.post.assert_not_called()
        self.assertTrue(result.get("suppressedByKillSwitch"))

    def test_live_mode_still_sends(self):
        # Guard the guard: default/live must NOT be broken by the kill switch.
        result, fake = self._run("live")
        self.assertTrue(fake.post.called)
        self.assertIn("broker@example.com", result.get("sent", []))
        self.assertFalse(result.get("suppressedByKillSwitch"))


class SendOutboxAsReplyKillSwitchTests(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_paused_mode_reply_does_not_hit_graph(self):
        os.environ[OUTBOUND_MODE_ENV] = "paused"
        fake = _fake_requests()
        with patch.object(email_module, "requests", fake), \
             patch.object(email_module, "_fetch_graph_message_metadata", return_value={}):
            result = email_module._send_outbox_as_reply(
                user_id="user-1",
                headers={"Authorization": "Bearer x"},
                body="Thanks, following up on the space.",
                reply_to_msg_id="msg-1",
                thread_id="thread-1",
                signature_mode="none",
            )
        fake.post.assert_not_called()
        self.assertFalse(result.get("sent"))
        self.assertTrue(result.get("suppressedByKillSwitch"))


class SingleOutboxItemKillSwitchTests(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_paused_mode_leaves_item_queued_without_claim(self):
        os.environ[OUTBOUND_MODE_ENV] = "paused"

        class FakeRef:
            def __init__(self):
                self.deleted = False

            def delete(self):
                self.deleted = True

        ref = FakeRef()
        item = {
            "doc": type("D", (), {"id": "outbox-1", "reference": ref})(),
            "data": {"assignedEmails": ["broker@example.com"], "script": "Hi"},
        }
        fake = _fake_requests()
        with patch.object(email_module, "requests", fake), \
             patch.object(
                 email_module, "_delete_cancelled_outbox_item_if_needed", return_value=False
             ), \
             patch.object(email_module, "_claim_outbox_item") as claim:
            email_module._send_single_outbox_item(
                user_id="user-1",
                headers={"Authorization": "Bearer x"},
                item=item,
            )
        # Fail-closed: never claimed, never sent, item left in the queue untouched.
        claim.assert_not_called()
        fake.post.assert_not_called()
        self.assertFalse(ref.deleted)


class DirectAutoReplyKillSwitchTests(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_paused_and_invalid_modes_stop_auto_reply_at_entry(self):
        for mode in ("paused", "Live!"):
            with self.subTest(mode=mode), patch.dict(
                os.environ,
                {
                    OUTBOUND_MODE_ENV: mode,
                    "SITESIFT_AUTO_REPLY_ALLOWLIST": "user-1",
                },
            ), patch.object(
                processing,
                "get_client_automation_decision",
                side_effect=AssertionError("entry kill switch must run first"),
            ) as campaign_gate, patch("requests.post") as graph_post:
                sent = processing.send_reply_in_thread(
                    user_id="user-1",
                    headers={"Authorization": "Bearer token"},
                    body="Hi Alex,\n\nThanks for the update.",
                    current_msg_id="message-1",
                    recipient="broker@example.com",
                    thread_id="thread-1",
                )

            self.assertFalse(sent)
            campaign_gate.assert_not_called()
            graph_post.assert_not_called()
            self.assertEqual(
                processing.send_reply_in_thread.last_outcome,
                "suppressed_by_kill_switch",
            )
            self.assertIn(
                "suppressed_by_kill_switch",
                processing.send_reply_in_thread.last_error,
            )

    def test_mode_change_to_paused_or_invalid_stops_auto_reply_at_send_boundary(self):
        for changed_mode in ("paused", "Live!"):
            with self.subTest(changed_mode=changed_mode):
                os.environ[OUTBOUND_MODE_ENV] = "live"
                os.environ["SITESIFT_AUTO_REPLY_ALLOWLIST"] = "user-1"
                post_urls = []

                def run_request(callback, *_args, **_kwargs):
                    return callback()

                def fake_post(url, **_kwargs):
                    post_urls.append(url)
                    if url.endswith("/createReplyAll"):
                        return _GraphResponse(201, {
                            "id": "reply-draft-1",
                            "toRecipients": [{
                                "emailAddress": {"address": "broker@example.com"}
                            }],
                            "ccRecipients": [],
                        })
                    raise AssertionError(f"kill switch allowed irreversible Graph POST {url}")

                def fake_patch(_url, **_kwargs):
                    os.environ[OUTBOUND_MODE_ENV] = changed_mode
                    return _GraphResponse(204, {})

                current_meta = {
                    "conversationId": "conversation-1",
                    "subject": "RE: 100 Safety Way",
                }
                recipient_result = {
                    "payload": {
                        "toRecipients": [{
                            "emailAddress": {"address": "broker@example.com"}
                        }],
                        "ccRecipients": [],
                    },
                    "skipped": {},
                }

                with patch("email_automation.clients._fs", _direct_send_firestore()), \
                     patch.object(
                         processing,
                         "get_client_automation_decision",
                         return_value=_allow_campaign_decision(),
                     ), \
                     patch(
                         "email_automation.utils.exponential_backoff_request",
                         side_effect=run_request,
                     ), \
                     patch("requests.get", return_value=_GraphResponse(200, current_meta)), \
                     patch("requests.post", side_effect=fake_post), \
                     patch("requests.patch", side_effect=fake_patch), \
                     patch(
                         "email_automation.email._hydrate_reply_all_draft_recipients",
                         side_effect=lambda _headers, draft, base=None: draft,
                     ), \
                     patch(
                         "email_automation.email._source_message_reply_all_fallback",
                         side_effect=lambda draft, _source: draft,
                     ), \
                     patch(
                         "email_automation.email._reviewed_recipient_reply_all_fallback",
                         side_effect=lambda draft, to_emails=None: draft,
                     ), \
                     patch(
                         "email_automation.email._filter_reply_all_draft_recipients",
                         return_value=recipient_result,
                     ), \
                     patch("email_automation.email._delete_graph_reply_draft") as delete_draft:
                    sent = processing.send_reply_in_thread(
                        user_id="user-1",
                        headers={"Authorization": "Bearer token"},
                        body="Hi Alex,\n\nThanks for the update.",
                        current_msg_id="message-1",
                        recipient="broker@example.com",
                        thread_id="thread-1",
                    )

                self.assertFalse(sent)
                self.assertEqual(
                    [url for url in post_urls if url.endswith("/createReplyAll")],
                    ["https://graph.microsoft.com/v1.0/me/messages/message-1/createReplyAll"],
                )
                self.assertFalse(any(url.endswith("/send") for url in post_urls))
                delete_draft.assert_called_once()
                self.assertEqual(
                    processing.send_reply_in_thread.last_outcome,
                    "suppressed_by_kill_switch",
                )


class DirectFollowupKillSwitchTests(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_paused_and_invalid_modes_stop_followup_at_entry(self):
        for mode in ("paused", "Live!"):
            with self.subTest(mode=mode), patch.dict(
                os.environ,
                {OUTBOUND_MODE_ENV: mode},
            ), patch.object(
                followup,
                "get_client_automation_decision",
                side_effect=AssertionError("entry kill switch must run first"),
            ) as campaign_gate, patch("requests.post") as graph_post:
                sent = followup._send_followup_email(
                    user_id="user-1",
                    headers={"Authorization": "Bearer token"},
                    thread_id="thread-1",
                    thread_data={
                        "clientId": CLIENT_ID,
                        "email": ["broker@example.com"],
                    },
                    followup_config={
                        "followUps": [{"message": "Hi Alex, following up."}],
                    },
                    followup_index=0,
                )

            self.assertFalse(sent)
            campaign_gate.assert_not_called()
            graph_post.assert_not_called()
            self.assertIn(
                "suppressed_by_kill_switch",
                followup._send_followup_email.last_error,
            )
            self.assertFalse(followup._send_followup_email.guard_failed_closed)

    def test_mode_change_to_paused_or_invalid_stops_followup_at_send_boundary(self):
        for changed_mode in ("paused", "Live!"):
            with self.subTest(changed_mode=changed_mode):
                os.environ[OUTBOUND_MODE_ENV] = "live"
                post_urls = []
                thread_data = {
                    "clientId": CLIENT_ID,
                    "email": ["broker@example.com"],
                    "contactName": "Alex Broker",
                    "status": "active",
                    "followUpStatus": "waiting",
                }
                followup_config = {
                    "enabled": True,
                    "currentFollowUpIndex": 0,
                    "followUps": [{"message": "Hi Alex, following up."}],
                }

                def run_request(callback, *_args, **_kwargs):
                    return callback()

                def fake_get(url, **_kwargs):
                    if url.endswith("/me/messages"):
                        return _GraphResponse(200, {"value": [{
                            "id": "graph-root",
                            "subject": "100 Safety Way",
                            "conversationId": "conversation-1",
                        }]})
                    raise AssertionError(f"Unexpected Graph GET {url}")

                def fake_post(url, **_kwargs):
                    post_urls.append(url)
                    if url.endswith("/createReplyAll"):
                        return _GraphResponse(201, {
                            "id": "reply-draft-1",
                            "toRecipients": [{
                                "emailAddress": {"address": "broker@example.com"}
                            }],
                            "ccRecipients": [],
                        })
                    raise AssertionError(f"kill switch allowed irreversible Graph POST {url}")

                def fake_patch(_url, **_kwargs):
                    os.environ[OUTBOUND_MODE_ENV] = changed_mode
                    return _GraphResponse(200, {})

                recipient_result = {
                    "payload": {
                        "toRecipients": [{
                            "emailAddress": {"address": "broker@example.com"}
                        }],
                        "ccRecipients": [],
                    },
                    "sentRecipients": ["broker@example.com"],
                }

                with patch.object(followup, "_fs", _direct_send_firestore()), \
                     patch.object(
                         followup,
                         "get_client_automation_decision",
                         return_value=_allow_campaign_decision(),
                     ), \
                     patch.object(
                         followup,
                         "_read_followup_send_precondition",
                         return_value=(thread_data, None),
                     ), \
                     patch.object(
                         followup,
                         "exponential_backoff_request",
                         side_effect=run_request,
                     ), \
                     patch("email_automation.processing.is_contact_opted_out", return_value=None), \
                     patch("requests.get", side_effect=fake_get), \
                     patch("requests.post", side_effect=fake_post), \
                     patch("requests.patch", side_effect=fake_patch), \
                     patch(
                         "email_automation.email._hydrate_reply_all_draft_recipients",
                         side_effect=lambda _headers, draft, base=None: draft,
                     ), \
                     patch(
                         "email_automation.email._source_message_reply_all_fallback",
                         side_effect=lambda draft, _source: draft,
                     ), \
                     patch(
                         "email_automation.email._reviewed_recipient_reply_all_fallback",
                         side_effect=lambda draft, to_emails=None, cc_emails=None: draft,
                     ), \
                     patch(
                         "email_automation.email._filter_reply_all_draft_recipients",
                         return_value=recipient_result,
                     ), \
                     patch("email_automation.email._delete_graph_reply_draft") as delete_draft:
                    sent = followup._send_followup_email(
                        user_id="user-1",
                        headers={"Authorization": "Bearer token"},
                        thread_id="thread-1",
                        thread_data=thread_data,
                        followup_config=followup_config,
                        followup_index=0,
                    )

                self.assertFalse(sent)
                self.assertEqual(
                    [url for url in post_urls if url.endswith("/createReplyAll")],
                    ["https://graph.microsoft.com/v1.0/me/messages/graph-root/createReplyAll"],
                )
                self.assertFalse(any(url.endswith("/send") for url in post_urls))
                delete_draft.assert_called_once()
                self.assertIn(
                    "suppressed_by_kill_switch",
                    followup._send_followup_email.last_error,
                )
                self.assertFalse(followup._send_followup_email.guard_failed_closed)


if __name__ == "__main__":
    unittest.main()
