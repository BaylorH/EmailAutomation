"""Focused B2-A1 identity/location harness and containment contracts."""

from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path

from tests import test_row_authority_contracts as authority_contracts
from tests.source_coordinator_fakes import FakeTransactionAborted


REPO_ROOT = Path(__file__).resolve().parents[1]
ROW_ID = "sr1_123e4567e89b42d3a456426614174000"


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
