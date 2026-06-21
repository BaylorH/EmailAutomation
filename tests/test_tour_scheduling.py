import unittest

from email_automation.tour_scheduling import (
    build_schedule_aware_tour_reply,
    evaluate_alternate_tour_time,
    format_tour_time,
    parse_tour_time_minutes,
)


class TourSchedulingTests(unittest.TestCase):
    def test_parse_tour_time_minutes_accepts_common_tour_times(self):
        self.assertEqual(14 * 60 + 15, parse_tour_time_minutes("2:15 PM"))
        self.assertEqual(9 * 60, parse_tour_time_minutes("9 AM"))
        self.assertEqual(12 * 60, parse_tour_time_minutes("noon"))
        self.assertIsNone(parse_tour_time_minutes("after lunch"))

    def test_format_tour_time_uses_readable_ampm(self):
        self.assertEqual("2:15 PM", format_tour_time(14 * 60 + 15))
        self.assertEqual("12:00 PM", format_tour_time(12 * 60))
        self.assertEqual("12:05 AM", format_tour_time(5))

    def test_evaluate_alternate_tour_time_fits_open_window(self):
        decision = evaluate_alternate_tour_time(
            [
                {
                    "id": "current-thread",
                    "propertyAddress": "4402 Rex Rd",
                    "tourInvite": {"arrivalTime": "9:00 AM", "departureTime": "9:30 AM"},
                },
                {
                    "id": "later-thread",
                    "propertyAddress": "505 Matrix Way",
                    "tourInvite": {"arrivalTime": "10:30 AM", "departureTime": "11:00 AM"},
                },
            ],
            "current-thread",
            "9:45 AM",
        )

        self.assertEqual("fits", decision["feasibility"])
        self.assertEqual("9:45 AM", decision["arrivalTime"])
        self.assertEqual("10:15 AM", decision["departureTime"])
        self.assertEqual([], decision["conflicts"])

    def test_evaluate_alternate_tour_time_conflicts_and_suggests_open_slots(self):
        decision = evaluate_alternate_tour_time(
            [
                {
                    "id": "current-thread",
                    "propertyAddress": "4402 Rex Rd",
                    "tourInvite": {"arrivalTime": "9:00 AM", "departureTime": "9:30 AM"},
                },
                {
                    "id": "conflict-thread",
                    "propertyAddress": "1000 Busy St",
                    "tourInvite": {"arrivalTime": "10:00 AM", "departureTime": "10:30 AM"},
                },
                {
                    "id": "second-thread",
                    "propertyAddress": "1115 Busy St",
                    "tourInvite": {"arrivalTime": "11:15 AM", "departureTime": "11:45 AM"},
                },
                {
                    "id": "third-thread",
                    "propertyAddress": "Noon Busy St",
                    "tourInvite": {"arrivalTime": "12:00 PM", "departureTime": "12:30 PM"},
                },
            ],
            "current-thread",
            "10:15 AM",
            buffer_minutes=5,
        )

        self.assertEqual("conflict", decision["feasibility"])
        self.assertEqual("10:15 AM", decision["arrivalTime"])
        self.assertEqual("10:45 AM", decision["departureTime"])
        self.assertEqual(["1000 Busy St"], [item["address"] for item in decision["conflicts"]])
        self.assertIn("12:45 PM", decision["suggestedOpenSlots"])
        self.assertIn("1:30 PM", decision["suggestedOpenSlots"])

    def test_evaluate_alternate_tour_time_needs_review_without_current_stop(self):
        decision = evaluate_alternate_tour_time(
            [
                {
                    "id": "other-thread",
                    "propertyAddress": "1000 Busy St",
                    "tourInvite": {"arrivalTime": "10:00 AM", "departureTime": "10:30 AM"},
                },
            ],
            "missing-thread",
            "10:15 AM",
        )

        self.assertEqual("needs_review", decision["feasibility"])
        self.assertEqual("10:15 AM", decision["arrivalTime"])
        self.assertEqual([], decision["conflicts"])
        self.assertIn("Current tour stop", decision["reviewReason"])

    def test_evaluate_alternate_tour_time_needs_review_when_schedule_incomplete(self):
        decision = evaluate_alternate_tour_time(
            [
                {
                    "id": "current-thread",
                    "scheduleComplete": False,
                    "propertyAddress": "4402 Rex Rd",
                    "tourInvite": {"arrivalTime": "10:00 AM", "departureTime": "10:30 AM"},
                },
            ],
            "current-thread",
            "2:15 PM",
        )

        self.assertEqual("needs_review", decision["feasibility"])
        self.assertIn("could not be loaded", decision["reviewReason"].lower())

    def test_evaluate_alternate_tour_time_honors_persisted_travel_buffer(self):
        decision = evaluate_alternate_tour_time(
            [
                {
                    "id": "current-thread",
                    "propertyAddress": "4402 Rex Rd",
                    "tourInvite": {
                        "arrivalTime": "10:00 AM",
                        "departureTime": "10:30 AM",
                        "travelBufferMinutes": 20,
                    },
                },
                {
                    "id": "other-thread",
                    "propertyAddress": "1000 Busy St",
                    "tourInvite": {
                        "arrivalTime": "11:00 AM",
                        "departureTime": "11:30 AM",
                        "travelBufferMinutes": 20,
                    },
                },
            ],
            "current-thread",
            "10:35 AM",
        )

        self.assertEqual("conflict", decision["feasibility"])
        self.assertEqual(["1000 Busy St"], [item["address"] for item in decision["conflicts"]])

    def test_evaluate_alternate_tour_time_honors_persisted_zero_travel_buffer(self):
        decision = evaluate_alternate_tour_time(
            [
                {
                    "id": "current-thread",
                    "propertyAddress": "4402 Rex Rd",
                    "tourInvite": {
                        "arrivalTime": "10:00 AM",
                        "departureTime": "10:30 AM",
                        "travelBufferMinutes": 0,
                    },
                },
                {
                    "id": "other-thread",
                    "propertyAddress": "1000 Busy St",
                    "tourInvite": {
                        "arrivalTime": "11:00 AM",
                        "departureTime": "11:30 AM",
                        "travelBufferMinutes": 0,
                    },
                },
            ],
            "current-thread",
            "10:30 AM",
        )

        self.assertEqual("fits", decision["feasibility"])
        self.assertEqual([], decision["conflicts"])

    def test_evaluate_alternate_tour_time_carries_tour_date(self):
        decision = evaluate_alternate_tour_time(
            [
                {
                    "id": "current-thread",
                    "propertyAddress": "4402 Rex Rd",
                    "tourInvite": {
                        "tourDate": "2026-06-23",
                        "arrivalTime": "9:00 AM",
                        "departureTime": "9:30 AM",
                    },
                },
            ],
            "current-thread",
            "10:15 AM",
        )

        self.assertEqual("fits", decision["feasibility"])
        self.assertEqual("2026-06-23", decision["tourDate"])

    def test_schedule_aware_reply_includes_tour_date(self):
        body = build_schedule_aware_tour_reply(
            "Lawton",
            "lawton@example.com",
            {
                "propertyAddress": "4402 Rex Rd",
                "tourInvite": {"tourDate": "2026-06-23"},
            },
            {
                "feasibility": "fits",
                "arrivalTime": "2:15 PM",
                "departureTime": "2:45 PM",
            },
        )

        self.assertIn("Tuesday, June 23, 2026 at 2:15 PM works on our end for 4402 Rex Rd.", body)


if __name__ == "__main__":
    unittest.main()
