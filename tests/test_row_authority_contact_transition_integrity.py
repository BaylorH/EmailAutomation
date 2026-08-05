"""Adversarial chronology and forward-reachability checks for B2-C."""

from __future__ import annotations

import importlib
import unittest
from copy import deepcopy
from unittest.mock import patch


class ContactTransitionIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")
        cls.transitions = importlib.import_module(
            "tests.test_row_authority_contact_transitions"
        )
        cls.fakes = importlib.import_module("tests.row_authority_fakes")
        cls.transitions.ContactTransitionTests.setUpClass()

    def setUp(self):
        self.case = self.transitions.ContactTransitionTests(
            methodName="runTest"
        )
        self.case.setUp()

    def _fanout_document(self, current, **overrides):
        values = {
            "user_scope_hash": current["userScopeHash"],
            "fanout_id": current["fanoutId"],
            "outcome": current["outcome"],
            "expected_contact_settlement_hash": current[
                "expectedContactSettlementHash"
            ],
            "state_revision": current["stateRevision"],
            "state": current["state"],
            "binding_revision": current["bindingRevision"],
            "binding_head_hash": current["bindingHeadHash"],
            "binding_association_count": current[
                "bindingAssociationCount"
            ],
            "discovery_cursor_row_id": current["discoveryCursorRowId"],
            "obligation_count": current["obligationCount"],
            "result_count": current["resultCount"],
            "lease_owner_hash": current["leaseOwnerHash"],
            "lease_until": current["leaseUntil"],
            "fencing_token": current["fencingToken"],
            "superseding_contact_settlement_hash": current[
                "supersedingContactSettlementHash"
            ],
            "completion_binding_revision": current[
                "completionBindingRevision"
            ],
            "completion_binding_head_hash": current[
                "completionBindingHeadHash"
            ],
            "completion_binding_association_count": current[
                "completionBindingAssociationCount"
            ],
            "completion_obligation_count": current[
                "completionObligationCount"
            ],
            "completion_result_count": current[
                "completionResultCount"
            ],
            "completed_at": current["completedAt"],
            "created_at": current["createdAt"],
            "updated_at": current["updatedAt"],
        }
        values.update(overrides)
        return self.module.build_contact_fanout_head_document(**values)

    def _receipt_document(self, receipt, **overrides):
        values = {
            "user_scope_hash": receipt["userScopeHash"],
            "transition_kind": receipt["transitionKind"],
            "exact_identity_hash": receipt["exactIdentityHash"],
            "canonical_mailbox_identity_hash": receipt[
                "canonicalMailboxIdentityHash"
            ],
            "authority_link_hash": receipt["authorityLinkHash"],
            "hard_optout_evidence_hash": receipt[
                "hardOptOutEvidenceHash"
            ],
            "actor_scope_hash": receipt["actorScopeHash"],
            "client_request_hash": receipt["clientRequestHash"],
            "expected_active_optout_settlement_hash": receipt[
                "expectedActiveOptOutSettlementHash"
            ],
            "reason_code": receipt["reasonCode"],
            "outcome": receipt["outcome"],
            "resulting_contact_generation": receipt[
                "resultingContactGeneration"
            ],
            "resulting_contact_settlement_hash": receipt[
                "resultingContactSettlementHash"
            ],
            "resulting_fanout_id": receipt["resultingFanoutId"],
            "resulting_contact_head_hash": receipt[
                "resultingContactHeadHash"
            ],
            "resulting_fanout_head_hash": receipt[
                "resultingFanoutHeadHash"
            ],
            "requested_at": receipt["requestedAt"],
        }
        values.update(overrides)
        return self.module.build_contact_transition_request_document(**values)

    def test_backdated_active_to_active_receipt_is_zero_write_conflict(self):
        store = self.fakes.BoundedFakeFirestore()
        first, _ = self.case._seed_bundle(store, "source-time-first")
        second, _ = self.case._seed_bundle(store, "source-time-second")
        self.case._record(store, first)
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self.case._record(
                store,
                second,
                requested_at="2026-08-04T12:00:02.000000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], self.case._writes(store))

    def test_active_to_active_ignores_unrelated_binding_state(self):
        store = self.fakes.BoundedFakeFirestore()
        first, _ = self.case._seed_bundle(store, "source-binding-first")
        second, _ = self.case._seed_bundle(store, "source-binding-second")
        created = self.case._record(store, first)
        canonical = created["settlement"]["canonicalMailboxIdentityHash"]
        self.case._user(store).collection(
            "contactRowBindingHeads"
        ).document(canonical).set(
            {"schemaVersion": 1, "malformed": True},
            merge=False,
        )
        store.events.clear()

        accepted = self.case._record(
            store,
            second,
            requested_at=self.case.later_at,
        )

        self.assertEqual("already_active", accepted["disposition"])
        self.assertEqual(1, len(self.case._writes(store)))

    def test_active_to_active_requires_current_creator_exact_alias(self):
        store = self.fakes.BoundedFakeFirestore()
        first, first_link = self.case._seed_bundle(
            store,
            "source-current-creator-alias-active",
        )
        second, _ = self.case._seed_bundle(
            store,
            "source-current-creator-alias-active-next",
            exact_hash="6" * 64,
        )
        self.case._record(store, first)
        creator_exact = first_link["exactIdentityHash"]
        self.assertNotEqual(
            creator_exact,
            first_link["canonicalMailboxIdentityHash"],
        )
        self.case._user(store).collection(
            "contactOptOutAliases"
        ).document(creator_exact).delete()
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.case._record(
                store,
                second,
                requested_at=self.case.later_at,
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], self.case._writes(store))

    def test_reoptout_requires_current_creator_exact_alias(self):
        store = self.fakes.BoundedFakeFirestore()
        _old, old_link = self.case._seed_bundle(
            store,
            "source-current-creator-alias-released",
        )
        self.case._seed_released_authority(store, old_link)
        new_bundle, _ = self.case._seed_bundle(
            store,
            "source-current-creator-alias-released-next",
            exact_hash="6" * 64,
        )
        creator_exact = old_link["exactIdentityHash"]
        self.assertNotEqual(
            creator_exact,
            old_link["canonicalMailboxIdentityHash"],
        )
        self.case._user(store).collection(
            "contactOptOutAliases"
        ).document(creator_exact).delete()
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.case._record(
                store,
                new_bundle,
                requested_at="2026-08-04T12:00:05.000000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], self.case._writes(store))

    def test_canonical_self_alias_time_must_equal_head_creation_time(self):
        store = self.fakes.BoundedFakeFirestore()
        first, first_link = self.case._seed_bundle(
            store,
            "source-alias-time-first",
        )
        second, _ = self.case._seed_bundle(
            store,
            "source-alias-time-second",
        )
        self.case._record(store, first)
        canonical_hash = first_link["canonicalMailboxIdentityHash"]
        self_alias = self.module.build_contact_alias_document(
            user_scope_hash=self.case.fixture.scope,
            exact_identity_hash=canonical_hash,
            canonical_mailbox_identity_hash=canonical_hash,
            created_at="2026-08-04T12:00:02.000000Z",
        )
        self.case._user(store).collection(
            "contactOptOutAliases"
        ).document(canonical_hash).set(self_alias, merge=False)
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.case._record(
                store,
                second,
                requested_at=self.case.later_at,
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], self.case._writes(store))

    def test_preexisting_self_alias_time_becomes_initial_head_time(self):
        store = self.fakes.BoundedFakeFirestore()
        bundle, link = self.case._seed_bundle(
            store,
            "source-preexisting-self-alias",
        )
        canonical = link["canonicalMailboxIdentityHash"]
        alias_time = "2026-08-04T12:00:02.000000Z"
        self_alias = self.module.build_contact_alias_document(
            user_scope_hash=self.case.fixture.scope,
            exact_identity_hash=canonical,
            canonical_mailbox_identity_hash=canonical,
            created_at=alias_time,
        )
        self.case._user(store).collection(
            "contactOptOutAliases"
        ).document(canonical).create(self_alias)

        created = self.case._record(store, bundle)

        self.assertEqual(alias_time, created["head"]["createdAt"])
        store.events.clear()
        retry = self.case._record(
            store,
            bundle,
            requested_at=self.case.later_at,
        )
        self.assertEqual("already_applied", retry["disposition"])
        self.assertEqual([], self.case._writes(store))

    def test_preexisting_alias_cannot_postdate_new_request(self):
        store = self.fakes.BoundedFakeFirestore()
        bundle, link = self.case._seed_bundle(
            store,
            "source-postdated-self-alias",
        )
        canonical = link["canonicalMailboxIdentityHash"]
        self_alias = self.module.build_contact_alias_document(
            user_scope_hash=self.case.fixture.scope,
            exact_identity_hash=canonical,
            canonical_mailbox_identity_hash=canonical,
            created_at="2026-08-04T12:00:04.000000Z",
        )
        self.case._user(store).collection(
            "contactOptOutAliases"
        ).document(canonical).create(self_alias)
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self.case._record(store, bundle)

        self.assertEqual(before, store.data)
        self.assertEqual([], self.case._writes(store))

    def test_reoptout_cannot_predate_completed_release_fanout(self):
        store = self.fakes.BoundedFakeFirestore()
        old_bundle, old_link = self.case._seed_bundle(
            store,
            "source-release-time-old",
        )
        self.case._seed_released_authority(store, old_link)
        new_bundle, _ = self.case._seed_bundle(
            store,
            "source-release-time-new",
        )
        del old_bundle
        released_head = next(
            iter(self.case._documents(store, "contactOptOutHeads").values())
        )
        current = self.case._documents(
            store,
            "contactOptOutFanoutHeads",
        )[released_head["activeFanoutId"]]
        completed = self.module.build_contact_fanout_head_document(
            user_scope_hash=current["userScopeHash"],
            fanout_id=current["fanoutId"],
            outcome=current["outcome"],
            expected_contact_settlement_hash=current[
                "expectedContactSettlementHash"
            ],
            state_revision=3,
            state="complete",
            binding_revision=current["bindingRevision"],
            binding_head_hash=current["bindingHeadHash"],
            binding_association_count=current["bindingAssociationCount"],
            discovery_cursor_row_id=None,
            obligation_count=0,
            result_count=0,
            lease_owner_hash=None,
            lease_until=None,
            fencing_token=2,
            superseding_contact_settlement_hash=None,
            completion_binding_revision=current["bindingRevision"],
            completion_binding_head_hash=current["bindingHeadHash"],
            completion_binding_association_count=0,
            completion_obligation_count=0,
            completion_result_count=0,
            completed_at="2026-08-04T12:00:05.000000Z",
            created_at=current["createdAt"],
            updated_at="2026-08-04T12:00:05.000000Z",
        )
        self.case._user(store).collection(
            "contactOptOutFanoutHeads"
        ).document(current["fanoutId"]).set(completed, merge=False)
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self.case._record(
                store,
                new_bundle,
                requested_at="2026-08-04T12:00:04.500000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], self.case._writes(store))

    def test_exact_retry_rejects_unreachable_unleased_revision_two(self):
        store = self.fakes.BoundedFakeFirestore()
        bundle, _ = self.case._seed_bundle(store, "source-fanout-drift")
        created = self.case._record(store, bundle)
        current = created["fanoutHead"]
        unreachable = self.module.build_contact_fanout_head_document(
            user_scope_hash=current["userScopeHash"],
            fanout_id=current["fanoutId"],
            outcome=current["outcome"],
            expected_contact_settlement_hash=current[
                "expectedContactSettlementHash"
            ],
            state_revision=2,
            state="discovering",
            binding_revision=current["bindingRevision"],
            binding_head_hash=current["bindingHeadHash"],
            binding_association_count=current["bindingAssociationCount"],
            discovery_cursor_row_id=None,
            obligation_count=current["obligationCount"],
            result_count=current["resultCount"],
            lease_owner_hash=None,
            lease_until=None,
            fencing_token=1,
            superseding_contact_settlement_hash=None,
            completion_binding_revision=None,
            completion_binding_head_hash=None,
            completion_binding_association_count=None,
            completion_obligation_count=None,
            completion_result_count=None,
            completed_at=None,
            created_at=current["createdAt"],
            updated_at=self.case.later_at,
        )
        self.case._user(store).collection(
            "contactOptOutFanoutHeads"
        ).document(current["fanoutId"]).set(unreachable, merge=False)
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.case._record(
                store,
                bundle,
                requested_at="2026-08-04T12:00:05.000000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], self.case._writes(store))

    def test_same_generation_head_cannot_point_to_superseding_fanout(self):
        store = self.fakes.BoundedFakeFirestore()
        bundle, _ = self.case._seed_bundle(
            store,
            "source-same-generation-superseding",
        )
        created = self.case._record(store, bundle)
        superseding = self.module._build_contact_superseding_fanout_head(
            current_document=created["fanoutHead"],
            superseding_contact_settlement_hash="f" * 64,
            updated_at=self.case.later_at,
        )
        self.case._user(store).collection(
            "contactOptOutFanoutHeads"
        ).document(superseding["fanoutId"]).set(
            superseding,
            merge=False,
        )
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.case._record(
                store,
                bundle,
                requested_at="2026-08-04T12:00:05.000000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], self.case._writes(store))

    def test_old_optout_retry_rejects_nonterminal_fanout_after_later_epoch(self):
        for state in ("discovering", "applying"):
            with self.subTest(state=state):
                store = self.fakes.BoundedFakeFirestore()
                bundle, _ = self.case._seed_bundle(
                    store,
                    f"source-old-optout-nonterminal-{state}",
                )
                created = self.case._record(store, bundle)
                self.case._install_release_after_image(
                    store,
                    released_at="2026-08-04T12:00:05.000000Z",
                )
                historical = created["fanoutHead"]
                if state == "applying":
                    historical = self._fanout_document(
                        historical,
                        state_revision=2,
                        state="applying",
                        lease_owner_hash="b" * 64,
                        lease_until="2026-08-04T12:05:00.000000Z",
                        fencing_token=2,
                        updated_at="2026-08-04T12:00:04.000000Z",
                    )
                self.case._user(store).collection(
                    "contactOptOutFanoutHeads"
                ).document(historical["fanoutId"]).set(
                    historical,
                    merge=False,
                )
                before = deepcopy(store.data)
                store.events.clear()

                with self.assertRaises(self.module.RowAuthorityAmbiguous):
                    self.case._record(
                        store,
                        bundle,
                        requested_at="2026-08-04T12:00:07.000000Z",
                    )

                self.assertEqual(before, store.data)
                self.assertEqual([], self.case._writes(store))

    def test_old_optout_retry_rejects_created_receipt_head_hash_drift(self):
        store = self.fakes.BoundedFakeFirestore()
        bundle, _ = self.case._seed_bundle(
            store,
            "source-old-optout-receipt-head-drift",
        )
        created = self.case._record(store, bundle)
        self.case._install_release_after_image(
            store,
            released_at="2026-08-04T12:00:05.000000Z",
        )
        receipt = created["transitionRequest"]
        self.assertNotEqual(
            "f" * 64,
            receipt["resultingContactHeadHash"],
        )
        self.case._user(store).collection(
            "contactOptOutTransitionRequests"
        ).document(receipt["contactTransitionId"]).set(
            self._receipt_document(
                receipt,
                resulting_contact_head_hash="f" * 64,
            ),
            merge=False,
        )
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.case._record(
                store,
                bundle,
                requested_at="2026-08-04T12:00:07.000000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], self.case._writes(store))

    def test_old_optout_retry_requires_later_current_creator_exact_alias(self):
        store = self.fakes.BoundedFakeFirestore()
        old_bundle, _ = self.case._seed_bundle(
            store,
            "source-old-retry-current-creator-alias",
        )
        self.case._record(store, old_bundle)
        self.case._install_release_after_image(
            store,
            released_at="2026-08-04T12:00:05.000000Z",
        )
        current_bundle, current_link = self.case._seed_bundle(
            store,
            "source-old-retry-current-creator-alias-next",
            exact_hash="6" * 64,
        )
        self.case._record(
            store,
            current_bundle,
            requested_at="2026-08-04T12:00:08.000000Z",
        )
        self.case._user(store).collection(
            "contactOptOutAliases"
        ).document(current_link["exactIdentityHash"]).delete()
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.case._record(
                store,
                old_bundle,
                requested_at="2026-08-04T12:00:09.000000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], self.case._writes(store))

    def test_later_head_requires_an_exact_predecessor_generation_chain(self):
        store = self.fakes.BoundedFakeFirestore()
        first_bundle, _ = self.case._seed_bundle(
            store,
            "source-chain-first",
        )
        created = self.case._record(store, first_bundle)
        _later_bundle, later_link = self.case._seed_bundle(
            store,
            "source-chain-forged-third",
        )
        scope = self.case.fixture.scope
        canonical = later_link["canonicalMailboxIdentityHash"]
        transition_id = self.module._verified_contact_transition_id(
            user_scope_hash=scope,
            authority_link=later_link,
        )
        settled_at = "2026-08-04T12:00:08.000000Z"
        settlement = self.module.build_contact_settlement_document(
            user_scope_hash=scope,
            canonical_mailbox_identity_hash=canonical,
            generation=3,
            predecessor_settlement_hash="a" * 64,
            transition_kind="verified_optout",
            contact_transition_id=transition_id,
            exact_identity_hash=later_link["exactIdentityHash"],
            authority_link=later_link,
            actor_scope_hash=None,
            reason_code=None,
            settled_at=settled_at,
        )
        fanout_id = self.module.domain_hash(
            self.module.CONTACT_FANOUT_ID_DOMAIN,
            {
                "contactSettlementHash": settlement[
                    "contactSettlementHash"
                ],
                "outcome": "apply",
            },
            user_scope_hash=scope,
        )
        fanout = self.module.build_contact_fanout_head_document(
            user_scope_hash=scope,
            fanout_id=fanout_id,
            outcome="apply",
            expected_contact_settlement_hash=settlement[
                "contactSettlementHash"
            ],
            state_revision=1,
            state="discovering",
            binding_revision=0,
            binding_head_hash=None,
            binding_association_count=0,
            discovery_cursor_row_id=None,
            obligation_count=0,
            result_count=0,
            lease_owner_hash=None,
            lease_until=None,
            fencing_token=1,
            superseding_contact_settlement_hash=None,
            completion_binding_revision=None,
            completion_binding_head_hash=None,
            completion_binding_association_count=None,
            completion_obligation_count=None,
            completion_result_count=None,
            completed_at=None,
            created_at=settled_at,
            updated_at=settled_at,
        )
        head = self.module.build_contact_head_document(
            user_scope_hash=scope,
            canonical_mailbox_identity_hash=canonical,
            state_revision=3,
            latest_generation=3,
            latest_settlement_hash=settlement["contactSettlementHash"],
            active_optout_settlement_hash=settlement[
                "contactSettlementHash"
            ],
            state="active",
            active_fanout_id=fanout_id,
            created_at=created["head"]["createdAt"],
            updated_at=settled_at,
        )
        receipt = self.module.build_contact_transition_request_document(
            user_scope_hash=scope,
            transition_kind="verified_optout",
            exact_identity_hash=later_link["exactIdentityHash"],
            canonical_mailbox_identity_hash=canonical,
            authority_link_hash=later_link["authorityLinkHash"],
            hard_optout_evidence_hash=later_link[
                "hardOptOutEvidenceHash"
            ],
            actor_scope_hash=None,
            client_request_hash=None,
            expected_active_optout_settlement_hash=None,
            reason_code=None,
            outcome="created",
            resulting_contact_generation=3,
            resulting_contact_settlement_hash=settlement[
                "contactSettlementHash"
            ],
            resulting_fanout_id=fanout_id,
            resulting_contact_head_hash=head["contactHeadHash"],
            resulting_fanout_head_hash=fanout["contactFanoutHeadHash"],
            requested_at=settled_at,
        )
        user = self.case._user(store)
        user.collection("contactOptOutSettlements").document(
            f"{canonical}--3"
        ).create(settlement)
        user.collection("contactOptOutTransitionRequests").document(
            transition_id
        ).create(receipt)
        user.collection("contactOptOutFanoutHeads").document(
            fanout_id
        ).create(fanout)
        user.collection("contactOptOutHeads").document(canonical).set(
            head,
            merge=False,
        )
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.case._record(
                store,
                first_bundle,
                requested_at="2026-08-04T12:00:09.000000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], self.case._writes(store))

    def test_exact_retry_ignores_observation_time_before_b1_readiness(self):
        store = self.fakes.BoundedFakeFirestore()
        bundle, _ = self.case._seed_bundle(
            store,
            "source-early-retry",
        )
        created = self.case._record(store, bundle)
        before = deepcopy(store.data)
        store.events.clear()

        retry = self.case._record(
            store,
            bundle,
            requested_at="2026-08-04T10:00:00.000000Z",
        )

        self.assertEqual("already_applied", retry["disposition"])
        self.assertEqual(created["transitionRequest"], retry["transitionRequest"])
        self.assertEqual(before, store.data)
        self.assertEqual([], self.case._writes(store))

    def test_snapshot_materialization_failure_is_retryable(self):
        store = self.fakes.BoundedFakeFirestore()
        bundle, _ = self.case._seed_bundle(
            store,
            "source-snapshot-failure",
        )
        snapshot_type = type(
            self.case._user(store).collection("sourceIdentities").document(
                bundle["identity"]["canonicalSourceId"]
            ).get()
        )
        before = deepcopy(store.data)
        store.events.clear()

        with patch.object(
            snapshot_type,
            "to_dict",
            side_effect=RuntimeError("snapshot materialization failed"),
        ), self.assertRaises(self.module.RowAuthorityRetryable):
            self.case._record(store, bundle)

        self.assertEqual(before, store.data)
        self.assertEqual([], self.case._writes(store))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
