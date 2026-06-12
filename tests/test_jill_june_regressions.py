import unittest
import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation import ai_processing, processing


class JillJuneRegressionTests(unittest.TestCase):
    def test_no_longer_represent_property_adds_unavailable_event(self):
        proposal = {"updates": [], "events": []}
        conversation = [
            {
                "direction": "inbound",
                "content": "Sorry for the delay, we no longer represent this property.",
            }
        ]

        augmented = ai_processing._augment_events_with_deterministic_signals(
            proposal,
            conversation,
        )

        self.assertIn(
            {"type": "property_unavailable", "reason": "no_longer_represented"},
            augmented["events"],
        )

    def test_no_space_and_signed_loi_adds_unavailable_event(self):
        proposal = {"updates": [], "events": []}
        conversation = [
            {
                "direction": "inbound",
                "content": "We do not have any space available and already have a signed LOI.",
            }
        ]

        augmented = ai_processing._augment_events_with_deterministic_signals(
            proposal,
            conversation,
        )

        self.assertEqual("property_unavailable", augmented["events"][0]["type"])
        self.assertEqual("signed_loi", augmented["events"][0]["reason"])

    def test_existing_unavailable_event_is_not_duplicated(self):
        proposal = {
            "updates": [],
            "events": [{"type": "property_unavailable", "reason": "model"}],
        }
        conversation = [
            {
                "direction": "inbound",
                "content": "We no longer represent this property.",
            }
        ]

        augmented = ai_processing._augment_events_with_deterministic_signals(
            proposal,
            conversation,
        )

        self.assertEqual(1, len(augmented["events"]))
        self.assertEqual("model", augmented["events"][0]["reason"])

    def test_new_property_event_defers_pdf_links_from_current_row(self):
        events = [{"type": "new_property", "address": "Elam Business Park"}]

        self.assertTrue(processing._has_new_property_path(events))


if __name__ == "__main__":
    unittest.main()
