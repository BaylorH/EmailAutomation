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
from email_automation.certification import evidence as ev
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


# ---------------------------------------------------------------------------
# Task 8 - evidence projections
# ---------------------------------------------------------------------------


class EvidenceProjectionTests(unittest.TestCase):
    """Evidence is durable, exported, and read by people not entitled to the
    fixture. So the guarantee has to be structural, not diligent.

    Task 7G established why: a static scan flagged 75 candidate log sites and
    the real leaks were only found by driving the lane. Evidence cannot rely on
    that kind of after-the-fact discovery, so it is built from an ALLOW-LIST of
    safe field kinds and refuses anything else on sight.
    """

    VALID = dict(
        run_id="cert-20260818-0001",
        scenario_id="campaign-one-property",
        revision="1a20ba44a46e0aeed7620a6408856c0aacf6c7d9",
        outcome="pass",
        phase="execute",
    )

    def test_a_minimal_record_projects_and_round_trips(self):
        record = ev.project_evidence(**self.VALID)
        payload = record.to_dict()
        self.assertEqual(payload["runId"], self.VALID["run_id"])
        self.assertEqual(payload["outcome"], "pass")
        self.assertNotIn("failureCode", payload)

    def test_the_record_is_immutable(self):
        record = ev.project_evidence(**self.VALID)
        with self.assertRaises(Exception):
            record.outcome = "fail"  # type: ignore[misc]

    def test_its_digest_changes_when_any_field_changes(self):
        base = ev.project_evidence(**self.VALID).canonical_digest()
        for override in (
            {"outcome": "fail"},
            {"phase": "cleanup"},
            {"run_id": "cert-20260818-0002"},
            {"counts": {"sent": 1}},
        ):
            with self.subTest(override=override):
                other = ev.project_evidence(**{**self.VALID, **override})
                self.assertNotEqual(base, other.canonical_digest())

    # -- the refusals that make the guarantee structural -------------------

    def test_an_address_is_refused_wherever_it_is_offered(self):
        for field_name, payload in (
            ("summary", {"summary": "could not reach broker@example.com"}),
            ("run_id", {"run_id": "broker@example.com"}),
            ("scenario_id", {"scenario_id": "scenario-broker@example.com"}),
        ):
            with self.subTest(field=field_name):
                with self.assertRaises(ev.EvidenceProjectionError) as ctx:
                    ev.project_evidence(**{**self.VALID, **payload})
                self.assertIn("e-mail address", str(ctx.exception))

    def test_a_provider_token_is_refused(self):
        with self.assertRaises(ev.EvidenceProjectionError):
            ev.project_evidence(**{**self.VALID, "summary": "Bearer abc.def.ghijklmnop"})

    def test_a_firestore_path_is_refused(self):
        with self.assertRaises(ev.EvidenceProjectionError):
            ev.project_evidence(
                **{**self.VALID, "summary": "write refused at users/uid-1/outbox"}
            )

    def test_a_long_opaque_blob_is_refused(self):
        """Base64 image bytes are the shape most likely to arrive by accident."""
        with self.assertRaises(ev.EvidenceProjectionError):
            ev.project_evidence(**{**self.VALID, "summary": "iVBORw0KGgo" + "A" * 70})

    def test_raw_exception_text_cannot_masquerade_as_a_failure_code(self):
        """A code is a lookup key; a description is a disclosure channel."""
        for bad in (
            "Traceback (most recent call last): KeyError",
            "could not send to the broker",
            "Timeout after 30s",
            "",
        ):
            with self.subTest(value=bad):
                with self.assertRaises(ev.EvidenceProjectionError):
                    ev.project_evidence(**{**self.VALID, "failure_code": bad})

    def test_a_bounded_summary_is_still_checked_for_shape_first(self):
        """Truncation is not redaction.

        A 200-character clip of a customer's message is still 200 characters of
        a customer's message, so the shape checks run before the length bound.
        """
        short_but_unsafe = "failed for broker@example.com"
        self.assertLess(len(short_but_unsafe), ev.MAX_SUMMARY_LENGTH)
        with self.assertRaises(ev.EvidenceProjectionError):
            ev.project_evidence(**{**self.VALID, "summary": short_but_unsafe})

    def test_an_oversized_summary_is_refused_rather_than_truncated(self):
        with self.assertRaises(ev.EvidenceProjectionError):
            ev.project_evidence(**{**self.VALID, "summary": "a" * 400})

    def test_counts_must_be_plain_non_negative_integers(self):
        for bad in ({"sent": -1}, {"sent": "1"}, {"sent": True}, {"sent": 1.5}):
            with self.subTest(counts=bad):
                with self.assertRaises(ev.EvidenceProjectionError):
                    ev.project_evidence(**{**self.VALID, "counts": bad})

    def test_a_digest_field_must_actually_be_a_digest(self):
        for bad in ("not-a-digest", "ABC123", "a" * 63, ""):
            with self.subTest(value=bad):
                with self.assertRaises(ev.EvidenceProjectionError):
                    ev.project_evidence(**{**self.VALID, "digests": {"body": bad}})

    def test_a_phase_outside_the_enumeration_is_refused(self):
        with self.assertRaises(ev.EvidenceProjectionError):
            ev.project_evidence(**{**self.VALID, "phase": "sending-the-email"})

    def test_an_unknown_outcome_is_refused(self):
        with self.assertRaises(ev.EvidenceProjectionError):
            ev.project_evidence(**{**self.VALID, "outcome": "probably-fine"})

    def test_there_is_no_arbitrary_passthrough(self):
        """The moment evidence accepts arbitrary keys, the allow-list is over."""
        with self.assertRaises(TypeError):
            ev.project_evidence(**self.VALID, raw_body="Hi Pat, the rent is...")

    # -- the sanctioned way to reference a fixture value -------------------

    def test_a_fixture_value_may_be_referenced_only_by_digest(self):
        body = "Hi Pat, could you share the asking rent?"
        record = ev.project_evidence(
            **self.VALID, digests={"body": ev.digest_of_text(body)}
        )
        self.assertNotIn(body, str(record.to_dict()))
        self.assertEqual(len(record.to_dict()["digests"]["body"]), 64)

    def test_the_same_logical_value_digests_identically_regardless_of_key_order(self):
        """Canonical bytes, so evidence can be diffed across runs and languages."""
        self.assertEqual(
            ev.digest_of({"a": 1, "b": [2, 3]}),
            ev.digest_of({"b": [2, 3], "a": 1}),
        )

    def test_different_values_digest_differently(self):
        self.assertNotEqual(ev.digest_of_text("one"), ev.digest_of_text("two"))

    # -- instrument-blocked is not failure ---------------------------------

    def test_instrument_blocked_is_distinct_from_failure(self):
        """Collapsing them would make 'never exercised' look like 'broken'.

        Only the second should ever block a release, so the distinction has to
        survive into the exported record.
        """
        record = ev.instrument_blocked(
            run_id=self.VALID["run_id"],
            scenario_id=self.VALID["scenario_id"],
            revision=self.VALID["revision"],
            phase="execute",
        )
        payload = record.to_dict()
        self.assertEqual(payload["outcome"], "instrument_blocked")
        self.assertEqual(payload["failureCode"], "user_runtime_launch_required")
        self.assertNotEqual(payload["outcome"], "fail")

    def test_every_projected_record_survives_the_log_canary_rules(self):
        """Cross-check against Task 7G: evidence must not leak what logs must not."""
        record = ev.project_evidence(
            **self.VALID,
            counts={"sent": 1, "captured": 1},
            digests={"body": ev.digest_of_text("Hi Pat")},
        )
        rendered = json.dumps(record.to_dict())
        for forbidden in (FIXTURE_RECIPIENT, FIXTURE_SHEET, FIXTURE_UID, "Hi Pat"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, rendered)


