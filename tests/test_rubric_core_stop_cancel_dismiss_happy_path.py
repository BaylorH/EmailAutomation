import os

os.environ.setdefault("E2E_TEST_MODE", "true")

import unittest

from email_automation import campaign_safety


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
        # docs: {doc_id: _FakeDocRef}
        self._docs = docs

    def document(self, name):
        return self._docs.get(name, _FakeDocRef(_FakeSnapshot({}, exists=False)))


class _FakeUsersRoot:
    """Minimal Firestore shim: users/<uid>/clients/<cid>."""

    def __init__(self, user_id, client_id, client_data):
        client_ref = _FakeDocRef(_FakeSnapshot(client_data, exists=True))
        clients_col = _FakeCollection({client_id: client_ref})
        archived_clients_col = _FakeCollection({})
        user_ref = _FakeDocRef(None)
        user_ref.collection = lambda name: (
            clients_col if name == "clients" else archived_clients_col
        )
        self._users = _FakeCollection({user_id: user_ref})
        self._system_config = _FakeCollection({
            "campaignAccess": _FakeDocRef(_FakeSnapshot({
                "automationEnabled": True,
                "allowedUids": [],
            }))
        })

    def collection(self, name):
        if name == "users":
            return self._users
        if name == "systemConfig":
            return self._system_config
        raise AssertionError(f"unexpected collection {name}")


class CoreStopCancelDismissHappyPathTests(unittest.TestCase):
    """Rubric cell: core.stop_cancel_dismiss / happy_path.

    Proves that stopping a client pauses automation so nothing further sends,
    by exercising the real production functions campaign_safety
    .get_client_automation_pause (client-state gate used in the follow-up
    send loop) and campaign_safety.stopped_followup_patch (the patch that
    tears down future-send scheduling).
    """

    def test_stopping_client_pauses_automation_and_disables_further_sends(self):
        user_id = "user-happy"
        client_id = "client-42"
        # A client that the operator has stopped.
        stopped_client = {"status": "stopped", "statusReason": "client_stopped_by_user"}

        fake_fs = _FakeUsersRoot(user_id, client_id, stopped_client)

        # REAL function: fetch client state and decide whether to keep sending.
        paused, reason, client_data = campaign_safety.get_client_automation_pause(
            user_id,
            client_id,
            firestore_client=fake_fs,
        )

        # A stopped client must gate automation off.
        self.assertTrue(paused, "stopped client must pause automation")
        self.assertEqual(reason, "client_stopped_by_user")
        self.assertEqual(client_data.get("status"), "stopped")

        # REAL function: the patch written to the thread when the client is paused.
        patch = campaign_safety.stopped_followup_patch(reason)

        # Nothing further sends: follow-up config is disabled and the next
        # scheduled send is cleared.
        self.assertIs(patch["followUpConfig.enabled"], False)
        self.assertIsNone(patch["followUpConfig.nextFollowUpAt"])
        self.assertEqual(patch["followUpStatus"], "stopped")
        self.assertEqual(patch["status"], "stopped")
        self.assertIs(patch["automationPaused"], True)

    def test_live_client_is_not_paused(self):
        # Guard against a vacuous gate: a live client must NOT be paused.
        user_id = "user-live"
        client_id = "client-7"
        fake_fs = _FakeUsersRoot(
            user_id,
            client_id,
            {"status": "live", "automationPaused": False},
        )

        paused, reason, _ = campaign_safety.get_client_automation_pause(
            user_id,
            client_id,
            firestore_client=fake_fs,
        )
        self.assertFalse(paused, "live client must not pause automation")
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
