"""Focused B2-A1 identity/location harness and containment contracts."""

from __future__ import annotations

import ast
import importlib
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from tests import test_row_authority_contracts as authority_contracts
from tests.source_coordinator_fakes import FakeTransactionAborted


REPO_ROOT = Path(__file__).resolve().parents[1]
ROW_ID = "sr1_123e4567e89b42d3a456426614174000"
SECOND_ROW_ID = "sr1_123e4567e89b42d3a456426614174001"


class RowMetadataContractTests(unittest.TestCase):
    @staticmethod
    def _load_module():
        if not authority_contracts.ROW_METADATA_PATH.exists():
            raise AssertionError("row metadata module is missing")
        return importlib.import_module("email_automation.row_metadata")

    @staticmethod
    def _lookup(row_id=ROW_ID):
        return {
            "developerMetadataLookup": {
                "metadataKey": "sitesift_row_id_v1",
                "metadataValue": row_id,
                "visibility": "DOCUMENT",
                "locationType": "ROW",
            }
        }

    @staticmethod
    def _metadata(
        *,
        row_id=ROW_ID,
        sheet_id=7,
        provider_row_index=0,
        metadata_id=1,
    ):
        return {
            "metadataId": metadata_id,
            "metadataKey": "sitesift_row_id_v1",
            "metadataValue": row_id,
            "location": {
                "locationType": "ROW",
                "dimensionRange": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": provider_row_index,
                    "endIndex": provider_row_index + 1,
                },
            },
            "visibility": "DOCUMENT",
        }

    @classmethod
    def _match(cls, **metadata_overrides):
        row_id = metadata_overrides.get("row_id", ROW_ID)
        return {
            "developerMetadata": cls._metadata(**metadata_overrides),
            "dataFilters": [cls._lookup(row_id)],
        }

    def assert_rejected(self, function, *args, **kwargs):
        with self.assertRaises(ValueError):
            function(*args, **kwargs)

    def test_row_metadata_module_exists(self):
        module = self._load_module()
        self.assertEqual("sitesift_row_id_v1", module.MARKER_KEY)
        self.assertEqual("DOCUMENT", module.MARKER_VISIBILITY)
        self.assertEqual("ROW", module.ROW_LOCATION_TYPE)
        self.assertEqual("ROWS", module.ROW_DIMENSION)

    def test_b2_modules_have_no_provider_or_runtime_imports(self):
        self._load_module()
        authority_tree = ast.parse(
            authority_contracts.ROW_AUTHORITY_PATH.read_text(encoding="utf-8"),
            filename=str(authority_contracts.ROW_AUTHORITY_PATH),
        )
        self.assertEqual(
            set(),
            authority_contracts._direct_import_roots(authority_tree)
            - authority_contracts.ROW_AUTHORITY_STANDARD_LIBRARY_IMPORTS,
        )
        self.assertEqual([], authority_contracts._literal_dynamic_imports(authority_tree))
        metadata_tree = ast.parse(
            authority_contracts.ROW_METADATA_PATH.read_text(encoding="utf-8"),
            filename=str(authority_contracts.ROW_METADATA_PATH),
        )
        self.assertEqual(
            set(),
            authority_contracts._direct_import_roots(metadata_tree)
            - authority_contracts.ROW_METADATA_STANDARD_LIBRARY_IMPORTS
            - {"email_automation"},
        )
        application_imports = [
            node
            for node in ast.walk(metadata_tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "email_automation.row_authority"
        ]
        self.assertEqual(1, len(application_imports))
        self.assertEqual(
            [(authority_contracts.ROW_METADATA_APPLICATION_IMPORT[1], None)],
            [(alias.name, alias.asname) for alias in application_imports[0].names],
        )
        self.assertEqual([], authority_contracts._literal_dynamic_imports(metadata_tree))
        all_application_imports = [
            node
            for node in ast.walk(metadata_tree)
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.split(".", 1)[0] == "email_automation"
            )
            or (
                isinstance(node, ast.Import)
                and any(
                    alias.name.split(".", 1)[0] == "email_automation"
                    for alias in node.names
                )
            )
        ]
        self.assertEqual(application_imports, all_application_imports)
        source = authority_contracts.ROW_METADATA_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "googleapiclient",
            "google.cloud",
            "requests",
            "build(",
            "credentials",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_create_request_has_exact_official_row_shape(self):
        module = self._load_module()
        request = module.build_row_marker_create_request(
            row_id=ROW_ID,
            sheet_id=7,
            provider_row_index=0,
        )
        self.assertEqual(
            {
                "createDeveloperMetadata": {
                    "developerMetadata": {
                        "metadataKey": "sitesift_row_id_v1",
                        "metadataValue": ROW_ID,
                        "location": {
                            "dimensionRange": {
                                "sheetId": 7,
                                "dimension": "ROWS",
                                "startIndex": 0,
                                "endIndex": 1,
                            }
                        },
                        "visibility": "DOCUMENT",
                    }
                }
            },
            request,
        )
        self.assertNotIn("metadataId", repr(request))
        self.assertNotIn("locationType", repr(request))
        request["createDeveloperMetadata"]["developerMetadata"][
            "metadataValue"
        ] = SECOND_ROW_ID
        self.assertEqual(
            ROW_ID,
            module.build_row_marker_create_request(
                row_id=ROW_ID,
                sheet_id=7,
                provider_row_index=0,
            )["createDeveloperMetadata"]["developerMetadata"]["metadataValue"],
        )
        self.assert_rejected(
            module.build_row_marker_create_request,
            row_id="legacy-row-id",
            sheet_id=7,
            provider_row_index=0,
        )
        for field_name, value in (
            ("sheet_id", True),
            ("sheet_id", -1),
            ("sheet_id", 1.0),
            ("provider_row_index", False),
            ("provider_row_index", -1),
            ("provider_row_index", "0"),
        ):
            arguments = {
                "row_id": ROW_ID,
                "sheet_id": 7,
                "provider_row_index": 0,
            }
            arguments[field_name] = value
            with self.subTest(field_name=field_name, value=value):
                self.assert_rejected(
                    module.build_row_marker_create_request,
                    **arguments,
                )

    def test_search_request_has_exact_key_value_visibility_and_row_lookup(self):
        module = self._load_module()
        first = module.build_row_marker_search_request(row_id=ROW_ID)
        second = module.build_row_marker_search_request(row_id=ROW_ID)
        self.assertEqual({"dataFilters": [self._lookup()]}, first)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertIsNot(first["dataFilters"], second["dataFilters"])
        self.assert_rejected(
            module.build_row_marker_search_request,
            row_id="legacy-row-id",
        )

    def test_direct_metadata_parser_returns_exact_observation(self):
        module = self._load_module()
        metadata = self._metadata(
            sheet_id=0,
            provider_row_index=0,
            metadata_id=1,
        )
        parsed = module.parse_row_developer_metadata(metadata)
        self.assertEqual(
            {
                "rowId": ROW_ID,
                "sheetId": 0,
                "providerRowIndex": 0,
                "displayRowNumber": 1,
                "metadataId": 1,
            },
            parsed,
        )
        metadata["location"]["dimensionRange"]["startIndex"] = 99
        self.assertEqual(0, parsed["providerRowIndex"])

    def test_parser_accepts_exact_positive_ids_and_zero_index(self):
        module = self._load_module()
        for metadata_id in (1, 2_147_483_647):
            with self.subTest(metadata_id=metadata_id):
                parsed = module.parse_row_developer_metadata(
                    self._metadata(
                        sheet_id=0,
                        provider_row_index=0,
                        metadata_id=metadata_id,
                    )
                )
                self.assertEqual(metadata_id, parsed["metadataId"])
                self.assertEqual(0, parsed["sheetId"])
                self.assertEqual(0, parsed["providerRowIndex"])

    def test_parser_rejects_unknown_missing_mistyped_and_boolean_fields(self):
        module = self._load_module()
        base = self._metadata()
        variants = []
        for key in base:
            missing = deepcopy(base)
            missing.pop(key)
            variants.append(missing)
        unknown = deepcopy(base)
        unknown["unknown"] = True
        variants.append(unknown)
        unknown_location = deepcopy(base)
        unknown_location["location"]["unknown"] = True
        variants.append(unknown_location)
        unknown_range = deepcopy(base)
        unknown_range["location"]["dimensionRange"]["unknown"] = True
        variants.append(unknown_range)
        for field, value in (
            ("metadataId", True),
            ("metadataId", "1"),
            ("metadataKey", 1),
            ("metadataValue", 1),
            ("location", []),
            ("visibility", None),
        ):
            mistyped = deepcopy(base)
            mistyped[field] = value
            variants.append(mistyped)
        for variant in variants:
            with self.subTest(variant=variant):
                self.assert_rejected(module.parse_row_developer_metadata, variant)
        for non_dictionary in (None, [], (), "metadata"):
            with self.subTest(non_dictionary=non_dictionary):
                self.assert_rejected(
                    module.parse_row_developer_metadata,
                    non_dictionary,
                )

    def test_parser_rejects_wrong_key_value_visibility_location_type_or_dimension(self):
        module = self._load_module()
        variants = []
        for path, value in (
            (("metadataKey",), "other"),
            (("metadataValue",), SECOND_ROW_ID[:-1]),
            (("visibility",), "PROJECT"),
            (("location", "locationType"), "COLUMN"),
            (("location", "dimensionRange", "dimension"), "COLUMNS"),
        ):
            variant = deepcopy(self._metadata())
            target = variant
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
            variants.append(variant)
        for variant in variants:
            with self.subTest(variant=variant):
                self.assert_rejected(module.parse_row_developer_metadata, variant)

    def test_parser_rejects_unbounded_multirow_reversed_or_wrong_sheet_ranges(self):
        module = self._load_module()
        variants = []
        for field, value in (
            ("sheetId", -1),
            ("sheetId", True),
            ("startIndex", -1),
            ("startIndex", True),
            ("endIndex", True),
            ("endIndex", 0),
            ("endIndex", 3),
        ):
            variant = deepcopy(self._metadata(provider_row_index=0))
            variant["location"]["dimensionRange"][field] = value
            variants.append(variant)
        extra_location = deepcopy(self._metadata())
        extra_location["location"]["sheetId"] = 7
        variants.append(extra_location)
        extra_range = deepcopy(self._metadata())
        extra_range["location"]["dimensionRange"]["startColumnIndex"] = 0
        variants.append(extra_range)
        for variant in variants:
            with self.subTest(variant=variant):
                self.assert_rejected(module.parse_row_developer_metadata, variant)

    def test_parser_rejects_non_uuid4_marker_values(self):
        module = self._load_module()
        for row_id in (
            "",
            "sr1_123e4567e89b12d3a456426614174000",
            "sr1_123e4567e89b42d3c456426614174000",
            ROW_ID.upper(),
            ROW_ID + "00",
            None,
        ):
            with self.subTest(row_id=row_id):
                metadata = self._metadata()
                metadata["metadataValue"] = row_id
                self.assert_rejected(module.parse_row_developer_metadata, metadata)
        self.assertEqual(
            SECOND_ROW_ID,
            module.parse_row_developer_metadata(
                self._metadata(row_id=SECOND_ROW_ID)
            )["rowId"],
        )

    def test_search_parser_distinguishes_successful_empty_from_lookup_failure(self):
        module = self._load_module()
        self.assertEqual(
            (),
            module.parse_row_marker_search_response({}, expected_row_id=ROW_ID),
        )
        self.assertEqual(
            (),
            module.parse_row_marker_search_response(
                {"matchedDeveloperMetadata": []},
                expected_row_id=ROW_ID,
            ),
        )
        for response in (
            None,
            [],
            {"error": {"code": 503}},
            {"matchedDeveloperMetadata": None},
            {"matchedDeveloperMetadata": (), "unknown": True},
            {"matchedDeveloperMetadata": [], "unknown": True},
        ):
            with self.subTest(response=response):
                self.assert_rejected(
                    module.parse_row_marker_search_response,
                    response,
                    expected_row_id=ROW_ID,
                )

    def test_search_parser_validates_every_wrapper_and_echoed_filter(self):
        module = self._load_module()
        valid = self._match()
        variants = []
        extra = deepcopy(valid)
        extra["unknown"] = True
        variants.append(extra)
        for key in ("developerMetadata", "dataFilters"):
            missing = deepcopy(valid)
            missing.pop(key)
            variants.append(missing)
        empty_filters = deepcopy(valid)
        empty_filters["dataFilters"] = []
        variants.append(empty_filters)
        tuple_filters = deepcopy(valid)
        tuple_filters["dataFilters"] = tuple(tuple_filters["dataFilters"])
        variants.append(tuple_filters)
        mistyped_filter = deepcopy(valid)
        mistyped_filter["dataFilters"] = [None]
        variants.append(mistyped_filter)
        extra_filter = deepcopy(valid)
        extra_filter["dataFilters"].append(self._lookup())
        variants.append(extra_filter)
        wrong_echo = deepcopy(valid)
        wrong_echo["dataFilters"][0]["developerMetadataLookup"][
            "metadataValue"
        ] = SECOND_ROW_ID
        variants.append(wrong_echo)
        extra_echo_field = deepcopy(valid)
        extra_echo_field["dataFilters"][0]["developerMetadataLookup"][
            "unknown"
        ] = True
        variants.append(extra_echo_field)
        wrong_metadata = deepcopy(valid)
        wrong_metadata["developerMetadata"]["metadataValue"] = SECOND_ROW_ID
        variants.append(wrong_metadata)
        for wrapper in variants:
            with self.subTest(wrapper=wrapper):
                self.assert_rejected(
                    module.parse_row_marker_search_response,
                    {"matchedDeveloperMetadata": [valid, wrapper]},
                    expected_row_id=ROW_ID,
                )

    def test_search_parser_returns_two_and_128_matches_in_canonical_order(self):
        module = self._load_module()
        two = [
            self._match(provider_row_index=3, metadata_id=9),
            self._match(provider_row_index=1, metadata_id=7),
        ]
        parsed_two = module.parse_row_marker_search_response(
            {"matchedDeveloperMetadata": two},
            expected_row_id=ROW_ID,
        )
        self.assertIsInstance(parsed_two, tuple)
        self.assertEqual(
            [(1, 7), (3, 9)],
            [
                (item["providerRowIndex"], item["metadataId"])
                for item in parsed_two
            ],
        )
        matches = [
            self._match(provider_row_index=index, metadata_id=index + 1)
            for index in reversed(range(128))
        ]
        parsed = module.parse_row_marker_search_response(
            {"matchedDeveloperMetadata": matches},
            expected_row_id=ROW_ID,
        )
        self.assertEqual(128, len(parsed))
        self.assertEqual(list(range(128)), [item["providerRowIndex"] for item in parsed])
        matches[0]["developerMetadata"]["metadataId"] = 999
        self.assertNotEqual(999, parsed[-1]["metadataId"])

    def test_search_parser_rejects_129_matches_before_authority(self):
        module = self._load_module()
        with patch.object(
            module,
            "parse_row_developer_metadata",
            side_effect=AssertionError("match parser must not run"),
        ) as parser, self.assertRaisesRegex(ValueError, "128"):
            module.parse_row_marker_search_response(
                {"matchedDeveloperMetadata": [None] * 129},
                expected_row_id=ROW_ID,
            )
        parser.assert_not_called()

    def test_fake_create_response_round_trips_through_parser(self):
        module = self._load_module()
        fakes = importlib.import_module("tests.row_authority_fakes")
        sheet = fakes.MarkerAwareSheet(sheet_id=7, rows=(("row",),))
        metadata = sheet.create_row_marker(provider_row_index=0, row_id=ROW_ID)
        self.assertEqual(
            {
                "rowId": ROW_ID,
                "sheetId": 7,
                "providerRowIndex": 0,
                "displayRowNumber": 1,
                "metadataId": 1,
            },
            module.parse_row_developer_metadata(metadata),
        )

    def test_moved_sorted_and_restarted_marker_parses_at_new_coordinate(self):
        module = self._load_module()
        fakes = importlib.import_module("tests.row_authority_fakes")
        sheet = fakes.MarkerAwareSheet(
            sheet_id=7,
            rows=(("target",), ("alpha",), ("zulu",)),
        )
        sheet.create_row_marker(provider_row_index=0, row_id=ROW_ID)
        sheet.move_row(0, 2)
        sheet.sort_rows(key=lambda cells: cells[0])
        restarted = sheet.restart()
        parsed = module.parse_row_developer_metadata(
            restarted.search_row_markers(ROW_ID)[0]
        )
        self.assertEqual(("target",), restarted.row_cells(parsed["providerRowIndex"]))
        self.assertEqual(parsed["providerRowIndex"] + 1, parsed["displayRowNumber"])

    def test_deleted_marker_search_returns_an_explicit_empty_result(self):
        module = self._load_module()
        fakes = importlib.import_module("tests.row_authority_fakes")
        sheet = fakes.MarkerAwareSheet(sheet_id=7, rows=(("delete",),))
        sheet.create_row_marker(provider_row_index=0, row_id=ROW_ID)
        sheet.delete_row(0)
        response = {
            "matchedDeveloperMetadata": list(sheet.search_row_markers(ROW_ID))
        }
        self.assertEqual(
            (),
            module.parse_row_marker_search_response(
                response,
                expected_row_id=ROW_ID,
            ),
        )

    def test_duplicate_matches_remain_distinct_and_deterministically_ordered(self):
        module = self._load_module()
        fakes = importlib.import_module("tests.row_authority_fakes")
        sheet = fakes.MarkerAwareSheet(
            sheet_id=7,
            rows=(("first",), ("second",)),
        )
        sheet.create_row_marker(provider_row_index=1, row_id=ROW_ID)
        sheet.create_row_marker(provider_row_index=0, row_id=ROW_ID)
        matches = list(reversed(sheet.search_row_markers(ROW_ID)))
        response = {
            "matchedDeveloperMetadata": [
                {
                    "developerMetadata": metadata,
                    "dataFilters": [self._lookup()],
                }
                for metadata in matches
            ]
        }
        parsed = module.parse_row_marker_search_response(
            response,
            expected_row_id=ROW_ID,
        )
        self.assertEqual(2, len(parsed))
        self.assertEqual(
            [(0, 2), (1, 1)],
            [
                (item["providerRowIndex"], item["metadataId"])
                for item in parsed
            ],
        )


