"""Focused B2-A1 identity/location harness and containment contracts."""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import unittest
import unicodedata
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from tests import test_row_authority_contracts as authority_contracts
from tests.source_coordinator_fakes import FakeTransactionAborted


REPO_ROOT = Path(__file__).resolve().parents[1]
ROW_ID = "sr1_123e4567e89b42d3a456426614174000"
SECOND_ROW_ID = "sr1_123e4567e89b42d3a456426614174001"
USER_SCOPE_HASH = (
    "48fafc848b44ae7b0414309666dcb54208b7867700240a0f343ec02c53eb0cf2"
)
CREATION_SOURCE_HASH = "1" * 64
CREATED_AT = "2026-08-04T12:34:56.123456Z"
LATER_AT = "2026-08-04T12:35:57.654321Z"
FROZEN_A1_HASHES = {
    "markerHash": "5dae5d60c0db2f02e951c7f38e6b71f37171ad3fbe8ad9110976a42359d6447d",
    "identityHash": "5b110eaa888cd16e17aabb34192b8c6f731cac7303f36545ba4879ee41b0349b",
    "headerHash": "633fa62b41647ee95e2338fde9b5b1152ac5a1af5dfec4aa315d98faeec73f75",
    "rowSnapshotHash": "de7bd24bec9be791fc9961c10ce246fb06e8585d3ae8555c194c174dbc763882",
    "observationEvidenceHash": (
        "2c0683283b3360378a1ba9e6db70f8a3b8631e88b090d88b97bb845b98bf27e4"
    ),
    "revisionHash": "c6f3b86e8a86ab03845aa985b334f1b22030c6d87f40abdae943a432595c04a9",
    "headHash": "7d4fcae9b8b2cacb78081e96cbf455e6b3e02f036e99d7adeaff6c326b3db0c7",
}


def _reference_domain_hash(domain, payload, *, user_scope_hash=USER_SCOPE_HASH):
    material = {
        **payload,
        "schemaVersion": 1,
        "userScopeHash": user_scope_hash,
    }
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(domain.encode("utf-8") + b"\0" + encoded).hexdigest()


class _RowIdentityFixtures:
    @staticmethod
    def _authority():
        return importlib.import_module("email_automation.row_authority")

    @staticmethod
    def _identity_kwargs(**overrides):
        values = {
            "user_scope_hash": USER_SCOPE_HASH,
            "row_id": ROW_ID,
            "client_id": "client-A",
            "spreadsheet_id": "spreadsheet-A",
            "sheet_id": 7,
            "creation_kind": "fresh",
            "creation_source_hash": CREATION_SOURCE_HASH,
            "created_at": CREATED_AT,
        }
        values.update(overrides)
        return values

    @staticmethod
    def _marker_observation(**overrides):
        values = {
            "rowId": ROW_ID,
            "sheetId": 7,
            "providerRowIndex": 2,
            "displayRowNumber": 3,
            "metadataId": 11,
        }
        values.update(overrides)
        return values

    @classmethod
    def _identity(cls, module):
        return module.build_row_identity_document(**cls._identity_kwargs())

    @classmethod
    def _observation(cls, module, **marker_overrides):
        return module.build_row_observation(
            spreadsheet_id="spreadsheet-A",
            marker_observation=cls._marker_observation(**marker_overrides),
            ordered_headers=(" Email ", "Status\r\nLine"),
            ordered_cell_values=("USER@EXAMPLE.COM", "  Keep  \rValue"),
            user_scope_hash=USER_SCOPE_HASH,
        )

    @classmethod
    def _revision(cls, module, *, revision=1, lifecycle="active", **kwargs):
        identity = kwargs.pop("identity_document", cls._identity(module))
        observations = kwargs.pop("observations", (cls._observation(module),))
        previous = kwargs.pop(
            "previous_revision_hash",
            None if revision == 1 else FROZEN_A1_HASHES["revisionHash"],
        )
        observed_at = kwargs.pop(
            "observed_at",
            CREATED_AT if revision == 1 else LATER_AT,
        )
        if kwargs:
            raise AssertionError(f"unknown revision fixture fields: {kwargs}")
        return module.build_row_location_revision_document(
            identity_document=identity,
            revision=revision,
            lifecycle=lifecycle,
            observations=observations,
            previous_revision_hash=previous,
            observed_at=observed_at,
        )

    @classmethod
    def _head(cls, module):
        identity = cls._identity(module)
        revision = cls._revision(module, identity_document=identity)
        return module.build_initial_row_authority_head(
            identity_document=identity,
            location_revision_document=revision,
            created_at=CREATED_AT,
        )

    @staticmethod
    def _rehash_head(head):
        payload = {
            key: value
            for key, value in head.items()
            if key not in {"schemaVersion", "userScopeHash", "headHash"}
        }
        head["headHash"] = _reference_domain_hash(
            "sitesift.row.authority_head.v1",
            payload,
            user_scope_hash=head["userScopeHash"],
        )
        return head

    @staticmethod
    def _rehash_revision(revision):
        payload = {
            key: revision[key]
            for key in (
                "rowId",
                "revision",
                "providerRowIndex",
                "displayRowNumber",
                "metadataId",
                "rowSnapshotHash",
                "markerHash",
                "lifecycle",
                "observationEvidenceHash",
                "previousRevisionHash",
                "observedAt",
            )
        }
        revision["revisionHash"] = _reference_domain_hash(
            "sitesift.row.location.v1",
            payload,
            user_scope_hash=revision["userScopeHash"],
        )
        return revision


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


