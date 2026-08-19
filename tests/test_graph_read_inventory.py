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
        6498,
        "_resolve_current_mailbox_email",
        "bare /me identity endpoint; the substring '/me/' does not occur in it. "
        "Now routed through the read boundary, which the URL-literal scan cannot "
        "see either - a read does not stop being a read by being converged",
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
        by_operation = {
            op["operation"]: op["url"]
            for op in self.report["operations"]
            if op["module"] == "email_automation/processing.py"
        }
        # Keyed by operation rather than by line: a line number is a fact about
        # today's file, and pinning one turns every unrelated edit above it into
        # a spurious failure that teaches people to re-pin without reading.
        self.assertIn("/me/mailFolders/Inbox/messages", by_operation["inbox_message_page"])
        self.assertIn("/me/mailFolders/SentItems/messages", by_operation["sent_items_page"])

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

    def test_scope_a_is_what_the_url_literal_scan_still_sees(self):
        """Scope A measured **19** before ``processing.py`` converged. It now measures 9.

        The figure moved because the CODE moved, and the drop is fully accounted
        for: eleven of ``processing.py``'s twelve reads were visible to this scan
        and are now routed through the read boundary, so it no longer sees them
        as direct calls. The twelfth - the bare ``/me`` identity endpoint - was
        never in the 19 at all.

        19 - 11 = 8, and the scan reports 9, because converging the module handed
        it a brand new PHANTOM: the boundary's own ``requests.get(url, **kwargs)``
        takes its URL from a parameter, and the module-flat binding table fills
        that gap with an unrelated assignment from elsewhere in the file. The
        scan's habit of inventing a read where it cannot resolve one is asserted
        directly in ``test_the_url_literal_scan_also_reports_reads_that_do_not_exist``.
        """
        scope_a = self.report["scopeA"]
        self.assertEqual(scope_a["readCount"], 9, scope_a["byModule"])
        self.assertEqual(len(scope_a["byModule"]), 6, scope_a["byModule"])

    def test_the_only_read_scope_a_still_sees_in_processing_is_a_phantom(self):
        """So the 9 is not mistaken for 'one direct read left to converge'."""
        phantoms = {
            (entry["module"], entry["line"])
            for entry in self.report["reconciliation"]["urlLiteralPhantomReads"]
        }
        remaining = [
            op for op in self.report["urlLiteralOperations"]
            if op["module"] == "email_automation/processing.py" and op["classification"] == "read"
        ]
        self.assertEqual(len(remaining), 1, remaining)
        self.assertIn(("email_automation/processing.py", remaining[0]["line"]), phantoms)

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

    def test_convergence_conserved_the_total_read_count(self):
        """The property that makes the inventory trustworthy across a refactor.

        Routing a read through a boundary must MOVE it between the direct and
        boundary columns and leave the total alone. If converging a module made
        the total fall, the instrument would be going blind to precisely the
        reads someone had just finished making observable - and every future
        convergence would be rewarded with a smaller number.
        """
        scope_b = self.report["scopeB"]
        self.assertEqual(scope_b["readCount"], 36)
        self.assertEqual(scope_b["readRoutes"], {"direct": 24, "boundary": 12})

    def test_every_boundary_routed_read_is_in_the_converged_module(self):
        """One module has converged so far. Say which, rather than implying more."""
        routed = {
            op["module"] for op in self.report["operations"]
            if op["classification"] == "read" and op["route"] == "boundary"
        }
        self.assertEqual(routed, {"email_automation/processing.py"})

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
        self.assertEqual(len(delta), 28, [f"{e['module']}:{e['line']}" for e in delta])

        # Scope A's raw count includes reads that do not exist, so the books only
        # balance once those are taken back out. That is the reconciliation's
        # sharpest result: the smaller figure was not simply a subset of the
        # larger one, and no adjustment to the TOTAL could have reconciled them -
        # it overcounts in one place and undercounts in another at the same time.
        scope_a_modules = set(self.report["scopeA"]["byModule"])
        phantoms_inside_scope_a = [
            entry for entry in self.report["reconciliation"]["urlLiteralPhantomReads"]
            if entry["module"] in scope_a_modules
        ]
        scope_a_real = self.report["scopeA"]["readCount"] - len(phantoms_inside_scope_a)
        self.assertEqual(
            scope_a_real + len(delta),
            self.report["scopeB"]["readCount"],
            f"scopeA={self.report['scopeA']['readCount']} "
            f"phantoms={len(phantoms_inside_scope_a)} delta={len(delta)}",
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


# ---------------------------------------------------------------------------
# The converged module
# ---------------------------------------------------------------------------
#
# ``processing.py`` holds the largest concentration of mailbox reads - twelve of
# the thirty-six on the deployed surface - so it converges first, mirroring how
# delivery converged onto ``message_transport``.
#
# The property under test is NOT "the reads call a helper". It is that the
# helper is the ONLY way to reach the mailbox from this module. A convergence
# with a surviving alternate path is worse than no convergence, because the
# certification runtime would fence one route and report a clean run while the
# other route ran unobserved. That is the entire bug class.


PROCESSING = "email_automation/processing.py"


def _processing_module():
    from email_automation import processing
    return processing


class ProcessingReadConvergenceTests(unittest.TestCase):
    """Structural half: one call site, one allowlist, no second route."""

    @classmethod
    def setUpClass(cls):
        cls.tool = _load_tool()
        cls.report = cls.tool.build_report(REPO_ROOT)
        cls.source = (REPO_ROOT / PROCESSING).read_text()
        cls.tree = ast.parse(cls.source)

    def _processing_ops(self):
        return [op for op in self.report["operations"] if op["module"] == PROCESSING]

    def test_processing_still_holds_all_twelve_of_its_mailbox_reads(self):
        """Convergence MOVES reads; it must never appear to remove them.

        If the inventory stopped counting a read the moment it was routed
        through a boundary, converging a module would look like the reads
        vanished - and the instrument would be rewarding the refactor by going
        blind to it.
        """
        reads = [op for op in self._processing_ops() if op["classification"] == "read"]
        self.assertEqual(len(reads), 12, [f"{o['line']}:{o['function']}" for o in reads])

    def test_all_twelve_are_routed_and_none_is_a_direct_provider_call(self):
        reads = [op for op in self._processing_ops() if op["classification"] == "read"]
        direct = [op for op in reads if op["route"] != "boundary"]
        self.assertEqual(
            direct, [],
            "a mailbox read in processing.py still reaches the provider directly",
        )

    def test_the_boundarys_own_provider_call_is_reported_not_hidden(self):
        """The one call the resolver cannot follow must still be visible.

        Its URL arrives as a parameter, so no static scan can resolve it. That
        is fine - what is not fine is dropping it. It belongs in the unresolved
        list, where a reviewer can see that the module's single remaining direct
        call is the boundary and satisfy themselves it is the right one.
        """
        unresolved = [
            entry for entry in self.report["unresolvedHttpCallSites"]
            if entry["module"] == PROCESSING
        ]
        self.assertEqual(len(unresolved), 1, unresolved)
        self.assertEqual(unresolved[0]["function"], "read")

    def test_no_http_verb_reaches_the_mailbox_outside_the_boundary(self):
        """Asserted over the AST rather than over the tool, deliberately.

        The tool and the product could drift in the same direction - a URL shape
        the resolver stops recognising would make an escaped call site invisible
        to both the inventory and this test. So this one walks the module
        directly and asks a blunter question: which functions contain a call to
        an HTTP verb at all?
        """
        offenders = []
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for child in ast.walk(node):
                if not isinstance(child, ast.Call) or not isinstance(child.func, ast.Attribute):
                    continue
                if child.func.attr not in ("get", "post", "patch", "put", "delete"):
                    continue
                receiver = child.func.value
                name = receiver.id if isinstance(receiver, ast.Name) else ""
                if name == "requests":
                    offenders.append((node.name, child.lineno))
        self.assertEqual(
            offenders, [("read", offenders[0][1] if offenders else 0)],
            f"a requests.* call survives outside the read boundary: {offenders}",
        )

    def test_the_allowlist_names_every_routed_operation_and_nothing_more(self):
        """A dead allowlist entry is a hole, and an unlisted name is a refusal.

        Both directions matter. An operation name in the allowlist that no call
        site uses is a door left open for the next caller; a call site whose name
        is absent would refuse at runtime, on a path a passing test may never
        walk.
        """
        processing = _processing_module()
        used = set()
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "read" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                used.add(first.value)
        self.assertEqual(used, set(processing.GRAPH_MAILBOX_READ_OPERATIONS))
        self.assertEqual(len(used), 12, sorted(used))

    def test_nothing_imports_the_fence_binding_by_value(self):
        """The hazard that has bitten this project twice, checked for a third shape.

        Ten modules import ``clients._fs`` BY VALUE and each patches its own
        copy, so patching one canonical global leaves the other nine live. A
        read fence has exactly the same failure mode: if another module bound
        the resolver or the context variable at import time, fencing
        ``processing``'s copy would leave that module's copy pointing at the
        real provider.
        """
        bound_names = {"_mailbox_reader", "_MAILBOX_READER", "graph_mailbox_reader_scope"}
        offenders = {}
        for path in sorted(REPO_ROOT.rglob("*.py")):
            relative = path.relative_to(REPO_ROOT).as_posix()
            if relative == PROCESSING or "__pycache__" in relative:
                continue
            if relative.startswith("tests/"):
                continue
            try:
                tree = ast.parse(path.read_text(errors="ignore"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                if not node.module.endswith("processing"):
                    continue
                taken = {alias.name for alias in node.names} & bound_names
                if taken:
                    offenders.setdefault(relative, set()).update(taken)
        self.assertEqual(offenders, {})


class ProcessingReadFenceDrivenTests(unittest.TestCase):
    """Driven half. Structural proof is not driven proof.

    Three phases of this project proved properties by test and the first ACTUAL
    run immediately found a real ordering bug. So these tests EXECUTE the
    converged functions with the provider primed to explode: any surviving
    direct call site raises rather than being quietly absent from a count.
    """

    def setUp(self):
        self.processing = _processing_module()

    class _Recorder:
        """Stands in for a certification reader: observes, and calls nothing."""

        def __init__(self, payloads=None):
            self.reads = []
            self.payloads = payloads or {}

        def read(self, operation, url, **kwargs):
            self.reads.append({"operation": operation, "url": url, "kwargs": kwargs})
            return _FakeResponse(self.payloads.get(operation, {}))

    def _exploding_provider(self):
        def explode(*args, **kwargs):
            raise AssertionError(
                "a mailbox read escaped the boundary and reached the provider"
            )
        return explode

    def test_a_fenced_reader_sees_the_reads_and_the_provider_is_never_touched(self):
        from unittest.mock import patch
        processing = self.processing
        recorder = self._Recorder(payloads={
            "mailbox_identity": {"mail": "operator@example.com"},
            "message_envelope_by_id": {"id": "abc", "subject": "s"},
        })
        with patch.object(processing.requests, "get", self._exploding_provider()), \
                processing.graph_mailbox_reader_scope(recorder):
            processing._fetch_graph_message_by_id({"Authorization": "x"}, "abc")
            resolved = processing._resolve_current_mailbox_email({"Authorization": "x"})

        self.assertEqual(resolved, "operator@example.com")
        self.assertEqual(
            [entry["operation"] for entry in recorder.reads],
            ["message_envelope_by_id", "mailbox_identity"],
        )

    def test_a_read_the_fence_refuses_stops_the_lane_rather_than_falling_through(self):
        """Refuse, don't sanitize - and refuse LOUDLY.

        Code reaching for a name it does not own fails as SILENCE in this
        codebase: a broad ``except Exception`` turns a NameError into a clean
        early return. So the refusal is asserted to propagate out of a function
        that has no try/except around its read, proving the fence is not being
        swallowed into a plausible-looking empty result.
        """
        from unittest.mock import patch

        class _Refusing:
            def read(self, operation, url, **kwargs):
                raise self.Refused(operation)

            class Refused(RuntimeError):
                pass

        processing = self.processing
        refusing = _Refusing()
        with patch.object(processing.requests, "get", self._exploding_provider()), \
                processing.graph_mailbox_reader_scope(refusing):
            with self.assertRaises(_Refusing.Refused):
                processing._fetch_graph_message_by_id({"Authorization": "x"}, "abc")

    def test_the_boundary_refuses_an_operation_outside_the_allowlist(self):
        from unittest.mock import patch
        processing = self.processing
        reader = processing.GraphMailboxReader()
        with patch.object(processing.requests, "get", self._exploding_provider()):
            with self.assertRaises(processing.GraphMailboxReadRefused):
                reader.read(
                    "delete_everything",
                    "https://graph.microsoft.com/v1.0/me/messages",
                    headers={},
                )

    def test_the_allowlist_is_checked_before_the_provider_is_reached(self):
        """Ordering, not politeness. A check after the call has already leaked."""
        from unittest.mock import patch
        processing = self.processing
        reader = processing.GraphMailboxReader()
        calls = []
        with patch.object(processing.requests, "get", lambda *a, **k: calls.append(a)):
            with self.assertRaises(processing.GraphMailboxReadRefused):
                reader.read("not_an_operation", "https://graph.microsoft.com/v1.0/me", headers={})
        self.assertEqual(calls, [])

    def test_the_default_reader_uses_the_modules_own_requests_binding(self):
        """The ambient-fallback rule, driven rather than asserted structurally.

        Every existing test in this suite fences ``processing`` by patching
        ``processing.requests``. If the boundary reached for a freshly imported
        ``requests`` instead, all of them would keep passing while fencing
        nothing - the exact shape of the ``clients._fs`` defect. So drive a real
        read through the default reader and require the module's own binding to
        intercept it.
        """
        from unittest.mock import patch
        processing = self.processing
        seen = {}

        def capture(url, **kwargs):
            seen["url"] = url
            return _FakeResponse({"id": "abc"})

        with patch.object(processing.requests, "get", capture):
            result = processing._fetch_graph_message_by_id({"Authorization": "x"}, "abc")
        self.assertEqual(result, {"id": "abc"})
        self.assertIn("/me/messages/abc", seen["url"])

    def test_the_fence_is_restored_when_its_scope_exits(self):
        """A fence that leaks past its scope would silence production reads."""
        processing = self.processing
        recorder = self._Recorder()
        with processing.graph_mailbox_reader_scope(recorder):
            self.assertIs(processing._mailbox_reader(), recorder)
        self.assertIsNot(processing._mailbox_reader(), recorder)

    def test_an_explicit_runtime_reader_outranks_the_ambient_one(self):
        """Delivery resolves runtime.outbound first; reads must resolve the same way."""
        import types
        processing = self.processing
        ambient, injected = self._Recorder(), self._Recorder()
        runtime = types.SimpleNamespace(mailbox_reader=injected)
        with processing.graph_mailbox_reader_scope(ambient):
            self.assertIs(processing._mailbox_reader(runtime), injected)


class RemainingWorkTests(unittest.TestCase):
    """What is left, pinned so the next session starts from a list.

    The list itself is EMITTED by the tool - ``--remaining`` - rather than
    written down, because a hand-written remaining-work list is stale the moment
    anything moves and a stale list is worse than none: it gets trusted. What is
    pinned here is the SHAPE of the remaining job, so that finishing a module
    shows up as a test to update rather than as a silent drift.
    """

    @classmethod
    def setUpClass(cls):
        cls.tool = _load_tool()
        cls.report = cls.tool.build_report(REPO_ROOT)

    def _direct_reads_by_module(self):
        counts = {}
        for op in self.report["operations"]:
            if op["classification"] == "read" and op["route"] == "direct":
                counts[op["module"]] = counts.get(op["module"], 0) + 1
        return counts

    def test_processing_is_the_only_converged_module_so_far(self):
        self.assertNotIn(PROCESSING, self._direct_reads_by_module())

    def test_the_remaining_application_reads_are_pinned_module_by_module(self):
        """Twenty-four direct reads remain on the application surface.

        Ordered by size, the next candidates are ``service_providers`` (5, but
        it is the raw provider - converging it means deciding whether the
        provider or the lanes own the boundary), ``app.py`` (4 reads AND a
        mailbox DELETE, on an operator surface no scheduler lane touches),
        ``email.py`` (4 reads and a DELETE, and the module whose delivery is
        already converged so the seam exists), and ``sent_mail_guard`` (3, all
        paginating and all currently invisible to the URL-literal scan).
        """
        counts = self._direct_reads_by_module()
        application = {
            module: count for module, count in counts.items()
            if self.tool._is_application_module(module)
        }
        self.assertEqual(
            application,
            {
                "app.py": 4,
                "email_automation/email.py": 4,
                "email_automation/email_operations.py": 4,
                "email_automation/file_handling.py": 1,
                "email_automation/followup.py": 1,
                "email_automation/messaging.py": 1,
                "email_automation/operator_replay.py": 1,
                "email_automation/sent_mail_guard.py": 3,
                "email_automation/service_providers.py": 5,
            },
        )
        self.assertEqual(sum(application.values()), 24)

    def test_the_destructive_calls_are_named_rather_than_left_to_be_rediscovered(self):
        """A fixture teardown IS a DELETE, and three of them are still direct.

        Recorded here because a read-convergence task is exactly the context in
        which a destructive call gets overlooked - it is not a read, so it falls
        outside the thing being worked on, which is how app.py's DELETE survived
        a whole send-focused inventory.
        """
        destructive = {
            (op["module"], op["function"])
            for op in self.report["operations"]
            if op["classification"] == "destructive"
        }
        self.assertEqual(
            destructive,
            {
                ("app.py", "delete_matching_emails"),
                ("email_automation/email.py", "_delete_graph_reply_draft"),
                ("email_automation/message_transport.py", "delete_draft"),
            },
        )

    def test_the_remaining_list_renders(self):
        """The operator-facing output is exercised, not just the data behind it."""
        rendered = self.tool._render_remaining(self.report)
        self.assertIn("ALREADY CONVERGED: " + PROCESSING, rendered)
        self.assertIn("email_automation/service_providers.py", rendered)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


if __name__ == "__main__":
    unittest.main()
