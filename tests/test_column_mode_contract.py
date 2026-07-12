import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import ai_processing, processing
from email_automation.column_config import (
    get_default_column_config,
    get_default_mode_for_canonical,
)


class CanonicalColumnModeDefaultsTests(unittest.TestCase):
    def test_rent_is_ask_required_and_flyer_is_note(self):
        config = get_default_column_config()

        self.assertEqual("ask_required", get_default_mode_for_canonical("rent_sf_yr"))
        self.assertIn("rent_sf_yr", config["requiredFields"])
        self.assertNotIn("rent_sf_yr", config["neverRequest"])

        self.assertEqual("note", get_default_mode_for_canonical("flyer_link"))
        self.assertNotIn("flyer_link", config["requiredFields"])
        self.assertIn("flyer_link", config["neverRequest"])


class ColumnConfigFailClosedTests(unittest.TestCase):
    def _propose(self, column_config, extraction_fields=None):
        return ai_processing.propose_sheet_updates(
            "uid",
            "client",
            "broker@example.com",
            "sheet",
            ["Property Address", "Rent/SF /Yr", "Flyer / Link"],
            3,
            ["123 Main St", "", ""],
            "thread",
            conversation=[
                {
                    "direction": "inbound",
                    "from": "broker@example.com",
                    "content": "The space is available.",
                }
            ],
            column_config=column_config,
            extraction_fields=extraction_fields,
            dry_run=True,
        )

    def test_missing_or_malformed_config_never_reaches_openai(self):
        malformed_configs = [
            None,
            {},
            {"mappings": []},
            {
                "mappings": {"rent_sf_yr": "Rent/SF /Yr"},
                "requiredFields": "rent_sf_yr",
                "formulaFields": [],
                "neverRequest": [],
                "customFields": {},
            },
        ]

        for column_config in malformed_configs:
            with self.subTest(column_config=column_config), patch.object(
                ai_processing.client.responses,
                "create",
            ) as create:
                proposal = self._propose(column_config)

                self.assertIsNone(proposal)
                create.assert_not_called()

    def test_duplicate_extraction_fields_drift_fails_closed(self):
        with patch.object(ai_processing.client.responses, "create") as create:
            proposal = self._propose(
                get_default_column_config(),
                extraction_fields=["flyer_link"],
            )

        self.assertIsNone(proposal)
        create.assert_not_called()


class BrokerReplyColumnModeValidationTests(unittest.TestCase):
    def setUp(self):
        self.config = get_default_column_config()
        self.config["customFields"] = {
            "Broker Context": {"mode": "note", "description": "Context only"},
            "Internal Score": {"mode": "skip", "description": "Ignored"},
        }

    def test_accepts_request_for_missing_ask_field_only(self):
        body = "Thanks for the details. Could you also confirm the asking rent?"

        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )

    def test_identity_column_words_do_not_count_as_skip_requests(self):
        body = "Could you confirm the asking rent for this city property?"

        self.assertTrue(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )

    def test_rejects_allowed_ask_mixed_with_note_field(self):
        body = (
            "Could you confirm the asking rent and also send the flyer or brochure?"
        )

        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )

    def test_rejects_allowed_ask_mixed_with_custom_skip_field(self):
        body = "Could you confirm the asking rent and your internal score?"

        self.assertFalse(
            processing._response_mentions_missing_fields(
                body,
                ["Rent/SF /Yr"],
                self.config,
            )
        )


if __name__ == "__main__":
    unittest.main()