class CertificationRunnerTests(unittest.TestCase):
    """Task 9: seed -> execute -> readback -> replay -> cleanup, as one machine.

    The bootstrap scenario already executed and reconciled. What it could not do
    was finish: a run that captures the right effects but leaves the fixture
    behind has not proved zero-residue, and a run that never re-executes has not
    proved convergence. Those two gaps are exactly why the first run returned
    INSTRUMENT_BLOCKED rather than a verdict.
    """

    REVISION = VALID_REVISION

    def _run(self, run_id):
        from email_automation.certification import runner as rn
        return rn.run_scenario(BOOTSTRAP_SCENARIO_ID, run_id=run_id, revision=self.REVISION)

    def test_replay_produces_zero_additional_effect(self):
        """A converged run re-executed must do nothing the second time."""
        _record, detail = self._run("cert-runner-replay-0001")
        self.assertEqual(detail["observed"]["replay_delta"], 0)
        self.assertTrue(detail["replay_ran"], "replay phase never executed")

    def test_cleanup_leaves_zero_fixture_residue(self):
        _record, detail = self._run("cert-runner-cleanup-0001")
        self.assertEqual(detail["observed"]["cleanup_residue"], 0)
        self.assertEqual(detail["cleanup_residue_paths"], [])

    def test_cleanup_never_deletes_the_global_policy_document(self):
        """Teardown is a DELETE. An over-broad one is a production effect.

        The campaign-authority document lives outside the fixture prefix and is
        readable-but-never-writable. A cleanup that walked it would be deleting
        real global policy while reporting a clean certification run.
        """
        from email_automation.certification import fixtures as fx
        _record, detail = self._run("cert-runner-policy-0001")
        self.assertIn(fx.CAMPAIGN_AUTHORITY_PATH, detail["surviving_global_paths"])

    def test_a_finished_run_reaches_a_real_verdict_not_instrument_blocked(self):
        record, detail = self._run("cert-runner-verdict-0001")
        self.assertEqual(detail["unmeasured"], [])
        self.assertEqual(record.outcome, "pass", detail.get("mismatches"))

    def test_cleanup_is_allocated_before_the_fixture_is_opened(self):
        """Ordering, not politeness: a fixture opened before cleanup exists can
        leak on any fault between the two."""
        _record, detail = self._run("cert-runner-order-0001")
        self.assertLess(detail["cleanup_allocated_seq"], detail["fixture_opened_seq"])


