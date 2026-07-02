import unittest
import os
from pathlib import Path

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation.column_config import (
    CAMPAIGN_CONTACT_NAME_HEADER_KEYS,
    analyze_launch_name_mapping,
    detect_column_mapping,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class LaunchNameMappingReadinessTests(unittest.TestCase):
    def test_launch_name_mapping_happy_path_accepts_leasing_contact_header(self):
        headers = [
            "Property Address",
            "City",
            "Leasing Company",
            "Leasing Contact",
            "Email",
            "Total SF",
        ]

        mapping = detect_column_mapping(headers, use_ai=False)
        readiness = analyze_launch_name_mapping(headers, mapping)

        self.assertEqual("ready", readiness["status"])
        self.assertEqual("Leasing Contact", readiness["contactNameColumn"])
        self.assertEqual([], readiness["blockingIssues"])

    def test_launch_name_mapping_blocks_generic_name_header_before_placeholder_reaches_outbox(self):
        headers = [
            "Property Address",
            "City",
            "Name",
            "Email",
            "Total SF",
        ]

        mapping = detect_column_mapping(headers, use_ai=False)
        readiness = analyze_launch_name_mapping(headers, mapping)

        self.assertEqual("missing_contact_name", readiness["status"])
        self.assertIn("Name", mapping["mappings"].get("property_name"))
        self.assertTrue(readiness["blockingIssues"])
        self.assertIn("contact name column", readiness["operatorMessage"].lower())
        self.assertIn("[NAME]", readiness["operatorMessage"])

    def test_launch_name_mapping_rejects_stale_manual_mapping_to_generic_name(self):
        headers = [
            "Property Address",
            "City",
            "Name",
            "Email",
        ]
        stale_manual_mapping = {"mappings": {"leasing_contact": "Name"}}

        readiness = analyze_launch_name_mapping(headers, stale_manual_mapping)

        self.assertEqual("unsafe_contact_name", readiness["status"])
        self.assertEqual("Name", readiness["contactNameColumn"])
        self.assertIn("unsafe_contact_name_column", readiness["blockingIssues"])
        self.assertIn("generic columns", readiness["operatorMessage"].lower())

    def test_launch_name_mapping_rejects_missing_mapped_header(self):
        headers = [
            "Property Address",
            "City",
            "Email",
        ]
        stale_manual_mapping = {"mappings": {"leasing_contact": "Leasing Contact"}}

        readiness = analyze_launch_name_mapping(headers, stale_manual_mapping)

        self.assertEqual("missing_contact_name", readiness["status"])
        self.assertEqual("Leasing Contact", readiness["contactNameColumn"])
        self.assertIn(
            "mapped_contact_name_column_missing_from_headers",
            readiness["blockingIssues"],
        )
        self.assertIn("not in the uploaded sheet", readiness["operatorMessage"])

    def test_launch_name_mapping_reports_operator_visible_message_for_ambiguous_person_headers(self):
        headers = [
            "Property Address",
            "City",
            "Leasing Contact",
            "Broker Name",
            "Email",
        ]

        mapping = detect_column_mapping(headers, use_ai=False)
        readiness = analyze_launch_name_mapping(headers, mapping)

        self.assertEqual("ambiguous_contact_name", readiness["status"])
        self.assertEqual(
            ["Leasing Contact", "Broker Name"],
            readiness["candidateContactNameColumns"],
        )
        self.assertTrue(readiness["blockingIssues"])
        self.assertIn("multiple contact name columns", readiness["operatorMessage"].lower())

    def test_launch_name_mapping_uses_same_contact_name_vocabulary_as_send_path(self):
        email_source = (REPO_ROOT / "email_automation" / "email.py").read_text()

        self.assertIn(
            "from .column_config import CAMPAIGN_CONTACT_NAME_HEADER_KEYS",
            email_source,
        )
        self.assertNotIn("CAMPAIGN_CONTACT_NAME_HEADER_KEYS = (", email_source)
        self.assertIn("leasing contact", CAMPAIGN_CONTACT_NAME_HEADER_KEYS)
        self.assertIn("recipient first name", CAMPAIGN_CONTACT_NAME_HEADER_KEYS)

    def test_send_path_resolves_underscore_style_contact_headers(self):
        from email_automation.email import _contact_name_resolution_from_campaign_row

        resolution = _contact_name_resolution_from_campaign_row(
            ["Property Address", "Contact_First_Name", "Email"],
            ["123 Test Way", "Megan", "bp21harrison@gmail.com"],
        )

        self.assertEqual("Megan", resolution["contact_name"])
        self.assertIsNone(resolution["failure_reason"])


if __name__ == "__main__":
    unittest.main()
