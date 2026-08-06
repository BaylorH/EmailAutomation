"""RED contracts for active contact fan-out late-row convergence."""

from __future__ import annotations

import unittest
from copy import deepcopy

from tests.test_row_authority_contact_fanout_completion import (
    ContactFanoutCompletionTests,
)
from tests.test_row_authority_contact_fanout_discovery import _row_id
from tests.test_row_authority_ownership import (
    ContactRowAssociationStoreTests,
)


class ContactLateAssociationTests(unittest.TestCase):
    """Compose the retained B2-B association fixture with active B2-C fan-out."""

    COMPLETION_FIELDS = (
        "completionBindingRevision",
        "completionBindingHeadHash",
        "completionBindingAssociationCount",
        "completionObligationCount",
        "completionResultCount",
        "completedAt",
    )

    @classmethod
    def setUpClass(cls):
        cls.completion_type = ContactFanoutCompletionTests
        cls.completion_type.setUpClass()
        cls.association_type = ContactRowAssociationStoreTests
        cls.association_type.setUpClass()
        cls.module = cls.completion_type.module

    def setUp(self):
        self.completion = self.completion_type(methodName="runTest")
        self.completion.setUp()
        self.discovery = self.completion.discovery
        self.association = self.association_type(methodName="runTest")
        self.association.setUp()
        self.associated_at = "2026-08-04T12:07:00.000000Z"

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

    def _reference(self, context, collection, document_id):
        return self.discovery._reference(
            context,
            collection,
            document_id,
        )

    def _current_fanout(self, context):
        return self._documents(
            context,
            "contactOptOutFanoutHeads",
        )[context["fanout"]["fanoutId"]]

    def _configure_association(
        self,
        context,
        *,
        row_id,
        thread_id="thread-1",
    ):
        fixture = self.association
        fixture.user_id = context["transition"].fixture.user_id
        fixture.scope = context["scope"]
        fixture.first = row_id
        fixture.second = _row_id(99)
        fixture.thread_id = thread_id
        fixture.canonical_hash = context["canonicalHash"]
        fixture.exact_hash = context["receipt"]["exactIdentityHash"]
        fixture.association_at = self.associated_at
        return fixture

    def _seed_association_prerequisites(
        self,
        context,
        *,
        row_id,
        lifecycle="active",
        thread_id="thread-1",
    ):
        fixture = self._configure_association(
            context,
            row_id=row_id,
            thread_id=thread_id,
        )
        rows, binding = fixture._seed_prerequisites(
            context["store"],
            row_ids=(row_id,),
            lifecycle=lifecycle,
            thread_id=thread_id,
        )
        context["store"].events.clear()
        return rows[row_id], binding[0]

    def _associate(
        self,
        context,
        *,
        row_id,
        exact_identity_hash=None,
        thread_id="thread-1",
        created_at=None,
        executor=None,
    ):
        try:
            return self.association._associate(
                context["store"],
                executor=executor,
                canonical_mailbox_identity_hash=context["canonicalHash"],
                exact_identity_hash=(
                    exact_identity_hash
                    or context["receipt"]["exactIdentityHash"]
                ),
                row_id=row_id,
                thread_id=thread_id,
                created_at=created_at or self.associated_at,
            )
        except self.module.RowAuthorityError as exc:
            self.fail(
                "active late association must converge through the current "
                f"fan-out, got {type(exc).__name__}: {exc}"
            )

    def _seed_complete(self):
        context = self.completion._seed(0)
        certified = self.completion._certify(context)
        self.assertEqual("certification_complete", certified["disposition"])
        context["fanout"] = certified["fanoutHead"]
        context["store"].events.clear()
        return context

    def _assert_late_artifacts(self, context, row_id):
        fanout = self._current_fanout(context)
        binding = self._documents(
            context,
            "contactRowBindingHeads",
        )[context["canonicalHash"]]
        obligation = self._documents(
            context,
            "contactOptOutFanoutObligations",
        )[f"{fanout['fanoutId']}--{row_id}"]
        edge = next(
            document
            for document in self._documents(
                context,
                "contactRowBindings",
            ).values()
            if document["rowId"] == row_id
        )
        self.assertEqual(
            obligation,
            self.module.validate_contact_fanout_obligation_document(
                document=obligation
            ),
        )
        self.assertEqual(edge["contactRowEdgeHash"], obligation["contactRowEdgeHash"])
        self.assertEqual(
            context["settlement"]["contactSettlementHash"],
            obligation["expectedContactSettlementHash"],
        )
        self.assertEqual("apply", obligation["outcome"])
        self.assertEqual(self.associated_at, obligation["createdAt"])
        return fanout, binding, obligation, edge

    def _assert_complete_recertification(
        self,
        *,
        before,
        after,
        binding,
    ):
        expected = self.discovery._fanout(
            before,
            state_revision=before["stateRevision"] + 1,
            binding_revision=binding["stateRevision"],
            binding_head_hash=binding["contactRowBindingHeadHash"],
            binding_association_count=binding["associationCount"],
            obligation_count=before["obligationCount"] + 1,
            result_count=before["resultCount"] + 1,
            updated_at=self.associated_at,
        )
        self.assertEqual(expected, after)
        self.assertEqual("complete", after["state"])
        self.assertIsNone(after["leaseOwnerHash"])
        self.assertIsNone(after["leaseUntil"])
        self.assertIsNone(after["discoveryCursorRowId"])
        self.assertEqual(
            {field: before[field] for field in self.COMPLETION_FIELDS},
            {field: after[field] for field in self.COMPLETION_FIELDS},
        )

    def test_nonterminal_association_adds_obligation_and_resets_cursor_atomically(self):
        first_row = _row_id(1)
        late_row = _row_id(2)
        context = self.discovery._seed(
            (first_row,),
            cursor=first_row,
            obligation_count=1,
        )
        self.discovery._obligation(context, context["edges"][0])
        self._seed_association_prerequisites(
            context,
            row_id=late_row,
            thread_id="thread-late",
        )
        before = deepcopy(context["fanout"])

        result = self._associate(
            context,
            row_id=late_row,
            thread_id="thread-late",
        )

        fanout, binding, _obligation, _edge = self._assert_late_artifacts(
            context,
            late_row,
        )
        expected = self.discovery._fanout(
            before,
            state_revision=before["stateRevision"] + 1,
            binding_revision=binding["stateRevision"],
            binding_head_hash=binding["contactRowBindingHeadHash"],
            binding_association_count=binding["associationCount"],
            discovery_cursor_row_id=None,
            cursor_processed_count=0,
            obligation_count=before["obligationCount"] + 1,
            updated_at=self.associated_at,
        )
        self.assertEqual(expected, fanout)
        self.assertEqual("created", result["disposition"])
        self.assertEqual(2, binding["associationCount"])
        self.assertEqual(5, len(self._writes(context["store"])))
        self.assertIn(("commit_applied", 5), context["store"].events)

    def test_nonterminal_association_clears_expired_lease_and_preserves_fence(self):
        first_row = _row_id(1)
        late_row = _row_id(2)
        self.associated_at = self.discovery.lease_until
        context = self.discovery._seed(
            (first_row,),
            cursor=first_row,
            obligation_count=1,
        )
        self.discovery._obligation(context, context["edges"][0])
        self._seed_association_prerequisites(
            context,
            row_id=late_row,
            thread_id="thread-late-expired",
        )
        before = deepcopy(context["fanout"])

        result = self._associate(
            context,
            row_id=late_row,
            thread_id="thread-late-expired",
        )

        fanout, _binding, _obligation, _edge = self._assert_late_artifacts(
            context,
            late_row,
        )
        self.assertEqual("created", result["disposition"])
        self.assertIsNone(fanout["leaseOwnerHash"])
        self.assertIsNone(fanout["leaseUntil"])
        self.assertEqual(before["fencingToken"], fanout["fencingToken"])
        self.assertIsNone(fanout["discoveryCursorRowId"])
        self.assertEqual(0, fanout["cursorProcessedCount"])
        self.assertEqual(5, len(self._writes(context["store"])))
        self.assertIn(("commit_applied", 5), context["store"].events)

    def test_existing_association_new_evidence_does_not_change_fanout(self):
        row_id = _row_id(1)
        context = self.discovery._seed((row_id,))
        self._seed_association_prerequisites(context, row_id=row_id)
        before_fanout = deepcopy(context["fanout"])
        before_binding = deepcopy(context["bindingHead"])
        new_exact = "e" * 64

        result = self._associate(
            context,
            row_id=row_id,
            exact_identity_hash=new_exact,
        )

        self.assertEqual("evidence_created", result["disposition"])
        self.assertEqual(before_fanout, self._current_fanout(context))
        self.assertEqual(
            before_binding,
            self._documents(
                context,
                "contactRowBindingHeads",
            )[context["canonicalHash"]],
        )
        writes = self._writes(context["store"])
        self.assertEqual(1, len(writes))
        self.assertIn("/contactRowBindingEvidence/", writes[0][1])
        accessed_writes = "\n".join(event[1] for event in writes)
        self.assertNotIn("contactOptOutFanout", accessed_writes)

        fanout_ref = self._reference(
            context,
            "contactOptOutFanoutHeads",
            before_fanout["fanoutId"],
        )
        advanced_fanout = self.discovery._fanout(
            before_fanout,
            state_revision=before_fanout["stateRevision"] + 1,
            updated_at="2026-08-04T12:08:00.000000Z",
        )
        fanout_ref.set(advanced_fanout, merge=False)
        context["store"].events.clear()

        replay = self._associate(
            context,
            row_id=row_id,
            exact_identity_hash=new_exact,
        )

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(advanced_fanout, self._current_fanout(context))
        self.assertEqual([], self._writes(context["store"]))

    def test_active_complete_late_row_is_applied_and_recertified_atomically(self):
        row_id = _row_id(1)
        context = self._seed_complete()
        (_identity, row_head), _binding = self._seed_association_prerequisites(
            context,
            row_id=row_id,
        )
        before = deepcopy(context["fanout"])
        before_head = deepcopy(row_head)
        context["store"].apply_then_raise_next_commit = RuntimeError(
            "configured late-association apply-then-raise"
        )

        result = self._associate(context, row_id=row_id)

        fanout, binding, obligation, _edge = self._assert_late_artifacts(
            context,
            row_id,
        )
        self._assert_complete_recertification(
            before=before,
            after=fanout,
            binding=binding,
        )
        result_document = self._documents(
            context,
            "contactOptOutFanoutResults",
        )[f"{fanout['fanoutId']}--{row_id}"]
        claim = self._documents(context, "rowClaimSets")[
            result_document["claimRequestId"]
        ]
        generation_id = f"{row_id}--{result_document['rowGeneration']}"
        generation = self._documents(
            context,
            "rowOwnerGenerations",
        )[generation_id]
        settlement = self._documents(
            context,
            "rowOwnerSettlements",
        )[generation_id]
        head = self._documents(context, "rowAuthorityHeads")[row_id]
        self.assertEqual("created", result["disposition"])
        self.assertEqual("applied", result_document["disposition"])
        self.assertEqual("claim_accepted", result_document["reasonCode"])
        self.assertEqual(
            before_head["headHash"],
            result_document["observedRowHeadHash"],
        )
        self.assertEqual(
            obligation["contactFanoutObligationHash"],
            result_document["obligationHash"],
        )
        self.assertEqual("contact_fanout", claim["authorityOrigin"])
        self.assertEqual([{"rowId": row_id, "role": "primary"}], claim["rowBindings"])
        self.assertEqual(
            context["settlement"]["contactSettlementHash"],
            claim["payloadHash"],
        )
        self.assertEqual(generation["generation"], result_document["rowGeneration"])
        self.assertEqual(
            settlement["settlementHash"],
            result_document["rowSettlementHash"],
        )
        self.assertEqual("contact_optout", head["effectiveOwnerKind"])
        self.assertEqual(settlement["settlementHash"], head["effectiveSettlementHash"])
        writes = self._writes(context["store"])
        self.assertEqual(10, len(writes))
        self.assertEqual(len(writes), next(
            event[1]
            for event in reversed(context["store"].events)
            if event[0] == "commit_applied"
        ))

    def test_active_complete_pending_predecessor_reconciles_eleven_write_unknown_commit(self):
        row_id = _row_id(1)
        context = self._seed_complete()
        self._seed_association_prerequisites(context, row_id=row_id)
        fixture = context["transition"].fixture
        prior_claim, prior_generation, pending_head = fixture._install_owner(
            context["store"],
            row_id,
            owner_kind="terminal",
        )
        before = deepcopy(context["fanout"])
        context["store"].events.clear()
        context["store"].apply_then_raise_next_commit = RuntimeError(
            "configured pending-predecessor late-association unknown commit"
        )

        result = self._associate(context, row_id=row_id)

        fanout, binding, _obligation, _edge = self._assert_late_artifacts(
            context,
            row_id,
        )
        self._assert_complete_recertification(
            before=before,
            after=fanout,
            binding=binding,
        )
        result_document = self._documents(
            context,
            "contactOptOutFanoutResults",
        )[f"{fanout['fanoutId']}--{row_id}"]
        generation_id = f"{row_id}--{result_document['rowGeneration']}"
        generation = self._documents(
            context,
            "rowOwnerGenerations",
        )[generation_id]
        settlements = self._documents(context, "rowOwnerSettlements")
        predecessor = settlements[
            f"{row_id}--{prior_generation['generation']}"
        ]
        settlement = settlements[generation_id]
        head = self._documents(context, "rowAuthorityHeads")[row_id]
        self.assertEqual("created", result["disposition"])
        self.assertEqual("applied", result_document["disposition"])
        self.assertEqual("accepted", self._documents(context, "rowClaimSets")[
            result_document["claimRequestId"]
        ]["outcome"])
        self.assertEqual(pending_head["headHash"], result_document["observedRowHeadHash"])
        self.assertEqual(2, generation["generation"])
        self.assertEqual("dominated", predecessor["outcome"])
        self.assertEqual(
            prior_claim["claimSetHash"],
            prior_generation["claimSetHash"],
        )
        self.assertEqual(
            generation["generationHash"],
            predecessor["dominantGenerationHash"],
        )
        self.assertEqual(
            settlement["settlementHash"],
            result_document["rowSettlementHash"],
        )
        self.assertEqual(
            settlement["settlementHash"],
            head["effectiveSettlementHash"],
        )
        self.assertEqual(11, len(self._writes(context["store"])))
        self.assertIn(("commit_applied", 11), context["store"].events)
        failure_index = context["store"].events.index(
            ("commit_raised_after_apply",)
        )
        self.assertTrue(
            any(
                event[0] == "query"
                and event[1].endswith("/rowOwnerSettlements")
                for event in context["store"].events[failure_index + 1 :]
            ),
            "unknown-commit reconciliation did not read the settlement query after-image",
        )

    def test_active_complete_late_row_may_record_dominated_result(self):
        row_id = _row_id(1)
        context = self._seed_complete()
        (_identity, row_head), _binding = self._seed_association_prerequisites(
            context,
            row_id=row_id,
        )
        installed = self.completion._install_contact_lineage(
            context,
            row_head,
            materialize_head=True,
            canonical_hash="c" * 64,
            contact_settlement_hash="e" * 64,
            fanout_id="f" * 64,
        )
        self.assertNotEqual(context["canonicalHash"], "c" * 64)
        before = deepcopy(context["fanout"])
        context["store"].events.clear()

        self._associate(context, row_id=row_id)

        fanout, binding, _obligation, _edge = self._assert_late_artifacts(
            context,
            row_id,
        )
        self._assert_complete_recertification(
            before=before,
            after=fanout,
            binding=binding,
        )
        result = self._documents(
            context,
            "contactOptOutFanoutResults",
        )[f"{fanout['fanoutId']}--{row_id}"]
        claim = self._documents(context, "rowClaimSets")[result["claimRequestId"]]
        self.assertEqual("dominated", result["disposition"])
        self.assertEqual("claim_dominated", result["reasonCode"])
        self.assertEqual(
            installed["settledHead"]["headHash"],
            result["observedRowHeadHash"],
        )
        self.assertEqual("dominated", claim["outcome"])
        self.assertEqual(1, claim["plannedWrites"])
        decision = claim["rowDecisions"][0]
        self.assertEqual("dominated", decision["decision"])
        self.assertEqual(
            installed["generation"]["generationHash"],
            decision["winnerGenerationHash"],
        )
        self.assertEqual(
            installed["settlement"]["settlementHash"],
            decision["winnerSettlementHash"],
        )
        self.assertEqual(
            installed["settledHead"],
            self._documents(context, "rowAuthorityHeads")[row_id],
        )
        self.assertEqual(7, len(self._writes(context["store"])))
        self.assertIn(("commit_applied", 7), context["store"].events)

    def test_active_complete_deleted_row_is_noop_and_recertified_atomically(self):
        row_id = _row_id(1)
        context = self._seed_complete()
        (_identity, row_head), _binding = self._seed_association_prerequisites(
            context,
            row_id=row_id,
            lifecycle="deleted",
        )
        before = deepcopy(context["fanout"])
        before_head = deepcopy(row_head)

        self._associate(context, row_id=row_id)

        fanout, binding, _obligation, _edge = self._assert_late_artifacts(
            context,
            row_id,
        )
        self._assert_complete_recertification(
            before=before,
            after=fanout,
            binding=binding,
        )
        result = self._documents(
            context,
            "contactOptOutFanoutResults",
        )[f"{fanout['fanoutId']}--{row_id}"]
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
                )
            )
        )
        self.assertEqual(
            before_head,
            self._documents(context, "rowAuthorityHeads")[row_id],
        )
        for collection in (
            "rowClaimSets",
            "rowOwnerGenerations",
            "rowOwnerSettlements",
        ):
            self.assertEqual({}, self._documents(context, collection))
        self.assertEqual(6, len(self._writes(context["store"])))
        self.assertIn(("commit_applied", 6), context["store"].events)

    def test_rolled_back_active_complete_cannot_associate_after_later_release(self):
        row_id = _row_id(1)
        context = self._seed_complete()
        self._seed_association_prerequisites(context, row_id=row_id)
        contact_head_ref = self._reference(
            context,
            "contactOptOutHeads",
            context["canonicalHash"],
        )
        active_head = deepcopy(
            self._documents(context, "contactOptOutHeads")[
                context["canonicalHash"]
            ]
        )
        active_fanout = deepcopy(self._current_fanout(context))
        active_fanout_ref = self._reference(
            context,
            "contactOptOutFanoutHeads",
            active_fanout["fanoutId"],
        )
        released = context["transition"]._authority(
            context["store"]
        ).record_authenticated_contact_release(
            verified_user_id=context["transition"].fixture.user_id,
            canonical_mailbox_identity_hash=context["canonicalHash"],
            expected_active_optout_settlement_hash=context["settlement"][
                "contactSettlementHash"
            ],
            actor_scope_hash="d" * 64,
            client_request_id="late-association-rollback-release",
            requested_at="2026-08-04T12:06:30.000000Z",
        )
        self.assertEqual("created", released["disposition"])
        self.assertEqual(
            "released",
            self._documents(context, "contactOptOutHeads")[
                context["canonicalHash"]
            ]["state"],
        )

        contact_head_ref.set(active_head, merge=False)
        active_fanout_ref.set(active_fanout, merge=False)
        before = deepcopy(context["store"].data)
        context["store"].events.clear()

        with self.assertRaises(self.module.RowAuthorityError):
            self.association._associate(
                context["store"],
                canonical_mailbox_identity_hash=context[
                    "canonicalHash"
                ],
                exact_identity_hash=context["receipt"][
                    "exactIdentityHash"
                ],
                row_id=row_id,
                thread_id="thread-1",
                created_at=self.associated_at,
            )

        self.assertEqual(before, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))
        for collection in (
            "contactRowBindings",
            "contactRowBindingEvidence",
            "contactRowBindingHeads",
            "contactOptOutFanoutObligations",
            "contactOptOutFanoutResults",
            "rowClaimSets",
            "rowOwnerGenerations",
            "rowOwnerSettlements",
        ):
            self.assertEqual({}, self._documents(context, collection))

    def test_late_association_ambiguity_creates_no_edge_or_evidence(self):
        row_id = _row_id(1)
        context = self._seed_complete()
        self._seed_association_prerequisites(context, row_id=row_id)
        fanout_ref = self._reference(
            context,
            "contactOptOutFanoutHeads",
            context["fanout"]["fanoutId"],
        )
        raced = {"called": False}

        def race_current_fanout():
            raced["called"] = True
            fanout_ref.set(
                {"malformed": "concurrent current-fanout replacement"},
                merge=False,
            )

        context["store"].before_next_commit_hook = race_current_fanout
        context["store"].events.clear()

        with self.assertRaises(self.module.RowAuthorityError):
            self.association._associate(
                context["store"],
                canonical_mailbox_identity_hash=context["canonicalHash"],
                exact_identity_hash=context["receipt"]["exactIdentityHash"],
                row_id=row_id,
                thread_id="thread-1",
                created_at=self.associated_at,
            )

        self.assertTrue(raced["called"], "fan-out race hook was never reached")
        self.assertIn(
            ("commit_aborted_stale_read", fanout_ref.path),
            context["store"].events,
        )
        for collection in (
            "contactRowBindings",
            "contactRowBindingEvidence",
            "contactRowBindingHeads",
            "contactOptOutFanoutObligations",
            "contactOptOutFanoutResults",
            "rowClaimSets",
            "rowOwnerGenerations",
            "rowOwnerSettlements",
        ):
            self.assertEqual({}, self._documents(context, collection))
        self.assertEqual(
            [
                (
                    "set",
                    fanout_ref.path,
                    {"malformed": "concurrent current-fanout replacement"},
                    False,
                )
            ],
            self._writes(context["store"]),
        )

    def test_current_contact_release_race_leaves_zero_late_artifacts(self):
        row_id = _row_id(1)
        context = self.discovery._seed(())
        self._seed_association_prerequisites(context, row_id=row_id)
        contact_head_ref = self._reference(
            context,
            "contactOptOutHeads",
            context["canonicalHash"],
        )
        raced = {"called": False}

        def release_current_contact():
            raced["called"] = True
            context["transition"]._install_release_after_image(
                context["store"],
                released_at="2026-08-04T12:06:00.000000Z",
            )

        context["store"].before_next_commit_hook = release_current_contact
        context["store"].events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.association._associate(
                context["store"],
                canonical_mailbox_identity_hash=context["canonicalHash"],
                exact_identity_hash=context["receipt"]["exactIdentityHash"],
                row_id=row_id,
                thread_id="thread-1",
                created_at=self.associated_at,
            )

        self.assertTrue(raced["called"], "contact race hook was never reached")
        self.assertIn(
            ("commit_aborted_stale_read", contact_head_ref.path),
            context["store"].events,
        )
        current_head = self._documents(context, "contactOptOutHeads")[
            context["canonicalHash"]
        ]
        self.assertEqual("released", current_head["state"])
        for collection in (
            "contactRowBindings",
            "contactRowBindingEvidence",
            "contactRowBindingHeads",
            "contactOptOutFanoutObligations",
            "contactOptOutFanoutResults",
            "rowClaimSets",
            "rowOwnerGenerations",
            "rowOwnerSettlements",
        ):
            self.assertEqual({}, self._documents(context, collection))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
