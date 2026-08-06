import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "production_browser_conversation_variants.json"
)
SELECTOR_PATH = REPO_ROOT / "scripts" / "select_production_browser_variant.py"

EXPECTED_ROOT_KEYS = {
    "schemaVersion",
    "historicalSources",
    "scenarioFamilies",
    "productionUse",
}
EXPECTED_VARIANT_KEYS = {
    "variantId",
    "scenarioFamily",
    "sourceClass",
    "semanticFacts",
    "expectedEvents",
    "forbiddenEvents",
    "expectedReplyPolicy",
    "axes",
    "body",
    "bodySha256",
    "lastProductionUse",
}
EXPECTED_AXIS_KEYS = {
    "tone",
    "register",
    "informationOrder",
    "quoteStyle",
    "attachmentBundle",
    "turnTiming",
}
SOURCE_CLASSES = {
    "production_report",
    "production_history",
    "production_model_misread",
    "synthetic_near_miss",
}
REQUIRED_SEMANTIC_CASES = {
    "flyer_wording_not_tour",
    "complete_rent_opex_ti",
    "partial_rent_opex_ti",
    "property_unavailable",
    "property_non_fit",
    "tour_unavailable_property_viable",
    "quoted_stale_terminal_fresh_positive",
    "confidential_question_with_useful_facts",
    "call_request_with_number",
    "call_request_without_number",
    "core_tour_offer",
    "alternate_tour_time",
    "temporary_tour_restriction",
    "alternate_property_before_rejection",
    "alternate_property_after_rejection",
    "out_of_office",
    "wrong_contact",
    "forwarded_contact",
    "opt_out",
    "attachment_only",
    "protected_link",
    "wrong_property_attachment",
    "manual_mailbox_continuation_before_retry",
    "property_issue_severity",
    "projection_failure",
}


def _read_fixture():
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


