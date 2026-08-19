"""Evidence projection: what a certification run is allowed to say about itself.

Task 8 of the production automation certification plan.

Evidence is durable, exported, and read by people who are not entitled to the
fixture's contents. So the rule is not "try to remember to redact" - Task 7G
showed how that fails, where a static scan flagged 75 candidate log sites and
the five real leaks were found only by driving the lane and reading the output.

The rule here is stronger and structural: an evidence record is built ONLY from
an allow-list of safe field kinds. A value that is not a stable code, a count, a
phase, a bounded generic summary, or a digest cannot be placed in evidence at
all - not because a filter removed it, but because there is no field that
accepts it. ``project_evidence`` refuses rather than sanitizes, because a
sanitizer that silently drops a field teaches the caller nothing, while a
refusal names the field that tried to escape.

Unsafe by construction, and therefore rejected on sight: e-mail addresses,
message bodies, subjects, contact names, file identifiers, provider tokens,
Firestore document paths, raw exception text, and base64/pixel payloads.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from .canonical_json import canonical_bytes, digest_of_bytes


class EvidenceProjectionError(ValueError):
    """A value that is not safe for durable evidence was offered to it."""


# Shapes that are unsafe wherever they appear. Deliberately conservative: a
# false positive costs a caller one explicit digest call, while a false negative
# is a permanent disclosure in an exported record.
_ADDRESS = re.compile(r"[A-Z0-9._%+\-']+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
_BEARER = re.compile(r"\b(?:bearer\s+\S+|ey[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{8,})", re.IGNORECASE)
_FIRESTORE_PATH = re.compile(r"\busers/[^/\s]+/", re.IGNORECASE)
_LONG_OPAQUE = re.compile(r"[A-Za-z0-9+/]{64,}={0,2}")

# A phase names WHERE a run stopped, never what it was carrying.
ALLOWED_PHASES = (
    "prepare",
    "claim",
    "seed",
    "execute",
    "readback",
    "replay",
    "cleanup",
    "terminalize",
)

# A failure code is a stable identifier a reader can look up, not a description.
_CODE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")

MAX_SUMMARY_LENGTH = 200


def _reject(field_name: str, reason: str) -> None:
    raise EvidenceProjectionError(f"{field_name}: {reason}")


def assert_safe_text(field_name: str, value: str) -> str:
    """Refuse any text carrying a shape that must never reach evidence."""
    text = value or ""
    for pattern, reason in (
        (_ADDRESS, "contains an e-mail address"),
        (_BEARER, "contains what looks like a provider token"),
        (_FIRESTORE_PATH, "contains a Firestore document path"),
        (_LONG_OPAQUE, "contains a long opaque blob (base64/pixels?)"),
    ):
        if pattern.search(text):
            _reject(field_name, reason)
    return text


def safe_code(field_name: str, value: str) -> str:
    """A stable lookup identifier. Free text is refused, not truncated."""
    text = (value or "").strip()
    if not _CODE.match(text):
        _reject(
            field_name,
            "must be a stable snake_case code (a description is not a code)",
        )
    return text


def safe_phase(field_name: str, value: str) -> str:
    text = (value or "").strip()
    if text not in ALLOWED_PHASES:
        _reject(field_name, f"must be one of {', '.join(ALLOWED_PHASES)}")
    return text


def safe_count(field_name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _reject(field_name, "must be a plain integer count")
    if value < 0:
        _reject(field_name, "must not be negative")
    return value


def safe_summary(field_name: str, value: str) -> str:
    """A bounded, generic sentence. Bounding alone is not safety.

    Truncating a body to 200 characters still exports 200 characters of a
    customer's message, so the shape checks run first and the bound second.
    """
    text = (value or "").strip()
    if not text:
        _reject(field_name, "was supplied but is blank")
    if len(text) > MAX_SUMMARY_LENGTH:
        _reject(field_name, f"exceeds {MAX_SUMMARY_LENGTH} characters")
    return assert_safe_text(field_name, text)


def digest_of(value: Any) -> str:
    """The ONLY sanctioned way to reference a fixture value from evidence.

    Canonical bytes, so the same logical value digests identically across runs
    and across the Python/Node boundary the plan requires to agree.
    """
    return digest_of_bytes(canonical_bytes(value))


def digest_of_text(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EvidenceRecord:
    """An immutable, exportable statement about one certification run."""

    run_id: str
    scenario_id: str
    revision: str
    outcome: str
    phase: str
    counts: Mapping[str, int] = field(default_factory=dict)
    digests: Mapping[str, str] = field(default_factory=dict)
    failure_code: Optional[str] = None
    summary: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "runId": self.run_id,
            "scenarioId": self.scenario_id,
            "revision": self.revision,
            "outcome": self.outcome,
            "phase": self.phase,
            "counts": dict(self.counts),
            "digests": dict(self.digests),
        }
        if self.failure_code:
            record["failureCode"] = self.failure_code
        if self.summary:
            record["summary"] = self.summary
        return record

    def canonical_digest(self) -> str:
        """Identity of the evidence itself, so a mutated record is detectable."""
        return digest_of_bytes(canonical_bytes(self.to_dict()))


ALLOWED_OUTCOMES = ("pass", "fail", "instrument_blocked", "aborted")


def project_evidence(
    *,
    run_id: str,
    scenario_id: str,
    revision: str,
    outcome: str,
    phase: str,
    counts: Optional[Mapping[str, Any]] = None,
    digests: Optional[Mapping[str, str]] = None,
    failure_code: Optional[str] = None,
    summary: Optional[str] = None,
) -> EvidenceRecord:
    """Build an evidence record, refusing anything unsafe.

    Keyword-only and explicitly enumerated: there is deliberately no ``**extra``
    passthrough, because the moment evidence accepts arbitrary keys the
    allow-list stops being an allow-list.
    """
    if outcome not in ALLOWED_OUTCOMES:
        _reject("outcome", f"must be one of {', '.join(ALLOWED_OUTCOMES)}")

    identity = {
        "run_id": assert_safe_text("run_id", run_id),
        "scenario_id": assert_safe_text("scenario_id", scenario_id),
        "revision": assert_safe_text("revision", revision),
    }
    for name, value in identity.items():
        if not value.strip():
            _reject(name, "is required")

    safe_counts = {
        str(key): safe_count(f"counts.{key}", value)
        for key, value in dict(counts or {}).items()
    }
    safe_digests: Dict[str, str] = {}
    for key, value in dict(digests or {}).items():
        text = str(value or "")
        if not re.fullmatch(r"[0-9a-f]{64}", text):
            _reject(f"digests.{key}", "must be a lowercase sha256 hex digest")
        safe_digests[str(key)] = text

    return EvidenceRecord(
        run_id=identity["run_id"],
        scenario_id=identity["scenario_id"],
        revision=identity["revision"],
        outcome=outcome,
        phase=safe_phase("phase", phase),
        counts=safe_counts,
        digests=safe_digests,
        # ``None`` means "no code"; a BLANK string means a caller tried to supply
        # one and produced nothing, which the plan treats as invalid rather than
        # absent. Collapsing the two would let a blank field reach a PASS.
        failure_code=(
            safe_code("failure_code", failure_code) if failure_code is not None else None
        ),
        summary=safe_summary("summary", summary) if summary is not None else None,
    )


def instrument_blocked(
    *,
    run_id: str,
    scenario_id: str,
    revision: str,
    phase: str,
    reason_code: str = "user_runtime_launch_required",
) -> EvidenceRecord:
    """The agent-safe outcome: the product did not misbehave, the lane refused.

    Distinct from ``fail`` on purpose. Collapsing the two would make a scenario
    that was never exercised indistinguishable from one that was exercised and
    broke - and the second is the only one that should block a release.
    """
    return project_evidence(
        run_id=run_id,
        scenario_id=scenario_id,
        revision=revision,
        outcome="instrument_blocked",
        phase=phase,
        failure_code=reason_code,
    )
