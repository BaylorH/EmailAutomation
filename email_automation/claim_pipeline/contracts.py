"""Immutable, JSON-safe contracts shared by claim-pipeline stages."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple


class EvidenceSource(str, Enum):
    FRESH_BODY = "fresh_body"
    QUOTED_BODY = "quoted_body"
    FORWARDED_BODY = "forwarded_body"
    SUBJECT = "subject"
    ATTACHMENT = "attachment"
    LINK = "link"
    SIGNATURE = "signature"
    MANUAL_OUTBOUND = "manual_outbound"


class EvidenceFreshness(str, Enum):
    FRESH = "fresh"
    QUOTED = "quoted"
    FORWARDED = "forwarded"
    HISTORICAL = "historical"


class Direction(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class ActorRole(str, Enum):
    BROKER = "broker"
    USER = "user"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class ContractAuthority(str, Enum):
    SETUP = "setup"
    USER = "user"
    USER_REVISION = "user_revision"
    SYSTEM_POLICY = "system_policy"


class EntityType(str, Enum):
    CAMPAIGN = "campaign"
    TARGET_PROPERTY = "target_property"
    PROPERTY = "property"
    BUILDING = "building"
    SUITE = "suite"
    CONTACT = "contact"
    ACTION = "action"


class ClaimPredicate(str, Enum):
    IDENTITY = "identity"
    AVAILABILITY = "availability"
    ASKING_STATUS = "asking_status"
    TRANSACTION_TYPE = "transaction_type"
    TOTAL_SF = "total_sf"
    OFFICE_SF = "office_sf"
    RENT = "rent"
    OPERATING_EXPENSES = "operating_expenses"
    POWER = "power"
    CLEAR_HEIGHT = "clear_height"
    DRIVE_INS = "drive_ins"
    DOCKS = "docks"
    OCCUPANCY_DATE = "occupancy_date"
    TERM = "term"
    REMEDIATION = "remediation"
    REFERRAL = "referral"
    CORRECTION = "correction"
    OPT_OUT = "opt_out"
    CALL_REQUEST = "call_request"
    TOUR_REQUEST = "tour_request"
    INFORMATION_REQUEST = "information_request"
    RETURN_DATE = "return_date"


class ClaimPolarity(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


class ClaimModality(str, Enum):
    ASSERTED = "asserted"
    CONDITIONAL = "conditional"
    POSSIBLE = "possible"
    REQUESTED = "requested"
    CORRECTED = "corrected"


class MarketState(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    CONDITIONAL = "conditional"
    UNKNOWN = "unknown"


class FitState(str, Enum):
    VIABLE = "viable"
    NONVIABLE = "nonviable"
    CONDITIONAL = "conditional"
    REVIEW = "review"


class CompletenessState(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"


class ConversationState(str, Enum):
    ACTIVE = "active"
    WAITING_BROKER = "waiting_broker"
    WAITING_USER = "waiting_user"
    REVIEW = "review"
    TERMINAL_INTENT = "terminal_intent"
    TERMINAL_PENDING_ACK = "terminal_pending_ack"
    TERMINAL = "terminal"


class ActionType(str, Enum):
    FACT_UPDATE = "fact_update"
    NOTE_APPEND = "note_append"
    ROW_MOVE = "row_move"
    ALTERNATE_PROPERTY_PROPOSAL = "alternate_property_proposal"
    FOLLOWUP_FREEZE = "followup_freeze"
    STATUS_TRANSITION = "status_transition"
    NOTIFICATION = "notification"
    REVIEW_ITEM = "review_item"
    RECIPIENT_CHANGE = "recipient_change"
    TOUR_REQUEST = "tour_request"
    CALL_REQUEST = "call_request"
    LOI_REQUEST = "loi_request"
    OUTBOUND_DRAFT = "outbound_draft"


class ApprovalClass(str, Enum):
    AUTOMATIC = "automatic"
    HUMAN_REQUIRED = "human_required"
    FORBIDDEN = "forbidden"


class EffectStatus(str, Enum):
    PENDING = "pending"
    APPLIED = "applied"
    FAILED = "failed"
    SKIPPED = "skipped"


def _require_text(label: str, value: Any) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{label} must be non-empty")
    return cleaned


def _require_enum(label: str, value: Any, enum_type: type[Enum]) -> None:
    if not isinstance(value, enum_type):
        raise ValueError(f"{label} must be a {enum_type.__name__} value")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("Contract mapping keys must be strings")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, Enum):
        return value
    if isinstance(value, float) and not math.isfinite(value):
        raise TypeError("Contract numeric values must be finite")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Contract values must be JSON-compatible, got {type(value).__name__}")


def _string_tuple(label: str, values: Any) -> Tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise TypeError(f"{label} must be a sequence of strings")
    if not all(isinstance(value, str) for value in values):
        raise TypeError(f"{label} must contain only strings")
    normalized = tuple(_require_text(f"{label} item", value) for value in values)
    return normalized


def _json_ready(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_ready(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_ready(item) for item in value]
    return value


def _stable_id(prefix: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _json_ready(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:24]}"


def _effect_destination(
    action_type: ActionType,
    payload: Mapping[str, Any],
) -> str:
    destination_key = {
        ActionType.FACT_UPDATE: "field",
        ActionType.ROW_MOVE: "destination",
        ActionType.STATUS_TRANSITION: "status",
    }.get(action_type)
    if not destination_key:
        return action_type.value
    return str(payload.get(destination_key, "") or "").strip().lower()


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return _json_ready(self)


@dataclass(frozen=True)
class Actor(_JsonContract):
    name: str
    email: str
    role: ActorRole

    def __post_init__(self) -> None:
        if not str(self.name or "").strip() and not str(self.email or "").strip():
            raise ValueError("actor must have a name or email")
        _require_enum("actor role", self.role, ActorRole)


@dataclass(frozen=True)
class EvidenceEnvelope(_JsonContract):
    tenant_id: str
    evidence_id: str
    message_id: str
    source_kind: EvidenceSource
    location: str
    content: str
    content_hash: str
    direction: Direction
    actor: Actor
    observed_at: str
    freshness: EvidenceFreshness
    parent_evidence_id: Optional[str] = None
    campaign_id: str = ""

    def __post_init__(self) -> None:
        for label in (
            "tenant_id",
            "evidence_id",
            "message_id",
            "location",
            "content",
            "observed_at",
        ):
            _require_text(label, getattr(self, label))
        _require_enum("evidence source_kind", self.source_kind, EvidenceSource)
        _require_enum("evidence direction", self.direction, Direction)
        _require_enum("evidence freshness", self.freshness, EvidenceFreshness)
        if not isinstance(self.campaign_id, str):
            raise TypeError("campaign_id must be a string")
        object.__setattr__(self, "campaign_id", self.campaign_id.strip())
        expected_content_hash = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.content_hash != expected_content_hash:
            raise ValueError("evidence content hash does not match content")
        identity = {
            "tenant_id": self.tenant_id,
            "message_id": self.message_id,
            "source_kind": self.source_kind,
            "location": self.location,
            "content_hash": self.content_hash,
            "parent_evidence_id": self.parent_evidence_id,
        }
        if self.campaign_id:
            identity["campaign_id"] = self.campaign_id
        expected_evidence_id = _stable_id("evidence", identity)
        if self.evidence_id != expected_evidence_id:
            raise ValueError("evidence identity does not match its source fields")

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        message_id: str,
        source_kind: EvidenceSource,
        location: str,
        content: str,
        direction: Direction,
        actor: Actor,
        observed_at: str,
        freshness: EvidenceFreshness,
        parent_evidence_id: Optional[str] = None,
        campaign_id: str = "",
    ) -> "EvidenceEnvelope":
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        identity = {
            "tenant_id": tenant_id,
            "message_id": message_id,
            "source_kind": source_kind,
            "location": location,
            "content_hash": content_hash,
            "parent_evidence_id": parent_evidence_id,
        }
        if str(campaign_id or "").strip():
            identity["campaign_id"] = str(campaign_id).strip()
        evidence_id = _stable_id("evidence", identity)
        return cls(
            tenant_id=tenant_id,
            evidence_id=evidence_id,
            message_id=message_id,
            source_kind=source_kind,
            location=location,
            content=content,
            content_hash=content_hash,
            direction=direction,
            actor=actor,
            observed_at=observed_at,
            freshness=freshness,
            parent_evidence_id=parent_evidence_id,
            campaign_id=campaign_id,
        )


@dataclass(frozen=True)
class EntityRef(_JsonContract):
    tenant_id: str
    campaign_id: str
    entity_id: str
    entity_type: EntityType
    label: str
    canonical_address: str = ""
    suite: str = ""
    relationship: str = "target"
    evidence_ids: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label in ("tenant_id", "campaign_id", "entity_id", "label", "relationship"):
            _require_text(label, getattr(self, label))
        _require_enum("entity type", self.entity_type, EntityType)
        object.__setattr__(
            self,
            "evidence_ids",
            _string_tuple("entity evidence_ids", self.evidence_ids),
        )
        expected_entity_id = _stable_id(
            "entity",
            {
                "tenant_id": self.tenant_id,
                "campaign_id": self.campaign_id,
                "entity_type": self.entity_type,
                "label": self.label.strip().lower(),
                "canonical_address": self.canonical_address.strip().lower(),
                "suite": self.suite.strip().lower(),
                "relationship": self.relationship,
            },
        )
        if self.entity_id != expected_entity_id:
            raise ValueError("entity identity does not match its fields")

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        campaign_id: str,
        entity_type: EntityType,
        label: str,
        canonical_address: str = "",
        suite: str = "",
        relationship: str = "target",
        evidence_ids: Tuple[str, ...] = (),
    ) -> "EntityRef":
        entity_id = _stable_id(
            "entity",
            {
                "tenant_id": tenant_id,
                "campaign_id": campaign_id,
                "entity_type": entity_type,
                "label": label.strip().lower(),
                "canonical_address": canonical_address.strip().lower(),
                "suite": suite.strip().lower(),
                "relationship": relationship,
            },
        )
        return cls(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            entity_id=entity_id,
            entity_type=entity_type,
            label=label,
            canonical_address=canonical_address,
            suite=suite,
            relationship=relationship,
            evidence_ids=tuple(evidence_ids),
        )


@dataclass(frozen=True)
class Claim(_JsonContract):
    tenant_id: str
    claim_id: str
    evidence_id: str
    subject_entity_id: str
    predicate: ClaimPredicate
    value: Any
    evidence_text: str
    actor_role: ActorRole
    polarity: ClaimPolarity
    modality: ClaimModality
    confidence: float
    unit: Optional[str] = None
    effective_at: Optional[str] = None
    supersedes_claim_id: Optional[str] = None
    campaign_id: str = ""
    actor_email: str = ""
    observed_at: str = ""

    def __post_init__(self) -> None:
        for label in ("tenant_id", "claim_id", "evidence_id", "subject_entity_id", "evidence_text"):
            _require_text(label, getattr(self, label))
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("claim confidence must be between 0 and 1")
        _require_enum("claim predicate", self.predicate, ClaimPredicate)
        _require_enum("claim actor_role", self.actor_role, ActorRole)
        _require_enum("claim polarity", self.polarity, ClaimPolarity)
        _require_enum("claim modality", self.modality, ClaimModality)
        for label in ("campaign_id", "actor_email", "observed_at"):
            if not isinstance(getattr(self, label), str):
                raise TypeError(f"claim {label} must be text")
        object.__setattr__(self, "campaign_id", self.campaign_id.strip())
        object.__setattr__(self, "actor_email", self.actor_email.strip().casefold())
        object.__setattr__(self, "observed_at", self.observed_at.strip())
        object.__setattr__(self, "value", _freeze_json(self.value))
        expected_claim_id = _stable_id(
            "claim",
            {
                "tenant_id": self.tenant_id,
                "evidence_id": self.evidence_id,
                "subject_entity_id": self.subject_entity_id,
                "predicate": self.predicate,
                "value": self.value,
                "evidence_text": self.evidence_text,
                "actor_role": self.actor_role,
                "polarity": self.polarity,
                "modality": self.modality,
                "unit": self.unit,
                "effective_at": self.effective_at,
                "supersedes_claim_id": self.supersedes_claim_id,
                "campaign_id": self.campaign_id,
                "actor_email": self.actor_email,
                "observed_at": self.observed_at,
            },
        )
        if self.claim_id != expected_claim_id:
            raise ValueError("claim identity does not match its fields")

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        evidence_id: str,
        subject_entity_id: str,
        predicate: ClaimPredicate,
        value: Any,
        evidence_text: str,
        actor_role: ActorRole,
        polarity: ClaimPolarity,
        modality: ClaimModality,
        confidence: float,
        unit: Optional[str] = None,
        effective_at: Optional[str] = None,
        supersedes_claim_id: Optional[str] = None,
        campaign_id: str = "",
        actor_email: str = "",
        observed_at: str = "",
    ) -> "Claim":
        frozen_value = _freeze_json(value)
        normalized_campaign = str(campaign_id or "").strip()
        normalized_actor_email = str(actor_email or "").strip().casefold()
        normalized_observed_at = str(observed_at or "").strip()
        claim_id = _stable_id(
            "claim",
            {
                "tenant_id": tenant_id,
                "evidence_id": evidence_id,
                "subject_entity_id": subject_entity_id,
                "predicate": predicate,
                "value": frozen_value,
                "evidence_text": evidence_text,
                "actor_role": actor_role,
                "polarity": polarity,
                "modality": modality,
                "unit": unit,
                "effective_at": effective_at,
                "supersedes_claim_id": supersedes_claim_id,
                "campaign_id": normalized_campaign,
                "actor_email": normalized_actor_email,
                "observed_at": normalized_observed_at,
            },
        )
        return cls(
            tenant_id=tenant_id,
            claim_id=claim_id,
            evidence_id=evidence_id,
            subject_entity_id=subject_entity_id,
            predicate=predicate,
            value=frozen_value,
            evidence_text=evidence_text,
            actor_role=actor_role,
            polarity=polarity,
            modality=modality,
            confidence=confidence,
            unit=unit,
            effective_at=effective_at,
            supersedes_claim_id=supersedes_claim_id,
            campaign_id=normalized_campaign,
            actor_email=normalized_actor_email,
            observed_at=normalized_observed_at,
        )


@dataclass(frozen=True)
class CampaignContract(_JsonContract):
    tenant_id: str
    client_id: str
    campaign_id: str
    contract_id: str
    version: int
    transaction_types: Tuple[str, ...]
    required_fields: Tuple[str, ...]
    hard_requirements: Mapping[str, Any]
    soft_preferences: Mapping[str, Any]
    source_authority: ContractAuthority

    def __post_init__(self) -> None:
        for label in (
            "tenant_id",
            "client_id",
            "campaign_id",
            "contract_id",
            "source_authority",
        ):
            _require_text(label, getattr(self, label))
        if self.version < 1:
            raise ValueError("contract version must be at least 1")
        _require_enum(
            "contract source authority",
            self.source_authority,
            ContractAuthority,
        )
        object.__setattr__(
            self,
            "transaction_types",
            _string_tuple("contract transaction_types", self.transaction_types),
        )
        object.__setattr__(
            self,
            "required_fields",
            _string_tuple("contract required_fields", self.required_fields),
        )
        object.__setattr__(self, "hard_requirements", _freeze_json(self.hard_requirements))
        object.__setattr__(self, "soft_preferences", _freeze_json(self.soft_preferences))
        expected_contract_id = _stable_id(
            "contract",
            {
                "tenant_id": self.tenant_id,
                "client_id": self.client_id,
                "campaign_id": self.campaign_id,
                "version": self.version,
                "transaction_types": self.transaction_types,
                "required_fields": self.required_fields,
                "hard_requirements": self.hard_requirements,
                "soft_preferences": self.soft_preferences,
                "source_authority": self.source_authority,
            },
        )
        if self.contract_id != expected_contract_id:
            raise ValueError("contract identity does not match its fields")

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        client_id: str,
        campaign_id: str,
        version: int,
        transaction_types: Tuple[str, ...] = (),
        required_fields: Tuple[str, ...] = (),
        hard_requirements: Optional[Mapping[str, Any]] = None,
        soft_preferences: Optional[Mapping[str, Any]] = None,
        source_authority: ContractAuthority | str = ContractAuthority.SETUP,
    ) -> "CampaignContract":
        try:
            authority = (
                source_authority
                if isinstance(source_authority, ContractAuthority)
                else ContractAuthority(str(source_authority or "").strip().lower())
            )
        except ValueError as exc:
            raise ValueError(
                "contract source authority must be setup, user, user_revision, "
                "or system_policy"
            ) from exc
        hard = _freeze_json(hard_requirements or {})
        soft = _freeze_json(soft_preferences or {})
        contract_id = _stable_id(
            "contract",
            {
                "tenant_id": tenant_id,
                "client_id": client_id,
                "campaign_id": campaign_id,
                "version": version,
                "transaction_types": transaction_types,
                "required_fields": required_fields,
                "hard_requirements": hard,
                "soft_preferences": soft,
                "source_authority": authority,
            },
        )
        return cls(
            tenant_id=tenant_id,
            client_id=client_id,
            campaign_id=campaign_id,
            contract_id=contract_id,
            version=version,
            transaction_types=tuple(transaction_types),
            required_fields=tuple(required_fields),
            hard_requirements=hard,
            soft_preferences=soft,
            source_authority=authority,
        )


@dataclass(frozen=True)
class DecisionSnapshot(_JsonContract):
    tenant_id: str
    client_id: str
    campaign_id: str
    decision_id: str
    contract_id: str
    entity_id: str
    contract_version: int
    snapshot_hash: str
    market_state: MarketState
    fit_state: FitState
    completeness_state: CompletenessState
    conversation_state: ConversationState
    reason_codes: Tuple[str, ...] = ()
    evidence_ids: Tuple[str, ...] = ()
    missing_fields: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label in (
            "tenant_id",
            "client_id",
            "campaign_id",
            "decision_id",
            "contract_id",
            "entity_id",
            "snapshot_hash",
        ):
            _require_text(label, getattr(self, label))
        if self.contract_version < 1:
            raise ValueError("decision contract_version must be at least 1")
        _require_enum("decision market_state", self.market_state, MarketState)
        _require_enum("decision fit_state", self.fit_state, FitState)
        _require_enum(
            "decision completeness_state",
            self.completeness_state,
            CompletenessState,
        )
        _require_enum(
            "decision conversation_state",
            self.conversation_state,
            ConversationState,
        )
        object.__setattr__(
            self,
            "reason_codes",
            _string_tuple("decision reason_codes", self.reason_codes),
        )
        object.__setattr__(
            self,
            "evidence_ids",
            _string_tuple("decision evidence_ids", self.evidence_ids),
        )
        object.__setattr__(
            self,
            "missing_fields",
            _string_tuple("decision missing_fields", self.missing_fields),
        )
        expected_decision_id = _stable_id(
            "decision",
            {
                "tenant_id": self.tenant_id,
                "client_id": self.client_id,
                "campaign_id": self.campaign_id,
                "contract_id": self.contract_id,
                "entity_id": self.entity_id,
                "contract_version": self.contract_version,
                "snapshot_hash": self.snapshot_hash,
                "market_state": self.market_state,
                "fit_state": self.fit_state,
                "completeness_state": self.completeness_state,
                "conversation_state": self.conversation_state,
                "reason_codes": self.reason_codes,
                "evidence_ids": self.evidence_ids,
                "missing_fields": self.missing_fields,
            },
        )
        if self.decision_id != expected_decision_id:
            raise ValueError("decision identity does not match its fields")

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        client_id: str,
        campaign_id: str,
        contract_id: str,
        entity_id: str,
        contract_version: int,
        snapshot_hash: str,
        market_state: MarketState,
        fit_state: FitState,
        completeness_state: CompletenessState,
        conversation_state: ConversationState,
        reason_codes: Tuple[str, ...] = (),
        evidence_ids: Tuple[str, ...] = (),
        missing_fields: Tuple[str, ...] = (),
    ) -> "DecisionSnapshot":
        decision_id = _stable_id(
            "decision",
            {
                "tenant_id": tenant_id,
                "client_id": client_id,
                "campaign_id": campaign_id,
                "contract_id": contract_id,
                "entity_id": entity_id,
                "contract_version": contract_version,
                "snapshot_hash": snapshot_hash,
                "market_state": market_state,
                "fit_state": fit_state,
                "completeness_state": completeness_state,
                "conversation_state": conversation_state,
                "reason_codes": reason_codes,
                "evidence_ids": evidence_ids,
                "missing_fields": missing_fields,
            },
        )
        return cls(
            tenant_id=tenant_id,
            client_id=client_id,
            campaign_id=campaign_id,
            decision_id=decision_id,
            contract_id=contract_id,
            entity_id=entity_id,
            contract_version=contract_version,
            snapshot_hash=snapshot_hash,
            market_state=market_state,
            fit_state=fit_state,
            completeness_state=completeness_state,
            conversation_state=conversation_state,
            reason_codes=tuple(reason_codes),
            evidence_ids=tuple(evidence_ids),
            missing_fields=tuple(missing_fields),
        )


@dataclass(frozen=True)
class ExecutionScope(_JsonContract):
    tenant_id: str
    client_id: str
    campaign_id: str
    thread_id: str
    sheet_id: str
    row_anchor: str

    def __post_init__(self) -> None:
        for label in (
            "tenant_id",
            "client_id",
            "campaign_id",
            "thread_id",
            "sheet_id",
            "row_anchor",
        ):
            _require_text(label, getattr(self, label))


@dataclass(frozen=True)
class PlannedAction(_JsonContract):
    tenant_id: str
    client_id: str
    campaign_id: str
    thread_id: str
    sheet_id: str
    row_anchor: str
    action_id: str
    idempotency_key: str
    decision_id: str
    contract_id: str
    action_type: ActionType
    approval_class: ApprovalClass
    target_entity_id: str
    contract_version: int
    snapshot_hash: str
    source_claim_ids: Tuple[str, ...]
    operation_key: str
    expected_prior_state: Mapping[str, Any]
    dependencies: Tuple[str, ...]
    sequence: int
    recipient: str
    payload: Mapping[str, Any]
    reason: str

    def __post_init__(self) -> None:
        for label in (
            "tenant_id",
            "client_id",
            "campaign_id",
            "thread_id",
            "sheet_id",
            "row_anchor",
            "action_id",
            "idempotency_key",
            "decision_id",
            "contract_id",
            "target_entity_id",
            "snapshot_hash",
            "operation_key",
            "reason",
        ):
            _require_text(label, getattr(self, label))
        if self.contract_version < 1:
            raise ValueError("planned action contract_version must be at least 1")
        if self.sequence < 1:
            raise ValueError("planned action sequence must be at least 1")
        _require_enum("planned action type", self.action_type, ActionType)
        _require_enum(
            "planned action approval_class",
            self.approval_class,
            ApprovalClass,
        )
        object.__setattr__(
            self,
            "source_claim_ids",
            _string_tuple("planned action source_claim_ids", self.source_claim_ids),
        )
        object.__setattr__(
            self,
            "dependencies",
            _string_tuple("planned action dependencies", self.dependencies),
        )
        object.__setattr__(
            self,
            "expected_prior_state",
            _freeze_json(self.expected_prior_state),
        )
        object.__setattr__(self, "payload", _freeze_json(self.payload))
        action_identity = {
            "tenant_id": self.tenant_id,
            "client_id": self.client_id,
            "campaign_id": self.campaign_id,
            "thread_id": self.thread_id,
            "sheet_id": self.sheet_id,
            "row_anchor": self.row_anchor,
            "decision_id": self.decision_id,
            "contract_id": self.contract_id,
            "action_type": self.action_type,
            "approval_class": self.approval_class,
            "target_entity_id": self.target_entity_id,
            "contract_version": self.contract_version,
            "snapshot_hash": self.snapshot_hash,
            "source_claim_ids": self.source_claim_ids,
            "operation_key": self.operation_key,
            "expected_prior_state": self.expected_prior_state,
            "dependencies": self.dependencies,
            "sequence": self.sequence,
            "recipient": self.recipient.strip().lower(),
            "payload": self.payload,
            "reason": self.reason,
        }
        effect_identity = {
            "tenant_id": self.tenant_id,
            "client_id": self.client_id,
            "campaign_id": self.campaign_id,
            "thread_id": self.thread_id,
            "sheet_id": self.sheet_id,
            "row_anchor": self.row_anchor,
            "action_type": self.action_type,
            "target_entity_id": self.target_entity_id,
            "source_claim_ids": tuple(sorted(self.source_claim_ids)),
            "destination": _effect_destination(self.action_type, self.payload),
            "recipient": self.recipient.strip().lower(),
        }
        if self.action_id != _stable_id("action", action_identity):
            raise ValueError("action identity does not match its fields")
        if self.idempotency_key != _stable_id("effect", effect_identity):
            raise ValueError("effect identity does not match its action scope")

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        client_id: str,
        campaign_id: str,
        thread_id: str,
        sheet_id: str,
        row_anchor: str,
        decision_id: str,
        contract_id: str,
        action_type: ActionType,
        approval_class: ApprovalClass,
        target_entity_id: str,
        contract_version: int,
        snapshot_hash: str,
        source_claim_ids: Tuple[str, ...],
        operation_key: str,
        expected_prior_state: Mapping[str, Any],
        dependencies: Tuple[str, ...],
        sequence: int,
        recipient: str,
        payload: Mapping[str, Any],
        reason: str,
    ) -> "PlannedAction":
        frozen_prior_state = _freeze_json(expected_prior_state)
        frozen_payload = _freeze_json(payload)
        action_identity = {
            "tenant_id": tenant_id,
            "client_id": client_id,
            "campaign_id": campaign_id,
            "thread_id": thread_id,
            "sheet_id": sheet_id,
            "row_anchor": row_anchor,
            "decision_id": decision_id,
            "contract_id": contract_id,
            "action_type": action_type,
            "approval_class": approval_class,
            "target_entity_id": target_entity_id,
            "contract_version": contract_version,
            "snapshot_hash": snapshot_hash,
            "source_claim_ids": tuple(source_claim_ids),
            "operation_key": operation_key,
            "expected_prior_state": frozen_prior_state,
            "dependencies": tuple(dependencies),
            "sequence": sequence,
            "recipient": recipient.strip().lower(),
            "payload": frozen_payload,
            "reason": reason,
        }
        effect_identity = {
            "tenant_id": tenant_id,
            "client_id": client_id,
            "campaign_id": campaign_id,
            "thread_id": thread_id,
            "sheet_id": sheet_id,
            "row_anchor": row_anchor,
            "action_type": action_type,
            "target_entity_id": target_entity_id,
            "source_claim_ids": tuple(sorted(source_claim_ids)),
            "destination": _effect_destination(action_type, frozen_payload),
            "recipient": recipient.strip().lower(),
        }
        return cls(
            tenant_id=tenant_id,
            client_id=client_id,
            campaign_id=campaign_id,
            thread_id=thread_id,
            sheet_id=sheet_id,
            row_anchor=row_anchor,
            action_id=_stable_id("action", action_identity),
            idempotency_key=_stable_id("effect", effect_identity),
            decision_id=decision_id,
            contract_id=contract_id,
            action_type=action_type,
            approval_class=approval_class,
            target_entity_id=target_entity_id,
            contract_version=contract_version,
            snapshot_hash=snapshot_hash,
            source_claim_ids=tuple(source_claim_ids),
            operation_key=operation_key,
            expected_prior_state=frozen_prior_state,
            dependencies=tuple(dependencies),
            sequence=sequence,
            recipient=recipient.strip().lower(),
            payload=frozen_payload,
            reason=reason,
        )


@dataclass(frozen=True)
class ActionPlan(_JsonContract):
    tenant_id: str
    client_id: str
    campaign_id: str
    plan_id: str
    decision_id: str
    contract_id: str
    contract_version: int
    snapshot_hash: str
    actions: Tuple[PlannedAction, ...]

    def __post_init__(self) -> None:
        for label in (
            "tenant_id",
            "client_id",
            "campaign_id",
            "plan_id",
            "decision_id",
            "contract_id",
            "snapshot_hash",
        ):
            _require_text(label, getattr(self, label))
        if self.contract_version < 1:
            raise ValueError("action plan contract_version must be at least 1")
        object.__setattr__(self, "actions", tuple(self.actions))
        if not all(isinstance(action, PlannedAction) for action in self.actions):
            raise TypeError("action plan actions must be PlannedAction values")
        expected_plan_id = _stable_id(
            "plan",
            {
                "tenant_id": self.tenant_id,
                "client_id": self.client_id,
                "campaign_id": self.campaign_id,
                "decision_id": self.decision_id,
                "contract_id": self.contract_id,
                "contract_version": self.contract_version,
                "snapshot_hash": self.snapshot_hash,
                "action_ids": tuple(action.action_id for action in self.actions),
            },
        )
        if self.plan_id != expected_plan_id:
            raise ValueError("plan identity does not match its fields")

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        client_id: str,
        campaign_id: str,
        decision_id: str,
        contract_id: str,
        contract_version: int,
        snapshot_hash: str,
        actions: Tuple[PlannedAction, ...],
    ) -> "ActionPlan":
        plan_id = _stable_id(
            "plan",
            {
                "tenant_id": tenant_id,
                "client_id": client_id,
                "campaign_id": campaign_id,
                "decision_id": decision_id,
                "contract_id": contract_id,
                "contract_version": contract_version,
                "snapshot_hash": snapshot_hash,
                "action_ids": tuple(action.action_id for action in actions),
            },
        )
        return cls(
            tenant_id=tenant_id,
            client_id=client_id,
            campaign_id=campaign_id,
            plan_id=plan_id,
            decision_id=decision_id,
            contract_id=contract_id,
            contract_version=contract_version,
            snapshot_hash=snapshot_hash,
            actions=tuple(actions),
        )


@dataclass(frozen=True)
class EffectReceipt(_JsonContract):
    action_id: str
    status: EffectStatus
    attempt: int
    external_id: Optional[str] = None
    error: Optional[str] = None

    def __post_init__(self) -> None:
        _require_text("action_id", self.action_id)
        _require_enum("effect status", self.status, EffectStatus)
        if self.attempt < 1:
            raise ValueError("effect receipt attempt must be at least 1")


@dataclass(frozen=True)
class CommitReceipt(_JsonContract):
    tenant_id: str
    plan_id: str
    effects: Tuple[EffectReceipt, ...]
    completed_at: Optional[str] = None

    def __post_init__(self) -> None:
        _require_text("tenant_id", self.tenant_id)
        _require_text("plan_id", self.plan_id)
        object.__setattr__(self, "effects", tuple(self.effects))
