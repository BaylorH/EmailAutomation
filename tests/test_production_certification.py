"""Closed certification request, runtime registry parity, and sealed input.

A certification caller may name only an approved scenario, a unique run id, and
the exact revision it expects. It may never choose a user, client, recipient,
body, spreadsheet, thread, or resource location - those come only from the bound
immutable fixture-config secret at execution time.
"""

from pathlib import Path
import json
import unittest

from email_automation.certification.canonical_json import (
    CanonicalJSONError,
    canonical_bytes,
    digest_of_bytes,
)
from email_automation.certification.input_handoff import SealedInput
from email_automation.certification.models import (
    CertificationRequest,
    CertificationRequestError,
)
from email_automation.certification import scenarios


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = (
    REPO_ROOT
    / "docs"
    / "superpowers"
    / "plans"
    / "2026-08-17-production-automation-certification-scenarios.json"
)
REGISTRY_PATH = REPO_ROOT / "email_automation" / "certification" / "scenario_registry.json"

VALID_REVISION = "1a20ba44a46e0aeed7620a6408856c0aacf6c7d9"
BOOTSTRAP_SCENARIO_ID = "campaign-one-property"


def valid_request_payload(**overrides):
    payload = {
        "scenarioId": BOOTSTRAP_SCENARIO_ID,
        "runId": "cert-20260817-0001",
        "expectedRevision": VALID_REVISION,
    }
    payload.update(overrides)
    return payload


class ProductionCertificationModelTests(unittest.TestCase):
    """The request schema is closed. Anything outside it is a refusal."""

    def setUp(self):
        self.seen_run_ids = set()

    def _parse(self, payload, **kwargs):
        kwargs.setdefault("current_revision", VALID_REVISION)
        kwargs.setdefault("known_scenario_ids", scenarios.scenario_ids())
        kwargs.setdefault("used_run_ids", self.seen_run_ids)
        return CertificationRequest.parse(payload, **kwargs)

    def test_exact_plain_request_is_accepted(self):
        request = self._parse(valid_request_payload())
        self.assertEqual(request.scenario_id, BOOTSTRAP_SCENARIO_ID)
        self.assertEqual(request.run_id, "cert-20260817-0001")
        self.assertEqual(request.expected_revision, VALID_REVISION)

    def test_request_is_immutable(self):
        request = self._parse(valid_request_payload())
        with self.assertRaises(Exception):
            request.scenario_id = "something-else"  # type: ignore[misc]

    def test_missing_key_is_rejected(self):
        for field in ("scenarioId", "runId", "expectedRevision"):
            with self.subTest(field=field):
                payload = valid_request_payload()
                del payload[field]
                with self.assertRaises(CertificationRequestError) as ctx:
                    self._parse(payload)
                self.assertIn(field, str(ctx.exception))

    def test_extra_key_is_rejected(self):
        with self.assertRaises(CertificationRequestError) as ctx:
            self._parse(valid_request_payload(extra="nope"))
        self.assertIn("extra", str(ctx.exception).lower())

    def test_caller_may_not_choose_a_resource_or_recipient(self):
        for field in (
            "uid",
            "clientId",
            "sheetId",
            "recipient",
            "to",
            "body",
            "threadId",
            "driveFolderId",
            "oracle",
        ):
            with self.subTest(field=field):
                with self.assertRaises(CertificationRequestError) as ctx:
                    self._parse(valid_request_payload(**{field: "attacker-chosen"}))
                self.assertIn(field, str(ctx.exception))

    def test_non_string_value_is_rejected(self):
        for field in ("scenarioId", "runId", "expectedRevision"):
            with self.subTest(field=field):
                with self.assertRaises(CertificationRequestError):
                    self._parse(valid_request_payload(**{field: 17}))

    def test_whitespace_padded_id_is_rejected(self):
        for value in (" campaign-one-property", "campaign-one-property ", "\tcampaign-one-property"):
            with self.subTest(value=value):
                with self.assertRaises(CertificationRequestError) as ctx:
                    self._parse(valid_request_payload(scenarioId=value))
                self.assertIn("whitespace", str(ctx.exception).lower())

    def test_unknown_scenario_is_rejected(self):
        with self.assertRaises(CertificationRequestError) as ctx:
            self._parse(valid_request_payload(scenarioId="not-a-scenario"))
        self.assertIn("unknown scenario", str(ctx.exception).lower())

    def test_reused_run_id_is_rejected(self):
        first = valid_request_payload()
        self._parse(first)
        self.seen_run_ids.add(first["runId"])
        with self.assertRaises(CertificationRequestError) as ctx:
            self._parse(first)
        self.assertIn("run id", str(ctx.exception).lower())

    def test_mismatched_revision_is_rejected(self):
        with self.assertRaises(CertificationRequestError) as ctx:
            self._parse(valid_request_payload(expectedRevision="0" * 40))
        self.assertIn("revision", str(ctx.exception).lower())

    def test_empty_values_are_rejected(self):
        for field in ("scenarioId", "runId", "expectedRevision"):
            with self.subTest(field=field):
                with self.assertRaises(CertificationRequestError):
                    self._parse(valid_request_payload(**{field: ""}))


