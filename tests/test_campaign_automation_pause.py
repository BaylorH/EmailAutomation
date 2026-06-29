import unittest

from email_automation import campaign_safety


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


if __name__ == "__main__":
    unittest.main()
