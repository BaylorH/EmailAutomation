"""Closed, product-free records for CE-Q1 qualification evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
from types import MappingProxyType
from typing import ClassVar


class Layer(str, Enum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class GateVerdict(str, Enum):
    BLOCKED = "BLOCKED"
    INSTRUMENT_FAILURE = "INSTRUMENT_FAILURE"
    FAIL = "FAIL"
    UNVERIFIED = "UNVERIFIED"
    PASS_OFFLINE = "PASS_OFFLINE"


class EvidenceResult(str, Enum):
    VERIFIED = "VERIFIED"
    REFUTED = "REFUTED"
    UNVERIFIED = "UNVERIFIED"


class PromotionClass(str, Enum):
    REQUIRED = "required"
    DIAGNOSTIC = "diagnostic"


class FutureGate(str, Enum):
    CE_Q1B_TEXT = "CE-Q1B-TEXT"
    CE_Q1B_VOICE = "CE-Q1B-VOICE"


_UNRESOLVED_NONCLAIMS = frozenset(
    {"UNVERIFIED_NO_SHARED_FINALIZER", "MISSING_DIAGNOSTIC_EVIDENCE"}
)


def _validate_json(value: object, active: set[int]) -> None:
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("canonical JSON rejects non-finite numbers")
        return
    if type(value) is list:
        identity = id(value)
        if identity in active:
            raise ValueError("canonical JSON rejects cycles")
        active.add(identity)
        try:
            for item in value:
                _validate_json(item, active)
        finally:
            active.remove(identity)
        return
    if type(value) is dict:
        identity = id(value)
        if identity in active:
            raise ValueError("canonical JSON rejects cycles")
        active.add(identity)
        try:
            for key, item in value.items():
                if type(key) is not str:
                    raise TypeError("canonical JSON object keys must be strings")
                _validate_json(item, active)
        finally:
            active.remove(identity)
        return
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


class _ClosedRecord:
    _KEYS: ClassVar[frozenset[str]]

    @classmethod
    def from_mapping(cls, value: object) -> _ClosedRecord:
        raise NotImplementedError

    def to_mapping(self) -> dict[str, object]:
        raise NotImplementedError


def canonical_json(value: object) -> bytes:
    """Serialize a strict JSON value or one of this module's known records."""
    if type(value) in _CLOSED_RECORD_TYPES:
        value = value.to_mapping()
    _validate_json(value, set())
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _require_mapping(value: object, keys: frozenset[str], label: str) -> Mapping[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an exact JSON object")
    actual = set(value)
    if actual != keys:
        raise ValueError(
            f"{label} keys are not closed; "
            f"missing={sorted(keys - actual)!r}, extra={sorted(actual - keys)!r}"
        )
    return value


def _string(value: object, label: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or not value:
        raise TypeError(f"{label} must be a non-empty string")
    return value


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} must be a boolean")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    if value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return value


def _digest(value: object, label: str, *, optional: bool = False) -> str | None:
    result = _string(value, label, optional=optional)
    if result is None:
        return None
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return result


def _enum(value: object, enum_type: type[Enum], label: str) -> Enum:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    try:
        return enum_type(value)
    except ValueError as error:
        raise ValueError(f"{label} is outside the closed enum") from error


def _strings(value: object, label: str, *, sorted_unique: bool = True) -> tuple[str, ...]:
    if type(value) is not list:
        raise TypeError(f"{label} must be a list")
    parsed: list[str] = []
    for index, item in enumerate(value):
        text = _string(item, f"{label}[{index}]")
        assert text is not None
        parsed.append(text)
    result = tuple(parsed)
    if sorted_unique and result != tuple(sorted(set(result))):
        raise ValueError(f"{label} must be sorted and unique")
    return result


def _freeze_json(value: object) -> object:
    _validate_json(value, set())
    return _freeze_valid_json(value)


def _freeze_valid_json(value: object) -> object:
    if type(value) is dict:
        return MappingProxyType(
            {key: _freeze_valid_json(item) for key, item in sorted(value.items())}
        )
    if type(value) is list:
        return tuple(_freeze_valid_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


def _record_list(value: object, record_type: type[_ClosedRecord], label: str) -> tuple:
    if type(value) is not list:
        raise TypeError(f"{label} must be a list")
    result = []
    for index, item in enumerate(value):
        try:
            result.append(record_type.from_mapping(item))
        except (TypeError, ValueError) as error:
            raise type(error)(f"{label}[{index}]: {error}") from error
    return tuple(result)


def _assert_tuple_of(value: object, item_type: type, label: str) -> None:
    if type(value) is not tuple or not all(type(item) is item_type for item in value):
        raise TypeError(f"{label} must be a tuple of {item_type.__name__}")


@dataclass(frozen=True, slots=True)
class EffectAttempt(_ClosedRecord):
    operationId: str
    attemptOrdinal: int
    effectClass: str
    method: str
    target: str
    outcome: str
    succeeded: bool

    _KEYS = frozenset(
        {
            "operationId",
            "attemptOrdinal",
            "effectClass",
            "method",
            "target",
            "outcome",
            "succeeded",
        }
    )
    _OUTCOMES = frozenset({"ALLOWED", "BLOCKED", "FAILED"})

    def __post_init__(self) -> None:
        for name in ("operationId", "effectClass", "method", "target"):
            _string(getattr(self, name), f"EffectAttempt.{name}")
        _integer(self.attemptOrdinal, "EffectAttempt.attemptOrdinal")
        if type(self.outcome) is not str or self.outcome not in self._OUTCOMES:
            raise ValueError("EffectAttempt.outcome is outside the closed enum")
        _boolean(self.succeeded, "EffectAttempt.succeeded")
        if self.succeeded != (self.outcome == "ALLOWED"):
            raise ValueError("EffectAttempt.succeeded contradicts outcome")

    @classmethod
    def from_mapping(cls, value: object) -> EffectAttempt:
        item = _require_mapping(value, cls._KEYS, "EffectAttempt")
        return cls(
            operationId=_string(item["operationId"], "EffectAttempt.operationId"),
            attemptOrdinal=_integer(item["attemptOrdinal"], "EffectAttempt.attemptOrdinal"),
            effectClass=_string(item["effectClass"], "EffectAttempt.effectClass"),
            method=_string(item["method"], "EffectAttempt.method"),
            target=_string(item["target"], "EffectAttempt.target"),
            outcome=_string(item["outcome"], "EffectAttempt.outcome"),
            succeeded=_boolean(item["succeeded"], "EffectAttempt.succeeded"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "operationId": self.operationId,
            "attemptOrdinal": self.attemptOrdinal,
            "effectClass": self.effectClass,
            "method": self.method,
            "target": self.target,
            "outcome": self.outcome,
            "succeeded": self.succeeded,
        }


@dataclass(frozen=True, slots=True)
class FactRecord(_ClosedRecord):
    field: str
    value: object
    unit: str | None
    basis: str | None
    sourceMessageId: str | None
    sourceSpan: tuple[int, int] | None
    targetPropertyId: str | None
    targetSuiteId: str | None
    freshness: str | None
    evidenceRef: str | None

    _KEYS = frozenset(
        {
            "field",
            "value",
            "unit",
            "basis",
            "sourceMessageId",
            "sourceSpan",
            "targetPropertyId",
            "targetSuiteId",
            "freshness",
            "evidenceRef",
        }
    )

    def __post_init__(self) -> None:
        _string(self.field, "FactRecord.field")
        _validate_json(self.value, set())
        object.__setattr__(self, "value", _freeze_valid_json(self.value))
        for name in (
            "unit",
            "basis",
            "sourceMessageId",
            "targetPropertyId",
            "targetSuiteId",
            "freshness",
            "evidenceRef",
        ):
            _string(getattr(self, name), f"FactRecord.{name}", optional=True)
        if self.sourceSpan is not None:
            if type(self.sourceSpan) is not tuple or len(self.sourceSpan) != 2:
                raise TypeError("FactRecord.sourceSpan must be a two-integer tuple or null")
            start = _integer(self.sourceSpan[0], "FactRecord.sourceSpan[0]")
            end = _integer(self.sourceSpan[1], "FactRecord.sourceSpan[1]")
            if start >= end:
                raise ValueError("FactRecord.sourceSpan must satisfy start < end")

    @classmethod
    def from_mapping(cls, value: object) -> FactRecord:
        item = _require_mapping(value, cls._KEYS, "FactRecord")
        span = item["sourceSpan"]
        if span is not None:
            if type(span) is not list or len(span) != 2:
                raise TypeError("FactRecord.sourceSpan must be a two-integer list or null")
            span = (
                _integer(span[0], "FactRecord.sourceSpan[0]"),
                _integer(span[1], "FactRecord.sourceSpan[1]"),
            )
        return cls(
            field=_string(item["field"], "FactRecord.field"),
            value=item["value"],
            unit=_string(item["unit"], "FactRecord.unit", optional=True),
            basis=_string(item["basis"], "FactRecord.basis", optional=True),
            sourceMessageId=_string(
                item["sourceMessageId"], "FactRecord.sourceMessageId", optional=True
            ),
            sourceSpan=span,
            targetPropertyId=_string(
                item["targetPropertyId"], "FactRecord.targetPropertyId", optional=True
            ),
            targetSuiteId=_string(
                item["targetSuiteId"], "FactRecord.targetSuiteId", optional=True
            ),
            freshness=_string(item["freshness"], "FactRecord.freshness", optional=True),
            evidenceRef=_string(item["evidenceRef"], "FactRecord.evidenceRef", optional=True),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "field": self.field,
            "value": _thaw_json(self.value),
            "unit": self.unit,
            "basis": self.basis,
            "sourceMessageId": self.sourceMessageId,
            "sourceSpan": None if self.sourceSpan is None else list(self.sourceSpan),
            "targetPropertyId": self.targetPropertyId,
            "targetSuiteId": self.targetSuiteId,
            "freshness": self.freshness,
            "evidenceRef": self.evidenceRef,
        }


@dataclass(frozen=True, slots=True)
class StateSnapshot(_ClosedRecord):
    state: Mapping[str, object]

    _KEYS = frozenset({"state"})
    _STATE_KEYS = frozenset(
        {
            "targetRow",
            "siblingRows",
            "formulas",
            "threads",
            "conversations",
            "messages",
            "indexes",
            "reviews",
            "terminalActions",
            "pendingResponses",
            "audit",
            "outbox",
            "sends",
            "followups",
            "providerLedger",
            "effectLedger",
            "actionOrder",
        }
    )

    def __post_init__(self) -> None:
        state = _require_mapping(self.state, self._STATE_KEYS, "StateSnapshot.state")
        _validate_json(state, set())
        for name in ("targetRow", "indexes"):
            if type(state[name]) is not dict:
                raise TypeError(f"StateSnapshot.state.{name} must be a JSON object")
        for name in self._STATE_KEYS - {"targetRow", "indexes"}:
            if type(state[name]) is not list:
                raise TypeError(f"StateSnapshot.state.{name} must be a JSON list")
        object.__setattr__(self, "state", _freeze_valid_json(state))

    @property
    def digest(self) -> str:
        return sha256_json(_thaw_json(self.state))

    @classmethod
    def from_mapping(cls, value: object) -> StateSnapshot:
        item = _require_mapping(value, cls._KEYS, "StateSnapshot")
        if type(item["state"]) is not dict:
            raise TypeError("StateSnapshot.state must be a JSON object")
        return cls(state=item["state"])

    def to_mapping(self) -> dict[str, object]:
        return {"state": _thaw_json(self.state)}


@dataclass(frozen=True, slots=True)
class EventRecord(_ClosedRecord):
    kind: str
    ordinal: int
    payload: Mapping[str, object]

    _KEYS = frozenset({"kind", "ordinal", "payload"})

    def __post_init__(self) -> None:
        _string(self.kind, "EventRecord.kind")
        _integer(self.ordinal, "EventRecord.ordinal")
        if type(self.payload) is not dict:
            raise TypeError("EventRecord.payload must be a JSON object")
        _validate_json(self.payload, set())
        object.__setattr__(self, "payload", _freeze_valid_json(self.payload))

    @classmethod
    def from_mapping(cls, value: object) -> EventRecord:
        item = _require_mapping(value, cls._KEYS, "EventRecord")
        if type(item["payload"]) is not dict:
            raise TypeError("EventRecord.payload must be a JSON object")
        _validate_json(item["payload"], set())
        return cls(
            kind=_string(item["kind"], "EventRecord.kind"),
            ordinal=_integer(item["ordinal"], "EventRecord.ordinal"),
            payload=item["payload"],
        )

    def to_mapping(self) -> dict[str, object]:
        return {"kind": self.kind, "ordinal": self.ordinal, "payload": _thaw_json(self.payload)}


@dataclass(frozen=True, slots=True)
class ExecutionResult(_ClosedRecord):
    scenarioId: str
    variantId: str
    layer: Layer
    sourceIdentity: str
    facts: tuple[FactRecord, ...]
    events: tuple[EventRecord, ...]
    draft: Mapping[str, object] | None
    stateBefore: StateSnapshot
    stateAfter: StateSnapshot
    effectLedger: tuple[EffectAttempt, ...]
    providerLedger: tuple[EffectAttempt, ...]
    runtimeProjectionDigest: str
    nonClaims: tuple[str, ...]

    _KEYS = frozenset(
        {
            "scenarioId",
            "variantId",
            "layer",
            "sourceIdentity",
            "facts",
            "events",
            "draft",
            "stateBefore",
            "stateAfter",
            "effectLedger",
            "providerLedger",
            "runtimeProjectionDigest",
            "nonClaims",
        }
    )

    def __post_init__(self) -> None:
        for name in ("scenarioId", "variantId", "sourceIdentity"):
            _string(getattr(self, name), f"ExecutionResult.{name}")
        if type(self.layer) is not Layer:
            raise TypeError("ExecutionResult.layer must be Layer")
        _assert_tuple_of(self.facts, FactRecord, "ExecutionResult.facts")
        _assert_tuple_of(self.events, EventRecord, "ExecutionResult.events")
        if self.draft is not None:
            if type(self.draft) is not dict:
                raise TypeError("ExecutionResult.draft must be a JSON object or null")
            _validate_json(self.draft, set())
            object.__setattr__(self, "draft", _freeze_valid_json(self.draft))
        if type(self.stateBefore) is not StateSnapshot or type(self.stateAfter) is not StateSnapshot:
            raise TypeError("ExecutionResult state fields must be StateSnapshot")
        _assert_tuple_of(self.effectLedger, EffectAttempt, "ExecutionResult.effectLedger")
        _assert_tuple_of(self.providerLedger, EffectAttempt, "ExecutionResult.providerLedger")
        _digest(self.runtimeProjectionDigest, "ExecutionResult.runtimeProjectionDigest")
        if type(self.nonClaims) is not tuple or not all(
            type(item) is str and item for item in self.nonClaims
        ):
            raise TypeError("ExecutionResult.nonClaims must be non-empty strings")
        if self.nonClaims != tuple(sorted(set(self.nonClaims))):
            raise ValueError("ExecutionResult.nonClaims must be sorted and unique")
        if self.draft is not None and "NOT_FINAL_DRAFT_UNVERIFIED" not in self.nonClaims:
            raise ValueError("draft observations require the Task 8 final-draft nonclaim")

    @classmethod
    def from_mapping(cls, value: object) -> ExecutionResult:
        item = _require_mapping(value, cls._KEYS, "ExecutionResult")
        draft = item["draft"]
        if draft is not None:
            if type(draft) is not dict:
                raise TypeError("ExecutionResult.draft must be a JSON object or null")
            _validate_json(draft, set())
        return cls(
            scenarioId=_string(item["scenarioId"], "ExecutionResult.scenarioId"),
            variantId=_string(item["variantId"], "ExecutionResult.variantId"),
            layer=_enum(item["layer"], Layer, "ExecutionResult.layer"),
            sourceIdentity=_string(item["sourceIdentity"], "ExecutionResult.sourceIdentity"),
            facts=_record_list(item["facts"], FactRecord, "ExecutionResult.facts"),
            events=_record_list(item["events"], EventRecord, "ExecutionResult.events"),
            draft=draft,
            stateBefore=StateSnapshot.from_mapping(item["stateBefore"]),
            stateAfter=StateSnapshot.from_mapping(item["stateAfter"]),
            effectLedger=_record_list(
                item["effectLedger"], EffectAttempt, "ExecutionResult.effectLedger"
            ),
            providerLedger=_record_list(
                item["providerLedger"], EffectAttempt, "ExecutionResult.providerLedger"
            ),
            runtimeProjectionDigest=_digest(
                item["runtimeProjectionDigest"], "ExecutionResult.runtimeProjectionDigest"
            ),
            nonClaims=_strings(item["nonClaims"], "ExecutionResult.nonClaims"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "scenarioId": self.scenarioId,
            "variantId": self.variantId,
            "layer": self.layer.value,
            "sourceIdentity": self.sourceIdentity,
            "facts": [fact.to_mapping() for fact in self.facts],
            "events": [event.to_mapping() for event in self.events],
            "draft": None if self.draft is None else _thaw_json(self.draft),
            "stateBefore": self.stateBefore.to_mapping(),
            "stateAfter": self.stateAfter.to_mapping(),
            "effectLedger": [effect.to_mapping() for effect in self.effectLedger],
            "providerLedger": [effect.to_mapping() for effect in self.providerLedger],
            "runtimeProjectionDigest": self.runtimeProjectionDigest,
            "nonClaims": list(self.nonClaims),
        }


@dataclass(frozen=True, slots=True)
class ScoreRecord(_ClosedRecord):
    scenarioId: str
    variantId: str
    layer: Layer
    promotionClass: PromotionClass
    evidenceResult: EvidenceResult
    failureReasons: tuple[str, ...]
    diff: Mapping[str, object]
    stateBeforeDigest: str
    stateAfterDigest: str
    stateReplayDigest: str | None
    nonClaims: tuple[str, ...]

    _KEYS = frozenset(
        {
            "scenarioId",
            "variantId",
            "layer",
            "promotionClass",
            "evidenceResult",
            "failureReasons",
            "diff",
            "stateBeforeDigest",
            "stateAfterDigest",
            "stateReplayDigest",
            "nonClaims",
        }
    )

    def __post_init__(self) -> None:
        _string(self.scenarioId, "ScoreRecord.scenarioId")
        _string(self.variantId, "ScoreRecord.variantId")
        if type(self.layer) is not Layer:
            raise TypeError("ScoreRecord.layer must be Layer")
        if type(self.promotionClass) is not PromotionClass:
            raise TypeError("ScoreRecord.promotionClass must be PromotionClass")
        if type(self.evidenceResult) is not EvidenceResult:
            raise TypeError("ScoreRecord.evidenceResult must be EvidenceResult")
        for name in ("failureReasons", "nonClaims"):
            values = getattr(self, name)
            if type(values) is not tuple or not all(type(item) is str and item for item in values):
                raise TypeError(f"ScoreRecord.{name} must contain non-empty strings")
            if values != tuple(sorted(set(values))):
                raise ValueError(f"ScoreRecord.{name} must be sorted and unique")
        if self.evidenceResult is EvidenceResult.VERIFIED and self.failureReasons:
            raise ValueError("VERIFIED scores cannot have failure reasons")
        if self.evidenceResult is not EvidenceResult.VERIFIED and not self.failureReasons:
            raise ValueError("non-VERIFIED scores require a stable failure reason")
        if self.evidenceResult is EvidenceResult.UNVERIFIED and not self.nonClaims:
            raise ValueError("UNVERIFIED scores require a binding nonclaim")
        if self.evidenceResult is EvidenceResult.VERIFIED and _UNRESOLVED_NONCLAIMS & set(
            self.nonClaims
        ):
            raise ValueError("VERIFIED scores cannot retain an unresolved nonclaim")
        if type(self.diff) is not dict:
            raise TypeError("ScoreRecord.diff must be a JSON object")
        _validate_json(self.diff, set())
        object.__setattr__(self, "diff", _freeze_valid_json(self.diff))
        _digest(self.stateBeforeDigest, "ScoreRecord.stateBeforeDigest")
        _digest(self.stateAfterDigest, "ScoreRecord.stateAfterDigest")
        replay = _digest(self.stateReplayDigest, "ScoreRecord.stateReplayDigest", optional=True)
        if replay is None:
            raise ValueError("stateReplayDigest is required for every scored layer")

    @property
    def identity(self) -> tuple[str, str, Layer]:
        return (self.scenarioId, self.variantId, self.layer)

    @classmethod
    def from_mapping(cls, value: object) -> ScoreRecord:
        item = _require_mapping(value, cls._KEYS, "ScoreRecord")
        if type(item["diff"]) is not dict:
            raise TypeError("ScoreRecord.diff must be a JSON object")
        _validate_json(item["diff"], set())
        return cls(
            scenarioId=_string(item["scenarioId"], "ScoreRecord.scenarioId"),
            variantId=_string(item["variantId"], "ScoreRecord.variantId"),
            layer=_enum(item["layer"], Layer, "ScoreRecord.layer"),
            promotionClass=_enum(
                item["promotionClass"], PromotionClass, "ScoreRecord.promotionClass"
            ),
            evidenceResult=_enum(
                item["evidenceResult"], EvidenceResult, "ScoreRecord.evidenceResult"
            ),
            failureReasons=_strings(item["failureReasons"], "ScoreRecord.failureReasons"),
            diff=item["diff"],
            stateBeforeDigest=_digest(
                item["stateBeforeDigest"], "ScoreRecord.stateBeforeDigest"
            ),
            stateAfterDigest=_digest(item["stateAfterDigest"], "ScoreRecord.stateAfterDigest"),
            stateReplayDigest=_digest(
                item["stateReplayDigest"], "ScoreRecord.stateReplayDigest", optional=True
            ),
            nonClaims=_strings(item["nonClaims"], "ScoreRecord.nonClaims"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "scenarioId": self.scenarioId,
            "variantId": self.variantId,
            "layer": self.layer.value,
            "promotionClass": self.promotionClass.value,
            "evidenceResult": self.evidenceResult.value,
            "failureReasons": list(self.failureReasons),
            "diff": _thaw_json(self.diff),
            "stateBeforeDigest": self.stateBeforeDigest,
            "stateAfterDigest": self.stateAfterDigest,
            "stateReplayDigest": self.stateReplayDigest,
            "nonClaims": list(self.nonClaims),
        }


@dataclass(frozen=True, slots=True)
class DiagnosticBlocker(_ClosedRecord):
    scenarioId: str
    variantId: str
    layer: Layer
    observedResult: EvidenceResult | None
    requiredResolution: str
    nonClaims: tuple[str, ...]

    _KEYS = frozenset(
        {"scenarioId", "variantId", "layer", "observedResult", "requiredResolution", "nonClaims"}
    )

    def __post_init__(self) -> None:
        _string(self.scenarioId, "DiagnosticBlocker.scenarioId")
        _string(self.variantId, "DiagnosticBlocker.variantId")
        if type(self.layer) is not Layer:
            raise TypeError("DiagnosticBlocker.layer must be Layer")
        if self.observedResult is not None and type(self.observedResult) is not EvidenceResult:
            raise TypeError("DiagnosticBlocker.observedResult must be EvidenceResult or null")
        if self.observedResult is EvidenceResult.VERIFIED:
            raise ValueError("a VERIFIED diagnostic cannot be a blocker")
        _string(self.requiredResolution, "DiagnosticBlocker.requiredResolution")
        if type(self.nonClaims) is not tuple or not all(
            type(item) is str and item for item in self.nonClaims
        ):
            raise TypeError("DiagnosticBlocker.nonClaims must contain non-empty strings")
        if self.nonClaims != tuple(sorted(set(self.nonClaims))):
            raise ValueError("DiagnosticBlocker.nonClaims must be sorted and unique")
        if self.observedResult is None and self.nonClaims != ("MISSING_DIAGNOSTIC_EVIDENCE",):
            raise ValueError("a missing diagnostic requires the exact missing-evidence nonclaim")
        if self.observedResult is not None and "MISSING_DIAGNOSTIC_EVIDENCE" in self.nonClaims:
            raise ValueError("an observed diagnostic cannot claim missing evidence")

    @property
    def identity(self) -> tuple[str, str, Layer]:
        return (self.scenarioId, self.variantId, self.layer)

    @classmethod
    def from_mapping(cls, value: object) -> DiagnosticBlocker:
        item = _require_mapping(value, cls._KEYS, "DiagnosticBlocker")
        return cls(
            scenarioId=_string(item["scenarioId"], "DiagnosticBlocker.scenarioId"),
            variantId=_string(item["variantId"], "DiagnosticBlocker.variantId"),
            layer=_enum(item["layer"], Layer, "DiagnosticBlocker.layer"),
            observedResult=(
                None
                if item["observedResult"] is None
                else _enum(
                    item["observedResult"],
                    EvidenceResult,
                    "DiagnosticBlocker.observedResult",
                )
            ),
            requiredResolution=_string(
                item["requiredResolution"], "DiagnosticBlocker.requiredResolution"
            ),
            nonClaims=_strings(item["nonClaims"], "DiagnosticBlocker.nonClaims"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "scenarioId": self.scenarioId,
            "variantId": self.variantId,
            "layer": self.layer.value,
            "observedResult": None if self.observedResult is None else self.observedResult.value,
            "requiredResolution": self.requiredResolution,
            "nonClaims": list(self.nonClaims),
        }


@dataclass(frozen=True, slots=True)
class SatisfiedDiagnostic(_ClosedRecord):
    scenarioId: str
    variantId: str
    layer: Layer
    requiredResolution: str
    nonClaims: tuple[str, ...]

    _KEYS = frozenset(
        {"scenarioId", "variantId", "layer", "requiredResolution", "nonClaims"}
    )

    def __post_init__(self) -> None:
        _string(self.scenarioId, "SatisfiedDiagnostic.scenarioId")
        _string(self.variantId, "SatisfiedDiagnostic.variantId")
        if type(self.layer) is not Layer:
            raise TypeError("SatisfiedDiagnostic.layer must be Layer")
        _string(self.requiredResolution, "SatisfiedDiagnostic.requiredResolution")
        if type(self.nonClaims) is not tuple or not all(
            type(item) is str and item for item in self.nonClaims
        ):
            raise TypeError("SatisfiedDiagnostic.nonClaims must contain non-empty strings")
        if self.nonClaims != tuple(sorted(set(self.nonClaims))):
            raise ValueError("SatisfiedDiagnostic.nonClaims must be sorted and unique")
        if _UNRESOLVED_NONCLAIMS & set(self.nonClaims):
            raise ValueError("SatisfiedDiagnostic cannot retain an unresolved nonclaim")

    @property
    def identity(self) -> tuple[str, str, Layer]:
        return (self.scenarioId, self.variantId, self.layer)

    @classmethod
    def from_mapping(cls, value: object) -> SatisfiedDiagnostic:
        item = _require_mapping(value, cls._KEYS, "SatisfiedDiagnostic")
        return cls(
            scenarioId=_string(item["scenarioId"], "SatisfiedDiagnostic.scenarioId"),
            variantId=_string(item["variantId"], "SatisfiedDiagnostic.variantId"),
            layer=_enum(item["layer"], Layer, "SatisfiedDiagnostic.layer"),
            requiredResolution=_string(
                item["requiredResolution"], "SatisfiedDiagnostic.requiredResolution"
            ),
            nonClaims=_strings(item["nonClaims"], "SatisfiedDiagnostic.nonClaims"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "scenarioId": self.scenarioId,
            "variantId": self.variantId,
            "layer": self.layer.value,
            "requiredResolution": self.requiredResolution,
            "nonClaims": list(self.nonClaims),
        }


@dataclass(frozen=True, slots=True)
class NextGateEligibility(_ClosedRecord):
    gateId: FutureGate
    ceq1aVerdict: GateVerdict
    eligible: bool
    blockingDiagnostics: tuple[DiagnosticBlocker, ...]
    satisfiedDiagnostics: tuple[SatisfiedDiagnostic, ...]
    nonClaims: tuple[str, ...]

    _KEYS = frozenset(
        {
            "gateId",
            "ceq1aVerdict",
            "eligible",
            "blockingDiagnostics",
            "satisfiedDiagnostics",
            "nonClaims",
        }
    )

    def __post_init__(self) -> None:
        if type(self.gateId) is not FutureGate:
            raise TypeError("NextGateEligibility.gateId must be FutureGate")
        if type(self.ceq1aVerdict) is not GateVerdict:
            raise TypeError("NextGateEligibility.ceq1aVerdict must be GateVerdict")
        _boolean(self.eligible, "NextGateEligibility.eligible")
        _assert_tuple_of(
            self.blockingDiagnostics,
            DiagnosticBlocker,
            "NextGateEligibility.blockingDiagnostics",
        )
        _assert_tuple_of(
            self.satisfiedDiagnostics,
            SatisfiedDiagnostic,
            "NextGateEligibility.satisfiedDiagnostics",
        )
        identities = tuple(blocker.identity for blocker in self.blockingDiagnostics)
        if identities != tuple(sorted(set(identities), key=lambda item: (item[0], item[1], item[2].value))):
            raise ValueError("NextGateEligibility.blockingDiagnostics must be sorted and unique")
        satisfied_identities = tuple(item.identity for item in self.satisfiedDiagnostics)
        if satisfied_identities != tuple(
            sorted(set(satisfied_identities), key=lambda item: (item[0], item[1], item[2].value))
        ):
            raise ValueError("NextGateEligibility.satisfiedDiagnostics must be sorted and unique")
        if set(identities) & set(satisfied_identities):
            raise ValueError("a diagnostic cannot be both blocking and satisfied")
        required_nonclaims = ("NO_MODEL_CALL_AUTHORIZED", "SEPARATE_AUTHORIZATION_REQUIRED")
        if self.nonClaims != required_nonclaims:
            raise ValueError("NextGateEligibility.nonClaims are not the closed gate nonclaims")
        if self.gateId is FutureGate.CE_Q1B_TEXT and (
            self.blockingDiagnostics or self.satisfiedDiagnostics
        ):
            raise ValueError("CE-Q1B-TEXT has no diagnostic dependency")
        if self.gateId is FutureGate.CE_Q1B_VOICE:
            allowed = set(_VOICE_DEPENDENCIES)
            for diagnostic in self.blockingDiagnostics + self.satisfiedDiagnostics:
                if diagnostic.identity not in allowed:
                    raise ValueError("CE-Q1B-VOICE has an unknown diagnostic dependency")
                if diagnostic.requiredResolution != "SHARED_PRODUCTION_FINALIZER_REQUIRED":
                    raise ValueError("CE-Q1B-VOICE has an unknown required resolution")
            if set(identities) | set(satisfied_identities) != allowed:
                raise ValueError("CE-Q1B-VOICE must account for all five dependencies")
        if self.ceq1aVerdict is GateVerdict.PASS_OFFLINE and any(
            item.observedResult is EvidenceResult.REFUTED for item in self.blockingDiagnostics
        ):
            raise ValueError("PASS_OFFLINE cannot coexist with a refuted diagnostic")
        expected_eligible = (
            self.ceq1aVerdict is GateVerdict.PASS_OFFLINE and not self.blockingDiagnostics
        )
        if self.eligible is not expected_eligible:
            raise ValueError("NextGateEligibility.eligible contradicts verdict or blockers")

    @classmethod
    def from_mapping(cls, value: object) -> NextGateEligibility:
        item = _require_mapping(value, cls._KEYS, "NextGateEligibility")
        return cls(
            gateId=_enum(item["gateId"], FutureGate, "NextGateEligibility.gateId"),
            ceq1aVerdict=_enum(
                item["ceq1aVerdict"], GateVerdict, "NextGateEligibility.ceq1aVerdict"
            ),
            eligible=_boolean(item["eligible"], "NextGateEligibility.eligible"),
            blockingDiagnostics=_record_list(
                item["blockingDiagnostics"],
                DiagnosticBlocker,
                "NextGateEligibility.blockingDiagnostics",
            ),
            satisfiedDiagnostics=_record_list(
                item["satisfiedDiagnostics"],
                SatisfiedDiagnostic,
                "NextGateEligibility.satisfiedDiagnostics",
            ),
            nonClaims=_strings(item["nonClaims"], "NextGateEligibility.nonClaims"),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "gateId": self.gateId.value,
            "ceq1aVerdict": self.ceq1aVerdict.value,
            "eligible": self.eligible,
            "blockingDiagnostics": [item.to_mapping() for item in self.blockingDiagnostics],
            "satisfiedDiagnostics": [item.to_mapping() for item in self.satisfiedDiagnostics],
            "nonClaims": list(self.nonClaims),
        }


_VOICE_DEPENDENCIES = (
    ("VOICE-CONTINUATION", "continuation", Layer.L1),
    ("VOICE-CORRECTION-CLOSE", "correction-close", Layer.L1),
    ("VOICE-FOLLOWUP", "followup", Layer.L1),
    ("VOICE-LAUNCH", "launch", Layer.L1),
    ("VOICE-MISSING", "missing-field", Layer.L1),
)
_GATE_NONCLAIMS = ("NO_MODEL_CALL_AUTHORIZED", "SEPARATE_AUTHORIZATION_REQUIRED")


def _score_identities(scores: tuple[ScoreRecord, ...]) -> tuple[tuple[str, str, Layer], ...]:
    return tuple(score.identity for score in scores)


def _required_reasons(scores: tuple[ScoreRecord, ...], result: EvidenceResult) -> tuple[str, ...]:
    return tuple(
        f"{score.scenarioId}/{score.variantId}/{score.layer.value}"
        for score in scores
        if score.evidenceResult is result
    )


def classify_gate(
    *,
    execution_started: bool = True,
    prerequisite_missing: bool = False,
    instrument_faults: list[str] | tuple[str, ...] = (),
    required_refutations: list[str] | tuple[str, ...] = (),
    missing_required_evidence: list[str] | tuple[str, ...] = (),
) -> GateVerdict:
    _boolean(execution_started, "execution_started")
    _boolean(prerequisite_missing, "prerequisite_missing")
    parsed: dict[str, tuple[str, ...]] = {}
    for label, value in (
        ("instrument_faults", instrument_faults),
        ("required_refutations", required_refutations),
        ("missing_required_evidence", missing_required_evidence),
    ):
        if type(value) not in (list, tuple):
            raise TypeError(f"{label} must be a list or tuple")
        if not all(type(item) is str and item for item in value):
            raise TypeError(f"{label} must contain non-empty strings")
        if len(value) != len(set(value)):
            raise ValueError(f"{label} must not contain duplicates")
        parsed[label] = tuple(value)
    if prerequisite_missing and execution_started:
        raise ValueError("BLOCKED is valid only before execution")
    if not execution_started and not prerequisite_missing:
        raise ValueError("a preflight classification requires a missing prerequisite")
    if not execution_started and any(parsed.values()):
        raise ValueError("preflight BLOCKED cannot carry execution evidence")
    if prerequisite_missing:
        return GateVerdict.BLOCKED
    if parsed["instrument_faults"]:
        return GateVerdict.INSTRUMENT_FAILURE
    if parsed["required_refutations"]:
        return GateVerdict.FAIL
    if parsed["missing_required_evidence"]:
        return GateVerdict.UNVERIFIED
    return GateVerdict.PASS_OFFLINE


def _next_gate_projection(
    verdict: GateVerdict,
    diagnostics: tuple[ScoreRecord, ...],
) -> tuple[NextGateEligibility, ...]:
    by_identity = {score.identity: score for score in diagnostics}
    blockers = []
    satisfied = []
    for identity in _VOICE_DEPENDENCIES:
        score = by_identity.get(identity)
        if score is None:
            blockers.append(
                DiagnosticBlocker(
                    scenarioId=identity[0],
                    variantId=identity[1],
                    layer=identity[2],
                    observedResult=None,
                    requiredResolution="SHARED_PRODUCTION_FINALIZER_REQUIRED",
                    nonClaims=("MISSING_DIAGNOSTIC_EVIDENCE",),
                )
            )
        elif score.evidenceResult is not EvidenceResult.VERIFIED:
            blockers.append(
                DiagnosticBlocker(
                    scenarioId=score.scenarioId,
                    variantId=score.variantId,
                    layer=score.layer,
                    observedResult=score.evidenceResult,
                    requiredResolution="SHARED_PRODUCTION_FINALIZER_REQUIRED",
                    nonClaims=score.nonClaims,
                )
            )
        else:
            satisfied.append(
                SatisfiedDiagnostic(
                    scenarioId=score.scenarioId,
                    variantId=score.variantId,
                    layer=score.layer,
                    requiredResolution="SHARED_PRODUCTION_FINALIZER_REQUIRED",
                    nonClaims=score.nonClaims,
                )
            )
    blockers_tuple = tuple(blockers)
    satisfied_tuple = tuple(satisfied)
    hard_green = verdict is GateVerdict.PASS_OFFLINE
    return (
        NextGateEligibility(
            gateId=FutureGate.CE_Q1B_TEXT,
            ceq1aVerdict=verdict,
            eligible=hard_green,
            blockingDiagnostics=(),
            satisfiedDiagnostics=(),
            nonClaims=_GATE_NONCLAIMS,
        ),
        NextGateEligibility(
            gateId=FutureGate.CE_Q1B_VOICE,
            ceq1aVerdict=verdict,
            eligible=hard_green and not blockers_tuple,
            blockingDiagnostics=blockers_tuple,
            satisfiedDiagnostics=satisfied_tuple,
            nonClaims=_GATE_NONCLAIMS,
        ),
    )


@dataclass(frozen=True, slots=True)
class GateReport(_ClosedRecord):
    verdict: GateVerdict
    executionStarted: bool
    missingPrerequisites: tuple[str, ...]
    instrumentFaults: tuple[str, ...]
    requiredScores: tuple[ScoreRecord, ...]
    diagnosticScores: tuple[ScoreRecord, ...]
    nextGateEligibility: tuple[NextGateEligibility, ...]

    _KEYS = frozenset(
        {
            "verdict",
            "executionStarted",
            "missingPrerequisites",
            "instrumentFaults",
            "requiredScores",
            "diagnosticScores",
            "nextGateEligibility",
        }
    )

    def __post_init__(self) -> None:
        if type(self.verdict) is not GateVerdict:
            raise TypeError("GateReport.verdict must be GateVerdict")
        _boolean(self.executionStarted, "GateReport.executionStarted")
        for name in ("missingPrerequisites", "instrumentFaults"):
            values = getattr(self, name)
            if type(values) is not tuple or values != tuple(sorted(set(values))):
                raise ValueError(f"GateReport.{name} must be sorted and unique")
            if not all(type(item) is str and item for item in values):
                raise TypeError(f"GateReport.{name} must contain non-empty strings")
        _assert_tuple_of(self.requiredScores, ScoreRecord, "GateReport.requiredScores")
        _assert_tuple_of(self.diagnosticScores, ScoreRecord, "GateReport.diagnosticScores")
        _assert_tuple_of(
            self.nextGateEligibility,
            NextGateEligibility,
            "GateReport.nextGateEligibility",
        )
        if not self.executionStarted and (self.requiredScores or self.diagnosticScores):
            raise ValueError("preflight BLOCKED cannot carry execution scores")
        if any(score.promotionClass is not PromotionClass.REQUIRED for score in self.requiredScores):
            raise ValueError("requiredScores contains a non-required score")
        if any(
            score.promotionClass is not PromotionClass.DIAGNOSTIC
            for score in self.diagnosticScores
        ):
            raise ValueError("diagnosticScores contains a non-diagnostic score")
        required_ids = _score_identities(self.requiredScores)
        diagnostic_ids = _score_identities(self.diagnosticScores)
        if len(required_ids) != len(set(required_ids)) or len(diagnostic_ids) != len(
            set(diagnostic_ids)
        ):
            raise ValueError("GateReport score identities must be unique")
        if set(required_ids) & set(diagnostic_ids):
            raise ValueError("required and diagnostic score identities overlap")
        if self.requiredScores != tuple(sorted(self.requiredScores, key=lambda score: score.identity)):
            raise ValueError("requiredScores must be identity sorted")
        if self.diagnosticScores != tuple(
            sorted(self.diagnosticScores, key=lambda score: score.identity)
        ):
            raise ValueError("diagnosticScores must be identity sorted")
        diagnostic_refutations = _required_reasons(
            self.diagnosticScores, EvidenceResult.REFUTED
        )
        derived_faults = tuple(
            f"DIAGNOSTIC_CONTRACT_MISMATCH:{identity}" for identity in diagnostic_refutations
        )
        if any(fault.startswith("DIAGNOSTIC_CONTRACT_MISMATCH:") for fault in self.instrumentFaults):
            if tuple(fault for fault in self.instrumentFaults if fault.startswith("DIAGNOSTIC_CONTRACT_MISMATCH:")) != derived_faults:
                raise ValueError("GateReport diagnostic contract faults do not match scores")
        elif derived_faults:
            raise ValueError("GateReport omits its diagnostic contract faults")
        reducer_faults = self.instrumentFaults
        expected_verdict = classify_gate(
            execution_started=self.executionStarted,
            prerequisite_missing=bool(self.missingPrerequisites),
            instrument_faults=reducer_faults,
            required_refutations=_required_reasons(
                self.requiredScores, EvidenceResult.REFUTED
            ),
            missing_required_evidence=_required_reasons(
                self.requiredScores, EvidenceResult.UNVERIFIED
            ),
        )
        if self.verdict is not expected_verdict:
            raise ValueError("GateReport.verdict does not match reducer inputs")
        if (
            not self.requiredScores
            and self.verdict not in (GateVerdict.BLOCKED, GateVerdict.INSTRUMENT_FAILURE)
        ):
            raise ValueError("an empty required score set cannot issue a product verdict")
        expected_projection = _next_gate_projection(self.verdict, self.diagnosticScores)
        if self.nextGateEligibility != expected_projection:
            raise ValueError("GateReport.nextGateEligibility does not match diagnostics")

    @classmethod
    def from_scores(
        cls,
        *,
        required_scores: tuple[ScoreRecord, ...],
        diagnostic_scores: tuple[ScoreRecord, ...],
        execution_started: bool = True,
        missing_prerequisites: tuple[str, ...] = (),
        instrument_faults: tuple[str, ...] = (),
    ) -> GateReport:
        _assert_tuple_of(required_scores, ScoreRecord, "required_scores")
        _assert_tuple_of(diagnostic_scores, ScoreRecord, "diagnostic_scores")
        if type(missing_prerequisites) is not tuple or type(instrument_faults) is not tuple:
            raise TypeError("gate reason collections must be tuples")
        if missing_prerequisites != tuple(sorted(set(missing_prerequisites))):
            raise ValueError("missing_prerequisites must be sorted and unique")
        if instrument_faults != tuple(sorted(set(instrument_faults))):
            raise ValueError("instrument_faults must be sorted and unique")
        if not all(type(item) is str and item for item in missing_prerequisites):
            raise TypeError("missing_prerequisites must contain non-empty strings")
        if not all(type(item) is str and item for item in instrument_faults):
            raise TypeError("instrument_faults must contain non-empty strings")
        missing = missing_prerequisites
        faults = instrument_faults
        required = tuple(sorted(required_scores, key=lambda score: score.identity))
        diagnostics = tuple(sorted(diagnostic_scores, key=lambda score: score.identity))
        diagnostic_refutations = _required_reasons(diagnostics, EvidenceResult.REFUTED)
        derived_faults = tuple(
            f"DIAGNOSTIC_CONTRACT_MISMATCH:{identity}" for identity in diagnostic_refutations
        )
        reducer_faults = tuple(sorted(faults + derived_faults))
        verdict = classify_gate(
            execution_started=execution_started,
            prerequisite_missing=bool(missing),
            instrument_faults=reducer_faults,
            required_refutations=_required_reasons(required, EvidenceResult.REFUTED),
            missing_required_evidence=_required_reasons(required, EvidenceResult.UNVERIFIED),
        )
        return cls(
            verdict=verdict,
            executionStarted=execution_started,
            missingPrerequisites=missing,
            instrumentFaults=reducer_faults,
            requiredScores=required,
            diagnosticScores=diagnostics,
            nextGateEligibility=_next_gate_projection(verdict, diagnostics),
        )

    @classmethod
    def from_mapping(cls, value: object) -> GateReport:
        item = _require_mapping(value, cls._KEYS, "GateReport")
        return cls(
            verdict=_enum(item["verdict"], GateVerdict, "GateReport.verdict"),
            executionStarted=_boolean(item["executionStarted"], "GateReport.executionStarted"),
            missingPrerequisites=_strings(
                item["missingPrerequisites"], "GateReport.missingPrerequisites"
            ),
            instrumentFaults=_strings(item["instrumentFaults"], "GateReport.instrumentFaults"),
            requiredScores=_record_list(
                item["requiredScores"], ScoreRecord, "GateReport.requiredScores"
            ),
            diagnosticScores=_record_list(
                item["diagnosticScores"], ScoreRecord, "GateReport.diagnosticScores"
            ),
            nextGateEligibility=_record_list(
                item["nextGateEligibility"],
                NextGateEligibility,
                "GateReport.nextGateEligibility",
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "verdict": self.verdict.value,
            "executionStarted": self.executionStarted,
            "missingPrerequisites": list(self.missingPrerequisites),
            "instrumentFaults": list(self.instrumentFaults),
            "requiredScores": [score.to_mapping() for score in self.requiredScores],
            "diagnosticScores": [score.to_mapping() for score in self.diagnosticScores],
            "nextGateEligibility": [item.to_mapping() for item in self.nextGateEligibility],
        }


_CLOSED_RECORD_TYPES = (
    EffectAttempt,
    FactRecord,
    StateSnapshot,
    EventRecord,
    ExecutionResult,
    ScoreRecord,
    DiagnosticBlocker,
    SatisfiedDiagnostic,
    NextGateEligibility,
    GateReport,
)
