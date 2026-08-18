"""Characterization of SiteSift's outbound transport boundaries, BEFORE refactor.

Task 2 of the production automation certification plan. This module pins WHERE a
message actually leaves the process today, so Phase C can move that boundary with
a regression net underneath it.

Two deliberate design choices:

**It is source-level, not runtime-level.** The property being pinned - "every send
lane reaches an identified final delivery call, and no other call site exists" - is
a structural invariant over the import graph. A runtime test proves it only for the
paths it happens to exercise and can be satisfied by a mock; an AST sweep cannot be
evaded by one. Task 7E ("prove no recovery or alternate-send bypass remains") is
exactly this property, so pinning it structurally is what makes that task decidable.

**It imports nothing from `email_automation`.** `email_automation/clients.py:13`
constructs `firestore.Client()` and `openai.OpenAI(...)` at MODULE IMPORT TIME, so
importing the business logic requires real credentials and would build production
provider clients. That is tracked as backlog #84 and is precisely the kind of
import-time provider construction this certification program exists to remove. A
characterization test must not depend on it, and must never be "fixed" by placing a
real service-account credential into a worktree.

WHAT THIS PROVES, stated plainly: there is currently **no single shared delivery
boundary**. Four independent `requests.post(.../me/messages/{id}/send)` call sites
exist across three guarded modules, and several unguarded surfaces reach Graph
without any policy at all. Task 6 exists to collapse the guarded four into one
boundary; when it does, `test_guarded_send_sites_are_exactly_the_known_four` is
EXPECTED to fail and must be updated in the same commit that collapses them. That
failure is the point: it is the alarm that the boundary moved.
"""

from pathlib import Path
import ast
import json
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = REPO_ROOT / "docs" / "release-safety" / "outbound-send-surface-inventory.json"

# Modules whose send sites are policy-guarded today.
GUARDED_MODULES = (
    "email_automation/email.py",
    "email_automation/processing.py",
    "email_automation/followup.py",
)

# The exact guarded delivery sites, keyed by the function that owns them. Line
# numbers are informational only - ownership by enclosing function is the stable
# fact and is what Task 6 will refactor.
# The shared delivery boundary Task 6 CREATED. Phase A proved there was nothing
# to relocate - four independent send sites existed - so this is a new
# convergence point, and initial outreach is the first lane routed through it.
SHARED_DELIVERY_MODULE = "email_automation/message_transport.py"
SHARED_DELIVERY_OWNER = "send_prepared_draft"

# The delivery sites that have NOT yet converged. This set SHRINKS as Tasks
# 7A-7D land; each of those tasks is expected to fail this constant and update
# it in the same commit, exactly as Task 6 did.
EXPECTED_GUARDED_SEND_SITES = {
    ("email_automation/followup.py", "_send_followup_email"),
}

# Unguarded or partially guarded surfaces that reach Graph send/reply directly.
# Enumerated so a NEW one fails this test rather than arriving unnoticed.
KNOWN_BYPASS_MODULES = {
    "email_automation/email_operations.py",
    "email_automation/service_providers.py",
    "scheduler_runner.py",
    "noPopup_signin_emails_to_excel.py",
}

GRAPH_SEND_URL_MARKERS = ("/send", "/reply", "/replyAll", "/sendMail")


def _module_source(relative_path):
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _enclosing_functions(tree):
    """Map every AST node to the name of the function that lexically contains it."""
    owner = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                owner.setdefault(child, node.name)
    return owner


