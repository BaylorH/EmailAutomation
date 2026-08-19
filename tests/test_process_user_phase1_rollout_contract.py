import ast
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "phase1_rollout.py"
SPEC = importlib.util.spec_from_file_location("phase1_rollout", MODULE_PATH)
phase1_rollout = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = phase1_rollout
SPEC.loader.exec_module(phase1_rollout)


RELEASE_BRANCH = "feat/native-image-attachment-ingestion-20260816"
OLD_REVISION = "process-user-stage-9491133f15d5"
CANDIDATE = "process-user-stage-1234567890ab"
OLD_IMAGE = (
    "us-central1-docker.pkg.dev/email-automation-cache/cloud-run-source-deploy/"
    "process-user@sha256:3415d3775696932dbaba4911560f3bacb544e4e6123b162d012e485e9d123968"
)
CANDIDATE_IMAGE = (
    "us-central1-docker.pkg.dev/email-automation-cache/cloud-run-source-deploy/"
    "process-user@sha256:" + "e" * 64
)
CANDIDATE_DIGEST = CANDIDATE_IMAGE.split("@", 1)[1]
HEAD_SHA = "1234567890abcdef1234567890abcdef12345678"
SERVICE_URL = "https://process-user-example.run.app"
AUX_TAGS = {
    "jill-one": "process-user-jill-one-202608020520",
    "lock": "process-user-lock-0837727b",
    "rollback-door": "process-user-door-294b7599f1",
}


def traffic(positive_revision=OLD_REVISION, release_revision=OLD_REVISION, extra=None):
    rows = [
        {"revisionName": positive_revision, "percent": 100},
        {"revisionName": release_revision, "tag": "release-a"},
    ]
    tags = dict(AUX_TAGS)
    if extra:
        tags.update(extra)
    rows.extend({"revisionName": revision, "tag": tag} for tag, revision in tags.items())
    return rows


def service(positive_revision=OLD_REVISION, release_revision=OLD_REVISION, extra=None):
    rows = traffic(positive_revision, release_revision, extra)
    status_rows = []
    for row in rows:
        row = dict(row)
        if row.get("tag"):
            row["url"] = f"https://{row['tag']}---process-user-example.run.app"
        status_rows.append(row)
    return {
        "metadata": {
            "name": "process-user",
            "annotations": {"run.googleapis.com/maxScale": "20"},
        },
        "spec": {"traffic": rows},
        "status": {
            "url": "https://process-user-example.run.app",
            "latestCreatedRevisionName": CANDIDATE,
            "latestReadyRevisionName": CANDIDATE,
            "traffic": status_rows,
        },
    }


