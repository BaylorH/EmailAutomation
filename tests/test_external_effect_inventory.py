"""Every AI and public-Drive effect in deployable source, enumerated.

Task 7F of the production automation certification plan. Delivery converged in
Tasks 6 and 7A-7D; this module opens the other two effect families a
certification run must not cause: a real model request, and a real public Drive
permission.

Like `test_message_transport`, this is SOURCE-LEVEL and imports nothing from
`email_automation`. The property being pinned - "no deployable module reaches a
provider except through the request-scoped adapter" - is a structural invariant
over the import graph. A runtime test proves it only for the paths it happens to
exercise and can be satisfied by a mock; an AST sweep cannot be evaded by one.
`email_automation/clients.py` also builds `openai.OpenAI(...)` at import time
(backlog #84), so importing the business logic here would construct a real
provider client just to enumerate it.

WHAT THIS PINS TODAY, stated plainly: the adapters exist in
`automation_runtime.py`, and NINE call sites still reach a provider directly -
four AI families and five public-Drive permission creates. Those five are exactly
where the plan said they would be, which is worth recording because Task 6's
equivalent premise turned out to be wrong. Each later step of Task 7F routes one
family and is EXPECTED to fail the corresponding constant here; that failure is
the alarm that an effect moved, and the constant is updated in the same commit.
"""

from pathlib import Path
import ast
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]

# The sanctioned adapters. These are the ONLY places a provider may be reached.
SANCTIONED_ADAPTER = "email_automation/automation_runtime.py"

# Deployable source. Scripts under scripts/ are operator tooling, run by hand and
# never imported by the automation lane, so they are enumerated separately rather
# than held to the same rule - see test_operator_scripts_are_listed_not_ignored.
SCRIPT_PREFIX = "scripts/"
SKIP_PARTS = {".git", "tests", "__pycache__", "node_modules", ".venv", "venv"}

# --- the pinned baseline --------------------------------------------------
#
# Each entry is (module, owning function). Line numbers are deliberately absent:
# ownership by enclosing function is the stable fact, and a line number would
# make this file churn on every unrelated edit.

# Step 2 of Task 7F routed the three sites reachable from the automation lane.
# What remains is deliberately NOT routed:
#   * service_providers.py IS the raw provider adapter - routing it would mean
#     routing a provider through itself. It is unreachable from the automation
#     lane (production imports only get_drive_service) and is held to the same
#     enumerated-bypass rule as its send helpers.
#   * scheduler_runner.py is a top-level script, imported by nothing.
UNROUTED_AI_SITES = {
    ("email_automation/service_providers.py", "chat_completion"),
    ("email_automation/service_providers.py", "upload_file"),
    ("scheduler_runner.py", "propose_sheet_updates"),
    ("scheduler_runner.py", "upload_pdf_user_data"),
}

# Routed in Task 7F step 2. Each reaches the provider only through the runtime's
# AI transport, so a certification runtime refuses before a request is built.
ROUTED_AI_SITES = {
    ("email_automation/ai_processing.py", "propose_sheet_updates"),
    ("email_automation/column_config.py", "_ai_match_columns"),
    ("email_automation/file_handling.py", "upload_pdf_user_data"),
}

# The plan predicted five public-Drive permission sites across four files. Unlike
# Task 6's premise, this one is CORRECT, and pinning it here is what makes that
# checkable rather than remembered.
UNROUTED_DRIVE_SITES = {
    ("email_automation/file_handling.py", "upload_pdf_to_drive"),
    ("email_automation/file_handling.py", "upload_property_image_to_drive"),
    ("email_automation/service_providers.py", "set_public_permission"),
    ("email_automation/utils.py", "_upload_logo_to_drive"),
    ("scheduler_runner.py", "upload_pdf_to_drive"),
}

OPERATOR_SCRIPT_SITES = {
    ("scripts/verify_production.py", "check_ai_extraction"),
}


def _source(relative_path):
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8", errors="ignore")


def _deployable_files():
    for path in REPO_ROOT.rglob("*.py"):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if SKIP_PARTS & set(Path(relative).parts):
            continue
        yield relative


def _enclosing_functions(tree):
    owner = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                owner.setdefault(child, node.name)
    return owner


