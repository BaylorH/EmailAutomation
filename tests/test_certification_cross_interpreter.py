"""Canonical JSON v1 whole-source matrix: fixed vectors, pinned digests, two interpreters.

Every certification identity - the runtime scenario-registry digest, the sealed
canonical input digest, the evidence digest - is a lowercase SHA-256 over bytes
produced by `email_automation.certification.canonical_json`. The certification
suite develops on CPython 3.14; the deployed image runs CPython 3.12. If those two
disagree about a single byte, every stamp minted on one is meaningless on the
other, and nothing else in the instrument would notice.

What makes this file different from a parity smoke test:

* **The expected bytes and the expected digests are literal constants.** A test
  that computes a digest and then compares it to itself agrees only with itself;
  it passes just as happily after the canonicalizer has been broken. Here the
  canonical text and its SHA-256 are both frozen in `EXPECTED_SERIALIZE` /
  `EXPECTED_PARSE`, and `test_pinned_digests_match_pinned_bytes_independently`
  re-derives each digest from the pinned text with `hashlib` alone, never through
  the module under test. The two halves of every pin have to agree without the
  canonicalizer in the room.
* **The corpus is executed under both interpreters.** The same module, the same
  vector builders, run in a 3.12 subprocess via `--emit-corpus-report`, and the
  parent compares that report against both its own in-process result and the
  pinned table.
* **Refusals are pinned by exact message, not by exception class.** A value one
  runtime accepts and the other refuses is a divergence that comparing digests
  over accepted values alone can never surface, because the diverging value never
  reaches the comparison. The refusal surface is part of the contract.

Scope, stated precisely and repeated wherever this file makes a claim: what is
proved here is a **HOST** result - two interpreters installed on this machine. It
is NOT an in-image result. Proving that the interpreter inside the built container
image is 3.12 and produces these same digests requires the image, which is
Baylor-blocked. `test_in_image_parity_is_instrument_blocked` carries the exact
command and skips rather than passing, so the missing half stays visible.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
from typing import Any, NoReturn, cast
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from email_automation.certification.canonical_json import (  # noqa: E402
    MAX_SIZE_BYTES,
    CanonicalJSONError,
    canonical_bytes,
    canonical_digest,
    loads_strict,
)

# --------------------------------------------------------------------------
# Interpreter discovery
# --------------------------------------------------------------------------

#: The two interpreters this matrix is about: the version the deployed image runs,
#: and the version the certification suite develops on. Which one drives the suite
#: is not fixed - the matrix must work in both directions - but 3.12 must be one of
#: the two, or what ran was not the parity the plan asks for.
PRODUCTION_VERSION = (3, 12)
DEVELOPMENT_VERSION = (3, 14)
MATRIX_VERSIONS = (PRODUCTION_VERSION, DEVELOPMENT_VERSION)

#: Set to any truthy value to turn a missing counterpart from a recorded block
#: into a hard failure. The orchestrator sets this where the matrix must run.
REQUIRE_ENV_VAR = "CERT_REQUIRE_CROSS_INTERPRETER"

#: Explicit override for the counterpart interpreter path.
INTERPRETER_ENV_VAR = "CERT_PYTHON312"

STATUS_EXERCISED = "EXERCISED"
STATUS_BLOCKED = "INSTRUMENT_BLOCKED"

EMIT_FLAG = "--emit-corpus-report"

#: The half of Task 13 that cannot be done without the built image. Baylor runs
#: this once the image exists; it is a five-second check, not a re-derivation.
#:
#: Preconditions: the image is built and pushed, `IMAGE_DIGEST` is the pinned
#: `sha256:...` digest recorded in the release stamp - a tag is not acceptable
#: here, because a tag can move after the stamp was minted - and the repo is the
#: working directory so `-m tests....` resolves.
IN_IMAGE_VERIFICATION_COMMAND = r"""
IMAGE_DIGEST=<the sha256:... from the release stamp>

# 1. The interpreter inside the image is 3.12.
docker run --rm --entrypoint python "$IMAGE_DIGEST" \
  -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version; print(sys.version)'

# 2. The image reproduces this pinned corpus byte for byte.
docker run --rm --entrypoint python "$IMAGE_DIGEST" \
  -m tests.test_certification_cross_interpreter --emit-corpus-report \
  > /tmp/in_image_corpus.json

# 3. The in-image report equals the host report. Exit status 0 is the proof.
python3 -m tests.test_certification_cross_interpreter --emit-corpus-report \
  > /tmp/host_corpus.json
