"""RED contracts for bounded contact fan-out completion certification."""

from __future__ import annotations

import inspect
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from unittest.mock import patch

from tests.row_authority_fakes import BoundedFakeTransaction
from tests.test_row_authority_contact_fanout_discovery import (
    ContactFanoutDiscoveryTests,
)


def _row_id(index):
    return f"sr1_{index:012x}4{index:03x}8{index:015x}"


class ContactFanoutCompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.discovery_type = ContactFanoutDiscoveryTests
        cls.discovery_type.setUpClass()
        cls.module = cls.discovery_type.module

    def setUp(self):
        self.discovery = self.discovery_type(methodName="runTest")
        self.discovery.setUp()
        self.paired_at = "2026-08-04T12:05:45.000000Z"
        self.certified_at = "2026-08-04T12:06:00.000000Z"
        self.release_result_at = "2026-08-04T12:06:20.000000Z"
        self.release_certified_at = "2026-08-04T12:06:30.000000Z"

    def _method(self):
        method = getattr(
            self.module.RowAuthorityStore,
            "certify_contact_fanout_page",
            None,
        )
        self.assertTrue(
            callable(method),
            "RowAuthorityStore.certify_contact_fanout_page is missing",
        )
        signature = inspect.signature(method)
        self.assertEqual(
            [
                "self",
                "verified_user_id",
                "fanout_id",
                "expected_fanout_head",
                "lease_owner_hash",
                "certified_at",
            ],
            list(signature.parameters),
        )
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.KEYWORD_ONLY
                for name, parameter in signature.parameters.items()
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

    def _result_reference(self, context, row_id):
        return self.discovery._reference(
            context,
            "contactOptOutFanoutResults",
            f"{context['fanout']['fanoutId']}--{row_id}",
        )

    def _obligation_reference(self, context, row_id):
        return self.discovery._reference(
            context,
            "contactOptOutFanoutObligations",
            f"{context['fanout']['fanoutId']}--{row_id}",
        )

    def _row_head_reference(self, context, row_id):
        return self.discovery._reference(
            context,
            "rowAuthorityHeads",
            row_id,
        )

    def _build_result(
        self,
        context,
        obligation,
        row_head,
        *,
        obligation_hash=None,
        observed_row_head_hash=None,
        disposition="noop",
        reason_code="row_deleted",
        claim_request_id=None,
        claim_set_hash=None,
        row_generation=None,
        row_settlement_hash=None,
        released_row_generation=None,
        released_row_settlement_hash=None,
        restored_effective_generation=None,
        restored_effective_settlement_hash=None,
        created_at=None,
    ):
        return self.module.build_contact_fanout_result_document(
            user_scope_hash=context["scope"],
            fanout_id=context["fanout"]["fanoutId"],
            row_id=obligation["rowId"],
            obligation_hash=(
                obligation_hash
                or obligation["contactFanoutObligationHash"]
            ),
            outcome=context["fanout"]["outcome"],
            disposition=disposition,
            reason_code=reason_code,
            observed_row_head_hash=(
                observed_row_head_hash or row_head["headHash"]
            ),
            claim_request_id=claim_request_id,
            claim_set_hash=claim_set_hash,
            row_generation=row_generation,
            row_settlement_hash=row_settlement_hash,
            released_row_generation=released_row_generation,
            released_row_settlement_hash=released_row_settlement_hash,
            restored_effective_generation=restored_effective_generation,
            restored_effective_settlement_hash=(
                restored_effective_settlement_hash
            ),
            created_at=created_at or self.paired_at,
        )

    def _seed(
        self,
        count,
        *,
        result_count=None,
        cursor=None,
        lifecycle="deleted",
    ):
        rows = [_row_id(index) for index in range(1, count + 1)]
        context = self.discovery._seed(rows)
        obligations = []
        results = []
        row_heads = []
        resolved = count if result_count is None else result_count
        for index, (row_id, edge) in enumerate(
            zip(rows, context["edges"], strict=True)
        ):
            _identity, row_head = context["transition"].fixture._seed_row(
                context["store"],
                row_id,
                lifecycle=lifecycle,
            )
            obligation = self.discovery._obligation(
                context,
                edge,
                created_at=self.paired_at,
            )
            obligations.append(obligation)
            row_heads.append(row_head)
            if index < resolved:
                result = self._build_result(context, obligation, row_head)
                self._result_reference(context, row_id).create(result)
                results.append(result)
        applying = self.discovery._fanout(
            context["fanout"],
            state_revision=context["fanout"]["stateRevision"] + 1,
            state="applying",
            discovery_cursor_row_id=cursor,
            obligation_count=count,
            result_count=resolved,
            updated_at=self.paired_at,
        )
        self.discovery._store_fanout(context, applying)
        context.update(
            {
                "rows": rows,
                "obligations": obligations,
                "results": results,
                "rowHeads": row_heads,
            }
        )
        return context

    def _replace_result(self, context, result):
        self._result_reference(context, result["rowId"]).set(
            result,
            merge=False,
        )
        replaced = [
            result if item["rowId"] == result["rowId"] else item
            for item in context["results"]
        ]
        if not any(item["rowId"] == result["rowId"] for item in replaced):
            replaced.append(result)
            replaced.sort(key=lambda item: item["rowId"])
        context["results"] = replaced
        context["store"].events.clear()
        return result

    def _active_contact_authority(self, context):
        if context["settlement"]["transitionKind"] == "verified_optout":
            settlement = context["settlement"]
        else:
            predecessor = context["settlement"]["predecessorSettlementHash"]
            settlement = next(
                document
                for document in context["transition"]._documents(
                    context["store"],
                    "contactOptOutSettlements",
                ).values()
                if document["contactSettlementHash"] == predecessor
            )
        receipt = context["transition"]._documents(
            context["store"],
            "contactOptOutTransitionRequests",
        )[settlement["contactTransitionId"]]
        return settlement, receipt

    def _install_contact_lineage(
        self,
        context,
        observed_head,
        *,
        materialize_head,
        canonical_hash=None,
        contact_settlement_hash=None,
        fanout_id=None,
        generation_number=1,
        predecessor_settlement_hash=None,
        first_fencing_token=1,
        authority_link=None,
        claimed_at="2026-08-04T12:05:00.000000Z",
        settled_at="2026-08-04T12:05:20.000000Z",
    ):
        active_settlement, active_receipt = self._active_contact_authority(
            context
        )
        row_id = observed_head["rowId"]
        claim = self.module.build_claim_set_document(
            user_scope_hash=context["scope"],
            authority_origin="contact_fanout",
            authority_link=(
                authority_link or active_settlement["authorityLink"]
            ),
            operator_action_document=None,
            fanout_id=fanout_id or active_receipt["resultingFanoutId"],
            row_ids=[row_id],
            primary_row_id=row_id,
            planned_writes=3,
            outcome="accepted",
            row_decisions=[
                {
                    "rowId": row_id,
                    "decision": "accepted",
                    "plannedGeneration": generation_number,
                    "winnerGenerationHash": None,
                    "winnerSettlementHash": None,
                }
            ],
            created_at=claimed_at,
            canonical_mailbox_identity_hash=(
                canonical_hash or context["canonicalHash"]
            ),
            contact_settlement_hash=(
                contact_settlement_hash
                or active_settlement["contactSettlementHash"]
            ),
        )
        generation = self.module.build_owner_generation_document(
            claim_set_document=claim,
            row_id=row_id,
            generation=generation_number,
            predecessor_head_hash=observed_head["headHash"],
            predecessor_settlement_hash=predecessor_settlement_hash,
            lease_epoch=1,
            first_fencing_token=first_fencing_token,
            created_at=claimed_at,
        )
        claimed_head = self.module._build_claim_advanced_head(
            expected_head=observed_head,
            generation_document=generation,
            lease_owner_hash="a" * 64,
            lease_until="2026-08-04T12:09:00.000000Z",
            dominated_predecessor_settlement_hash=None,
            claimed_at=claimed_at,
        )
        settlement = self.module.build_owner_settlement_document(
            generation_document=generation,
            claim_set_document=claim,
            fencing_token=claimed_head["fencingToken"],
            outcome="contact_optout",
            settled_at=settled_at,
            superseded_effective_settlement_hash=(
                predecessor_settlement_hash
            ),
        )
        settled_head = self.module._build_settlement_advanced_head(
            expected_head=claimed_head,
            generation_document=generation,
            settlement_document=settlement,
        )
        fixture = context["transition"].fixture
        fixture._claim_reference(context["store"], claim["requestId"]).create(
            claim
        )
        fixture._generation_reference(
            context["store"],
            row_id,
            generation_number,
        ).create(generation)
        fixture._settlement_reference(
            context["store"],
            row_id,
            generation_number,
        ).create(settlement)
        if materialize_head:
            fixture._row_references(context["store"], row_id)[1].set(
                settled_head,
                merge=False,
            )
        context["store"].events.clear()
        return {
            "claim": claim,
            "generation": generation,
            "settlement": settlement,
            "observedHead": observed_head,
            "settledHead": settled_head,
        }

    def _install_dominated_claim(
        self,
        context,
        *,
        winner_generation_hash,
        winner_settlement_hash,
    ):
        active_settlement, active_receipt = self._active_contact_authority(
            context
        )
        row_id = context["rows"][0]
        claim = self.module.build_claim_set_document(
            user_scope_hash=context["scope"],
            authority_origin="contact_fanout",
            authority_link=active_settlement["authorityLink"],
            operator_action_document=None,
            fanout_id=active_receipt["resultingFanoutId"],
            row_ids=[row_id],
            primary_row_id=row_id,
            planned_writes=1,
            outcome="dominated",
            row_decisions=[
                {
                    "rowId": row_id,
                    "decision": "dominated",
                    "plannedGeneration": None,
                    "winnerGenerationHash": winner_generation_hash,
                    "winnerSettlementHash": winner_settlement_hash,
                }
            ],
            created_at="2026-08-04T12:05:30.000000Z",
            canonical_mailbox_identity_hash=context["canonicalHash"],
            contact_settlement_hash=active_settlement[
                "contactSettlementHash"
            ],
        )
        context["transition"].fixture._claim_reference(
            context["store"],
            claim["requestId"],
        ).create(claim)
        context["store"].events.clear()
        return claim

    def _seed_release_matrix(self):
        row_id = _row_id(1)
        context = self.discovery._seed_release([row_id])
        _identity, row_head = context["transition"].fixture._seed_row(
            context["store"],
            row_id,
            lifecycle="active",
        )
        obligation = self.discovery._obligation(
            context,
            context["edges"][0],
            created_at=self.release_result_at,
        )
        applying = self.discovery._fanout(
            context["fanout"],
            state_revision=context["fanout"]["stateRevision"] + 1,
            state="applying",
            discovery_cursor_row_id=None,
            obligation_count=1,
            result_count=1,
            updated_at=self.release_result_at,
        )
        self.discovery._store_fanout(context, applying)
        context.update(
            {
                "rows": [row_id],
                "obligations": [obligation],
                "results": [],
                "rowHeads": [row_head],
            }
        )
        return context

    def _install_clear_release_after_image(self, context, lineage, result):
        released_head = {
            key: deepcopy(value)
            for key, value in lineage["settledHead"].items()
            if key != "headHash"
        }
        released_head.update(
            {
                "stateRevision": lineage["settledHead"]["stateRevision"] + 1,
                "effectiveOwnerGeneration": None,
                "effectiveOwnerGenerationHash": None,
                "effectiveOwnerKind": None,
                "effectivePriority": None,
                "state": "clear",
                "leaseOwnerHash": None,
                "leaseUntil": None,
                "fencingToken": None,
                "latestSettlementHash": lineage["settlement"][
                    "settlementHash"
                ],
                "effectiveSettlementHash": None,
                "latestOptOutReleaseResultHash": result[
                    "contactFanoutResultHash"
                ],
                "updatedAt": result["createdAt"],
            }
        )
        released_head = context["transition"].fixture._rehash_head(
            released_head
        )
        self._row_head_reference(
            context,
            result["rowId"],
        ).set(released_head, merge=False)
        context["store"].events.clear()
        return released_head

    def _install_terminal_successor(self, context, lineage):
        fixture = context["transition"].fixture
        bundle = fixture._seed_b1_bundle(
            context["store"],
            owner_kind="terminal",
            source_id="completion-matrix-terminal",
        )
        authority_link = self.module.build_b1_authority_link(
            user_scope_hash=context["scope"],
            source_identity_document=bundle["identity"],
            source_classification_document=bundle["classification"],
            source_owner_document=bundle["owner"],
            source_ledger_document=bundle["ledger"],
            work_key=bundle["work_key"],
        )
        row_id = lineage["settledHead"]["rowId"]
        claim = self.module.build_claim_set_document(
            user_scope_hash=context["scope"],
            authority_origin="b1_source",
            authority_link=authority_link,
            operator_action_document=None,
            fanout_id=None,
            row_ids=[row_id],
            primary_row_id=row_id,
            planned_writes=3,
            outcome="accepted",
            row_decisions=[
                {
                    "rowId": row_id,
                    "decision": "accepted",
                    "plannedGeneration": 2,
                    "winnerGenerationHash": None,
                    "winnerSettlementHash": None,
                }
            ],
            created_at="2026-08-04T12:05:30.000000Z",
        )
        generation = self.module.build_owner_generation_document(
            claim_set_document=claim,
            row_id=row_id,
            generation=2,
            predecessor_head_hash=lineage["settledHead"]["headHash"],
            predecessor_settlement_hash=lineage["settlement"][
                "settlementHash"
            ],
            lease_epoch=1,
            first_fencing_token=2,
            created_at="2026-08-04T12:05:30.000000Z",
        )
        claimed_head = self.module._build_claim_advanced_head(
            expected_head=lineage["settledHead"],
            generation_document=generation,
            lease_owner_hash="b" * 64,
            lease_until="2026-08-04T12:09:00.000000Z",
            dominated_predecessor_settlement_hash=None,
            claimed_at="2026-08-04T12:05:30.000000Z",
        )
        settlement = self.module.build_owner_settlement_document(
            generation_document=generation,
            claim_set_document=claim,
            fencing_token=claimed_head["fencingToken"],
            outcome="terminal",
            settled_at="2026-08-04T12:05:50.000000Z",
        )
        settled_head = self.module._build_settlement_advanced_head(
            expected_head=claimed_head,
            generation_document=generation,
            settlement_document=settlement,
        )
        fixture._claim_reference(context["store"], claim["requestId"]).create(
            claim
        )
        fixture._generation_reference(context["store"], row_id, 2).create(
            generation
        )
        fixture._settlement_reference(context["store"], row_id, 2).create(
            settlement
        )
        self._row_head_reference(context, row_id).set(
            settled_head,
            merge=False,
        )
        context["store"].events.clear()
        return {
            "claim": claim,
            "generation": generation,
            "settlement": settlement,
            "settledHead": settled_head,
        }

    def _certify(
        self,
        context,
        expected=None,
        *,
        owner=None,
        certified_at=None,
        executor=None,
    ):
        self._method()
        return context["transition"]._authority(
            context["store"],
            executor=executor,
        ).certify_contact_fanout_page(
            verified_user_id=context["transition"].fixture.user_id,
            fanout_id=context["fanout"]["fanoutId"],
            expected_fanout_head=(expected or context["fanout"]),
            lease_owner_hash=(
                self.discovery.lease_owner if owner is None else owner
            ),
            certified_at=certified_at or self.certified_at,
        )

    def _certify_with_queries(
        self,
        context,
        expected=None,
        *,
        certified_at=None,
    ):
        observed = []
        original = BoundedFakeTransaction.get_query

        def inspect_query(transaction, query):
            snapshots = original(transaction, query)
            observed.append(
                {
                    "collection": query._collection.path.rsplit("/", 1)[-1],
                    "filters": query._filters,
                    "ordering": query._ordering,
                    "directions": query._directions,
                    "limit": query._limit_count,
                    "cursor": query._start_after_values,
                    "cursorPath": query._start_after_path,
                    "documents": tuple(
                        snapshot.to_dict() for snapshot in snapshots
                    ),
                }
            )
            return snapshots

        with patch.object(
            BoundedFakeTransaction,
            "get_query",
            new=inspect_query,
        ):
            result = self._certify(
                context,
                expected,
                certified_at=certified_at,
            )
        return result, observed

    def _assert_result(self, result, disposition):
        self.assertEqual(
            {"disposition", "fanoutHead", "obligations", "results"},
            set(result),
        )
        self.assertEqual(disposition, result["disposition"])
        self.module.validate_contact_fanout_head_document(
            document=result["fanoutHead"]
        )
        obligations = [
            self.module.validate_contact_fanout_obligation_document(
                document=document
            )
            for document in result["obligations"]
        ]
        results = [
            self.module.validate_contact_fanout_result_document(
                document=document
            )
            for document in result["results"]
        ]
        self.assertEqual(
            [document["rowId"] for document in obligations],
            [document["rowId"] for document in results],
        )
        self.assertTrue(
            all(
                result_document["obligationHash"]
                == obligation["contactFanoutObligationHash"]
                for obligation, result_document in zip(
                    obligations,
                    results,
                    strict=True,
                )
            )
        )

    def _assert_query_shapes(self, context, observed, *, cursor):
        collections = [item["collection"] for item in observed]
        self.assertEqual(4, len(observed))
        self.assertCountEqual(
            [
                "contactOptOutSettlements",
                "contactRowBindings",
                "contactOptOutFanoutObligations",
                "contactOptOutFanoutResults",
            ],
            collections,
        )
        by_collection = {item["collection"]: item for item in observed}
        settlement = by_collection["contactOptOutSettlements"]
        self.assertEqual(
            (
                (
                    "contactSettlementHash",
                    "==",
                    context["fanout"]["expectedContactSettlementHash"],
                ),
            ),
            settlement["filters"],
        )
        self.assertEqual(("__name__",), settlement["ordering"])
        self.assertEqual(("ASCENDING",), settlement["directions"])
        self.assertEqual(2, settlement["limit"])
        self.assertIsNone(settlement["cursor"])
        binding = by_collection["contactRowBindings"]
        self.assertEqual(
            (
                (
                    "canonicalMailboxIdentityHash",
                    "==",
                    context["canonicalHash"],
                ),
            ),
            binding["filters"],
        )
        self.assertEqual(("rowId",), binding["ordering"])
        self.assertEqual(("ASCENDING",), binding["directions"])
        self.assertEqual(33, binding["limit"])
        self.assertEqual(
            None if cursor is None else (cursor,),
            binding["cursor"],
        )
        self.assertIsNone(binding["cursorPath"])
        for collection in (
            "contactOptOutFanoutObligations",
            "contactOptOutFanoutResults",
        ):
            query = by_collection[collection]
            self.assertEqual(
                (("fanoutId", "==", context["fanout"]["fanoutId"]),),
                query["filters"],
            )
            self.assertEqual(("rowId",), query["ordering"])
            self.assertEqual(("ASCENDING",), query["directions"])
            self.assertEqual(33, query["limit"])
            self.assertEqual(
                None if cursor is None else (cursor,),
                query["cursor"],
            )
            self.assertIsNone(query["cursorPath"])
        paired_collections = (
            "contactRowBindings",
            "contactOptOutFanoutObligations",
            "contactOptOutFanoutResults",
        )
        row_pages = [
            [document["rowId"] for document in by_collection[name]["documents"]]
            for name in paired_collections
        ]
        self.assertEqual(row_pages[0], row_pages[1])
        self.assertEqual(row_pages[0], row_pages[2])
        self.assertTrue(
            all(
                edge["contactRowEdgeHash"]
                == obligation["contactRowEdgeHash"]
                for edge, obligation in zip(
                    by_collection["contactRowBindings"]["documents"],
                    by_collection[
                        "contactOptOutFanoutObligations"
                    ]["documents"],
                    strict=True,
                )
            )
        )

    def _expected_head(self, before, *, cursor, certified_at, complete):
        overrides = {
            "state_revision": before["stateRevision"] + 1,
            "discovery_cursor_row_id": cursor,
            "cursor_processed_count": (
                0
                if cursor is None
                else before["cursorProcessedCount"] + 32
            ),
            "updated_at": certified_at,
        }
        if complete:
            overrides.update(
                {
                    "state": "complete",
                    "lease_owner_hash": None,
                    "lease_until": None,
                    "completion_binding_revision": before[
                        "bindingRevision"
                    ],
                    "completion_binding_head_hash": before[
                        "bindingHeadHash"
                    ],
                    "completion_binding_association_count": before[
                        "bindingAssociationCount"
                    ],
                    "completion_obligation_count": before[
                        "obligationCount"
                    ],
                    "completion_result_count": before["resultCount"],
                    "completed_at": certified_at,
                }
            )
        return self.discovery._fanout(before, **overrides)

    def _assert_one_head_write(self, context, expected_head):
        writes = self._writes(context["store"])
        self.assertEqual(1, len(writes))
        operation, path, payload, merge = writes[0]
        self.assertEqual(("set", False), (operation, merge))
        self.assertTrue(
            path.endswith(
                f"/contactOptOutFanoutHeads/{context['fanout']['fanoutId']}"
            )
        )
        self.assertEqual(expected_head, payload)

    def _assert_failure_without_write(self, context, operation=None):
        before = deepcopy(context["store"].data)
        context["store"].events.clear()
        with self.assertRaises(self.module.RowAuthorityError):
            (operation or (lambda: self._certify(context)))()
        self.assertEqual(before, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))

    def test_completion_requires_no_next_obligation_equal_counts_and_stable_binding(self):
        self._method()

        unequal = self._seed(1, result_count=0)
        result, queries = self._certify_with_queries(unequal)
        self._assert_result(result, "needs_processing")
        self.assertEqual(unequal["fanout"], result["fanoutHead"])
        self.assertEqual([], result["obligations"])
        self.assertEqual([], result["results"])
        self.assertFalse(
            {
                "contactOptOutFanoutObligations",
                "contactOptOutFanoutResults",
            }
            & {query["collection"] for query in queries}
        )
        self.assertEqual([], self._writes(unequal["store"]))

        complete = self._seed(0)
        before = complete["fanout"]
        result = self._certify(complete)
        self._assert_result(result, "certification_complete")
        expected = self._expected_head(
            before,
            cursor=None,
            certified_at=self.certified_at,
            complete=True,
        )
        self.assertEqual(expected, result["fanoutHead"])
        self._assert_one_head_write(complete, expected)
        complete["store"].events.clear()
        self.assertEqual(result, self._certify(complete, before))
        self.assertEqual([], self._writes(complete["store"]))

        drifted = self._seed(1)
        self.discovery._add_binding(
            drifted,
            _row_id(2),
            created_at="2026-08-04T12:05:50.000000Z",
        )
        self._assert_failure_without_write(drifted)

        request_cases = (
            (
                "wrong-owner",
                lambda context: self._certify(context, owner="e" * 64),
            ),
            (
                "deadline-equality",
                lambda context: self._certify(
                    context,
                    certified_at=self.discovery.lease_until,
                ),
            ),
            (
                "expired-deadline",
                lambda context: self._certify(
                    context,
                    certified_at="2026-08-04T12:10:00.000001Z",
                ),
            ),
        )
        for label, operation in request_cases:
            with self.subTest(gate=label):
                rejected = self._seed(1)
                self._assert_failure_without_write(
                    rejected,
                    lambda: operation(rejected),
                )

        with self.subTest(gate="unleased"):
            rejected = self._seed(1)
            unleased = self.discovery._fanout(
                rejected["fanout"],
                state_revision=rejected["fanout"]["stateRevision"] + 1,
                lease_owner_hash=None,
                lease_until=None,
                updated_at="2026-08-04T12:05:50.000000Z",
            )
            self.discovery._store_fanout(rejected, unleased)
            self._assert_failure_without_write(rejected)

        with self.subTest(gate="stale-fence-and-expected-head"):
            rejected = self._seed(1)
            stale = rejected["fanout"]
            rejected["transition"]._authority(
                rejected["store"]
            ).acquire_contact_fanout_lease(
                verified_user_id=rejected["transition"].fixture.user_id,
                fanout_id=stale["fanoutId"],
                expected_fanout_head=stale,
                lease_owner_hash=self.discovery.lease_owner,
                lease_until="2026-08-04T12:11:00.000000Z",
                acquired_at="2026-08-04T12:05:50.000000Z",
            )
            self._assert_failure_without_write(
                rejected,
                lambda: self._certify(rejected, stale),
            )

        with self.subTest(gate="non-applying"):
            rejected = self._seed(1)
            discovering = self.discovery._fanout(
                rejected["fanout"],
                state_revision=rejected["fanout"]["stateRevision"] + 1,
                state="discovering",
                updated_at="2026-08-04T12:05:50.000000Z",
            )
            self.discovery._store_fanout(rejected, discovering)
            self._assert_failure_without_write(rejected)

        with self.subTest(gate="terminal"):
            rejected = self._seed(1)
            terminal = self._expected_head(
                rejected["fanout"],
                cursor=None,
                certified_at="2026-08-04T12:05:50.000000Z",
                complete=True,
            )
            self.discovery._store_fanout(rejected, terminal)
            self._assert_failure_without_write(rejected)

        with self.subTest(commit="preapply"):
            uncertain = self._seed(1)
            uncertain["store"].fail_next_commit = RuntimeError(
                "configured certification preapply failure"
            )
            self._assert_failure_without_write(uncertain)

        with self.subTest(commit="apply-then-raise"):
            uncertain = self._seed(1)
            uncertain["store"].apply_then_raise_next_commit = RuntimeError(
                "unknown certification commit"
            )
            applied = self._certify(uncertain)
            self._assert_result(applied, "certification_complete")
            self.assertIn(
                ("commit_raised_after_apply",),
                uncertain["store"].events,
            )

        with self.subTest(commit="drifted-readback"):
            uncertain = self._seed(33)

            def drifted_executor(transaction, callback):
                transaction._begin()
                callback(transaction)
                transaction._rollback()
                drifted = self.discovery._fanout(
                    uncertain["fanout"],
                    state_revision=(
                        uncertain["fanout"]["stateRevision"] + 1
                    ),
                    discovery_cursor_row_id=uncertain["rows"][0],
                    cursor_processed_count=1,
                    updated_at=self.certified_at,
                )
                self.discovery._reference(
                    uncertain,
                    "contactOptOutFanoutHeads",
                    uncertain["fanout"]["fanoutId"],
                ).set(drifted, merge=False)
                raise RuntimeError("drifted certification readback")

            with self.assertRaises(self.module.RowAuthorityAmbiguous):
                self._certify(uncertain, executor=drifted_executor)

        with self.subTest(commit="same-page-race"):
            raced = self._seed(1)
            raced["store"].before_commit_barrier = Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(self._certify, raced) for _ in range(2)
                ]
                results = [future.result(timeout=10) for future in futures]
            self.assertEqual(results[0], results[1])
            self.assertEqual(
                1,
                raced["store"].events.count(("commit_applied", 1)),
            )
            self.assertEqual(
                1,
                raced["store"].events.count(("commit_applied", 0)),
            )

    def test_certification_pages_32_with_33rd_as_sentinel(self):
        self._method()
        expected_pages = {0: 1, 1: 1, 32: 1, 33: 2, 128: 4, 129: 5}
        for count, page_count in expected_pages.items():
            with self.subTest(count=count):
                context = self._seed(count)
                expected = context["fanout"]
                cursor = None
                offset = 0
                for page_index in range(page_count):
                    certified_at = (
                        f"2026-08-04T12:06:{page_index:02d}.000000Z"
                    )
                    context["store"].events.clear()
                    result, queries = self._certify_with_queries(
                        context,
                        expected,
                        certified_at=certified_at,
                    )
                    terminal = page_index + 1 == page_count
                    disposition = (
                        "certification_complete"
                        if terminal
                        else "page_certified"
                    )
                    self._assert_result(result, disposition)
                    page_rows = context["rows"][offset : offset + 32]
                    self.assertEqual(
                        page_rows,
                        [item["rowId"] for item in result["obligations"]],
                    )
                    self._assert_query_shapes(
                        context,
                        queries,
                        cursor=cursor,
                    )
                    next_cursor = None if terminal else page_rows[-1]
                    expected_after = self._expected_head(
                        expected,
                        cursor=next_cursor,
                        certified_at=certified_at,
                        complete=terminal,
                    )
                    self.assertEqual(expected_after, result["fanoutHead"])
                    self._assert_one_head_write(context, expected_after)
                    if page_index == 0 and count in {1, 33}:
                        context["store"].events.clear()
                        retry = self._certify(
                            context,
                            expected,
                            certified_at=certified_at,
                        )
                        self.assertEqual(result, retry)
                        self.assertEqual(
                            [],
                            self._writes(context["store"]),
                        )
                    if not terminal and offset + 32 < count:
                        sentinel = self._row_head_reference(
                            context,
                            context["rows"][offset + 32],
                        ).path
                        self.assertNotIn(
                            sentinel,
                            [
                                event[1]
                                for event in context["store"].events
                                if event[0] == "get"
                            ],
                        )
                    expected = expected_after
                    cursor = next_cursor
                    offset += len(page_rows)

    def test_certification_rejects_missing_swapped_or_extra_evidence(self):
        self._method()

        with self.subTest(case="matched-missing-triple-frozen-two"):
            context = self._seed(2)
            missing_index = 1
            missing_row = context["rows"][missing_index]
            missing_edge = context["edges"][missing_index]
            binding_ref = self.discovery._reference(
                context,
                "contactRowBindings",
                missing_edge["edgeId"],
            )
            context["store"].data.pop(binding_ref.path)
            context["store"].data.pop(
                self._obligation_reference(context, missing_row).path
            )
            context["store"].data.pop(
                self._result_reference(context, missing_row).path
            )
            self._assert_failure_without_write(context)

        with self.subTest(case="matched-extra-triple-frozen-one"):
            context = self._seed(1)
            extra_row = _row_id(2)
            _identity, extra_head = context["transition"].fixture._seed_row(
                context["store"],
                extra_row,
                lifecycle="deleted",
            )
            extra_edge = self.module.build_contact_row_binding_document(
                user_scope_hash=context["scope"],
                canonical_mailbox_identity_hash=context["canonicalHash"],
                row_id=extra_row,
                created_at="2026-08-04T12:01:00.000002Z",
            )
            extra_obligation = (
                self.module.build_contact_fanout_obligation_document(
                    user_scope_hash=context["scope"],
                    fanout_id=context["fanout"]["fanoutId"],
                    row_id=extra_row,
                    contact_row_edge_hash=extra_edge["contactRowEdgeHash"],
                    expected_contact_settlement_hash=context["fanout"][
                        "expectedContactSettlementHash"
                    ],
                    outcome=context["fanout"]["outcome"],
                    created_at=self.paired_at,
                )
            )
            extra_result = self._build_result(
                context,
                extra_obligation,
                extra_head,
            )
            self.discovery._reference(
                context,
                "contactRowBindings",
                extra_edge["edgeId"],
            ).create(extra_edge)
            self._obligation_reference(context, extra_row).create(
                extra_obligation
            )
            self._result_reference(context, extra_row).create(extra_result)
            self._assert_failure_without_write(context)

        with self.subTest(case="matched-post-cursor-triple-deletion"):
            context = self._seed(33)
            first = self._certify(context)
            self._assert_result(first, "page_certified")
            missing_index = 32
            missing_row = context["rows"][missing_index]
            missing_edge = context["edges"][missing_index]
            binding_ref = self.discovery._reference(
                context,
                "contactRowBindings",
                missing_edge["edgeId"],
            )
            context["store"].data.pop(binding_ref.path)
            context["store"].data.pop(
                self._obligation_reference(context, missing_row).path
            )
            context["store"].data.pop(
                self._result_reference(context, missing_row).path
            )
            self._assert_failure_without_write(
                context,
                lambda: self._certify(context, first["fanoutHead"]),
            )

        with self.subTest(case="matched-missing-pair-frozen-two"):
            context = self._seed(2)
            missing_row = context["rows"][1]
            context["store"].data.pop(
                self._obligation_reference(context, missing_row).path
            )
            context["store"].data.pop(
                self._result_reference(context, missing_row).path
            )
            self._assert_failure_without_write(context)

        with self.subTest(case="matched-extra-pair-frozen-one"):
            context = self._seed(1)
            extra_row = _row_id(2)
            _identity, extra_head = context["transition"].fixture._seed_row(
                context["store"],
                extra_row,
                lifecycle="deleted",
            )
            extra_obligation = (
                self.module.build_contact_fanout_obligation_document(
                    user_scope_hash=context["scope"],
                    fanout_id=context["fanout"]["fanoutId"],
                    row_id=extra_row,
                    contact_row_edge_hash="e" * 64,
                    expected_contact_settlement_hash=context["fanout"][
                        "expectedContactSettlementHash"
                    ],
                    outcome=context["fanout"]["outcome"],
                    created_at=self.paired_at,
                )
            )
            extra_result = self._build_result(
                context,
                extra_obligation,
                extra_head,
            )
            self._obligation_reference(context, extra_row).create(
                extra_obligation
            )
            self._result_reference(context, extra_row).create(extra_result)
            self._assert_failure_without_write(context)

        with self.subTest(case="matched-post-cursor-deletion"):
            context = self._seed(33)
            first = self._certify(context)
            self._assert_result(first, "page_certified")
            missing_row = context["rows"][32]
            context["store"].data.pop(
                self._obligation_reference(context, missing_row).path
            )
            context["store"].data.pop(
                self._result_reference(context, missing_row).path
            )
            self._assert_failure_without_write(
                context,
                lambda: self._certify(context, first["fanoutHead"]),
            )

        with self.subTest(matrix="apply-applied-reachable"):
            context = self._seed(1, lifecycle="active")
            lineage = self._install_contact_lineage(
                context,
                context["rowHeads"][0],
                materialize_head=True,
            )
            result = self._build_result(
                context,
                context["obligations"][0],
                lineage["observedHead"],
                disposition="applied",
                reason_code="claim_accepted",
                claim_request_id=lineage["claim"]["requestId"],
                claim_set_hash=lineage["claim"]["claimSetHash"],
                row_generation=lineage["generation"]["generation"],
                row_settlement_hash=lineage["settlement"][
                    "settlementHash"
                ],
            )
            self._replace_result(context, result)
            certified = self._certify(context)
            self._assert_result(certified, "certification_complete")

        with self.subTest(matrix="apply-applied-unreachable-after-image"):
            context = self._seed(1, lifecycle="active")
            lineage = self._install_contact_lineage(
                context,
                context["rowHeads"][0],
                materialize_head=False,
            )
            result = self._build_result(
                context,
                context["obligations"][0],
                lineage["observedHead"],
                disposition="applied",
                reason_code="claim_accepted",
                claim_request_id=lineage["claim"]["requestId"],
                claim_set_hash=lineage["claim"]["claimSetHash"],
                row_generation=lineage["generation"]["generation"],
                row_settlement_hash=lineage["settlement"][
                    "settlementHash"
                ],
            )
            self._replace_result(context, result)
            self._assert_failure_without_write(context)

        for valid_winner in (True, False):
            with self.subTest(matrix="apply-dominated", valid=valid_winner):
                context = self._seed(1, lifecycle="active")
                fixture = context["transition"].fixture
                winner_claim, winner_generation, claimed_head = (
                    fixture._install_owner(
                        context["store"],
                        context["rows"][0],
                        owner_kind="terminal",
                    )
                )
                winner_settlement, winner_head = (
                    fixture._settle_terminal_owner(
                        context["store"],
                        winner_claim,
                        winner_generation,
                        claimed_head,
                        settled_at="2026-08-04T12:05:20.000000Z",
                    )
                )
                claim = self._install_dominated_claim(
                    context,
                    winner_generation_hash=(
                        winner_generation["generationHash"]
                        if valid_winner
                        else "a" * 64
                    ),
                    winner_settlement_hash=(
                        winner_settlement["settlementHash"]
                        if valid_winner
                        else "b" * 64
                    ),
                )
                result = self._build_result(
                    context,
                    context["obligations"][0],
                    winner_head,
                    disposition="dominated",
                    reason_code="claim_dominated",
                    claim_request_id=claim["requestId"],
                    claim_set_hash=claim["claimSetHash"],
                )
                self._replace_result(context, result)
                if valid_winner:
                    self._assert_result(
                        self._certify(context),
                        "certification_complete",
                    )
                else:
                    self._assert_failure_without_write(context)

        with self.subTest(matrix="apply-noop-deleted-valid"):
            context = self._seed(1)
            self._assert_result(
                self._certify(context),
                "certification_complete",
            )

        with self.subTest(matrix="apply-noop-deleted-active-row"):
            context = self._seed(1, lifecycle="active")
            self._assert_failure_without_write(context)

        with self.subTest(matrix="apply-superseded-current-fanout"):
            context = self._seed(1, lifecycle="active")
            result = self._build_result(
                context,
                context["obligations"][0],
                context["rowHeads"][0],
                disposition="superseded",
                reason_code="contact_head_advanced",
            )
            self._replace_result(context, result)
            self._assert_failure_without_write(context)

        for valid_lineage in (True, False):
            with self.subTest(
                matrix="release-restore",
                valid=valid_lineage,
            ):
                context = self._seed_release_matrix()
                lineage = self._install_contact_lineage(
                    context,
                    context["rowHeads"][0],
                    materialize_head=True,
                    canonical_hash=(
                        None if valid_lineage else "c" * 64
                    ),
                )
                result = self._build_result(
                    context,
                    context["obligations"][0],
                    lineage["settledHead"],
                    disposition="restore",
                    reason_code="exact_predecessor",
                    released_row_generation=lineage["generation"][
                        "generation"
                    ],
                    released_row_settlement_hash=lineage["settlement"][
                        "settlementHash"
                    ],
                    created_at=self.release_result_at,
                )
                self._replace_result(context, result)
                self._install_clear_release_after_image(
                    context,
                    lineage,
                    result,
                )
                if valid_lineage:
                    self._assert_result(
                        self._certify(
                            context,
                            certified_at=self.release_certified_at,
                        ),
                        "certification_complete",
                    )
                else:
                    self._assert_failure_without_write(
                        context,
                        lambda: self._certify(
                            context,
                            certified_at=self.release_certified_at,
                        ),
                    )

        for row_was_applied in (False, True):
            with self.subTest(
                matrix="release-noop-not-applied",
                row_was_applied=row_was_applied,
            ):
                context = self._seed_release_matrix()
                row_head = context["rowHeads"][0]
                if row_was_applied:
                    lineage = self._install_contact_lineage(
                        context,
                        row_head,
                        materialize_head=True,
                    )
                    row_head = lineage["settledHead"]
                result = self._build_result(
                    context,
                    context["obligations"][0],
                    row_head,
                    disposition="noop",
                    reason_code="row_optout_not_applied",
                    created_at=self.release_result_at,
                )
                self._replace_result(context, result)
                if row_was_applied:
                    self._assert_failure_without_write(
                        context,
                        lambda: self._certify(
                            context,
                            certified_at=self.release_certified_at,
                        ),
                    )
                else:
                    self._assert_result(
                        self._certify(
                            context,
                            certified_at=self.release_certified_at,
                        ),
                        "certification_complete",
                    )

        with self.subTest(matrix="release-noop-different-owner-lineage"):
            context = self._seed_release_matrix()
            lineage = self._install_contact_lineage(
                context,
                context["rowHeads"][0],
                materialize_head=False,
                canonical_hash="c" * 64,
            )
            result = self._build_result(
                context,
                context["obligations"][0],
                context["rowHeads"][0],
                disposition="noop",
                reason_code="different_effective_owner",
                released_row_generation=lineage["generation"]["generation"],
                released_row_settlement_hash=lineage["settlement"][
                    "settlementHash"
                ],
                created_at=self.release_result_at,
            )
            self._replace_result(context, result)
            self._assert_failure_without_write(
                context,
                lambda: self._certify(
                    context,
                    certified_at=self.release_certified_at,
                ),
            )

        with self.subTest(matrix="release-superseded-current-fanout"):
            context = self._seed_release_matrix()
            result = self._build_result(
                context,
                context["obligations"][0],
                context["rowHeads"][0],
                disposition="superseded",
                reason_code="contact_head_advanced",
                created_at=self.release_result_at,
            )
            self._replace_result(context, result)
            self._assert_failure_without_write(
                context,
                lambda: self._certify(
                    context,
                    certified_at=self.release_certified_at,
                ),
            )

        with self.subTest(case="missing-result"):
            context = self._seed(2)
            context["store"].data.pop(
                self._result_reference(context, context["rows"][0]).path
            )
            self._assert_failure_without_write(context)

        with self.subTest(case="swapped-obligation-hashes"):
            context = self._seed(2)
            for index, other in ((0, 1), (1, 0)):
                crossed = self._build_result(
                    context,
                    context["obligations"][index],
                    context["rowHeads"][index],
                    obligation_hash=context["obligations"][other][
                        "contactFanoutObligationHash"
                    ],
                )
                context["store"].data[
                    self._result_reference(
                        context,
                        context["rows"][index],
                    ).path
                ] = crossed
            self._assert_failure_without_write(context)

        with self.subTest(case="extra-result"):
            context = self._seed(1)
            extra_row = _row_id(2)
            _identity, extra_head = context["transition"].fixture._seed_row(
                context["store"],
                extra_row,
                lifecycle="deleted",
            )
            extra_obligation = deepcopy(context["obligations"][0])
            extra_obligation["rowId"] = extra_row
            extra = self._build_result(
                context,
                extra_obligation,
                extra_head,
                obligation_hash="f" * 64,
            )
            self._result_reference(context, extra_row).create(extra)
            self._assert_failure_without_write(context)

        with self.subTest(case="missing-named-row-head"):
            context = self._seed(1)
            context["store"].data.pop(
                self._row_head_reference(context, context["rows"][0]).path
            )
            self._assert_failure_without_write(context)

        with self.subTest(case="swapped-named-row-head"):
            context = self._seed(2)
            crossed = self._build_result(
                context,
                context["obligations"][0],
                context["rowHeads"][0],
                observed_row_head_hash=context["rowHeads"][1]["headHash"],
            )
            context["store"].data[
                self._result_reference(context, context["rows"][0]).path
            ] = crossed
            self._assert_failure_without_write(context)

    def test_completion_never_reads_unbounded_history(self):
        self._method()
        context = self._seed(33)
        context["store"].events.clear()

        result, queries = self._certify_with_queries(context)

        self._assert_result(result, "page_certified")
        self._assert_query_shapes(context, queries, cursor=None)
        self.assertEqual(32, len(result["obligations"]))
        row_head_gets = [
            event[1]
            for event in context["store"].events
            if event[0] == "get" and "/rowAuthorityHeads/" in event[1]
        ]
        self.assertEqual(
            [
                self._row_head_reference(context, row_id).path
                for row_id in context["rows"][:32]
            ],
            row_head_gets,
        )
        self.assertNotIn(
            self._row_head_reference(context, context["rows"][32]).path,
            row_head_gets,
        )
        self._assert_one_head_write(context, result["fanoutHead"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
