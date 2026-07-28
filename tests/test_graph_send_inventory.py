import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = REPO_ROOT / "docs" / "release-safety" / "outbound-send-surface-inventory.json"
SOURCE_BASELINE_PATH = (
    REPO_ROOT
    / "docs"
    / "release-safety"
    / "outbound-send-source-baseline.json"
)

GRAPH_SEND_PATTERN = re.compile(
    r"/me/(?:sendMail|messages/\{[^}]+\}/(?:reply|send|createReply|createReplyAll))"
    r"|graph\.microsoft\.com/v1\.0/me/(?:sendMail|messages/.*/(?:reply|send))"
)
FINAL_SEND_LITERAL_PATTERN = re.compile(
    r"/me/sendMail"
    r"|/me/messages/(?:\\{[^}]+\\}|[^\\s\"']+)/send"
)

IGNORED_PATH_PARTS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    "venv",
}

LEGACY_EMAIL_OPERATIONS_FLAG = "SITESIFT_ENABLE_LEGACY_EMAIL_OPERATIONS"
LEGACY_FINAL_SEND_PATTERN = re.compile(
    r"/me/sendMail|/send\b|/reply\b"
)


def _repo_python_files():
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        if any(part in IGNORED_PATH_PARTS for part in rel.parts):
            continue
        if rel.parts[0] == "tests":
            continue
        yield rel


def _workflow_files():
    workflows_dir = REPO_ROOT / ".github" / "workflows"
    if not workflows_dir.exists():
        return []
    return sorted(
        path.relative_to(REPO_ROOT)
        for path in workflows_dir.glob("*")
        if path.suffix in {".yml", ".yaml"}
    )


