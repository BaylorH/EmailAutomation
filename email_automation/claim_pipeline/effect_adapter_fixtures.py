"""Sanitized fixtures for the pure disabled effect-adapter evaluator."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .contracts import (
    ActionPlan,
    ActionType,
    ActorRole,
    ApprovalClass,
    CampaignContract,
    Claim,
    ClaimModality,
    ClaimPolarity,
    ClaimPredicate,
    CompletenessState,
    ConversationState,
    ContractAuthority,
    DecisionSnapshot,
    EntityRef,
    EntityType,
    ExecutionScope,
    FitState,
    MarketState,
    PlannedAction,
)
from .effect_adapter import (
    ActionStateSnapshot,
    ApprovalGrant,
    DryRunReason,
    DryRunStatus,
    EffectAdapterRequest,
    evaluate_effect_plan,
)
from .validation import validate_action_plan


EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION = (
    "claim-pipeline-effect-adapter-fixtures-v1"
)

_ROOT_KEYS = frozenset({"schemaVersion", "cases"})
_CASE_KEYS = frozenset({"caseId", "actions", "mutations", "expectedReceipts"})
_ACTION_KEYS = frozenset({"type", "approval", "dependsOn"})
_ACTION_REQUIRED_KEYS = frozenset({"type", "approval"})
_RECEIPT_KEYS = frozenset({"action", "status", "reason"})
_CASE_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SIMPLE_MUTATIONS = frozenset(
    {
        "stale_snapshot",
        "stale_contract",
        "terminal_decision",
        "scope_row_mismatch",
        "reverse_request_collections",
    }
)
_INDEXED_MUTATIONS = frozenset(
    {
        "stale_prior_state",
        "committed",
        "approve",
        "approval_other_action",
        "approval_wrong_plan",
    }
)
_REASONS_BY_STATUS = MappingProxyType(
    {
        DryRunStatus.WOULD_APPLY: frozenset(
            {
                DryRunReason.ELIGIBLE_AUTOMATIC_ACTION,
                DryRunReason.ELIGIBLE_HUMAN_APPROVED_ACTION,
            }
        ),
        DryRunStatus.SKIPPED: frozenset(
            {
                DryRunReason.APPROVAL_REQUIRED,
                DryRunReason.IDEMPOTENCY_KEY_ALREADY_COMMITTED,
            }
        ),
        DryRunStatus.BLOCKED: frozenset(
            {
                DryRunReason.APPROVAL_SCOPE_MISMATCH,
                DryRunReason.UNSUPPORTED_ACTION_TYPE,
                DryRunReason.STALE_SNAPSHOT,
                DryRunReason.STALE_CONTRACT,
                DryRunReason.PRIOR_STATE_MISMATCH,
                DryRunReason.DEPENDENCY_BLOCKED,
                DryRunReason.TERMINAL_OUTBOUND_SUPPRESSED,
                DryRunReason.PLAN_CONTRACT_VIOLATION,
            }
        ),
    }
)


@dataclass(frozen=True)
class EffectAdapterFixtureValidationError(ValueError):
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class EffectAdapterFixtureCase:
    case_id: str
    actions: tuple[Mapping[str, Any], ...]
    mutations: tuple[str, ...]
    expected_receipts: tuple[Mapping[str, str], ...]


@dataclass(frozen=True)
class EffectAdapterFixtureCatalog:
    schema_version: str
    cases: tuple[EffectAdapterFixtureCase, ...]


@dataclass(frozen=True)
class EffectAdapterFixtureResult:
    case_id: str
    passed: bool
    receipt_id: str
    receipts: tuple[Mapping[str, str], ...]


def _fail(message: str) -> None:
    raise EffectAdapterFixtureValidationError(message)


def _exact_keys(
    value: Any,
    expected: frozenset[str],
    label: str,
    *,
    required: frozenset[str] | None = None,
) -> None:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    actual = set(value)
    required_keys = expected if required is None else required
    missing = required_keys - actual
    unknown = actual - expected
    if missing:
        _fail(f"{label} missing keys: {sorted(missing)}")
    if unknown:
        _fail(f"{label} has unknown keys: {sorted(unknown)}")


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be non-empty text")
    return value.strip()


def _enum_token(value: Any, enum_type: type, label: str):
    token = _required_text(value, label)
    try:
        return enum_type(token)
    except ValueError as exc:
        raise EffectAdapterFixtureValidationError(
            f"{label} has invalid value {token!r}"
        ) from exc


def _validate_dependencies(
    value: Any,
    *,
    action_count: int,
    sequence: int,
    label: str,
) -> tuple[int, ...]:
    if not isinstance(value, list):
        _fail(f"{label} dependsOn must be a list")
    dependencies = []
    for item in value:
        if type(item) is not int or not 1 <= item <= action_count:
            _fail(f"{label} dependsOn must contain valid action sequences")
        if item == sequence:
            _fail(f"{label} dependsOn cannot reference itself")
        dependencies.append(item)
    if len(dependencies) != len(set(dependencies)):
        _fail(f"{label} dependsOn contains duplicates")
    return tuple(dependencies)


def _dependencies_have_cycle(actions: tuple[Mapping[str, Any], ...]) -> bool:
    graph = {
        sequence: tuple(action.get("dependsOn", ()))
        for sequence, action in enumerate(actions, start=1)
    }
    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(sequence: int) -> bool:
        if sequence in visiting:
            return True
        if sequence in visited:
            return False
        visiting.add(sequence)
        if any(visit(dependency) for dependency in graph[sequence]):
            return True
        visiting.remove(sequence)
        visited.add(sequence)
        return False

    return any(visit(sequence) for sequence in graph)


def _validate_mutation(
    value: Any,
    *,
    action_count: int,
    label: str,
) -> str:
    token = _required_text(value, label)
    if token in _SIMPLE_MUTATIONS:
        return token
    prefix, separator, raw_index = token.partition(":")
    if (
        not separator
        or prefix not in _INDEXED_MUTATIONS
        or not raw_index.isdigit()
        or str(int(raw_index)) != raw_index
        or not 1 <= int(raw_index) <= action_count
    ):
        _fail(f"{label} has invalid mutation token {token!r}")
    return token


def _validate_expected_receipt(
    raw: Any,
    *,
    action_types: tuple[ActionType, ...],
    label: str,
) -> Mapping[str, str]:
    _exact_keys(raw, _RECEIPT_KEYS, label)
    action_signature = _required_text(raw["action"], f"{label} action")
    action_token, separator, sequence_token = action_signature.rpartition(":")
    if (
        not separator
        or not sequence_token.isdigit()
        or str(int(sequence_token)) != sequence_token
    ):
        _fail(f"{label} action is malformed")
    action_type = _enum_token(action_token, ActionType, f"{label} action type")
    sequence = int(sequence_token)
    if not 1 <= sequence <= len(action_types):
        _fail(f"{label} action sequence is invalid")
    if action_types[sequence - 1] is not action_type:
        _fail(f"{label} action does not match the planned action")

    status = _enum_token(raw["status"], DryRunStatus, f"{label} status")
    reason = _enum_token(raw["reason"], DryRunReason, f"{label} reason")
    if reason not in _REASONS_BY_STATUS[status]:
        _fail(f"{label} status/reason combination is invalid")
    return MappingProxyType(
        {
            "action": action_signature,
            "status": status.value,
            "reason": reason.value,
        }
    )


def _validate_case(raw: Any, index: int) -> EffectAdapterFixtureCase:
    label = f"case {index}"
    _exact_keys(raw, _CASE_KEYS, label)
    case_id = _required_text(raw["caseId"], f"{label} caseId")
    if not _CASE_ID_PATTERN.fullmatch(case_id):
        _fail(f"{label} caseId must be an opaque kebab-case token")

    raw_actions = raw["actions"]
    if not isinstance(raw_actions, list) or not raw_actions:
        _fail(f"case {case_id} actions must be a non-empty list")
    action_count = len(raw_actions)
    actions = []
    action_types = []
    for action_index, raw_action in enumerate(raw_actions, start=1):
        action_label = f"case {case_id} action {action_index}"
        _exact_keys(
            raw_action,
            _ACTION_KEYS,
            action_label,
            required=_ACTION_REQUIRED_KEYS,
        )
        action_type = _enum_token(
            raw_action["type"],
            ActionType,
            f"{action_label} action type",
        )
        approval = _enum_token(
            raw_action["approval"],
            ApprovalClass,
            f"{action_label} approval",
        )
        dependencies = _validate_dependencies(
            raw_action.get("dependsOn", []),
            action_count=action_count,
            sequence=action_index,
            label=action_label,
        )
        action_types.append(action_type)
        action = {
            "type": action_type.value,
            "approval": approval.value,
        }
        if "dependsOn" in raw_action:
            action["dependsOn"] = dependencies
        actions.append(MappingProxyType(action))
    frozen_actions = tuple(actions)
    if _dependencies_have_cycle(frozen_actions):
        _fail(f"case {case_id} dependsOn contains a cycle")

    raw_mutations = raw["mutations"]
    if not isinstance(raw_mutations, list):
        _fail(f"case {case_id} mutations must be a list")
    mutations = tuple(
        _validate_mutation(
            mutation,
            action_count=action_count,
            label=f"case {case_id} mutation {mutation_index}",
        )
        for mutation_index, mutation in enumerate(raw_mutations, start=1)
    )
    if len(mutations) != len(set(mutations)):
        _fail(f"case {case_id} mutations contains duplicates")

    raw_receipts = raw["expectedReceipts"]
    if not isinstance(raw_receipts, list):
        _fail(f"case {case_id} expectedReceipts must be a list")
    expected_receipts = tuple(
        _validate_expected_receipt(
            receipt,
            action_types=tuple(action_types),
            label=f"case {case_id} expected receipt {receipt_index}",
        )
        for receipt_index, receipt in enumerate(raw_receipts, start=1)
    )
    if len(expected_receipts) != action_count:
        _fail(f"case {case_id} must have exactly one expected receipt per action")
    signatures = tuple(receipt["action"] for receipt in expected_receipts)
    if len(signatures) != len(set(signatures)):
        _fail(f"case {case_id} expectedReceipts contains duplicate actions")
    if tuple(int(signature.rpartition(":")[2]) for signature in signatures) != tuple(
        range(1, action_count + 1)
    ):
        _fail(f"case {case_id} expectedReceipts must be in action sequence order")

    return EffectAdapterFixtureCase(
        case_id=case_id,
        actions=frozen_actions,
        mutations=mutations,
        expected_receipts=expected_receipts,
    )


def load_effect_adapter_fixture_catalog(
    path: Path,
) -> EffectAdapterFixtureCatalog:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EffectAdapterFixtureValidationError(
            f"effect-adapter fixture catalog cannot be read: {exc}"
        ) from exc
    _exact_keys(payload, _ROOT_KEYS, "effect-adapter fixture catalog")
    if payload["schemaVersion"] != EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION:
        _fail("unsupported effect-adapter fixture schemaVersion")
    if not isinstance(payload["cases"], list) or not payload["cases"]:
        _fail("effect-adapter fixture cases must be a non-empty list")
    cases = tuple(
        _validate_case(raw, index)
        for index, raw in enumerate(payload["cases"], start=1)
    )
    case_ids = tuple(case.case_id for case in cases)
    if len(case_ids) != len(set(case_ids)):
        _fail("effect-adapter fixture catalog has duplicate caseId")
    return EffectAdapterFixtureCatalog(
        schema_version=EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION,
        cases=cases,
    )


_ACTION_SHAPES = MappingProxyType(
    {
        ActionType.FACT_UPDATE: (
            ClaimPredicate.AVAILABILITY,
            "available",
            {"field": "availability", "value": "available", "confidence": 0.99},
            {"availability": "unknown"},
            "",
        ),
        ActionType.NOTE_APPEND: (
            ClaimPredicate.AVAILABILITY,
            "available",
            {"text": "note-opaque"},
            {"note": ""},
            "",
        ),
        ActionType.ROW_MOVE: (
            ClaimPredicate.AVAILABILITY,
            "available",
            {"destination": "destination-opaque"},
            {"rowState": "active"},
            "",
        ),
        ActionType.FOLLOWUP_FREEZE: (
            ClaimPredicate.OPT_OUT,
            True,
            {"reason": "contact_opt_out"},
            {"followUpStatus": "waiting"},
            "",
        ),
        ActionType.STATUS_TRANSITION: (
            ClaimPredicate.AVAILABILITY,
            "available",
            {"status": "waiting_user"},
            {"conversationState": "active"},
            "",
        ),
        ActionType.NOTIFICATION: (
            ClaimPredicate.AVAILABILITY,
            "available",
            {"message": "notification-opaque"},
            {},
            "",
        ),
        ActionType.LOI_REQUEST: (
            ClaimPredicate.AVAILABILITY,
            "available",
            {"notes": "loi-opaque", "terms": {}},
            {},
            "",
        ),
        ActionType.OUTBOUND_DRAFT: (
            ClaimPredicate.AVAILABILITY,
            "available",
            {"subject": "subject-opaque", "body": "body-opaque"},
            {},
            "recipient-opaque",
        ),
        ActionType.INFORMATION_REQUEST: (
            ClaimPredicate.INFORMATION_REQUEST,
            "information-opaque",
            {"notes": "information-opaque"},
            {},
            "",
        ),
    }
)


def _rebuild_action(
    action: PlannedAction,
    *,
    approval_class: ApprovalClass | None = None,
    dependencies: tuple[str, ...] | None = None,
) -> PlannedAction:
    rebuilt = PlannedAction.create(
        tenant_id=action.tenant_id,
        client_id=action.client_id,
        campaign_id=action.campaign_id,
        thread_id=action.thread_id,
        sheet_id=action.sheet_id,
        row_anchor=action.row_anchor,
        decision_id=action.decision_id,
        contract_id=action.contract_id,
        action_type=action.action_type,
        approval_class=approval_class or action.approval_class,
        target_entity_id=action.target_entity_id,
        contract_version=action.contract_version,
        snapshot_hash=action.snapshot_hash,
        source_claim_ids=action.source_claim_ids,
        operation_key=action.operation_key,
        expected_prior_state=action.expected_prior_state,
        dependencies=action.dependencies if dependencies is None else dependencies,
        sequence=action.sequence,
        recipient=action.recipient,
        payload=action.payload,
        reason=action.reason,
    )
    return replace(
        action,
        action_id=rebuilt.action_id,
        idempotency_key=rebuilt.idempotency_key,
        approval_class=rebuilt.approval_class,
        dependencies=rebuilt.dependencies,
    )


def _replace_plan_actions(
    plan: ActionPlan,
    actions: tuple[PlannedAction, ...],
) -> ActionPlan:
    identity_bound = ActionPlan.create(
        tenant_id=plan.tenant_id,
        client_id=plan.client_id,
        campaign_id=plan.campaign_id,
        decision_id=plan.decision_id,
        contract_id=plan.contract_id,
        contract_version=plan.contract_version,
        snapshot_hash=plan.snapshot_hash,
        actions=actions,
    )
    return replace(plan, plan_id=identity_bound.plan_id, actions=actions)


def _build_effect_adapter_request(
    case: EffectAdapterFixtureCase,
) -> EffectAdapterRequest:
    tenant_id = "tenant-opaque"
    client_id = "client-opaque"
    campaign_id = "campaign-opaque"
    snapshot_hash = "snapshot-opaque"
    contract = CampaignContract.create(
        tenant_id=tenant_id,
        client_id=client_id,
        campaign_id=campaign_id,
        version=1,
        transaction_types=("transaction-opaque",),
        required_fields=("availability",),
        source_authority=ContractAuthority.SETUP,
    )
    scope = ExecutionScope(
        tenant_id=tenant_id,
        client_id=client_id,
        campaign_id=campaign_id,
        thread_id="thread-opaque",
        sheet_id="sheet-opaque",
        row_anchor="row-opaque",
    )
    target = EntityRef.create(
        tenant_id=tenant_id,
        campaign_id=campaign_id,
        entity_type=EntityType.PROPERTY,
        label="entity-target-opaque",
    )
    entities = [target]
    if "reverse_request_collections" in case.mutations:
        entities.append(
            EntityRef.create(
                tenant_id=tenant_id,
                campaign_id=campaign_id,
                entity_type=EntityType.PROPERTY,
                label="entity-extra-opaque",
                relationship="context",
            )
        )

    claims = []
    action_shapes = []
    for sequence, raw_action in enumerate(case.actions, start=1):
        action_type = ActionType(raw_action["type"])
        predicate, claim_value, payload, prior_state, recipient = _ACTION_SHAPES[
            action_type
        ]
        claim = Claim.create(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            evidence_id=f"evidence-{sequence}-opaque",
            subject_entity_id=target.entity_id,
            predicate=predicate,
            value=claim_value,
            evidence_text=f"evidence-{sequence}-opaque",
            actor_role=ActorRole.BROKER,
            polarity=ClaimPolarity.POSITIVE,
            modality=ClaimModality.ASSERTED,
            confidence=0.99,
        )
        claims.append(claim)
        action_shapes.append(
            (
                action_type,
                ApprovalClass(raw_action["approval"]),
                payload,
                prior_state,
                recipient,
            )
        )

    terminal = "terminal_decision" in case.mutations
    reason_codes = ("contact_opt_out",) if terminal else ()
    decision = DecisionSnapshot.create(
        tenant_id=tenant_id,
        client_id=client_id,
        campaign_id=campaign_id,
        contract_id=contract.contract_id,
        entity_id=target.entity_id,
        contract_version=contract.version,
        snapshot_hash=snapshot_hash,
        market_state=MarketState.AVAILABLE,
        fit_state=FitState.VIABLE,
        completeness_state=CompletenessState.INCOMPLETE,
        conversation_state=(
            ConversationState.TERMINAL_INTENT
            if terminal
            else ConversationState.ACTIVE
        ),
        reason_codes=reason_codes,
        evidence_ids=tuple(claim.evidence_id for claim in claims),
    )

    actions = []
    for sequence, (raw_action, shape, claim) in enumerate(
        zip(case.actions, action_shapes, claims),
        start=1,
    ):
        action_type, requested_approval, payload, prior_state, recipient = shape
        approval = (
            ApprovalClass.AUTOMATIC
            if requested_approval is ApprovalClass.FORBIDDEN
            else requested_approval
        )
        preceding_dependencies = tuple(
            actions[dependency - 1].action_id
            for dependency in raw_action.get("dependsOn", ())
            if dependency < sequence
        )
        actions.append(
            PlannedAction.create(
                tenant_id=tenant_id,
                client_id=client_id,
                campaign_id=campaign_id,
                thread_id=scope.thread_id,
                sheet_id=scope.sheet_id,
                row_anchor=scope.row_anchor,
                decision_id=decision.decision_id,
                contract_id=contract.contract_id,
                action_type=action_type,
                approval_class=approval,
                target_entity_id=target.entity_id,
                contract_version=contract.version,
                snapshot_hash=snapshot_hash,
                source_claim_ids=(claim.claim_id,),
                operation_key=f"operation-{sequence}-opaque",
                expected_prior_state=prior_state,
                dependencies=preceding_dependencies,
                sequence=sequence,
                recipient=recipient,
                payload=payload,
                reason=(
                    "contact_opt_out"
                    if action_type is ActionType.FOLLOWUP_FREEZE
                    else f"reason-{sequence}-opaque"
                ),
            )
        )
    plan = ActionPlan.create(
        tenant_id=tenant_id,
        client_id=client_id,
        campaign_id=campaign_id,
        decision_id=decision.decision_id,
        contract_id=contract.contract_id,
        contract_version=contract.version,
        snapshot_hash=snapshot_hash,
        actions=tuple(actions),
    )
    authorized_recipients = tuple(
        action.recipient for action in actions if action.recipient
    )
    validate_action_plan(
        plan,
        decision,
        scope=scope,
        entities=entities,
        claims=claims,
        authorized_recipients=authorized_recipients,
    )

    malformed_actions = list(actions)
    for sequence, raw_action in enumerate(case.actions, start=1):
        requested_approval = ApprovalClass(raw_action["approval"])
        desired_dependencies = tuple(
            malformed_actions[dependency - 1].action_id
            for dependency in raw_action.get("dependsOn", ())
        )
        current = malformed_actions[sequence - 1]
        if (
            requested_approval is not current.approval_class
            or desired_dependencies != current.dependencies
        ):
            malformed_actions[sequence - 1] = _rebuild_action(
                current,
                approval_class=requested_approval,
                dependencies=desired_dependencies,
            )
    if tuple(malformed_actions) != tuple(actions):
        plan = _replace_plan_actions(plan, tuple(malformed_actions))
        actions = malformed_actions

    current_states = tuple(
        ActionStateSnapshot.create(
            action_id=action.action_id,
            values=action.expected_prior_state,
        )
        for action in actions
    )
    approval_grants: tuple[ApprovalGrant, ...] = ()
    committed_idempotency_keys: tuple[str, ...] = ()
    if "reverse_request_collections" in case.mutations:
        approval_grants = tuple(
            ApprovalGrant.create(
                tenant_id=tenant_id,
                plan_id=plan.plan_id,
                action_id=f"approval-action-{index}-opaque",
                snapshot_hash=snapshot_hash,
                approved_by=f"operator-{index}-opaque",
            )
            for index in (1, 2)
        )
        committed_idempotency_keys = (
            "committed-key-1-opaque",
            "committed-key-2-opaque",
        )

    request = EffectAdapterRequest.create(
        plan=plan,
        decision=decision,
        scope=scope,
        entities=tuple(entities),
        claims=tuple(claims),
        authorized_recipients=authorized_recipients,
        current_snapshot_hash=snapshot_hash,
        current_contract_id=contract.contract_id,
        current_contract_version=contract.version,
        current_states=current_states,
        approval_grants=approval_grants,
        committed_idempotency_keys=committed_idempotency_keys,
    )

    for mutation in case.mutations:
        prefix, _, raw_index = mutation.partition(":")
        index = int(raw_index) - 1 if raw_index else None
        if prefix == "stale_prior_state":
            states = list(request.current_states)
            states[index] = ActionStateSnapshot.create(
                action_id=states[index].action_id,
                values={"state": "stale-opaque"},
            )
            request = replace(request, current_states=tuple(states))
        elif prefix == "stale_snapshot":
            request = replace(request, current_snapshot_hash="snapshot-stale-opaque")
        elif prefix == "stale_contract":
            request = replace(request, current_contract_version=2)
        elif prefix == "committed":
            request = replace(
                request,
                committed_idempotency_keys=(
                    *request.committed_idempotency_keys,
                    request.plan.actions[index].idempotency_key,
                ),
            )
        elif prefix == "approve":
            action = request.plan.actions[index]
            grant = ApprovalGrant.create(
                tenant_id=request.plan.tenant_id,
                plan_id=request.plan.plan_id,
                action_id=action.action_id,
                snapshot_hash=request.current_snapshot_hash,
                approved_by="operator-opaque",
            )
            request = replace(
                request,
                approval_grants=(*request.approval_grants, grant),
            )
        elif prefix == "approval_other_action":
            grant = ApprovalGrant.create(
                tenant_id=request.plan.tenant_id,
                plan_id=request.plan.plan_id,
                action_id="action-other-opaque",
                snapshot_hash=request.current_snapshot_hash,
                approved_by="operator-other-opaque",
            )
            request = replace(
                request,
                approval_grants=(*request.approval_grants, grant),
            )
        elif prefix == "approval_wrong_plan":
            action = request.plan.actions[index]
            grant = ApprovalGrant.create(
                tenant_id=request.plan.tenant_id,
                plan_id="plan-wrong-opaque",
                action_id=action.action_id,
                snapshot_hash=request.current_snapshot_hash,
                approved_by="operator-wrong-opaque",
            )
            request = replace(
                request,
                approval_grants=(*request.approval_grants, grant),
            )
        elif prefix == "scope_row_mismatch":
            request = replace(
                request,
                scope=replace(request.scope, row_anchor="row-mismatch-opaque"),
            )
        elif prefix == "reverse_request_collections":
            request = replace(
                request,
                entities=tuple(reversed(request.entities)),
                claims=tuple(reversed(request.claims)),
                current_states=tuple(reversed(request.current_states)),
                approval_grants=tuple(reversed(request.approval_grants)),
                committed_idempotency_keys=tuple(
                    reversed(request.committed_idempotency_keys)
                ),
            )
    return request


def _receipt_projection(commit) -> tuple[Mapping[str, str], ...]:
    return tuple(
        MappingProxyType(
            {
                "action": f"{effect.action_type}:{effect.sequence}",
                "status": effect.status.value,
                "reason": effect.reason.value,
            }
        )
        for effect in commit.effects
    )


def _commit_bytes(commit) -> str:
    return json.dumps(
        commit.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def run_effect_adapter_fixture_case(
    case: EffectAdapterFixtureCase,
) -> EffectAdapterFixtureResult:
    request = _build_effect_adapter_request(case)
    stable = True
    if "reverse_request_collections" in case.mutations:
        forward_request = replace(
            request,
            entities=tuple(reversed(request.entities)),
            claims=tuple(reversed(request.claims)),
            current_states=tuple(reversed(request.current_states)),
            approval_grants=tuple(reversed(request.approval_grants)),
            committed_idempotency_keys=tuple(
                reversed(request.committed_idempotency_keys)
            ),
        )
        forward = evaluate_effect_plan(forward_request)
        repeated = evaluate_effect_plan(forward_request)
        commit = evaluate_effect_plan(request)
        stable = (
            forward.receipt_id == repeated.receipt_id == commit.receipt_id
            and _commit_bytes(forward)
            == _commit_bytes(repeated)
            == _commit_bytes(commit)
        )
    else:
        commit = evaluate_effect_plan(request)

    receipts = _receipt_projection(commit)
    expected = tuple(
        (
            receipt["action"],
            receipt["status"],
            receipt["reason"],
        )
        for receipt in case.expected_receipts
    )
    actual = tuple(
        (
            receipt["action"],
            receipt["status"],
            receipt["reason"],
        )
        for receipt in receipts
    )
    return EffectAdapterFixtureResult(
        case_id=case.case_id,
        passed=(
            actual == expected
            and len(receipts) == len(case.actions)
            and stable
        ),
        receipt_id=commit.receipt_id,
        receipts=receipts,
    )
