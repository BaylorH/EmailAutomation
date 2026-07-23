"""No-effect composition of pinned claim proposals and deterministic policy."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .claim_fixtures import ClaimFixtureCatalog
from .contracts import (
    ActionType,
    ActorRole,
    CampaignContract,
    Claim,
    ClaimPredicate,
    ContractAuthority,
    EntityRef,
    EntityType,
    ExecutionScope,
)
from .entities import resolve_entities
from .evidence import normalize_message_evidence
from .extraction import build_claim_extraction_request, extract_claims
from .interpretation_fixtures import InterpretationFixtureCatalog
from .policy import PolicyEvaluationRequest, evaluate_policy
from .provider_policy_fixtures import (
    ProviderPolicyFixtureCase,
    ProviderPolicyFixtureCatalog,
)
from .provider_quality_fixtures import (
    ProviderQualityFixtureCatalog,
    ProviderQualityFixtureCase,
)
from .replay import (
    MAX_REPLAY_REPEATS,
    RECORDED_MODEL_ID,
    RECORDED_PROMPT_HASH,
    RECORDED_PROMPT_ID,
    RECORDED_PROVIDER_ID,
    ProposalAdapter,
    ProposalResponse,
    ProposalUsage,
    ProviderTelemetry,
    ReplayIdentity,
    _prior_claim_from_fixture,
    run_claim_replay,
)


PROVIDER_POLICY_SHADOW_PROFILE = "provider-policy-shadow-v1"


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _selected_provider_quality_catalog(
    provider_quality_catalog: ProviderQualityFixtureCatalog,
    provider_policy_catalog: ProviderPolicyFixtureCatalog,
) -> ProviderQualityFixtureCatalog:
    provider_by_id = {
        case.case_id: case for case in provider_quality_catalog.cases
    }
    selected_ids = tuple(
        sorted(case.provider_case_id for case in provider_policy_catalog.cases)
    )
    selection_hash = _digest(
        {
            "profile": PROVIDER_POLICY_SHADOW_PROFILE,
            "providerQualityFixtureHash": provider_quality_catalog.manifest_hash,
            "providerPolicyFixtureHash": provider_policy_catalog.manifest_hash,
            "providerCaseIds": selected_ids,
        }
    )
    return ProviderQualityFixtureCatalog(
        schema_version=provider_quality_catalog.schema_version,
        catalog_id="provider-policy-selection-v1",
        claim_fixture_hash=provider_quality_catalog.claim_fixture_hash,
        manifest_hash=selection_hash,
        cases=tuple(provider_by_id[case_id] for case_id in selected_ids),
    )


def select_provider_policy_cases(
    catalog: ProviderPolicyFixtureCatalog,
    *,
    case_ids: tuple[str, ...],
) -> ProviderPolicyFixtureCatalog:
    """Select a strict case subset and bind the selection into a fresh hash."""

    if not case_ids or len(case_ids) != len(set(case_ids)):
        raise ValueError("provider-policy case selection must be non-empty and unique")
    by_id = {case.case_id: case for case in catalog.cases}
    unknown = set(case_ids) - set(by_id)
    if unknown:
        raise ValueError("provider-policy case selection contains an unknown case")
    ordered_ids = tuple(sorted(case_ids))
    manifest_hash = _digest(
        {
            "profile": PROVIDER_POLICY_SHADOW_PROFILE,
            "sourceFixtureHash": catalog.manifest_hash,
            "caseIds": ordered_ids,
        }
    )
    selected = tuple(by_id[case_id] for case_id in ordered_ids)
    return ProviderPolicyFixtureCatalog(
        schema_version=catalog.schema_version,
        catalog_id="provider-policy-selection-v1",
        provider_quality_fixture_hash=catalog.provider_quality_fixture_hash,
        cases=selected,
        covered_dimensions=frozenset(
            dimension for case in selected for dimension in case.dimensions
        ),
        manifest_hash=manifest_hash,
    )


class ProviderBudgetExceeded(ValueError):
    """Raised before a provider call that would exceed a hard reservation cap."""


@dataclass(frozen=True)
class ProviderBudgetLimits:
    max_calls: int
    max_reserved_tokens: int
    max_reserved_cost_microusd: int

    def __post_init__(self) -> None:
        for field in (
            "max_calls",
            "max_reserved_tokens",
            "max_reserved_cost_microusd",
        ):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field} must be a nonnegative integer")


@dataclass(frozen=True)
class ProviderReservationSnapshot:
    reserved_calls: int = 0
    reserved_tokens: int = 0
    reserved_cost_microusd: int = 0


class BudgetedProviderTransport:
    """Reserve conservative maximum provider exposure before each invocation."""

    def __init__(
        self,
        delegate: Any,
        *,
        limits: ProviderBudgetLimits,
        max_output_tokens: int,
        input_token_overhead: int,
        input_cost_microusd_per_million: int,
        output_cost_microusd_per_million: int,
    ):
        self.provider_id = getattr(delegate, "provider_id", "")
        self.model_id = getattr(delegate, "model_id", "")
        if not self.provider_id or not self.model_id:
            raise ValueError("budgeted provider transport identity is incomplete")
        for label, value in (
            ("max_output_tokens", max_output_tokens),
            ("input_cost_microusd_per_million", input_cost_microusd_per_million),
            ("output_cost_microusd_per_million", output_cost_microusd_per_million),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{label} must be positive")
        if (
            not isinstance(input_token_overhead, int)
            or isinstance(input_token_overhead, bool)
            or input_token_overhead < 0
        ):
            raise ValueError("input_token_overhead must be nonnegative")
        if not callable(getattr(delegate, "invoke", None)) or not callable(
            getattr(delegate, "snapshot", None)
        ):
            raise ValueError("budgeted provider transport is incomplete")
        self._delegate = delegate
        self.limits = limits
        self._max_output_tokens = max_output_tokens
        self._input_token_overhead = input_token_overhead
        self._input_rate = input_cost_microusd_per_million
        self._output_rate = output_cost_microusd_per_million
        self._reserved_calls = 0
        self._reserved_tokens = 0
        self._reserved_cost_microusd = 0

    def snapshot(self) -> Any:
        return self._delegate.snapshot()

    def reservation_snapshot(self) -> ProviderReservationSnapshot:
        return ProviderReservationSnapshot(
            reserved_calls=self._reserved_calls,
            reserved_tokens=self._reserved_tokens,
            reserved_cost_microusd=self._reserved_cost_microusd,
        )

    def invoke(
        self,
        *,
        case_id: str,
        instructions: str,
        payload: str,
    ) -> Any:
        max_input_tokens = (
            len(f"{instructions}{payload}".encode("utf-8"))
            + self._input_token_overhead
        )
        reserved_tokens = max_input_tokens + self._max_output_tokens
        reserved_cost = math.ceil(
            max_input_tokens * self._input_rate / 1_000_000
        ) + math.ceil(
            self._max_output_tokens * self._output_rate / 1_000_000
        )
        next_calls = self._reserved_calls + 1
        next_tokens = self._reserved_tokens + reserved_tokens
        next_cost = self._reserved_cost_microusd + reserved_cost
        if next_calls > self.limits.max_calls:
            raise ProviderBudgetExceeded("provider call cap would be exceeded")
        if next_tokens > self.limits.max_reserved_tokens:
            raise ProviderBudgetExceeded("provider token reservation cap would be exceeded")
        if next_cost > self.limits.max_reserved_cost_microusd:
            raise ProviderBudgetExceeded("provider cost reservation cap would be exceeded")
        self._reserved_calls = next_calls
        self._reserved_tokens = next_tokens
        self._reserved_cost_microusd = next_cost
        return self._delegate.invoke(
            case_id=case_id,
            instructions=instructions,
            payload=payload,
        )


class RecordedProviderQualityProposalAdapter:
    """Materialize the complete accepted provider oracle without a provider call."""

    provider_id = RECORDED_PROVIDER_ID
    model_id = RECORDED_MODEL_ID
    prompt_id = RECORDED_PROMPT_ID
    prompt_hash = RECORDED_PROMPT_HASH

    def __init__(
        self,
        provider_quality_catalog: ProviderQualityFixtureCatalog,
        claim_catalog: ClaimFixtureCatalog,
    ):
        self._provider_cases = {
            case.case_id: case for case in provider_quality_catalog.cases
        }
        self._claim_cases = {case.case_id: case for case in claim_catalog.cases}

    def propose(
        self,
        *,
        case_id: str,
        request: Any,
        evidence: tuple[Any, ...],
        entities: tuple[EntityRef, ...],
    ) -> ProposalResponse:
        provider_case = self._provider_cases[case_id]
        entity_by_selector = {
            (item.relationship, item.suite, item.canonical_address): item
            for item in entities
        }
        materialized: dict[str, dict[str, Any]] = {}
        for source_case_id in provider_case.source_claim_case_ids:
            source = self._claim_cases[source_case_id]
            for index in source.expected["acceptedClaimIndexes"]:
                raw = source.claims[index]
                subject = raw["subject"]
                entity = entity_by_selector[
                    (
                        subject["relationship"],
                        subject["suite"],
                        subject["canonicalAddress"],
                    )
                ]
                claim = {
                    key: _plain(value)
                    for key, value in raw.items()
                    if key not in {"evidenceIndex", "subject"}
                }
                supersedes = claim["supersedesClaimId"]
                if isinstance(supersedes, str) and supersedes.startswith("prior:"):
                    claim["supersedesClaimId"] = request.prior_claims[
                        int(supersedes.removeprefix("prior:"))
                    ].claim_id
                claim.update(
                    {
                        "evidenceId": evidence[raw["evidenceIndex"]].evidence_id,
                        "subjectEntityId": entity.entity_id,
                    }
                )
                materialized[_canonical_json(claim)] = claim
        reviews = [
            {
                "evidenceId": evidence[item.evidence_index].evidence_id,
                "reason": item.category,
            }
            for item in provider_case.expected_reviews
        ]
        return ProposalResponse(
            model_output={
                "claims": list(materialized.values()),
                "review": reviews,
            },
            usage=ProposalUsage(provider_billed=False),
        )


class _CapturingProposalAdapter:
    def __init__(self, delegate: ProposalAdapter):
        self.provider_id = delegate.provider_id
        self.model_id = delegate.model_id
        self.prompt_id = delegate.prompt_id
        self.prompt_hash = delegate.prompt_hash
        self._delegate = delegate
        self.responses: dict[str, list[ProposalResponse]] = {}

    def propose(self, **kwargs: Any) -> ProposalResponse:
        response = self._delegate.propose(**kwargs)
        if isinstance(response, ProposalResponse):
            self.responses.setdefault(kwargs["case_id"], []).append(response)
        return response


@dataclass(frozen=True)
class ProviderPolicyShadowIdentity:
    code_revision: str
    source_tree_hash: str
    source_tree_dirty: bool
    python_version: str
    dependency_lock_hash: str
    interpretation_fixture_hash: str
    claim_fixture_hash: str
    provider_quality_fixture_hash: str
    provider_policy_fixture_hash: str
    extraction_schema_version: int
    provider_id: str
    model_id: str
    prompt_id: str
    prompt_hash: str
    call_mode: str
    max_provider_calls: int
    max_reserved_tokens: int
    max_reserved_cost_microusd: int
    repeats: int
    case_count: int
    planned_calls: int
    identity_id: str

    @classmethod
    def create(cls, **values: Any) -> "ProviderPolicyShadowIdentity":
        required = {
            "code_revision",
            "source_tree_hash",
            "source_tree_dirty",
            "python_version",
            "dependency_lock_hash",
            "interpretation_fixture_hash",
            "claim_fixture_hash",
            "provider_quality_fixture_hash",
            "provider_policy_fixture_hash",
            "extraction_schema_version",
            "provider_id",
            "model_id",
            "prompt_id",
            "prompt_hash",
            "repeats",
            "case_count",
        }
        optional = {
            "call_mode",
            "max_provider_calls",
            "max_reserved_tokens",
            "max_reserved_cost_microusd",
        }
        if set(values) - (required | optional) or required - set(values):
            raise ValueError("provider-policy identity fields are incomplete")
        for field in (
            "source_tree_hash",
            "dependency_lock_hash",
            "interpretation_fixture_hash",
            "claim_fixture_hash",
            "provider_quality_fixture_hash",
            "provider_policy_fixture_hash",
            "prompt_hash",
        ):
            value = values[field]
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{field} must be a SHA-256 hash")
        if not isinstance(values["source_tree_dirty"], bool):
            raise ValueError("source_tree_dirty must be boolean")
        repeats = values["repeats"]
        case_count = values["case_count"]
        if (
            not isinstance(repeats, int)
            or isinstance(repeats, bool)
            or repeats < 1
            or repeats > MAX_REPLAY_REPEATS
        ):
            raise ValueError("repeats is outside the replay bound")
        if (
            not isinstance(case_count, int)
            or isinstance(case_count, bool)
            or case_count < 1
        ):
            raise ValueError("case_count must be positive")
        planned_calls = repeats * case_count
        normalized = dict(values)
        normalized.setdefault("call_mode", "recorded")
        normalized.setdefault("max_provider_calls", planned_calls)
        normalized.setdefault("max_reserved_tokens", 0)
        normalized.setdefault("max_reserved_cost_microusd", 0)
        if normalized["call_mode"] not in {"recorded", "smoke", "final"}:
            raise ValueError("call_mode is unsupported")
        for field in (
            "max_provider_calls",
            "max_reserved_tokens",
            "max_reserved_cost_microusd",
        ):
            value = normalized[field]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field} must be nonnegative")
        if normalized["max_provider_calls"] < planned_calls:
            raise ValueError("provider call cap is below the planned call count")
        normalized["planned_calls"] = planned_calls
        normalized["identity_id"] = _digest(
            {"profile": PROVIDER_POLICY_SHADOW_PROFILE, **normalized}
        )[:32]
        return cls(**normalized)

    def to_dict(self) -> dict[str, Any]:
        return {
            "identityId": self.identity_id,
            "profile": PROVIDER_POLICY_SHADOW_PROFILE,
            "codeRevision": self.code_revision,
            "sourceTreeHash": self.source_tree_hash,
            "sourceTreeDirty": self.source_tree_dirty,
            "pythonVersion": self.python_version,
            "dependencyLockHash": self.dependency_lock_hash,
            "interpretationFixtureHash": self.interpretation_fixture_hash,
            "claimFixtureHash": self.claim_fixture_hash,
            "providerQualityFixtureHash": self.provider_quality_fixture_hash,
            "providerPolicyFixtureHash": self.provider_policy_fixture_hash,
            "extractionSchemaVersion": self.extraction_schema_version,
            "providerId": self.provider_id,
            "modelId": self.model_id,
            "promptId": self.prompt_id,
            "promptHash": self.prompt_hash,
            "callMode": self.call_mode,
            "maxProviderCalls": self.max_provider_calls,
            "maxReservedTokens": self.max_reserved_tokens,
            "maxReservedCostMicrousd": self.max_reserved_cost_microusd,
            "repeats": self.repeats,
            "caseCount": self.case_count,
            "plannedCalls": self.planned_calls,
        }


@dataclass(frozen=True)
class ProviderPolicyShadowCaseResult:
    case_id: str
    repeat_index: int
    subject_keys: tuple[str, ...]
    disposition: str
    gap_codes: tuple[str, ...]
    mismatch_codes: tuple[str, ...]
    provider_quality_mismatch_codes: tuple[str, ...]
    policy_outcome_digest: str
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "caseId": self.case_id,
            "repeatIndex": self.repeat_index,
            "subjectKeys": list(self.subject_keys),
            "disposition": self.disposition,
            "gapCodes": list(self.gap_codes),
            "mismatchCodes": list(self.mismatch_codes),
            "providerQualityMismatchCodes": list(
                self.provider_quality_mismatch_codes
            ),
            "policyOutcomeDigest": self.policy_outcome_digest,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class ProviderPolicyShadowReport:
    identity: ProviderPolicyShadowIdentity
    evaluation_passed: bool
    passed: bool
    results: tuple[ProviderPolicyShadowCaseResult, ...]
    policy_outcome_variance_case_ids: tuple[str, ...]
    provider_calls: int
    provider_billed_calls: int
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_microusd: int
    usage_complete: bool
    reserved_calls: int
    reserved_tokens: int
    reserved_cost_microusd: int
    result_digest: str

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "evaluationPassed": self.evaluation_passed,
            "passed": self.passed,
            "summary": {
                "resultCount": len(self.results),
                "passedResultCount": sum(item.passed for item in self.results),
                "policyOutcomeVarianceCaseIds": list(
                    self.policy_outcome_variance_case_ids
                ),
                "providerCalls": self.provider_calls,
                "providerBilledCalls": self.provider_billed_calls,
                "inputTokens": self.input_tokens,
                "outputTokens": self.output_tokens,
                "totalTokens": self.total_tokens,
                "latencyMs": self.latency_ms,
                "costMicrousd": self.cost_microusd,
                "usageComplete": self.usage_complete,
                "reservedCalls": self.reserved_calls,
                "reservedTokens": self.reserved_tokens,
                "reservedCostMicrousd": self.reserved_cost_microusd,
            },
            "resultDigest": self.result_digest,
            "results": [item.to_dict() for item in self.results],
        }


def _validate_identity(
    identity: ProviderPolicyShadowIdentity,
    *,
    interpretation_catalog: InterpretationFixtureCatalog,
    claim_catalog: ClaimFixtureCatalog,
    provider_quality_catalog: ProviderQualityFixtureCatalog,
    provider_policy_catalog: ProviderPolicyFixtureCatalog,
    adapter: ProposalAdapter,
) -> None:
    expected = (
        interpretation_catalog.manifest_hash,
        claim_catalog.manifest_hash,
        provider_quality_catalog.manifest_hash,
        provider_policy_catalog.manifest_hash,
        len(provider_policy_catalog.cases),
        adapter.provider_id,
        adapter.model_id,
        adapter.prompt_id,
        adapter.prompt_hash,
    )
    actual = (
        identity.interpretation_fixture_hash,
        identity.claim_fixture_hash,
        identity.provider_quality_fixture_hash,
        identity.provider_policy_fixture_hash,
        identity.case_count,
        identity.provider_id,
        identity.model_id,
        identity.prompt_id,
        identity.prompt_hash,
    )
    if actual != expected:
        raise ValueError("provider-policy identity does not match the replay inputs")


def _source_context(
    provider_case: ProviderQualityFixtureCase,
    *,
    interpretation_by_id: Mapping[str, Any],
    claim_by_id: Mapping[str, Any],
) -> tuple[Any, tuple[Any, ...], tuple[EntityRef, ...], tuple[Claim, ...], Any]:
    source = interpretation_by_id[provider_case.interpretation_case_id]
    source_case = claim_by_id[provider_case.source_claim_case_ids[0]]
    normalized = normalize_message_evidence(source.message)
    resolved = resolve_entities(
        tenant_id=source.message.tenant_id,
        campaign_id=source.campaign_id,
        seeds=source.seeds,
        evidence=normalized.evidence,
    )
    prior_claims = tuple(
        _prior_claim_from_fixture(raw, normalized.evidence, resolved.entities)
        for raw in source_case.prior_claims
    )
    request = build_claim_extraction_request(
        tenant_id=source.message.tenant_id,
        campaign_id=source.campaign_id,
        evidence=normalized.evidence,
        entities=resolved.entities,
        prior_claims=prior_claims,
        resolution_issues=resolved.issues,
    )
    return source, normalized.evidence, resolved.entities, prior_claims, request


def _select_subjects(
    case: ProviderPolicyFixtureCase,
    entities: tuple[EntityRef, ...],
) -> tuple[dict[str, EntityRef], tuple[str, ...]]:
    selected = {}
    mismatch_codes = set()
    for subject in case.subjects:
        matches = tuple(
            entity
            for entity in entities
            if entity.entity_type.value == subject["entityType"]
            and entity.relationship == subject["relationship"]
            and entity.suite.casefold() == str(subject["suite"]).casefold()
        )
        if len(matches) != 1:
            mismatch_codes.add("subject_selector_mismatch")
            continue
        selected[str(subject["key"])] = matches[0]
    return selected, tuple(sorted(mismatch_codes))


def _authorized_referral_recipients(claims: tuple[Claim, ...]) -> tuple[str, ...]:
    recipients = set()
    for claim in claims:
        if claim.predicate is not ClaimPredicate.REFERRAL:
            continue
        value = claim.value
        if isinstance(value, Mapping):
            email = value.get("email")
            if isinstance(email, str) and email:
                recipients.add(email)
        elif isinstance(value, str) and value:
            recipients.add(value)
    return tuple(sorted(recipients))


def _build_policy_request(
    case: ProviderPolicyFixtureCase,
    *,
    source: Any,
    selected: Mapping[str, EntityRef],
    claims: tuple[Claim, ...],
) -> PolicyEvaluationRequest:
    contract = CampaignContract.create(
        tenant_id=source.message.tenant_id,
        client_id="client-shadow",
        campaign_id=source.campaign_id,
        version=1,
        required_fields=tuple(case.contract["requiredFields"]),
        hard_requirements=dict(case.contract["hardRequirements"]),
        soft_preferences=dict(case.contract["softPreferences"]),
        source_authority=ContractAuthority.SYSTEM_POLICY,
    )
    entity_by_key = dict(selected)

    def remap(values: Mapping[str, Any]) -> dict[str, Any]:
        return {
            entity_by_key[str(key)].entity_id: _plain(value)
            for key, value in values.items()
        }

    return PolicyEvaluationRequest.create(
        contract=contract,
        scope=ExecutionScope(
            tenant_id=source.message.tenant_id,
            client_id="client-shadow",
            campaign_id=source.campaign_id,
            thread_id="thread-shadow",
            sheet_id="sheet-shadow",
            row_anchor="shadow-row",
        ),
        entities=tuple(entity_by_key.values()),
        claims=claims,
        snapshot_hash=f"snapshot-{case.case_id}",
        current_facts=remap(case.current_state["facts"]),
        current_conversation_states=remap(
            case.current_state["conversationStates"]
        ),
        current_followup_states=remap(case.current_state["followupStates"]),
        authorized_recipients=_authorized_referral_recipients(claims),
    )


def _policy_projection(
    policy_result: Any,
    *,
    selected: Mapping[str, EntityRef],
) -> dict[str, dict[str, Any]]:
    key_by_id = {entity.entity_id: key for key, entity in selected.items()}
    projection = {}
    for item in policy_result.results:
        key = key_by_id[item.decision.entity_id]
        signatures = tuple(
            sorted(
                {
                    f"{action.action_type.value}:{action.approval_class.value}"
                    for action in item.action_plan.actions
                }
            )
        )
        projection[key] = {
            "subject": key,
            "marketState": item.decision.market_state.value,
            "fitState": item.decision.fit_state.value,
            "completenessState": item.decision.completeness_state.value,
            "conversationState": item.decision.conversation_state.value,
            "approvalClass": item.approval_class.value,
            "reasonCodes": list(item.decision.reason_codes),
            "missingFields": list(item.decision.missing_fields),
            "actionCount": len(item.action_plan.actions),
            "actionSignatures": list(signatures),
        }
    return dict(sorted(projection.items()))


def _gap_codes(
    claims: tuple[Claim, ...],
    policy_result: Any,
) -> tuple[str, ...]:
    actions_by_entity: dict[str, set[ActionType]] = {}
    for result in policy_result.results:
        actions_by_entity[result.decision.entity_id] = {
            action.action_type for action in result.action_plan.actions
        }
    gaps = set()
    for claim in claims:
        actions = actions_by_entity.get(claim.subject_entity_id, set())
        if (
            claim.predicate is ClaimPredicate.TOUR_REQUEST
            and claim.value is True
            and ActionType.TOUR_REQUEST not in actions
        ):
            gaps.add("tour_request_action_missing")
        if claim.predicate is ClaimPredicate.INFORMATION_REQUEST and claim.value is True:
            gaps.add("information_request_action_missing")
    return tuple(sorted(gaps))


def _grade_policy(
    case: ProviderPolicyFixtureCase,
    *,
    projection: Mapping[str, Mapping[str, Any]],
    gap_codes: tuple[str, ...],
) -> tuple[str, ...]:
    mismatches = set()
    expected_results = {
        str(item["subject"]): item for item in case.expected["results"]
    }
    if set(projection) != set(expected_results):
        mismatches.add("policy_subject_set_mismatch")
    scalar_fields = (
        "marketState",
        "fitState",
        "completenessState",
        "conversationState",
        "approvalClass",
    )
    for key in sorted(set(projection) & set(expected_results)):
        actual = projection[key]
        expected = expected_results[key]
        if any(actual[field] != expected[field] for field in scalar_fields):
            mismatches.add("policy_decision_mismatch")
        if tuple(actual["reasonCodes"]) != tuple(expected["reasonCodes"]):
            mismatches.add("policy_reason_mismatch")
        if tuple(actual["missingFields"]) != tuple(expected["missingFields"]):
            mismatches.add("policy_missing_fields_mismatch")
        if actual["actionCount"] != expected["actionCount"]:
            mismatches.add("policy_action_count_mismatch")
        signatures = set(actual["actionSignatures"])
        if set(expected["requiredActions"]) - signatures:
            mismatches.add("policy_required_action_missing")
        if set(expected["forbiddenActions"]) & signatures:
            mismatches.add("policy_forbidden_action_present")
    if gap_codes != tuple(case.expected["gapCodes"]):
        mismatches.add("policy_gap_mismatch")
    return tuple(sorted(mismatches))


def run_provider_policy_shadow(
    *,
    interpretation_catalog: InterpretationFixtureCatalog,
    claim_catalog: ClaimFixtureCatalog,
    provider_quality_catalog: ProviderQualityFixtureCatalog,
    provider_policy_catalog: ProviderPolicyFixtureCatalog,
    adapter: ProposalAdapter,
    identity: ProviderPolicyShadowIdentity,
    telemetry: ProviderTelemetry | None = None,
    budget: BudgetedProviderTransport | None = None,
) -> ProviderPolicyShadowReport:
    """Run the bounded provider-policy corpus without persistence or effects."""

    _validate_identity(
        identity,
        interpretation_catalog=interpretation_catalog,
        claim_catalog=claim_catalog,
        provider_quality_catalog=provider_quality_catalog,
        provider_policy_catalog=provider_policy_catalog,
        adapter=adapter,
    )
    if identity.provider_id != RECORDED_PROVIDER_ID:
        if budget is None or telemetry is not budget:
            raise ValueError(
                "provider-policy calls require one budgeted telemetry transport"
            )
        expected_limits = ProviderBudgetLimits(
            max_calls=identity.max_provider_calls,
            max_reserved_tokens=identity.max_reserved_tokens,
            max_reserved_cost_microusd=identity.max_reserved_cost_microusd,
        )
        if budget.limits != expected_limits:
            raise ValueError("provider-policy identity budget does not match transport")
    selected_provider_catalog = _selected_provider_quality_catalog(
        provider_quality_catalog,
        provider_policy_catalog,
    )
    replay_identity = ReplayIdentity.create(
        code_revision=identity.code_revision,
        source_tree_hash=identity.source_tree_hash,
        source_tree_dirty=identity.source_tree_dirty,
        python_version=identity.python_version,
        dependency_lock_hash=identity.dependency_lock_hash,
        interpretation_fixture_hash=identity.interpretation_fixture_hash,
        claim_fixture_hash=identity.claim_fixture_hash,
        evaluation_fixture_hash=selected_provider_catalog.manifest_hash,
        extraction_schema_version=identity.extraction_schema_version,
        provider_id=identity.provider_id,
        model_id=identity.model_id,
        prompt_id=identity.prompt_id,
        prompt_hash=identity.prompt_hash,
        evaluation_profile="provider_quality",
        repeats=identity.repeats,
        case_count=identity.case_count,
        interpretation_case_count=len(interpretation_catalog.cases),
    )
    capturing = _CapturingProposalAdapter(adapter)
    provider_report = run_claim_replay(
        interpretation_catalog=interpretation_catalog,
        claim_catalog=claim_catalog,
        provider_quality_catalog=selected_provider_catalog,
        adapter=capturing,
        identity=replay_identity,
        telemetry=telemetry,
        fail_fast=True,
    )

    interpretation_by_id = {
        case.case_id: case for case in interpretation_catalog.cases
    }
    claim_by_id = {case.case_id: case for case in claim_catalog.cases}
    provider_by_id = {
        case.case_id: case for case in provider_quality_catalog.cases
    }
    quality_by_key = {
        (item.case_id, item.repeat_index): item for item in provider_report.results
    }
    results = []
    outcome_digests: dict[str, set[str]] = {
        case.case_id: set() for case in provider_policy_catalog.cases
    }
    ordered_cases = tuple(
        sorted(provider_policy_catalog.cases, key=lambda item: item.case_id)
    )
    for repeat_index in range(identity.repeats):
        for case in ordered_cases:
            provider_case = provider_by_id[case.provider_case_id]
            quality = quality_by_key.get((case.provider_case_id, repeat_index))
            subject_keys = tuple(sorted(str(item["key"]) for item in case.subjects))
            if quality is None:
                mismatch_codes = ("not_run_after_failure",)
                outcome_digest = _digest(
                    {"caseId": case.case_id, "mismatchCodes": mismatch_codes}
                )
                results.append(
                    ProviderPolicyShadowCaseResult(
                        case_id=case.case_id,
                        repeat_index=repeat_index,
                        subject_keys=subject_keys,
                        disposition="blocker",
                        gap_codes=(),
                        mismatch_codes=mismatch_codes,
                        provider_quality_mismatch_codes=(),
                        policy_outcome_digest=outcome_digest,
                        passed=False,
                    )
                )
                continue
            if not quality.passed:
                mismatch_codes = ("provider_quality_failed",)
                provider_quality_mismatch_codes = tuple(
                    sorted(
                        {
                            *quality.quality_mismatch_codes,
                            *([quality.error_code] if quality.error_code else []),
                        }
                    )
                )
                outcome_digest = _digest(
                    {
                        "caseId": case.case_id,
                        "mismatchCodes": mismatch_codes,
                        "providerQualityMismatchCodes": provider_quality_mismatch_codes,
                    }
                )
                result = ProviderPolicyShadowCaseResult(
                    case_id=case.case_id,
                    repeat_index=repeat_index,
                    subject_keys=subject_keys,
                    disposition="blocker",
                    gap_codes=(),
                    mismatch_codes=mismatch_codes,
                    provider_quality_mismatch_codes=(
                        provider_quality_mismatch_codes
                    ),
                    policy_outcome_digest=outcome_digest,
                    passed=False,
                )
                results.append(result)
                continue

            source, evidence, entities, prior_claims, request = _source_context(
                provider_case,
                interpretation_by_id=interpretation_by_id,
                claim_by_id=claim_by_id,
            )
            response = capturing.responses[case.provider_case_id][repeat_index]
            extracted = extract_claims(
                tenant_id=source.message.tenant_id,
                campaign_id=source.campaign_id,
                evidence=evidence,
                entities=entities,
                prior_claims=request.prior_claims,
                resolution_issues=request.resolution_issues,
                model_output=response.model_output,
            )
            selected, selector_mismatches = _select_subjects(case, entities)
            selected_ids = {entity.entity_id for entity in selected.values()}
            selected_claims = tuple(
                claim
                for claim in (*prior_claims, *extracted.claims)
                if claim.subject_entity_id in selected_ids
            )
            if selector_mismatches:
                mismatch_codes = selector_mismatches
                gap_codes = ()
                projection = {}
            else:
                try:
                    policy_request = _build_policy_request(
                        case,
                        source=source,
                        selected=selected,
                        claims=selected_claims,
                    )
                    policy_result = evaluate_policy(policy_request)
                    projection = _policy_projection(policy_result, selected=selected)
                    gap_codes = _gap_codes(selected_claims, policy_result)
                    mismatch_codes = _grade_policy(
                        case,
                        projection=projection,
                        gap_codes=gap_codes,
                    )
                except (TypeError, ValueError):
                    projection = {}
                    gap_codes = ()
                    mismatch_codes = ("policy_evaluation_failed",)
            outcome_digest = _digest(
                {
                    "caseId": case.case_id,
                    "projection": projection,
                    "gapCodes": gap_codes,
                    "mismatchCodes": mismatch_codes,
                }
            )
            disposition = (
                "blocker"
                if mismatch_codes
                else "expected_gap"
                if gap_codes
                else "pass"
            )
            result = ProviderPolicyShadowCaseResult(
                case_id=case.case_id,
                repeat_index=repeat_index,
                subject_keys=subject_keys,
                disposition=disposition,
                gap_codes=gap_codes,
                mismatch_codes=mismatch_codes,
                provider_quality_mismatch_codes=(),
                policy_outcome_digest=outcome_digest,
                passed=not mismatch_codes,
            )
            results.append(result)
            outcome_digests[case.case_id].add(outcome_digest)

    ordered_results = tuple(
        sorted(results, key=lambda item: (item.case_id, item.repeat_index))
    )
    variance = tuple(
        sorted(case_id for case_id, digests in outcome_digests.items() if len(digests) > 1)
    )
    evaluation_passed = (
        provider_report.evaluation_passed
        and len(ordered_results) == identity.planned_calls
        and all(item.passed for item in ordered_results)
        and not variance
    )
    result_digest = _digest([item.to_dict() for item in ordered_results])
    reservations = (
        budget.reservation_snapshot()
        if budget is not None
        else ProviderReservationSnapshot()
    )
    return ProviderPolicyShadowReport(
        identity=identity,
        evaluation_passed=evaluation_passed,
        passed=evaluation_passed and not identity.source_tree_dirty,
        results=ordered_results,
        policy_outcome_variance_case_ids=variance,
        provider_calls=provider_report.provider_calls,
        provider_billed_calls=provider_report.provider_billed_calls,
        input_tokens=provider_report.input_tokens,
        output_tokens=provider_report.output_tokens,
        latency_ms=provider_report.latency_ms,
        cost_microusd=provider_report.cost_microusd,
        usage_complete=provider_report.usage_complete,
        reserved_calls=reservations.reserved_calls,
        reserved_tokens=reservations.reserved_tokens,
        reserved_cost_microusd=reservations.reserved_cost_microusd,
        result_digest=result_digest,
    )


__all__ = [
    "PROVIDER_POLICY_SHADOW_PROFILE",
    "BudgetedProviderTransport",
    "ProviderBudgetExceeded",
    "ProviderBudgetLimits",
    "ProviderReservationSnapshot",
    "ProviderPolicyShadowCaseResult",
    "ProviderPolicyShadowIdentity",
    "ProviderPolicyShadowReport",
    "RecordedProviderQualityProposalAdapter",
    "run_provider_policy_shadow",
    "select_provider_policy_cases",
]
