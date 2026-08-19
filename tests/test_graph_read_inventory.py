"""Graph mailbox READS: the definitive inventory, and one converged module.

Delivery converged first (Tasks 6, 7A-7D) and its absence of an alternate send
path is proven. READS did not converge with it, and the project carried TWO
disagreeing measurements of how many there are:

  * **19 reads across six modules** - the figure in the project record.
  * **33 reads across nine modules** - a later verb-complete count.

Neither was wrong. They measure different things, and this module makes the
difference mechanical rather than remembered: ``scripts/inventory_graph_operations``
produces both scans from the same parse, and the tests below pin the exact sites
each figure counts. See ``ReconciliationTests``.

The reconciliation matters beyond bookkeeping, because the delta is not noise -
it is three specific classes of blindness that a URL-literal scan has by
construction, each of which hides a real mailbox call:

  1. ``/me`` (the mailbox-identity endpoint) does not contain the substring
     ``/me/``, so a scan testing for ``"/me/" in url`` cannot see it.
  2. A URL bound through a lambda default (``lambda u=url: requests.get(u)``)
     is not a literal at the call site, so it does not resolve.
  3. Binding names module-flat rather than per-scope makes every ``url`` in a
     file collapse to whichever assignment the walker happened to see last.

All three are present in this repository right now, which is the point.
"""

import ast
import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPO_ROOT / "scripts" / "inventory_graph_operations.py"


