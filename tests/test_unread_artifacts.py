"""Which shipped artifacts are read by NO test?

``scripts/deploy_certification_twin.sh`` was read by no test at all. Three
one-token edits to it -- ``--no-allow-unauthenticated`` flipped to
``--allow-unauthenticated``, the invoker binding moved to ``allUsers``, the
role widened to ``roles/run.admin`` -- each left the ENTIRE suite green. Any
one of them would have put a public, unauthenticated route on the
certification twin.

Nothing about a shell script, a Cloud Run manifest, or a GitHub Actions
workflow forces a test to exist for it. Python modules at least get imported
by something; a ``.sh`` file is inert text that only the deploy path reads,
and the deploy path is not the test suite. So the failure mode is silent by
construction, and it does not announce itself -- the twin script sat unread
for its entire life while the suite reported green.

This module makes that condition VISIBLE and keeps it visible. It computes,
from the tree as it actually is, the set of shipped deploy/operations
artifacts that no test reads, and pins it. The pin fails in both directions on
purpose:

  * a NEW unread artifact appearing is a regression -- somebody shipped
    another inert control surface;
  * an artifact here gaining coverage is also a failure, forcing this list to
    SHRINK rather than quietly carrying names that are no longer true. A stale
    allowlist is how a list like this rots into decoration.

WHAT COUNTS AS "READ"
---------------------
Naive substring matching over the test tree gets this wrong, and getting it
wrong in the generous direction is exactly the defect being hunted:
``scripts/verify_image_source_manifest.py`` is named in a test docstring, and
a substring scan therefore calls it covered. It is not. A sentence about a
file exercises nothing.

So the instrument parses each test module and counts a reference only when the
artifact's name appears in a string that is NOT a docstring, or in an import.
Prose is tracked separately and reported, never credited.

The census below scans this module too, minus the names inside its own
``UNREAD_ARTIFACTS`` literal -- otherwise a file would count as covered merely
by being listed here as uncovered, which is the exact self-referential trap
that left 91 scenarios unguarded once already.

THE PINS
--------
The second half of this module is the other side of the census: targeted pins
for the three worst artifacts mutation found unread. Each was confirmed NOT
CAUGHT by ``scripts/mutation_probe.py`` -- the shipped file was really edited,
the plausible covering modules were really run, and nothing failed. Each pin
below now bites that exact mutation, and each is paired with a vacuity guard,
because an assertion that would also hold if the thing it checks disappeared
is not a pin.
"""

import ast
import re
import os
import subprocess
import unittest
from pathlib import Path

os.environ.setdefault("E2E_TEST_MODE", "true")

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"

# The shipped surfaces this pin governs: everything in the deploy/operations
# path that is not a Python module some import graph already drags in.
ARTIFACT_GLOBS = (
    "scripts/",
    "deploy/",
    ".github/workflows/",
    "Dockerfile",
    ".dockerignore",
    ".gcloudignore",
    "requirements.txt",
    "requirements.lock",
)

# Artifacts no test reads, with what an undetected regression to each would
# cost. Ordered worst first. Each entry is a debt, not an exemption: the right
# number here is zero.
#
# .github/workflows/email-dev-scoped.yml, .github/workflows/email.yml and
# scripts/production_reset.py were all on this list and have been struck from
# it: the pins at the bottom of this module now read them.
UNREAD_ARTIFACTS = {
    "scripts/verify_image_source_manifest.py": (
        "MEDIUM. Recomputes the deployable source set that the image is built "
        "from. tests/test_ws_b_dockerignore_contract.py names it in a "
        "docstring and never runs it -- the .dockerignore contract is pinned, "
        "the tool that enforces it is not."
    ),
    "scripts/provision_local_certification_env.py": (
        "MEDIUM. Provisions the local certification environment the "
        "certification suite runs against. If it drifts, certification runs "
        "against an environment nobody has checked."
    ),
    "scripts/make_synthetic_credential.py": (
        "MEDIUM. Mints the synthetic credential used by test/certification "
        "lanes. A change making it emit something real-looking, or reachable "
        "by production config, is unguarded."
    ),
    "scripts/analyze_production.py": (
        "LOW. Read-only production analysis, no external effect. Listed for "
        "completeness so this set is the whole truth rather than the "
        "interesting part of it."
    ),
}


SELF_NAME = Path(__file__).name

# Names that this module lists as UNCOVERED must not count as coverage. Without
# this, adding a file to UNREAD_ARTIFACTS would mark it covered and the census
# would report an empty set forever -- a test that passes because it tests
# itself, which is defect #1 in this project's history.
CENSUS_LITERALS = ("UNREAD_ARTIFACTS", "ARTIFACT_GLOBS", "SCANNER_SELFCHECK")

