"""RED contracts for retry-safe authenticated contact release transitions."""

from __future__ import annotations

import importlib
import inspect
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from unittest.mock import patch

from tests.test_row_authority_contact_transitions import ContactTransitionTests


class ContactReleaseTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")
        cls.fakes = importlib.import_module("tests.row_authority_fakes")
        ContactTransitionTests.setUpClass()

    def setUp(self):
        self.transition = ContactTransitionTests(methodName="runTest")
        self.transition.setUp()
        self.actor = "d" * 64
        self.other_actor = "c" * 64
        self.client_request_id = "release-request-one"
        self.release_at = "2026-08-04T12:00:05.000000Z"
        self.progress_at = "2026-08-04T12:00:06.000000Z"
        self.reopt_at = "2026-08-04T12:00:08.000000Z"
        self.second_release_at = "2026-08-04T12:00:10.000000Z"

    def _method(self):
        self.assertTrue(
            hasattr(
                self.module.RowAuthorityStore,
                "record_authenticated_contact_release",
            ),
            "RowAuthorityStore.record_authenticated_contact_release is missing",
        )
        return self.module.RowAuthorityStore.record_authenticated_contact_release

    def _authority(self, store, *, executor=None):
        return self.module.RowAuthorityStore(
            store,
            transaction_executor=(
                executor or self.fakes.run_bounded_transaction
            ),
        )

    def _user(self, store):
        return self.transition._user(store)

    def _documents(self, store, collection):
        return self.transition._documents(store, collection)

    @staticmethod
    def _writes(store):
        return ContactTransitionTests._writes(store)

    def _active_authority(self, source_id):
        store = self.fakes.BoundedFakeFirestore()
        bundle, link = self.transition._seed_bundle(store, source_id)
        created = self.transition._record(store, bundle)
        aliases, receipts, settlement, head, fanout = (
            self.transition._assert_one_active_epoch(store)
        )
        self.assertEqual("created", created["disposition"])
        self.assertEqual(settlement, created["settlement"])
        self.assertEqual(head, created["head"])
        self.assertEqual(fanout, created["fanoutHead"])
        self.assertEqual(
            sorted(aliases.values(), key=lambda item: item["exactIdentityHash"]),
            created["aliases"],
        )
        return {
            "store": store,
            "bundle": bundle,
            "link": link,
            "aliases": aliases,
            "receipt": next(iter(receipts.values())),
            "settlement": settlement,
            "head": head,
            "fanout": fanout,
        }

    def _release(
        self,
        active,
        *,
        actor=None,
        client_request_id=None,
        requested_at=None,
        expected_settlement_hash=None,
        executor=None,
        canonical_hash=None,
    ):
        self._method()
        return self._authority(
            active["store"], executor=executor
        ).record_authenticated_contact_release(
            verified_user_id=self.transition.fixture.user_id,
            canonical_mailbox_identity_hash=(
                canonical_hash
                or active["settlement"]["canonicalMailboxIdentityHash"]
            ),
            expected_active_optout_settlement_hash=(
                expected_settlement_hash
                or active["settlement"]["contactSettlementHash"]
            ),
            actor_scope_hash=actor or self.actor,
            client_request_id=(
                client_request_id or self.client_request_id
            ),
            requested_at=requested_at or self.release_at,
        )

    def _client_request_hash(self, client_request_id):
        return self.module.domain_hash(
            self.module.OPERATOR_CLIENT_REQUEST_HASH_DOMAIN,
            {"clientRequestId": client_request_id},
            user_scope_hash=self.transition.fixture.scope,
        )

    def _release_transition_id(
        self, active_settlement, *, actor, client_request_id
    ):
        return self.module.domain_hash(
            self.module.CONTACT_TRANSITION_ID_DOMAIN,
            {
                "transitionKind": "authenticated_release",
                "exactIdentityHash": active_settlement["exactIdentityHash"],
                "canonicalMailboxIdentityHash": active_settlement[
                    "canonicalMailboxIdentityHash"
                ],
                "authorityLinkHash": None,
                "hardOptOutEvidenceHash": None,
                "actorScopeHash": actor,
                "clientRequestHash": self._client_request_hash(
                    client_request_id
                ),
                "expectedActiveOptOutSettlementHash": active_settlement[
                    "contactSettlementHash"
                ],
                "reasonCode": "authenticated_release",
            },
            user_scope_hash=self.transition.fixture.scope,
        )

    def _assert_current_graph_valid(self, store):
        aliases = self._documents(store, "contactOptOutAliases")
        receipts = self._documents(store, "contactOptOutTransitionRequests")
        settlements = self._documents(store, "contactOptOutSettlements")
        heads = self._documents(store, "contactOptOutHeads")
        fanouts = self._documents(store, "contactOptOutFanoutHeads")
        for alias in aliases.values():
            self.assertEqual(
                alias,
                self.module.validate_contact_alias_document(document=alias),
            )
        for receipt in receipts.values():
            self.assertEqual(
                receipt,
                self.module.validate_contact_transition_request_document(
                    document=receipt
                ),
            )
        for settlement in settlements.values():
            self.assertEqual(
                settlement,
                self.module.validate_contact_settlement_document(
                    document=settlement
                ),
            )
            self.assertIn(settlement["contactTransitionId"], receipts)
        for fanout in fanouts.values():
            self.assertEqual(
                fanout,
                self.module.validate_contact_fanout_head_document(
                    document=fanout
                ),
            )
        self.assertEqual(1, len(heads))
        canonical, head = next(iter(heads.items()))
        self.assertEqual(
            head,
            self.module.validate_contact_head_document(document=head),
        )
        latest = settlements[f"{canonical}--{head['latestGeneration']}"]
        receipt = receipts[latest["contactTransitionId"]]
        self.module._validate_contact_settlement_creating_receipt(
            settlement_document=latest,
            receipt_document=receipt,
            contact_head=head,
            user_scope_hash=self.transition.fixture.scope,
            canonical_mailbox_identity_hash=canonical,
        )
        self.assertIn(head["activeFanoutId"], fanouts)
        return aliases, receipts, settlements, head, fanouts

    def _assert_created_release(
        self,
        active,
        result,
        *,
        actor=None,
        client_request_id=None,
        requested_at=None,
    ):
        actor = actor or self.actor
        client_request_id = client_request_id or self.client_request_id
        requested_at = requested_at or self.release_at
        store = active["store"]
        aliases, receipts, settlements, head, fanouts = (
            self._assert_current_graph_valid(store)
        )
        receipt = result["transitionRequest"]
        settlement = result["settlement"]
        fanout = result["fanoutHead"]
        canonical = active["settlement"]["canonicalMailboxIdentityHash"]
        expected_id = self._release_transition_id(
            active["settlement"],
            actor=actor,
            client_request_id=client_request_id,
        )

        self.assertEqual("created", result["disposition"])
        self.assertEqual(expected_id, receipt["contactTransitionId"])
        self.assertEqual("authenticated_release", receipt["transitionKind"])
        self.assertEqual(active["settlement"]["exactIdentityHash"], receipt["exactIdentityHash"])
        self.assertEqual(canonical, receipt["canonicalMailboxIdentityHash"])
        self.assertIsNone(receipt["authorityLinkHash"])
        self.assertIsNone(receipt["hardOptOutEvidenceHash"])
        self.assertEqual(actor, receipt["actorScopeHash"])
        self.assertEqual(
            self._client_request_hash(client_request_id),
            receipt["clientRequestHash"],
        )
        self.assertEqual(
            active["settlement"]["contactSettlementHash"],
            receipt["expectedActiveOptOutSettlementHash"],
        )
        self.assertEqual("authenticated_release", receipt["reasonCode"])
        self.assertEqual(requested_at, receipt["requestedAt"])

        self.assertEqual(2, settlement["generation"])
        self.assertEqual("authenticated_release", settlement["transitionKind"])
        self.assertEqual(expected_id, settlement["contactTransitionId"])
        self.assertEqual(
            active["settlement"]["contactSettlementHash"],
            settlement["predecessorSettlementHash"],
        )
        self.assertEqual(actor, settlement["actorScopeHash"])
        self.assertEqual(requested_at, settlement["settledAt"])
        self.assertIsNone(settlement["authorityLink"])
        self.assertEqual("released", head["state"])
        self.assertEqual(2, head["latestGeneration"])
        self.assertEqual(
            settlement["contactSettlementHash"], head["latestSettlementHash"]
        )
        self.assertIsNone(head["activeOptOutSettlementHash"])
        self.assertEqual(active["head"]["createdAt"], head["createdAt"])
        self.assertEqual(requested_at, head["updatedAt"])

        self.assertEqual("release", fanout["outcome"])
        self.assertEqual("discovering", fanout["state"])
        self.assertEqual(1, fanout["stateRevision"])
        self.assertEqual(1, fanout["fencingToken"])
        self.assertIsNone(fanout["leaseOwnerHash"])
        self.assertIsNone(fanout["leaseUntil"])
        self.assertIsNone(fanout["discoveryCursorRowId"])
        self.assertEqual(requested_at, fanout["createdAt"])
        self.assertEqual(requested_at, fanout["updatedAt"])
        for key in (
            "bindingRevision",
            "bindingHeadHash",
            "bindingAssociationCount",
        ):
            self.assertEqual(active["fanout"][key], fanout[key])

        self.assertEqual(receipt, receipts[expected_id])
        self.assertEqual(
            settlement,
            settlements[f"{canonical}--{settlement['generation']}"]
        )
        self.assertEqual(head, result["head"])
        self.assertEqual(fanout, fanouts[fanout["fanoutId"]])
        self.assertEqual(
            sorted(aliases.values(), key=lambda item: item["exactIdentityHash"]),
            result["aliases"],
        )
        self.assertEqual(
            receipt["resultingContactSettlementHash"],
            settlement["contactSettlementHash"],
        )
        self.assertEqual(receipt["resultingContactHeadHash"], head["contactHeadHash"])
        self.assertEqual(receipt["resultingFanoutId"], fanout["fanoutId"])
        self.assertEqual(
            receipt["resultingFanoutHeadHash"],
            fanout["contactFanoutHeadHash"],
        )
        return receipt, settlement, head, fanout

    def _advance_fanout(self, store, fanout_id, *, state, updated_at):
        current = self._documents(store, "contactOptOutFanoutHeads")[fanout_id]
        self.assertIn(state, {"applying", "complete"})
        complete = state == "complete"
        advanced = self.module.build_contact_fanout_head_document(
            user_scope_hash=current["userScopeHash"],
            fanout_id=current["fanoutId"],
            outcome=current["outcome"],
            expected_contact_settlement_hash=current[
                "expectedContactSettlementHash"
            ],
            # The synthetic after-image retains the real reachable sequence:
            # acquire increments revision/fence, then completion increments
            # revision again while retaining the acquired fence.
            state_revision=current["stateRevision"] + (2 if complete else 1),
            state=state,
            binding_revision=current["bindingRevision"],
            binding_head_hash=current["bindingHeadHash"],
            binding_association_count=current["bindingAssociationCount"],
            discovery_cursor_row_id=(
                None if complete else current["discoveryCursorRowId"]
            ),
            obligation_count=current["obligationCount"],
            result_count=current["resultCount"],
            lease_owner_hash=None if complete else "b" * 64,
            lease_until=(
                None
                if complete
                else "2026-08-04T12:30:00.000000Z"
            ),
            fencing_token=current["fencingToken"] + 1,
            superseding_contact_settlement_hash=None,
            completion_binding_revision=(
                current["bindingRevision"] if complete else None
            ),
            completion_binding_head_hash=(
                current["bindingHeadHash"] if complete else None
            ),
            completion_binding_association_count=(
                current["bindingAssociationCount"] if complete else None
            ),
            completion_obligation_count=(
                current["obligationCount"] if complete else None
            ),
            completion_result_count=(
                current["resultCount"] if complete else None
            ),
            completed_at=updated_at if complete else None,
            created_at=current["createdAt"],
            updated_at=updated_at,
        )
        self._user(store).collection("contactOptOutFanoutHeads").document(
            fanout_id
        ).set(advanced, merge=False)
        return advanced

    def test_release_binds_actor_request_and_exact_active_settlement(self):
        active = self._active_authority("source-release-binding")
        method = self._method()
        signature = inspect.signature(method)
        self.assertEqual(
            [
                "self",
                "verified_user_id",
                "canonical_mailbox_identity_hash",
                "expected_active_optout_settlement_hash",
                "actor_scope_hash",
                "client_request_id",
                "requested_at",
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
        active["store"].events.clear()
        result = self._release(active)
        self._assert_created_release(active, result)
        self.assertEqual(5, len(self._writes(active["store"])))
        self.assertEqual(1, active["store"].events.count(("commit_applied", 5)))

    def test_release_never_creates_or_repairs_alias(self):
        healthy = self._active_authority("source-release-alias-healthy")
        before_aliases = deepcopy(healthy["aliases"])
        healthy["store"].events.clear()
        self._release(healthy)
        self.assertEqual(
            before_aliases,
            self._documents(healthy["store"], "contactOptOutAliases"),
        )
        self.assertFalse(
            any("/contactOptOutAliases/" in event[1] for event in self._writes(healthy["store"]))
        )

        for mode in ("missing", "drift"):
            for alias_kind in ("exact", "canonical"):
                with self.subTest(mode=mode, alias=alias_kind):
                    active = self._active_authority(
                        f"source-release-alias-{mode}-{alias_kind}"
                    )
                    settlement = active["settlement"]
                    alias_id = (
                        settlement["exactIdentityHash"]
                        if alias_kind == "exact"
                        else settlement["canonicalMailboxIdentityHash"]
                    )
                    reference = self._user(active["store"]).collection(
                        "contactOptOutAliases"
                    ).document(alias_id)
                    if mode == "missing":
                        reference.delete()
                    else:
                        malformed = deepcopy(active["aliases"][alias_id])
                        malformed["contactAliasHash"] = "f" * 64
                        reference.set(malformed, merge=False)
                    before = deepcopy(active["store"].data)
                    active["store"].events.clear()
                    with self.assertRaises(self.module.RowAuthorityAmbiguous):
                        self._release(active)
                    self.assertEqual(before, active["store"].data)
                    self.assertEqual([], self._writes(active["store"]))

    def test_release_supersedes_current_nonterminal_apply_fanout_atomically(self):
        for state in ("discovering", "applying", "complete"):
            with self.subTest(state=state):
                active = self._active_authority(f"source-release-{state}")
                if state != "discovering":
                    self.transition._set_current_fanout_state(
                        active["store"], state
                    )
                prior = self._documents(
                    active["store"], "contactOptOutFanoutHeads"
                )[active["fanout"]["fanoutId"]]
                active["store"].max_writes_per_commit = (
                    4 if state == "complete" else 5
                )
                active["store"].events.clear()
                result = self._release(active)
                _receipt, settlement, _head, _fanout = (
                    self._assert_created_release(active, result)
                )
                updated_prior = self._documents(
                    active["store"], "contactOptOutFanoutHeads"
                )[prior["fanoutId"]]
                if state == "complete":
                    self.assertEqual(prior, updated_prior)
                    expected_writes = 4
                else:
                    self.assertEqual("superseding", updated_prior["state"])
                    self.assertEqual(
                        prior["stateRevision"] + 1,
                        updated_prior["stateRevision"],
                    )
                    self.assertEqual(
                        prior["fencingToken"] + 1,
                        updated_prior["fencingToken"],
                    )
                    self.assertEqual(
                        settlement["contactSettlementHash"],
                        updated_prior["supersedingContactSettlementHash"],
                    )
                    self.assertIsNone(updated_prior["leaseOwnerHash"])
                    self.assertIsNone(updated_prior["leaseUntil"])
                    self.assertIsNone(updated_prior["discoveryCursorRowId"])
                    self.assertEqual(self.release_at, updated_prior["updatedAt"])
                    expected_writes = 5
                self.assertEqual(expected_writes, len(self._writes(active["store"])))
                self.assertEqual(
                    1,
                    active["store"].events.count(
                        ("commit_applied", expected_writes)
                    ),
                )

        for mode in ("superseding", "malformed"):
            with self.subTest(rejected=mode):
                active = self._active_authority(
                    f"source-release-rejected-{mode}"
                )
                if mode == "superseding":
                    self.transition._set_current_fanout_state(
                        active["store"], "superseding"
                    )
                else:
                    fanout = deepcopy(active["fanout"])
                    fanout["contactFanoutHeadHash"] = "f" * 64
                    self._user(active["store"]).collection(
                        "contactOptOutFanoutHeads"
                    ).document(fanout["fanoutId"]).set(fanout, merge=False)
                before = deepcopy(active["store"].data)
                active["store"].events.clear()
                with self.assertRaises(self.module.RowAuthorityAmbiguous):
                    self._release(active)
                self.assertEqual(before, active["store"].data)
                self.assertEqual([], self._writes(active["store"]))

    def test_stale_or_repeated_distinct_release_is_zero_write(self):
        released = self._active_authority("source-release-distinct")
        self._release(released)
        before = deepcopy(released["store"].data)
        released["store"].events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._release(
                released,
                actor=self.other_actor,
                client_request_id="release-request-two",
                requested_at=self.progress_at,
            )
        self.assertEqual(before, released["store"].data)
        self.assertEqual([], self._writes(released["store"]))

        stale = self._active_authority("source-release-stale")
        old_settlement_hash = stale["settlement"]["contactSettlementHash"]
        self._release(stale)
        later_bundle, _ = self.transition._seed_bundle(
            stale["store"], "source-release-stale-reopt"
        )
        self.transition._record(
            stale["store"], later_bundle, requested_at=self.reopt_at
        )
        before = deepcopy(stale["store"].data)
        stale["store"].events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._release(
                stale,
                actor=self.other_actor,
                client_request_id="release-request-stale",
                requested_at="2026-08-04T12:00:09.000000Z",
                expected_settlement_hash=old_settlement_hash,
            )
        self.assertEqual(before, stale["store"].data)
        self.assertEqual([], self._writes(stale["store"]))

    def test_release_retry_and_apply_then_raise_read_exact_receipt(self):
        active = self._active_authority("source-release-apply-then-raise")
        active["store"].events.clear()
        active["store"].apply_then_raise_next_commit = RuntimeError(
            "unknown release commit"
        )
        created = self._release(active)
        receipt, settlement, head, fanout = self._assert_created_release(
            active, created
        )
        self.assertIn(("commit_raised_after_apply",), active["store"].events)
        self.assertEqual(
            receipt,
            self._documents(
                active["store"], "contactOptOutTransitionRequests"
            )[receipt["contactTransitionId"]],
        )
        self.assertEqual(
            settlement,
            self._documents(active["store"], "contactOptOutSettlements")[
                f"{settlement['canonicalMailboxIdentityHash']}--2"
            ],
        )
        self.assertEqual(created["head"], head)
        self.assertEqual(created["fanoutHead"], fanout)

        active["store"].events.clear()
        retry = self._release(
            active,
            requested_at="2026-08-04T12:00:07.000000Z",
        )
        self.assertEqual("already_applied", retry["disposition"])
        self.assertEqual(receipt, retry["transitionRequest"])
        self.assertEqual(self.release_at, retry["transitionRequest"]["requestedAt"])
        self.assertEqual([], self._writes(active["store"]))

    def test_old_release_receipt_retry_after_new_optout_never_mutates_new_epoch(self):
        active = self._active_authority("source-release-old-retry")
        released = self._release(active)
        old_receipt = released["transitionRequest"]
        later_bundle, _ = self.transition._seed_bundle(
            active["store"], "source-release-old-retry-reopt"
        )
        self.transition._record(
            active["store"], later_bundle, requested_at=self.reopt_at
        )
        before = deepcopy(active["store"].data)
        current_head = next(
            iter(self._documents(active["store"], "contactOptOutHeads").values())
        )
        self.assertEqual(("active", 3), (current_head["state"], current_head["latestGeneration"]))
        active["store"].events.clear()
        retry = self._release(
            active,
            requested_at="2026-08-04T12:00:09.000000Z",
        )
        self.assertEqual("already_applied", retry["disposition"])
        self.assertEqual(old_receipt, retry["transitionRequest"])
        self.assertEqual(2, retry["settlement"]["generation"])
        self.assertEqual(3, retry["head"]["latestGeneration"])
        self.assertEqual(before, active["store"].data)
        self.assertEqual([], self._writes(active["store"]))

    def test_historical_release_lookup_is_unique_bounded_and_path_exact(self):
        active = self._active_authority("source-release-bounded-lookup")
        store = active["store"]
        collection = self._user(store).collection("contactOptOutSettlements")
        query_type = type(collection.where("probe", "==", "probe"))
        original_limit = query_type.limit
        observed_limits = []

        def traced_limit(query, count):
            if query._collection.path == collection.path:
                observed_limits.append((query._filters, count))
            return original_limit(query, count)

        store.events.clear()
        with patch.object(query_type, "limit", traced_limit):
            result = self._release(active)
        settlement = result["settlement"]
        expected_filters = (
            (
                "canonicalMailboxIdentityHash",
                "==",
                active["settlement"]["canonicalMailboxIdentityHash"],
            ),
            (
                "contactSettlementHash",
                "==",
                active["settlement"]["contactSettlementHash"],
            ),
        )
        self.assertEqual(1, len(observed_limits))
        self.assertCountEqual(expected_filters, observed_limits[0][0])
        self.assertEqual(2, observed_limits[0][1])
        relevant_queries = [
            event
            for event in store.events
            if event[0] == "query" and event[1] == collection.path
        ]
        self.assertEqual(1, len(relevant_queries))
        self.assertCountEqual(expected_filters, relevant_queries[0][2])
        self.assertEqual(("__name__",), relevant_queries[0][3])
        active_path = collection.document(
            f"{active['settlement']['canonicalMailboxIdentityHash']}--1"
        ).path
        self.assertEqual(active["settlement"], store.data[active_path])
        self.assertEqual(2, settlement["generation"])

    def test_historical_release_lookup_missing_duplicate_or_drift_is_zero_write(self):
        for mode in ("missing", "duplicate", "drift"):
            with self.subTest(mode=mode):
                active = self._active_authority(
                    f"source-release-lookup-{mode}"
                )
                canonical = active["settlement"][
                    "canonicalMailboxIdentityHash"
                ]
                collection = self._user(active["store"]).collection(
                    "contactOptOutSettlements"
                )
                exact = collection.document(f"{canonical}--1")
                if mode == "missing":
                    exact.delete()
                elif mode == "duplicate":
                    collection.document("duplicate-active-settlement").create(
                        active["settlement"]
                    )
                else:
                    collection.document("wrong-active-settlement-path").create(
                        active["settlement"]
                    )
                    exact.delete()
                before = deepcopy(active["store"].data)
                active["store"].events.clear()
                with self.assertRaises(self.module.RowAuthorityAmbiguous):
                    self._release(active)
                self.assertEqual(before, active["store"].data)
                self.assertEqual([], self._writes(active["store"]))

    def test_transition_retry_accepts_valid_fanout_progress_and_later_contact_epochs(self):
        for state in ("applying", "complete"):
            with self.subTest(progress=state):
                active = self._active_authority(
                    f"source-release-retry-progress-{state}"
                )
                created = self._release(active)
                receipt = created["transitionRequest"]
                fanout = self._advance_fanout(
                    active["store"],
                    created["fanoutHead"]["fanoutId"],
                    state=state,
                    updated_at=self.progress_at,
                )
                active["store"].events.clear()
                retry = self._release(
                    active,
                    requested_at="2026-08-04T12:00:07.000000Z",
                )
                self.assertEqual("already_applied", retry["disposition"])
                self.assertEqual(receipt, retry["transitionRequest"])
                self.assertEqual(fanout, retry["fanoutHead"])
                self.assertEqual([], self._writes(active["store"]))

        active = self._active_authority("source-release-retry-later-head")
        first_release = self._release(active)
        self._advance_fanout(
            active["store"],
            first_release["fanoutHead"]["fanoutId"],
            state="applying",
            updated_at=self.progress_at,
        )
        later_bundle, _ = self.transition._seed_bundle(
            active["store"], "source-release-retry-later-head-reopt"
        )
        self.transition._record(
            active["store"], later_bundle, requested_at=self.reopt_at
        )
        active["store"].events.clear()
        after_reopt = self._release(
            active,
            requested_at="2026-08-04T12:00:09.000000Z",
        )
        self.assertEqual("already_applied", after_reopt["disposition"])
        self.assertEqual(2, after_reopt["settlement"]["generation"])
        self.assertEqual(("active", 3), (after_reopt["head"]["state"], after_reopt["head"]["latestGeneration"]))
        self.assertEqual("superseding", after_reopt["fanoutHead"]["state"])
        self.assertEqual([], self._writes(active["store"]))

        current_settlement = self._documents(
            active["store"], "contactOptOutSettlements"
        )[
            f"{active['settlement']['canonicalMailboxIdentityHash']}--3"
        ]
        second_release_active = {
            **active,
            "settlement": current_settlement,
        }
        self._release(
            second_release_active,
            actor=self.other_actor,
            client_request_id="release-request-later-epoch",
            requested_at=self.second_release_at,
        )
        before = deepcopy(active["store"].data)
        active["store"].events.clear()
        after_second_release = self._release(
            active,
            requested_at="2026-08-04T12:00:11.000000Z",
        )
        self.assertEqual("already_applied", after_second_release["disposition"])
        self.assertEqual(2, after_second_release["settlement"]["generation"])
        self.assertEqual(("released", 4), (after_second_release["head"]["state"], after_second_release["head"]["latestGeneration"]))
        self.assertEqual(before, active["store"].data)
        self.assertEqual([], self._writes(active["store"]))

    def test_first_winner_time_is_retained_on_same_id_retry(self):
        active = self._active_authority("source-release-first-winner")
        self._method()
        first_time = self.release_at
        second_time = self.progress_at
        active["store"].events.clear()
        active["store"].before_commit_barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self._release, active, requested_at=requested_at)
                for requested_at in (first_time, second_time)
            ]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(
            ["already_applied", "created"],
            sorted(result["disposition"] for result in results),
        )
        self.assertEqual(
            results[0]["transitionRequest"],
            results[1]["transitionRequest"],
        )
        winning_receipt = results[0]["transitionRequest"]
        self.assertIn(winning_receipt["requestedAt"], {first_time, second_time})
        self.assertEqual(1, active["store"].events.count(("commit_applied", 5)))
        self.assertEqual(1, active["store"].events.count(("commit_applied", 0)))
        self.assertEqual(
            2,
            len(self._documents(active["store"], "contactOptOutSettlements")),
        )

        active["store"].events.clear()
        retry = self._release(
            active,
            requested_at="2026-08-04T12:00:07.000000Z",
        )
        self.assertEqual("already_applied", retry["disposition"])
        self.assertEqual(winning_receipt, retry["transitionRequest"])
        self.assertEqual([], self._writes(active["store"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
