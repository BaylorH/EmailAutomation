"""Sealed one-use canonical-input envelope.

A sealed input is BYTES, not an object graph. The distinction is the whole point:
if execution read a caller-owned Python object, that object could be mutated after
its digest was computed, and the evidence would then describe an input that never
ran. Sealing serializes once to canonical bytes, digests those exact bytes, and
re-decodes freshly for every read.

This envelope is stored in the certification database and is never accepted in a
backend request body.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from email_automation.certification.canonical_json import (
    canonical_bytes,
    digest_of_bytes,
    loads_strict,
)


@dataclass(frozen=True)
class SealedInput:
    """Immutable canonical bytes plus their digest."""

    canonical_bytes: bytes
    digest: str

    @classmethod
    def seal(cls, payload: Any) -> "SealedInput":
        """Serialize once, digest those exact bytes, and keep nothing else.

        Raises CanonicalJSONError if the payload is not canonicalizable - a float,
        an unsupported type, or a payload past the width/depth/size bounds.
        """
        raw = canonical_bytes(payload)
        return cls(canonical_bytes=raw, digest=digest_of_bytes(raw))

    def execution_input(self) -> Any:
        """A FRESH bounded decode of the sealed bytes.

        Returns a new structure on every call, so a caller that mutates what it
        received cannot affect the next read or any other consumer.
        """
        return loads_strict(self.canonical_bytes)