class ProductionBrowserConversationVariantTests(unittest.TestCase):
    def test_fixture_root_and_historical_sources_are_explicit(self):
        fixture = _read_fixture()
        self.assertEqual(EXPECTED_ROOT_KEYS, set(fixture))
        self.assertEqual(1, fixture["schemaVersion"])
        self.assertTrue(fixture["historicalSources"])
        self.assertIsInstance(fixture["productionUse"], list)

        for source in fixture["historicalSources"]:
            with self.subTest(source=source.get("sourceId")):
                self.assertEqual(
                    {"sourceId", "sourceClass", "path", "sanitizationPolicy"},
                    set(source),
                )
                self.assertIn(source["sourceClass"], SOURCE_CLASSES)
                self.assertTrue((REPO_ROOT / source["path"]).is_file())
                self.assertEqual("semantic_facts_only", source["sanitizationPolicy"])

    def test_every_variant_has_valid_schema_hash_and_sanitized_body(self):
        variants = _read_fixture()["scenarioFamilies"]
        self.assertTrue(variants)
        variant_ids = []
        body_hashes = []

        for variant in variants:
            with self.subTest(variant=variant.get("variantId")):
                self.assertEqual(EXPECTED_VARIANT_KEYS, set(variant))
                self.assertEqual(EXPECTED_AXIS_KEYS, set(variant["axes"]))
                self.assertIn(variant["sourceClass"], SOURCE_CLASSES)
                self.assertTrue(variant["scenarioFamily"])
                self.assertTrue(variant["semanticFacts"])
                self.assertTrue(variant["expectedEvents"])
                self.assertTrue(variant["forbiddenEvents"])
                self.assertTrue(variant["expectedReplyPolicy"])
                self.assertTrue(variant["body"])
                self.assertIsNone(variant["lastProductionUse"])
                self.assertRegex(variant["bodySha256"], r"^[0-9a-f]{64}$")
                self.assertEqual(
                    hashlib.sha256(variant["body"].encode("utf-8")).hexdigest(),
                    variant["bodySha256"],
                )
                self.assertIsNone(
                    re.search(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", variant["body"]),
                    "Sanitized fixture bodies must not contain email addresses.",
                )
                self.assertIsNone(
                    re.search(
                        r"\b\d{1,6}\s+(?:[A-Za-z0-9.-]+\s+){0,4}"
                        r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr)\b",
                        variant["body"],
                        re.IGNORECASE,
                    ),
                    "Sanitized fixture bodies must not contain property addresses.",
                )

            variant_ids.append(variant["variantId"])
            body_hashes.append(variant["bodySha256"])

        self.assertEqual(len(variant_ids), len(set(variant_ids)))
        self.assertEqual(len(body_hashes), len(set(body_hashes)))

    def test_required_historical_semantics_are_seeded(self):
        variants = _read_fixture()["scenarioFamilies"]
        semantic_cases = {
            variant["semanticFacts"]["semanticCase"] for variant in variants
        }
        self.assertTrue(REQUIRED_SEMANTIC_CASES.issubset(semantic_cases))

    def test_select_unused_variant_is_deterministic(self):
        from scripts.select_production_browser_variant import select_unused_variant

        variants = [
            {
                "variantId": "call_action.002",
                "scenarioFamily": "call_action",
                "bodySha256": "b" * 64,
            },
            {
                "variantId": "call_action.001",
                "scenarioFamily": "call_action",
                "bodySha256": "a" * 64,
            },
            {
                "variantId": "tour_action.001",
                "scenarioFamily": "tour_action",
                "bodySha256": "c" * 64,
            },
        ]

        selected = select_unused_variant("call_action", variants, set())
        self.assertEqual("call_action.001", selected["variantId"])

    def test_select_unused_variant_rejects_used_body_hashes(self):
        from scripts.select_production_browser_variant import select_unused_variant

        variants = [
            {
                "variantId": "call_action.001",
                "scenarioFamily": "call_action",
                "bodySha256": "a" * 64,
            },
            {
                "variantId": "call_action.002",
                "scenarioFamily": "call_action",
                "bodySha256": "b" * 64,
            },
        ]

        selected = select_unused_variant("call_action", variants, {"a" * 64})
        self.assertEqual("call_action.002", selected["variantId"])
        with self.assertRaisesRegex(RuntimeError, "no unused production variant"):
            select_unused_variant("call_action", variants, {"a" * 64, "b" * 64})

    def test_select_unused_variant_rejects_malformed_records(self):
        from scripts.select_production_browser_variant import select_unused_variant

        with self.assertRaises(ValueError):
            select_unused_variant(
                "call_action",
                [{"variantId": "call_action.001", "scenarioFamily": "call_action"}],
                set(),
            )
        duplicate_hash = "d" * 64
        with self.assertRaisesRegex(ValueError, "duplicate variant bodySha256"):
            select_unused_variant(
                "call_action",
                [
                    {
                        "variantId": "call_action.001",
                        "scenarioFamily": "call_action",
                        "bodySha256": duplicate_hash,
                    },
                    {
                        "variantId": "call_action.002",
                        "scenarioFamily": "call_action",
                        "bodySha256": duplicate_hash,
                    },
                ],
                set(),
            )

    def test_cli_prints_exactly_one_unused_record_and_never_falls_back(self):
        fixture = _read_fixture()
        family = "flyer_not_tour"
        variant = next(
            item for item in fixture["scenarioFamilies"] if item["scenarioFamily"] == family
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint_path = Path(temp_dir) / "checkpoints.jsonl"
            checkpoint_path.write_text("", encoding="utf-8")
            command = [
                sys.executable,
                str(SELECTOR_PATH),
                family,
                str(checkpoint_path),
                "--fixture",
                str(FIXTURE_PATH),
            ]
            first = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(1, len(first.stdout.splitlines()))
            self.assertEqual(variant, json.loads(first.stdout))
            self.assertEqual("", first.stderr)

            used_checkpoint = {
                "checkpointId": "SYNTHETIC-001",
                "exactBodyHashes": [variant["bodySha256"]],
            }
            checkpoint_path.write_text(
                json.dumps(used_checkpoint) + "\n", encoding="utf-8"
            )
            exhausted = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertNotEqual(0, exhausted.returncode)
            self.assertEqual("", exhausted.stdout)
            self.assertIn("no unused production variant", exhausted.stderr)


if __name__ == "__main__":
    unittest.main()
