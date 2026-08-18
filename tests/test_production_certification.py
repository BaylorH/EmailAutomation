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


# ---------------------------------------------------------------------------
# Task 5A - first vertical slice through scoped data clients
# ---------------------------------------------------------------------------

import os
import re
import io
import logging
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import replace
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import automation_runtime as ar  # noqa: E402
from email_automation.column_config import get_default_column_config  # noqa: E402
from email_automation import campaign_safety as campaign_safety_module  # noqa: E402
from email_automation import clients as clients_module  # noqa: E402
from email_automation import email as email_module  # noqa: E402
from email_automation import followup as followup_module  # noqa: E402
from email_automation import messaging as messaging_module  # noqa: E402
from email_automation import notifications as notifications_module  # noqa: E402
from email_automation import processing as processing_module  # noqa: E402
from email_automation import sheets as sheets_module  # noqa: E402


FIXTURE_UID = "cert-uid-0001"
FIXTURE_CLIENT = "cert-client-0001"
FIXTURE_SHEET = "cert-sheet-0001"
FIXTURE_PREFIX = f"users/{FIXTURE_UID}"
FIXTURE_RECIPIENT = "broker@fixture.example.com"
FIXTURE_ROW = 7


class AmbientReached(AssertionError):
    """The ambient production client was touched during a scoped run."""


class ExplodingClient:
    """Stands in for every ambient production client.

    ANY attribute access is a failure. That is the whole experiment: if the slice
    still reaches a module-level ``_fs``, a freshly constructed provider client,
    or an OAuth-backed Sheets service, this object turns the silent fallback into
    a loud one.
    """

    def __init__(self, label):
        self._label = label

    def __getattr__(self, name):
        raise AmbientReached(f"ambient {self._label} was reached: .{name}")

    def __call__(self, *args, **kwargs):
        raise AmbientReached(f"ambient {self._label} was constructed")


# --- the fixture store -----------------------------------------------------
#
# Path-keyed rather than shape-specific, because the slice walks arbitrary
# chains and the point of the exercise is that the fence - not the double -
# is what constrains where a write may land.


class FixtureSnapshot:
    def __init__(self, store, path, data, exists=True):
        self._store = store
        self._path = path
        self.id = path.rsplit("/", 1)[-1]
        self._data = dict(data)
        self.exists = exists

    def to_dict(self):
        return dict(self._data)

    @property
    def reference(self):
        return FixtureDocument(self._store, self._path)


class FixtureDocument:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    @property
    def id(self):
        return self._path.rsplit("/", 1)[-1]

    def collection(self, name):
        return FixtureCollection(self._store, f"{self._path}/{name}")

    def get(self, transaction=None):
        exists = self._path in self._store.data
        self._store.reads.append(self._path)
        return FixtureSnapshot(
            self._store, self._path, self._store.data.get(self._path, {}), exists=exists
        )

    def set(self, data, merge=False):
        self._store.writes.append(("set", self._path, dict(data), merge))
        current = dict(self._store.data.get(self._path, {})) if merge else {}
        current.update(data)
        self._store.data[self._path] = current

    def update(self, data):
        self._store.writes.append(("update", self._path, dict(data), None))
        current = dict(self._store.data.get(self._path, {}))
        current.update(data)
        self._store.data[self._path] = current

    def create(self, data):
        self._store.writes.append(("create", self._path, dict(data), None))
        self._store.data[self._path] = dict(data)

    def delete(self):
        self._store.writes.append(("delete", self._path, None, None))
        self._store.data.pop(self._path, None)


class FixtureCollection:
    def __init__(self, store, path, filters=()):
        self._store = store
        self._path = path
        self._filters = tuple(filters)

    def document(self, name):
        return FixtureDocument(self._store, f"{self._path}/{name}")

    def where(self, field=None, op=None, value=None, **kwargs):
        return FixtureCollection(self._store, self._path, self._filters + ((field, op, value),))

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def add(self, data):
        index = self._store.generated
        self._store.generated += 1
        path = f"{self._path}/generated-{index}"
        self._store.writes.append(("add", path, dict(data), None))
        self._store.data[path] = dict(data)
        return FixtureDocument(self._store, path)

    def _matches(self, data):
        for field, op, value in self._filters:
            actual = data.get(field)
            if op == "array_contains":
                if not isinstance(actual, (list, tuple)) or value not in actual:
                    return False
            elif actual != value:
                return False
        return True

    def stream(self):
        self._store.reads.append(self._path)
        depth = self._path.count("/") + 1
        for path, data in sorted(self._store.data.items()):
            if path.startswith(self._path + "/") and path.count("/") == depth:
                if self._matches(data):
                    yield FixtureSnapshot(self._store, path, data)

    def get(self):
        return list(self.stream())