class RunAuthorizationDigestTests(unittest.TestCase):
    """The one-use authorization a run is bound to, and its digest.

    The digest is what makes an authorization non-transferable. If a stored
    record could be edited and still verify, a run could be re-pointed at a
    different scenario, revision, image, or fixture secret after review -- and
    the stamp would then certify something nobody approved.

    So the rule is narrow and absolute: the digest is RECOMPUTED from the stored
    scalars before every transition and compared. The stored digest is evidence
    to check, never a value to trust.
    """

    VALID = {
        "scenario_id": "campaign-one-property",
        "run_id": "cert-auth-0001",
        "source_revision": "1a20ba44a46e0aeed7620a6408856c0aacf6c7d9",
        "image_digest": "sha256:" + "b" * 64,
        "certification_service": "process-user-certification",
        "certification_revision": "process-user-certification-00001-abc",
        "production_candidate_revision": "process-user-00042-xyz",
        "caller_identity_digest": "c" * 64,
        "fixture_config_secret_version": "7",
        "fixture_config_digest": "d" * 64,
        "scenario_registry_digest": "e" * 64,
        "launch_class": "agent_safe",
        "input_producer_kind": "backend_registry_v1",
        "canonical_input_digest": "f" * 64,
        "input_producer_artifact_digest": "0" * 64,
        "authorization_expires_at": "2026-08-19T00:00:00Z",
    }

    def _auth(self, **overrides):
        from email_automation.certification import models as m
        return m.RunAuthorization.create(**{**self.VALID, **overrides})

    # -- the digest itself -------------------------------------------------

    def test_digest_is_lowercase_sha256_hex(self):
        self.assertRegex(self._auth().authorization_digest, r"^[0-9a-f]{64}$")

    def test_digest_recomputes_to_the_same_value(self):
        auth = self._auth()
        self.assertEqual(auth.compute_digest(), auth.authorization_digest)
        auth.verify()

    def test_the_digest_never_includes_itself(self):
        """A self-including preimage cannot be computed, only faked."""
        from email_automation.certification import models as m
        self.assertNotIn("authorization_digest", self._auth().digest_preimage())
        self.assertEqual(len(m.AUTHORIZATION_FIELDS), 16)

    def test_mutating_any_single_field_changes_the_digest(self):
        baseline = self._auth().authorization_digest
        for field_name in self.VALID:
            with self.subTest(field=field_name):
                # Two fields are closed sets, so "append a character" would
                # produce a refusal rather than a different digest.
                if field_name == "authorization_expires_at":
                    mutated = "2026-08-19T00:00:01Z"
                elif field_name == "input_producer_kind":
                    mutated = "frontend_functions_adapter_v1"
                else:
                    mutated = self.VALID[field_name] + "x"
                self.assertNotEqual(
                    self._auth(**{field_name: mutated}).authorization_digest,
                    baseline,
                    f"{field_name} does not enter the digest",
                )

    def test_a_tampered_stored_digest_is_refused(self):
        from email_automation.certification import models as m
        from dataclasses import replace
        tampered = replace(self._auth(), authorization_digest="a" * 64)
        with self.assertRaises(m.AuthorizationInvalid):
            tampered.verify()

    def test_a_stored_record_is_revalidated_not_trusted(self):
        """from_stored recomputes. A record whose digest was edited to match an
        edited field must still fail, because the recomputation disagrees."""
        from email_automation.certification import models as m
        auth = self._auth()
        stored = auth.to_stored()
        stored["scenario_id"] = "some-other-scenario"
        with self.assertRaises(m.AuthorizationInvalid):
            m.RunAuthorization.from_stored(stored)

    def test_a_consistently_reforged_record_still_fails_on_request_mismatch(self):
        """Recomputation alone is not enough: an attacker who edits a field AND
        recomputes the digest produces a self-consistent record. It is the
        binding to the actual request that refuses it."""
        from email_automation.certification import models as m
        forged = self._auth(scenario_id="some-other-scenario").to_stored()
        reloaded = m.RunAuthorization.from_stored(forged)   # self-consistent
        request = m.CertificationRequest(
            scenario_id="campaign-one-property",
            run_id="cert-auth-0001",
            expected_revision=self.VALID["source_revision"],
        )
        with self.assertRaises(m.AuthorizationInvalid):
            reloaded.assert_matches_request(request)

    # -- expiry encoding ---------------------------------------------------

    def test_expiry_must_be_exact_rfc3339_utc_without_fractional_seconds(self):
        from email_automation.certification import models as m
        for bad in ("2026-08-19T00:00:00.000Z",     # fractional seconds
                    "2026-08-19T00:00:00+00:00",    # offset alias for Z
                    "2026-08-19T00:00:00-04:00",    # non-UTC offset
                    "2026-08-19T00:00:00",          # no zone at all
                    "2026-08-19 00:00:00Z",         # space instead of T
                    "2026-08-19t00:00:00z"):        # lowercase designators
            with self.subTest(expiry=bad), self.assertRaises(m.AuthorizationInvalid):
                self._auth(authorization_expires_at=bad)

    # -- field hygiene -----------------------------------------------------

    def test_missing_blank_or_padded_fields_are_refused(self):
        from email_automation.certification import models as m
        for value in ("", "   ", " campaign-one-property", "campaign-one-property "):
            with self.subTest(value=repr(value)), self.assertRaises(m.AuthorizationInvalid):
                self._auth(scenario_id=value)
        incomplete = dict(self.VALID)
        incomplete.pop("launch_class")
        with self.assertRaises(m.AuthorizationInvalid) as caught:
            m.RunAuthorization.create(**incomplete)
        self.assertIn("launch_class", str(caught.exception))
        with self.assertRaises(m.AuthorizationInvalid):
            m.RunAuthorization.create(**{**self.VALID, "surprise": "x"})

    def test_non_string_scalars_are_refused(self):
        from email_automation.certification import models as m
        for value in (7, None, True, ["7"], {"v": "7"}):
            with self.subTest(value=repr(value)), self.assertRaises(m.AuthorizationInvalid):
                self._auth(fixture_config_secret_version=value)

    def test_only_the_two_approved_producer_kinds_are_allowed(self):
        from email_automation.certification import models as m
        for kind in ("backend_registry_v1", "frontend_functions_adapter_v1"):
            with self.subTest(kind=kind):
                self._auth(input_producer_kind=kind)
        with self.assertRaises(m.AuthorizationInvalid):
            self._auth(input_producer_kind="anything_else_v1")

    # -- a fixed vector, so the algorithm itself is pinned ------------------

    def test_fixed_digest_vector(self):
        """Pins the preimage construction, not just its self-consistency.

        A digest test that only compares recomputation to itself agrees with any
        algorithm, including a wrong one.
        """
        self.assertEqual(
            self._auth().authorization_digest,
            _EXPECTED_AUTHORIZATION_DIGEST,
        )


