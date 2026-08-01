import json
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path

from email_automation.campaign_capabilities import (
    CampaignCapabilitiesResolution,
    CapabilityDecision,
    capability_allowed,
    read_campaign_capabilities,
    resolve_campaign_capabilities,
)


CONTRACT = json.loads(
    (Path(__file__).parents[1] / "contracts" / "campaign-capabilities-v2.json").read_text(
        encoding="utf-8"
    )
)


def materialize(value):
    if isinstance(value, list):
        return [materialize(child) for child in value]
    if not isinstance(value, dict):
        return value
    if CONTRACT["timestampSentinel"] in value:
        return datetime.fromisoformat(
            value[CONTRACT["timestampSentinel"]].replace("Z", "+00:00")
        )
    return {key: materialize(child) for key, child in value.items()}


def as_contract(resolution):
    return {
        "sourcePath": resolution.source_path,
        "schemaVersion": resolution.schema_version,
        "revision": resolution.revision,
        "decisions": {
            name: {
                "allowed": resolution.decisions[name].allowed,
                "reasonCode": resolution.decisions[name].reason_code,
            }
            for name in CONTRACT["capabilityNames"]
        },
    }


def fixture(case_id):
    for candidate in CONTRACT["cases"]:
        if candidate["id"] == case_id:
            return candidate
    raise AssertionError(f"missing fixture {case_id}")


def resolve_fixture(candidate):
    test_input = materialize(candidate["input"])
    return resolve_campaign_capabilities(
        uid=test_input.get("uid"),
        document_id=test_input.get("documentId"),
        data=test_input.get("data"),
    )


class _FakeSnapshot:
    def __init__(self, *, document_id, data=None, exists=True):
        self.id = document_id
        self.exists = exists
        self._data = data

    def to_dict(self):
        return self._data


class _FakeDocument:
    def __init__(self, firestore_client, document_id):
        self._firestore_client = firestore_client
        self._document_id = document_id

    def get(self):
        if self._firestore_client.read_error is not None:
            raise self._firestore_client.read_error
        return self._firestore_client.snapshot


class _FakeCollection:
    def __init__(self, firestore_client):
        self._firestore_client = firestore_client

    def document(self, document_id):
        self._firestore_client.reads.append(
            f"campaignCapabilities/{document_id}"
        )
        return _FakeDocument(self._firestore_client, document_id)


class _FakeFirestore:
    def __init__(self, *, snapshot=None, read_error=None):
        self.snapshot = snapshot
        self.read_error = read_error
        self.reads = []

    def collection(self, name):
        if name != "campaignCapabilities":
            raise AssertionError(f"unexpected collection read: {name}")
        return _FakeCollection(self)


class _NoReadFirestore:
    def collection(self, _name):
        raise AssertionError("database access must not occur for an invalid UID")


class CampaignCapabilitiesTests(unittest.TestCase):
    def test_interface_uses_frozen_decision_types(self):
        self.assertTrue(CapabilityDecision.__dataclass_params__.frozen)
        self.assertTrue(CampaignCapabilitiesResolution.__dataclass_params__.frozen)

        candidate = fixture("combination-100")
        resolution = resolve_fixture(candidate)
        with self.assertRaises(FrozenInstanceError):
            resolution.source_path = "campaignCapabilities/changed"
        with self.assertRaises(FrozenInstanceError):
            resolution.decisions["start"].allowed = False

    def test_shared_contract_includes_all_independent_boolean_combinations(self):
        combinations = sorted(
            candidate["id"]
            for candidate in CONTRACT["cases"]
            if candidate["id"].startswith("combination-")
        )
        self.assertEqual(
            [
                "combination-000",
                "combination-001",
                "combination-010",
                "combination-011",
                "combination-100",
                "combination-101",
                "combination-110",
                "combination-111",
            ],
            combinations,
        )

    def test_resolver_matches_every_shared_synthetic_decision_fixture(self):
        for candidate in CONTRACT["cases"]:
            if candidate.get("operation") == "readError":
                continue
            resolved = resolve_fixture(candidate)
            with self.subTest(case=candidate["id"]):
                self.assertEqual(candidate["expected"], as_contract(resolved))

    def test_resolver_trims_uid_and_requires_authoritative_document_id(self):
        candidate = fixture("combination-100")
        test_input = materialize(candidate["input"])

        resolved = resolve_campaign_capabilities(
            uid=f"  {test_input['uid']}  ",
            document_id=test_input["documentId"],
            data=test_input["data"],
        )

        self.assertEqual(candidate["expected"], as_contract(resolved))

    def test_reader_fetches_only_the_authoritative_requested_document(self):
        candidate = fixture("combination-110")
        firestore_client = _FakeFirestore(
            snapshot=_FakeSnapshot(
                document_id=candidate["input"]["documentId"],
                data=materialize(candidate["input"]["data"]),
            )
        )

        resolved = read_campaign_capabilities(
            firestore_client=firestore_client,
            uid=candidate["input"]["uid"],
        )

        self.assertEqual(candidate["expected"], as_contract(resolved))
        self.assertEqual(
            ["campaignCapabilities/synthetic-user-a"], firestore_client.reads
        )

    def test_reader_resolves_a_missing_snapshot_without_legacy_state(self):
        candidate = fixture("document-data-missing")
        firestore_client = _FakeFirestore(
            snapshot=_FakeSnapshot(
                document_id=candidate["input"]["documentId"],
                exists=False,
            )
        )

        resolved = read_campaign_capabilities(
            firestore_client=firestore_client,
            uid=candidate["input"]["uid"],
        )

        self.assertEqual(candidate["expected"], as_contract(resolved))

    def test_reader_converts_every_firestore_failure_to_locked_denial(self):
        candidate = fixture("read-error")
        firestore_client = _FakeFirestore(
            read_error=RuntimeError("synthetic Firestore read failure")
        )

        resolved = read_campaign_capabilities(
            firestore_client=firestore_client,
            uid=candidate["input"]["uid"],
        )

        self.assertEqual(candidate["expected"], as_contract(resolved))

    def test_reader_rejects_blank_uid_before_database_access(self):
        candidate = fixture("uid-blank")

        resolved = read_campaign_capabilities(
            firestore_client=_NoReadFirestore(),
            uid=candidate["input"]["uid"],
        )

        self.assertEqual(candidate["expected"], as_contract(resolved))

    def test_capability_allowed_requires_an_exact_allowed_decision(self):
        candidate = fixture("combination-100")
        resolution = resolve_fixture(candidate)

        self.assertTrue(capability_allowed(resolution, "start"))
        self.assertFalse(capability_allowed(resolution, "initialDispatch"))
        self.assertFalse(capability_allowed(resolution, "inboundAutomation"))
        self.assertFalse(capability_allowed(resolution, "unknownCapability"))
        self.assertFalse(capability_allowed(None, "start"))
        self.assertFalse(capability_allowed(object(), "start"))
        malformed = CampaignCapabilitiesResolution(
            source_path="campaignCapabilities/synthetic-user-a",
            schema_version=2,
            revision=7,
            decisions=None,
        )
        self.assertFalse(capability_allowed(malformed, "start"))


if __name__ == "__main__":
    unittest.main()