def revision(name, image):
    is_candidate = name == CANDIDATE
    environment = [
        {"name": "FIREBASE_BUCKET", "value": "bucket"},
        {"name": "OPENAI_API_KEY", "valueFrom": {
            "secretKeyRef": {"name": "OPENAI_API_KEY", "key": "latest"}
        }},
    ]
    if is_candidate:
        # The stamp is what binds this revision to one reviewed source commit
        # and one built artifact. It sits before the gate because the gate is
        # addressed positionally by the native-gate cases below.
        environment.append({
            "name": "SITESIFT_IMAGE_DIGEST",
            "value": image.split("@", 1)[1],
        })
        environment.append({
            "name": "SITESIFT_SOURCE_REVISION",
            "value": HEAD_SHA,
        })
        environment.append({
            "name": "SITESIFT_NATIVE_IMAGE_INGESTION",
            "value": "false",
        })
    return {
        "metadata": {
            "name": name,
            "annotations": {
                "autoscaling.knative.dev/maxScale": "10",
                "autoscaling.knative.dev/minScale": "0",
                "run.googleapis.com/operation-id": (
                    "candidate-operation" if is_candidate else "baseline-operation"
                ),
                "run.googleapis.com/startup-cpu-boost": "true",
            },
            "labels": {
                "cloud.googleapis.com/location": "us-central1",
                "serving.knative.dev/configurationGeneration": (
                    "98" if is_candidate else "97"
                ),
                **({} if is_candidate else {"serving.knative.dev/route": "process-user"}),
                "serving.knative.dev/service": "process-user",
            },
        },
        "spec": {
            "serviceAccountName": "248289505828-compute@developer.gserviceaccount.com",
            "containerConcurrency": 1,
            "timeoutSeconds": 540,
            "containers": [{
                "image": image,
                "command": ["gunicorn"],
                "args": [
                    "--bind=:8080", "--workers=1", "--threads=8",
                    "--max-requests=1", "--timeout=0", "service:app",
                ],
                "resources": {"limits": {"memory": "2Gi"}},
                "env": environment,
            }],
        },
        "status": {
            "imageDigest": image,
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


TWIN_SERVICE_NAME = "process-user-certification"
TWIN_REVISION = "process-user-certification-1234567890ab"
TWIN_RUNTIME_SA = (
    "sitesift-certification-runtime@email-automation-cache.iam.gserviceaccount.com"
)
FIXTURE_CONFIG_SECRET = "sitesift-certification-fixture-config"

# The twin-only values the SHIPPED deploy script really sets, read off
# scripts/deploy_certification_twin.sh rather than restated. The derivation
# lives once, in tests/test_certification_mutation_controls.py.
#
# This is the whole point of the fixture below: while it named three twin-only
# fields and the script set eight, `test_exact_twin_stamp_is_accepted` passed
# against a twin nobody could deploy, and the rollout comparator would have
# refused every real one at promotion time.
from tests.test_certification_mutation_controls import (  # noqa: E402
    DEPLOYED_TWIN_ENV as _DEPLOYED_TWIN_ENV,
)

TWIN_OPERATOR_SA = _DEPLOYED_TWIN_ENV["SITESIFT_CERTIFICATION_OPERATOR_EMAIL"]
TWIN_AUDIENCE = _DEPLOYED_TWIN_ENV["SITESIFT_CERTIFICATION_AUDIENCE"]
TWIN_OPERATOR_SUB = _DEPLOYED_TWIN_ENV["SITESIFT_CERTIFICATION_OPERATOR_SUB"]


def twin_service(
    *,
    image=CANDIDATE_IMAGE,
    source_revision=HEAD_SHA,
    name=TWIN_SERVICE_NAME,
    service_account=TWIN_RUNTIME_SA,
    fixture_version="7",
    fixture_version_env=None,
    fixture_secret=FIXTURE_CONFIG_SECRET,
    candidate_revision=CANDIDATE,
    audience=TWIN_AUDIENCE,
    operator_email=TWIN_OPERATOR_SA,
    operator_sub=TWIN_OPERATOR_SUB,
    ingress="internal",
    traffic_revision=TWIN_REVISION,
    extra_env=(),
    drop_env=(),
):
    # The two spellings of the fixture version are ONE fact, so they default
    # together; a case that wants them to disagree has to say so.
    if fixture_version_env is None:
        fixture_version_env = fixture_version
    env = [
        {
            "name": "OPENAI_API_KEY",
            "valueFrom": {"secretKeyRef": {"name": "OPENAI_API_KEY", "key": "latest"}},
        },
        {"name": "SITESIFT_IMAGE_DIGEST", "value": image.split("@", 1)[1]},
        {"name": "SITESIFT_SOURCE_REVISION", "value": source_revision},
        {"name": "SITESIFT_NATIVE_IMAGE_INGESTION", "value": "false"},
        {"name": "K_SERVICE", "value": name},
        {"name": "FIRESTORE_DATABASE", "value": "sitesift-certification"},
        {
            "name": "CERTIFICATION_FIXTURE_CONFIG",
            "valueFrom": {
                "secretKeyRef": {"name": fixture_secret, "key": fixture_version}
            },
        },
        # The five the deploy script also sets, and the rollout comparator did
        # not classify. Each is REQUIRED here and FORBIDDEN on the candidate.
        {
            "name": "SITESIFT_PRODUCTION_CANDIDATE_REVISION",
            "value": candidate_revision,
        },
        {
            "name": "SITESIFT_FIXTURE_CONFIG_SECRET_VERSION",
            "value": fixture_version_env,
        },
        {"name": "SITESIFT_CERTIFICATION_AUDIENCE", "value": audience},
        {"name": "SITESIFT_CERTIFICATION_OPERATOR_EMAIL", "value": operator_email},
        {"name": "SITESIFT_CERTIFICATION_OPERATOR_SUB", "value": operator_sub},
    ]
    env = [entry for entry in env if entry["name"] not in drop_env]
    env.extend(dict(entry) for entry in extra_env)
    traffic = [{"revisionName": traffic_revision, "percent": 100}]
    return {
        "metadata": {
            "name": name,
            "annotations": {"run.googleapis.com/ingress": ingress},
        },
        "spec": {
            "traffic": [dict(row) for row in traffic],
            "template": {
                "spec": {
                    "serviceAccountName": service_account,
                    "containers": [{"image": image, "env": env}],
                }
            },
        },
        "status": {"traffic": [dict(row) for row in traffic]},
    }


def twin_policy(members=None, role="roles/run.invoker"):
    if members is None:
        members = [f"serviceAccount:{TWIN_OPERATOR_SA}"]
    return {"bindings": [{"role": role, "members": list(members)}]}


def queue(state="RUNNING"):
    return {
        "name": "projects/email-automation-cache/locations/us-central1/queues/graph-process-user",
        "state": state,
        "rateLimits": {
            "maxBurstSize": 10,
            "maxConcurrentDispatches": 1,
            "maxDispatchesPerSecond": 1.0,
        },
        "retryConfig": {
            "maxAttempts": 15,
            "maxBackoff": "300s",
            "maxDoublings": 4,
            "minBackoff": "30s",
        },
    }


def lock_document(nonce="a" * 64, head="1" * 40, update_time="2026-08-12T00:00:00Z"):
    return {
        "name": phase1_rollout.LOCK_DOCUMENT_NAME,
        "fields": {
            "schemaVersion": {"integerValue": "1"},
            "service": {"stringValue": "process-user"},
            "headSha": {"stringValue": head},
            "ownerNonce": {"stringValue": nonce},
        },
        "createTime": "2026-08-12T00:00:00Z",
        "updateTime": update_time,
    }


def firestore_error(code, status):
    return json.dumps(
        {"error": {"code": code, "message": "generic", "status": status}}
    ).encode()


class FakeOps:
    def __init__(self):
        self.events = []
        self.service = service()
        self.old_revision = revision(OLD_REVISION, OLD_IMAGE)
        self.candidate_revision = revision(CANDIDATE, CANDIDATE_IMAGE)
        self.queue = queue()
        self.task_snapshots = [[], [], [], [], [], []]
        self.fail_remove = False
        self.fail_promote = False
        self.fail_prerequisites = False
        self.fail_prerequisites_after = None
        self.fail_resume_after_change = False
        self.legacy_health_calls = 0
        self.fail_legacy_health_after = None
        self.fail_legacy_health_on = None
        self.interrupt_legacy_health_on = None
        self.prerequisite_calls = 0
        self.lock_held = False
        self.lock_nonce = None
        self.lock_assertions = 0
        self.lose_lock_on_assert = None
        self.fail_acquire_lock = False
        self.acquire_error = None
        self.post_tag_removal_fault = None
        self.tag_removed = False
        self.post_tag_service_calls = 0
        self.twin_service = twin_service()
        self.twin_policy = twin_policy()

    def _post_tag_fault(self, name):
        return self.tag_removed and self.post_tag_removal_fault == name

    def preflight(self):
        self.events.append("preflight")

    def verify_lock_permissions(self):
        self.events.append("lock:permissions")

    def acquire_lock(self, head_sha, nonce):
        self.events.append("lock:acquire")
        if self.acquire_error is not None:
            raise self.acquire_error
        if self.fail_acquire_lock or self.lock_held:
            raise phase1_rollout.RolloutLockHeld("lock unavailable")
        self.lock_held = True
        self.lock_nonce = nonce
        return phase1_rollout.RolloutLock(
            owner_nonce=nonce,
            head_sha=head_sha,
            update_time="2026-08-12T00:00:00Z",
        )

    def assert_lock(self, lock):
        self.events.append("lock:assert")
        self.lock_assertions += 1
        if self.lose_lock_on_assert == self.lock_assertions:
            self.lock_held = False
        if not self.lock_held or lock.owner_nonce != self.lock_nonce:
            raise phase1_rollout.RolloutLockLost("lock lost")

    def release_lock(self, lock):
        self.events.append("lock:release")
        if not self.lock_held or lock.owner_nonce != self.lock_nonce:
            raise phase1_rollout.RolloutLockLost("lock lost")
        self.lock_held = False

    def verify_rules_ui_switches(self):
        self.prerequisite_calls += 1
        self.events.append("prerequisites")
        if self._post_tag_fault("switches") or self.fail_prerequisites or (
            self.fail_prerequisites_after is not None
            and self.prerequisite_calls > self.fail_prerequisites_after
        ):
            raise phase1_rollout.RolloutError("prerequisite failed")

    def verify_service_access(self, topology):
        self.events.append("service-access")
        if self._post_tag_fault("iam") or topology.service_url != SERVICE_URL:
            raise phase1_rollout.RolloutError("service access rejected")

    def artifact_image(self):
        self.events.append("artifact")
        if self._post_tag_fault("candidate_artifact"):
            raise phase1_rollout.RolloutError("artifact read failed")
        return CANDIDATE_IMAGE

    def get_service(self):
        self.events.append("service")
        if self.tag_removed:
            self.post_tag_service_calls += 1
            if (
                self.post_tag_removal_fault == "topology"
                and self.post_tag_service_calls == 2
            ):
                return service(CANDIDATE, OLD_REVISION)
        return json.loads(json.dumps(self.service))

    def get_revision(self, name):
        self.events.append(f"revision:{name}")
        if self._post_tag_fault("candidate_revision") and name == CANDIDATE:
            raise phase1_rollout.RolloutError("candidate revision read failed")
        if self._post_tag_fault("rollback_revision") and name == OLD_REVISION:
            raise phase1_rollout.RolloutError("rollback revision read failed")
        source = self.candidate_revision if name == CANDIDATE else self.old_revision
        return json.loads(json.dumps(source))

    def get_twin_service(self):
        self.events.append("twin:service")
        if self._post_tag_fault("twin_read"):
            raise phase1_rollout.RolloutError("twin service read failed")
        return json.loads(json.dumps(self.twin_service))

    def get_twin_iam_policy(self):
        self.events.append("twin:iam")
        if self._post_tag_fault("twin_iam_read"):
            raise phase1_rollout.RolloutError("twin IAM read failed")
        return json.loads(json.dumps(self.twin_policy))

    def get_queue(self):
        self.events.append("queue")
        result = json.loads(json.dumps(self.queue))
        if self._post_tag_fault("queue"):
            result["state"] = "RUNNING"
        return result

    def list_tasks(self):
        self.events.append("tasks")
        if self._post_tag_fault("task_read"):
            raise phase1_rollout.RolloutError("task read failed")
        if not self.task_snapshots:
            return []
        return self.task_snapshots.pop(0)

    def pause_queue(self):
        self.events.append("pause")
        self.queue["state"] = "PAUSED"

    def resume_queue(self):
        self.events.append("resume")
        self.queue["state"] = "RUNNING"
        if self.fail_resume_after_change:
            raise phase1_rollout.RolloutError("resume response failed")

    def add_cert_tag(self, tag, candidate):
        self.events.append("tag:add")
        self.service = service(extra={tag: candidate})

    def remove_cert_tag(self, tag):
        self.events.append("tag:remove")
        if self.fail_remove:
            raise phase1_rollout.RolloutError("remove failed")
        self.service = service()
        self.tag_removed = True
        self.post_tag_service_calls = 0
        fault = self.post_tag_removal_fault
        if fault == "candidate_digest":
            wrong = CANDIDATE_IMAGE.rsplit("sha256:", 1)[0] + "sha256:" + "d" * 64
            self.candidate_revision["spec"]["containers"][0]["image"] = wrong
            self.candidate_revision["status"]["imageDigest"] = wrong
        elif fault == "candidate_gate":
            gate = next(
                entry
                for entry in self.candidate_revision["spec"]["containers"][0]["env"]
                if entry.get("name") == "SITESIFT_NATIVE_IMAGE_INGESTION"
            )
            gate["value"] = "true"
        elif fault == "rollback_digest":
            wrong = OLD_IMAGE.rsplit("sha256:", 1)[0] + "sha256:" + "d" * 64
            self.old_revision["spec"]["containers"][0]["image"] = wrong
            self.old_revision["status"]["imageDigest"] = wrong
        elif fault == "twin_image":
            self.twin_service = twin_service(
                image=CANDIDATE_IMAGE.rsplit("sha256:", 1)[0] + "sha256:" + "d" * 64
            )
        elif fault == "twin_service_account":
            self.twin_service = twin_service(
                service_account="248289505828-compute@developer.gserviceaccount.com"
            )
        elif fault == "twin_identity":
            self.twin_service = twin_service(name="process-user")
        elif fault == "twin_stamp":
            self.twin_service = twin_service(source_revision="0" * 40)
        elif fault == "twin_fixture_alias":
            self.twin_service = twin_service(fixture_version="latest")
        elif fault == "twin_forbidden_env":
            self.twin_service = twin_service(
                extra_env=[{"name": "FIREBASE_BUCKET", "value": "bucket"}]
            )
        elif fault == "twin_unclassified_env":
            self.twin_service = twin_service(
                extra_env=[{"name": "UNAPPROVED_MODE", "value": "1"}]
            )
        elif fault == "twin_public_invoker":
            self.twin_policy = twin_policy(members=["allUsers"])
        elif fault == "twin_production_traffic":
            self.twin_service = twin_service(traffic_revision=CANDIDATE)
        elif fault == "twin_read":
            pass
        elif fault == "twin_iam_read":
            pass
        elif fault == "lock_before":
            self.lose_lock_on_assert = self.lock_assertions + 2
        elif fault == "lock_after":
            self.lose_lock_on_assert = self.lock_assertions + 3

    def promote(self, candidate, old):
        self.events.append("promote")
        if self.fail_promote:
            raise phase1_rollout.RolloutError("promote failed")
        self.service = service(candidate, candidate)

    def rollback(self, old, candidate):
        self.events.append("rollback")
        self.service = service()

    def legacy_health_get(self, base_url, audience):
        self.legacy_health_calls += 1
        self.events.append(f"legacy:{base_url}|aud:{audience}")
        if self.interrupt_legacy_health_on == self.legacy_health_calls:
            raise KeyboardInterrupt()
        if self.fail_legacy_health_on == self.legacy_health_calls:
            return {"status": "wrong"}
        if (
            self.fail_legacy_health_after is not None
            and self.legacy_health_calls > self.fail_legacy_health_after
        ):
            return {"status": "wrong"}
        return {"status": "ok"}


class ValidatorTests(unittest.TestCase):
    def test_release_packet_is_bound_to_branch_and_live_rollback(self):
        self.assertEqual(RELEASE_BRANCH, phase1_rollout.BRANCH)
        self.assertEqual(OLD_REVISION, phase1_rollout.OLD_REVISION)
        self.assertEqual(OLD_IMAGE, phase1_rollout.OLD_IMAGE)

    def test_controller_pins_current_promoted_production_baseline(self):
        expected = {
            "branch": RELEASE_BRANCH,
            "old revision": OLD_REVISION,
            "old image": OLD_IMAGE,
            "rules hash": (
                "7acf2bdbe2a7a42221efaa1ae15c2b406e4d6bef6b2c4131b3b0a6b5de8f8ee8"
            ),
            "hosting version": "a3758fb175d427f5",
            "index hash": (
                "33a041852c11a578b5d4836c64e76b7208afbbf20ccac2208d1b2fc10e0182c0"
            ),
            "JavaScript path": "static/js/main.e628d195.js",
            "JavaScript hash": (
                "7858189175c50bed17581c6f206988a6ba5918dbaab636b2ea2673f43de73ea9"
            ),
            "stylesheet path": "static/css/main.aad5f62b.css",
            "stylesheet hash": (
                "43bd2f02d0f3de9ba18fce0c638b94b0e84c9f7a13542f3b3747a90736a54d22"
            ),
            "auxiliary tags": AUX_TAGS,
        }
        observed = {
            "branch": phase1_rollout.BRANCH,
            "old revision": phase1_rollout.OLD_REVISION,
            "old image": phase1_rollout.OLD_IMAGE,
            "rules hash": phase1_rollout.RULES_HASH,
            "hosting version": phase1_rollout.HOSTING_VERSION,
            "index hash": phase1_rollout.INDEX_HASH,
            "JavaScript path": phase1_rollout.JS_PATH,
            "JavaScript hash": phase1_rollout.JS_HASH,
            "stylesheet path": phase1_rollout.CSS_PATH,
            "stylesheet hash": phase1_rollout.CSS_HASH,
            "auxiliary tags": phase1_rollout.AUX_TAGS,
        }
        for field, value in expected.items():
            with self.subTest(field=field):
                self.assertEqual(value, observed[field])

    def test_baseline_topology_accepts_exact_auxiliary_tags(self):
        topology = phase1_rollout.validate_topology(
            service(), expected_positive=OLD_REVISION,
            expected_release=OLD_REVISION, expected_aux=AUX_TAGS,
        )
        self.assertEqual(OLD_REVISION, topology.positive_revision)

    def test_baseline_topology_rejects_latest_or_implicit_targets(self):
        value = service()
        value["spec"]["traffic"][0] = {"latestRevision": True, "percent": 100}
        with self.assertRaises(phase1_rollout.RolloutError):
            phase1_rollout.validate_topology(
                value, expected_positive=OLD_REVISION,
                expected_release=OLD_REVISION, expected_aux=AUX_TAGS,
            )

    def test_baseline_topology_rejects_duplicate_positive_targets(self):
        value = service()
        value["spec"]["traffic"][0]["percent"] = 50
        value["spec"]["traffic"].insert(
            1, {"revisionName": OLD_REVISION, "percent": 50}
        )
        value["status"]["traffic"][0]["percent"] = 50
        value["status"]["traffic"].insert(
            1, {"revisionName": OLD_REVISION, "percent": 50}
        )
        with self.assertRaises(phase1_rollout.RolloutError):
            phase1_rollout.validate_topology(
                value, expected_positive=OLD_REVISION,
                expected_release=OLD_REVISION, expected_aux=AUX_TAGS,
            )

    def test_topology_rejects_wrong_service_identity_or_external_health_urls(self):
        for mutate in (
            lambda value: value["metadata"].update(name="other-service"),
            lambda value: value["status"].update(url="https://evil.example.com"),
            lambda value: value["status"]["traffic"][1].update(
                url="https://evil.example.com"
            ),
            lambda value: value["status"]["traffic"][1].update(
                url="https://release-a---process-user-other.run.app"
            ),
        ):
            with self.subTest(mutate=mutate):
                value = service()
                mutate(value)
                with self.assertRaises(phase1_rollout.RolloutError):
                    phase1_rollout.validate_topology(
                        value, expected_positive=OLD_REVISION,
                        expected_release=OLD_REVISION, expected_aux=AUX_TAGS,
                    )

    def test_queue_contract_is_closed(self):
        phase1_rollout.validate_queue(queue(), "RUNNING")
        for override in (
            {"uriOverride": {"uri": "https://wrong"}},
            {"httpMethod": "POST"},
            {"headerOverrides": [{"header": {"key": "x", "value": "y"}}]},
            {"oauthToken": {"serviceAccountEmail": "wrong@example.test"}},
            {"oidcToken": {"serviceAccountEmail": "wrong@example.test"}},
        ):
            with self.subTest(override=override):
                bad = queue()
                bad["httpTarget"] = override
                with self.assertRaises(phase1_rollout.RolloutError):
                    phase1_rollout.validate_queue(bad, "RUNNING")

    def test_candidate_must_be_ready_and_exact_dark_config_delta(self):
        phase1_rollout.validate_candidate(
            revision(CANDIDATE, CANDIDATE_IMAGE),
            revision(OLD_REVISION, OLD_IMAGE),
            CANDIDATE, CANDIDATE_IMAGE,
            expected_source_revision=HEAD_SHA,
        )
        changed = revision(CANDIDATE, CANDIDATE_IMAGE)
        changed["spec"]["containerConcurrency"] = 2
        with self.assertRaises(phase1_rollout.RolloutError):
            phase1_rollout.validate_candidate(
                changed, revision(OLD_REVISION, OLD_IMAGE),
                CANDIDATE, CANDIDATE_IMAGE,
                expected_source_revision=HEAD_SHA,
            )

    def test_candidate_functional_metadata_must_match_baseline(self):
        baseline = revision(OLD_REVISION, OLD_IMAGE)
        for mutate in (
            lambda value: value["metadata"]["annotations"].update(
                {"run.googleapis.com/startup-cpu-boost": "false"}
            ),
            lambda value: value["metadata"]["annotations"].update(
                {"run.googleapis.com/vpc-access-connector": "other"}
            ),
            lambda value: value["metadata"]["labels"].update(
                {"cloud.googleapis.com/location": "other"}
            ),
        ):
            with self.subTest(mutate=mutate):
                changed = revision(CANDIDATE, CANDIDATE_IMAGE)
                mutate(changed)
                with self.assertRaises(phase1_rollout.RolloutError):
                    phase1_rollout.validate_candidate(
                        changed,
                        baseline,
                        CANDIDATE,
                        CANDIDATE_IMAGE,
                        expected_source_revision=HEAD_SHA,
                    )

    def test_candidate_requires_one_exact_false_native_image_gate(self):
        baseline = revision(OLD_REVISION, OLD_IMAGE)
        phase1_rollout.validate_candidate(
            revision(CANDIDATE, CANDIDATE_IMAGE),
            baseline,
            CANDIDATE,
            CANDIDATE_IMAGE,
            expected_source_revision=HEAD_SHA,
        )

        def candidate_with_gate(gate):
            value = revision(CANDIDATE, CANDIDATE_IMAGE)
            environment = value["spec"]["containers"][0]["env"]
            environment[-1:] = gate
            return value

        invalid_candidates = {
            "missing": candidate_with_gate([]),
            "true": candidate_with_gate([{
                "name": "SITESIFT_NATIVE_IMAGE_INGESTION", "value": "true",
            }]),
            "capitalized": candidate_with_gate([{
                "name": "SITESIFT_NATIVE_IMAGE_INGESTION", "value": "False",
            }]),
            "padded": candidate_with_gate([{
                "name": "SITESIFT_NATIVE_IMAGE_INGESTION", "value": " false ",
            }]),
            "duplicate": candidate_with_gate([
                {"name": "SITESIFT_NATIVE_IMAGE_INGESTION", "value": "false"},
                {"name": "SITESIFT_NATIVE_IMAGE_INGESTION", "value": "false"},
            ]),
            "secret-bound": candidate_with_gate([{
                "name": "SITESIFT_NATIVE_IMAGE_INGESTION",
                "valueFrom": {"secretKeyRef": {"name": "gate", "key": "latest"}},
            }]),
            "extra-keyed": candidate_with_gate([{
                "name": "SITESIFT_NATIVE_IMAGE_INGESTION",
                "value": "false",
                "unexpected": "field",
            }]),
        }
        for label, candidate in invalid_candidates.items():
            with self.subTest(label=label):
                with self.assertRaises(phase1_rollout.RolloutError):
                    phase1_rollout.validate_candidate(
                        candidate,
                        baseline,
                        CANDIDATE,
                        CANDIDATE_IMAGE,
                        expected_source_revision=HEAD_SHA,
                    )

        polluted_baseline = revision(OLD_REVISION, OLD_IMAGE)
        polluted_baseline["spec"]["containers"][0]["env"].append({
            "name": "SITESIFT_NATIVE_IMAGE_INGESTION",
            "value": "false",
        })
        with self.assertRaises(phase1_rollout.RolloutError):
            phase1_rollout.validate_candidate(
                revision(CANDIDATE, CANDIDATE_IMAGE),
                polluted_baseline,
                CANDIDATE,
                CANDIDATE_IMAGE,
                expected_source_revision=HEAD_SHA,
            )


    def test_candidate_release_stamp_must_bind_this_source_and_this_artifact(self):
        baseline = revision(OLD_REVISION, OLD_IMAGE)
        phase1_rollout.validate_candidate(
            revision(CANDIDATE, CANDIDATE_IMAGE),
            baseline,
            CANDIDATE,
            CANDIDATE_IMAGE,
            expected_source_revision=HEAD_SHA,
        )

        def candidate_with_stamp(entries):
            value = revision(CANDIDATE, CANDIDATE_IMAGE)
            environment = value["spec"]["containers"][0]["env"]
            value["spec"]["containers"][0]["env"] = [
                entry
                for entry in environment
                if entry.get("name")
                not in ("SITESIFT_IMAGE_DIGEST", "SITESIFT_SOURCE_REVISION")
            ]
            value["spec"]["containers"][0]["env"][-1:-1] = entries
            return value

        rejected = {
            "missing-both": candidate_with_stamp([]),
            "missing-source": candidate_with_stamp([
                {"name": "SITESIFT_IMAGE_DIGEST", "value": CANDIDATE_DIGEST},
            ]),
            "missing-digest": candidate_with_stamp([
                {"name": "SITESIFT_SOURCE_REVISION", "value": HEAD_SHA},
            ]),
            "forged-source": candidate_with_stamp([
                {"name": "SITESIFT_IMAGE_DIGEST", "value": CANDIDATE_DIGEST},
                {"name": "SITESIFT_SOURCE_REVISION", "value": "0" * 40},
            ]),
            "forged-digest": candidate_with_stamp([
                {"name": "SITESIFT_IMAGE_DIGEST", "value": "sha256:" + "f" * 64},
                {"name": "SITESIFT_SOURCE_REVISION", "value": HEAD_SHA},
            ]),
            "duplicate-source": candidate_with_stamp([
                {"name": "SITESIFT_IMAGE_DIGEST", "value": CANDIDATE_DIGEST},
                {"name": "SITESIFT_SOURCE_REVISION", "value": HEAD_SHA},
                {"name": "SITESIFT_SOURCE_REVISION", "value": HEAD_SHA},
            ]),
            "secret-bound-source": candidate_with_stamp([
                {"name": "SITESIFT_IMAGE_DIGEST", "value": CANDIDATE_DIGEST},
                {
                    "name": "SITESIFT_SOURCE_REVISION",
                    "valueFrom": {
                        "secretKeyRef": {"name": "stamp", "key": "latest"}
                    },
                },
            ]),
            "extra-keyed-source": candidate_with_stamp([
                {"name": "SITESIFT_IMAGE_DIGEST", "value": CANDIDATE_DIGEST},
                {
                    "name": "SITESIFT_SOURCE_REVISION",
                    "value": HEAD_SHA,
                    "unexpected": "field",
                },
            ]),
        }
        for label, candidate in rejected.items():
            with self.subTest(label=label):
                with self.assertRaises(phase1_rollout.RolloutError):
                    phase1_rollout.validate_candidate(
                        candidate,
                        baseline,
                        CANDIDATE,
                        CANDIDATE_IMAGE,
                        expected_source_revision=HEAD_SHA,
                    )

    def test_baseline_carrying_a_release_stamp_is_refused(self):
        polluted = revision(OLD_REVISION, OLD_IMAGE)
        polluted["spec"]["containers"][0]["env"].append(
            {"name": "SITESIFT_SOURCE_REVISION", "value": HEAD_SHA}
        )
        with self.assertRaises(phase1_rollout.RolloutError):
            phase1_rollout.validate_candidate(
                revision(CANDIDATE, CANDIDATE_IMAGE),
                polluted,
                CANDIDATE,
                CANDIDATE_IMAGE,
                expected_source_revision=HEAD_SHA,
            )

    def test_a_stamp_from_another_checkout_is_refused(self):
        with self.assertRaises(phase1_rollout.RolloutError):
            phase1_rollout.validate_candidate(
                revision(CANDIDATE, CANDIDATE_IMAGE),
                revision(OLD_REVISION, OLD_IMAGE),
                CANDIDATE,
                CANDIDATE_IMAGE,
                expected_source_revision="b" * 40,
            )


    def _twin_call(self, **overrides):
        arguments = {
            "candidate_spec": revision(CANDIDATE, CANDIDATE_IMAGE)["spec"],
            "expected_image": CANDIDATE_IMAGE,
            "expected_source_revision": HEAD_SHA,
            "expected_candidate_revision": CANDIDATE,
            "production": phase1_rollout.validate_topology(
                service(),
                expected_positive=OLD_REVISION,
                expected_release=OLD_REVISION,
                expected_aux=AUX_TAGS,
            ),
        }
        arguments.update(overrides)
        return arguments

    def _twin_refusal(self, twin=None, policy=None, **call):
        """The exact sentence one rule emits, not 'something was refused'."""
        with self.assertRaises(phase1_rollout.RolloutError) as caught:
            phase1_rollout.validate_twin_stamp(
                twin_service() if twin is None else twin,
                twin_policy() if policy is None else policy,
                **self._twin_call(**call),
            )
        return str(caught.exception)

    def test_exact_twin_stamp_is_accepted(self):
        """The vacuity guard for every case below, and the one that caught the
        drift: this fixture is the twin scripts/deploy_certification_twin.sh
        really deploys, so a comparator that refuses it refuses every real
        twin -- at promotion time, under the lock."""
        phase1_rollout.validate_twin_stamp(
            twin_service(), twin_policy(), **self._twin_call()
        )

    # -- one classification, not two -----------------------------------------

    def test_the_rollout_and_the_contract_share_one_classification(self):
        """These were two hand-maintained copies of one allowlist and they
        drifted, so the rollout would have refused every deploy-script twin.

        Three assertions, because equality alone is not a binding: two literal
        tuples that happen to agree today satisfy it, and that is precisely the
        state this defect was in. So this pins (1) that the rollout's names are
        the very objects of the module it loaded -- no local copy anywhere in
        scripts/phase1_rollout.py -- (2) that the module it loaded is THIS
        contract file and not some other one, and only then (3) that the values
        agree. Retyping the tuple in the rollout breaks (1); pointing the loader
        somewhere else breaks (2).
        """
        from email_automation.certification import twin_contract

        loaded = phase1_rollout.twin_contract
        self.assertIs(phase1_rollout.TWIN_ONLY_ENV, loaded.TWIN_ONLY)
        self.assertIs(phase1_rollout.TWIN_FORBIDDEN_ENV, loaded.FORBIDDEN_ON_TWIN)

        self.assertEqual(
            Path(loaded.__file__).resolve(), Path(twin_contract.__file__).resolve()
        )

        self.assertEqual(phase1_rollout.TWIN_ONLY_ENV, twin_contract.TWIN_ONLY)
        self.assertEqual(
            phase1_rollout.TWIN_FORBIDDEN_ENV, twin_contract.FORBIDDEN_ON_TWIN
        )

    def test_the_rollout_states_no_second_copy_of_the_classification(self):
        """The binding above is only as good as there being nothing to drift.

        A future edit that pastes the allowlist back into
        scripts/phase1_rollout.py would leave the import in place and every
        other case green -- which is exactly the state this defect was found in.
        So the absence of a second LIST is asserted on the source itself.

        Naming one field to check its value is not a restatement and stays
        legal; collecting two or more of them into a literal sequence is a
        second allowlist, and that is what is refused here.
        """
        classified = set(phase1_rollout.TWIN_ONLY_ENV) | set(
            phase1_rollout.TWIN_FORBIDDEN_ENV
        )
        self.assertGreaterEqual(len(classified), 8)  # vacuity guard

        offenders = []
        for node in ast.walk(ast.parse(MODULE_PATH.read_text())):
            if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                continue
            literals = {
                element.value
                for element in node.elts
                if isinstance(element, ast.Constant)
                and isinstance(element.value, str)
            }
            if len(literals & classified) >= 2:
                offenders.append((node.lineno, sorted(literals & classified)))
        self.assertEqual(
            offenders,
            [],
            "the classification is restated in the rollout instead of imported",
        )

    def test_the_shared_classification_is_not_vacuously_small(self):
        """If the import silently produced an empty tuple, every 'must be
        present' case below would pass while proving nothing."""
        self.assertGreaterEqual(len(phase1_rollout.TWIN_ONLY_ENV), 8)
        self.assertIn(
            phase1_rollout.TWIN_FIXTURE_CONFIG_NAME, phase1_rollout.TWIN_ONLY_ENV
        )
        for name in (
            "SITESIFT_PRODUCTION_CANDIDATE_REVISION",
            "SITESIFT_FIXTURE_CONFIG_SECRET_VERSION",
            "SITESIFT_CERTIFICATION_AUDIENCE",
            "SITESIFT_CERTIFICATION_OPERATOR_EMAIL",
            "SITESIFT_CERTIFICATION_OPERATOR_SUB",
        ):
            with self.subTest(name=name):
                self.assertIn(name, phase1_rollout.TWIN_ONLY_ENV)

    def test_every_classified_field_is_refused_in_both_directions(self):
        """Approved differences are PAIRED, never waved through. Each name is
        required on the twin and forbidden on the candidate."""
        for name in phase1_rollout.TWIN_ONLY_ENV:
            with self.subTest(name=name, direction="missing from twin"):
                self.assertEqual(
                    self._twin_refusal(twin=twin_service(drop_env=(name,))),
                    f"twin is missing required certification field {name}",
                )
            with self.subTest(name=name, direction="present on candidate"):
                polluted = revision(CANDIDATE, CANDIDATE_IMAGE)["spec"]
                polluted["containers"][0]["env"].append(
                    {"name": name, "value": "anything"}
                )
                self.assertEqual(
                    self._twin_refusal(candidate_spec=polluted),
                    f"{name} must not appear on the candidate",
                )

    # -- the five newly classified values, each by its own sentence -----------

    def test_twin_must_name_the_candidate_revision_under_certification(self):
        """A classified name with an unchecked value is a hole with a label on
        it: the twin could name any revision at all and the allowlist would be
        satisfied."""
        self.assertEqual(
            self._twin_refusal(
                twin=twin_service(candidate_revision="process-user-stage-000000000000")
            ),
            "twin does not name the production candidate under certification",
        )

    def test_a_twin_naming_its_own_revision_is_refused_by_its_own_rule(self):
        """A twin that certifies itself certifies nothing. The twin's service
        name has the certification prefix, so this must be caught BEFORE the
        equality rule or it would report the wrong reason."""
        self.assertEqual(
            self._twin_refusal(twin=twin_service(candidate_revision=TWIN_REVISION)),
            "twin production candidate revision names the twin's own service",
        )
        self.assertEqual(
            self._twin_refusal(twin=twin_service(candidate_revision=TWIN_SERVICE_NAME)),
            "twin production candidate revision names the twin's own service",
        )

    def test_the_two_spellings_of_the_fixture_version_must_agree(self):
        """The deploy script sets the env var and the secret reference from ONE
        variable. Disagreement means the run cannot say which fixture it
        executed against -- and the residual comparison cannot see it, because
        both names are classified twin-only."""
        self.assertEqual(
            self._twin_refusal(twin=twin_service(fixture_version_env="8")),
            "twin fixture config version and secret reference disagree",
        )

    def test_the_fixture_version_env_must_be_a_positive_decimal(self):
        for spelling in ("latest", "0", "07", "v7", "", "1.0", "-1"):
            with self.subTest(spelling=spelling):
                self.assertIn(
                    self._twin_refusal(
                        twin=twin_service(fixture_version_env=spelling)
                    ),
                    (
                        "twin fixture config secret version is not a positive decimal",
                        "twin SITESIFT_FIXTURE_CONFIG_SECRET_VERSION is not a "
                        "plain literal value",
                    ),
                )

    def test_the_certification_audience_must_be_the_twins_own_url(self):
        """An audience naming another service is the confused-deputy shape
        audience verification exists to stop: a token minted for the twin would
        be replayable against whatever the audience really named."""
        for wrong in (
            "https://process-user-abcdef-uc.a.run.app",
            "https://process-user-certification.example.com",
            "http://process-user-certification-abcdef-uc.a.run.app",
            "https://process-user-certification-abcdef-uc.a.run.app/extra",
            "process-user-certification-abcdef-uc.a.run.app",
        ):
            with self.subTest(audience=wrong):
                self.assertEqual(
                    self._twin_refusal(twin=twin_service(audience=wrong)),
                    "twin certification audience is not the twin's own URL",
                )

    def test_the_certification_operator_must_be_the_approved_account(self):
        """The twin verifies incoming tokens against this address. Any other
        one points it at an identity nobody approved."""
        for wrong in (
            "sitesift-certification-runtime@email-automation-cache."
            "iam.gserviceaccount.com",
            "sitesift-certification-operator@somewhere-else."
            "iam.gserviceaccount.com",
            "bp21harrison@gmail.com",
            "248289505828-compute@developer.gserviceaccount.com",
        ):
            with self.subTest(operator=wrong):
                self.assertEqual(
                    self._twin_refusal(twin=twin_service(operator_email=wrong)),
                    "twin certification operator is not the approved operator account",
                )

    def test_the_certification_operator_subject_must_be_numeric(self):
        """An address can be reassigned to a new principal; the numeric subject
        is what actually pins the identity, so a non-numeric one pins nothing."""
        for wrong in ("not-a-number", "104729384756019283746x", "1.5", "-1"):
            with self.subTest(subject=wrong):
                self.assertEqual(
                    self._twin_refusal(twin=twin_service(operator_sub=wrong)),
                    "twin certification operator subject is not a numeric uniqueId",
                )

    def test_a_twin_only_field_may_not_hide_behind_a_secret_reference(self):
        """A value the comparator cannot read is not a value it may approve.
        Refuse, never sanitise."""
        for name in (
            "SITESIFT_PRODUCTION_CANDIDATE_REVISION",
            "SITESIFT_FIXTURE_CONFIG_SECRET_VERSION",
            "SITESIFT_CERTIFICATION_AUDIENCE",
            "SITESIFT_CERTIFICATION_OPERATOR_EMAIL",
            "SITESIFT_CERTIFICATION_OPERATOR_SUB",
        ):
            with self.subTest(name=name):
                hidden = twin_service(drop_env=(name,))
                hidden["spec"]["template"]["spec"]["containers"][0]["env"].append(
                    {
                        "name": name,
                        "valueFrom": {
                            "secretKeyRef": {"name": "somewhere", "key": "1"}
                        },
                    }
                )
                self.assertEqual(
                    self._twin_refusal(twin=hidden),
                    f"twin {name} is not a plain literal value",
                )

    def test_the_expected_candidate_revision_itself_must_be_a_revision_name(self):
        """The rule compares the twin's claim against a caller-supplied name. A
        caller that supplied junk would make the comparison meaningless, so the
        input is refused rather than compared."""
        self.assertEqual(
            self._twin_refusal(expected_candidate_revision="Not A Revision"),
            "expected candidate revision is not a revision name",
        )

    def test_none_of_the_new_rules_fire_on_the_deployable_twin(self):
        """Vacuity guards. An assertion that would also hold if the thing it
        checks disappeared is not a pin, and a rule that fires on good input is
        not a rule anyone can deploy past."""
        for sentence in (
            "twin does not name the production candidate under certification",
            "twin production candidate revision names the twin's own service",
            "twin fixture config version and secret reference disagree",
            "twin fixture config secret version is not a positive decimal",
            "twin certification audience is not the twin's own URL",
            "twin certification operator is not the approved operator account",
            "twin certification operator subject is not a numeric uniqueId",
        ):
            with self.subTest(sentence=sentence):
                # Raises nothing at all on the deployable twin; if it did, the
                # exact-stamp case above would already be failing.
                phase1_rollout.validate_twin_stamp(
                    twin_service(), twin_policy(), **self._twin_call()
                )

    def test_twin_must_run_the_candidate_artifact_byte_for_byte(self):
        other = CANDIDATE_IMAGE.rsplit("sha256:", 1)[0] + "sha256:" + "d" * 64
        with self.assertRaises(phase1_rollout.RolloutError):
            phase1_rollout.validate_twin_stamp(
                twin_service(image=other), twin_policy(), **self._twin_call()
            )
        # A twin whose stamp still claims this build while the container runs a
        # different one: only the image comparison can catch this.
        lying = twin_service()
        lying["spec"]["template"]["spec"]["containers"][0]["image"] = other
        with self.assertRaises(phase1_rollout.RolloutError):
            phase1_rollout.validate_twin_stamp(
                lying, twin_policy(), **self._twin_call()
            )
        # ...and a twin pinned to a mutable tag rather than a digest.
        tagged = twin_service()
        tagged["spec"]["template"]["spec"]["containers"][0]["image"] = (
            CANDIDATE_IMAGE.split("@", 1)[0] + ":latest"
        )
        with self.assertRaises(phase1_rollout.RolloutError):
            phase1_rollout.validate_twin_stamp(
                tagged, twin_policy(), **self._twin_call()
            )

    def test_twin_identity_and_runtime_account_must_be_the_certification_pair(self):
        for label, document in (
            ("production name", twin_service(name="process-user")),
            (
                "production runtime",
                twin_service(
                    service_account=(
                        "248289505828-compute@developer.gserviceaccount.com"
                    )
                ),
            ),
            ("unknown runtime", twin_service(service_account="someone@example.com")),
            ("public ingress", twin_service(ingress="all")),
        ):
            with self.subTest(label=label):
                with self.assertRaises(phase1_rollout.RolloutError):
                    phase1_rollout.validate_twin_stamp(
                        document, twin_policy(), **self._twin_call()
                    )

    def test_twin_is_never_a_production_traffic_target(self):
        with self.assertRaises(phase1_rollout.RolloutError):
            phase1_rollout.validate_twin_stamp(
                twin_service(traffic_revision=CANDIDATE),
                twin_policy(),
                **self._twin_call(),
            )
        with self.assertRaises(phase1_rollout.RolloutError):
            phase1_rollout.validate_twin_stamp(
                twin_service(),
                twin_policy(),
                **self._twin_call(
                    production=phase1_rollout.validate_topology(
                        service(extra={"twin": TWIN_REVISION}),
                        expected_positive=OLD_REVISION,
                        expected_release=OLD_REVISION,
                        expected_aux=AUX_TAGS,
                        expected_extra={"twin": TWIN_REVISION},
                    )
                ),
            )

    def test_twin_stamp_must_name_this_commit_and_this_artifact(self):
        for label, document in (
            ("foreign source", twin_service(source_revision="0" * 40)),
            (
                "dropped source",
                twin_service(drop_env=("SITESIFT_SOURCE_REVISION",)),
            ),
            ("dropped digest", twin_service(drop_env=("SITESIFT_IMAGE_DIGEST",))),
        ):
            with self.subTest(label=label):
                with self.assertRaises(phase1_rollout.RolloutError):
                    phase1_rollout.validate_twin_stamp(
                        document, twin_policy(), **self._twin_call()
                    )

        # Candidate and twin agreeing with each other while both disagree with
        # the reviewed commit. Symmetry proves nothing here: only the check
        # against the expected value can see it.
        other = "b" * 40
        agreed = revision(CANDIDATE, CANDIDATE_IMAGE)["spec"]
        for entry in agreed["containers"][0]["env"]:
            if entry["name"] == "SITESIFT_SOURCE_REVISION":
                entry["value"] = other
        with self.assertRaises(phase1_rollout.RolloutError):
            phase1_rollout.validate_twin_stamp(
                twin_service(source_revision=other),
                twin_policy(),
                **self._twin_call(candidate_spec=agreed),
            )

    def test_twin_fixture_secret_must_pin_a_positive_decimal_version(self):
        for version in ("latest", "0", "07", "", "1.0", "-1", "v1"):
            with self.subTest(version=version):
                with self.assertRaises(phase1_rollout.RolloutError):
                    phase1_rollout.validate_twin_stamp(
                        twin_service(fixture_version=version),
                        twin_policy(),
                        **self._twin_call(),
                    )
        with self.assertRaises(phase1_rollout.RolloutError):
            phase1_rollout.validate_twin_stamp(
                twin_service(fixture_secret="some-other-secret"),
                twin_policy(),
                **self._twin_call(),
            )

    def test_twin_must_not_carry_production_capabilities(self):
        for name in (
            "FIREBASE_BUCKET",
            "AZURE_API_APP_ID",
            "AZURE_API_CLIENT_SECRET",
            "FIREBASE_API_KEY",
            "GOOGLE_OAUTH_CLIENT_ID",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "GOOGLE_REFRESH_TOKEN",
            "PROCESS_USER_AUTH",
            "SITESIFT_AUTO_REPLY_ALLOWLIST",
            "SITESIFT_TOUR_ACTION_ALLOWLIST",
        ):
            with self.subTest(name=name):
                with self.assertRaises(phase1_rollout.RolloutError):
                    phase1_rollout.validate_twin_stamp(
                        twin_service(extra_env=[{"name": name, "value": "x"}]),
                        twin_policy(),
                        **self._twin_call(),
                    )

    def test_unclassified_twin_difference_is_a_failure_not_a_third_category(self):
        with self.assertRaises(phase1_rollout.RolloutError):
            phase1_rollout.validate_twin_stamp(
                twin_service(extra_env=[{"name": "UNAPPROVED_MODE", "value": "1"}]),
                twin_policy(),
                **self._twin_call(),
            )
        with self.assertRaises(phase1_rollout.RolloutError):
            phase1_rollout.validate_twin_stamp(
                twin_service(
                    extra_env=[
                        {"name": "SITESIFT_NATIVE_IMAGE_INGESTION", "value": "false"}
                    ]
                ),
                twin_policy(),
                **self._twin_call(),
            )

    def test_shared_configuration_must_be_identical_on_both_surfaces(self):
        drifted = twin_service(drop_env=("SITESIFT_NATIVE_IMAGE_INGESTION",))
        with self.assertRaises(phase1_rollout.RolloutError):
            phase1_rollout.validate_twin_stamp(
                drifted, twin_policy(), **self._twin_call()
            )
        changed = twin_service()
        for entry in changed["spec"]["template"]["spec"]["containers"][0]["env"]:
            if entry["name"] == "SITESIFT_NATIVE_IMAGE_INGESTION":
                entry["value"] = "true"
        with self.assertRaises(phase1_rollout.RolloutError):
            phase1_rollout.validate_twin_stamp(
                changed, twin_policy(), **self._twin_call()
            )

    def test_only_the_certification_operator_may_invoke_the_twin(self):
        for label, policy in (
            ("public", twin_policy(members=["allUsers"])),
            ("authenticated", twin_policy(members=["allAuthenticatedUsers"])),
            ("human token", twin_policy(members=["user:bp21harrison@gmail.com"])),
            (
                "production runtime",
                twin_policy(
                    members=[
                        "serviceAccount:248289505828-compute@"
                        "developer.gserviceaccount.com"
                    ]
                ),
            ),
            (
                "extra member",
                twin_policy(
                    members=[
                        f"serviceAccount:{TWIN_OPERATOR_SA}",
                        "user:bp21harrison@gmail.com",
                    ]
                ),
            ),
            ("wrong role", twin_policy(role="roles/run.admin")),
            ("no bindings", {"bindings": []}),
        ):
            with self.subTest(label=label):
                with self.assertRaises(phase1_rollout.RolloutError):
                    phase1_rollout.validate_twin_stamp(
                        twin_service(), policy, **self._twin_call()
                    )

    def test_duplicate_twin_environment_names_are_refused(self):
        with self.assertRaises(phase1_rollout.RolloutError):
            phase1_rollout.validate_twin_stamp(
                twin_service(
                    extra_env=[{"name": "K_SERVICE", "value": TWIN_SERVICE_NAME}]
                ),
                twin_policy(),
                **self._twin_call(),
            )


class StateMachineTests(unittest.TestCase):
    def make_rollout(self, ops, nonce="a" * 64):
        sleeps = []
        rollout = phase1_rollout.Phase1Rollout(
            ops=ops,
            head_sha=HEAD_SHA,
            sleeper=sleeps.append,
            nonce_factory=lambda: nonce,
        )
        return rollout, sleeps

    def test_happy_path_orders_pause_tag_health_remove_promote_resume(self):
        ops = FakeOps()
        rollout, sleeps = self.make_rollout(ops)
        rollout.apply()
        self.assertEqual([5, 5], sleeps)
        self.assertLess(
            ops.events.index(
                "legacy:https://process-user-example.run.app|"
                "aud:https://process-user-example.run.app"
            ),
            ops.events.index("pause"),
        )
        tag_health = next(
            event for event in ops.events
            if event.startswith("legacy:https://phase1-cert-")
        )
        self.assertEqual(
            1,
            sum(
                event.startswith("legacy:https://phase1-cert-")
                for event in ops.events
            ),
        )
        self.assertTrue(
            tag_health.endswith("|aud:https://process-user-example.run.app")
        )
        self.assertLess(ops.events.index("pause"), ops.events.index("tag:add"))
        self.assertLess(ops.events.index("tag:add"), ops.events.index("tag:remove"))
        self.assertLess(ops.events.index("tag:remove"), ops.events.index("promote"))
        self.assertLess(ops.events.index("promote"), ops.events.index("resume"))
        self.assertLess(ops.events.index("preflight"), ops.events.index("lock:acquire"))
        self.assertLess(ops.events.index("lock:acquire"), ops.events.index("pause"))
        self.assertEqual("lock:release", ops.events[-1])
        self.assertFalse(ops.lock_held)
        mutation_names = {
            "pause", "resume", "tag:add", "tag:remove", "promote", "rollback"
        }
        for index, event in enumerate(ops.events):
            if event in mutation_names:
                with self.subTest(event=event, index=index):
                    self.assertEqual("lock:assert", ops.events[index - 1])
                    self.assertEqual("lock:assert", ops.events[index + 1])
        remove_index = ops.events.index("tag:remove")
        promote_index = ops.events.index("promote")
        self.assertEqual(
            [
                "lock:assert",
                "tag:remove",
                "lock:assert",
                "service",
                "lock:assert",
                "artifact",
                f"revision:{CANDIDATE}",
                f"revision:{OLD_REVISION}",
                "prerequisites",
                "service",
                "service-access",
                "twin:service",
                "twin:iam",
                "queue",
                "tasks",
                "lock:assert",
                "lock:assert",
                "promote",
                "lock:assert",
            ],
            ops.events[remove_index - 1:promote_index + 2],
        )
        self.assertEqual("RUNNING", ops.queue["state"])

    def test_twin_stamp_checks_sit_inside_the_authorization_lock(self):
        # A check outside the lock is a check something can change between
        # passing and being relied on. The twin reads must therefore be bounded
        # by a lock assertion on BOTH sides, with no mutation in between.
        ops = FakeOps()
        rollout, _ = self.make_rollout(ops)
        rollout.apply()
        remove_index = ops.events.index("tag:remove")
        promote_index = ops.events.index("promote")
        slice_events = ops.events[remove_index:promote_index]
        self.assertIn("twin:service", slice_events)
        self.assertIn("twin:iam", slice_events)

        first = ops.events.index("twin:service")
        last = ops.events.index("twin:iam")
        self.assertLess(first, last)
        opening = max(
            index
            for index, event in enumerate(ops.events[:first])
            if event == "lock:assert"
        )
        closing = min(
            index
            for index, event in enumerate(ops.events)
            if index > last and event == "lock:assert"
        )
        self.assertLess(opening, first)
        self.assertLess(last, closing)
        mutations = {"pause", "resume", "tag:add", "tag:remove", "promote", "rollback"}
        self.assertEqual(
            set(),
            mutations & set(ops.events[opening:closing]),
            "no mutation may run between the twin check and the lock assertion "
            "that certifies it",
        )
        self.assertEqual(1, ops.events.count("twin:service"))
        self.assertEqual(1, ops.events.count("twin:iam"))

    def test_twin_is_never_read_once_the_authorization_lock_is_gone(self):
        # If the twin were inspected before the slice re-asserts the lock, this
        # would still record a twin read after ownership was lost.
        ops = FakeOps()
        ops.post_tag_removal_fault = "lock_before"
        rollout, _ = self.make_rollout(ops)
        with self.assertRaises(phase1_rollout.RolloutError):
            rollout.apply()
        remove_index = ops.events.index("tag:remove")
        self.assertNotIn("twin:service", ops.events[remove_index:])
        self.assertNotIn("promote", ops.events)

    def test_twin_reads_never_happen_before_the_lock_is_acquired(self):
        ops = FakeOps()
        rollout, _ = self.make_rollout(ops)
        rollout.apply()
        acquire = ops.events.index("lock:acquire")
        release = ops.events.index("lock:release")
        for event in ("twin:service", "twin:iam"):
            with self.subTest(event=event):
                self.assertLess(acquire, ops.events.index(event))
                self.assertLess(ops.events.index(event), release)

    def test_every_post_tag_pre_promotion_revalidation_failure_stops_traffic(self):
        expected_events = {
            "candidate_artifact": "artifact",
            "candidate_revision": f"revision:{CANDIDATE}",
            "candidate_digest": f"revision:{CANDIDATE}",
            "candidate_gate": f"revision:{CANDIDATE}",
            "rollback_revision": f"revision:{OLD_REVISION}",
            "rollback_digest": f"revision:{OLD_REVISION}",
            "switches": "prerequisites",
            "topology": "service",
            "iam": "service-access",
            "twin_read": "twin:service",
            "twin_iam_read": "twin:iam",
            "twin_image": "twin:service",
            "twin_service_account": "twin:service",
            "twin_identity": "twin:service",
            "twin_stamp": "twin:service",
            "twin_fixture_alias": "twin:service",
            "twin_forbidden_env": "twin:service",
            "twin_unclassified_env": "twin:service",
            "twin_public_invoker": "twin:iam",
            "twin_production_traffic": "twin:service",
            "queue": "queue",
            "task_read": "tasks",
            "lock_before": "lock:assert",
            "lock_after": "lock:assert",
        }
        for fault, expected_event in expected_events.items():
            with self.subTest(fault=fault):
                ops = FakeOps()
                ops.post_tag_removal_fault = fault
                rollout, _ = self.make_rollout(ops)
                with self.assertRaises(phase1_rollout.RolloutError):
                    rollout.apply()
                self.assertIn("tag:remove", ops.events)
                self.assertNotIn("promote", ops.events)
                remove_index = ops.events.index("tag:remove")
                after_remove = ops.events[remove_index:]
                cleanup_index = (
                    after_remove.index("pause")
                    if "pause" in after_remove
                    else len(after_remove)
                )
                authorization_events = after_remove[:cleanup_index]
                self.assertIn(expected_event, authorization_events)

                if fault.startswith("candidate_"):
                    self.assertNotIn(
                        f"revision:{OLD_REVISION}", authorization_events
                    )
                elif fault.startswith("rollback_"):
                    self.assertLess(
                        authorization_events.index(f"revision:{CANDIDATE}"),
                        authorization_events.index(f"revision:{OLD_REVISION}"),
                    )

                if fault == "topology":
                    self.assertEqual(
                        [
                            "tag:remove",
                            "lock:assert",
                            "service",
                            "lock:assert",
                            "artifact",
                            f"revision:{CANDIDATE}",
                            f"revision:{OLD_REVISION}",
                            "prerequisites",
                            "service",
                        ],
                        ops.events[remove_index:remove_index + 9],
                    )
                elif fault == "lock_before":
                    self.assertNotIn("artifact", after_remove)
                    self.assertEqual("lock:assert", after_remove[-1])
                elif fault == "lock_after":
                    self.assertIn("tasks", after_remove)
                    self.assertEqual("lock:assert", after_remove[-1])

    def test_nonempty_final_task_inventory_stops_before_promotion(self):
        ops = FakeOps()
        ops.task_snapshots = [
            [],
            [],
            [],
            [],
            [{"name": "opaque"}],
        ]
        rollout, _ = self.make_rollout(ops)
        with self.assertRaisesRegex(phase1_rollout.RolloutError, "MANUAL_RECOVERY"):
            rollout.apply()

        remove_index = ops.events.index("tag:remove")
        helper_task_index = ops.events.index("tasks", remove_index)
        self.assertLess(
            ops.events.index(f"revision:{OLD_REVISION}", remove_index),
            helper_task_index,
        )
        self.assertNotIn("promote", ops.events)
        self.assertTrue(rollout.task_observed)
        self.assertEqual("PAUSED", ops.queue["state"])
        self.assertTrue(ops.lock_held)

    def test_staging_prerequisites_prove_old_running_empty_without_mutation(self):
        ops = FakeOps()
        rollout, _ = self.make_rollout(ops)
        rollout.verify_staging_prerequisites()
        self.assertIn("preflight", ops.events)
        self.assertIn("prerequisites", ops.events)
        self.assertIn("service-access", ops.events)
        self.assertIn(f"revision:{OLD_REVISION}", ops.events)
        self.assertIn("tasks", ops.events)
        self.assertNotIn("artifact", ops.events)
        self.assertNotIn(f"revision:{CANDIDATE}", ops.events)
        self.assertFalse(
            {"pause", "resume", "tag:add", "tag:remove", "promote", "rollback"}
            & set(ops.events)
        )

        blocked = FakeOps()
        blocked.task_snapshots = [[{"name": "opaque-task"}]]
        blocked_rollout, _ = self.make_rollout(blocked)
        with self.assertRaisesRegex(phase1_rollout.RolloutError, "tasks are not empty"):
            blocked_rollout.verify_staging_prerequisites()

    def test_competing_lock_stops_after_read_only_baseline(self):
        ops = FakeOps()
        ops.fail_acquire_lock = True
        rollout, _ = self.make_rollout(ops)
        with self.assertRaises(phase1_rollout.RolloutError):
            rollout.apply()
        self.assertIn("lock:acquire", ops.events)
        self.assertFalse(
            {"pause", "resume", "tag:add", "tag:remove", "promote", "rollback"}
            & set(ops.events)
        )
        self.assertEqual("RUNNING", ops.queue["state"])

    def test_authoritative_lock_failures_do_not_enter_abort_or_retry_path(self):
        for error in (
            phase1_rollout.RolloutLockHeld("competing lock"),
            phase1_rollout.RolloutError("rollout lock create was rejected"),
        ):
            with self.subTest(error=error):
                ops = FakeOps()
                ops.acquire_error = error
                rollout, _ = self.make_rollout(ops)
                with self.assertRaises(phase1_rollout.RolloutError):
                    rollout.apply()
                self.assertEqual(1, ops.events.count("lock:acquire"))
                self.assertNotIn("lock:abort", ops.events)
                self.assertFalse(
                    {"pause", "resume", "tag:add", "tag:remove", "promote", "rollback"}
                    & set(ops.events)
                )

    def test_lock_nonce_is_unique_input_not_head_identity(self):
        first = FakeOps()
        second = FakeOps()
        first_rollout, _ = self.make_rollout(first, "a" * 64)
        second_rollout, _ = self.make_rollout(second, "b" * 64)
        first_rollout.apply()
        second_rollout.apply()
        self.assertEqual("a" * 64, first.lock_nonce)
        self.assertEqual("b" * 64, second.lock_nonce)

    def test_lock_loss_before_promotion_stops_without_cleanup_mutation(self):
        ops = FakeOps()
        ops.lose_lock_on_assert = 8
        rollout, _ = self.make_rollout(ops)
        with self.assertRaisesRegex(
            phase1_rollout.RolloutError, "rollout lock ownership lost"
        ):
            rollout.apply()
        lost_index = len(ops.events) - 1
        self.assertEqual("lock:assert", ops.events[lost_index])
        self.assertNotIn("promote", ops.events)
        self.assertFalse(
            {"pause", "resume", "tag:add", "tag:remove", "promote", "rollback"}
            & set(ops.events[lost_index + 1:])
        )
        self.assertTrue(ops.lock_held is False)

    def test_lock_loss_after_promotion_never_rolls_back_or_resumes(self):
        ops = FakeOps()
        ops.lose_lock_on_assert = 14
        rollout, _ = self.make_rollout(ops)
        with self.assertRaisesRegex(
            phase1_rollout.RolloutError, "rollout lock ownership lost"
        ):
            rollout.apply()
        self.assertIn("promote", ops.events)
        lost_index = len(ops.events) - 1
        self.assertEqual("lock:assert", ops.events[lost_index])
        self.assertFalse(
            {"pause", "resume", "tag:add", "tag:remove", "promote", "rollback"}
            & set(ops.events[lost_index + 1:])
        )

    def test_manual_recovery_keeps_owned_lock_and_queue_paused(self):
        ops = FakeOps()
        ops.fail_remove = True
        rollout, _ = self.make_rollout(ops)
        with self.assertRaisesRegex(phase1_rollout.RolloutError, "MANUAL_RECOVERY"):
            rollout.apply()
        self.assertTrue(ops.lock_held)
        self.assertNotIn("lock:release", ops.events)
        self.assertEqual("PAUSED", ops.queue["state"])

    def test_orphan_clear_requires_two_exact_old_state_proofs(self):
        ops = FakeOps()
        ops.lock_held = True
        ops.lock_nonce = "a" * 64
        lock = phase1_rollout.RolloutLock(
            owner_nonce="a" * 64,
            head_sha=HEAD_SHA,
            update_time="2026-08-12T00:00:00Z",
        )
        rollout, _ = self.make_rollout(ops)
        rollout.clear_orphan_lock_old_state(lock)
        self.assertEqual(2, ops.events.count("preflight"))
        self.assertEqual("lock:release", ops.events[-1])
        self.assertFalse(ops.lock_held)
        self.assertFalse(
            {"pause", "resume", "tag:add", "tag:remove", "promote", "rollback"}
            & set(ops.events)
        )

    def test_orphan_clear_refuses_non_old_state_without_deleting(self):
        ops = FakeOps()
        ops.lock_held = True
        ops.lock_nonce = "a" * 64
        ops.queue["state"] = "PAUSED"
        lock = phase1_rollout.RolloutLock(
            owner_nonce="a" * 64,
            head_sha=HEAD_SHA,
            update_time="2026-08-12T00:00:00Z",
        )
        rollout, _ = self.make_rollout(ops)
        with self.assertRaises(phase1_rollout.RolloutError):
            rollout.clear_orphan_lock_old_state(lock)
        self.assertNotIn("lock:release", ops.events)
        self.assertTrue(ops.lock_held)

    def test_orphan_clear_refuses_tasks_in_either_old_state_proof(self):
        for task_snapshots in (
            [[{"name": "opaque-task"}]],
            [[], [{"name": "opaque-task"}]],
        ):
            with self.subTest(task_snapshots=task_snapshots):
                ops = FakeOps()
                ops.lock_held = True
                ops.lock_nonce = "a" * 64
                ops.task_snapshots = task_snapshots
                lock = phase1_rollout.RolloutLock(
                    owner_nonce="a" * 64,
                    head_sha=HEAD_SHA,
                    update_time="2026-08-12T00:00:00Z",
                )
                rollout, _ = self.make_rollout(ops)
                with self.assertRaisesRegex(
                    phase1_rollout.RolloutError, "tasks are not empty"
                ):
                    rollout.clear_orphan_lock_old_state(lock)
                self.assertNotIn("lock:release", ops.events)
                self.assertTrue(ops.lock_held)
                self.assertFalse(
                    {"pause", "resume", "tag:add", "tag:remove", "promote", "rollback"}
                    & set(ops.events)
                )

    def test_wrong_tag_health_removes_tag_and_resumes_without_traffic_mutation(self):
        ops = FakeOps()
        ops.fail_legacy_health_on = 5
        rollout, _ = self.make_rollout(ops)
        with self.assertRaises(phase1_rollout.RolloutError):
            rollout.apply()
        self.assertIn("tag:remove", ops.events)
        self.assertNotIn("promote", ops.events)
        self.assertNotIn("rollback", ops.events)
        self.assertEqual("RUNNING", ops.queue["state"])

    def test_cleanup_ambiguity_leaves_queue_paused(self):
        ops = FakeOps()
        ops.fail_remove = True
        rollout, _ = self.make_rollout(ops)
        with self.assertRaisesRegex(phase1_rollout.RolloutError, "MANUAL_RECOVERY"):
            rollout.apply()
        self.assertEqual("PAUSED", ops.queue["state"])

    def test_failed_old_health_during_cleanup_keeps_queue_paused(self):
        ops = FakeOps()
        ops.fail_legacy_health_after = 5
        rollout, _ = self.make_rollout(ops)
        with self.assertRaisesRegex(phase1_rollout.RolloutError, "MANUAL_RECOVERY"):
            rollout.apply()
        self.assertEqual("PAUSED", ops.queue["state"])
        self.assertNotIn("resume", ops.events)

    def test_task_appearance_leaves_queue_paused(self):
        ops = FakeOps()
        ops.task_snapshots = [[], [{"name": "opaque"}]]
        rollout, _ = self.make_rollout(ops)
        with self.assertRaisesRegex(phase1_rollout.RolloutError, "MANUAL_RECOVERY"):
            rollout.apply()
        self.assertEqual("PAUSED", ops.queue["state"])

    def test_promotion_failure_rolls_back_before_resuming_old_revision(self):
        ops = FakeOps()
        ops.fail_promote = True
        rollout, _ = self.make_rollout(ops)
        with self.assertRaises(phase1_rollout.RolloutError):
            rollout.apply()
        self.assertIn("rollback", ops.events)
        self.assertEqual(service(), ops.service)
        self.assertEqual("RUNNING", ops.queue["state"])

    def test_resume_failure_after_state_change_repauses_and_rolls_back(self):
        ops = FakeOps()
        ops.fail_resume_after_change = True
        rollout, _ = self.make_rollout(ops)
        with self.assertRaisesRegex(phase1_rollout.RolloutError, "MANUAL_RECOVERY"):
            rollout.apply()
        self.assertIn("rollback", ops.events)
        self.assertEqual(service(), ops.service)
        self.assertEqual("PAUSED", ops.queue["state"])

    def test_prerequisite_failure_has_no_queue_tag_or_traffic_mutation(self):
        ops = FakeOps()
        ops.fail_prerequisites = True
        rollout, _ = self.make_rollout(ops)
        with self.assertRaises(phase1_rollout.RolloutError):
            rollout.apply()
        self.assertFalse(
            {"pause", "resume", "tag:add", "tag:remove", "promote", "rollback"}
            & set(ops.events)
        )
        self.assertEqual("RUNNING", ops.queue["state"])

    def test_prerequisite_drift_before_promotion_removes_tag_and_resumes_old(self):
        ops = FakeOps()
        ops.fail_prerequisites_after = 2
        rollout, _ = self.make_rollout(ops)
        with self.assertRaises(phase1_rollout.RolloutError):
            rollout.apply()
        self.assertNotIn("promote", ops.events)
        self.assertEqual(service(), ops.service)
        self.assertEqual("PAUSED", ops.queue["state"])

    def test_prerequisite_drift_during_cleanup_keeps_queue_paused(self):
        ops = FakeOps()
        ops.fail_prerequisites_after = 2
        rollout, _ = self.make_rollout(ops)
        with self.assertRaisesRegex(phase1_rollout.RolloutError, "MANUAL_RECOVERY"):
            rollout.apply()
        self.assertEqual("PAUSED", ops.queue["state"])

    def test_keyboard_interrupt_after_tag_runs_cleanup_and_resumes_old(self):
        ops = FakeOps()
        ops.interrupt_legacy_health_on = 5
        rollout, _ = self.make_rollout(ops)
        with self.assertRaises(phase1_rollout.RolloutError):
            rollout.apply()
        self.assertIn("tag:remove", ops.events)
        self.assertNotIn("promote", ops.events)
        self.assertEqual("RUNNING", ops.queue["state"])

    def test_final_prerequisite_drift_after_resume_repauses_and_rolls_back(self):
        ops = FakeOps()
        ops.fail_prerequisites_after = 4
        rollout, _ = self.make_rollout(ops)
        with self.assertRaisesRegex(phase1_rollout.RolloutError, "MANUAL_RECOVERY"):
            rollout.apply()
        self.assertEqual("PAUSED", ops.queue["state"])
        self.assertEqual(service(), ops.service)

    def test_resume_and_repause_failure_reports_unverified_queue_state(self):
        ops = FakeOps()
        ops.fail_resume_after_change = True
        original_pause = ops.pause_queue
        pause_calls = {"count": 0}

        def fail_second_pause():
            pause_calls["count"] += 1
            if pause_calls["count"] > 2:
                raise phase1_rollout.RolloutError("repause failed")
            original_pause()

        ops.pause_queue = fail_second_pause
        rollout, _ = self.make_rollout(ops)
        with self.assertRaisesRegex(
            phase1_rollout.RolloutError, "queue state unverified"
        ):
            rollout.apply()
        self.assertEqual("RUNNING", ops.queue["state"])

    def test_dry_run_has_zero_ops_calls(self):
        ops = FakeOps()
        rollout, _ = self.make_rollout(ops)
        summary = rollout.dry_run()
        self.assertEqual([], ops.events)
        self.assertIn(CANDIDATE, summary)


class StaticSafetyTests(unittest.TestCase):
    def test_wrapper_and_module_have_no_forbidden_effects(self):
        module = MODULE_PATH.read_text()
        wrapper = (ROOT / "scripts" / "rollout_process_user_phase1.sh").read_text()
        combined = module + wrapper
        for forbidden in (
            "eval(", "shell=True", "run revisions delete",
            "creationEnabled=true", "automationEnabled=true", "queues purge",
        ):
            self.assertNotIn(forbidden, combined)
        string_values = {
            node.value
            for node in ast.walk(ast.parse(module))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        for route in ("/process-user", "/process-outbox"):
            self.assertNotIn(route, string_values)
            self.assertFalse(any(value.startswith(f"{route}?") for value in string_values))
        service_source = (ROOT / "service.py").read_text()
        self.assertNotIn("/health/identity/v1", service_source)
        self.assertNotIn("unauthenticated_status", module)

    def test_orphan_clear_cli_binds_exact_packet_without_printing_nonce(self):
        head = "1" * 40
        nonce = "a" * 64
        update_time = "2026-08-12T00:00:00Z"
        observed = []

        class FakeRollout:
            def __init__(self, *, ops, head_sha):
                del ops
                self.head_sha = head_sha

            def clear_orphan_lock_old_state(self, lock):
                observed.append(lock)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(phase1_rollout, "_current_head", return_value=head), patch.object(
            phase1_rollout, "SubprocessOps", return_value=object()
        ), patch.object(phase1_rollout, "Phase1Rollout", FakeRollout), redirect_stdout(
            stdout
        ), redirect_stderr(stderr):
            result = phase1_rollout.main(
                ["--clear-orphan-lock-old-state", head, nonce, update_time]
            )

        self.assertEqual(0, result)
        self.assertEqual(
            [phase1_rollout.RolloutLock(nonce, head, update_time)], observed
        )
        self.assertNotIn(nonce, stdout.getvalue())
        self.assertEqual("", stderr.getvalue())


class AdapterContractTests(unittest.TestCase):
    def test_lock_permissions_are_exact_before_create(self):
        ops = phase1_rollout.SubprocessOps(ROOT, "1" * 40)
        exact = json.dumps(
            {"permissions": list(phase1_rollout.LOCK_PERMISSIONS)}
        ).encode()
        with patch.object(
            ops, "_firestore_exchange", return_value=(200, exact)
        ):
            ops.verify_lock_permissions()
        reordered = json.dumps(
            {"permissions": list(reversed(phase1_rollout.LOCK_PERMISSIONS))}
        ).encode()
        with patch.object(
            ops, "_firestore_exchange", return_value=(200, reordered)
        ):
            ops.verify_lock_permissions()
        for value in (
            {"permissions": list(phase1_rollout.LOCK_PERMISSIONS[:-1])},
            {"permissions": [*phase1_rollout.LOCK_PERMISSIONS, "extra"]},
            {"permissions": [phase1_rollout.LOCK_PERMISSIONS[0]] * 3},
            {"permissions": [{"unexpected": "shape"}] * 3},
        ):
            with self.subTest(value=value), patch.object(
                ops,
                "_firestore_exchange",
                return_value=(200, json.dumps(value).encode()),
            ):
                with self.assertRaises(phase1_rollout.RolloutError):
                    ops.verify_lock_permissions()

    def test_ambiguous_create_retries_once_only_after_canonical_not_found(self):
        ops = phase1_rollout.SubprocessOps(ROOT, "1" * 40)
        with patch.object(
            ops,
            "_firestore_exchange",
            side_effect=[
                phase1_rollout.FirestoreExchangeError(None),
                (404, firestore_error(404, "NOT_FOUND")),
                (200, json.dumps(lock_document()).encode()),
            ],
        ) as exchange:
            lock = ops.acquire_lock("1" * 40, "a" * 64)
        self.assertEqual("a" * 64, lock.owner_nonce)
        self.assertEqual(3, exchange.call_count)

        with patch.object(
            ops,
            "_firestore_exchange",
            side_effect=[
                phase1_rollout.FirestoreExchangeError(None),
                (404, b""),
            ],
        ):
            with self.assertRaisesRegex(
                phase1_rollout.RolloutError, "acquisition unverified"
            ):
                ops.acquire_lock("1" * 40, "a" * 64)

    def test_ambiguous_create_never_treats_competing_owner_as_safe(self):
        ops = phase1_rollout.SubprocessOps(ROOT, "1" * 40)
        competitor = (200, json.dumps(lock_document(nonce="b" * 64)).encode())
        cases = (
            [phase1_rollout.FirestoreExchangeError(None), competitor],
            [
                phase1_rollout.FirestoreExchangeError(None),
                (404, firestore_error(404, "NOT_FOUND")),
                phase1_rollout.FirestoreExchangeError(None),
                competitor,
            ],
            [KeyboardInterrupt(), competitor],
        )
        for outcomes in cases:
            with self.subTest(outcomes=outcomes), patch.object(
                ops, "_firestore_exchange", side_effect=outcomes
            ):
                with self.assertRaisesRegex(
                    phase1_rollout.RolloutError,
                    "MANUAL_RECOVERY: rollout lock acquisition unverified",
                ):
                    ops.acquire_lock("1" * 40, "a" * 64)

    def test_authoritative_first_conflict_never_adopts_same_nonce(self):
        ops = phase1_rollout.SubprocessOps(ROOT, "1" * 40)
        with patch.object(
            ops,
            "_firestore_exchange",
            return_value=(409, firestore_error(409, "ALREADY_EXISTS")),
        ) as exchange:
            with self.assertRaises(phase1_rollout.RolloutLockHeld):
                ops.acquire_lock("1" * 40, "a" * 64)
        self.assertEqual(1, exchange.call_count)

    def test_lock_document_contract_is_closed_and_update_time_fenced(self):
        exact = lock_document()
        lock = phase1_rollout.validate_lock_document(
            exact, owner_nonce="a" * 64, head_sha="1" * 40
        )
        self.assertEqual("2026-08-12T00:00:00Z", lock.update_time)
        for bad in (
            {**exact, "extra": True},
            {**exact, "name": exact["name"] + "-other"},
            {**exact, "fields": {**exact["fields"], "extra": {"stringValue": "x"}}},
            {**exact, "fields": {**exact["fields"], "ownerNonce": {"stringValue": "b" * 64}}},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(phase1_rollout.RolloutError):
                    phase1_rollout.validate_lock_document(
                        bad, owner_nonce="a" * 64, head_sha="1" * 40
                    )

    def test_ambiguous_lock_create_adopts_only_exact_nonce_document(self):
        ops = phase1_rollout.SubprocessOps(ROOT, "1" * 40)
        with patch.object(
            ops,
            "_firestore_exchange",
            side_effect=[
                phase1_rollout.FirestoreExchangeError(None),
                (200, json.dumps(lock_document()).encode()),
            ],
        ):
            lock = ops.acquire_lock("1" * 40, "a" * 64)
        self.assertEqual("a" * 64, lock.owner_nonce)

        with patch.object(
            ops,
            "_firestore_exchange",
            side_effect=[
                phase1_rollout.FirestoreExchangeError(None),
                (200, json.dumps(lock_document(nonce="b" * 64)).encode()),
            ],
        ):
            with self.assertRaises(phase1_rollout.RolloutError):
                ops.acquire_lock("1" * 40, "a" * 64)

        for invalid in (b"not-json", json.dumps({"name": "wrong"}).encode()):
            with self.subTest(invalid=invalid):
                with patch.object(
                    ops,
                    "_firestore_exchange",
                    side_effect=[
                        phase1_rollout.FirestoreExchangeError(None),
                        (200, invalid),
                    ],
                ):
                    with self.assertRaisesRegex(
                        phase1_rollout.RolloutError, "acquisition unverified"
                    ):
                        ops.acquire_lock("1" * 40, "a" * 64)

    def test_interrupted_lock_create_releases_exact_nonce_and_never_proceeds(self):
        ops = phase1_rollout.SubprocessOps(ROOT, "1" * 40)
        with patch.object(
            ops,
            "_firestore_exchange",
            side_effect=[
                KeyboardInterrupt(),
                (200, json.dumps(lock_document()).encode()),
                (200, json.dumps(lock_document()).encode()),
                (200, b"{}"),
                (404, firestore_error(404, "NOT_FOUND")),
            ],
        ):
            with self.assertRaisesRegex(
                phase1_rollout.RolloutError, "interrupted safely"
            ):
                ops.acquire_lock("1" * 40, "a" * 64)

        for invalid in (b"not-json", json.dumps({"name": "wrong"}).encode()):
            with self.subTest(invalid=invalid):
                with patch.object(
                    ops,
                    "_firestore_exchange",
                    side_effect=[KeyboardInterrupt(), (200, invalid)],
                ):
                    with self.assertRaisesRegex(
                        phase1_rollout.RolloutError, "acquisition unverified"
                    ):
                        ops.acquire_lock("1" * 40, "a" * 64)

        with patch.object(
            ops,
            "_firestore_exchange",
            side_effect=[KeyboardInterrupt(), KeyboardInterrupt()],
        ):
            with self.assertRaisesRegex(
                phase1_rollout.RolloutError, "acquisition unverified"
            ):
                ops.acquire_lock("1" * 40, "a" * 64)

    def test_controller_interrupted_create_never_mutates_queue_tag_or_traffic(self):
        ops = FakeOps()

        def interrupt_acquire(head_sha, nonce):
            del head_sha, nonce
            raise phase1_rollout.RolloutError("rollout interrupted safely")

        ops.acquire_lock = interrupt_acquire
        rollout, _ = StateMachineTests().make_rollout(ops)
        with self.assertRaisesRegex(
            phase1_rollout.RolloutError, "interrupted safely"
        ):
            rollout.apply()
        self.assertFalse(
            {"pause", "resume", "tag:add", "tag:remove", "promote", "rollback"}
            & set(ops.events)
        )

    def test_lock_release_requires_conditional_delete_and_exact_reconciliation(self):
        ops = phase1_rollout.SubprocessOps(ROOT, "1" * 40)
        lock = phase1_rollout.RolloutLock(
            owner_nonce="a" * 64,
            head_sha="1" * 40,
            update_time="2026-08-12T00:00:00Z",
        )
        exchanges = [
            (200, json.dumps(lock_document()).encode()),
            (200, b"{}"),
            (404, firestore_error(404, "NOT_FOUND")),
        ]
        with patch.object(ops, "_firestore_exchange", side_effect=exchanges) as exchange:
            ops.release_lock(lock)
        delete_call = exchange.call_args_list[1]
        self.assertEqual("DELETE", delete_call.args[0])
        self.assertIn("currentDocument.updateTime=", delete_call.args[1])

    def test_lock_release_unknown_readback_is_manual_recovery(self):
        ops = phase1_rollout.SubprocessOps(ROOT, "1" * 40)
        lock = phase1_rollout.RolloutLock(
            owner_nonce="a" * 64,
            head_sha="1" * 40,
            update_time="2026-08-12T00:00:00Z",
        )
        with patch.object(
            ops,
            "_firestore_exchange",
            side_effect=[
                (200, json.dumps(lock_document()).encode()),
                phase1_rollout.FirestoreExchangeError(None),
                phase1_rollout.FirestoreExchangeError(None),
            ],
        ):
            with self.assertRaisesRegex(
                phase1_rollout.RolloutError, "release unverified"
            ):
                ops.release_lock(lock)

    def test_successful_rollout_release_interrupt_is_manual_recovery(self):
        ops = FakeOps()
        original_release = ops.release_lock

        def interrupt_release(lock):
            original_release(lock)
            raise KeyboardInterrupt()

        ops.release_lock = interrupt_release
        rollout, _ = StateMachineTests().make_rollout(ops)
        with self.assertRaisesRegex(
            phase1_rollout.RolloutError, "lock release unverified"
        ):
            rollout.apply()
        self.assertEqual("RUNNING", ops.queue["state"])
        self.assertEqual(service(CANDIDATE, CANDIDATE), ops.service)

    def test_firestore_rules_source_is_exactly_one_closed_file(self):
        content = "rules_version = '2';\n"
        exact = {"files": [{"name": "firestore.rules", "content": content}]}
        with patch.object(
            phase1_rollout,
            "RULES_HASH",
            __import__("hashlib").sha256(content.encode()).hexdigest(),
        ):
            phase1_rollout.validate_rules_source(exact)
            for bad in (
                {"files": exact["files"] + [{"name": "extra.rules", "content": ""}]},
                {"files": [{**exact["files"][0], "extra": True}]},
                {"files": exact["files"], "extra": True},
            ):
                with self.subTest(bad=bad):
                    with self.assertRaises(phase1_rollout.RolloutError):
                        phase1_rollout.validate_rules_source(bad)

    def test_cloud_sdk_auth_override_environment_is_closed(self):
        phase1_rollout.validate_auth_environment({"GCLOUD_ACCOUNT": phase1_rollout.ACCOUNT})
        for name in (
            "CLOUDSDK_AUTH_ACCESS_TOKEN",
            "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
            "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
            "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT",
            "CLOUDSDK_CORE_ACCOUNT",
            "CLOUDSDK_CORE_PROJECT",
        ):
            with self.subTest(name=name):
                with self.assertRaises(phase1_rollout.RolloutError):
                    phase1_rollout.validate_auth_environment({
                        "GCLOUD_ACCOUNT": phase1_rollout.ACCOUNT,
                        name: "override",
                    })

    def test_cloud_sdk_auth_override_config_is_closed(self):
        ops = phase1_rollout.SubprocessOps(ROOT, "1" * 40)
        with patch.object(ops, "_gcloud", return_value="(unset)") as gcloud:
            ops._validate_gcloud_auth_config()
        self.assertEqual(3, gcloud.call_count)
        with patch.object(ops, "_gcloud", side_effect=["(unset)", "/tmp/token", "(unset)"]):
            with self.assertRaises(phase1_rollout.RolloutError):
                ops._validate_gcloud_auth_config()

    def test_iam_contract_is_exact_and_rejects_public_or_extra_members(self):
        exact = {
            "bindings": [{
                "role": "roles/run.invoker",
                "members": [
                    "serviceAccount:248289505828-compute@developer.gserviceaccount.com"
                ],
            }]
        }
        phase1_rollout.validate_iam(exact)
        for bad in (
            {"bindings": [{"role": "roles/run.invoker", "members": ["allUsers"]}]},
            {"bindings": exact["bindings"] + [{"role": "roles/viewer", "members": ["user:x@example.com"]}]},
            {"bindings": [{
                **exact["bindings"][0],
                "condition": {"title": "sometimes", "expression": "true"},
            }]},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(phase1_rollout.RolloutError):
                    phase1_rollout.validate_iam(bad)

    def test_project_iam_rejects_broad_or_inherited_invoker_principals(self):
        exact_member = (
            "serviceAccount:248289505828-compute@developer.gserviceaccount.com"
        )
        phase1_rollout.validate_project_iam({
            "bindings": [
                {"role": "roles/run.invoker", "members": [exact_member]},
                {"role": "roles/viewer", "members": ["user:operator@example.test"]},
            ]
        })
        for bad in (
            {"bindings": [{"role": "roles/run.invoker", "members": ["allAuthenticatedUsers"]}]},
            {"bindings": [{"role": "roles/run.invoker", "members": [exact_member, "user:extra@example.test"]}]},
            {"bindings": [{"role": "roles/viewer", "members": ["allUsers"]}, {"role": "roles/run.invoker", "members": [exact_member]}]},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(phase1_rollout.RolloutError):
                    phase1_rollout.validate_project_iam(bad)

    def test_user_identity_token_omits_custom_audience_but_health_target_stays_split(self):
        ops = phase1_rollout.SubprocessOps(ROOT, "1" * 40)
        calls = []

        def fake_gcloud(args, timeout=600):
            calls.append(list(args))
            return "opaque-token"

        with patch.object(ops, "_gcloud", side_effect=fake_gcloud), patch.object(
            ops, "_http_json", return_value={"status": "ok"}
        ) as http:
            ops.legacy_health_get(
                "https://phase1-cert-111111111111---process-user-example.run.app",
                SERVICE_URL,
            )

        self.assertEqual([["auth", "print-identity-token"]], calls)
        http.assert_called_once_with(
            "https://phase1-cert-111111111111---process-user-example.run.app/health",
            token="opaque-token",
        )

    def test_health_target_cannot_cross_the_validated_service_origin(self):
        ops = phase1_rollout.SubprocessOps(ROOT, "1" * 40)
        with patch.object(ops, "_gcloud") as gcloud:
            with self.assertRaises(phase1_rollout.RolloutError):
                ops.legacy_health_get(
                    "https://phase1-cert-111111111111---process-user-other.run.app",
                    SERVICE_URL,
                )
        gcloud.assert_not_called()

    def test_authenticated_redirect_handler_never_follows(self):
        handler = phase1_rollout._NoRedirect()
        self.assertIsNone(
            handler.redirect_request(None, None, 302, "moved", {}, "https://evil")
        )

    def test_queue_tag_and_traffic_commands_are_closed_argument_arrays(self):
        ops = phase1_rollout.SubprocessOps(ROOT, "1" * 40)
        calls = []

        def fake_gcloud(args, timeout=600):
            calls.append((list(args), timeout))
            return ""

        with patch.object(ops, "_gcloud", side_effect=fake_gcloud):
            ops.pause_queue()
            ops.resume_queue()
            ops.add_cert_tag("phase1-cert-111111111111", CANDIDATE)
            ops.remove_cert_tag("phase1-cert-111111111111")
            ops.promote(CANDIDATE, OLD_REVISION)
            ops.rollback(OLD_REVISION, CANDIDATE)

        self.assertEqual(
            [
                ["tasks", "queues", "pause", "graph-process-user", "--location", "us-central1"],
                ["tasks", "queues", "resume", "graph-process-user", "--location", "us-central1"],
                ["run", "services", "update-traffic", "process-user", "--region", "us-central1", "--update-tags", f"phase1-cert-111111111111={CANDIDATE}"],
                ["run", "services", "update-traffic", "process-user", "--region", "us-central1", "--remove-tags", "phase1-cert-111111111111"],
                ["run", "services", "update-traffic", "process-user", "--region", "us-central1", "--update-tags", f"release-a={CANDIDATE}", "--to-revisions", f"{CANDIDATE}=100,{OLD_REVISION}=0"],
                ["run", "services", "update-traffic", "process-user", "--region", "us-central1", "--update-tags", f"release-a={OLD_REVISION}", "--to-revisions", f"{OLD_REVISION}=100,{CANDIDATE}=0"],
            ],
            [args for args, _ in calls],
        )


if __name__ == "__main__":
    unittest.main()
