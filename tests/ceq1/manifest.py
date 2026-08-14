"""Closed public/sealed fixture contracts for the CE-Q1 qualification deck."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Mapping

from tests.ceq1.contracts import canonical_json, sha256_json
from tests.ceq1.privacy import (
    GenerationProvenance,
    scan_bytes,
    scan_json,
    validate_generation_provenance,
    validate_generation_provenance_bytes,
)


PRODUCTION_ANCESTOR = "6caa8ec14cc525299cfb8ed13bdd219f35c4322b"
IMPLEMENTATION_BASE = "b400ee5ad55ac75203da6a53730c4a134cad79e5"
MAX_ARTIFACT_BYTES = 1024 * 1024
DESCRIPTOR_SEPARATION_NONCLAIM = (
    "Task 3 proves field, byte, and capability separation. The semantic scenario and "
    "variant names still hint at intent; Task 7 must prove the harness does not branch "
    "on those IDs before passing bundle content into product seams."
)
OWNER_COMPLETENESS_NONCLAIM = (
    "Task 3 verifies raw owner bytes against a trusted external allowlist; Task 7 owns "
    "the complete runtime-binding owner inventory."
)
VALIDATED_SCHEDULE_CAPABILITY_NONCLAIM = (
    "ValidatedExecutionSchedule closes ordinary Python construction and "
    "dataclasses.replace with a content-bound aggregate digest. Forgery through "
    "object.__new__ plus object.__setattr__ is outside this Python capability claim."
)

MANDATORY_SCENARIO_IDS = frozenset(
    {
        "CEQ-LONG-01",
        "CEQ-MEM-01",
        "CEQ-TERM-01",
        "CEQ-TERM-02",
        "CEQ-SUITE-01",
        "CEQ-PDF-01",
        "CEQ-OPEX-01",
        "CEQ-OPEX-02",
        "CEQ-ALT-01",
        "CEQ-IN-09",
        "CEQ-IN-10",
        "CEQ-WRONG-01",
        "CEQ-OOO-01",
        "CEQ-AUDIENCE-01",
        "VOICE-LAUNCH",
        "VOICE-MISSING",
        "VOICE-CORRECTION-CLOSE",
        "VOICE-FOLLOWUP",
        "VOICE-CONTINUATION",
    }
)

_SCENARIO_ORDER = (
    "CEQ-LONG-01",
    "CEQ-MEM-01",
    "CEQ-TERM-01",
    "CEQ-TERM-02",
    "CEQ-SUITE-01",
    "CEQ-PDF-01",
    "CEQ-OPEX-01",
    "CEQ-OPEX-02",
    "CEQ-ALT-01",
    "CEQ-IN-09",
    "CEQ-IN-10",
    "CEQ-WRONG-01",
    "CEQ-OOO-01",
    "CEQ-AUDIENCE-01",
    "VOICE-LAUNCH",
    "VOICE-MISSING",
    "VOICE-CORRECTION-CLOSE",
    "VOICE-FOLLOWUP",
    "VOICE-CONTINUATION",
)

SCENARIO_PRIMARY_FAMILIES = {
    "CEQ-LONG-01": "chronology",
    "CEQ-MEM-01": "EXT-01",
    "CEQ-TERM-01": "EXT-05",
    "CEQ-TERM-02": "EXT-02",
    "CEQ-SUITE-01": "EXT-03",
    "CEQ-PDF-01": "PDF layout",
    "CEQ-OPEX-01": "EXT-04",
    "CEQ-OPEX-02": "EXT-04",
    "CEQ-ALT-01": "EXT-06",
    "CEQ-IN-09": "IN-09",
    "CEQ-IN-10": "IN-10",
    "CEQ-WRONG-01": "EXT-02",
    "CEQ-OOO-01": "autoresponse",
    "CEQ-AUDIENCE-01": "audience",
    "VOICE-LAUNCH": "voice",
    "VOICE-MISSING": "voice",
    "VOICE-CORRECTION-CLOSE": "voice",
    "VOICE-FOLLOWUP": "voice",
    "VOICE-CONTINUATION": "voice",
}


@dataclass(frozen=True, slots=True)
class ReviewedVariant:
    ordinal: int
    variantFamily: str
    variantId: str
    scenarioId: str
    layers: tuple[str, ...]
    sabotageId: str
    sabotageReason: str
    responseClass: str
    voiceEligibility: bool
    promotionClass: str
    expectedVerdict: str
    nonClaims: tuple[str, ...]
    baseline: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "variantFamily": self.variantFamily,
            "variantId": self.variantId,
            "scenarioId": self.scenarioId,
            "layers": list(self.layers),
            "sabotageId": self.sabotageId,
            "sabotageReason": self.sabotageReason,
            "responseClass": self.responseClass,
            "voiceEligibility": self.voiceEligibility,
            "promotionClass": self.promotionClass,
            "expectedVerdict": self.expectedVerdict,
            "nonClaims": list(self.nonClaims),
            "baseline": self.baseline,
        }


def _reviewed(
    ordinal: int,
    family: str,
    variant: str,
    scenario: str,
    layers: tuple[str, ...],
    sabotage: str,
    reason: str,
    response: str,
    promotion: str,
    baseline: str,
    nonclaims: tuple[str, ...] = (),
) -> ReviewedVariant:
    return ReviewedVariant(
        ordinal=ordinal,
        variantFamily=family,
        variantId=variant,
        scenarioId=scenario,
        layers=layers,
        sabotageId=sabotage,
        sabotageReason=reason,
        responseClass=response,
        voiceEligibility=False,
        promotionClass=promotion,
        expectedVerdict="UNVERIFIED" if promotion == "diagnostic" else "PASS_OFFLINE",
        nonClaims=nonclaims,
        baseline=baseline,
    )


_REVIEWED_ROW_DATA = (
    ("EXT-01", "known-filled", "CEQ-MEM-01", ("L1", "L2", "L3"), "SAB-EXT01-01", "KNOWN_FACT_REASKED", "missing_field_reply", "required", "FAIL", ()),
    ("EXT-01", "explicit-decline", "CEQ-MEM-01", ("L1", "L2", "L3"), "SAB-EXT01-02", "DECLINED_FACT_REASKED", "missing_field_reply", "required", "FAIL", ()),
    ("EXT-01", "correction-after-window", "CEQ-LONG-01", ("L1", "L2", "L3"), "SAB-EXT01-03", "STALE_CORRECTION_WON", "correction_close_reply", "required", "FAIL", ()),
    ("EXT-01", "acknowledgement-not-question", "CEQ-MEM-01", ("L1", "L2", "L3"), "SAB-EXT01-04", "ACK_MISCLASSIFIED_AS_QUESTION", "missing_field_reply", "required", "FAIL", ()),
    ("EXT-02", "fresh-target-terminal", "CEQ-TERM-01", ("L1", "L2", "L3"), "SAB-EXT02-01", "CITED_TERMINAL_NOT_APPLIED", "terminal_reply", "required", "FAIL", ()),
    ("EXT-02", "stale-quoted-terminal", "CEQ-TERM-02", ("L1", "L2", "L3"), "SAB-EXT02-02", "QUOTED_ONLY_TERMINAL_ACCEPTED", "review_no_reply", "required", "VERIFY", ()),
    ("EXT-02", "wrong-property-terminal", "CEQ-WRONG-01", ("L1", "L2", "L3"), "SAB-EXT02-03", "CROSS_ENTITY_TERMINAL_ACCEPTED", "review_no_reply", "required", "VERIFY", ()),
    ("EXT-02", "addressless-terminal", "CEQ-TERM-02", ("L1", "L2", "L3"), "SAB-EXT02-04", "UNCITED_TERMINAL_ACCEPTED", "review_no_reply", "required", "VERIFY", ()),
    ("EXT-02", "ambiguous-terminal", "CEQ-TERM-02", ("L1", "L2", "L3"), "SAB-EXT02-05", "AMBIGUOUS_TERMINAL_ACCEPTED", "review_no_reply", "required", "VERIFY", ()),
    ("EXT-03", "same-address-two-suites", "CEQ-SUITE-01", ("L1", "L2", "L3"), "SAB-EXT03-01", "CROSS_SUITE_FACT_ACCEPTED", "review_no_reply", "required", "FAIL", ()),
    ("EXT-03", "mixed-property-pdf", "CEQ-PDF-01", ("L1", "L2", "L3"), "SAB-EXT03-02", "CROSS_PROPERTY_PDF_FACT_ACCEPTED", "review_no_reply", "required", "VERIFY", ()),
    ("EXT-03", "mixed-suite-pdf", "CEQ-SUITE-01", ("L1", "L2", "L3"), "SAB-EXT03-03", "CROSS_SUITE_PDF_FACT_ACCEPTED", "review_no_reply", "required", "FAIL", ()),
    ("EXT-03", "exact-target-attachment", "CEQ-PDF-01", ("L1", "L2", "L3"), "SAB-EXT03-04", "SUPPORTED_TARGET_FACT_DROPPED", "missing_field_reply", "required", "FAIL", ()),
    ("EXT-04", "rent14-opex4", "CEQ-OPEX-01", ("L1", "L2", "L3"), "SAB-EXT04-01", "RENT_OPEX_CONFLATED", "missing_field_reply", "required", "FAIL", ()),
    ("EXT-04", "monthly-annual", "CEQ-OPEX-01", ("L1", "L2", "L3"), "SAB-EXT04-02", "BASIS_CONVERSION_WRONG", "missing_field_reply", "required", "FAIL", ()),
    ("EXT-04", "latest-correction", "CEQ-OPEX-01", ("L1", "L2", "L3"), "SAB-EXT04-03", "STALE_NUMERIC_VALUE_WON", "missing_field_reply", "required", "FAIL", ()),
    ("EXT-04", "numeric-range", "CEQ-OPEX-01", ("L1", "L2", "L3"), "SAB-EXT04-04", "NUMERIC_RANGE_TRANSFORM_WRONG", "missing_field_reply", "required", "FAIL", ()),
    ("EXT-04", "digit-decoy", "CEQ-OPEX-01", ("L1", "L2", "L3"), "SAB-EXT04-05", "DIGIT_DECOY_ACCEPTED", "missing_field_reply", "required", "FAIL", ()),
    ("EXT-04", "unsupported-opex", "CEQ-OPEX-02", ("L1", "L2", "L3"), "SAB-EXT04-06", "INVENTED_OPEX_ACCEPTED", "missing_field_reply", "required", "FAIL", ()),
    ("EXT-05", "ordered-success", "CEQ-TERM-01", ("L2", "L3"), "SAB-EXT05-01", "TERMINAL_OPERATION_ORDER_WRONG", "terminal_reply", "required", "FAIL", ()),
    ("EXT-05", "move-failure", "CEQ-TERM-01", ("L2", "L3"), "SAB-EXT05-02", "MOVE_FAILURE_HIDDEN", "terminal_reply", "required", "FAIL", ()),
    ("EXT-05", "comment-failure", "CEQ-TERM-01", ("L2", "L3"), "SAB-EXT05-03", "COMMENT_FAILURE_HIDDEN", "terminal_reply", "required", "FAIL", ()),
    ("EXT-05", "highlight-failure", "CEQ-TERM-01", ("L2", "L3"), "SAB-EXT05-04", "HIGHLIGHT_FAILURE_HIDDEN", "terminal_reply", "required", "FAIL", ()),
    ("EXT-05", "audit-write-failure", "CEQ-TERM-01", ("L2", "L3"), "SAB-EXT05-05", "AUDIT_FAILURE_HIDDEN", "terminal_reply", "required", "FAIL", ()),
    ("EXT-05", "terminal-state-failure", "CEQ-TERM-01", ("L2", "L3"), "SAB-EXT05-06", "FALSE_TERMINAL_COMPLETION", "terminal_reply", "required", "FAIL", ()),
    ("EXT-05", "column-beyond-z", "CEQ-TERM-01", ("L2", "L3"), "SAB-EXT05-07", "COMMENT_COLUMN_ADDRESS_TRUNCATED", "terminal_reply", "required", "FAIL", ()),
    ("EXT-05", "retry-after-partial-attempt", "CEQ-TERM-01", ("L2", "L3"), "SAB-EXT05-08", "PARTIAL_RETRY_DUPLICATED_EFFECT", "terminal_reply", "required", "FAIL", ()),
    ("EXT-06", "viable-alternate", "CEQ-ALT-01", ("L1", "L2", "L3"), "SAB-EXT06-01", "ALTERNATE_ACTION_MISSING", "alternate_reply", "required", "FAIL", ()),
    ("EXT-06", "alternate-unavailable", "CEQ-ALT-01", ("L1", "L2", "L3"), "SAB-EXT06-02", "UNAVAILABLE_ALTERNATE_ACTIONED", "terminal_reply", "required", "FAIL", ()),
    ("EXT-06", "two-alternates", "CEQ-ALT-01", ("L1", "L2", "L3"), "SAB-EXT06-03", "ALTERNATE_CARDINALITY_WRONG", "alternate_reply", "required", "FAIL", ()),
    ("EXT-06", "same-event-replay", "CEQ-ALT-01", ("L1", "L2", "L3"), "SAB-EXT06-04", "DUPLICATE_ALTERNATE_ACTION", "alternate_reply", "required", "FAIL", ()),
    ("IN-09", "direct-broker-question", "CEQ-IN-09", ("L1", "L2", "L3"), "SAB-IN09-01", "UNSAFE_BROKER_QUESTION_ANSWERED", "review_no_reply", "required", "VERIFY", ()),
    ("IN-09", "confidential-identity-question", "CEQ-IN-09", ("L1", "L2", "L3"), "SAB-IN09-02", "CONFIDENTIAL_IDENTITY_DISCLOSED", "review_no_reply", "required", "VERIFY", ()),
    ("IN-09", "question-plus-partial-specs", "CEQ-IN-09", ("L1", "L2", "L3"), "SAB-IN09-03", "SAFE_FACTS_DROPPED_ON_REVIEW", "review_no_reply", "required", "FAIL", ()),
    ("IN-10", "unrelated-mail", "CEQ-IN-10", ("L2", "L3"), "SAB-IN10-01", "UNTRACKED_MAIL_MUTATED_STATE", "no_reply", "required", "VERIFY", ()),
    ("IN-10", "quoted-cre-nearmiss", "CEQ-IN-10", ("L2", "L3"), "SAB-IN10-02", "QUOTED_CRE_NEARMISS_PROCESSED", "no_reply", "required", "VERIFY", ()),
    ("IN-10", "tracked-reply-nearmiss", "CEQ-IN-10", ("L2", "L3"), "SAB-IN10-03", "TRACKED_NEARMISS_PROCESSED", "no_reply", "required", "VERIFY", ()),
    ("chronology", "thirteen-message-window", "CEQ-LONG-01", ("L1", "L2", "L3"), "SAB-CHR-01", "HISTORY_WINDOW_BYPASSED", "correction_close_reply", "required", "FAIL", ()),
    ("chronology", "delayed-inbound-order", "CEQ-LONG-01", ("L1", "L2", "L3"), "SAB-CHR-02", "DELAYED_INBOUND_ORDER_WRONG", "missing_field_reply", "required", "FAIL", ()),
    ("chronology", "pause-hold", "CEQ-LONG-01", ("L2", "L3"), "SAB-CHR-03", "PAUSED_THREAD_CONTINUED", "no_reply", "required", "VERIFY", ()),
    ("chronology", "monitored-resume", "CEQ-LONG-01", ("L2", "L3"), "SAB-CHR-04", "UNSUPPORTED_RESUME", "monitored_continuation_reply", "required", "FAIL", ()),
    ("chronology", "settled-replay", "CEQ-LONG-01", ("L2", "L3"), "SAB-CHR-05", "SETTLED_REPLAY_STATE_DELTA", "no_reply", "required", "FAIL", ()),
    ("autoresponse", "dated-ooo", "CEQ-OOO-01", ("L2", "L3"), "SAB-AUTO-01", "OOO_EXTRACTED_OR_REPLIED", "no_reply", "required", "VERIFY", ()),
    ("autoresponse", "generic-auto-ack", "CEQ-OOO-01", ("L2", "L3"), "SAB-AUTO-02", "AUTOACK_EXTRACTED_OR_REPLIED", "no_reply", "required", "VERIFY", ()),
    ("autoresponse", "quoted-cre-ooo", "CEQ-OOO-01", ("L2", "L3"), "SAB-AUTO-03", "QUOTED_CRE_OOO_PROCESSED", "no_reply", "required", "VERIFY", ()),
    ("audience", "copied-party-reply-all", "CEQ-AUDIENCE-01", ("L2", "L3"), "SAB-AUD-01", "CC_DROPPED_OR_MISROUTED", "reply_all_draft", "required", "UNVERIFIED", ()),
    ("audience", "display-name-ambiguity", "CEQ-AUDIENCE-01", ("L2", "L3"), "SAB-AUD-02", "AMBIGUOUS_AUDIENCE_GUESSED", "reply_all_draft", "required", "UNVERIFIED", ()),
    ("audience", "wrong-tenant-signature-decoy", "CEQ-AUDIENCE-01", ("L2", "L3"), "SAB-AUD-03", "SIGNATURE_IDENTITY_DRIFT", "reply_all_draft", "required", "UNVERIFIED", ()),
    ("PDF layout", "native-text-three-page", "CEQ-PDF-01", ("L1", "L2", "L3"), "SAB-PDF-01", "NATIVE_PDF_PAGE_BINDING_WRONG", "review_no_reply", "required", "VERIFY", ()),
    ("PDF layout", "image-only-explicitly-unverified", "CEQ-PDF-01", ("L1",), "SAB-PDF-02", "OCR_CAPABILITY_OVERCLAIMED", "no_reply", "diagnostic", "UNVERIFIED", ("UNVERIFIED_NO_EFFECT_FREE_OCR",)),
    ("voice", "launch", "VOICE-LAUNCH", ("L1",), "SAB-VOICE-01", "VOICE_DRAFT_ADMITTED_WITHOUT_SHARED_FINALIZER", "launch_draft", "diagnostic", "UNVERIFIED", ("UNVERIFIED_NO_SHARED_FINALIZER",)),
    ("voice", "missing-field", "VOICE-MISSING", ("L1",), "SAB-VOICE-02", "VOICE_DRAFT_ADMITTED_WITHOUT_SHARED_FINALIZER", "missing_field_draft", "diagnostic", "UNVERIFIED", ("UNVERIFIED_NO_SHARED_FINALIZER",)),
    ("voice", "correction-close", "VOICE-CORRECTION-CLOSE", ("L1",), "SAB-VOICE-03", "VOICE_DRAFT_ADMITTED_WITHOUT_SHARED_FINALIZER", "correction_close_draft", "diagnostic", "UNVERIFIED", ("UNVERIFIED_NO_SHARED_FINALIZER",)),
    ("voice", "followup", "VOICE-FOLLOWUP", ("L1",), "SAB-VOICE-04", "VOICE_DRAFT_ADMITTED_WITHOUT_SHARED_FINALIZER", "followup_draft", "diagnostic", "UNVERIFIED", ("UNVERIFIED_NO_SHARED_FINALIZER",)),
    ("voice", "continuation", "VOICE-CONTINUATION", ("L1",), "SAB-VOICE-05", "VOICE_DRAFT_ADMITTED_WITHOUT_SHARED_FINALIZER", "continuation_draft", "diagnostic", "UNVERIFIED", ("UNVERIFIED_NO_SHARED_FINALIZER",)),
)

REVIEWED_VARIANT_MATRIX = tuple(
    _reviewed(ordinal, *row) for ordinal, row in enumerate(_REVIEWED_ROW_DATA)
)
MANDATORY_VARIANT_IDS = frozenset(row.variantId for row in REVIEWED_VARIANT_MATRIX)
_REVIEWED_BY_VARIANT = {row.variantId: row for row in REVIEWED_VARIANT_MATRIX}

_PUBLIC_TOP_KEYS = frozenset(
    {"schemaVersion", "productionAncestor", "implementationBase", "scenarios"}
)
_PUBLIC_SCENARIO_KEYS = frozenset(
    {
        "id",
        "family",
        "purpose",
        "provenanceLabel",
        "inputBundle",
        "inputHash",
        "responseBundle",
        "responseHash",
        "ownerModuleHashes",
    }
)
_SCHEDULE_TOP_KEYS = frozenset(
    {"schemaVersion", "productionAncestor", "implementationBase", "entries"}
)
_SCHEDULE_ENTRY_KEYS = frozenset(
    {"ordinal", "scenarioId", "variantId", "layers", "inputHash", "responseHash"}
)
_COVERAGE_TOP_KEYS = frozenset(
    {"schemaVersion", "productionAncestor", "implementationBase", "records"}
)
_COVERAGE_RECORD_KEYS = frozenset(
    {
        "variantId",
        "scenarioId",
        "layers",
        "responseClass",
        "voiceEligibility",
        "oracleHash",
        "sabotageId",
        "promotionClass",
        "expectedVerdict",
        "nonClaims",
    }
)
_PUBLIC_FORBIDDEN_KEYS = frozenset(
    {
        "oraclehash",
        "expectedverdict",
        "expectedstate",
        "expectedoutcome",
        "sabotageid",
        "sabotagereason",
        "responseclass",
        "voiceeligibility",
        "promotionclass",
        "nonclaims",
        "baseline",
        "variantfamily",
    }
)
_LOWER_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ManifestValidationError(ValueError):
    """A path- and content-redacted contract failure."""


class JsonValidationError(ValueError):
    """A redacted strict-JSON failure."""


def _fail(rule: str, artifact: str) -> None:
    raise ManifestValidationError(rule, artifact)


def _exact_dict(value: object, keys: frozenset[str], artifact: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        _fail("CEQ_MANIFEST_SCHEMA", artifact)
    return value


def _strict_string(value: object, artifact: str) -> str:
    if type(value) is not str or not value:
        _fail("CEQ_MANIFEST_STRING", artifact)
    return value


def _digest(value: object, artifact: str) -> str:
    if type(value) is not str or _LOWER_DIGEST.fullmatch(value) is None:
        _fail("CEQ_MANIFEST_DIGEST", artifact)
    return value


def _validate_refs(value: Mapping[str, object], artifact: str) -> None:
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        _fail("CEQ_MANIFEST_SCHEMA_VERSION", artifact)
    if value["productionAncestor"] != PRODUCTION_ANCESTOR:
        _fail("CEQ_MANIFEST_PRODUCTION_ANCESTOR", artifact)
    if value["implementationBase"] != IMPLEMENTATION_BASE:
        _fail("CEQ_MANIFEST_IMPLEMENTATION_BASE", artifact)


def _reject_public_leaks(value: object, artifact: str) -> None:
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str:
                _fail("CEQ_PUBLIC_NONSTRING_KEY", artifact)
            normalized = re.sub(r"[^a-z]", "", key.lower())
            if normalized in _PUBLIC_FORBIDDEN_KEYS:
                _fail("CEQ_PUBLIC_SEALED_FIELD", artifact)
            _reject_public_leaks(child, artifact)
    elif type(value) is list:
        for child in value:
            _reject_public_leaks(child, artifact)


def load_json_bytes(data: bytes, *, artifact_id: str) -> object:
    invalid = type(data) is not bytes or data.startswith(b"\xef\xbb\xbf")
    parsed: object = None
    if not invalid:
        try:
            text = data.decode("utf-8", "strict")

            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError("duplicate")
                    result[key] = value
                return result

            parsed = json.loads(
                text,
                object_pairs_hook=pairs,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            invalid = True
    if invalid or type(parsed) is not dict:
        raise JsonValidationError("CEQ_JSON_INVALID", artifact_id)
    return parsed


def _canonical_relative(value: object, artifact: str) -> str:
    if type(value) is not str or not value:
        _fail("CEQ_PATH_CANONICAL", artifact)
    if (
        "\x00" in value
        or "\\" in value
        or "%" in value
        or ":" in value
        or value.startswith("/")
        or "//" in value
    ):
        _fail("CEQ_PATH_CANONICAL", artifact)
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        _fail("CEQ_PATH_CANONICAL", artifact)
    return value


@dataclass(frozen=True, slots=True)
class _BoundFile:
    data: bytes
    sha256: str
    identity: tuple[int, int]


def _stat_signature(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_bound_file(root: Path, relative: object, artifact: str) -> _BoundFile:
    relative_text = _canonical_relative(relative, artifact)
    parts = PurePosixPath(relative_text).parts
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    directory_flags = flags | os.O_DIRECTORY
    opened: list[int] = []
    directory_memberships: list[tuple[int, str, tuple[int, int, int]]] = []
    failure = False
    try:
        root_fd = os.open(os.fspath(root), directory_flags)
        opened.append(root_fd)
        root_before = os.fstat(root_fd)
        if not stat.S_ISDIR(root_before.st_mode):
            failure = True
        directory_fd = root_fd
        for component in parts[:-1]:
            observed = os.stat(component, dir_fd=directory_fd, follow_symlinks=False)
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            opened.append(next_fd)
            opened_directory = os.fstat(next_fd)
            observed_identity = (observed.st_dev, observed.st_ino, observed.st_mode)
            if (
                not stat.S_ISDIR(observed.st_mode)
                or (opened_directory.st_dev, opened_directory.st_ino, opened_directory.st_mode)
                != observed_identity
            ):
                failure = True
            directory_memberships.append((directory_fd, component, observed_identity))
            directory_fd = next_fd
        observed_file = os.stat(parts[-1], dir_fd=directory_fd, follow_symlinks=False)
        file_fd = os.open(parts[-1], flags, dir_fd=directory_fd)
        opened.append(file_fd)
        before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > MAX_ARTIFACT_BYTES
            or _stat_signature(before) != _stat_signature(observed_file)
        ):
            failure = True
            data = b""
            after = before
        else:
            chunks = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(file_fd, min(remaining, 65536))
                if not chunk:
                    failure = True
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(file_fd, 1) != b"":
                failure = True
            data = b"".join(chunks)
            after = os.fstat(file_fd)
            member_after = os.stat(parts[-1], dir_fd=directory_fd, follow_symlinks=False)
            if (
                remaining
                or len(data) != before.st_size
                or _stat_signature(before) != _stat_signature(after)
                or _stat_signature(before) != _stat_signature(member_after)
            ):
                failure = True
        for parent_fd, component, expected_identity in reversed(directory_memberships):
            member = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            if (member.st_dev, member.st_ino, member.st_mode) != expected_identity:
                failure = True
        root_after = os.fstat(root_fd)
        if (root_after.st_dev, root_after.st_ino, root_after.st_mode) != (
            root_before.st_dev,
            root_before.st_ino,
            root_before.st_mode,
        ):
            failure = True
    except OSError:
        failure = True
        data = b""
        before = None
    finally:
        for descriptor in reversed(opened):
            try:
                os.close(descriptor)
            except OSError:
                pass
    if failure or before is None:
        _fail("CEQ_BOUND_FILE", artifact)
    return _BoundFile(
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        identity=(before.st_dev, before.st_ino),
    )


def _read_control_file(path: Path, artifact: str) -> _BoundFile:
    if not isinstance(path, Path) or not path.is_absolute():
        _fail("CEQ_CONTROL_PATH", artifact)
    return _read_bound_file(path.parent, path.name, artifact)


@dataclass(frozen=True, slots=True)
class ValidatedScenario:
    id: str
    family: str
    purpose: str
    provenanceLabel: str
    inputBundle: str
    inputHash: str
    responseBundle: str
    responseHash: str
    ownerModuleHashes: tuple[tuple[str, str], ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "family": self.family,
            "purpose": self.purpose,
            "provenanceLabel": self.provenanceLabel,
            "inputBundle": self.inputBundle,
            "inputHash": self.inputHash,
            "responseBundle": self.responseBundle,
            "responseHash": self.responseHash,
            "ownerModuleHashes": dict(self.ownerModuleHashes),
        }


@dataclass(frozen=True, slots=True)
class ValidatedManifest:
    schemaVersion: int
    productionAncestor: str
    implementationBase: str
    scenarios: tuple[ValidatedScenario, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schemaVersion,
            "productionAncestor": self.productionAncestor,
            "implementationBase": self.implementationBase,
            "scenarios": [scenario.to_mapping() for scenario in self.scenarios],
        }


@dataclass(frozen=True, slots=True)
class ValidatedScheduleEntry:
    ordinal: int
    scenarioId: str
    variantId: str
    layers: tuple[str, ...]
    inputHash: str
    responseHash: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "scenarioId": self.scenarioId,
            "variantId": self.variantId,
            "layers": list(self.layers),
            "inputHash": self.inputHash,
            "responseHash": self.responseHash,
        }


def _assert_typed_schedule(entries: object) -> None:
    if type(entries) is not tuple or len(entries) != len(REVIEWED_VARIANT_MATRIX):
        raise ValueError("typed schedule is not closed")
    for entry, reviewed in zip(entries, REVIEWED_VARIANT_MATRIX, strict=True):
        if type(entry) is not ValidatedScheduleEntry:
            raise ValueError("typed schedule entry is not closed")
        if (
            entry.ordinal,
            entry.variantId,
            entry.scenarioId,
            entry.layers,
        ) != (
            reviewed.ordinal,
            reviewed.variantId,
            reviewed.scenarioId,
            reviewed.layers,
        ):
            raise ValueError("typed schedule diverges from reviewed matrix")
        _digest(entry.inputHash, "typed-schedule")
        _digest(entry.responseHash, "typed-schedule")


_SCHEDULE_AGGREGATE_CLOSURE = (
    "validate_fixture_contracts:manifest+schedule+coverage+cross-surface-inodes:v1"
)


def _schedule_aggregate_digest(
    schema_version: int,
    production_ancestor: str,
    implementation_base: str,
    entries: tuple[ValidatedScheduleEntry, ...],
) -> str:
    return sha256_json(
        {
            "aggregateClosure": _SCHEDULE_AGGREGATE_CLOSURE,
            "schemaVersion": schema_version,
            "productionAncestor": production_ancestor,
            "implementationBase": implementation_base,
            "entries": [entry.to_mapping() for entry in entries],
        }
    )


@dataclass(frozen=True, slots=True, init=False)
class ValidatedExecutionSchedule:
    schemaVersion: int
    productionAncestor: str
    implementationBase: str
    entries: tuple[ValidatedScheduleEntry, ...]
    _aggregateDigest: str = field(init=False, repr=False, compare=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise ValueError(
            "ValidatedExecutionSchedule is minted only by aggregate fixture validation"
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schemaVersion,
            "productionAncestor": self.productionAncestor,
            "implementationBase": self.implementationBase,
            "entries": [entry.to_mapping() for entry in self.entries],
        }


@dataclass(frozen=True, slots=True)
class _ParsedExecutionSchedule:
    schemaVersion: int
    productionAncestor: str
    implementationBase: str
    entries: tuple[ValidatedScheduleEntry, ...]


@dataclass(frozen=True, slots=True)
class ValidatedCoverageRecord:
    variantId: str
    scenarioId: str
    layers: tuple[str, ...]
    responseClass: str
    voiceEligibility: bool
    oracleHash: str
    sabotageId: str
    promotionClass: str
    expectedVerdict: str
    nonClaims: tuple[str, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "variantId": self.variantId,
            "scenarioId": self.scenarioId,
            "layers": list(self.layers),
            "responseClass": self.responseClass,
            "voiceEligibility": self.voiceEligibility,
            "oracleHash": self.oracleHash,
            "sabotageId": self.sabotageId,
            "promotionClass": self.promotionClass,
            "expectedVerdict": self.expectedVerdict,
            "nonClaims": list(self.nonClaims),
        }


@dataclass(frozen=True, slots=True)
class ValidatedCoverage:
    schemaVersion: int
    productionAncestor: str
    implementationBase: str
    records: tuple[ValidatedCoverageRecord, ...]
    count: int
    contractDigest: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schemaVersion,
            "productionAncestor": self.productionAncestor,
            "implementationBase": self.implementationBase,
            "records": [record.to_mapping() for record in self.records],
        }


@dataclass(frozen=True, slots=True)
class _ValidatedPublic:
    manifest: ValidatedManifest
    identities: tuple[frozenset[tuple[int, int]], ...]


@dataclass(frozen=True, slots=True)
class _ValidatedSealed:
    coverage: ValidatedCoverage
    identities: frozenset[tuple[int, int]]


def _provenance(value: object) -> GenerationProvenance:
    if type(value) is GenerationProvenance:
        return validate_generation_provenance(value.to_mapping())
    return validate_generation_provenance(value)


def _validate_public_internal(
    value: object,
    *,
    input_root: Path,
    response_root: Path,
    owner_root: Path,
    expected_owner_paths: Mapping[str, frozenset[Path]],
    provenance: object,
) -> _ValidatedPublic:
    parsed_provenance = _provenance(provenance)
    scan_json(
        value,
        artifact_id="public-manifest",
        provenance=parsed_provenance,
    )
    _reject_public_leaks(value, "public-manifest")
    item = _exact_dict(value, _PUBLIC_TOP_KEYS, "public-manifest")
    _validate_refs(item, "public-manifest")
    scenarios = item["scenarios"]
    if type(scenarios) is not list or len(scenarios) != len(_SCENARIO_ORDER):
        _fail("CEQ_SCENARIO_SET", "public-manifest")
    if type(expected_owner_paths) is not dict or set(expected_owner_paths) != MANDATORY_SCENARIO_IDS:
        _fail("CEQ_OWNER_ALLOWLIST", "public-manifest")
    parsed: list[ValidatedScenario] = []
    seen_ids: list[str] = []
    inputs: set[tuple[int, int]] = set()
    responses: set[tuple[int, int]] = set()
    owners: set[tuple[int, int]] = set()
    for index, raw_scenario in enumerate(scenarios):
        artifact = f"public-scenario-{index}"
        scenario = _exact_dict(raw_scenario, _PUBLIC_SCENARIO_KEYS, artifact)
        scenario_id = _strict_string(scenario["id"], artifact)
        seen_ids.append(scenario_id)
        if scenario_id not in MANDATORY_SCENARIO_IDS:
            _fail("CEQ_SCENARIO_SET", artifact)
        if scenario["family"] != SCENARIO_PRIMARY_FAMILIES[scenario_id]:
            _fail("CEQ_SCENARIO_FAMILY", artifact)
        purpose = _strict_string(scenario["purpose"], artifact)
        provenance_label = _strict_string(scenario["provenanceLabel"], artifact)
        if provenance_label != "newly-authored-synthetic":
            _fail("CEQ_PROVENANCE_LABEL", artifact)
        input_path = _canonical_relative(scenario["inputBundle"], artifact)
        response_path = _canonical_relative(scenario["responseBundle"], artifact)
        if (
            PurePosixPath(input_path).suffix != ".json"
            or PurePosixPath(response_path).suffix != ".json"
        ):
            _fail("CEQ_BUNDLE_FORMAT", artifact)
        input_hash = _digest(scenario["inputHash"], artifact)
        response_hash = _digest(scenario["responseHash"], artifact)
        input_file = _read_bound_file(input_root, input_path, f"input-{index}")
        response_file = _read_bound_file(response_root, response_path, f"response-{index}")
        if input_file.sha256 != input_hash or response_file.sha256 != response_hash:
            _fail("CEQ_BUNDLE_HASH", artifact)
        scan_bytes(
            input_file.data,
            artifact_id=f"input-{index}",
            provenance=parsed_provenance,
            require_json=True,
        )
        scan_bytes(
            response_file.data,
            artifact_id=f"response-{index}",
            provenance=parsed_provenance,
            require_json=True,
        )
        inputs.add(input_file.identity)
        responses.add(response_file.identity)
        owner_hashes = scenario["ownerModuleHashes"]
        if type(owner_hashes) is not dict or not owner_hashes:
            _fail("CEQ_OWNER_SCHEMA", artifact)
        expected = expected_owner_paths[scenario_id]
        if type(expected) is not frozenset or not expected:
            _fail("CEQ_OWNER_ALLOWLIST", artifact)
        expected_relatives: set[str] = set()
        for expected_path in expected:
            if not isinstance(expected_path, Path) or not expected_path.is_absolute():
                _fail("CEQ_OWNER_ALLOWLIST", artifact)
            try:
                relative = expected_path.relative_to(owner_root).as_posix()
            except ValueError:
                _fail("CEQ_OWNER_ALLOWLIST", artifact)
            expected_relatives.add(_canonical_relative(relative, artifact))
        if set(owner_hashes) != expected_relatives:
            _fail("CEQ_OWNER_ALLOWLIST", artifact)
        parsed_owners = []
        for owner_path in sorted(owner_hashes):
            owner_digest = _digest(owner_hashes[owner_path], artifact)
            owner_file = _read_bound_file(owner_root, owner_path, f"owner-{index}")
            if owner_file.sha256 != owner_digest:
                _fail("CEQ_OWNER_HASH", artifact)
            owners.add(owner_file.identity)
            parsed_owners.append((owner_path, owner_digest))
        parsed.append(
            ValidatedScenario(
                id=scenario_id,
                family=scenario["family"],
                purpose=purpose,
                provenanceLabel=provenance_label,
                inputBundle=input_path,
                inputHash=input_hash,
                responseBundle=response_path,
                responseHash=response_hash,
                ownerModuleHashes=tuple(parsed_owners),
            )
        )
    if tuple(seen_ids) != _SCENARIO_ORDER or set(seen_ids) != MANDATORY_SCENARIO_IDS:
        _fail("CEQ_SCENARIO_SET", "public-manifest")
    if inputs & responses or inputs & owners or responses & owners:
        _fail("CEQ_CROSS_SURFACE_ALIAS", "public-manifest")
    return _ValidatedPublic(
        manifest=ValidatedManifest(
            schemaVersion=1,
            productionAncestor=PRODUCTION_ANCESTOR,
            implementationBase=IMPLEMENTATION_BASE,
            scenarios=tuple(parsed),
        ),
        identities=(frozenset(inputs), frozenset(responses), frozenset(owners)),
    )


def validate_public_manifest(
    value: object,
    *,
    input_root: Path,
    response_root: Path,
    owner_root: Path,
    expected_owner_paths: Mapping[str, frozenset[Path]],
    provenance: object,
) -> ValidatedManifest:
    return _validate_public_internal(
        value,
        input_root=input_root,
        response_root=response_root,
        owner_root=owner_root,
        expected_owner_paths=expected_owner_paths,
        provenance=provenance,
    ).manifest


def validate_public_manifest_bytes(
    data: bytes,
    *,
    artifact_id: str,
    input_root: Path,
    response_root: Path,
    owner_root: Path,
    expected_owner_paths: Mapping[str, frozenset[Path]],
    provenance: object,
) -> ValidatedManifest:
    return validate_public_manifest(
        load_json_bytes(data, artifact_id=artifact_id),
        input_root=input_root,
        response_root=response_root,
        owner_root=owner_root,
        expected_owner_paths=expected_owner_paths,
        provenance=provenance,
    )


def validate_execution_schedule(value: object) -> _ParsedExecutionSchedule:
    _reject_public_leaks(value, "public-schedule")
    item = _exact_dict(value, _SCHEDULE_TOP_KEYS, "public-schedule")
    _validate_refs(item, "public-schedule")
    entries = item["entries"]
    if type(entries) is not list or len(entries) != len(REVIEWED_VARIANT_MATRIX):
        _fail("CEQ_SCHEDULE_SET", "public-schedule")
    parsed: list[ValidatedScheduleEntry] = []
    for index, (raw_entry, reviewed) in enumerate(zip(entries, REVIEWED_VARIANT_MATRIX, strict=True)):
        artifact = f"public-schedule-{index}"
        entry = _exact_dict(raw_entry, _SCHEDULE_ENTRY_KEYS, artifact)
        if type(entry["ordinal"]) is not int:
            _fail("CEQ_SCHEDULE_ORDINAL", artifact)
        layers = entry["layers"]
        if type(layers) is not list or not all(type(layer) is str for layer in layers):
            _fail("CEQ_SCHEDULE_LAYERS", artifact)
        parsed_layers = tuple(layers)
        if (
            entry["ordinal"],
            entry["variantId"],
            entry["scenarioId"],
            parsed_layers,
        ) != (
            reviewed.ordinal,
            reviewed.variantId,
            reviewed.scenarioId,
            reviewed.layers,
        ):
            _fail("CEQ_REVIEWED_MATRIX", artifact)
        parsed.append(
            ValidatedScheduleEntry(
                ordinal=reviewed.ordinal,
                scenarioId=reviewed.scenarioId,
                variantId=reviewed.variantId,
                layers=reviewed.layers,
                inputHash=_digest(entry["inputHash"], artifact),
                responseHash=_digest(entry["responseHash"], artifact),
            )
        )
    return _ParsedExecutionSchedule(
        schemaVersion=1,
        productionAncestor=PRODUCTION_ANCESTOR,
        implementationBase=IMPLEMENTATION_BASE,
        entries=tuple(parsed),
    )


def oracle_path_for_variant(variant_id: str) -> PurePosixPath:
    if type(variant_id) is not str or variant_id not in MANDATORY_VARIANT_IDS:
        raise ValueError("variant is outside the reviewed oracle convention")
    return PurePosixPath(f"{variant_id}.json")


def _validate_coverage_internal(
    value: object,
    *,
    oracle_root: Path,
    provenance: object,
) -> _ValidatedSealed:
    parsed_provenance = _provenance(provenance)
    item = _exact_dict(value, _COVERAGE_TOP_KEYS, "sealed-coverage")
    _validate_refs(item, "sealed-coverage")
    records = item["records"]
    if type(records) is not list or len(records) != len(REVIEWED_VARIANT_MATRIX):
        _fail("CEQ_COVERAGE_SET", "sealed-coverage")
    by_variant: dict[str, dict[str, object]] = {}
    for index, raw_record in enumerate(records):
        record = _exact_dict(raw_record, _COVERAGE_RECORD_KEYS, f"sealed-record-{index}")
        variant_id = record["variantId"]
        if type(variant_id) is not str or variant_id in by_variant:
            _fail("CEQ_COVERAGE_SET", f"sealed-record-{index}")
        by_variant[variant_id] = record
    if set(by_variant) != MANDATORY_VARIANT_IDS:
        _fail("CEQ_COVERAGE_SET", "sealed-coverage")
    parsed: list[ValidatedCoverageRecord] = []
    identities: set[tuple[int, int]] = set()
    for reviewed in REVIEWED_VARIANT_MATRIX:
        artifact = f"sealed-record-{reviewed.ordinal}"
        record = by_variant[reviewed.variantId]
        layers = record["layers"]
        nonclaims = record["nonClaims"]
        if type(layers) is not list or type(nonclaims) is not list:
            _fail("CEQ_COVERAGE_SCHEMA", artifact)
        if not all(type(item) is str for item in (*layers, *nonclaims)):
            _fail("CEQ_COVERAGE_SCHEMA", artifact)
        if (
            type(record["scenarioId"]) is not str
            or type(record["responseClass"]) is not str
            or type(record["voiceEligibility"]) is not bool
            or type(record["sabotageId"]) is not str
            or type(record["promotionClass"]) is not str
            or type(record["expectedVerdict"]) is not str
        ):
            _fail("CEQ_COVERAGE_SCHEMA", artifact)
        expected = (
            reviewed.scenarioId,
            reviewed.layers,
            reviewed.responseClass,
            reviewed.voiceEligibility,
            reviewed.sabotageId,
            reviewed.promotionClass,
            reviewed.expectedVerdict,
            reviewed.nonClaims,
        )
        observed = (
            record["scenarioId"],
            tuple(layers),
            record["responseClass"],
            record["voiceEligibility"],
            record["sabotageId"],
            record["promotionClass"],
            record["expectedVerdict"],
            tuple(nonclaims),
        )
        if observed != expected:
            _fail("CEQ_REVIEWED_SEALED_MATRIX", artifact)
        oracle_hash = _digest(record["oracleHash"], artifact)
        oracle_file = _read_bound_file(
            oracle_root,
            oracle_path_for_variant(reviewed.variantId).as_posix(),
            f"oracle-{reviewed.ordinal}",
        )
        if oracle_file.sha256 != oracle_hash:
            _fail("CEQ_ORACLE_HASH", artifact)
        scan_bytes(
            oracle_file.data,
            artifact_id=f"oracle-{reviewed.ordinal}",
            provenance=parsed_provenance,
            require_json=True,
        )
        identities.add(oracle_file.identity)
        parsed.append(
            ValidatedCoverageRecord(
                variantId=reviewed.variantId,
                scenarioId=reviewed.scenarioId,
                layers=reviewed.layers,
                responseClass=reviewed.responseClass,
                voiceEligibility=False,
                oracleHash=oracle_hash,
                sabotageId=reviewed.sabotageId,
                promotionClass=reviewed.promotionClass,
                expectedVerdict=reviewed.expectedVerdict,
                nonClaims=reviewed.nonClaims,
            )
        )
    sorted_contract = sorted(
        (record.to_mapping() for record in parsed), key=lambda record: record["variantId"]
    )
    coverage = ValidatedCoverage(
        schemaVersion=1,
        productionAncestor=PRODUCTION_ANCESTOR,
        implementationBase=IMPLEMENTATION_BASE,
        records=tuple(parsed),
        count=len(parsed),
        contractDigest=sha256_json(sorted_contract),
    )
    return _ValidatedSealed(coverage=coverage, identities=frozenset(identities))


def validate_coverage(
    value: object,
    *,
    oracle_root: Path,
    provenance: object,
) -> ValidatedCoverage:
    return _validate_coverage_internal(
        value,
        oracle_root=oracle_root,
        provenance=provenance,
    ).coverage


def validate_fixture_contracts(
    manifest: object,
    schedule: object,
    coverage: object,
    *,
    input_root: Path,
    response_root: Path,
    owner_root: Path,
    oracle_root: Path,
    expected_owner_paths: Mapping[str, frozenset[Path]],
    provenance: object,
) -> tuple[ValidatedManifest, ValidatedExecutionSchedule, ValidatedCoverage]:
    public = _validate_public_internal(
        manifest,
        input_root=input_root,
        response_root=response_root,
        owner_root=owner_root,
        expected_owner_paths=expected_owner_paths,
        provenance=provenance,
    )
    parsed_provenance = _provenance(provenance)
    scan_json(
        schedule,
        artifact_id="public-schedule",
        provenance=parsed_provenance,
    )
    parsed_schedule = validate_execution_schedule(schedule)
    sealed = _validate_coverage_internal(
        coverage,
        oracle_root=oracle_root,
        provenance=provenance,
    )
    scenario_by_id = {scenario.id: scenario for scenario in public.manifest.scenarios}
    for entry in parsed_schedule.entries:
        scenario = scenario_by_id[entry.scenarioId]
        if entry.inputHash != scenario.inputHash or entry.responseHash != scenario.responseHash:
            _fail("CEQ_SCHEDULE_MANIFEST_HASH", "fixture-contracts")
    public_identities = set().union(*public.identities)
    if public_identities & set(sealed.identities):
        _fail("CEQ_CROSS_SURFACE_ALIAS", "fixture-contracts")
    validated_schedule = object.__new__(ValidatedExecutionSchedule)
    for name, value in (
        ("schemaVersion", parsed_schedule.schemaVersion),
        ("productionAncestor", parsed_schedule.productionAncestor),
        ("implementationBase", parsed_schedule.implementationBase),
        ("entries", parsed_schedule.entries),
    ):
        object.__setattr__(validated_schedule, name, value)
    object.__setattr__(
        validated_schedule,
        "_aggregateDigest",
        _schedule_aggregate_digest(
            parsed_schedule.schemaVersion,
            parsed_schedule.productionAncestor,
            parsed_schedule.implementationBase,
            parsed_schedule.entries,
        ),
    )
    return public.manifest, validated_schedule, sealed.coverage


def validate_fixture_contract_files(
    *,
    manifest_path: Path,
    schedule_path: Path,
    coverage_path: Path,
    provenance_path: Path,
    input_root: Path,
    response_root: Path,
    owner_root: Path,
    oracle_root: Path,
    expected_owner_paths: Mapping[str, frozenset[Path]],
) -> tuple[ValidatedManifest, ValidatedExecutionSchedule, ValidatedCoverage]:
    manifest_file = _read_control_file(manifest_path, "public-manifest")
    schedule_file = _read_control_file(schedule_path, "public-schedule")
    coverage_file = _read_control_file(coverage_path, "sealed-coverage")
    provenance_file = _read_control_file(provenance_path, "provenance")
    control_identities = {
        manifest_file.identity,
        schedule_file.identity,
        coverage_file.identity,
        provenance_file.identity,
    }
    if len(control_identities) != 4:
        _fail("CEQ_CONTROL_ALIAS", "fixture-contracts")
    parsed_provenance = validate_generation_provenance_bytes(
        provenance_file.data,
        artifact_id="provenance",
    )
    return validate_fixture_contracts(
        load_json_bytes(manifest_file.data, artifact_id="public-manifest"),
        load_json_bytes(schedule_file.data, artifact_id="public-schedule"),
        load_json_bytes(coverage_file.data, artifact_id="sealed-coverage"),
        input_root=input_root,
        response_root=response_root,
        owner_root=owner_root,
        oracle_root=oracle_root,
        expected_owner_paths=expected_owner_paths,
        provenance=parsed_provenance,
    )


def emit_child_descriptors(
    schedule: ValidatedExecutionSchedule,
) -> list[dict[str, object]]:
    if type(schedule) is not ValidatedExecutionSchedule:
        raise TypeError("descriptor emission requires a validated public schedule")
    if (
        type(schedule.schemaVersion) is not int
        or schedule.schemaVersion != 1
        or schedule.productionAncestor != PRODUCTION_ANCESTOR
        or schedule.implementationBase != IMPLEMENTATION_BASE
    ):
        raise ValueError("descriptor emission requires valid schedule references")
    _assert_typed_schedule(schedule.entries)
    expected_digest = _schedule_aggregate_digest(
        schedule.schemaVersion,
        schedule.productionAncestor,
        schedule.implementationBase,
        schedule.entries,
    )
    if getattr(schedule, "_aggregateDigest", None) != expected_digest:
        raise ValueError("descriptor emission requires a bound aggregate digest")
    return [
        {
            "scenarioId": entry.scenarioId,
            "variantId": entry.variantId,
            "layer": layer,
            "inputHash": entry.inputHash,
            "responseHash": entry.responseHash,
        }
        for entry in schedule.entries
        for layer in entry.layers
    ]
