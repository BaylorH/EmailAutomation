"""RED contracts for bounded contact fan-out discovery."""

from __future__ import annotations

import importlib
import inspect
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from unittest.mock import patch

from tests.row_authority_fakes import BoundedFakeTransaction


def _row_id(index):
    return f"sr1_{index:012x}4{index:03x}8{index:015x}"


class ContactFanoutDiscoveryTests(unittest.TestCase):
    FANOUT_FIELDS = {
        "user_scope_hash": "userScopeHash",
        "fanout_id": "fanoutId",
        "outcome": "outcome",
        "expected_contact_settlement_hash": "expectedContactSettlementHash",
        "state_revision": "stateRevision",
        "state": "state",
        "binding_revision": "bindingRevision",
        "binding_head_hash": "bindingHeadHash",
        "binding_association_count": "bindingAssociationCount",
        "discovery_cursor_row_id": "discoveryCursorRowId",
        "cursor_processed_count": "cursorProcessedCount",
        "obligation_count": "obligationCount",
        "result_count": "resultCount",
        "lease_owner_hash": "leaseOwnerHash",
        "lease_until": "leaseUntil",
        "fencing_token": "fencingToken",
        "superseding_contact_settlement_hash": (
            "supersedingContactSettlementHash"
        ),
        "completion_binding_revision": "completionBindingRevision",
        "completion_binding_head_hash": "completionBindingHeadHash",
        "completion_binding_association_count": (
            "completionBindingAssociationCount"
        ),
        "completion_obligation_count": "completionObligationCount",
        "completion_result_count": "completionResultCount",
        "completed_at": "completedAt",
        "created_at": "createdAt",
        "updated_at": "updatedAt",
    }

    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")
        transitions = importlib.import_module(
            "tests.test_row_authority_contact_transitions"
        )
        cls.transition_type = transitions.ContactTransitionTests
        cls.transition_type.setUpClass()

    def setUp(self):
        self.lease_owner = "d" * 64
        self.leased_at = "2026-08-04T12:05:00.000000Z"
        self.lease_until = "2026-08-04T12:10:00.000000Z"
        self.discovered_at = "2026-08-04T12:05:30.000000Z"

    def _method(self):
        method = getattr(
            self.module.RowAuthorityStore,
            "discover_contact_fanout_page",
            None,
        )
        self.assertTrue(
            callable(method),
            "RowAuthorityStore.discover_contact_fanout_page is missing",
        )
        signature = inspect.signature(method)
        self.assertEqual(
            [
                "self",
                "verified_user_id",
                "fanout_id",
                "expected_fanout_head",
                "lease_owner_hash",
                "discovered_at",
            ],
            list(signature.parameters),
        )
        self.assertTrue(
            all(
                value.kind is inspect.Parameter.KEYWORD_ONLY
                for name, value in signature.parameters.items()
                if name != "self"
            )
        )
        return method

    @staticmethod
    def _writes(store):
        return [
            event
            for event in store.events
            if event[0] in {"create", "set", "update", "delete"}
        ]

    def _fanout(self, current, **overrides):
        values = {
            argument: current[field]
            for argument, field in self.FANOUT_FIELDS.items()
        }
        values.update(overrides)
        return self.module.build_contact_fanout_head_document(**values)

    def _user(self, context):
        return context["transition"]._user(context["store"])

    def _reference(self, context, collection, document_id):
        return self._user(context).collection(collection).document(document_id)

    def _contact_references(self, context):
        return {
            "head": self._reference(
                context,
                "contactOptOutHeads",
                context["canonicalHash"],
            ),
            "settlement": self._reference(
                context,
                "contactOptOutSettlements",
                (
                    f"{context['canonicalHash']}--"
                    f"{context['settlement']['generation']}"
                ),
            ),
            "receipt": self._reference(
                context,
                "contactOptOutTransitionRequests",
                context["settlement"]["contactTransitionId"],
            ),
        }

    def _store_fanout(self, context, fanout):
        self._reference(
            context,
            "contactOptOutFanoutHeads",
            fanout["fanoutId"],
        ).set(fanout, merge=False)
        context["fanout"] = fanout
        context["store"].events.clear()
        return fanout

    def _binding_head(self, context, edges, *, prior=None):
        if not edges:
            return None
        first = edges[0]["createdAt"] if prior is None else prior["createdAt"]
        revision = len(edges) if prior is None else prior["stateRevision"] + 1
        return self.module.build_contact_row_binding_head_document(
            user_scope_hash=context["scope"],
            canonical_mailbox_identity_hash=context["canonicalHash"],
            state_revision=revision,
            association_count=len(edges),
            last_association_hash=edges[-1]["contactRowEdgeHash"],
            created_at=first,
            updated_at=edges[-1]["createdAt"],
        )

    def _seed(
        self,
        row_ids=(),
        *,
        frozen_binding=True,
        cursor=None,
        obligation_count=0,
        source_id="source-contact-fanout-discovery",
    ):
        transition = self.transition_type(methodName="runTest")
        transition.setUp()
        store = transition.fixture._store()
        bundle, link = transition._seed_bundle(
            store,
            source_id,
        )
        created = transition._record(store, bundle)
        context = {
            "transition": transition,
            "store": store,
            "scope": transition.fixture.scope,
            "canonicalHash": link["canonicalMailboxIdentityHash"],
            "settlement": created["settlement"],
            "receipt": created["transitionRequest"],
            "contactHead": created["head"],
            "edges": [],
        }
        for index, row_id in enumerate(row_ids, start=1):
            edge = self.module.build_contact_row_binding_document(
                user_scope_hash=context["scope"],
                canonical_mailbox_identity_hash=context["canonicalHash"],
                row_id=row_id,
                created_at=f"2026-08-04T12:01:00.{index:06d}Z",
            )
            self._reference(
                context,
                "contactRowBindings",
                edge["edgeId"],
            ).create(edge)
            context["edges"].append(edge)
        binding = self._binding_head(context, context["edges"])
        if binding is not None:
            self._reference(
                context,
                "contactRowBindingHeads",
                context["canonicalHash"],
            ).create(binding)
        original = created["fanoutHead"]
        snapshot = binding if frozen_binding else None
        leased = self._fanout(
            original,
            state_revision=original["stateRevision"] + 1,
            binding_revision=(0 if snapshot is None else snapshot["stateRevision"]),
            binding_head_hash=(
                None
                if snapshot is None
                else snapshot["contactRowBindingHeadHash"]
            ),
            binding_association_count=(
                0 if snapshot is None else snapshot["associationCount"]
            ),
            discovery_cursor_row_id=cursor,
            cursor_processed_count=(
                0 if cursor is None else obligation_count
            ),
            obligation_count=obligation_count,
            lease_owner_hash=self.lease_owner,
            lease_until=self.lease_until,
            fencing_token=original["fencingToken"] + 1,
            updated_at=self.leased_at,
        )
        self._reference(
            context,
            "contactOptOutFanoutHeads",
            leased["fanoutId"],
        ).set(leased, merge=False)
        context.update({"bindingHead": binding, "fanout": leased})
        store.events.clear()
        return context

    def _add_binding(self, context, row_id, *, created_at, clear_events=True):
        edge = self.module.build_contact_row_binding_document(
            user_scope_hash=context["scope"],
            canonical_mailbox_identity_hash=context["canonicalHash"],
            row_id=row_id,
            created_at=created_at,
        )
        self._reference(
            context,
            "contactRowBindings",
            edge["edgeId"],
        ).create(edge)
        context["edges"].append(edge)
        binding = self._binding_head(
            context,
            context["edges"],
            prior=context["bindingHead"],
        )
        self._reference(
            context,
            "contactRowBindingHeads",
            context["canonicalHash"],
        ).set(binding, merge=False)
        context["bindingHead"] = binding
        if clear_events:
            context["store"].events.clear()
        return edge

    def _add_cross_canonical_binding(self, context, row_id):
        edge = self.module.build_contact_row_binding_document(
            user_scope_hash=context["scope"],
            canonical_mailbox_identity_hash="c" * 64,
            row_id=row_id,
            created_at="2026-08-04T12:02:00.000000Z",
        )
        self._reference(
            context,
            "contactRowBindings",
            edge["edgeId"],
        ).create(edge)
        context["store"].events.clear()
        return edge

    def _seed_release(self, row_ids):
        context = self._seed(row_ids)
        released_head = context["transition"]._install_release_after_image(
            context["store"],
            released_at="2026-08-04T12:06:00.000000Z",
        )
        settlements = context["transition"]._documents(
            context["store"],
            "contactOptOutSettlements",
        )
        released = settlements[
            f"{context['canonicalHash']}--{released_head['latestGeneration']}"
        ]
        release_fanout = context["transition"]._documents(
            context["store"],
            "contactOptOutFanoutHeads",
        )[released_head["activeFanoutId"]]
        leased = self._fanout(
            release_fanout,
            state_revision=release_fanout["stateRevision"] + 1,
            binding_revision=(
                0
                if context["bindingHead"] is None
                else context["bindingHead"]["stateRevision"]
            ),
            binding_head_hash=(
                None
                if context["bindingHead"] is None
                else context["bindingHead"]["contactRowBindingHeadHash"]
            ),
            binding_association_count=len(context["edges"]),
            lease_owner_hash=self.lease_owner,
            lease_until=self.lease_until,
            fencing_token=release_fanout["fencingToken"] + 1,
            updated_at="2026-08-04T12:06:10.000000Z",
        )
        context.update(
            {
                "settlement": released,
                "receipt": context["transition"]._documents(
                    context["store"],
                    "contactOptOutTransitionRequests",
                )[released["contactTransitionId"]],
                "contactHead": released_head,
            }
        )
        self._store_fanout(context, leased)
        return context

    def _obligation(self, context, edge, *, created_at=None):
        obligation = self.module.build_contact_fanout_obligation_document(
            user_scope_hash=context["scope"],
            fanout_id=context["fanout"]["fanoutId"],
            row_id=edge["rowId"],
            contact_row_edge_hash=edge["contactRowEdgeHash"],
            expected_contact_settlement_hash=context["settlement"][
                "contactSettlementHash"
            ],
            outcome=context["fanout"]["outcome"],
            created_at=created_at or self.discovered_at,
        )
        self._reference(
            context,
            "contactOptOutFanoutObligations",
            f"{context['fanout']['fanoutId']}--{edge['rowId']}",
        ).create(obligation)
        context["store"].events.clear()
        return obligation

    def _discover(
        self,
        context,
        expected=None,
        *,
        owner=None,
        discovered_at=None,
        executor=None,
    ):
        self._method()
        return context["transition"]._authority(
            context["store"],
            executor=executor,
        ).discover_contact_fanout_page(
            verified_user_id=context["transition"].fixture.user_id,
            fanout_id=context["fanout"]["fanoutId"],
            expected_fanout_head=expected or context["fanout"],
            lease_owner_hash=owner or self.lease_owner,
            discovered_at=discovered_at or self.discovered_at,
        )

    def _discover_with_query_shape(self, context, expected=None):
        observed = []
        original = BoundedFakeTransaction.get_query

        def inspect_query(transaction, query):
            if query._collection.path.endswith("/contactRowBindings"):
                observed.append(
                    {
                        "filters": query._filters,
                        "ordering": query._ordering,
                        "directions": query._directions,
                        "limit": query._limit_count,
                        "cursor": query._start_after_values,
                        "cursorPath": query._start_after_path,
                    }
                )
            return original(transaction, query)

        with patch.object(
            BoundedFakeTransaction,
            "get_query",
            new=inspect_query,
        ):
            result = self._discover(context, expected)
        return result, observed

    def _discover_with_settlement_query_shape(self, context, expected=None):
        observed = []
        original = BoundedFakeTransaction.get_query

        def inspect_query(transaction, query):
            if query._collection.path.endswith(
                "/contactOptOutSettlements"
            ):
                observed.append(
                    {
                        "filters": query._filters,
                        "ordering": query._ordering,
                        "directions": query._directions,
                        "limit": query._limit_count,
                        "cursor": query._start_after_values,
                        "cursorPath": query._start_after_path,
                    }
                )
            return original(transaction, query)

        with patch.object(
            BoundedFakeTransaction,
            "get_query",
            new=inspect_query,
        ):
            result = self._discover(context, expected)
        return result, observed

    def _assert_binding_query_shape(self, context, observed, *, cursor):
        self.assertEqual(
            [
                {
                    "filters": (
                        (
                            "canonicalMailboxIdentityHash",
                            "==",
                            context["canonicalHash"],
                        ),
                    ),
                    "ordering": ("rowId",),
                    "directions": ("ASCENDING",),
                    "limit": 129,
                    "cursor": cursor,
                    "cursorPath": None,
                }
            ],
            observed,
        )

    def _assert_settlement_query_shape(self, context, observed):
        self.assertEqual(
            [
                {
                    "filters": (
                        (
                            "contactSettlementHash",
                            "==",
                            context["fanout"][
                                "expectedContactSettlementHash"
                            ],
                        ),
                    ),
                    "ordering": ("__name__",),
                    "directions": ("ASCENDING",),
                    "limit": 2,
                    "cursor": None,
                    "cursorPath": None,
                }
            ],
            observed,
        )

    def _assert_result(self, result, disposition):
        self.assertEqual(
            {"disposition", "fanoutHead", "obligations"},
            set(result),
        )
        self.assertEqual(disposition, result["disposition"])
        self.module.validate_contact_fanout_head_document(
            document=result["fanoutHead"]
        )
        for obligation in result["obligations"]:
            self.module.validate_contact_fanout_obligation_document(
                document=obligation
            )

    def _assert_progress(
        self,
        before,
        result,
        *,
        added,
        state,
        cursor,
        updated_at=None,
    ):
        after = result["fanoutHead"]
        self.assertEqual(before["stateRevision"] + 1, after["stateRevision"])
        self.assertEqual(before["obligationCount"] + added, after["obligationCount"])
        self.assertEqual(before["resultCount"], after["resultCount"])
        self.assertEqual(state, after["state"])
        self.assertEqual(cursor, after["discoveryCursorRowId"])
        self.assertEqual(
            (
                0
                if cursor is None
                else before["cursorProcessedCount"] + added
            ),
            after["cursorProcessedCount"],
        )
        self.assertEqual(updated_at or self.discovered_at, after["updatedAt"])
        for field in ("leaseOwnerHash", "leaseUntil", "fencingToken"):
            self.assertEqual(before[field], after[field])
        self.assertTrue(
            all(
                obligation["createdAt"]
                == (updated_at or self.discovered_at)
                for obligation in result["obligations"]
            )
        )

    def _assert_create_only_obligations(self, context, expected_count):
        writes = self._writes(context["store"])
        obligation_writes = [
            event
            for event in writes
            if "/contactOptOutFanoutObligations/" in event[1]
        ]
        self.assertEqual(expected_count, len(obligation_writes))
        self.assertTrue(all(event[0] == "create" for event in obligation_writes))

    def _assert_failure_without_write(self, context, operation):
        before = deepcopy(context["store"].data)
        context["store"].events.clear()
        with self.assertRaises(self.module.RowAuthorityError):
            operation()
        self.assertEqual(before, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))

    def test_discovery_pages_128_with_129th_as_sentinel(self):
        rows = [_row_id(index) for index in range(1, 130)]
        context = self._seed(rows)

        result = self._discover(context)

        self._assert_result(result, "page_discovered")
        self.assertEqual(rows[:128], [item["rowId"] for item in result["obligations"]])
        advanced = result["fanoutHead"]
        self._assert_progress(
            context["fanout"],
            result,
            added=128,
            state="discovering",
            cursor=rows[127],
        )
        self.assertEqual(129, len(self._writes(context["store"])))
        self.assertEqual(128, advanced["cursorProcessedCount"])
        self._assert_create_only_obligations(context, 128)
        missing = self._reference(
            context,
            "contactOptOutFanoutObligations",
            f"{advanced['fanoutId']}--{rows[128]}",
        ).path
        self.assertNotIn(missing, context["store"].data)

    def test_discovery_uses_row_field_cursor_and_exact_obligation_replay(self):
        rows = [_row_id(index) for index in range(1, 259)]
        context = self._seed(rows)
        foreign = self._add_cross_canonical_binding(context, _row_id(999))
        first, first_queries = self._discover_with_query_shape(context)
        self._assert_result(first, "page_discovered")
        self.assertEqual(rows[:128], [item["rowId"] for item in first["obligations"]])
        self._assert_binding_query_shape(context, first_queries, cursor=None)
        context["store"].events.clear()

        retry = self._discover(context, context["fanout"])

        self.assertEqual(first, retry)
        self.assertEqual([], self._writes(context["store"]))
        context["store"].events.clear()
        second, second_queries = self._discover_with_query_shape(
            context,
            first["fanoutHead"],
        )
        self._assert_result(second, "page_discovered")
        self.assertEqual(256, second["fanoutHead"]["cursorProcessedCount"])
        self.assertEqual(
            rows[128:256],
            [item["rowId"] for item in second["obligations"]],
        )
        self._assert_binding_query_shape(
            context,
            second_queries,
            cursor=(rows[127],),
        )
        context["store"].events.clear()
        final, final_queries = self._discover_with_query_shape(
            context,
            second["fanoutHead"],
        )
        self._assert_result(final, "discovery_complete")
        self.assertEqual(rows[256:], [item["rowId"] for item in final["obligations"]])
        self._assert_binding_query_shape(
            context,
            final_queries,
            cursor=(rows[255],),
        )
        self.assertEqual("applying", final["fanoutHead"]["state"])
        self.assertIsNone(final["fanoutHead"]["discoveryCursorRowId"])
        self.assertEqual(0, final["fanoutHead"]["cursorProcessedCount"])
        self.assertNotIn(
            foreign["rowId"],
            [item["rowId"] for item in first["obligations"]]
            + [item["rowId"] for item in second["obligations"]]
            + [item["rowId"] for item in final["obligations"]],
        )

    def test_binding_revision_drift_resets_cursor_before_rescan(self):
        row = _row_id(1)
        context = self._seed([row], frozen_binding=False)

        reset = self._discover(context)

        self._assert_result(reset, "binding_reset")
        self.assertEqual([], reset["obligations"])
        advanced = reset["fanoutHead"]
        self.assertEqual(
            context["bindingHead"]["stateRevision"],
            advanced["bindingRevision"],
        )
        self.assertEqual(
            context["bindingHead"]["contactRowBindingHeadHash"],
            advanced["bindingHeadHash"],
        )
        self.assertIsNone(advanced["discoveryCursorRowId"])
        self.assertEqual(0, advanced["cursorProcessedCount"])
        self.assertEqual(1, len(self._writes(context["store"])))

        context["store"].events.clear()
        discovered = self._discover(context, advanced)
        self._assert_result(discovered, "discovery_complete")
        self.assertEqual([row], [item["rowId"] for item in discovered["obligations"]])

        phantom = self._seed([_row_id(3)])
        phantom["store"].before_next_commit_hook = lambda: self._add_binding(
            phantom,
            _row_id(2),
            created_at="2026-08-04T12:05:10.000000Z",
            clear_events=False,
        )
        raced = self._discover(phantom)
        self._assert_result(raced, "binding_reset")
        self.assertTrue(
            any(
                event[0]
                in {"commit_aborted_stale_read", "commit_aborted_stale_query"}
                for event in phantom["store"].events
            )
        )

        with self.subTest(case="new-row-after-non-null-cursor"):
            prior, later = _row_id(10), _row_id(20)
            context = self._seed(
                [prior],
                cursor=prior,
                obligation_count=1,
            )
            self._obligation(context, context["edges"][0])
            self._add_binding(
                context,
                later,
                created_at="2026-08-04T12:05:10.000000Z",
            )
            before = context["fanout"]

            reset = self._discover(context)

            self._assert_result(reset, "binding_reset")
            after = reset["fanoutHead"]
            self.assertIsNone(after["discoveryCursorRowId"])
            self.assertEqual(0, after["cursorProcessedCount"])
            self.assertEqual(before["obligationCount"], after["obligationCount"])
            self.assertEqual(before["resultCount"], after["resultCount"])
            for field in ("leaseOwnerHash", "leaseUntil", "fencingToken"):
                self.assertEqual(before[field], after[field])
            writes = self._writes(context["store"])
            self.assertEqual(1, len(writes))
            self.assertEqual("set", writes[0][0])
            self.assertTrue(
                writes[0][1].endswith(
                    f"/contactOptOutFanoutHeads/{before['fanoutId']}"
                )
            )

            context["store"].events.clear()
            rescanned = self._discover(context, after)
            self._assert_result(rescanned, "discovery_complete")
            self.assertEqual(
                [prior, later],
                [item["rowId"] for item in rescanned["obligations"]],
            )
            self.assertEqual(2, rescanned["fanoutHead"]["obligationCount"])

    def test_earlier_sorted_late_row_is_never_skipped(self):
        earlier, prior = _row_id(10), _row_id(20)
        context = self._seed([prior], cursor=prior, obligation_count=1)
        self._obligation(context, context["edges"][0])
        late = self._add_binding(
            context,
            earlier,
            created_at="2026-08-04T12:05:10.000000Z",
        )

        reset = self._discover(context)

        self._assert_result(reset, "binding_reset")
        self.assertIsNone(reset["fanoutHead"]["discoveryCursorRowId"])
        self.assertEqual(0, reset["fanoutHead"]["cursorProcessedCount"])
        context["store"].events.clear()
        completed = self._discover(context, reset["fanoutHead"])
        self._assert_result(completed, "discovery_complete")
        self.assertEqual(
            [earlier, prior],
            [item["rowId"] for item in completed["obligations"]],
        )
        self.assertIn(
            f"{context['fanout']['fanoutId']}--{late['rowId']}",
            {
                path.rsplit("/", 1)[-1]
                for path in context["store"].data
                if "/contactOptOutFanoutObligations/" in path
            },
        )

    def test_stable_exhaustion_moves_to_applying_with_null_cursor(self):
        for count in (0, 1, 128):
            with self.subTest(count=count):
                rows = [_row_id(index) for index in range(1, count + 1)]
                context = self._seed(rows)
                result = self._discover(context)
                self._assert_result(result, "discovery_complete")
                self.assertEqual(
                    rows,
                    [item["rowId"] for item in result["obligations"]],
                )
                self._assert_progress(
                    context["fanout"],
                    result,
                    added=count,
                    state="applying",
                    cursor=None,
                )
                self.assertEqual(count + 1, len(self._writes(context["store"])))
                self._assert_create_only_obligations(context, count)

        with self.subTest(outcome="release"):
            context = self._seed_release([_row_id(1)])
            result = self._discover(
                context,
                discovered_at="2026-08-04T12:06:30.000000Z",
            )
            self._assert_result(result, "discovery_complete")
            self.assertEqual("release", result["fanoutHead"]["outcome"])
            self.assertEqual(
                ["release"],
                [item["outcome"] for item in result["obligations"]],
            )
            self._assert_progress(
                context["fanout"],
                result,
                added=1,
                state="applying",
                cursor=None,
                updated_at="2026-08-04T12:06:30.000000Z",
            )

    def test_fanout_work_requires_contact_settlements_exact_creating_receipt(self):
        with self.subTest(case="settlement-lookup-shape"):
            context = self._seed([_row_id(1)])
            result, observed = self._discover_with_settlement_query_shape(
                context
            )
            self._assert_result(result, "discovery_complete")
            self._assert_settlement_query_shape(context, observed)

        for artifact in ("head", "settlement", "receipt"):
            with self.subTest(case=f"missing-{artifact}"):
                context = self._seed([_row_id(1)])
                reference = self._contact_references(context)[artifact]
                context["store"].data.pop(reference.path)
                self._assert_failure_without_write(
                    context,
                    lambda: self._discover(context),
                )

        for artifact in ("head", "receipt"):
            with self.subTest(case=f"malformed-{artifact}"):
                context = self._seed([_row_id(1)])
                reference = self._contact_references(context)[artifact]
                context["store"].data[reference.path]["unknown"] = None
                self._assert_failure_without_write(
                    context,
                    lambda: self._discover(context),
                )

        with self.subTest(case="advanced-contact-head"):
            context = self._seed([_row_id(1)])
            expected = context["fanout"]
            context["transition"]._install_release_after_image(
                context["store"],
                released_at="2026-08-04T12:06:00.000000Z",
            )
            self._store_fanout(context, expected)
            self._assert_failure_without_write(
                context,
                lambda: self._discover(context, expected),
            )

        for artifact in ("settlement", "receipt"):
            with self.subTest(case=f"crossed-{artifact}"):
                context = self._seed([_row_id(1)])
                foreign = self._seed(
                    [_row_id(1)],
                    source_id=f"source-crossed-{artifact}",
                )
                target = self._contact_references(context)[artifact]
                context["store"].data[target.path] = deepcopy(
                    foreign[artifact]
                )
                self._assert_failure_without_write(
                    context,
                    lambda: self._discover(context),
                )

    def test_discovery_failure_or_drift_writes_nothing(self):
        request_cases = (
            ("wrong-owner", {"owner": "e" * 64}),
            ("deadline-equality", {"discovered_at": self.lease_until}),
            (
                "expired-deadline",
                {"discovered_at": "2026-08-04T12:10:00.000001Z"},
            ),
        )
        for label, arguments in request_cases:
            with self.subTest(case=label):
                context = self._seed([_row_id(1)])
                self._assert_failure_without_write(
                    context,
                    lambda: self._discover(context, **arguments),
                )

        with self.subTest(case="unleased"):
            context = self._seed([_row_id(1)])
            unleased = self._fanout(
                context["fanout"],
                state_revision=context["fanout"]["stateRevision"] + 1,
                lease_owner_hash=None,
                lease_until=None,
                updated_at="2026-08-04T12:05:15.000000Z",
            )
            self._store_fanout(context, unleased)
            self._assert_failure_without_write(
                context,
                lambda: self._discover(context),
            )

        for state in ("applying", "superseding"):
            with self.subTest(case=f"state-{state}"):
                context = self._seed([_row_id(1)])
                overrides = {
                    "state_revision": context["fanout"]["stateRevision"] + 1,
                    "state": state,
                    "discovery_cursor_row_id": None,
                    "updated_at": "2026-08-04T12:05:15.000000Z",
                }
                if state == "superseding":
                    overrides["superseding_contact_settlement_hash"] = "f" * 64
                self._store_fanout(
                    context,
                    self._fanout(context["fanout"], **overrides),
                )
                self._assert_failure_without_write(
                    context,
                    lambda: self._discover(context),
                )

        with self.subTest(case="state-complete"):
            context = self._seed([])
            complete = self._fanout(
                context["fanout"],
                state_revision=context["fanout"]["stateRevision"] + 1,
                state="complete",
                lease_owner_hash=None,
                lease_until=None,
                discovery_cursor_row_id=None,
                completion_binding_revision=0,
                completion_binding_head_hash=None,
                completion_binding_association_count=0,
                completion_obligation_count=0,
                completion_result_count=0,
                completed_at="2026-08-04T12:05:15.000000Z",
                updated_at="2026-08-04T12:05:15.000000Z",
            )
            self._store_fanout(context, complete)
            self._assert_failure_without_write(
                context,
                lambda: self._discover(context),
            )

        with self.subTest(case="stale-fence-hash"):
            context = self._seed([_row_id(1)])
            expected = context["fanout"]
            advanced = self._fanout(
                expected,
                state_revision=expected["stateRevision"] + 1,
                fencing_token=expected["fencingToken"] + 1,
                lease_until="2026-08-04T12:11:00.000000Z",
                updated_at="2026-08-04T12:05:15.000000Z",
            )
            self._store_fanout(context, advanced)
            self._assert_failure_without_write(
                context,
                lambda: self._discover(context, expected),
            )

        with self.subTest(case="malformed-edge"):
            context = self._seed([_row_id(1)])
            edge_path = self._reference(
                context,
                "contactRowBindings",
                context["edges"][0]["edgeId"],
            ).path
            context["store"].data[edge_path]["unknown"] = None
            self._assert_failure_without_write(
                context,
                lambda: self._discover(context),
            )

        with self.subTest(case="malformed-obligation"):
            context = self._seed([_row_id(1)])
            obligation = self._obligation(context, context["edges"][0])
            obligation_path = self._reference(
                context,
                "contactOptOutFanoutObligations",
                f"{context['fanout']['fanoutId']}--{obligation['rowId']}",
            ).path
            context["store"].data[obligation_path][
                "contactFanoutObligationHash"
            ] = "0" * 64
            self._assert_failure_without_write(
                context,
                lambda: self._discover(context),
            )

        with self.subTest(case="query-failure"):
            context = self._seed([_row_id(1)])
            with patch.object(
                BoundedFakeTransaction,
                "get_query",
                side_effect=RuntimeError("configured discovery query failure"),
            ):
                self._assert_failure_without_write(
                    context,
                    lambda: self._discover(context),
                )

        with self.subTest(case="preapply-failure"):
            context = self._seed([_row_id(1)])
            context["store"].fail_next_commit = RuntimeError(
                "configured discovery preapply failure"
            )
            self._assert_failure_without_write(
                context,
                lambda: self._discover(context),
            )

        with self.subTest(case="apply-then-raise"):
            context = self._seed([_row_id(1)])
            context["store"].apply_then_raise_next_commit = RuntimeError(
                "unknown discovery commit"
            )
            result = self._discover(context)
            self._assert_result(result, "discovery_complete")
            self.assertIn(("commit_raised_after_apply",), context["store"].events)

        with self.subTest(case="partial-readback"):
            context = self._seed([_row_id(1)])

            def partial_executor(transaction, callback):
                transaction._begin()
                callback(transaction)
                operation, reference, payload, merge = transaction._operations[0]
                transaction._rollback()
                self.assertEqual(("create", False), (operation, merge))
                reference.create(payload)
                raise RuntimeError("partial discovery apply")

            with self.assertRaises(self.module.RowAuthorityAmbiguous):
                self._discover(context, executor=partial_executor)

        with self.subTest(case="same-page-race"):
            context = self._seed([_row_id(1)])
            context["store"].before_commit_barrier = Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(self._discover, context)
                    for _worker in range(2)
                ]
                results = []
                errors = []
                for future in futures:
                    try:
                        results.append(future.result(timeout=10))
                    except BaseException as exc:
                        errors.append(exc)
            self.assertEqual(
                [],
                errors,
                "same-page race must return one deterministic after-image",
            )
            self.assertEqual(results[0], results[1])
            self.assertEqual(2, len(self._writes(context["store"])))
            self.assertEqual(1, context["store"].events.count(("commit_applied", 2)))
            self.assertEqual(1, context["store"].events.count(("commit_applied", 0)))
