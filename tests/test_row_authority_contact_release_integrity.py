"""Adversarial snapshot and chronology checks for authenticated release."""

from __future__ import annotations

import importlib
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier


class ContactReleaseIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")
        cls.releases = importlib.import_module(
            "tests.test_row_authority_contact_releases"
        )
        cls.releases.ContactReleaseTransitionTests.setUpClass()

    def setUp(self):
        self.case = self.releases.ContactReleaseTransitionTests(
            methodName="runTest"
        )
        self.case.setUp()

    def _binding_head_reference(self, active):
        canonical = active["settlement"]["canonicalMailboxIdentityHash"]
        return self.case._user(active["store"]).collection(
            "contactRowBindingHeads"
        ).document(canonical)

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

    def test_release_fanout_freezes_latest_binding_head_not_prior_fanout(self):
        active = self.case._active_authority(
            "source-release-binding-snapshot"
        )
        canonical = active["settlement"]["canonicalMailboxIdentityHash"]
        binding = self.module.build_contact_row_binding_head_document(
            user_scope_hash=self.case.transition.fixture.scope,
            canonical_mailbox_identity_hash=canonical,
            state_revision=4,
            association_count=3,
            last_association_hash="a" * 64,
            created_at="2026-08-04T12:00:04.000000Z",
            updated_at="2026-08-04T12:00:04.000000Z",
        )
        self._binding_head_reference(active).create(binding)

        released = self.case._release(active)

        fanout = released["fanoutHead"]
        self.assertEqual(4, fanout["bindingRevision"])
        self.assertEqual(
            binding["contactRowBindingHeadHash"],
            fanout["bindingHeadHash"],
        )
        self.assertEqual(3, fanout["bindingAssociationCount"])

    def test_release_binding_snapshot_retries_after_exact_head_cas(self):
        active = self.case._active_authority(
            "source-release-binding-cas"
        )
        canonical = active["settlement"]["canonicalMailboxIdentityHash"]
        first = self.module.build_contact_row_binding_head_document(
            user_scope_hash=self.case.transition.fixture.scope,
            canonical_mailbox_identity_hash=canonical,
            state_revision=4,
            association_count=3,
            last_association_hash="a" * 64,
            created_at="2026-08-04T12:00:04.000000Z",
            updated_at="2026-08-04T12:00:04.000000Z",
        )
        second = self.module.build_contact_row_binding_head_document(
            user_scope_hash=self.case.transition.fixture.scope,
            canonical_mailbox_identity_hash=canonical,
            state_revision=5,
            association_count=4,
            last_association_hash="b" * 64,
            created_at=first["createdAt"],
            updated_at="2026-08-04T12:00:04.500000Z",
        )
        reference = self._binding_head_reference(active)
        reference.create(first)
        active["store"].before_next_commit_hook = lambda: reference.set(
            second,
            merge=False,
        )
        active["store"].events.clear()

        released = self.case._release(active)

        fanout = released["fanoutHead"]
        self.assertEqual(5, fanout["bindingRevision"])
        self.assertEqual(
            second["contactRowBindingHeadHash"],
            fanout["bindingHeadHash"],
        )
        self.assertEqual(4, fanout["bindingAssociationCount"])
        self.assertIn(
            ("commit_aborted_stale_read", reference.path),
            active["store"].events,
        )

    def test_release_requires_active_settlement_exact_creating_receipt(self):
        for mode in ("missing", "crossed"):
            with self.subTest(mode=mode):
                active = self.case._active_authority(
                    f"source-release-creator-{mode}"
                )
                receipt = active["receipt"]
                reference = self.case._user(active["store"]).collection(
                    "contactOptOutTransitionRequests"
                ).document(receipt["contactTransitionId"])
                if mode == "missing":
                    reference.delete()
                else:
                    reference.set(
                        self._receipt_document(
                            receipt,
                            resulting_contact_head_hash="f" * 64,
                        ),
                        merge=False,
                    )
                before = deepcopy(active["store"].data)
                active["store"].events.clear()

                with self.assertRaises(self.module.RowAuthorityAmbiguous):
                    self.case._release(active)

                self.assertEqual(before, active["store"].data)
                self.assertEqual([], self.case._writes(active["store"]))

    def test_release_rejects_unreachable_current_apply_fanout(self):
        for mode in ("missing", "unreachable", "superseded"):
            with self.subTest(mode=mode):
                active = self.case._active_authority(
                    f"source-release-current-fanout-{mode}"
                )
                current = active["fanout"]
                reference = self.case._user(active["store"]).collection(
                    "contactOptOutFanoutHeads"
                ).document(current["fanoutId"])
                if mode == "missing":
                    reference.delete()
                elif mode == "unreachable":
                    reference.set(
                        self._fanout_document(
                            current,
                            state_revision=2,
                            state="discovering",
                            fencing_token=1,
                            updated_at="2026-08-04T12:00:04.000000Z",
                        ),
                        merge=False,
                    )
                else:
                    reference.set(
                        self._fanout_document(
                            current,
                            state_revision=3,
                            state="superseded",
                            fencing_token=2,
                            superseding_contact_settlement_hash="f" * 64,
                            updated_at="2026-08-04T12:00:04.000000Z",
                        ),
                        merge=False,
                    )
                before = deepcopy(active["store"].data)
                active["store"].events.clear()

                with self.assertRaises(self.module.RowAuthorityAmbiguous):
                    self.case._release(active)

                self.assertEqual(before, active["store"].data)
                self.assertEqual([], self.case._writes(active["store"]))

    def test_exact_release_retry_rejects_unreachable_historical_fanout(self):
        for mode in (
            "missing",
            "unreachable",
            "same_generation_superseding",
        ):
            with self.subTest(mode=mode):
                active = self.case._active_authority(
                    f"source-release-retry-fanout-{mode}"
                )
                created = self.case._release(active)
                current = created["fanoutHead"]
                reference = self.case._user(active["store"]).collection(
                    "contactOptOutFanoutHeads"
                ).document(current["fanoutId"])
                if mode == "missing":
                    reference.delete()
                else:
                    values = {
                        "state_revision": 2,
                        "state": "discovering",
                        "fencing_token": 1,
                        "updated_at": self.case.progress_at,
                    }
                    if mode == "same_generation_superseding":
                        values.update(
                            {
                                "state": "superseding",
                                "fencing_token": 2,
                                "superseding_contact_settlement_hash": "f"
                                * 64,
                            }
                        )
                    reference.set(
                        self._fanout_document(current, **values),
                        merge=False,
                    )
                before = deepcopy(active["store"].data)
                active["store"].events.clear()

                with self.assertRaises(self.module.RowAuthorityAmbiguous):
                    self.case._release(
                        active,
                        requested_at="2026-08-04T12:00:07.000000Z",
                    )

                self.assertEqual(before, active["store"].data)
                self.assertEqual([], self.case._writes(active["store"]))

    def test_old_release_retry_rejects_nonterminal_fanout_after_later_epoch(self):
        active = self.case._active_authority(
            "source-release-later-epoch-nonterminal"
        )
        released = self.case._release(active)
        later_bundle, _ = self.case.transition._seed_bundle(
            active["store"],
            "source-release-later-epoch-nonterminal-reopt",
        )
        self.case.transition._record(
            active["store"],
            later_bundle,
            requested_at=self.case.reopt_at,
        )
        historical = released["fanoutHead"]
        self.case._user(active["store"]).collection(
            "contactOptOutFanoutHeads"
        ).document(historical["fanoutId"]).set(historical, merge=False)
        before = deepcopy(active["store"].data)
        active["store"].events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.case._release(
                active,
                requested_at="2026-08-04T12:00:09.000000Z",
            )

        self.assertEqual(before, active["store"].data)
        self.assertEqual([], self.case._writes(active["store"]))

    def test_reoptout_rejects_missing_positive_binding_head(self):
        active = self.case._active_authority(
            "source-reoptout-missing-positive-binding"
        )
        canonical = active["settlement"]["canonicalMailboxIdentityHash"]
        binding = self.module.build_contact_row_binding_head_document(
            user_scope_hash=self.case.transition.fixture.scope,
            canonical_mailbox_identity_hash=canonical,
            state_revision=2,
            association_count=2,
            last_association_hash="a" * 64,
            created_at="2026-08-04T12:00:04.000000Z",
            updated_at="2026-08-04T12:00:04.000000Z",
        )
        reference = self._binding_head_reference(active)
        reference.create(binding)
        released = self.case._release(active)
        self.assertEqual(2, released["fanoutHead"]["bindingRevision"])
        self.assertEqual(
            binding["contactRowBindingHeadHash"],
            released["fanoutHead"]["bindingHeadHash"],
        )
        later_bundle, _ = self.case.transition._seed_bundle(
            active["store"],
            "source-reoptout-missing-positive-binding-next",
        )
        reference.delete()
        before = deepcopy(active["store"].data)
        active["store"].events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.case.transition._record(
                active["store"],
                later_bundle,
                requested_at=self.case.reopt_at,
            )

        self.assertEqual(before, active["store"].data)
        self.assertEqual([], self.case._writes(active["store"]))

    def test_release_rejects_regressed_binding_head(self):
        store = self.case.fakes.BoundedFakeFirestore()
        bundle, link = self.case.transition._seed_bundle(
            store,
            "source-release-regressed-binding",
        )
        canonical = link["canonicalMailboxIdentityHash"]
        binding_reference = self.case._user(store).collection(
            "contactRowBindingHeads"
        ).document(canonical)
        binding_two = self.module.build_contact_row_binding_head_document(
            user_scope_hash=self.case.transition.fixture.scope,
            canonical_mailbox_identity_hash=canonical,
            state_revision=2,
            association_count=2,
            last_association_hash="a" * 64,
            created_at="2026-08-04T12:00:02.000000Z",
            updated_at="2026-08-04T12:00:02.000000Z",
        )
        binding_reference.create(binding_two)
        created = self.case.transition._record(store, bundle)
        self.assertEqual(2, created["fanoutHead"]["bindingRevision"])
        self.assertEqual(
            binding_two["contactRowBindingHeadHash"],
            created["fanoutHead"]["bindingHeadHash"],
        )
        binding_reference.set(
            self.module.build_contact_row_binding_head_document(
                user_scope_hash=self.case.transition.fixture.scope,
                canonical_mailbox_identity_hash=canonical,
                state_revision=1,
                association_count=1,
                last_association_hash="b" * 64,
                created_at=binding_two["createdAt"],
                updated_at=binding_two["createdAt"],
            ),
            merge=False,
        )
        active = {
            "store": store,
            "settlement": created["settlement"],
        }
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.case._release(active)

        self.assertEqual(before, store.data)
        self.assertEqual([], self.case._writes(store))

    def test_release_rejects_future_dated_current_binding_head(self):
        active = self.case._active_authority(
            "source-release-future-binding"
        )
        canonical = active["settlement"]["canonicalMailboxIdentityHash"]
        binding = self.module.build_contact_row_binding_head_document(
            user_scope_hash=self.case.transition.fixture.scope,
            canonical_mailbox_identity_hash=canonical,
            state_revision=1,
            association_count=1,
            last_association_hash="a" * 64,
            created_at="2026-08-04T12:00:04.000000Z",
            updated_at=self.case.progress_at,
        )
        self._binding_head_reference(active).create(binding)
        before = deepcopy(active["store"].data)
        active["store"].events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self.case._release(active)

        self.assertEqual(before, active["store"].data)
        self.assertEqual([], self.case._writes(active["store"]))

    def test_release_preapply_and_partial_commit_uncertainty_are_exact(self):
        preapply = self.case._active_authority(
            "source-release-preapply"
        )
        before = deepcopy(preapply["store"].data)
        preapply["store"].events.clear()
        preapply["store"].fail_next_commit = RuntimeError(
            "release failed before apply"
        )

        with self.assertRaises(self.module.RowAuthorityRetryable):
            self.case._release(preapply)

        self.assertEqual(before, preapply["store"].data)
        self.assertEqual([], self.case._writes(preapply["store"]))

        def partial_executor(transaction, callback):
            transaction._begin()
            callback(transaction)
            operation, reference, payload, merge = transaction._operations[0]
            transaction._rollback()
            if operation == "create":
                reference.create(payload)
            elif operation == "set":
                reference.set(payload, merge=merge)
            else:  # pragma: no cover - release planner is create/set only
                raise AssertionError(operation)
            raise RuntimeError("partial release apply")

        partial = self.case._active_authority("source-release-partial")
        partial["store"].events.clear()
        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.case._release(partial, executor=partial_executor)
        self.assertEqual(1, len(self.case._writes(partial["store"])))

    def test_release_apply_raise_competing_fanout_progress_fails_closed(self):
        active = self.case._active_authority(
            "source-release-apply-raise-fanout-progress"
        )
        observed = {}

        def apply_mutate_raise_executor(transaction, callback):
            transaction._begin()
            callback(transaction)
            fanout_reference, fanout = next(
                (reference, payload)
                for operation, reference, payload, _merge
                in transaction._operations
                if operation == "create"
                and "/contactOptOutFanoutHeads/" in reference.path
            )
            transaction._commit()
            observed["advanced"] = self.case._advance_fanout(
                active["store"],
                fanout["fanoutId"],
                state="applying",
                updated_at=self.case.progress_at,
            )
            self.assertEqual(fanout_reference.id, fanout["fanoutId"])
            raise RuntimeError("release result raced fan-out progress")

        active["store"].events.clear()
        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.case._release(
                active,
                executor=apply_mutate_raise_executor,
            )

        active["store"].events.clear()
        retry = self.case._release(
            active,
            requested_at="2026-08-04T12:00:07.000000Z",
        )

        self.assertEqual("already_applied", retry["disposition"])
        self.assertEqual(observed["advanced"], retry["fanoutHead"])
        self.assertEqual([], self.case._writes(active["store"]))

    def test_distinct_release_ids_race_to_one_epoch(self):
        active = self.case._active_authority("source-release-distinct-race")
        active["store"].events.clear()
        active["store"].before_commit_barrier = Barrier(2)

        def attempt(client_request_id):
            try:
                result = self.case._release(
                    active,
                    client_request_id=client_request_id,
                )
                return result["disposition"]
            except self.module.RowAuthorityConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    attempt,
                    ("release-race-one", "release-race-two"),
                )
            )

        self.assertEqual(["conflict", "created"], sorted(results))
        self.assertEqual(
            2,
            len(
                self.case._documents(
                    active["store"], "contactOptOutSettlements"
                )
            ),
        )
        self.assertEqual(
            1,
            active["store"].events.count(("commit_applied", 5)),
        )

    def test_release_rejects_validly_rehashed_alias_chronology_drift(self):
        active = self.case._active_authority(
            "source-release-alias-chronology"
        )
        canonical = active["settlement"]["canonicalMailboxIdentityHash"]
        drifted = self.module.build_contact_alias_document(
            user_scope_hash=self.case.transition.fixture.scope,
            exact_identity_hash=canonical,
            canonical_mailbox_identity_hash=canonical,
            created_at="2026-08-04T12:00:02.000000Z",
        )
        self.case._user(active["store"]).collection(
            "contactOptOutAliases"
        ).document(canonical).set(drifted, merge=False)
        before = deepcopy(active["store"].data)
        active["store"].events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.case._release(active)

        self.assertEqual(before, active["store"].data)
        self.assertEqual([], self.case._writes(active["store"]))

    def test_release_request_cannot_predate_current_fanout_progress(self):
        active = self.case._active_authority(
            "source-release-backdated"
        )
        self.case._advance_fanout(
            active["store"],
            active["fanout"]["fanoutId"],
            state="applying",
            updated_at=self.case.progress_at,
        )
        before = deepcopy(active["store"].data)
        active["store"].events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self.case._release(active, requested_at=self.case.release_at)

        self.assertEqual(before, active["store"].data)
        self.assertEqual([], self.case._writes(active["store"]))

    def test_release_retry_rejects_reconstructible_receipt_head_hash_drift(self):
        for mode in ("release_result", "expected_active_creator"):
            with self.subTest(mode=mode):
                active = self.case._active_authority(
                    f"source-release-receipt-head-drift-{mode}"
                )
                released = self.case._release(active)
                if mode == "release_result":
                    later_bundle, _ = self.case.transition._seed_bundle(
                        active["store"],
                        f"source-release-receipt-head-drift-{mode}-next",
                    )
                    self.case.transition._record(
                        active["store"],
                        later_bundle,
                        requested_at=self.case.reopt_at,
                    )
                    receipt = released["transitionRequest"]
                    retry_at = "2026-08-04T12:00:09.000000Z"
                else:
                    receipt = active["receipt"]
                    retry_at = "2026-08-04T12:00:07.000000Z"
                self.assertNotEqual(
                    "f" * 64,
                    receipt["resultingContactHeadHash"],
                )
                self.case._user(active["store"]).collection(
                    "contactOptOutTransitionRequests"
                ).document(receipt["contactTransitionId"]).set(
                    self._receipt_document(
                        receipt,
                        resulting_contact_head_hash="f" * 64,
                    ),
                    merge=False,
                )
                before = deepcopy(active["store"].data)
                active["store"].events.clear()

                with self.assertRaises(self.module.RowAuthorityAmbiguous):
                    self.case._release(active, requested_at=retry_at)

                self.assertEqual(before, active["store"].data)
                self.assertEqual([], self.case._writes(active["store"]))

    def test_historical_superseding_fanout_requires_advanced_revision(self):
        for mode in ("lease", "cursor"):
            with self.subTest(mode=mode):
                active = self.case._active_authority(
                    f"source-release-superseding-revision-{mode}"
                )
                released = self.case._release(active)
                later_bundle, _ = self.case.transition._seed_bundle(
                    active["store"],
                    f"source-release-superseding-revision-{mode}-next",
                )
                self.case.transition._record(
                    active["store"],
                    later_bundle,
                    requested_at=self.case.reopt_at,
                )
                fanout_id = released["fanoutHead"]["fanoutId"]
                reference = self.case._user(active["store"]).collection(
                    "contactOptOutFanoutHeads"
                ).document(fanout_id)
                current = self.case._documents(
                    active["store"],
                    "contactOptOutFanoutHeads",
                )[fanout_id]
                self.assertEqual("superseding", current["state"])
                self.assertEqual(2, current["stateRevision"])
                self.assertEqual(2, current["fencingToken"])
                overrides = {}
                if mode == "lease":
                    overrides.update(
                        lease_owner_hash="b" * 64,
                        lease_until="2026-08-04T12:30:00.000000Z",
                    )
                else:
                    overrides["discovery_cursor_row_id"] = (
                        "sr1_123e4567e89b42d3a456426614174000"
                    )
                reference.set(
                    self._fanout_document(current, **overrides),
                    merge=False,
                )
                before = deepcopy(active["store"].data)
                active["store"].events.clear()

                with self.assertRaises(self.module.RowAuthorityAmbiguous):
                    self.case._release(
                        active,
                        requested_at="2026-08-04T12:00:09.000000Z",
                    )

                self.assertEqual(before, active["store"].data)
                self.assertEqual([], self.case._writes(active["store"]))

    def test_exact_release_retry_ignores_unrelated_later_binding_state(self):
        active = self.case._active_authority(
            "source-release-binding-retry"
        )
        created = self.case._release(active)
        self._binding_head_reference(active).set(
            {"schemaVersion": 1, "malformed": True},
            merge=False,
        )
        before = deepcopy(active["store"].data)
        active["store"].events.clear()

        retry = self.case._release(
            active,
            requested_at="2026-08-04T12:00:07.000000Z",
        )

        self.assertEqual("already_applied", retry["disposition"])
        self.assertEqual(
            created["transitionRequest"],
            retry["transitionRequest"],
        )
        self.assertEqual(before, active["store"].data)
        self.assertEqual([], self.case._writes(active["store"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
