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
        return (case, *self._request_for_interpretation(source.case_id))

    def _request_for_interpretation(self, case_id):
        source = next(
            item for item in self.interpretation.cases if item.case_id == case_id
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
        return request, normalized.evidence, resolved.entities

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
        self.assertEqual({"claims": [], "review": []}, response.model_output)

    def test_prompt_constrains_review_reasons_to_safe_category_tokens(self):
        self.assertIn("entity_ambiguity", PINNED_PROMPT)
        self.assertIn("insufficient_evidence", PINNED_PROMPT)
        self.assertIn("review.reason", PINNED_PROMPT)
        self.assertNotIn("state a concise reason", PINNED_PROMPT)

    def test_prompt_defines_complete_source_and_workflow_claim_rules(self):
        self.assertEqual("sitesift-claim-proposal-2026-07-22-v7", PINNED_PROMPT_ID)
        required_rules = (
            "emit every distinct supported claim",
            "Do not emit any claim from quoted, forwarded, or historical evidence",
            "Identity claims are allowed only from fresh attachment or link evidence",
            "never for the seeded target",
            "For a suite identity, value must be the exact Suite",
            "Never emit a claim from signature evidence",
            "do not emit any claim from that ambiguous evidence",
            "do not emit a candidate; emit one insufficient_evidence review item",
            "opt_out, call_request, tour_request, and information_request use value true",
            "referral uses an object containing only explicit name, email, or phone values",
            "return_date uses an ISO date and the same effectiveAt date",
            "emit each one as a separate claim",
            "correction claim carries the exact phrase that negates or replaces the old value",
            "tour_request evidenceText must contain both the request cue and the tour term",
            "remediation value and evidenceText use the same exact repair-action clause",
            "supersedesClaimId must be the exact claimId from priorClaims",
            "same subjectEntityId as that prior claim",
            "For remediation and correction, set value exactly equal to evidenceText",
        )
        for rule in required_rules:
            with self.subTest(rule=rule):
                self.assertIn(rule, PINNED_PROMPT)

    def test_adapter_derives_text_backed_values_from_exact_evidence_spans(self):
        output = {
            "claims": [
                {
                    "predicate": "remediation",
                    "value": "model paraphrase",
                    "evidenceText": "The roof leak will be repaired",
                },
                {
                    "predicate": "correction",
                    "value": "different model wording",
                    "evidenceText": "not the earlier asking rent",
                },
                {
                    "predicate": "availability",
                    "value": "available",
                    "evidenceText": "is available",
                },
            ],
            "review": [],
        }
        transport = _FakeTransport(json.dumps(output))
        adapter = PinnedProviderProposalAdapter(transport)
        case, request, evidence, entities = self._request()

        response = adapter.propose(
            case_id=case.case_id,
            request=request,
            evidence=evidence,
            entities=entities,
        )

        self.assertEqual(
            [
                "The roof leak will be repaired",
                "not the earlier asking rent",
                "available",
            ],
            [item["value"] for item in response.model_output["claims"]],
        )

    def test_adapter_replaces_external_identity_with_resolved_claim(self):
        scenarios = (
            ("wrong-property-attachment", "alternate", "999 Other Road"),
            ("attachment-only-reply", "suite_of_target", "Suite C"),
        )
        for case_id, relationship, expected_value in scenarios:
            with self.subTest(case_id=case_id):
                request, evidence, entities = self._request_for_interpretation(case_id)
                entity = next(item for item in entities if item.relationship == relationship)
                output = {
                    "claims": [
                        {
                            "predicate": "identity",
                            "subjectEntityId": entity.entity_id,
                            "value": "model-selected identity",
                        }
                    ],
                    "review": [],
                }
                adapter = PinnedProviderProposalAdapter(_FakeTransport(json.dumps(output)))

                response = adapter.propose(
                    case_id=case_id,
                    request=request,
                    evidence=evidence,
                    entities=entities,
                )

                identities = [
                    item
                    for item in response.model_output["claims"]
                    if item["predicate"] == "identity"
                    and item["subjectEntityId"] == entity.entity_id
                ]
                self.assertEqual(1, len(identities))
                self.assertEqual(expected_value, identities[0]["value"])
                self.assertEqual(expected_value, identities[0]["evidenceText"])

    def test_adapter_keeps_only_deterministically_supported_reviews(self):
        scenarios = (
            ("attachment-extraction-failure-visible", "insufficient_evidence", False),
            ("ordinary-prose-does-not-fabricate-entities", "insufficient_evidence", True),
            ("ambiguous-other-building", "entity_ambiguity", True),
        )
        for case_id, reason, expected_kept in scenarios:
            with self.subTest(case_id=case_id):
                request, evidence, entities = self._request_for_interpretation(case_id)
                target_evidence = (
                    next(
                        item
                        for item in evidence
                        if any(
                            item.evidence_id in issue.evidence_ids
                            for issue in request.resolution_issues
                        )
                    )
                    if reason == "entity_ambiguity"
                    else evidence[-1]
                )
                output = {
                    "claims": [],
                    "review": [
                        {
                            "evidenceId": target_evidence.evidence_id,
                            "reason": reason,
                        }
                    ],
                }
                adapter = PinnedProviderProposalAdapter(_FakeTransport(json.dumps(output)))

                response = adapter.propose(
                    case_id=case_id,
                    request=request,
                    evidence=evidence,
                    entities=entities,
                )

                self.assertEqual(expected_kept, bool(response.model_output["review"]))

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
