"""SiteSift canonical JSON v1 — fixed byte vectors, digests, and bounds.

Canonicalization is the foundation of every certification identity: the runtime
scenario-registry digest, the sealed canonical input digest, and the evidence
digest are all lowercase SHA-256 over these exact bytes. If canonicalization is
not byte-stable across supported runtimes, every one of those identities is
unstable and no stamp means anything.

The plan requires Python 3.12 parity vectors. `CrossRuntimeParityTests` exercises
every alternate interpreter it can find and records an explicit skip when one is
absent; a skip here is `unverifiable`, never a pass. The full pinned whole-source
matrix - fixed vectors with literal expected bytes and literal expected digests,
driven under BOTH interpreters - lives in
`tests/test_certification_cross_interpreter.py`.
"""

from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys
import unittest

from email_automation.certification.canonical_json import (
    CanonicalJSONError,
    canonical_bytes,
    canonical_digest,
    loads_strict,
)


REPO_ROOT = Path(__file__).resolve().parents[1]

# Alternate interpreters used to prove byte-stability. 3.12 is the version the
# plan names; the others stand in when it is absent so the property is still
# exercised rather than merely asserted.
PARITY_INTERPRETERS = ("python3.12", "python3.13", "python3.14")


class CanonicalByteVectorTests(unittest.TestCase):
    """Fixed vectors. These bytes may never change without a new schema version."""

    def test_empty_object_and_array(self):
        self.assertEqual(canonical_bytes({}), b"{}")
        self.assertEqual(canonical_bytes([]), b"[]")

    def test_no_insignificant_whitespace(self):
        self.assertEqual(canonical_bytes({"a": 1, "b": [1, 2]}), b'{"a":1,"b":[1,2]}')

    def test_keys_sort_by_unicode_code_point_not_locale(self):
        payload = {"b": 1, "A": 2, "a": 3, "Z": 4, "é": 5, "É": 6}
        # Uppercase sorts before lowercase by code point; accented forms follow.
        self.assertEqual(
            canonical_bytes(payload),
            '{"A":2,"Z":4,"a":3,"b":1,"É":6,"é":5}'.encode("utf-8"),
        )

    def test_non_ascii_is_emitted_as_utf8_not_escaped(self):
        self.assertEqual(canonical_bytes({"k": "café"}), '{"k":"café"}'.encode("utf-8"))

    def test_nested_ordering_is_recursive(self):
        payload = {"z": {"b": 1, "a": 2}, "a": [{"d": 1, "c": 2}]}
        self.assertEqual(canonical_bytes(payload), b'{"a":[{"c":2,"d":1}],"z":{"a":2,"b":1}}')

    def test_booleans_and_null_are_json_literals(self):
        self.assertEqual(canonical_bytes({"t": True, "f": False, "n": None}),
                         b'{"f":false,"n":null,"t":true}')

    def test_array_order_is_preserved_exactly(self):
        self.assertEqual(canonical_bytes([3, 1, 2]), b"[3,1,2]")

    def test_digest_is_lowercase_sha256_of_canonical_bytes(self):
        payload = {"b": 1, "a": 2}
        expected = hashlib.sha256(b'{"a":2,"b":1}').hexdigest()
        self.assertEqual(canonical_digest(payload), expected)
        self.assertEqual(canonical_digest(payload), canonical_digest({"a": 2, "b": 1}))
        self.assertRegex(canonical_digest(payload), r"^[0-9a-f]{64}$")

    def test_key_order_never_changes_the_digest(self):
        self.assertEqual(
            canonical_digest({"one": 1, "two": 2, "three": 3}),
            canonical_digest({"three": 3, "two": 2, "one": 1}),
        )


