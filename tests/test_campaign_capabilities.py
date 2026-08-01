import json
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from types import MappingProxyType

from google.api_core.datetime_helpers import DatetimeWithNanoseconds

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


class _BrokenTzinfoDatetime(datetime):
    @property
    def tzinfo(self):
        raise RuntimeError("synthetic tzinfo failure")


class _BrokenOffsetDatetime(datetime):
    def utcoffset(self):
        raise RuntimeError("synthetic UTC offset failure")


class _LyingDatetime(datetime):
    @property
    def tzinfo(self):
        return object()

    def utcoffset(self):
        return object()


class _AwareDatetimeSubclass(datetime):
    pass


class _InvalidOffsetTypeTimezone(tzinfo):
    def utcoffset(self, _value):
        return object()


class _OutOfRangeTimezone(tzinfo):
    def utcoffset(self, _value):
        return timedelta(hours=24)


class _HostileDict(dict):
    def get(self, _key, _default=None):
        raise RuntimeError("synthetic mapping getter failure")


class _HostileString(str):
    def strip(self, _chars=None):
        raise RuntimeError("synthetic string normalization failure")


class _HostileEquality:
    def __eq__(self, _other):
        raise RuntimeError("synthetic equality failure")


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

    def test_resolver_rejects_padded_bom_nel_and_path_separated_uids(self):
        for case_id in (
            "uid-ascii-padded",
            "uid-bom-wrapped",
            "uid-nel-wrapped",
            "uid-path-separator",
        ):
            candidate = fixture(case_id)
            with self.subTest(case=case_id):
                self.assertEqual(
                    candidate["expected"], as_contract(resolve_fixture(candidate))
                )

    def test_shared_boundary_whitespace_rejects_uid_edges_and_blank_actors(self):
        valid = materialize(fixture("combination-111")["input"])

        for encoded in CONTRACT["boundaryWhitespaceCodePoints"]:
            whitespace = chr(int(encoded, 16))
            for uid in (f"{whitespace}{valid['uid']}", f"{valid['uid']}{whitespace}"):
                with self.subTest(kind="uid", code_point=encoded, uid=repr(uid)):
                    resolution = resolve_campaign_capabilities(
                        uid=uid,
                        document_id=valid["documentId"],
                        data=valid["data"],
                    )
                    self.assertEqual("campaignCapabilities/", resolution.source_path)
                    self.assertEqual(
                        "capability_uid_invalid",
                        resolution.decisions["start"].reason_code,
                    )

            with self.subTest(kind="actor", code_point=encoded):
                resolution = resolve_campaign_capabilities(
                    uid=valid["uid"],
                    document_id=valid["documentId"],
                    data={**valid["data"], "updatedBy": whitespace},
                )
                self.assertEqual(
                    "capability_audit_invalid",
                    resolution.decisions["start"].reason_code,
                )

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

    def test_reader_rejects_every_invalid_uid_before_database_access(self):
        firestore_client = _NoReadFirestore()
        for case_id in (
            "uid-blank",
            "uid-null",
            "uid-ascii-padded",
            "uid-bom-wrapped",
            "uid-nel-wrapped",
            "uid-path-separator",
        ):
            candidate = fixture(case_id)
            with self.subTest(case=case_id):
                resolved = read_campaign_capabilities(
                    firestore_client=firestore_client,
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

    def test_resolution_decision_mapping_and_values_are_deeply_immutable(self):
        resolution = resolve_fixture(fixture("combination-111"))

        self.assertIs(type(resolution.decisions), type(MappingProxyType({})))
        with self.assertRaises(TypeError):
            resolution.decisions["start"] = CapabilityDecision(
                False, "capability_disabled"
            )
        with self.assertRaises(TypeError):
            del resolution.decisions["start"]
        with self.assertRaises(FrozenInstanceError):
            resolution.decisions = {}
        with self.assertRaises(FrozenInstanceError):
            resolution.decisions["start"].allowed = False
        self.assertTrue(resolution.decisions["start"].allowed)

    def test_capability_allowed_rejects_manually_constructed_resolutions(self):
        produced = resolve_fixture(fixture("combination-100"))
        forged = CampaignCapabilitiesResolution(
            source_path=produced.source_path,
            schema_version=produced.schema_version,
            revision=produced.revision,
            decisions=MappingProxyType(
                {
                    "start": CapabilityDecision(True, "allowed"),
                    "initialDispatch": CapabilityDecision(
                        False, "capability_disabled"
                    ),
                    "inboundAutomation": CapabilityDecision(
                        False, "capability_disabled"
                    ),
                }
            ),
        )

        self.assertTrue(capability_allowed(produced, "start"))
        self.assertFalse(capability_allowed(forged, "start"))
        self.assertFalse(capability_allowed(produced, _HostileString("start")))
        self.assertFalse(capability_allowed(produced, _HostileEquality()))

    def test_oversized_revision_denies_without_raising(self):
        candidate = fixture("combination-111")
        test_input = materialize(candidate["input"])
        test_input["data"]["revision"] = 10**10000

        resolution = resolve_campaign_capabilities(
            uid=test_input["uid"],
            document_id=test_input["documentId"],
            data=test_input["data"],
        )

        self.assertIsNone(resolution.revision)
        for decision in resolution.decisions.values():
            self.assertFalse(decision.allowed)
            self.assertEqual("capability_revision_invalid", decision.reason_code)

    def test_pathological_datetime_values_deny_without_raising(self):
        candidate = fixture("combination-111")
        test_input = materialize(candidate["input"])

        for timestamp in (
            _BrokenTzinfoDatetime(2026, 7, 31),
            _BrokenOffsetDatetime(2026, 7, 31, tzinfo=timezone.utc),
        ):
            with self.subTest(timestamp_type=type(timestamp).__name__):
                data = {**test_input["data"], "updatedAt": timestamp}
                resolution = resolve_campaign_capabilities(
                    uid=test_input["uid"],
                    document_id=test_input["documentId"],
                    data=data,
                )
                for decision in resolution.decisions.values():
                    self.assertFalse(decision.allowed)
                    self.assertEqual("capability_audit_invalid", decision.reason_code)

    def test_deceptive_datetime_subclass_cannot_forge_audit_metadata(self):
        candidate = fixture("combination-111")
        test_input = materialize(candidate["input"])
        data = {
            **test_input["data"],
            "updatedAt": _LyingDatetime(2026, 7, 31),
        }

        resolution = resolve_campaign_capabilities(
            uid=test_input["uid"],
            document_id=test_input["documentId"],
            data=data,
        )

        for decision in resolution.decisions.values():
            self.assertFalse(decision.allowed)
            self.assertEqual("capability_audit_invalid", decision.reason_code)

    def test_genuine_aware_datetime_subclasses_remain_supported(self):
        candidate = fixture("combination-111")
        test_input = materialize(candidate["input"])
        timestamps = (
            _AwareDatetimeSubclass(2026, 7, 31, tzinfo=timezone.utc),
            DatetimeWithNanoseconds(2026, 7, 31, tzinfo=timezone.utc),
        )

        for timestamp in timestamps:
            with self.subTest(timestamp_type=type(timestamp).__name__):
                resolution = resolve_campaign_capabilities(
                    uid=test_input["uid"],
                    document_id=test_input["documentId"],
                    data={**test_input["data"], "updatedAt": timestamp},
                )
                self.assertEqual(candidate["expected"], as_contract(resolution))

    def test_invalid_intrinsic_datetime_offsets_deny_without_raising(self):
        candidate = fixture("combination-111")
        test_input = materialize(candidate["input"])
        timestamps = (
            datetime(2026, 7, 31, tzinfo=_InvalidOffsetTypeTimezone()),
            datetime(2026, 7, 31, tzinfo=_OutOfRangeTimezone()),
        )

        for timestamp in timestamps:
            intrinsic_timezone = datetime.tzinfo.__get__(timestamp)
            with self.subTest(timezone_type=type(intrinsic_timezone).__name__):
                resolution = resolve_campaign_capabilities(
                    uid=test_input["uid"],
                    document_id=test_input["documentId"],
                    data={**test_input["data"], "updatedAt": timestamp},
                )
                for decision in resolution.decisions.values():
                    self.assertFalse(decision.allowed)
                    self.assertEqual(
                        "capability_audit_invalid", decision.reason_code
                    )

    def test_pathological_mapping_and_uid_subclasses_deny_without_raising(self):
        candidate = fixture("combination-111")
        test_input = materialize(candidate["input"])

        malformed_document = resolve_campaign_capabilities(
            uid=test_input["uid"],
            document_id=test_input["documentId"],
            data=_HostileDict(test_input["data"]),
        )
        malformed_uid = resolve_campaign_capabilities(
            uid=_HostileString(test_input["uid"]),
            document_id=test_input["documentId"],
            data=test_input["data"],
        )

        self.assertEqual(
            "capability_document_malformed",
            malformed_document.decisions["start"].reason_code,
        )
        self.assertEqual(
            "capability_uid_invalid", malformed_uid.decisions["start"].reason_code
        )


if __name__ == "__main__":
    unittest.main()
