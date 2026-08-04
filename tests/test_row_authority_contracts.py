"""Focused contracts for provider-free B2 row authority."""

from __future__ import annotations

import importlib
import unittest
from copy import deepcopy
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

        class IntSubclass(int):
            pass

        for value in (
            True,
            False,
            0,
            -1,
            1.5,
            "400",
            None,
            IntSubclass(400),
        ):
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

        class FailOnCallBarrier:
            def wait(self, timeout=5):
                raise AssertionError(
                    f"commit barrier was touched with timeout {timeout}"
                )

        store = module.BoundedFakeFirestore(max_writes_per_commit=400)
        store.collection("seeded").document("existing").create(
            {"seeded": True}
        )
        data_before = deepcopy(store.data)
        versions_before = dict(store._versions)
        version_clock_before = store._version_clock
        store.events.clear()
        store.before_commit_barrier = FailOnCallBarrier()

        transaction = store.transaction()
        for index in range(401):
            transaction.create(
                store.collection("bounded").document(str(index)),
                {"index": index},
            )
        with self.assertRaisesRegex(RuntimeError, "400-write ceiling"):
            transaction.commit()
        self.assertEqual(data_before, store.data)
        self.assertEqual(versions_before, store._versions)
        self.assertEqual(version_clock_before, store._version_clock)
        self.assertEqual(
            [("commit_refused_write_ceiling", 401, 400)],
            store.events,
        )


import ast
import hashlib
import json
from types import MappingProxyType
from uuid import UUID


ROW_AUTHORITY_PATH = REPO_ROOT / "email_automation" / "row_authority.py"
ROW_AUTHORITY_STANDARD_LIBRARY_IMPORTS = frozenset(
    {"__future__", "hashlib", "json", "re", "unicodedata", "uuid"}
)
ROW_AUTHORITY_IMPORTER_ALLOWLIST = frozenset()
_APPLICATION_SCAN_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "site-packages",
        "tests",
        "vendor",
        "venv",
    }
)


def _direct_import_roots(tree):
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                roots.add("<relative>")
            elif node.module:
                roots.add(node.module.split(".", 1)[0])
    return roots


def _literal_dynamic_imports(tree):
    targets = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Call)
            or not node.args
            or not isinstance(node.args[0], ast.Constant)
            or type(node.args[0].value) is not str
        ):
            continue
        call_name = None
        if isinstance(node.func, ast.Name):
            call_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            call_name = node.func.attr
        if call_name in {"__import__", "import_module"}:
            targets.append(node.args[0].value)
    return targets


def _tree_imports_row_authority(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == "email_automation.row_authority"
            for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom):
            if node.module in {
                "email_automation.row_authority",
                "row_authority",
            }:
                return True
            if (
                node.module == "email_automation"
                or (node.level and node.module is None)
            ) and any(alias.name == "row_authority" for alias in node.names):
                return True
    return "email_automation.row_authority" in _literal_dynamic_imports(tree)


def _application_python_paths():
    for path in sorted(REPO_ROOT.rglob("*.py")):
        relative = path.relative_to(REPO_ROOT)
        if (
            relative == Path("email_automation/row_authority.py")
            or any(
                part in _APPLICATION_SCAN_EXCLUDED_PARTS
                for part in relative.parts
            )
        ):
            continue
        yield relative


class CanonicalRowAuthorityPrimitiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = None
        if ROW_AUTHORITY_PATH.exists():
            cls.module = importlib.import_module("email_automation.row_authority")

    def test_row_authority_module_exists(self):
        self.assertTrue(
            ROW_AUTHORITY_PATH.exists(),
            "row authority module is missing",
        )

    def test_row_authority_imports_are_standard_library_only(self):
        self.assertTrue(
            ROW_AUTHORITY_PATH.exists(),
            "row authority module is missing",
        )
        tree = ast.parse(
            ROW_AUTHORITY_PATH.read_text(encoding="utf-8"),
            filename=str(ROW_AUTHORITY_PATH),
        )
        self.assertEqual(
            set(),
            _direct_import_roots(tree)
            - ROW_AUTHORITY_STANDARD_LIBRARY_IMPORTS,
        )
        self.assertEqual([], _literal_dynamic_imports(tree))

    def test_row_authority_is_not_imported_by_runtime(self):
        importers = [
            relative.as_posix()
            for relative in _application_python_paths()
            if relative.as_posix() not in ROW_AUTHORITY_IMPORTER_ALLOWLIST
            and _tree_imports_row_authority(
                ast.parse(
                    (REPO_ROOT / relative).read_text(encoding="utf-8"),
                    filename=str(relative),
                )
            )
        ]
        self.assertEqual([], importers)

    def _require_module(self):
        if self.module is None:
            self.skipTest("row authority module is missing")
        return self.module

    @staticmethod
    def _reference_hash(domain, material):
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(
            domain.encode("utf-8") + b"\0" + encoded
        ).hexdigest()

    def test_canonical_json_is_exact_utf8_sorted_compact_and_tuple_normalized(self):
        module = self._require_module()
        self.assertEqual(
            b'{"a":[true,1,"\xc3\xa9"],"z":null}',
            module.canonical_json_bytes(
                {"z": None, "a": (True, 1, "é")}
            ),
        )

    def test_canonical_json_rejects_unsupported_unsafe_and_cyclic_values(self):
        module = self._require_module()
        cyclic = []
        cyclic.append(cyclic)
        invalid_values = (
            float("nan"),
            float("inf"),
            1.25,
            9007199254740992,
            b"bytes",
            {"set"},
            MappingProxyType({"a": 1}),
            {1: "non-string-key"},
            chr(0xD800),
            cyclic,
        )
        for value in invalid_values:
            with self.subTest(value=type(value).__name__), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.canonical_json_bytes(value)

    def test_domain_hash_uses_byte_prefix_and_frozen_expected_digest(self):
        module = self._require_module()
        scope_hash = "a" * 64
        payload = {"nullable": None, "value": 1}
        expected = (
            "773aefc65ea24cf28562eb10df940fc5fec5a6e9e520e2b64536e0957398568d"
        )
        actual = module.domain_hash(
            "sitesift.test.payload.v1",
            payload,
            user_scope_hash=scope_hash,
        )
        self.assertEqual(expected, actual)
        self.assertEqual(
            expected,
            self._reference_hash(
                "sitesift.test.payload.v1",
                {
                    **payload,
                    "schemaVersion": 1,
                    "userScopeHash": scope_hash,
                },
            ),
        )
        legacy_inside_json = hashlib.sha256(
            json.dumps(
                {
                    "domain": "sitesift.test.payload.v1",
                    **payload,
                    "schemaVersion": 1,
                    "userScopeHash": scope_hash,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertNotEqual(actual, legacy_inside_json)

    def test_domain_hash_changes_for_domain_scope_null_and_value_drift(self):
        module = self._require_module()
        base = module.domain_hash(
            "sitesift.test.payload.v1",
            {"nullable": None, "value": 1},
            user_scope_hash="a" * 64,
        )
        variants = (
            module.domain_hash(
                "sitesift.test.other.v1",
                {"nullable": None, "value": 1},
                user_scope_hash="a" * 64,
            ),
            module.domain_hash(
                "sitesift.test.payload.v1",
                {"nullable": None, "value": 1},
                user_scope_hash="b" * 64,
            ),
            module.domain_hash(
                "sitesift.test.payload.v1",
                {"nullable": "", "value": 1},
                user_scope_hash="a" * 64,
            ),
            module.domain_hash(
                "sitesift.test.payload.v1",
                {"nullable": None, "value": 2},
                user_scope_hash="a" * 64,
            ),
        )
        self.assertEqual(4, len(set(variants)))
        self.assertNotIn(base, variants)

    def test_user_scope_hash_is_exact_untransformed_and_frozen(self):
        module = self._require_module()
        self.assertEqual(
            "48fafc848b44ae7b0414309666dcb54208b7867700240a0f343ec02c53eb0cf2",
            module.user_scope_hash("uid-1"),
        )
        self.assertNotEqual(
            module.user_scope_hash("uid"),
            module.user_scope_hash(" uid "),
        )
        self.assertNotEqual(
            module.user_scope_hash("UID"),
            module.user_scope_hash("uid"),
        )

    def test_user_scope_rejects_empty_control_oversize_and_non_string(self):
        module = self._require_module()
        for value in (
            "",
            "uid\n1",
            "x" * 513,
            chr(0xD800),
            None,
            1,
            True,
        ):
            with self.subTest(value=repr(value)), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.user_scope_hash(value)

    def test_row_id_requires_rfc4122_uuid4_and_preserves_all_hex(self):
        module = self._require_module()
        value = UUID("123e4567-e89b-42d3-a456-426614174000")
        self.assertEqual(
            "sr1_123e4567e89b42d3a456426614174000",
            module.new_row_id(uuid_factory=lambda: value),
        )
        self.assertEqual(
            "sr1_123e4567e89b42d3a456426614174000",
            module.validate_row_id(
                "sr1_123e4567e89b42d3a456426614174000"
            ),
        )

    def test_row_id_rejects_wrong_version_variant_shape_and_factory_type(self):
        module = self._require_module()
        invalid_factories = (
            lambda: UUID("123e4567-e89b-12d3-a456-426614174000"),
            lambda: UUID("123e4567-e89b-42d3-7456-426614174000"),
            lambda: "123e4567-e89b-42d3-a456-426614174000",
        )
        for factory in invalid_factories:
            with self.subTest(factory=factory), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.new_row_id(uuid_factory=factory)
        for value in (
            "",
            "sr1_123e4567e89b12d3a456426614174000",
            "sr1_123e4567e89b42d37456426614174000",
            "sr1_123E4567E89B42D3A456426614174000",
            None,
        ):
            with self.subTest(value=value), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.validate_row_id(value)

    def test_domain_and_scope_hash_validation_is_exact(self):
        module = self._require_module()
        for domain in (
            "",
            "sitesift.test",
            "sitesift.TEST.v1",
            "sitesift.test.v0",
            "sitesift." + chr(0xD800) + ".v1",
            "x" * 129,
            None,
        ):
            with self.subTest(domain=domain), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.domain_hash(
                    domain,
                    {},
                    user_scope_hash="a" * 64,
                )
        for scope_hash in ("a" * 63, "A" * 64, "g" * 64, None):
            with self.subTest(scope_hash=scope_hash), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.domain_hash(
                    "sitesift.test.payload.v1",
                    {},
                    user_scope_hash=scope_hash,
                )
        for payload in (
            None,
            [],
            {"schemaVersion": 1},
            {"userScopeHash": "a" * 64},
        ):
            with self.subTest(payload=payload), self.assertRaises(
                module.RowAuthorityConfigError
            ):
                module.domain_hash(
                    "sitesift.test.payload.v1",
                    payload,
                    user_scope_hash="a" * 64,
                )

    def test_error_codes_are_stable_and_specific(self):
        self.assertIsNotNone(self.module, "row authority module is missing")
        module = self.module
        self.assertEqual("row_authority_error", module.RowAuthorityError.code)
        self.assertEqual(
            "row_authority_retryable", module.RowAuthorityRetryable.code
        )
        self.assertEqual(
            "row_authority_ambiguous", module.RowAuthorityAmbiguous.code
        )
        self.assertEqual(
            "row_authority_conflict", module.RowAuthorityConflict.code
        )
        self.assertEqual(
            "row_authority_config_error",
            module.RowAuthorityConfigError.code,
        )


if __name__ == "__main__":
    unittest.main()
