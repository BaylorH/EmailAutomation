#!/usr/bin/env python3
"""Every Graph mailbox operation in first-party code, classified by HTTP METHOD.

    python3 scripts/inventory_graph_operations.py                # table
    python3 scripts/inventory_graph_operations.py --format json  # machine-readable
    python3 scripts/inventory_graph_operations.py --reconcile    # 19 vs 33

WHY THIS EXISTS AS A TOOL RATHER THAN A NUMBER IN A REPORT
-----------------------------------------------------------
Delivery converged and its inventory is under test. Reads did not, and the
project carried two disagreeing read counts - 19 and 33 - with no way to ask
either of them a question. A number in a document cannot be re-derived after
the code moves; this can.

CLASSIFY BY VERB, NOT BY URL TEXT
---------------------------------
Two scanning traps have already cost this project real findings, and both come
from classifying by what a URL literal SAYS:

  * A module that assembles its URL by concatenation shows no matching literal.
    ``service_providers.py`` is send-capable by name while a URL-literal sweep
    reported it clean.
  * An inventory scoped to one verb is blind to every other. A "what can send?"
    sweep reported ``app.py`` clean while it holds three mailbox reads AND a
    mailbox DELETE.

So the unit here is the CALL SITE and its HTTP method. A DELETE is destructive
wherever it points. A GET is a read wherever it points. The URL is recorded as
evidence, never used to decide the class.

WHAT IT REFUSES TO GUESS
------------------------
The resolver follows string constants, f-strings, ``+`` concatenation, local
assignments, parameter defaults, module constants, and lambda argument defaults.
When it cannot resolve a URL it says so - the site lands in
``unresolvedHttpCallSites`` rather than being dropped. Likewise, HTTP verbs
called on a receiver outside the known allowlist land in ``unrecognisedReceivers``
rather than being silently ignored. A scan that quietly skips what it cannot
resolve reports a smaller, cleaner inventory than the truth, which is precisely
the failure this file exists to prevent. Allowlist, never denylist; report,
never sanitize.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

HTTP_VERBS = ("get", "post", "patch", "put", "delete")

# Kept byte-identical to tests/test_graph_send_inventory.py's SEND_SUFFIXES.
# ``createReply``/``createReplyAll`` are deliberately absent: they create a
# DRAFT and are classified "write". Widening this here would silently change the
# send figures the certification program already proved, under cover of a read
# inventory - a behaviour change smuggled into a measurement.
SEND_SUFFIXES = ("/send", "/sendMail", "/reply", "/replyAll")

# Receivers an HTTP verb may legitimately be called on. This allowlist is the
# tool's OWN blind spot, so anything outside it is reported rather than skipped.
HTTP_RECEIVERS = frozenset({"requests", "http", "session", "_request", "_http"})

SKIP_PARTS = {".git", "tests", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache"}

# A resolved URL template names the mailbox when the ``/me`` segment sits
# directly under a Graph API root - either spelled out, or standing in as "{}"
# because the base was a parameter. Anchoring it this way is what lets the bare
# ``/me`` identity endpoint be counted; a plain ``"/me/" in url`` test cannot
# see it, and that blindness is one of the sites in the 19-vs-33 delta.
MAILBOX_PATH_RE = re.compile(r"(?:^|\{\}|graph\.microsoft\.com/v[0-9.]+)/me(?:/|\?|$)")

# Scope A: the product lanes the certification runtime actually drives. The
# shared boundary, the quarantined legacy send module, the raw provider, the
# operator Flask app, the local operator utility and the by-hand scripts are all
# real mailbox surfaces - they are simply not this scope, which is exactly why
# the two figures differ rather than one being wrong.
SCOPE_A_MODULES = (
    "email_automation/processing.py",
    "email_automation/email.py",
    "email_automation/followup.py",
    "email_automation/messaging.py",
    "email_automation/file_handling.py",
    "email_automation/sent_mail_guard.py",
)

SCOPE_A_EXCLUSION_REASONS = {
    "email_automation/message_transport.py": "the shared delivery boundary itself, not a lane",
    "email_automation/email_operations.py": "legacy send module, disabled by default and imported by nothing",
    "email_automation/service_providers.py": "raw provider primitives beneath the lanes",
    "email_automation/operator_replay.py": "local operator recovery utility, not deployed",
    "app.py": "operator Flask surface, not a scheduler lane",
}

# Scope B: the DEPLOYED application surface. Everything that ships in the image
# and can be reached in production - the six lanes plus the operator app, the
# raw provider, the quarantined legacy send module and the operator utility.
# The shared delivery boundary is excluded because it is the convergence TARGET;
# counting its reads as unconverged debt would make convergence look like it
# increased the number.
SCOPE_B_EXCLUDED = ("email_automation/message_transport.py",)


def _is_application_module(module: str) -> bool:
    if module in SCOPE_B_EXCLUDED:
        return False
    return module == "app.py" or (
        module.startswith("email_automation/") and module.count("/") == 1
    )


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def _resolve(node: ast.AST, env: Dict[str, str], depth: int = 0) -> Optional[str]:
    """Best-effort string value of an expression, or None.

    ``None`` means "unknown", and every caller must treat it as unknown rather
    than as empty. Returning "" for an unresolved URL would make an unknown site
    look like a non-Graph one.
    """
    if depth > 8:
        return None
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, ast.JoinedStr):
        parts: List[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                inner = _resolve(value.value, env, depth + 1)
                # "{}" is a deliberate placeholder, not an empty string: it keeps
                # the PATH SHAPE intact so "{}/me/messages/{}" still reads as a
                # mailbox URL when the base arrived as a parameter.
                parts.append(inner if inner is not None else "{}")
            else:
                parts.append("{}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _resolve(node.left, env, depth + 1)
        right = _resolve(node.right, env, depth + 1)
        if left is None and right is None:
            return None
        return (left if left is not None else "{}") + (right if right is not None else "{}")
    return None


def _receiver_name(func: ast.Attribute) -> str:
    target = func.value
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def _classify(method: str, url: Optional[str]) -> str:
    """Verb first. The URL only ever narrows write -> send, never the reverse."""
    if method == "delete":
        return "destructive"
    if method == "get":
        return "read"
    if url and url.rstrip().endswith(SEND_SUFFIXES):
        return "send"
    return "write"


class _ModuleScanner:
    """Walks one module in statement order, carrying a per-scope binding table.

    Statement ORDER is what makes a local ``url = ...`` visible to the calls
    below it and not to the ones above, and per-SCOPE bindings are what stop
    every ``url`` in a file collapsing to one value. A module-flat table gets
    both wrong in the same direction: it reports a URL confidently, and reports
    the wrong one.
    """

    def __init__(self, module: str, tree: ast.Module) -> None:
        self.module = module
        self.tree = tree
        self.operations: List[Dict[str, Any]] = []
        self.unresolved: List[Dict[str, Any]] = []
        self.unrecognised: List[Dict[str, Any]] = []

    # -- entry ------------------------------------------------------------

    def scan(self) -> None:
        self._walk_body(self.tree.body, {}, "<module>")

    # -- statements -------------------------------------------------------

    def _walk_body(self, body: List[ast.stmt], env: Dict[str, str], scope: str) -> None:
        for stmt in body:
            self._walk_stmt(stmt, env, scope)

    def _walk_stmt(self, stmt: ast.stmt, env: Dict[str, str], scope: str) -> None:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            child = dict(env)
            self._bind_defaults(stmt.args, child, env)
            self._walk_body(stmt.body, child, stmt.name)
            return

        if isinstance(stmt, ast.ClassDef):
            self._walk_body(stmt.body, dict(env), scope)
            return

        target_name = None
        value = None
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            target_name, value = stmt.targets[0].id, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            target_name, value = stmt.target.id, stmt.value

        if target_name is not None:
            self._scan_expr(value, env, scope)
            resolved = _resolve(value, env)
            if resolved is None:
                # UNBIND rather than keep the old value. A name reassigned to
                # something unknowable (``url = next_link``) must stop resolving,
                # or later call sites inherit a stale URL and the inventory
                # asserts a target the code no longer uses.
                env.pop(target_name, None)
            else:
                env[target_name] = resolved
            return

        for child in ast.iter_child_nodes(stmt):
            if isinstance(child, ast.stmt):
                self._walk_stmt(child, env, scope)
            elif isinstance(child, ast.expr):
                self._scan_expr(child, env, scope)
            elif isinstance(child, (ast.excepthandler, ast.match_case)):
                for grand in ast.iter_child_nodes(child):
                    if isinstance(grand, ast.stmt):
                        self._walk_stmt(grand, env, scope)
                    elif isinstance(grand, ast.expr):
                        self._scan_expr(grand, env, scope)

    def _bind_defaults(self, args: ast.arguments, child: Dict[str, str], outer: Dict[str, str]) -> None:
        """Parameter defaults are bindings too.

        ``base: str = "https://graph.microsoft.com/v1.0"`` is how three of the
        read sites in ``sent_mail_guard.py`` know where they point, and a scanner
        that only reads assignments cannot see it.
        """
        positional = list(args.posonlyargs) + list(args.args)
        for arg, default in zip(positional[len(positional) - len(args.defaults):], args.defaults):
            resolved = _resolve(default, outer)
            if resolved is not None:
                child[arg.arg] = resolved
            else:
                child.pop(arg.arg, None)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults):
            if default is None:
                child.pop(arg.arg, None)
                continue
            resolved = _resolve(default, outer)
            if resolved is not None:
                child[arg.arg] = resolved
            else:
                child.pop(arg.arg, None)

    # -- expressions ------------------------------------------------------

    def _scan_expr(self, node: Optional[ast.expr], env: Dict[str, str], scope: str) -> None:
        if node is None:
            return

        if isinstance(node, ast.Lambda):
            # ``lambda u=url, p=params: requests.get(u, ...)`` - the retry idiom
            # used throughout this codebase. The URL reaches the call through a
            # DEFAULT, so a scanner that only looks at the call's own arguments
            # sees a bare name and gives up. Two live read sites hide here.
            child = dict(env)
            self._bind_defaults(node.args, child, env)
            self._scan_expr(node.body, child, scope)
            return

        if isinstance(node, ast.Call):
            self._scan_call(node, env, scope)

        for child_node in ast.iter_child_nodes(node):
            if isinstance(child_node, ast.expr):
                self._scan_expr(child_node, env, scope)
            elif isinstance(child_node, ast.comprehension):
                self._scan_expr(child_node.iter, env, scope)

    def _scan_call(self, node: ast.Call, env: Dict[str, str], scope: str) -> None:
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in HTTP_VERBS:
            return

        keywords = {kw.arg for kw in node.keywords if kw.arg}
        url_node: Optional[ast.expr] = node.args[0] if node.args else None
        if url_node is None:
            for kw in node.keywords:
                if kw.arg == "url":
                    url_node = kw.value
        if url_node is None:
            return

        receiver = _receiver_name(func)
        looks_like_http = "headers" in keywords or "timeout" in keywords
        if receiver not in HTTP_RECEIVERS:
            if looks_like_http:
                # Shaped like an HTTP call but on a receiver this tool does not
                # know. Reported, because an unknown receiver is precisely how a
                # mailbox call would slip past a scanner like this one.
                self.unrecognised.append({
                    "module": self.module,
                    "line": node.lineno,
                    "function": scope,
                    "receiver": receiver,
                    "method": func.attr,
                })
            return

        url = _resolve(url_node, env)
        if url is None:
            self.unresolved.append({
                "module": self.module,
                "line": node.lineno,
                "function": scope,
                "method": func.attr,
                "reason": "url expression did not resolve to a string template",
            })
            return

        if not MAILBOX_PATH_RE.search(url):
            return

        self.operations.append({
            "module": self.module,
            "line": node.lineno,
            "function": scope,
            "method": func.attr,
            "classification": _classify(func.attr, url),
            "url": url,
        })


# ---------------------------------------------------------------------------
# the legacy URL-literal scan, reproduced for reconciliation
# ---------------------------------------------------------------------------


def url_literal_scan(module: str, tree: ast.Module) -> List[Dict[str, Any]]:
    """A faithful reproduction of tests/test_graph_send_inventory.py::_mailbox_calls.

    Reproduced rather than imported so this tool stands alone, and reproduced
    rather than improved because its BLIND SPOTS are the measurement: the whole
    point is to show which sites the 19 figure cannot see. Its three limitations
    are preserved exactly - a module-flat binding table, a first-argument-only
    literal test, and a ``"/me/" in url`` substring check that the bare ``/me``
    identity endpoint does not satisfy.
    """
    def literal(node: ast.AST) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(
                v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}"
                for v in node.values
            )
        return ""

    assigned: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Name):
                text = literal(node.value)
                if text:
                    assigned[node.targets[0].id] = text

    owner: Dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                owner.setdefault(id(child), node.name)

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in HTTP_VERBS or not node.args:
            continue
        url = literal(node.args[0])
        if not url and isinstance(node.args[0], ast.Name):
            url = assigned.get(node.args[0].id, "")
        if "/me/" not in url:
            continue
        found.append({
            "module": module,
            "line": node.lineno,
            "function": owner.get(id(node), "<module>"),
            "method": func.attr,
            "classification": _classify(func.attr, url),
            "url": url,
        })
    return found


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _python_files(root: Path) -> List[Tuple[str, Path]]:
    files = []
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if SKIP_PARTS & set(relative.parts):
            continue
        files.append((relative.as_posix(), path))
    return sorted(files)


def build_report(root: Path = REPO_ROOT) -> Dict[str, Any]:
    operations: List[Dict[str, Any]] = []
    url_literal: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    unrecognised: List[Dict[str, Any]] = []

    for module, path in _python_files(root):
        source = path.read_text(errors="ignore")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            unresolved.append({
                "module": module, "line": 0, "function": "<module>",
                "method": "-", "reason": "module does not parse",
            })
            continue
        scanner = _ModuleScanner(module, tree)
        scanner.scan()
        operations.extend(scanner.operations)
        unresolved.extend(scanner.unresolved)
        unrecognised.extend(scanner.unrecognised)
        url_literal.extend(url_literal_scan(module, tree))

    operations.sort(key=lambda op: (op["module"], op["line"]))
    url_literal.sort(key=lambda op: (op["module"], op["line"]))

    by_module: Dict[str, Dict[str, int]] = {}
    for op in operations:
        counts = by_module.setdefault(op["module"], {})
        counts[op["classification"]] = counts.get(op["classification"], 0) + 1

    literal_read_sites = {
        (op["module"], op["line"]) for op in url_literal if op["classification"] == "read"
    }

    scope_a_by_module: Dict[str, int] = {}
    for op in url_literal:
        if op["classification"] != "read" or op["module"] not in SCOPE_A_MODULES:
            continue
        scope_a_by_module[op["module"]] = scope_a_by_module.get(op["module"], 0) + 1

    scope_b_by_module: Dict[str, int] = {}
    scope_c_by_module: Dict[str, int] = {}
    for op in operations:
        if op["classification"] != "read":
            continue
        scope_c_by_module[op["module"]] = scope_c_by_module.get(op["module"], 0) + 1
        if _is_application_module(op["module"]):
            scope_b_by_module[op["module"]] = scope_b_by_module.get(op["module"], 0) + 1

    # --- B minus A: what the narrower figure does not count, itemised --------
    scope_b_only = []
    for op in operations:
        if op["classification"] != "read" or not _is_application_module(op["module"]):
            continue
        if op["module"] in SCOPE_A_MODULES and (op["module"], op["line"]) in literal_read_sites:
            continue
        if op["module"] not in SCOPE_A_MODULES:
            reason = "module_not_in_scope"
            detail = SCOPE_A_EXCLUSION_REASONS.get(
                op["module"], "not a certification-driven product lane"
            )
        else:
            reason = "url_literal_blind_spot"
            detail = (
                "bare /me identity endpoint; '/me/' does not occur in it"
                if op["url"].rstrip().endswith("/me")
                else "URL reaches the call through a binding the literal scan cannot follow"
            )
        entry = dict(op)
        entry["excludedFromScopeABy"] = reason
        entry["detail"] = detail
        scope_b_only.append(entry)

    # --- A minus B: sites the URL-literal scan reports that DO NOT EXIST -----
    #
    # The module-flat binding table does not merely undercount. When a file
    # assigns several different URLs to the same name, every unresolved use of
    # that name inherits whichever assignment the walker saw last - so a token
    # endpoint or a generic URL fetcher can be reported as a mailbox read. These
    # are phantoms, and they were being counted.
    method_sites = {(op["module"], op["line"]) for op in operations}
    phantom_reads = [
        {
            "module": op["module"],
            "line": op["line"],
            "function": op["function"],
            "attributedUrl": op["url"],
            "detail": (
                "the URL-literal scan attributes a mailbox URL to this call by "
                "inheriting an unrelated assignment to the same name elsewhere in "
                "the module; the call site does not resolve to the mailbox"
            ),
        }
        for op in url_literal
        if op["classification"] == "read" and (op["module"], op["line"]) not in method_sites
    ]

    # --- same site, different verdict ---------------------------------------
    method_by_site = {(op["module"], op["line"]): op for op in operations}
    misclassified = []
    for op in url_literal:
        site = (op["module"], op["line"])
        counterpart = method_by_site.get(site)
        if counterpart and counterpart["classification"] != op["classification"]:
            misclassified.append({
                "module": op["module"],
                "line": op["line"],
                "function": op["function"],
                "urlLiteralSays": op["classification"],
                "methodScanSays": counterpart["classification"],
                "resolvedUrl": counterpart["url"],
            })

    return {
        "schemaVersion": 1,
        "operations": operations,
        "urlLiteralOperations": url_literal,
        "unresolvedHttpCallSites": unresolved,
        "unrecognisedReceivers": unrecognised,
        "byModule": by_module,
        "scopeA": {
            "definition": (
                "Unconverged mailbox READS in the six certification-driven product "
                "lanes, as the URL-literal scan sees them. This is the project "
                "record's 19."
            ),
            "modules": list(SCOPE_A_MODULES),
            "byModule": scope_a_by_module,
            "readCount": sum(scope_a_by_module.values()),
        },
        "scopeB": {
            "definition": (
                "Every Graph mailbox READ in the DEPLOYED application surface - "
                "email_automation/*.py plus app.py, excluding the shared delivery "
                "boundary - classified by HTTP method. This is the scope the "
                "'33 across 9 modules' figure was reaching for."
            ),
            "byModule": scope_b_by_module,
            "readCount": sum(scope_b_by_module.values()),
        },
        "scopeC": {
            "definition": (
                "Every Graph mailbox READ call site in first-party non-test Python, "
                "including standalone legacy scripts and by-hand tooling."
            ),
            "byModule": scope_c_by_module,
            "readCount": sum(scope_c_by_module.values()),
        },
        "reconciliation": {
            "why": (
                "The two figures are not a disagreement about the code. Scope A is "
                "narrower in TWO independent ways: it looks at six modules rather "
                "than the whole application surface, and inside those six it "
                "inherits the URL-literal scan's blind spots. The scan is also "
                "not merely conservative - it reports reads that are not there "
                "and grades three real sends as ordinary writes."
            ),
            "scopeBOnly": scope_b_only,
            "urlLiteralPhantomReads": phantom_reads,
            "urlLiteralMisclassifications": misclassified,
        },
    }


def _render_table(report: Dict[str, Any]) -> str:
    lines = ["module                                              read write dest send",
             "-" * 74]
    for module in sorted(report["byModule"]):
        counts = report["byModule"][module]
        lines.append(
            f"{module:<50s} {counts.get('read',0):>4d} {counts.get('write',0):>4d} "
            f"{counts.get('destructive',0):>4d} {counts.get('send',0):>4d}"
        )
    totals = {k: 0 for k in ("read", "write", "destructive", "send")}
    for counts in report["byModule"].values():
        for key, value in counts.items():
            totals[key] += value
    lines.append("-" * 74)
    lines.append(
        f"{'TOTAL':<50s} {totals['read']:>4d} {totals['write']:>4d} "
        f"{totals['destructive']:>4d} {totals['send']:>4d}"
    )
    lines.append("")
    lines.append(f"unresolved HTTP call sites : {len(report['unresolvedHttpCallSites'])}")
    lines.append(f"unrecognised receivers     : {len(report['unrecognisedReceivers'])}")
    return "\n".join(lines)


def _render_sites(report: Dict[str, Any]) -> str:
    lines = []
    current = None
    for op in report["operations"]:
        if op["module"] != current:
            current = op["module"]
            lines.append(f"\n{current}")
        lines.append(
            f"  {op['line']:>6d}  {op['classification']:<11s} {op['method']:<6s} "
            f"{op['function']:<45s} {op['url']}"
        )
    return "\n".join(lines)


def _render_reconciliation(report: Dict[str, Any]) -> str:
    lines = []
    for key in ("scopeA", "scopeB", "scopeC"):
        scope = report[key]
        lines.append(f"{key.upper():<8s} {scope['definition']}")
        lines.append(
            f"         reads={scope['readCount']} modules={len(scope['byModule'])}"
        )
        for module in sorted(scope["byModule"]):
            lines.append(f"           {module:<52s} {scope['byModule'][module]}")
        lines.append("")

    reconciliation = report["reconciliation"]
    lines += ["IN SCOPE B, NOT IN SCOPE A", "  " + reconciliation["why"], ""]
    for entry in reconciliation["scopeBOnly"]:
        lines.append(
            f"  {entry['module']}:{entry['line']:<6d} {entry['function']:<45s} "
            f"{entry['excludedFromScopeABy']:<24s} {entry['detail']}"
        )

    lines += ["", "PHANTOM READS the URL-literal scan reports and the method scan does not"]
    for entry in reconciliation["urlLiteralPhantomReads"]:
        lines.append(
            f"  {entry['module']}:{entry['line']:<6d} {entry['function']:<45s} "
            f"attributed {entry['attributedUrl']}"
        )

    lines += ["", "SAME SITE, DIFFERENT VERDICT"]
    for entry in reconciliation["urlLiteralMisclassifications"]:
        lines.append(
            f"  {entry['module']}:{entry['line']:<6d} {entry['function']:<30s} "
            f"url-literal={entry['urlLiteralSays']:<8s} method={entry['methodScanSays']:<8s} "
            f"{entry['resolvedUrl']}"
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(REPO_ROOT))
    parser.add_argument("--format", choices=("table", "json", "sites"), default="table")
    parser.add_argument("--reconcile", action="store_true",
                        help="explain the 19-vs-33 read figures as two scopes")
    args = parser.parse_args(argv)

    report = build_report(Path(args.root).resolve())

    if args.reconcile:
        print(_render_reconciliation(report))
        return 0
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    elif args.format == "sites":
        print(_render_sites(report).lstrip("\n"))
    else:
        print(_render_table(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