# Fixtures for this module's own vacuity guards. These name artifacts in order
# to check the SCANNER's behaviour, not to exercise the artifacts, so naming
# one here must not credit it with coverage. They live in a named literal
# rather than inline so the exclusion above can find them precisely.
SCANNER_SELFCHECK = {
    # Named only in a docstring elsewhere in the suite. A substring scan calls
    # it covered; it is not.
    "prose_only": "scripts/verify_image_source_manifest.py",
    # Genuinely read by tests/test_certification_mutation_controls.py.
    "really_read": "scripts/deploy_certification_twin.sh",
    "also_really_read": "Dockerfile",
}


def _census_constant_ids(tree: ast.Module) -> set:
    """ids of Constant nodes inside this module's own census literals."""
    out = set()
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        names = {t.id for t in targets if isinstance(t, ast.Name)}
        if names & set(CENSUS_LITERALS):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant):
                    out.add(id(sub))
    return out


def _docstring_constant_ids(tree: ast.Module) -> set:
    """ids of the Constant nodes that are docstrings, not live strings."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                out.add(id(body[0].value))
    return out


def _module_paths(artifacts):
    """Map a Python artifact's dotted import name to its repo path.

    Needed because a test can exercise a script WITHOUT ever naming its file:
    ``from scripts import replay_exact_message`` reads nothing that looks like
    a path. Missing this produced a false entry in the census below on the
    first pass -- replay_exact_message.py was recorded as read by no test while
    tests/test_operator_message_replay.py was importing and driving its CLI.
    Which is the same defect this whole module hunts, found in its own
    instrument.
    """
    out = {}
    for path in artifacts:
        if path.endswith(".py"):
            out[path[: -len(".py")].replace("/", ".")] = path
    return out


def _imported_modules(tree: ast.Module):
    """Every dotted module name this file imports, however it spells it."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}")
    return names


