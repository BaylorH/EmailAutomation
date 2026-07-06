import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)


class PropertyImageResolverTests(unittest.TestCase):
    def test_processing_initializes_link_buckets_before_optional_pdf_manifest(self):
        processing_source = Path("email_automation/processing.py").read_text()

        pdf_manifest_branch = processing_source.index("if pdf_manifest:")
        flyer_bucket = processing_source.index("flyer_links = []")
        floorplan_bucket = processing_source.index("floorplan_links = []")
        linked_asset_branch = processing_source.index("linked_asset_manifest = fetch_and_process_linked_assets")

        self.assertLess(flyer_bucket, pdf_manifest_branch)
        self.assertLess(floorplan_bucket, pdf_manifest_branch)
        self.assertLess(flyer_bucket, linked_asset_branch)
        self.assertLess(floorplan_bucket, linked_asset_branch)

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
        direct_pdf = build_download_candidate(
            "https://broker.example.com/flyers/4402-Rex-Rd.pdf",
            filename_hint="4402 Rex Rd Flyer.pdf",
        )
        generic_page = build_download_candidate(
            "https://broker.example.com/listings/4402-rex-rd",
            filename_hint="4402 Rex Rd",
        )
        direct_image = build_download_candidate(
            "https://lh3.googleusercontent.com/p/AF1QipExample=w1200-h800",
            filename_hint="410 Genesis Blvd.jpg",
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
        self.assertEqual(
            "https://broker.example.com/flyers/4402-Rex-Rd.pdf",
            direct_pdf["downloadUrl"],
        )
        self.assertEqual("public_pdf", direct_pdf["sourceType"])
        self.assertIn("4402 Rex Rd Flyer.pdf", direct_pdf["sourceLabel"])
        self.assertIsNone(generic_page)
        self.assertEqual(
            "https://lh3.googleusercontent.com/p/AF1QipExample=w1200-h800",
            direct_image["downloadUrl"],
        )
        self.assertEqual("direct_image", direct_image["sourceType"])
        self.assertIn("410 Genesis Blvd.jpg", direct_image["sourceLabel"])

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
                    "pageCount": 4,
                    "strategy": "property_preview_heuristic_v1",
                    "selectionReason": "selected page with property-detail text",
                    "signals": {
                        "positiveTerms": ["sf"],
                        "rawPreviewBytes": "RAW_SIGNAL_BYTES_SHOULD_NOT_LEAK",
                        "nested": {"raw": "RAW_NESTED_SIGNAL_SHOULD_NOT_LEAK"},
                    },
                    "contentType": "image/png",
                    "byteCount": 12345,
                    "sha256": "abc123",
                    "rawPreviewBytes": "RAW_PREVIEW_SHOULD_NOT_LEAK",
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
        self.assertNotIn("RAW_PREVIEW_SHOULD_NOT_LEAK", serialized)
        self.assertNotIn("RAW_SIGNAL_BYTES_SHOULD_NOT_LEAK", serialized)
        self.assertNotIn("RAW_NESTED_SIGNAL_SHOULD_NOT_LEAK", serialized)
        self.assertNotIn("images", serialized)
        self.assertEqual("property_preview_heuristic_v1", candidate["meta"]["strategy"])
        self.assertEqual(["sf"], candidate["meta"]["signals"]["positiveTerms"])

    def test_manifest_candidate_prefers_link_or_address_matching_replacement_property(self):
        from email_automation.property_images import select_property_image_candidate

        manifest = [
            {
                "name": "16_Jupiter_Lease_Brochure-5600-SF.pdf",
                "source_url": "https://www.dropbox.com/scl/fi/key/16_Jupiter_Lease_Brochure-5600-SF.pdf?dl=0",
                "drive_link": "https://drive.google.com/file/d/jupiter-pdf/view",
                "property_image_url": "https://drive.google.com/uc?export=view&id=jupiter-image",
                "property_image_source": "Broker flyer link preview: 16_Jupiter_Lease_Brochure-5600-SF.pdf, page 1",
                "text": "16 Jupiter Park available office warehouse space",
            },
            {
                "name": "12-Petra-Lease-Brochure-6686-SF.pdf",
                "source_url": "https://www.dropbox.com/scl/fi/key/12-Petra-Lease-Brochure-6686-SF.pdf?dl=0",
                "drive_link": "https://drive.google.com/file/d/petra-pdf/view",
                "property_image_url": "https://drive.google.com/uc?export=view&id=petra-image",
                "property_image_source": "Broker flyer link preview: 12-Petra-Lease-Brochure-6686-SF.pdf, page 1",
                "text": "12 Petra 6,700 total SF with dock and drive-in",
            },
        ]

        candidate = select_property_image_candidate(
            manifest,
            address="12 Petra",
            city="Albany",
            source_url="https://www.dropbox.com/scl/fi/key/12-Petra-Lease-Brochure-6686-SF.pdf?dl=0",
        )

        self.assertEqual(
            "https://drive.google.com/uc?export=view&id=petra-image",
            candidate["url"],
        )
        self.assertIn("12-Petra", candidate["sourceLabel"])
        self.assertEqual("12-Petra-Lease-Brochure-6686-SF.pdf", candidate["sourceFilename"])

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
    def _make_test_pdf(self):
        import fitz

        doc = fitz.open()
        cover = doc.new_page(width=612, height=792)
        cover.insert_text(
            (72, 90),
            "Tour Packet\nPrepared for Metal Supermarkets\nCampaign overview and route map",
            fontsize=24,
        )
        property_page = doc.new_page(width=612, height=792)
        property_page.insert_text(
            (72, 80),
            (
                "9950 Windmill Lakes Blvd\n"
                "14,600 SF available industrial space\n"
                "24 ft clear height, dock high loading, drive-in door, trailer parking\n"
                "Lease rate and NNN details available from broker"
            ),
            fontsize=16,
        )
        property_page.draw_rect(
            fitz.Rect(72, 210, 540, 520),
            color=(0.1, 0.5, 0.3),
            fill=(0.1, 0.5, 0.3),
        )
        return doc.tobytes()

    def test_render_pdf_property_preview_skips_cover_page_for_property_page(self):
        from email_automation import file_handling

        if not file_handling.HAS_PYMUPDF:
            self.skipTest("PyMuPDF is required for PDF preview selection")

        preview = file_handling.render_pdf_property_preview(self._make_test_pdf())

        self.assertIsNotNone(preview)
        self.assertEqual(2, preview["pageNumber"])
        self.assertEqual(2, preview["pageCount"])
        self.assertEqual("property_preview_heuristic_v1", preview["strategy"])
        self.assertIn("property", preview["selectionReason"])
        self.assertIsInstance(preview["bytes"], bytes)
        self.assertNotIn(preview["bytes"], repr(preview.get("signals", {})).encode("utf-8"))

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

    def test_fetch_and_process_pdfs_uses_selected_preview_page_metadata(self):
        from email_automation import file_handling

        with patch.object(file_handling, "fetch_pdf_attachments", return_value=[
            {"name": "Tour Packet.pdf", "bytes": b"%PDF fake"}
        ]), patch.object(file_handling, "process_pdf_for_ai", return_value={
            "text": "Tour packet with property pages",
            "images": [],
            "method": "local_extraction",
        }), patch.object(file_handling, "upload_pdf_to_drive", return_value="https://drive.google.com/file/d/pdf-id/view"), patch.object(
            file_handling,
            "render_pdf_property_preview",
            return_value={
                "bytes": b"PNG_BYTES",
                "pageNumber": 3,
                "pageIndex": 2,
                "pageCount": 8,
                "strategy": "property_preview_heuristic_v1",
                "selectionReason": "selected page with property-detail text",
                "score": 12.5,
                "signals": {
                    "textChars": 410,
                    "positiveTerms": ["sf", "clear height"],
                    "negativeTerms": [],
                    "rawPreviewBytes": "RAW_SIGNAL_BYTES_SHOULD_NOT_LEAK",
                    "nested": {"raw": "RAW_NESTED_SIGNAL_SHOULD_NOT_LEAK"},
                },
            },
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

        self.assertEqual(
            "Broker flyer preview: Tour Packet.pdf, page 3",
            processed[0]["property_image_source"],
        )
        self.assertEqual(3, processed[0]["property_image_meta"]["pageNumber"])
        self.assertEqual(8, processed[0]["property_image_meta"]["pageCount"])
        self.assertEqual(
            "property_preview_heuristic_v1",
            processed[0]["property_image_meta"]["strategy"],
        )
        self.assertIn("positiveTerms", processed[0]["property_image_meta"]["signals"])
        self.assertNotIn("PNG_BYTES", repr(processed[0]))
        self.assertNotIn("RAW_SIGNAL_BYTES_SHOULD_NOT_LEAK", repr(processed[0]))
        self.assertNotIn("RAW_NESTED_SIGNAL_SHOULD_NOT_LEAK", repr(processed[0]))

    def test_fetch_and_process_linked_assets_resolves_pdf_link_property_preview(self):
        from email_automation import file_handling

        response = type("Response", (), {})()
        response.url = "https://www.dropbox.com/scl/fi/key/912-Gemini-Flyer.pdf?dl=1"
        response.status_code = 200
        response.headers = {"content-type": "application/pdf"}
        response.raise_for_status = lambda: None
        response.iter_content = lambda chunk_size=65536: iter([b"%PDF linked flyer"])

        with patch.object(file_handling.socket, "getaddrinfo", return_value=[
            (None, None, None, None, ("93.184.216.34", 443)),
        ]), patch.object(file_handling.requests, "get", return_value=response), patch.object(
            file_handling,
            "process_pdf_for_ai",
            return_value={
                "text": "912 Gemini St industrial availability with clear height and drive-in door",
                "images": [],
                "method": "local_extraction",
            },
        ), patch.object(
            file_handling,
            "upload_pdf_to_drive",
            return_value="https://drive.google.com/file/d/linked-pdf/view",
        ), patch.object(
            file_handling,
            "render_pdf_property_preview",
            return_value={
                "bytes": b"PNG_BYTES",
                "pageNumber": 2,
                "pageIndex": 1,
                "pageCount": 4,
                "strategy": "property_preview_heuristic_v1",
                "selectionReason": "selected page with property-detail text",
                "score": 9.25,
                "signals": {
                    "textChars": 320,
                    "positiveTerms": ["sf", "clear height"],
                    "negativeTerms": [],
                    "rawPreviewBytes": "RAW_SIGNAL_BYTES_SHOULD_NOT_LEAK",
                },
            },
        ), patch.object(
            file_handling,
            "upload_property_image_to_drive",
            return_value={
                "url": "https://drive.google.com/uc?export=view&id=linked-image",
                "driveLink": "https://drive.google.com/file/d/linked-image/view",
                "contentType": "image/png",
                "byteCount": 9,
                "sha256": "abc123",
            },
        ):
            processed = file_handling.fetch_and_process_linked_assets([
                "https://www.dropbox.com/scl/fi/key/912-Gemini-Flyer.pdf?dl=0",
            ])

        self.assertEqual(1, len(processed))
        self.assertEqual("912-Gemini-Flyer.pdf", processed[0]["name"])
        self.assertEqual("https://drive.google.com/file/d/linked-pdf/view", processed[0]["drive_link"])
        self.assertEqual(
            "https://drive.google.com/uc?export=view&id=linked-image",
            processed[0]["property_image_url"],
        )
        self.assertEqual(
            "Broker flyer link preview: 912-Gemini-Flyer.pdf, page 2",
            processed[0]["property_image_source"],
        )
        self.assertEqual("broker_pdf_link_preview", processed[0]["property_image_source_type"])
        self.assertEqual(
            "https://www.dropbox.com/scl/fi/key/912-Gemini-Flyer.pdf?dl=0",
            processed[0]["source_url"],
        )
        self.assertNotIn("PNG_BYTES", repr(processed[0]))
        self.assertNotIn("RAW_SIGNAL_BYTES_SHOULD_NOT_LEAK", repr(processed[0]))

    def test_fetch_and_process_linked_assets_resolves_safe_direct_image_link(self):
        from email_automation import file_handling

        response = type("Response", (), {})()
        response.url = "https://lh3.googleusercontent.com/p/AF1QipExample=w1200-h800"
        response.status_code = 200
        response.headers = {"content-type": "image/jpeg"}
        response.raise_for_status = lambda: None
        response.iter_content = lambda chunk_size=65536: iter([b"JPEG linked image"])

        with patch.object(file_handling.socket, "getaddrinfo", return_value=[
            (None, None, None, None, ("142.250.72.225", 443)),
        ]), patch.object(file_handling.requests, "get", return_value=response), patch.object(
            file_handling,
            "_image_link_to_png_preview",
            return_value=b"PNG_BYTES",
        ), patch.object(
            file_handling,
            "upload_property_image_to_drive",
            return_value={
                "url": "https://drive.google.com/uc?export=view&id=direct-linked-image",
                "driveLink": "https://drive.google.com/file/d/direct-linked-image/view",
                "contentType": "image/png",
                "byteCount": 9,
                "sha256": "abc123",
            },
        ):
            processed = file_handling.fetch_and_process_linked_assets([
                "https://lh3.googleusercontent.com/p/AF1QipExample=w1200-h800",
            ])

        self.assertEqual(1, len(processed))
        self.assertEqual("direct_image_link", processed[0]["method"])
        self.assertEqual(
            "https://drive.google.com/uc?export=view&id=direct-linked-image",
            processed[0]["property_image_url"],
        )
        self.assertEqual(
            "Broker image link: broker property image.png",
            processed[0]["property_image_source"],
        )
        self.assertEqual("broker_image_link", processed[0]["property_image_source_type"])
        self.assertNotIn("PNG_BYTES", repr(processed[0]))

    def test_fetch_and_process_linked_assets_skips_generic_company_links(self):
        from email_automation import file_handling

        with patch.object(file_handling, "_download_linked_asset") as download:
            processed = file_handling.fetch_and_process_linked_assets([
                "https://example.com",
            ])

        self.assertEqual([], processed)
        download.assert_not_called()

    def test_linked_asset_download_rejects_private_network_targets_before_request(self):
        from email_automation import file_handling

        with patch.object(file_handling.requests, "get") as mock_get:
            with self.assertRaises(ValueError):
                file_handling._download_linked_asset("https://127.0.0.1/flyer.pdf")

        mock_get.assert_not_called()

    def test_linked_asset_download_allows_public_direct_asset_hosts(self):
        from email_automation import file_handling

        class Response:
            url = "https://broker.example.com/flyer.pdf"
            status_code = 200
            headers = {"content-type": "application/pdf"}

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size=65536):
                yield b"%PDF broker flyer"

        with patch.object(file_handling.socket, "getaddrinfo", return_value=[
            (None, None, None, None, ("93.184.216.34", 443)),
        ]), patch.object(file_handling.requests, "get", return_value=Response()) as mock_get:
            content, content_type = file_handling._download_linked_asset(
                "https://broker.example.com/flyer.pdf"
            )

        self.assertEqual(b"%PDF broker flyer", content)
        self.assertEqual("application/pdf", content_type)
        mock_get.assert_called_once()

    def test_linked_asset_download_rejects_blocked_listing_hosts_before_request(self):
        from email_automation import file_handling

        with patch.object(file_handling.requests, "get") as mock_get:
            with self.assertRaises(ValueError):
                file_handling._download_linked_asset(
                    "https://www.loopnet.com/Listing/902-910-Gemini-Houston-TX/40231241/"
                )

        mock_get.assert_not_called()

    def test_linked_asset_download_streams_and_stops_at_size_cap(self):
        from email_automation import file_handling

        class StreamingResponse:
            url = "https://broker.example/flyer.pdf"
            status_code = 200
            headers = {"content-type": "application/pdf"}

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size=65536):
                yield b"1234"
                yield b"56"

        with patch.object(file_handling, "MAX_LINKED_PROPERTY_ASSET_BYTES", 5), patch.object(
            file_handling,
            "_validate_public_https_url",
            return_value="https://broker.example/flyer.pdf",
        ), patch.object(
            file_handling.requests,
            "get",
            return_value=StreamingResponse(),
        ) as mock_get:
            with self.assertRaises(ValueError):
                file_handling._download_linked_asset("https://broker.example/flyer.pdf")

        mock_get.assert_called_once()
        self.assertTrue(mock_get.call_args.kwargs.get("stream"))


if __name__ == "__main__":
    unittest.main()