class CanonicalTypeRejectionTests(unittest.TestCase):
    """Types whose serialization is not byte-stable are refused, not coerced."""

    def test_float_is_rejected(self):
        with self.assertRaises(CanonicalJSONError):
            canonical_bytes({"rent": 20.5})

    def test_nan_and_infinity_are_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(CanonicalJSONError):
                    canonical_bytes({"v": value})

    def test_nan_is_stopped_by_the_type_gate_before_json_dumps_is_reached(self):
        """`allow_nan=False` is unreachable, and this is what keeps it honest.

        Mutating `allow_nan=False` to `True` kills no test - the only mutation in
        this module that survives. That is not a hole: `_check` refuses every
        float before `json.dumps` is called, so `allow_nan` is a second lock on a
        door the first lock already holds. The proof is the message. If json were
        doing the refusing it would be a bare ValueError about out-of-range
        floats; because it is the type gate, it is a CanonicalJSONError naming
        the float rule. Remove the type gate and this test says so immediately.
        """
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=repr(value)):
                with self.assertRaises(CanonicalJSONError) as ctx:
                    canonical_bytes({"v": value})
                self.assertEqual(
                    str(ctx.exception),
                    "float values are not canonical; binary floating point does not "
                    "round-trip identically across runtimes - use a string or a "
                    "scaled integer",
                )

    def test_non_string_key_is_rejected(self):
        with self.assertRaises(CanonicalJSONError):
            canonical_bytes({1: "a"})

    def test_unsupported_object_is_rejected(self):
        with self.assertRaises(CanonicalJSONError):
            canonical_bytes({"when": object()})

    def test_bool_is_not_treated_as_integer(self):
        self.assertEqual(canonical_bytes({"v": True}), b'{"v":true}')
        self.assertEqual(canonical_bytes({"v": 1}), b'{"v":1}')

    def test_large_integers_round_trip_exactly(self):
        value = 2**63 + 12345
        raw = canonical_bytes({"v": value})
        self.assertEqual(raw, f'{{"v":{value}}}'.encode("utf-8"))
        self.assertEqual(loads_strict(raw)["v"], value)


class SurrogateRefusalTests(unittest.TestCase):
    """An unpaired surrogate has no UTF-8 encoding, so it must be a typed refusal.

    Before this was pinned, a lone surrogate escaped canonicalization as a raw
    `UnicodeEncodeError` from `str.encode`. Callers guard the canonicalizer with
    `except CanonicalJSONError`, so an untyped escape either crashed the runner or
    - worse, in a broad `except Exception` - read as a clean early return. And
    `loads_strict` accepted `"\\ud800"` happily, so a payload could be parsed,
    stored, and then explode when re-serialized: a round-trip that is not a
    round-trip. The refusal surface must be closed, and identical on every runtime.
    """

    def test_lone_high_surrogate_in_a_value_is_a_typed_refusal(self):
        with self.assertRaises(CanonicalJSONError) as ctx:
            canonical_bytes({"k": "\ud800"})
        self.assertEqual(
            str(ctx.exception),
            "string value contains an unpaired surrogate at index 0; an unpaired "
            "surrogate has no UTF-8 encoding, so it cannot be canonicalized",
        )

    def test_lone_low_surrogate_mid_string_reports_its_index(self):
        with self.assertRaises(CanonicalJSONError) as ctx:
            canonical_bytes({"k": "ab\udfffc"})
        self.assertEqual(
            str(ctx.exception),
            "string value contains an unpaired surrogate at index 2; an unpaired "
            "surrogate has no UTF-8 encoding, so it cannot be canonicalized",
        )

    def test_unpaired_surrogate_in_a_key_is_a_typed_refusal(self):
        with self.assertRaises(CanonicalJSONError) as ctx:
            canonical_bytes({"\ud800": 1})
        self.assertEqual(
            str(ctx.exception),
            "object key contains an unpaired surrogate at index 0; an unpaired "
            "surrogate has no UTF-8 encoding, so it cannot be canonicalized",
        )

    def test_escaped_surrogate_is_refused_at_parse_not_accepted_then_exploded(self):
        with self.assertRaises(CanonicalJSONError):
            loads_strict(b'{"k":"\\ud800"}')

    def test_a_well_formed_surrogate_pair_is_accepted_as_one_astral_character(self):
        # U+1D11E written as an escaped surrogate pair decodes to a single
        # non-surrogate code point, so it is canonical, not a refusal.
        parsed = loads_strict(b'{"k":"\\ud834\\udd1e"}')
        self.assertEqual(parsed, {"k": "\U0001d11e"})
        self.assertEqual(canonical_bytes(parsed), b'{"k":"\xf0\x9d\x84\x9e"}')