def _attribute_chain(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return ".".join(reversed(parts))


def find_provider_effects(relative_path):
    """Return (kind, owner) for every direct provider effect in one module.

    Kinds: ``ai_response``, ``ai_chat``, ``ai_upload``, ``drive_permission``.
    """
    source = _source(relative_path)
    if not any(
        token in source
        for token in ("responses.create", "completions.create", "files.create", "permissions()")
    ):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    owner = _enclosing_functions(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        chain = _attribute_chain(node.func)
        kind = None
        if chain.endswith("responses.create"):
            kind = "ai_response"
        elif chain.endswith("completions.create"):
            kind = "ai_chat"
        elif chain.endswith("files.create"):
            kind = "ai_upload"
        elif node.func.attr == "create":
            # ``service.permissions().create(...)`` - the receiver is a CALL, so the
            # plain attribute chain above cannot see it.
            receiver = node.func.value
            if (
                isinstance(receiver, ast.Call)
                and isinstance(receiver.func, ast.Attribute)
                and receiver.func.attr == "permissions"
            ):
                kind = "drive_permission"
        if kind:
            found.append((kind, owner.get(node, "<module>")))
    return found


def _sites(predicate):
    sites = set()
    for relative in _deployable_files():
        for kind, function_name in find_provider_effects(relative):
            if predicate(kind):
                sites.add((relative, function_name))
    return sites


class ProviderEffectInventoryTests(unittest.TestCase):
    """The baseline Task 7F moves, pinned before it moves."""

    def test_ai_provider_sites_are_exactly_the_known_set(self):
        actual = _sites(lambda kind: kind.startswith("ai_"))
        expected = UNROUTED_AI_SITES | OPERATOR_SCRIPT_SITES | {
            (SANCTIONED_ADAPTER, "create_response"),
            (SANCTIONED_ADAPTER, "create_chat_completion"),
            (SANCTIONED_ADAPTER, "upload_file"),
        }
        self.assertEqual(
            actual,
            expected,
            "the AI provider surface changed; if a step of Task 7F routed one, "
            "update UNROUTED_AI_SITES in the same commit - and if a NEW direct "
            "call appeared, that is the alarm",
        )

    def test_routed_ai_sites_no_longer_reach_a_provider_directly(self):
        """The lanes Task 7F step 2 converged."""
        actual = _sites(lambda kind: kind.startswith("ai_"))
        for site in ROUTED_AI_SITES:
            with self.subTest(site=site):
                self.assertNotIn(site, actual)

    def test_every_routed_ai_site_resolves_through_the_runtime_transport(self):
        """Structural: the call goes through ai_for, not a module-level client."""
        for module, function_name in ROUTED_AI_SITES:
            with self.subTest(module=module, function=function_name):
                source = _source(module)
                tree = ast.parse(source)
                owner = _enclosing_functions(tree)
                uses_resolver = any(
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "ai_for"
                    and owner.get(node) == function_name
                    for node in ast.walk(tree)
                )
                self.assertTrue(
                    uses_resolver,
                    f"{module}:{function_name} no longer resolves its AI transport "
                    "through ai_for",
                )

    def test_the_unrouted_remainder_is_unreachable_rather_than_merely_listed(self):
        """service_providers is the raw adapter; scheduler_runner is a script.

        Neither is reachable from the automation lane, which is what makes
        leaving them unrouted defensible rather than an omission.
        """
        remaining_modules = {module for module, _fn in UNROUTED_AI_SITES}
        self.assertEqual(
            remaining_modules,
            {"email_automation/service_providers.py", "scheduler_runner.py"},
        )
        for relative in _deployable_files():
            if relative in remaining_modules or relative.startswith(SCRIPT_PREFIX):
                continue
            try:
                tree = ast.parse(_source(relative))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.rsplit(".", 1)[-1] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.rsplit(".", 1)[-1]]
                if "scheduler_runner" in names:
                    self.fail(f"{relative} imports the scheduler script")

    def test_public_drive_permission_sites_are_exactly_the_known_set(self):
        actual = _sites(lambda kind: kind == "drive_permission")
        expected = UNROUTED_DRIVE_SITES | {(SANCTIONED_ADAPTER, "publish")}
        self.assertEqual(
            actual,
            expected,
            "the public-Drive permission surface changed; update "
            "UNROUTED_DRIVE_SITES in the same commit that routes one",
        )

    def test_the_plan_predicted_the_drive_surface_correctly(self):
        """Worth asserting, because Task 6's equivalent premise was wrong.

        The plan states five permission call sites across four files. Task 6's
        plan text was confidently wrong about the delivery boundary, so a stated
        premise is not evidence until it is checked.
        """
        self.assertEqual(len(UNROUTED_DRIVE_SITES), 5)
        self.assertEqual(len({module for module, _fn in UNROUTED_DRIVE_SITES}), 4)

    def test_the_adapters_exist_and_are_the_only_sanctioned_reach(self):
        adapters = _sites(lambda kind: True)
        adapter_sites = {site for site in adapters if site[0] == SANCTIONED_ADAPTER}
        self.assertEqual(
            {name for _module, name in adapter_sites},
            {"create_response", "create_chat_completion", "upload_file", "publish"},
        )

    def test_operator_scripts_are_listed_not_ignored(self):
        """A script that reaches a provider is still a surface.

        It is held to a different rule - imported by nothing, run by hand - but
        omitting it entirely would let the inventory claim a completeness it does
        not have.
        """
        for module, _function in OPERATOR_SCRIPT_SITES:
            with self.subTest(module=module):
                self.assertTrue(module.startswith(SCRIPT_PREFIX))
                self.assertTrue((REPO_ROOT / module).is_file())

    def test_no_deployable_module_imports_an_operator_script(self):
        script_modules = {
            module.rsplit("/", 1)[-1][:-3] for module, _fn in OPERATOR_SCRIPT_SITES
        }
        offenders = []
        for relative in _deployable_files():
            if relative.startswith(SCRIPT_PREFIX):
                continue
            try:
                tree = ast.parse(_source(relative))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.rsplit(".", 1)[-1] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.rsplit(".", 1)[-1]]
                if script_modules & set(names):
                    offenders.append(relative)
        self.assertEqual(offenders, [])

    def test_this_module_imports_no_business_logic(self):
        """Enumerating providers must not construct one.

        clients.py builds openai.OpenAI(...) at import time (backlog #84), so an
        import here would create a real provider client purely to count them.
        """
        tree = ast.parse(_source("tests/test_external_effect_inventory.py"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("email_automation", imported)


if __name__ == "__main__":
    unittest.main()
