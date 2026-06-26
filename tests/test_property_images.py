import unittest
from unittest.mock import patch

import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)

from email_automation import file_handling, property_images


class PropertyImageTests(unittest.TestCase):
    def test_download_candidate_blocks_costar_and_loopnet_listing_urls(self):
        self.assertIsNone(
            property_images.build_download_candidate(
                "https://www.costar.com/detail/industrial/example-building",
                filename_hint="example.pdf",
            )
        )
        self.assertIsNone(
            property_images.build_download_candidate(
                "https://www.loopnet.com/Listing/example-building/123",
                filename_hint="example.pdf",
            )
        )

    def test_select_property_image_prefers_matching_address_manifest_item(self):
        manifest = [
            {
                "name": "Unrelated Flyer.pdf",
                "property_image_url": "https://drive.google.com/uc?export=view&id=wrong",
                "property_image_source": "Broker flyer preview: Unrelated Flyer.pdf, page 1",
            },
            {
                "name": "410 Genesis Blvd Flyer.pdf",
                "source_url": "https://broker.example/410-genesis-flyer.pdf",
                "property_image_url": "https://drive.google.com/uc?export=view&id=right",
                "property_image_source": "Broker flyer preview: 410 Genesis Blvd Flyer.pdf, page 2",
                "property_image_meta": {
                    "pageNumber": 2,
                    "sha256": "abc123",
                    "signals": {
                        "imageAreaRatio": 0.42,
                        "rawPageImageBytes": "should-not-survive",
                    },
                },
            },
        ]

        candidate = property_images.select_property_image_candidate(
            manifest,
            address="410 Genesis Blvd",
            city="Webster",
        )

        self.assertEqual("https://drive.google.com/uc?export=view&id=right", candidate["url"])
        self.assertEqual("Broker flyer preview: 410 Genesis Blvd Flyer.pdf, page 2", candidate["sourceLabel"])
        self.assertEqual(2, candidate["meta"]["pageNumber"])
        self.assertEqual({"imageAreaRatio": 0.42}, candidate["meta"]["signals"])
        self.assertNotIn("rawPageImageBytes", repr(candidate))

    def test_sheet_updates_write_image_and_source_only_when_image_cell_is_blank(self):
        header = ["Property Address", "Property Image", "Property Image Source"]
        candidate = {
            "url": "https://drive.google.com/uc?export=view&id=image-id",
            "sourceLabel": "Broker flyer preview: 410 Genesis Blvd Flyer.pdf, page 1",
        }

        self.assertEqual(
            {
                "Property Image": ["https://drive.google.com/uc?export=view&id=image-id"],
                "Property Image Source": ["Broker flyer preview: 410 Genesis Blvd Flyer.pdf, page 1"],
            },
            property_images.build_property_image_sheet_updates(header, ["410 Genesis Blvd", "", ""], candidate),
        )
        self.assertEqual(
            {},
            property_images.build_property_image_sheet_updates(
                header,
                ["410 Genesis Blvd", "https://manual.example/property.jpg", ""],
                candidate,
            ),
        )

    def test_linked_broker_image_becomes_property_image_manifest_without_network(self):
        with patch.object(file_handling, "_download_linked_asset", return_value=(b"image-bytes", "image/jpeg")), \
             patch.object(file_handling, "_image_link_to_png_preview", return_value=b"png-bytes"), \
             patch.object(
                 file_handling,
                 "upload_property_image_to_drive",
                 return_value={
                     "url": "https://drive.google.com/uc?export=view&id=linked-image",
                     "contentType": "image/png",
                     "byteCount": 9,
                     "sha256": "hash",
                     "driveLink": "https://drive.google.com/file/d/linked-image/view",
                 },
             ):
            processed = file_handling.fetch_and_process_linked_assets([
                "https://broker.example/410-genesis-photo.jpg"
            ])

        self.assertEqual(1, len(processed))
        self.assertEqual("direct_image_link", processed[0]["method"])
        self.assertEqual("https://drive.google.com/uc?export=view&id=linked-image", processed[0]["property_image_url"])
        self.assertEqual("Broker image link: 410-genesis-photo.jpg", processed[0]["property_image_source"])
        self.assertEqual("broker_image_link", processed[0]["property_image_source_type"])
        self.assertEqual("broker-provided public image link", processed[0]["property_image_meta"]["selectionReason"])
        self.assertNotIn("image-bytes", repr(processed[0]))

    def test_linked_assets_skip_blocked_listing_domains_before_download(self):
        with patch.object(file_handling, "_download_linked_asset") as download:
            processed = file_handling.fetch_and_process_linked_assets([
                "https://loopnet.com/Listing/410-genesis-photo.jpg"
            ])

        self.assertEqual([], processed)
        download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
