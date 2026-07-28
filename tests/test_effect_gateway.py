"""Credential-free contracts for the provider-effect gateway."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
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
        self.receipts[receipt.effect_id] = receipt
        self.histories[receipt.effect_id].append(receipt)
        return receipt

    def load_receipt(self, request):
        self.events.append(("receipt_read", request.effect_id))
        return self.receipts.get(request.effect_id)

    def record_blocked(self, request, reason):
        current = self.receipts.get(request.effect_id)
        return self._save(
            EffectReceipt(
                effect_id=request.effect_id,
                content_idempotency_key=request.content_idempotency_key,
                state=ReceiptState.BLOCKED,
                attempts=current.attempts if current else 0,
                reason=reason,
            )
        )

    def prepare(self, request):
        current = self.receipts.get(request.effect_id)
        return self._save(
            EffectReceipt(
                effect_id=request.effect_id,
                content_idempotency_key=request.content_idempotency_key,
                state=ReceiptState.PREPARED,
                attempts=current.attempts if current else 0,
            )
        )

    def reserve_attempt(self, request, limits):
        """Atomically enforces receipt state, identity, bounds, and counters."""
        self.events.append(("reserve", request.effect_id))
        current = self.receipts.get(request.effect_id)
        if current:
            if current.content_idempotency_key != request.content_idempotency_key:
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
                    receipt=self.record_blocked(request, reason),
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
        current = self.receipts[request.effect_id]
        self.events.append(("transition", state.value))
        if state in self.fail_transition_once:
            self.fail_transition_once.remove(state)
            raise RuntimeError(f"simulated {state.value} persistence failure")
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
        self.assertEqual(first.provider_reference, "graph-message-1")
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
        self.assertEqual(receipt.provider_reference, "graph-message-1")
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
            # The legacy module loaded dotenv at import time. Suppress local
            # credential discovery while the RED test drives its retirement.
            with mock.patch.object(Path, "exists", return_value=False):
                spec.loader.exec_module(module)
        finally:
            sys.path.remove(tests_path)
        cls.live = module

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop(cls.live.__name__, None)

    def test_live_pipeline_fails_closed_before_starting_a_subprocess(self):
        runner = self.live.MultiTurnTestRunner(wait_seconds=0)
        authorization = {
            "SITESIFT_LIVE_TEST_AUTHORIZATION":
                "I_UNDERSTAND_LIVE_PROVIDER_EFFECTS"
        }
        with mock.patch.dict(os.environ, authorization, clear=True), mock.patch.object(
            self.live.subprocess,
            "run",
        ) as run:
            with self.assertRaises(self.live.LiveAuthorizationError):
                runner._run_pipeline()

        run.assert_not_called()

    def test_authorized_pipeline_invokes_only_the_tracked_python_module(self):
        runner = self.live.MultiTurnTestRunner(
            wait_seconds=0,
            live_authorized=True,
        )
        completed = SimpleNamespace(
            returncode=0,
            stdout="pipeline complete",
            stderr="",
        )
        authorization = {
            "SITESIFT_LIVE_TEST_AUTHORIZATION":
                "I_UNDERSTAND_LIVE_PROVIDER_EFFECTS"
        }
        with mock.patch.dict(os.environ, authorization, clear=True), mock.patch.object(
            self.live.subprocess,
            "run",
            return_value=completed,
        ) as run:
            runner._run_pipeline()

        self.assertEqual(
            run.call_args.args[0],
            [sys.executable, "-m", "main"],
        )
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_environment_only_cleanup_cannot_touch_data_clients(self):
        authorization = {
            "SITESIFT_LIVE_TEST_AUTHORIZATION":
                "I_UNDERSTAND_LIVE_PROVIDER_EFFECTS"
        }
        fake_fs = mock.MagicMock()
        fake_clients = SimpleNamespace(_fs=fake_fs)

        with mock.patch.dict(os.environ, authorization, clear=True), mock.patch.dict(
            sys.modules,
            {"email_automation.clients": fake_clients},
        ):
            with self.assertRaises(self.live.LiveAuthorizationError):
                self.live.cleanup_test_data()

        fake_fs.collection.assert_not_called()

    def test_live_runner_has_no_dependency_on_the_ignored_shell_launcher(self):
        source = (
            REPO_ROOT / "tests" / "multi_turn_live_test.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("run_production.sh", source)
        self.assertNotIn('["bash"', source)


if __name__ == "__main__":
    unittest.main()
