import json
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from email_automation.claim_pipeline.claim_fixtures import (
    CLAIM_FIXTURE_SCHEMA_VERSION,
    ClaimFixtureValidationError,
    load_claim_fixture_catalog,
    validate_claim_fixture_coverage,
)
from email_automation.claim_pipeline.contracts import (
    ActorRole,
    Claim,
    ClaimModality,
    ClaimPolarity,
    ClaimPredicate,
)
from email_automation.claim_pipeline.entities import resolve_entities
from email_automation.claim_pipeline.evidence import normalize_message_evidence
from email_automation.claim_pipeline.extraction import extract_claims
from email_automation.claim_pipeline.interpretation_fixtures import (
    load_interpretation_fixture_catalog,
)


CLAIM_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "claim_pipeline_claim_cases.json"
)
INTERPRETATION_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "claim_pipeline_interpretation_cases.json"
)


def _subject_key(entity):
    return (entity.relationship, entity.suite, entity.canonical_address)


class ClaimFixtureTests(unittest.TestCase):
    def test_catalog_is_versioned_broad_immutable_and_reproducible(self):
        first = load_claim_fixture_catalog(CLAIM_FIXTURE_PATH)
        second = load_claim_fixture_catalog(CLAIM_FIXTURE_PATH)

        self.assertEqual(CLAIM_FIXTURE_SCHEMA_VERSION, first.schema_version)
        self.assertGreaterEqual(len(first.cases), 18)
        self.assertEqual(len(first.cases), len({case.case_id for case in first.cases}))
        self.assertEqual(first.manifest_hash, second.manifest_hash)
        self.assertIsInstance(first.cases[0].expected, MappingProxyType)

    def test_catalog_mechanically_requires_every_predicate_and_incident_dimension(self):
        catalog = load_claim_fixture_catalog(CLAIM_FIXTURE_PATH)

        self.assertEqual(3, CLAIM_FIXTURE_SCHEMA_VERSION)
        self.assertEqual(
            {item.value for item in ClaimPredicate},
            set(catalog.required_predicate_outcomes),
        )
        self.assertTrue(
            all(
                {"accepted", "rejected"}.issubset(outcomes)
                for outcomes in catalog.required_predicate_outcomes.values()
            )
        )
        self.assertEqual(
            {
                "alternate_property",
                "attachment",
                "call_request",
                "continued_followup_hazard",
                "correction",
                "link",
                "multi_turn",
                "opt_out",
                "redirect",
                "repeated_question",
                "requirements_mismatch",
                "split_suite",
                "terminal_closeout",
                "tour_request",
            },
            set(catalog.required_incident_dimensions),
        )
        validate_claim_fixture_coverage(catalog)

    def test_coverage_declarations_must_be_proven_by_case_outcomes(self):
        raw = json.loads(CLAIM_FIXTURE_PATH.read_text(encoding="utf-8"))
        raw["cases"][0]["coverage"]["predicateOutcomes"] = [
            {"predicate": "rent", "outcome": "accepted"}
        ]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dishonest-coverage.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(
                ClaimFixtureValidationError,
                "does not prove accepted rent",
            ):
                load_claim_fixture_catalog(path)

    def test_report_visible_fixture_identifiers_reject_private_text(self):
        raw = json.loads(CLAIM_FIXTURE_PATH.read_text(encoding="utf-8"))
        raw["cases"][0]["caseId"] = "private-broker@example.com"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-identifier.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(
                ClaimFixtureValidationError,
                "caseId must be a report-safe identifier",
            ):
                load_claim_fixture_catalog(path)

    def test_missing_required_coverage_fails_closed(self):
        raw = json.loads(CLAIM_FIXTURE_PATH.read_text(encoding="utf-8"))
        raw["cases"] = [
            case
            for case in raw["cases"]
            if not any(
                item == {"predicate": "power", "outcome": "accepted"}
                for item in case["coverage"]["predicateOutcomes"]
            )
        ]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing-coverage.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(
                ClaimFixtureValidationError,
                "missing required predicate coverage:.*power:accepted",
            ):
                load_claim_fixture_catalog(path)

    def test_coverage_contract_cannot_shrink_supported_predicates_or_incidents(self):
        raw = json.loads(CLAIM_FIXTURE_PATH.read_text(encoding="utf-8"))
        mutations = (
            (
                lambda value: value["coverageContract"][
                    "requiredPredicateOutcomes"
                ].pop("power"),
                "must name every supported predicate",
            ),
            (
                lambda value: value["coverageContract"][
                    "requiredIncidentDimensions"
                ].remove("attachment"),
                "must name every required incident dimension",
            ),
        )

        for mutate, message in mutations:
            with self.subTest(message=message):
                candidate = json.loads(json.dumps(raw))
                mutate(candidate)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "shrunk-coverage.json"
                    path.write_text(json.dumps(candidate), encoding="utf-8")
                    with self.assertRaisesRegex(
                        ClaimFixtureValidationError,
                        message,
                    ):
                        load_claim_fixture_catalog(path)

    def test_multi_turn_cases_require_prior_claims_and_symbolic_supersession(self):
        catalog = load_claim_fixture_catalog(CLAIM_FIXTURE_PATH)
        multi_turn = [
            case
            for case in catalog.cases
            if "multi_turn" in case.incident_dimensions
        ]

        self.assertTrue(multi_turn)
        self.assertTrue(all(case.prior_claims for case in multi_turn))
        correction_cases = [
            case
            for case in multi_turn
            if "correction" in case.incident_dimensions
        ]
        self.assertTrue(correction_cases)
        self.assertTrue(
            any(
                claim["supersedesClaimId"] == "prior:0"
                for case in correction_cases
                for claim in case.claims
            )
        )

    def test_every_case_executes_normalization_resolution_and_claim_extraction(self):
        claim_catalog = load_claim_fixture_catalog(CLAIM_FIXTURE_PATH)
        interpretation_catalog = load_interpretation_fixture_catalog(
            INTERPRETATION_FIXTURE_PATH
        )
        interpretation_by_id = {
            case.case_id: case for case in interpretation_catalog.cases
        }

        for case in claim_catalog.cases:
            with self.subTest(case=case.case_id):
                source = interpretation_by_id[case.interpretation_case_id]
                normalized = normalize_message_evidence(source.message)
                resolved = resolve_entities(
                    tenant_id=source.message.tenant_id,
                    campaign_id=source.campaign_id,
                    seeds=source.seeds,
                    evidence=normalized.evidence,
                )
                entity_by_key = {_subject_key(item): item for item in resolved.entities}
                prior_claims = []
                for raw in case.prior_claims:
                    envelope = normalized.evidence[raw["evidenceIndex"]]
                    subject = entity_by_key[
                        (
                            raw["subject"]["relationship"],
                            raw["subject"]["suite"],
                            raw["subject"]["canonicalAddress"],
                        )
                    ]
                    prior_claims.append(
                        Claim.create(
                            tenant_id=envelope.tenant_id,
                            campaign_id=envelope.campaign_id,
                            evidence_id=envelope.evidence_id,
                            subject_entity_id=subject.entity_id,
                            predicate=ClaimPredicate(raw["predicate"]),
                            value=raw["value"],
                            evidence_text=raw["evidenceText"],
                            actor_role=ActorRole(raw["actorRole"]),
                            polarity=ClaimPolarity(raw["polarity"]),
                            modality=ClaimModality(raw["modality"]),
                            confidence=raw["confidence"],
                            unit=raw["unit"],
                            effective_at=raw["effectiveAt"],
                            supersedes_claim_id=raw["supersedesClaimId"],
                            actor_email=raw["actorEmail"],
                            observed_at=raw["observedAt"],
                        )
                    )
                model_claims = []
                for raw in case.claims:
                    subject = entity_by_key[
                        (
                            raw["subject"]["relationship"],
                            raw["subject"]["suite"],
                            raw["subject"]["canonicalAddress"],
                        )
                    ]
                    claim = {
                            key: value
                            for key, value in raw.items()
                            if key not in {"evidenceIndex", "subject"}
                        }
                    supersedes = claim["supersedesClaimId"]
                    if isinstance(supersedes, str) and supersedes.startswith("prior:"):
                        claim["supersedesClaimId"] = prior_claims[
                            int(supersedes.removeprefix("prior:"))
                        ].claim_id
                    model_claims.append(
                        claim
                        | {
                            "evidenceId": normalized.evidence[
                                raw["evidenceIndex"]
                            ].evidence_id,
                            "subjectEntityId": subject.entity_id,
                        }
                    )
                model_review = [
                    {
                        "evidenceId": normalized.evidence[item["evidenceIndex"]].evidence_id,
                        "reason": item["reason"],
                    }
                    for item in case.review
                ]
                result = extract_claims(
                    tenant_id=source.message.tenant_id,
                    campaign_id=source.campaign_id,
                    evidence=normalized.evidence,
                    entities=resolved.entities,
                    prior_claims=prior_claims,
                    resolution_issues=resolved.issues,
                    model_output={"claims": model_claims, "review": model_review},
                )
                entities_by_id = {item.entity_id: item for item in resolved.entities}
                actual_claims = sorted(
                    (
                        item.predicate.value,
                        item.value,
                        entities_by_id[item.subject_entity_id].relationship,
                        entities_by_id[item.subject_entity_id].suite,
                    )
                    for item in result.claims
                )
                expected_claims = sorted(
                    (
                        case.claims[index]["predicate"],
                        case.claims[index]["value"],
                        case.claims[index]["subject"]["relationship"],
                        case.claims[index]["subject"]["suite"],
                    )
                    for index in case.expected["acceptedClaimIndexes"]
                )
                self.assertEqual(expected_claims, actual_claims)
                self.assertEqual(
                    sorted(item["code"] for item in case.expected["issues"]),
                    sorted(item.code for item in result.issues),
                )

    def test_every_interpretation_case_has_claim_quality_coverage(self):
        claim_catalog = load_claim_fixture_catalog(CLAIM_FIXTURE_PATH)
        interpretation_catalog = load_interpretation_fixture_catalog(
            INTERPRETATION_FIXTURE_PATH
        )

        self.assertEqual(
            {case.case_id for case in interpretation_catalog.cases},
            {case.interpretation_case_id for case in claim_catalog.cases},
        )

    def test_unknown_keys_and_duplicate_case_ids_are_rejected(self):
        raw = json.loads(CLAIM_FIXTURE_PATH.read_text(encoding="utf-8"))
        mutations = (
            lambda value: value.__setitem__("unknown", True),
            lambda value: value["cases"][0].__setitem__("unknown", True),
            lambda value: value["cases"][0]["claims"][0].__setitem__("unknown", True),
            lambda value: value["cases"].append(dict(value["cases"][0])),
        )

        for mutate in mutations:
            with self.subTest(mutation=mutate):
                candidate = json.loads(json.dumps(raw))
                mutate(candidate)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "invalid.json"
                    path.write_text(json.dumps(candidate), encoding="utf-8")
                    with self.assertRaises(ClaimFixtureValidationError):
                        load_claim_fixture_catalog(path)


if __name__ == "__main__":
    unittest.main()
