import unittest
from email_automation import results_feature_gate


class ResultsFeatureAdminGateTests(unittest.TestCase):
    def test_pauses_non_admin_tour_invite_outbox_items(self):
        self.assertTrue(results_feature_gate.should_pause_results_outbox_for_user(
            "normal-user",
            {
                "actionType": "tour_invite",
                "source": "dashboard_tour_planner",
                "assignedEmails": ["broker@example.com"],
            },
        ))

    def test_allows_baylor_and_jill_tour_invite_outbox_items(self):
        for uid in [
            "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
            "C4X3UH1r6QhgP3ivXD1QjyhuGyI2",
        ]:
            self.assertFalse(results_feature_gate.should_pause_results_outbox_for_user(
                uid,
                {"actionType": "tour_invite", "assignedEmails": ["bp21harrison@gmail.com"]},
            ))

    def test_allows_regular_non_results_outbox_items_for_normal_users(self):
        self.assertFalse(results_feature_gate.should_pause_results_outbox_for_user(
            "normal-user",
            {"actionType": "campaign_launch", "assignedEmails": ["broker@example.com"]},
        ))


if __name__ == "__main__":
    unittest.main()