class RowAuthorityA1ContainmentTests(unittest.TestCase):
    def test_only_row_metadata_may_import_row_authority(self):
        self.assertEqual(
            frozenset({"email_automation/row_metadata.py"}),
            authority_contracts.ROW_AUTHORITY_IMPORTER_ALLOWLIST,
        )
        importers = [
            relative.as_posix()
            for relative in authority_contracts._application_python_paths()
            if relative.as_posix()
            not in authority_contracts.ROW_AUTHORITY_IMPORTER_ALLOWLIST
            and authority_contracts._tree_imports_row_authority(
                ast.parse(
                    (REPO_ROOT / relative).read_text(encoding="utf-8"),
                    filename=str(relative),
                )
            )
        ]
        self.assertEqual([], importers)

    def test_no_runtime_module_imports_row_metadata(self):
        self.assertEqual(
            frozenset(),
            authority_contracts.ROW_METADATA_IMPORTER_ALLOWLIST,
        )
        synthetic_dynamic_imports = (
            "from email_automation import row_metadata",
            "__import__('email_automation.' + 'row_metadata')",
            "importlib.import_module(name)",
        )
        for source in synthetic_dynamic_imports:
            with self.subTest(source=source):
                self.assertTrue(
                    authority_contracts._tree_imports_row_metadata(
                        ast.parse(source)
                    )
                )
        application_paths = (
            Path("email_automation/row_authority.py"),
            *authority_contracts._application_python_paths(),
        )
        importers = [
            relative.as_posix()
            for relative in application_paths
            if relative != Path("email_automation/row_metadata.py")
            and relative.as_posix()
            not in authority_contracts.ROW_METADATA_IMPORTER_ALLOWLIST
            and authority_contracts._tree_imports_row_metadata(
                ast.parse(
                    (REPO_ROOT / relative).read_text(encoding="utf-8"),
                    filename=str(relative),
                )
            )
        ]
        self.assertEqual([], importers)


