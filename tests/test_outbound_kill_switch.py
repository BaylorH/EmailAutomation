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


def _fake_requests():
    resp = _make_graph_response()
    fake = MagicMock(name="requests")
    fake.post = Mock(return_value=resp)
    fake.get = Mock(return_value=resp)
    fake.patch = Mock(return_value=resp)
    return fake


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


if __name__ == "__main__":
    unittest.main()
