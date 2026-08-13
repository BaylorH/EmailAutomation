import ast
import importlib.util
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

    def preflight(self):
        self.events.append("preflight")

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
    def make_rollout(self, ops):
        sleeps = []
        rollout = phase1_rollout.Phase1Rollout(
            ops=ops,
            head_sha="1234567890abcdef1234567890abcdef12345678",
            sleeper=sleeps.append,
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
        self.assertEqual("RUNNING", ops.queue["state"])

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
        ops.fail_legacy_health_after = 2
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
        ops.fail_prerequisites_after = 1
        rollout, _ = self.make_rollout(ops)
        with self.assertRaises(phase1_rollout.RolloutError):
            rollout.apply()
        self.assertNotIn("promote", ops.events)
        self.assertEqual(service(), ops.service)
        self.assertEqual("PAUSED", ops.queue["state"])

    def test_prerequisite_drift_during_cleanup_keeps_queue_paused(self):
        ops = FakeOps()
        ops.identity["revision"] = OLD_REVISION
        ops.fail_prerequisites_after = 1
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
        ops.fail_prerequisites_after = 3
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


class AdapterContractTests(unittest.TestCase):
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
