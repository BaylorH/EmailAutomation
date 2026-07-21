import os
import unittest


os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "service-account.json",
    ),
)

from email_automation import ai_processing, processing


def _conversation(body):
    return [{"direction": "inbound", "content": body}]


class JillLiveCampaignRegressionTests(unittest.TestCase):
    def test_explicit_opex_wins_over_earlier_nnn_rent_basis(self):
        examples = {
            (
                "We are marketing the Units at $14.00 psf NNN, "
                "OPEX approximately $4.00 psf."
            ): "4.00",
            (
                "The lease price is $15.50 psf nnn and estimated "
                "Taxes & CAM are $3.00 psf."
            ): "3.00",
        }

        for text, expected in examples.items():
            with self.subTest(text=text):
                self.assertEqual(
                    expected,
                    ai_processing._extract_ops_ex_sf_from_text(text),
                )

    def test_rampable_dock_is_not_a_terminal_drive_in_mismatch(self):
        proposal = {
            "updates": [],
            "events": [
                {"type": "property_unavailable", "reason": "requirements_mismatch"}
            ],
            "response_email": "We'll cross this one off.",
        }
        conversation = _conversation(
            "No drive in door. 1 loading dock. The loading dock can be ramped "
            "for drive in. The unit is 7753 sf."
        )

        result = ai_processing._augment_events_with_deterministic_signals(
            proposal,
            conversation,
            target_anchor="102 Iron Mountain Rd, Mine Hill",
        )

        event_types = [event.get("type") for event in result["events"]]
        self.assertNotIn("property_unavailable", event_types)
        self.assertIn("needs_user_input", event_types)
        self.assertIsNone(result["response_email"])

    def test_rampable_dock_does_not_hide_separate_office_mismatch(self):
        self.assertTrue(
            ai_processing._looks_like_requirements_mismatch_nonviable(
                "The space is too office-heavy for the client. The dock could be "
                "ramped for drive-in access, but there is almost no warehouse."
            )
        )

    def test_matching_route_address_brochure_is_not_treated_as_competing(self):
        proposal = {
            "updates": [{"column": "Total SF", "value": "7500"}],
            "events": [],
            "response_email": None,
        }
        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation("We have the current building available; brochure attached."),
            "3344 S Carolina 51, Fort Mill",
            [{
                "name": "3344 SC-51 brochure.pdf",
                "text": "3344 S Carolina 51, Fort Mill - 7,500 SF",
            }],
        )

        self.assertEqual([{"column": "Total SF", "value": "7500"}], result["updates"])

    def test_competing_multi_property_brochure_escalates_instead_of_writing_current_row(self):
        proposal = {
            "updates": [
                {"column": "Rent/SF /Yr", "value": "15.75", "confidence": 0.72},
                {"column": "Total SF", "value": "9500", "confidence": 0.92},
            ],
            "events": [{"type": "tour_requested", "question": "Glad to show."}],
            "response_email": "Thanks.",
        }
        brochure = {
            "name": "AUSTIN BUSINESS PARK NEW.pdf",
            "text": (
                "Austin Business Park 3336 SC-51 Fort Mill. "
                "Building 1: 9,500 SF, $18 PSF. "
                "Building 2: 3,000 SF, $13 PSF. "
                "Building 3: 7,500 SF, $15 PSF."
            ),
        }

        result = ai_processing._suppress_competing_attachment_updates(
            proposal,
            _conversation(
                "I have 2 buildings here: 7,500 SF and 9,500 SF. "
                "Brochure with rent info attached."
            ),
            "3344 S Carolina 51, Fort Mill",
            [brochure],
        )

        self.assertEqual([], result["updates"])
        self.assertIn(
            "multi_property_attachment",
            [event.get("reason") for event in result["events"]],
        )
        self.assertIsNone(result["response_email"])

        current, _ = processing._partition_property_attachments(
            [brochure],
            current_anchor="3344 S Carolina 51, Fort Mill",
            events=result["events"],
        )
        self.assertEqual([], current)

    def test_replacement_only_reply_cannot_update_original_row(self):
        proposal = {
            "updates": [
                {"column": "Total SF", "value": "8000", "confidence": 0.72},
                {"column": "Drive Ins", "value": "1", "confidence": 0.78},
                {"column": "Ceiling Ht", "value": "12", "confidence": 0.86},
            ],
            "events": [
                {
                    "type": "new_property",
                    "address": "48 Richboynton Road",
                    "city": "Dover",
                }
            ],
        }

        result = ai_processing._suppress_cross_property_current_row_updates(
            proposal,
            _conversation(
                "I have ~8K S.F. at 48 Richboynton Road in Dover. It has a "
                "10' drive-in door. Ceilings are 14' to the deck but only 12' clear."
            ),
            "53 Richboynton Rd, Dover",
        )

        self.assertEqual([], result["updates"])

    def test_mixed_reply_keeps_current_property_updates(self):
        proposal = {
            "updates": [
                {"column": "Total SF", "value": "7200", "confidence": 0.95},
                {"column": "Drive Ins", "value": "3", "confidence": 0.9},
            ],
            "events": [
                {
                    "type": "new_property",
                    "address": "[TBD] Sterling Plaza Phase II",
                    "city": "Ponte Vedra, FL",
                }
            ],
        }

        result = ai_processing._suppress_cross_property_current_row_updates(
            proposal,
            _conversation(
                "Yes, this space meets your criteria and is available for sale. "
                "It is 7,200 sf and has three grade-level doors. We also have a "
                "newly built park adjacent to this location called Sterling Plaza Phase II."
            ),
            "200 Sterling Plaza Dr, Town Of Nocatee",
        )

        self.assertEqual(2, len(result["updates"]))

    def test_sterling_attachments_are_partitioned_by_property(self):
        permit = {
            "name": "2121 American Wall Beds Co PERMIT REV2 11 18 22.pdf",
            "text": "ROF TUO DLIUB TNANET .OC DEB LLAW NACIREMA RD AZALP GNILRETS 002",
        }
        alternate_flyer = {
            "name": "STERLING PLAZA PHASE II FLYER UPDATE 5.8.pdf",
            "text": "STERLING PLAZA PHASE II PONTE VEDRA, FL - 2,400 SF units",
        }
        events = [
            {
                "type": "new_property",
                "address": "[TBD] newly built park adjacent to Sterling Plaza "
                "(FutureFlex / Sterling Plaza Phase II)",
                "city": "Ponte Vedra, FL",
            }
        ]

        current, by_event = processing._partition_property_attachments(
            [permit, alternate_flyer],
            current_anchor="200 Sterling Plaza Dr, Town Of Nocatee",
            events=events,
        )

        self.assertEqual([permit], current)
        self.assertEqual([alternate_flyer], by_event[0])

    def test_replacement_floorplan_does_not_land_on_original_row(self):
        floorplan = {
            "name": "48RichboyntonRoad1stFloor8910.pdf",
            "text": "48 Richboynton Road - 1st Floor - 8,910 S.F.",
        }
        events = [
            {
                "type": "new_property",
                "address": "48 Richboynton Road",
                "city": "Dover",
            }
        ]

        current, by_event = processing._partition_property_attachments(
            [floorplan],
            current_anchor="53 Richboynton Rd, Dover",
            events=events,
        )

        self.assertEqual([], current)
        self.assertEqual([floorplan], by_event[0])

    def test_target_brochure_ignores_brokerage_office_address(self):
        brochure = {
            "name": "105 W Dewey Ave, Bldg B, 9&10, Wharton_Brochure.pdf",
            "text": (
                "105 W Dewey Ave FOR LEASE. 8,000 SF. "
                "Garden State Realty, 204 Passaic Ave, Fairfield."
            ),
        }

        current, by_event = processing._partition_property_attachments(
            [brochure],
            current_anchor="105 W Dewey Ave, Wharton",
            events=[],
        )

        self.assertEqual([brochure], current)
        self.assertEqual([], by_event)

    def test_requirements_mismatch_has_truthful_terminal_label(self):
        event = {"type": "property_unavailable", "reason": "requirements_mismatch"}

        self.assertEqual(
            "requirements_mismatch",
            processing._nonviable_status_reason(event),
        )
        comment = processing._build_property_unavailable_comment(
            "07/21/2026",
            "requirements_mismatch",
            [event],
        )
        self.assertIn("does not meet client requirements", comment.lower())
        self.assertNotIn("marked unavailable", comment.lower())

    def test_requirements_mismatch_stops_followups_before_sheet_move(self):
        events = [{"type": "property_unavailable", "reason": "requirements_mismatch"}]

        patch = processing._pending_nonviable_followup_patch(
            events,
            row_anchor="111 Canfield Ave, Randolph",
            message_text="The units do not have a drive in door.",
        )

        self.assertEqual("stopped", patch["followUpStatus"])
        self.assertIsNone(patch["followUpConfig.nextFollowUpAt"])
        self.assertEqual("requirements_mismatch", patch["pendingTerminalReason"])


if __name__ == "__main__":
    unittest.main()
