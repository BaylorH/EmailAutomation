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


OLD_REVISION = "process-user-00097-yus"
CANDIDATE = "process-user-stage-1234567890ab"
OLD_IMAGE = (
    "us-central1-docker.pkg.dev/email-automation-cache/cloud-run-source-deploy/"
    "process-user@sha256:cd49af55848b7d9fe481d501e087626240d9dc273d0dee663f5c82e04fb62780"
)
CANDIDATE_IMAGE = (
    "us-central1-docker.pkg.dev/email-automation-cache/cloud-run-source-deploy/"
    "process-user@sha256:" + "e" * 64
)
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
                "env": [
                    {"name": "FIREBASE_BUCKET", "value": "bucket"},
                    {"name": "OPENAI_API_KEY", "valueFrom": {
                        "secretKeyRef": {"name": "OPENAI_API_KEY", "key": "latest"}
                    }},
                ],
            }],
        },
        "status": {
            "imageDigest": image,
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


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
        self.identity = {
            "status": "ok", "service": "process-user", "revision": CANDIDATE
        }
        self.fail_remove = False
        self.fail_promote = False
        self.fail_prerequisites = False
        self.fail_prerequisites_after = None
        self.fail_resume_after_change = False
        self.legacy_health_calls = 0
        self.fail_legacy_health_after = None
        self.identity_interrupt = False
        self.prerequisite_calls = 0
        self.lock_held = False
        self.lock_nonce = None
        self.lock_assertions = 0
        self.lose_lock_on_assert = None
        self.fail_acquire_lock = False
        self.acquire_error = None

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
        if self.fail_prerequisites or (
            self.fail_prerequisites_after is not None
            and self.prerequisite_calls > self.fail_prerequisites_after
        ):
            raise phase1_rollout.RolloutError("prerequisite failed")

    def verify_service_access(self, topology):
        self.events.append("service-access")
        if topology.service_url != SERVICE_URL:
            raise phase1_rollout.RolloutError("wrong service URL")

    def artifact_image(self):
        self.events.append("artifact")
        return CANDIDATE_IMAGE

    def get_service(self):
        self.events.append("service")
        return json.loads(json.dumps(self.service))

    def get_revision(self, name):
        self.events.append(f"revision:{name}")
        source = self.candidate_revision if name == CANDIDATE else self.old_revision
        return json.loads(json.dumps(source))

    def get_queue(self):
        self.events.append("queue")
        return json.loads(json.dumps(self.queue))

    def list_tasks(self):
        self.events.append("tasks")
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

    def promote(self, candidate, old):
        self.events.append("promote")
        if self.fail_promote:
            raise phase1_rollout.RolloutError("promote failed")
        self.service = service(candidate, candidate)

    def rollback(self, old, candidate):
        self.events.append("rollback")
        self.service = service()

    def identity_get(self, base_url, audience):
        self.events.append(f"identity:{base_url}|aud:{audience}")
        if self.identity_interrupt:
            raise KeyboardInterrupt()
        return dict(self.identity)

    def legacy_health_get(self, base_url, audience):
        self.legacy_health_calls += 1
        self.events.append(f"legacy:{base_url}|aud:{audience}")
        if (
            self.fail_legacy_health_after is not None
            and self.legacy_health_calls > self.fail_legacy_health_after
        ):
            return {"status": "wrong"}
        return {"status": "ok"}

    def unauthenticated_status(self, base_url, path):
        self.events.append(f"unauth:{base_url}{path}")
        return 403


class ValidatorTests(unittest.TestCase):
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

    def test_candidate_must_be_ready_and_exact_config_clone(self):
        phase1_rollout.validate_candidate(
            revision(CANDIDATE, CANDIDATE_IMAGE),
            revision(OLD_REVISION, OLD_IMAGE),
            CANDIDATE, CANDIDATE_IMAGE,
        )
        changed = revision(CANDIDATE, CANDIDATE_IMAGE)
        changed["spec"]["containerConcurrency"] = 2
        with self.assertRaises(phase1_rollout.RolloutError):
            phase1_rollout.validate_candidate(
                changed, revision(OLD_REVISION, OLD_IMAGE),
                CANDIDATE, CANDIDATE_IMAGE,
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
                        changed, baseline, CANDIDATE, CANDIDATE_IMAGE
                    )


class StateMachineTests(unittest.TestCase):
    def make_rollout(self, ops, nonce="a" * 64):
        sleeps = []
        rollout = phase1_rollout.Phase1Rollout(
            ops=ops,
            head_sha="1234567890abcdef1234567890abcdef12345678",
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
        tag_identity = next(
            event for event in ops.events
            if event.startswith("identity:https://phase1-cert-")
        )
        self.assertTrue(
            tag_identity.endswith("|aud:https://process-user-example.run.app")
        )
        release_identity = next(
            event for event in ops.events
            if event.startswith("identity:https://release-a---")
        )
        self.assertTrue(
            release_identity.endswith("|aud:https://process-user-example.run.app")
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
        self.assertEqual("RUNNING", ops.queue["state"])

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
        ops.identity["revision"] = OLD_REVISION
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
            head_sha="1234567890abcdef1234567890abcdef12345678",
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
            head_sha="1234567890abcdef1234567890abcdef12345678",
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
                    head_sha="1234567890abcdef1234567890abcdef12345678",
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

    def test_wrong_identity_removes_tag_and_resumes_without_traffic_mutation(self):
        ops = FakeOps()
        ops.identity["revision"] = OLD_REVISION
        rollout, _ = self.make_rollout(ops)
        with self.assertRaises(phase1_rollout.RolloutError):
            rollout.apply()
        self.assertIn("tag:remove", ops.events)
        self.assertNotIn("promote", ops.events)
        self.assertNotIn("rollback", ops.events)
        self.assertEqual("RUNNING", ops.queue["state"])

    def test_cleanup_ambiguity_leaves_queue_paused(self):
        ops = FakeOps()
        ops.identity["revision"] = OLD_REVISION
        ops.fail_remove = True
        rollout, _ = self.make_rollout(ops)
        with self.assertRaisesRegex(phase1_rollout.RolloutError, "MANUAL_RECOVERY"):
            rollout.apply()
        self.assertEqual("PAUSED", ops.queue["state"])

    def test_failed_old_health_during_cleanup_keeps_queue_paused(self):
        ops = FakeOps()
        ops.identity["revision"] = OLD_REVISION
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
        ops.identity["revision"] = OLD_REVISION
        ops.fail_prerequisites_after = 2
        rollout, _ = self.make_rollout(ops)
        with self.assertRaisesRegex(phase1_rollout.RolloutError, "MANUAL_RECOVERY"):
            rollout.apply()
        self.assertEqual("PAUSED", ops.queue["state"])

    def test_keyboard_interrupt_after_tag_runs_cleanup_and_resumes_old(self):
        ops = FakeOps()
        ops.identity_interrupt = True
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
            if pause_calls["count"] > 3:
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
        self.assertNotIn("/process-user", string_values)
        self.assertFalse(any(value.startswith("/process-user?") for value in string_values))

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

    def test_user_identity_token_omits_custom_audience_but_request_stays_split(self):
        ops = phase1_rollout.SubprocessOps(ROOT, "1" * 40)
        calls = []

        def fake_gcloud(args, timeout=600):
            calls.append(list(args))
            return "opaque-token"

        with patch.object(ops, "_gcloud", side_effect=fake_gcloud), patch.object(
            ops, "_http_json", return_value={"status": "ok"}
        ) as http:
            ops.identity_get(
                "https://phase1-cert-111111111111---process-user-example.run.app",
                SERVICE_URL,
            )

        self.assertEqual([["auth", "print-identity-token"]], calls)
        http.assert_called_once_with(
            "https://phase1-cert-111111111111---process-user-example.run.app/health/identity/v1",
            token="opaque-token",
        )

    def test_health_target_cannot_cross_the_validated_service_origin(self):
        ops = phase1_rollout.SubprocessOps(ROOT, "1" * 40)
        with patch.object(ops, "_gcloud") as gcloud:
            with self.assertRaises(phase1_rollout.RolloutError):
                ops.identity_get(
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
