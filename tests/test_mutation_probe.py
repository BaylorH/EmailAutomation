"""The mutation probe is itself a test instrument, so it needs a test that bites.

A harness that silently no-ops reports a FALSE verdict, and a false "NOT CAUGHT"
is worse than no verdict at all: it sends someone to write a test for a surface
that was already covered, while a genuinely uncovered surface goes unlooked-at.
Two agents in this project have already been burned by exactly that. Worse, a
harness that dies mid-case with a mutation still applied leaves a sabotaged
control in the working tree -- that happened here once, and only luck kept it
out of a commit.

So these cases prove three separate things about the harness:

1. It APPLIES -- the bytes on disk really change, and the SHA really moves.
2. It RESTORES -- byte-for-byte, verified by SHA equality, including when the
   body under it raises.
3. It REFUSES -- an anchor that does not occur the declared number of times,
   and a replacement identical to its anchor, are both rejected rather than
   silently producing a meaningless verdict.

Every case here works on a throwaway file in a tmpdir. Nothing in this module
touches a tracked source file: a test for a mutation tool must not be the thing
that leaves the tree dirty.
"""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("E2E_TEST_MODE", "true")

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.mutation_probe import (  # noqa: E402
    _FAIL_LINE,
    CASES,
    CASES_BY_NAME,
    GREEN,
    RED,
    UNREADABLE,
    Mutation,
    MutationCase,
    MutationGuardError,
    classify_run,
    probe,
    sha256_of,
)


class MutationApplyAndRestoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "guard.py"
        self.path.write_text(
            'ALLOW_SEND = False\n\ndef gate():\n    return ALLOW_SEND\n',
            encoding="utf-8",
        )
        self.original_sha = sha256_of(self.path)

    def test_mutation_actually_changes_the_bytes_on_disk(self):
        """Not "we wrote something" -- the file re-read from disk must differ."""
        with Mutation(self.path, "ALLOW_SEND = False", "ALLOW_SEND = True") as mut:
            on_disk = self.path.read_text(encoding="utf-8")
            self.assertIn("ALLOW_SEND = True", on_disk)
            self.assertNotIn("ALLOW_SEND = False", on_disk)
            self.assertNotEqual(mut.mutated_sha, self.original_sha)
            self.assertEqual(sha256_of(self.path), mut.mutated_sha)

    def test_restore_returns_the_exact_original_bytes(self):
        with Mutation(self.path, "ALLOW_SEND = False", "ALLOW_SEND = True"):
            pass
        self.assertEqual(sha256_of(self.path), self.original_sha)

    def test_restore_happens_even_when_the_body_raises(self):
        """The whole point of the context manager. A test run that blows up
        mid-mutation must not leave the sabotage behind."""

        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            with Mutation(self.path, "ALLOW_SEND = False", "ALLOW_SEND = True"):
                raise Boom()
        self.assertEqual(sha256_of(self.path), self.original_sha)

    def test_restore_is_verified_by_sha_not_by_assumption(self):
        mut = Mutation(self.path, "ALLOW_SEND = False", "ALLOW_SEND = True")
        mut.apply()
        mut.restore()
        self.assertEqual(mut.original_sha, self.original_sha)
        self.assertEqual(sha256_of(self.path), self.original_sha)


