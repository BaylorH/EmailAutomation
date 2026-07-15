import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = (
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json")
)

from email_automation import processing


class PdfLinkChangeLogTests(unittest.TestCase):
    def test_record_pdf_link_updates_audits_overflow_column_and_changelog(self):
        sheets_client = MagicMock()
        updates = {"Flyers 2": ["https://drive.google.com/file/d/flyer-two/view"]}

        with patch.object(processing, "_append_ai_meta") as append_ai_meta, patch.object(
            processing,
            "_store_pdf_link_sheet_change",
        ) as store_sheet_change:
            processing._record_pdf_link_updates(
                sheets_client,
                "user-1",
                "client-1",
                "sheet-1",
                ["Property Address", "Flyers", "Flyers 2"],
                3,
                ["123 Test Dr", "https://drive.google.com/file/d/flyer-one/view", ""],
                "thread-1",
                "broker@example.com",
                [{"name": "flyer-two.pdf"}],
                updates,
            )

        append_ai_meta.assert_called_once_with(
            sheets_client,
            "sheet-1",
            3,
            "Flyers 2",
            "https://drive.google.com/file/d/flyer-two/view",
            override=False,
        )
        self.assertEqual(updates, store_sheet_change.call_args.args[-1])

    def test_partial_asset_write_failure_is_visible_and_retryable(self):
        error = processing.AssetLinkWriteError(
            "Flyer / Link",
            RuntimeError("write failed"),
            applied_updates={"Flyers": ["https://drive.google.com/file/d/flyer-one/view"]},
            created_columns=["Flyers 2"],
        )

        with patch.object(processing, "_record_pdf_link_updates") as record_updates, patch.object(
            processing,
            "_record_ai_processing_failure",
            return_value=True,
        ) as record_failure:
            with self.assertRaises(processing.RetryableProcessingError):
                processing._raise_retryable_asset_link_write_failure(
                    error,
                    MagicMock(),
                    "user-1",
                    "client-1",
                    "sheet-1",
                    ["Property Address", "Flyers", "Flyers 2"],
                    3,
                    ["123 Test Dr", "", ""],
                    "thread-1",
                    "message-1",
                    "broker@example.com",
                    [{"name": "flyer-one.pdf"}, {"name": "flyer-two.pdf"}],
                    {},
                )

        record_updates.assert_called_once()
        self.assertEqual(
            {"Flyers": ["https://drive.google.com/file/d/flyer-one/view"]},
            record_updates.call_args.args[-1],
        )
        record_failure.assert_called_once()
        self.assertTrue(record_failure.call_args.kwargs["retryable"])
        self.assertEqual(
            ["Flyers 2"],
            record_failure.call_args.kwargs["metadata"]["createdAssetColumns"],
        )

    def test_pdf_link_record_preserves_existing_values_and_adds_drive_links(self):
        header = ["Property Address", "City", "Flyer / Link", "Floorplan"]
        rowvals = [
            "555 Geocoded Map Dr",
            "Tempe",
            "https://example.com/original-flyer.pdf",
            "",
        ]

        applied = processing._build_pdf_link_sheet_change_applied_record(
            header,
            rowvals,
            {
                "Flyer / Link": ["https://drive.google.com/file/d/flyer/view"],
                "Floorplan": ["https://drive.google.com/file/d/floorplan/view"],
            },
        )

        self.assertEqual(
            applied["applied"],
            [
                {
                    "column": "Flyer / Link",
                    "oldValue": "https://example.com/original-flyer.pdf",
                    "newValue": "https://example.com/original-flyer.pdf\nhttps://drive.google.com/file/d/flyer/view",
                    "confidence": 1.0,
                    "reason": "Broker PDF attachment uploaded to Drive.",
                },
                {
                    "column": "Floorplan",
                    "oldValue": "",
                    "newValue": "https://drive.google.com/file/d/floorplan/view",
                    "confidence": 1.0,
                    "reason": "Broker PDF attachment uploaded to Drive.",
                },
            ],
        )
        self.assertEqual(applied["rowNumber"], None)
        self.assertEqual(applied["source"], "pdf_link_write")

    def test_property_image_record_preserves_manual_source_and_omits_raw_bytes(self):
        header = ["Property Address", "Property Image", "Property Image Source"]
        rowvals = ["555 Geocoded Map Dr", "", ""]

        applied = processing._build_property_image_sheet_change_applied_record(
            header,
            rowvals,
            {
                "Property Image": ["https://drive.google.com/uc?export=view&id=image-id"],
                "Property Image Source": ["Broker flyer preview: 555 Geocoded Map Dr Flyer.pdf, page 1"],
            },
            row_number=7,
        )

        self.assertEqual(
            applied["applied"],
            [
                {
                    "column": "Property Image",
                    "oldValue": "",
                    "newValue": "https://drive.google.com/uc?export=view&id=image-id",
                    "confidence": 1.0,
                    "reason": "Broker flyer preview image resolved from attachment.",
                },
                {
                    "column": "Property Image Source",
                    "oldValue": "",
                    "newValue": "Broker flyer preview: 555 Geocoded Map Dr Flyer.pdf, page 1",
                    "confidence": 1.0,
                    "reason": "Broker flyer preview image resolved from attachment.",
                },
            ],
        )
        serialized = repr(applied)
        self.assertNotIn("base64", serialized.lower())
        self.assertEqual(applied["rowNumber"], 7)
        self.assertEqual(applied["source"], "property_image_write")


if __name__ == "__main__":
    unittest.main()