class FixtureTransaction:
    """Applies immediately. Atomicity is not what this slice is proving."""

    def __init__(self, store):
        self._store = store
        self._max_attempts = 1
        self._read_only = False
        self._id = b"fixture"

    def _clean_up(self):
        return None

    def _begin(self, retry_id=None):
        return None

    def _commit(self):
        return []

    def _rollback(self):
        return None

    def set(self, ref, data, merge=False):
        ref.set(data, merge=merge)

    def update(self, ref, data):
        ref.update(data)

    def create(self, ref, data):
        ref.create(data)

    def delete(self, ref):
        ref.delete()


class FixtureBatch(FixtureTransaction):
    def commit(self):
        return []


class FixtureFirestore:
    def __init__(self):
        self.data = {}
        self.writes = []
        self.reads = []
        self.generated = 0

    def collection(self, name):
        return FixtureCollection(self, name)

    def transaction(self, **kwargs):
        return FixtureTransaction(self)

    def batch(self):
        return FixtureBatch(self)


class FixtureSheetRequest:
    def __init__(self, provider, label, kwargs, payload):
        self._provider = provider
        self._label = label
        self._kwargs = kwargs
        self._payload = payload

    def execute(self):
        self._provider.calls.append((self._label, self._kwargs))
        return self._payload


class FixtureSheetValues:
    def __init__(self, provider):
        self._provider = provider

    def get(self, **kwargs):
        range_name = kwargs.get("range") or ""
        return FixtureSheetRequest(
            self._provider, "values.get", kwargs,
            {"values": [self._provider.row_for(range_name)]},
        )

    def update(self, **kwargs):
        return FixtureSheetRequest(self._provider, "values.update", kwargs, {})

    def batchUpdate(self, **kwargs):  # noqa: N802 - Google API name
        return FixtureSheetRequest(self._provider, "values.batchUpdate", kwargs, {})


class FixtureSpreadsheets:
    def __init__(self, provider):
        self._provider = provider

    def values(self):
        return FixtureSheetValues(self._provider)

    def get(self, **kwargs):
        return FixtureSheetRequest(
            self._provider, "spreadsheets.get", kwargs,
            {"sheets": [{"properties": {"title": "Sheet1", "sheetId": 0}}]},
        )

    def batchUpdate(self, **kwargs):  # noqa: N802 - Google API name
        return FixtureSheetRequest(self._provider, "spreadsheets.batchUpdate", kwargs, {})


class FixtureSheets:
    def __init__(self, header, row):
        self.calls = []
        self._header = header
        self._row = row

    def row_for(self, range_name):
        return self._row if range_name.endswith(f"{FIXTURE_ROW}:{FIXTURE_ROW}") else self._header

    def spreadsheets(self):
        return FixtureSpreadsheets(self)


