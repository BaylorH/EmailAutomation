"""Immutable contracts for the disabled dry-run effect adapter."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from .contracts import (
    ActionPlan,
    Claim,
    DecisionSnapshot,
    EntityRef,
    ExecutionScope,
)


class DryRunStatus(str, Enum):
    WOULD_APPLY = "would_apply"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class DryRunReason(str, Enum):
    ELIGIBLE_AUTOMATIC_ACTION = "eligible_automatic_action"
    ELIGIBLE_HUMAN_APPROVED_ACTION = "eligible_human_approved_action"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_SCOPE_MISMATCH = "approval_scope_mismatch"
    UNSUPPORTED_ACTION_TYPE = "unsupported_action_type"
    STALE_SNAPSHOT = "stale_snapshot"
    STALE_CONTRACT = "stale_contract"
    PRIOR_STATE_MISMATCH = "prior_state_mismatch"
    IDEMPOTENCY_KEY_ALREADY_COMMITTED = "idempotency_key_already_committed"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    TERMINAL_OUTBOUND_SUPPRESSED = "terminal_outbound_suppressed"
    PLAN_CONTRACT_VIOLATION = "plan_contract_violation"


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("Dry-run contract mapping keys must be strings")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, Enum):
        return value
    if isinstance(value, float) and not math.isfinite(value):
        raise TypeError("Dry-run contract numeric values must be finite")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(
        f"Dry-run contract values must be JSON-compatible, got {type(value).__name__}"
    )


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


def _require_text(label: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _string_tuple(label: str, values: Any) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise TypeError(f"{label} must be a sequence of strings")
    if not all(isinstance(value, str) for value in values):
        raise TypeError(f"{label} must contain only strings")
    return tuple(_require_text(f"{label} item", value) for value in values)


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return _json_ready(self)


@dataclass(frozen=True)
class ActionStateSnapshot(_JsonContract):
    action_id: str
    state_id: str
    values: Mapping[str, Any]

    def __post_init__(self) -> None:
        _require_text("action state action_id", self.action_id)
        _require_text("action state state_id", self.state_id)
        frozen_values = _freeze_json(self.values)
        if not isinstance(frozen_values, Mapping):
            raise TypeError("action state values must be a mapping")
        object.__setattr__(self, "values", frozen_values)
        expected_state_id = _stable_id(
            "state",
            {"action_id": self.action_id, "values": self.values},
        )
        if self.state_id != expected_state_id:
            raise ValueError("action state identity does not match its fields")

    @classmethod
    def create(
        cls,
        *,
        action_id: str,
        values: Mapping[str, Any],
    ) -> "ActionStateSnapshot":
        frozen_values = _freeze_json(values)
        if not isinstance(frozen_values, Mapping):
            raise TypeError("action state values must be a mapping")
        return cls(
            action_id=action_id,
            state_id=_stable_id(
                "state",
                {"action_id": action_id, "values": frozen_values},
            ),
            values=frozen_values,
        )


@dataclass(frozen=True)
class ApprovalGrant(_JsonContract):
    tenant_id: str
    plan_id: str
    action_id: str
    snapshot_hash: str
    approved_by: str
    grant_id: str

    def __post_init__(self) -> None:
        for label in (
            "tenant_id",
            "plan_id",
            "action_id",
            "snapshot_hash",
            "approved_by",
            "grant_id",
        ):
            _require_text(f"approval grant {label}", getattr(self, label))
        expected_grant_id = _stable_id(
            "grant",
            {
                "tenant_id": self.tenant_id,
                "plan_id": self.plan_id,
                "action_id": self.action_id,
                "snapshot_hash": self.snapshot_hash,
                "approved_by": self.approved_by,
            },
        )
        if self.grant_id != expected_grant_id:
            raise ValueError("grant identity does not match its fields")

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        plan_id: str,
        action_id: str,
        snapshot_hash: str,
        approved_by: str,
    ) -> "ApprovalGrant":
        identity = {
            "tenant_id": tenant_id,
            "plan_id": plan_id,
            "action_id": action_id,
            "snapshot_hash": snapshot_hash,
            "approved_by": approved_by,
        }
        return cls(grant_id=_stable_id("grant", identity), **identity)


@dataclass(frozen=True)
class EffectAdapterRequest(_JsonContract):
    plan: ActionPlan
    decision: DecisionSnapshot
    scope: ExecutionScope
    entities: tuple[EntityRef, ...]
    claims: tuple[Claim, ...]
    authorized_recipients: tuple[str, ...]
    current_snapshot_hash: str
    current_contract_id: str
    current_contract_version: int
    current_states: tuple[ActionStateSnapshot, ...]
    approval_grants: tuple[ApprovalGrant, ...]
    committed_idempotency_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.plan, ActionPlan):
            raise TypeError("effect adapter plan must be an ActionPlan")
        if not isinstance(self.decision, DecisionSnapshot):
            raise TypeError("effect adapter decision must be a DecisionSnapshot")
        if not isinstance(self.scope, ExecutionScope):
            raise TypeError("effect adapter scope must be an ExecutionScope")
        _require_text("current_snapshot_hash", self.current_snapshot_hash)
        _require_text("current_contract_id", self.current_contract_id)
        if self.current_contract_version < 1:
            raise ValueError("current_contract_version must be at least 1")

        object.__setattr__(self, "entities", tuple(self.entities))
        object.__setattr__(self, "claims", tuple(self.claims))
        object.__setattr__(
            self,
            "authorized_recipients",
            _string_tuple("authorized_recipients", self.authorized_recipients),
        )
        object.__setattr__(self, "current_states", tuple(self.current_states))
        object.__setattr__(self, "approval_grants", tuple(self.approval_grants))
        object.__setattr__(
            self,
            "committed_idempotency_keys",
            _string_tuple(
                "committed_idempotency_keys",
                self.committed_idempotency_keys,
            ),
        )

        if not all(isinstance(entity, EntityRef) for entity in self.entities):
            raise TypeError("effect adapter entities must be EntityRef values")
        if not all(isinstance(claim, Claim) for claim in self.claims):
            raise TypeError("effect adapter claims must be Claim values")
        if not all(
            isinstance(state, ActionStateSnapshot) for state in self.current_states
        ):
            raise TypeError("effect adapter current_states must be ActionStateSnapshot values")
        if not all(isinstance(grant, ApprovalGrant) for grant in self.approval_grants):
            raise TypeError("effect adapter approval_grants must be ApprovalGrant values")

        state_action_ids = tuple(state.action_id for state in self.current_states)
        if len(set(state_action_ids)) != len(state_action_ids):
            raise ValueError("duplicate action state action_id")
        grant_ids = tuple(grant.grant_id for grant in self.approval_grants)
        if len(set(grant_ids)) != len(grant_ids):
            raise ValueError("duplicate approval grant ID (duplicate grant ID)")
        if len(set(self.committed_idempotency_keys)) != len(
            self.committed_idempotency_keys
        ):
            raise ValueError("duplicate committed idempotency key")

        plan_action_ids = tuple(action.action_id for action in self.plan.actions)
        if len(state_action_ids) != len(plan_action_ids) or set(state_action_ids) != set(
            plan_action_ids
        ):
            raise ValueError(
                "effect adapter request requires exactly one state entry "
                "(one action state) per plan action"
            )

    @classmethod
    def create(
        cls,
        *,
        plan: ActionPlan,
        decision: DecisionSnapshot,
        scope: ExecutionScope,
        entities: tuple[EntityRef, ...],
        claims: tuple[Claim, ...],
        authorized_recipients: tuple[str, ...],
        current_snapshot_hash: str,
        current_contract_id: str,
        current_contract_version: int,
        current_states: tuple[ActionStateSnapshot, ...],
        approval_grants: tuple[ApprovalGrant, ...],
        committed_idempotency_keys: tuple[str, ...],
    ) -> "EffectAdapterRequest":
        return cls(
            plan=plan,
            decision=decision,
            scope=scope,
            entities=tuple(entities),
            claims=tuple(claims),
            authorized_recipients=tuple(authorized_recipients),
            current_snapshot_hash=current_snapshot_hash,
            current_contract_id=current_contract_id,
            current_contract_version=current_contract_version,
            current_states=tuple(current_states),
            approval_grants=tuple(approval_grants),
            committed_idempotency_keys=tuple(committed_idempotency_keys),
        )


@dataclass(frozen=True)
class DryRunEffectReceipt(_JsonContract):
    receipt_id: str
    plan_id: str
    action_id: str
    idempotency_key: str
    action_type: str
    sequence: int
    status: DryRunStatus
    reason: DryRunReason
    dependency_receipt_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for label in (
            "receipt_id",
            "plan_id",
            "action_id",
            "idempotency_key",
            "action_type",
        ):
            _require_text(f"dry-run effect {label}", getattr(self, label))
        if self.sequence < 1:
            raise ValueError("dry-run effect sequence must be at least 1")
        if not isinstance(self.status, DryRunStatus):
            raise TypeError("dry-run effect status must be a DryRunStatus")
        if not isinstance(self.reason, DryRunReason):
            raise TypeError("dry-run effect reason must be a DryRunReason")
        object.__setattr__(
            self,
            "dependency_receipt_ids",
            _string_tuple(
                "dry-run effect dependency_receipt_ids",
                self.dependency_receipt_ids,
            ),
        )
        expected_receipt_id = _stable_id("dry_effect", self._identity())
        if self.receipt_id != expected_receipt_id:
            raise ValueError("effect receipt identity does not match its fields")

    def _identity(self) -> Mapping[str, Any]:
        return {
            "plan_id": self.plan_id,
            "action_id": self.action_id,
            "idempotency_key": self.idempotency_key,
            "action_type": self.action_type,
            "sequence": self.sequence,
            "status": self.status,
            "reason": self.reason,
            "dependency_receipt_ids": self.dependency_receipt_ids,
        }

    @classmethod
    def create(
        cls,
        *,
        plan_id: str,
        action_id: str,
        idempotency_key: str,
        action_type: str,
        sequence: int,
        status: DryRunStatus,
        reason: DryRunReason,
        dependency_receipt_ids: tuple[str, ...],
    ) -> "DryRunEffectReceipt":
        identity = {
            "plan_id": plan_id,
            "action_id": action_id,
            "idempotency_key": idempotency_key,
            "action_type": action_type,
            "sequence": sequence,
            "status": status,
            "reason": reason,
            "dependency_receipt_ids": tuple(dependency_receipt_ids),
        }
        return cls(receipt_id=_stable_id("dry_effect", identity), **identity)


@dataclass(frozen=True)
class DryRunCommitReceipt(_JsonContract):
    receipt_id: str
    tenant_id: str
    plan_id: str
    decision_id: str
    contract_id: str
    contract_version: int
    snapshot_hash: str
    effects: tuple[DryRunEffectReceipt, ...]

    def __post_init__(self) -> None:
        for label in (
            "receipt_id",
            "tenant_id",
            "plan_id",
            "decision_id",
            "contract_id",
            "snapshot_hash",
        ):
            _require_text(f"dry-run commit {label}", getattr(self, label))
        if self.contract_version < 1:
            raise ValueError("dry-run commit contract_version must be at least 1")
        object.__setattr__(self, "effects", tuple(self.effects))
        if not all(isinstance(effect, DryRunEffectReceipt) for effect in self.effects):
            raise TypeError("dry-run commit effects must be DryRunEffectReceipt values")
        expected_receipt_id = _stable_id("dry_commit", self._identity())
        if self.receipt_id != expected_receipt_id:
            raise ValueError("commit receipt identity does not match its fields")

    def _identity(self) -> Mapping[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "plan_id": self.plan_id,
            "decision_id": self.decision_id,
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "snapshot_hash": self.snapshot_hash,
            "effect_receipt_ids": tuple(effect.receipt_id for effect in self.effects),
        }

    @property
    def status_counts(self) -> Mapping[str, int]:
        return MappingProxyType(
            {
                status.value: sum(effect.status is status for effect in self.effects)
                for status in DryRunStatus
            }
        )

    def to_dict(self) -> dict[str, Any]:
        value = super().to_dict()
        value["status_counts"] = dict(self.status_counts)
        return value

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        plan_id: str,
        decision_id: str,
        contract_id: str,
        contract_version: int,
        snapshot_hash: str,
        effects: tuple[DryRunEffectReceipt, ...],
    ) -> "DryRunCommitReceipt":
        frozen_effects = tuple(effects)
        identity = {
            "tenant_id": tenant_id,
            "plan_id": plan_id,
            "decision_id": decision_id,
            "contract_id": contract_id,
            "contract_version": contract_version,
            "snapshot_hash": snapshot_hash,
            "effect_receipt_ids": tuple(effect.receipt_id for effect in frozen_effects),
        }
        return cls(
            receipt_id=_stable_id("dry_commit", identity),
            tenant_id=tenant_id,
            plan_id=plan_id,
            decision_id=decision_id,
            contract_id=contract_id,
            contract_version=contract_version,
            snapshot_hash=snapshot_hash,
            effects=frozen_effects,
        )