class RowIdentityHashContractTests(_RowIdentityFixtures, unittest.TestCase):
    def test_provider_text_normalization_is_exact_bounded_and_defensive(self):
        module = self._authority()
        self.assertEqual(
            "Café\n  Keep\tCase \n",
            module.normalize_provider_text(
                value="Cafe\u0301\r\n  Keep\tCase \r",
                field_name="cell",
            ),
        )
        self.assertEqual(
            unicodedata.normalize("NFC", "Cafe\u0301"),
            module.normalize_provider_text(
                value="Cafe\u0301",
                field_name="header",
            ),
        )
        for value in (None, 1, True, b"cell", "x" * 8193, "\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.normalize_provider_text(value=value, field_name="cell")
        self.assertEqual(
            "é" * 4096,
            module.normalize_provider_text(
                value="é" * 4096,
                field_name="cell",
            ),
        )
        self.assertEqual(
            module.header_hash(
                ordered_headers=("x",) * 256,
                user_scope_hash=USER_SCOPE_HASH,
            ),
            module.header_hash(
                ordered_headers=["x"] * 256,
                user_scope_hash=USER_SCOPE_HASH,
            ),
        )
        with self.assertRaises(module.RowAuthorityConfigError):
            module.header_hash(
                ordered_headers=("x",) * 257,
                user_scope_hash=USER_SCOPE_HASH,
            )
        with self.assertRaises(module.RowAuthorityConfigError):
            module.row_snapshot_hash(
                spreadsheet_id="spreadsheet-A",
                sheet_id=7,
                ordered_headers=(),
                ordered_cell_values=("x",) * 257,
                user_scope_hash=USER_SCOPE_HASH,
            )

    def test_opaque_timestamp_and_integer_inputs_are_exact(self):
        module = self._authority()

        class IntSubclass(int):
            pass

        valid = self._identity_kwargs()
        module.build_row_identity_document(**valid)
        invalid_overrides = (
            {"client_id": "Cafe\u0301"},
            {"client_id": "x" * 513},
            {"spreadsheet_id": "sheet\nA"},
            {"sheet_id": True},
            {"sheet_id": -1},
            {"sheet_id": 9007199254740992},
            {"sheet_id": IntSubclass(7)},
            {"creation_kind": ["fresh"]},
            {"created_at": "2026-8-04T12:34:56.123456Z"},
            {"created_at": "2026-02-29T12:34:56.123456Z"},
            {"created_at": "2026-08-04T24:00:00.000000Z"},
            {"created_at": "2026-08-04T12:34:56.123456+00:00"},
            {"created_at": "2026-08-04T12:34:56Z"},
        )
        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.build_row_identity_document(
                    **self._identity_kwargs(**overrides)
                )
        exact_opaque = module.build_row_identity_document(
            **self._identity_kwargs(client_id=" Client-A ")
        )
        self.assertEqual(" Client-A ", exact_opaque["clientId"])
        leap = module.build_row_identity_document(
            **self._identity_kwargs(
                created_at="2028-02-29T23:59:59.999999Z"
            )
        )
        self.assertEqual("2028-02-29T23:59:59.999999Z", leap["createdAt"])

    def test_all_a1_hashes_match_frozen_independent_vectors(self):
        module = self._authority()
        marker = module.marker_hash(
            row_id=ROW_ID,
            spreadsheet_id="spreadsheet-A",
            sheet_id=7,
            user_scope_hash=USER_SCOPE_HASH,
        )
        header = module.header_hash(
            ordered_headers=(" Email ", "Status\r\nLine"),
            user_scope_hash=USER_SCOPE_HASH,
        )
        snapshot = module.row_snapshot_hash(
            spreadsheet_id="spreadsheet-A",
            sheet_id=7,
            ordered_headers=(" Email ", "Status\r\nLine"),
            ordered_cell_values=("USER@EXAMPLE.COM", "  Keep  \rValue"),
            user_scope_hash=USER_SCOPE_HASH,
        )
        observation = self._observation(module)
        evidence = module.observation_evidence_hash(
            lifecycle="active",
            observations=(observation,),
            user_scope_hash=USER_SCOPE_HASH,
        )
        identity = self._identity(module)
        revision = self._revision(module, identity_document=identity)
        head = module.build_initial_row_authority_head(
            identity_document=identity,
            location_revision_document=revision,
            created_at=CREATED_AT,
        )
        actual = {
            "markerHash": marker,
            "identityHash": identity["identityHash"],
            "headerHash": header,
            "rowSnapshotHash": snapshot,
            "observationEvidenceHash": evidence,
            "revisionHash": revision["revisionHash"],
            "headHash": head["headHash"],
        }
        self.assertEqual(FROZEN_A1_HASHES, actual)
        independent = {
            "markerHash": _reference_domain_hash(
                "sitesift.row.marker.v1",
                {
                    "rowId": ROW_ID,
                    "markerKey": "sitesift_row_id_v1",
                    "markerValue": ROW_ID,
                    "visibility": "DOCUMENT",
                    "spreadsheetId": "spreadsheet-A",
                    "sheetId": 7,
                },
            ),
            "identityHash": _reference_domain_hash(
                "sitesift.row.identity.v1",
                {
                    "rowId": ROW_ID,
                    "clientId": "client-A",
                    "spreadsheetId": "spreadsheet-A",
                    "sheetId": 7,
                    "markerHash": marker,
                    "creationKind": "fresh",
                    "creationSourceHash": CREATION_SOURCE_HASH,
                },
            ),
            "headerHash": _reference_domain_hash(
                "sitesift.row.header.v1",
                {"orderedHeaders": [" Email ", "Status\nLine"]},
            ),
            "rowSnapshotHash": _reference_domain_hash(
                "sitesift.row.snapshot.v1",
                {
                    "spreadsheetId": "spreadsheet-A",
                    "sheetId": 7,
                    "headerHash": header,
                    "orderedCellValues": [
                        "USER@EXAMPLE.COM",
                        "  Keep  \nValue",
                    ],
                },
            ),
            "observationEvidenceHash": _reference_domain_hash(
                "sitesift.row.observation_evidence.v1",
                {
                    "observationKind": "active",
                    "observations": [observation],
                },
            ),
        }
        revision_payload = {
            key: revision[key]
            for key in (
                "rowId",
                "revision",
                "providerRowIndex",
                "displayRowNumber",
                "metadataId",
                "rowSnapshotHash",
                "markerHash",
                "lifecycle",
                "observationEvidenceHash",
                "previousRevisionHash",
                "observedAt",
            )
        }
        independent["revisionHash"] = _reference_domain_hash(
            "sitesift.row.location.v1",
            revision_payload,
        )
        head_payload = {
            key: value
            for key, value in head.items()
            if key not in {"schemaVersion", "userScopeHash", "headHash"}
        }
        independent["headHash"] = _reference_domain_hash(
            "sitesift.row.authority_head.v1",
            head_payload,
        )
        self.assertEqual(FROZEN_A1_HASHES, independent)

    def test_hashes_change_for_field_null_scope_order_and_domain_drift(self):
        module = self._authority()
        base_marker = module.marker_hash(
            row_id=ROW_ID,
            spreadsheet_id="spreadsheet-A",
            sheet_id=7,
            user_scope_hash=USER_SCOPE_HASH,
        )
        marker_variants = (
            module.marker_hash(
                row_id=SECOND_ROW_ID,
                spreadsheet_id="spreadsheet-A",
                sheet_id=7,
                user_scope_hash=USER_SCOPE_HASH,
            ),
            module.marker_hash(
                row_id=ROW_ID,
                spreadsheet_id="spreadsheet-B",
                sheet_id=7,
                user_scope_hash=USER_SCOPE_HASH,
            ),
            module.marker_hash(
                row_id=ROW_ID,
                spreadsheet_id="spreadsheet-A",
                sheet_id=8,
                user_scope_hash=USER_SCOPE_HASH,
            ),
            module.marker_hash(
                row_id=ROW_ID,
                spreadsheet_id="spreadsheet-A",
                sheet_id=7,
                user_scope_hash="2" * 64,
            ),
        )
        self.assertNotIn(base_marker, marker_variants)
        self.assertNotEqual(
            module.header_hash(
                ordered_headers=("A", "B"),
                user_scope_hash=USER_SCOPE_HASH,
            ),
            module.header_hash(
                ordered_headers=("B", "A"),
                user_scope_hash=USER_SCOPE_HASH,
            ),
        )
        payload = {"nullable": None, "value": 1}
        self.assertNotEqual(
            module.domain_hash(
                "sitesift.row.location.v1",
                payload,
                user_scope_hash=USER_SCOPE_HASH,
            ),
            module.domain_hash(
                "sitesift.row.location.v2",
                payload,
                user_scope_hash=USER_SCOPE_HASH,
            ),
        )
        self.assertNotEqual(
            module.domain_hash(
                "sitesift.row.location.v1",
                payload,
                user_scope_hash=USER_SCOPE_HASH,
            ),
            module.domain_hash(
                "sitesift.row.location.v1",
                {"nullable": "", "value": 1},
                user_scope_hash=USER_SCOPE_HASH,
            ),
        )

    def test_observation_builder_derives_exact_fields_and_rejects_overrides(self):
        module = self._authority()
        marker = self._marker_observation()
        observation = module.build_row_observation(
            spreadsheet_id="spreadsheet-A",
            marker_observation=marker,
            ordered_headers=(" Email ", "Status\r\nLine"),
            ordered_cell_values=("USER@EXAMPLE.COM", "  Keep  \rValue"),
            user_scope_hash=USER_SCOPE_HASH,
        )
        self.assertEqual(
            {
                "providerRowIndex",
                "displayRowNumber",
                "metadataId",
                "markerHash",
                "rowSnapshotHash",
            },
            set(observation),
        )
        self.assertEqual(FROZEN_A1_HASHES["markerHash"], observation["markerHash"])
        self.assertEqual(
            FROZEN_A1_HASHES["rowSnapshotHash"],
            observation["rowSnapshotHash"],
        )
        marker["providerRowIndex"] = 99
        self.assertEqual(2, observation["providerRowIndex"])
        for mutation in (
            {"unknown": True},
            {"displayRowNumber": 4},
            {"sheetId": -1},
            {"metadataId": True},
        ):
            invalid = self._marker_observation()
            invalid.update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.build_row_observation(
                    spreadsheet_id="spreadsheet-A",
                    marker_observation=invalid,
                    ordered_headers=(),
                    ordered_cell_values=(),
                    user_scope_hash=USER_SCOPE_HASH,
                )
        other_sheet = module.build_row_observation(
            spreadsheet_id="spreadsheet-A",
            marker_observation=self._marker_observation(sheetId=8),
            ordered_headers=(),
            ordered_cell_values=(),
            user_scope_hash=USER_SCOPE_HASH,
        )
        self.assertNotEqual(observation["markerHash"], other_sheet["markerHash"])

    def test_evidence_is_sorted_bounded_and_lifecycle_specific(self):
        module = self._authority()
        first = self._observation(module)
        second = self._observation(
            module,
            providerRowIndex=4,
            displayRowNumber=5,
            metadataId=12,
        )
        forward = module.observation_evidence_hash(
            lifecycle="ambiguous",
            observations=(first, second),
            user_scope_hash=USER_SCOPE_HASH,
        )
        reverse = module.observation_evidence_hash(
            lifecycle="ambiguous",
            observations=(second, first),
            user_scope_hash=USER_SCOPE_HASH,
        )
        self.assertEqual(forward, reverse)
        self.assertEqual(
            module.observation_evidence_hash(
                lifecycle="deleted",
                observations=(),
                user_scope_hash=USER_SCOPE_HASH,
            ),
            module.observation_evidence_hash(
                lifecycle="deleted",
                observations=[],
                user_scope_hash=USER_SCOPE_HASH,
            ),
        )
        invalid = (
            ("active", ()),
            ("active", (first, second)),
            ("deleted", (first,)),
            ("ambiguous", (first,)),
            ("ambiguous", (first,) * 129),
            ("unknown", (first,)),
            ([], (first,)),
        )
        for lifecycle, observations in invalid:
            with self.subTest(lifecycle=lifecycle), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.observation_evidence_hash(
                    lifecycle=lifecycle,
                    observations=observations,
                    user_scope_hash=USER_SCOPE_HASH,
                )


class RowIdentityDocumentSchemaTests(_RowIdentityFixtures, unittest.TestCase):
    IDENTITY_KEYS = {
        "schemaVersion",
        "userScopeHash",
        "rowId",
        "clientId",
        "spreadsheetId",
        "sheetId",
        "markerKey",
        "markerValue",
        "creationKind",
        "creationSourceHash",
        "markerHash",
        "identityHash",
        "createdAt",
    }
    REVISION_KEYS = {
        "schemaVersion",
        "userScopeHash",
        "rowId",
        "revision",
        "spreadsheetId",
        "sheetId",
        "providerRowIndex",
        "displayRowNumber",
        "metadataId",
        "markerHash",
        "rowSnapshotHash",
        "lifecycle",
        "observationEvidenceHash",
        "previousRevisionHash",
        "revisionHash",
        "observedAt",
    }
    HEAD_KEYS = {
        "schemaVersion",
        "userScopeHash",
        "rowId",
        "stateRevision",
        "currentLocationRevision",
        "currentLocationHash",
        "currentLocationLifecycle",
        "effectiveOwnerGeneration",
        "effectiveOwnerGenerationHash",
        "effectiveOwnerKind",
        "effectivePriority",
        "state",
        "leaseOwnerHash",
        "leaseUntil",
        "fencingToken",
        "latestSettlementHash",
        "effectiveSettlementHash",
        "latestSourceSettlementLinkHash",
        "latestOptOutReleaseResultHash",
        "projectionBacklogCount",
        "headHash",
        "createdAt",
        "updatedAt",
    }

    def _owned_head(self, module, *, state):
        head = self._head(module)
        head.update(
            {
                "effectiveOwnerGeneration": 1,
                "effectiveOwnerGenerationHash": "a" * 64,
                "effectiveOwnerKind": (
                    "human_decision" if state == "review_pending" else "terminal"
                ),
                "effectivePriority": 1 if state == "review_pending" else 2,
                "state": state,
                "fencingToken": 1,
            }
        )
        if state in {"claimed", "review_pending"}:
            head.update(
                {
                    "leaseOwnerHash": "b" * 64,
                    "leaseUntil": LATER_AT,
                    "latestSettlementHash": None,
                    "effectiveSettlementHash": None,
                }
            )
        elif state == "settled":
            head.update(
                {
                    "leaseOwnerHash": None,
                    "leaseUntil": None,
                    "latestSettlementHash": "c" * 64,
                    "effectiveSettlementHash": "c" * 64,
                }
            )
        else:
            raise AssertionError(f"unsupported fixture state: {state}")
        return self._rehash_head(head)

    def test_identity_builder_and_validator_use_exact_defensive_schema(self):
        module = self._authority()
        identity = self._identity(module)
        self.assertEqual(self.IDENTITY_KEYS, set(identity))
        self.assertEqual(1, identity["schemaVersion"])
        self.assertEqual(ROW_ID, identity["rowId"])
        self.assertEqual(ROW_ID, identity["markerValue"])
        self.assertEqual("sitesift_row_id_v1", identity["markerKey"])
        self.assertEqual(FROZEN_A1_HASHES["markerHash"], identity["markerHash"])
        self.assertEqual(FROZEN_A1_HASHES["identityHash"], identity["identityHash"])
        validated = module.validate_row_identity_document(document=identity)
        self.assertEqual(identity, validated)
        self.assertIsNot(identity, validated)
        identity["clientId"] = "mutated"
        self.assertEqual("client-A", validated["clientId"])
        migration = module.build_row_identity_document(
            **self._identity_kwargs(creation_kind="migration")
        )
        self.assertEqual("migration", migration["creationKind"])

    def test_identity_validator_rejects_missing_unknown_drift_and_bad_inputs(self):
        module = self._authority()
        identity = self._identity(module)
        variants = []
        for key in self.IDENTITY_KEYS:
            missing = deepcopy(identity)
            missing.pop(key)
            variants.append(missing)
        unknown = deepcopy(identity)
        unknown["unknown"] = True
        variants.append(unknown)
        for field, value in (
            ("schemaVersion", True),
            ("userScopeHash", "A" * 64),
            ("rowId", SECOND_ROW_ID),
            ("sheetId", True),
            ("markerKey", "legacy"),
            ("markerValue", SECOND_ROW_ID),
            ("creationKind", "import"),
            ("markerHash", "2" * 64),
            ("identityHash", "3" * 64),
            ("createdAt", "2026-08-04T12:34:56Z"),
        ):
            drift = deepcopy(identity)
            drift[field] = value
            variants.append(drift)
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.validate_row_identity_document(document=variant)

    def test_location_revision_schemas_follow_lifecycle_and_hash_rules(self):
        module = self._authority()
        identity = self._identity(module)
        active = self._revision(module, identity_document=identity)
        self.assertEqual(self.REVISION_KEYS, set(active))
        self.assertEqual(2, active["providerRowIndex"])
        self.assertEqual(3, active["displayRowNumber"])
        self.assertEqual(11, active["metadataId"])
        self.assertEqual(
            FROZEN_A1_HASHES["rowSnapshotHash"],
            active["rowSnapshotHash"],
        )
        self.assertEqual(FROZEN_A1_HASHES["revisionHash"], active["revisionHash"])
        self.assertEqual(
            active,
            module.validate_row_location_revision_document(
                document=active,
                identity_document=identity,
            ),
        )
        nonviable = self._revision(
            module,
            identity_document=identity,
            lifecycle="nonviable",
        )
        self.assertEqual("nonviable", nonviable["lifecycle"])
        deleted = self._revision(
            module,
            identity_document=identity,
            lifecycle="deleted",
            observations=(),
        )
        ambiguous = self._revision(
            module,
            identity_document=identity,
            lifecycle="ambiguous",
            observations=(
                self._observation(module),
                self._observation(
                    module,
                    providerRowIndex=4,
                    displayRowNumber=5,
                    metadataId=12,
                ),
            ),
        )
        for document in (deleted, ambiguous):
            with self.subTest(lifecycle=document["lifecycle"]):
                self.assertIsNone(document["providerRowIndex"])
                self.assertIsNone(document["displayRowNumber"])
                self.assertIsNone(document["metadataId"])
                self.assertIsNone(document["rowSnapshotHash"])
                self.assertEqual(
                    document,
                    module.validate_row_location_revision_document(
                        document=document,
                        identity_document=identity,
                    ),
                )

    def test_revision_builder_rejects_bad_sequence_observation_and_identity(self):
        module = self._authority()
        identity = self._identity(module)
        observation = self._observation(module)
        invalid_calls = (
            {
                "revision": 1,
                "previous_revision_hash": "a" * 64,
                "lifecycle": "active",
                "observations": (observation,),
            },
            {
                "revision": 2,
                "previous_revision_hash": None,
                "lifecycle": "active",
                "observations": (observation,),
                "observed_at": LATER_AT,
            },
            {
                "revision": True,
                "previous_revision_hash": None,
                "lifecycle": "active",
                "observations": (observation,),
            },
            {
                "revision": 1,
                "previous_revision_hash": None,
                "lifecycle": "deleted",
                "observations": (observation,),
            },
        )
        for values in invalid_calls:
            with self.subTest(values=values), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.build_row_location_revision_document(
                    identity_document=identity,
                    observed_at=values.pop("observed_at", CREATED_AT),
                    **values,
                )
        wrong_marker = deepcopy(observation)
        wrong_marker["markerHash"] = "2" * 64
        with self.assertRaises(module.RowAuthorityConfigError):
            module.build_row_location_revision_document(
                identity_document=identity,
                revision=1,
                lifecycle="active",
                observations=(wrong_marker,),
                previous_revision_hash=None,
                observed_at=CREATED_AT,
            )

    def test_revision_validator_rejects_schema_hash_geometry_and_scope_drift(self):
        module = self._authority()
        identity = self._identity(module)
        revision = self._revision(module, identity_document=identity)
        variants = []
        for key in self.REVISION_KEYS:
            missing = deepcopy(revision)
            missing.pop(key)
            variants.append(missing)
        unknown = deepcopy(revision)
        unknown["unknown"] = True
        variants.append(unknown)
        for field, value in (
            ("schemaVersion", True),
            ("userScopeHash", "2" * 64),
            ("rowId", SECOND_ROW_ID),
            ("revision", True),
            ("sheetId", 8),
            ("displayRowNumber", 4),
            ("metadataId", 0),
            ("lifecycle", "missing"),
            ("previousRevisionHash", "a" * 64),
            ("revisionHash", "3" * 64),
            ("observedAt", "not-a-time"),
        ):
            drift = deepcopy(revision)
            drift[field] = value
            variants.append(drift)
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.validate_row_location_revision_document(
                    document=variant,
                    identity_document=identity,
                )

    def test_initial_head_is_exact_clear_revision_one_and_defensive(self):
        module = self._authority()
        identity = self._identity(module)
        revision = self._revision(module, identity_document=identity)
        head = module.build_initial_row_authority_head(
            identity_document=identity,
            location_revision_document=revision,
            created_at=CREATED_AT,
        )
        self.assertEqual(self.HEAD_KEYS, set(head))
        self.assertEqual(1, head["stateRevision"])
        self.assertEqual(1, head["currentLocationRevision"])
        self.assertEqual("clear", head["state"])
        self.assertEqual(0, head["projectionBacklogCount"])
        for field in (
            "effectiveOwnerGeneration",
            "effectiveOwnerGenerationHash",
            "effectiveOwnerKind",
            "effectivePriority",
            "leaseOwnerHash",
            "leaseUntil",
            "fencingToken",
            "latestSettlementHash",
            "effectiveSettlementHash",
            "latestSourceSettlementLinkHash",
            "latestOptOutReleaseResultHash",
        ):
            with self.subTest(field=field):
                self.assertIsNone(head[field])
        self.assertEqual(FROZEN_A1_HASHES["headHash"], head["headHash"])
        validated = module.validate_row_authority_head(document=head)
        self.assertEqual(head, validated)
        self.assertIsNot(head, validated)
        with self.assertRaises(module.RowAuthorityConfigError):
            module.build_initial_row_authority_head(
                identity_document=identity,
                location_revision_document=revision,
                created_at=LATER_AT,
            )

    def test_claimed_pending_and_settled_heads_validate_and_location_preserves(self):
        module = self._authority()
        for state in ("claimed", "review_pending", "settled"):
            with self.subTest(state=state):
                head = self._owned_head(module, state=state)
                self.assertEqual(
                    head,
                    module.validate_row_authority_head(document=head),
                )
        claimed = self._owned_head(module, state="claimed")
        identity = self._identity(module)
        next_observation = self._observation(
            module,
            providerRowIndex=4,
            displayRowNumber=5,
            metadataId=11,
        )
        next_revision = self._revision(
            module,
            revision=2,
            identity_document=identity,
            observations=(next_observation,),
        )
        advanced = module.build_location_advanced_head(
            expected_head=claimed,
            location_revision_document=next_revision,
        )
        self.assertEqual(2, advanced["stateRevision"])
        self.assertEqual(2, advanced["currentLocationRevision"])
        self.assertEqual(next_revision["revisionHash"], advanced["currentLocationHash"])
        self.assertEqual(LATER_AT, advanced["updatedAt"])
        for field in (
            "effectiveOwnerGeneration",
            "effectiveOwnerGenerationHash",
            "effectiveOwnerKind",
            "effectivePriority",
            "state",
            "leaseOwnerHash",
            "leaseUntil",
            "fencingToken",
            "latestSettlementHash",
            "effectiveSettlementHash",
            "latestSourceSettlementLinkHash",
            "latestOptOutReleaseResultHash",
            "projectionBacklogCount",
            "createdAt",
        ):
            with self.subTest(field=field):
                self.assertEqual(claimed[field], advanced[field])
        self.assertNotEqual(claimed["headHash"], advanced["headHash"])
        invalid_revisions = []
        wrong_row = deepcopy(next_revision)
        wrong_row["rowId"] = SECOND_ROW_ID
        invalid_revisions.append(self._rehash_revision(wrong_row))
        wrong_scope = deepcopy(next_revision)
        wrong_scope["userScopeHash"] = "2" * 64
        invalid_revisions.append(self._rehash_revision(wrong_scope))
        skipped = deepcopy(next_revision)
        skipped["revision"] = 3
        invalid_revisions.append(self._rehash_revision(skipped))
        wrong_predecessor = deepcopy(next_revision)
        wrong_predecessor["previousRevisionHash"] = "d" * 64
        invalid_revisions.append(self._rehash_revision(wrong_predecessor))
        for invalid_revision in invalid_revisions:
            with self.subTest(invalid_revision=invalid_revision), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.build_location_advanced_head(
                    expected_head=claimed,
                    location_revision_document=invalid_revision,
                )

    def test_head_validator_rejects_exact_schema_type_hash_and_null_drift(self):
        module = self._authority()
        clear = self._head(module)
        variants = []
        for key in self.HEAD_KEYS:
            missing = deepcopy(clear)
            missing.pop(key)
            variants.append(missing)
        unknown = deepcopy(clear)
        unknown["unknown"] = True
        variants.append(unknown)
        for field, value in (
            ("schemaVersion", True),
            ("rowId", SECOND_ROW_ID),
            ("stateRevision", True),
            ("currentLocationLifecycle", "missing"),
            ("state", "pending"),
            ("projectionBacklogCount", -1),
            ("headHash", "2" * 64),
            ("updatedAt", "bad-time"),
        ):
            drift = deepcopy(clear)
            drift[field] = value
            variants.append(drift)
        clear_owner = deepcopy(clear)
        clear_owner["effectiveOwnerGeneration"] = 1
        self._rehash_head(clear_owner)
        variants.append(clear_owner)
        claimed_missing_lease = self._owned_head(module, state="claimed")
        claimed_missing_lease["leaseOwnerHash"] = None
        self._rehash_head(claimed_missing_lease)
        variants.append(claimed_missing_lease)
        settled_with_lease = self._owned_head(module, state="settled")
        settled_with_lease["leaseOwnerHash"] = "b" * 64
        settled_with_lease["leaseUntil"] = LATER_AT
        self._rehash_head(settled_with_lease)
        variants.append(settled_with_lease)
        settled_without_effective = self._owned_head(module, state="settled")
        settled_without_effective["effectiveSettlementHash"] = None
        self._rehash_head(settled_without_effective)
        variants.append(settled_without_effective)
        boolean_priority = self._owned_head(module, state="review_pending")
        boolean_priority["effectivePriority"] = True
        self._rehash_head(boolean_priority)
        variants.append(boolean_priority)
        mistyped_state = deepcopy(clear)
        mistyped_state["state"] = ["clear"]
        self._rehash_head(mistyped_state)
        variants.append(mistyped_state)
        reversed_time = deepcopy(clear)
        reversed_time["createdAt"] = LATER_AT
        self._rehash_head(reversed_time)
        variants.append(reversed_time)
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.validate_row_authority_head(document=variant)


class RowIdentityInitializationTests(_RowIdentityFixtures, unittest.TestCase):
    @staticmethod
    def _fakes():
        return importlib.import_module("tests.row_authority_fakes")

    @classmethod
    def _references(cls, store):
        user = store.collection("users").document("uid-1")
        return (
            user.collection("rowIdentities").document(ROW_ID),
            user.collection("rowLocationRevisions").document(f"{ROW_ID}--1"),
            user.collection("rowAuthorityHeads").document(ROW_ID),
        )

    @classmethod
    def _initialize(cls, module, store, *, executor=None, **overrides):
        if executor is None:
            executor = cls._fakes().run_bounded_transaction
        coordinator = module.RowAuthorityStore(
            store,
            transaction_executor=executor,
        )
        arguments = {
            "verified_user_id": "uid-1",
            "client_id": "client-A",
            "spreadsheet_id": "spreadsheet-A",
            "marker_observation": cls._marker_observation(),
            "headers": (" Email ", "Status\r\nLine"),
            "cells": ("USER@EXAMPLE.COM", "  Keep  \rValue"),
            "lifecycle": "active",
            "creation_kind": "fresh",
            "creation_source_hash": CREATION_SOURCE_HASH,
            "created_at": CREATED_AT,
        }
        arguments.update(overrides)
        return coordinator.initialize_row_identity(**arguments)

    def test_initialization_creates_exact_three_documents_atomically(self):
        module = self._authority()
        store = self._fakes().BoundedFakeFirestore()
        result = self._initialize(module, store)
        self.assertEqual(
            {"disposition", "identity", "locationRevision", "authorityHead"},
            set(result),
        )
        self.assertEqual("created", result["disposition"])
        references = self._references(store)
        self.assertEqual({reference.path for reference in references}, set(store.data))
        self.assertEqual(result["identity"], store.data[references[0].path])
        self.assertEqual(
            result["locationRevision"],
            store.data[references[1].path],
        )
        self.assertEqual(result["authorityHead"], store.data[references[2].path])
        get_indexes = [
            index
            for index, event in enumerate(store.events)
            if event[0] == "get"
        ]
        create_indexes = [
            index
            for index, event in enumerate(store.events)
            if event[0] == "create"
        ]
        self.assertEqual(3, len(get_indexes))
        self.assertEqual(3, len(create_indexes))
        self.assertLess(max(get_indexes), min(create_indexes))
        self.assertIn(("commit_applied", 3), store.events)
        result["identity"]["clientId"] = "mutated"
        self.assertEqual("client-A", store.data[references[0].path]["clientId"])

    def test_initialization_supports_active_and_nonviable_only(self):
        module = self._authority()
        fakes = self._fakes()
        for lifecycle in ("active", "nonviable"):
            with self.subTest(lifecycle=lifecycle):
                store = fakes.BoundedFakeFirestore()
                result = self._initialize(module, store, lifecycle=lifecycle)
                self.assertEqual(
                    lifecycle,
                    result["locationRevision"]["lifecycle"],
                )
        for lifecycle in ("deleted", "ambiguous", "unknown", None):
            with self.subTest(lifecycle=lifecycle):
                store = fakes.BoundedFakeFirestore()
                with self.assertRaises(module.RowAuthorityConfigError):
                    self._initialize(module, store, lifecycle=lifecycle)
                self.assertEqual([], store.events)
                self.assertEqual({}, store.data)

    def test_exact_initialization_retry_is_zero_write_noop(self):
        module = self._authority()
        store = self._fakes().BoundedFakeFirestore()
        created = self._initialize(module, store)
        data_before = deepcopy(store.data)
        store.events.clear()
        existing = self._initialize(module, store)
        self.assertEqual("created", created["disposition"])
        self.assertEqual("existing", existing["disposition"])
        self.assertEqual(data_before, store.data)
        self.assertFalse(
            any(event[0] in {"create", "set", "update", "delete"} for event in store.events)
        )
        self.assertIn(("commit_applied", 0), store.events)

        retry_store = self._fakes().BoundedFakeFirestore()

        def prepare_then_observe_existing_then_raise(transaction, callback):
            transaction._begin(retry_id=0)
            callback(transaction)
            operations = [
                (reference, deepcopy(payload))
                for _operation, reference, payload, _merge
                in transaction._operations
            ]
            transaction._rollback()
            for reference, payload in operations:
                reference.create(payload)
            transaction._begin(retry_id=1)
            self.assertEqual("existing", callback(transaction))
            transaction._rollback()
            raise module.RowAuthorityRetryable(
                "zero-write retry outcome was unknown"
            )

        retried = self._initialize(
            module,
            retry_store,
            executor=prepare_then_observe_existing_then_raise,
        )
        self.assertEqual("existing", retried["disposition"])
        self.assertEqual(3, len(retry_store.data))

    def test_partial_existing_state_is_ambiguous_with_zero_writes(self):
        module = self._authority()
        fakes = self._fakes()
        source_store = fakes.BoundedFakeFirestore()
        expected = self._initialize(module, source_store)
        store = fakes.BoundedFakeFirestore()
        references = self._references(store)
        references[0].create(expected["identity"])
        data_before = deepcopy(store.data)
        store.events.clear()
        with self.assertRaises(module.RowAuthorityAmbiguous):
            self._initialize(module, store)
        self.assertEqual(data_before, store.data)
        self.assertFalse(
            any(event[0] in {"create", "set", "update", "delete"} for event in store.events)
        )

    def test_any_existing_document_drift_is_ambiguous_with_zero_writes(self):
        module = self._authority()
        fakes = self._fakes()
        source_store = fakes.BoundedFakeFirestore()
        expected = self._initialize(module, source_store)
        documents = (
            expected["identity"],
            expected["locationRevision"],
            expected["authorityHead"],
        )
        for drift_index in range(3):
            with self.subTest(drift_index=drift_index):
                store = fakes.BoundedFakeFirestore()
                references = self._references(store)
                for index, (reference, document) in enumerate(
                    zip(references, documents)
                ):
                    payload = deepcopy(document)
                    if index == drift_index:
                        payload["schemaVersion"] = 2
                    reference.create(payload)
                data_before = deepcopy(store.data)
                store.events.clear()
                with self.assertRaises(module.RowAuthorityAmbiguous):
                    self._initialize(module, store)
                self.assertEqual(data_before, store.data)
                self.assertFalse(
                    any(
                        event[0] in {"create", "set", "update", "delete"}
                        for event in store.events
                    )
                )

    def test_invalid_marker_snapshot_timestamp_and_scope_fail_before_transaction(self):
        module = self._authority()
        fakes = self._fakes()
        invalid_overrides = (
            {"marker_observation": {**self._marker_observation(), "unknown": True}},
            {"headers": ("x",) * 257},
            {"cells": ("x",) * 257},
            {"created_at": "not-a-time"},
            {"verified_user_id": "uid\n1"},
            {"creation_source_hash": "short"},
        )
        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides):
                store = fakes.BoundedFakeFirestore()
                with self.assertRaises(module.RowAuthorityConfigError):
                    self._initialize(module, store, **overrides)
                self.assertEqual([], store.events)
                self.assertEqual({}, store.data)

    def test_preapply_commit_failure_has_zero_writes_and_is_retryable(self):
        module = self._authority()
        fakes = self._fakes()
        store = fakes.BoundedFakeFirestore()
        store.fail_next_commit = RuntimeError("preapply failure")
        with self.assertRaises(module.RowAuthorityRetryable):
            self._initialize(module, store)
        self.assertEqual({}, store.data)
        self.assertIn(("commit_failed_before_apply",), store.events)

        start_store = fakes.BoundedFakeFirestore()

        for failure in (
            RuntimeError("executor could not start"),
            module.RowAuthorityAmbiguous("domain ambiguity before callback"),
            module.RowAuthorityConfigError("domain config before callback"),
        ):
            with self.subTest(failure=type(failure).__name__):
                start_store = fakes.BoundedFakeFirestore()

                def cannot_start(_transaction, _callback):
                    raise failure

                with self.assertRaises(module.RowAuthorityRetryable):
                    self._initialize(module, start_store, executor=cannot_start)
                self.assertEqual({}, start_store.data)
                self.assertFalse(
                    any(
                        event[0] == "transaction_began"
                        for event in start_store.events
                    )
                )

    def test_apply_then_raise_succeeds_only_after_exact_three_document_readback(self):
        module = self._authority()
        store = self._fakes().BoundedFakeFirestore()
        store.apply_then_raise_next_commit = RuntimeError("unknown commit")
        result = self._initialize(module, store)
        self.assertEqual("created", result["disposition"])
        self.assertEqual(3, len(store.data))
        self.assertIn(("commit_raised_after_apply",), store.events)
        references = self._references(store)
        self.assertEqual(
            [
                result["identity"],
                result["locationRevision"],
                result["authorityHead"],
            ],
            [store.data[reference.path] for reference in references],
        )
        for failure in (
            module.RowAuthorityRetryable("domain retryable after apply"),
            module.RowAuthorityAmbiguous("domain ambiguous after apply"),
        ):
            with self.subTest(failure=type(failure).__name__):
                domain_store = self._fakes().BoundedFakeFirestore()
                domain_store.apply_then_raise_next_commit = failure
                domain_result = self._initialize(module, domain_store)
                self.assertEqual("created", domain_result["disposition"])
                self.assertEqual(3, len(domain_store.data))

    def test_apply_then_raise_partial_or_drifted_readback_is_ambiguous(self):
        module = self._authority()
        fakes = self._fakes()

        def executor_with_readback(store, *, mode):
            def execute(transaction, callback):
                transaction._begin()
                callback(transaction)
                operations = [
                    (operation, reference, deepcopy(payload), merge)
                    for operation, reference, payload, merge
                    in transaction._operations
                ]
                transaction._rollback()
                selected = operations[:1] if mode == "partial" else operations
                for index, (_operation, reference, payload, _merge) in enumerate(
                    selected
                ):
                    applied = deepcopy(payload)
                    if mode == "drift" and index == len(selected) - 1:
                        applied["schemaVersion"] = 2
                    reference.create(applied)
                raise RuntimeError(f"{mode} unknown commit")

            return execute

        for mode in ("partial", "drift"):
            with self.subTest(mode=mode):
                store = fakes.BoundedFakeFirestore()
                with self.assertRaises(module.RowAuthorityAmbiguous):
                    self._initialize(
                        module,
                        store,
                        executor=executor_with_readback(store, mode=mode),
                    )
                self.assertNotEqual(3, sum(
                    payload.get("schemaVersion") == 1
                    for payload in store.data.values()
                ))

    def test_initialization_never_stores_raw_verified_user_or_mailbox_material(self):
        module = self._authority()
        store = self._fakes().BoundedFakeFirestore()
        result = self._initialize(
            module,
            store,
            headers=("Email",),
            cells=("private@example.com",),
        )
        stored_payloads = json.dumps(
            list(store.data.values()),
            sort_keys=True,
        )
        result_payloads = json.dumps(result, sort_keys=True)
        for forbidden in ("uid-1", "private@example.com"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, stored_payloads)
                self.assertNotIn(forbidden, result_payloads)

    def test_initialization_calculates_three_writes_below_the_bound(self):
        module = self._authority()
        fakes = self._fakes()
        self.assertGreaterEqual(module.MAX_ROW_AUTHORITY_PLANNED_WRITES, 3)
        store = fakes.BoundedFakeFirestore()
        self._initialize(module, store)
        self.assertIn(("commit_applied", 3), store.events)
        bounded_store = fakes.BoundedFakeFirestore()
        with patch.object(module, "MAX_ROW_AUTHORITY_PLANNED_WRITES", 2):
            with self.assertRaises(module.RowAuthorityConfigError):
                self._initialize(module, bounded_store)
        self.assertEqual([], bounded_store.events)
        self.assertEqual({}, bounded_store.data)


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
