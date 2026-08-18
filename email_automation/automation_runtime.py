"""Immutable request-scoped runtime.

Task 5 of the production automation certification plan. One runtime object carries
every dependency a single request may touch: inbound and conversation sources,
outbound delivery, AI inference, Drive publication, the clock, the counter store,
scoped data clients, run identity, and fixture scope.

Two properties are the whole point.

**Isolation.** A certification run and an ordinary production run can be in flight
in the same process at the same moment. If they shared a capture, clock, counter,
source, transport, run id, or scope, a fixture effect could escape into production
- or a production effect could be counted as certification evidence. Every factory
therefore builds fresh instances; nothing is module-global.

**Ordinary production is the default.** Omitting a dependency yields ordinary
production behavior, so the runtime cannot become a back door for changing what
production does. There is deliberately NO way to build a runtime from request
data: ``runtime_from_request`` exists only to refuse.

This module is PURE. Provider clients are resolved LAZILY inside the transports,
never at import or at runtime construction, so merely describing a runtime needs
no credential. That is the same defect backlog #84 exists to remove from
``clients.py``; this module must not reintroduce it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Tuple

from .message_transport import (
    ConversationStateSource,
    FixtureConversationStateSource,
    FixtureInboundMessageSource,
    InboundMessageSource,
    OutboundDraft,
    OutboundDraftTransport,
    DeliveryReceipt,
)


class UserRuntimeLaunchRequired(RuntimeError):
    """A model-dependent effect was reached on an agent-safe lane.

    Carries the exact blocker reason the plan mandates so a runner can classify the
    scenario as INSTRUMENT_BLOCKED rather than FAIL: the product did not misbehave,
    the instrument is simply not permitted to invoke a real provider.
    """


class EffectScopeViolation(RuntimeError):
    """An effect targeted a resource outside the declared fixture scope."""


class CounterReservationError(RuntimeError):
    """A reservation was released illegitimately."""


class RuntimeConstructionError(RuntimeError):
    """Someone tried to build a runtime from untrusted input."""


# ---------------------------------------------------------------------------
# counters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CounterReservation:
    scope: str
    key: str
    amount: int
    limit: int


@dataclass(frozen=True)
class CounterReservationToken:
    reservation_id: str
    reservations: Tuple[CounterReservation, ...]


class CounterStore(Protocol):
    def reserve_many(
        self,
        reservations: Tuple[CounterReservation, ...],
        idempotency_key: str,
    ) -> Optional[CounterReservationToken]: ...
    def release_many(self, token: CounterReservationToken) -> None: ...


class InMemoryCounterStore:
    """Atomic multi-scope reservations.

    Send caps are the last line of defence against mailing a broker twice, so the
    rules here are stricter than they may look:

    * **All or nothing.** A reservation spanning user AND global scope either takes
      both or takes neither. A partial take would let a later request pass a cap
      that had already been consumed.
    * **Idempotent under retry.** The same idempotency key returns the ORIGINAL
      token without incrementing, because a retried send must not consume two.
    * **Release only after a PROVEN no-send.** An ``ambiguous`` outcome RETAINS the
      reservation. Releasing on ambiguity is exactly how a duplicate send gets
      authorised: the message may well have gone out.
    * **Release is by token, idempotent, and never negative.** A forged or foreign
      token is refused rather than silently ignored.
    """

    def __init__(self, limits: Optional[Mapping[Tuple[str, str], int]] = None) -> None:
        self._limits: Dict[Tuple[str, str], int] = dict(limits or {})
        self._used: Dict[Tuple[str, str], int] = {}
        self._by_key: Dict[str, CounterReservationToken] = {}
        self._live: Dict[str, Tuple[CounterReservation, ...]] = {}
        self._outcome: Dict[str, str] = {}
        self._released: set = set()
        self._lock = threading.Lock()
        self._sequence = 0

    def used(self, scope: str, key: str) -> int:
        with self._lock:
            return self._used.get((scope, key), 0)

    def reserve_many(
        self,
        reservations: Tuple[CounterReservation, ...],
        idempotency_key: str,
    ) -> Optional[CounterReservationToken]:
        with self._lock:
            existing = self._by_key.get(idempotency_key)
            if existing is not None:
                return existing

            # Check every scope BEFORE mutating any of them.
            for reservation in reservations:
                slot = (reservation.scope, reservation.key)
                limit = self._limits.get(slot, reservation.limit)
                if self._used.get(slot, 0) + reservation.amount > limit:
                    return None

            for reservation in reservations:
                slot = (reservation.scope, reservation.key)
                self._used[slot] = self._used.get(slot, 0) + reservation.amount

            self._sequence += 1
            token = CounterReservationToken(
                reservation_id=f"res-{self._sequence}",
                reservations=tuple(reservations),
            )
            self._by_key[idempotency_key] = token
            self._live[token.reservation_id] = token.reservations
            return token

    def record_outcome(self, token: CounterReservationToken, *, outcome: str) -> None:
        with self._lock:
            self._outcome[token.reservation_id] = outcome

    def release_many(self, token: CounterReservationToken) -> None:
        with self._lock:
            outcome = self._outcome.get(token.reservation_id)
            if outcome in ("sent", "ambiguous"):
                raise CounterReservationError(
                    f"reservation {token.reservation_id} has outcome {outcome!r}; "
                    "only a proven no-send may be refunded"
                )

            live = self._live.get(token.reservation_id)
            if live is None:
                if token.reservation_id in self._released:
                    # Already released. A repeat release is a ZERO DELTA success, not
                    # an error: a retrying caller must not be punished, and must also
                    # not refund twice.
                    return
                raise CounterReservationError(
                    f"unknown reservation {token.reservation_id}"
                )

            for reservation in live:
                slot = (reservation.scope, reservation.key)
                # max(0, ...) makes usage structurally unable to go negative, so no
                # sequence of releases can manufacture headroom under a send cap.
                self._used[slot] = max(0, self._used.get(slot, 0) - reservation.amount)
            del self._live[token.reservation_id]
            self._released.add(token.reservation_id)


# ---------------------------------------------------------------------------
# effect scope
# ---------------------------------------------------------------------------


class EffectScope(Protocol):
    def assert_firestore_path(self, path: str) -> None: ...
    def assert_sheet_target(self, spreadsheet_id: str, range_name: str) -> None: ...
    def assert_sheet_request(self, spreadsheet_id: str, body: Mapping[str, Any]) -> None: ...
    def assert_drive_parent(self, parent_id: str) -> None: ...
    def assert_drive_permission(self, file_id: str, body: Mapping[str, Any]) -> None: ...


class UnrestrictedEffectScope:
    """Ordinary production. Asserts nothing; existing product guards still apply."""

    def assert_firestore_path(self, path: str) -> None: ...
    def assert_sheet_target(self, spreadsheet_id: str, range_name: str) -> None: ...
    def assert_sheet_request(self, spreadsheet_id: str, body: Mapping[str, Any]) -> None: ...
    def assert_drive_parent(self, parent_id: str) -> None: ...
    def assert_drive_permission(self, file_id: str, body: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True)
class FixtureEffectScope:
    """Certification. Contains NO business rules - only boundary checks."""

    firestore_prefix: str
    sheet_ids: Tuple[str, ...] = ()
    drive_parents: Tuple[str, ...] = ()

    def assert_firestore_path(self, path: str) -> None:
        if not path.startswith(self.firestore_prefix):
            raise EffectScopeViolation("firestore path outside the fixture prefix")

    def assert_sheet_target(self, spreadsheet_id: str, range_name: str) -> None:
        if spreadsheet_id not in self.sheet_ids:
            raise EffectScopeViolation("sheet target outside the fixture set")
        if not range_name:
            raise EffectScopeViolation("sheet range must be explicit")

    def assert_sheet_request(self, spreadsheet_id: str, body: Mapping[str, Any]) -> None:
        if spreadsheet_id not in self.sheet_ids:
            raise EffectScopeViolation("sheet request outside the fixture set")
        if not isinstance(body, Mapping) or not body:
            raise EffectScopeViolation("sheet request body must be an explicit mapping")

    def assert_drive_parent(self, parent_id: str) -> None:
        if parent_id not in self.drive_parents:
            raise EffectScopeViolation("drive parent outside the fixture set")

    def assert_drive_permission(self, file_id: str, body: Mapping[str, Any]) -> None:
        # Public-link publication is NOT_TESTED by contract; certification may only
        # ever capture the would-publish request, never authorise a real one.
        raise EffectScopeViolation(
            "certification may never create a Drive permission"
        )


# ---------------------------------------------------------------------------
# provider transports
# ---------------------------------------------------------------------------


class AIProviderTransport(Protocol):
    def create_response(self, request: Mapping[str, Any]) -> Any: ...
    def create_chat_completion(self, request: Mapping[str, Any]) -> Any: ...
    def upload_file(self, file_obj: Any, purpose: str) -> Any: ...


class ProviderBackedAITransport:
    """Ordinary production. Resolves the real client LAZILY, never at build time."""

    def __init__(self) -> None:
        self._client: Any = None

    def is_resolved(self) -> bool:
        return self._client is not None

    def _resolve(self) -> Any:
        if self._client is None:
            from .clients import client  # imported here so building needs no credential
            self._client = client
        return self._client

    def create_response(self, request: Mapping[str, Any]) -> Any:
        return self._resolve().responses.create(**dict(request))

    def create_chat_completion(self, request: Mapping[str, Any]) -> Any:
        return self._resolve().chat.completions.create(**dict(request))

    def upload_file(self, file_obj: Any, purpose: str) -> Any:
        return self._resolve().files.create(file=file_obj, purpose=purpose)


class DenyingAITransport:
    """Certification, agent-safe. Refuses BEFORE any provider request is built.

    Records only that an attempt happened and which method - never the request
    payload, because a prompt may contain fixture content and evidence keeps
    digests, not bodies.
    """

    def __init__(self, reason: str = "user_runtime_launch_required") -> None:
        self.reason = reason
        self.attempts: list = []

    def _deny(self, method: str) -> None:
        self.attempts.append(method)
        raise UserRuntimeLaunchRequired(
            f"{self.reason}: a real model request is not permitted on an agent-safe "
            "lane; Baylor must launch the prepared command"
        )

    def create_response(self, request: Mapping[str, Any]) -> Any:
        self._deny("create_response")

    def create_chat_completion(self, request: Mapping[str, Any]) -> Any:
        self._deny("create_chat_completion")

    def upload_file(self, file_obj: Any, purpose: str) -> Any:
        self._deny("upload_file")


class DrivePublicationTransport(Protocol):
    def publish(self, file_id: str, permission: Mapping[str, Any]) -> Mapping[str, Any]: ...


class ProviderBackedDrivePublication:
    """Ordinary production. Resolves the real Drive service LAZILY."""

    def __init__(self) -> None:
        self._service: Any = None

    def is_resolved(self) -> bool:
        return self._service is not None

    def _resolve(self) -> Any:
        if self._service is None:
            from .service_providers import get_drive_service
            self._service = get_drive_service()
        return self._service

    def publish(self, file_id: str, permission: Mapping[str, Any]) -> Mapping[str, Any]:
        service = self._resolve()
        return service.permissions().create(fileId=file_id, body=dict(permission)).execute()


class CapturingDrivePublication:
    """Certification. Validates, records the would-publish request, calls nothing.

    ``real_permission_calls`` stays 0 by construction and is asserted by the
    scenario contract; public-link publication remains NOT_TESTED.
    """

    def __init__(self) -> None:
        self.captured: list = []
        self.real_permission_calls = 0

    def publish(self, file_id: str, permission: Mapping[str, Any]) -> Mapping[str, Any]:
        if not file_id:
            raise ValueError("drive publication requires an exact file id")
        if not isinstance(permission, Mapping) or not permission:
            raise ValueError("drive publication requires an explicit permission body")
        self.captured.append((file_id, dict(permission)))
        return {"status": "captured", "fileId": file_id}


class RecordingOutboundTransport:
    """Certification delivery capture. Produces a receipt without sending."""

    def __init__(self) -> None:
        self.delivered: list = []

    def deliver(self, draft: OutboundDraft) -> DeliveryReceipt:
        self.delivered.append(draft)
        index = len(self.delivered)
        return DeliveryReceipt(
            status="captured",
            provider_message_id=f"captured-{index}",
            internet_message_id=f"<captured-{index}@certification.invalid>",
            conversation_id="captured-conversation",
        )


# ---------------------------------------------------------------------------
# the runtime
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AutomationRuntime:
    inbound: Optional[InboundMessageSource]
    conversations: Optional[ConversationStateSource]
    outbound: Optional[OutboundDraftTransport]
    counters: CounterStore
    now: Callable[[], datetime]
    firestore: Any
    sheets: Any
    drive: Any
    effect_scope: EffectScope
    ai_provider: AIProviderTransport
    drive_publication: DrivePublicationTransport
    certification_run_id: Optional[str] = None
    certification_scope: Optional[str] = None


def production_runtime(
    *,
    inbound: Optional[InboundMessageSource] = None,
    conversations: Optional[ConversationStateSource] = None,
    outbound: Optional[OutboundDraftTransport] = None,
    counters: Optional[CounterStore] = None,
    now: Optional[Callable[[], datetime]] = None,
    firestore: Any = None,
    sheets: Any = None,
    drive: Any = None,
    effect_scope: Optional[EffectScope] = None,
    ai_provider: Optional[AIProviderTransport] = None,
    drive_publication: Optional[DrivePublicationTransport] = None,
) -> AutomationRuntime:
    """Ordinary production. Every omitted dependency defaults to production behavior."""
    return AutomationRuntime(
        inbound=inbound,
        conversations=conversations,
        outbound=outbound,
        counters=counters if counters is not None else InMemoryCounterStore(),
        now=now or _utc_now,
        firestore=firestore,
        sheets=sheets,
        drive=drive,
        effect_scope=effect_scope or UnrestrictedEffectScope(),
        ai_provider=ai_provider or ProviderBackedAITransport(),
        drive_publication=drive_publication or ProviderBackedDrivePublication(),
        certification_run_id=None,
        certification_scope=None,
    )


def certification_runtime(
    *,
    run_id: str,
    scope: str,
    inbound_snapshot: Optional[Mapping[str, Any]] = None,
    conversation_snapshot: Optional[Mapping[str, Any]] = None,
    limits: Optional[Mapping[Tuple[str, str], int]] = None,
    now: Optional[Callable[[], datetime]] = None,
) -> AutomationRuntime:
    """Agent-safe certification. Fresh instances only, so two runs share nothing."""
    if not run_id or not scope:
        raise RuntimeConstructionError("certification requires an exact run id and scope")
    return AutomationRuntime(
        inbound=(
            FixtureInboundMessageSource(snapshot=inbound_snapshot)
            if inbound_snapshot is not None
            else None
        ),
        conversations=(
            FixtureConversationStateSource(snapshot=conversation_snapshot)
            if conversation_snapshot is not None
            else None
        ),
        outbound=RecordingOutboundTransport(),
        counters=InMemoryCounterStore(limits=limits),
        now=now or _utc_now,
        firestore=None,
        sheets=None,
        drive=None,
        effect_scope=FixtureEffectScope(firestore_prefix=f"certification/{run_id}"),
        ai_provider=DenyingAITransport(),
        drive_publication=CapturingDrivePublication(),
        certification_run_id=run_id,
        certification_scope=scope,
    )


def runtime_from_request(payload: Mapping[str, Any]) -> AutomationRuntime:
    """Always refuses.

    A runtime names transports, scopes, and counter limits. If a request could
    supply one, a caller could disable a send cap, widen an effect scope, or swap
    the AI transport - so the only correct implementation is a refusal. It exists
    as a named function purely so that intent is explicit and testable rather than
    implied by the absence of a constructor.
    """
    raise RuntimeConstructionError(
        "a runtime may never be constructed from request data; use "
        "production_runtime() or certification_runtime()"
    )