# Filled from the implementation, then mutation-checked: changing any single
# input field must move this value.
_EXPECTED_AUTHORIZATION_DIGEST = (
    "3833ab44ec5eb14b3b19154081aa0a4e4921efb3066fde16ea0751e37208beda"
)


class CertificationRunLedgerTests(unittest.TestCase):
    """The permanent sanitized ledger: PREPARING → PREPARED → CLAIMED → … → terminal.

    This is the only thing standing between "a run happened" and "a run happened
    exactly once, under an authorization somebody approved". Everything else in
    the program can be retried; the ledger is what makes retrying safe.
    """

    AUTH = RunAuthorizationDigestTests.VALID

    def _ledger(self):
        from email_automation.certification import ledger as lg
        return lg.InMemoryRunLedger()

    def _request(self, run_id="cert-auth-0001", scenario_id="campaign-one-property"):
        from email_automation.certification import models as m
        return m.CertificationRequest(
            scenario_id=scenario_id, run_id=run_id,
            expected_revision=self.AUTH["source_revision"])

    def _auth(self, **overrides):
        from email_automation.certification import models as m
        return m.RunAuthorization.create(**{**self.AUTH, **overrides})

    # -- monotonic lifecycle ------------------------------------------------

    def test_the_happy_path_walks_the_whole_machine(self):
        ledger, request = self._ledger(), self._request()
        ledger.begin_preparing(request)
        self.assertEqual(ledger.state(request.run_id), "PREPARING")
        ledger.mark_prepared(request, self._auth())
        self.assertEqual(ledger.state(request.run_id), "PREPARED")
        ledger.claim(request, self._auth())
        self.assertEqual(ledger.state(request.run_id), "CLAIMED")
        for phase in ("fixture_open", "seed", "execute", "replay", "cleanup"):
            ledger.mark_running(request.run_id, phase)
        self.assertTrue(ledger.record_terminal(request.run_id, "PASS", "d" * 64))
        self.assertEqual(ledger.state(request.run_id), "TERMINAL")

    def test_state_never_moves_backwards(self):
        from email_automation.certification import ledger as lg
        ledger, request = self._ledger(), self._request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self._auth())
        ledger.claim(request, self._auth())
        with self.assertRaises(lg.LedgerStateError):
            ledger.mark_prepared(request, self._auth())
        with self.assertRaises(lg.LedgerStateError):
            ledger.begin_preparing(request)

    def test_a_run_cannot_be_claimed_before_it_is_prepared(self):
        from email_automation.certification import ledger as lg
        ledger, request = self._ledger(), self._request()
        ledger.begin_preparing(request)
        with self.assertRaises(lg.LedgerStateError):
            ledger.claim(request, self._auth())

    # -- single use ---------------------------------------------------------

    def test_a_run_id_is_never_reusable(self):
        from email_automation.certification import ledger as lg
        ledger, request = self._ledger(), self._request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self._auth())
        ledger.claim(request, self._auth())
        ledger.record_terminal(request.run_id, "PASS", "d" * 64)
        with self.assertRaises(lg.LedgerStateError):
            ledger.begin_preparing(self._request())

    def test_claiming_twice_is_refused(self):
        from email_automation.certification import ledger as lg
        ledger, request = self._ledger(), self._request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self._auth())
        ledger.claim(request, self._auth())
        with self.assertRaises(lg.LedgerStateError):
            ledger.claim(request, self._auth())

    def test_claiming_consumes_the_one_use_records(self):
        """The authorization and sealed input are ephemeral. If they outlived
        the claim there would be a window where a second caller could use
        them."""
        ledger, request = self._ledger(), self._request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self._auth())
        self.assertIsNotNone(ledger.peek_ephemeral(request.run_id))
        ledger.claim(request, self._auth())
        self.assertIsNone(ledger.peek_ephemeral(request.run_id))

    # -- the authorization is revalidated at the boundary -------------------

    def test_a_claim_whose_authorization_mismatches_the_request_is_refused(self):
        """The authorization grants ONE scenario. A request for another is
        refused even though the authorization is internally valid."""
        from email_automation.certification import models as m
        ledger = self._ledger()
        request = self._request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self._auth())
        with self.assertRaises(m.AuthorizationInvalid):
            ledger.claim(request, self._auth(scenario_id="some-other-scenario"))

    def test_a_wholesale_substituted_pair_is_caught_by_the_prepared_binding(self):
        """Swapping BOTH request and authorization keeps them consistent with
        each other, so the request binding cannot see it. What refuses it is
        that the run was PREPARED under a different authorization -- two
        independent checks, and this case needs the second one."""
        from email_automation.certification import ledger as lg
        ledger = self._ledger()
        request = self._request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self._auth())
        with self.assertRaises(lg.LedgerStateError):
            ledger.claim(self._request(scenario_id="some-other-scenario"),
                         self._auth(scenario_id="some-other-scenario"))

    def test_a_claim_against_a_different_authorization_than_prepared_is_refused(self):
        from email_automation.certification import ledger as lg
        ledger, request = self._ledger(), self._request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self._auth())
        with self.assertRaises(lg.LedgerStateError):
            ledger.claim(request, self._auth(fixture_config_secret_version="8"))

    # -- terminal recording -------------------------------------------------

    def test_terminal_recording_is_idempotent_and_never_rewrites_the_verdict(self):
        ledger, request = self._ledger(), self._request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self._auth())
        ledger.claim(request, self._auth())
        self.assertTrue(ledger.record_terminal(request.run_id, "FAIL", "d" * 64))
        # A repeat is accepted (a retried write must converge) but changes
        # nothing -- and an attempt to record a DIFFERENT verdict is refused.
        self.assertFalse(ledger.record_terminal(request.run_id, "FAIL", "d" * 64))
        from email_automation.certification import ledger as lg
        with self.assertRaises(lg.LedgerStateError):
            ledger.record_terminal(request.run_id, "PASS", "d" * 64)
        self.assertEqual(ledger.verdict(request.run_id), "FAIL")

    def test_cleanup_evidence_appends_without_changing_the_verdict(self):
        ledger, request = self._ledger(), self._request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self._auth())
        ledger.claim(request, self._auth())
        ledger.record_terminal(request.run_id, "FAIL", "d" * 64)
        self.assertTrue(ledger.append_cleanup_result(request.run_id, "e" * 64, {"residue": 0}))
        self.assertEqual(ledger.verdict(request.run_id), "FAIL")

    def test_cleanup_evidence_is_refused_before_the_run_is_terminal(self):
        from email_automation.certification import ledger as lg
        ledger, request = self._ledger(), self._request()
        ledger.begin_preparing(request)
        with self.assertRaises(lg.LedgerStateError):
            ledger.append_cleanup_result(request.run_id, "e" * 64, {"residue": 0})

    # -- the ledger stays sanitized ----------------------------------------

    def test_the_ledger_holds_no_fixture_value_or_raw_text(self):
        """Durable state is digests, states, phases and counts. A recipient or
        a message body in here would outlive the fixture it belongs to."""
        ledger, request = self._ledger(), self._request()
        ledger.begin_preparing(request)
        ledger.mark_prepared(request, self._auth())
        ledger.claim(request, self._auth())
        ledger.record_terminal(request.run_id, "PASS", "d" * 64)
        blob = json.dumps(ledger.export(), sort_keys=True)
        for forbidden in ("@", "broker", "Hi Pat", "100 Fixture Way"):
            self.assertNotIn(forbidden, blob, f"{forbidden!r} reached the ledger")

    def test_an_unknown_run_has_no_state_rather_than_a_guessed_one(self):
        self.assertIsNone(self._ledger().state("never-seen"))


