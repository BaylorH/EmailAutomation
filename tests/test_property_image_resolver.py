import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)


class PropertyImageResolverTests(unittest.TestCase):
    def test_download_candidates_allow_drive_and_dropbox_but_skip_listing_pages(self):
        from email_automation.property_images import build_download_candidate

        drive = build_download_candidate(
            "https://drive.google.com/file/d/abc123/view?usp=drivesdk",
            filename_hint="4402 Rex Rd Flyer.pdf",
        )
        dropbox = build_download_candidate(
            "https://www.dropbox.com/scl/fi/key/Lease-Flyer.pdf?rlkey=one&dl=0",
            filename_hint="1419 Atlantis Drive Flyer.pdf",
        )
        loopnet = build_download_candidate(
            "https://www.loopnet.com/Listing/902-910-Gemini-Houston-TX/40231241/",
            filename_hint="912 Gemini listing",
        )

        self.assertEqual(
            "https://drive.google.com/uc?export=download&id=abc123",
            drive["downloadUrl"],
        )
        self.assertEqual("google_drive_pdf", drive["sourceType"])
        self.assertIn("4402 Rex Rd Flyer.pdf", drive["sourceLabel"])
        self.assertEqual(
            "https://www.dropbox.com/scl/fi/key/Lease-Flyer.pdf?rlkey=one&dl=1",
            dropbox["downloadUrl"],
        )
        self.assertEqual("dropbox_pdf", dropbox["sourceType"])
        self.assertIsNone(loopnet)

    def test_manifest_candidate_writes_safe_property_image_columns_without_raw_image_bytes(self):
        from email_automation.property_images import (
            build_property_image_sheet_updates,
            select_property_image_candidate,
        )

        manifest = [
            {
                "name": "4402 Rex Rd Flyer.pdf",
                "drive_link": "https://drive.google.com/file/d/pdf-id/view",
                "images": ["RAW_BASE64_SHOULD_NOT_LEAK"],
                "property_image_url": "https://drive.google.com/uc?export=view&id=image-id",
                "property_image_source": "Broker flyer preview: 4402 Rex Rd Flyer.pdf, page 1",
                "property_image_meta": {
                    "pageNumber": 1,
                    "contentType": "image/png",
                    "byteCount": 12345,
                    "sha256": "abc123",
                },
            }
        ]

        candidate = select_property_image_candidate(manifest)
        updates = build_property_image_sheet_updates(
            ["Property Address", "City", "Property Image", "Property Image Source"],
            ["4402 Rex Rd", "Friendswood", "", ""],
            candidate,
        )

        self.assertEqual(
            {
                "Property Image": ["https://drive.google.com/uc?export=view&id=image-id"],
                "Property Image Source": ["Broker flyer preview: 4402 Rex Rd Flyer.pdf, page 1"],
            },
            updates,
        )
        serialized = repr(candidate) + repr(updates)
        self.assertNotIn("RAW_BASE64_SHOULD_NOT_LEAK", serialized)
        self.assertNotIn("images", serialized)

    def test_existing_property_image_is_not_overwritten(self):
        from email_automation.property_images import build_property_image_sheet_updates

        updates = build_property_image_sheet_updates(
            ["Property Address", "Property Image", "Property Image Source"],
            ["4402 Rex Rd", "https://existing.example/photo.jpg", "Manual source"],
            {
                "url": "https://drive.google.com/uc?export=view&id=image-id",
                "sourceLabel": "Broker flyer preview: 4402 Rex Rd Flyer.pdf, page 1",
            },
        )

        self.assertEqual({}, updates)

    def test_generic_url_text_fetch_skips_listing_domains(self):
        from email_automation import utils

        with patch.object(utils.requests, "get") as mock_get:
            text = utils.fetch_url_as_text(
                "https://www.costar.com/detail/industrial/123-example"
            )

        self.assertIsNone(text)
        mock_get.assert_not_called()


class PropertyImageFileHandlingTests(unittest.TestCase):
    def test_fetch_and_process_pdfs_adds_hosted_property_preview_when_render_and_upload_succeed(self):
        from email_automation import file_handling

        with patch.object(file_handling, "fetch_pdf_attachments", return_value=[
            {"name": "4402 Rex Rd Flyer.pdf", "bytes": b"%PDF fake"}
        ]), patch.object(file_handling, "process_pdf_for_ai", return_value={
            "text": "Baywood Commercial Park",
            "images": [],
            "method": "local_extraction",
        }), patch.object(file_handling, "upload_pdf_to_drive", return_value="https://drive.google.com/file/d/pdf-id/view"), patch.object(
            file_handling,
            "render_pdf_first_page_preview",
            return_value=b"PNG_BYTES",
        ), patch.object(
            file_handling,
            "upload_property_image_to_drive",
            return_value={
                "url": "https://drive.google.com/uc?export=view&id=image-id",
                "driveLink": "https://drive.google.com/file/d/image-id/view",
                "contentType": "image/png",
                "byteCount": 9,
                "sha256": "abc123",
            },
        ):
            processed = file_handling.fetch_and_process_pdfs({"Authorization": "Bearer fake"}, "msg-1")

        self.assertEqual(1, len(processed))
        self.assertEqual(
            "https://drive.google.com/uc?export=view&id=image-id",
            processed[0]["property_image_url"],
        )
        self.assertEqual(
            "Broker flyer preview: 4402 Rex Rd Flyer.pdf, page 1",
            processed[0]["property_image_source"],
        )
        self.assertEqual(1, processed[0]["property_image_meta"]["pageNumber"])
        self.assertNotIn("PNG_BYTES", repr(processed[0]))


if __name__ == "__main__":
    unittest.main()
