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
    def assert_firestore_read(self, path: str) -> None: ...
    def assert_sheet_target(self, spreadsheet_id: str, range_name: str) -> None: ...
    def assert_sheet_request(self, spreadsheet_id: str, body: Mapping[str, Any]) -> None: ...
    def assert_drive_parent(self, parent_id: str) -> None: ...
    def assert_drive_permission(self, file_id: str, body: Mapping[str, Any]) -> None: ...


class UnrestrictedEffectScope:
    """Ordinary production. Asserts nothing; existing product guards still apply."""

    violations: Tuple[str, ...] = ()

    def assert_firestore_path(self, path: str) -> None: ...
    def assert_firestore_read(self, path: str) -> None: ...
    def assert_sheet_target(self, spreadsheet_id: str, range_name: str) -> None: ...
    def assert_sheet_request(self, spreadsheet_id: str, body: Mapping[str, Any]) -> None: ...
    def assert_drive_parent(self, parent_id: str) -> None: ...
    def assert_drive_permission(self, file_id: str, body: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True)
class FixtureEffectScope:
    """Certification. Contains NO business rules - only boundary checks.

    Every refusal is RECORDED as well as raised. That is not belt-and-braces: the
    product wraps nearly every store call in ``except Exception``, so a raised
    violation on its own would be swallowed, logged as a warning, and reported as
    an ordinary ``False``. The run would look unlucky rather than out of scope.
    The record survives the swallow, so evidence can assert on it.
    """

    firestore_prefix: str
    sheet_ids: Tuple[str, ...] = ()
    drive_parents: Tuple[str, ...] = ()
    readable_paths: Tuple[str, ...] = ()
    violations: list = field(default_factory=list, compare=False, repr=False)

    def _refuse(self, message: str) -> None:
        self.violations.append(message)
        raise EffectScopeViolation(message)

    def assert_firestore_path(self, path: str) -> None:
        """Writes. Confined to the fixture subtree, with no exceptions at all."""
        if not path.startswith(self.firestore_prefix):
            self._refuse(f"firestore path outside the fixture prefix: {path}")

    def assert_firestore_read(self, path: str) -> None:
        """Reads. The fixture subtree, PLUS exactly the named global documents.

        Some product decisions are genuinely global: campaign authority reads
        ``systemConfig/campaignAccess``, which lives outside every per-user
        subtree. A prefix-only fence refuses that read, and because the product
        treats an unreadable policy as UNKNOWN and fails closed, certification
        would suppress its own send and report an ordinary requeue - the run would
        look merely unlucky while actually proving nothing.

        The allowance is therefore exact-match and READ-ONLY. It never widens
        ``assert_firestore_path``, so a global document a run may consult is still
        a global document it may never modify.
        """
        if path in self.readable_paths:
            return
        self.assert_firestore_path(path)

    def assert_sheet_target(self, spreadsheet_id: str, range_name: str) -> None:
        if spreadsheet_id not in self.sheet_ids:
            self._refuse(f"sheet target outside the fixture set: {spreadsheet_id}")
        if not range_name:
            self._refuse("sheet range must be explicit")

    def assert_sheet_request(self, spreadsheet_id: str, body: Mapping[str, Any]) -> None:
        if spreadsheet_id not in self.sheet_ids:
            self._refuse(f"sheet request outside the fixture set: {spreadsheet_id}")
        if not isinstance(body, Mapping) or not body:
            self._refuse("sheet request body must be an explicit mapping")

    def assert_drive_parent(self, parent_id: str) -> None:
        if parent_id not in self.drive_parents:
            self._refuse(f"drive parent outside the fixture set: {parent_id}")

    def assert_drive_permission(self, file_id: str, body: Mapping[str, Any]) -> None:
        # Public-link publication is NOT_TESTED by contract; certification may only
        # ever capture the would-publish request, never authorise a real one.
        self._refuse("certification may never create a Drive permission")


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
    firestore: Any = None,
    sheets: Any = None,
    firestore_prefix: Optional[str] = None,
    sheet_ids: Tuple[str, ...] = (),
    drive_parents: Tuple[str, ...] = (),
    readable_paths: Tuple[str, ...] = (),
) -> AutomationRuntime:
    """Agent-safe certification. Fresh instances only, so two runs share nothing.

    ``firestore``/``sheets`` are the FIXTURE providers, and they arrive already
    decided by the bound immutable fixture config - never from request data, which
    ``runtime_from_request`` refuses outright. Both are wrapped before they are
    stored, so no caller can ever hold the unfenced object.

    ``firestore_prefix`` defaults to ``certification/<run_id>``. A fixture whose
    documents live under an ordinary product path (``users/<fixture-uid>``)
    declares that prefix instead, which is what makes the ordinary state machine
    reachable at all - the fence still refuses every path outside it.
    """
    if not run_id or not scope:
        raise RuntimeConstructionError("certification requires an exact run id and scope")
    effect_scope = FixtureEffectScope(
        firestore_prefix=firestore_prefix or f"certification/{run_id}",
        sheet_ids=tuple(sheet_ids),
        drive_parents=tuple(drive_parents),
        readable_paths=tuple(readable_paths),
    )
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
        firestore=ScopedFirestore(firestore, effect_scope) if firestore is not None else None,
        sheets=ScopedSheets(sheets, effect_scope) if sheets is not None else None,
        drive=DenyingDriveClient(),
        effect_scope=effect_scope,
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


