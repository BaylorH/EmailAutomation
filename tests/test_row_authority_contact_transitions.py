"""RED contracts for retry-safe verified contact opt-out transitions."""

from __future__ import annotations

import importlib
import inspect
import unittest
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier


class ContactTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")
        cls.ownership = importlib.import_module(
            "tests.test_row_authority_ownership"
        )
        cls.fakes = importlib.import_module("tests.row_authority_fakes")
        cls.fixture_type = cls.ownership.RowClaimStoreTests
        cls.fixture_type.setUpClass()

    def setUp(self):
        self.fixture = self.fixture_type(methodName="runTest")
        self.fixture.setUp()
        self.requested_at = "2026-08-04T12:00:03.000000Z"
        self.later_at = "2026-08-04T12:00:04.000000Z"

    def _method(self):
        self.assertTrue(
            hasattr(
                self.module.RowAuthorityStore,
                "record_verified_contact_optout",
            ),
            "RowAuthorityStore.record_verified_contact_optout is missing",
        )
        return self.module.RowAuthorityStore.record_verified_contact_optout

    def _authority(self, store, *, executor=None):
        return self.module.RowAuthorityStore(
            store,
            transaction_executor=(
                executor or self.fakes.run_bounded_transaction
            ),
        )

    def _user(self, store):
        return store.collection("users").document(self.fixture.user_id)

    def _seed_bundle(self, store, source_id, *, version=2, exact_hash=None):
        bundle = self.ownership.RowOwnershipContractTests._b1_bundle(
            self.fixture,
            owner_kind="contact_optout",
            contact_evidence_version=version,
            source_id=source_id,
        )
        if exact_hash is not None:
            bundle = self._retarget_bundle_exact(bundle, exact_hash)
        user = self._user(store)
        for collection, key in (
            ("sourceIdentities", "identity"),
            ("sourceClassifications", "classification"),
            ("sourceTransitionOwners", "owner"),
            ("sourceWorkLedgers", "ledger"),
        ):
            user.collection(collection).document(source_id).create(bundle[key])
        link = self.module.build_b1_authority_link(
            user_scope_hash=self.fixture.scope,
            source_identity_document=bundle["identity"],
            source_classification_document=bundle["classification"],
            source_owner_document=bundle["owner"],
            source_ledger_document=bundle["ledger"],
            work_key=bundle["work_key"],
        )
        if version == 2:
            self.assertEqual(
                link,
                self.module.validate_b1_authority_link(
                    authority_link=link,
                    user_scope_hash=self.fixture.scope,
                ),
            )
        return bundle, link

    def _retarget_bundle_exact(self, bundle, exact_hash):
        """Rebuild every B1 digest after changing only the v2 exact hash."""
        self.assertRegex(exact_hash, r"^[0-9a-f]{64}$")
        rebuilt = deepcopy(bundle)
        classification = rebuilt["classification"]
        source_id = rebuilt["identity"]["canonicalSourceId"]
        digest = self.ownership._independent_b1_hash
        evidence = deepcopy(classification["deterministicEvidence"])
        evidence["exactIdentityHash"] = exact_hash
        evidence_hash = digest(evidence)
        candidate = {
            "type": "contact_optout",
            "evidenceHash": evidence_hash,
        }
        transition_candidates = [deepcopy(candidate)]
        ordinary_obligations = []
        complete_proposal = {
            "schemaVersion": 1,
            "transitionCandidates": [deepcopy(candidate)],
            "ordinaryObligations": [],
        }
        selected_candidates = [deepcopy(candidate)]
        owner_key = digest(
            {
                "hashKind": "source-selection-v1",
                "canonicalSourceId": source_id,
                "ownerKind": "contact_optout",
                "selectedCandidates": selected_candidates,
            }
        )
        selection_snapshot = {
            "candidateTaxonomyVersion": "source-candidate-taxonomy-v1",
            "ownerKind": "contact_optout",
            "ownerKey": owner_key,
            "selectedCandidates": deepcopy(selected_candidates),
            "candidateDominance": [
                {
                    "candidateHash": digest(candidate),
                    "outcome": "selected",
                }
            ],
            "transitionCandidatesHash": digest(transition_candidates),
            "ordinaryObligationsHash": digest(ordinary_obligations),
        }
        selection_hash = digest(selection_snapshot)
        complete_proposal_hash = digest(complete_proposal)
        snapshot_material = {
            "schemaVersion": 1,
            "hashKind": "source-classification-snapshot-v1",
            "canonicalSourceId": source_id,
            "classificationInputSchemaVersion": classification[
                "classificationInputSchemaVersion"
            ],
            "classificationInputHash": classification[
                "classificationInputHash"
            ],
            "modelRequestKey": classification["modelRequestKey"],
            "completeProposalSnapshot": deepcopy(complete_proposal),
            "completeProposalHash": complete_proposal_hash,
            "transitionCandidates": deepcopy(transition_candidates),
            "ordinaryObligations": [],
            "selectionSnapshot": deepcopy(selection_snapshot),
            "selectionHash": selection_hash,
            "proposalEvidence": deepcopy(classification["proposalEvidence"]),
            "proposalEvidenceHash": classification["proposalEvidenceHash"],
            "deterministicEvidence": deepcopy(evidence),
            "deterministicEvidenceHash": evidence_hash,
        }
        snapshot_hash = digest(snapshot_material)
        classification.update(
            {
                "completeProposalSnapshot": complete_proposal,
                "completeProposalHash": complete_proposal_hash,
                "transitionCandidates": transition_candidates,
                "ordinaryObligations": ordinary_obligations,
                "selectionSnapshot": selection_snapshot,
                "selectionHash": selection_hash,
                "snapshotImmutableHash": snapshot_hash,
                "deterministicEvidence": evidence,
                "deterministicEvidenceHash": evidence_hash,
            }
        )
        owner_immutable = {
            "schemaVersion": 1,
            "canonicalSourceId": source_id,
            "snapshotImmutableHash": snapshot_hash,
            "selectionHash": selection_hash,
            "ownerKind": "contact_optout",
            "ownerKey": owner_key,
        }
        owner_hash = digest(
            {"hashKind": "source-transition-owner-v1", **owner_immutable}
        )
        old_owner = rebuilt["owner"]
        rebuilt["owner"] = {
            **owner_immutable,
            "ownerDecisionHash": owner_hash,
            "revision": old_owner["revision"],
            "createdAt": old_owner["createdAt"],
            "updatedAt": old_owner["updatedAt"],
        }
        payload_hash = digest(candidate)
        work_key = digest(
            {
                "hashKind": "source-work-key-v1",
                "canonicalSourceId": source_id,
                "snapshotImmutableHash": snapshot_hash,
                "selectionHash": selection_hash,
                "lane": "transition",
                "payloadHash": payload_hash,
                "occurrenceOrdinal": 1,
            }
        )
        old_entry = rebuilt["ledger"]["entries"][0]
        entry = {
            **deepcopy(old_entry),
            "workKey": work_key,
            "payload": deepcopy(candidate),
            "payloadHash": payload_hash,
            "selectedOwnerKey": owner_key,
        }
        immutable_fields = {
            "workKey",
            "lane",
            "kind",
            "payload",
            "payloadHash",
            "occurrenceOrdinal",
            "selectedOwnerKind",
            "selectedOwnerKey",
            "dominanceOutcome",
            "completionContract",
        }
        immutable_entry = {
            field: deepcopy(entry[field]) for field in sorted(immutable_fields)
        }
        ledger_hash = digest(
            {
                "hashKind": "source-work-ledger-v1",
                "canonicalSourceId": source_id,
                "completeProposalHash": complete_proposal_hash,
                "snapshotImmutableHash": snapshot_hash,
                "selectionHash": selection_hash,
                "ownerDecisionHash": owner_hash,
                "entries": [immutable_entry],
            }
        )
        old_ledger = rebuilt["ledger"]
        rebuilt["ledger"] = {
            "schemaVersion": 1,
            "canonicalSourceId": source_id,
            "completeProposalHash": complete_proposal_hash,
            "snapshotImmutableHash": snapshot_hash,
            "selectionHash": selection_hash,
            "ownerDecisionHash": owner_hash,
            "entries": [entry],
            "entryCount": 1,
            "ledgerHash": ledger_hash,
            "revision": old_ledger["revision"],
            "createdAt": old_ledger["createdAt"],
            "updatedAt": old_ledger["updatedAt"],
        }
        rebuilt.update(
            {
                "work_key": work_key,
                "payload_hash": payload_hash,
                "hard_optout_hash": evidence_hash,
            }
        )
        return rebuilt

    def _record(self, store, bundle, *, executor=None, requested_at=None, **extra):
        self._method()
        return self._authority(store, executor=executor).record_verified_contact_optout(
            verified_user_id=self.fixture.user_id,
            canonical_source_id=bundle["identity"]["canonicalSourceId"],
            work_key=bundle["work_key"],
            requested_at=requested_at or self.requested_at,
            **extra,
        )

    @staticmethod
    def _writes(store):
        return [
            event
            for event in store.events
            if event[0] in {"create", "set", "update", "delete"}
        ]

    def _documents(self, store, collection):
        prefix = self._user(store).collection(collection).path + "/"
        return {
            path[len(prefix) :]: deepcopy(payload)
            for path, payload in store.data.items()
            if path.startswith(prefix) and "/" not in path[len(prefix) :]
        }

    def _contact_after_image(self, store):
        aliases = self._documents(store, "contactOptOutAliases")
        receipts = self._documents(store, "contactOptOutTransitionRequests")
        settlements = self._documents(store, "contactOptOutSettlements")
        heads = self._documents(store, "contactOptOutHeads")
        fanouts = self._documents(store, "contactOptOutFanoutHeads")
        return aliases, receipts, settlements, heads, fanouts

    def _assert_one_active_epoch(self, store, *, receipt_count=1):
        aliases, receipts, settlements, heads, fanouts = self._contact_after_image(
            store
        )
        self.assertEqual(2, len(aliases))
        self.assertEqual(receipt_count, len(receipts))
        self.assertEqual(1, len(settlements))
        self.assertEqual(1, len(heads))
        self.assertEqual(1, len(fanouts))
        settlement = next(iter(settlements.values()))
        head = next(iter(heads.values()))
        fanout = next(iter(fanouts.values()))
        self.assertEqual(
            settlement,
            self.module.validate_contact_settlement_document(document=settlement),
        )
        self.assertEqual(
            head,
            self.module.validate_contact_head_document(document=head),
        )
        self.assertEqual(
            fanout,
            self.module.validate_contact_fanout_head_document(document=fanout),
        )
        self.assertEqual(1, settlement["generation"])
        self.assertEqual("active", head["state"])
        self.assertEqual(settlement["contactSettlementHash"], head["latestSettlementHash"])
        self.assertEqual(settlement["contactSettlementHash"], head["activeOptOutSettlementHash"])
        self.assertEqual("apply", fanout["outcome"])
        self.assertEqual("discovering", fanout["state"])
        self.assertEqual(1, fanout["fencingToken"])
        self.assertEqual(settlement["contactSettlementHash"], fanout["expectedContactSettlementHash"])
        for receipt in receipts.values():
            self.assertEqual(
                receipt,
                self.module.validate_contact_transition_request_document(
                    document=receipt
                ),
            )
            self.assertEqual(settlement["contactSettlementHash"], receipt["resultingContactSettlementHash"])
            self.assertEqual(head["contactHeadHash"], receipt["resultingContactHeadHash"])
            self.assertEqual(fanout["contactFanoutHeadHash"], receipt["resultingFanoutHeadHash"])
        return aliases, receipts, settlement, head, fanout

    def _transition_id(self, *, link=None, expected=None, actor=None, request=None):
        verified = link is not None
        return self.module.domain_hash(
            self.module.CONTACT_TRANSITION_ID_DOMAIN,
            {
                "transitionKind": "verified_optout" if verified else "authenticated_release",
                "exactIdentityHash": (link["exactIdentityHash"] if verified else "4" * 64),
                "canonicalMailboxIdentityHash": (link["canonicalMailboxIdentityHash"] if verified else "5" * 64),
                "authorityLinkHash": link["authorityLinkHash"] if verified else None,
                "hardOptOutEvidenceHash": link["hardOptOutEvidenceHash"] if verified else None,
                "actorScopeHash": None if verified else actor,
                "clientRequestHash": None if verified else request,
                "expectedActiveOptOutSettlementHash": None if verified else expected,
                "reasonCode": None if verified else "authenticated_release",
            },
            user_scope_hash=self.fixture.scope,
        )

    def _fanout(self, settlement_hash, *, outcome, created_at, state="discovering"):
        fanout_id = self.module.domain_hash(
            self.module.CONTACT_FANOUT_ID_DOMAIN,
            {"contactSettlementHash": settlement_hash, "outcome": outcome},
            user_scope_hash=self.fixture.scope,
        )
        complete = state == "complete"
        return self.module.build_contact_fanout_head_document(
            user_scope_hash=self.fixture.scope,
            fanout_id=fanout_id,
            outcome=outcome,
            expected_contact_settlement_hash=settlement_hash,
            state_revision=2 if complete else 1,
            state=state,
            binding_revision=0,
            binding_head_hash=None,
            binding_association_count=0,
            discovery_cursor_row_id=None,
            cursor_processed_count=0,
            obligation_count=0,
            result_count=0,
            lease_owner_hash=None,
            lease_until=None,
            fencing_token=1,
            superseding_contact_settlement_hash=None,
            completion_binding_revision=0 if complete else None,
            completion_binding_head_hash=None,
            completion_binding_association_count=0 if complete else None,
            completion_obligation_count=0 if complete else None,
            completion_result_count=0 if complete else None,
            completed_at=created_at if complete else None,
            created_at=self.requested_at if complete else created_at,
            updated_at=created_at,
        )

    def _seed_released_authority(self, store, link):
        scope = self.fixture.scope
        exact = link["exactIdentityHash"]
        canonical = link["canonicalMailboxIdentityHash"]
        user = self._user(store)
        for alias_hash in (exact, canonical):
            alias = self.module.build_contact_alias_document(
                user_scope_hash=scope,
                exact_identity_hash=alias_hash,
                canonical_mailbox_identity_hash=canonical,
                created_at=self.requested_at,
            )
            user.collection("contactOptOutAliases").document(alias_hash).create(alias)

        active_id = self._transition_id(link=link)
        active = self.module.build_contact_settlement_document(
            user_scope_hash=scope,
            canonical_mailbox_identity_hash=canonical,
            generation=1,
            predecessor_settlement_hash=None,
            transition_kind="verified_optout",
            contact_transition_id=active_id,
            exact_identity_hash=exact,
            authority_link=link,
            actor_scope_hash=None,
            reason_code=None,
            settled_at=self.requested_at,
        )
        active_initial_fanout = self._fanout(
            active["contactSettlementHash"],
            outcome="apply",
            created_at=self.requested_at,
        )
        active_head = self.module.build_contact_head_document(
            user_scope_hash=scope,
            canonical_mailbox_identity_hash=canonical,
            state_revision=1,
            latest_generation=1,
            latest_settlement_hash=active["contactSettlementHash"],
            active_optout_settlement_hash=active["contactSettlementHash"],
            state="active",
            active_fanout_id=active_initial_fanout["fanoutId"],
            created_at=self.requested_at,
            updated_at=self.requested_at,
        )
        active_receipt = self.module.build_contact_transition_request_document(
            user_scope_hash=scope,
            transition_kind="verified_optout",
            exact_identity_hash=exact,
            canonical_mailbox_identity_hash=canonical,
            authority_link_hash=link["authorityLinkHash"],
            hard_optout_evidence_hash=link["hardOptOutEvidenceHash"],
            actor_scope_hash=None,
            client_request_hash=None,
            expected_active_optout_settlement_hash=None,
            reason_code=None,
            outcome="created",
            resulting_contact_generation=1,
            resulting_contact_settlement_hash=active["contactSettlementHash"],
            resulting_fanout_id=active_initial_fanout["fanoutId"],
            resulting_contact_head_hash=active_head["contactHeadHash"],
            resulting_fanout_head_hash=active_initial_fanout["contactFanoutHeadHash"],
            requested_at=self.requested_at,
        )
        actor = "d" * 64
        request = "e" * 64
        release_id = self._transition_id(
            expected=active["contactSettlementHash"], actor=actor, request=request
        )
        released = self.module.build_contact_settlement_document(
            user_scope_hash=scope,
            canonical_mailbox_identity_hash=canonical,
            generation=2,
            predecessor_settlement_hash=active["contactSettlementHash"],
            transition_kind="authenticated_release",
            contact_transition_id=release_id,
            exact_identity_hash=exact,
            authority_link=None,
            actor_scope_hash=actor,
            reason_code="authenticated_release",
            settled_at=self.later_at,
        )
        release_fanout = self._fanout(
            released["contactSettlementHash"],
            outcome="release",
            created_at=self.later_at,
        )
        released_head = self.module.build_contact_head_document(
            user_scope_hash=scope,
            canonical_mailbox_identity_hash=canonical,
            state_revision=2,
            latest_generation=2,
            latest_settlement_hash=released["contactSettlementHash"],
            active_optout_settlement_hash=None,
            state="released",
            active_fanout_id=release_fanout["fanoutId"],
            created_at=self.requested_at,
            updated_at=self.later_at,
        )
        release_receipt = self.module.build_contact_transition_request_document(
            user_scope_hash=scope,
            transition_kind="authenticated_release",
            exact_identity_hash=exact,
            canonical_mailbox_identity_hash=canonical,
            authority_link_hash=None,
            hard_optout_evidence_hash=None,
            actor_scope_hash=actor,
            client_request_hash=request,
            expected_active_optout_settlement_hash=active["contactSettlementHash"],
            reason_code="authenticated_release",
            outcome="created",
            resulting_contact_generation=2,
            resulting_contact_settlement_hash=released["contactSettlementHash"],
            resulting_fanout_id=release_fanout["fanoutId"],
            resulting_contact_head_hash=released_head["contactHeadHash"],
            resulting_fanout_head_hash=release_fanout["contactFanoutHeadHash"],
            requested_at=self.later_at,
        )
        active_complete = self._fanout(
            active["contactSettlementHash"],
            outcome="apply",
            created_at=self.later_at,
            state="complete",
        )
        for collection, document_id, document in (
            ("contactOptOutSettlements", f"{canonical}--1", active),
            ("contactOptOutSettlements", f"{canonical}--2", released),
            ("contactOptOutTransitionRequests", active_id, active_receipt),
            ("contactOptOutTransitionRequests", release_id, release_receipt),
            ("contactOptOutFanoutHeads", active_complete["fanoutId"], active_complete),
            ("contactOptOutFanoutHeads", release_fanout["fanoutId"], release_fanout),
            ("contactOptOutHeads", canonical, released_head),
        ):
            user.collection(collection).document(document_id).create(document)
        return released

    def test_verified_optout_derives_v2_identity_and_creates_exact_authority_atomically(self):
        store = self.fakes.BoundedFakeFirestore()
        bundle, link = self._seed_bundle(store, "source-atomic")
        observed_plans = []

        def inspect_before_commit(transaction):
            self.assertEqual(({}, {}, {}, {}, {}), self._contact_after_image(store))
            observed_plans.append(tuple(ref.path for _op, ref, _data, _merge in transaction._operations))

        store.before_commit_hook = inspect_before_commit
        store.events.clear()
        result = self._record(store, bundle)
        self.assertIsInstance(result, dict)
        aliases, receipts, settlement, _head, _fanout = self._assert_one_active_epoch(store)
        self.assertEqual({link["exactIdentityHash"], link["canonicalMailboxIdentityHash"]}, set(aliases))
        self.assertEqual(link, settlement["authorityLink"])
        self.assertEqual(6, len(observed_plans[0]))
        self.assertEqual(6, len(set(observed_plans[0])))
        self.assertEqual(1, len(receipts))
        self.assertEqual(1, store.events.count(("commit_applied", 6)))

    def test_verified_optout_after_release_allocates_next_contact_generation(self):
        store = self.fakes.BoundedFakeFirestore()
        old_bundle, old_link = self._seed_bundle(store, "source-old")
        released = self._seed_released_authority(store, old_link)
        new_bundle, new_link = self._seed_bundle(store, "source-new")
        self.assertNotEqual(old_bundle["work_key"], new_bundle["work_key"])
        store.events.clear()
        self._record(store, new_bundle, requested_at="2026-08-04T12:00:05.000000Z")
        settlements = self._documents(store, "contactOptOutSettlements")
        heads = self._documents(store, "contactOptOutHeads")
        latest = settlements[f"{new_link['canonicalMailboxIdentityHash']}--3"]
        head = heads[new_link["canonicalMailboxIdentityHash"]]
        self.assertEqual(3, latest["generation"])
        self.assertEqual(released["contactSettlementHash"], latest["predecessorSettlementHash"])
        self.assertEqual("active", head["state"])
        self.assertEqual(latest["contactSettlementHash"], head["latestSettlementHash"])

    def test_transition_request_linearizes_retry_and_two_worker_race(self):
        store = self.fakes.BoundedFakeFirestore()
        bundle, _link = self._seed_bundle(store, "source-race")
        store.events.clear()
        store.before_commit_barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self._record, store, bundle) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertTrue(all(isinstance(result, dict) for result in results))
        self._assert_one_active_epoch(store)
        self.assertEqual(1, store.events.count(("commit_applied", 6)))
        self.assertEqual(1, store.events.count(("commit_applied", 0)))
        before = deepcopy(self._contact_after_image(store))
        self._user(store).collection("contactRowBindingHeads").document(
            "5" * 64
        ).create({"malformed": "later unrelated binding state"})
        store.events.clear()
        self._record(store, bundle, requested_at=self.later_at)
        self.assertEqual(before, self._contact_after_image(store))
        self.assertEqual([], self._writes(store))

    def test_two_distinct_initial_optouts_commit_one_epoch_and_one_already_active_receipt(self):
        store = self.fakes.BoundedFakeFirestore()
        first, _ = self._seed_bundle(store, "source-first")
        second, _ = self._seed_bundle(store, "source-second")
        store.events.clear()
        store.before_commit_barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self._record, store, bundle) for bundle in (first, second)]
            [future.result(timeout=10) for future in futures]
        _aliases, receipts, _settlement, _head, _fanout = self._assert_one_active_epoch(store, receipt_count=2)
        self.assertEqual(["already_active", "created"], sorted(item["outcome"] for item in receipts.values()))
        self.assertEqual(1, store.events.count(("commit_applied", 6)))
        self.assertEqual(1, store.events.count(("commit_applied", 1)))

    def test_three_active_optouts_create_three_receipts_but_one_epoch(self):
        store = self.fakes.BoundedFakeFirestore()
        bundles = [self._seed_bundle(store, f"source-{index}")[0] for index in range(3)]
        store.events.clear()
        for bundle in bundles:
            self._record(store, bundle)
        _aliases, receipts, _settlement, _head, _fanout = self._assert_one_active_epoch(store, receipt_count=3)
        self.assertEqual(1, sum(item["outcome"] == "created" for item in receipts.values()))
        self.assertEqual(2, sum(item["outcome"] == "already_active" for item in receipts.values()))
        self.assertEqual(8, len(self._writes(store)))

    def test_optout_preapply_apply_then_raise_and_partial_readback(self):
        pre_store = self.fakes.BoundedFakeFirestore()
        pre_bundle, _ = self._seed_bundle(pre_store, "source-pre")
        before = deepcopy(pre_store.data)
        pre_store.events.clear()
        pre_store.fail_next_commit = RuntimeError("preapply optout failure")
        with self.assertRaises(self.module.RowAuthorityRetryable):
            self._record(pre_store, pre_bundle)
        self.assertEqual(before, pre_store.data)
        self.assertEqual([], self._writes(pre_store))

        applied_store = self.fakes.BoundedFakeFirestore()
        applied_bundle, _ = self._seed_bundle(applied_store, "source-applied")
        applied_store.events.clear()
        applied_store.apply_then_raise_next_commit = RuntimeError("unknown optout commit")
        result = self._record(applied_store, applied_bundle)
        self.assertIsInstance(result, dict)
        self._assert_one_active_epoch(applied_store)
        self.assertIn(("commit_raised_after_apply",), applied_store.events)

        def partial_executor(transaction, callback):
            transaction._begin()
            callback(transaction)
            operation, reference, payload, merge = transaction._operations[0]
            transaction._rollback()
            self.assertEqual(("create", False), (operation, merge))
            reference.create(payload)
            raise RuntimeError("partial optout apply")

        partial_store = self.fakes.BoundedFakeFirestore()
        partial_bundle, _ = self._seed_bundle(partial_store, "source-partial")
        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._record(partial_store, partial_bundle, executor=partial_executor)
        self.assertEqual(1, sum(len(group) for group in self._contact_after_image(partial_store)))

    def test_v1_or_caller_supplied_contact_authority_cannot_write(self):
        method = self._method()
        signature = inspect.signature(method)
        self.assertEqual(
            ["self", "verified_user_id", "canonical_source_id", "work_key", "requested_at"],
            list(signature.parameters),
        )
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.KEYWORD_ONLY
                for name, parameter in signature.parameters.items()
                if name != "self"
            )
        )
        v1_store = self.fakes.BoundedFakeFirestore()
        v1_bundle, _ = self._seed_bundle(v1_store, "source-v1", version=1)
        before = deepcopy(v1_store.data)
        v1_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._record(v1_store, v1_bundle)
        self.assertEqual(before, v1_store.data)
        self.assertEqual([], self._writes(v1_store))

        for forbidden in (
            "authority_link",
            "hard_optout_evidence_hash",
            "exact_identity_hash",
            "canonical_mailbox_identity_hash",
            "contact_generation",
            "priority",
            "contact_settlement_hash",
            "fanout_id",
            "outcome",
        ):
            store = self.fakes.BoundedFakeFirestore()
            bundle, _ = self._seed_bundle(store, f"source-{forbidden}")
            before = deepcopy(store.data)
            store.events.clear()
            with self.subTest(forbidden=forbidden), self.assertRaises(TypeError):
                self._record(store, bundle, **{forbidden: "f" * 64})
            self.assertEqual(before, store.data)
            self.assertEqual([], self._writes(store))

    def test_transition_write_count_matches_exact_after_image(self):
        store = self.fakes.BoundedFakeFirestore(max_writes_per_commit=6)
        bundle, _ = self._seed_bundle(store, "source-count")
        store.events.clear()
        self._record(store, bundle)
        aliases, receipts, settlements, heads, fanouts = self._contact_after_image(store)
        expected_paths = {
            self._user(store).collection(collection).document(document_id).path
            for collection, documents in (
                ("contactOptOutAliases", aliases),
                ("contactOptOutTransitionRequests", receipts),
                ("contactOptOutSettlements", settlements),
                ("contactOptOutHeads", heads),
                ("contactOptOutFanoutHeads", fanouts),
            )
            for document_id in documents
        }
        writes = self._writes(store)
        self.assertEqual(6, len(writes))
        self.assertEqual(expected_paths, {event[1] for event in writes})
        self.assertEqual(1, store.events.count(("commit_applied", 6)))
        store.events.clear()
        self._record(store, bundle, requested_at=self.later_at)
        self.assertEqual([], self._writes(store))
        self.assertEqual(1, store.events.count(("commit_applied", 0)))

    def _set_current_fanout_state(self, store, state):
        fanouts = self._documents(store, "contactOptOutFanoutHeads")
        self.assertEqual(1, len(fanouts))
        fanout_id, current = next(iter(fanouts.items()))
        applying = state == "applying"
        complete = state == "complete"
        values = {
            "user_scope_hash": current["userScopeHash"],
            "fanout_id": current["fanoutId"],
            "outcome": current["outcome"],
            "expected_contact_settlement_hash": current[
                "expectedContactSettlementHash"
            ],
            # discovering(1/1) -> acquire/applying(2/2) -> complete(3/2).
            # A direct complete fixture represents both already-committed steps.
            "state_revision": 3 if complete else 2,
            "state": state,
            "binding_revision": current["bindingRevision"],
            "binding_head_hash": current["bindingHeadHash"],
            "binding_association_count": current["bindingAssociationCount"],
            "discovery_cursor_row_id": None,
            "cursor_processed_count": 0,
            "obligation_count": current["obligationCount"],
            "result_count": current["resultCount"],
            "lease_owner_hash": "a" * 64 if applying else None,
            "lease_until": (
                "2026-08-04T12:05:00.000000Z" if applying else None
            ),
            "fencing_token": 2,
            "superseding_contact_settlement_hash": (
                "f" * 64 if state == "superseding" else None
            ),
            "completion_binding_revision": (
                current["bindingRevision"] if state == "complete" else None
            ),
            "completion_binding_head_hash": (
                current["bindingHeadHash"] if state == "complete" else None
            ),
            "completion_binding_association_count": (
                current["bindingAssociationCount"]
                if state == "complete"
                else None
            ),
            "completion_obligation_count": (
                current["obligationCount"] if state == "complete" else None
            ),
            "completion_result_count": (
                current["resultCount"] if state == "complete" else None
            ),
            "completed_at": self.later_at if state == "complete" else None,
            "created_at": current["createdAt"],
            "updated_at": self.later_at,
        }
        advanced = self.module.build_contact_fanout_head_document(**values)
        self._user(store).collection("contactOptOutFanoutHeads").document(
            fanout_id
        ).set(advanced, merge=False)
        return advanced

    def _install_release_after_image(self, store, *, released_at):
        user = self._user(store)
        settlements = self._documents(store, "contactOptOutSettlements")
        heads = self._documents(store, "contactOptOutHeads")
        fanouts = self._documents(store, "contactOptOutFanoutHeads")
        self.assertEqual(1, len(settlements))
        self.assertEqual(1, len(heads))
        active = next(iter(settlements.values()))
        canonical = active["canonicalMailboxIdentityHash"]
        active_head = heads[canonical]
        active_fanout = fanouts[active_head["activeFanoutId"]]
        actor = "d" * 64
        request = "e" * 64
        release_id = self._transition_id(
            expected=active["contactSettlementHash"],
            actor=actor,
            request=request,
        )
        released = self.module.build_contact_settlement_document(
            user_scope_hash=self.fixture.scope,
            canonical_mailbox_identity_hash=canonical,
            generation=2,
            predecessor_settlement_hash=active["contactSettlementHash"],
            transition_kind="authenticated_release",
            contact_transition_id=release_id,
            exact_identity_hash=active["exactIdentityHash"],
            authority_link=None,
            actor_scope_hash=actor,
            reason_code="authenticated_release",
            settled_at=released_at,
        )
        release_fanout = self._fanout(
            released["contactSettlementHash"],
            outcome="release",
            created_at=released_at,
        )
        released_head = self.module.build_contact_head_document(
            user_scope_hash=self.fixture.scope,
            canonical_mailbox_identity_hash=canonical,
            state_revision=2,
            latest_generation=2,
            latest_settlement_hash=released["contactSettlementHash"],
            active_optout_settlement_hash=None,
            state="released",
            active_fanout_id=release_fanout["fanoutId"],
            created_at=active_head["createdAt"],
            updated_at=released_at,
        )
        release_receipt = self.module.build_contact_transition_request_document(
            user_scope_hash=self.fixture.scope,
            transition_kind="authenticated_release",
            exact_identity_hash=active["exactIdentityHash"],
            canonical_mailbox_identity_hash=canonical,
            authority_link_hash=None,
            hard_optout_evidence_hash=None,
            actor_scope_hash=actor,
            client_request_hash=request,
            expected_active_optout_settlement_hash=active[
                "contactSettlementHash"
            ],
            reason_code="authenticated_release",
            outcome="created",
            resulting_contact_generation=2,
            resulting_contact_settlement_hash=released[
                "contactSettlementHash"
            ],
            resulting_fanout_id=release_fanout["fanoutId"],
            resulting_contact_head_hash=released_head["contactHeadHash"],
            resulting_fanout_head_hash=release_fanout[
                "contactFanoutHeadHash"
            ],
            requested_at=released_at,
        )
        superseding = self.module._build_contact_superseding_fanout_head(
            current_document=active_fanout,
            superseding_contact_settlement_hash=released[
                "contactSettlementHash"
            ],
            updated_at=released_at,
        )
        user.collection("contactOptOutSettlements").document(
            f"{canonical}--2"
        ).create(released)
        user.collection("contactOptOutTransitionRequests").document(
            release_id
        ).create(release_receipt)
        user.collection("contactOptOutFanoutHeads").document(
            release_fanout["fanoutId"]
        ).create(release_fanout)
        user.collection("contactOptOutFanoutHeads").document(
            active_fanout["fanoutId"]
        ).set(superseding, merge=False)
        user.collection("contactOptOutHeads").document(canonical).set(
            released_head,
            merge=False,
        )
        return released_head

    def test_active_to_active_creates_only_durable_receipt(self):
        store = self.fakes.BoundedFakeFirestore()
        first, _ = self._seed_bundle(store, "source-active-first")
        second, _ = self._seed_bundle(store, "source-active-second")
        self._record(store, first)
        store.events.clear()
        accepted = self._record(store, second, requested_at=self.later_at)
        self.assertEqual("already_active", accepted["disposition"])
        self.assertEqual(1, len(self._writes(store)))
        self.assertIn("/contactOptOutTransitionRequests/", self._writes(store)[0][1])
        receipt = accepted["transitionRequest"]
        store.events.clear()
        retry = self._record(
            store,
            second,
            requested_at="2026-08-04T12:00:05.000000Z",
        )
        self.assertEqual("already_applied", retry["disposition"])
        self.assertEqual(receipt, retry["transitionRequest"])
        self.assertEqual(self.later_at, retry["transitionRequest"]["requestedAt"])
        self.assertEqual([], self._writes(store))

    def test_active_to_active_may_add_only_missing_exact_alias(self):
        store = self.fakes.BoundedFakeFirestore()
        first, _ = self._seed_bundle(store, "source-alias-first")
        second, second_link = self._seed_bundle(
            store,
            "source-alias-second",
            exact_hash="6" * 64,
        )
        self._record(store, first)
        before_head = deepcopy(self._documents(store, "contactOptOutHeads"))
        before_settlements = deepcopy(
            self._documents(store, "contactOptOutSettlements")
        )
        before_fanouts = deepcopy(
            self._documents(store, "contactOptOutFanoutHeads")
        )
        store.events.clear()
        accepted = self._record(store, second, requested_at=self.later_at)
        self.assertEqual("already_active", accepted["disposition"])
        aliases = self._documents(store, "contactOptOutAliases")
        self.assertEqual({"4" * 64, "5" * 64, "6" * 64}, set(aliases))
        self.assertEqual(second_link["exactIdentityHash"], aliases["6" * 64]["exactIdentityHash"])
        self.assertEqual(before_head, self._documents(store, "contactOptOutHeads"))
        self.assertEqual(before_settlements, self._documents(store, "contactOptOutSettlements"))
        self.assertEqual(before_fanouts, self._documents(store, "contactOptOutFanoutHeads"))
        self.assertEqual(2, len(self._writes(store)))
        store.events.clear()
        retry = self._record(
            store,
            second,
            requested_at="2026-08-04T12:00:05.000000Z",
        )
        self.assertEqual("already_applied", retry["disposition"])
        self.assertEqual([], self._writes(store))

    def test_active_to_active_never_advances_contact_or_row_epoch(self):
        store = self.fakes.BoundedFakeFirestore()
        self.fixture._seed_row(store, self.fixture.first)
        first, _ = self._seed_bundle(store, "source-epoch-first")
        second, _ = self._seed_bundle(store, "source-epoch-second")
        self._record(store, first)
        row_before = {
            path: deepcopy(document)
            for path, document in store.data.items()
            if "/row" in path
        }
        contact_before = {
            "heads": self._documents(store, "contactOptOutHeads"),
            "settlements": self._documents(store, "contactOptOutSettlements"),
            "fanouts": self._documents(store, "contactOptOutFanoutHeads"),
        }
        accepted = self._record(store, second, requested_at=self.later_at)
        self.assertEqual("already_active", accepted["disposition"])
        self.assertEqual(row_before, {path: deepcopy(document) for path, document in store.data.items() if "/row" in path})
        self.assertEqual(contact_before["heads"], self._documents(store, "contactOptOutHeads"))
        self.assertEqual(contact_before["settlements"], self._documents(store, "contactOptOutSettlements"))
        self.assertEqual(contact_before["fanouts"], self._documents(store, "contactOptOutFanoutHeads"))
        store.events.clear()
        retry = self._record(
            store,
            second,
            requested_at="2026-08-04T12:00:05.000000Z",
        )
        self.assertEqual("already_applied", retry["disposition"])
        self.assertEqual([], self._writes(store))

    def test_active_to_active_racing_release_retries_or_fails_stale(self):
        store = self.fakes.BoundedFakeFirestore()
        first, _ = self._seed_bundle(store, "source-release-first")
        second, _ = self._seed_bundle(store, "source-release-second")
        self._record(store, first)

        def apply_then_release(transaction, callback):
            transaction._begin()
            disposition = callback(transaction)
            transaction._commit()
            self._install_release_after_image(
                store,
                released_at="2026-08-04T12:00:05.000000Z",
            )
            raise RuntimeError(f"{disposition} raced authenticated release")

        accepted = self._record(
            store,
            second,
            executor=apply_then_release,
            requested_at=self.later_at,
        )
        self.assertEqual("already_active", accepted["disposition"])
        released_head = next(
            iter(self._documents(store, "contactOptOutHeads").values())
        )
        self.assertEqual("released", released_head["state"])
        store.events.clear()
        retry = self._record(
            store,
            second,
            requested_at="2026-08-04T12:00:06.000000Z",
        )
        self.assertEqual("already_applied", retry["disposition"])
        self.assertEqual("released", next(iter(self._documents(store, "contactOptOutHeads").values()))["state"])
        self.assertEqual([], self._writes(store))

    def test_active_to_active_requires_exact_current_fanout(self):
        for state in ("discovering", "applying", "complete"):
            with self.subTest(allowed=state):
                store = self.fakes.BoundedFakeFirestore()
                first, _ = self._seed_bundle(store, f"source-{state}-first")
                second, _ = self._seed_bundle(store, f"source-{state}-second")
                self._record(store, first)
                if state != "discovering":
                    self._set_current_fanout_state(store, state)
                accepted = self._record(store, second, requested_at="2026-08-04T12:00:05.000000Z")
                self.assertEqual("already_active", accepted["disposition"])
                store.events.clear()
                retry = self._record(store, second, requested_at="2026-08-04T12:00:06.000000Z")
                self.assertEqual("already_applied", retry["disposition"])
                self.assertEqual([], self._writes(store))

        for mode in ("superseding", "malformed"):
            with self.subTest(rejected=mode):
                store = self.fakes.BoundedFakeFirestore()
                first, _ = self._seed_bundle(store, f"source-{mode}-first")
                second, _ = self._seed_bundle(store, f"source-{mode}-second")
                self._record(store, first)
                if mode == "superseding":
                    self._set_current_fanout_state(store, mode)
                else:
                    fanout_id, fanout = next(
                        iter(self._documents(store, "contactOptOutFanoutHeads").items())
                    )
                    fanout["contactFanoutHeadHash"] = "f" * 64
                    self._user(store).collection("contactOptOutFanoutHeads").document(fanout_id).set(fanout, merge=False)
                before = deepcopy(store.data)
                store.events.clear()
                with self.assertRaises(self.module.RowAuthorityAmbiguous):
                    self._record(store, second, requested_at="2026-08-04T12:00:05.000000Z")
                self.assertEqual(before, store.data)
                self.assertEqual([], self._writes(store))

    def test_active_to_active_validates_active_settlements_creating_receipt(self):
        healthy = self.fakes.BoundedFakeFirestore()
        first, _ = self._seed_bundle(healthy, "source-proof-first")
        second, _ = self._seed_bundle(healthy, "source-proof-second")
        self._record(healthy, first)
        self._record(healthy, second, requested_at=self.later_at)
        healthy.events.clear()
        retry = self._record(
            healthy,
            second,
            requested_at="2026-08-04T12:00:05.000000Z",
        )
        self.assertEqual("already_applied", retry["disposition"])
        self.assertEqual([], self._writes(healthy))

        for mode in ("missing", "mismatched"):
            with self.subTest(mode=mode):
                store = self.fakes.BoundedFakeFirestore()
                original, _ = self._seed_bundle(store, f"source-{mode}-first")
                later, _ = self._seed_bundle(store, f"source-{mode}-second")
                created = self._record(store, original)
                receipt = created["transitionRequest"]
                receipt_ref = self._user(store).collection(
                    "contactOptOutTransitionRequests"
                ).document(receipt["contactTransitionId"])
                if mode == "missing":
                    receipt_ref.delete()
                else:
                    drifted = self.module.build_contact_transition_request_document(
                        user_scope_hash=receipt["userScopeHash"],
                        transition_kind=receipt["transitionKind"],
                        exact_identity_hash=receipt["exactIdentityHash"],
                        canonical_mailbox_identity_hash=receipt["canonicalMailboxIdentityHash"],
                        authority_link_hash=receipt["authorityLinkHash"],
                        hard_optout_evidence_hash=receipt["hardOptOutEvidenceHash"],
                        actor_scope_hash=None,
                        client_request_hash=None,
                        expected_active_optout_settlement_hash=None,
                        reason_code=None,
                        outcome="created",
                        resulting_contact_generation=receipt["resultingContactGeneration"],
                        resulting_contact_settlement_hash=receipt["resultingContactSettlementHash"],
                        resulting_fanout_id=receipt["resultingFanoutId"],
                        resulting_contact_head_hash="f" * 64,
                        resulting_fanout_head_hash=receipt["resultingFanoutHeadHash"],
                        requested_at=receipt["requestedAt"],
                    )
                    receipt_ref.set(drifted, merge=False)
                before = deepcopy(store.data)
                store.events.clear()
                with self.assertRaises(self.module.RowAuthorityAmbiguous):
                    self._record(store, later, requested_at=self.later_at)
                self.assertEqual(before, store.data)
                self.assertEqual([], self._writes(store))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
