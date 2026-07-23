import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from email_automation.claim_pipeline.provider_policy_shadow import (
    BudgetedProviderTransport,
    ProviderBudgetExceeded,
    ProviderBudgetLimits,
    select_provider_policy_cases,
)
from email_automation.claim_pipeline.provider_replay import ProviderTransportResult
from email_automation.claim_pipeline.replay import (
    ProposalUsage,
    ProviderTelemetrySnapshot,
)


REPO_ROOT = Path(__file__).parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_claim_pipeline_provider_policy_shadow.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("provider_policy_shadow_script", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeTransport:
    provider_id = "openai"
    model_id = "gpt-test"

    def __init__(self):
        self.calls = 0

    def snapshot(self):
        return ProviderTelemetrySnapshot(attempts=self.calls)

    def invoke(self, *, case_id, instructions, payload):
        self.calls += 1
        return ProviderTransportResult(
            model_output={"claims": [], "review": []},
            usage=ProposalUsage(
                input_tokens=1,
                output_tokens=1,
                latency_ms=1,
                provider_calls=1,
                provider_billed=False,
                usage_complete=True,
            ),
        )


class ProviderBudgetTests(unittest.TestCase):
    def test_budget_reserves_conservative_tokens_and_cost_before_call(self):
        delegate = _FakeTransport()
        transport = BudgetedProviderTransport(
            delegate,
            limits=ProviderBudgetLimits(
                max_calls=1,
                max_reserved_tokens=100,
                max_reserved_cost_microusd=100,
            ),
            max_output_tokens=10,
            input_token_overhead=0,
            input_cost_microusd_per_million=1_000_000,
            output_cost_microusd_per_million=2_000_000,
        )

        transport.invoke(case_id="safe-case", instructions="abc", payload="de")

        self.assertEqual(1, delegate.calls)
        self.assertEqual(15, transport.reservation_snapshot().reserved_tokens)
        self.assertEqual(25, transport.reservation_snapshot().reserved_cost_microusd)

    def test_budget_refuses_next_call_before_delegate_invocation(self):
        delegate = _FakeTransport()
        transport = BudgetedProviderTransport(
            delegate,
            limits=ProviderBudgetLimits(
                max_calls=1,
                max_reserved_tokens=100,
                max_reserved_cost_microusd=100,
            ),
            max_output_tokens=10,
            input_token_overhead=0,
            input_cost_microusd_per_million=1_000_000,
            output_cost_microusd_per_million=2_000_000,
        )
        transport.invoke(case_id="safe-case", instructions="a", payload="b")

        with self.assertRaises(ProviderBudgetExceeded):
            transport.invoke(case_id="safe-case", instructions="a", payload="b")

        self.assertEqual(1, delegate.calls)
        self.assertEqual(1, transport.reservation_snapshot().reserved_calls)

    def test_token_or_cost_overflow_refuses_first_call(self):
        for limits in (
            ProviderBudgetLimits(
                max_calls=1,
                max_reserved_tokens=5,
                max_reserved_cost_microusd=100,
            ),
            ProviderBudgetLimits(
                max_calls=1,
                max_reserved_tokens=100,
                max_reserved_cost_microusd=5,
            ),
        ):
            with self.subTest(limits=limits):
                delegate = _FakeTransport()
                transport = BudgetedProviderTransport(
                    delegate,
                    limits=limits,
                    max_output_tokens=10,
                    input_token_overhead=0,
                    input_cost_microusd_per_million=1_000_000,
                    output_cost_microusd_per_million=2_000_000,
                )
                with self.assertRaises(ProviderBudgetExceeded):
                    transport.invoke(
                        case_id="safe-case",
                        instructions="abc",
                        payload="de",
                    )
                self.assertEqual(0, delegate.calls)


class ProviderPolicyShadowScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = _load_script()

    def _main_json(self, *arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(
            self.script, "_source_tree_dirty", return_value=False
        ):
            status = self.script.main(arguments)
        return status, json.loads(output.getvalue())

    def test_recorded_smoke_is_exactly_one_case_and_zero_usage(self):
        status, report = self._main_json("--provider", "recorded", "--mode", "smoke")

        self.assertEqual(0, status)
        self.assertEqual(1, report["identity"]["plannedCalls"])
        self.assertEqual(1, report["summary"]["resultCount"])
        self.assertEqual(0, report["summary"]["providerCalls"])
        self.assertEqual("unavailable-optout-suppression", report["results"][0]["caseId"])

    def test_recorded_final_is_fixed_at_three_by_eight(self):
        status, report = self._main_json("--provider", "recorded", "--mode", "final")

        self.assertEqual(0, status)
        self.assertEqual(24, report["identity"]["plannedCalls"])
        self.assertEqual(24, report["summary"]["resultCount"])
        self.assertEqual(8, len({item["caseId"] for item in report["results"]}))

    def test_openai_requires_explicit_opt_in_before_key_or_transport(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.script.main(("--provider", "openai", "--mode", "smoke"))

    def test_report_contains_caps_and_no_fixture_values(self):
        _, report = self._main_json("--provider", "recorded", "--mode", "final")
        serialized = json.dumps(report, sort_keys=True)

        self.assertEqual(24, report["identity"]["maxProviderCalls"])
        self.assertEqual(1_500_000, report["identity"]["maxReservedTokens"])
        self.assertEqual(
            5_000_000,
            report["identity"]["maxReservedCostMicrousd"],
        )
        for forbidden in (
            "123 Industrial",
            "999 Other",
            "alex@example",
            "jordan@example",
            "evidenceText",
            "rawOutput",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_case_selection_is_hashed_and_rejects_unknown_ids(self):
        catalog = self.script._load_catalogs()[-1]
        selected = select_provider_policy_cases(
            catalog,
            case_ids=("unavailable-optout-suppression",),
        )

        self.assertEqual(1, len(selected.cases))
        self.assertNotEqual(catalog.manifest_hash, selected.manifest_hash)
        with self.assertRaises(ValueError):
            select_provider_policy_cases(catalog, case_ids=("unknown",))


if __name__ == "__main__":
    unittest.main()
