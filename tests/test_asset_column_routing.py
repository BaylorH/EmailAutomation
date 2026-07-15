import os
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import property_images, sheets


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeValues:
    def __init__(self, service):
        self.service = service

    def get(self, *, spreadsheetId, range):
        if range.endswith("!2:2"):
            return FakeRequest({"values": [list(self.service.headers)]})
        return FakeRequest({"values": [[self.service.cells[range]]] if range in self.service.cells else []})

    def update(self, *, spreadsheetId, range, valueInputOption, body):
        value = body["values"][0][0]
        if range.endswith("2"):
            column = range.split("!")[1][:-1]
            column_number = 0
            for char in column:
                column_number = (column_number * 26) + ord(char.upper()) - 64
            while len(self.service.headers) < column_number:
                self.service.headers.append("")
            self.service.headers[column_number - 1] = value
        else:
            self.service.cells[range] = value
        self.service.updates.append((range, value))
        if range in self.service.fail_ranges:
            return FakeRequest(RuntimeError(f"write failed for {range}"))
        return FakeRequest({})


class FakeSpreadsheets:
    def __init__(self, service):
        self.service = service
        self.values_api = FakeValues(service)

    def get(self, *, spreadsheetId):
        return FakeRequest({"sheets": [{"properties": {"sheetId": 1, "title": "FOR LEASE"}}]})

    def values(self):
        return self.values_api


class FakeSheetsService:
    def __init__(self, headers):
        self.headers = list(headers)
        self.cells = {}
        self.updates = []
        self.fail_ranges = set()
        self.api = FakeSpreadsheets(self)

    def spreadsheets(self):
        return self.api