class MutationGuardRefusalTests(unittest.TestCase):
    """The guards. Each of these is a way the harness could lie, and does not."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "guard.py"
        self.path.write_text("x = 1\ny = 1\n", encoding="utf-8")
        self.original_sha = sha256_of(self.path)

    def test_absent_anchor_is_refused_and_leaves_the_file_untouched(self):
        mut = Mutation(self.path, "z = 99", "z = 0", occurrences=1)
        with self.assertRaises(MutationGuardError) as ctx:
            mut.apply()
        self.assertIn("anchor occurs 0x", str(ctx.exception))
        self.assertEqual(sha256_of(self.path), self.original_sha)

    def test_anchor_occurring_more_often_than_declared_is_refused(self):
        """An anchor that multiplied is a stale case. Mutating both sites would
        test something other than what the case says it tests."""
        mut = Mutation(self.path, " = 1", " = 2", occurrences=1)
        with self.assertRaises(MutationGuardError) as ctx:
            mut.apply()
        self.assertIn("anchor occurs 2x", str(ctx.exception))
        self.assertEqual(sha256_of(self.path), self.original_sha)

    def test_declaring_the_true_count_lets_the_same_anchor_through(self):
        """Vacuity guard for the case above: the refusal must be about the
        COUNT, not about that anchor being unusable."""
        with Mutation(self.path, " = 1", " = 2", occurrences=2):
            self.assertEqual(
                self.path.read_text(encoding="utf-8"), "x = 2\ny = 2\n"
            )
        self.assertEqual(sha256_of(self.path), self.original_sha)

    def test_replacement_identical_to_anchor_is_refused(self):
        """A no-op mutation would report NOT CAUGHT for every surface on
        earth. It is refused before a single byte is written."""
        mut = Mutation(self.path, "x = 1", "x = 1", occurrences=1)
        with self.assertRaises(MutationGuardError) as ctx:
            mut.apply()
        self.assertIn("no-op", str(ctx.exception))
        self.assertEqual(sha256_of(self.path), self.original_sha)

    def test_missing_target_is_refused(self):
        missing = Path(self._tmp.name) / "does_not_exist.py"
        mut = Mutation(missing, "a", "b")
        with self.assertRaises(MutationGuardError) as ctx:
            mut.apply()
        self.assertIn("does not exist", str(ctx.exception))


class ThreeStateVerdictTests(unittest.TestCase):
    """Caught, not caught, and could-not-tell are THREE states.

    Collapsing the third into either of the others is the worst thing this
    tool can do. Folded into NOT CAUGHT, the report invents a coverage hole and
    sends someone to write a test for a gap that does not exist, while the real
    gaps stay hidden. Folded into CAUGHT, it hides a real hole -- a mutation
    that breaks collection outright would score as "the tests noticed" when in
    fact no test ran.
    """

    def test_clean_run_is_green(self):
        state, _ = classify_run({"exit_code": 0, "failures": [], "timed_out": False})
        self.assertIs(state, GREEN)

    def test_failing_tests_with_parseable_failures_are_red(self):
        state, _ = classify_run(
            {
                "exit_code": 1,
                "failures": ["tests/test_x.py::T::test_y"],
                "timed_out": False,
            }
        )
        self.assertIs(state, RED)

    def test_nonzero_exit_with_no_parseable_failure_is_unreadable_not_red(self):
        """Exit 1 with nothing parseable means the output format moved under
        us. Scoring it CAUGHT would silently retire a real finding."""
        state, why = classify_run(
            {"exit_code": 1, "failures": [], "timed_out": False}
        )
        self.assertIs(state, UNREADABLE)
        self.assertIn("cannot be trusted", why)

    def test_internal_error_is_unreadable(self):
        """pytest exit 3 is typically a collection failure caused by the
        mutation itself. No test outcome was produced at all."""
        state, why = classify_run(
            {"exit_code": 3, "failures": [], "timed_out": False}
        )
        self.assertIs(state, UNREADABLE)
        self.assertIn("internal error", why)

    def test_no_tests_collected_is_unreadable_not_green(self):
        """The dangerous one. A run in which nothing executed exits non-zero
        but proves nothing -- and if it ever exited 0 it would be scored NOT
        CAUGHT, manufacturing a coverage hole out of a typo in a module path."""
        state, why = classify_run(
            {"exit_code": 5, "failures": [], "timed_out": False}
        )
        self.assertIs(state, UNREADABLE)
        self.assertIn("nothing executed", why)

    def test_usage_error_is_unreadable(self):
        state, _ = classify_run({"exit_code": 4, "failures": [], "timed_out": False})
        self.assertIs(state, UNREADABLE)

    def test_timeout_is_unreadable_and_never_a_verdict(self):
        state, why = classify_run(
            {"exit_code": None, "failures": [], "timed_out": True}
        )
        self.assertIs(state, UNREADABLE)
        self.assertIn("timed out", why)

    def test_unrecognised_exit_status_is_unreadable(self):
        state, why = classify_run(
            {"exit_code": 137, "failures": [], "timed_out": False}
        )
        self.assertIs(state, UNREADABLE)
        self.assertIn("unrecognised", why)


class FailureLineParsingTests(unittest.TestCase):
    """The harness must recognise every shape of failure this suite emits.

    A failure shape the parser cannot read turns into "exit 1 with nothing
    parseable", which classify_run refuses -- so an UNPARSED failure becomes a
    COULD NOT TELL for a case the suite actually caught. That is not a
    catastrophe like a false NOT CAUGHT, but it is still the tool lying about
    its own coverage, and it happened: this suite reports failed subtests as
    ``SUBFAILED(param='x') path::Class::test``, which the plain FAILED/ERROR
    pattern does not match.
    """

    def _parse(self, text):
        return sorted({m.group(1) for m in _FAIL_LINE.finditer(text)})

    def test_plain_failed_lines_are_parsed(self):
        self.assertEqual(
            self._parse("FAILED tests/test_a.py::T::test_b\n"),
            ["tests/test_a.py::T::test_b"],
        )

    def test_error_lines_are_parsed(self):
        self.assertEqual(
            self._parse("ERROR tests/test_a.py\n"), ["tests/test_a.py"]
        )

    def test_subfailed_lines_are_parsed(self):
        """The one that was missing."""
        line = (
            "SUBFAILED(env='SITESIFT_SCHEDULER_TARGET_USER_IDS') "
            "tests/test_unread_artifacts.py::SchedulerLaunchGatePinTests::"
            "test_production_cron_targets_only_the_one_pinned_mailbox\n"
        )
        self.assertEqual(
            self._parse(line),
            [
                "tests/test_unread_artifacts.py::SchedulerLaunchGatePinTests::"
                "test_production_cron_targets_only_the_one_pinned_mailbox"
            ],
        )

    def test_a_run_caught_only_by_a_subtest_classifies_as_red(self):
        """End to end for the bug: exit 1 plus only SUBFAILED output must be
        CAUGHT, not COULD NOT TELL."""
        out = (
            "SUBFAILED(env='X') tests/test_x.py::T::t\n"
            "1 failed, 77 passed in 10.82s\n"
        )
        run = {
            "exit_code": 1,
            "timed_out": False,
            "failures": self._parse(out),
        }
        state, _ = classify_run(run)
        self.assertIs(state, RED)

    def test_passing_output_yields_no_failures(self):
        """Vacuity guard: a parser that matched everything would make every
        mutation look caught."""
        self.assertEqual(self._parse("77 passed in 10.82s\n"), [])
        self.assertEqual(self._parse("....F....  [100%]\n"), [])


class ProbeUnreadableRunTests(unittest.TestCase):
    """End to end: an unparseable run must report COULD NOT TELL, and the
    source must still come back by SHA.

    A harness that dies or gets confused mid-case is how a sabotaged control
    ends up in the working tree; that has already happened in this repo. So
    the restore is asserted on the failure path, not only the happy one.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "scripts").mkdir()
        self.target = self.root / "scripts" / "guard.py"
        self.target.write_text("ALLOW = False\n", encoding="utf-8")
        self.original_sha = sha256_of(self.target)
        self.case = MutationCase(
            name="fake",
            target="scripts/guard.py",
            anchor="ALLOW = False",
            replacement="ALLOW = True",
            occurrences=1,
            modules=["tests/test_nothing.py"],
            blast_radius="n/a - fixture",
            rationale="n/a - fixture",
        )

    def _runner(self, results):
        calls = list(results)

        def run(_modules):
            return calls.pop(0)

        return run

    def test_unparseable_mutated_run_reports_could_not_tell_not_not_caught(self):
        green = {"exit_code": 0, "failures": [], "timed_out": False, "seconds": 0.1}
        internal_error = {
            "exit_code": 3,
            "failures": [],
            "timed_out": False,
            "seconds": 0.1,
        }
        result = probe(
            self.case,
            repo_root=self.root,
            runner=self._runner([green, internal_error]),
        )
        self.assertEqual(result["verdict"], "COULD NOT TELL")
        self.assertNotEqual(result["verdict"], "NOT CAUGHT")
        self.assertIn("HARNESS FAILURE", result["reason"])

    def test_the_source_is_restored_by_sha_after_an_unreadable_run(self):
        green = {"exit_code": 0, "failures": [], "timed_out": False, "seconds": 0.1}
        timed_out = {
            "exit_code": None,
            "failures": [],
            "timed_out": True,
            "seconds": 900,
        }
        result = probe(
            self.case,
            repo_root=self.root,
            runner=self._runner([green, timed_out]),
        )
        self.assertEqual(result["verdict"], "COULD NOT TELL")
        self.assertTrue(result["restored_ok"])
        self.assertEqual(sha256_of(self.target), self.original_sha)
        self.assertEqual(
            self.target.read_text(encoding="utf-8"), "ALLOW = False\n"
        )

    def test_a_runner_that_raises_still_restores_the_source(self):
        green = {"exit_code": 0, "failures": [], "timed_out": False, "seconds": 0.1}
        calls = [green]

        def run(_modules):
            if calls:
                return calls.pop(0)
            raise RuntimeError("subprocess exploded")

        with self.assertRaises(RuntimeError):
            probe(self.case, repo_root=self.root, runner=run)
        self.assertEqual(sha256_of(self.target), self.original_sha)

    def test_a_red_baseline_is_could_not_tell_not_a_coverage_verdict(self):
        """If the covering modules are already failing, every mutation would
        look caught. That is measured against nothing."""
        red = {
            "exit_code": 1,
            "failures": ["tests/test_nothing.py::T::t"],
            "timed_out": False,
            "seconds": 0.1,
        }
        result = probe(self.case, repo_root=self.root, runner=self._runner([red]))
        self.assertEqual(result["verdict"], "COULD NOT TELL")
        self.assertEqual(sha256_of(self.target), self.original_sha)

    def test_a_genuinely_unnoticed_mutation_still_reports_not_caught(self):
        """Vacuity guard for this whole class: if every path returned COULD
        NOT TELL, the tool would never report a finding at all."""
        green = {"exit_code": 0, "failures": [], "timed_out": False, "seconds": 0.1}
        result = probe(
            self.case, repo_root=self.root, runner=self._runner([green, green])
        )
        self.assertEqual(result["verdict"], "NOT CAUGHT")
        self.assertTrue(result["restored_ok"])
        self.assertNotEqual(result["sha_mutated"], result["sha_before"])

    def test_a_caught_mutation_reports_caught(self):
        green = {"exit_code": 0, "failures": [], "timed_out": False, "seconds": 0.1}
        red = {
            "exit_code": 1,
            "failures": ["tests/test_nothing.py::T::t"],
            "timed_out": False,
            "seconds": 0.1,
        }
        result = probe(
            self.case, repo_root=self.root, runner=self._runner([green, red])
        )
        self.assertEqual(result["verdict"], "CAUGHT")
        self.assertEqual(sha256_of(self.target), self.original_sha)


