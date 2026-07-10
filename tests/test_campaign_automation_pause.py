import unittest

from email_automation import campaign_safety


class _FakeDoc:
    def __init__(self, *, exists, data=None):
        self._exists = exists
        self._data = data or {}

    @property
    def exists(self):
        return self._exists

    def to_dict(self):
        return dict(self._data)


class _FakeRef:
    """Chainable Firestore stub: collection()/document() return self; get() yields the doc."""

    def __init__(self, doc=None, *, raise_on_get=False):
        self._doc = doc
        self._raise_on_get = raise_on_get

    def collection(self, *_args):
        return self

    def document(self, *_args):
        return self

    def get(self):
        if self._raise_on_get:
            raise RuntimeError("firestore transient error")
        return self._doc


class CampaignAutomationPauseTests(unittest.TestCase):
    def test_explicit_automation_pause_blocks_client_processing(self):
        self.assertTrue(
            campaign_safety.is_client_automation_paused({
                "status": "live",
                "automationPaused": True,
                "pausedReason": "admin_incident_pause",
            })
        )

    def test_stopped_or_archived_clients_block_client_processing(self):
        self.assertTrue(campaign_safety.is_client_automation_paused({"status": "stopped"}))
        self.assertTrue(campaign_safety.is_client_automation_paused({"status": "archived"}))

    def test_live_client_without_pause_can_process(self):
        self.assertFalse(campaign_safety.is_client_automation_paused({"status": "live"}))


class CompletedCampaignGatingTests(unittest.TestCase):
    """A campaign auto-marked `completed` (all threads terminal) must stop being
    monitored. `_maybe_mark_client_completed` already writes status=completed;
    the gate must honor it so hand-freezing is no longer required."""

    def test_completed_status_blocks_client_processing(self):
        self.assertTrue(campaign_safety.is_client_automation_paused({"status": "completed"}))

    def test_completed_case_and_whitespace_insensitive(self):
        self.assertTrue(campaign_safety.is_client_automation_paused({"status": "  Completed "}))

    def test_new_campaign_still_monitored(self):
        # The reopen invariant: a freshly-started campaign is never `completed`.
        self.assertFalse(campaign_safety.is_client_automation_paused({"status": "active"}))
        self.assertFalse(campaign_safety.is_client_automation_paused({"status": "live"}))

    def test_get_pause_gates_existing_completed_client(self):
        fs = _FakeRef(_FakeDoc(exists=True, data={"status": "completed"}))
        paused, reason, data = campaign_safety.get_client_automation_pause(
            "uid-1", "client-1", firestore_client=fs
        )
        self.assertTrue(paused)
        self.assertEqual(data.get("status"), "completed")


class MissingClientDocFailsOpenTests(unittest.TestCase):
    """Guardrail: a MISSING client doc must NOT gate the hot inbound/send/followup path.
    `exists==False` is not proof of a deleted campaign — a live thread can legitimately
    reach the gate with a clientId whose doc read returns not-exists. Gating here
    over-gates mainline processing (the tour/nonviable suite proves it). Real
    orphan-gating belongs in a reconcile sweep keyed on an explicit deletion tombstone,
    not this predicate. This test exists so the deliberate fail-open is not naively
    'fixed' back into an over-gate."""

    def test_missing_client_doc_fails_open(self):
        fs = _FakeRef(_FakeDoc(exists=False))
        paused, _reason, _data = campaign_safety.get_client_automation_pause(
            "uid-1", "client-deleted", firestore_client=fs
        )
        self.assertFalse(paused, "missing client doc must fail open (no hot-path over-gate)")

    def test_transient_read_error_fails_closed_and_retryable(self):
        fs = _FakeRef(raise_on_get=True)
        with self.assertRaises(campaign_safety.CampaignStateUnavailableError):
            campaign_safety.get_client_automation_pause(
                "uid-1", "client-1", firestore_client=fs
            )

    def test_no_client_id_is_not_gated(self):
        # Threads with NO clientId hit the downstream email-recovery path.
        paused, _reason, _data = campaign_safety.get_client_automation_pause(
            "uid-1", None, firestore_client=_FakeRef(_FakeDoc(exists=False))
        )
        self.assertFalse(paused)


if __name__ == "__main__":
    unittest.main()
