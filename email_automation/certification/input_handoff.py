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


# ---------------------------------------------------------------------------
# The transient human-review artifact
# ---------------------------------------------------------------------------
#
# Human naturalness review is the one thing in the instrument that cannot be
# decided by a digest: Baylor has to read what the product actually wrote. So
# this is the one artifact that holds raw captured prose, and every property it
# has exists to keep that exception from spreading.
#
# EPHEMERAL. It lives in process memory, expires within a day, and cleanup owns
# it. It is never written to the permanent ledger, never digested into evidence,
# and never logged. The ledger keeps ordered digests; this keeps the words.
#
# REDACTED, THEN BOUNDED, IN THAT ORDER. Addresses, tokens, and store paths are
# removed by shape and the result is re-checked with the real evidence
# sanitizer; only after that does the length bound apply. The reverse order is
# the classic mistake -- truncating a body to 4000 characters still exports 4000
# characters of somebody's message, and it also hides an address that happened
# to sit past the cut while making the message look checked.
#
# WHOLE AND ORDERED. It returns every message with ordinals from one, or it
# refuses. It never paginates and never returns "the first few", because a
# review that saw some of the messages is not a review of the pack.

import re
from typing import Dict, Mapping, Optional, Sequence, Tuple

from email_automation.certification import evidence as _evidence
from email_automation.certification.canonical_json import canonical_digest

# At most one day, and expiry revokes rather than extends. A review artifact
# that outlived its run would be raw fixture text with nothing left to clean it.
REVIEW_SET_TTL_SECONDS = 24 * 60 * 60

# Bounds, not suggestions. Past any of them the projection is REFUSED: a
# silently shortened list is a review of a different pack than the one that ran.
MAX_REVIEW_MESSAGES = 64
MAX_REVIEW_SUBJECT_CHARS = 300
MAX_REVIEW_BODY_CHARS = 4000

# ALLOWLIST. A kind nobody anticipated is a lane nobody wrote a rubric for.
ALLOWED_REVIEW_KINDS = ("outreach", "reply", "followup", "notification")

REDACTED_ADDRESS = "[address-removed]"
REDACTED_TOKEN = "[token-removed]"
REDACTED_PATH = "[path-removed]/"
REDACTED_OPAQUE = "[opaque-removed]"

# Deliberately the SAME shapes the durable evidence sanitizer refuses, so the
# two cannot drift into disagreeing about what an address looks like.
_REDACTIONS: Tuple[Tuple[re.Pattern, str], ...] = (
    (_evidence._ADDRESS, REDACTED_ADDRESS),
    (_evidence._BEARER, REDACTED_TOKEN),
    (_evidence._FIRESTORE_PATH, REDACTED_PATH),
    (_evidence._LONG_OPAQUE, REDACTED_OPAQUE),
)


class ReviewProjectionRefused(ValueError):
    """The review set may not be built or served. Names the rule, never the text."""


def _redact(text: str) -> str:
    """Remove every shape that must never leave the fixture, by shape."""
    redacted = text or ""
    for pattern, marker in _REDACTIONS:
        redacted = pattern.sub(marker, redacted)
    return redacted


def _safe_projection(field_name: str, text: str, limit: int) -> str:
    """Redact, verify with the REAL sanitizer, and only then bound the length."""
    redacted = _redact(text)
    try:
        _evidence.assert_safe_text(field_name, redacted)
    except _evidence.EvidenceProjectionError as exc:
        # Refuse the whole set. A partially redacted body is a disclosure that
        # looks like a review.
        raise ReviewProjectionRefused(
            f"{field_name} still carries an unsafe shape after redaction"
        ) from None
    bounded = redacted[:limit]
    # Bounding cannot introduce a shape, but re-checking costs nothing and means
    # no path reaches the reader without having passed the sanitizer last.
    try:
        _evidence.assert_safe_text(field_name, bounded)
    except _evidence.EvidenceProjectionError:
        raise ReviewProjectionRefused(
            f"{field_name} still carries an unsafe shape after bounding"
        ) from None
    return bounded


