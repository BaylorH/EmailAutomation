"""Focused contracts for provider-free B2 row authority."""

from __future__ import annotations

import importlib
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ROW_AUTHORITY_FAKES_PATH = REPO_ROOT / "tests" / "row_authority_fakes.py"


class BoundedRowAuthorityFakeTests(unittest.TestCase):
    def _load_fakes(self):
        self.assertTrue(
            ROW_AUTHORITY_FAKES_PATH.exists(),
            "row authority fakes module is missing",
        )
        return importlib.import_module("tests.row_authority_fakes")

    def test_invalid_write_ceilings_are_rejected(self):
        module = self._load_fakes()
        for value in (True, False, 0, -1, 1.5, "400", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.BoundedFakeFirestore(max_writes_per_commit=value)

    def test_exactly_400_writes_commit(self):
        module = self._load_fakes()
        store = module.BoundedFakeFirestore(max_writes_per_commit=400)
        transaction = store.transaction()
        for index in range(400):
            transaction.create(
                store.collection("bounded").document(str(index)),
                {"index": index},
            )
        transaction.commit()
        self.assertEqual(400, len(store.data))
        self.assertIn(("commit_applied", 400), store.events)

    def test_401_writes_fail_before_any_apply(self):
        module = self._load_fakes()
        store = module.BoundedFakeFirestore(max_writes_per_commit=400)
        transaction = store.transaction()
        for index in range(401):
            transaction.create(
                store.collection("bounded").document(str(index)),
                {"index": index},
            )
        with self.assertRaisesRegex(RuntimeError, "400-write ceiling"):
            transaction.commit()
        self.assertEqual({}, store.data)
        self.assertIn(
            ("commit_refused_write_ceiling", 401, 400),
            store.events,
        )
        self.assertNotIn(("commit_applied", 401), store.events)


if __name__ == "__main__":
    unittest.main()
