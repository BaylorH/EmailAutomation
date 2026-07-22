import json
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from email_automation.claim_pipeline.claim_fixtures import (
    CLAIM_FIXTURE_SCHEMA_VERSION,
    ClaimFixtureValidationError,
    load_claim_fixture_catalog,
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
                model_claims = []
                for raw in case.claims:
                    subject = entity_by_key[
                        (
                            raw["subject"]["relationship"],
                            raw["subject"]["suite"],
                            raw["subject"]["canonicalAddress"],
                        )
                    ]
                    model_claims.append(
                        {
                            key: value
                            for key, value in raw.items()
                            if key not in {"evidenceIndex", "subject"}
                        }
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
                        item["predicate"],
                        item["value"],
                        item["relationship"],
                        item["suite"],
                    )
                    for item in case.expected["accepted"]
                )
                self.assertEqual(expected_claims, actual_claims)
                self.assertEqual(
                    sorted(case.expected["issueCodes"]),
                    sorted(item.code for item in result.issues),
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