# ---------------------------------------------------------------------------
# scoped data clients
# ---------------------------------------------------------------------------
#
# The product reaches Firestore through chains, not single calls:
#
#     fs.collection("users").document(uid).collection("threads") \
#       .document(tid).collection("messages").stream()
#     ...then snapshot.reference.delete()
#
# Guarding only the entry point would be theatre. ONE unwrapped return value
# anywhere along that chain hands the caller the ambient production client, and
# because nearly every product call site is wrapped in ``except Exception``, the
# escape would be SILENT - the run would look merely unlucky.
#
# So the rule here is absolute: a fenced object may only ever return fenced
# objects, and unwrapping happens exactly once, at the provider call itself.


class ScopedClientEscape(EffectScopeViolation):
    """An unfenced provider object was handed back into a fenced call."""


class _Fenced:
    __slots__ = ("_inner", "_scope", "_path")

    def __init__(self, inner: Any, scope: EffectScope, path: str = "") -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_scope", scope)
        object.__setattr__(self, "_path", path)

    def _check(self, path: Optional[str] = None) -> None:
        self._scope.assert_firestore_path(self._path if path is None else path)

    def _check_read(self, path: Optional[str] = None) -> None:
        self._scope.assert_firestore_read(self._path if path is None else path)


class ScopedSnapshot(_Fenced):
    """A read result. Its ``.reference`` is the classic escape, so it is fenced."""

    @property
    def exists(self) -> bool:
        return bool(getattr(self._inner, "exists", False))

    @property
    def id(self) -> str:
        return getattr(self._inner, "id", self._path.rsplit("/", 1)[-1])

    def to_dict(self) -> Any:
        return self._inner.to_dict()

    @property
    def reference(self) -> "ScopedDocument":
        return ScopedDocument(self._inner.reference, self._scope, self._path)

    def get(self, key: str, default: Any = None) -> Any:
        return (self.to_dict() or {}).get(key, default)


class ScopedDocument(_Fenced):
    @property
    def id(self) -> str:
        return getattr(self._inner, "id", self._path.rsplit("/", 1)[-1])

    @property
    def path(self) -> str:
        return self._path

    def collection(self, name: str) -> "ScopedCollection":
        return ScopedCollection(self._inner.collection(name), self._scope, f"{self._path}/{name}")

    def get(self, transaction: Any = None) -> ScopedSnapshot:
        self._check_read()
        inner = self._inner.get(transaction=_unwrap_transaction(transaction)) \
            if transaction is not None else self._inner.get()
        return ScopedSnapshot(inner, self._scope, self._path)

    def set(self, data: Any, merge: bool = False) -> Any:
        self._check()
        return self._inner.set(data, merge=merge)

    def update(self, data: Any) -> Any:
        self._check()
        return self._inner.update(data)

    def create(self, data: Any) -> Any:
        self._check()
        return self._inner.create(data)

    def delete(self) -> Any:
        self._check()
        return self._inner.delete()