class FirstSliceClientIsolationTests(unittest.TestCase):
    """One-property outreach, driven end to end through scoped clients only.

    Every ambient production client is booby-trapped, so a single unthreaded call
    site anywhere in the reached graph fails the run rather than quietly writing
    to production. The Graph boundary itself is NOT extracted here - that is Task
    6 - so it is patched with the established deterministic fake and asserted on;
    nothing in this test makes a network request.
    """

    HEADER = ["Property Address", "Email", "Name"]

    def _row(self):
        return ["100 Fixture Way", FIXTURE_RECIPIENT, "Pat Fixture"]

    def _seed(self):
        store = FixtureFirestore()
        store.data[FIXTURE_PREFIX] = {
            "email": "sender@fixture.invalid",
            "signatureMode": "none",
        }
        store.data["systemConfig/campaignAccess"] = {
            "automationEnabled": True,
            "allowedUids": [FIXTURE_UID],
        }
        store.data[f"{FIXTURE_PREFIX}/clients/{FIXTURE_CLIENT}"] = {
            "sheetId": FIXTURE_SHEET,
            "status": "live",
            "columnConfig": get_default_column_config(),
        }
        store.data[
            f"{FIXTURE_PREFIX}/clients/{FIXTURE_CLIENT}/notifications/notif-1"
        ] = {"kind": "sheet_update"}
        store.data[f"{FIXTURE_PREFIX}/outbox/outbox-1"] = {
            "assignedEmails": [FIXTURE_RECIPIENT],
            "script": "Hi Pat, could you share the asking rent for 100 Fixture Way?",
            "scriptSelectionMode": "exact",
            "clientId": FIXTURE_CLIENT,
            "subject": "100 Fixture Way",
            "rowNumber": FIXTURE_ROW,
            "source": "dashboard_new_campaign",
            "actionType": "campaign_launch",
            "contactName": "Pat",
            "actionAuditId": "audit-1",
            "notificationId": "notif-1",
            "notificationClientId": FIXTURE_CLIENT,
            "deleteNotificationOnSend": True,
            "followUpConfig": {
                "enabled": True,
                "followUps": [
                    {"waitTime": 3, "waitUnit": "days",
                     "message": "Following up on 100 Fixture Way."}
                ],
            },
            "createdAt": "2026-08-17T00:00:00Z",
        }
        return store

    def _runtime(self, store, sheets):
        return ar.certification_runtime(
            run_id="cert-run-5a",
            scope="campaign-one-property",
            firestore=store,
            sheets=sheets,
            firestore_prefix=FIXTURE_PREFIX,
            sheet_ids=(FIXTURE_SHEET,),
            # campaign authority is a genuinely global decision; readable, never writable
            readable_paths=("systemConfig/campaignAccess",),
        )

    def _drive_run(self, lane="capture"):
        """Drive the slice with every ambient client booby-trapped."""
        from tests.test_full_campaign_e2e import FakeGraph

        store = self._seed()
        sheets = FixtureSheets(self.HEADER, self._row())
        runtime = self._runtime(store, sheets)
        if lane == "graph":
            # Same fenced data clients, but the ORDINARY production delivery
            # boundary. This is what makes the parity claim meaningful: only the
            # transport differs between the two lanes.
            runtime = replace(runtime, outbound=None)
        graph = FakeGraph()

        exploding_modules = (
            (clients_module, "_fs"),
            (messaging_module, "_fs"),
            (processing_module, "_fs"),
            (followup_module, "_fs"),
            (notifications_module, "_fs"),
        )
        with ExitStack() as stack:
            for module, attribute in exploding_modules:
                stack.enter_context(
                    patch.object(module, attribute, ExplodingClient(f"{module.__name__}.{attribute}"))
                )
            # a fresh provider client is just as much an escape as the global one
            stack.enter_context(
                patch("google.cloud.firestore.Client", ExplodingClient("firestore.Client"))
            )
            for module in (clients_module, sheets_module):
                stack.enter_context(
                    patch.object(module, "_sheets_client", ExplodingClient("_sheets_client"))
                )
            stack.enter_context(patch.object(email_module, "requests", graph))
            stack.enter_context(patch.object(email_module.time, "sleep", return_value=None))
            states = email_module.send_outboxes(
                FIXTURE_UID,
                {"Authorization": "Bearer fixture"},
                runtime=runtime,
            )
        return {
            "store": store,
            "sheets": sheets,
            "runtime": runtime,
            "graph": graph,
            "states": states,
        }

    # -- the run itself ---------------------------------------------------

    def test_the_slice_completes_without_touching_any_ambient_client(self):
        """The certification lane reaches delivery and captures, sending nothing."""
        result = self._drive_run()
        captured = result["runtime"].outbound.captured
        self.assertEqual([draft.to for draft in captured], [(FIXTURE_RECIPIENT,)])
        self.assertEqual(result["graph"].sent_recipients(), [])
        self.assertEqual(result["graph"].sent_draft_ids, [])

    def test_the_production_lane_of_the_same_fixture_really_sends(self):
        """Guards against a capture lane that passes because nothing runs."""
        result = self._drive_run(lane="graph")
        self.assertEqual(result["graph"].sent_recipients(), [FIXTURE_RECIPIENT])
        self.assertEqual(len(result["graph"].sent_draft_ids), 1)

    def test_no_effect_escaped_the_fixture_scope(self):
        result = self._drive_run()
        self.assertEqual(result["runtime"].effect_scope.violations, [])
        for _kind, path, _payload, _merge in result["store"].writes:
            self.assertTrue(
                path.startswith(FIXTURE_PREFIX),
                f"write escaped the fixture prefix: {path}",
            )

    def test_ordinary_thread_message_and_index_writes_all_landed(self):
        writes = {path for _kind, path, _payload, _merge in self._drive_run()["store"].writes}
        self.assertTrue(any(p.startswith(f"{FIXTURE_PREFIX}/threads/") for p in writes))
        self.assertTrue(any("/messages/" in p for p in writes))
        self.assertTrue(any(f"{FIXTURE_PREFIX}/msgIndex/" in p for p in writes))
        self.assertTrue(any(f"{FIXTURE_PREFIX}/convIndex/" in p for p in writes))

    def test_action_audit_and_outbox_terminalization_landed(self):
        writes = self._drive_run()["store"].writes
        audit = [w for w in writes if w[1] == f"{FIXTURE_PREFIX}/actionAudit/audit-1"]
        self.assertTrue(audit, "no action-audit write")
        self.assertEqual(audit[-1][2].get("status"), "sent")
        self.assertIn(
            ("delete", f"{FIXTURE_PREFIX}/outbox/outbox-1", None, None), writes
        )

    def test_campaign_authority_and_optout_reads_went_through_the_fence(self):
        """Both gates ran, and both ran against the fixture store.

        These two reads are the ones a certification run most needs to be real:
        skipping authority would let a stopped client send, and skipping the
        opt-out check would mail someone who asked not to be mailed. Proving the
        run reached them - rather than fail-closing before them - is the point.
        """
        reads = self._drive_run()["store"].reads
        self.assertIn(f"{FIXTURE_PREFIX}/clients/{FIXTURE_CLIENT}", reads)
        self.assertIn(f"{FIXTURE_PREFIX}/archivedClients/{FIXTURE_CLIENT}", reads)
        self.assertIn("systemConfig/campaignAccess", reads)
        self.assertTrue(
            any(r.startswith(f"{FIXTURE_PREFIX}/optedOutContacts/") for r in reads),
            f"the opt-out gate never read the fixture store: {reads}",
        )

    def test_the_global_policy_document_is_read_but_never_written(self):
        """The one path outside the fixture subtree the run may touch at all."""
        result = self._drive_run()
        self.assertIn("systemConfig/campaignAccess", result["store"].reads)
        self.assertEqual(
            [w for w in result["store"].writes if not w[1].startswith(FIXTURE_PREFIX)],
            [],
        )

    def test_row_highlight_reached_the_fixture_sheet_only(self):
        sheets = self._drive_run()["sheets"]
        highlights = [
            kwargs for label, kwargs in sheets.calls if label == "spreadsheets.batchUpdate"
        ]
        self.assertTrue(highlights, "row highlight never happened")
        for kwargs in highlights:
            self.assertEqual(kwargs.get("spreadsheetId"), FIXTURE_SHEET)

    def test_followup_was_scheduled_through_the_scoped_client(self):
        writes = self._drive_run()["store"].writes
        self.assertTrue(
            any("followUp" in str(payload) or "followup" in path.lower()
                for _kind, path, payload, _merge in writes if payload),
            "no follow-up scheduling write",
        )

    def test_notification_deletion_did_not_fall_back_to_the_ambient_client(self):
        writes = self._drive_run()["store"].writes
        self.assertIn(
            ("delete",
             f"{FIXTURE_PREFIX}/clients/{FIXTURE_CLIENT}/notifications/notif-1",
             None, None),
            writes,
        )

    def test_the_slice_makes_zero_drive_calls(self):
        result = self._drive_run()
        self.assertIsInstance(result["runtime"].drive, ar.DenyingDriveClient)
        self.assertEqual(result["runtime"].drive_publication.real_permission_calls, 0)
        self.assertEqual(result["runtime"].drive_publication.captured, [])

    def test_certification_never_touches_production_send_counters(self):
        """Production's ``sendCounters`` live outside any fixture prefix.

        Reading them would leak a real user's allowance into a fixture run and
        writing them would consume it, so the isolated CounterStore is the only
        legitimate store here - and the fixture store must show no counter path.
        """
        result = self._drive_run()
        touched = [
            path for path in
            [w[1] for w in result["store"].writes] + result["store"].reads
            if "sendCounter" in path
        ]
        self.assertEqual(touched, [])

    # -- state parity: a captured receipt must behave like a Graph receipt ----

    @staticmethod
    def _normalized_writes(store):
        """Write shapes with message-derived identifiers collapsed.

        The two lanes necessarily produce different message ids - that is the
        one thing that legitimately differs - so comparing raw paths would
        always fail and prove nothing. Everything else must match exactly.
        """
        shapes = []
        for kind, path, _payload, _merge in store.writes:
            segments = []
            for segment in path.split("/"):
                if "@" in segment or segment.startswith("captured-") or segment.startswith("conv-"):
                    segments.append("<id>")
                elif len(segment) > 24 and segment.isalnum():
                    segments.append("<id>")
                else:
                    segments.append(segment)
            shapes.append((kind, "/".join(segments)))
        return shapes

    def test_a_captured_receipt_drives_the_same_writes_as_a_graph_receipt(self):
        """Task 6's core claim.

        If capture drove even slightly different state, every downstream
        assertion in a certification run would be describing a code path the
        product never takes in production.
        """
        capture_lane = self._drive_run(lane="capture")
        graph_lane = self._drive_run(lane="graph")
        self.assertEqual(
            self._normalized_writes(capture_lane["store"]),
            self._normalized_writes(graph_lane["store"]),
        )

    def test_both_lanes_terminalize_the_outbox_and_audit_identically(self):
        for lane in ("capture", "graph"):
            with self.subTest(lane=lane):
                writes = self._drive_run(lane=lane)["store"].writes
                audit = [w for w in writes if w[1] == f"{FIXTURE_PREFIX}/actionAudit/audit-1"]
                self.assertEqual(audit[-1][2].get("status"), "sent")
                self.assertIn(
                    ("delete", f"{FIXTURE_PREFIX}/outbox/outbox-1", None, None), writes
                )

    def test_both_lanes_reach_the_row_highlight(self):
        for lane in ("capture", "graph"):
            with self.subTest(lane=lane):
                sheets = self._drive_run(lane=lane)["sheets"]
                self.assertTrue(
                    [k for label, k in sheets.calls if label == "spreadsheets.batchUpdate"]
                )

    def test_two_concurrent_runtimes_share_no_store(self):
        first = self._drive_run()
        second = self._drive_run()
        self.assertIsNot(first["store"], second["store"])
        self.assertIsNot(first["runtime"].counters, second["runtime"].counters)
        self.assertIsNot(first["runtime"].effect_scope, second["runtime"].effect_scope)