class StrictParseTests(unittest.TestCase):
    """Parsing is a fresh bounded decode. A duplicate key is a refusal."""

    def test_duplicate_key_is_rejected(self):
        with self.assertRaises(CanonicalJSONError) as ctx:
            loads_strict(b'{"a":1,"a":2}')
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_nested_duplicate_key_is_rejected(self):
        with self.assertRaises(CanonicalJSONError):
            loads_strict(b'{"outer":{"k":1,"k":2}}')

    def test_round_trip_is_stable(self):
        payload = {"b": [1, 2, {"z": "é", "a": None}], "a": True}
        raw = canonical_bytes(payload)
        self.assertEqual(canonical_bytes(loads_strict(raw)), raw)

    def test_trailing_content_is_rejected(self):
        with self.assertRaises(CanonicalJSONError):
            loads_strict(b'{"a":1} trailing')

    def test_accepts_str_or_bytes(self):
        self.assertEqual(loads_strict('{"a":1}'), {"a": 1})
        self.assertEqual(loads_strict(b'{"a":1}'), {"a": 1})


class CanonicalBoundsTests(unittest.TestCase):
    """Width, depth, and size are bounded so a hostile payload cannot exhaust the runner."""

    def test_depth_bound_is_enforced(self):
        deep = current = {}
        for _ in range(200):
            current["n"] = {}
            current = current["n"]
        with self.assertRaises(CanonicalJSONError) as ctx:
            canonical_bytes(deep)
        self.assertIn("depth", str(ctx.exception).lower())

    def test_width_bound_is_enforced(self):
        wide = {f"k{i}": i for i in range(100_000)}
        with self.assertRaises(CanonicalJSONError) as ctx:
            canonical_bytes(wide)
        self.assertIn("width", str(ctx.exception).lower())

    def test_size_bound_is_enforced_on_parse(self):
        oversized = b'{"a":"' + b"x" * (4 * 1024 * 1024) + b'"}'
        with self.assertRaises(CanonicalJSONError) as ctx:
            loads_strict(oversized)
        self.assertIn("size", str(ctx.exception).lower())

    def test_a_realistic_registry_sized_payload_is_accepted(self):
        payload = {f"scenario-{i}": {"a": i, "b": [i, i + 1]} for i in range(500)}
        self.assertTrue(canonical_bytes(payload))


class CrossRuntimeParityTests(unittest.TestCase):
    """Canonical bytes must be identical on every supported interpreter."""

    VECTOR = {
        "z": {"b": 1, "a": 2},
        "a": [1, 2, {"d": None, "c": True}],
        "é": "café",
        "n": 2**63 + 1,
    }

    def _digest_under(self, interpreter):
        program = (
            "import sys, json; sys.path.insert(0, %r);"
            "from email_automation.certification.canonical_json import canonical_digest;"
            "print(canonical_digest(json.loads(sys.argv[1])))" % str(REPO_ROOT)
        )
        completed = subprocess.run(
            [interpreter, "-B", "-c", program, json.dumps(self.VECTOR)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        if completed.returncode != 0:
            self.fail(f"{interpreter} failed to canonicalize: {completed.stderr.strip()[:400]}")
        return completed.stdout.strip()

    def test_canonical_digest_is_identical_across_available_interpreters(self):
        local = canonical_digest(self.VECTOR)
        # shutil.which returns None for a name that is not installed, so the
        # lookup is done once and the None case is handled, never re-queried and
        # assumed present the second time.
        alternates = []
        for name in PARITY_INTERPRETERS:
            found = shutil.which(name)
            if found and Path(found).resolve() != Path(sys.executable).resolve():
                alternates.append(name)
        if not alternates:
            self.skipTest(
                "unverifiable: no alternate interpreter available for parity; "
                f"looked for {PARITY_INTERPRETERS}"
            )
        for interpreter in alternates:
            with self.subTest(interpreter=interpreter):
                self.assertEqual(self._digest_under(interpreter), local)

    def test_python312_parity_is_recorded_not_silently_skipped(self):
        """The plan names 3.12 explicitly. Absence is recorded, never assumed equal."""
        if not shutil.which("python3.12"):
            self.skipTest(
                "unverifiable: python3.12 is not installed on this host, so the "
                "plan's named 3.12 parity vector could not be exercised. Cross-runtime "
                "parity is still proved against the available alternate interpreter by "
                "test_canonical_digest_is_identical_across_available_interpreters."
            )
        self.assertEqual(self._digest_under("python3.12"), canonical_digest(self.VECTOR))


if __name__ == "__main__":
    unittest.main()
