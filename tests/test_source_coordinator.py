import hashlib
import importlib
import importlib.util
import json
import unittest
from dataclasses import FrozenInstanceError


MODULE_NAME = "email_automation.source_coordinator"


def _load_source_coordinator(test_case):
    spec = importlib.util.find_spec(MODULE_NAME)
    test_case.assertIsNotNone(spec, "source coordinator module is missing")
    if spec is None:
        return None
    return importlib.import_module(MODULE_NAME)


class MalformedMode(str):
    def __new__(cls, value):
        instance = super().__new__(cls, value)
        instance.equality_calls = 0
        return instance

    def __eq__(self, other):
        self.equality_calls += 1
        return other == "enforced"

    __hash__ = str.__hash__


class HostileString(str):
    def __eq__(self, other):
        return True

    def strip(self, *args, **kwargs):
        raise AssertionError("hostile strip executed")

    def encode(self, *args, **kwargs):
        raise AssertionError("hostile encode executed")

    __hash__ = str.__hash__


class SourceCoordinatorContractTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = _load_source_coordinator(self)

    def test_public_error_codes_are_stable(self):
        coordinator = self.coordinator
        expected_codes = {
            "SourceCoordinatorError": "source_coordinator_error",
            "SourceCoordinatorRetryable": "source_coordinator_retryable",
            "SourceCoordinatorAmbiguous": "source_coordinator_ambiguous",
            "SourceCoordinatorConflict": "source_coordinator_conflict",
            "SourceCoordinatorConfigError": "source_coordinator_config",
        }
        base_error = coordinator.SourceCoordinatorError
        self.assertTrue(issubclass(base_error, RuntimeError))
        for name, code in expected_codes.items():
            with self.subTest(name=name):
                error_type = getattr(coordinator, name)
                self.assertTrue(issubclass(error_type, base_error))
                self.assertEqual(code, error_type.code)

    def test_mode_defaults_disabled_and_unknown_fails_disabled(self):
        coordinator = self.coordinator
        mode = coordinator.CoordinatorMode
        self.assertEqual(
            ["disabled", "shadow", "enforced"],
            [item.value for item in mode],
        )
        self.assertIs(
            mode.DISABLED,
            coordinator.resolve_source_coordinator_mode({}),
        )
        self.assertIs(
            mode.SHADOW,
            coordinator.resolve_source_coordinator_mode(
                {"SITESIFT_SOURCE_COORDINATOR_MODE": "shadow"}
            ),
        )
        self.assertIs(
            mode.ENFORCED,
            coordinator.resolve_source_coordinator_mode(
                {"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"}
            ),
        )
        for invalid in ("typo", "SHADOW", " shadow ", "", None):
            with self.subTest(invalid=invalid):
                self.assertIs(
                    mode.DISABLED,
                    coordinator.resolve_source_coordinator_mode(
                        {"SITESIFT_SOURCE_COORDINATOR_MODE": invalid}
                    ),
                )

        malformed = MalformedMode("garbage")
        self.assertIs(
            mode.DISABLED,
            coordinator.resolve_source_coordinator_mode(
                {"SITESIFT_SOURCE_COORDINATOR_MODE": malformed}
            ),
        )
        self.assertEqual(0, malformed.equality_calls)

    def test_source_alias_is_frozen_and_limit_is_exact(self):
        coordinator = self.coordinator
        alias = coordinator.SourceAlias("graph", "opaque")
        self.assertEqual(("graph", "opaque", ""), (alias.alias_type, alias.value, alias.key))
        self.assertEqual(1024, coordinator.MAX_SOURCE_ALIAS_BYTES)
        with self.assertRaises(FrozenInstanceError):
            alias.value = "changed"

    def test_canonical_json_hash_uses_sorted_compact_finite_json(self):
        coordinator = self.coordinator
        value = {"z": [True, None, 2.5], "a": "café"}
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        expected = hashlib.sha256(encoded).hexdigest()
        actual = coordinator.canonical_json_hash(value)
        self.assertEqual(expected, actual)
        self.assertEqual(64, len(actual))

    def test_canonical_json_hash_translates_invalid_values_to_config_error(self):
        coordinator = self.coordinator
        invalid_values = (
            {"value": float("nan")},
            {"value": float("inf")},
            {"value": float("-inf")},
            {"value": object()},
            {"value": {"mutable-set"}},
        )
        cyclic = []
        cyclic.append(cyclic)
        invalid_values += (cyclic,)
        for value in invalid_values:
            with self.subTest(value=repr(value)), self.assertRaises(
                coordinator.SourceCoordinatorConfigError
            ):
                coordinator.canonical_json_hash(value)

    def test_alias_normalization_preserves_opaque_case(self):
        coordinator = self.coordinator
        graph = coordinator.normalize_source_alias("graph", "  AbC+/=  ")
        rfc = coordinator.normalize_source_alias(
            "internet_message_id", " <<Case@Example.TEST>> "
        )
        self.assertEqual(
            coordinator.SourceAlias("graph", "AbC+/="),
            graph,
        )
        self.assertEqual(
            coordinator.SourceAlias("internet_message_id", "Case@Example.TEST"),
            rfc,
        )

    def test_alias_normalization_rejects_invalid_values(self):
        coordinator = self.coordinator
        invalid_aliases = (
            ("unknown", "value"),
            (None, "value"),
            (HostileString("graph"), "value"),
            ("graph", None),
            ("graph", 123),
            ("graph", HostileString("value")),
            ("graph", ""),
            ("graph", "   "),
            ("graph", "abc\x00def"),
            ("graph", "abc\ndef"),
            ("internet_message_id", "<<>>"),
            ("graph", "é" * 513),
            ("graph", "a" * (coordinator.MAX_SOURCE_ALIAS_BYTES + 1)),
        )
        for alias_type, value in invalid_aliases:
            with self.subTest(alias_type=alias_type, value=repr(value)), self.assertRaises(
                coordinator.SourceCoordinatorConfigError
            ):
                coordinator.normalize_source_alias(alias_type, value)

        boundary = "a" * coordinator.MAX_SOURCE_ALIAS_BYTES
        self.assertEqual(
            boundary,
            coordinator.normalize_source_alias("graph", boundary).value,
        )

    def test_source_alias_key_is_full_domain_separated_sha256(self):
        coordinator = self.coordinator
        alias = coordinator.normalize_source_alias("graph", "AbC+/=")
        expected = hashlib.sha256(
            b"source-alias-v2\0user-1\0graph\0AbC+/="
        ).hexdigest()
        key = coordinator.source_alias_key("user-1", alias)
        self.assertEqual(expected, key)
        self.assertEqual(64, len(key))

        variants = {
            coordinator.source_alias_key("user-2", alias),
            coordinator.source_alias_key(
                "user-1",
                coordinator.normalize_source_alias(
                    "internet_message_id", "AbC+/="
                ),
            ),
            coordinator.source_alias_key(
                "user-1", coordinator.normalize_source_alias("graph", "AbC+/=2")
            ),
        }
        self.assertEqual(3, len(variants))
        self.assertNotIn(key, variants)

    def test_source_alias_key_validates_user_and_canonical_alias(self):
        coordinator = self.coordinator
        canonical = coordinator.normalize_source_alias("graph", "opaque")
        invalid_inputs = (
            ("", canonical),
            (None, canonical),
            (123, canonical),
            (HostileString("user-1"), canonical),
            ("\ud800", canonical),
            ("user-1", coordinator.SourceAlias("graph", " opaque ")),
            (
                "user-1",
                coordinator.SourceAlias(HostileString("graph"), "opaque"),
            ),
            (
                "user-1",
                coordinator.SourceAlias("graph", HostileString("opaque")),
            ),
            ("user-1", coordinator.SourceAlias("unknown", "opaque")),
            ("user-1", object()),
        )
        for user_id, alias in invalid_inputs:
            with self.subTest(user_id=user_id, alias=repr(alias)), self.assertRaises(
                coordinator.SourceCoordinatorConfigError
            ):
                coordinator.source_alias_key(user_id, alias)


if __name__ == "__main__":
    unittest.main()
