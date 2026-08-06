"""RED contracts for released contact fan-out late-row convergence."""

from __future__ import annotations

import unittest
from copy import deepcopy

from tests.test_row_authority_contact_fanout_discovery import _row_id
from tests import test_row_authority_contact_late_association as active_late


class ContactLateReleaseAssociationTests(unittest.TestCase):
    COMPLETION_FIELDS = active_late.ContactLateAssociationTests.COMPLETION_FIELDS

    @classmethod
    def setUpClass(cls):
        cls.active_type = active_late.ContactLateAssociationTests
        cls.active_type.setUpClass()
        cls.module = cls.active_type.module

    def setUp(self):
        self.active = self.active_type(methodName="runTest")
        self.active.setUp()
        self.completion = self.active.completion
        self.discovery = self.active.discovery
        self.association = self.active.association
        self.associated_at = "2026-08-04T12:07:00.000000Z"
        self.active.associated_at = self.associated_at

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

    def _current_fanout(self, context, fanout_id=None):
        return self._documents(
            context,
            "contactOptOutFanoutHeads",
        )[fanout_id or context["fanout"]["fanoutId"]]

    def _seed_prerequisites(
        self,
        context,
        *,
        row_id,
        thread_id,
    ):
        return self.active._seed_association_prerequisites(
            context,
            row_id=row_id,
            thread_id=thread_id,
        )

    def _associate(
        self,
        context,
        *,
        row_id,
        exact_identity_hash=None,
        thread_id="thread-release-late",
        executor=None,
    ):
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
            created_at=self.associated_at,
        )

    def _seed_nonterminal_release(self, *, state="discovering"):
        first_row = _row_id(1)
        late_row = _row_id(2)
        context = self.discovery._seed_release((first_row,))
        first_obligation = self.discovery._obligation(
            context,
            context["edges"][0],
            created_at="2026-08-04T12:06:20.000000Z",
        )
        current = context["fanout"]
        progressed = self.discovery._fanout(
            current,
            state_revision=current["stateRevision"] + 1,
            state=state,
            discovery_cursor_row_id=first_row,
            cursor_processed_count=1,
            obligation_count=1,
            updated_at="2026-08-04T12:06:20.000000Z",
        )
        self.discovery._store_fanout(context, progressed)
        (identity, row_head), thread_binding = self._seed_prerequisites(
            context,
            row_id=late_row,
            thread_id="thread-release-late",
        )
        context.update(
            {
                "firstRow": first_row,
                "lateRow": late_row,
                "firstObligation": first_obligation,
                "lateIdentity": identity,
                "lateRowHead": row_head,
                "lateThreadBinding": thread_binding,
            }
        )
        context["store"].events.clear()
        return context

    def _seed_complete_release(self):
        context = self.discovery._seed_release(())
        discovered = self.discovery._discover(
            context,
            discovered_at="2026-08-04T12:06:20.000000Z",
        )
        self.assertEqual("discovery_complete", discovered["disposition"])
        self.assertEqual("applying", discovered["fanoutHead"]["state"])
        context["fanout"] = discovered["fanoutHead"]
        certified = self.completion._certify(
            context,
            certified_at="2026-08-04T12:06:30.000000Z",
        )
        self.assertEqual("certification_complete", certified["disposition"])
        self.assertEqual("complete", certified["fanoutHead"]["state"])
        context["fanout"] = certified["fanoutHead"]
        context["store"].events.clear()
        return context

    def _seed_complete_with_late_prerequisites(self):
        row_id = _row_id(1)
        context = self._seed_complete_release()
        (identity, row_head), thread_binding = self._seed_prerequisites(
            context,
            row_id=row_id,
            thread_id="thread-release-complete-late",
        )
        context.update(
            {
                "lateRow": row_id,
                "lateIdentity": identity,
                "lateRowHead": row_head,
                "lateThreadBinding": thread_binding,
            }
        )
        context["store"].events.clear()
        return context

    def _binding_head(self, context):
        return self._documents(
            context,
            "contactRowBindingHeads",
        )[context["canonicalHash"]]

    def _late_edge(self, context, row_id):
        return next(
            document
            for document in self._documents(
                context,
                "contactRowBindings",
            ).values()
            if document["rowId"] == row_id
        )

    def _assert_release_noop_result(
        self,
        context,
        *,
        row_id,
        fanout,
        obligation,
    ):
        result = self._documents(
            context,
            "contactOptOutFanoutResults",
        )[f"{fanout['fanoutId']}--{row_id}"]
        self.assertEqual(
            result,
            self.module.validate_contact_fanout_result_document(
                document=result
            ),
        )
        self.assertEqual("release", result["outcome"])
        self.assertEqual("noop", result["disposition"])
        self.assertEqual("row_optout_not_applied", result["reasonCode"])
        self.assertEqual(
            obligation["contactFanoutObligationHash"],
            result["obligationHash"],
        )
        self.assertEqual(
            context["lateRowHead"]["headHash"],
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
        return result

    def _assert_complete_recertified(self, *, before, after, binding):
        expected = self.discovery._fanout(
            before,
            state_revision=before["stateRevision"] + 1,
            binding_revision=binding["stateRevision"],
            binding_head_hash=binding["contactRowBindingHeadHash"],
            binding_association_count=binding["associationCount"],
            updated_at=self.associated_at,
        )
        self.assertEqual(expected, after)
        self.assertEqual("release", after["outcome"])
        self.assertEqual("complete", after["state"])
        self.assertEqual(before["obligationCount"], after["obligationCount"])
        self.assertEqual(before["resultCount"], after["resultCount"])
        self.assertEqual(before["fencingToken"], after["fencingToken"])
        self.assertIsNone(after["leaseOwnerHash"])
        self.assertIsNone(after["leaseUntil"])
        self.assertIsNone(after["discoveryCursorRowId"])
        self.assertEqual(0, after["cursorProcessedCount"])
        self.assertIsNone(after["supersedingContactSettlementHash"])
        self.assertEqual(
            {field: before[field] for field in self.COMPLETION_FIELDS},
            {field: after[field] for field in self.COMPLETION_FIELDS},
        )

    def _reoptout(self, context):
        bundle, link = context["transition"]._seed_bundle(
            context["store"],
            "source-release-late-next-active-epoch",
            exact_hash=context["receipt"]["exactIdentityHash"],
        )
        self.assertEqual(
            context["canonicalHash"],
            link["canonicalMailboxIdentityHash"],
        )
        return context["transition"]._record(
            context["store"],
            bundle,
            requested_at="2026-08-04T12:08:00.000000Z",
        )

    def _assert_released_nonterminal_late_row(self, state):
        context = self._seed_nonterminal_release(state=state)
        before = deepcopy(context["fanout"])
        self.assertEqual(state, before["state"])

        associated = self._associate(context, row_id=context["lateRow"])

        fanout = self._current_fanout(context)
        binding = self._binding_head(context)
        edge = self._late_edge(context, context["lateRow"])
        obligation = self._documents(
            context,
            "contactOptOutFanoutObligations",
        )[f"{fanout['fanoutId']}--{context['lateRow']}"]
        self.assertEqual(
            obligation,
            self.module.validate_contact_fanout_obligation_document(
                document=obligation
            ),
        )
        self.assertEqual("release", obligation["outcome"])
        self.assertEqual(
            edge["contactRowEdgeHash"],
            obligation["contactRowEdgeHash"],
        )
        self.assertEqual(
            context["settlement"]["contactSettlementHash"],
            obligation["expectedContactSettlementHash"],
        )
        self.assertEqual(self.associated_at, obligation["createdAt"])
        self._assert_release_noop_result(
            context,
            row_id=context["lateRow"],
            fanout=fanout,
            obligation=obligation,
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
            result_count=before["resultCount"] + 1,
            updated_at=self.associated_at,
        )
        self.assertEqual(expected, fanout)
        self.assertEqual("created", associated["disposition"])
        self.assertEqual(6, len(self._writes(context["store"])))
        self.assertIn(("commit_applied", 6), context["store"].events)

    def test_released_nonterminal_late_row_adds_immediate_noop_and_resets_cursor(self):
        for state in ("discovering", "applying"):
            with self.subTest(state=state):
                self._assert_released_nonterminal_late_row(state)

    def test_released_nonterminal_new_association_unknown_commit_and_replay_are_exact(
        self,
    ):
        context = self._seed_nonterminal_release()
        context["store"].apply_then_raise_next_commit = RuntimeError(
            "unknown released nonterminal late-association commit outcome"
        )

        created = self._associate(context, row_id=context["lateRow"])

        fanout = self._current_fanout(context)
        obligation = self._documents(
            context,
            "contactOptOutFanoutObligations",
        )[f"{fanout['fanoutId']}--{context['lateRow']}"]
        result = self._assert_release_noop_result(
            context,
            row_id=context["lateRow"],
            fanout=fanout,
            obligation=obligation,
        )
        self.assertEqual("created", created["disposition"])
        self.assertEqual(6, len(self._writes(context["store"])))
        self.assertIn(("commit_applied", 6), context["store"].events)
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
            "released nonterminal unknown commit must perform exact readback",
        )
        committed = deepcopy(context["store"].data)
        context["store"].events.clear()

        replay = self._associate(context, row_id=context["lateRow"])

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(
            result,
            self._documents(
                context,
                "contactOptOutFanoutResults",
            )[f"{fanout['fanoutId']}--{context['lateRow']}"],
        )
        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))

    def test_released_complete_late_row_recertifies_without_obligation_or_result(self):
        context = self._seed_complete_with_late_prerequisites()
        before = deepcopy(context["fanout"])
        before_obligations = self._documents(
            context,
            "contactOptOutFanoutObligations",
        )
        before_results = self._documents(
            context,
            "contactOptOutFanoutResults",
        )

        associated = self._associate(
            context,
            row_id=context["lateRow"],
            thread_id="thread-release-complete-late",
        )

        fanout = self._current_fanout(context)
        binding = self._binding_head(context)
        self._assert_complete_recertified(
            before=before,
            after=fanout,
            binding=binding,
        )
        self.assertEqual(
            before_obligations,
            self._documents(context, "contactOptOutFanoutObligations"),
        )
        self.assertEqual(
            before_results,
            self._documents(context, "contactOptOutFanoutResults"),
        )
        self.assertEqual("created", associated["disposition"])
        self.assertEqual(4, len(self._writes(context["store"])))
        self.assertIn(("commit_applied", 4), context["store"].events)

    def test_released_complete_late_association_retry_is_zero_write(self):
        context = self._seed_complete_with_late_prerequisites()

        first = self._associate(
            context,
            row_id=context["lateRow"],
            thread_id="thread-release-complete-late",
        )
        committed = deepcopy(context["store"].data)
        recertified = deepcopy(self._current_fanout(context))
        context["store"].events.clear()

        replay = self._associate(
            context,
            row_id=context["lateRow"],
            thread_id="thread-release-complete-late",
        )

        self.assertEqual("created", first["disposition"])
        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(recertified, self._current_fanout(context))
        self.assertEqual(committed, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))

    def test_released_complete_recertification_preserves_terminal_certificate(self):
        context = self._seed_complete_with_late_prerequisites()
        before = deepcopy(context["fanout"])

        self._associate(
            context,
            row_id=context["lateRow"],
            thread_id="thread-release-complete-late",
        )

        after = self._current_fanout(context)
        self._assert_complete_recertified(
            before=before,
            after=after,
            binding=self._binding_head(context),
        )
        self.assertEqual(
            before["completionBindingAssociationCount"],
            before["completionObligationCount"],
        )
        self.assertEqual(
            before["completionBindingAssociationCount"] + 1,
            after["bindingAssociationCount"],
        )
        self.assertEqual(
            before["completionObligationCount"],
            after["obligationCount"],
        )
        self.assertEqual(
            before["completionResultCount"],
            after["resultCount"],
        )
        self.assertEqual(
            after,
            self.module.validate_contact_fanout_head_document(document=after),
        )

    def test_released_complete_count_exception_survives_later_contact_epoch(self):
        context = self._seed_complete_with_late_prerequisites()
        release_fanout_id = context["fanout"]["fanoutId"]
        completion_certificate = {
            field: context["fanout"][field]
            for field in self.COMPLETION_FIELDS
        }
        self._associate(
            context,
            row_id=context["lateRow"],
            thread_id="thread-release-complete-late",
        )
        recertified = deepcopy(self._current_fanout(context))
        context["store"].events.clear()

        next_epoch = self._reoptout(context)

        retained = self._current_fanout(context, release_fanout_id)
        self.assertEqual(recertified, retained)
        self.assertEqual(
            completion_certificate,
            {field: retained[field] for field in self.COMPLETION_FIELDS},
        )
        self.assertEqual(
            retained,
            self.module.validate_contact_fanout_head_document(
                document=retained
            ),
        )
        self.assertEqual("created", next_epoch["disposition"])
        self.assertEqual("active", next_epoch["head"]["state"])
        self.assertEqual("apply", next_epoch["fanoutHead"]["outcome"])
        self.assertEqual(
            retained["bindingAssociationCount"],
            next_epoch["fanoutHead"]["bindingAssociationCount"],
        )

    def test_released_complete_next_active_epoch_discovers_late_row(self):
        context = self._seed_complete_with_late_prerequisites()
        self._associate(
            context,
            row_id=context["lateRow"],
            thread_id="thread-release-complete-late",
        )
        next_epoch = self._reoptout(context)
        next_fanout = next_epoch["fanoutHead"]
        leased = self.discovery._fanout(
            next_fanout,
            state_revision=next_fanout["stateRevision"] + 1,
            lease_owner_hash=self.discovery.lease_owner,
            lease_until="2026-08-04T12:12:00.000000Z",
            fencing_token=next_fanout["fencingToken"] + 1,
            updated_at="2026-08-04T12:08:10.000000Z",
        )
        context.update(
            {
                "settlement": next_epoch["settlement"],
                "receipt": next_epoch["transitionRequest"],
                "contactHead": next_epoch["head"],
                "fanout": leased,
            }
        )
        self.discovery._store_fanout(context, leased)
        context["store"].events.clear()

        discovered = self.discovery._discover(
            context,
            discovered_at="2026-08-04T12:08:20.000000Z",
        )

        self.assertEqual("discovery_complete", discovered["disposition"])
        self.assertEqual(
            [context["lateRow"]],
            [item["rowId"] for item in discovered["obligations"]],
        )
        self.assertEqual("apply", discovered["obligations"][0]["outcome"])
        self.assertEqual(1, discovered["fanoutHead"]["obligationCount"])

    def test_released_existing_edge_new_evidence_and_exact_replay_are_bounded(self):
        row_id = _row_id(1)
        context = self.discovery._seed_release((row_id,))
        self._seed_prerequisites(
            context,
            row_id=row_id,
            thread_id="thread-release-evidence",
        )
        before_fanout = deepcopy(context["fanout"])
        before_binding = deepcopy(context["bindingHead"])
        new_exact = "e" * 64

        created = self._associate(
            context,
            row_id=row_id,
            exact_identity_hash=new_exact,
            thread_id="thread-release-evidence",
        )

        self.assertEqual("evidence_created", created["disposition"])
        self.assertEqual(before_fanout, self._current_fanout(context))
        self.assertEqual(before_binding, self._binding_head(context))
        writes = self._writes(context["store"])
        self.assertEqual(1, len(writes))
        self.assertIn("/contactRowBindingEvidence/", writes[0][1])
        context["store"].events.clear()

        replay = self._associate(
            context,
            row_id=row_id,
            exact_identity_hash=new_exact,
            thread_id="thread-release-evidence",
        )

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(before_fanout, self._current_fanout(context))
        self.assertEqual([], self._writes(context["store"]))

    def test_released_malformed_or_terminal_fanout_writes_nothing(self):
        for mode in (
            "missing",
            "malformed",
            "ambiguous",
            "superseding",
            "superseded",
        ):
            with self.subTest(mode=mode):
                row_id = _row_id(1)
                context = self.discovery._seed_release(())
                self._seed_prerequisites(
                    context,
                    row_id=row_id,
                    thread_id=f"thread-release-invalid-{mode}",
                )
                fanout_ref = self._reference(
                    context,
                    "contactOptOutFanoutHeads",
                    context["fanout"]["fanoutId"],
                )
                if mode == "missing":
                    fanout_ref.delete()
                elif mode == "malformed":
                    fanout_ref.set(
                        {"malformed": "released late fan-out"},
                        merge=False,
                    )
                elif mode == "ambiguous":
                    self._reference(
                        context,
                        "contactOptOutSettlements",
                        "duplicate-current-release-settlement",
                    ).create(deepcopy(context["settlement"]))
                else:
                    superseding = (
                        self.module._build_contact_superseding_fanout_head(
                            current_document=context["fanout"],
                            superseding_contact_settlement_hash="f" * 64,
                            updated_at="2026-08-04T12:06:30.000000Z",
                        )
                    )
                    replacement = superseding
                    if mode == "superseded":
                        replacement = self.discovery._fanout(
                            superseding,
                            state_revision=superseding["stateRevision"] + 1,
                            state="superseded",
                            updated_at="2026-08-04T12:06:40.000000Z",
                        )
                    fanout_ref.set(replacement, merge=False)
                before = deepcopy(context["store"].data)
                context["store"].events.clear()

                with self.assertRaises(self.module.RowAuthorityAmbiguous):
                    self._associate(
                        context,
                        row_id=row_id,
                        thread_id=f"thread-release-invalid-{mode}",
                    )

                self.assertEqual(before, context["store"].data)
                self.assertEqual([], self._writes(context["store"]))
                for collection in (
                    "contactRowBindings",
                    "contactRowBindingEvidence",
                    "contactRowBindingHeads",
                    "contactOptOutFanoutObligations",
                    "contactOptOutFanoutResults",
                ):
                    self.assertEqual({}, self._documents(context, collection))

    def test_released_contact_fanout_race_creates_no_late_artifacts(self):
        row_id = _row_id(1)
        context = self.discovery._seed_release(())
        self._seed_prerequisites(
            context,
            row_id=row_id,
            thread_id="thread-release-race",
        )
        fanout_ref = self._reference(
            context,
            "contactOptOutFanoutHeads",
            context["fanout"]["fanoutId"],
        )
        raced = {"called": False}
        advanced = self.discovery._fanout(
            context["fanout"],
            state_revision=context["fanout"]["stateRevision"] + 1,
            updated_at="2026-08-04T12:07:01.000000Z",
        )

        def race_current_fanout():
            raced["called"] = True
            fanout_ref.set(advanced, merge=False)

        context["store"].before_next_commit_hook = race_current_fanout
        context["store"].events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._associate(
                context,
                row_id=row_id,
                thread_id="thread-release-race",
            )

        self.assertTrue(raced["called"], "released fan-out race hook was never reached")
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
        ):
            self.assertEqual({}, self._documents(context, collection))
        external_race_write = ("set", fanout_ref.path, advanced, False)
        self.assertEqual(
            [external_race_write],
            self._writes(context["store"]),
            "only the intentional external race mutation may be recorded",
        )

    def test_released_late_association_retries_then_rejects_future_row_head(
        self,
    ):
        context = self._seed_nonterminal_release()
        row_id = context["lateRow"]
        row_head_ref = context["transition"].fixture._row_references(
            context["store"],
            row_id,
        )[1]
        advanced = deepcopy(context["lateRowHead"])
        advanced.update(
            {
                "stateRevision": advanced["stateRevision"] + 1,
                "currentLocationRevision": (
                    advanced["currentLocationRevision"] + 1
                ),
                "currentLocationHash": "f" * 64,
                "updatedAt": "2026-08-04T12:07:00.000001Z",
            }
        )
        advanced = context["transition"].fixture._rehash_head(advanced)
        before = deepcopy(context["store"].data)
        expected = deepcopy(before)
        expected[row_head_ref.path] = advanced
        late_artifacts_before = {
            collection: deepcopy(self._documents(context, collection))
            for collection in (
                "contactRowBindings",
                "contactRowBindingEvidence",
                "contactRowBindingHeads",
                "contactOptOutFanoutObligations",
                "contactOptOutFanoutResults",
            )
        }
        raced = {"called": False}

        def advance_row_head_after_first_read():
            raced["called"] = True
            row_head_ref.set(advanced, merge=False)

        context["store"].before_next_commit_hook = (
            advance_row_head_after_first_read
        )
        context["store"].events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._associate(context, row_id=row_id)

        self.assertTrue(raced["called"], "row-head race hook was never reached")
        self.assertIn(
            ("commit_aborted_stale_read", row_head_ref.path),
            context["store"].events,
        )
        self.assertGreaterEqual(
            sum(
                event[0] == "transaction_began"
                for event in context["store"].events
            ),
            2,
            "late association must retry after its stale row-head read",
        )
        self.assertGreater(advanced["updatedAt"], self.associated_at)
        self.assertEqual(expected, context["store"].data)
        for collection, documents in late_artifacts_before.items():
            self.assertEqual(documents, self._documents(context, collection))
        self.assertEqual(
            [("set", row_head_ref.path, advanced, False)],
            self._writes(context["store"]),
            "only the intentional future row-head mutation may be recorded",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