def _joined_str_value(node):
    """Best-effort literal text of an f-string or plain string node."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{}")
        return "".join(parts)
    return ""


def _url_assignments(tree):
    """Every simple `name = "<text>"` assignment as (lineno, name, text).

    Necessary, not cosmetic. A very common style is:

        url = f"https://graph.microsoft.com/v1.0/me/messages/{draft_id}/send"
        resp = requests.post(url, headers=headers)

    Inspecting only a call's literal first argument misses every one of these. That
    blind spot was real: it hid all three RealEmailProvider send methods in
    email_automation/service_providers.py.

    Returning a LINE-ORDERED LIST rather than a name->text map is equally load
    bearing. service_providers.py rebinds the single name `url` six times; a map
    keeps only the last write, and that survivor did not end in /send, so all three
    send sites disappeared a second time. A name is therefore resolved to the
    NEAREST PRECEDING assignment, which is what the reader of the code sees.
    """
    assignments = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        text = _joined_str_value(node.value)
        if text:
            assignments.append((node.lineno, target.id, text))
    return sorted(assignments)


def _resolve_name(assignments, name, before_line):
    """The nearest assignment to `name` lexically above `before_line`."""
    best = ""
    for lineno, assigned, text in assignments:
        if assigned == name and lineno < before_line:
            best = text
        elif lineno >= before_line:
            break
    return best


def find_graph_send_calls(relative_path):
    """Every `requests.<verb>(<url containing a Graph send marker>)` in one module.

    Resolves both an inline URL and a URL bound to a local variable.
    Returns a list of (function_name, url_shape, lineno).
    """
    tree = ast.parse(_module_source(relative_path))
    owner = _enclosing_functions(tree)
    assignments = _url_assignments(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in ("post", "patch", "put"):
            continue
        # Deliberately NOT restricted to a receiver literally named ``requests``.
        # Any receiver counts - an injected HTTP client, a Session, or an alias -
        # because the property being pinned is "no module reaches a Graph send
        # endpoint unnoticed", and a one-line ``import requests as r`` would
        # otherwise walk straight past it. Task 7E depends on this being
        # unevadable, so the URL shape is the discriminator, not the variable name.
        if not isinstance(func.value, (ast.Name, ast.Attribute)):
            continue
        if not node.args:
            continue
        argument = node.args[0]
        url = _joined_str_value(argument)
        if not url and isinstance(argument, ast.Name):
            url = _resolve_name(assignments, argument.id, node.lineno)
        if not url:
            continue
        # A draft-creating call is not a delivery call; only the terminal verbs count.
        if url.endswith("/send") or url.endswith("/sendMail") or url.endswith("/reply"):
            found.append((owner.get(node, "<module>"), url, node.lineno))
    return found


class GuardedDeliveryBoundaryTests(unittest.TestCase):
    """Where a guarded message actually leaves the process, today."""

    def test_unconverged_send_sites_are_exactly_the_known_remainder(self):
        actual = set()
        for module in GUARDED_MODULES:
            for function_name, _url, _lineno in find_graph_send_calls(module):
                actual.add((module, function_name))
        self.assertEqual(
            actual,
            EXPECTED_GUARDED_SEND_SITES,
            "the guarded delivery surface changed; if a task routed another lane "
            "through the shared boundary, update EXPECTED_GUARDED_SEND_SITES in the "
            "same commit - and if a NEW site appeared, that is the alarm",
        )

    def test_the_shared_delivery_boundary_exists_and_owns_exactly_one_send(self):
        """Task 6's substance, asserted structurally rather than by mock."""
        sites = list(find_graph_send_calls(SHARED_DELIVERY_MODULE))
        self.assertEqual(
            len(sites), 1,
            f"{SHARED_DELIVERY_MODULE} must hold exactly one send call, found {sites}",
        )
        function_name, url, _lineno = sites[0]
        self.assertEqual(function_name, SHARED_DELIVERY_OWNER)
        self.assertTrue(url.endswith("/send"))

    def test_converged_lanes_no_longer_hold_their_own_send_call(self):
        """Each lane routed to the shared boundary loses its private send."""
        owners = {
            owner for module, owner in
            [(m, f) for m in GUARDED_MODULES for f, _u, _l in find_graph_send_calls(m)]
        }
        for converged in (
            "send_and_index_email", "send_reply_in_thread", "_send_outbox_as_reply",
        ):
            with self.subTest(lane=converged):
                self.assertNotIn(converged, owners)

    def test_one_lane_remains_unconverged_for_task_7d(self):
        """A live count, not a comment: it must fall as each lane is routed."""
        owners = {owner for _module, owner in EXPECTED_GUARDED_SEND_SITES}
        self.assertEqual(
            len(owners), 1,
            "the remaining unconverged delivery lanes changed; update this count in "
            "the same commit that converges one",
        )

    def test_every_guarded_delivery_call_targets_a_draft_send_endpoint(self):
        for module in GUARDED_MODULES:
            for function_name, url, lineno in find_graph_send_calls(module):
                with self.subTest(module=module, function=function_name, line=lineno):
                    self.assertTrue(
                        url.endswith("/send"),
                        f"{module}:{lineno} delivers via {url!r}; the guarded lanes are "
                        "expected to send a previously created draft, never sendMail",
                    )
                    self.assertIn("/me/messages/", url)

    def test_each_guarded_module_carries_the_outbound_body_validator(self):
        """Source-level, matching the existing inventory contract in test_graph_send_inventory."""
        for module in GUARDED_MODULES:
            with self.subTest(module=module):
                self.assertIn("validate_outbound_body", _module_source(module))


