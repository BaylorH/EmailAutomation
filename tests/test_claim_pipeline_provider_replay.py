import json
import unittest

from email_automation.claim_pipeline.claim_fixtures import load_claim_fixture_catalog
from email_automation.claim_pipeline.entities import resolve_entities
from email_automation.claim_pipeline.evidence import normalize_message_evidence
from email_automation.claim_pipeline.extraction import build_claim_extraction_request
from email_automation.claim_pipeline.interpretation_fixtures import (
    load_interpretation_fixture_catalog,
)
from email_automation.claim_pipeline.provider_replay import (
    PINNED_MODEL_ID,
    PINNED_PROMPT,
    PINNED_PROMPT_HASH,
    PINNED_PROMPT_ID,
    PINNED_PROVIDER_ID,
    PinnedProviderProposalAdapter,
    ProviderTransportResult,
)
from email_automation.claim_pipeline.replay import ProposalUsage


FIXTURE_ROOT = __import__("pathlib").Path(__file__).parent / "fixtures"


class _FakeTransport:
    provider_id = PINNED_PROVIDER_ID
    model_id = PINNED_MODEL_ID

    def __init__(self, output):
        self.output = output
        self.calls = []

    def invoke(self, *, case_id, instructions, payload):
        self.calls.append((case_id, instructions, payload))
        return ProviderTransportResult(
            model_output=self.output,
            usage=ProposalUsage(
                input_tokens=10,
                output_tokens=5,
                latency_ms=9,
                cost_microusd=4,
                provider_calls=1,
                provider_billed=True,
                usage_complete=True,
            ),
        )


class PinnedProviderProposalAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.interpretation = load_interpretation_fixture_catalog(
            FIXTURE_ROOT / "claim_pipeline_interpretation_cases.json"
        )
        cls.claims = load_claim_fixture_catalog(
            FIXTURE_ROOT / "claim_pipeline_claim_cases.json"
        )

    def _request(self):
        case = self.claims.cases[0]
        source = next(
            item
            for item in self.interpretation.cases
            if item.case_id == case.interpretation_case_id
        )
        normalized = normalize_message_evidence(source.message)
        resolved = resolve_entities(
            tenant_id=source.message.tenant_id,
            campaign_id=source.campaign_id,
            seeds=source.seeds,
            evidence=normalized.evidence,
        )
        request = build_claim_extraction_request(
            tenant_id=source.message.tenant_id,
            campaign_id=source.campaign_id,
            evidence=normalized.evidence,
            entities=resolved.entities,
            resolution_issues=resolved.issues,
        )
        return case, request, normalized.evidence, resolved.entities

    def test_adapter_pins_identity_and_serializes_only_the_request(self):
        transport = _FakeTransport('{"claims":[],"review":[]}')
        adapter = PinnedProviderProposalAdapter(transport)
        case, request, evidence, entities = self._request()

        response = adapter.propose(
            case_id=case.case_id,
            request=request,
            evidence=evidence,
            entities=entities,
        )

        self.assertEqual(PINNED_PROVIDER_ID, adapter.provider_id)
        self.assertEqual(PINNED_MODEL_ID, adapter.model_id)
        self.assertEqual(PINNED_PROMPT_ID, adapter.prompt_id)
        self.assertEqual(PINNED_PROMPT_HASH, adapter.prompt_hash)
        self.assertEqual(1, len(transport.calls))
        _, instructions, payload = transport.calls[0]
        self.assertNotIn("expected", instructions.casefold())
        self.assertEqual(request.to_dict(), json.loads(payload))
        self.assertEqual('{"claims":[],"review":[]}', response.model_output)

    def test_prompt_constrains_review_reasons_to_safe_category_tokens(self):
        self.assertIn("entity_ambiguity", PINNED_PROMPT)
        self.assertIn("insufficient_evidence", PINNED_PROMPT)
        self.assertIn("review.reason", PINNED_PROMPT)
        self.assertNotIn("state a concise reason", PINNED_PROMPT)

    def test_adapter_rejects_context_that_does_not_match_request(self):
        transport = _FakeTransport('{"claims":[],"review":[]}')
        adapter = PinnedProviderProposalAdapter(transport)
        case, request, evidence, entities = self._request()

        with self.assertRaises(ValueError):
            adapter.propose(
                case_id=case.case_id,
                request=request,
                evidence=(),
                entities=entities,
            )
        self.assertEqual([], transport.calls)


if __name__ == "__main__":
    unittest.main()
