import copy
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.scan_clearance_evidence_pii import EMAIL_RE, PROPERTY_ADDRESS_RE


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


def _contract(
    facts,
    expected_events,
    forbidden_events,
    reply_policy,
    body_tokens_in_order,
    body_tokens_absent=(),
):
    return {
        "facts": facts,
        "expectedEvents": expected_events,
        "forbiddenEvents": forbidden_events,
        "expectedReplyPolicy": reply_policy,
        "bodyTokensInOrder": body_tokens_in_order,
        "bodyTokensAbsent": body_tokens_absent,
    }


SEMANTIC_CONTRACTS = {
    "flyer_wording_not_tour": _contract(
        {"flyerMention": True, "tourOffered": False},
        ("attachment_reference_recorded",),
        ("tour_action_created",),
        "continue",
        ("brochure", "does not offer", "walkthrough"),
    ),
    "complete_rent_opex_ti": _contract(
        {
            "availability": "available",
            "rent": "$14.25",
            "operatingExpenses": "$3.10",
            "tenantImprovement": "$1.50",
        },
        ("economics_extracted", "completion_evaluated"),
        ("unsourced_economics",),
        "continue",
        ("$14.25", "$3.10", "$1.50"),
    ),
    "partial_rent_opex_ti": _contract(
        {
            "rent": "$12.80",
            "operatingExpenses": "missing",
            "tenantImprovement": "missing",
        },
        ("economics_partial_extracted", "missing_facts_followup"),
        ("premature_completion",),
        "follow_up_missing_facts",
        ("$12.80", "operating expenses", "improvement allowance", "confirmed"),
    ),
    "property_unavailable": _contract(
        {"propertyAvailable": False, "alternateOffered": False},
        ("property_unavailable",),
        ("property_non_fit",),
        "stop_property_outreach",
        ("no longer available", "does not include a replacement"),
    ),
    "property_non_fit": _contract(
        {"propertyAvailable": True, "fit": False},
        ("property_non_fit",),
        ("property_unavailable",),
        "stop_property_outreach",
        ("remains available", "does not fit"),
    ),
    "tour_unavailable_property_viable": _contract(
        {"propertyAvailable": True, "tourAvailable": False},
        ("tour_restriction_recorded",),
        ("property_unavailable",),
        "continue_property_research",
        ("space is available", "walkthroughs are paused"),
    ),
    "quoted_stale_terminal_fresh_positive": _contract(
        {"quotedAvailability": "unavailable", "freshAvailability": "available"},
        ("fresh_availability_correction",),
        ("stale_terminal_state",),
        "continue",
        (
            "quoted earlier",
            "no longer available",
            "current correction",
            "space is available",
        ),
    ),
    "confidential_question_with_useful_facts": _contract(
        {"squareFeet": 28000, "dockDoors": 2, "identityQuestion": True},
        ("facts_extracted", "confidential_question_action"),
        ("confidential_disclosure",),
        "pause_for_operator",
        (
            "28,000 square feet",
            "two dock doors",
            "before discussing pricing",
            "identify the represented organization",
        ),
    ),
    "call_request_with_number": _contract(
        {"callRequested": True, "numberPresent": True},
        ("call_action_created",),
        ("tour_invite_created",),
        "pause_for_operator",
        ("please call", "555-0107"),
    ),
    "call_request_without_number": _contract(
        {"callRequested": True, "numberPresent": False},
        ("call_action_created",),
        ("tour_invite_created",),
        "pause_for_operator",
        ("a call", "suitable time", "coordinate"),
        ("555-",),
    ),
    "core_tour_offer": _contract(
        {"propertyAvailable": True, "tourOffered": True},
        ("tour_action_created",),
        ("tour_invite_created",),
        "pause_for_operator",
        ("remains available", "offer a walkthrough"),
    ),
    "alternate_tour_time": _contract(
        {"proposedTimeRejected": True, "alternateTimeOffered": True},
        ("tour_alternate_time_action",),
        ("route_plan_created",),
        "pause_for_operator",
        ("will not work", "available instead"),
    ),
    "temporary_tour_restriction": _contract(
        {"propertyAvailable": True, "restrictionTemporary": True},
        ("tour_restriction_recorded",),
        ("property_unavailable",),
        "continue_property_research",
        ("property is available", "walkthroughs cannot occur", "this week"),
    ),
    "alternate_property_before_rejection": _contract(
        {"alternateOffered": True, "originalFit": False},
        ("alternate_property_referred", "original_property_non_fit"),
        ("alternate_facts_crossed",),
        "pause_for_operator",
        ("another portfolio space", "originally requested space", "not a fit"),
    ),
    "alternate_property_after_rejection": _contract(
        {"alternateOffered": True, "originalFit": False},
        ("original_property_non_fit", "alternate_property_referred"),
        ("alternate_facts_crossed",),
        "pause_for_operator",
        ("requested space", "not a fit", "another portfolio space"),
    ),
    "out_of_office": _contract(
        {"automaticReply": True, "wrongContact": False},
        ("out_of_office_recorded",),
        ("wrong_contact_transition",),
        "defer_until_return",
        ("automatic absence notice", "return window"),
    ),
    "wrong_contact": _contract(
        {"wrongContact": True, "replacementKnown": False},
        ("wrong_contact_action",),
        ("redirected_contact_send",),
        "pause_for_operator",
        ("does not handle leasing", "correct role"),
    ),
    "forwarded_contact": _contract(
        {"forwarded": True, "replacementRoleKnown": True},
        ("forwarded_contact_action",),
        ("redirected_contact_send",),
        "pause_for_operator",
        ("forwarded for handling", "correct role"),
    ),
    "opt_out": _contract(
        {"optOut": True, "scope": "contact"},
        ("opt_out_applied",),
        ("future_followup",),
        "no_reply",
        ("stop this outreach", "do not send"),
    ),
    "attachment_only": _contract(
        {"bodyFacts": False, "attachmentPresent": True},
        ("attachment_reference_recorded",),
        ("processed_without_extraction",),
        "extract_attachment",
        ("attached fact sheet",),
    ),
    "protected_link": _contract(
        {"linkPresent": True, "authorizationRequired": True},
        ("protected_link_issue",),
        ("premature_completion",),
        "pause_for_operator",
        ("shared link", "access requires authorization"),
    ),
    "wrong_property_attachment": _contract(
        {
            "attachmentProperty": "portfolio_item_b",
            "requestedProperty": "portfolio_item_a",
        },
        ("attachment_property_mismatch",),
        ("cross_property_extraction",),
        "pause_for_operator",
        ("portfolio item b", "not the requested", "portfolio item a"),
    ),
    "manual_mailbox_continuation_before_retry": _contract(
        {"manualContinuation": True, "retryQueued": True},
        ("manual_continuation_detected",),
        ("autonomous_retry_send",),
        "suppress_autonomous_retry",
        ("already continued", "before the queued retry"),
    ),
    "property_issue_severity": _contract(
        {"issue": "localized_water_leak", "propertyOperating": True},
        ("property_issue_action_with_severity",),
        ("property_terminalized",),
        "pause_for_operator",
        ("water leak", "remainder", "operating", "flag the issue"),
    ),
    "projection_failure": _contract(
        {"sourceProcessed": True, "dashboardProjectionVisible": False},
        ("projection_recovery_required",),
        ("event_settled_without_projection",),
        "record_operator_visible_failure",
        ("processed", "did not appear", "keep the event open", "projection recovery"),
    ),
}