class _ScopedQueryBase(_Fenced):
    def _rewrap(self, inner: Any) -> "ScopedQuery":
        return ScopedQuery(inner, self._scope, self._path)

    def where(self, *args: Any, **kwargs: Any) -> "ScopedQuery":
        return self._rewrap(self._inner.where(*args, **kwargs))

    def order_by(self, *args: Any, **kwargs: Any) -> "ScopedQuery":
        return self._rewrap(self._inner.order_by(*args, **kwargs))

    def limit(self, *args: Any, **kwargs: Any) -> "ScopedQuery":
        return self._rewrap(self._inner.limit(*args, **kwargs))

    def start_after(self, *args: Any, **kwargs: Any) -> "ScopedQuery":
        return self._rewrap(self._inner.start_after(*args, **kwargs))

    def stream(self, *args: Any, **kwargs: Any):
        self._check_read()
        for snapshot in self._inner.stream(*args, **kwargs):
            yield ScopedSnapshot(
                snapshot,
                self._scope,
                f"{self._path}/{getattr(snapshot, 'id', 'unknown')}",
            )

    def get(self, *args: Any, **kwargs: Any) -> list:
        return list(self.stream(*args, **kwargs))


class ScopedQuery(_ScopedQueryBase):
    pass


class ScopedCollection(_ScopedQueryBase):
    def document(self, name: str) -> ScopedDocument:
        return ScopedDocument(self._inner.document(name), self._scope, f"{self._path}/{name}")

    def add(self, data: Any) -> ScopedDocument:
        self._check()
        inner = self._inner.add(data)
        # Real Firestore returns (write_result, reference); fakes return the ref.
        if isinstance(inner, tuple):
            inner = inner[-1]
        return ScopedDocument(
            inner, self._scope, f"{self._path}/{getattr(inner, 'id', 'generated')}"
        )


def _unwrap_transaction(transaction: Any) -> Any:
    return transaction._inner if isinstance(transaction, _Fenced) else transaction


class ScopedTransaction(_Fenced):
    """Mutations are fenced by the reference's path, then unwrapped exactly once.

    A RAW reference arriving here is refused rather than delegated: it means the
    fence leaked somewhere upstream, and honouring it would write to whatever
    path that raw reference happens to carry.
    """

    def _ref(self, ref: Any) -> Any:
        if not isinstance(ref, ScopedDocument):
            raise ScopedClientEscape(
                "an unfenced document reference reached a scoped transaction: "
                f"{type(ref).__name__}"
            )
        self._scope.assert_firestore_path(ref.path)
        return ref._inner

    def set(self, ref: Any, data: Any, merge: bool = False) -> Any:
        return self._inner.set(self._ref(ref), data, merge=merge)

    def update(self, ref: Any, data: Any) -> Any:
        return self._inner.update(self._ref(ref), data)

    def create(self, ref: Any, data: Any) -> Any:
        return self._inner.create(self._ref(ref), data)

    def delete(self, ref: Any) -> Any:
        return self._inner.delete(self._ref(ref))

    # ``@firestore.transactional`` drives the transaction through this private
    # protocol; without it the decorator cannot run a fenced transaction at all.
    @property
    def _max_attempts(self) -> int:
        return getattr(self._inner, "_max_attempts", 1)

    @property
    def _read_only(self) -> bool:
        return getattr(self._inner, "_read_only", False)

    @property
    def _id(self) -> Any:
        return getattr(self._inner, "_id", None)

    def _clean_up(self) -> Any:
        return self._inner._clean_up()

    def _begin(self, retry_id: Any = None) -> Any:
        return self._inner._begin(retry_id=retry_id)

    def _commit(self) -> Any:
        return self._inner._commit()

    def _rollback(self) -> Any:
        return self._inner._rollback()


class ScopedBatch(ScopedTransaction):
    def commit(self) -> Any:
        return self._inner.commit()


class ScopedFirestore(_Fenced):
    """The fenced root. Everything reachable from here stays fenced."""

    def collection(self, name: str) -> ScopedCollection:
        return ScopedCollection(self._inner.collection(name), self._scope, name)

    def collection_group(self, name: str) -> ScopedQuery:
        return ScopedQuery(self._inner.collection_group(name), self._scope, name)

    def transaction(self, **kwargs: Any) -> ScopedTransaction:
        return ScopedTransaction(self._inner.transaction(**kwargs), self._scope, self._path)

    def batch(self) -> ScopedBatch:
        return ScopedBatch(self._inner.batch(), self._scope, self._path)


# --- sheets ----------------------------------------------------------------
#
# Row highlighting carries NO A1 range at all - only a numeric grid body - so a
# fence that validated A1 ranges alone would wave the single most common
# certification sheet write straight through. That is why ``EffectScope`` has a
# typed ``assert_sheet_request`` alongside ``assert_sheet_target``.


class ScopedSheetRequest:
    __slots__ = ("_inner",)

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        return self._inner.execute(*args, **kwargs)