class CertificationLifecycleTests(unittest.TestCase):
    """prepare → run → status, driven through the ledger, without Flask.

    The lifecycle is the part a route is only a thin shell around. Keeping it
    testable without a request context is what lets the hostile cases -- reused
    run ids, a run claimed twice, a missing deployment identity -- be exercised
    directly rather than through HTTP status codes that flatten them.
    """

    REVISION = "1a20ba44a46e0aeed7620a6408856c0aacf6c7d9"

    def _env(self):
        return {
            "SITESIFT_SOURCE_REVISION": self.REVISION,
            "SITESIFT_IMAGE_DIGEST": "sha256:" + "b" * 64,
            "K_SERVICE": "process-user-certification",
            "K_REVISION": "process-user-certification-00001-abc",
            "SITESIFT_PRODUCTION_CANDIDATE_REVISION": "process-user-00042-xyz",
            "SITESIFT_FIXTURE_CONFIG_SECRET_VERSION": "7",
            "SITESIFT_FIXTURE_CONFIG_DIGEST": "d" * 64,
            "SITESIFT_CALLER_IDENTITY_DIGEST": "c" * 64,
        }

    def _fresh(self):
        from email_automation.certification import ledger as lg
        return lg.InMemoryRunLedger()

    def _prepare(self, ledger, run_id="cert-life-0001",
                 scenario_id=BOOTSTRAP_SCENARIO_ID, env=None, revision=None):
        from email_automation.certification import lifecycle as lc
        return lc.prepare(
            {"scenarioId": scenario_id, "runId": run_id,
             "expectedRevision": revision or self.REVISION},
            ledger=ledger, environ=env if env is not None else self._env())

    def _run(self, ledger, run_id="cert-life-0001", env=None):
        from email_automation.certification import lifecycle as lc
        return lc.run(
            {"runId": run_id, "expectedRevision": self.REVISION},
            ledger=ledger, environ=env if env is not None else self._env())

    # -- happy path ---------------------------------------------------------

    def test_prepare_moves_the_ledger_to_prepared_and_returns_safe_digests(self):
        ledger = self._fresh()
        payload, code = self._prepare(ledger)
        self.assertEqual(code, 200)
        self.assertEqual(payload["state"], "PREPARED")
        self.assertEqual(ledger.state("cert-life-0001"), "PREPARED")
        self.assertRegex(payload["authorizationDigest"], r"^[0-9a-f]{64}$")

    def test_run_claims_executes_and_terminalizes(self):
        ledger = self._fresh()
        self._prepare(ledger)
        payload, code = self._run(ledger)
        self.assertEqual(code, 200)
        self.assertEqual(payload["verdict"], "PASS")
        self.assertEqual(ledger.state("cert-life-0001"), "TERMINAL")
        self.assertEqual(ledger.verdict("cert-life-0001"), "PASS")

    def test_status_reports_state_without_changing_it(self):
        from email_automation.certification import lifecycle as lc
        ledger = self._fresh()
        self._prepare(ledger)
        payload, code = lc.status({"runId": "cert-life-0001",
                                   "expectedRevision": self.REVISION},
                                  ledger=ledger, environ=self._env())
        self.assertEqual(code, 200)
        self.assertEqual(payload["state"], "PREPARED")
        self.assertEqual(ledger.state("cert-life-0001"), "PREPARED")

    # -- single use ---------------------------------------------------------

    def test_a_run_id_cannot_be_prepared_twice(self):
        ledger = self._fresh()
        self._prepare(ledger)
        _payload, code = self._prepare(ledger)
        self.assertEqual(code, 409)

    def test_a_run_cannot_be_executed_twice(self):
        ledger = self._fresh()
        self._prepare(ledger)
        self._run(ledger)
        _payload, code = self._run(ledger)
        self.assertEqual(code, 409)

    def test_run_without_prepare_is_refused_and_never_executes(self):
        ledger = self._fresh()
        payload, code = self._run(ledger)
        self.assertEqual(code, 409)
        self.assertIsNone(ledger.state("cert-life-0001"))
        self.assertNotIn("verdict", payload)

    # -- fail closed on identity --------------------------------------------

    def test_every_deployment_identity_field_is_required(self):
        """Absent identity is instrument_unavailable, never a default.

        A missing candidate revision or fixture-secret version silently defaulted
        would produce an authorization -- and therefore a stamp -- bound to
        something nobody deployed.
        """
        for key in ("K_REVISION", "SITESIFT_PRODUCTION_CANDIDATE_REVISION",
                    "SITESIFT_FIXTURE_CONFIG_SECRET_VERSION",
                    "SITESIFT_FIXTURE_CONFIG_DIGEST",
                    "SITESIFT_CALLER_IDENTITY_DIGEST"):
            with self.subTest(missing=key):
                env = self._env()
                env.pop(key)
                _payload, code = self._prepare(self._fresh(), env=env)
                self.assertEqual(code, 503, f"{key} was defaulted instead of required")

    def test_an_unknown_scenario_is_refused_before_the_ledger_is_touched(self):
        ledger = self._fresh()
        _payload, code = self._prepare(ledger, scenario_id="not-a-real-scenario")
        self.assertEqual(code, 404)
        self.assertIsNone(ledger.state("cert-life-0001"))

    # -- the authorization actually binds -----------------------------------

    def test_the_prepared_authorization_binds_the_registry_digest(self):
        """The scenario set is part of what a stamp certifies. If the registry
        changed, the stamp is about a different set of scenarios."""
        from email_automation.certification import scenarios
        ledger = self._fresh()
        self._prepare(ledger)
        stored = ledger.peek_ephemeral("cert-life-0001")
        self.assertEqual(stored.scenario_registry_digest, scenarios.registry_digest())

    def test_claiming_consumes_the_authorization_so_a_replay_finds_nothing(self):
        ledger = self._fresh()
        self._prepare(ledger)
        self.assertIsNotNone(ledger.peek_ephemeral("cert-life-0001"))
        self._run(ledger)
        self.assertIsNone(ledger.peek_ephemeral("cert-life-0001"))

    def test_running_under_a_different_scenario_than_prepared_is_refused(self):
        """The run body names a scenario, and it must be the prepared one.

        If run() defaulted to the scenario on file, the caller's scenarioId
        would be decorative and the binding check would compare a value to
        itself.
        """
        from email_automation.certification import lifecycle as lc
        ledger = self._fresh()
        self._prepare(ledger)
        payload, code = lc.run(
            {"scenarioId": "certification-transport-refutation",
             "runId": "cert-life-0001", "expectedRevision": self.REVISION},
            ledger=ledger, environ=self._env())
        self.assertEqual(code, 409)
        self.assertNotIn("verdict", payload)
        self.assertEqual(ledger.state("cert-life-0001"), "PREPARED")

    # -- responses stay sanitized -------------------------------------------

    def test_no_response_carries_a_fixture_value(self):
        from email_automation.certification import lifecycle as lc
        ledger = self._fresh()
        payloads = [self._prepare(ledger)[0], self._run(ledger)[0],
                    lc.status({"runId": "cert-life-0001",
                               "expectedRevision": self.REVISION},
                              ledger=ledger, environ=self._env())[0]]
        blob = json.dumps(payloads, sort_keys=True, default=str)
        for forbidden in ("@", "broker", "Hi Pat", "100 Fixture Way", "cert-uid-0001"):
            self.assertNotIn(forbidden, blob, f"{forbidden!r} reached a route response")
