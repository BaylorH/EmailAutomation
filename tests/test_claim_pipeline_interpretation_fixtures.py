import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import MappingProxyType

from email_automation.claim_pipeline.entities import resolve_entities
from email_automation.claim_pipeline.evidence import normalize_message_evidence
from email_automation.claim_pipeline.interpretation_fixtures import (
    INTERPRETATION_FIXTURE_SCHEMA_VERSION,
    InterpretationFixtureValidationError,
    load_interpretation_fixture_catalog,
)


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "claim_pipeline_interpretation_cases.json"
)


class InterpretationFixtureTests(unittest.TestCase):
    def test_catalog_is_versioned_broad_and_reproducible(self):
        first = load_interpretation_fixture_catalog(FIXTURE_PATH)
        second = load_interpretation_fixture_catalog(FIXTURE_PATH)

        self.assertEqual(INTERPRETATION_FIXTURE_SCHEMA_VERSION, first.schema_version)
        self.assertGreaterEqual(len(first.cases), 11)
        self.assertEqual(len(first.cases), len({case.case_id for case in first.cases}))
        self.assertEqual(first.manifest_hash, second.manifest_hash)
        self.assertIsInstance(first.cases[0].expected, MappingProxyType)
        with self.assertRaises(TypeError):
            first.cases[0].expected["issueCodes"] = ()

    def test_report_visible_fixture_identifiers_reject_private_text(self):
        raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        raw["cases"][0]["caseId"] = "private-broker@example.com"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-identifier.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(
                InterpretationFixtureValidationError,
                "caseId must be a report-safe identifier",
            ):
                load_interpretation_fixture_catalog(path)

    def test_every_case_executes_through_real_normalization_and_resolution(self):
        catalog = load_interpretation_fixture_catalog(FIXTURE_PATH)

        for case in catalog.cases:
            with self.subTest(case=case.case_id):
                normalized = normalize_message_evidence(case.message)
                resolved = resolve_entities(
                    tenant_id=case.message.tenant_id,
                    campaign_id=case.campaign_id,
                    seeds=case.seeds,
                    evidence=normalized.evidence,
                )

                actual_counts = Counter(
                    item.source_kind.value for item in normalized.evidence
                )
                self.assertEqual(dict(case.expected["sourceCounts"]), dict(actual_counts))
                evidence_indexes = {
                    item.evidence_id: index
                    for index, item in enumerate(normalized.evidence)
                }
                self.assertEqual(
                    tuple(case.expected["evidenceSequence"]),
                    tuple(
                        {
                            "sourceKind": item.source_kind.value,
                            "freshness": item.freshness.value,
                            "location": item.location,
                            "content": item.content,
                            "parentIndex": (
                                evidence_indexes[item.parent_evidence_id]
                                if item.parent_evidence_id
                                else None
                            ),
                            "actorEmail": item.actor.email.lower(),
                            "actorRole": item.actor.role.value,
                        }
                        for item in normalized.evidence
                    ),
                )
                self.assertEqual(
                    tuple(case.expected["failures"]),
                    tuple(
                        {
                            "sourceKind": item.source_kind.value,
                            "location": item.location,
                            "reason": item.reason,
                            "parentIndex": (
                                evidence_indexes[item.parent_evidence_id]
                                if item.parent_evidence_id
                                else None
                            ),
                        }
                        for item in normalized.failures
                    ),
                )
                actual_entities = sorted(
                    (
                        item.entity_type.value,
                        item.label,
                        item.canonical_address,
                        item.suite,
                        item.relationship,
                        tuple(sorted(evidence_indexes[value] for value in item.evidence_ids)),
                    )
                    for item in resolved.entities
                )
                expected_entities = sorted(
                    (
                        item["entityType"],
                        item["label"],
                        item["canonicalAddress"],
                        item["suite"],
                        item["relationship"],
                        tuple(item["evidenceIndexes"]),
                    )
                    for item in case.expected["entities"]
                )
                self.assertEqual(
                    len(resolved.entities),
                    len({item.entity_id for item in resolved.entities}),
                )
                self.assertEqual(expected_entities, actual_entities)

                entity_by_id = {item.entity_id: item for item in resolved.entities}
                matched_indexes = {}
                expected_match_kind = {}
                for item in resolved.entities:
                    matched_indexes[item.entity_id] = set()
                    if item.entity_type.value == "contact":
                        expected_match_kind[item.entity_id] = "actor_contact"
                    elif item.entity_type.value == "suite":
                        expected_match_kind[item.entity_id] = "suite_reference"
                    elif item.relationship == "alternate":
                        expected_match_kind[item.entity_id] = "alternate_address"
                    else:
                        expected_match_kind[item.entity_id] = "target_exact"
                for match in resolved.matches:
                    self.assertIn(match.entity_id, entity_by_id)
                    self.assertIn(match.evidence_id, evidence_indexes)
                    self.assertEqual(
                        expected_match_kind[match.entity_id],
                        match.match_kind,
                    )
                    matched_indexes[match.entity_id].add(
                        evidence_indexes[match.evidence_id]
                    )
                    source_kind = next(
                        item.source_kind.value
                        for item in normalized.evidence
                        if item.evidence_id == match.evidence_id
                    )
                    self.assertEqual(
                        {
                            "quoted_body": 0.6,
                            "forwarded_body": 0.5,
                            "signature": 0.9,
                        }.get(source_kind, 1.0),
                        match.confidence,
                    )
                self.assertEqual(
                    len(resolved.matches),
                    len(
                        {
                            (item.evidence_id, item.entity_id, item.match_kind)
                            for item in resolved.matches
                        }
                    ),
                )
                for item in resolved.entities:
                    self.assertEqual(
                        set(evidence_indexes[value] for value in item.evidence_ids),
                        matched_indexes[item.entity_id],
                    )

                self.assertEqual(
                    sorted(
                        (
                            item["code"],
                            tuple(item["evidenceIndexes"]),
                        )
                        for item in case.expected["issues"]
                    ),
                    sorted(
                        (
                            issue.code,
                            tuple(
                                sorted(
                                    evidence_indexes[value]
                                    for value in issue.evidence_ids
                                )
                            ),
                        )
                        for issue in resolved.issues
                    ),
                )

    def test_unknown_keys_are_rejected_at_every_schema_level(self):
        raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        mutations = (
            lambda value: value.__setitem__("unknownRoot", True),
            lambda value: value["cases"][0].__setitem__("unknownCase", True),
            lambda value: value["cases"][0]["message"].__setitem__(
                "unknownMessage", True
            ),
            lambda value: value["cases"][0]["seeds"][0].__setitem__(
                "unknownSeed", True
            ),
            lambda value: value["cases"][2]["message"]["external"][0].__setitem__(
                "unknownExternal", True
            ),
            lambda value: value["cases"][0]["expected"].__setitem__(
                "unknownExpected", True
            ),
        )

        for mutate in mutations:
            with self.subTest(mutation=mutate):
                candidate = json.loads(json.dumps(raw))
                mutate(candidate)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "invalid.json"
                    path.write_text(json.dumps(candidate), encoding="utf-8")
                    with self.assertRaises(InterpretationFixtureValidationError):
                        load_interpretation_fixture_catalog(path)

    def test_non_string_message_text_is_rejected(self):
        raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        raw["cases"][0]["message"]["body"] = 42
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps(raw), encoding="utf-8")

            with self.assertRaises(InterpretationFixtureValidationError):
                load_interpretation_fixture_catalog(path)


if __name__ == "__main__":
    unittest.main()
