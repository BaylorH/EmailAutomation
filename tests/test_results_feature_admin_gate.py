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

    def test_results_admin_visibility_is_separate_from_tour_email_send_permission(self):
        self.assertTrue(results_feature_gate.is_results_feature_admin_user(
            "C4X3UH1r6QhgP3ivXD1QjyhuGyI2",
        ))
        self.assertTrue(results_feature_gate.should_pause_results_outbox_for_user(
            "C4X3UH1r6QhgP3ivXD1QjyhuGyI2",
            {"actionType": "tour_invite", "assignedEmails": ["broker@example.com"]},
        ))

    def test_allows_only_baylor_test_uid_for_tour_invite_outbox_items(self):
        self.assertFalse(results_feature_gate.should_pause_results_outbox_for_user(
            "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
            {"actionType": "tour_invite", "assignedEmails": ["bp21harrison@gmail.com"]},
        ))

    def test_allows_regular_non_results_outbox_items_for_normal_users(self):
        self.assertFalse(results_feature_gate.should_pause_results_outbox_for_user(
            "normal-user",
            {"actionType": "campaign_launch", "assignedEmails": ["broker@example.com"]},
        ))


if __name__ == "__main__":
    unittest.main()