def _load_tool():
    """Import the committed tool by path.

    Deliberately NOT ``from scripts import ...``: the inventory has to be
    runnable as a standalone command by an operator who has not put the repo on
    the path, and importing it the same way the test does keeps those two uses
    from drifting.
    """
    spec = importlib.util.spec_from_file_location(
        "inventory_graph_operations", TOOL_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["inventory_graph_operations"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# The three sites a URL-literal scan structurally cannot see.
# ---------------------------------------------------------------------------
#
# Written as (module, line, function, why) rather than as a count, because a
# count that drifts tells you nothing about WHICH site moved. Each of these was
# read out of the source by hand before the tool existed; the tool has to find
# them, not define them.

BLIND_SPOT_SITES = (
    (
        "email_automation/processing.py",
        6365,
        "_resolve_current_mailbox_email",
        "bare /me identity endpoint; the substring '/me/' does not occur in it",
    ),
    (
        "app.py",
        1916,
        "api_debug_inbox",
        "bare /me identity endpoint; same substring blindness",
    ),
    (
        "email_automation/sent_mail_guard.py",
        323,
        "find_sent_conversation_continuation_for_retry",
        "URL reaches requests.get through a lambda default (u=url)",
    ),
    (
        "email_automation/sent_mail_guard.py",
        443,
        "find_sent_recipient_continuation_for_retry",
        "URL reaches requests.get through a lambda default (u=url)",
    ),
)


class InventoryToolTests(unittest.TestCase):
    """The tool exists, runs, and classifies by METHOD rather than by URL text."""

    @classmethod
    def setUpClass(cls):
        cls.tool = _load_tool()
        cls.report = cls.tool.build_report(REPO_ROOT)

    def test_the_inventory_tool_is_committed_and_runnable(self):
        self.assertTrue(
            TOOL_PATH.exists(),
            "the inventory must be a re-runnable committed tool, not a number in a report",
        )

    def test_every_operation_is_classified_by_http_method(self):
        allowed = {"read", "write", "destructive", "send"}
        for op in self.report["operations"]:
            with self.subTest(site=f"{op['module']}:{op['line']}"):
                self.assertIn(op["classification"], allowed)
                self.assertIn(op["method"], ("get", "post", "patch", "put", "delete"))

    def test_classification_follows_the_verb_not_the_url(self):
        """DELETE is destructive wherever it points; GET is a read wherever it points."""
        for op in self.report["operations"]:
            with self.subTest(site=f"{op['module']}:{op['line']}"):
                if op["method"] == "delete":
                    self.assertEqual(op["classification"], "destructive")
                elif op["method"] == "get":
                    self.assertEqual(op["classification"], "read")

    def test_the_method_scan_finds_every_site_the_url_scan_is_blind_to(self):
        found = {
            (op["module"], op["line"])
            for op in self.report["operations"]
        }
        for module, line, function, why in BLIND_SPOT_SITES:
            with self.subTest(module=module, line=line):
                self.assertIn(
                    (module, line),
                    found,
                    f"method scan missed {module}:{line} ({function}) - {why}",
                )

    def test_the_url_literal_scan_really_is_blind_to_them(self):
        """The claim under test is about the OLD scan, so assert its blindness too.

        Without this, a future change that fixed the URL scan would leave the
        reconciliation above quietly asserting a difference that no longer
        exists, and the two figures would silently become one.
        """
        seen = {
            (op["module"], op["line"])
            for op in self.report["urlLiteralOperations"]
        }
        for module, line, function, why in BLIND_SPOT_SITES:
            with self.subTest(module=module, line=line):
                self.assertNotIn((module, line), seen, why)

    def test_a_scoped_binding_is_not_a_module_flat_one(self):
        """Trap 3: every ``url`` in a file must not collapse to one value.

        ``processing.py`` binds a local ``url`` in two different functions to two
        different mailbox folders. If the resolver were module-flat, both sites
        would report the same folder and one of them would be a lie.
        """
        by_line = {
            op["line"]: op["url"]
            for op in self.report["operations"]
            if op["module"] == "email_automation/processing.py"
        }
        self.assertIn("/me/mailFolders/Inbox/messages", by_line.get(8774, ""))
        self.assertIn("/me/mailFolders/SentItems/messages", by_line.get(9331, ""))

    def test_an_http_call_the_tool_cannot_classify_is_reported_not_dropped(self):
        """Allowlist, never denylist: silence is the failure mode being avoided.

        A scan that quietly skips what it cannot resolve reports a smaller,
        cleaner inventory than the truth. Unresolvable call sites therefore land
        in their own list and are counted.
        """
        self.assertIn("unresolvedHttpCallSites", self.report)
        for entry in self.report["unresolvedHttpCallSites"]:
            with self.subTest(site=f"{entry['module']}:{entry['line']}"):
                self.assertTrue(entry.get("reason"))

    def test_an_unrecognised_http_receiver_is_surfaced(self):
        """The receiver allowlist is the tool's own blind spot; it must self-report."""
        self.assertIn("unrecognisedReceivers", self.report)


class ReconciliationTests(unittest.TestCase):
    """19 vs 33, stated as scopes rather than as an argument about which is right.

    The answer is that BOTH figures were measuring something real and neither
    was the whole picture:

      * **19** is exact and reproducible. It is the mailbox reads in the six
        certification-driven product lanes, as the URL-literal scan sees them.
      * **33 across 9 modules** is directionally right and numerically not
        reproducible. It correctly identified the four extra modules and the
        two undercounts, but the deployed application surface holds **36 reads
        across 10 modules**, and ``sent_mail_guard.py`` holds three, not two.

    The gap between 19 and 36 is itemised site by site below - it is never left
    as "the bigger number is bigger".
    """

    @classmethod
    def setUpClass(cls):
        cls.tool = _load_tool()
        cls.report = cls.tool.build_report(REPO_ROOT)

    def test_scope_a_reproduces_the_nineteen_read_figure(self):
        """Scope A: unconverged product-module reads, as the URL-literal scan sees them."""
        scope_a = self.report["scopeA"]
        self.assertEqual(scope_a["readCount"], 19, scope_a["byModule"])
        self.assertEqual(len(scope_a["byModule"]), 6, scope_a["byModule"])

    def test_the_reconciliation_names_the_six_scope_a_modules_exactly(self):
        self.assertEqual(
            sorted(self.report["scopeA"]["byModule"]),
            [
                "email_automation/email.py",
                "email_automation/file_handling.py",
                "email_automation/followup.py",
                "email_automation/messaging.py",
                "email_automation/processing.py",
                "email_automation/sent_mail_guard.py",
            ],
        )

    def test_scope_b_is_the_deployed_application_surface(self):
        """Scope B: what the '33 across 9' figure was reaching for - measured at 36/10."""
        scope_b = self.report["scopeB"]
        self.assertEqual(scope_b["readCount"], 36, scope_b["byModule"])
        self.assertEqual(len(scope_b["byModule"]), 10, sorted(scope_b["byModule"]))
        for added in (
            "app.py",
            "email_automation/email_operations.py",
            "email_automation/operator_replay.py",
            "email_automation/service_providers.py",
        ):
            with self.subTest(module=added):
                self.assertIn(added, scope_b["byModule"])

    def test_the_two_undercounted_modules_are_pinned_at_their_true_counts(self):
        """The corrections the 33 figure was right to make, and one it got wrong.

        ``processing.py`` really does hold twelve reads rather than eleven. But
        ``sent_mail_guard.py`` holds THREE, not the two that figure recorded -
        both of its paginating continuation guards reach ``requests.get``
        through a lambda default, and both were invisible.
        """
        scope_b = self.report["scopeB"]["byModule"]
        self.assertEqual(scope_b["email_automation/processing.py"], 12)
        self.assertEqual(scope_b["email_automation/sent_mail_guard.py"], 3)

    def test_scope_c_is_every_read_including_scripts_and_the_boundary(self):
        scope_c = self.report["scopeC"]
        self.assertEqual(scope_c["readCount"], 56, scope_c["byModule"])
        self.assertIn("scheduler_runner.py", scope_c["byModule"])
        self.assertIn("email_automation/message_transport.py", scope_c["byModule"])

    def test_the_delta_between_the_two_figures_is_fully_itemised(self):
        """The actual reconciliation: every site in B and not in A, with a reason.

        Nineteen plus the itemised delta must equal thirty-six exactly. If it
        does not, the accounting has a hole and the inventory is back to being
        two numbers that disagree.
        """
        delta = self.report["reconciliation"]["scopeBOnly"]
        self.assertEqual(
            self.report["scopeA"]["readCount"] + len(delta),
            self.report["scopeB"]["readCount"],
        )
        for entry in delta:
            with self.subTest(site=f"{entry['module']}:{entry['line']}"):
                self.assertIn(
                    entry["excludedFromScopeABy"],
                    ("module_not_in_scope", "url_literal_blind_spot"),
                )
                self.assertTrue(entry["detail"])

    def test_the_url_literal_scan_also_reports_reads_that_do_not_exist(self):
        """It is not merely conservative, which is the more dangerous half.

        A module-flat binding table makes every unresolved use of a name inherit
        whichever assignment the walker saw last. Two token-acquisition helpers
        and two generic URL fetchers are currently reported as mailbox reads.
        An inventory that overcounts in one place and undercounts in another
        cannot be corrected by adjusting the total.
        """
        phantoms = self.report["reconciliation"]["urlLiteralPhantomReads"]
        self.assertTrue(phantoms)
        reported = {(p["module"], p["line"]) for p in phantoms}
        self.assertIn(("scripts/e2e_tools.py", 44), reported)
        self.assertIn(("scripts/analyze_overnight_campaign.py", 45), reported)
        for entry in phantoms:
            with self.subTest(site=f"{entry['module']}:{entry['line']}"):
                self.assertTrue(entry["detail"])

    def test_three_real_sends_were_being_graded_as_ordinary_writes(self):
        """The finding that matters most, and the reason sendCapableByName exists.

        ``service_providers.py`` holds three genuine send call sites. The
        URL-literal scan resolves all of them to the same non-send URL and grades
        each one a plain ``write`` - so the machine-checked send inventory could
        not see them, and the module had to be added to a hand-maintained
        ``sendCapableByName`` list to compensate. Classifying by verb finds them
        without the hand-maintained list.
        """
        misclassified = self.report["reconciliation"]["urlLiteralMisclassifications"]
        sends = [
            entry for entry in misclassified
            if entry["methodScanSays"] == "send" and entry["urlLiteralSays"] == "write"
        ]
        self.assertEqual(len(sends), 3, misclassified)
        for entry in sends:
            with self.subTest(site=f"{entry['module']}:{entry['line']}"):
                self.assertEqual(entry["module"], "email_automation/service_providers.py")

    def test_no_send_or_destructive_call_hides_outside_the_scanned_set(self):
        """Trap 2 restated as a test: an inventory scoped to one verb is blind.

        Reads are the subject here, but the same parse produced the write, send
        and destructive columns, so assert they are populated. A read inventory
        that silently reported zero destructive calls would repeat exactly the
        mistake that let a mailbox DELETE sit unnoticed in app.py.
        """
        classifications = {op["classification"] for op in self.report["operations"]}
        self.assertEqual(
            classifications, {"read", "write", "destructive", "send"},
        )
        destructive = [
            op for op in self.report["operations"] if op["classification"] == "destructive"
        ]
        self.assertIn("app.py", {op["module"] for op in destructive})


if __name__ == "__main__":
    unittest.main()
