"""SiteSift canonical JSON v1.

One bounded deterministic canonicalizer shared by the deployed certification route,
the runner, the tests, and the ranker. Every certification identity is a lowercase
SHA-256 over bytes produced here: the runtime scenario-registry digest, the sealed
canonical input digest, and the evidence digest.

The rules, and why each exists:

* **UTF-8, unescaped.** `ensure_ascii` escaping is stable but doubles the byte cost
  and hides mojibake; emitting real UTF-8 makes a corrupted alias visible.
* **Object keys sorted by Unicode code point.** Sorting must not depend on locale
  or on dictionary insertion order, or the same logical payload digests differently
  on two hosts.
* **No insignificant whitespace.** `(",", ":")` separators, so formatting can never
  contribute to a digest.
* **Array order preserved.** Order is data, not presentation.
* **Floats refused.** Binary floating point does not round-trip identically across
  every runtime and platform, so a float in a digested payload is a latent identity
  bug. Money and measurements enter certification as strings or scaled integers.
* **Duplicate keys refused at parse.** `json.loads` silently keeps the last value,
  which lets one payload have two meanings - exactly the ambiguity a sealed input
  exists to prevent.
* **Width, depth, and size bounded.** A hostile or accidental payload must fail
  fast rather than exhaust the runner.

Version note: these bytes are a contract. Changing any rule requires a NEW schema
version, never an edit here, because existing stamps are bound to digests produced
under v1.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Union

SCHEMA_VERSION = "sitesift-canonical-json-v1"

MAX_DEPTH = 64
MAX_WIDTH = 4096
MAX_SIZE_BYTES = 2 * 1024 * 1024


class CanonicalJSONError(ValueError):
    """A canonicalization or strict-parse refusal.

    Messages are sanitized: they name the offending structure or key, never a
    fixture value, recipient, secret, or raw payload body.
    """


def _check(value: Any, depth: int) -> Any:
    """Validate and normalize recursively. Returns a structure safe to serialize."""
    if depth > MAX_DEPTH:
        raise CanonicalJSONError(
            f"payload exceeds maximum nesting depth of {MAX_DEPTH}"
        )

    if value is None or isinstance(value, str):
        return value

    # bool must be tested before int: in Python, bool IS a subclass of int.
    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        raise CanonicalJSONError(
            "float values are not canonical; binary floating point does not "
            "round-trip identically across runtimes - use a string or a scaled integer"
        )

    if isinstance(value, dict):
        if len(value) > MAX_WIDTH:
            raise CanonicalJSONError(
                f"object exceeds maximum width of {MAX_WIDTH} keys"
            )
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalJSONError(
                    f"object keys must be strings; found {type(key).__name__}"
                )
            normalized[key] = _check(item, depth + 1)
        return normalized

    if isinstance(value, (list, tuple)):
        if len(value) > MAX_WIDTH:
            raise CanonicalJSONError(
                f"array exceeds maximum width of {MAX_WIDTH} items"
            )
        return [_check(item, depth + 1) for item in value]

    raise CanonicalJSONError(
        f"unsupported type for canonical JSON: {type(value).__name__}"
    )


def canonical_bytes(payload: Any) -> bytes:
    """Serialize to canonical UTF-8 bytes, or refuse."""
    checked = _check(payload, depth=1)
    text = json.dumps(
        checked,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_SIZE_BYTES:
        raise CanonicalJSONError(
            f"canonical payload size {len(encoded)} exceeds maximum of {MAX_SIZE_BYTES} bytes"
        )
    return encoded


def canonical_digest(payload: Any) -> str:
    """Lowercase SHA-256 hex digest of the canonical bytes."""
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def digest_of_bytes(raw: bytes) -> str:
    """Digest bytes that are ALREADY canonical.

    Used where the on-disk artifact is itself the canonical form - the in-image
    scenario registry and a sealed input envelope - so the digest covers exactly
    the bytes that were stored, never a re-serialization of them.
    """
    return hashlib.sha256(raw).hexdigest()


def _reject_duplicate_keys(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise CanonicalJSONError(
                f"duplicate object key {key!r}; a payload may not carry two meanings"
            )
        seen[key] = value
    return seen


def loads_strict(raw: Union[str, bytes]) -> Any:
    """Fresh bounded decode. Duplicate keys and trailing content are refusals.

    Execution always parses from stored canonical bytes through this function,
    never from a caller-owned object, so a caller cannot mutate a payload after
    it was sealed and digested.
    """
    if isinstance(raw, str):
        encoded = raw.encode("utf-8")
    elif isinstance(raw, (bytes, bytearray)):
        encoded = bytes(raw)
    else:
        raise CanonicalJSONError(
            f"strict parse requires str or bytes; found {type(raw).__name__}"
        )

    if len(encoded) > MAX_SIZE_BYTES:
        raise CanonicalJSONError(
            f"payload size {len(encoded)} exceeds maximum of {MAX_SIZE_BYTES} bytes"
        )

    try:
        text = encoded.decode("utf-8")
    except UnicodeDecodeError:
        raise CanonicalJSONError("payload is not valid UTF-8") from None

    decoder = json.JSONDecoder(object_pairs_hook=_reject_duplicate_keys)
    try:
        value, end = decoder.raw_decode(text)
    except json.JSONDecodeError as exc:
        raise CanonicalJSONError(
            f"payload is not valid JSON (line {exc.lineno}, column {exc.colno})"
        ) from None

    if text[end:].strip():
        raise CanonicalJSONError("payload carries trailing content after the JSON value")

    # Re-validate bounds and types on the decoded structure so a parsed payload is
    # held to exactly the same contract as a serialized one.
    return _check(value, depth=1)
