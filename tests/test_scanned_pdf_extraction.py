import base64
import json
import os
import re
import unittest
from types import SimpleNamespace
from unittest import mock

import fitz


os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import ai_processing, file_handling


SCANNED_PDF_NAME = "fictional-seven-page-raster-brochure.pdf"
SCANNED_FILE_ID = "file-fictional-seven-page-scan"
PAGE_COUNT = 7
EXACT_PAGE_MARKER_LINE_RE = re.compile(r"(?m)^--- Page [1-9]\d* ---$")
NATIVE_TEXT = (
    "Fictional lease notes keep --- Page 12 --- inside this sentence while "
    "supplying enough substantive detail."
)


def _build_image_only_pdf() -> bytes:
    """Return a deterministic PDF whose seven pages contain raster content only."""
    document = fitz.open()
    try:
        for page_number in range(1, PAGE_COUNT + 1):
            page = document.new_page(width=288, height=288)
            pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 64, 64), False)
            pixmap.clear_with(0xEEEEEE)

            accent = (
                20 + (page_number * 23) % 180,
                30 + (page_number * 31) % 170,
                40 + (page_number * 37) % 160,
            )
            for y in range(8, 56):
                for x in range(8, 56):
                    if (x // 8 + y // 8 + page_number) % 2 == 0:
                        pixmap.set_pixel(x, y, accent)

            page.insert_image(
                fitz.Rect(36, 36, 252, 252),
                stream=pixmap.tobytes("png"),
            )

        document.set_metadata({})
        return document.tobytes(garbage=4, deflate=True, no_new_id=True)
    finally:
        document.close()


def _build_native_text_pdf() -> bytes:
    document = fitz.open()
    try:
        page = document.new_page(width=900, height=300)
        remaining_height = page.insert_textbox(
            fitz.Rect(50, 50, 850, 250),
            NATIVE_TEXT,
            fontname="helv",
            fontsize=11,
        )
        if remaining_height < 0:
            raise AssertionError("Native-text fixture did not fit on its page")
        document.set_metadata({})
        return document.tobytes(garbage=4, deflate=True, no_new_id=True)
    finally:
        document.close()


def _substantive_projection_for_assertion(extracted_text: str) -> str:
    return EXACT_PAGE_MARKER_LINE_RE.sub("", extracted_text).strip()


def _column_config() -> dict:
    extraction_fields = [
        "total_sf",
        "rent_sf_yr",
        "ops_ex_sf",
        "ceiling_ht",
        "drive_ins",
        "docks",
        "power",
    ]
    return {
        "mappings": {
            "property_address": "Property Address",
            "city": "City",
            "email": "Email",
            "total_sf": "Total SF",
            "rent_sf_yr": "Rent/SF /Yr",
            "ops_ex_sf": "Ops Ex /SF",
            "ceiling_ht": "Ceiling Ht",
            "drive_ins": "Drive Ins",
            "docks": "Docks",
            "power": "Power",
            "flyer_link": "Flyer / Link",
            "floorplan": "Floorplan",
        },
        "extractionFields": extraction_fields,
        "requiredFields": ["total_sf", "ops_ex_sf", "ceiling_ht"],
        "formulaFields": [],
        "neverRequest": ["rent_sf_yr"],
        "customFields": {},
    }


class ScannedPdfExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scanned_pdf = _build_image_only_pdf()

    def test_real_image_only_pdf_has_visible_raster_pages_and_only_generated_markers(self):
        self.assertEqual(self.scanned_pdf, _build_image_only_pdf())

        document = fitz.open(stream=self.scanned_pdf, filetype="pdf")
        try:
            self.assertEqual(PAGE_COUNT, len(document))
            for page in document:
                self.assertEqual("", page.get_text("text").strip())
                self.assertTrue(page.get_images(full=True))
                rendered = page.get_pixmap(colorspace=fitz.csRGB, alpha=False)
                self.assertTrue(any(channel < 230 for channel in rendered.samples))
        finally:
            document.close()

        extracted_text, page_images = file_handling.extract_pdf_text(
            self.scanned_pdf,
            SCANNED_PDF_NAME,
        )

        self.assertEqual(
            [f"--- Page {page_number} ---" for page_number in range(1, PAGE_COUNT + 1)],
            EXACT_PAGE_MARKER_LINE_RE.findall(extracted_text),
        )
        self.assertEqual("", _substantive_projection_for_assertion(extracted_text))
        self.assertEqual(PAGE_COUNT, len(page_images))
        self.assertTrue(
            all(image.startswith(b"\x89PNG") and len(image) > 100 for image in page_images)
        )

    def test_marker_only_scan_uploads_full_file_and_keeps_five_previews(self):
        with mock.patch.object(
            file_handling,
            "upload_pdf_user_data",
            return_value=SCANNED_FILE_ID,
        ) as upload_pdf:
            processed = file_handling.process_pdf_for_ai(
                self.scanned_pdf,
                SCANNED_PDF_NAME,
            )

        self.assertEqual("openai_upload+images", processed["method"])
        upload_pdf.assert_called_once_with(SCANNED_PDF_NAME, self.scanned_pdf)
        self.assertEqual(SCANNED_FILE_ID, processed["file_id"])
        self.assertEqual(processed["file_id"], processed["id"])
        self.assertEqual("", processed["text"])
        self.assertEqual(5, len(processed["images"]))
        self.assertTrue(
            all(base64.b64decode(image).startswith(b"\x89PNG") for image in processed["images"])
        )

    def test_real_scanned_manifest_reaches_ai_as_file_three_images_and_prompt(self):
        with mock.patch.object(
            file_handling,
            "upload_pdf_user_data",
            return_value=SCANNED_FILE_ID,
        ):
            processed = file_handling.process_pdf_for_ai(
                self.scanned_pdf,
                SCANNED_PDF_NAME,
            )

        response = SimpleNamespace(
            output_text=json.dumps(
                {
                    "updates": [],
                    "events": [],
                    "response_email": None,
                    "notes": "",
                }
            ),
            usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
            id="resp-fictional-scanned-pdf",
        )
        conversation = [
            {
                "direction": "inbound",
                "from": "broker@leasing.example.test",
                "to": ["analyst@tenant.example.test"],
                "subject": "Re: 101 Raster Road",
                "timestamp": "2026-08-16T12:00:00Z",
                "content": "Please review the attached scanned brochure for this property.",
            }
        ]
        header = [
            "Property Address",
            "City",
            "Email",
            "Total SF",
            "Rent/SF /Yr",
            "Ops Ex /SF",
            "Ceiling Ht",
            "Drive Ins",
            "Docks",
            "Power",
            "Flyer / Link",
            "Floorplan",
        ]
        row_values = [
            "101 Raster Road",
            "Exampleton",
            "broker@leasing.example.test",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ]

        with mock.patch.object(
            ai_processing.client.responses,
            "create",
            return_value=response,
        ) as create_response:
            proposal = ai_processing.propose_sheet_updates(
                uid="fictional-user",
                client_id="fictional-client",
                email="broker@leasing.example.test",
                sheet_id="fictional-sheet",
                header=header,
                rownum=3,
                rowvals=row_values,
                thread_id="fictional-thread",
                pdf_manifest=[processed],
                conversation=conversation,
                column_config=_column_config(),
                dry_run=True,
            )

        self.assertIsNotNone(proposal)
        create_response.assert_called_once()
        request_content = create_response.call_args.kwargs["input"][0]["content"]
        self.assertEqual(
            ["input_image", "input_image", "input_image", "input_file", "input_text"],
            [item["type"] for item in request_content],
        )
        self.assertEqual(
            [SCANNED_FILE_ID],
            [item["file_id"] for item in request_content if item["type"] == "input_file"],
        )
        self.assertEqual(
            [
                f"data:image/png;base64,{image}"
                for image in processed["images"][:3]
            ],
            [item["image_url"] for item in request_content if item["type"] == "input_image"],
        )
        self.assertTrue(request_content[-1]["text"])

    def test_native_text_over_threshold_stays_local_without_upload(self):
        native_pdf = _build_native_text_pdf()
        extracted_text, _ = file_handling.extract_pdf_text(
            native_pdf,
            "fictional-native-text.pdf",
        )
        substantive_text = _substantive_projection_for_assertion(extracted_text)
        self.assertGreater(len(substantive_text), 100)
        self.assertIn("keep --- Page 12 --- inside this sentence", substantive_text)

        with mock.patch.object(file_handling, "upload_pdf_user_data") as upload_pdf:
            processed = file_handling.process_pdf_for_ai(
                native_pdf,
                "fictional-native-text.pdf",
            )

        upload_pdf.assert_not_called()
        self.assertEqual("local_extraction", processed["method"])
        self.assertIsNone(processed["file_id"])
        self.assertIsNone(processed["id"])
        self.assertEqual(extracted_text, processed["text"])


if __name__ == "__main__":
    unittest.main()