@dataclass(frozen=True)
class ReviewMessage:
    """One captured message, as Baylor reads it. Exactly five fields."""

    ordinal: int
    kind: str
    body_digest: str
    subject: str
    body: str

    def to_dict(self) -> dict:
        return {"ordinal": self.ordinal, "kind": self.kind,
                "bodyDigest": self.body_digest, "subject": self.subject,
                "body": self.body}


@dataclass(frozen=True)
class ReviewSet:
    """The whole ordered pack for one run, with an expiry it cannot outlive."""

    run_id: str
    created_at_epoch: int
    expires_at_epoch: int
    messages: Tuple[ReviewMessage, ...]
    set_digest: str

    def expired(self, now_epoch: int) -> bool:
        return now_epoch > self.expires_at_epoch

    def to_dict(self) -> dict:
        return {"runId": self.run_id,
                "reviewSetDigest": self.set_digest,
                "expiresAtEpoch": self.expires_at_epoch,
                "messages": [m.to_dict() for m in self.messages]}


def project_review_set(run_id: str, messages: Sequence[Mapping[str, str]], *,
                       now_epoch: int) -> ReviewSet:
    """Build the bounded ordered projection, or refuse. Never a partial pack."""
    rows = list(messages)
    if not rows:
        raise ReviewProjectionRefused("a review set with no messages is not a review")
    if len(rows) > MAX_REVIEW_MESSAGES:
        # Refused, not truncated: a review of the first 64 of 65 messages is a
        # verdict on a pack that never ran.
        raise ReviewProjectionRefused(
            f"a review set may hold at most {MAX_REVIEW_MESSAGES} messages")

    projected = []
    for index, row in enumerate(rows, start=1):
        kind = str(row.get("kind") or "")
        if kind not in ALLOWED_REVIEW_KINDS:
            raise ReviewProjectionRefused(
                f"message {index} has a kind outside the approved set")
        subject = _safe_projection("subject", str(row.get("subject") or ""),
                                   MAX_REVIEW_SUBJECT_CHARS)
        body = _safe_projection("body", str(row.get("body") or ""),
                                MAX_REVIEW_BODY_CHARS)
        projected.append(ReviewMessage(
            ordinal=index, kind=kind,
            body_digest=_evidence.digest_of_text(body),
            subject=subject, body=body,
        ))

    # Digested over ordinals, kinds and body digests ONLY. This value is safe to
    # bind into /review and into the permanent ledger; the prose is not.
    set_digest = canonical_digest([
        {"ordinal": m.ordinal, "kind": m.kind, "bodyDigest": m.body_digest}
        for m in projected
    ])
    return ReviewSet(
        run_id=run_id,
        created_at_epoch=int(now_epoch),
        expires_at_epoch=int(now_epoch) + REVIEW_SET_TTL_SECONDS,
        messages=tuple(projected),
        set_digest=set_digest,
    )


class TransientReviewStore:
    """Process-scoped, cleanup-owned, expiring. Deliberately not durable.

    Nothing here may ever be persisted. A raw body written to durable storage
    would outlive the fixture it belongs to and the cleanup that erases it,
    which is precisely the residue certification exists to prove absent.
    """

    def __init__(self) -> None:
        self._sets: Dict[str, ReviewSet] = {}

    def deposit(self, run_id: str, messages: Sequence[Mapping[str, str]], *,
                now_epoch: int) -> ReviewSet:
        if run_id in self._sets:
            # One deposit per run. A second would let a later capture replace
            # the text a reviewer already looked at.
            raise ReviewProjectionRefused(
                f"run {run_id} already has a review set")
        review_set = project_review_set(run_id, messages, now_epoch=now_epoch)
        self._sets[run_id] = review_set
        return review_set

    def get(self, run_id: str, *, now_epoch: int) -> Optional[ReviewSet]:
        """The pack, or None. An expired pack is discarded rather than served."""
        review_set = self._sets.get(run_id)
        if review_set is None:
            return None
        if review_set.expired(now_epoch):
            del self._sets[run_id]
            return None
        return review_set

    def discard(self, run_id: str) -> bool:
        return self._sets.pop(run_id, None) is not None

    def export_run_ids(self) -> Tuple[str, ...]:
        """Run ids only. There is deliberately no way to export the text."""
        return tuple(sorted(self._sets))
