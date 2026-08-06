"""RED contracts for atomic one-row contact fan-out application."""

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


class ContactFanoutApplyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.discovery_type = importlib.import_module(
            "tests.test_row_authority_contact_fanout_discovery"
        ).ContactFanoutDiscoveryTests
        cls.discovery_type.setUpClass()
        cls.completion_type = importlib.import_module(
            "tests.test_row_authority_contact_fanout_completion"
        ).ContactFanoutCompletionTests
        cls.completion_type.setUpClass()
        cls.history_type = importlib.import_module(
            "tests.test_row_authority_contact_compliance"
        ).ReleaseAwareRowHistoryTests
        cls.history_type.setUpClass()
        cls.module = cls.discovery_type.module

    def setUp(self):
        self.discovery = self.discovery_type(methodName="runTest")
        self.discovery.setUp()
        self.completion = self.completion_type(methodName="runTest")
        self.completion.setUp()
        self.history = self.history_type(methodName="runTest")
        self.history.setUp()
        self.processed_at = "2026-08-04T12:05:45.000000Z"

    def _method(self):
        method = getattr(
            self.module.RowAuthorityStore,
            "process_contact_fanout_obligation",
            None,
        )
        self.assertTrue(
            callable(method),
            "RowAuthorityStore.process_contact_fanout_obligation is missing",
        )
        signature = inspect.signature(method)
        self.assertEqual(
            [
                "self",
                "verified_user_id",
                "fanout_id",
                "row_id",
                "expected_fanout_head",
                "lease_owner_hash",
                "processed_at",
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

    def _documents(self, context, collection):
        return context["transition"]._documents(
            context["store"],
            collection,
        )

    def _seed_apply(self, *, lifecycle="active", prior_owner=None):
        row_id = _row_id(1)
        context = self.discovery._seed([row_id])
        fixture = context["transition"].fixture
        _identity, row_head = fixture._seed_row(
            context["store"],
            row_id,
            lifecycle=lifecycle,
        )
        prior = None
        if prior_owner == "terminal":
            claim, generation, claimed_head = fixture._install_owner(
                context["store"],
                row_id,
                owner_kind="terminal",
            )
            settlement, row_head = fixture._settle_terminal_owner(
                context["store"],
                claim,
                generation,
                claimed_head,
            )
            prior = {
                "claim": claim,
                "generation": generation,
                "settlement": settlement,
            }
        elif prior_owner == "human_decision":
            fixture._seed_thread_binding(context["store"], [row_id])
            (
                action,
                claim,
                generation,
                settlement,
                row_head,
            ) = fixture._install_settled_human_owner(
                context["store"],
                row_id,
            )
            prior = {
                "action": action,
                "claim": claim,
                "generation": generation,
                "settlement": settlement,
            }
        elif prior_owner is not None:
            raise AssertionError(f"unsupported prior owner fixture: {prior_owner}")

        obligation = self.discovery._obligation(
            context,
            context["edges"][0],
        )
        applying = self.discovery._fanout(
            context["fanout"],
            state_revision=context["fanout"]["stateRevision"] + 1,
            state="applying",
            discovery_cursor_row_id=None,
            cursor_processed_count=0,
            obligation_count=1,
            result_count=0,
            updated_at=self.discovery.discovered_at,
        )
        self.discovery._store_fanout(context, applying)
        context.update(
            {
                "rowId": row_id,
                "rowHead": row_head,
                "prior": prior,
                "obligation": obligation,
                "fanout": applying,
            }
        )
        context["store"].events.clear()
        return context

    def _seed_restored_history_apply(self):
        context = self._seed_apply(prior_owner="human_decision")
        human = context["prior"]
        historical = self.completion._install_contact_lineage(
            context,
            context["rowHead"],
            materialize_head=True,
            generation_number=2,
            predecessor_settlement_hash=human["settlement"][
                "settlementHash"
            ],
            first_fencing_token=2,
            claimed_at="2026-08-04T12:05:00.000000Z",
            settled_at="2026-08-04T12:05:20.000000Z",
        )
        released_contact_head = (
            context["transition"]._install_release_after_image(
                context["store"],
                released_at="2026-08-04T12:05:30.000000Z",
            )
        )
        release_fanout_id = released_contact_head["activeFanoutId"]
        _release_result, restored_head = self.history._release_to(
            context["store"],
            released_generation=historical["generation"],
            released_settlement=historical["settlement"],
            settled_head=historical["settledHead"],
            fanout_id=release_fanout_id,
            restored_generation=human["generation"],
            restored_settlement=human["settlement"],
            released_at="2026-08-04T12:05:40.000000Z",
            cycle=201,
            row_id=context["rowId"],
        )

        newer_bundle, _newer_link = context["transition"]._seed_bundle(
            context["store"],
            "source-contact-fanout-restored-history",
            exact_hash=context["settlement"]["exactIdentityHash"],
        )
        newer = context["transition"]._record(
            context["store"],
            newer_bundle,
            requested_at="2026-08-04T12:06:00.000000Z",
        )
        self.assertEqual("created", newer["disposition"])
        context.update(
            {
                "settlement": newer["settlement"],
                "receipt": newer["transitionRequest"],
                "contactHead": newer["head"],
                "fanout": newer["fanoutHead"],
                "rowHead": restored_head,
                "historical": historical,
            }
        )

        leased = self.discovery._fanout(
            context["fanout"],
            state_revision=context["fanout"]["stateRevision"] + 1,
            lease_owner_hash=self.discovery.lease_owner,
            lease_until=self.discovery.lease_until,
            fencing_token=context["fanout"]["fencingToken"] + 1,
            updated_at="2026-08-04T12:06:10.000000Z",
        )
        self.discovery._store_fanout(context, leased)
        obligation = self.discovery._obligation(
            context,
            context["edges"][0],
            created_at="2026-08-04T12:06:30.000000Z",
        )
        applying = self.discovery._fanout(
            context["fanout"],
            state_revision=context["fanout"]["stateRevision"] + 1,
            state="applying",
            obligation_count=1,
            result_count=0,
            updated_at="2026-08-04T12:06:30.000000Z",
        )
        self.discovery._store_fanout(context, applying)
        context.update({"obligation": obligation, "fanout": applying})
        context["store"].events.clear()
        return context

    def _process(
        self,
        context,
        *,
        expected_fanout_head=None,
        lease_owner_hash=None,
        processed_at=None,
        executor=None,
    ):
        self._method()
        authority = context["transition"]._authority(
            context["store"],
            executor=executor,
        )
        return authority.process_contact_fanout_obligation(
            verified_user_id=context["transition"].fixture.user_id,
            fanout_id=context["fanout"]["fanoutId"],
            row_id=context["rowId"],
            expected_fanout_head=(
                expected_fanout_head or context["fanout"]
            ),
            lease_owner_hash=(
                lease_owner_hash or self.discovery.lease_owner
            ),
            processed_at=processed_at or self.processed_at,
        )

    def _process_with_queries(self, context):
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
            result = self._process(context)
        return result, observed

    def _result(self, context):
        result_id = f"{context['fanout']['fanoutId']}--{context['rowId']}"
        return self._documents(
            context,
            "contactOptOutFanoutResults",
        )[result_id]

    def _claim_for_result(self, context, result):
        return self._documents(context, "rowClaimSets")[
            result["claimRequestId"]
        ]

    def _add_thread_evidence(
        self,
        context,
        *,
        thread_id,
        binding_at,
        evidence_at,
    ):
        row_id = context["rowId"]
        binding = self.module.build_thread_row_binding_document(
            user_scope_hash=context["scope"],
            thread_id=thread_id,
            client_id="client-1",
            row_ids=[row_id],
            primary_row_id=row_id,
            created_at=binding_at,
        )
        self.discovery._reference(
            context,
            "threadRowBindings",
            thread_id,
        ).create(binding)
        reverse = self.module.build_row_thread_binding_documents(
            thread_binding_document=binding
        )[0]
        self.discovery._reference(
            context,
            "rowThreadBindings",
            reverse["edgeId"],
        ).create(reverse)
        evidence = self.module.build_contact_row_binding_evidence_document(
            user_scope_hash=context["scope"],
            edge_id=context["edges"][0]["edgeId"],
            thread_id=thread_id,
            thread_binding_hash=binding["bindingHash"],
            exact_identity_hash=context["settlement"]["exactIdentityHash"],
            created_at=evidence_at,
        )
        self.discovery._reference(
            context,
            "contactRowBindingEvidence",
            evidence["evidenceId"],
        ).create(evidence)
        context["store"].events.clear()
        return binding, reverse, evidence

    def _assert_fanout_incremented_once(
        self,
        context,
        before_fanout,
        *,
        updated_at=None,
    ):
        fanout = self._documents(
            context,
            "contactOptOutFanoutHeads",
        )[before_fanout["fanoutId"]]
        expected = self.discovery._fanout(
            before_fanout,
            state_revision=before_fanout["stateRevision"] + 1,
            result_count=before_fanout["resultCount"] + 1,
            updated_at=updated_at or self.processed_at,
        )
        self.assertEqual(expected, fanout)
        return fanout

    def test_apply_atomically_creates_claim_generation_settlement_head_and_result(self):
        self._method()
        for prior_owner in (None, "terminal", "human_decision"):
            with self.subTest(prior_owner=prior_owner or "clear"):
                context = self._seed_apply(prior_owner=prior_owner)
                store = context["store"]
                row_id = context["rowId"]
                before_head = deepcopy(context["rowHead"])
                before_fanout = deepcopy(context["fanout"])
                before_counts = {
                    collection: len(self._documents(context, collection))
                    for collection in (
                        "rowClaimSets",
                        "rowOwnerGenerations",
                        "rowOwnerSettlements",
                        "contactOptOutFanoutResults",
                    )
                }

                processed = self._process(context)

                result_id = f"{before_fanout['fanoutId']}--{row_id}"
                result = self._documents(
                    context,
                    "contactOptOutFanoutResults",
                )[result_id]
                claim = self._documents(context, "rowClaimSets")[
                    result["claimRequestId"]
                ]
                generation_id = f"{row_id}--{result['rowGeneration']}"
                generation = self._documents(
                    context,
                    "rowOwnerGenerations",
                )[generation_id]
                settlement = self._documents(
                    context,
                    "rowOwnerSettlements",
                )[generation_id]
                head = self._documents(context, "rowAuthorityHeads")[row_id]
                fanout = self._documents(
                    context,
                    "contactOptOutFanoutHeads",
                )[before_fanout["fanoutId"]]

                self.assertEqual(
                    result,
                    self.module.validate_contact_fanout_result_document(
                        document=result
                    ),
                )
                self.assertEqual(
                    claim,
                    self.module.validate_claim_set_document(document=claim),
                )
                self.assertEqual(
                    generation,
                    self.module.validate_owner_generation_document(
                        document=generation
                    ),
                )
                self.assertEqual(
                    settlement,
                    self.module.validate_owner_settlement_document(
                        document=settlement
                    ),
                )
                self.assertEqual(
                    head,
                    self.module.validate_row_authority_head(document=head),
                )
                self.assertEqual(
                    fanout,
                    self.module.validate_contact_fanout_head_document(
                        document=fanout
                    ),
                )

                self.assertEqual("applied", result["disposition"])
                self.assertEqual("claim_accepted", result["reasonCode"])
                self.assertEqual(
                    before_head["headHash"],
                    result["observedRowHeadHash"],
                )
                self.assertEqual(claim["requestId"], result["claimRequestId"])
                self.assertEqual(claim["claimSetHash"], result["claimSetHash"])
                self.assertEqual(
                    generation["generation"],
                    result["rowGeneration"],
                )
                self.assertEqual(
                    settlement["settlementHash"],
                    result["rowSettlementHash"],
                )
                self.assertTrue(
                    all(
                        result[field] is None
                        for field in (
                            "releasedRowGeneration",
                            "releasedRowSettlementHash",
                            "restoredEffectiveGeneration",
                            "restoredEffectiveSettlementHash",
                        )
                    )
                )

                self.assertEqual("contact_fanout", claim["authorityOrigin"])
                self.assertEqual(
                    context["settlement"]["authorityLink"],
                    claim["authorityLink"],
                )
                self.assertEqual(before_fanout["fanoutId"], claim["fanoutId"])
                self.assertEqual(
                    [{"rowId": row_id, "role": "primary"}],
                    claim["rowBindings"],
                )
                self.assertEqual(1, claim["bindingCount"])
                self.assertEqual(row_id, claim["primaryRowId"])
                self.assertEqual("contact_optout", claim["ownerKind"])
                self.assertEqual(
                    context["settlement"]["contactSettlementHash"],
                    claim["payloadHash"],
                )
                self.assertEqual(
                    f"{before_fanout['fanoutId']}--{row_id}",
                    claim["workKey"],
                )
                self.assertEqual("accepted", claim["outcome"])
                self.assertEqual(3, claim["plannedWrites"])
                self.assertEqual(
                    [
                        {
                            "rowId": row_id,
                            "decision": "accepted",
                            "plannedGeneration": generation["generation"],
                            "winnerGenerationHash": None,
                            "winnerSettlementHash": None,
                        }
                    ],
                    claim["rowDecisions"],
                )

                self.assertEqual(claim["requestId"], generation["requestId"])
                self.assertEqual(claim["claimSetHash"], generation["claimSetHash"])
                self.assertEqual(
                    before_head["headHash"],
                    generation["predecessorHeadHash"],
                )
                self.assertEqual(
                    before_head["effectiveSettlementHash"],
                    generation["predecessorSettlementHash"],
                )
                self.assertEqual("contact_optout", generation["ownerKind"])
                self.assertEqual(
                    generation["generationHash"],
                    settlement["generationHash"],
                )
                self.assertEqual("contact_optout", settlement["outcome"])
                self.assertEqual(
                    before_head["effectiveSettlementHash"],
                    settlement["supersededEffectiveSettlementHash"],
                )
                self.assertEqual("settled", head["state"])
                self.assertEqual("contact_optout", head["effectiveOwnerKind"])
                self.assertEqual(
                    generation["generationHash"],
                    head["effectiveOwnerGenerationHash"],
                )
                self.assertEqual(
                    settlement["settlementHash"],
                    head["effectiveSettlementHash"],
                )

                expected_fanout = self.discovery._fanout(
                    before_fanout,
                    state_revision=before_fanout["stateRevision"] + 1,
                    result_count=before_fanout["resultCount"] + 1,
                    updated_at=self.processed_at,
                )
                self.assertEqual(expected_fanout, fanout)
                self.assertEqual("applied", processed["disposition"])
                self.assertEqual(fanout, processed["fanoutHead"])
                self.assertEqual(result, processed["result"])

                after_counts = {
                    collection: len(self._documents(context, collection))
                    for collection in before_counts
                }
                self.assertEqual(
                    {
                        collection: count + 1
                        for collection, count in before_counts.items()
                    },
                    after_counts,
                )
                self.assertEqual(6, len(self._writes(store)))
                self.assertIn(("commit_applied", 6), store.events)

    def test_apply_dominance_creates_claim_set_and_result_without_generation(self):
        self._method()
        context = self._seed_apply()
        installed = self.completion._install_contact_lineage(
            context,
            context["rowHead"],
            materialize_head=True,
            canonical_hash="c" * 64,
            contact_settlement_hash="e" * 64,
            fanout_id="f" * 64,
        )
        context["rowHead"] = installed["settledHead"]
        before_head = deepcopy(context["rowHead"])
        before_fanout = deepcopy(context["fanout"])
        before_generations = self._documents(
            context,
            "rowOwnerGenerations",
        )
        before_settlements = self._documents(
            context,
            "rowOwnerSettlements",
        )
        before_claim_count = len(self._documents(context, "rowClaimSets"))
        context["store"].events.clear()

        processed = self._process(context)

        result = self._result(context)
        claim = self._claim_for_result(context, result)
        current_head = self._documents(
            context,
            "rowAuthorityHeads",
        )[context["rowId"]]
        self.assertEqual(
            result,
            self.module.validate_contact_fanout_result_document(
                document=result
            ),
        )
        self.assertEqual(
            claim,
            self.module.validate_claim_set_document(document=claim),
        )
        self.assertEqual("dominated", result["disposition"])
        self.assertEqual("claim_dominated", result["reasonCode"])
        self.assertEqual(before_head["headHash"], result["observedRowHeadHash"])
        self.assertEqual(claim["requestId"], result["claimRequestId"])
        self.assertEqual(claim["claimSetHash"], result["claimSetHash"])
        self.assertTrue(
            all(
                result[field] is None
                for field in (
                    "rowGeneration",
                    "rowSettlementHash",
                    "releasedRowGeneration",
                    "releasedRowSettlementHash",
                    "restoredEffectiveGeneration",
                    "restoredEffectiveSettlementHash",
                )
            )
        )
        self.assertEqual("contact_fanout", claim["authorityOrigin"])
        self.assertEqual("dominated", claim["outcome"])
        self.assertEqual(1, claim["plannedWrites"])
        self.assertEqual(
            [{"rowId": context["rowId"], "role": "primary"}],
            claim["rowBindings"],
        )
        self.assertEqual(
            [
                {
                    "rowId": context["rowId"],
                    "decision": "dominated",
                    "plannedGeneration": None,
                    "winnerGenerationHash": installed["generation"][
                        "generationHash"
                    ],
                    "winnerSettlementHash": installed["settlement"][
                        "settlementHash"
                    ],
                }
            ],
            claim["rowDecisions"],
        )
        self.assertEqual(before_head, current_head)
        self.assertEqual(
            before_generations,
            self._documents(context, "rowOwnerGenerations"),
        )
        self.assertEqual(
            before_settlements,
            self._documents(context, "rowOwnerSettlements"),
        )
        self.assertEqual(
            before_claim_count + 1,
            len(self._documents(context, "rowClaimSets")),
        )
        fanout = self._assert_fanout_incremented_once(context, before_fanout)
        self.assertEqual("dominated", processed["disposition"])
        self.assertEqual(result, processed["result"])
        self.assertEqual(fanout, processed["fanoutHead"])
        self.assertEqual(3, len(self._writes(context["store"])))
        self.assertIn(("commit_applied", 3), context["store"].events)

    def test_apply_deleted_row_records_noop_without_claim_or_generation(self):
        self._method()
        context = self._seed_apply(lifecycle="deleted")
        before_head = deepcopy(context["rowHead"])
        before_fanout = deepcopy(context["fanout"])
        before_documents = {
            collection: self._documents(context, collection)
            for collection in (
                "rowClaimSets",
                "rowOwnerGenerations",
                "rowOwnerSettlements",
            )
        }

        processed = self._process(context)

        result = self._result(context)
        self.assertEqual(
            result,
            self.module.validate_contact_fanout_result_document(
                document=result
            ),
        )
        self.assertEqual("noop", result["disposition"])
        self.assertEqual("row_deleted", result["reasonCode"])
        self.assertEqual(before_head["headHash"], result["observedRowHeadHash"])
        self.assertTrue(
            all(
                result[field] is None
                for field in (
                    "claimRequestId",
                    "claimSetHash",
                    "rowGeneration",
                    "rowSettlementHash",
                    "releasedRowGeneration",
                    "releasedRowSettlementHash",
                    "restoredEffectiveGeneration",
                    "restoredEffectiveSettlementHash",
                )
            )
        )
        self.assertEqual(
            before_head,
            self._documents(context, "rowAuthorityHeads")[context["rowId"]],
        )
        for collection, documents in before_documents.items():
            self.assertEqual(documents, self._documents(context, collection))
        fanout = self._assert_fanout_incremented_once(context, before_fanout)
        self.assertEqual("noop", processed["disposition"])
        self.assertEqual(result, processed["result"])
        self.assertEqual(fanout, processed["fanoutHead"])
        self.assertEqual(2, len(self._writes(context["store"])))
        self.assertIn(("commit_applied", 2), context["store"].events)

    def test_apply_uses_v2_contact_settlement_link_not_thread_authority(self):
        self._method()
        context = self._seed_apply()
        fixture = context["transition"].fixture
        thread_binding = fixture._seed_thread_binding(
            context["store"],
            [context["rowId"]],
        )
        decoy_bundle = fixture._seed_b1_bundle(
            context["store"],
            owner_kind="terminal",
            source_id="decoy-thread-authority",
        )
        decoy_link = self.module.build_b1_authority_link(
            user_scope_hash=context["scope"],
            source_identity_document=decoy_bundle["identity"],
            source_classification_document=decoy_bundle["classification"],
            source_owner_document=decoy_bundle["owner"],
            source_ledger_document=decoy_bundle["ledger"],
            work_key=decoy_bundle["work_key"],
        )
        context["store"].events.clear()

        self._process(context)

        result = self._result(context)
        claim = self._claim_for_result(context, result)
        contact_link = context["settlement"]["authorityLink"]
        self.assertEqual("contact_fanout", claim["authorityOrigin"])
        self.assertEqual(contact_link, claim["authorityLink"])
        self.assertEqual(
            self.module._B1_LINK_V2_KEYS,
            frozenset(claim["authorityLink"]),
        )
        self.assertEqual(
            contact_link["authorityLinkHash"],
            claim["authorityLinkHash"],
        )
        self.assertEqual(
            context["settlement"]["contactSettlementHash"],
            claim["payloadHash"],
        )
        self.assertEqual(context["fanout"]["fanoutId"], claim["fanoutId"])
        self.assertNotEqual(decoy_link, claim["authorityLink"])
        self.assertNotEqual(decoy_link["ownerKey"], claim["ownerKey"])
        self.assertEqual(
            [{"rowId": context["rowId"], "role": "primary"}],
            claim["rowBindings"],
        )
        self.assertEqual(
            thread_binding["rowBindingsHash"],
            claim["rowBindingsHash"],
        )

    def test_multiple_thread_evidence_roots_produce_one_canonical_row_claim(self):
        self._method()
        one_root = self._seed_apply()
        two_roots = self._seed_apply()
        self._add_thread_evidence(
            one_root,
            thread_id="thread-zeta",
            binding_at="2026-08-04T12:00:01.000001Z",
            evidence_at="2026-08-04T12:01:30.000001Z",
        )
        self._add_thread_evidence(
            two_roots,
            thread_id="thread-alpha",
            binding_at="2026-08-04T12:00:01.000002Z",
            evidence_at="2026-08-04T12:01:30.000002Z",
        )
        self._add_thread_evidence(
            two_roots,
            thread_id="thread-zeta",
            binding_at="2026-08-04T12:00:01.000001Z",
            evidence_at="2026-08-04T12:01:30.000001Z",
        )

        self._process(one_root)
        self._process(two_roots)

        one_claim = self._claim_for_result(one_root, self._result(one_root))
        two_claim = self._claim_for_result(two_roots, self._result(two_roots))
        self.assertEqual(
            [{"rowId": one_root["rowId"], "role": "primary"}],
            one_claim["rowBindings"],
        )
        self.assertEqual(one_claim["rowBindings"], two_claim["rowBindings"])
        self.assertEqual(
            one_claim["rowBindingsHash"],
            two_claim["rowBindingsHash"],
        )
        self.assertEqual(one_claim["requestId"], two_claim["requestId"])
        self.assertEqual(one_claim["claimSetHash"], two_claim["claimSetHash"])
        self.assertEqual(one_claim, two_claim)

    def test_apply_result_hashes_exact_row_head_before_image(self):
        self._method()
        context = self._seed_apply()
        original_head = deepcopy(context["rowHead"])
        advanced_head = deepcopy(original_head)
        advanced_head.update(
            {
                "stateRevision": original_head["stateRevision"] + 1,
                "currentLocationRevision": (
                    original_head["currentLocationRevision"] + 1
                ),
                "currentLocationHash": "4" * 64,
                "updatedAt": "2026-08-04T12:05:40.000000Z",
            }
        )
        advanced_head = context["transition"].fixture._rehash_head(
            advanced_head
        )
        context["transition"].fixture._row_references(
            context["store"],
            context["rowId"],
        )[1].set(advanced_head, merge=False)
        context["rowHead"] = advanced_head
        context["store"].events.clear()

        self._process(context)

        result = self._result(context)
        claim = self._claim_for_result(context, result)
        generation_id = f"{context['rowId']}--{result['rowGeneration']}"
        generation = self._documents(
            context,
            "rowOwnerGenerations",
        )[generation_id]
        after_head = self._documents(
            context,
            "rowAuthorityHeads",
        )[context["rowId"]]
        self.assertNotEqual(original_head["headHash"], advanced_head["headHash"])
        self.assertEqual(
            advanced_head["headHash"],
            result["observedRowHeadHash"],
        )
        self.assertEqual(
            advanced_head["headHash"],
            generation["predecessorHeadHash"],
        )
        self.assertNotEqual(result["observedRowHeadHash"], after_head["headHash"])
        rebuilt = self.module.build_contact_fanout_result_document(
            user_scope_hash=context["scope"],
            fanout_id=context["fanout"]["fanoutId"],
            row_id=context["rowId"],
            obligation_hash=context["obligation"][
                "contactFanoutObligationHash"
            ],
            outcome="apply",
            disposition="applied",
            reason_code="claim_accepted",
            observed_row_head_hash=advanced_head["headHash"],
            claim_request_id=claim["requestId"],
            claim_set_hash=claim["claimSetHash"],
            row_generation=generation["generation"],
            row_settlement_hash=result["rowSettlementHash"],
            released_row_generation=None,
            released_row_settlement_hash=None,
            restored_effective_generation=None,
            restored_effective_settlement_hash=None,
            created_at=self.processed_at,
        )
        self.assertEqual(rebuilt, result)

    def test_apply_after_release_restore_allocates_above_history_atomically(self):
        self._method()
        context = self._seed_restored_history_apply()
        restored_head = deepcopy(context["rowHead"])
        human = context["prior"]
        historical = context["historical"]
        before_fanout = deepcopy(context["fanout"])

        processed = self._process(
            context,
            processed_at="2026-08-04T12:06:45.000000Z",
        )

        result = self._result(context)
        claim = self._claim_for_result(context, result)
        generation_id = f"{context['rowId']}--{result['rowGeneration']}"
        generation = self._documents(
            context,
            "rowOwnerGenerations",
        )[generation_id]
        settlement = self._documents(
            context,
            "rowOwnerSettlements",
        )[generation_id]
        head = self._documents(
            context,
            "rowAuthorityHeads",
        )[context["rowId"]]

        self.assertEqual("applied", processed["disposition"])
        self.assertEqual("applied", result["disposition"])
        self.assertEqual("claim_accepted", result["reasonCode"])
        self.assertEqual(restored_head["headHash"], result["observedRowHeadHash"])
        self.assertEqual(claim["requestId"], result["claimRequestId"])
        self.assertEqual(3, result["rowGeneration"])
        self.assertEqual(3, generation["generation"])
        self.assertEqual(3, generation["firstFencingToken"])
        self.assertGreater(
            generation["generation"],
            historical["generation"]["generation"],
        )
        self.assertGreater(
            generation["firstFencingToken"],
            historical["settlement"]["fencingToken"],
        )
        self.assertEqual(
            restored_head["headHash"],
            generation["predecessorHeadHash"],
        )
        self.assertEqual(
            human["settlement"]["settlementHash"],
            generation["predecessorSettlementHash"],
        )
        self.assertEqual(
            human["settlement"]["settlementHash"],
            settlement["supersededEffectiveSettlementHash"],
        )
        self.assertEqual(settlement["settlementHash"], result["rowSettlementHash"])
        self.assertEqual(settlement["settlementHash"], head["effectiveSettlementHash"])
        self.assertEqual(settlement["settlementHash"], head["latestSettlementHash"])
        fanout = self._assert_fanout_incremented_once(
            context,
            before_fanout,
            updated_at="2026-08-04T12:06:45.000000Z",
        )
        self.assertEqual(result, processed["result"])
        self.assertEqual(fanout, processed["fanoutHead"])
        self.assertEqual(6, len(self._writes(context["store"])))
        self.assertIn(("commit_applied", 6), context["store"].events)

    def test_apply_rejects_invalid_worker_preconditions_without_writes(self):
        self._method()
        cases = (
            ("wrong-lease-owner", {"lease_owner_hash": "e" * 64}),
            (
                "expired-lease",
                {"processed_at": self.discovery.lease_until},
            ),
            ("wrong-state", {}),
        )
        for case, overrides in cases:
            with self.subTest(case=case):
                context = self._seed_apply()
                if case == "wrong-state":
                    discovering = self.discovery._fanout(
                        context["fanout"],
                        state_revision=context["fanout"]["stateRevision"] + 1,
                        state="discovering",
                        updated_at="2026-08-04T12:05:40.000000Z",
                    )
                    self.discovery._store_fanout(context, discovering)
                before = deepcopy(context["store"].data)
                context["store"].events.clear()

                with self.assertRaises(self.module.RowAuthorityError):
                    self._process(context, **overrides)

                self.assertEqual(before, context["store"].data)
                self.assertEqual([], self._writes(context["store"]))

    def test_apply_loses_safely_to_contact_head_or_fence_advance(self):
        self._method()
        with self.subTest(race="contact-head-advance"):
            context = self._seed_apply()
            stale_expected = deepcopy(context["fanout"])
            context["transition"]._install_release_after_image(
                context["store"],
                released_at="2026-08-04T12:05:40.000000Z",
            )
            before = deepcopy(context["store"].data)
            context["store"].events.clear()
            with self.assertRaises(self.module.RowAuthorityError):
                self._process(
                    context,
                    expected_fanout_head=stale_expected,
                )
            self.assertEqual(before, context["store"].data)
            self.assertEqual([], self._writes(context["store"]))

        with self.subTest(race="fanout-fence-advance"):
            context = self._seed_apply()
            stale_expected = deepcopy(context["fanout"])
            advanced = self.discovery._fanout(
                stale_expected,
                state_revision=stale_expected["stateRevision"] + 1,
                fencing_token=stale_expected["fencingToken"] + 1,
                lease_until="2026-08-04T12:11:00.000000Z",
                updated_at="2026-08-04T12:05:40.000000Z",
            )
            self.discovery._store_fanout(context, advanced)
            before = deepcopy(context["store"].data)
            with self.assertRaises(self.module.RowAuthorityError):
                self._process(
                    context,
                    expected_fanout_head=stale_expected,
                )
            self.assertEqual(before, context["store"].data)
            self.assertEqual([], self._writes(context["store"]))

    def test_apply_retry_preapply_and_apply_then_raise_are_exact(self):
        self._method()
        with self.subTest(commit="exact-retry"):
            context = self._seed_apply()
            first = self._process(context)
            committed = deepcopy(context["store"].data)
            context["store"].events.clear()

            replay = self._process(context)

            self.assertEqual(first, replay)
            self.assertEqual(committed, context["store"].data)
            self.assertEqual([], self._writes(context["store"]))
            self.assertIn(("commit_applied", 0), context["store"].events)

        with self.subTest(commit="preapply-failure"):
            context = self._seed_apply()
            before = deepcopy(context["store"].data)
            context["store"].fail_next_commit = RuntimeError(
                "configured apply preapply failure"
            )
            with self.assertRaises(self.module.RowAuthorityRetryable):
                self._process(context)
            self.assertEqual(before, context["store"].data)
            self.assertEqual([], self._writes(context["store"]))
            self.assertIn(
                ("commit_failed_before_apply",),
                context["store"].events,
            )

        with self.subTest(commit="apply-then-raise"):
            context = self._seed_apply()
            context["store"].apply_then_raise_next_commit = RuntimeError(
                "unknown apply commit outcome"
            )

            applied = self._process(context)

            result = self._result(context)
            fanout = self._documents(
                context,
                "contactOptOutFanoutHeads",
            )[context["fanout"]["fanoutId"]]
            self.assertEqual("applied", applied["disposition"])
            self.assertEqual(result, applied["result"])
            self.assertEqual(fanout, applied["fanoutHead"])
            self.assertEqual(6, len(self._writes(context["store"])))
            self.assertIn(("commit_applied", 6), context["store"].events)
            failure_index = context["store"].events.index(
                ("commit_raised_after_apply",)
            )
            self.assertTrue(
                any(
                    event[0] == "get"
                    for event in context["store"].events[failure_index + 1 :]
                )
            )

    def test_apply_partial_commit_is_ambiguous_and_same_obligation_race_converges(
        self,
    ):
        self._method()
        with self.subTest(commit="partial-readback"):
            context = self._seed_apply()
            before_fanout = deepcopy(context["fanout"])

            def partial_executor(transaction, callback):
                transaction._begin()
                callback(transaction)
                operation, reference, payload, merge = transaction._operations[0]
                transaction._rollback()
                self.assertEqual(("create", False), (operation, merge))
                reference.create(payload)
                raise RuntimeError("partial fanout apply")

            with self.assertRaises(self.module.RowAuthorityAmbiguous):
                self._process(context, executor=partial_executor)

            self.assertEqual(1, len(self._writes(context["store"])))
            self.assertEqual(
                before_fanout,
                self._documents(
                    context,
                    "contactOptOutFanoutHeads",
                )[before_fanout["fanoutId"]],
            )
            self.assertEqual(
                {},
                self._documents(context, "contactOptOutFanoutResults"),
            )
            self.assertIn(("commit_applied", 1), context["store"].events)

        with self.subTest(commit="same-obligation-race"):
            context = self._seed_apply()
            context["store"].before_commit_barrier = Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(self._process, context)
                    for _worker in range(2)
                ]
                results = [future.result(timeout=10) for future in futures]

            self.assertEqual(results[0], results[1])
            self.assertEqual("applied", results[0]["disposition"])
            self.assertEqual(self._result(context), results[0]["result"])
            self.assertEqual(6, len(self._writes(context["store"])))
            self.assertEqual(
                1,
                context["store"].events.count(("commit_applied", 6)),
            )
            self.assertEqual(
                1,
                context["store"].events.count(("commit_applied", 0)),
            )
            self.assertEqual(
                1,
                sum(
                    event[0].startswith("commit_aborted_stale")
                    for event in context["store"].events
                ),
            )

    def test_apply_scans_ordered_first_missing_and_only_advances_full_pages(self):
        self._method()
        with self.subTest(scan="arbitrary-later-row"):
            context = self.completion._seed(
                2,
                result_count=0,
                lifecycle="deleted",
            )
            context["rowId"] = context["rows"][1]
            before = deepcopy(context["store"].data)
            context["store"].events.clear()

            with self.assertRaises(self.module.RowAuthorityConflict):
                self._process_with_queries(context)

            self.assertEqual(before, context["store"].data)
            self.assertEqual([], self._writes(context["store"]))
            row_head_paths = {
                self.completion._row_head_reference(context, row_id).path
                for row_id in context["rows"]
            }
            self.assertTrue(
                row_head_paths.isdisjoint(
                    {
                        event[1]
                        for event in context["store"].events
                        if event[0] == "get"
                    }
                )
            )

        with self.subTest(scan="32-existing-plus-sentinel"):
            context = self.completion._seed(
                33,
                result_count=32,
                lifecycle="deleted",
            )
            context["rowId"] = context["rows"][32]
            before_fanout = deepcopy(context["fanout"])
            before_result_documents = self._documents(
                context,
                "contactOptOutFanoutResults",
            )
            context["store"].events.clear()

            processed, queries = self._process_with_queries(context)

            obligation_queries = [
                query
                for query in queries
                if query["collection"] == "contactOptOutFanoutObligations"
            ]
            self.assertEqual(1, len(obligation_queries))
            obligation_query = obligation_queries[0]
            self.assertEqual(
                (("fanoutId", "==", before_fanout["fanoutId"]),),
                obligation_query["filters"],
            )
            self.assertEqual(("rowId",), obligation_query["ordering"])
            self.assertEqual(("ASCENDING",), obligation_query["directions"])
            self.assertEqual(33, obligation_query["limit"])
            self.assertIsNone(obligation_query["cursor"])
            self.assertEqual(
                context["rows"],
                [
                    document["rowId"]
                    for document in obligation_query["documents"]
                ],
            )
            expected_fanout = self.discovery._fanout(
                before_fanout,
                state_revision=before_fanout["stateRevision"] + 1,
                discovery_cursor_row_id=context["rows"][31],
                cursor_processed_count=32,
                updated_at=self.processed_at,
            )
            self.assertEqual("cursor_advanced", processed["disposition"])
            self.assertEqual(expected_fanout, processed["fanoutHead"])
            self.assertIsNone(processed["result"])
            self.assertEqual(
                expected_fanout,
                self._documents(
                    context,
                    "contactOptOutFanoutHeads",
                )[before_fanout["fanoutId"]],
            )
            self.assertEqual(
                before_result_documents,
                self._documents(context, "contactOptOutFanoutResults"),
            )
            self.assertEqual(1, len(self._writes(context["store"])))
            self.assertIn(("commit_applied", 1), context["store"].events)
            sentinel_head_path = self.completion._row_head_reference(
                context,
                context["rows"][32],
            ).path
            self.assertNotIn(
                sentinel_head_path,
                [
                    event[1]
                    for event in context["store"].events
                    if event[0] == "get"
                ],
            )

    def test_apply_pending_owner_atomically_settles_predecessor_in_seven_writes(
        self,
    ):
        self._method()
        context = self._seed_apply()
        fixture = context["transition"].fixture
        prior_claim, prior_generation, pending_head = fixture._install_owner(
            context["store"],
            context["rowId"],
            owner_kind="terminal",
        )
        context["rowHead"] = pending_head
        before_fanout = deepcopy(context["fanout"])
        context["store"].events.clear()

        processed = self._process(context)

        result = self._result(context)
        claim = self._claim_for_result(context, result)
        generation_id = f"{context['rowId']}--{result['rowGeneration']}"
        generation = self._documents(
            context,
            "rowOwnerGenerations",
        )[generation_id]
        settlements = self._documents(context, "rowOwnerSettlements")
        predecessor = settlements[
            f"{context['rowId']}--{prior_generation['generation']}"
        ]
        contact_settlement = settlements[generation_id]
        head = self._documents(
            context,
            "rowAuthorityHeads",
        )[context["rowId"]]

        self.assertEqual("applied", processed["disposition"])
        self.assertEqual(result, processed["result"])
        self.assertEqual("claim_accepted", result["reasonCode"])
        self.assertEqual(pending_head["headHash"], result["observedRowHeadHash"])
        self.assertEqual("accepted", claim["outcome"])
        self.assertEqual(4, claim["plannedWrites"])
        self.assertEqual(2, generation["generation"])
        self.assertEqual(
            pending_head["headHash"],
            generation["predecessorHeadHash"],
        )
        self.assertEqual(
            pending_head["effectiveSettlementHash"],
            generation["predecessorSettlementHash"],
        )
        self.assertEqual(
            predecessor,
            self.module.validate_owner_settlement_document(
                document=predecessor
            ),
        )
        self.assertEqual(
            prior_generation["generationHash"],
            predecessor["generationHash"],
        )
        self.assertEqual("dominated", predecessor["outcome"])
        self.assertEqual(
            generation["generationHash"],
            predecessor["dominantGenerationHash"],
        )
        self.assertEqual(
            pending_head["fencingToken"],
            predecessor["fencingToken"],
        )
        self.assertEqual(
            prior_claim["claimSetHash"],
            prior_generation["claimSetHash"],
        )
        self.assertEqual("contact_optout", contact_settlement["outcome"])
        self.assertEqual(
            contact_settlement["settlementHash"],
            head["effectiveSettlementHash"],
        )
        self.assertEqual(
            contact_settlement["settlementHash"],
            result["rowSettlementHash"],
        )
        fanout = self._assert_fanout_incremented_once(
            context,
            before_fanout,
        )
        self.assertEqual(fanout, processed["fanoutHead"])
        self.assertEqual(7, len(self._writes(context["store"])))
        self.assertIn(("commit_applied", 7), context["store"].events)

    def test_apply_rejects_contact_row_edge_drift_without_writes(self):
        self._method()
        context = self._seed_apply()
        original_edge = context["edges"][0]
        drifted_edge = self.module.build_contact_row_binding_document(
            user_scope_hash=context["scope"],
            canonical_mailbox_identity_hash=context["canonicalHash"],
            row_id=context["rowId"],
            created_at="2026-08-04T12:01:00.999999Z",
        )
        self.assertEqual(original_edge["edgeId"], drifted_edge["edgeId"])
        self.assertNotEqual(
            original_edge["contactRowEdgeHash"],
            drifted_edge["contactRowEdgeHash"],
        )
        self.discovery._reference(
            context,
            "contactRowBindings",
            original_edge["edgeId"],
        ).set(drifted_edge, merge=False)
        before = deepcopy(context["store"].data)
        context["store"].events.clear()

        with self.assertRaises(self.module.RowAuthorityError):
            self._process(context)

        self.assertEqual(before, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