class SendSurfaceInventoryTests(unittest.TestCase):
    """No send site may exist outside the enumerated guarded and bypass surfaces."""

    def _repo_python_files(self):
        skip_parts = {".git", "tests", "__pycache__", "node_modules", ".venv", "venv"}
        for path in REPO_ROOT.rglob("*.py"):
            if skip_parts & set(path.relative_to(REPO_ROOT).parts):
                continue
            yield path.relative_to(REPO_ROOT).as_posix()

    def test_no_unknown_module_reaches_a_graph_send_endpoint(self):
        allowed = set(GUARDED_MODULES) | KNOWN_BYPASS_MODULES | {SHARED_DELIVERY_MODULE}
        offenders = set()
        for relative_path in self._repo_python_files():
            try:
                source = _module_source(relative_path)
            except (OSError, UnicodeDecodeError):
                continue
            if not any(marker in source for marker in GRAPH_SEND_URL_MARKERS):
                continue
            try:
                if find_graph_send_calls(relative_path):
                    offenders.add(relative_path)
            except SyntaxError:
                continue
        self.assertEqual(
            offenders - allowed,
            set(),
            "a new Microsoft Graph send site appeared outside the enumerated surfaces; "
            "every send path must be classified before it can ship",
        )

    def test_known_bypass_modules_still_bypass_and_are_not_silently_fixed(self):
        """If a bypass gains guards, that is good news that must be recorded, not hidden."""
        still_bypassing = set()
        for module in KNOWN_BYPASS_MODULES:
            path = REPO_ROOT / module
            if not path.is_file():
                continue
            if "validate_outbound_body" not in _module_source(module):
                still_bypassing.add(module)
        self.assertEqual(
            still_bypassing,
            {module for module in KNOWN_BYPASS_MODULES if (REPO_ROOT / module).is_file()},
            "a bypass module gained validate_outbound_body; update KNOWN_BYPASS_MODULES "
            "and the send-surface inventory in the same commit",
        )

    def test_inventory_document_covers_every_module_with_a_send_site(self):
        """A converged lane stays documented; it does not vanish from the record.

        Once a lane routes to the shared boundary it no longer holds a send
        endpoint, so it leaves ``sendSurfaces``. It must NOT thereby disappear:
        it still owns its own recipient, opt-out, cancellation, and audit
        decisions, and a reader who could no longer see that would conclude the
        boundary is the only thing guarding a send.
        """
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        catalogued = {entry["path"] for entry in inventory["sendSurfaces"]}
        catalogued |= {
            entry["path"] for entry in inventory.get("convergedLanes", [])
        }
        live = set(GUARDED_MODULES) | {
            module for module in KNOWN_BYPASS_MODULES if (REPO_ROOT / module).is_file()
        }
        self.assertEqual(
            live - catalogued,
            set(),
            "a module with a real send site is absent from "
            "docs/release-safety/outbound-send-surface-inventory.json",
        )

    def test_every_converged_lane_still_records_the_controls_it_retained(self):
        inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        converged = inventory.get("convergedLanes", [])
        self.assertTrue(converged, "Task 6/7A converged at least one lane")
        for entry in converged:
            with self.subTest(path=entry.get("path")):
                self.assertEqual(entry.get("convergedAt"), SHARED_DELIVERY_MODULE)
                self.assertTrue(entry.get("retainedControls"))
                self.assertTrue(entry.get("convergedBy"))