class CatalogueIntegrityTests(unittest.TestCase):
    """The catalogue points at REAL shipped files with REAL anchors.

    A case whose anchor has drifted is a case that silently stops probing. The
    harness refuses such a case at run time, but that refusal only shows up
    when someone runs the probe. This makes it show up in the suite.
    """

    def test_every_case_targets_a_file_that_exists(self):
        for case in CASES:
            with self.subTest(case=case.name):
                self.assertTrue(
                    (REPO_ROOT / case.target).is_file(),
                    f"{case.name} targets {case.target}, which does not exist",
                )

    def test_every_case_anchor_occurs_exactly_as_declared(self):
        for case in CASES:
            with self.subTest(case=case.name):
                text = (REPO_ROOT / case.target).read_text(encoding="utf-8")
                self.assertEqual(
                    text.count(case.anchor),
                    case.occurrences,
                    f"{case.name}: anchor has drifted in {case.target}; the "
                    f"case would refuse to run",
                )

    def test_every_case_names_covering_modules_that_exist(self):
        for case in CASES:
            with self.subTest(case=case.name):
                self.assertTrue(case.modules, f"{case.name} names no modules")
                for module in case.modules:
                    self.assertTrue(
                        (REPO_ROOT / module).is_file(),
                        f"{case.name} names {module}, which does not exist -- a "
                        f"missing module silently narrows the covering set and "
                        f"can turn a CAUGHT into a NOT CAUGHT",
                    )

    def test_no_case_is_a_no_op(self):
        for case in CASES:
            with self.subTest(case=case.name):
                self.assertNotEqual(case.anchor, case.replacement)

    def test_case_names_are_unique(self):
        self.assertEqual(len(CASES), len(CASES_BY_NAME))


if __name__ == "__main__":
    unittest.main()
