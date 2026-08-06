"""RED contracts for atomic one-row contact fan-out release."""

from __future__ import annotations

import importlib
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from threading import Barrier
from unittest.mock import patch

from tests.row_authority_fakes import BoundedFakeTransaction


def _row_id(index):
    return f"sr1_{index:012x}4{index:03x}8{index:015x}"


class ContactFanoutReleaseTests(unittest.TestCase):
    B1_COLLECTIONS = (
        "sourceIdentities",
        "sourceClassifications",
        "sourceTransitionOwners",
        "sourceWorkLedgers",
        "sourceSettlements",
    )

    @classmethod
    def setUpClass(cls):
        cls.apply_type = importlib.import_module(
            "tests.test_row_authority_contact_fanout_apply"
        ).ContactFanoutApplyTests
        cls.apply_type.setUpClass()
        cls.release_type = importlib.import_module(
            "tests.test_row_authority_contact_releases"
        ).ContactReleaseTransitionTests
        cls.release_type.setUpClass()
        cls.ownership = importlib.import_module(
            "tests.test_row_authority_ownership"
        )
        cls.source_link_type = cls.ownership.RowSourceSettlementLinkTests
        cls.source_link_type.setUpClass()
        cls.module = cls.apply_type.module

    def setUp(self):
        self.apply = self.apply_type(methodName="runTest")
        self.apply.setUp()
        self.discovery = self.apply.discovery
        self.completion = self.apply.completion
        self.release = self.release_type(methodName="runTest")
        self.release.setUp()
        self.release_at = "2026-08-04T12:06:00.000000Z"
        self.leased_at = "2026-08-04T12:06:10.000000Z"
        self.discovered_at = "2026-08-04T12:06:20.000000Z"
        self.processed_at = "2026-08-04T12:06:30.000000Z"

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

    def _row_head(self, context):
        return self._documents(context, "rowAuthorityHeads")[
            context["rowId"]
        ]

    def _source_linker(self, context):
        linker = self.source_link_type(methodName="runTest")
        linker.setUp()
        linker.fixture = context["transition"].fixture
        linker.user_id = linker.fixture.user_id
        linker.scope = context["scope"]
        linker.row_id = context["rowId"]
        return linker

    def _b1_snapshot(self, context):
        return {
            path: deepcopy(document)
            for path, document in context["store"].data.items()
            if any(
                f"/{collection}/" in path
                for collection in self.B1_COLLECTIONS
            )
        }

    def _capture_non_b1_writes(self, context, observed):
        writes = deepcopy(self._writes(context["store"]))
        for event in writes:
            self.assertFalse(
                any(
                    f"/{collection}/" in event[1]
                    for collection in self.B1_COLLECTIONS
                ),
                event,
            )
        observed.extend(writes)
        context["store"].events.clear()
        return writes

    def _stored_b1_bundle(self, context, authority_link):
        source_id = authority_link["canonicalSourceId"]
        return {
            "identity": self._documents(
                context, "sourceIdentities"
            )[source_id],
            "classification": self._documents(
                context, "sourceClassifications"
            )[source_id],
            "owner": self._documents(
                context, "sourceTransitionOwners"
            )[source_id],
            "ledger": self._documents(
                context, "sourceWorkLedgers"
            )[source_id],
            "work_key": authority_link["workKey"],
        }

    def _store_b1_bundle(self, context, bundle):
        source_id = bundle["identity"]["canonicalSourceId"]
        user = context["transition"]._user(context["store"])
        for collection, key in (
            ("sourceIdentities", "identity"),
            ("sourceClassifications", "classification"),
            ("sourceTransitionOwners", "owner"),
            ("sourceWorkLedgers", "ledger"),
        ):
            user.collection(collection).document(source_id).create(
                bundle[key]
            )

    def _seed_source_thread_binding(self, context):
        return context["transition"].fixture._seed_thread_binding(
            context["store"],
            [context["rowId"]],
        )

    def _complete_actual_release(self, context, observed_writes):
        context["beforeReleaseHead"] = deepcopy(self._row_head(context))
        transition = self._release_transition(context)
        context["releaseTransition"] = transition
        self._capture_non_b1_writes(context, observed_writes)

        fanout = transition["fanoutHead"]
        leased = self.discovery._fanout(
            fanout,
            state_revision=fanout["stateRevision"] + 1,
            lease_owner_hash=self.discovery.lease_owner,
            lease_until=self.discovery.lease_until,
            fencing_token=fanout["fencingToken"] + 1,
            updated_at=self.leased_at,
        )
        context.update(
            {
                "settlement": transition["settlement"],
                "receipt": transition["transitionRequest"],
                "contactHead": transition["head"],
                "fanout": leased,
            }
        )
        self._reference(
            context,
            "contactOptOutFanoutHeads",
            leased["fanoutId"],
        ).set(leased, merge=False)
        self._capture_non_b1_writes(context, observed_writes)

        discovered = self.discovery._discover(
            context,
            discovered_at=self.discovered_at,
        )
        self.assertEqual("discovery_complete", discovered["disposition"])
        self.assertEqual("applying", discovered["fanoutHead"]["state"])
        self.assertEqual(1, len(discovered["obligations"]))
        context.update(
            {
                "fanout": discovered["fanoutHead"],
                "obligation": discovered["obligations"][0],
            }
        )
        self._capture_non_b1_writes(context, observed_writes)

        context["beforeFanout"] = deepcopy(context["fanout"])
        released = self._process(context)
        self._capture_non_b1_writes(context, observed_writes)
        context["fanout"] = released["fanoutHead"]
        context["rowHead"] = self._row_head(context)
        return released

    def _result(self, context):
        result_id = f"{context['fanout']['fanoutId']}--{context['rowId']}"
        return self._documents(
            context,
            "contactOptOutFanoutResults",
        )[result_id]

    def _retarget_contact_link(
        self,
        link,
        *,
        canonical_hash,
        exact_hash,
        user_scope_hash,
    ):
        retargeted = deepcopy(link)
        retargeted["canonicalMailboxIdentityHash"] = canonical_hash
        retargeted["exactIdentityHash"] = exact_hash
        material = {
            key: deepcopy(value)
            for key, value in retargeted.items()
            if key != "authorityLinkHash"
        }
        retargeted["authorityLinkHash"] = self.module.domain_hash(
            self.module.B1_CONTACT_AUTHORITY_LINK_HASH_DOMAIN,
            material,
            user_scope_hash=user_scope_hash,
        )
        return retargeted

    def _install_dominating_contact(self, context):
        prior = context.get("prior")
        installed = self.completion._install_contact_lineage(
            context,
            context["rowHead"],
            materialize_head=True,
            canonical_hash="c" * 64,
            contact_settlement_hash="e" * 64,
            fanout_id="f" * 64,
            generation_number=(
                1 if prior is None else prior["generation"]["generation"] + 1
            ),
            predecessor_settlement_hash=(
                None
                if prior is None
                else prior["settlement"]["settlementHash"]
            ),
            first_fencing_token=(
                1
                if prior is None
                else prior["settlement"]["fencingToken"] + 1
            ),
        )
        context["rowHead"] = installed["settledHead"]
        return installed

    def _install_different_contact_successor(self, context):
        before = self._row_head(context)
        released_result = context["applyResult"]
        generation_id = (
            f"{context['rowId']}--{released_result['rowGeneration']}"
        )
        released_settlement = self._documents(
            context,
            "rowOwnerSettlements",
        )[generation_id]
        different_canonical = "c" * 64
        different_link = self._retarget_contact_link(
            context["activeSettlement"]["authorityLink"],
            canonical_hash=different_canonical,
            exact_hash="b" * 64,
            user_scope_hash=context["scope"],
        )
        generation_number = released_result["rowGeneration"] + 1
        first_fence = released_settlement["fencingToken"] + 1
        claim = self.module.build_claim_set_document(
            user_scope_hash=context["scope"],
            authority_origin="contact_fanout",
            authority_link=different_link,
            operator_action_document=None,
            fanout_id="f" * 64,
            row_ids=[context["rowId"]],
            primary_row_id=context["rowId"],
            planned_writes=3,
            outcome="accepted",
            row_decisions=[
                {
                    "rowId": context["rowId"],
                    "decision": "accepted",
                    "plannedGeneration": generation_number,
                    "winnerGenerationHash": None,
                    "winnerSettlementHash": None,
                }
            ],
            created_at="2026-08-04T12:05:46.000000Z",
            canonical_mailbox_identity_hash=different_canonical,
            contact_settlement_hash="e" * 64,
        )
        generation = self.module.build_owner_generation_document(
            claim_set_document=claim,
            row_id=context["rowId"],
            generation=generation_number,
            predecessor_head_hash=before["headHash"],
            predecessor_settlement_hash=released_settlement[
                "settlementHash"
            ],
            lease_epoch=1,
            first_fencing_token=first_fence,
            created_at="2026-08-04T12:05:46.000000Z",
        )
        claimed = {
            key: deepcopy(value)
            for key, value in before.items()
            if key != "headHash"
        }
        claimed.update(
            {
                "stateRevision": before["stateRevision"] + 1,
                "effectiveOwnerGeneration": generation_number,
                "effectiveOwnerGenerationHash": generation[
                    "generationHash"
                ],
                "effectiveOwnerKind": "contact_optout",
                "effectivePriority": 3,
                "state": "claimed",
                "leaseOwnerHash": "a" * 64,
                "leaseUntil": "2026-08-04T12:09:00.000000Z",
                "fencingToken": first_fence,
                "effectiveSettlementHash": released_settlement[
                    "settlementHash"
                ],
                "updatedAt": "2026-08-04T12:05:46.000000Z",
            }
        )
        claimed = context["transition"].fixture._rehash_head(claimed)
        settlement = self.module.build_owner_settlement_document(
            generation_document=generation,
            claim_set_document=claim,
            fencing_token=first_fence,
            outcome="contact_optout",
            settled_at="2026-08-04T12:05:47.000000Z",
            superseded_effective_settlement_hash=released_settlement[
                "settlementHash"
            ],
        )
        settled_head = self.module._build_settlement_advanced_head(
            expected_head=claimed,
            generation_document=generation,
            settlement_document=settlement,
        )
        fixture = context["transition"].fixture
        fixture._claim_reference(context["store"], claim["requestId"]).create(
            claim
        )
        fixture._generation_reference(
            context["store"],
            context["rowId"],
            generation_number,
        ).create(generation)
        fixture._settlement_reference(
            context["store"],
            context["rowId"],
            generation_number,
        ).create(settlement)
        fixture._row_references(context["store"], context["rowId"])[1].set(
            settled_head,
            merge=False,
        )
        context["store"].events.clear()
        installed = {
            "claim": claim,
            "generation": generation,
            "settlement": settlement,
            "settledHead": settled_head,
        }
        context["rowHead"] = settled_head
        return installed

    def _lease_and_discover(
        self,
        context,
        transition,
        *,
        leased_at=None,
        discovered_at=None,
    ):
        leased_at = leased_at or self.leased_at
        discovered_at = discovered_at or self.discovered_at
        fanout = transition["fanoutHead"]
        leased = self.discovery._fanout(
            fanout,
            state_revision=fanout["stateRevision"] + 1,
            lease_owner_hash=self.discovery.lease_owner,
            lease_until=self.discovery.lease_until,
            fencing_token=fanout["fencingToken"] + 1,
            updated_at=leased_at,
        )
        context.update(
            {
                "settlement": transition["settlement"],
                "receipt": transition["transitionRequest"],
                "contactHead": transition["head"],
                "fanout": leased,
            }
        )
        self.discovery._store_fanout(context, leased)
        discovered = self.discovery._discover(
            context,
            discovered_at=discovered_at,
        )
        self.assertEqual("discovery_complete", discovered["disposition"])
        self.assertEqual("applying", discovered["fanoutHead"]["state"])
        self.assertEqual(1, len(discovered["obligations"]))
        context.update(
            {
                "fanout": discovered["fanoutHead"],
                "obligation": discovered["obligations"][0],
            }
        )
        context["store"].events.clear()
        return context

    def _release_transition(self, context, *, requested_at=None):
        transition = self.release._release(
            {
                "store": context["store"],
                "settlement": context["activeSettlement"],
            },
            requested_at=requested_at or self.release_at,
        )
        self.assertEqual("created", transition["disposition"])
        self.assertEqual("release", transition["fanoutHead"]["outcome"])
        return transition

    def _seed_release(
        self,
        *,
        prior_owner=None,
        apply_outcome="applied",
        different_effective_owner=False,
    ):
        context = self.apply._seed_apply(prior_owner=prior_owner)
        context["activeSettlement"] = context["settlement"]
        context["activeReceipt"] = context["receipt"]
        context["activeFanout"] = context["fanout"]
        context["preApplyHead"] = deepcopy(context["rowHead"])

        if apply_outcome == "dominated":
            context["dominant"] = self._install_dominating_contact(context)
        elif apply_outcome != "not_applied":
            processed = self.apply._process(context)
            self.assertEqual("applied", processed["disposition"])
            context["applyResult"] = processed["result"]
            context["fanout"] = processed["fanoutHead"]
            context["rowHead"] = self._row_head(context)
            if different_effective_owner:
                context["differentOwner"] = (
                    self._install_different_contact_successor(context)
                )

        if apply_outcome == "dominated":
            processed = self.apply._process(context)
            self.assertEqual("dominated", processed["disposition"])
            context["applyResult"] = processed["result"]
            context["fanout"] = processed["fanoutHead"]
            context["rowHead"] = self._row_head(context)
        elif apply_outcome == "not_applied":
            context["applyResult"] = None

        context["beforeReleaseHead"] = deepcopy(self._row_head(context))
        transition = self._release_transition(context)
        context["releaseTransition"] = transition
        self._lease_and_discover(context, transition)
        context["beforeFanout"] = deepcopy(context["fanout"])
        context["store"].events.clear()
        return context

    def _seed_release_scan(self, count, *, result_count):
        rows = [_row_id(index) for index in range(1, count + 1)]
        context = self.discovery._seed_release(tuple(rows))
        obligations = [
            self.discovery._obligation(
                context,
                edge,
                created_at=self.discovered_at,
            )
            for edge in context["edges"]
        ]
        for obligation in obligations[:result_count]:
            result = self.module.build_contact_fanout_result_document(
                user_scope_hash=context["scope"],
                fanout_id=context["fanout"]["fanoutId"],
                row_id=obligation["rowId"],
                obligation_hash=obligation[
                    "contactFanoutObligationHash"
                ],
                outcome="release",
                disposition="noop",
                reason_code="row_optout_not_applied",
                observed_row_head_hash="a" * 64,
                claim_request_id=None,
                claim_set_hash=None,
                row_generation=None,
                row_settlement_hash=None,
                released_row_generation=None,
                released_row_settlement_hash=None,
                restored_effective_generation=None,
                restored_effective_settlement_hash=None,
                created_at=self.processed_at,
            )
            result_id = (
                f"{context['fanout']['fanoutId']}--{obligation['rowId']}"
            )
            self._reference(
                context,
                "contactOptOutFanoutResults",
                result_id,
            ).create(result)
        applying = self.discovery._fanout(
            context["fanout"],
            state_revision=context["fanout"]["stateRevision"] + 1,
            state="applying",
            obligation_count=count,
            result_count=result_count,
            updated_at=(
                self.processed_at if result_count else self.discovered_at
            ),
        )
        self.discovery._store_fanout(context, applying)
        context.update(
            {
                "rows": rows,
                "obligations": obligations,
                "fanout": applying,
                "rowId": rows[-1],
            }
        )
        context["store"].events.clear()
        return context

    def _materialize_scan_rows(self, context, *, lifecycle="deleted"):
        fixture = context["transition"].fixture
        for row_id in context["rows"]:
            fixture._seed_row(
                context["store"],
                row_id,
                lifecycle=lifecycle,
            )
        context["store"].events.clear()
        return context

    def _process(
        self,
        context,
        *,
        expected_fanout_head=None,
        processed_at=None,
        executor=None,
    ):
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
            lease_owner_hash=self.discovery.lease_owner,
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

    def _reference(self, context, collection, document_id):
        return self.discovery._reference(
            context,
            collection,
            document_id,
        )

    def _replace(self, context, collection, document_id, document):
        self._reference(context, collection, document_id).set(
            document,
            merge=False,
        )
        context["store"].events.clear()

    def _delete(self, context, collection, document_id):
        self._reference(context, collection, document_id).delete()
        context["store"].events.clear()

    def _assert_integrity_rejected(
        self,
        context,
        *,
        error_type=None,
        executor=None,
    ):
        expected_error = error_type or self.module.RowAuthorityAmbiguous
        before = deepcopy(context["store"].data)
        context["store"].events.clear()
        with self.assertRaises(expected_error):
            self._process(context, executor=executor)
        self.assertEqual(before, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))

    def _prior_authority(self, context):
        prior = context["prior"]
        generation = prior["generation"]
        settlement = prior["settlement"]
        claim = prior["claim"]
        generation_id = f"{context['rowId']}--{generation['generation']}"
        return {
            "claim": claim,
            "claimId": claim["requestId"],
            "generation": generation,
            "generationId": generation_id,
            "settlement": settlement,
            "settlementId": generation_id,
        }

    def _applied_authority(self, context):
        result = context["applyResult"]
        generation_id = f"{context['rowId']}--{result['rowGeneration']}"
        generation = deepcopy(
            self._documents(context, "rowOwnerGenerations")[generation_id]
        )
        claim = deepcopy(
            self._documents(context, "rowClaimSets")[
                generation["requestId"]
            ]
        )
        settlement = deepcopy(
            self._documents(context, "rowOwnerSettlements")[generation_id]
        )
        return claim, generation, settlement, generation_id

    def _assert_owner_lineage_self_valid(self, claim, generation, settlement):
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
        self.assertEqual(claim["requestId"], generation["requestId"])
        self.assertEqual(claim["claimSetHash"], generation["claimSetHash"])
        self.assertEqual(generation["rowId"], settlement["rowId"])
        self.assertEqual(generation["generation"], settlement["generation"])
        self.assertEqual(
            generation["generationHash"], settlement["generationHash"]
        )

    def _retarget_applied_predecessor(self, context, predecessor_settlement):
        claim, generation, settlement, generation_id = self._applied_authority(
            context
        )
        rebuilt_generation = self.module.build_owner_generation_document(
            claim_set_document=claim,
            row_id=context["rowId"],
            generation=generation["generation"],
            predecessor_head_hash=generation["predecessorHeadHash"],
            predecessor_settlement_hash=predecessor_settlement[
                "settlementHash"
            ],
            lease_epoch=1,
            first_fencing_token=generation["firstFencingToken"],
            created_at=generation["createdAt"],
        )
        rebuilt_settlement = self.module.build_owner_settlement_document(
            generation_document=rebuilt_generation,
            claim_set_document=claim,
            fencing_token=settlement["fencingToken"],
            outcome="contact_optout",
            settled_at=settlement["settledAt"],
            superseded_effective_settlement_hash=predecessor_settlement[
                "settlementHash"
            ],
        )
        result = context["applyResult"]
        rebuilt_result = self.module.build_contact_fanout_result_document(
            user_scope_hash=result["userScopeHash"],
            fanout_id=result["fanoutId"],
            row_id=result["rowId"],
            obligation_hash=result["obligationHash"],
            outcome="apply",
            disposition="applied",
            reason_code="claim_accepted",
            observed_row_head_hash=result["observedRowHeadHash"],
            claim_request_id=claim["requestId"],
            claim_set_hash=claim["claimSetHash"],
            row_generation=rebuilt_generation["generation"],
            row_settlement_hash=rebuilt_settlement["settlementHash"],
            released_row_generation=None,
            released_row_settlement_hash=None,
            restored_effective_generation=None,
            restored_effective_settlement_hash=None,
            created_at=result["createdAt"],
        )
        head = deepcopy(self._row_head(context))
        head.update(
            {
                "effectiveOwnerGenerationHash": rebuilt_generation[
                    "generationHash"
                ],
                "latestSettlementHash": rebuilt_settlement[
                    "settlementHash"
                ],
                "effectiveSettlementHash": rebuilt_settlement[
                    "settlementHash"
                ],
            }
        )
        head = context["transition"].fixture._rehash_head(head)
        self._replace(
            context,
            "rowOwnerGenerations",
            generation_id,
            rebuilt_generation,
        )
        self._replace(
            context,
            "rowOwnerSettlements",
            generation_id,
            rebuilt_settlement,
        )
        self._replace(
            context,
            "contactOptOutFanoutResults",
            f"{result['fanoutId']}--{context['rowId']}",
            rebuilt_result,
        )
        context["transition"].fixture._row_references(
            context["store"], context["rowId"]
        )[1].set(head, merge=False)
        context.update(
            {
                "applyResult": rebuilt_result,
                "beforeReleaseHead": deepcopy(head),
                "rowHead": head,
            }
        )
        context["store"].events.clear()

    def _foreign_terminal_predecessor(self, context, *, generation=1):
        row_id = _row_id(2)
        fixture = context["transition"].fixture
        fixture._seed_row(context["store"], row_id, lifecycle="active")
        claim, owner_generation, claimed_head = fixture._install_owner(
            context["store"], row_id, owner_kind="terminal"
        )
        settlement, _settled_head = fixture._settle_terminal_owner(
            context["store"], claim, owner_generation, claimed_head
        )
        if generation == 1:
            self._assert_owner_lineage_self_valid(
                claim,
                owner_generation,
                settlement,
            )
            context["store"].events.clear()
            return settlement
        later_link = deepcopy(claim["authorityLink"])
        later_link["workKey"] = "f" * 64
        later_link["authorityLinkHash"] = self.module.domain_hash(
            self.module.B1_AUTHORITY_LINK_HASH_DOMAIN,
            {
                key: deepcopy(value)
                for key, value in later_link.items()
                if key != "authorityLinkHash"
            },
            user_scope_hash=context["scope"],
        )
        later_claim = self.module.build_claim_set_document(
            user_scope_hash=context["scope"],
            authority_origin="b1_source",
            authority_link=later_link,
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
                    "plannedGeneration": generation,
                    "winnerGenerationHash": None,
                    "winnerSettlementHash": None,
                }
            ],
            created_at="2026-08-04T12:04:00.000000Z",
        )
        later_generation = self.module.build_owner_generation_document(
            claim_set_document=later_claim,
            row_id=row_id,
            generation=generation,
            predecessor_head_hash=claimed_head["headHash"],
            predecessor_settlement_hash=settlement["settlementHash"],
            lease_epoch=1,
            first_fencing_token=generation,
            created_at="2026-08-04T12:04:00.000000Z",
        )
        later_settlement = self.module.build_owner_settlement_document(
            generation_document=later_generation,
            claim_set_document=later_claim,
            fencing_token=generation,
            outcome="terminal",
            settled_at="2026-08-04T12:04:01.000000Z",
        )
        self._assert_owner_lineage_self_valid(
            later_claim,
            later_generation,
            later_settlement,
        )
        self._reference(
            context, "rowClaimSets", later_claim["requestId"]
        ).create(later_claim)
        self._reference(
            context,
            "rowOwnerGenerations",
            f"{row_id}--{generation}",
        ).create(later_generation)
        self._reference(
            context,
            "rowOwnerSettlements",
            f"{row_id}--{generation}",
        ).create(later_settlement)
        context["store"].events.clear()
        return later_settlement

    def _install_self_valid_contact_predecessor(self, context):
        current_claim, _generation, _settlement, _generation_id = (
            self._applied_authority(context)
        )
        claim = self.module.build_claim_set_document(
            user_scope_hash=context["scope"],
            authority_origin="contact_fanout",
            authority_link=current_claim["authorityLink"],
            operator_action_document=None,
            fanout_id="f" * 64,
            row_ids=[context["rowId"]],
            primary_row_id=context["rowId"],
            planned_writes=3,
            outcome="accepted",
            row_decisions=[
                {
                    "rowId": context["rowId"],
                    "decision": "accepted",
                    "plannedGeneration": 1,
                    "winnerGenerationHash": None,
                    "winnerSettlementHash": None,
                }
            ],
            created_at=current_claim["createdAt"],
            canonical_mailbox_identity_hash=context["canonicalHash"],
            contact_settlement_hash="e" * 64,
        )
        generation = self.module.build_owner_generation_document(
            claim_set_document=claim,
            row_id=context["rowId"],
            generation=1,
            predecessor_head_hash="a" * 64,
            predecessor_settlement_hash=None,
            lease_epoch=1,
            first_fencing_token=1,
            created_at=claim["createdAt"],
        )
        settlement = self.module.build_owner_settlement_document(
            generation_document=generation,
            claim_set_document=claim,
            fencing_token=1,
            outcome="contact_optout",
            settled_at=current_claim["createdAt"],
            superseded_effective_settlement_hash=None,
        )
        self._assert_owner_lineage_self_valid(claim, generation, settlement)
        prior = self._prior_authority(context)
        self._reference(context, "rowClaimSets", claim["requestId"]).create(
            claim
        )
        self._replace(
            context,
            "rowOwnerGenerations",
            prior["generationId"],
            generation,
        )
        self._replace(
            context,
            "rowOwnerSettlements",
            prior["settlementId"],
            settlement,
        )
        self._retarget_applied_predecessor(context, settlement)
        return settlement

    def _assert_fanout_incremented(self, context, before):
        current = self._documents(
            context,
            "contactOptOutFanoutHeads",
        )[before["fanoutId"]]
        expected = self.discovery._fanout(
            before,
            state_revision=before["stateRevision"] + 1,
            result_count=before["resultCount"] + 1,
            updated_at=self.processed_at,
        )
        self.assertEqual(expected, current)
        return current

    def _assert_restore_result(
        self,
        context,
        result,
        *,
        restored_generation,
        restored_settlement,
    ):
        before = context["beforeReleaseHead"]
        applied = context["applyResult"]
        self.assertEqual(
            result,
            self.module.validate_contact_fanout_result_document(
                document=result
            ),
        )
        self.assertEqual("release", result["outcome"])
        self.assertEqual("restore", result["disposition"])
        self.assertEqual("exact_predecessor", result["reasonCode"])
        self.assertEqual(before["headHash"], result["observedRowHeadHash"])
        self.assertEqual(
            applied["rowGeneration"], result["releasedRowGeneration"]
        )
        self.assertEqual(
            applied["rowSettlementHash"],
            result["releasedRowSettlementHash"],
        )
        self.assertEqual(
            None if restored_generation is None else restored_generation["generation"],
            result["restoredEffectiveGeneration"],
        )
        self.assertEqual(
            None if restored_settlement is None else restored_settlement["settlementHash"],
            result["restoredEffectiveSettlementHash"],
        )
        for field in (
            "claimRequestId",
            "claimSetHash",
            "rowGeneration",
            "rowSettlementHash",
        ):
            self.assertIsNone(result[field])
        expected = self.module.build_contact_fanout_result_document(
            user_scope_hash=context["scope"],
            fanout_id=context["fanout"]["fanoutId"],
            row_id=context["rowId"],
            obligation_hash=context["obligation"][
                "contactFanoutObligationHash"
            ],
            outcome="release",
            disposition="restore",
            reason_code="exact_predecessor",
            observed_row_head_hash=before["headHash"],
            claim_request_id=None,
            claim_set_hash=None,
            row_generation=None,
            row_settlement_hash=None,
            released_row_generation=applied["rowGeneration"],
            released_row_settlement_hash=applied["rowSettlementHash"],
            restored_effective_generation=(
                None
                if restored_generation is None
                else restored_generation["generation"]
            ),
            restored_effective_settlement_hash=(
                None
                if restored_settlement is None
                else restored_settlement["settlementHash"]
            ),
            created_at=self.processed_at,
        )
        self.assertEqual(expected, result)

    def _assert_release_noop_result(
        self,
        context,
        result,
        *,
        reason_code,
        released_generation=None,
        released_settlement_hash=None,
    ):
        expected = self.module.build_contact_fanout_result_document(
            user_scope_hash=context["scope"],
            fanout_id=context["fanout"]["fanoutId"],
            row_id=context["rowId"],
            obligation_hash=context["obligation"][
                "contactFanoutObligationHash"
            ],
            outcome="release",
            disposition="noop",
            reason_code=reason_code,
            observed_row_head_hash=context["beforeReleaseHead"]["headHash"],
            claim_request_id=None,
            claim_set_hash=None,
            row_generation=None,
            row_settlement_hash=None,
            released_row_generation=released_generation,
            released_row_settlement_hash=released_settlement_hash,
            restored_effective_generation=None,
            restored_effective_settlement_hash=None,
            created_at=self.processed_at,
        )
        self.assertEqual(
            result,
            self.module.validate_contact_fanout_result_document(
                document=result
            ),
        )
        self.assertEqual(expected, result)

    def _install_preserved_row_history(self, context):
        claim, generation, settlement, generation_id = (
            self._applied_authority(context)
        )
        linked_at = "2026-08-04T12:06:25.000000Z"
        source_link = self.module.build_source_settlement_link_document(
            user_scope_hash=context["scope"],
            row_id=context["rowId"],
            generation=generation["generation"],
            generation_hash=generation["generationHash"],
            authority_link_hash=claim["authorityLinkHash"],
            b1_identity_hash="7" * 64,
            b1_final_ledger_evidence_hash="8" * 64,
            b1_settlement_revision=7,
            b1_settlement_hash="9" * 64,
            b2_settlement_hash=settlement["settlementHash"],
            linked_at=linked_at,
        )
        self.assertEqual(
            source_link,
            self.module.validate_source_settlement_link_document(
                document=source_link
            ),
        )
        self._reference(
            context,
            "rowSourceSettlementLinks",
            generation_id,
        ).create(source_link)
        head = deepcopy(self._row_head(context))
        head.update(
            {
                "stateRevision": head["stateRevision"] + 1,
                "latestSourceSettlementLinkHash": source_link[
                    "sourceSettlementLinkHash"
                ],
                "projectionBacklogCount": 7,
                "updatedAt": linked_at,
            }
        )
        head = context["transition"].fixture._rehash_head(head)
        self.assertEqual(
            head,
            self.module.validate_row_authority_head(document=head),
        )
        context["transition"].fixture._row_references(
            context["store"], context["rowId"]
        )[1].set(head, merge=False)
        context["beforeReleaseHead"] = deepcopy(head)
        context["preservedSourceLink"] = source_link
        context["store"].events.clear()
        return head

    def test_release_restores_exact_terminal_or_human_predecessor(self):
        for prior_owner in ("terminal", "human_decision"):
            with self.subTest(prior_owner=prior_owner):
                context = self._seed_release(prior_owner=prior_owner)
                self._install_preserved_row_history(context)
                prior = context["prior"]
                before_head = deepcopy(context["beforeReleaseHead"])
                before_fanout = deepcopy(context["beforeFanout"])
                before_authority = {
                    collection: self._documents(context, collection)
                    for collection in (
                        "rowClaimSets",
                        "rowOwnerGenerations",
                        "rowOwnerSettlements",
                        "rowSourceSettlementLinks",
                    )
                }

                fake_module = importlib.import_module(
                    "tests.source_coordinator_fakes"
                )
                original_limit = fake_module.FakeQuery.limit
                observed_limits = []

                def record_limit(query, count):
                    observed_limits.append(
                        (
                            query._collection.path,
                            query._filters,
                            count,
                        )
                    )
                    return original_limit(query, count)

                with patch.object(
                    fake_module.FakeQuery,
                    "limit",
                    new=record_limit,
                ):
                    processed = self._process(context)

                result = self._result(context)
                head = self._row_head(context)
                self._assert_restore_result(
                    context,
                    result,
                    restored_generation=prior["generation"],
                    restored_settlement=prior["settlement"],
                )
                self.assertEqual(
                    prior["generation"]["generation"],
                    head["effectiveOwnerGeneration"],
                )
                self.assertEqual(
                    prior["generation"]["generationHash"],
                    head["effectiveOwnerGenerationHash"],
                )
                self.assertEqual(prior_owner, head["effectiveOwnerKind"])
                self.assertEqual(
                    prior["generation"]["priority"],
                    head["effectivePriority"],
                )
                self.assertEqual(
                    prior["settlement"]["settlementHash"],
                    head["effectiveSettlementHash"],
                )
                self.assertEqual("settled", head["state"])
                self.assertEqual(
                    prior["settlement"]["fencingToken"],
                    head["fencingToken"],
                )
                self.assertEqual(
                    context["applyResult"]["rowSettlementHash"],
                    head["latestSettlementHash"],
                )
                self.assertEqual(
                    result["contactFanoutResultHash"],
                    head["latestOptOutReleaseResultHash"],
                )
                self.assertEqual(
                    before_head["stateRevision"] + 1,
                    head["stateRevision"],
                )
                self.assertIsNone(head["leaseOwnerHash"])
                self.assertIsNone(head["leaseUntil"])
                self.assertEqual(
                    before_head["latestSourceSettlementLinkHash"],
                    head["latestSourceSettlementLinkHash"],
                )
                self.assertEqual(
                    before_head["projectionBacklogCount"],
                    head["projectionBacklogCount"],
                )
                for collection, documents in before_authority.items():
                    self.assertEqual(
                        documents,
                        self._documents(context, collection),
                    )
                fanout = self._assert_fanout_incremented(
                    context,
                    before_fanout,
                )
                self.assertEqual("restore", processed["disposition"])
                self.assertEqual(result, processed["result"])
                self.assertEqual(fanout, processed["fanoutHead"])
                self.assertEqual(3, len(self._writes(context["store"])))
                self.assertIn(
                    ("commit_applied", 3),
                    context["store"].events,
                )
                self.assertIn(
                    (
                        self.discovery._user(context)
                        .collection("rowOwnerSettlements")
                        .path,
                        (
                            (
                                "settlementHash",
                                "==",
                                prior["settlement"]["settlementHash"],
                            ),
                        ),
                        2,
                    ),
                    observed_limits,
                    "exact predecessor lookup must use equality plus limit(2)",
                )

    def test_release_restores_clear_without_reusing_generation(self):
        context = self._seed_release()
        self._install_preserved_row_history(context)
        before_head = deepcopy(context["beforeReleaseHead"])
        before_fanout = deepcopy(context["beforeFanout"])
        generations = self._documents(context, "rowOwnerGenerations")
        settlements = self._documents(context, "rowOwnerSettlements")
        source_links = self._documents(context, "rowSourceSettlementLinks")

        processed = self._process(context)

        result = self._result(context)
        head = self._row_head(context)
        self._assert_restore_result(
            context,
            result,
            restored_generation=None,
            restored_settlement=None,
        )
        self.assertEqual("clear", head["state"])
        for field in (
            "effectiveOwnerGeneration",
            "effectiveOwnerGenerationHash",
            "effectiveOwnerKind",
            "effectivePriority",
            "effectiveSettlementHash",
            "fencingToken",
        ):
            self.assertIsNone(head[field])
        self.assertEqual(
            context["applyResult"]["rowSettlementHash"],
            head["latestSettlementHash"],
        )
        self.assertEqual(
            before_head["stateRevision"] + 1,
            head["stateRevision"],
        )
        self.assertEqual(
            before_head["latestSourceSettlementLinkHash"],
            head["latestSourceSettlementLinkHash"],
        )
        self.assertEqual(
            before_head["projectionBacklogCount"],
            head["projectionBacklogCount"],
        )
        self.assertEqual(
            generations,
            self._documents(context, "rowOwnerGenerations"),
        )
        self.assertEqual(
            settlements,
            self._documents(context, "rowOwnerSettlements"),
        )
        self.assertEqual(
            source_links,
            self._documents(context, "rowSourceSettlementLinks"),
        )
        self.assertEqual(1, max(item["generation"] for item in generations.values()))
        fanout = self._assert_fanout_incremented(context, before_fanout)
        self.assertEqual("restore", processed["disposition"])
        self.assertEqual(result, processed["result"])
        self.assertEqual(fanout, processed["fanoutHead"])
        self.assertEqual(3, len(self._writes(context["store"])))
        self.assertIn(("commit_applied", 3), context["store"].events)

    def test_release_noops_when_row_optout_was_dominated_or_not_applied(self):
        for apply_outcome in ("dominated", "not_applied"):
            with self.subTest(apply_outcome=apply_outcome):
                context = self._seed_release(apply_outcome=apply_outcome)
                before_head = deepcopy(context["beforeReleaseHead"])
                before_fanout = deepcopy(context["beforeFanout"])

                processed = self._process(context)

                result = self._result(context)
                self._assert_release_noop_result(
                    context,
                    result,
                    reason_code="row_optout_not_applied",
                )
                self.assertEqual("noop", result["disposition"])
                self.assertEqual(
                    "row_optout_not_applied",
                    result["reasonCode"],
                )
                self.assertEqual(
                    before_head["headHash"],
                    result["observedRowHeadHash"],
                )
                for field in (
                    "claimRequestId",
                    "claimSetHash",
                    "rowGeneration",
                    "rowSettlementHash",
                    "releasedRowGeneration",
                    "releasedRowSettlementHash",
                    "restoredEffectiveGeneration",
                    "restoredEffectiveSettlementHash",
                ):
                    self.assertIsNone(result[field])
                self.assertEqual(before_head, self._row_head(context))
                fanout = self._assert_fanout_incremented(
                    context,
                    before_fanout,
                )
                self.assertEqual("noop", processed["disposition"])
                self.assertEqual(result, processed["result"])
                self.assertEqual(fanout, processed["fanoutHead"])
                self.assertEqual(2, len(self._writes(context["store"])))
                self.assertIn(
                    ("commit_applied", 2),
                    context["store"].events,
                )

    def test_release_rejects_impossible_equal_priority_different_owner_lineage(
        self,
    ):
        context = self._seed_release(different_effective_owner=True)
        other = context["differentOwner"]
        self.assertEqual(
            context["applyResult"]["rowGeneration"] + 1,
            other["generation"]["generation"],
        )
        self.assertEqual(
            context["applyResult"]["rowSettlementHash"],
            other["generation"]["predecessorSettlementHash"],
        )
        self.assertEqual(
            3,
            other["generation"]["priority"],
        )

        self._assert_integrity_rejected(context)

    def test_release_rejects_applied_target_moved_by_other_release_bridge(
        self,
    ):
        context = self._seed_release(prior_owner="terminal")
        before = deepcopy(self._row_head(context))
        _claim, target_generation, target_settlement, generation_id = (
            self._applied_authority(context)
        )
        prior = context["prior"]
        alternate = self.module.build_contact_fanout_result_document(
            user_scope_hash=context["scope"],
            fanout_id="d" * 64,
            row_id=context["rowId"],
            obligation_hash="a" * 64,
            outcome="release",
            disposition="restore",
            reason_code="exact_predecessor",
            observed_row_head_hash=before["headHash"],
            claim_request_id=None,
            claim_set_hash=None,
            row_generation=None,
            row_settlement_hash=None,
            released_row_generation=target_generation["generation"],
            released_row_settlement_hash=target_settlement[
                "settlementHash"
            ],
            restored_effective_generation=prior["generation"]["generation"],
            restored_effective_settlement_hash=prior["settlement"][
                "settlementHash"
            ],
            created_at="2026-08-04T12:05:48.000000Z",
        )
        restored = self.module._build_contact_fanout_release_row_head(
            expected_head=before,
            result_document=alternate,
            released_generation_document=target_generation,
            released_settlement_document=target_settlement,
            restored_generation_document=prior["generation"],
            restored_settlement_document=prior["settlement"],
            released_at=alternate["createdAt"],
        )
        different_canonical = "c" * 64
        different_link = self._retarget_contact_link(
            context["activeSettlement"]["authorityLink"],
            canonical_hash=different_canonical,
            exact_hash="b" * 64,
            user_scope_hash=context["scope"],
        )
        successor_claim = self.module.build_claim_set_document(
            user_scope_hash=context["scope"],
            authority_origin="contact_fanout",
            authority_link=different_link,
            operator_action_document=None,
            fanout_id="e" * 64,
            row_ids=[context["rowId"]],
            primary_row_id=context["rowId"],
            planned_writes=3,
            outcome="accepted",
            row_decisions=[
                {
                    "rowId": context["rowId"],
                    "decision": "accepted",
                    "plannedGeneration": target_generation["generation"] + 1,
                    "winnerGenerationHash": None,
                    "winnerSettlementHash": None,
                }
            ],
            created_at="2026-08-04T12:05:49.000000Z",
            canonical_mailbox_identity_hash=different_canonical,
            contact_settlement_hash="f" * 64,
        )
        successor_generation = self.module.build_owner_generation_document(
            claim_set_document=successor_claim,
            row_id=context["rowId"],
            generation=target_generation["generation"] + 1,
            predecessor_head_hash=restored["headHash"],
            predecessor_settlement_hash=prior["settlement"][
                "settlementHash"
            ],
            lease_epoch=1,
            first_fencing_token=target_settlement["fencingToken"] + 1,
            created_at="2026-08-04T12:05:49.000000Z",
        )
        claimed = self.module._build_claim_advanced_head(
            expected_head=restored,
            generation_document=successor_generation,
            lease_owner_hash="a" * 64,
            lease_until="2026-08-04T12:09:00.000000Z",
            dominated_predecessor_settlement_hash=None,
            claimed_at="2026-08-04T12:05:49.000000Z",
            expected_generation=successor_generation["generation"],
            expected_first_fencing_token=successor_generation[
                "firstFencingToken"
            ],
        )
        successor_settlement = self.module.build_owner_settlement_document(
            generation_document=successor_generation,
            claim_set_document=successor_claim,
            fencing_token=claimed["fencingToken"],
            outcome="contact_optout",
            settled_at="2026-08-04T12:05:50.000000Z",
            superseded_effective_settlement_hash=prior["settlement"][
                "settlementHash"
            ],
        )
        settled = self.module._build_settlement_advanced_head(
            expected_head=claimed,
            generation_document=successor_generation,
            settlement_document=successor_settlement,
        )
        self._reference(
            context,
            "contactOptOutFanoutResults",
            f"{'d' * 64}--{context['rowId']}",
        ).create(alternate)
        fixture = context["transition"].fixture
        fixture._claim_reference(
            context["store"], successor_claim["requestId"]
        ).create(successor_claim)
        fixture._generation_reference(
            context["store"],
            context["rowId"],
            successor_generation["generation"],
        ).create(successor_generation)
        fixture._settlement_reference(
            context["store"],
            context["rowId"],
            successor_generation["generation"],
        ).create(successor_settlement)
        fixture._row_references(
            context["store"], context["rowId"]
        )[1].set(settled, merge=False)
        context["beforeReleaseHead"] = deepcopy(settled)
        context["store"].events.clear()
        self.assertEqual(
            generation_id,
            f"{context['rowId']}--{target_generation['generation']}",
        )

        self._assert_integrity_rejected(context)

    def test_release_never_restores_another_canonical_contacts_optout(self):
        context = self._seed_release(apply_outcome="dominated")
        other = context["dominant"]
        before_head = deepcopy(context["beforeReleaseHead"])
        self.assertEqual(
            "c" * 64,
            other["claim"]["ownerKey"],
        )
        self.assertNotEqual(
            context["canonicalHash"],
            other["claim"]["ownerKey"],
        )

        processed = self._process(context)

        result = self._result(context)
        self.assertEqual("noop", processed["disposition"])
        self.assertEqual("row_optout_not_applied", result["reasonCode"])
        self.assertIsNone(result["releasedRowGeneration"])
        self.assertIsNone(result["releasedRowSettlementHash"])
        self.assertEqual(before_head, self._row_head(context))
        self.assertEqual(2, len(self._writes(context["store"])))

    def test_release_rejects_missing_duplicate_wrong_row_or_nonlower_predecessor(
        self,
    ):
        for case in (
            "missing",
            "duplicate",
            "wrong_row",
            "non_lower_generation",
        ):
            with self.subTest(case=case):
                context = self._seed_release(prior_owner="terminal")
                prior = self._prior_authority(context)
                if case == "missing":
                    self._delete(
                        context,
                        "rowOwnerSettlements",
                        prior["settlementId"],
                    )
                elif case == "duplicate":
                    self._reference(
                        context,
                        "rowOwnerSettlements",
                        "duplicate-predecessor-settlement",
                    ).create(prior["settlement"])
                    context["store"].events.clear()
                elif case == "wrong_row":
                    crossed = self._foreign_terminal_predecessor(
                        context, generation=1
                    )
                    self._retarget_applied_predecessor(context, crossed)
                else:
                    crossed = self._foreign_terminal_predecessor(
                        context,
                        generation=context["applyResult"]["rowGeneration"],
                    )
                    self._retarget_applied_predecessor(context, crossed)

                if case in {"wrong_row", "non_lower_generation"}:
                    selected = self._applied_authority(context)[2]
                    candidates = [
                        item
                        for item in self._documents(
                            context, "rowOwnerSettlements"
                        ).values()
                        if item["settlementHash"]
                        == selected["supersededEffectiveSettlementHash"]
                    ]
                    self.assertEqual(1, len(candidates))
                    self.assertEqual(
                        candidates[0],
                        self.module.validate_owner_settlement_document(
                            document=candidates[0]
                        ),
                        "crossed predecessor must be self-valid before release",
                    )

                self._assert_integrity_rejected(context)

    def test_release_rejects_wrong_predecessor_generation_hash_or_claim(
        self,
    ):
        for case in ("wrong_generation_hash", "wrong_claim"):
            with self.subTest(case=case):
                context = self._seed_release(prior_owner="terminal")
                prior = self._prior_authority(context)
                if case == "wrong_generation_hash":
                    crossed = self.module.build_owner_generation_document(
                        claim_set_document=prior["claim"],
                        row_id=context["rowId"],
                        generation=prior["generation"]["generation"],
                        predecessor_head_hash="f" * 64,
                        predecessor_settlement_hash=prior["generation"][
                            "predecessorSettlementHash"
                        ],
                        lease_epoch=1,
                        first_fencing_token=prior["generation"][
                            "firstFencingToken"
                        ],
                        created_at=prior["generation"]["createdAt"],
                    )
                    self.assertNotEqual(
                        prior["settlement"]["generationHash"],
                        crossed["generationHash"],
                    )
                    self.assertEqual(
                        crossed,
                        self.module.validate_owner_generation_document(
                            document=crossed
                        ),
                    )
                    self._replace(
                        context,
                        "rowOwnerGenerations",
                        prior["generationId"],
                        crossed,
                    )
                else:
                    crossed_link = deepcopy(
                        prior["claim"]["authorityLink"]
                    )
                    crossed_link["workKey"] = "e" * 64
                    crossed_link["authorityLinkHash"] = self.module.domain_hash(
                        self.module.B1_AUTHORITY_LINK_HASH_DOMAIN,
                        {
                            key: deepcopy(value)
                            for key, value in crossed_link.items()
                            if key != "authorityLinkHash"
                        },
                        user_scope_hash=context["scope"],
                    )
                    crossed = self.module.build_claim_set_document(
                        user_scope_hash=context["scope"],
                        authority_origin="b1_source",
                        authority_link=crossed_link,
                        operator_action_document=None,
                        fanout_id=None,
                        row_ids=[context["rowId"]],
                        primary_row_id=context["rowId"],
                        planned_writes=3,
                        outcome="accepted",
                        row_decisions=deepcopy(
                            prior["claim"]["rowDecisions"]
                        ),
                        created_at=prior["claim"]["createdAt"],
                    )
                    self.assertNotEqual(
                        prior["claim"]["claimSetHash"],
                        crossed["claimSetHash"],
                    )
                    self.assertEqual(
                        crossed,
                        self.module.validate_claim_set_document(
                            document=crossed
                        ),
                    )
                    self._replace(
                        context,
                        "rowClaimSets",
                        prior["claimId"],
                        crossed,
                    )

                self._assert_integrity_rejected(context)

    def test_release_rejects_unrestorable_predecessor_semantics(self):
        for case in (
            "dominated",
            "contact_optout",
            "nonsettled",
            "non_lower_priority",
        ):
            with self.subTest(case=case):
                context = self._seed_release(prior_owner="terminal")
                prior = self._prior_authority(context)
                if case == "dominated":
                    _claim, current_generation, _settlement, _generation_id = (
                        self._applied_authority(context)
                    )
                    corrupted = self.module.build_owner_settlement_document(
                        generation_document=prior["generation"],
                        claim_set_document=prior["claim"],
                        fencing_token=prior["settlement"]["fencingToken"],
                        outcome="dominated",
                        settled_at=prior["settlement"]["settledAt"],
                        dominant_generation_hash=current_generation[
                            "generationHash"
                        ],
                    )
                    self._assert_owner_lineage_self_valid(
                        prior["claim"],
                        prior["generation"],
                        corrupted,
                    )
                    self._replace(
                        context,
                        "rowOwnerSettlements",
                        prior["settlementId"],
                        corrupted,
                    )
                    self._retarget_applied_predecessor(context, corrupted)
                elif case in {"contact_optout", "non_lower_priority"}:
                    corrupted = self._install_self_valid_contact_predecessor(
                        context
                    )
                    self.assertEqual(
                        corrupted,
                        self.module.validate_owner_settlement_document(
                            document=corrupted
                        ),
                    )
                elif case == "nonsettled":
                    self.assertEqual(
                        prior["generation"],
                        self.module.validate_owner_generation_document(
                            document=prior["generation"]
                        ),
                    )
                    self._replace(
                        context,
                        "rowOwnerSettlements",
                        prior["settlementId"],
                        prior["generation"],
                    )

                self._assert_integrity_rejected(context)

    def test_release_rejects_stale_valid_row_head_without_writes(self):
        context = self._seed_release(prior_owner="terminal")
        stale_head = deepcopy(context["preApplyHead"])
        self.assertEqual(
            stale_head,
            self.module.validate_row_authority_head(document=stale_head),
        )
        context["transition"].fixture._row_references(
            context["store"],
            context["rowId"],
        )[1].set(stale_head, merge=False)
        context["store"].events.clear()

        self._assert_integrity_rejected(context)

    def test_release_partial_readback_is_ambiguous_without_repair(self):
        context = self._seed_release(prior_owner="human_decision")
        before = deepcopy(context["store"].data)

        def partial_executor(transaction, callback):
            transaction._begin()
            callback(transaction)
            operation, reference, payload, merge = transaction._operations[0]
            transaction._rollback()
            if operation == "create":
                reference.create(payload)
            elif operation == "set":
                reference.set(payload, merge=merge)
            elif operation == "update":
                reference.update(payload)
            elif operation == "delete":
                reference.delete()
            else:
                raise AssertionError(
                    f"unsupported partial release operation: {operation}"
                )
            raise RuntimeError("partial contact fan-out release")

        context["store"].events.clear()
        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._process(context, executor=partial_executor)

        after = context["store"].data
        changed_paths = {
            path
            for path in set(before) | set(after)
            if before.get(path) != after.get(path)
        }
        self.assertEqual(1, len(changed_paths))
        self.assertEqual(1, len(self._writes(context["store"])))
        self.assertIn(("commit_applied", 1), context["store"].events)

    def test_release_scans_ordered_first_missing_and_only_advances_full_pages(self):
        with self.subTest(scan="arbitrary-later-row"):
            context = self._seed_release_scan(2, result_count=0)
            before = deepcopy(context["store"].data)

            with self.assertRaises(self.module.RowAuthorityConflict):
                self._process_with_queries(context)

            self.assertEqual(before, context["store"].data)
            self.assertEqual([], self._writes(context["store"]))
            row_head_paths = {
                context["transition"].fixture._row_references(
                    context["store"],
                    row_id,
                )[1].path
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
            context = self._seed_release_scan(33, result_count=32)
            before_fanout = deepcopy(context["fanout"])
            before_results = deepcopy(
                self._documents(context, "contactOptOutFanoutResults")
            )

            processed, queries = self._process_with_queries(context)

            obligation_queries = [
                query
                for query in queries
                if query["collection"]
                == "contactOptOutFanoutObligations"
            ]
            self.assertEqual(1, len(obligation_queries))
            obligation_query = obligation_queries[0]
            self.assertEqual(
                (("fanoutId", "==", before_fanout["fanoutId"]),),
                obligation_query["filters"],
            )
            self.assertEqual(("rowId",), obligation_query["ordering"])
            self.assertEqual(
                ("ASCENDING",),
                obligation_query["directions"],
            )
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
                self._documents(context, "contactOptOutFanoutHeads")[
                    before_fanout["fanoutId"]
                ],
            )
            self.assertEqual(
                before_results,
                self._documents(context, "contactOptOutFanoutResults"),
            )
            self.assertEqual(1, len(self._writes(context["store"])))
            self.assertIn(("commit_applied", 1), context["store"].events)
            sentinel_head_path = (
                context["transition"].fixture._row_references(
                    context["store"],
                    context["rows"][32],
                )[1].path
            )
            self.assertNotIn(
                sentinel_head_path,
                [
                    event[1]
                    for event in context["store"].events
                    if event[0] == "get"
                ],
            )

    def test_release_rejects_orphan_future_result_not_reflected_by_head(self):
        context = self._seed_release_scan(2, result_count=0)
        self._materialize_scan_rows(context)
        first_obligation = context["obligations"][0]
        orphan = self.module.build_contact_fanout_result_document(
            user_scope_hash=context["scope"],
            fanout_id=context["fanout"]["fanoutId"],
            row_id=first_obligation["rowId"],
            obligation_hash=first_obligation[
                "contactFanoutObligationHash"
            ],
            outcome="release",
            disposition="noop",
            reason_code="row_optout_not_applied",
            observed_row_head_hash="a" * 64,
            claim_request_id=None,
            claim_set_hash=None,
            row_generation=None,
            row_settlement_hash=None,
            released_row_generation=None,
            released_row_settlement_hash=None,
            restored_effective_generation=None,
            restored_effective_settlement_hash=None,
            created_at="2026-08-04T12:06:25.000000Z",
        )
        self._reference(
            context,
            "contactOptOutFanoutResults",
            f"{orphan['fanoutId']}--{orphan['rowId']}",
        ).create(orphan)
        before = deepcopy(context["store"].data)
        context["store"].events.clear()

        with self.assertRaises(self.module.RowAuthorityError):
            self._process(context)

        self.assertEqual(before, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))

    def test_release_rejects_missing_or_excess_obligation_cardinality(self):
        with self.subTest(cardinality="missing"):
            context = self._seed_release_scan(2, result_count=0)
            self._materialize_scan_rows(context)
            self._delete(
                context,
                "contactOptOutFanoutObligations",
                (
                    f"{context['fanout']['fanoutId']}--"
                    f"{context['rows'][1]}"
                ),
            )
            context["rowId"] = context["rows"][0]
            before = deepcopy(context["store"].data)
            with self.assertRaises(self.module.RowAuthorityError):
                self._process(context)
            self.assertEqual(before, context["store"].data)
            self.assertEqual([], self._writes(context["store"]))

        with self.subTest(cardinality="excess"):
            context = self._seed_release_scan(2, result_count=0)
            self._materialize_scan_rows(context)
            narrowed = self.discovery._fanout(
                context["fanout"],
                obligation_count=1,
            )
            self.discovery._store_fanout(context, narrowed)
            context["fanout"] = narrowed
            context["rowId"] = context["rows"][0]
            context["store"].events.clear()
            before = deepcopy(context["store"].data)
            with self.assertRaises(self.module.RowAuthorityError):
                self._process(context)
            self.assertEqual(before, context["store"].data)
            self.assertEqual([], self._writes(context["store"]))

    def test_release_rejects_target_obligation_outside_frozen_time_bounds(
        self,
    ):
        cases = (
            ("before-edge", "2026-08-04T12:00:00.000000Z"),
            ("after-fanout", "2026-08-04T12:06:25.000000Z"),
        )
        for label, created_at in cases:
            with self.subTest(bound=label):
                context = self._seed_release(prior_owner="human_decision")
                original = context["obligation"]
                replaced = self.module.build_contact_fanout_obligation_document(
                    user_scope_hash=original["userScopeHash"],
                    fanout_id=original["fanoutId"],
                    row_id=original["rowId"],
                    contact_row_edge_hash=original["contactRowEdgeHash"],
                    expected_contact_settlement_hash=original[
                        "expectedContactSettlementHash"
                    ],
                    outcome=original["outcome"],
                    created_at=created_at,
                )
                self._replace(
                    context,
                    "contactOptOutFanoutObligations",
                    f"{original['fanoutId']}--{original['rowId']}",
                    replaced,
                )
                before = deepcopy(context["store"].data)
                context["store"].events.clear()

                with self.assertRaises(self.module.RowAuthorityError):
                    self._process(context)

                self.assertEqual(before, context["store"].data)
                self.assertEqual([], self._writes(context["store"]))

    def test_release_cursor_advance_exact_retry_is_zero_write(self):
        context = self._seed_release_scan(33, result_count=32)
        original_expected = deepcopy(context["fanout"])
        first = self._process(context)
        self.assertEqual("cursor_advanced", first["disposition"])
        context["fanout"] = first["fanoutHead"]
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()

        replay = self._process(
            context,
            expected_fanout_head=original_expected,
        )

        self.assertEqual(first, replay)
        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))
        self.assertIn(("commit_applied", 0), context["store"].events)

    def test_release_result_exact_retry_after_later_row_progress_is_zero_write(
        self,
    ):
        context = self._seed_release_scan(2, result_count=0)
        self._materialize_scan_rows(context)
        original_expected = deepcopy(context["fanout"])

        context["rowId"] = context["rows"][0]
        first = self._process(
            context,
            expected_fanout_head=original_expected,
            processed_at="2026-08-04T12:06:30.000000Z",
        )
        context["fanout"] = first["fanoutHead"]
        context["rowId"] = context["rows"][1]
        second = self._process(
            context,
            processed_at="2026-08-04T12:06:40.000000Z",
        )
        context["fanout"] = second["fanoutHead"]
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()

        context["rowId"] = context["rows"][0]
        replay = self._process(
            context,
            expected_fanout_head=original_expected,
            processed_at="2026-08-04T12:06:50.000000Z",
        )

        self.assertEqual(first, replay)
        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))
        self.assertIn(("commit_applied", 0), context["store"].events)

    def test_release_result_retry_rejects_cursor_reset_without_progress(self):
        context = self._seed_release_scan(2, result_count=0)
        self._materialize_scan_rows(context)
        original_expected = deepcopy(context["fanout"])
        context["rowId"] = context["rows"][0]
        first = self._process(context)
        forged = self.discovery._fanout(
            first["fanoutHead"],
            state_revision=first["fanoutHead"]["stateRevision"] + 1,
            discovery_cursor_row_id=None,
            cursor_processed_count=0,
            updated_at="2026-08-04T12:06:40.000000Z",
        )
        self.assertEqual(
            first["fanoutHead"]["resultCount"],
            forged["resultCount"],
        )
        self.discovery._store_fanout(context, forged)
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()

        with self.assertRaises(self.module.RowAuthorityError):
            self._process(
                context,
                expected_fanout_head=original_expected,
                processed_at="2026-08-04T12:06:50.000000Z",
            )

        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))

    def test_release_result_retry_rejects_cursor_skip_without_row_result(self):
        context = self._seed_release_scan(3, result_count=0)
        self._materialize_scan_rows(context)
        original_expected = deepcopy(context["fanout"])
        context["rowId"] = context["rows"][0]
        first = self._process(context)
        skipped_row = context["rows"][1]
        skipped_result_id = (
            f"{context['fanout']['fanoutId']}--{skipped_row}"
        )
        self.assertNotIn(
            skipped_result_id,
            self._documents(context, "contactOptOutFanoutResults"),
        )
        forged = self.discovery._fanout(
            first["fanoutHead"],
            state_revision=first["fanoutHead"]["stateRevision"] + 1,
            discovery_cursor_row_id=skipped_row,
            cursor_processed_count=2,
            updated_at="2026-08-04T12:06:40.000000Z",
        )
        self.discovery._store_fanout(context, forged)
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()

        with self.assertRaises(self.module.RowAuthorityError):
            self._process(
                context,
                expected_fanout_head=original_expected,
                processed_at="2026-08-04T12:06:50.000000Z",
            )

        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))

    def test_release_result_retry_rejects_completion_predating_result(self):
        context = self._seed_release_scan(1, result_count=0)
        self._materialize_scan_rows(context)
        original_expected = deepcopy(context["fanout"])
        first = self._process(context)
        result = first["result"]
        completed = self.discovery._fanout(
            first["fanoutHead"],
            state_revision=first["fanoutHead"]["stateRevision"] + 1,
            state="complete",
            lease_owner_hash=None,
            lease_until=None,
            completion_binding_revision=first["fanoutHead"][
                "bindingRevision"
            ],
            completion_binding_head_hash=first["fanoutHead"][
                "bindingHeadHash"
            ],
            completion_binding_association_count=first["fanoutHead"][
                "bindingAssociationCount"
            ],
            completion_obligation_count=first["fanoutHead"][
                "obligationCount"
            ],
            completion_result_count=first["fanoutHead"]["resultCount"],
            completed_at="2026-08-04T12:06:25.000000Z",
            updated_at="2026-08-04T12:06:40.000000Z",
        )
        self.assertLess(completed["completedAt"], result["createdAt"])
        self.discovery._store_fanout(context, completed)
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()

        with self.assertRaises(self.module.RowAuthorityError):
            self._process(
                context,
                expected_fanout_head=original_expected,
                processed_at="2026-08-04T12:06:50.000000Z",
            )

        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))

    def test_release_result_retry_rejects_decreasing_future_result_times(self):
        context = self._seed_release_scan(2, result_count=0)
        self._materialize_scan_rows(context)
        original_expected = deepcopy(context["fanout"])
        row_heads = self._documents(context, "rowAuthorityHeads")
        created_times = (
            "2026-08-04T12:06:40.000000Z",
            "2026-08-04T12:06:30.000000Z",
        )
        results = []
        for obligation, created_at in zip(
            context["obligations"],
            created_times,
            strict=True,
        ):
            result = self.module.build_contact_fanout_result_document(
                user_scope_hash=context["scope"],
                fanout_id=context["fanout"]["fanoutId"],
                row_id=obligation["rowId"],
                obligation_hash=obligation[
                    "contactFanoutObligationHash"
                ],
                outcome="release",
                disposition="noop",
                reason_code="row_optout_not_applied",
                observed_row_head_hash=row_heads[obligation["rowId"]][
                    "headHash"
                ],
                claim_request_id=None,
                claim_set_hash=None,
                row_generation=None,
                row_settlement_hash=None,
                released_row_generation=None,
                released_row_settlement_hash=None,
                restored_effective_generation=None,
                restored_effective_settlement_hash=None,
                created_at=created_at,
            )
            self._reference(
                context,
                "contactOptOutFanoutResults",
                f"{result['fanoutId']}--{result['rowId']}",
            ).create(result)
            results.append(result)
        self.assertLess(results[1]["createdAt"], results[0]["createdAt"])
        forged = self.discovery._fanout(
            original_expected,
            state_revision=original_expected["stateRevision"] + 2,
            result_count=2,
            updated_at="2026-08-04T12:06:50.000000Z",
        )
        self.discovery._store_fanout(context, forged)
        context["rowId"] = context["rows"][0]
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()

        with self.assertRaises(self.module.RowAuthorityError):
            self._process(
                context,
                expected_fanout_head=original_expected,
                processed_at="2026-08-04T12:07:00.000000Z",
            )

        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))

    def test_release_result_retry_after_expected_lease_expiry_is_zero_write(
        self,
    ):
        context = self._seed_release(prior_owner="human_decision")
        original_expected = deepcopy(context["fanout"])
        first = self._process(context)
        context["fanout"] = first["fanoutHead"]
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()

        retry = self._process(
            context,
            expected_fanout_head=original_expected,
            processed_at="2026-08-04T12:10:01.000000Z",
        )

        self.assertLess(
            original_expected["leaseUntil"],
            "2026-08-04T12:10:01.000000Z",
        )
        self.assertEqual(first, retry)
        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))
        self.assertIn(("commit_applied", 0), context["store"].events)

    def test_release_cursor_retry_after_later_row_processing_is_exact(self):
        context = self._seed_release_scan(33, result_count=32)
        self._materialize_scan_rows(context)
        original_expected = deepcopy(context["fanout"])
        first = self._process(context)
        self.assertEqual("cursor_advanced", first["disposition"])
        context["fanout"] = first["fanoutHead"]
        later = self._process(
            context,
            processed_at="2026-08-04T12:06:40.000000Z",
        )
        self.assertEqual("noop", later["disposition"])
        context["fanout"] = later["fanoutHead"]
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()

        retry = self._process(
            context,
            expected_fanout_head=original_expected,
            processed_at=self.processed_at,
        )

        self.assertEqual(first, retry)
        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))
        self.assertIn(("commit_applied", 0), context["store"].events)

    def test_release_same_obligation_two_worker_cas_converges_exactly_once(self):
        context = self._seed_release(prior_owner="human_decision")
        context["store"].before_commit_barrier = Barrier(2)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self._process, context)
                for _worker in range(2)
            ]
            results = [future.result(timeout=10) for future in futures]

        self.assertEqual(results[0], results[1])
        self.assertEqual("restore", results[0]["disposition"])
        self.assertEqual(self._result(context), results[0]["result"])
        self.assertEqual(3, len(self._writes(context["store"])))
        self.assertEqual(
            1,
            context["store"].events.count(("commit_applied", 3)),
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

    def test_already_restored_cannot_be_a_first_write_result(self):
        context = self._seed_release(prior_owner="human_decision")

        first = self._process(context)

        result = first["result"]
        self.assertEqual("restore", result["disposition"])
        self.assertEqual("exact_predecessor", result["reasonCode"])
        self.assertNotEqual("already_restored", result["reasonCode"])
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self.module.build_contact_fanout_result_document(
                user_scope_hash=context["scope"],
                fanout_id=context["fanout"]["fanoutId"],
                row_id=context["rowId"],
                obligation_hash=context["obligation"][
                    "contactFanoutObligationHash"
                ],
                outcome="release",
                disposition="restore",
                reason_code="already_restored",
                observed_row_head_hash=context["beforeReleaseHead"][
                    "headHash"
                ],
                claim_request_id=None,
                claim_set_hash=None,
                row_generation=None,
                row_settlement_hash=None,
                released_row_generation=result["releasedRowGeneration"],
                released_row_settlement_hash=result[
                    "releasedRowSettlementHash"
                ],
                restored_effective_generation=result[
                    "restoredEffectiveGeneration"
                ],
                restored_effective_settlement_hash=result[
                    "restoredEffectiveSettlementHash"
                ],
                created_at=self.processed_at,
            )
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()

        retry = self._process(context)

        self.assertEqual(first, retry)
        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))

    def test_partial_release_a_then_optout_and_release_b_restores_still_a_controlled_row(
        self,
    ):
        context = self._seed_release(prior_owner="human_decision")
        epoch_a_result = deepcopy(context["applyResult"])
        epoch_a_head = deepcopy(context["beforeReleaseHead"])
        prior = context["prior"]

        bundle, _link = context["transition"]._seed_bundle(
            context["store"],
            "source-release-worker-epoch-b",
            exact_hash=context["activeSettlement"]["exactIdentityHash"],
        )
        epoch_b = context["transition"]._record(
            context["store"],
            bundle,
            requested_at="2026-08-04T12:06:40.000000Z",
        )
        self.assertEqual("created", epoch_b["disposition"])
        self._lease_and_discover(
            context,
            epoch_b,
            leased_at="2026-08-04T12:06:50.000000Z",
            discovered_at="2026-08-04T12:07:00.000000Z",
        )
        epoch_b_apply = self.apply._process(
            context,
            processed_at="2026-08-04T12:07:10.000000Z",
        )
        self.assertEqual("dominated", epoch_b_apply["disposition"])
        self.assertEqual(epoch_a_head, self._row_head(context))
        context.update(
            {
                "activeSettlement": epoch_b["settlement"],
                "activeReceipt": epoch_b["transitionRequest"],
                "activeFanout": epoch_b_apply["fanoutHead"],
                "fanout": epoch_b_apply["fanoutHead"],
            }
        )

        release_b = self._release_transition(
            context,
            requested_at="2026-08-04T12:07:20.000000Z",
        )
        self._lease_and_discover(
            context,
            release_b,
            leased_at="2026-08-04T12:07:30.000000Z",
            discovered_at="2026-08-04T12:07:40.000000Z",
        )
        context["beforeReleaseHead"] = deepcopy(self._row_head(context))
        context["beforeFanout"] = deepcopy(context["fanout"])
        context["store"].events.clear()

        processed = self._process(
            context,
            processed_at="2026-08-04T12:07:50.000000Z",
        )

        result = self._result(context)
        head = self._row_head(context)
        self.assertEqual("restore", processed["disposition"])
        self.assertEqual("exact_predecessor", result["reasonCode"])
        self.assertEqual(
            epoch_a_result["rowGeneration"],
            result["releasedRowGeneration"],
        )
        self.assertEqual(
            epoch_a_result["rowSettlementHash"],
            result["releasedRowSettlementHash"],
        )
        self.assertEqual(
            prior["generation"]["generation"],
            result["restoredEffectiveGeneration"],
        )
        self.assertEqual(
            prior["settlement"]["settlementHash"],
            result["restoredEffectiveSettlementHash"],
        )
        self.assertEqual(
            prior["settlement"]["settlementHash"],
            head["effectiveSettlementHash"],
        )
        self.assertEqual(3, len(self._writes(context["store"])))
        self.assertIn(("commit_applied", 3), context["store"].events)

    def test_release_retry_returns_exact_result_without_already_restored(self):
        context = self._seed_release(prior_owner="human_decision")

        first = self._process(context)
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()
        replay = self._process(context)

        self.assertEqual(first, replay)
        self.assertEqual("restore", replay["disposition"])
        self.assertEqual("exact_predecessor", replay["result"]["reasonCode"])
        self.assertNotEqual("already_restored", replay["result"]["reasonCode"])
        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))
        self.assertIn(("commit_applied", 0), context["store"].events)

    def test_release_retry_after_fanout_certification_is_zero_write(self):
        context = self._seed_release(prior_owner="human_decision")
        original_expected = deepcopy(context["fanout"])
        first = self._process(context)
        context["fanout"] = first["fanoutHead"]
        certified = self.completion._certify(
            context,
            certified_at="2026-08-04T12:06:40.000000Z",
        )
        self.assertEqual("certification_complete", certified["disposition"])
        self.assertEqual("complete", certified["fanoutHead"]["state"])
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()

        retry = self._process(
            context,
            expected_fanout_head=original_expected,
            processed_at="2026-08-04T12:06:50.000000Z",
        )

        self.assertEqual(first["disposition"], retry["disposition"])
        self.assertEqual(first["result"], retry["result"])
        self.assertEqual(first["fanoutHead"], retry["fanoutHead"])
        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))
        self.assertIn(("commit_applied", 0), context["store"].events)

    def test_release_result_retry_after_nonterminal_late_association_is_exact(
        self,
    ):
        context = self._seed_release(prior_owner="human_decision")
        original_expected = deepcopy(context["fanout"])
        first = self._process(context)
        context["fanout"] = first["fanoutHead"]

        late_type = importlib.import_module(
            "tests.test_row_authority_contact_late_release"
        ).ContactLateReleaseAssociationTests
        late_type.setUpClass()
        late = late_type(methodName="runTest")
        late.setUp()
        late_row = _row_id(2)
        thread_id = "thread-release-replay-nonterminal-late"
        late._seed_prerequisites(
            context,
            row_id=late_row,
            thread_id=thread_id,
        )
        associated = late._associate(
            context,
            row_id=late_row,
            thread_id=thread_id,
        )
        self.assertEqual("created", associated["disposition"])
        context["fanout"] = late._current_fanout(
            context,
            first["fanoutHead"]["fanoutId"],
        )
        self.assertEqual("applying", context["fanout"]["state"])
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()

        retry = self._process(
            context,
            expected_fanout_head=original_expected,
            processed_at="2026-08-04T12:07:10.000000Z",
        )

        self.assertEqual(first, retry)
        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))
        self.assertIn(("commit_applied", 0), context["store"].events)

    def test_release_result_retry_after_complete_late_recertification_is_exact(
        self,
    ):
        context = self._seed_release(prior_owner="human_decision")
        original_expected = deepcopy(context["fanout"])
        first = self._process(context)
        context["fanout"] = first["fanoutHead"]
        certified = self.completion._certify(
            context,
            certified_at="2026-08-04T12:06:40.000000Z",
        )
        self.assertEqual("certification_complete", certified["disposition"])
        context["fanout"] = certified["fanoutHead"]

        late_type = importlib.import_module(
            "tests.test_row_authority_contact_late_release"
        ).ContactLateReleaseAssociationTests
        late_type.setUpClass()
        late = late_type(methodName="runTest")
        late.setUp()
        late_row = _row_id(2)
        thread_id = "thread-release-replay-complete-late"
        late._seed_prerequisites(
            context,
            row_id=late_row,
            thread_id=thread_id,
        )
        associated = late._associate(
            context,
            row_id=late_row,
            thread_id=thread_id,
        )
        self.assertEqual("created", associated["disposition"])
        context["fanout"] = late._current_fanout(
            context,
            first["fanoutHead"]["fanoutId"],
        )
        self.assertEqual("complete", context["fanout"]["state"])
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()

        retry = self._process(
            context,
            expected_fanout_head=original_expected,
            processed_at="2026-08-04T12:07:10.000000Z",
        )

        self.assertEqual(first, retry)
        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))
        self.assertIn(("commit_applied", 0), context["store"].events)

    def test_release_result_retry_after_later_valid_reoptout_is_exact(self):
        context = self._seed_release(prior_owner="human_decision")
        original_expected = deepcopy(context["fanout"])
        first = self._process(context)
        context["fanout"] = first["fanoutHead"]
        certified = self.completion._certify(
            context,
            certified_at="2026-08-04T12:06:40.000000Z",
        )
        self.assertEqual("certification_complete", certified["disposition"])
        context["fanout"] = certified["fanoutHead"]

        bundle, link = context["transition"]._seed_bundle(
            context["store"],
            "source-release-result-retry-next-active",
            exact_hash=context["activeSettlement"]["exactIdentityHash"],
        )
        self.assertEqual(
            context["canonicalHash"],
            link["canonicalMailboxIdentityHash"],
        )
        reoptout = context["transition"]._record(
            context["store"],
            bundle,
            requested_at="2026-08-04T12:06:50.000000Z",
        )
        self.assertEqual("created", reoptout["disposition"])
        self.assertEqual("active", reoptout["head"]["state"])
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()

        retry = self._process(
            context,
            expected_fanout_head=original_expected,
            processed_at="2026-08-04T12:07:00.000000Z",
        )

        self.assertEqual(first, retry)
        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))
        self.assertIn(("commit_applied", 0), context["store"].events)

    def test_release_result_retry_after_nonterminal_newer_epoch_is_exact(self):
        context = self._seed_release(prior_owner="human_decision")
        original_expected = deepcopy(context["fanout"])
        first = self._process(context)
        context["fanout"] = first["fanoutHead"]

        bundle, _link = context["transition"]._seed_bundle(
            context["store"],
            "source-release-replay-nonterminal-newer",
            exact_hash=context["activeSettlement"]["exactIdentityHash"],
        )
        reoptout = context["transition"]._record(
            context["store"],
            bundle,
            requested_at="2026-08-04T12:06:40.000000Z",
        )
        self.assertEqual("created", reoptout["disposition"])
        old_fanout = self._documents(
            context,
            "contactOptOutFanoutHeads",
        )[original_expected["fanoutId"]]
        self.assertEqual("superseding", old_fanout["state"])
        context["fanout"] = old_fanout
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()

        retry = self._process(
            context,
            expected_fanout_head=original_expected,
            processed_at="2026-08-04T12:06:50.000000Z",
        )

        self.assertEqual(first, retry)
        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))
        self.assertIn(("commit_applied", 0), context["store"].events)

    def test_release_result_retry_after_completed_supersession_is_exact(self):
        context = self._seed_release_scan(2, result_count=0)
        self._materialize_scan_rows(context)
        original_expected = deepcopy(context["fanout"])
        context["rowId"] = context["rows"][0]
        first = self._process(context)
        context["fanout"] = first["fanoutHead"]

        bundle, _link = context["transition"]._seed_bundle(
            context["store"],
            "source-release-replay-completed-supersession",
            exact_hash=context["receipt"]["exactIdentityHash"],
        )
        reoptout = context["transition"]._record(
            context["store"],
            bundle,
            requested_at="2026-08-04T12:06:40.000000Z",
        )
        self.assertEqual("created", reoptout["disposition"])
        old_fanout = self._documents(
            context,
            "contactOptOutFanoutHeads",
        )[original_expected["fanoutId"]]
        self.assertEqual("superseding", old_fanout["state"])
        authority = context["transition"]._authority(context["store"])
        supersession_owner = "e" * 64
        leased = authority.acquire_contact_fanout_lease(
            verified_user_id=context["transition"].fixture.user_id,
            fanout_id=old_fanout["fanoutId"],
            expected_fanout_head=old_fanout,
            lease_owner_hash=supersession_owner,
            lease_until="2026-08-04T12:12:00.000000Z",
            acquired_at="2026-08-04T12:06:50.000000Z",
        )["fanoutHead"]
        finished = authority.supersede_contact_fanout_page(
            verified_user_id=context["transition"].fixture.user_id,
            fanout_id=leased["fanoutId"],
            expected_fanout_head=leased,
            lease_owner_hash=supersession_owner,
            superseded_at="2026-08-04T12:06:55.000000Z",
        )
        self.assertEqual("supersession_complete", finished["disposition"])
        self.assertEqual("superseded", finished["fanoutHead"]["state"])
        other_result = self._documents(
            context,
            "contactOptOutFanoutResults",
        )[f"{old_fanout['fanoutId']}--{context['rows'][1]}"]
        self.assertEqual("superseded", other_result["disposition"])
        self.assertEqual("contact_head_advanced", other_result["reasonCode"])
        context["fanout"] = finished["fanoutHead"]
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()

        retry = self._process(
            context,
            expected_fanout_head=original_expected,
            processed_at="2026-08-04T12:07:00.000000Z",
        )

        self.assertEqual(first, retry)
        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))
        self.assertIn(("commit_applied", 0), context["store"].events)

    def test_release_result_retry_after_two_additional_contact_cycles_is_exact(
        self,
    ):
        context = self._seed_release(prior_owner="human_decision")
        original_expected = deepcopy(context["fanout"])
        first = self._process(context)
        context["fanout"] = first["fanoutHead"]
        certified = self.completion._certify(
            context,
            certified_at="2026-08-04T12:06:35.000000Z",
        )
        self.assertEqual("certification_complete", certified["disposition"])
        context["fanout"] = certified["fanoutHead"]

        first_bundle, _first_link = context["transition"]._seed_bundle(
            context["store"],
            "source-release-replay-deep-cycle-one",
            exact_hash=context["activeSettlement"]["exactIdentityHash"],
        )
        first_active = context["transition"]._record(
            context["store"],
            first_bundle,
            requested_at="2026-08-04T12:06:40.000000Z",
        )
        first_release = self.release._release(
            {
                "store": context["store"],
                "settlement": first_active["settlement"],
            },
            client_request_id="release-replay-deep-cycle-one",
            requested_at="2026-08-04T12:06:50.000000Z",
        )
        self.assertEqual("created", first_release["disposition"])
        second_bundle, _second_link = context["transition"]._seed_bundle(
            context["store"],
            "source-release-replay-deep-cycle-two",
            exact_hash=context["activeSettlement"]["exactIdentityHash"],
        )
        second_active = context["transition"]._record(
            context["store"],
            second_bundle,
            requested_at="2026-08-04T12:07:00.000000Z",
        )
        second_release = self.release._release(
            {
                "store": context["store"],
                "settlement": second_active["settlement"],
            },
            client_request_id="release-replay-deep-cycle-two",
            requested_at="2026-08-04T12:07:10.000000Z",
        )
        self.assertEqual("created", second_release["disposition"])
        self.assertEqual(6, second_release["head"]["latestGeneration"])
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()

        retry = self._process(
            context,
            expected_fanout_head=original_expected,
            processed_at="2026-08-04T12:07:20.000000Z",
        )

        self.assertEqual(first, retry)
        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))
        self.assertIn(("commit_applied", 0), context["store"].events)

    def test_release_result_retry_after_earlier_sorted_late_row_is_exact(self):
        context = self._seed_release(prior_owner="human_decision")
        original_expected = deepcopy(context["fanout"])
        original_row = context["rowId"]
        first = self._process(context)
        context["fanout"] = first["fanoutHead"]

        late_type = importlib.import_module(
            "tests.test_row_authority_contact_late_release"
        ).ContactLateReleaseAssociationTests
        late_type.setUpClass()
        late = late_type(methodName="runTest")
        late.setUp()
        earlier_row = _row_id(0)
        self.assertLess(earlier_row, original_row)
        thread_id = "thread-release-replay-earlier-late-row"
        late._seed_prerequisites(
            context,
            row_id=earlier_row,
            thread_id=thread_id,
        )
        associated = late._associate(
            context,
            row_id=earlier_row,
            thread_id=thread_id,
        )
        self.assertEqual("created", associated["disposition"])
        context["fanout"] = late._current_fanout(
            context,
            first["fanoutHead"]["fanoutId"],
        )
        context["rowId"] = original_row
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()

        retry = self._process(
            context,
            expected_fanout_head=original_expected,
            processed_at="2026-08-04T12:07:10.000000Z",
        )

        self.assertEqual(first, retry)
        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))
        self.assertIn(("commit_applied", 0), context["store"].events)

    def test_release_rejects_non_target_obligation_outside_frozen_times(self):
        cases = (
            ("before-fanout", "2026-08-04T12:05:59.000000Z"),
            ("after-expected-head", "2026-08-04T12:06:25.000000Z"),
        )
        for label, created_at in cases:
            with self.subTest(bound=label):
                context = self._seed_release_scan(2, result_count=0)
                self._materialize_scan_rows(context)
                context["rowId"] = context["rows"][0]
                non_target = context["obligations"][1]
                replaced = self.module.build_contact_fanout_obligation_document(
                    user_scope_hash=non_target["userScopeHash"],
                    fanout_id=non_target["fanoutId"],
                    row_id=non_target["rowId"],
                    contact_row_edge_hash=non_target[
                        "contactRowEdgeHash"
                    ],
                    expected_contact_settlement_hash=non_target[
                        "expectedContactSettlementHash"
                    ],
                    outcome=non_target["outcome"],
                    created_at=created_at,
                )
                self._replace(
                    context,
                    "contactOptOutFanoutObligations",
                    f"{non_target['fanoutId']}--{non_target['rowId']}",
                    replaced,
                )
                committed = deepcopy(context["store"].data)
                context["store"].events.clear()

                with self.assertRaises(self.module.RowAuthorityError):
                    self._process(context)

                self.assertEqual(committed, context["store"].data)
                self.assertEqual([], self._writes(context["store"]))

    def test_release_result_retries_after_public_location_advance_are_exact(
        self,
    ):
        cases = (
            ("restore", {"prior_owner": "human_decision"}, "restore"),
            ("noop", {"apply_outcome": "not_applied"}, "noop"),
        )
        for label, seed_arguments, expected_disposition in cases:
            with self.subTest(result=label):
                context = self._seed_release(**seed_arguments)
                original_expected = deepcopy(context["fanout"])
                row_id = context["rowId"]
                identity = self._documents(
                    context,
                    "rowIdentities",
                )[row_id]
                original_observation = self.module.build_row_observation(
                    spreadsheet_id=identity["spreadsheetId"],
                    marker_observation={
                        "rowId": row_id,
                        "sheetId": identity["sheetId"],
                        "providerRowIndex": 0,
                        "displayRowNumber": 1,
                        "metadataId": 1,
                    },
                    ordered_headers=("Email",),
                    ordered_cell_values=("row-0@example.test",),
                    user_scope_hash=context["scope"],
                )
                original_revision = (
                    self.module.build_row_location_revision_document(
                        identity_document=identity,
                        revision=1,
                        lifecycle="active",
                        observations=(original_observation,),
                        previous_revision_hash=None,
                        observed_at=identity["createdAt"],
                    )
                )
                self.assertEqual(
                    self._row_head(context)["currentLocationHash"],
                    original_revision["revisionHash"],
                )
                self._reference(
                    context,
                    "rowLocationRevisions",
                    f"{row_id}--1",
                ).create(original_revision)
                context["store"].events.clear()

                first = self._process(context)
                self.assertEqual(
                    expected_disposition,
                    first["disposition"],
                )
                context["fanout"] = first["fanoutHead"]
                expected_location_head = deepcopy(self._row_head(context))
                moved_observation = self.module.build_row_observation(
                    spreadsheet_id=identity["spreadsheetId"],
                    marker_observation={
                        "rowId": row_id,
                        "sheetId": identity["sheetId"],
                        "providerRowIndex": 5,
                        "displayRowNumber": 6,
                        "metadataId": 1,
                    },
                    ordered_headers=("Email",),
                    ordered_cell_values=("row-0@example.test",),
                    user_scope_hash=context["scope"],
                )
                advanced = context["transition"]._authority(
                    context["store"]
                ).advance_row_location(
                    verified_user_id=context["transition"].fixture.user_id,
                    row_id=row_id,
                    expected_head=expected_location_head,
                    observations=(moved_observation,),
                    lifecycle="active",
                    observed_at="2026-08-04T12:06:40.000000Z",
                )
                self.assertEqual("advanced", advanced["disposition"])
                self.assertEqual(
                    2,
                    advanced["authorityHead"]["currentLocationRevision"],
                )
                committed = deepcopy(context["store"].data)
                context["store"].events.clear()

                retry = self._process(
                    context,
                    expected_fanout_head=original_expected,
                    processed_at="2026-08-04T12:06:50.000000Z",
                )

                self.assertEqual(first, retry)
                self.assertEqual(
                    advanced["authorityHead"],
                    self._row_head(context),
                )
                self.assertEqual(committed, context["store"].data)
                self.assertEqual([], self._writes(context["store"]))
                self.assertIn(("commit_applied", 0), context["store"].events)

    def test_release_retry_after_source_link_head_advance_is_zero_write(self):
        context = self._seed_release(prior_owner="human_decision")
        original_expected = deepcopy(context["fanout"])
        first = self._process(context)
        claim, generation, settlement, generation_id = (
            self._applied_authority(context)
        )
        source_link = self.module.build_source_settlement_link_document(
            user_scope_hash=context["scope"],
            row_id=context["rowId"],
            generation=generation["generation"],
            generation_hash=generation["generationHash"],
            authority_link_hash=claim["authorityLinkHash"],
            b1_identity_hash="7" * 64,
            b1_final_ledger_evidence_hash="8" * 64,
            b1_settlement_revision=7,
            b1_settlement_hash="9" * 64,
            b2_settlement_hash=settlement["settlementHash"],
            linked_at="2026-08-04T12:06:40.000000Z",
        )
        self._reference(
            context,
            "rowSourceSettlementLinks",
            generation_id,
        ).create(source_link)
        linked_head = self.module._build_source_link_advanced_head(
            expected_head=self._row_head(context),
            source_link_document=source_link,
        )
        self._replace(
            context,
            "rowAuthorityHeads",
            context["rowId"],
            linked_head,
        )
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()

        retry = self._process(
            context,
            expected_fanout_head=original_expected,
            processed_at="2026-08-04T12:06:50.000000Z",
        )

        self.assertEqual(first["disposition"], retry["disposition"])
        self.assertEqual(first["result"], retry["result"])
        self.assertEqual(first["fanoutHead"], retry["fanoutHead"])
        self.assertEqual(linked_head, self._row_head(context))
        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))
        self.assertIn(("commit_applied", 0), context["store"].events)

    def test_release_retry_after_origin_supersession_finishes_is_zero_write(self):
        context = self._seed_release(prior_owner="human_decision")
        original_expected = deepcopy(context["fanout"])
        first = self._process(context)
        apply_fanout_id = context["activeFanout"]["fanoutId"]
        superseding = deepcopy(
            self._documents(context, "contactOptOutFanoutHeads")[
                apply_fanout_id
            ]
        )
        authority = context["transition"]._authority(context["store"])
        supersession_owner = "e" * 64
        leased = authority.acquire_contact_fanout_lease(
            verified_user_id=context["transition"].fixture.user_id,
            fanout_id=apply_fanout_id,
            expected_fanout_head=superseding,
            lease_owner_hash=supersession_owner,
            lease_until="2026-08-04T12:12:00.000000Z",
            acquired_at="2026-08-04T12:06:40.000000Z",
        )["fanoutHead"]
        finished = authority.supersede_contact_fanout_page(
            verified_user_id=context["transition"].fixture.user_id,
            fanout_id=apply_fanout_id,
            expected_fanout_head=leased,
            lease_owner_hash=supersession_owner,
            superseded_at="2026-08-04T12:06:42.000000Z",
        )
        self.assertEqual("supersession_complete", finished["disposition"])
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()

        replay = self._process(
            context,
            expected_fanout_head=original_expected,
            processed_at="2026-08-04T12:06:50.000000Z",
        )

        self.assertEqual(first, replay)
        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))
        self.assertIn(("commit_applied", 0), context["store"].events)

    def test_release_restore_and_row_not_applied_unknown_commits_read_back_exactly(
        self,
    ):
        cases = (
            (
                "restore",
                {"prior_owner": "human_decision"},
                "restore",
                "exact_predecessor",
                3,
            ),
            (
                "noop",
                {"apply_outcome": "not_applied"},
                "noop",
                "row_optout_not_applied",
                2,
            ),
        )
        for label, seed_arguments, disposition, reason_code, write_count in cases:
            with self.subTest(commit=label):
                context = self._seed_release(**seed_arguments)
                context["store"].apply_then_raise_next_commit = RuntimeError(
                    f"unknown release {label} commit outcome"
                )

                applied = self._process(context)

                result = self._result(context)
                self.assertEqual(disposition, applied["disposition"])
                self.assertEqual(reason_code, result["reasonCode"])
                self.assertEqual(result, applied["result"])
                self.assertEqual(
                    write_count,
                    len(self._writes(context["store"])),
                )
                self.assertIn(
                    ("commit_applied", write_count),
                    context["store"].events,
                )
                failure_index = context["store"].events.index(
                    ("commit_raised_after_apply",)
                )
                self.assertTrue(
                    any(
                        event[0] == "get"
                        for event in context["store"].events[
                            failure_index + 1 :
                        ]
                    ),
                    f"release {label} unknown commit must perform exact readback",
                )

    def test_release_noop_exact_replay_is_zero_write(self):
        for apply_outcome in ("dominated", "not_applied"):
            with self.subTest(apply_outcome=apply_outcome):
                context = self._seed_release(apply_outcome=apply_outcome)
                first = self._process(context)
                committed = deepcopy(context["store"].data)
                context["store"].events.clear()

                replay = self._process(context)

                self.assertEqual(first, replay)
                self.assertEqual("noop", replay["disposition"])
                self.assertEqual(
                    "row_optout_not_applied",
                    replay["result"]["reasonCode"],
                )
                self.assertEqual(committed, context["store"].data)
                self.assertEqual([], self._writes(context["store"]))
                self.assertIn(
                    ("commit_applied", 0),
                    context["store"].events,
                )

    def test_release_accepts_apply_supersession_worker_finishing_first(self):
        context = self._seed_release(apply_outcome="not_applied")
        release_fanout = deepcopy(context["fanout"])
        apply_fanout = deepcopy(
            self._documents(context, "contactOptOutFanoutHeads")[
                context["activeFanout"]["fanoutId"]
            ]
        )
        self.assertEqual("apply", apply_fanout["outcome"])
        self.assertEqual("superseding", apply_fanout["state"])
        self.assertEqual(
            context["settlement"]["contactSettlementHash"],
            apply_fanout["supersedingContactSettlementHash"],
        )

        authority = context["transition"]._authority(context["store"])
        supersession_owner = "e" * 64
        leased = authority.acquire_contact_fanout_lease(
            verified_user_id=context["transition"].fixture.user_id,
            fanout_id=apply_fanout["fanoutId"],
            expected_fanout_head=apply_fanout,
            lease_owner_hash=supersession_owner,
            lease_until="2026-08-04T12:12:00.000000Z",
            acquired_at="2026-08-04T12:06:21.000000Z",
        )["fanoutHead"]
        superseded = authority.supersede_contact_fanout_page(
            verified_user_id=context["transition"].fixture.user_id,
            fanout_id=leased["fanoutId"],
            expected_fanout_head=leased,
            lease_owner_hash=supersession_owner,
            superseded_at="2026-08-04T12:06:22.000000Z",
        )
        self.assertEqual("supersession_complete", superseded["disposition"])
        self.assertEqual("superseded", superseded["fanoutHead"]["state"])
        self.assertEqual(1, len(superseded["results"]))
        apply_result = superseded["results"][0]
        self.assertEqual("apply", apply_result["outcome"])
        self.assertEqual("superseded", apply_result["disposition"])
        self.assertEqual("contact_head_advanced", apply_result["reasonCode"])

        context["fanout"] = release_fanout
        context["store"].events.clear()
        processed = self._process(context)

        self.assertEqual("noop", processed["disposition"])
        result = self._result(context)
        self.assertEqual("row_optout_not_applied", result["reasonCode"])
        self.assertEqual(context["beforeReleaseHead"], self._row_head(context))
        self.assertEqual(2, len(self._writes(context["store"])))

    def test_release_loses_safely_to_newer_contact_transition(self):
        with self.subTest(race="contact-head-advance"):
            context = self._seed_release(prior_owner="human_decision")
            stale_expected = deepcopy(context["fanout"])
            bundle, _link = context["transition"]._seed_bundle(
                context["store"],
                "source-release-worker-newer-contact",
                exact_hash=context["activeSettlement"]["exactIdentityHash"],
            )
            context["transition"]._record(
                context["store"],
                bundle,
                requested_at="2026-08-04T12:06:25.000000Z",
            )
            superseding = deepcopy(
                self._documents(context, "contactOptOutFanoutHeads")[
                    stale_expected["fanoutId"]
                ]
            )
            self.assertEqual("superseding", superseding["state"])
            before = deepcopy(context["store"].data)
            before_head = self._row_head(context)
            context["store"].events.clear()

            with self.assertRaises(self.module.RowAuthorityError):
                self._process(
                    context,
                    expected_fanout_head=stale_expected,
                )

            self.assertEqual(before, context["store"].data)
            self.assertEqual(before_head, self._row_head(context))
            self.assertEqual([], self._writes(context["store"]))
            self.assertEqual(
                superseding,
                self._documents(context, "contactOptOutFanoutHeads")[
                    stale_expected["fanoutId"]
                ],
            )

            authority = context["transition"]._authority(context["store"])
            supersession_owner = "e" * 64
            leased = authority.acquire_contact_fanout_lease(
                verified_user_id=context["transition"].fixture.user_id,
                fanout_id=superseding["fanoutId"],
                expected_fanout_head=superseding,
                lease_owner_hash=supersession_owner,
                lease_until="2026-08-04T12:12:00.000000Z",
                acquired_at="2026-08-04T12:06:26.000000Z",
            )["fanoutHead"]
            context["store"].events.clear()
            superseded = authority.supersede_contact_fanout_page(
                verified_user_id=context["transition"].fixture.user_id,
                fanout_id=leased["fanoutId"],
                expected_fanout_head=leased,
                lease_owner_hash=supersession_owner,
                superseded_at="2026-08-04T12:06:27.000000Z",
            )
            self.assertEqual("supersession_complete", superseded["disposition"])
            self.assertEqual("superseded", superseded["fanoutHead"]["state"])
            self.assertEqual(1, len(superseded["results"]))
            result = superseded["results"][0]
            expected_result = self.module.build_contact_fanout_result_document(
                user_scope_hash=context["scope"],
                fanout_id=stale_expected["fanoutId"],
                row_id=context["rowId"],
                obligation_hash=context["obligation"][
                    "contactFanoutObligationHash"
                ],
                outcome="release",
                disposition="superseded",
                reason_code="contact_head_advanced",
                observed_row_head_hash=before_head["headHash"],
                claim_request_id=None,
                claim_set_hash=None,
                row_generation=None,
                row_settlement_hash=None,
                released_row_generation=None,
                released_row_settlement_hash=None,
                restored_effective_generation=None,
                restored_effective_settlement_hash=None,
                created_at="2026-08-04T12:06:27.000000Z",
            )
            self.assertEqual(expected_result, result)
            self.assertEqual(before_head, self._row_head(context))

        with self.subTest(race="fanout-fence-advance"):
            context = self._seed_release(prior_owner="human_decision")
            stale_expected = deepcopy(context["fanout"])
            advanced = self.discovery._fanout(
                stale_expected,
                state_revision=stale_expected["stateRevision"] + 1,
                fencing_token=stale_expected["fencingToken"] + 1,
                lease_until="2026-08-04T12:11:00.000000Z",
                updated_at="2026-08-04T12:06:25.000000Z",
            )
            self.discovery._store_fanout(context, advanced)
            before = deepcopy(context["store"].data)
            before_head = self._row_head(context)

            with self.assertRaises(self.module.RowAuthorityError):
                self._process(
                    context,
                    expected_fanout_head=stale_expected,
                )

            self.assertEqual(before, context["store"].data)
            self.assertEqual(before_head, self._row_head(context))
            self.assertEqual([], self._writes(context["store"]))

    def test_release_result_uses_before_image_and_exact_nullable_matrix(self):
        context = self._seed_release(prior_owner="human_decision")
        original = deepcopy(context["beforeReleaseHead"])
        advanced = deepcopy(original)
        advanced.update(
            {
                "stateRevision": original["stateRevision"] + 1,
                "currentLocationRevision": original[
                    "currentLocationRevision"
                ]
                + 1,
                "currentLocationHash": "4" * 64,
                "updatedAt": "2026-08-04T12:06:25.000000Z",
            }
        )
        advanced = context["transition"].fixture._rehash_head(advanced)
        context["transition"].fixture._row_references(
            context["store"],
            context["rowId"],
        )[1].set(advanced, merge=False)
        context["beforeReleaseHead"] = advanced
        context["store"].events.clear()

        self._process(context)

        result = self._result(context)
        prior = context["prior"]
        rebuilt = self.module.build_contact_fanout_result_document(
            user_scope_hash=context["scope"],
            fanout_id=context["fanout"]["fanoutId"],
            row_id=context["rowId"],
            obligation_hash=context["obligation"][
                "contactFanoutObligationHash"
            ],
            outcome="release",
            disposition="restore",
            reason_code="exact_predecessor",
            observed_row_head_hash=advanced["headHash"],
            claim_request_id=None,
            claim_set_hash=None,
            row_generation=None,
            row_settlement_hash=None,
            released_row_generation=context["applyResult"]["rowGeneration"],
            released_row_settlement_hash=context["applyResult"][
                "rowSettlementHash"
            ],
            restored_effective_generation=prior["generation"]["generation"],
            restored_effective_settlement_hash=prior["settlement"][
                "settlementHash"
            ],
            created_at=self.processed_at,
        )
        self.assertEqual(rebuilt, result)
        self.assertNotEqual(original["headHash"], result["observedRowHeadHash"])
        self.assertNotEqual(
            result["observedRowHeadHash"],
            self._row_head(context)["headHash"],
        )

    def test_b1_source_link_before_contact_apply_and_release_preserves_exact_authority(
        self,
    ):
        context = self.apply._seed_apply(prior_owner="terminal")
        self._seed_source_thread_binding(context)
        linker = self._source_linker(context)
        prior = context["prior"]
        prior_link = prior["claim"]["authorityLink"]
        bundle = self.ownership.RowOwnershipContractTests._b1_bundle(
            context["transition"].fixture,
            owner_kind="terminal",
            source_id=prior_link["canonicalSourceId"],
        )
        rebuilt_link = self.module.build_b1_authority_link(
            user_scope_hash=context["scope"],
            source_identity_document=bundle["identity"],
            source_classification_document=bundle["classification"],
            source_owner_document=bundle["owner"],
            source_ledger_document=bundle["ledger"],
            work_key=bundle["work_key"],
        )
        self.assertEqual(prior_link, rebuilt_link)
        self._store_b1_bundle(context, bundle)
        settled_bundle = linker._seed_b1_settlement(
            context["store"], bundle
        )
        link_state = {
            "store": context["store"],
            "bundle": settled_bundle,
            "claim": prior["claim"],
            "generation": prior["generation"],
            "b2Settlement": prior["settlement"],
            "head": self._row_head(context),
        }
        b1_before = self._b1_snapshot(context)
        observed_writes = []
        context["store"].events.clear()

        linked = linker._link(link_state)
        self._capture_non_b1_writes(context, observed_writes)
        expected_source_link = linker._expected_link(link_state)
        self.assertEqual("linked", linked["disposition"])
        self.assertEqual(
            expected_source_link,
            linked["sourceSettlementLink"],
        )
        self.assertEqual(
            expected_source_link["sourceSettlementLinkHash"],
            linked["head"]["latestSourceSettlementLinkHash"],
        )

        context.update(
            {
                "activeSettlement": context["settlement"],
                "activeReceipt": context["receipt"],
                "activeFanout": context["fanout"],
                "preApplyHead": deepcopy(linked["head"]),
            }
        )
        applied = self.apply._process(context)
        self.assertEqual("applied", applied["disposition"])
        self._capture_non_b1_writes(context, observed_writes)
        context.update(
            {
                "applyResult": applied["result"],
                "fanout": applied["fanoutHead"],
                "rowHead": self._row_head(context),
            }
        )
        claim, _generation, _settlement, _generation_id = (
            self._applied_authority(context)
        )
        contact_link = context["activeSettlement"]["authorityLink"]
        self.assertEqual(
            contact_link,
            self.module.validate_b1_authority_link(
                authority_link=contact_link,
                user_scope_hash=context["scope"],
            ),
        )
        self.assertEqual(contact_link, claim["authorityLink"])
        self.assertEqual(
            expected_source_link["sourceSettlementLinkHash"],
            context["rowHead"]["latestSourceSettlementLinkHash"],
        )

        released = self._complete_actual_release(
            context, observed_writes
        )
        self.assertEqual("restore", released["disposition"])
        final_head = self._row_head(context)
        self.assertEqual(
            (
                prior["generation"]["generation"],
                prior["generation"]["generationHash"],
                prior["settlement"]["settlementHash"],
            ),
            (
                final_head["effectiveOwnerGeneration"],
                final_head["effectiveOwnerGenerationHash"],
                final_head["effectiveSettlementHash"],
            ),
        )
        self.assertEqual(
            expected_source_link["sourceSettlementLinkHash"],
            final_head["latestSourceSettlementLinkHash"],
        )
        self.assertEqual(
            {f"{context['rowId']}--1": expected_source_link},
            self._documents(context, "rowSourceSettlementLinks"),
        )
        self.assertEqual(b1_before, self._b1_snapshot(context))
        self.assertTrue(observed_writes)

    def test_contact_source_settlement_links_before_or_after_actual_release_without_reactivation(
        self,
    ):
        for timing in ("before_release", "after_release"):
            with self.subTest(timing=timing):
                context = self.apply._seed_apply()
                self._seed_source_thread_binding(context)
                linker = self._source_linker(context)
                context.update(
                    {
                        "activeSettlement": context["settlement"],
                        "activeReceipt": context["receipt"],
                        "activeFanout": context["fanout"],
                        "preApplyHead": deepcopy(context["rowHead"]),
                    }
                )
                source_link = context["activeSettlement"]["authorityLink"]
                bundle = self._stored_b1_bundle(context, source_link)
                b1_before_apply = self._b1_snapshot(context)
                observed_writes = []
                context["store"].events.clear()

                applied = self.apply._process(context)
                self.assertEqual("applied", applied["disposition"])
                self._capture_non_b1_writes(context, observed_writes)
                context.update(
                    {
                        "applyResult": applied["result"],
                        "fanout": applied["fanoutHead"],
                        "rowHead": self._row_head(context),
                    }
                )
                self.assertEqual(b1_before_apply, self._b1_snapshot(context))
                claim, generation, b2_settlement, _generation_id = (
                    self._applied_authority(context)
                )
                self.assertEqual(
                    source_link,
                    self.module.validate_b1_authority_link(
                        authority_link=source_link,
                        user_scope_hash=context["scope"],
                    ),
                )
                self.assertEqual(source_link, claim["authorityLink"])
                link_state = {
                    "store": context["store"],
                    "bundle": bundle,
                    "claim": claim,
                    "generation": generation,
                    "b2Settlement": b2_settlement,
                    "head": context["rowHead"],
                }
                before_unsettled_link = deepcopy(context["store"].data)
                with self.assertRaises(self.module.RowAuthorityAmbiguous):
                    linker._link(
                        link_state,
                        linked_at="2026-08-04T12:05:46.000000Z",
                    )
                self._capture_non_b1_writes(context, observed_writes)
                self.assertEqual(
                    before_unsettled_link,
                    context["store"].data,
                )

                if timing == "after_release":
                    released = self._complete_actual_release(
                        context, observed_writes
                    )
                    self.assertEqual("restore", released["disposition"])
                    self.assertIsNone(
                        self._row_head(context)["effectiveOwnerGeneration"]
                    )

                settled_at = (
                    datetime(
                        2026,
                        8,
                        4,
                        12,
                        5,
                        50,
                        tzinfo=timezone.utc,
                    )
                    if timing == "before_release"
                    else datetime(
                        2026,
                        8,
                        4,
                        12,
                        6,
                        35,
                        tzinfo=timezone.utc,
                    )
                )
                settled_bundle = linker._seed_b1_settlement(
                    context["store"],
                    bundle,
                    settled_at=settled_at,
                )
                link_state["bundle"] = settled_bundle
                b1_after_settlement = self._b1_snapshot(context)
                context["store"].events.clear()

                before_link = deepcopy(self._row_head(context))
                linked_at = (
                    "2026-08-04T12:05:55.000000Z"
                    if timing == "before_release"
                    else "2026-08-04T12:06:40.000000Z"
                )
                linked = linker._link(
                    link_state,
                    linked_at=linked_at,
                )
                self._capture_non_b1_writes(context, observed_writes)
                expected_link = linker._expected_link(
                    link_state,
                    linked_at=linked_at,
                )
                expected_head = self.module._build_source_link_advanced_head(
                    expected_head=before_link,
                    source_link_document=expected_link,
                )
                self.assertEqual("linked", linked["disposition"])
                self.assertEqual(expected_link, linked["sourceSettlementLink"])
                self.assertEqual(expected_head, linked["head"])
                for field in (
                    "state",
                    "effectiveOwnerGeneration",
                    "effectiveOwnerGenerationHash",
                    "effectiveOwnerKind",
                    "effectivePriority",
                    "effectiveSettlementHash",
                    "latestSettlementHash",
                    "fencingToken",
                ):
                    self.assertEqual(before_link[field], linked["head"][field])
                context["rowHead"] = linked["head"]

                if timing == "before_release":
                    released = self._complete_actual_release(
                        context, observed_writes
                    )
                    self.assertEqual("restore", released["disposition"])

                final_head = self._row_head(context)
                self.assertEqual(
                    (
                        "clear",
                        None,
                        None,
                        None,
                        None,
                        None,
                    ),
                    (
                        final_head["state"],
                        final_head["effectiveOwnerGeneration"],
                        final_head["effectiveOwnerGenerationHash"],
                        final_head["effectiveOwnerKind"],
                        final_head["effectivePriority"],
                        final_head["effectiveSettlementHash"],
                    ),
                )
                self.assertEqual(
                    expected_link["sourceSettlementLinkHash"],
                    final_head["latestSourceSettlementLinkHash"],
                )
                self.assertEqual(
                    {f"{context['rowId']}--1": expected_link},
                    self._documents(context, "rowSourceSettlementLinks"),
                )
                stored_claim = self._documents(context, "rowClaimSets")[
                    claim["requestId"]
                ]
                self.assertEqual(source_link, stored_claim["authorityLink"])
                self.assertEqual(
                    b1_after_settlement,
                    self._b1_snapshot(context),
                )
                self.assertTrue(observed_writes)

    def test_already_active_source_receipt_that_never_owned_row_cannot_mint_source_link(
        self,
    ):
        context = self.apply._seed_apply()
        self._seed_source_thread_binding(context)
        linker = self._source_linker(context)
        active_link = context["settlement"]["authorityLink"]
        other_bundle, other_link = context["transition"]._seed_bundle(
            context["store"],
            "source-step6-already-active",
            exact_hash="6" * 64,
        )
        observed_writes = []
        context["store"].events.clear()

        already_active = context["transition"]._record(
            context["store"],
            other_bundle,
            requested_at="2026-08-04T12:05:40.000000Z",
        )
        self.assertEqual("already_active", already_active["disposition"])
        self.assertEqual(
            other_link["authorityLinkHash"],
            already_active["transitionRequest"]["authorityLinkHash"],
        )
        self._capture_non_b1_writes(context, observed_writes)
        linker._seed_b1_settlement(
            context["store"],
            other_bundle,
            settled_at=datetime(
                2026,
                8,
                4,
                12,
                5,
                41,
                tzinfo=timezone.utc,
            ),
        )
        b1_after_settlement = self._b1_snapshot(context)
        context["store"].events.clear()

        context.update(
            {
                "activeSettlement": context["settlement"],
                "activeReceipt": context["receipt"],
                "activeFanout": context["fanout"],
                "preApplyHead": deepcopy(context["rowHead"]),
            }
        )
        applied = self.apply._process(context)
        self.assertEqual("applied", applied["disposition"])
        self._capture_non_b1_writes(context, observed_writes)
        context.update(
            {
                "applyResult": applied["result"],
                "fanout": applied["fanoutHead"],
                "rowHead": self._row_head(context),
            }
        )
        claim, generation, b2_settlement, _generation_id = (
            self._applied_authority(context)
        )
        self.assertEqual(active_link, claim["authorityLink"])
        self.assertNotEqual(
            other_link["authorityLinkHash"],
            claim["authorityLinkHash"],
        )
        self.assertFalse(
            any(
                document["authorityLinkHash"]
                == other_link["authorityLinkHash"]
                for document in self._documents(
                    context, "rowClaimSets"
                ).values()
            )
        )
        before_link = deepcopy(context["store"].data)
        link_state = {
            "store": context["store"],
            "generation": generation,
            "claim": claim,
            "b2Settlement": b2_settlement,
        }
        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            linker._link(
                link_state,
                linked_at="2026-08-04T12:05:55.000000Z",
            )
        self._capture_non_b1_writes(context, observed_writes)

        self.assertEqual(before_link, context["store"].data)
        self.assertEqual(
            {},
            self._documents(context, "rowSourceSettlementLinks"),
        )
        self.assertEqual(b1_after_settlement, self._b1_snapshot(context))
        self.assertTrue(observed_writes)