def _iter_fixture_strings(value, path="$"):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for index, (key, child) in enumerate(value.items()):
            yield f"{path}.keys[{index}]", key
            yield from _iter_fixture_strings(child, f"{path}.values[{index}]")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_fixture_strings(child, f"{path}[{index}]")


def _read_fixture():
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


class ProductionBrowserConversationVariantTests(unittest.TestCase):
    def _assert_semantic_contract(self, variant):
        semantic_case = variant["semanticFacts"]["semanticCase"]
        contract = SEMANTIC_CONTRACTS[semantic_case]
        self.assertEqual(
            {"semanticCase": semantic_case, **contract["facts"]},
            variant["semanticFacts"],
        )
        self.assertEqual(
            list(contract["expectedEvents"]),
            variant["expectedEvents"],
        )
        self.assertEqual(
            list(contract["forbiddenEvents"]),
            variant["forbiddenEvents"],
        )
        self.assertEqual(
            contract["expectedReplyPolicy"],
            variant["expectedReplyPolicy"],
        )

        body = variant["body"].casefold()
        previous_position = -1
        for token in contract["bodyTokensInOrder"]:
            position = body.find(token.casefold(), previous_position + 1)
            self.assertGreaterEqual(
                position,
                0,
                f"{semantic_case} body is missing required token {token!r}",
            )
            self.assertGreater(position, previous_position)
            previous_position = position
        for token in contract["bodyTokensAbsent"]:
            self.assertNotIn(token.casefold(), body)

    def _find_fixture_pii(self, fixture):
        findings = []
        for path, value in _iter_fixture_strings(fixture):
            if EMAIL_RE.search(value):
                findings.append({"path": path, "kind": "email"})
            if PROPERTY_ADDRESS_RE.search(value):
                findings.append({"path": path, "kind": "property_address"})
        return findings

    def _assert_fixture_rejected(self, fixture):
        from scripts.select_production_browser_variant import load_variants

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "variants.json"
            path.write_text(json.dumps(fixture), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_variants(path)

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

    def test_every_required_historical_case_satisfies_executable_contract(self):
        variants = _read_fixture()["scenarioFamilies"]
        variants_by_case = {
            variant["semanticFacts"]["semanticCase"]: variant for variant in variants
        }
        self.assertEqual(REQUIRED_SEMANTIC_CASES, set(variants_by_case))
        for semantic_case, variant in variants_by_case.items():
            with self.subTest(semantic_case=semantic_case):
                self._assert_semantic_contract(variant)

    def test_semantic_contract_rejects_label_only_and_unrelated_prose_mutations(self):
        original = _read_fixture()["scenarioFamilies"][0]
        label_only = copy.deepcopy(original)
        label_only["semanticFacts"] = {
            "semanticCase": original["semanticFacts"]["semanticCase"]
        }
        unrelated_prose = copy.deepcopy(original)
        unrelated_prose["body"] = (
            "This unrelated note contains no required scenario meaning."
        )
        unrelated_prose["bodySha256"] = hashlib.sha256(
            unrelated_prose["body"].encode("utf-8")
        ).hexdigest()

        for name, variant in (
            ("label_only", label_only),
            ("unrelated_prose", unrelated_prose),
        ):
            with self.subTest(case=name), self.assertRaises(AssertionError):
                self._assert_semantic_contract(variant)

    def test_all_fixture_strings_are_recursively_sanitized(self):
        self.assertEqual([], self._find_fixture_pii(_read_fixture()))

    def test_recursive_sanitizer_detects_non_body_pii_and_lane_addresses(self):
        fixture = _read_fixture()
        runtime_email = "@".join(("leasing", ".".join(("example", "com"))))
        runtime_lane = " ".join(("424", "Research", "Lane"))
        mutations = (
            ("email_in_semantic_facts", "contactNote", runtime_email, "email"),
            (
                "lane_in_semantic_facts",
                "locationNote",
                runtime_lane,
                "property_address",
            ),
        )

        for name, field, value, expected_kind in mutations:
            with self.subTest(case=name):
                mutated = copy.deepcopy(fixture)
                mutated["scenarioFamilies"][0]["semanticFacts"][field] = value
                findings = self._find_fixture_pii(mutated)
                self.assertTrue(
                    any(finding["kind"] == expected_kind for finding in findings),
                    findings,
                )

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

    def test_loader_rejects_invalid_root_collections_and_historical_sources(self):
        fixture = _read_fixture()
        root_cases = (
            ("historical_sources_null", "historicalSources", None),
            ("historical_sources_empty", "historicalSources", []),
            ("historical_sources_object", "historicalSources", {}),
            ("production_use_null", "productionUse", None),
            ("production_use_object", "productionUse", {}),
        )
        for name, field, value in root_cases:
            with self.subTest(case=name):
                mutated = copy.deepcopy(fixture)
                mutated[field] = value
                self._assert_fixture_rejected(mutated)

        valid_source = fixture["historicalSources"][0]
        source_cases = (
            ("source_not_object", "not-an-object"),
            (
                "missing_source_id",
                {key: value for key, value in valid_source.items() if key != "sourceId"},
            ),
            ("empty_source_id", {**valid_source, "sourceId": ""}),
            ("bad_source_class", {**valid_source, "sourceClass": "unknown"}),
            ("non_string_source_class", {**valid_source, "sourceClass": []}),
            ("non_string_path", {**valid_source, "path": 7}),
            ("empty_path", {**valid_source, "path": ""}),
            (
                "bad_sanitization_policy",
                {**valid_source, "sanitizationPolicy": "copy_raw_text"},
            ),
            ("extra_source_key", {**valid_source, "unexpected": True}),
        )
        for name, source in source_cases:
            with self.subTest(case=name):
                mutated = copy.deepcopy(fixture)
                mutated["historicalSources"] = [source]
                self._assert_fixture_rejected(mutated)

    def test_loader_rejects_invalid_complete_variant_schema_types(self):
        fixture = _read_fixture()
        variant = fixture["scenarioFamilies"][0]
        valid_axes = variant["axes"]
        cases = (
            ("source_class", "sourceClass", "unknown"),
            ("source_class_non_string", "sourceClass", []),
            ("semantic_facts_null", "semanticFacts", None),
            ("semantic_facts_empty", "semanticFacts", {}),
            ("semantic_facts_list", "semanticFacts", []),
            ("expected_events_null", "expectedEvents", None),
            ("expected_events_empty", "expectedEvents", []),
            ("expected_events_empty_value", "expectedEvents", [""]),
            ("expected_events_non_string", "expectedEvents", [7]),
            ("forbidden_events_null", "forbiddenEvents", None),
            ("forbidden_events_empty", "forbiddenEvents", []),
            ("forbidden_events_empty_value", "forbiddenEvents", [""]),
            ("forbidden_events_non_string", "forbiddenEvents", [7]),
            ("reply_policy_null", "expectedReplyPolicy", None),
            ("reply_policy_empty", "expectedReplyPolicy", ""),
            ("axes_null", "axes", None),
            (
                "axes_missing_key",
                "axes",
                {key: value for key, value in valid_axes.items() if key != "tone"},
            ),
            ("axes_extra_key", "axes", {**valid_axes, "unexpected": "value"}),
            ("axes_empty_value", "axes", {**valid_axes, "tone": ""}),
            ("axes_non_string_value", "axes", {**valid_axes, "tone": 7}),
            ("body_empty", "body", ""),
            ("last_production_use_non_null", "lastProductionUse", "2026-08-06"),
        )
        for name, field, value in cases:
            with self.subTest(case=name):
                mutated = copy.deepcopy(fixture)
                mutated["scenarioFamilies"][0][field] = value
                self._assert_fixture_rejected(mutated)

    def test_loader_and_checkpoint_history_reject_nonstandard_json_constants(self):
        from scripts.select_production_browser_variant import (
            load_used_hashes,
            load_variants,
        )

        fixture = _read_fixture()
        fixture["productionUse"] = [float("nan")]
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            fixture_path = temp_root / "variants.json"
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_variants(fixture_path)

            checkpoint_path = temp_root / "checkpoints.jsonl"
            checkpoint_path.write_text(
                '{"exactBodyHashes":[],"probe":Infinity}\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_used_hashes(checkpoint_path)

    def test_cli_fails_closed_for_complete_schema_violations(self):
        fixture = _read_fixture()
        mutations = (
            ("axes_null", "variant", "axes", None),
            ("semantic_facts_null", "variant", "semanticFacts", None),
            (
                "last_production_use_non_null",
                "variant",
                "lastProductionUse",
                "2026-08-06",
            ),
            ("historical_sources_wrong_type", "root", "historicalSources", {}),
            ("production_use_wrong_type", "root", "productionUse", {}),
            ("nonstandard_constant", "root", "productionUse", [float("nan")]),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            checkpoint_path = temp_root / "checkpoints.jsonl"
            checkpoint_path.write_text("", encoding="utf-8")
            for name, target, field, value in mutations:
                with self.subTest(case=name):
                    mutated = copy.deepcopy(fixture)
                    if target == "variant":
                        mutated["scenarioFamilies"][0][field] = value
                    else:
                        mutated[field] = value
                    fixture_path = temp_root / f"{name}.json"
                    fixture_path.write_text(
                        json.dumps(mutated),
                        encoding="utf-8",
                    )
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(SELECTOR_PATH),
                            "flyer_not_tour",
                            str(checkpoint_path),
                            "--fixture",
                            str(fixture_path),
                        ],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertNotEqual(0, result.returncode)
                    self.assertEqual("", result.stdout)

    def test_cli_fails_closed_for_nonstandard_checkpoint_constant(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint_path = Path(temp_dir) / "checkpoints.jsonl"
            checkpoint_path.write_text(
                '{"exactBodyHashes":[],"probe":-Infinity}\n',
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SELECTOR_PATH),
                    "flyer_not_tour",
                    str(checkpoint_path),
                    "--fixture",
                    str(FIXTURE_PATH),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(0, result.returncode)
        self.assertEqual("", result.stdout)

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
