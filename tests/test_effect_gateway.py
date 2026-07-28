"""Credential-free contracts for the provider-effect gateway."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import os
import sys
import threading
import unittest
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from email_automation.effect_gateway import (
    AttemptLimits,
    AttemptReservation,
    AuthoritativeDecision,
    AuthorityState,
    EffectGateway,
    EffectGatewayConfig,
    EffectReceipt,
    ProviderEffectRequest,
    ProviderEffectResult,
    ReceiptState,
    RetryableProviderError,
    TerminalProviderError,
    UncertainProviderOutcomeError,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class InMemoryReceiptStore:
    """Transactional in-memory fake for the persistence boundary."""

    def __init__(self, events=None):
        self._lock = threading.RLock()
        self.events = events if events is not None else []
        self.receipts = {}
        self.histories = defaultdict(list)
        self.run_attempts = defaultdict(int)
        self.user_attempts = defaultdict(int)
        self.provider_attempts = defaultdict(int)
        self.authority = {}
        self.before_authoritative_read = None
        self.fail_transition_once = set()

    def _save(self, receipt):
        with self._lock:
            self.receipts[receipt.effect_id] = receipt
            self.histories[receipt.effect_id].append(receipt)
            return receipt

    def load_receipt(self, request):
        with self._lock:
            self.events.append(("receipt_read", request.effect_id))
            return self.receipts.get(request.effect_id)

    def create_blocked_if_absent(self, request, reason):
        with self._lock:
            current = self.receipts.get(request.effect_id)
            if current is not None:
                return current
            return self._save(
                EffectReceipt(
                    effect_id=request.effect_id,
                    content_idempotency_key=request.content_idempotency_key,
                    state=ReceiptState.BLOCKED,
                    attempts=0,
                    reason=reason,
                )
            )

    def reserve_attempt(self, request, limits):
        """Atomically enforces receipt state, identity, bounds, and counters."""
        with self._lock:
            self.events.append(("reserve", request.effect_id))
            current = self.receipts.get(request.effect_id)
            if current:
                if (
                    current.content_idempotency_key
                    != request.content_idempotency_key
                ):
                    return AttemptReservation(
                        receipt=self._save(
                            replace(
                                current,
                                state=ReceiptState.TERMINAL_FAILED,
                                reason="content_identity_conflict",
                            )
                        ),
                        acquired=False,
                    )
                if current.state in {
                    ReceiptState.CLAIMED,
                    ReceiptState.PROVIDER_ACCEPTED,
                    ReceiptState.SUCCEEDED,
                    ReceiptState.CANCELLED,
                    ReceiptState.TERMINAL_FAILED,
                    ReceiptState.RECONCILIATION_REQUIRED,
                }:
                    return AttemptReservation(receipt=current, acquired=False)

            attempts = current.attempts if current else 0
            if current is None or current.state == ReceiptState.BLOCKED:
                current = self._save(
                    EffectReceipt(
                        effect_id=request.effect_id,
                        content_idempotency_key=request.content_idempotency_key,
                        state=ReceiptState.PREPARED,
                        attempts=attempts,
                    )
                )

            if attempts >= limits.max_attempts:
                receipt = self._save(
                    EffectReceipt(
                        request.effect_id,
                        request.content_idempotency_key,
                        ReceiptState.TERMINAL_FAILED,
                        attempts,
                        "provider_attempts_exhausted",
                    )
                )
                return AttemptReservation(receipt=receipt, acquired=False)

            cap_checks = (
                (
                    self.run_attempts[request.run_id],
                    limits.max_per_run,
                    "run_cap_reached",
                ),
                (
                    self.user_attempts[(request.run_id, request.user_id)],
                    limits.max_per_user,
                    "user_cap_reached",
                ),
                (
                    self.provider_attempts[(request.run_id, request.provider)],
                    limits.max_per_provider,
                    "provider_cap_reached",
                ),
            )
            for count, cap, reason in cap_checks:
                if count >= cap:
                    return AttemptReservation(
                        receipt=self._save(
                            EffectReceipt(
                                effect_id=request.effect_id,
                                content_idempotency_key=(
                                    request.content_idempotency_key
                                ),
                                state=ReceiptState.BLOCKED,
                                attempts=attempts,
                                reason=reason,
                            )
                        ),
                        acquired=False,
                    )

            self.run_attempts[request.run_id] += 1
            self.user_attempts[(request.run_id, request.user_id)] += 1
            self.provider_attempts[(request.run_id, request.provider)] += 1
            receipt = self._save(
                EffectReceipt(
                    request.effect_id,
                    request.content_idempotency_key,
                    ReceiptState.CLAIMED,
                    attempts + 1,
                )
            )
            return AttemptReservation(receipt=receipt, acquired=True)

    def read_authoritative_state(self, request):
        with self._lock:
            self.events.append(("authoritative_read", request.effect_id))
            if self.before_authoritative_read:
                self.before_authoritative_read(request)
            return self.authority.get(
                request.effect_id,
                AuthoritativeDecision(AuthorityState.ACTIVE),
            )

    def transition(
        self,
        request,
        state,
        *,
        reason="",
        provider_reference="",
    ):
        with self._lock:
            current = self.receipts[request.effect_id]
            self.events.append(("transition", state.value))
            if state in self.fail_transition_once:
                self.fail_transition_once.remove(state)
                raise RuntimeError(
                    f"simulated {state.value} persistence failure"
                )
            return self._save(
                replace(
                    current,
                    state=state,
                    reason=reason,
                    provider_reference=provider_reference,
                )
            )


class InMemoryProvider:
    def __init__(self, outcomes=None, events=None):
        self.outcomes = list(outcomes or [ProviderEffectResult("provider-ok")])
        self.events = events if events is not None else []
        self.calls = []

    def execute(self, request):
        self.events.append(("provider", request.effect_id))
        self.calls.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class AdversarialInterleavingReceiptStore(InMemoryReceiptStore):
    """Force both contenders to observe absence before the first claim."""

    def __init__(self):
        super().__init__()
        self._first_loaded = threading.Event()
        self._second_loaded = threading.Event()
        self._first_claimed = threading.Event()

    @staticmethod
    def _wait(event):
        if not event.wait(timeout=5):
            raise RuntimeError("concurrency test interleaving timed out")

    def load_receipt(self, request):
        receipt = super().load_receipt(request)
        if threading.current_thread().name == "contender-a":
            self._first_loaded.set()
            self._wait(self._second_loaded)
        elif threading.current_thread().name == "contender-b":
            self._second_loaded.set()
            self._wait(self._first_loaded)
            self._wait(self._first_claimed)
        return receipt

    def reserve_attempt(self, request, limits):
        reservation = super().reserve_attempt(request, limits)
        if (
            threading.current_thread().name == "contender-a"
            and reservation.acquired
        ):
            self._first_claimed.set()
        return reservation


class DelayedBlockedSettlementReceiptStore(InMemoryReceiptStore):
    """Pause a stale absent read until another worker finishes its lifecycle."""

    def __init__(self):
        super().__init__()
        self.blocked_load_observed = threading.Event()
        self.release_blocked_settlement = threading.Event()

    def load_receipt(self, request):
        receipt = super().load_receipt(request)
        if threading.current_thread().name == "delayed-blocked":
            if receipt is not None:
                raise RuntimeError("expected delayed worker to observe absence")
            self.blocked_load_observed.set()
            if not self.release_blocked_settlement.wait(timeout=5):
                raise RuntimeError("blocked-settlement interleaving timed out")
        return receipt


class ThreadSafeProvider(InMemoryProvider):
    def __init__(self, outcomes=None, events=None):
        super().__init__(outcomes, events)
        self._lock = threading.Lock()

    def execute(self, request):
        with self._lock:
            return super().execute(request)


def request(
    effect_key="thread-1:reply-1",
    *,
    run_id="run-1",
    user_id="user-1",
    provider="graph",
    content=None,
):
    return ProviderEffectRequest.create(
        run_id=run_id,
        user_id=user_id,
        provider=provider,
        effect_type="mail.reply",
        effect_key=effect_key,
        content=content or {
            "to": ["broker@example.test"],
            "subject": "Re: 100 Main",
            "body": "Thanks",
        },
    )


def live_config(**overrides):
    values = {
        "enabled": True,
        "global_effects_enabled": True,
        "limits": AttemptLimits(
            max_attempts=3,
            max_per_run=20,
            max_per_user=10,
            max_per_provider=10,
        ),
    }
    values.update(overrides)
    return EffectGatewayConfig(**values)


def receipt_reference(raw_reference):
    digest = hashlib.sha256(raw_reference.encode("utf-8")).hexdigest()
    return f"provider_ref_{digest}"


class EffectIdentityTests(unittest.TestCase):
    def test_receipt_state_values_match_the_frozen_durable_lifecycle(self):
        self.assertEqual(
            {
                state.value
                for state in ReceiptState
                if state is not ReceiptState.BLOCKED
            },
            {
                "prepared",
                "claimed",
                "provider_accepted",
                "succeeded",
                "cancelled",
                "terminal_failed",
                "reconciliation_required",
            },
        )

    def test_effect_and_content_identities_are_distinct_stable_and_scoped(self):
        first = request(content={"subject": "Hi", "body": "same", "to": ["a@test"]})
        reordered = request(
            run_id="later-run",
            content={"to": ["a@test"], "body": "same", "subject": "Hi"},
        )
        edited = request(
            run_id="later-run",
            content={"to": ["a@test"], "body": "edited", "subject": "Hi"},
        )
        another_user = request(
            user_id="user-2",
            content={"subject": "Hi", "body": "same", "to": ["a@test"]},
        )

        self.assertEqual(first.effect_id, reordered.effect_id)
        self.assertEqual(
            first.content_idempotency_key,
            reordered.content_idempotency_key,
        )
        self.assertNotEqual(first.effect_id, first.content_idempotency_key)
        self.assertEqual(first.effect_id, edited.effect_id)
        self.assertNotEqual(
            first.content_idempotency_key,
            edited.content_idempotency_key,
        )
        self.assertNotEqual(first.effect_id, another_user.effect_id)
        self.assertEqual(
            first.to_dict()["effectId"],
            first.effect_id,
        )
        self.assertEqual(
            first.to_dict()["contentIdempotencyKey"],
            first.content_idempotency_key,
        )


class EffectGatewayGateTests(unittest.TestCase):
    _CAP_ENVIRONMENT = {
        "SITESIFT_EFFECT_MAX_ATTEMPTS": "3",
        "SITESIFT_EFFECT_MAX_PER_RUN": "20",
        "SITESIFT_EFFECT_MAX_PER_USER": "10",
        "SITESIFT_EFFECT_MAX_PER_PROVIDER": "10",
    }

    def test_no_environment_configuration_means_zero_provider_effects(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            config = EffectGatewayConfig.from_env()
        store = InMemoryReceiptStore()
        provider = InMemoryProvider()

        receipt = EffectGateway(store, {"graph": provider}, config).execute(request())

        self.assertFalse(config.enabled)
        self.assertFalse(config.global_effects_enabled)
        self.assertEqual(receipt.state, ReceiptState.BLOCKED)
        self.assertEqual(receipt.reason, "gateway_disabled")
        self.assertEqual(provider.calls, [])
        self.assertIs(store.receipts[receipt.effect_id], receipt)

    def test_environment_authorization_requires_exact_unmodified_bytes(self):
        cases = (
            ("TRUE", "live", "gateway_disabled"),
            ("True", "live", "gateway_disabled"),
            (" true", "live", "gateway_disabled"),
            ("true ", "live", "gateway_disabled"),
            ("true", "LIVE", "global_kill"),
            ("true", "Live", "global_kill"),
            ("true", " live", "global_kill"),
            ("true", "live ", "global_kill"),
        )
        for enabled_value, global_value, expected_reason in cases:
            with self.subTest(
                enabled=enabled_value,
                global_mode=global_value,
            ):
                environment = {
                    **self._CAP_ENVIRONMENT,
                    "SITESIFT_PROVIDER_EFFECTS_ENABLED": enabled_value,
                    "SITESIFT_OUTBOUND_MODE": global_value,
                }
                with mock.patch.dict(os.environ, environment, clear=True):
                    config = EffectGatewayConfig.from_env()
                store = InMemoryReceiptStore()
                provider = InMemoryProvider()

                receipt = EffectGateway(
                    store,
                    {"graph": provider},
                    config,
                ).execute(request())

                self.assertEqual(receipt.state, ReceiptState.BLOCKED)
                self.assertEqual(receipt.reason, expected_reason)
                self.assertEqual(provider.calls, [])

    def test_exact_environment_authorization_allows_one_effect(self):
        environment = {
            **self._CAP_ENVIRONMENT,
            "SITESIFT_PROVIDER_EFFECTS_ENABLED": "true",
            "SITESIFT_OUTBOUND_MODE": "live",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            config = EffectGatewayConfig.from_env()
        store = InMemoryReceiptStore()
        provider = InMemoryProvider()

        receipt = EffectGateway(
            store,
            {"graph": provider},
            config,
        ).execute(request())

        self.assertTrue(config.enabled)
        self.assertTrue(config.global_effects_enabled)
        self.assertEqual(receipt.state, ReceiptState.SUCCEEDED)
        self.assertEqual(len(provider.calls), 1)

    def test_global_kill_blocks_an_explicitly_enabled_gateway(self):
        store = InMemoryReceiptStore()
        provider = InMemoryProvider()
        config = live_config(global_effects_enabled=False)

        receipt = EffectGateway(store, {"graph": provider}, config).execute(request())

        self.assertEqual(receipt.state, ReceiptState.BLOCKED)
        self.assertEqual(receipt.reason, "global_kill")
        self.assertEqual(provider.calls, [])

    def test_invalid_or_missing_caps_fail_closed(self):
        for limits in (
            AttemptLimits(),
            AttemptLimits(3, 1, 1, 0),
        ):
            with self.subTest(limits=limits):
                store = InMemoryReceiptStore()
                provider = InMemoryProvider()
                receipt = EffectGateway(
                    store,
                    {"graph": provider},
                    live_config(limits=limits),
                ).execute(request())

                self.assertEqual(receipt.state, ReceiptState.BLOCKED)
                self.assertEqual(receipt.reason, "invalid_or_missing_caps")
                self.assertEqual(provider.calls, [])

    def test_blocked_settlement_preserves_every_existing_durable_state(self):
        req = request()
        durable_states = (
            ReceiptState.CLAIMED,
            ReceiptState.PROVIDER_ACCEPTED,
            ReceiptState.SUCCEEDED,
            ReceiptState.CANCELLED,
            ReceiptState.TERMINAL_FAILED,
            ReceiptState.RECONCILIATION_REQUIRED,
        )
        for state in durable_states:
            with self.subTest(state=state):
                store = InMemoryReceiptStore()
                existing = store._save(
                    EffectReceipt(
                        effect_id=req.effect_id,
                        content_idempotency_key=req.content_idempotency_key,
                        state=state,
                        attempts=1,
                        provider_reference=(
                            receipt_reference("graph-message-1")
                            if state
                            in {
                                ReceiptState.PROVIDER_ACCEPTED,
                                ReceiptState.SUCCEEDED,
                            }
                            else ""
                        ),
                    )
                )
                history_before = tuple(store.histories[req.effect_id])

                settled = store.create_blocked_if_absent(
                    req,
                    "gateway_disabled",
                )

                self.assertIs(settled, existing)
                self.assertIs(store.receipts[req.effect_id], existing)
                self.assertEqual(
                    tuple(store.histories[req.effect_id]),
                    history_before,
                )

    def test_transactional_reservation_enforces_run_user_and_provider_caps(self):
        cases = (
            (
                AttemptLimits(3, 1, 10, 10),
                request("effect-a"),
                request("effect-b", user_id="user-2"),
                "run_cap_reached",
            ),
            (
                AttemptLimits(3, 10, 1, 10),
                request("effect-a"),
                request("effect-b"),
                "user_cap_reached",
            ),
            (
                AttemptLimits(3, 10, 10, 1),
                request("effect-a"),
                request("effect-b", user_id="user-2"),
                "provider_cap_reached",
            ),
        )
        for limits, first, second, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                store = InMemoryReceiptStore()
                provider = InMemoryProvider(
                    [ProviderEffectResult("one"), ProviderEffectResult("two")]
                )
                gateway = EffectGateway(
                    store,
                    {"graph": provider},
                    live_config(limits=limits),
                )

                self.assertEqual(gateway.execute(first).state, ReceiptState.SUCCEEDED)
                blocked = gateway.execute(second)

                self.assertEqual(blocked.state, ReceiptState.BLOCKED)
                self.assertEqual(blocked.reason, expected_reason)
                self.assertEqual(len(provider.calls), 1)


class EffectGatewayConcurrencyTests(unittest.TestCase):
    def test_two_contenders_share_one_atomic_lifecycle(self):
        req = request()
        store = AdversarialInterleavingReceiptStore()
        provider = ThreadSafeProvider(
            [
                ProviderEffectResult("graph-message-1"),
                ProviderEffectResult("graph-message-2"),
            ]
        )
        gateway = EffectGateway(store, {"graph": provider}, live_config())
        receipts = {}
        failures = {}

        def execute(name):
            try:
                receipts[name] = gateway.execute(req)
            except Exception as error:  # pragma: no cover - failure diagnostics
                failures[name] = error

        contenders = [
            threading.Thread(
                target=execute,
                args=("a",),
                name="contender-a",
            ),
            threading.Thread(
                target=execute,
                args=("b",),
                name="contender-b",
            ),
        ]
        for contender in contenders:
            contender.start()
        for contender in contenders:
            contender.join(timeout=5)

        self.assertFalse(
            any(contender.is_alive() for contender in contenders),
            "concurrent gateway execution deadlocked",
        )
        self.assertEqual(failures, {})
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(
            [receipt.state for receipt in store.histories[req.effect_id]],
            [
                ReceiptState.PREPARED,
                ReceiptState.CLAIMED,
                ReceiptState.PROVIDER_ACCEPTED,
                ReceiptState.SUCCEEDED,
            ],
        )
        self.assertEqual(
            store.receipts[req.effect_id].state,
            ReceiptState.SUCCEEDED,
        )
        self.assertEqual(set(receipts), {"a", "b"})

    def test_delayed_blocked_settlement_never_regresses_success(self):
        blocked_configs = (
            ("gateway_disabled", live_config(enabled=False)),
            ("global_kill", live_config(global_effects_enabled=False)),
            (
                "invalid_or_missing_caps",
                live_config(limits=AttemptLimits()),
            ),
        )
        for expected_reason, blocked_config in blocked_configs:
            with self.subTest(reason=expected_reason):
                req = request()
                store = DelayedBlockedSettlementReceiptStore()
                provider = ThreadSafeProvider(
                    [
                        ProviderEffectResult("graph-message-1"),
                        ProviderEffectResult("must-not-run"),
                    ]
                )
                enabled_gateway = EffectGateway(
                    store,
                    {"graph": provider},
                    live_config(),
                )
                blocked_gateway = EffectGateway(
                    store,
                    {"graph": provider},
                    blocked_config,
                )
                delayed_result = {}
                delayed_failure = {}

                def execute_delayed_block():
                    try:
                        delayed_result["receipt"] = blocked_gateway.execute(req)
                    except Exception as error:
                        delayed_failure["error"] = error

                delayed = threading.Thread(
                    target=execute_delayed_block,
                    name="delayed-blocked",
                )
                delayed.start()
                self.assertTrue(
                    store.blocked_load_observed.wait(timeout=5),
                    "delayed worker did not observe receipt absence",
                )

                succeeded = enabled_gateway.execute(req)
                store.release_blocked_settlement.set()
                delayed.join(timeout=5)
                retry = enabled_gateway.execute(req)

                self.assertFalse(
                    delayed.is_alive(),
                    "delayed blocked settlement deadlocked",
                )
                self.assertEqual(delayed_failure, {})
                self.assertEqual(
                    delayed_result["receipt"],
                    succeeded,
                    expected_reason,
                )
                self.assertEqual(retry, succeeded)
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(
                    [
                        receipt.state
                        for receipt in store.histories[req.effect_id]
                    ],
                    [
                        ReceiptState.PREPARED,
                        ReceiptState.CLAIMED,
                        ReceiptState.PROVIDER_ACCEPTED,
                        ReceiptState.SUCCEEDED,
                    ],
                )


class EffectGatewayAuthorityTests(unittest.TestCase):
    def test_authoritative_cancellation_landing_at_final_read_prevents_provider(self):
        events = []
        req = request()
        store = InMemoryReceiptStore(events)
        provider = InMemoryProvider(events=events)

        def cancel_at_read(current_request):
            store.authority[current_request.effect_id] = AuthoritativeDecision(
                AuthorityState.CANCELLED,
                "operator_cancelled",
            )

        store.before_authoritative_read = cancel_at_read
        receipt = EffectGateway(
            store,
            {"graph": provider},
            live_config(),
        ).execute(req)

        self.assertEqual(receipt.state, ReceiptState.CANCELLED)
        self.assertEqual(receipt.reason, "authoritative_cancelled")
        self.assertEqual(provider.calls, [])
        self.assertIn(("authoritative_read", req.effect_id), events)

    def test_authoritative_terminal_state_landing_at_final_read_prevents_provider(self):
        events = []
        req = request()
        store = InMemoryReceiptStore(events)
        provider = InMemoryProvider(events=events)
        store.before_authoritative_read = lambda current_request: store.authority.__setitem__(
            current_request.effect_id,
            AuthoritativeDecision(AuthorityState.TERMINAL, "campaign_stopped"),
        )

        receipt = EffectGateway(
            store,
            {"graph": provider},
            live_config(),
        ).execute(req)

        self.assertEqual(receipt.state, ReceiptState.TERMINAL_FAILED)
        self.assertEqual(receipt.reason, "authoritative_terminal")
        self.assertEqual(provider.calls, [])

    def test_authoritative_read_is_the_last_external_step_before_provider(self):
        events = []
        req = request()
        store = InMemoryReceiptStore(events)
        provider = InMemoryProvider(events=events)

        receipt = EffectGateway(
            store,
            {"graph": provider},
            live_config(),
        ).execute(req)

        self.assertEqual(receipt.state, ReceiptState.SUCCEEDED)
        provider_index = events.index(("provider", req.effect_id))
        self.assertEqual(
            events[provider_index - 1],
            ("authoritative_read", req.effect_id),
        )


class EffectGatewayReceiptTests(unittest.TestCase):
    def test_succeeded_retry_is_idempotent_and_never_calls_provider_twice(self):
        store = InMemoryReceiptStore()
        provider = InMemoryProvider([ProviderEffectResult("graph-message-1")])
        gateway = EffectGateway(store, {"graph": provider}, live_config())
        req = request()

        first = gateway.execute(req)
        retry = gateway.execute(req)

        self.assertEqual(first.state, ReceiptState.SUCCEEDED)
        self.assertEqual(
            first.provider_reference,
            receipt_reference("graph-message-1"),
        )
        self.assertEqual(retry, first)
        self.assertEqual(len(provider.calls), 1)

    def test_definite_no_effect_failures_retry_only_to_the_attempt_bound(self):
        req = request()
        limits = AttemptLimits(2, 10, 10, 10)
        store = InMemoryReceiptStore()
        provider = InMemoryProvider(
            [
                RetryableProviderError("request rejected before provider accepted it"),
                RetryableProviderError("request rejected before provider accepted it"),
            ]
        )
        gateway = EffectGateway(
            store,
            {"graph": provider},
            live_config(limits=limits),
        )

        first = gateway.execute(req)
        second = gateway.execute(req)
        third = gateway.execute(req)

        self.assertEqual(first.state, ReceiptState.PREPARED)
        self.assertEqual(second.state, ReceiptState.TERMINAL_FAILED)
        self.assertEqual(first.reason, "provider_retryable")
        self.assertEqual(second.reason, "provider_attempts_exhausted")
        self.assertEqual(third, second)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(
            [item.state for item in store.histories[req.effect_id]],
            [
                ReceiptState.PREPARED,
                ReceiptState.CLAIMED,
                ReceiptState.PREPARED,
                ReceiptState.CLAIMED,
                ReceiptState.TERMINAL_FAILED,
            ],
        )

    def test_terminal_provider_failure_is_immediately_durable_and_visible(self):
        req = request()
        store = InMemoryReceiptStore()
        provider = InMemoryProvider([TerminalProviderError("policy rejected")])

        receipt = EffectGateway(
            store,
            {"graph": provider},
            live_config(),
        ).execute(req)

        self.assertEqual(receipt.state, ReceiptState.TERMINAL_FAILED)
        self.assertEqual(receipt.reason, "provider_terminal")
        self.assertEqual(store.receipts[req.effect_id], receipt)

    def test_missing_provider_adapter_is_a_terminal_receipt_not_an_exception(self):
        req = request(provider="unconfigured-provider")
        store = InMemoryReceiptStore()

        receipt = EffectGateway(store, {}, live_config()).execute(req)

        self.assertEqual(receipt.state, ReceiptState.TERMINAL_FAILED)
        self.assertEqual(receipt.reason, "provider_adapter_missing")

    def test_content_change_under_one_effect_identity_fails_visible(self):
        store = InMemoryReceiptStore()
        provider = InMemoryProvider([ProviderEffectResult("graph-message-1")])
        gateway = EffectGateway(store, {"graph": provider}, live_config())
        original = request(content={"body": "first"})
        changed = request(run_id="run-2", content={"body": "changed"})

        self.assertEqual(gateway.execute(original).state, ReceiptState.SUCCEEDED)
        conflict = gateway.execute(changed)

        self.assertEqual(conflict.state, ReceiptState.TERMINAL_FAILED)
        self.assertEqual(conflict.reason, "content_identity_conflict")
        self.assertEqual(len(provider.calls), 1)

    def test_provider_acceptance_is_durable_before_success(self):
        req = request()
        store = InMemoryReceiptStore()
        provider = InMemoryProvider([ProviderEffectResult("graph-message-1")])

        receipt = EffectGateway(
            store,
            {"graph": provider},
            live_config(),
        ).execute(req)

        self.assertEqual(receipt.state, ReceiptState.SUCCEEDED)
        self.assertEqual(
            [item.state for item in store.histories[req.effect_id]],
            [
                ReceiptState.PREPARED,
                ReceiptState.CLAIMED,
                ReceiptState.PROVIDER_ACCEPTED,
                ReceiptState.SUCCEEDED,
            ],
        )

    def test_accepted_effect_that_cannot_finalize_requires_reconciliation(self):
        req = request()
        store = InMemoryReceiptStore()
        store.fail_transition_once.add(ReceiptState.SUCCEEDED)
        provider = InMemoryProvider([ProviderEffectResult("graph-message-1")])

        receipt = EffectGateway(
            store,
            {"graph": provider},
            live_config(),
        ).execute(req)

        self.assertEqual(receipt.state, ReceiptState.RECONCILIATION_REQUIRED)
        self.assertEqual(
            receipt.provider_reference,
            receipt_reference("graph-message-1"),
        )
        self.assertEqual(receipt.reason, "receipt_finalize_failed")
        self.assertEqual(len(provider.calls), 1)

    def test_uncertain_provider_outcome_never_automatically_retries(self):
        secret_details = (
            "timeout after accept token=secret-token "
            "recipient=broker@example.test body=private-message"
        )
        req = request()
        store = InMemoryReceiptStore()
        provider = InMemoryProvider(
            [
                UncertainProviderOutcomeError(secret_details),
                ProviderEffectResult("must-not-run"),
            ]
        )
        gateway = EffectGateway(store, {"graph": provider}, live_config())

        first = gateway.execute(req)
        retry = gateway.execute(req)
        serialized = json.dumps(first.to_dict(), sort_keys=True)

        self.assertEqual(first.state, ReceiptState.RECONCILIATION_REQUIRED)
        self.assertEqual(first.reason, "provider_outcome_unknown")
        self.assertEqual(retry, first)
        self.assertEqual(len(provider.calls), 1)
        for sensitive in (
            "secret-token",
            "broker@example.test",
            "private-message",
        ):
            self.assertNotIn(sensitive, repr(first))
            self.assertNotIn(sensitive, serialized)

    def test_unknown_provider_exception_is_conservatively_uncertain(self):
        req = request()
        store = InMemoryReceiptStore()
        provider = InMemoryProvider(
            [
                RuntimeError("SDK exploded after unknown acceptance"),
                ProviderEffectResult("must-not-run"),
            ]
        )
        gateway = EffectGateway(store, {"graph": provider}, live_config())

        first = gateway.execute(req)
        retry = gateway.execute(req)

        self.assertEqual(first.state, ReceiptState.RECONCILIATION_REQUIRED)
        self.assertEqual(first.reason, "provider_outcome_unknown")
        self.assertEqual(retry, first)
        self.assertEqual(len(provider.calls), 1)

    def test_terminal_provider_exception_details_never_enter_receipt(self):
        sensitive = (
            "policy denied token=secret-token "
            "recipient=broker@example.test body=private-message"
        )
        req = request()
        store = InMemoryReceiptStore()
        provider = InMemoryProvider([TerminalProviderError(sensitive)])

        receipt = EffectGateway(
            store,
            {"graph": provider},
            live_config(),
        ).execute(req)
        serialized = json.dumps(receipt.to_dict(), sort_keys=True)

        self.assertEqual(receipt.reason, "provider_terminal")
        for fragment in (
            "secret-token",
            "broker@example.test",
            "private-message",
        ):
            self.assertNotIn(fragment, repr(receipt))
            self.assertNotIn(fragment, serialized)


class ProviderReferenceSafetyTests(unittest.TestCase):
    def test_graph_style_reference_is_deterministically_tokenized(self):
        raw_reference = "AAMkAGI2AAABEgAQABCD_123+/=="
        req = request()
        store = InMemoryReceiptStore()
        receipt = EffectGateway(
            store,
            {"graph": InMemoryProvider([ProviderEffectResult(raw_reference)])},
            live_config(),
        ).execute(req)
        expected = receipt_reference(raw_reference)

        self.assertEqual(receipt.state, ReceiptState.SUCCEEDED)
        self.assertEqual(receipt.provider_reference, expected)
        self.assertEqual(
            receipt.to_dict()["providerReference"],
            expected,
        )
        self.assertNotIn(raw_reference, repr(receipt))
        self.assertNotIn(raw_reference, json.dumps(receipt.to_dict()))

    def test_secret_bearing_provider_reference_serializes_only_a_hash(self):
        raw_reference = (
            "token=secret-token recipient=broker@example.test "
            "body=private-message customer_id=client-123"
        )

        class SecretBearingReferenceProvider:
            def __init__(self):
                self.calls = []

            def execute(self, current_request):
                self.calls.append(current_request)
                return ProviderEffectResult(raw_reference)

        req = request()
        store = InMemoryReceiptStore()
        provider = SecretBearingReferenceProvider()
        receipt = EffectGateway(
            store,
            {"graph": provider},
            live_config(),
        ).execute(req)
        serialized = json.dumps(receipt.to_dict(), sort_keys=True)

        self.assertEqual(receipt.state, ReceiptState.SUCCEEDED)
        self.assertEqual(
            receipt.provider_reference,
            receipt_reference(raw_reference),
        )
        self.assertEqual(len(provider.calls), 1)
        for fragment in (
            "secret-token",
            "broker@example.test",
            "private-message",
            "client-123",
        ):
            self.assertNotIn(fragment, repr(receipt))
            self.assertNotIn(fragment, serialized)

    def test_receipts_reject_every_raw_provider_reference(self):
        req = request()
        for raw_reference in (
            "AAMkAGI2AAABEgAQABCD_123+/==",
            "token=secret-token",
            "recipient=broker@example.test",
            "A" * 513,
        ):
            with self.subTest(reference=raw_reference[:24]):
                with self.assertRaisesRegex(
                    ValueError,
                    "SHA-256 token",
                ):
                    EffectReceipt(
                        effect_id=req.effect_id,
                        content_idempotency_key=req.content_idempotency_key,
                        state=ReceiptState.PROVIDER_ACCEPTED,
                        attempts=1,
                        provider_reference=raw_reference,
                    )


class EffectWorkerWiringTests(unittest.TestCase):
    def test_main_is_the_only_tracked_provider_effect_worker_entry(self):
        main_tree = ast.parse((REPO_ROOT / "main.py").read_text(encoding="utf-8"))
        service_tree = ast.parse(
            (REPO_ROOT / "service.py").read_text(encoding="utf-8")
        )

        main_workers = [
            node
            for node in ast.walk(main_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "run_provider_effect_worker"
        ]
        service_workers = [
            node
            for node in ast.walk(service_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "run_provider_effect_worker"
        ]

        self.assertEqual(len(main_workers), 1)
        self.assertEqual(service_workers, [])
        main_source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("EffectGatewayConfig.from_env()", main_source)
        self.assertIn("EffectGateway(", main_source)

    def test_main_worker_uses_fail_closed_environment_defaults(self):
        with mock.patch.dict(
            os.environ,
            {"E2E_TEST_MODE": "true"},
            clear=True,
        ), mock.patch(
            "google.cloud.firestore.Client",
            return_value=mock.MagicMock(),
        ):
            main_module = importlib.import_module("main")

        store = InMemoryReceiptStore()
        provider = InMemoryProvider()
        with mock.patch.dict(os.environ, {}, clear=True):
            receipts = main_module.run_provider_effect_worker(
                [request()],
                receipt_store=store,
                provider_adapters={"graph": provider},
            )

        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].state, ReceiptState.BLOCKED)
        self.assertEqual(receipts[0].reason, "gateway_disabled")
        self.assertEqual(provider.calls, [])


class LiveRunnerContainmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = REPO_ROOT / "tests" / "multi_turn_live_test.py"
        module_name = "_sitesift_multi_turn_live_test_for_gateway_tests"
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        tests_path = str(path.parent)
        sys.path.insert(0, tests_path)
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(tests_path)
        cls.live = module

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop(cls.live.__name__, None)

    def test_legacy_live_authorized_constructor_parameter_is_rejected(self):
        with self.assertRaises(TypeError):
            self.live.MultiTurnTestRunner(
                wait_seconds=0,
                live_authorized=True,
            )

    def test_forged_attributes_and_environment_never_start_a_subprocess(self):
        exact = "I_UNDERSTAND_LIVE_PROVIDER_EFFECTS"
        for environment_value in (exact, f" {exact} "):
            for forged_value in (True, object()):
                with self.subTest(
                    environment_value=environment_value,
                    forged_type=type(forged_value).__name__,
                ):
                    runner = self.live.MultiTurnTestRunner(wait_seconds=0)
                    runner.live_authorized = forged_value
                    with mock.patch.dict(
                        os.environ,
                        {
                            "SITESIFT_LIVE_TEST_AUTHORIZATION":
                                environment_value
                        },
                        clear=True,
                    ), mock.patch("subprocess.run") as run:
                        with self.assertRaises(
                            self.live.LiveAuthorizationError
                        ):
                            runner._run_pipeline()

                    run.assert_not_called()

    def test_legacy_boolean_cleanup_authority_is_rejected_before_data_clients(self):
        fake_fs = mock.MagicMock()
        fake_clients = SimpleNamespace(_fs=fake_fs)
        with mock.patch.dict(
            os.environ,
            {
                "SITESIFT_LIVE_TEST_AUTHORIZATION":
                    "I_UNDERSTAND_LIVE_PROVIDER_EFFECTS"
            },
            clear=True,
        ), mock.patch.dict(
            sys.modules,
            {"email_automation.clients": fake_clients},
        ), mock.patch.object(self.live.RunState, "clear"):
            with self.assertRaises(TypeError):
                self.live.cleanup_test_data(live_authorized=True)

        fake_fs.collection.assert_not_called()

    def test_cleanup_always_fails_closed_for_exact_and_padded_environment(self):
        exact = "I_UNDERSTAND_LIVE_PROVIDER_EFFECTS"
        for environment_value in (exact, f" {exact} "):
            with self.subTest(environment_value=environment_value):
                with mock.patch.dict(
                    os.environ,
                    {
                        "SITESIFT_LIVE_TEST_AUTHORIZATION":
                            environment_value
                    },
                    clear=True,
                ):
                    with self.assertRaises(
                        self.live.LiveAuthorizationError
                    ):
                        self.live.cleanup_test_data()

    def test_legacy_effect_execution_code_is_absent(self):
        source = (
            REPO_ROOT / "tests" / "multi_turn_live_test.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("run_production.sh", source)
        self.assertNotIn('["bash"', source)
        self.assertNotIn("subprocess.run(", source)
        self.assertNotIn('[sys.executable, "-m", "main"]', source)
        self.assertNotIn("SITESIFT_LIVE_TEST_AUTHORIZATION", source)
        self.assertNotIn("--authorize-live-effects", source)
        self.assertNotIn("load_dotenv", source)

    def test_list_remains_read_only_and_available(self):
        stdout = io.StringIO()
        with mock.patch.object(
            sys,
            "argv",
            ["multi_turn_live_test.py", "--list"],
        ), mock.patch.object(sys, "stdout", stdout), mock.patch.object(
            self.live,
            "MultiTurnTestRunner",
        ) as runner:
            self.live.main()

        runner.assert_not_called()
        self.assertIn("Available scenarios:", stdout.getvalue())

    def test_legacy_cli_authorization_flag_is_rejected_without_execution(self):
        exact = "I_UNDERSTAND_LIVE_PROVIDER_EFFECTS"
        for environment_value in (exact, f" {exact} "):
            with self.subTest(environment_value=environment_value):
                fake_runner = mock.MagicMock()
                fake_runner.return_value.run.return_value = {
                    "summary": {
                        "scenarios_passed": 1,
                        "scenarios_total": 1,
                        "turns_passed": 1,
                        "turns_total": 1,
                        "total_duration_seconds": 0,
                        "overall_pass": True,
                    }
                }
                with mock.patch.dict(
                    os.environ,
                    {
                        "SITESIFT_LIVE_TEST_AUTHORIZATION":
                            environment_value
                    },
                    clear=True,
                ), mock.patch.object(
                    sys,
                    "argv",
                    [
                        "multi_turn_live_test.py",
                        "--scenario",
                        "gradual_info_gathering",
                        "--authorize-live-effects",
                    ],
                ), mock.patch.object(
                    self.live,
                    "MultiTurnTestRunner",
                    fake_runner,
                ), mock.patch.object(
                    self.live,
                    "load_dotenv",
                    create=True,
                ), mock.patch.object(
                    sys,
                    "stdout",
                    io.StringIO(),
                ), mock.patch.object(
                    sys,
                    "stderr",
                    io.StringIO(),
                ):
                    with self.assertRaises(SystemExit) as exit_context:
                        self.live.main()

                self.assertEqual(exit_context.exception.code, 2)
                fake_runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