class ScopedValues:
    __slots__ = ("_inner", "_scope")

    def __init__(self, inner: Any, scope: EffectScope) -> None:
        self._inner = inner
        self._scope = scope

    def get(self, **kwargs: Any) -> ScopedSheetRequest:
        self._scope.assert_sheet_target(kwargs.get("spreadsheetId") or "", kwargs.get("range") or "")
        return ScopedSheetRequest(self._inner.get(**kwargs))

    def update(self, **kwargs: Any) -> ScopedSheetRequest:
        spreadsheet_id = kwargs.get("spreadsheetId") or ""
        self._scope.assert_sheet_target(spreadsheet_id, kwargs.get("range") or "")
        self._scope.assert_sheet_request(spreadsheet_id, kwargs.get("body") or {})
        return ScopedSheetRequest(self._inner.update(**kwargs))

    def batchUpdate(self, **kwargs: Any) -> ScopedSheetRequest:  # noqa: N802 - Google API name
        self._scope.assert_sheet_request(
            kwargs.get("spreadsheetId") or "", kwargs.get("body") or {}
        )
        return ScopedSheetRequest(self._inner.batchUpdate(**kwargs))


class ScopedSpreadsheets:
    __slots__ = ("_inner", "_scope")

    def __init__(self, inner: Any, scope: EffectScope) -> None:
        self._inner = inner
        self._scope = scope

    def values(self) -> ScopedValues:
        return ScopedValues(self._inner.values(), self._scope)

    def get(self, **kwargs: Any) -> ScopedSheetRequest:
        spreadsheet_id = kwargs.get("spreadsheetId") or ""
        self._scope.assert_sheet_request(spreadsheet_id, {"metadata": True})
        return ScopedSheetRequest(self._inner.get(**kwargs))

    def batchUpdate(self, **kwargs: Any) -> ScopedSheetRequest:  # noqa: N802 - Google API name
        self._scope.assert_sheet_request(
            kwargs.get("spreadsheetId") or "", kwargs.get("body") or {}
        )
        return ScopedSheetRequest(self._inner.batchUpdate(**kwargs))


class ScopedSheets:
    __slots__ = ("_inner", "_scope")

    def __init__(self, inner: Any, scope: EffectScope) -> None:
        self._inner = inner
        self._scope = scope

    def spreadsheets(self) -> ScopedSpreadsheets:
        return ScopedSpreadsheets(self._inner.spreadsheets(), self._scope)


class DenyingDriveClient:
    """Deny-all. The first slice must make ZERO Drive calls, so there is nothing
    to allow yet - and an allow-list that is empty is best expressed as a wall."""

    def __getattr__(self, name: str) -> Any:
        raise EffectScopeViolation(
            f"the first certification slice may not reach Drive (.{name})"
        )


# --- resolution ------------------------------------------------------------


def firestore_for(runtime: Optional["AutomationRuntime"], ambient: Any) -> Any:
    """Return the runtime's fenced client, or ordinary ambient production.

    ``ambient`` is passed in rather than imported so each module keeps using its
    OWN ``_fs`` binding. ``clients._fs`` is imported by value into ten modules,
    so a helper that reached for one canonical global would silently disagree
    with whatever a caller had patched.
    """
    if runtime is not None and getattr(runtime, "firestore", None) is not None:
        return runtime.firestore
    return ambient


def sheets_for(runtime: Optional["AutomationRuntime"], ambient_factory: Callable[[], Any]) -> Any:
    """Return the runtime's fenced Sheets service, else build the ordinary one.

    The ambient side is a FACTORY, not a value: building a production Sheets
    client performs an OAuth refresh, and a certification run must never pay for
    - or trigger - one.
    """
    if runtime is not None and getattr(runtime, "sheets", None) is not None:
        return runtime.sheets
    return ambient_factory()


def clock_for(
    runtime: Optional["AutomationRuntime"],
    ambient: Callable[[], datetime],
) -> Callable[[], datetime]:
    """Return the request clock, or the CALLER'S OWN ambient clock.

    The ambient clock is passed in for the same reason ``firestore_for`` takes the
    caller's ``_fs``: a module's clock is a seam its callers already reach for.
    Substituting one canonical UTC function here would silently defeat every
    caller that freezes time by patching its own ``datetime`` - production would
    be unchanged and the tests around it would quietly stop constraining anything.
    """
    if runtime is not None and getattr(runtime, "now", None) is not None:
        return runtime.now
    return ambient


def is_certification(runtime: Optional["AutomationRuntime"]) -> bool:
    return runtime is not None and runtime.certification_run_id is not None
