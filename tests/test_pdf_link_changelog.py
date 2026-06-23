import os
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = (
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json"
)

from email_automation import processing


class PdfLinkChangeLogTests(unittest.TestCase):
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