class AssetColumnRoutingTests(unittest.TestCase):
    def test_flyer_alias_and_floorplan_use_distinct_cells_with_stale_input_header(self):
        service = FakeSheetsService(["Property Address", "Flyer"])
        stale_header = list(service.headers)

        flyer_updates = sheets.append_links_to_flyer_link_column(
            service,
            "sheet-1",
            stale_header,
            3,
            ["https://drive.google.com/file/d/flyer/view"],
        )
        floorplan_updates = sheets.append_links_to_floorplan_column(
            service,
            "sheet-1",
            stale_header,
            3,
            ["https://drive.google.com/file/d/floorplan/view"],
        )

        self.assertEqual({"Flyer": ["https://drive.google.com/file/d/flyer/view"]}, flyer_updates)
        self.assertEqual(
            {"Floorplan": ["https://drive.google.com/file/d/floorplan/view"]},
            floorplan_updates,
        )
        self.assertEqual(["Property Address", "Flyer", "Floorplan"], service.headers)
        self.assertEqual("https://drive.google.com/file/d/flyer/view", service.cells["FOR LEASE!B3"])
        self.assertEqual("https://drive.google.com/file/d/floorplan/view", service.cells["FOR LEASE!C3"])
        self.assertNotIn("\n", service.cells["FOR LEASE!B3"])
        self.assertNotIn("\n", service.cells["FOR LEASE!C3"])

    def test_multiple_flyers_are_written_one_link_per_cell(self):
        service = FakeSheetsService(["Property Address", "Flyers"])

        updates = sheets.append_links_to_flyer_link_column(
            service,
            "sheet-1",
            list(service.headers),
            3,
            [
                "https://drive.google.com/file/d/flyer-one/view",
                "https://drive.google.com/file/d/flyer-two/view",
            ],
        )

        self.assertEqual(
            {
                "Flyers": ["https://drive.google.com/file/d/flyer-one/view"],
                "Flyers 2": ["https://drive.google.com/file/d/flyer-two/view"],
            },
            updates,
        )
        self.assertEqual(["Property Address", "Flyers", "Flyers 2"], service.headers)
        self.assertEqual("https://drive.google.com/file/d/flyer-one/view", service.cells["FOR LEASE!B3"])
        self.assertEqual("https://drive.google.com/file/d/flyer-two/view", service.cells["FOR LEASE!C3"])

    def test_new_asset_column_preserves_live_header_missing_from_snapshot(self):
        service = FakeSheetsService(["Property Address", "Flyer"])
        stale_header = list(service.headers)
        service.headers.append("Manual Notes")

        updates = sheets.append_links_to_floorplan_column(
            service,
            "sheet-1",
            stale_header,
            3,
            ["https://drive.google.com/file/d/floorplan/view"],
        )

        self.assertEqual(
            {"Floorplan": ["https://drive.google.com/file/d/floorplan/view"]},
            updates,
        )
        self.assertEqual(
            ["Property Address", "Flyer", "Manual Notes", "Floorplan"],
            service.headers,
        )
        self.assertEqual("https://drive.google.com/file/d/floorplan/view", service.cells["FOR LEASE!D3"])

    def test_partial_asset_write_raises_with_applied_updates(self):
        service = FakeSheetsService(["Property Address", "Flyers"])
        service.fail_ranges.add("FOR LEASE!C3")

        with self.assertRaises(sheets.AssetLinkWriteError) as ctx:
            sheets.append_links_to_flyer_link_column(
                service,
                "sheet-1",
                list(service.headers),
                3,
                [
                    "https://drive.google.com/file/d/flyer-one/view",
                    "https://drive.google.com/file/d/flyer-two/view",
                ],
            )

        self.assertEqual(
            {
                "Flyers": ["https://drive.google.com/file/d/flyer-one/view"],
                "Flyers 2": ["https://drive.google.com/file/d/flyer-two/view"],
            },
            ctx.exception.applied_updates,
        )
        self.assertEqual(["Flyers 2"], ctx.exception.created_columns)
        self.assertEqual("https://drive.google.com/file/d/flyer-one/view", service.cells["FOR LEASE!B3"])

        service.fail_ranges.clear()
        retry_updates = sheets.append_links_to_flyer_link_column(
            service,
            "sheet-1",
            list(service.headers),
            3,
            [
                "https://drive.google.com/file/d/flyer-one/view",
                "https://drive.google.com/file/d/flyer-two/view",
            ],
        )
        self.assertEqual({}, retry_updates)

    def test_existing_joined_links_are_deduped_but_never_appended(self):
        service = FakeSheetsService(["Property Address", "Flyer"])
        service.cells["FOR LEASE!B3"] = (
            "https://drive.google.com/file/d/old-one/view\n"
            "https://drive.google.com/file/d/old-two/view"
        )

        updates = sheets.append_links_to_flyer_link_column(
            service,
            "sheet-1",
            list(service.headers),
            3,
            [
                "https://drive.google.com/file/d/old-two/view",
                "https://drive.google.com/file/d/new-three/view",
            ],
        )

        self.assertEqual(
            {"Flyer 2": ["https://drive.google.com/file/d/new-three/view"]},
            updates,
        )
        self.assertEqual(["Property Address", "Flyer", "Flyer 2"], service.headers)
        self.assertEqual(
            "https://drive.google.com/file/d/new-three/view",
            service.cells["FOR LEASE!C3"],
        )
        self.assertEqual(
            "https://drive.google.com/file/d/old-one/view\n"
            "https://drive.google.com/file/d/old-two/view",
            service.cells["FOR LEASE!B3"],
        )

    def test_property_image_columns_are_never_invented(self):
        candidate = {
            "url": "https://drive.google.com/uc?export=view&id=preview",
            "sourceLabel": "Broker flyer preview: brochure.pdf, page 1",
        }

        self.assertEqual(
            {},
            property_images.build_property_image_sheet_updates(
                ["Property Address", "Flyer", "Floorplan"],
                ["123 Test Dr", "", ""],
                candidate,
            ),
        )

        service = FakeSheetsService(["Property Address", "Flyer", "Floorplan"])
        self.assertEqual(
            {},
            sheets.write_property_image_columns(
                service,
                "sheet-1",
                list(service.headers),
                3,
                {
                    "Property Image": [candidate["url"]],
                    "Property Image Source": [candidate["sourceLabel"]],
                },
            ),
        )
        self.assertEqual(["Property Address", "Flyer", "Floorplan"], service.headers)

        image_only_service = FakeSheetsService(["Property Address", "Property Image"])
        self.assertEqual(
            {"Property Image": [candidate["url"]]},
            sheets.write_property_image_columns(
                image_only_service,
                "sheet-1",
                list(image_only_service.headers),
                3,
                {
                    "Property Image": [candidate["url"]],
                    "Property Image Source": [candidate["sourceLabel"]],
                },
            ),
        )
        self.assertEqual(["Property Address", "Property Image"], image_only_service.headers)
        self.assertEqual(candidate["url"], image_only_service.cells["FOR LEASE!B3"])


if __name__ == "__main__":
    unittest.main()
