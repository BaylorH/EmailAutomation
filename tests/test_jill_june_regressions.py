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

    def test_unavailable_event_without_address_does_not_apply_to_replacement_row(self):
        event = {"type": "property_unavailable", "reason": "fully_leased"}
        message_text = (
            "404 Replacement Signal Ave is fully leased. "
            "A similar option is 414 Alternate Signal Ave in Las Vegas. "
            "Following up with the package details for 414 Alternate Signal Ave: "
            "19,250 SF, asking $1.05/SF/month NNN."
        )

        self.assertFalse(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="414 Alternate Signal Ave, Las Vegas",
                message_text=message_text,
            )
        )

    def test_unavailable_event_without_address_applies_when_current_row_is_named_unavailable(self):
        event = {"type": "property_unavailable", "reason": "fully_leased"}
        message_text = (
            "404 Replacement Signal Ave is fully leased. "
            "A similar option is 414 Alternate Signal Ave in Las Vegas."
        )

        self.assertTrue(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="404 Replacement Signal Ave, Las Vegas",
                message_text=message_text,
            )
        )

    def test_unavailable_event_with_different_address_does_not_apply_to_current_row(self):
        event = {
            "type": "property_unavailable",
            "address": "404 Replacement Signal Ave",
            "city": "Las Vegas",
        }

        self.assertFalse(
            processing._property_unavailable_event_applies_to_row(
                event,
                row_anchor="414 Alternate Signal Ave, Las Vegas",
                message_text="404 Replacement Signal Ave is fully leased.",
            )
        )


if __name__ == "__main__":
    unittest.main()