def _tracked_artifacts():
    proc = subprocess.run(
        ["git", "ls-files", *ARTIFACT_GLOBS],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    paths = [p for p in proc.stdout.split() if p and not p.endswith("README.md")]
    return sorted(paths)


def scan_references():
    """Return (live, prose): artifact path -> set of test modules referencing it.

    ``live`` is a real reference -- the name appears in a non-docstring string
    or an import. ``prose`` is a mention in a docstring or comment, which is
    credited to nothing.
    """
    artifacts = _tracked_artifacts()
    by_name = {}
    for path in artifacts:
        by_name.setdefault(Path(path).name, []).append(path)
    by_module = _module_paths(artifacts)

    live = {p: set() for p in artifacts}
    prose = {p: set() for p in artifacts}

    for test_file in sorted(TESTS_DIR.glob("*.py")):
        try:
            source = test_file.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        docstrings = _docstring_constant_ids(tree)
        if test_file.name == SELF_NAME:
            docstrings |= _census_constant_ids(tree)

        # An import is the strongest possible reference: the module is really
        # executed. It is also invisible to any name- or path-based search.
        for module in _imported_modules(tree):
            path = by_module.get(module)
            if path:
                live[path].add(test_file.name)

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                bucket = prose if id(node) in docstrings else live
                for name, paths in by_name.items():
                    if name in node.value:
                        for p in paths:
                            bucket[p].add(test_file.name)

        for line in source.splitlines():
            if line.lstrip().startswith("#"):
                for name, paths in by_name.items():
                    if name in line:
                        for p in paths:
                            prose[p].add(test_file.name)

    return live, prose


class UnreadShippedArtifactTests(unittest.TestCase):
    def setUp(self):
        self.live, self.prose = scan_references()

    def test_the_set_of_unread_artifacts_is_exactly_what_is_recorded(self):
        """Fails in BOTH directions. A new unread artifact is a regression; a
        newly covered one must be struck from the list, not left to rot."""
        observed = {p for p, refs in self.live.items() if not refs}
        recorded = set(UNREAD_ARTIFACTS)

        newly_unread = sorted(observed - recorded)
        self.assertFalse(
            newly_unread,
            "shipped artifacts that NO test reads and that are not recorded "
            "in UNREAD_ARTIFACTS: "
            + ", ".join(newly_unread)
            + ". deploy_certification_twin.sh was exactly this shape: three "
            "one-token edits, entire suite green. Either write a test that "
            "reads the artifact, or record it here with its blast radius.",
        )

        now_covered = sorted(recorded - observed)
        self.assertFalse(
            now_covered,
            "these are recorded as unread but a test now reads them: "
            + ", ".join(now_covered)
            + ". Remove them from UNREAD_ARTIFACTS -- a list of known gaps is "
            "only useful while every entry is still true.",
        )

    def test_the_scan_credits_live_references_and_not_prose(self):
        """Vacuity guard. If the scanner credited every substring mention, this
        set would be empty and the pin above would assert nothing.

        verify_image_source_manifest.py is the live proof: a test docstring
        names it, and a substring scan would call it covered. It is not.
        """
        prose_only = {
            p for p in self.live if not self.live[p] and self.prose[p]
        }
        self.assertIn(
            SCANNER_SELFCHECK["prose_only"],
            prose_only,
            "expected the scanner to classify this as prose-only; if it no "
            "longer does, either the artifact gained a real test (good -- "
            "update this case) or the scanner started crediting docstrings "
            "(bad -- the pin above is now vacuous)",
        )

    def test_an_imported_script_counts_as_read(self):
        """A test can drive a script without ever naming its file.

        ``tests/test_operator_message_replay.py`` does ``from scripts import
        replay_exact_message`` and exercises its CLI. Nothing in that file
        contains the string "replay_exact_message.py", so a name-based census
        records it as unread -- and this one did, on its first pass, until
        mutation showed the CLI's --apply gate WAS caught. An instrument that
        reports phantom gaps is as useless as one that hides real ones.
        """
        self.assertIn(
            "test_operator_message_replay.py",
            self.live.get("scripts/replay_exact_message.py", set()),
            "the scanner must credit `from scripts import <module>` as a read",
        )

    def test_the_scan_actually_finds_references_when_they_exist(self):
        """The other half of the vacuity guard: a scanner that credited
        nothing would make every artifact look unread and the pin would be
        loud but meaningless."""
        self.assertTrue(
            self.live.get(SCANNER_SELFCHECK["really_read"]),
            "deploy_certification_twin.sh is read by "
            "tests/test_certification_mutation_controls.py; a scanner that "
            "cannot see that reference cannot be trusted about the ones it "
            "reports as missing",
        )
        self.assertTrue(self.live.get(SCANNER_SELFCHECK["also_really_read"]))

    def test_every_recorded_gap_still_exists_in_the_tree(self):
        tracked = set(_tracked_artifacts())
        for path in sorted(UNREAD_ARTIFACTS):
            with self.subTest(artifact=path):
                self.assertIn(
                    path,
                    tracked,
                    f"{path} is recorded as an unread artifact but is no "
                    f"longer tracked; delete the entry",
                )

    def test_every_recorded_gap_states_its_blast_radius(self):
        for path, why in sorted(UNREAD_ARTIFACTS.items()):
            with self.subTest(artifact=path):
                self.assertTrue(
                    why.startswith(("CRITICAL", "HIGH", "MEDIUM", "LOW")),
                    f"{path}: an unread artifact must be recorded with a "
                    f"severity so this list can be worked worst-first",
                )


PROD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "email.yml"
DEV_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "email-dev-scoped.yml"
PRODUCTION_RESET = REPO_ROOT / "scripts" / "production_reset.py"


class SchedulerLaunchGatePinTests(unittest.TestCase):
    """The emergency launch gate on the every-30-minutes production cron.

    ``.github/workflows/email.yml`` runs ``python main.py`` against production
    Firebase on a ``*/30 * * * *`` schedule. The three env lines pinned here
    are the whole of what keeps that run confined to one mailbox. The file's
    own comment calls them an "emergency launch safety gate ... until the full
    Baylor/BP21 proof is clean again".

    Mutation confirmed no test read them: flipping
    ``SITESIFT_DEV_SCOPED_SCHEDULER`` from "1" to "0", and separately emptying
    ``SITESIFT_SCHEDULER_TARGET_USER_IDS``, each left the whole suite green.
    Either edit widens the autonomous cron from Baylor's mailbox to every live
    beta user -- real sends, to real recipients, irreversibly.

    ``email_automation/scheduler_scope.py`` is thoroughly tested, and that is
    exactly why this gap survived: the LOGIC that reads these variables is
    pinned, while the VALUES that production actually supplies were not read by
    anything.

    The UID is imported from the product rather than retyped. A test that
    rebuilds a value which also exists as a real artifact is only testing
    itself.
    """

    def setUp(self):
        from email_automation.scheduler_scope import BAYLOR_DEV_UID

        self.uid = BAYLOR_DEV_UID
        self.prod = PROD_WORKFLOW.read_text(encoding="utf-8")
        self.dev = DEV_WORKFLOW.read_text(encoding="utf-8")

    def test_production_cron_declares_the_dev_scoped_flag_enabled(self):
        self.assertIn(
            'SITESIFT_DEV_SCOPED_SCHEDULER: "1"',
            self.prod,
            "the scheduled production workflow must keep the launch safety "
            "gate ON; with it off the every-30-minutes cron processes every "
            "live beta user autonomously",
        )

    def test_production_cron_targets_only_the_one_pinned_mailbox(self):
        for var in (
            "SITESIFT_SCHEDULER_TARGET_USER_IDS",
            "SITESIFT_SCHEDULER_ALLOWED_USER_IDS",
        ):
            with self.subTest(env=var):
                self.assertIn(
                    f'{var}: "{self.uid}"',
                    self.prod,
                    f"{var} must name exactly the pinned mailbox. An empty or "
                    f"widened value un-scopes the production cron.",
                )

    def test_dev_scoped_workflow_is_confined_to_the_same_single_mailbox(self):
        """The dev button also runs main.py against PRODUCTION Firebase. Its
        only containment is this pair."""
        self.assertIn('SITESIFT_DEV_SCOPED_SCHEDULER: "1"', self.dev)
        for var in (
            "SITESIFT_SCHEDULER_TARGET_USER_IDS",
            "SITESIFT_SCHEDULER_ALLOWED_USER_IDS",
        ):
            with self.subTest(env=var):
                self.assertIn(f'{var}: "{self.uid}"', self.dev)

    def test_dev_scoped_workflow_is_manual_only_and_never_scheduled(self):
        """A schedule trigger on the dev workflow would double the autonomous
        production cadence with no review step."""
        self.assertIn("workflow_dispatch:", self.dev)
        self.assertNotIn("schedule:", self.dev)

    def test_no_other_uid_is_named_in_either_workflow(self):
        """Vacuity guard. ``assertIn`` on a pinned pair still passes if a
        SECOND mailbox is appended elsewhere in the file, so widening by
        addition has to be refused separately."""
        uid_like = re.compile(r"\b[A-Za-z0-9]{28}\b")
        for name, text in (("email.yml", self.prod), ("email-dev-scoped.yml", self.dev)):
            with self.subTest(workflow=name):
                found = set(uid_like.findall(text))
                self.assertEqual(
                    found,
                    {self.uid},
                    f"{name} names a mailbox id other than the pinned one",
                )

    def test_these_pins_would_fail_if_the_gate_were_removed(self):
        """Vacuity guard for the whole class: prove the assertions are keyed on
        text that is actually load-bearing, by checking the mutated form is
        NOT present. If the anchors ever drifted, this catches it."""
        self.assertNotIn('SITESIFT_DEV_SCOPED_SCHEDULER: "0"', self.prod)
        self.assertNotIn('SITESIFT_SCHEDULER_TARGET_USER_IDS: ""', self.prod)
        self.assertNotIn('SITESIFT_SCHEDULER_ALLOWED_USER_IDS: "*"', self.dev)


class ProductionResetFailSafePinTests(unittest.TestCase):
    """``scripts/production_reset.py`` mass-deletes production data.

    Thirteen Firestore collections per user, plus nested ``notifications`` and
    ``messages`` subcollections. Read by no test at all, so mutation confirmed
    that flipping its ``dry_run=True`` fail-safe default to ``False`` left the
    entire suite green -- and every caller that omits the argument would then
    perform a live, irreversible wipe.

    These pins read the shipped script as text rather than importing it,
    because importing it constructs a Firestore client at module scope. The
    point is the fail-safe defaults, and those are visible in the source.
    """

    def setUp(self):
        self.source = PRODUCTION_RESET.read_text(encoding="utf-8")

    def test_every_delete_helper_defaults_to_dry_run(self):
        """The default is the fail-safe. A caller that forgets the argument
        must preview, never delete."""
        for signature in (
            "def delete_collection_batched(db, collection_ref, batch_size=50, dry_run=True):",
            "def wipe_user_data(db, user_id, dry_run=True):",
        ):
            with self.subTest(fn=signature.split("(")[0]):
                self.assertIn(
                    signature,
                    self.source,
                    "the deleting helpers must default to dry_run=True; "
                    "flipping this default turns every argument-omitting "
                    "caller into an irreversible production wipe",
                )

    def test_no_delete_helper_defaults_to_live(self):
        """Vacuity guard: assertIn above would still pass if a THIRD deleting
        helper defaulted to dry_run=False."""
        self.assertNotIn(
            "dry_run=False",
            self.source.replace("dry_run=args.dry_run", ""),
            "no helper in this script may default to live deletion",
        )

    def test_wiping_all_users_requires_an_explicit_confirmation(self):
        """--all-users must not be able to wipe without either --confirm or an
        interactive 'yes'."""
        self.assertIn("if not args.confirm and not args.dry_run:", self.source)
        self.assertIn("if not confirm_action(", self.source)
        self.assertIn('return response == "yes"', self.source)

    def test_the_confirmation_prompt_is_still_reached_from_main(self):
        """Vacuity guard: the guard clause above could exist while nothing
        called it. Assert the abort path is wired."""
        self.assertIn('print("Aborted.")', self.source)


if __name__ == "__main__":
    unittest.main()