def _semantic_ast_sha256(source: str) -> str:
    normalized = ast.dump(
        ast.parse(source),
        annotate_fields=True,
        include_attributes=False,
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _functions_with_legacy_final_send_literals(relative_path: str):
    tree = ast.parse(
        (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    )
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    functions = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Constant, ast.JoinedStr)):
            continue
        rendered = ast.unparse(node)
        if not LEGACY_FINAL_SEND_PATTERN.search(rendered):
            continue
        parent = node
        while parent is not None and not isinstance(
            parent,
            (ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            parent = parents.get(parent)
        if parent is not None:
            functions[parent.name] = parent
    return tree, functions


def _first_executable_statement(function):
    body = list(function.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body[0] if body else None


class GraphSendInventoryTests(unittest.TestCase):
    def test_outbound_source_review_baseline_covers_every_production_python_ast(self):
        self.assertTrue(
            SOURCE_BASELINE_PATH.exists(),
            "A reviewed production-source baseline is required.",
        )
        baseline = json.loads(SOURCE_BASELINE_PATH.read_text())
        self.assertEqual(baseline.get("schemaVersion"), 1)
        self.assertEqual(
            baseline.get("algorithm"),
            "python-ast-dump-sha256-v1",
        )
        self.assertEqual(
            baseline.get("pythonVersion"),
            ".".join(str(part) for part in sys.version_info[:3]),
        )
        expected = baseline.get("files")
        self.assertIsInstance(expected, dict)

        production_files = {
            str(relative_path): _semantic_ast_sha256(
                (REPO_ROOT / relative_path).read_text(
                    encoding="utf-8",
                    errors="strict",
                )
            )
            for relative_path in _repo_python_files()
        }
        self.assertEqual(
            set(expected),
            set(production_files),
            "Production Python files changed; every addition/removal requires "
            "an explicit outbound-source baseline review.",
        )
        self.assertEqual(
            expected,
            production_files,
            "Production Python semantics changed; review outbound effects and "
            "update the baseline deliberately.",
        )

    def test_source_review_baseline_catches_syntax_independent_bypass_shapes(self):
        reviewed_source = "def reviewed():\n    return 'safe'\n"
        reviewed_hash = _semantic_ast_sha256(reviewed_source)
        bypasses = {
            "split_endpoint": (
                'post(base + "/me/messages/" + draft_id + "/" + "send")'
            ),
            "mapping_percent": (
                'post("%(base)s/me/messages/%(id)s/%(op)s" % '
                '{"base": base, "id": draft_id, "op": "send"})'
            ),
            "match_case": (
                'match action:\n'
                '    case "send":\n'
                '        post(f"{base}/me/messages/{draft_id}/{action}")'
            ),
            "provider_alias": (
                "raw = provider.send_draft\n"
                'raw("draft-id")'
            ),
            "getattr_alias": (
                'getattr(provider, "send_" + "draft")("draft-id")'
            ),
            "late_bound_closure": (
                "def invoke():\n"
                '    raw("draft-id")\n'
                "raw = provider.send_draft"
            ),
        }

        for name, bypass in bypasses.items():
            with self.subTest(name=name):
                self.assertNotEqual(
                    reviewed_hash,
                    _semantic_ast_sha256(
                        f"{reviewed_source}\n{bypass}\n"
                    ),
                )

    def test_source_review_baseline_ignores_comments_and_formatting_only(self):
        compact = "def reviewed(value):\n    return value + 1\n"
        reformatted = (
            "# explanatory comment\n\n"
            "def reviewed(\n"
            "    value,\n"
            "):\n"
            "    return (\n"
            "        value\n"
            "        + 1\n"
            "    )\n"
        )

        self.assertEqual(
            _semantic_ast_sha256(compact),
            _semantic_ast_sha256(reformatted),
        )

    def test_inventory_exists_and_covers_every_graph_send_surface(self):
        self.assertTrue(
            INVENTORY_PATH.exists(),
            "docs/release-safety/outbound-send-surface-inventory.json must document every Graph send surface",
        )

        inventory = json.loads(INVENTORY_PATH.read_text())
        registered_paths = {
            entry["path"]
            for entry in inventory.get("sendSurfaces", [])
            if entry.get("path")
        }

        discovered_paths = set()
        for rel in _repo_python_files():
            text = (REPO_ROOT / rel).read_text(errors="ignore")
            if GRAPH_SEND_PATTERN.search(text):
                discovered_paths.add(str(rel))

        self.assertEqual(
            discovered_paths,
            registered_paths,
            "Graph send/reply endpoints changed; update the outbound send inventory and safety notes.",
        )

    def test_inventory_marks_each_surface_policy_status(self):
        inventory = json.loads(INVENTORY_PATH.read_text())
        allowed_statuses = {
            "gateway",
            "guarded",
            "provider",
            "legacy_disabled",
            "legacy_script",
        }

        for entry in inventory.get("sendSurfaces", []):
            with self.subTest(path=entry.get("path")):
                self.assertIn(entry.get("policyStatus"), allowed_statuses)
                self.assertTrue(entry.get("trigger"))
                self.assertTrue(entry.get("risk"))
                self.assertTrue(entry.get("nextGate"))

    def test_legacy_email_operations_inventory_matches_runtime_quarantine(self):
        inventory = json.loads(INVENTORY_PATH.read_text())
        entries = {
            entry["path"]: entry
            for entry in inventory.get("sendSurfaces", [])
        }

        self.assertEqual(
            "legacy_disabled",
            entries["email_automation/email_operations.py"]["policyStatus"],
        )

    def test_active_send_surfaces_reference_shared_body_policy(self):
        inventory = json.loads(INVENTORY_PATH.read_text())
        guarded_paths = [
            entry["path"]
            for entry in inventory.get("sendSurfaces", [])
            if entry.get("policyStatus") == "guarded"
        ]

        for path in guarded_paths:
            with self.subTest(path=path):
                text = (REPO_ROOT / path).read_text(errors="ignore")
                self.assertIn(
                    "validate_outbound_body",
                    text,
                    f"{path} is an active Graph send surface and must use the shared body policy",
                )

    def test_production_configs_do_not_enable_legacy_email_operations(self):
        production_configs = (
            *_workflow_files(),
            Path("deploy/cloudrun-job.yaml"),
            Path("deploy/cloudrun-service.yaml"),
            Path("scripts/deploy_process_user.sh"),
        )
        for rel in production_configs:
            with self.subTest(path=str(rel)):
                text = (REPO_ROOT / rel).read_text(errors="ignore")
                self.assertNotIn(
                    LEGACY_EMAIL_OPERATIONS_FLAG,
                    text,
                    "Production workflows must not opt into legacy direct-Graph send helpers",
                )

    def test_every_exempt_legacy_raw_final_send_function_has_an_executable_gate(self):
        expected_functions = {
            "scheduler_runner.py": {
                "send_remaining_questions_email",
                "send_closing_email",
                "send_new_property_email",
                "send_and_index_email",
                "send_weekly_email",
                "process_replies",
            },
            "noPopup_signin_emails_to_excel.py": {
                "send_weekly_email",
                "process_replies",
            },
        }
        for relative_path, expected_names in expected_functions.items():
            _tree, functions = _functions_with_legacy_final_send_literals(
                relative_path
            )
            with self.subTest(path=relative_path):
                self.assertEqual(set(functions), expected_names)
            for function_name, function in functions.items():
                statement = _first_executable_statement(function)
                with self.subTest(
                    path=relative_path,
                    function=function_name,
                ):
                    self.assertIsInstance(statement, ast.Expr)
                    call = statement.value
                    self.assertIsInstance(call, ast.Call)
                    self.assertIsInstance(call.func, ast.Name)
                    self.assertEqual(
                        call.func.id,
                        "_require_legacy_email_operations_enabled",
                    )
                    self.assertEqual(
                        [ast.literal_eval(argument) for argument in call.args],
                        [function_name],
                    )

    def test_nopopup_default_gate_precedes_side_effectful_imports(self):
        tree, _functions = _functions_with_legacy_final_send_literals(
            "noPopup_signin_emails_to_excel.py"
        )
        module_gates = [
            node
            for node in tree.body
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id
            == "_require_legacy_email_operations_enabled"
        ]
        self.assertEqual(len(module_gates), 1)
        module_gate = module_gates[0]
        side_effectful_imports = [
            node
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            and not (
                isinstance(node, ast.Import)
                and [alias.name for alias in node.names] == ["os"]
            )
        ]

        self.assertEqual(
            [ast.literal_eval(argument) for argument in module_gate.value.args],
            ["module_import"],
        )
        self.assertTrue(side_effectful_imports)
        self.assertLess(
            module_gate.lineno,
            min(node.lineno for node in side_effectful_imports),
        )

    def test_nopopup_default_execution_stops_at_the_legacy_gate(self):
        environment = os.environ.copy()
        environment.pop(LEGACY_EMAIL_OPERATIONS_FLAG, None)

        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "noPopup_signin_emails_to_excel.py"),
            ],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LegacyStandaloneEffectsDisabled", result.stderr)
        self.assertIn("module_import is a legacy direct-Graph helper", result.stderr)
        self.assertNotIn("FIREBASE_API_KEY is not set", result.stderr)

    def test_nopopup_helpers_recheck_gate_before_requests_io_in_compiled_quarantine(self):
        source = (
            REPO_ROOT / "noPopup_signin_emails_to_excel.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        selected_names = {
            "LegacyStandaloneEffectsDisabled",
            "_require_legacy_email_operations_enabled",
            "send_weekly_email",
            "process_replies",
        }
        selected_nodes = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name)
                and target.id == "LEGACY_EMAIL_OPERATIONS_FLAG"
                for target in node.targets
            ):
                selected_nodes.append(node)
            elif isinstance(node, (ast.ClassDef, ast.FunctionDef)) and (
                node.name in selected_names
            ):
                selected_nodes.append(node)

        quarantined_module = ast.Module(
            body=selected_nodes,
            type_ignores=[],
        )
        ast.fix_missing_locations(quarantined_module)
        namespace = {"os": os}
        exec(
            compile(
                quarantined_module,
                "noPopup_signin_emails_to_excel.py",
                "exec",
            ),
            namespace,
        )
        requests_module = mock.Mock()
        namespace["requests"] = requests_module
        namespace["headers"] = {"Authorization": "Bearer test"}

        with mock.patch.dict(
            os.environ,
            {LEGACY_EMAIL_OPERATIONS_FLAG: "1"},
            clear=False,
        ), mock.patch.object(requests_module, "get") as graph_get, \
             mock.patch.object(requests_module, "post") as graph_post, \
             mock.patch.object(requests_module, "patch") as graph_patch:
            self.assertIsNone(
                namespace["_require_legacy_email_operations_enabled"](
                    "controlled_probe"
                )
            )
            graph_get.assert_not_called()
            graph_post.assert_not_called()
            graph_patch.assert_not_called()

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(LEGACY_EMAIL_OPERATIONS_FLAG, None)
            cases = (
                (namespace["send_weekly_email"], (["broker@example.com"],)),
                (namespace["process_replies"], ()),
            )
            for function, arguments in cases:
                with self.subTest(function=function.__name__), \
                     mock.patch.object(requests_module, "get") as graph_get, \
                     mock.patch.object(requests_module, "post") as graph_post, \
                     mock.patch.object(requests_module, "patch") as graph_patch:
                    with self.assertRaises(
                        namespace["LegacyStandaloneEffectsDisabled"]
                    ):
                        function(*arguments)
                    graph_get.assert_not_called()
                    graph_post.assert_not_called()
                    graph_patch.assert_not_called()

    def test_production_code_does_not_call_raw_email_provider_senders_directly(self):
        offenders = []
        for rel in _repo_python_files():
            if str(rel) == "email_automation/service_providers.py":
                continue
            tree = ast.parse(
                (REPO_ROOT / rel).read_text(
                    encoding="utf-8",
                    errors="ignore",
                )
            )
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                raw_provider_call = (
                    isinstance(function, ast.Attribute)
                    and function.attr
                    in {
                        "send_draft",
                        "send_new_message",
                        "reply_to_message",
                    }
                )
                raw_provider_constructor = (
                    (
                        isinstance(function, ast.Name)
                        and function.id == "RealEmailProvider"
                    )
                    or (
                        isinstance(function, ast.Attribute)
                        and function.attr == "RealEmailProvider"
                    )
                )
                raw_provider_lookup = (
                    (
                        isinstance(function, ast.Name)
                        and function.id == "get_provider"
                    )
                    or (
                        isinstance(function, ast.Attribute)
                        and function.attr == "get_provider"
                    )
                ) and bool(node.args) and (
                    isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "email"
                )
                if (
                    raw_provider_call
                    or raw_provider_constructor
                    or raw_provider_lookup
                ):
                    offenders.append(
                        f"{rel}:{node.lineno}:{ast.unparse(node)}"
                    )

        self.assertEqual(
            [],
            offenders,
            "Production code should route sends through policy-aware modules, not raw RealEmailProvider helpers.",
        )

    def test_active_send_lanes_have_no_direct_irreversible_graph_send(self):
        inventory = json.loads(INVENTORY_PATH.read_text())
        entries = {
            entry["path"]: entry
            for entry in inventory.get("sendSurfaces", [])
        }
        active_paths = {
            path
            for path, entry in entries.items()
            if entry.get("policyStatus") == "guarded"
        }
        offenders = []

        for relative_path in sorted(active_paths):
            path = REPO_ROOT / relative_path
            source = path.read_text(encoding="utf-8")
            self.assertIn(
                "execute_graph_draft_final_send",
                source,
                f"{relative_path} must route final sends through the adapter",
            )
            for match in FINAL_SEND_LITERAL_PATTERN.finditer(source):
                offenders.append(
                    f"{relative_path}:{source.count(chr(10), 0, match.start()) + 1}"
                )

        self.assertEqual(
            [],
            offenders,
            "Active Graph final sends must route through the single "
            "GraphFinalSendAdapter: " + ", ".join(offenders),
        )

    def test_no_unapproved_final_send_literal_can_hide_behind_an_alternate_http_call(self):
        inventory = json.loads(INVENTORY_PATH.read_text())
        explicitly_excluded = {
            entry["path"]
            for entry in inventory.get("sendSurfaces", [])
            if entry.get("policyStatus")
            in {"legacy_disabled", "legacy_script", "provider"}
        }
        allowed = {
            "email_automation/graph_final_send.py",
            *explicitly_excluded,
        }
        offenders = []

        for rel in _repo_python_files():
            relative_path = str(rel)
            if relative_path in allowed:
                continue
            source = (REPO_ROOT / rel).read_text(
                encoding="utf-8",
                errors="ignore",
            )
            for match in FINAL_SEND_LITERAL_PATTERN.finditer(source):
                offenders.append(
                    f"{relative_path}:"
                    f"{source.count(chr(10), 0, match.start()) + 1}"
                )

        self.assertEqual(
            [],
            offenders,
            "Final Graph endpoint literals are allowed only in the single "
            "adapter or explicitly disabled legacy/provider files; this "
            "catches .post, .request, aliases, and wrapper call shapes: "
            + ", ".join(offenders),
        )

    def test_pending_response_retries_delegate_to_the_guarded_processing_lane(self):
        source = (
            REPO_ROOT / "email_automation" / "pending_responses.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "processing_module.send_reply_in_thread",
            source,
        )
        self.assertNotRegex(source, FINAL_SEND_LITERAL_PATTERN)

    def test_every_active_adapter_call_supplies_exact_authority_and_stable_identity_inputs(self):
        expected_call_counts = {
            "email_automation/email.py": 2,
            "email_automation/processing.py": 1,
            "email_automation/followup.py": 1,
        }
        required_keywords = {
            "user_id",
            "client_id",
            "run_id",
            "effect_type",
            "effect_key",
            "content",
            "draft_id",
            "headers",
            "http_post",
        }

        for relative_path, expected_count in expected_call_counts.items():
            source = (REPO_ROOT / relative_path).read_text(
                encoding="utf-8"
            )
            tree = ast.parse(source)
            calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "execute_graph_draft_final_send"
            ]
            with self.subTest(path=relative_path):
                self.assertEqual(len(calls), expected_count)
                for call in calls:
                    keywords = {
                        keyword.arg: keyword.value
                        for keyword in call.keywords
                    }
                    self.assertTrue(
                        required_keywords
                        <= set(keywords),
                    )
                    self.assertEqual(
                        ast.unparse(keywords["http_post"]),
                        "requests.post",
                    )
                    self.assertEqual(
                        ast.unparse(keywords["run_id"]),
                        "resolve_effect_run_id()",
                    )

    def test_every_outbox_production_caller_supplies_a_business_effect_key(self):
        source = (
            REPO_ROOT / "email_automation" / "email.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        call_names = {
            "send_and_index_email",
            "_send_outbox_as_reply",
        }
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in call_names
        ]

        self.assertEqual(len(calls), 5)
        for call in calls:
            with self.subTest(line=call.lineno):
                self.assertIn(
                    "effect_key",
                    {keyword.arg for keyword in call.keywords},
                )

    def test_processing_and_followup_effect_keys_bind_business_identity(self):
        processing_source = (
            REPO_ROOT / "email_automation" / "processing.py"
        ).read_text(encoding="utf-8")
        followup_source = (
            REPO_ROOT / "email_automation" / "followup.py"
        ).read_text(encoding="utf-8")
        email_source = (
            REPO_ROOT / "email_automation" / "email.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'effect_key=f"inbox-reply:{thread_id}:{current_msg_id}"',
            processing_source,
        )
        self.assertIn(
            'effect_key=f"followup:{thread_id}:{followup_index}"',
            followup_source,
        )
        self.assertIn(
            'combined_effect_key = "combined:" + ",".join(',
            email_source,
        )


if __name__ == "__main__":
    unittest.main()
