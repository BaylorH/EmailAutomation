import json
import os
import re
import unittest
from pathlib import Path

os.environ.setdefault("E2E_TEST_MODE", "true")
for candidate_credentials in [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
]:
    if os.path.exists(candidate_credentials):
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", candidate_credentials)
        break

from email_automation import ai_processing, processing


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "jill_readonly_replay_scenarios.json"


def load_replay_fixture():
    with FIXTURE_PATH.open() as handle:
        return json.load(handle)


class JillReadonlyReplayFixtureTests(unittest.TestCase):
    def test_fixture_is_sanitized_for_baylor_bp21_replay(self):
        fixture = load_replay_fixture()
        serialized = json.dumps(fixture).lower()

        self.assertNotIn("jill.ames@mohrpartners.com", serialized)
        self.assertNotIn("megan.enis@transwestern.com", serialized)
        self.assertNotIn("michelle.wogan@transwestern.com", serialized)

        all_emails = set(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", serialized))
        self.assertTrue(all_emails, "fixture should include safe replay emails")
        for email in all_emails:
            with self.subTest(email=email):
                self.assertRegex(email, r"^bp21harrison\+[a-z0-9._%+-]+@gmail\.com$")

        for scenario in fixture["scenarios"]:
            with self.subTest(scenario=scenario["id"]):
                contact_email = scenario["contact"]["email"].lower()
                self.assertTrue(
                    contact_email.startswith("bp21harrison+"),
                    f"fixture replay contact must stay in BP21-safe aliases: {contact_email}",
                )
                self.assertTrue(
                    contact_email.endswith("@gmail.com"),
                    f"fixture replay contact must stay in Gmail safe lane: {contact_email}",
                )

    def test_compound_nonviable_alternative_tour_keeps_all_events(self):
        fixture = load_replay_fixture()
        scenario = next(
            item
            for item in fixture["scenarios"]
            if item["id"] == "office_heavy_original_nonviable_with_alternate_tour"
        )
        proposal = {
            "updates": [],
            "events": list(scenario["proposalEventsBeforeDeterministic"]),
        }

        augmented = ai_processing._augment_events_with_deterministic_signals(
            proposal,
            scenario["conversation"],
        )

        events = augmented["events"]
        event_types = [event.get("type") for event in events]

        for expected_type in scenario["expectedEvents"]:
            self.assertIn(expected_type, event_types)

        self.assertEqual(
            "property_unavailable",
            event_types[0],
            "The original row must be marked non-viable before replacement/tour actions are handled.",
        )

        unavailable_event = next(
            event for event in events if event.get("type") == "property_unavailable"
        )
        self.assertEqual(
            scenario["expectedUnavailableReason"],
            unavailable_event.get("reason"),
        )

        new_property_event = next(
            event for event in events if event.get("type") == "new_property"
        )
        self.assertEqual("27610 Commerce Oaks Dr", new_property_event.get("address"))
        self.assertEqual("bp21harrison+19241@gmail.com", new_property_event.get("email"))

        old_row_terminalized = False
        skipped_after_terminal = []
        allowed_after_terminal = []
        for event in events:
            event_type = event.get("type")
            if processing._should_skip_event_after_original_row_terminalized(
                event_type,
                old_row_became_nonviable=old_row_terminalized,
            ):
                skipped_after_terminal.append(event_type)
                continue
            allowed_after_terminal.append(event_type)
            if event_type == "property_unavailable":
                old_row_terminalized = True

        self.assertIn("new_property", allowed_after_terminal)
        self.assertIn("tour_requested", skipped_after_terminal)


if __name__ == "__main__":
    unittest.main()