class RowAuthorityA1HarnessTests(unittest.TestCase):
    @staticmethod
    def _load_fakes():
        return importlib.import_module("tests.row_authority_fakes")

    def test_transaction_executor_retries_one_stale_snapshot(self):
        fakes = self._load_fakes()
        store = fakes.BoundedFakeFirestore()
        source = store.collection("items").document("source")
        result_ref = store.collection("items").document("result")
        source.create({"value": 0})
        attempts = []

        def callback(transaction):
            value = source.get(transaction=transaction).to_dict()["value"]
            attempts.append(value)
            if len(attempts) == 1:
                source.set({"value": 1})
            transaction.create(result_ref, {"seen": value})
            return value

        result = fakes.run_bounded_transaction(
            store.transaction(max_attempts=2),
            callback,
        )

        self.assertEqual(1, result)
        self.assertEqual([0, 1], attempts)
        self.assertEqual({"seen": 1}, result_ref.get().to_dict())
        self.assertIn(
            ("commit_aborted_stale_read", source.path),
            store.events,
        )

    def test_transaction_executor_stops_after_max_attempts(self):
        fakes = self._load_fakes()
        store = fakes.BoundedFakeFirestore()
        source = store.collection("items").document("source")
        result_ref = store.collection("items").document("result")
        source.create({"value": 0})
        attempts = []

        def callback(transaction):
            value = source.get(transaction=transaction).to_dict()["value"]
            attempts.append(value)
            source.set({"value": value + 1})
            transaction.create(result_ref, {"seen": value})
            return value

        with self.assertRaises(FakeTransactionAborted):
            fakes.run_bounded_transaction(
                store.transaction(max_attempts=2),
                callback,
            )

        self.assertEqual([0, 1], attempts)
        self.assertFalse(result_ref.get().exists)

    def test_transaction_executor_preserves_apply_then_raise(self):
        fakes = self._load_fakes()
        store = fakes.BoundedFakeFirestore()
        target = store.collection("items").document("target")
        store.apply_then_raise_next_commit = RuntimeError("unknown commit")

        def callback(transaction):
            transaction.create(target, {"value": 1})
            return "created"

        transaction = store.transaction()
        with self.assertRaisesRegex(RuntimeError, "unknown commit"):
            fakes.run_bounded_transaction(transaction, callback)

        self.assertEqual({"value": 1}, target.get().to_dict())
        self.assertFalse(transaction.in_progress)
        self.assertIn(("commit_raised_after_apply",), store.events)

    def test_marker_fake_preserves_marker_through_insert_move_sort_and_restart(self):
        fakes = self._load_fakes()
        sheet = fakes.MarkerAwareSheet(
            sheet_id=7,
            rows=(("beta",), ("anchor",), ("alpha",)),
        )
        created = sheet.create_row_marker(
            provider_row_index=1,
            row_id=ROW_ID,
        )
        metadata_id = created["metadataId"]

        sheet.insert_row(0, ("inserted",))
        sheet.move_row(2, 0)
        sheet.sort_rows(key=lambda cells: cells[0])
        restarted = sheet.restart()
        match = restarted.search_row_markers(ROW_ID)[0]
        index = match["location"]["dimensionRange"]["startIndex"]

        self.assertEqual(metadata_id, match["metadataId"])
        self.assertEqual(("anchor",), restarted.row_cells(index))
        self.assertEqual(index + 1, match["location"]["dimensionRange"]["endIndex"])

        sheet.move_row(index, len(sheet) - 1)
        restarted_match = restarted.search_row_markers(ROW_ID)[0]
        self.assertEqual(index, restarted_match["location"]["dimensionRange"]["startIndex"])

    def test_marker_fake_deletes_marker_with_row(self):
        fakes = self._load_fakes()
        sheet = fakes.MarkerAwareSheet(
            sheet_id=7,
            rows=(("keep",), ("delete",)),
        )
        sheet.create_row_marker(provider_row_index=1, row_id=ROW_ID)

        sheet.delete_row(1)

        self.assertEqual((), sheet.search_row_markers(ROW_ID))
        self.assertEqual(1, len(sheet))

    def test_marker_fake_can_expose_duplicate_locations_without_election(self):
        fakes = self._load_fakes()
        sheet = fakes.MarkerAwareSheet(
            sheet_id=7,
            rows=(("first",), ("middle",), ("second",)),
        )
        first = sheet.create_row_marker(provider_row_index=0, row_id=ROW_ID)
        second = sheet.create_row_marker(provider_row_index=2, row_id=ROW_ID)

        matches = sheet.search_row_markers(ROW_ID)

        self.assertEqual(2, len(matches))
        self.assertEqual(
            [0, 2],
            [
                match["location"]["dimensionRange"]["startIndex"]
                for match in matches
            ],
        )
        self.assertEqual(
            {first["metadataId"], second["metadataId"]},
            {match["metadataId"] for match in matches},
        )
        self.assertTrue(all(match["metadataValue"] == ROW_ID for match in matches))


if __name__ == "__main__":
    unittest.main()
