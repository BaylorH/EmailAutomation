"""Transactional Firestore persistence for provider-effect receipts.

All attempt accounting and ownership changes happen in Firestore transactions.
Receipt documents deliberately omit raw recipients, message bodies, thread
identifiers, client identifiers, and run identifiers.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from typing import Any, Callable

from .effect_gateway import (
    AttemptLimits,
    AttemptReservation,
    AuthoritativeDecision,
    AuthorityState,
    EffectReceipt,
    ProviderEffectRequest,
    ReceiptState,
)


_RECEIPT_COLLECTION = "effectReceipts"
_RUN_COLLECTION = "providerEffectRuns"
_DURABLE_FINAL_STATES = frozenset(
    {
        ReceiptState.SUCCEEDED,
        ReceiptState.CANCELLED,
        ReceiptState.TERMINAL_FAILED,
        ReceiptState.RECONCILIATION_REQUIRED,
    }
)
_NON_RETRYABLE_STATES = _DURABLE_FINAL_STATES | frozenset(
    {
        ReceiptState.CLAIMED,
        ReceiptState.PROVIDER_ACCEPTED,
    }
)
_ALLOWED_TRANSITIONS = {
    ReceiptState.CLAIMED: frozenset(
        {
            ReceiptState.PREPARED,
            ReceiptState.PROVIDER_ACCEPTED,
            ReceiptState.CANCELLED,
            ReceiptState.TERMINAL_FAILED,
            ReceiptState.RECONCILIATION_REQUIRED,
        }
    ),
    ReceiptState.PROVIDER_ACCEPTED: frozenset(
        {
            ReceiptState.SUCCEEDED,
            ReceiptState.RECONCILIATION_REQUIRED,
        }
    ),
}


def _hash_identity(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def _require_exact_identity(label: str, value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be an exact non-empty string")
    return value


class FirestoreEffectReceiptStore:
    """Firestore-backed, atomic implementation of ``EffectReceiptStore``."""

    def __init__(
        self,
        firestore_client,
        *,
        authority_reader: Callable[..., Any] | None = None,
        transactional_runner: Callable[[Callable[..., Any]], Callable[..., Any]]
        | None = None,
        server_timestamp: Any = None,
        owner_token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._firestore = firestore_client
        if authority_reader is None:
            from .campaign_safety import get_client_automation_decision

            authority_reader = get_client_automation_decision
        self._authority_reader = authority_reader

        if transactional_runner is None:
            from google.cloud import firestore

            transactional_runner = firestore.transactional
        self._transactional_runner = transactional_runner

        if server_timestamp is None:
            from google.cloud.firestore import SERVER_TIMESTAMP

            server_timestamp = SERVER_TIMESTAMP
        self._server_timestamp = server_timestamp
        self._owner_token_factory = owner_token_factory or (
            lambda: uuid.uuid4().hex
        )

    def _receipt_reference(self, request: ProviderEffectRequest):
        user_id = _require_exact_identity("user_id", request.user_id)
        return (
            self._firestore.collection("users")
            .document(user_id)
            .collection(_RECEIPT_COLLECTION)
            .document(request.effect_id)
        )

    def _run_reference(self, request: ProviderEffectRequest):
        return self._firestore.collection(_RUN_COLLECTION).document(
            _hash_identity("run", request.run_id)
        )

    @staticmethod
    def _snapshot_payload(snapshot) -> dict[str, Any] | None:
        if snapshot is None or not getattr(snapshot, "exists", False):
            return None
        payload = snapshot.to_dict()
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _receipt_from_payload(payload: dict[str, Any]) -> EffectReceipt:
        try:
            state = ReceiptState(payload.get("state"))
        except (TypeError, ValueError) as error:
            raise ValueError("effect receipt has an invalid state") from error
        return EffectReceipt(
            effect_id=str(payload.get("effectId") or ""),
            content_idempotency_key=str(
                payload.get("contentIdempotencyKey") or ""
            ),
            state=state,
            attempts=int(payload.get("attempts") or 0),
            reason=str(payload.get("reason") or ""),
            provider_reference=str(payload.get("providerReference") or ""),
            claim_owner=str(payload.get("claimOwner") or ""),
            version=int(payload.get("version") or 0),
        )

    def _receipt_payload(
        self,
        request: ProviderEffectRequest,
        receipt: EffectReceipt,
    ) -> dict[str, Any]:
        return {
            "effectId": receipt.effect_id,
            "contentIdempotencyKey": receipt.content_idempotency_key,
            "state": receipt.state.value,
            "attempts": receipt.attempts,
            "reason": receipt.reason,
            "providerReference": receipt.provider_reference,
            "provider": request.provider,
            "effectType": request.effect_type,
            "claimOwner": receipt.claim_owner,
            "version": receipt.version,
            "updatedAt": self._server_timestamp,
        }

    def _run_transaction(self, callback):
        transaction = self._firestore.transaction()
        return self._transactional_runner(callback)(transaction)

    def load_receipt(
        self,
        request: ProviderEffectRequest,
    ) -> EffectReceipt | None:
        payload = self._snapshot_payload(self._receipt_reference(request).get())
        return self._receipt_from_payload(payload) if payload else None

    def create_blocked_if_absent(
        self,
        request: ProviderEffectRequest,
        reason: str,
    ) -> EffectReceipt:
        reference = self._receipt_reference(request)

        def settle(transaction):
            payload = self._snapshot_payload(transaction.get(reference))
            if payload is not None:
                return self._receipt_from_payload(payload)
            receipt = EffectReceipt(
                effect_id=request.effect_id,
                content_idempotency_key=request.content_idempotency_key,
                state=ReceiptState.BLOCKED,
                attempts=0,
                reason=str(reason or ""),
                version=1,
            )
            transaction.set(reference, self._receipt_payload(request, receipt))
            return receipt

        return self._run_transaction(settle)

    def reserve_attempt(
        self,
        request: ProviderEffectRequest,
        limits: AttemptLimits,
    ) -> AttemptReservation:
        reference = self._receipt_reference(request)
        run_reference = self._run_reference(request)
        owner_token = _require_exact_identity(
            "owner_token",
            self._owner_token_factory(),
        )
        user_counter_key = _hash_identity("user", request.user_id)
        provider_counter_key = _hash_identity(
            "provider",
            request.provider,
        )

        def reserve(transaction):
            receipt_payload = self._snapshot_payload(
                transaction.get(reference)
            )
            run_payload = self._snapshot_payload(
                transaction.get(run_reference)
            ) or {}
            current = (
                self._receipt_from_payload(receipt_payload)
                if receipt_payload is not None
                else None
            )

            if current is not None:
                if (
                    current.content_idempotency_key
                    != request.content_idempotency_key
                ):
                    # Never mutate the original lifecycle: it may be owned by
                    # an in-flight sender or already prove a durable success.
                    # The conflicting caller gets a fail-closed result while
                    # the authoritative receipt remains monotonic.
                    conflict = EffectReceipt(
                        effect_id=request.effect_id,
                        content_idempotency_key=(
                            request.content_idempotency_key
                        ),
                        state=ReceiptState.TERMINAL_FAILED,
                        attempts=current.attempts,
                        reason="content_identity_conflict",
                        version=current.version,
                    )
                    return AttemptReservation(conflict, False)
                if current.state in _NON_RETRYABLE_STATES:
                    return AttemptReservation(current, False)

            attempts = current.attempts if current is not None else 0
            current_version = current.version if current is not None else 0
            if attempts >= limits.max_attempts:
                exhausted = EffectReceipt(
                    effect_id=request.effect_id,
                    content_idempotency_key=(
                        request.content_idempotency_key
                    ),
                    state=ReceiptState.TERMINAL_FAILED,
                    attempts=attempts,
                    reason="provider_attempts_exhausted",
                    version=current_version + 1,
                )
                transaction.set(
                    reference,
                    self._receipt_payload(request, exhausted),
                )
                return AttemptReservation(exhausted, False)

            total_attempts = int(run_payload.get("attempts") or 0)
            user_attempts = dict(run_payload.get("users") or {})
            provider_attempts = dict(run_payload.get("providers") or {})
            cap_checks = (
                (
                    total_attempts,
                    limits.max_per_run,
                    "run_cap_reached",
                ),
                (
                    int(user_attempts.get(user_counter_key) or 0),
                    limits.max_per_user,
                    "user_cap_reached",
                ),
                (
                    int(provider_attempts.get(provider_counter_key) or 0),
                    limits.max_per_provider,
                    "provider_cap_reached",
                ),
            )
            for count, cap, reason in cap_checks:
                if count >= cap:
                    blocked = EffectReceipt(
                        effect_id=request.effect_id,
                        content_idempotency_key=(
                            request.content_idempotency_key
                        ),
                        state=ReceiptState.BLOCKED,
                        attempts=attempts,
                        reason=reason,
                        version=current_version + 1,
                    )
                    transaction.set(
                        reference,
                        self._receipt_payload(request, blocked),
                    )
                    return AttemptReservation(blocked, False)

            claimed = EffectReceipt(
                effect_id=request.effect_id,
                content_idempotency_key=request.content_idempotency_key,
                state=ReceiptState.CLAIMED,
                attempts=attempts + 1,
                claim_owner=owner_token,
                version=current_version + 1,
            )
            user_attempts[user_counter_key] = (
                int(user_attempts.get(user_counter_key) or 0) + 1
            )
            provider_attempts[provider_counter_key] = (
                int(provider_attempts.get(provider_counter_key) or 0) + 1
            )
            transaction.set(
                run_reference,
                {
                    "attempts": total_attempts + 1,
                    "users": user_attempts,
                    "providers": provider_attempts,
                    "updatedAt": self._server_timestamp,
                },
            )
            transaction.set(
                reference,
                self._receipt_payload(request, claimed),
            )
            return AttemptReservation(
                receipt=claimed,
                acquired=True,
                owner_token=owner_token,
                version=claimed.version,
            )

        return self._run_transaction(reserve)

    def read_authoritative_state(
        self,
        request: ProviderEffectRequest,
    ) -> AuthoritativeDecision:
        user_id = _require_exact_identity("user_id", request.user_id)
        client_id = _require_exact_identity(
            "authority_client_id",
            request.authority_client_id,
        )
        decision = self._authority_reader(
            user_id,
            client_id,
            firestore_client=self._firestore,
        )
        state = str(getattr(decision, "state", "") or "")
        reason = str(getattr(decision, "reason", "") or "")
        metadata = getattr(decision, "metadata", {}) or {}
        if state == "allow":
            authority_state = AuthorityState.ACTIVE
        elif bool(metadata.get("terminal")):
            authority_state = AuthorityState.TERMINAL
        else:
            authority_state = AuthorityState.PAUSED
        return AuthoritativeDecision(authority_state, reason)

    def transition(
        self,
        request: ProviderEffectRequest,
        state: ReceiptState,
        *,
        reason: str = "",
        provider_reference: str = "",
        expected_owner: str = "",
        expected_version: int = 0,
    ) -> EffectReceipt:
        reference = self._receipt_reference(request)

        def apply_transition(transaction):
            payload = self._snapshot_payload(transaction.get(reference))
            if payload is None:
                raise RuntimeError("effect receipt does not exist")
            current = self._receipt_from_payload(payload)
            if (
                current.claim_owner != expected_owner
                or current.version != expected_version
            ):
                return current
            if current.state in _DURABLE_FINAL_STATES:
                return current
            if state not in _ALLOWED_TRANSITIONS.get(
                current.state,
                frozenset(),
            ):
                return current
            updated = replace(
                current,
                state=state,
                reason=str(reason or ""),
                provider_reference=(
                    provider_reference or current.provider_reference
                ),
                version=current.version + 1,
            )
            transaction.set(
                reference,
                self._receipt_payload(request, updated),
            )
            return updated

        return self._run_transaction(apply_transition)

    def read_operator_receipt(
        self,
        request: ProviderEffectRequest,
    ) -> dict[str, Any] | None:
        payload = self._snapshot_payload(self._receipt_reference(request).get())
        if payload is None:
            return None
        return {
            "effectId": payload.get("effectId", ""),
            "contentIdempotencyKey": payload.get(
                "contentIdempotencyKey",
                "",
            ),
            "state": payload.get("state", ""),
            "attempts": int(payload.get("attempts") or 0),
            "reason": payload.get("reason", ""),
            "providerReference": payload.get("providerReference", ""),
            "provider": payload.get("provider", ""),
            "effectType": payload.get("effectType", ""),
            "version": int(payload.get("version") or 0),
            "updatedAt": payload.get("updatedAt"),
        }