python3 - <<'EOF'
import json
a = json.load(open("/tmp/in_image_corpus.json"))
b = json.load(open("/tmp/host_corpus.json"))
assert a["python"][:2] == [3, 12], a["python"]
assert a["serialize"] == b["serialize"], "serialize corpus diverged in image"
assert a["parse"] == b["parse"], "parse corpus diverged in image"
print("in-image canonical JSON parity: OK")
EOF
""".strip()


class InterpreterUnavailable(Exception):
    """No counterpart interpreter exists on this host. Blocked, not failed."""


def _candidate_interpreters():
    """Every place a matrix interpreter might live, most explicit first."""
    candidates = []
    override = os.environ.get(INTERPRETER_ENV_VAR)
    if override:
        candidates.append(override)
    for name in ("python3.12", "python3.14", "python3.13", "python3"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    # uv installs interpreters outside PATH on some machines.
    uv_root = Path.home() / ".local" / "share" / "uv" / "python"
    if uv_root.is_dir():
        for major, minor in MATRIX_VERSIONS:
            pattern = "cpython-%d.%d*/bin/python%d.%d" % (major, minor, major, minor)
            candidates.extend(str(path) for path in sorted(uv_root.glob(pattern)))
    seen = set()
    unique = []
    for candidate in candidates:
        key = str(Path(candidate).resolve()) if Path(candidate).exists() else candidate
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def _reported_version(interpreter):
    """(major, minor, micro) as the interpreter itself reports it, or None.

    A candidate that will not run, or will not answer, is simply not a candidate.
    It is never assumed to be the version its filename claims.
    """
    try:
        completed = subprocess.run(
            [interpreter, "-B", "-c", "import sys;print(*sys.version_info[:3])"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        return tuple(int(part) for part in completed.stdout.split())
    except ValueError:
        return None


def find_counterpart_interpreter():
    """(path, version) of a REAL interpreter that is not the one running this suite.

    Raises `InterpreterUnavailable` - the blocked condition - when no such
    interpreter exists. The returned version is never None, so callers never
    subscript an absent value; absence leaves through the exception instead.
    """
    running = sys.version_info[:2]
    for candidate in _candidate_interpreters():
        version = _reported_version(candidate)
        if not version:
            continue
        if version[:2] not in MATRIX_VERSIONS:
            continue
        if version[:2] == running:
            continue
        if PRODUCTION_VERSION not in (running, version[:2]):
            continue
        return candidate, version
    raise InterpreterUnavailable(
        "no counterpart CPython from %s found on this host (running %d.%d.%d)"
        % (
            ", ".join("%d.%d" % v for v in MATRIX_VERSIONS),
            *sys.version_info[:3],
        )
    )


# --------------------------------------------------------------------------
# The vector corpus
#
# Each entry is (vector_id, stress_note, builder). Builders take no arguments and
# are pure, so the same source produces the same payload on every interpreter.
# --------------------------------------------------------------------------


def _depth_chain(depth):
    """A dict nested `depth` levels deep: depth=1 is `{}`."""
    top = current = {}
    for _ in range(depth - 1):
        current["n"] = {}
        current = current["n"]
    return top


def _wide(width):
    return {"k%d" % i: i for i in range(width)}


def _oversize_payload():
    return {"a": "x" * MAX_SIZE_BYTES}


SERIALIZE_VECTORS = (
    # ---- degenerate and empty shapes -------------------------------------
    ("struct/empty-object", "empty container", lambda: {}),
    ("struct/empty-array", "empty container", lambda: []),
    ("struct/bare-null", "top-level scalar", lambda: None),
    ("struct/bare-true", "top-level scalar", lambda: True),
    ("struct/bare-int", "top-level scalar", lambda: 7),
    ("struct/bare-string", "top-level scalar", lambda: "x"),
    ("struct/empty-string", "empty string is not absent", lambda: {"k": ""}),
    ("struct/single-space", "a lone space must survive verbatim", lambda: " "),
    ("struct/empty-key", "the empty string is a legal key", lambda: {"": 1}),
    ("struct/nested-empties", "nesting of empty containers", lambda: [[], [[]], [[[]]], {}]),
    # ---- ordering and whitespace -----------------------------------------
    ("struct/no-insignificant-whitespace", "separator bytes", lambda: {"a": 1, "b": [1, 2]}),
    ("struct/array-order-preserved", "array order is data", lambda: [3, 1, 2]),
    ("struct/recursive-key-sort", "sorting recurses into values", lambda: {"z": {"b": 1, "a": 2}, "a": [{"d": 1, "c": 2}]}),
    ("struct/tuple-serializes-as-array", "tuple normalizes to list", lambda: {"k": (1, 2)}),
    ("order/insertion-order-forward", "insertion order must not reach the bytes", lambda: {"one": 1, "two": 2, "three": 3}),
    ("order/insertion-order-reversed", "same dict built backwards - must pin identically", lambda: {"three": 3, "two": 2, "one": 1}),
    ("order/ascii-case-and-accent", "code-point sort, never locale collation", lambda: {"b": 1, "A": 2, "a": 3, "Z": 4, "é": 5, "É": 6}),
    ("order/digit-like-keys", "keys sort lexicographically, not numerically", lambda: {"10": 1, "9": 2, "1": 3}),
    ("order/astral-key-after-bmp", "astral key sorts after every BMP key", lambda: {"\U0001f600": 1, "￿": 2, "a": 3}),
    ("order/nfc-and-nfd-keys-are-distinct", "two keys that render identically are two keys", lambda: {"é": 1, "é": 2}),
    # ---- unicode ---------------------------------------------------------
    ("unicode/nfc-e-acute", "precomposed form", lambda: {"k": "é"}),
    ("unicode/nfd-e-acute", "decomposed form - looks identical, must NOT match NFC", lambda: {"k": "é"}),
    ("unicode/astral-clef", "astral plane, 4-byte UTF-8", lambda: {"k": "\U0001d11e"}),
    ("unicode/emoji-zwj-family", "ZWJ sequence spanning several code points", lambda: {"k": "\U0001f469‍\U0001f469‍\U0001f467‍\U0001f466"}),
    ("unicode/regional-indicator-flag", "two regional indicators, one rendered glyph", lambda: {"k": "\U0001f1fa\U0001f1f8"}),
    ("unicode/bmp-max", "U+FFFF, the last BMP code point", lambda: {"k": "￿"}),
    ("unicode/line-and-paragraph-separator", "U+2028/U+2029 are emitted raw, not escaped", lambda: {"k": "  "}),
    ("unicode/bom-inside-string", "U+FEFF mid-payload is content, not a marker", lambda: {"k": "a﻿b"}),
    ("unicode/del-is-not-escaped", "U+007F is a control character JSON does not escape", lambda: {"k": "\x7f"}),
    ("unicode/rtl-override", "bidi override survives verbatim", lambda: {"k": "‮"}),
    ("unicode/stacked-combining-marks", "multiple combining marks keep their order", lambda: {"k": "á̂̃"}),
    ("unicode/non-ascii-unescaped", "ensure_ascii=False emits real UTF-8", lambda: {"k": "café"}),
    # ---- escaping --------------------------------------------------------
    ("escape/json-mandatory", "the six escapes JSON requires", lambda: {"k": '"\\\b\f\n\r\t'}),
    ("escape/c0-controls", "C0 controls become \\u00XX", lambda: {"k": "\x00\x01\x1f"}),
    ("escape/solidus-not-escaped", "forward slash is NOT escaped", lambda: {"k": "a/b"}),
    # ---- integers --------------------------------------------------------
    ("int/zero", "zero", lambda: {"k": 0}),
    ("int/negative", "sign", lambda: {"k": -1}),
    ("int/2pow53-minus-1", "last integer an IEEE double represents exactly", lambda: {"k": 2**53 - 1}),
    ("int/2pow53", "the double boundary itself", lambda: {"k": 2**53}),
    ("int/2pow53-plus-1", "first integer a double cannot represent", lambda: {"k": 2**53 + 1}),
    ("int/2pow63", "signed 64-bit boundary", lambda: {"k": 2**63}),
    ("int/2pow64", "unsigned 64-bit boundary", lambda: {"k": 2**64}),
    ("int/neg-2pow128", "far beyond any machine word, negative", lambda: {"k": -(2**128)}),
    ("int/200-digit", "arbitrary precision integer", lambda: {"k": 10**200}),
    ("int/bool-is-not-int", "bool subclasses int but must emit true/false", lambda: {"t": True, "f": False, "n": None, "i": 1, "z": 0}),
    # ---- bounds ----------------------------------------------------------
    ("bounds/depth-64-accepted", "exactly at MAX_DEPTH", lambda: _depth_chain(64)),
    ("bounds/width-4096-accepted", "exactly at MAX_WIDTH", lambda: _wide(4096)),
    # ---- refusals --------------------------------------------------------
    ("refuse/float", "binary float does not round-trip", lambda: {"k": 1.5}),
    ("refuse/float-whole", "even 1.0 is refused - the type is the problem", lambda: {"k": 1.0}),
    ("refuse/nan", "NaN", lambda: {"k": float("nan")}),
    ("refuse/inf", "+Infinity", lambda: {"k": float("inf")}),
    ("refuse/neg-inf", "-Infinity", lambda: {"k": float("-inf")}),
    ("refuse/int-key", "non-string key", lambda: {1: "a"}),
    ("refuse/bool-key", "bool key - and bool is an int subclass", lambda: {True: "a"}),
    ("refuse/none-key", "None key", lambda: {None: "a"}),
    ("refuse/float-key", "float key", lambda: {1.5: "a"}),
    ("refuse/tuple-key", "hashable but unserializable key", lambda: {(1, 2): "a"}),
    ("refuse/set", "set has no JSON form and no stable order", lambda: {"k": {1, 2}}),
    ("refuse/frozenset", "frozenset likewise", lambda: {"k": frozenset({1})}),
    ("refuse/bytes", "bytes are not text", lambda: {"k": b"ab"}),
    ("refuse/bytearray", "bytearray likewise", lambda: {"k": bytearray(b"ab")}),
    ("refuse/complex", "complex", lambda: {"k": complex(1, 2)}),
    ("refuse/decimal", "Decimal is exact but not JSON-native", lambda: {"k": Decimal("1.5")}),
    ("refuse/datetime", "datetime has many textual forms", lambda: {"k": datetime.datetime(2026, 1, 1)}),
    ("refuse/plain-object", "an arbitrary object", lambda: {"k": object()}),
    ("refuse/surrogate-value-lone-high", "lone high surrogate has no UTF-8 encoding", lambda: {"k": "\ud800"}),
    ("refuse/surrogate-value-lone-low-midstring", "lone low surrogate, index is reported", lambda: {"k": "ab\udfffc"}),
    ("refuse/surrogate-key", "lone surrogate in a key", lambda: {"\ud800": 1}),
    ("refuse/surrogate-pair-split-across-strings", "an unpaired half is unpaired even beside its mate", lambda: {"a": "\ud834", "b": "\udd1e"}),
    ("refuse/depth-65", "one past MAX_DEPTH", lambda: _depth_chain(65)),
    ("refuse/width-4097", "one past MAX_WIDTH", lambda: _wide(4097)),
    ("refuse/size-over-limit", "canonical bytes exceed MAX_SIZE_BYTES", _oversize_payload),
)


PARSE_VECTORS = (
    ("parse/canonical-object", "the ordinary path", lambda: b'{"a":1,"b":[1,2]}'),
    ("parse/big-int-literal", "arbitrary-precision int survives the parse", lambda: b'{"a":123456789012345678901234567890}'),
    ("parse/2pow53-plus-1-literal", "no silent float coercion at the double boundary", lambda: b'{"a":9007199254740993}'),
    ("parse/escaped-surrogate-pair-is-astral", "a WELL-FORMED pair decodes to one astral character", lambda: b'{"k":"\\ud834\\udd1e"}'),
    ("parse/utf8-astral-direct", "the same character as raw UTF-8", lambda: '{"k":"\U0001d11e"}'.encode("utf-8")),
    ("parse/nfd-round-trip", "decomposed text is not normalized on the way through", lambda: '{"k":"é"}'.encode("utf-8")),
    ("parse/duplicate-key", "last-value-wins is an ambiguity, so it is a refusal", lambda: b'{"a":1,"a":2}'),
    ("parse/nested-duplicate-key", "the check recurses", lambda: b'{"outer":{"k":1,"k":2}}'),
    ("parse/trailing-content", "two values in one payload", lambda: b'{"a":1} trailing'),
    ("parse/leading-whitespace", "raw_decode does not skip leading whitespace", lambda: b'  {"a":1}'),
    ("parse/not-json", "syntax error names line and column only", lambda: b"{oops}"),
    ("parse/invalid-utf8", "a byte sequence that is not UTF-8", lambda: b'{"a":"\xff"}'),
    ("parse/float-literal", "a float in the wire form is still a float", lambda: b'{"a":1.5}'),
    ("parse/exponent-overflow-to-inf", "1e400 parses to inf, which is refused", lambda: b'{"a":1e400}'),
    ("parse/nan-literal", "json.loads accepts bare NaN; canonical JSON does not", lambda: b'{"a":NaN}'),
    ("parse/escaped-lone-surrogate", "a U+D800 escape must not survive the parse", lambda: b'{"k":"\\ud800"}'),
    ("parse/depth-65", "depth is re-checked on the decoded structure", lambda: ('{"n":' * 64 + "{}" + "}" * 64).encode("utf-8")),
    ("parse/oversize", "size is checked before decoding", lambda: b'{"a":"' + b"x" * MAX_SIZE_BYTES + b'"}'),
    ("parse/wrong-input-type", "loads_strict takes str or bytes, nothing else", lambda: 123),
)


# --------------------------------------------------------------------------
# Execution: one classifier, used identically in-process and in the subprocess
# --------------------------------------------------------------------------


def _classify(run):
    """Run `run` and describe the outcome as plain JSON-safe data.

    `escaped` is deliberately its own outcome and appears in no pinned table: an
    exception that is not a `CanonicalJSONError` - a `UnicodeEncodeError`, or a
    `NameError` from code reaching for a name it does not own - lands there and
    fails loudly with its type named, instead of being absorbed into a refusal.
    """
    try:
        raw = run()
    except CanonicalJSONError as exc:
        return {"outcome": "refuse", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - deliberately visible, never swallowed
        return {"outcome": "escaped", "error_type": type(exc).__name__, "message": str(exc)}
    return {
        "outcome": "accept",
        "text": raw.decode("utf-8"),
        "digest": hashlib.sha256(raw).hexdigest(),
    }


def run_serialize_corpus():
    results = {}
    for vector_id, _note, builder in SERIALIZE_VECTORS:
        try:
            payload = builder()
        except Exception as exc:  # noqa: BLE001
            results[vector_id] = {"outcome": "builder_error", "error_type": type(exc).__name__}
            continue
        record = _classify(lambda payload=payload: canonical_bytes(payload))
        if record["outcome"] == "accept":
            # canonical_digest must be exactly sha256(canonical_bytes), not a
            # second, independently drifting implementation.
            record["module_digest"] = canonical_digest(payload)
        results[vector_id] = record
    return results


def run_parse_corpus():
    results = {}
    for vector_id, _note, builder in PARSE_VECTORS:
        try:
            raw = builder()
        except Exception as exc:  # noqa: BLE001
            results[vector_id] = {"outcome": "builder_error", "error_type": type(exc).__name__}
            continue
        # Parse, then re-canonicalize, so the pin covers the whole round trip.
        # `parse/wrong-input-type` deliberately hands loads_strict something that
        # is neither str nor bytes - refusing that IS the vector - so the cast is
        # here to keep the checker quiet without weakening the real signature.
        results[vector_id] = _classify(
            lambda raw=raw: canonical_bytes(loads_strict(cast(Any, raw)))
        )
    return results


def corpus_report():
    return {
        "python": list(sys.version_info[:3]),
        "serialize": run_serialize_corpus(),
        "parse": run_parse_corpus(),
    }


# --------------------------------------------------------------------------
# The pinned table. Literal constants. Do not regenerate these from the module
# under test - re-deriving a pin from the thing it pins deletes the pin.
# --------------------------------------------------------------------------

# `bounds/depth-64-accepted` and `bounds/width-4096-accepted` produce tens of
# kilobytes of canonical text, so their expected text is spelled as an
# independent formula rather than inlined. The formulas are written from the
# canonical JSON grammar directly and share no code with the canonicalizer.
_DEPTH64_TEXT = '{"n":' * 63 + "{}" + "}" * 63
_WIDTH4096_TEXT = (
    "{" + ",".join('"%s":%d' % (k, int(k[1:])) for k in sorted("k%d" % i for i in range(4096))) + "}"
)

EXPECTED_SERIALIZE = {
    # empty container
    'struct/empty-object': (
        "accept",
        '{}',
        '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a',
    ),
    # empty container
    'struct/empty-array': (
        "accept",
        '[]',
        '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945',
    ),
    # top-level scalar
    'struct/bare-null': (
        "accept",
        'null',
        '74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b',
    ),
    # top-level scalar
    'struct/bare-true': (
        "accept",
        'true',
        'b5bea41b6c623f7c09f1bf24dcae58ebab3c0cdd90ad966bc43a45b44867e12b',
    ),
    # top-level scalar
    'struct/bare-int': (
        "accept",
        '7',
        '7902699be42c8a8e46fbbb4501726517e86b22c56a189f7625a6da49081b2451',
    ),
    # top-level scalar
    'struct/bare-string': (
        "accept",
        '"x"',
        'ba2df4903a2c14e86dc3bcca58911b44ac1d2514b7227bf6eb08cfb978f55a1b',
    ),
    # empty string is not absent
    'struct/empty-string': (
        "accept",
        '{"k":""}',
        '780dbee244ff7855be35a16ef5473b11ca05844f96977ba39be7f49a2434fbef',
    ),
    # a lone space must survive verbatim
    'struct/single-space': (
        "accept",
        '" "',
        '52109349dabf69106e04ec2f493fb8b6ade94ea100227cccce6559ab8b96553f',
    ),
    # the empty string is a legal key
    'struct/empty-key': (
        "accept",
        '{"":1}',
        'f3d86de1bf4b354382f91a12a4487daaeb56692b2c46f2df52f6f83a84610074',
    ),
    # nesting of empty containers
    'struct/nested-empties': (
        "accept",
        '[[],[[]],[[[]]],{}]',
        'e2d76db9553e5e2f69223f8aa13b1e70a7acf96bd198a3d624b400bca64b63b4',
    ),
    # separator bytes
    'struct/no-insignificant-whitespace': (
        "accept",
        '{"a":1,"b":[1,2]}',
        '8baa73198470c7bb4c3ce142a8fd651affc0310d878bb9bd159e37a573fb4874',
    ),
    # array order is data
    'struct/array-order-preserved': (
        "accept",
        '[3,1,2]',
        '51bda7ab4e44726cde71fcb6e4b515357059bb6b6dd5146d1fc50f73f11678c6',
    ),
    # sorting recurses into values
    'struct/recursive-key-sort': (
        "accept",
        '{"a":[{"c":2,"d":1}],"z":{"a":2,"b":1}}',
        '30c7456111a655707102c6d377ea8f955daa1e943d1de97412564a9f2336124c',
    ),
    # tuple normalizes to list
    'struct/tuple-serializes-as-array': (
        "accept",
        '{"k":[1,2]}',
        'f917c469ead2dde9740dc5f586b36c60e2be16cfa9c25ef90d85f9dc2a25c6f5',
    ),
    # insertion order must not reach the bytes
    'order/insertion-order-forward': (
        "accept",
        '{"one":1,"three":3,"two":2}',
        '98078dde5b56b0396ae0ee93c09e0d927e11c32da1287369b88a7593edc4e449',
    ),
    # same dict built backwards - must pin identically
    'order/insertion-order-reversed': (
        "accept",
        '{"one":1,"three":3,"two":2}',
        '98078dde5b56b0396ae0ee93c09e0d927e11c32da1287369b88a7593edc4e449',
    ),
    # code-point sort, never locale collation
    'order/ascii-case-and-accent': (
        "accept",
        '{"A":2,"Z":4,"a":3,"b":1,"É":6,"é":5}',
        '859929d9310c3b4e660356c58c62e4bcc66cd3245a070f9a773bb534f40353ff',
    ),
    # keys sort lexicographically, not numerically
    'order/digit-like-keys': (
        "accept",
        '{"1":3,"10":1,"9":2}',
        '38a3fdd6f89c34bb41bdb86e885678938b4aaaa0b743d49f3f509d65bbefa88e',
    ),
    # astral key sorts after every BMP key
    'order/astral-key-after-bmp': (
        "accept",
        '{"a":3,"\uffff":2,"😀":1}',
        '48f8c136ce1b1fb6cc384c2a496fcab8d870f137c9ef71a97d7c4475f6d0eb5d',
    ),
    # two keys that render identically are two keys
    'order/nfc-and-nfd-keys-are-distinct': (
        "accept",
        '{"é":2,"é":1}',
        'a7962fb10dc1255be368ece9c22b2256605921dc6d0a8c9409d3ee406bcb86e5',
    ),
    # precomposed form
    'unicode/nfc-e-acute': (
        "accept",
        '{"k":"é"}',
        '0ca09f1dffb485d259fc791100d48ad7ae9c17f52a2bb07b608c0e28fbca34a1',
    ),
    # decomposed form - looks identical, must NOT match NFC
    'unicode/nfd-e-acute': (
        "accept",
        '{"k":"é"}',
        '4cb477ab754099c91e4c79f77deaab085090978b8abbc981c8eefde872575da8',
    ),
    # astral plane, 4-byte UTF-8
    'unicode/astral-clef': (
        "accept",
        '{"k":"𝄞"}',
        'decda82ae588bfdb43a007098b71db644ffb413392f2d67eae5f8b98831d4731',
    ),
    # ZWJ sequence spanning several code points
    'unicode/emoji-zwj-family': (
        "accept",
        '{"k":"👩\u200d👩\u200d👧\u200d👦"}',
        '522ec9abb72afd07710cee738cfa9b7df6aed05d35358781f23ed38a6c2d2758',
    ),
    # two regional indicators, one rendered glyph
    'unicode/regional-indicator-flag': (
        "accept",
        '{"k":"🇺🇸"}',
        '393a8b17a9b857a78eb1fd1fb39e9a1eafba62749582f9b19d98237c6ee04e92',
    ),
    # U+FFFF, the last BMP code point
    'unicode/bmp-max': (
        "accept",
        '{"k":"\uffff"}',
        'd40f11c8ee86795c4b1bbe30b8f64e007714eeb894370d012a4a741ec6d1af1d',
    ),
    # U+2028/U+2029 are emitted raw, not escaped
    'unicode/line-and-paragraph-separator': (
        "accept",
        '{"k":"\u2028\u2029"}',
        '4bce7ce75d13dc32564fdce8d75f8bee43d0dc94b9dc5bcf73d249dc49ebe1f7',
    ),
    # U+FEFF mid-payload is content, not a marker
    'unicode/bom-inside-string': (
        "accept",
        '{"k":"a\ufeffb"}',
        '7f25bf3190effe865231b8a29806e2b9dcf9d921842afed512f7f7617755c44d',
    ),
    # U+007F is a control character JSON does not escape
    'unicode/del-is-not-escaped': (
        "accept",
        '{"k":"\x7f"}',
        '2ce383ee565d038f6ee3602e1bfebf757649531d458792d6c814d02881fb98fb',
    ),
    # bidi override survives verbatim
    'unicode/rtl-override': (
        "accept",
        '{"k":"\u202e"}',
        '5f70906597f0ea6c8cfe00b8a60d0615ae2804a85b11551dbdd80c0e782c214e',
    ),
    # multiple combining marks keep their order
    'unicode/stacked-combining-marks': (
        "accept",
        '{"k":"á̂̃"}',
        '3067af15362343dd38ac22aad15d0a79f8cf283c3d3ff2865c54ced2053b7b25',
    ),
    # ensure_ascii=False emits real UTF-8
    'unicode/non-ascii-unescaped': (
        "accept",
        '{"k":"café"}',
        '2303df0176226e83b89fa2a9311d76a8a4c29b0e8ae83ffaa0e431fa4f8b5359',
    ),
    # the six escapes JSON requires
    'escape/json-mandatory': (
        "accept",
        '{"k":"\\"\\\\\\b\\f\\n\\r\\t"}',
        '6f564da980a811a6d4a79c901bd0ae05a04cb506fd5d5ab5d6c9240931e1307f',
    ),
    # C0 controls become \u00XX
    'escape/c0-controls': (
        "accept",
        '{"k":"\\u0000\\u0001\\u001f"}',
        'b3b0a5f35cb1beac35fde92890503c36f71f70c0130b72cf66cfd76521dcc676',
    ),
    # forward slash is NOT escaped
    'escape/solidus-not-escaped': (
        "accept",
        '{"k":"a/b"}',
        '2df8c51747b2319a3820aea161c47aa9641e8d555a69da59ed5c7c4a1b5f6e3d',
    ),
    # zero
    'int/zero': (
        "accept",
        '{"k":0}',
        '90ad26fe5bc6a0cbc55a8ad437018970348ad7fad461661044c583af09816e00',
    ),
    # sign
    'int/negative': (
        "accept",
        '{"k":-1}',
        'b96b3e8dabce8e3ab8d3d4af14067cd10a1848703574dad58604e0c4fcbb5119',
    ),
    # last integer an IEEE double represents exactly
    'int/2pow53-minus-1': (
        "accept",
        '{"k":9007199254740991}',
        'ca2ab790d99d339e9c714ee69acb65043c5038dad3e0b87efb83e643d559707d',
    ),
    # the double boundary itself
    'int/2pow53': (
        "accept",
        '{"k":9007199254740992}',
        '799ce8f671b780c217ffe57b48fae8c817e45b9d4901dadf575e44464b415d2b',
    ),
    # first integer a double cannot represent
    'int/2pow53-plus-1': (
        "accept",
        '{"k":9007199254740993}',
        'cdfe289eaa3afcc4d723163262508c03d5fca3dbed9e679afcfae430dff5f1aa',
    ),
    # signed 64-bit boundary
    'int/2pow63': (
        "accept",
        '{"k":9223372036854775808}',
        'b193a3f941a5e464437969eb03013048cdc668611519288707f210e0b5221244',
    ),
    # unsigned 64-bit boundary
    'int/2pow64': (
        "accept",
        '{"k":18446744073709551616}',
        '2d3c4ab0a8c658b6becb9f9f804602f3648ec9c80826a16a9171c1a93835ba10',
    ),
    # far beyond any machine word, negative
    'int/neg-2pow128': (
        "accept",
        '{"k":-340282366920938463463374607431768211456}',
        '547613b231f2460335035a308805cd193d7c7671e46502ecbb8a9f645bb1288e',
    ),
    # arbitrary precision integer
    'int/200-digit': (
        "accept",
        '{"k":100000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000}',
        'f984b937ec5f2f5698a4212f6bcb065159a1fc928314961fda6f2ce7f3ba7698',
    ),
    # bool subclasses int but must emit true/false
    'int/bool-is-not-int': (
        "accept",
        '{"f":false,"i":1,"n":null,"t":true,"z":0}',
        'c83f35af86a87bbd111b7df5d623b64131ffcbad5b11175741f8a50036c0913c',
    ),
    # exactly at MAX_DEPTH
    'bounds/depth-64-accepted': (
        "accept",
        _DEPTH64_TEXT,
        '10f5148f8d9dde8baf2d5647cf383444a33550cf3dcdf37328f16a49f1476869',
    ),
    # exactly at MAX_WIDTH
    'bounds/width-4096-accepted': (
        "accept",
        _WIDTH4096_TEXT,
        'd811c661a9bb8241dca2fa872f5091a527eaf78cd38232242932734b417c5e45',
    ),
    # binary float does not round-trip
    'refuse/float': (
        "refuse",
        'float values are not canonical; binary floating point does not round-trip identically across runtimes - use a string or a scaled integer',
    ),
    # even 1.0 is refused - the type is the problem
    'refuse/float-whole': (
        "refuse",
        'float values are not canonical; binary floating point does not round-trip identically across runtimes - use a string or a scaled integer',
    ),
    # NaN
    'refuse/nan': (
        "refuse",
        'float values are not canonical; binary floating point does not round-trip identically across runtimes - use a string or a scaled integer',
    ),
    # +Infinity
    'refuse/inf': (
        "refuse",
        'float values are not canonical; binary floating point does not round-trip identically across runtimes - use a string or a scaled integer',
    ),
    # -Infinity
    'refuse/neg-inf': (
        "refuse",
        'float values are not canonical; binary floating point does not round-trip identically across runtimes - use a string or a scaled integer',
    ),
    # non-string key
    'refuse/int-key': (
        "refuse",
        'object keys must be strings; found int',
    ),
    # bool key - and bool is an int subclass
    'refuse/bool-key': (
        "refuse",
        'object keys must be strings; found bool',
    ),
    # None key
    'refuse/none-key': (
        "refuse",
        'object keys must be strings; found NoneType',
    ),
    # float key
    'refuse/float-key': (
        "refuse",
        'object keys must be strings; found float',
    ),
    # hashable but unserializable key
    'refuse/tuple-key': (
        "refuse",
        'object keys must be strings; found tuple',
    ),
    # set has no JSON form and no stable order
    'refuse/set': (
        "refuse",
        'unsupported type for canonical JSON: set',
    ),
    # frozenset likewise
    'refuse/frozenset': (
        "refuse",
        'unsupported type for canonical JSON: frozenset',
    ),
    # bytes are not text
    'refuse/bytes': (
        "refuse",
        'unsupported type for canonical JSON: bytes',
    ),
    # bytearray likewise
    'refuse/bytearray': (
        "refuse",
        'unsupported type for canonical JSON: bytearray',
    ),
    # complex
    'refuse/complex': (
        "refuse",
        'unsupported type for canonical JSON: complex',
    ),
    # Decimal is exact but not JSON-native
    'refuse/decimal': (
        "refuse",
        'unsupported type for canonical JSON: Decimal',
    ),
    # datetime has many textual forms
    'refuse/datetime': (
        "refuse",
        'unsupported type for canonical JSON: datetime',
    ),
    # an arbitrary object
    'refuse/plain-object': (
        "refuse",
        'unsupported type for canonical JSON: object',
    ),
    # lone high surrogate has no UTF-8 encoding
    'refuse/surrogate-value-lone-high': (
        "refuse",
        'string value contains an unpaired surrogate at index 0; an unpaired surrogate has no UTF-8 encoding, so it cannot be canonicalized',
    ),
    # lone low surrogate, index is reported
    'refuse/surrogate-value-lone-low-midstring': (
        "refuse",
        'string value contains an unpaired surrogate at index 2; an unpaired surrogate has no UTF-8 encoding, so it cannot be canonicalized',
    ),
    # lone surrogate in a key
    'refuse/surrogate-key': (
        "refuse",
        'object key contains an unpaired surrogate at index 0; an unpaired surrogate has no UTF-8 encoding, so it cannot be canonicalized',
    ),
    # an unpaired half is unpaired even beside its mate
    'refuse/surrogate-pair-split-across-strings': (
        "refuse",
        'string value contains an unpaired surrogate at index 0; an unpaired surrogate has no UTF-8 encoding, so it cannot be canonicalized',
    ),
    # one past MAX_DEPTH
    'refuse/depth-65': (
        "refuse",
        'payload exceeds maximum nesting depth of 64',
    ),
    # one past MAX_WIDTH
    'refuse/width-4097': (
        "refuse",
        'object exceeds maximum width of 4096 keys',
    ),
    # canonical bytes exceed MAX_SIZE_BYTES
    'refuse/size-over-limit': (
        "refuse",
        'canonical payload size 2097160 exceeds maximum of 2097152 bytes',
    ),
}

EXPECTED_PARSE = {
    # the ordinary path
    'parse/canonical-object': (
        "accept",
        '{"a":1,"b":[1,2]}',
        '8baa73198470c7bb4c3ce142a8fd651affc0310d878bb9bd159e37a573fb4874',
    ),
    # arbitrary-precision int survives the parse
    'parse/big-int-literal': (
        "accept",
        '{"a":123456789012345678901234567890}',
        '08d8ea6bf3a5b2cb046add37cfff5567b0fe380cfb61426366e904fd757988cd',
    ),
    # no silent float coercion at the double boundary
    'parse/2pow53-plus-1-literal': (
        "accept",
        '{"a":9007199254740993}',
        'd509df67f639ccc7538a0e52d149de6b89e9a93e16d350095bb4478957f410ca',
    ),
    # a WELL-FORMED pair decodes to one astral character
    'parse/escaped-surrogate-pair-is-astral': (
        "accept",
        '{"k":"𝄞"}',
        'decda82ae588bfdb43a007098b71db644ffb413392f2d67eae5f8b98831d4731',
    ),
    # the same character as raw UTF-8
    'parse/utf8-astral-direct': (
        "accept",
        '{"k":"𝄞"}',
        'decda82ae588bfdb43a007098b71db644ffb413392f2d67eae5f8b98831d4731',
    ),
    # decomposed text is not normalized on the way through
    'parse/nfd-round-trip': (
        "accept",
        '{"k":"é"}',
        '4cb477ab754099c91e4c79f77deaab085090978b8abbc981c8eefde872575da8',
    ),
    # last-value-wins is an ambiguity, so it is a refusal
    'parse/duplicate-key': (
        "refuse",
        "duplicate object key 'a'; a payload may not carry two meanings",
    ),
    # the check recurses
    'parse/nested-duplicate-key': (
        "refuse",
        "duplicate object key 'k'; a payload may not carry two meanings",
    ),
    # two values in one payload
    'parse/trailing-content': (
        "refuse",
        'payload carries trailing content after the JSON value',
    ),
    # raw_decode does not skip leading whitespace
    'parse/leading-whitespace': (
        "refuse",
        'payload is not valid JSON (line 1, column 1)',
    ),
    # syntax error names line and column only
    'parse/not-json': (
        "refuse",
        'payload is not valid JSON (line 1, column 2)',
    ),
    # a byte sequence that is not UTF-8
    'parse/invalid-utf8': (
        "refuse",
        'payload is not valid UTF-8',
    ),
    # a float in the wire form is still a float
    'parse/float-literal': (
        "refuse",
        'float values are not canonical; binary floating point does not round-trip identically across runtimes - use a string or a scaled integer',
    ),
    # 1e400 parses to inf, which is refused
    'parse/exponent-overflow-to-inf': (
        "refuse",
        'float values are not canonical; binary floating point does not round-trip identically across runtimes - use a string or a scaled integer',
    ),
    # json.loads accepts bare NaN; canonical JSON does not
    'parse/nan-literal': (
        "refuse",
        'float values are not canonical; binary floating point does not round-trip identically across runtimes - use a string or a scaled integer',
    ),
    # a U+D800 escape must not survive the parse
    'parse/escaped-lone-surrogate': (
        "refuse",
        'string value contains an unpaired surrogate at index 0; an unpaired surrogate has no UTF-8 encoding, so it cannot be canonicalized',
    ),
    # depth is re-checked on the decoded structure
    'parse/depth-65': (
        "refuse",
        'payload exceeds maximum nesting depth of 64',
    ),
    # size is checked before decoding
    'parse/oversize': (
        "refuse",
        'payload size 2097160 exceeds maximum of 2097152 bytes',
    ),
    # loads_strict takes str or bytes, nothing else
    'parse/wrong-input-type': (
        "refuse",
        'strict parse requires str or bytes; found int',
    ),
}


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def assert_corpus_matches(case, actual, expected, label):
    """Compare a corpus report against the pinned table.

    Module-level and explicit about both operands on purpose. As a method reached
    across test classes it was called with a foreign receiver, which is the exact
    shape that ends up comparing a corpus to itself and calling that agreement.
    """
    case.assertEqual(sorted(actual), sorted(expected), "%s: vector set changed" % label)
    for vector_id in sorted(expected):
        with case.subTest(source=label, vector=vector_id):
            want = expected[vector_id]
            got = actual[vector_id]
            case.assertEqual(
                got["outcome"],
                want[0],
                "%s: outcome changed for %s: %r" % (label, vector_id, got),
            )
            if want[0] == "accept":
                case.assertEqual(got["text"], want[1], "%s: canonical bytes changed" % label)
                case.assertEqual(got["digest"], want[2], "%s: pinned digest changed" % label)
            else:
                case.assertEqual(got["message"], want[1], "%s: refusal message changed" % label)


class PinnedCorpusTests(unittest.TestCase):
    """The corpus, under whichever interpreter is running this suite."""

    maxDiff = None

    def test_pinned_table_covers_every_vector_exactly(self):
        """No vector may be added without a pin, or removed while its pin stays."""
        self.assertEqual(
            sorted(vector_id for vector_id, _n, _b in SERIALIZE_VECTORS),
            sorted(EXPECTED_SERIALIZE),
        )
        self.assertEqual(
            sorted(vector_id for vector_id, _n, _b in PARSE_VECTORS),
            sorted(EXPECTED_PARSE),
        )
        self.assertEqual(len(SERIALIZE_VECTORS), len({v for v, _n, _b in SERIALIZE_VECTORS}))
        self.assertEqual(len(PARSE_VECTORS), len({v for v, _n, _b in PARSE_VECTORS}))

    def test_pinned_digests_match_pinned_bytes_independently(self):
        """sha256(pinned text) == pinned digest, computed without the module.

        This is what stops the pins from being two copies of one number. Neither
        `canonical_bytes` nor `canonical_digest` is called here.
        """
        pinned = 0
        for table in (EXPECTED_SERIALIZE, EXPECTED_PARSE):
            for vector_id, expected in table.items():
                if expected[0] != "accept":
                    continue
                with self.subTest(vector=vector_id):
                    _kind, text, digest = expected
                    self.assertEqual(
                        hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        digest,
                        "pinned digest does not match pinned bytes",
                    )
                    pinned += 1
        self.assertGreater(pinned, 50, "the accept corpus shrank unexpectedly")

    def test_serialize_corpus_matches_the_pinned_table(self):
        assert_corpus_matches(self, run_serialize_corpus(), EXPECTED_SERIALIZE, "in-process serialize")

    def test_parse_corpus_matches_the_pinned_table(self):
        assert_corpus_matches(self, run_parse_corpus(), EXPECTED_PARSE, "in-process parse")

    def test_canonical_digest_equals_sha256_of_canonical_bytes_for_every_vector(self):
        for vector_id, record in run_serialize_corpus().items():
            if record["outcome"] != "accept":
                continue
            with self.subTest(vector=vector_id):
                self.assertEqual(record["module_digest"], record["digest"])

    def test_nfc_and_nfd_never_collide(self):
        """Two strings that render identically must not share a digest.

        No normalization is applied anywhere, deliberately: normalizing would mean
        the sealed bytes differ from the bytes the caller sent.
        """
        self.assertNotEqual(
            EXPECTED_SERIALIZE["unicode/nfc-e-acute"][2],
            EXPECTED_SERIALIZE["unicode/nfd-e-acute"][2],
        )

    def test_insertion_order_does_not_reach_the_digest(self):
        self.assertEqual(
            EXPECTED_SERIALIZE["order/insertion-order-forward"],
            EXPECTED_SERIALIZE["order/insertion-order-reversed"],
        )



class CrossInterpreterTests(unittest.TestCase):
    """The same corpus, executed by a second, genuinely different CPython.

    Symmetric on purpose: whichever of 3.12 / 3.14 drives the suite, the other is
    the counterpart, and 3.12 must be one of the two. An earlier version searched
    only for "a 3.12" and, when run BY 3.12, found itself - the report it compared
    was its own, and the comparison was trivially true while looking green.

    HOST result. Says nothing about the interpreter inside the built image.
    """

    maxDiff = None

    interpreter: str | None = None
    version: tuple[int, ...] | None = None
    blocked_reason: str | None = None
    status: str = STATUS_BLOCKED

    #: Raw stdout of the counterpart run, cached so the corpus is not rebuilt per
    #: test. Deliberately cached as TEXT, not as a parsed object: every test parses
    #: it afresh, so the local and remote reports can never be the same object.
    _raw_report: str | None = None

    @classmethod
    def setUpClass(cls):
        try:
            cls.interpreter, cls.version = find_counterpart_interpreter()
            cls.blocked_reason = None
            cls.status = STATUS_EXERCISED
        except InterpreterUnavailable as exc:
            cls.interpreter = None
            cls.version = None
            cls.blocked_reason = str(exc)
            cls.status = STATUS_BLOCKED

    # -- the two conditions the orchestrator asked to keep separate ---------

    def _blocked(self) -> NoReturn:
        """No counterpart exists. NEVER EXERCISED - a skip, never a pass.

        Declared NoReturn because both branches raise. That is not decoration: it
        is what lets every caller below reach `self.version` without a possible
        None in hand, instead of guarding and hoping.
        """
        message = (
            "%s: %s. The cross-interpreter matrix was NEVER EXERCISED - this is "
            "not a pass and not a failure, it is an instrument that did not run. "
            "Point %s at a CPython %d.%d, or set %s=1 to make this state fatal."
            % (
                STATUS_BLOCKED,
                self.blocked_reason,
                INTERPRETER_ENV_VAR,
                PRODUCTION_VERSION[0],
                PRODUCTION_VERSION[1],
                REQUIRE_ENV_VAR,
            )
        )
        if os.environ.get(REQUIRE_ENV_VAR):
            self.fail(message)
        self.skipTest(message)

    def _require_counterpart(self) -> tuple[str, tuple[int, ...]]:
        """The counterpart, or a block. Never a None that a caller must remember."""
        if self.interpreter is None or self.version is None:
            self._blocked()
        return self.interpreter, self.version

    def _counterpart_report(self):
        """Run the corpus under the counterpart. A broken run is a FAILURE.

        Distinct from `_blocked`: "no interpreter to run" and "the interpreter ran
        and produced nothing usable" are different facts and must not share a
        path. The second one means the instrument is broken, and a broken
        instrument is a failure, not a block.
        """
        interpreter, _version = self._require_counterpart()
        raw = type(self)._raw_report
        if raw is None:
            env = dict(os.environ)
            env["PYTHONPATH"] = str(REPO_ROOT)
            completed = subprocess.run(
                [
                    interpreter,
                    "-B",
                    "-m",
                    "tests.test_certification_cross_interpreter",
                    EMIT_FLAG,
                ],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                env=env,
                timeout=300,
            )
            self.assertEqual(
                completed.returncode,
                0,
                "%s exited %d emitting the corpus: %s"
                % (interpreter, completed.returncode, completed.stderr[-2000:]),
            )
            self.assertTrue(
                completed.stdout.strip(),
                "%s produced an empty corpus report" % interpreter,
            )
            raw = completed.stdout
            type(self)._raw_report = raw
        try:
            report = json.loads(raw)
        except ValueError as exc:
            self.fail("%s emitted unparseable corpus report: %s" % (interpreter, exc))
        for key in ("python", "serialize", "parse"):
            self.assertIn(key, report, "corpus report is missing %r" % key)
        return report

    # -- tests --------------------------------------------------------------

    def test_cross_interpreter_status_is_stated_explicitly(self):
        """Always runs, never skips. The log always says which world we are in."""
        self.assertIn(self.status, (STATUS_EXERCISED, STATUS_BLOCKED))
        sys.stderr.write(
            "\n[canonical-json cross-interpreter] status=%s driver=%d.%d.%d "
            "counterpart=%s scope=HOST-ONLY (says nothing about the built image)\n"
            % (
                self.status,
                sys.version_info[0],
                sys.version_info[1],
                sys.version_info[2],
                ("%d.%d.%d" % self.version) if self.version else "ABSENT",
            )
        )
        if self.status == STATUS_BLOCKED and os.environ.get(REQUIRE_ENV_VAR):
            self.fail(
                "%s: %s is set but no counterpart interpreter was found (%s)"
                % (STATUS_BLOCKED, REQUIRE_ENV_VAR, self.blocked_reason)
            )

    def test_production_version_312_is_one_of_the_two_interpreters(self):
        """3.12 is the version the image runs. A 3.13/3.14 pair would prove the
        wrong thing while looking exactly as green."""
        _interpreter, version = self._require_counterpart()
        self.assertIn(
            PRODUCTION_VERSION,
            (sys.version_info[:2], version[:2]),
            "neither interpreter is the production version",
        )

    def test_counterpart_is_a_different_real_cpython(self):
        _interpreter, version = self._require_counterpart()
        self.assertIn(version[:2], MATRIX_VERSIONS)
        self.assertNotEqual(
            version[:2],
            sys.version_info[:2],
            "the counterpart is the running interpreter; nothing was cross-checked",
        )

    def test_counterpart_reproduces_the_pinned_corpus_byte_for_byte(self):
        report = self._counterpart_report()
        label = "python%d.%d.%d" % tuple(report["python"][:3])
        assert_corpus_matches(self, report["serialize"], EXPECTED_SERIALIZE, label + " serialize")
        assert_corpus_matches(self, report["parse"], EXPECTED_PARSE, label + " parse")

    def test_the_two_interpreters_agree_vector_for_vector(self):
        """Whole-report equality, so a divergence fails whether pinned or not."""
        report = self._counterpart_report()
        self.assertEqual(report["serialize"], run_serialize_corpus())
        self.assertEqual(report["parse"], run_parse_corpus())

    def test_the_agreement_is_between_two_distinct_runs(self):
        """Guards the comparison itself, not the thing compared.

        "The corpora are identical" is worthless if they are the same object, the
        same process, or the same interpreter. This is the runtime counterpart of
        pinning digests as literals instead of recomputing them: it makes the
        agreement expensive to satisfy trivially.
        """
        report = self._counterpart_report()
        local = {"serialize": run_serialize_corpus(), "parse": run_parse_corpus()}

        # 1. Different objects, not two names for one dict.
        self.assertIsNot(report["serialize"], local["serialize"])
        self.assertIsNot(report["parse"], local["parse"])

        # 2. Different interpreters, reported by the interpreters themselves.
        self.assertNotEqual(
            tuple(report["python"][:3]),
            sys.version_info[:3],
            "the counterpart report came from this very interpreter",
        )

        # 3. Parsing the cached report twice yields two distinct objects, so a
        #    later refactor cannot quietly make the two sides one shared value.
        again = self._counterpart_report()
        self.assertIsNot(again["serialize"], report["serialize"])
        self.assertEqual(again["serialize"], report["serialize"])

        # 4. The comparison machinery bites. Perturb one pinned digest in a COPY
        #    of the remote report and confirm the assertion that just passed now
        #    fails - equality here is a real constraint, not a tautology.
        victim = "unicode/astral-clef"
        tampered = dict(report["serialize"])
        tampered[victim] = dict(tampered[victim])
        tampered[victim]["digest"] = "0" * 64
        self.assertNotEqual(tampered, local["serialize"])
        with self.assertRaises(AssertionError):
            assert_corpus_matches(
                unittest.TestCase(), tampered, EXPECTED_SERIALIZE, "tampered"
            )

    def test_refusal_surface_is_identical_across_interpreters(self):
        """A value one runtime accepts and the other refuses never becomes a
        digest, so a digest-only comparison is blind to exactly that divergence."""
        report = self._counterpart_report()
        local = dict(run_serialize_corpus())
        local.update(run_parse_corpus())
        remote = dict(report["serialize"])
        remote.update(report["parse"])
        self.assertEqual(
            {k: v["outcome"] for k, v in local.items()},
            {k: v["outcome"] for k, v in remote.items()},
        )
        self.assertEqual(
            {k: v.get("message") for k, v in local.items() if v["outcome"] == "refuse"},
            {k: v.get("message") for k, v in remote.items() if v["outcome"] == "refuse"},
        )

    def test_no_vector_escapes_the_typed_refusal_surface(self):
        """`escaped` means an exception that is not a CanonicalJSONError got out -
        a UnicodeEncodeError, or a NameError from code reaching for a name it does
        not own. Neither may be absorbed into something that reads like a refusal."""
        combined = dict(run_serialize_corpus())
        combined.update(run_parse_corpus())
        escaped = {
            vector_id: record
            for vector_id, record in combined.items()
            if record["outcome"] in ("escaped", "builder_error")
        }
        self.assertEqual(escaped, {}, "untyped exception escaped canonicalization")


class InImageScopeTests(unittest.TestCase):
    """What this file does NOT prove, recorded so it cannot be mistaken for done."""

    def test_in_image_parity_is_instrument_blocked(self):
        self.skipTest(
            "%s: the interpreter INSIDE the built image is unverified. Everything "
            "in this file is a HOST result - two interpreters on this machine - and "
            "a host result is not a production stamp. Building the image is "
            "Baylor-only. Exact command:\n%s"
            % (STATUS_BLOCKED, IN_IMAGE_VERIFICATION_COMMAND)
        )

    def test_the_blocked_command_is_recorded_and_specific(self):
        """The command must stay a command, not decay into a description."""
        for fragment in (
            "docker run",
            "--entrypoint python",
            "sys.version_info[:2] == (3, 12)",
            EMIT_FLAG,
            "IMAGE_DIGEST",
        ):
            self.assertIn(fragment, IN_IMAGE_VERIFICATION_COMMAND)


if __name__ == "__main__":
    if EMIT_FLAG in sys.argv:
        json.dump(corpus_report(), sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
        raise SystemExit(0)
    unittest.main()
