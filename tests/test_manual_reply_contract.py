"""Contract tests for canonical manual-reply source resolution keys."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import unittest

from email_automation.manual_reply import (
    manual_reply_resolution_key,
    normalize_internet_message_id,
)


CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "manual-reply-resolution-v1.json"
)
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
DOMAIN = CONTRACT["domain"]
BOUNDARY_CODE_POINTS = tuple(
    chr(int(value, 16)) for value in CONTRACT["boundaryRejectedCodePoints"]
)
INTERNET_TRIM_CODE_POINTS = tuple(
    chr(int(value, 16)) for value in CONTRACT["internetBoundaryTrimCodePoints"]
)
GENERATED_CASES = CONTRACT["generatedCases"]
FIELDS = (
    "uid",
    "threadId",
    "immutableGraphMessageId",
    "internetMessageId",
    "source",
)


def _vector(name: str) -> dict:
    return next(item for item in CONTRACT["vectors"] if item["name"] == name)


def _key(values: dict) -> str:
    return manual_reply_resolution_key(
        uid=values["uid"],
        thread_id=values["threadId"],
        immutable_graph_message_id=values["immutableGraphMessageId"],
        internet_message_id=values["internetMessageId"],
        source=values["source"],
    )


def _framed(*members: str) -> bytes:
    chunks = []
    for member in members:
        encoded = member.encode("utf-8")
        chunks.extend((len(encoded).to_bytes(4, "big"), encoded))
    return b"".join(chunks)


def _oracle_key(values: dict, normalized_internet_message_id: str) -> str:
    payload = _framed(
        DOMAIN,
        values["uid"],
        values["threadId"],
        values["immutableGraphMessageId"],
        normalized_internet_message_id,
        values["source"],
    )
    return hashlib.sha256(payload).hexdigest()


class ManualReplyResolutionContractTests(unittest.TestCase):
    def test_fixture_declares_exact_tuple_framing_order_and_hash(self):
        self.assertEqual(1, CONTRACT["schemaVersion"])
        self.assertEqual(7, len(CONTRACT["vectors"]))
        self.assertEqual("utf-8", CONTRACT["encoding"])
        self.assertEqual(
            "uint32be-byte-length followed by raw bytes for every tuple member",
            CONTRACT["framing"],
        )
        self.assertEqual(
            [
                "domain",
                "uid",
                "threadId",
                "immutableGraphMessageId",
                "normalizedInternetMessageId",
                "source",
            ],
            CONTRACT["tupleOrder"],
        )
        self.assertEqual("sha256-lowercase-hex", CONTRACT["hash"])
        self.assertEqual("^__.*__$", CONTRACT["firestoreReservedPattern"])
        self.assertEqual(
            ("\t", "\n", "\r", " "),
            INTERNET_TRIM_CODE_POINTS,
        )

    def test_all_fixture_vectors_pin_normalization_and_resolution_keys(self):
        for vector in CONTRACT["vectors"]:
            with self.subTest(vector=vector["name"]):
                values = vector["input"]
                self.assertEqual(
                    vector["normalizedInternetMessageId"],
                    normalize_internet_message_id(values["internetMessageId"]),
                )
                self.assertEqual(vector["expectedKey"], _key(values))

    def test_internet_message_id_aliases_converge(self):
        baseline = _vector("unicode-baseline")
        alias = _vector("internet-id-alias")

        self.assertEqual(
            normalize_internet_message_id(baseline["input"]["internetMessageId"]),
            normalize_internet_message_id(alias["input"]["internetMessageId"]),
        )
        self.assertEqual(_key(baseline["input"]), _key(alias["input"]))

    def test_opaque_graph_message_id_preserves_case(self):
        baseline = _vector("unicode-baseline")
        case_variant = _vector("opaque-case-differs")

        self.assertNotEqual(
            baseline["input"]["immutableGraphMessageId"],
            case_variant["input"]["immutableGraphMessageId"],
        )
        self.assertNotEqual(_key(baseline["input"]), _key(case_variant["input"]))

    def test_composed_and_decomposed_unicode_remain_distinct(self):
        composed = _vector("unicode-composed")
        decomposed = _vector("unicode-decomposed")

        self.assertNotEqual(composed["input"]["uid"], decomposed["input"]["uid"])
        self.assertNotEqual(_key(composed["input"]), _key(decomposed["input"]))

    def test_source_message_identity_pair_changes_the_key(self):
        baseline = _vector("unicode-baseline")
        different_pair = _vector("canonical-source-pair-differs")

        self.assertNotEqual(
            (
                baseline["input"]["immutableGraphMessageId"],
                normalize_internet_message_id(
                    baseline["input"]["internetMessageId"]
                ),
            ),
            (
                different_pair["input"]["immutableGraphMessageId"],
                normalize_internet_message_id(
                    different_pair["input"]["internetMessageId"]
                ),
            ),
        )
        self.assertNotEqual(_key(baseline["input"]), _key(different_pair["input"]))

    def test_internet_message_id_alone_changes_the_key(self):
        baseline = _vector("unicode-baseline")
        internet_only = _vector("internet-id-only-differs")

        self.assertEqual(
            baseline["input"]["immutableGraphMessageId"],
            internet_only["input"]["immutableGraphMessageId"],
        )
        self.assertNotEqual(
            normalize_internet_message_id(baseline["input"]["internetMessageId"]),
            normalize_internet_message_id(
                internet_only["input"]["internetMessageId"]
            ),
        )
        self.assertNotEqual(_key(baseline["input"]), _key(internet_only["input"]))

    def test_key_uses_explicit_uint32be_framing_and_tuple_order(self):
        for vector in CONTRACT["vectors"]:
            with self.subTest(vector=vector["name"]):
                values = vector["input"]
                normalized = vector["normalizedInternetMessageId"]
                self.assertEqual(
                    vector["expectedKey"],
                    _oracle_key(values, normalized),
                )

        baseline = _vector("unicode-baseline")
        values = baseline["input"]
        normalized = baseline["normalizedInternetMessageId"]
        canonical_members = (
            DOMAIN,
            values["uid"],
            values["threadId"],
            values["immutableGraphMessageId"],
            normalized,
            values["source"],
        )
        framed = _framed(*canonical_members)
        self.assertEqual(
            len(DOMAIN.encode("utf-8")),
            int.from_bytes(framed[:4], "big"),
        )
        self.assertEqual(
            baseline["expectedKey"], hashlib.sha256(framed).hexdigest()
        )
        self.assertNotEqual(
            baseline["expectedKey"],
            hashlib.sha256(
                _framed(
                    DOMAIN,
                    values["threadId"],
                    values["uid"],
                    values["immutableGraphMessageId"],
                    normalized,
                    values["source"],
                )
            ).hexdigest(),
        )
        self.assertNotEqual(
            baseline["expectedKey"],
            hashlib.sha256("".join(canonical_members).encode("utf-8")).hexdigest(),
        )

    def test_missing_blank_and_wrong_type_values_are_rejected(self):
        valid = dict(_vector("internet-id-alias")["input"])

        for field in FIELDS:
            missing = dict(valid)
            missing.pop(field)
            kwargs = {
                "uid": missing.get("uid"),
                "thread_id": missing.get("threadId"),
                "immutable_graph_message_id": missing.get(
                    "immutableGraphMessageId"
                ),
                "internet_message_id": missing.get("internetMessageId"),
                "source": missing.get("source"),
            }
            missing_keyword = {
                "uid": "uid",
                "threadId": "thread_id",
                "immutableGraphMessageId": "immutable_graph_message_id",
                "internetMessageId": "internet_message_id",
                "source": "source",
            }[field]
            kwargs.pop(missing_keyword)
            with self.subTest(kind="missing", field=field):
                with self.assertRaises(TypeError):
                    manual_reply_resolution_key(**kwargs)

        for field in FIELDS:
            for invalid in ("", " \t"):
                values = dict(valid)
                values[field] = invalid
                with self.subTest(kind="blank", field=field, value=repr(invalid)):
                    with self.assertRaises((TypeError, ValueError)):
                        _key(values)

        for field in FIELDS:
            for invalid in (None, 7, b"synthetic", ["synthetic"]):
                values = dict(valid)
                values[field] = invalid
                with self.subTest(
                    kind="wrong-type", field=field, type=type(invalid).__name__
                ):
                    with self.assertRaises((TypeError, ValueError)):
                        _key(values)

        with self.assertRaises(TypeError):
            normalize_internet_message_id()
        for invalid in (None, 7, b"<synthetic@example.test>", []):
            with self.subTest(kind="normalizer-wrong-type", value=repr(invalid)):
                with self.assertRaises((TypeError, ValueError)):
                    normalize_internet_message_id(invalid)

    def test_uid_and_thread_reject_noncanonical_document_ids(self):
        valid = dict(_vector("internet-id-alias")["input"])
        invalid_ids = {
            "leading-padding": " synthetic-id",
            "trailing-padding": "synthetic-id\t",
            "path-separator": "synthetic/path",
            "nul": "synthetic\x00id",
            "unit-separator": "synthetic\x1fid",
            "delete": "synthetic\x7fid",
            "dot": ".",
            "dot-dot": "..",
        }
        for field in ("uid", "threadId"):
            for name, invalid in invalid_ids.items():
                values = dict(valid)
                values[field] = invalid
                with self.subTest(field=field, case=name):
                    with self.assertRaises((TypeError, ValueError)):
                        _key(values)

    def test_uid_thread_and_graph_reject_every_listed_boundary_code_point(self):
        valid = dict(_vector("internet-id-alias")["input"])
        field_values = {
            "uid": "synthetic-user",
            "threadId": "synthetic-thread",
            "immutableGraphMessageId": "AQMkSynthetic==",
        }
        self.assertIn("FEFF", CONTRACT["boundaryRejectedCodePoints"])

        for field, base in field_values.items():
            for encoded, code_point in zip(
                CONTRACT["boundaryRejectedCodePoints"],
                BOUNDARY_CODE_POINTS,
            ):
                for position, invalid in (
                    ("leading", code_point + base),
                    ("trailing", base + code_point),
                ):
                    values = dict(valid)
                    values[field] = invalid
                    with self.subTest(
                        field=field,
                        code_point=encoded,
                        position=position,
                    ):
                        with self.assertRaises((TypeError, ValueError)):
                            _key(values)

    def test_uid_and_thread_reject_reserved_firestore_document_ids(self):
        valid = dict(_vector("internet-id-alias")["input"])
        pattern = re.compile(CONTRACT["firestoreReservedPattern"])
        reserved_ids = ("__synthetic__", "____")
        self.assertTrue(all(pattern.fullmatch(value) for value in reserved_ids))

        for field in ("uid", "threadId"):
            for invalid in reserved_ids:
                values = dict(valid)
                values[field] = invalid
                with self.subTest(field=field, value=invalid):
                    with self.assertRaises((TypeError, ValueError)):
                        _key(values)

    def test_document_id_exact_ascii_and_multibyte_byte_limits(self):
        valid = dict(_vector("internet-id-alias")["input"])
        generated = GENERATED_CASES["documentId"]
        cases = (
            (
                "ascii",
                generated["asciiUnit"],
                generated["asciiMaxCount"],
                generated["asciiOverCount"],
            ),
            (
                "multibyte",
                generated["multibyteUnit"],
                generated["multibyteMaxCount"],
                generated["multibyteOverCount"],
            ),
        )

        for field in ("uid", "threadId"):
            for name, unit, max_count, over_count in cases:
                maximum = unit * max_count
                over = unit * over_count
                self.assertEqual(1500, len(maximum.encode("utf-8")))
                self.assertGreater(len(over.encode("utf-8")), 1500)
                values = dict(valid)
                values[field] = maximum
                with self.subTest(field=field, encoding=name, boundary="maximum"):
                    self.assertRegex(_key(values), r"^[0-9a-f]{64}$")
                values[field] = over
                with self.subTest(field=field, encoding=name, boundary="over"):
                    with self.assertRaises((TypeError, ValueError)):
                        _key(values)

    def test_graph_message_id_rejects_padding_controls_and_oversize_values(self):
        valid = dict(_vector("internet-id-alias")["input"])
        invalid_ids = {
            "leading-padding": " AQMkSynthetic",
            "trailing-padding": "AQMkSynthetic\r\n",
            "nul": "AQMk\x00Synthetic",
            "unit-separator": "AQMk\x1fSynthetic",
            "delete": "AQMk\x7fSynthetic",
        }
        for name, invalid in invalid_ids.items():
            values = dict(valid)
            values["immutableGraphMessageId"] = invalid
            with self.subTest(case=name):
                with self.assertRaises((TypeError, ValueError)):
                    _key(values)

    def test_graph_message_id_exact_ascii_and_multibyte_byte_limits(self):
        valid = dict(_vector("internet-id-alias")["input"])
        generated = GENERATED_CASES["immutableGraphMessageId"]
        cases = (
            (
                "ascii",
                generated["asciiUnit"],
                generated["asciiMaxCount"],
                generated["asciiOverCount"],
            ),
            (
                "multibyte",
                generated["multibyteUnit"],
                generated["multibyteMaxCount"],
                generated["multibyteOverCount"],
            ),
        )

        for name, unit, max_count, over_count in cases:
            maximum = unit * max_count
            over = unit * over_count
            self.assertEqual(2048, len(maximum.encode("utf-8")))
            self.assertGreater(len(over.encode("utf-8")), 2048)
            values = dict(valid)
            values["immutableGraphMessageId"] = maximum
            with self.subTest(encoding=name, boundary="maximum"):
                self.assertRegex(_key(values), r"^[0-9a-f]{64}$")
            values["immutableGraphMessageId"] = over
            with self.subTest(encoding=name, boundary="over"):
                with self.assertRaises((TypeError, ValueError)):
                    _key(values)

    def test_positive_opaque_graph_message_id_is_preserved_exactly(self):
        valid = dict(_vector("internet-id-alias")["input"])
        opaque = CONTRACT["positiveOpaqueGraphMessageId"]
        for character in "/+=. ":
            self.assertIn(character, opaque)
        self.assertFalse(opaque[0].isspace() or opaque[-1].isspace())

        values = dict(valid)
        values["immutableGraphMessageId"] = opaque
        normalized = normalize_internet_message_id(values["internetMessageId"])
        self.assertEqual(_oracle_key(values, normalized), _key(values))

        case_variant = dict(values)
        case_variant["immutableGraphMessageId"] = opaque.swapcase()
        self.assertNotEqual(_key(values), _key(case_variant))

    def test_lone_surrogate_is_rejected_on_every_identity_surface(self):
        valid = dict(_vector("internet-id-alias")["input"])
        surrogate = chr(int(CONTRACT["loneSurrogateCodeUnit"], 16))
        self.assertEqual("D800", CONTRACT["loneSurrogateCodeUnit"])

        for field in ("uid", "threadId", "immutableGraphMessageId"):
            values = dict(valid)
            values[field] = "synthetic" + surrogate
            with self.subTest(field=field):
                with self.assertRaises((TypeError, ValueError)):
                    _key(values)

        invalid_internet_id = "<synthetic" + surrogate + "@example.test>"
        with self.assertRaises((TypeError, ValueError)):
            normalize_internet_message_id(invalid_internet_id)
        values = dict(valid)
        values["internetMessageId"] = invalid_internet_id
        with self.assertRaises((TypeError, ValueError)):
            _key(values)

    def test_internet_message_id_rejects_invalid_wire_forms(self):
        valid = dict(_vector("internet-id-alias")["input"])
        invalid_ids = {
            "missing-brackets": "synthetic@example.test",
            "empty-brackets": "<>",
            "missing-at": "<synthetic.example.test>",
            "blank-local": "<@example.test>",
            "blank-domain": "<synthetic@>",
            "multiple-at": "<synthetic@alias@example.test>",
            "non-ascii-local": "<synth\u00e9tic@example.test>",
            "non-ascii-boundary": "\u00a0<synthetic@example.test>",
            "interior-space": "<synthetic @example.test>",
            "interior-tab": "<synthetic@\texample.test>",
            "interior-newline": "<synthetic@example.\ntest>",
            "interior-control": "<synthetic@exam\x00ple.test>",
        }

        for name, invalid in invalid_ids.items():
            with self.subTest(case=name, surface="normalizer"):
                with self.assertRaises((TypeError, ValueError)):
                    normalize_internet_message_id(invalid)
            values = dict(valid)
            values["internetMessageId"] = invalid
            with self.subTest(case=name, surface="key"):
                with self.assertRaises((TypeError, ValueError)):
                    _key(values)

    def test_internet_message_id_exact_limit_and_trim_set(self):
        valid = dict(_vector("internet-id-alias")["input"])
        generated = GENERATED_CASES["internetMessageId"]
        maximum = (
            "<"
            + (generated["localUnit"] * generated["maxLocalCount"])
            + "@"
            + generated["domain"]
            + ">"
        )
        over = (
            "<"
            + (generated["localUnit"] * generated["overLocalCount"])
            + "@"
            + generated["domain"]
            + ">"
        )
        self.assertEqual(998, len(maximum.encode("ascii")))
        self.assertEqual(999, len(over.encode("ascii")))
        self.assertEqual(maximum, normalize_internet_message_id(maximum))

        values = dict(valid)
        values["internetMessageId"] = maximum
        maximum_key = _key(values)
        self.assertRegex(maximum_key, r"^[0-9a-f]{64}$")

        trim_packet = "".join(INTERNET_TRIM_CODE_POINTS)
        padded = trim_packet + maximum + trim_packet[::-1]
        self.assertEqual(maximum, normalize_internet_message_id(padded))
        values["internetMessageId"] = padded
        self.assertEqual(maximum_key, _key(values))

        for encoded, trim_character in zip(
            CONTRACT["internetBoundaryTrimCodePoints"],
            INTERNET_TRIM_CODE_POINTS,
        ):
            for position, candidate in (
                ("leading", trim_character + maximum),
                ("trailing", maximum + trim_character),
            ):
                with self.subTest(
                    code_point=encoded,
                    position=position,
                    behavior="trim",
                ):
                    self.assertEqual(maximum, normalize_internet_message_id(candidate))

        with self.assertRaises((TypeError, ValueError)):
            normalize_internet_message_id(over)
        values["internetMessageId"] = over
        with self.assertRaises((TypeError, ValueError)):
            _key(values)

        for code_point in ("000B", "000C"):
            character = chr(int(code_point, 16))
            for position, candidate in (
                ("leading", character + maximum),
                ("trailing", maximum + character),
            ):
                with self.subTest(
                    code_point=code_point,
                    position=position,
                    behavior="reject",
                ):
                    with self.assertRaises((TypeError, ValueError)):
                        normalize_internet_message_id(candidate)

    def test_string_subclasses_are_not_primitive_strings(self):
        class StringSubclass(str):
            pass

        valid = dict(_vector("internet-id-alias")["input"])
        for field in FIELDS:
            values = dict(valid)
            values[field] = StringSubclass(values[field])
            with self.subTest(field=field):
                with self.assertRaises(TypeError):
                    _key(values)

        with self.assertRaises(TypeError):
            normalize_internet_message_id(
                StringSubclass(valid["internetMessageId"])
            )

    def test_source_must_be_exact(self):
        valid = dict(_vector("internet-id-alias")["input"])
        for invalid in (
            "dashboard_manual_reply",
            "Dashboard_Inline_Reply",
            "dashboard_inline_reply ",
        ):
            values = dict(valid)
            values["source"] = invalid
            with self.subTest(source=repr(invalid)):
                with self.assertRaises((TypeError, ValueError)):
                    _key(values)


if __name__ == "__main__":
    unittest.main()