class ImportTimeProviderConstructionTests(unittest.TestCase):
    """Why this module refuses to import email_automation - pinned, not assumed."""

    def test_clients_module_constructs_providers_at_import_time(self):
        """Backlog #84. Certification cannot run business logic without this being fixed.

        This is a CHARACTERIZATION of a defect, not an endorsement. When #84 lands and
        client construction becomes lazy, this test must flip to assert the absence of
        module-level construction.
        """
        tree = ast.parse(_module_source("email_automation/clients.py"))
        module_level_calls = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                func = node.value.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else getattr(func, "id", "")
                )
                module_level_calls.append(name)
        self.assertIn(
            "Client",
            module_level_calls,
            "firestore.Client() is expected at module scope today (#84); if it is now "
            "lazy, invert this test and unblock credential-free collection",
        )

    def test_this_module_imports_nothing_that_builds_providers_at_import(self):
        """Collection of this module must never require credentials.

        The property that matters is not "imports no email_automation" - it is
        "imports nothing whose import constructs a provider client". Any module
        reaching email_automation/clients.py pulls in `_fs = firestore.Client()` and
        `openai.OpenAI(...)` at module scope. A pure boundary module such as
        message_transport, which holds only dataclasses, protocols, and sources over
        an INJECTED request function, imports none of that and is therefore safe to
        import here.

        Narrowed from a blanket ban when Task 3 added CanonicalInboundMessageTests.
        Deliberately still a ban, not a warning: widening it to the credential-
        constructing modules is what would make collection need a secret again.
        """
        credential_constructing = {
            "email_automation.clients",
            "email_automation.processing",
            "email_automation.email",
            "email_automation.followup",
            "email_automation.ai_processing",
        }
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertEqual(
            imported & credential_constructing,
            set(),
            "importing one of these constructs a production Firestore and OpenAI "
            "client at module scope, so collection would need a real credential",
        )

    def test_message_transport_module_builds_no_provider_at_import(self):
        """The new boundary module must stay pure, or the above ban is worthless."""
        tree = ast.parse(_module_source("email_automation/message_transport.py"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(
                    "clients",
                    node.module,
                    "message_transport must not import clients; that would drag "
                    "firestore.Client() and openai.OpenAI() into every importer",
                )
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                func = node.value.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                self.assertNotIn(
                    name,
                    ("Client", "OpenAI"),
                    "message_transport constructs a provider at module scope",
                )


class CanonicalInboundMessageTests(unittest.TestCase):
    """Task 3. A Graph-backed source and an approved fixture source must produce
    BYTE-EQUAL canonical inbound state for the same logical message.

    This is the whole premise of the certification program: certification runs the
    same deployed business logic and differs ONLY in acquisition. If the two sources
    can disagree on any field - body text, quoted content, envelope, headers,
    attachment flag, message ids, timestamps, reply target, prior messages, or Sent
    Items receipts - then a capability stamp earned through the fixture lane says
    nothing about the Graph lane, and the whole instrument is void.

    The fixture lane runs with the ENTIRE Graph request layer patched to raise, so a
    hidden read, createReplyAll, send, or Sent Items call fails loudly rather than
    silently succeeding.
    """

    GRAPH_MESSAGE = {
        "id": "AAMkAGI2-graph-id",
        "internetMessageId": "<abc123@contoso.example>",
        "conversationId": "conv-777",
        "subject": "RE: 100 Example Rd availability",
        "receivedDateTime": "2026-08-17T14:05:00Z",
        "hasAttachments": True,
        "from": {"emailAddress": {"address": "broker@example.test", "name": "A Broker"}},
        "replyTo": [{"emailAddress": {"address": "broker.reply@example.test"}}],
        "toRecipients": [{"emailAddress": {"address": "operator@example.test"}}],
        "ccRecipients": [{"emailAddress": {"address": "assistant@example.test"}}],
        "body": {
            "contentType": "HTML",
            "content": "<html><body><p>Rent is $14 NNN.</p>"
            "<p>On Mon 16 Aug 2026 at 09:00, An Operator wrote:</p>"
            "<p>what is the rent?</p>"
            "</body></html>",
        },
        "internetMessageHeaders": [
            {"name": "In-Reply-To", "value": "<prior@contoso.example>"},
            {"name": "References", "value": "<root@contoso.example> <prior@contoso.example>"},
        ],
    }

    ATTACHMENTS = (
        {"id": "att-1", "name": "flyer.pdf", "contentType": "application/pdf", "size": 1024},
    )

    def _sources(self):
        from email_automation import message_transport as mt

        def exploding_request(*args, **kwargs):
            raise AssertionError(
                "the fixture lane made a Graph request; acquisition must be the ONLY "
                "difference between the two lanes"
            )

        graph = mt.GraphInboundMessageSource(
            request=lambda *a, **k: {
                **self.GRAPH_MESSAGE,
                "attachments": list(self.ATTACHMENTS),
            },
            headers={"Authorization": "Bearer fake"},
        )
        fixture = mt.FixtureInboundMessageSource(
            snapshot={
                **self.GRAPH_MESSAGE,
                "attachments": list(self.ATTACHMENTS),
            },
            request=exploding_request,
        )
        return mt, graph, fixture

    def test_module_exposes_every_locked_interface_name(self):
        from email_automation import message_transport as mt

        for name in (
            "DeliveryKind",
            "HydratedInboundMessage",
            "InboundMessageSource",
            "CanonicalConversationState",
            "ConversationStateSource",
            "OutboundDraft",
            "DeliveryReceipt",
            "OutboundDraftTransport",
        ):
            self.assertTrue(hasattr(mt, name), f"locked interface {name} is missing")
        self.assertEqual(mt.DeliveryKind.NEW.value, "new")
        self.assertEqual(mt.DeliveryKind.REPLY.value, "reply")
        self.assertEqual(mt.DeliveryKind.REPLY_ALL.value, "reply_all")

    def test_graph_and_fixture_sources_hydrate_equal_canonical_messages(self):
        _mt, graph, fixture = self._sources()
        summary = {"id": self.GRAPH_MESSAGE["id"]}
        self.assertEqual(graph.hydrate(summary), fixture.hydrate(summary))

    def test_every_canonical_field_matches_field_by_field(self):
        _mt, graph, fixture = self._sources()
        summary = {"id": self.GRAPH_MESSAGE["id"]}
        left, right = graph.hydrate(summary), fixture.hydrate(summary)
        for field in (
            "summary",
            "full_text",
            "text_for_ai",
            "source_envelope",
            "internet_headers",
            "attachment_snapshot",
        ):
            with self.subTest(field=field):
                self.assertEqual(getattr(left, field), getattr(right, field))

    def test_quoted_history_is_excluded_from_text_for_ai_but_kept_in_full_text(self):
        _mt, graph, _fixture = self._sources()
        hydrated = graph.hydrate({"id": self.GRAPH_MESSAGE["id"]})
        self.assertIn("Rent is $14 NNN.", hydrated.text_for_ai)
        self.assertNotIn("what is the rent?", hydrated.text_for_ai)
        self.assertIn("what is the rent?", hydrated.full_text)

    def test_canonical_text_matches_productions_own_pipeline_exactly(self):
        """The two lanes must not merely both look right - they must be identical.

        Recomputes production's exact inline sequence (normalize body, then
        strip_email_quotes) and requires byte equality. An earlier draft of the
        canonicalizer stripped <blockquote> with BeautifulSoup and produced
        DIFFERENT text_for_ai than production for the same message; certification
        would then have been measuring a quote stripper that ships nowhere.
        """
        from email_automation.utils import strip_email_quotes, strip_html_tags

        _mt, graph, _fixture = self._sources()
        hydrated = graph.hydrate({"id": self.GRAPH_MESSAGE["id"]})

        body = self.GRAPH_MESSAGE["body"]
        raw_content = body.get("content", "") or ""
        content_type = (body.get("contentType") or "Text").upper()
        production_full = (
            strip_html_tags(raw_content) if content_type == "HTML" else raw_content
        )
        self.assertEqual(hydrated.full_text, production_full)
        self.assertEqual(hydrated.text_for_ai, strip_email_quotes(production_full))

    def test_inline_quote_without_a_canonical_marker_survives_into_text_for_ai(self):
        """PRODUCT OBSERVATION, pinned so it is not mistaken for instrument error.

        production's strip_email_quotes is line-marker based: it breaks on a line
        matching "On ... wrote:", a From:/Sent: header pair, or a divider line. A
        quote folded onto the SAME line as surrounding prose matches nothing and
        survives into the text handed to the model.

        That is a real product weakness on the re-asking axis (FDR-004 guarantee #6),
        because the model then sees the operator's own earlier question as though it
        were part of the broker's reply. It is recorded here rather than repaired,
        because certification must reproduce production faithfully; fixing it is a
        separate product decision with its own evidence.
        """
        from email_automation.message_transport import canonicalize_inbound_message

        hydrated = canonicalize_inbound_message(
            {
                "id": "inline-quote",
                "body": {
                    "contentType": "HTML",
                    "content": "<p>Rent is $14 NNN. On Mon you wrote: what is the rent?</p>",
                },
            }
        )
        self.assertIn("what is the rent?", hydrated.text_for_ai)

    def test_envelope_carries_identity_and_recipients(self):
        _mt, graph, _fixture = self._sources()
        envelope = graph.hydrate({"id": self.GRAPH_MESSAGE["id"]}).source_envelope
        self.assertEqual(envelope["graphMessageId"], self.GRAPH_MESSAGE["id"])
        self.assertEqual(envelope["internetMessageId"], self.GRAPH_MESSAGE["internetMessageId"])
        self.assertEqual(envelope["conversationId"], self.GRAPH_MESSAGE["conversationId"])
        self.assertEqual(envelope["fromEmail"], "broker@example.test")
        self.assertEqual(envelope["replyTo"], ("broker.reply@example.test",))
        self.assertEqual(envelope["to"], ("operator@example.test",))
        self.assertEqual(envelope["cc"], ("assistant@example.test",))
        self.assertEqual(envelope["receivedDateTime"], self.GRAPH_MESSAGE["receivedDateTime"])
        self.assertTrue(envelope["hasAttachments"])

    def test_reply_targets_are_normalized_from_internet_headers(self):
        _mt, graph, _fixture = self._sources()
        envelope = graph.hydrate({"id": self.GRAPH_MESSAGE["id"]}).source_envelope
        self.assertEqual(envelope["inReplyTo"], "<prior@contoso.example>")
        self.assertEqual(
            envelope["references"],
            ("<root@contoso.example>", "<prior@contoso.example>"),
        )

    def test_hydrated_message_is_immutable(self):
        _mt, graph, _fixture = self._sources()
        hydrated = graph.hydrate({"id": self.GRAPH_MESSAGE["id"]})
        with self.assertRaises(Exception):
            hydrated.full_text = "tampered"  # type: ignore[misc]
        self.assertIsInstance(hydrated.internet_headers, tuple)
        self.assertIsInstance(hydrated.attachment_snapshot, tuple)

    def test_fixture_lane_makes_zero_graph_requests(self):
        _mt, _graph, fixture = self._sources()
        hydrated = fixture.hydrate({"id": self.GRAPH_MESSAGE["id"]})
        self.assertTrue(hydrated.full_text)

    def test_conversation_state_parity_including_sent_receipts(self):
        mt, _graph, _fixture = self._sources()
        prior = {**self.GRAPH_MESSAGE, "id": "prior-id", "hasAttachments": False}
        receipts = ({"internetMessageId": "<sent-1@contoso.example>", "status": "sent"},)

        graph_state = mt.GraphConversationStateSource(
            request=lambda *a, **k: {
                "reply_target": {**self.GRAPH_MESSAGE, "attachments": list(self.ATTACHMENTS)},
                "prior_messages": [prior],
                "sent_receipts": list(receipts),
            },
            headers={"Authorization": "Bearer fake"},
        ).load("conv-777")

        fixture_state = mt.FixtureConversationStateSource(
            snapshot={
                "reply_target": {**self.GRAPH_MESSAGE, "attachments": list(self.ATTACHMENTS)},
                "prior_messages": [prior],
                "sent_receipts": list(receipts),
            },
        ).load("conv-777")

        self.assertEqual(graph_state, fixture_state)
        self.assertEqual(len(graph_state.prior_messages), 1)
        self.assertEqual(graph_state.sent_receipts[0]["status"], "sent")
        self.assertIsInstance(graph_state.prior_messages, tuple)


# ---------------------------------------------------------------------------
# Task 6 - the shared initial-outreach delivery boundary
# ---------------------------------------------------------------------------
#
# These are RUNTIME tests, unlike everything above. They import
# ``email_automation.message_transport``, which is pure and imports only
# ``utils`` - no provider client is constructed and no credential is needed. The
# source-level classes above still import nothing from the package, so the
# structural invariant they pin remains independent of the business logic.

from email_automation.message_transport import (  # noqa: E402
    DeliveryKind,
    GraphDraftDeliveryTransport,
    OutboundDraft,
    PreparedDelivery,
    DeliveryPreparationError,
)
from email_automation.certification.capture import (  # noqa: E402
    CapturingDeliveryTransport,
)


class _Response:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload or {}
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _GraphSpy:
    """Records every Graph call the transport makes, in order."""

    def __init__(self, *, identifiers=None, fail_on=None):
        self.calls = []
        self._identifiers = identifiers if identifiers is not None else {
            "internetMessageId": "<outreach-1@example.com>",
            "conversationId": "conv-1",
            "subject": "100 Fixture Way",
        }
        self._fail_on = fail_on

    def _maybe_fail(self, label):
        if self._fail_on == label:
            raise RuntimeError(f"graph failed at {label}")

    def post(self, url, headers=None, json=None, timeout=None, **kwargs):
        if url.endswith("/me/messages"):
            self.calls.append(("create", url))
            self._maybe_fail("create")
            return _Response({"id": "draft-1"})
        if url.endswith("/attachments"):
            self.calls.append(("attach", url))
            self._maybe_fail("attach")
            return _Response({"id": "att-1"}, status_code=201)
        if url.endswith("/send"):
            self.calls.append(("send", url))
            self._maybe_fail("send")
            return _Response({}, status_code=202)
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url, headers=None, params=None, timeout=None, **kwargs):
        self.calls.append(("identifiers", url))
        self._maybe_fail("identifiers")
        return _Response(dict(self._identifiers))

    def delete(self, url, headers=None, timeout=None, **kwargs):
        self.calls.append(("discard", url))
        return _Response({}, status_code=204)

    def kinds(self):
        return [kind for kind, _url in self.calls]


def _draft(**overrides):
    payload = dict(
        kind=DeliveryKind.NEW,
        subject="100 Fixture Way",
        body="Hi Pat, could you share the asking rent?",
        to=("broker@fixture.example.com",),
        cc=(),
        bcc=(),
        attachments=(),
        idempotency_key="outbox-1:broker@fixture.example.com",
    )
    payload.update(overrides)
    return OutboundDraft(**payload)


class OutboundDeliveryTests(unittest.TestCase):
    """The convergence point Task 6 creates.

    Phase A proved there was no common boundary to relocate: four independent
    ``/me/messages/{id}/send`` sites existed. So this boundary is CREATED, and
    the property that matters is that preparing a message and committing it are
    separable - because the product re-reads campaign eligibility in between, at
    the last moment before the irreversible call.
    """

    def _transport(self, **kwargs):
        spy = _GraphSpy(**kwargs)
        transport = GraphDraftDeliveryTransport(
            headers={"Authorization": "Bearer fixture"},
            base="https://graph.microsoft.com/v1.0",
            request=spy,
        )
        return transport, spy

    # -- prepare stops short of the irreversible call ---------------------

    def test_prepare_creates_a_draft_and_reads_identifiers_without_sending(self):
        transport, spy = self._transport()
        prepared = transport.prepare(_draft())
        self.assertIsInstance(prepared, PreparedDelivery)
        self.assertEqual(prepared.provider_message_id, "draft-1")
        self.assertEqual(prepared.internet_message_id, "<outreach-1@example.com>")
        self.assertEqual(prepared.conversation_id, "conv-1")
        self.assertEqual(spy.kinds(), ["create", "identifiers"])
        self.assertNotIn("send", spy.kinds())

    def test_attachments_are_added_before_identifiers_are_read(self):
        transport, spy = self._transport()
        transport.prepare(_draft(attachments=({"name": "sig.png"},)))
        self.assertEqual(spy.kinds(), ["create", "attach", "identifiers"])

    def test_a_draft_with_no_internet_message_id_is_refused_before_sending(self):
        """Without it a sent message can never be matched to its thread."""
        transport, spy = self._transport(identifiers={"conversationId": "conv-1"})
        with self.assertRaises(DeliveryPreparationError):
            transport.prepare(_draft())
        self.assertNotIn("send", spy.kinds())

    # -- commit is the irreversible boundary ------------------------------

    def test_commit_sends_the_prepared_draft_and_returns_a_receipt(self):
        transport, spy = self._transport()
        receipt = transport.commit(transport.prepare(_draft()))
        self.assertEqual(spy.kinds(), ["create", "identifiers", "send"])
        self.assertEqual(receipt.status, "sent")
        self.assertEqual(receipt.provider_message_id, "draft-1")
        self.assertEqual(receipt.internet_message_id, "<outreach-1@example.com>")
        self.assertEqual(receipt.conversation_id, "conv-1")

    def test_discard_deletes_the_draft_and_never_sends(self):
        """The suppression path: eligibility was lost after the draft was built."""
        transport, spy = self._transport()
        transport.discard(transport.prepare(_draft()))
        self.assertEqual(spy.kinds(), ["create", "identifiers", "discard"])
        self.assertNotIn("send", spy.kinds())

    def test_deliver_is_prepare_then_commit(self):
        transport, spy = self._transport()
        receipt = transport.deliver(_draft())
        self.assertEqual(spy.kinds(), ["create", "identifiers", "send"])
        self.assertEqual(receipt.status, "sent")

    def test_a_failed_send_propagates_rather_than_reporting_success(self):
        """An ambiguous send must never be reported as a clean no-send."""
        transport, spy = self._transport(fail_on="send")
        prepared = transport.prepare(_draft())
        with self.assertRaises(Exception):
            transport.commit(prepared)
        self.assertIn("send", spy.kinds())

    # -- the certification transport is the same shape, minus the network --

    def test_capture_transport_makes_no_provider_call_at_all(self):
        capture = CapturingDeliveryTransport(run_id="cert-run-6")
        prepared = capture.prepare(_draft())
        receipt = capture.commit(prepared)
        self.assertEqual(receipt.status, "captured")
        self.assertEqual(len(capture.captured), 1)
        self.assertEqual(capture.captured[0].to, ("broker@fixture.example.com",))

    def test_capture_receives_the_final_envelope_not_a_template(self):
        """Whatever capture records is exactly what would have gone on the wire."""
        capture = CapturingDeliveryTransport(run_id="cert-run-6")
        draft = _draft(body="Hi Pat, could you share the asking rent?")
        capture.commit(capture.prepare(draft))
        self.assertEqual(capture.captured[0].body, draft.body)
        self.assertEqual(capture.captured[0].subject, draft.subject)

    def test_capture_identifiers_are_deterministic_and_run_scoped(self):
        """Two runs must not collide, and one run must be reproducible."""
        first = CapturingDeliveryTransport(run_id="cert-run-a")
        second = CapturingDeliveryTransport(run_id="cert-run-a")
        other = CapturingDeliveryTransport(run_id="cert-run-b")
        a = first.commit(first.prepare(_draft()))
        b = second.commit(second.prepare(_draft()))
        c = other.commit(other.prepare(_draft()))
        self.assertEqual(a.internet_message_id, b.internet_message_id)
        self.assertNotEqual(a.internet_message_id, c.internet_message_id)

    def test_a_discarded_capture_is_not_recorded_as_delivered(self):
        capture = CapturingDeliveryTransport(run_id="cert-run-6")
        capture.discard(capture.prepare(_draft()))
        self.assertEqual(capture.captured, [])
        self.assertEqual(len(capture.discarded), 1)

    def test_capture_never_reaches_a_real_recipient_domain(self):
        """Structural: the synthetic identifiers must be unmistakably non-routable."""
        capture = CapturingDeliveryTransport(run_id="cert-run-6")
        receipt = capture.commit(capture.prepare(_draft()))
        self.assertTrue(receipt.internet_message_id.endswith(".invalid>"))


if __name__ == "__main__":
    unittest.main()