class RuntimeScenarioRegistryTests(unittest.TestCase):
    """The in-image registry is the runtime authority and matches the manifest exactly."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.raw = REGISTRY_PATH.read_bytes()

    def test_registry_bytes_are_canonical(self):
        parsed = json.loads(self.raw.decode("utf-8"))
        self.assertEqual(
            canonical_bytes(parsed),
            self.raw,
            "the on-disk registry must already be canonical bytes",
        )

    def test_registry_digest_is_lowercase_sha256_of_those_bytes(self):
        digest = scenarios.registry_digest()
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(digest, digest_of_bytes(self.raw))

    def test_registry_holds_exactly_ninety_three_unique_scenarios(self):
        all_scenarios = scenarios.all_scenarios()
        self.assertEqual(len(all_scenarios), 93)
        ids = [item["scenarioId"] for item in all_scenarios]
        self.assertEqual(len(set(ids)), 93)

    def test_registry_classes_split_one_bootstrap_one_refutation_ninety_one_capability(self):
        counts = {}
        for item in scenarios.all_scenarios():
            counts[item["scenarioClass"]] = counts.get(item["scenarioClass"], 0) + 1
        self.assertEqual(counts, {"bootstrap": 1, "refutation": 1, "capability": 91})

    def test_every_manifest_scenario_appears_one_for_one_in_the_registry(self):
        manifest_ids = {item["scenarioId"] for item in self.manifest["scenarioDefinitions"]}
        manifest_ids.add(self.manifest["bootstrapScenario"]["scenarioId"])
        manifest_ids.update(
            item["scenarioId"] for item in self.manifest["refutationScenarios"]
        )
        self.assertEqual(set(scenarios.scenario_ids()), manifest_ids)

    def test_registry_fields_match_the_manifest_field_for_field(self):
        manifest_by_id = {
            item["scenarioId"]: item for item in self.manifest["scenarioDefinitions"]
        }
        manifest_by_id[self.manifest["bootstrapScenario"]["scenarioId"]] = self.manifest[
            "bootstrapScenario"
        ]
        for item in self.manifest["refutationScenarios"]:
            manifest_by_id[item["scenarioId"]] = item

        for scenario_id, expected in manifest_by_id.items():
            with self.subTest(scenario=scenario_id):
                actual = dict(scenarios.get(scenario_id))
                actual.pop("scenarioClass")
                self.assertEqual(
                    actual, expected, "runtime scenario drifted from the approved manifest"
                )

    def test_bootstrap_and_refutation_never_carry_a_capability_stamp(self):
        for item in scenarios.all_scenarios():
            if item["scenarioClass"] in ("bootstrap", "refutation"):
                self.assertFalse(item["capabilityStamp"])

    def test_registry_carries_no_concrete_resource_identity(self):
        text = self.raw.decode("utf-8")
        for marker in ("@", "://", "docs.google", "drive.google"):
            self.assertNotIn(
                marker,
                text,
                "the registry owns logical aliases only; concrete identities come "
                "from the bound fixture-config secret",
            )

    def test_unknown_scenario_lookup_raises(self):
        with self.assertRaises(KeyError):
            scenarios.get("not-a-scenario")


class SealedInputTests(unittest.TestCase):
    """A sealed input is bytes. Mutating the caller's object cannot reach it."""

    def _payload(self):
        return {"scenarioId": BOOTSTRAP_SCENARIO_ID, "nested": {"rows": [{"a": 1}, {"b": 2}]}}

    def test_sealing_stores_canonical_bytes_and_digest(self):
        sealed = SealedInput.seal(self._payload())
        self.assertEqual(sealed.canonical_bytes, canonical_bytes(self._payload()))
        self.assertEqual(sealed.digest, digest_of_bytes(sealed.canonical_bytes))
        self.assertRegex(sealed.digest, r"^[0-9a-f]{64}$")

    def test_mutating_the_original_nested_payload_changes_nothing(self):
        payload = self._payload()
        sealed = SealedInput.seal(payload)
        before_bytes, before_digest = sealed.canonical_bytes, sealed.digest
        before_execution = sealed.execution_input()

        # Hostile post-seal mutation of the caller-owned object, including nesting.
        payload["scenarioId"] = "attacker-swapped"
        payload["nested"]["rows"].append({"c": 3})
        payload["nested"]["rows"][0]["a"] = 999

        self.assertEqual(sealed.canonical_bytes, before_bytes)
        self.assertEqual(sealed.digest, before_digest)
        self.assertEqual(sealed.execution_input(), before_execution)
        self.assertEqual(sealed.execution_input()["scenarioId"], BOOTSTRAP_SCENARIO_ID)
        self.assertEqual(len(sealed.execution_input()["nested"]["rows"]), 2)

    def test_execution_input_is_a_fresh_decode_each_time(self):
        sealed = SealedInput.seal(self._payload())
        first = sealed.execution_input()
        first["nested"]["rows"].append({"injected": True})
        second = sealed.execution_input()
        self.assertEqual(len(second["nested"]["rows"]), 2, "execution input must not be shared")

    def test_evidence_digest_is_stable_across_reseal(self):
        self.assertEqual(
            SealedInput.seal(self._payload()).digest,
            SealedInput.seal(self._payload()).digest,
        )

    def test_sealing_a_non_canonical_type_is_refused(self):
        with self.assertRaises(CanonicalJSONError):
            SealedInput.seal({"rent": 20.5})

    def test_sealed_bytes_are_not_writable_through_the_attribute(self):
        sealed = SealedInput.seal(self._payload())
        self.assertIsInstance(sealed.canonical_bytes, bytes)
        with self.assertRaises(Exception):
            sealed.digest = "0" * 64  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