# ---------------------------------------------------------------------------
# Task 7G - no fixture value reaches a durable log
# ---------------------------------------------------------------------------


class CertificationLogCanaryTests(unittest.TestCase):
    """Drive the real slice with canaries and read what the logs actually said.

    A static scan for "risky" interpolations flagged 75 sites here, most of them
    harmless and none of them proof. Driving the lane and reading the captured
    output found FIVE real leaks, all of the same value - the recipient identity
    - and three of the five were only reachable on FAILURE paths. The happy path
    alone would have reported clean, which is why the probe covers guard
    rejections and dead-lettering too.
    """

    CANARIES = {
        "body": "CANARYBODYaaa111",
        "name": "CANARYNAMEccc333",
    }

    def _capture(self, mutate=None):
        """Run the certification slice and return everything it logged."""
        original_seed = FirstSliceClientIsolationTests._seed

        def seeded(inner_self):
            store = original_seed(inner_self)
            item = store.data[f"{FIXTURE_PREFIX}/outbox/outbox-1"]
            item["script"] = (
                f"Hi {self.CANARIES['name']}, could you share the asking rent "
                f"for {self.CANARIES['body']}?"
            )
            item["contactName"] = self.CANARIES["name"]
            if mutate:
                mutate(store)
            return store

        FirstSliceClientIsolationTests._seed = seeded
        try:
            case = FirstSliceClientIsolationTests(
                "test_the_slice_completes_without_touching_any_ambient_client"
            )
            stream = io.StringIO()
            log_stream = io.StringIO()
            handler = logging.StreamHandler(log_stream)
            logging.getLogger().addHandler(handler)
            try:
                with redirect_stdout(stream), redirect_stderr(stream):
                    try:
                        case._drive_run()
                    except Exception:  # a guard path may abort; its LOGS still count
                        pass
            finally:
                logging.getLogger().removeHandler(handler)
            return stream.getvalue() + log_stream.getvalue()
        finally:
            FirstSliceClientIsolationTests._seed = original_seed

    # the paths that actually reach a log statement
    def _row_mismatch(self, store):
        store.data[f"{FIXTURE_PREFIX}/outbox/outbox-1"]["rowNumber"] = 999

    def _unresolved_placeholder(self, store):
        item = store.data[f"{FIXTURE_PREFIX}/outbox/outbox-1"]
        item["script"] = f"Hi [NAME], about {self.CANARIES['body']}?"
        item.pop("contactName", None)

    def _unsafe_body(self, store):
        store.data[f"{FIXTURE_PREFIX}/outbox/outbox-1"]["script"] = (
            f"Hi {self.CANARIES['name']}, I guarantee {self.CANARIES['body']} "
            "and will sign the lease myself."
        )

    def _paths(self):
        return {
            "happy": None,
            "row mismatch": self._row_mismatch,
            "unresolved placeholder": self._unresolved_placeholder,
            "unsafe body": self._unsafe_body,
        }

    def test_no_recipient_identity_reaches_a_durable_log(self):
        """Logs are aggregated and outlive the run, the cleanup, and the fixture."""
        for label, mutate in self._paths().items():
            with self.subTest(path=label):
                captured = self._capture(mutate)
                self.assertNotIn(
                    FIXTURE_RECIPIENT,
                    captured,
                    f"the recipient identity reached the log on the {label} path",
                )

    def test_no_message_body_or_contact_name_reaches_a_durable_log(self):
        for label, mutate in self._paths().items():
            for canary, token in self.CANARIES.items():
                with self.subTest(path=label, canary=canary):
                    self.assertNotIn(token, self._capture(mutate))

    def test_no_fixture_resource_identity_reaches_a_durable_log(self):
        captured = self._capture()
        for label, token in (("sheet", FIXTURE_SHEET), ("user", FIXTURE_UID)):
            with self.subTest(resource=label):
                self.assertNotIn(token, captured)

    def test_the_run_still_says_something_useful(self):
        """Sanitizing must not silence the lane - operational meaning survives."""
        captured = self._capture()
        self.assertIn("Created draft", captured)
        self.assertIn("Sent and indexed email", captured)
        self.assertIn("subject-", captured, "no stable subject digest was emitted")

    def test_the_same_subject_correlates_across_lines_within_one_run(self):
        """A digest that did not correlate would be useless to an operator."""
        captured = self._capture()
        digests = set(re.findall(r"subject-[0-9a-f]{12}", captured))
        self.assertEqual(
            len(digests), 1, f"one subject should yield one digest, got {digests}"
        )

    def test_production_logging_is_deliberately_unchanged(self):
        """The seam must not have quietly redacted real operations.

        Redacting production logs would trade a certification property for an
        operational regression: operators debug live campaigns by grepping for a
        real address.
        """
        self.assertEqual(ar.log_identity(None, "broker@example.com"), "broker@example.com")
        self.assertEqual(
            ar.log_identity(ar.production_runtime(), "broker@example.com"),
            "broker@example.com",
        )
        self.assertEqual(
            ar.log_reason(None, "queued=broker@example.com mismatched"),
            "queued=broker@example.com mismatched",
        )

    def test_a_certification_runtime_digests_rather_than_drops(self):
        runtime = ar.certification_runtime(run_id="cert-log", scope="logs")
        masked = ar.log_identity(runtime, "broker@example.com")
        self.assertTrue(masked.startswith("subject-"))
        self.assertNotIn("broker", masked)
        self.assertEqual(masked, ar.log_identity(runtime, "BROKER@example.com"))

        other = ar.certification_runtime(run_id="cert-log-2", scope="logs")
        self.assertNotEqual(masked, ar.log_identity(other, "broker@example.com"))

    def test_reason_scrubbing_keeps_the_operational_sentence(self):
        runtime = ar.certification_runtime(run_id="cert-log", scope="logs")
        scrubbed = ar.log_reason(
            runtime, "Queued recipient does not match sheet row 7; queued=broker@example.com"
        )
        self.assertIn("does not match sheet row 7", scrubbed)
        self.assertNotIn("broker@example.com", scrubbed)
        self.assertIn("subject-", scrubbed)
