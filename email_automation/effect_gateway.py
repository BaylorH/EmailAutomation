"""Fail-closed, dependency-injected gateway for provider-side effects.

The module deliberately contains no Graph, Firestore, or other provider
client. A worker must inject:

* a receipt store whose ``reserve_attempt`` operation is atomic; and
* an adapter for the requested provider.

That keeps effect policy in one place while leaving provider and persistence
mechanics replaceable and credential-free in tests.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol


PROVIDER_EFFECTS_ENABLED_ENV = "SITESIFT_PROVIDER_EFFECTS_ENABLED"
GLOBAL_OUTBOUND_MODE_ENV = "SITESIFT_OUTBOUND_MODE"
MAX_ATTEMPTS_ENV = "SITESIFT_EFFECT_MAX_ATTEMPTS"
MAX_PER_RUN_ENV = "SITESIFT_EFFECT_MAX_PER_RUN"
MAX_PER_USER_ENV = "SITESIFT_EFFECT_MAX_PER_USER"
MAX_PER_PROVIDER_ENV = "SITESIFT_EFFECT_MAX_PER_PROVIDER"


class ReceiptState(str, Enum):
    """Durable states for one logical provider effect."""

    BLOCKED = "blocked"
    PREPARED = "prepared"
    CLAIMED = "claimed"
    PROVIDER_ACCEPTED = "provider_accepted"
    SUCCEEDED = "succeeded"
    CANCELLED = "cancelled"
    TERMINAL_FAILED = "terminal_failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class AuthorityState(str, Enum):
    """Latest authoritative business state read immediately before an effect."""

    ACTIVE = "active"
    CANCELLED = "cancelled"
    TERMINAL = "terminal"


def _positive_env_int(name: str) -> int:
    raw = os.getenv(name, "").strip()
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _require_text(label: str, value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{label} must be non-empty")
    return normalized


def _json_ready(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("provider-effect content keys must be strings")
        return {
            key: _json_ready(item)
            for key, item in sorted(value.items(), key=lambda pair: pair[0])
        }
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(
        "provider-effect content must be JSON-compatible, "
        f"got {type(value).__name__}"
    )


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _stable_id(prefix: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _json_ready(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:32]}"


@dataclass(frozen=True)
class AttemptLimits:
    """Explicit attempt caps; zero is invalid and therefore fail-closed."""

    max_attempts: int = 0
    max_per_run: int = 0
    max_per_user: int = 0
    max_per_provider: int = 0

    @property
    def valid(self) -> bool:
        values = (
            self.max_attempts,
            self.max_per_run,
            self.max_per_user,
            self.max_per_provider,
        )
        return all(
            isinstance(value, int)
            and not isinstance(value, bool)
            and value > 0
            for value in values
        )


@dataclass(frozen=True)
class EffectGatewayConfig:
    """Runtime controls for the single provider-effect boundary."""

    enabled: bool = False
    global_effects_enabled: bool = False
    limits: AttemptLimits = AttemptLimits()

    @classmethod
    def from_env(cls) -> "EffectGatewayConfig":
        """Load fail-closed controls.

        Both the gateway-specific opt-in and the existing global outbound
        ``live`` mode are required. Every cap must also be explicitly positive.
        Missing, malformed, zero, and negative values all disable effects.
        """

        return cls(
            enabled=os.getenv(PROVIDER_EFFECTS_ENABLED_ENV, "").strip().lower()
            == "true",
            global_effects_enabled=os.getenv(
                GLOBAL_OUTBOUND_MODE_ENV, ""
            ).strip().lower()
            == "live",
            limits=AttemptLimits(
                max_attempts=_positive_env_int(MAX_ATTEMPTS_ENV),
                max_per_run=_positive_env_int(MAX_PER_RUN_ENV),
                max_per_user=_positive_env_int(MAX_PER_USER_ENV),
                max_per_provider=_positive_env_int(MAX_PER_PROVIDER_ENV),
            ),
        )


@dataclass(frozen=True)
class ProviderEffectRequest:
    """One logical effect and its independently keyed content."""

    run_id: str
    user_id: str
    provider: str
    effect_type: str
    effect_key: str
    content: Mapping[str, Any]
    effect_id: str
    content_idempotency_key: str

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        user_id: str,
        provider: str,
        effect_type: str,
        effect_key: str,
        content: Mapping[str, Any],
    ) -> "ProviderEffectRequest":
        normalized = {
            "run_id": _require_text("run_id", run_id),
            "user_id": _require_text("user_id", user_id),
            "provider": _require_text("provider", provider).lower(),
            "effect_type": _require_text("effect_type", effect_type).lower(),
            "effect_key": _require_text("effect_key", effect_key),
        }
        if not isinstance(content, Mapping):
            raise TypeError("content must be a mapping")
        ready_content = _json_ready(content)
        effect_id = _stable_id(
            "effect",
            {
                "userId": normalized["user_id"],
                "provider": normalized["provider"],
                "effectType": normalized["effect_type"],
                "effectKey": normalized["effect_key"],
            },
        )
        content_key = _stable_id(
            "content",
            {
                "effectId": effect_id,
                "content": ready_content,
            },
        )
        return cls(
            **normalized,
            content=_freeze_json(ready_content),
            effect_id=effect_id,
            content_idempotency_key=content_key,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "userId": self.user_id,
            "provider": self.provider,
            "effectType": self.effect_type,
            "effectKey": self.effect_key,
            "content": _json_ready(self.content),
            "effectId": self.effect_id,
            "contentIdempotencyKey": self.content_idempotency_key,
        }


@dataclass(frozen=True)
class EffectReceipt:
    effect_id: str
    content_idempotency_key: str
    state: ReceiptState
    attempts: int
    reason: str = ""
    provider_reference: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "effectId": self.effect_id,
            "contentIdempotencyKey": self.content_idempotency_key,
            "state": self.state.value,
            "attempts": self.attempts,
            "reason": self.reason,
            "providerReference": self.provider_reference,
        }


@dataclass(frozen=True)
class AttemptReservation:
    receipt: EffectReceipt
    acquired: bool


@dataclass(frozen=True)
class AuthoritativeDecision:
    state: AuthorityState
    reason: str = ""


@dataclass(frozen=True)
class ProviderEffectResult:
    provider_reference: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_reference",
            _require_text("provider_reference", self.provider_reference),
        )


class RetryableProviderError(RuntimeError):
    """The adapter proves that no provider effect occurred; retry is safe."""


class TerminalProviderError(RuntimeError):
    """The adapter proves a terminal rejection and that no effect occurred."""


class UncertainProviderOutcomeError(RuntimeError):
    """The provider may have accepted the effect; automatic retry is unsafe."""


class EffectReceiptStore(Protocol):
    """Persistence port; ``reserve_attempt`` must be one atomic operation."""

    def load_receipt(
        self,
        request: ProviderEffectRequest,
    ) -> EffectReceipt | None: ...

    def record_blocked(
        self,
        request: ProviderEffectRequest,
        reason: str,
    ) -> EffectReceipt: ...

    def prepare(
        self,
        request: ProviderEffectRequest,
    ) -> EffectReceipt: ...

    def reserve_attempt(
        self,
        request: ProviderEffectRequest,
        limits: AttemptLimits,
    ) -> AttemptReservation: ...

    def read_authoritative_state(
        self,
        request: ProviderEffectRequest,
    ) -> AuthoritativeDecision: ...

    def transition(
        self,
        request: ProviderEffectRequest,
        state: ReceiptState,
        *,
        reason: str = "",
        provider_reference: str = "",
    ) -> EffectReceipt: ...


class ProviderEffectAdapter(Protocol):
    def execute(self, request: ProviderEffectRequest) -> ProviderEffectResult: ...


_NON_RETRYABLE_STATES = frozenset(
    {
        ReceiptState.CLAIMED,
        ReceiptState.SUCCEEDED,
        ReceiptState.CANCELLED,
        ReceiptState.TERMINAL_FAILED,
        ReceiptState.RECONCILIATION_REQUIRED,
    }
)


class EffectGateway:
    """Execute at most one guarded provider attempt for a request."""

    def __init__(
        self,
        store: EffectReceiptStore,
        providers: Mapping[str, ProviderEffectAdapter],
        config: EffectGatewayConfig | None = None,
    ) -> None:
        self._store = store
        self._providers = dict(providers)
        self._config = config or EffectGatewayConfig()

    def execute(self, request: ProviderEffectRequest) -> EffectReceipt:
        current = self._store.load_receipt(request)
        if current is not None:
            same_content = (
                current.content_idempotency_key
                == request.content_idempotency_key
            )
            if same_content and current.state == ReceiptState.PROVIDER_ACCEPTED:
                return self._finalize_accepted(request, current)
            if same_content and current.state in _NON_RETRYABLE_STATES:
                return current
            if not same_content:
                # The atomic store owns conflict settlement so a content edit
                # cannot race a succeeded or in-flight logical effect.
                return self._store.reserve_attempt(
                    request,
                    self._config.limits,
                ).receipt

        if not self._config.enabled:
            return self._store.record_blocked(request, "gateway_disabled")
        if not self._config.global_effects_enabled:
            return self._store.record_blocked(request, "global_kill")
        if not self._config.limits.valid:
            return self._store.record_blocked(
                request,
                "invalid_or_missing_caps",
            )

        if current is None or current.state == ReceiptState.BLOCKED:
            self._store.prepare(request)

        reservation = self._store.reserve_attempt(
            request,
            self._config.limits,
        )
        if not reservation.acquired:
            return reservation.receipt

        authoritative = self._store.read_authoritative_state(request)
        if authoritative.state == AuthorityState.CANCELLED:
            return self._store.transition(
                request,
                ReceiptState.CANCELLED,
                reason="authoritative_cancelled",
            )
        if authoritative.state == AuthorityState.TERMINAL:
            return self._store.transition(
                request,
                ReceiptState.TERMINAL_FAILED,
                reason="authoritative_terminal",
            )
        if authoritative.state != AuthorityState.ACTIVE:
            return self._store.transition(
                request,
                ReceiptState.TERMINAL_FAILED,
                reason="invalid_authoritative_state",
            )

        adapter = self._providers.get(request.provider)
        if adapter is None:
            return self._store.transition(
                request,
                ReceiptState.TERMINAL_FAILED,
                reason="provider_adapter_missing",
            )

        # Keep this call directly adjacent to the authoritative re-read above:
        # no provider or persistence operation may be inserted between them.
        try:
            result = adapter.execute(request)
            if not isinstance(result, ProviderEffectResult):
                return self._store.transition(
                    request,
                    ReceiptState.RECONCILIATION_REQUIRED,
                    reason="provider_outcome_unknown",
                )
        except TerminalProviderError:
            return self._store.transition(
                request,
                ReceiptState.TERMINAL_FAILED,
                reason="provider_terminal",
            )
        except RetryableProviderError:
            if reservation.receipt.attempts >= self._config.limits.max_attempts:
                state = ReceiptState.TERMINAL_FAILED
                reason = "provider_attempts_exhausted"
            else:
                state = ReceiptState.PREPARED
                reason = "provider_retryable"
            return self._store.transition(
                request,
                state,
                reason=reason,
            )
        except UncertainProviderOutcomeError:
            return self._store.transition(
                request,
                ReceiptState.RECONCILIATION_REQUIRED,
                reason="provider_outcome_unknown",
            )
        except Exception:
            # Provider SDK exceptions are uncertain unless the adapter
            # explicitly proves the no-effect RetryableProviderError contract.
            return self._store.transition(
                request,
                ReceiptState.RECONCILIATION_REQUIRED,
                reason="provider_outcome_unknown",
            )

        try:
            accepted = self._store.transition(
                request,
                ReceiptState.PROVIDER_ACCEPTED,
                provider_reference=result.provider_reference,
            )
        except Exception:
            return self._store.transition(
                request,
                ReceiptState.RECONCILIATION_REQUIRED,
                reason="receipt_acceptance_failed",
                provider_reference=result.provider_reference,
            )
        return self._finalize_accepted(request, accepted)

    def _finalize_accepted(
        self,
        request: ProviderEffectRequest,
        accepted: EffectReceipt,
    ) -> EffectReceipt:
        """Finalize an accepted effect without ever calling its provider again."""

        try:
            return self._store.transition(
                request,
                ReceiptState.SUCCEEDED,
                provider_reference=accepted.provider_reference,
            )
        except Exception:
            return self._store.transition(
                request,
                ReceiptState.RECONCILIATION_REQUIRED,
                reason="receipt_finalize_failed",
                provider_reference=accepted.provider_reference,
            )
