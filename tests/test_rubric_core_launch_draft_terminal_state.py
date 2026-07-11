import os

os.environ.setdefault("E2E_TEST_MODE", "true")

import unittest
from unittest import mock

import google.cloud.firestore as _gcf


class _FsForImport:
    """Stand-in returned by firestore.Client() so email_automation.clients is
    importable offline (no ADC). The real datastore boundary is faked per-call
    via mock.patch on email_automation.clients._fs; any accidental use here
    fails loudly instead of hitting real Firestore."""

    def __getattr__(self, name):
        raise AssertionError(
            f"real Firestore access '{name}' during test -- boundary not faked"
        )


# clients.py runs `_fs = firestore.Client()` at import time; stub it first.
_gcf.Client = lambda *a, **k: _FsForImport()

from email_automation import email as email_mod
from email_automation.campaign_safety import CampaignAutomationDecision


class _FakeSnapshot:
    def __init__(self, data, exists=True):
        self._data = data
        self.exists = exists

    def to_dict(self):
        return dict(self._data)


class _FakeDocRef:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def get(self):
        return self._snapshot


class _FakeCollection:
    def __init__(self, docs):
        self._docs = docs

    def document(self, name):
        return self._docs.get(name, _FakeDocRef(_FakeSnapshot({}, exists=False)))


class _FakeFirestore:
    """Minimal Firestore shim: users/<uid>/clients/<cid> -> client doc."""

    def __init__(self, user_id, client_id, client_data):
        client_ref = _FakeDocRef(_FakeSnapshot(client_data, exists=True))
        clients_col = _FakeCollection({client_id: client_ref})
        archived_clients_col = _FakeCollection({})
        user_ref = _FakeDocRef(None)
        user_ref.collection = lambda name: (
            clients_col if name == "clients" else archived_clients_col
        )
        self._users = _FakeCollection({user_id: user_ref})

    def collection(self, name):
        if name == "users":
            return self._users
        if name == "systemConfig":
            return _FakeCollection({
                "campaignAccess": _FakeDocRef(_FakeSnapshot({
                    "automationEnabled": True,
                    "allowedUids": [],
                }, exists=True))
            })
        raise AssertionError(f"Unexpected collection: {name}")


class _FakeOutboxDocRef:
    """Stand-in for the outbox item's Firestore document reference."""

    def __init__(self, doc_id):
        self.id = doc_id
        self.set_calls = []

    def set(self, data, merge=False):
        self.set_calls.append((data, merge))


