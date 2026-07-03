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
        user_ref = _FakeDocRef(None)
        user_ref.collection = lambda name: clients_col
        self._users = _FakeCollection({user_id: user_ref})

    def collection(self, name):
        assert name == "users"
        return self._users


class _FakeOutboxDocRef:
    """Stand-in for the outbox item's Firestore document reference."""

    def __init__(self, doc_id):
        self.id = doc_id


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


if __name__ == "__main__":
    unittest.main()