class _FakeGraphResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class CoreLaunchDraftTerminalStateTests(unittest.TestCase):
    """Rubric cell: core.launch_draft / terminal_state.

    Proves that a campaign-launch outbox destined for a client whose thread
    state is terminal/stopped is dropped BEFORE send, by exercising the real
    production send-path gate email._pause_client_outbox_item_if_needed
    (which runs inside _send_single_outbox_item just before the send call).
    The only patched surfaces are the datastore boundaries: the Firestore
    client that reports client state, and _move_to_dead_letter (the drop
    sink). The gating decision itself is real production code.
    """

    USER_ID = "user-launch"
    CLIENT_ID = "client-42"

    def _launch_outbox_data(self):
        # A genuine campaign-launch outbox payload (source recognized by the
        # real _is_campaign_launch_outbox classifier).
        return {
            "clientId": self.CLIENT_ID,
            "source": "dashboard_new_campaign",
            "assignedEmails": ["broker@example.com"],
            "subject": "123 Main St",
            "script": "Hi, following up about the listing.",
        }

    def _run_gate(self, client_data):
        """Exercise the REAL gate against a given client state.

        Returns (dropped, dead_letter_mock).
        """
        data = self._launch_outbox_data()
        # Sanity: this really is a launch outbox on the real classifier.
        self.assertTrue(
            email_mod._is_campaign_launch_outbox(data),
            "fixture must be a real campaign-launch outbox",
        )

        fake_fs = _FakeFirestore(self.USER_ID, self.CLIENT_ID, client_data)
        doc_ref = _FakeOutboxDocRef("outbox-1")

        # Patch ONLY the datastore boundaries. get_client_automation_pause
        # (called with no firestore_client) reads email_automation.clients._fs,
        # and _move_to_dead_letter is the terminal drop sink. The gate logic
        # under test is untouched real code.
        with mock.patch("email_automation.clients._fs", fake_fs), \
             mock.patch.object(email_mod, "_move_to_dead_letter") as dead_letter:
            dropped = email_mod._pause_client_outbox_item_if_needed(
                self.USER_ID, doc_ref, data
            )
        return dropped, dead_letter

    def test_launch_outbox_for_terminal_client_is_dropped_before_send(self):
        # Client/thread is in a terminal (stopped) state.
        dropped, dead_letter = self._run_gate(
            {"status": "stopped", "statusReason": "client_stopped_by_user"}
        )

        # The launch outbox must be dropped before any send happens...
        self.assertTrue(
            dropped,
            "launch outbox for a terminal/stopped client must be dropped before send",
        )
        # ...and the drop must route it out of the send queue (dead-letter),
        # never to the mailer.
        dead_letter.assert_called_once()
        _uid, _ref, _data, reason = dead_letter.call_args.args
        self.assertIn("client_stopped_by_user", reason)

    def test_launch_outbox_for_live_client_is_not_dropped(self):
        # NEGATIVE CONTROL: identical launch outbox, but a live (non-terminal)
        # client. The gate must NOT fire, so the item stays queued for send.
        dropped, dead_letter = self._run_gate({"status": "live"})

        self.assertFalse(
            dropped,
            "launch outbox for a live client must NOT be dropped by the terminal gate",
        )
        dead_letter.assert_not_called()

    def test_launch_outbox_for_maintenance_pause_stays_retryable(self):
        data = self._launch_outbox_data()
        doc_ref = _FakeOutboxDocRef("outbox-maintenance")
        decision = CampaignAutomationDecision(
            state="blocked",
            reason="campaign_maintenance",
            client_data={"status": "live", "automationPaused": True},
            metadata={"terminal": False, "stopKind": "maintenance_pause"},
        )

        with mock.patch.object(
            email_mod,
            "get_client_automation_decision",
            return_value=decision,
            create=True,
        ), mock.patch.object(email_mod, "_move_to_dead_letter") as dead_letter:
            blocked = email_mod._pause_client_outbox_item_if_needed(
                self.USER_ID, doc_ref, data
            )

        self.assertTrue(blocked)
        dead_letter.assert_not_called()
        payload, merge = doc_ref.set_calls[-1]
        self.assertTrue(merge)
        self.assertEqual("queued", payload["status"])
        self.assertEqual("blocked", payload["automationSuppressedState"])
        self.assertEqual("campaign_maintenance", payload["automationSuppressedReason"])
        self.assertIsNone(payload["processingBy"])
        self.assertNotIn("attempts", payload)

    def test_launch_outbox_for_unknown_state_stays_retryable(self):
        data = self._launch_outbox_data()
        doc_ref = _FakeOutboxDocRef("outbox-unknown")
        decision = CampaignAutomationDecision(
            state="unknown",
            reason="client_automation_state_read_error",
            client_data={},
            metadata={"terminal": False, "stopKind": "none"},
        )

        with mock.patch.object(
            email_mod,
            "get_client_automation_decision",
            return_value=decision,
            create=True,
        ), mock.patch.object(email_mod, "_move_to_dead_letter") as dead_letter:
            blocked = email_mod._pause_client_outbox_item_if_needed(
                self.USER_ID, doc_ref, data
            )

        self.assertTrue(blocked)
        dead_letter.assert_not_called()
        payload, _merge = doc_ref.set_calls[-1]
        self.assertEqual("queued", payload["status"])
        self.assertEqual("unknown", payload["automationSuppressedState"])
        self.assertNotIn("attempts", payload)

    def test_indexed_send_rechecks_campaign_stop_immediately_before_graph_send(self):
        decision = CampaignAutomationDecision(
            state="blocked",
            reason="client_stopped_by_user",
            client_data={"status": "stopping"},
            metadata={"terminal": True, "stopKind": "terminal_stop"},
        )
        posts = []

        def fake_post(url, **_kwargs):
            posts.append(url)
            if url.endswith("/me/messages"):
                return _FakeGraphResponse(201, {"id": "draft-stop"})
            if url.endswith("/send"):
                raise AssertionError("stopped campaign must not reach Graph /send")
            return _FakeGraphResponse(500)

        with mock.patch.object(
            email_mod, "get_client_automation_decision", return_value=decision
        ), mock.patch.object(
            email_mod, "exponential_backoff_request", side_effect=lambda func, **_kwargs: func()
        ), mock.patch.object(
            email_mod.requests, "post", side_effect=fake_post
        ), mock.patch.object(
            email_mod.requests,
            "get",
            return_value=_FakeGraphResponse(200, {
                "internetMessageId": "<draft-stop@example.test>",
                "conversationId": "conversation-stop",
                "subject": "123 Main St",
            }),
        ), mock.patch.object(
            email_mod, "_delete_graph_reply_draft"
        ) as delete_draft, mock.patch(
            "email_automation.processing.is_contact_opted_out", return_value=None
        ):
            result = email_mod.send_and_index_email(
                self.USER_ID,
                {"Authorization": "Bearer token"},
                "Hi Broker,\n\nCan you confirm availability?",
                ["bp21harrison@gmail.com"],
                client_id_or_none=self.CLIENT_ID,
                subject_override="123 Main St",
            )

        self.assertTrue(result["campaignAutomationSuppressed"])
        self.assertTrue(result["campaignAutomationTerminal"])
        self.assertFalse(any(url.endswith("/send") for url in posts))
        delete_draft.assert_called_once()

    def test_mid_batch_maintenance_suppression_retries_only_unsent_recipients(self):
        data = {
            **self._launch_outbox_data(),
            "assignedEmails": ["sent@example.com", "waiting@example.com"],
            "sentRecipients": [],
        }
        doc_ref = _FakeOutboxDocRef("outbox-partial")
        send_result = {
            "sent": ["sent@example.com"],
            "errors": {"waiting@example.com": "campaign_maintenance"},
            "campaignAutomationSuppressed": True,
            "campaignAutomationState": "blocked",
            "campaignAutomationReason": "campaign_maintenance",
            "campaignAutomationTerminal": False,
        }

        handled = email_mod._handle_suppressed_outbox_send_result(
            self.USER_ID,
            doc_ref,
            data,
            send_result,
        )

        self.assertTrue(handled)
        payload, merge = doc_ref.set_calls[-1]
        self.assertTrue(merge)
        self.assertEqual(["waiting@example.com"], payload.get("assignedEmails"))
        self.assertEqual(["sent@example.com"], payload.get("sentRecipients"))
        self.assertTrue(payload.get("partialSend"))
        self.assertNotIn("attempts", payload)


if __name__ == "__main__":
    unittest.main()
