"""RED contracts for release-origin APPLY evidence authority."""

from __future__ import annotations

import unittest
from copy import deepcopy

from tests import test_row_authority_contact_fanout_release as release_contracts


class ContactFanoutReleaseOriginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release_type = release_contracts.ContactFanoutReleaseTests
        cls.release_type.setUpClass()
        cls.module = cls.release_type.module

    def setUp(self):
        self.release = self.release_type(methodName="runTest")
        self.release.setUp()

    def _origin_evidence(self, context):
        result = deepcopy(context["selectedApplyResult"])
        document_id = f"{result['fanoutId']}--{context['rowId']}"
        obligation = deepcopy(
            self.release._documents(
                context,
                "contactOptOutFanoutObligations",
            )[document_id]
        )
        stored_result = deepcopy(
            self.release._documents(
                context,
                "contactOptOutFanoutResults",
            )[document_id]
        )
        claim = deepcopy(
            self.release._documents(context, "rowClaimSets")[
                result["claimRequestId"]
            ]
        )
        generation_id = f"{context['rowId']}--{result['rowGeneration']}"
        generation = deepcopy(
            self.release._documents(context, "rowOwnerGenerations")[
                generation_id
            ]
        )
        settlement = deepcopy(
            self.release._documents(context, "rowOwnerSettlements")[
                generation_id
            ]
        )
        fanout = deepcopy(
            self.release._documents(context, "contactOptOutFanoutHeads")[
                result["fanoutId"]
            ]
        )
        contact_settlements = [
            item
            for item in self.release._documents(
                context,
                "contactOptOutSettlements",
            ).values()
            if item["contactSettlementHash"]
            == obligation["expectedContactSettlementHash"]
        ]
        self.assertEqual(1, len(contact_settlements))
        contact_settlement = deepcopy(contact_settlements[0])
        receipt = deepcopy(
            self.release._documents(
                context,
                "contactOptOutTransitionRequests",
            )[contact_settlement["contactTransitionId"]]
        )
        self.assertEqual(
            obligation,
            self.module.validate_contact_fanout_obligation_document(
                document=obligation
            ),
        )
        self.assertEqual(
            stored_result,
            self.module.validate_contact_fanout_result_document(
                document=stored_result
            ),
        )
        self.assertEqual(result, stored_result)
        self.assertEqual("apply", obligation["outcome"])
        self.assertEqual("apply", result["outcome"])
        self.assertEqual("applied", result["disposition"])
        self.assertEqual(
            claim,
            self.module.validate_claim_set_document(document=claim),
        )
        self.release._assert_owner_lineage_self_valid(
            claim,
            generation,
            settlement,
        )
        self.assertEqual(
            fanout,
            self.module.validate_contact_fanout_head_document(document=fanout),
        )
        self.assertEqual(
            contact_settlement,
            self.module.validate_contact_settlement_document(
                document=contact_settlement
            ),
        )
        self.assertEqual(
            receipt,
            self.module.validate_contact_transition_request_document(
                document=receipt
            ),
        )
        self.assertEqual(context["canonicalHash"], claim["ownerKey"])
        self.assertEqual(result["fanoutId"], claim["fanoutId"])
        self.assertEqual(result["claimSetHash"], claim["claimSetHash"])
        self.assertEqual(result["rowGeneration"], generation["generation"])
        self.assertEqual(
            result["rowSettlementHash"], settlement["settlementHash"]
        )
        self.assertEqual(result["fanoutId"], fanout["fanoutId"])
        self.assertEqual(
            obligation["expectedContactSettlementHash"],
            contact_settlement["contactSettlementHash"],
        )
        self.assertEqual(
            context["canonicalHash"],
            contact_settlement["canonicalMailboxIdentityHash"],
        )
        self.assertEqual(
            contact_settlement["contactTransitionId"],
            receipt["contactTransitionId"],
        )
        self.assertEqual(
            obligation["contactFanoutObligationHash"],
            result["obligationHash"],
        )
        return {
            "documentId": document_id,
            "obligation": obligation,
            "result": result,
            "claim": claim,
            "generation": generation,
            "settlement": settlement,
            "fanout": fanout,
            "contactSettlement": contact_settlement,
            "receipt": receipt,
        }

    def _seed_current_release_origin(self):
        context = self.release._seed_release(prior_owner="human_decision")
        context.update(
            {
                "selectedApplyResult": deepcopy(context["applyResult"]),
                "releaseProcessedAt": self.release.processed_at,
            }
        )
        origin = self._origin_evidence(context)
        self.assertEqual(
            context["activeFanout"]["fanoutId"],
            origin["result"]["fanoutId"],
        )
        self.assertEqual(
            context["beforeReleaseHead"]["effectiveSettlementHash"],
            origin["result"]["rowSettlementHash"],
        )
        context["selectedOrigin"] = origin
        return context

    def _seed_older_same_canonical_release_origin(self):
        context = self.release._seed_release(prior_owner="human_decision")
        epoch_a_result = deepcopy(context["applyResult"])
        epoch_a_head = deepcopy(context["beforeReleaseHead"])

        bundle, _link = context["transition"]._seed_bundle(
            context["store"],
            "source-release-origin-epoch-b",
            exact_hash=context["activeSettlement"]["exactIdentityHash"],
        )
        epoch_b = context["transition"]._record(
            context["store"],
            bundle,
            requested_at="2026-08-04T12:06:40.000000Z",
        )
        self.assertEqual("created", epoch_b["disposition"])
        self.release._lease_and_discover(
            context,
            epoch_b,
            leased_at="2026-08-04T12:06:50.000000Z",
            discovered_at="2026-08-04T12:07:00.000000Z",
        )
        epoch_b_apply = self.release.apply._process(
            context,
            processed_at="2026-08-04T12:07:10.000000Z",
        )
        self.assertEqual("dominated", epoch_b_apply["disposition"])
        self.assertEqual(epoch_a_head, self.release._row_head(context))
        context.update(
            {
                "activeSettlement": epoch_b["settlement"],
                "activeReceipt": epoch_b["transitionRequest"],
                "activeFanout": epoch_b_apply["fanoutHead"],
                "fanout": epoch_b_apply["fanoutHead"],
                "epochBApplyResult": deepcopy(epoch_b_apply["result"]),
            }
        )

        release_b = self.release._release_transition(
            context,
            requested_at="2026-08-04T12:07:20.000000Z",
        )
        self.release._lease_and_discover(
            context,
            release_b,
            leased_at="2026-08-04T12:07:30.000000Z",
            discovered_at="2026-08-04T12:07:40.000000Z",
        )
        context.update(
            {
                "beforeReleaseHead": deepcopy(
                    self.release._row_head(context)
                ),
                "beforeFanout": deepcopy(context["fanout"]),
                "selectedApplyResult": epoch_a_result,
                "releaseProcessedAt": "2026-08-04T12:07:50.000000Z",
            }
        )
        context["store"].events.clear()

        origin = self._origin_evidence(context)
        self.assertNotEqual(
            context["activeFanout"]["fanoutId"],
            origin["result"]["fanoutId"],
        )
        self.assertEqual(
            context["beforeReleaseHead"]["effectiveSettlementHash"],
            origin["result"]["rowSettlementHash"],
        )
        self.assertEqual(
            context["activeSettlement"]["canonicalMailboxIdentityHash"],
            context["canonicalHash"],
        )
        context["selectedOrigin"] = origin
        return context

    def _crossed_apply_evidence(self, context):
        origin = context["selectedOrigin"]
        obligation = origin["obligation"]
        result = origin["result"]
        foreign_row_id = release_contracts._row_id(2)
        fixture = context["transition"].fixture
        _identity, foreign_head = fixture._seed_row(
            context["store"],
            foreign_row_id,
            lifecycle="active",
        )
        foreign_edge = self.module.build_contact_row_binding_document(
            user_scope_hash=context["scope"],
            canonical_mailbox_identity_hash=context["canonicalHash"],
            row_id=foreign_row_id,
            created_at=obligation["createdAt"],
        )
        self.release._reference(
            context,
            "contactRowBindings",
            foreign_edge["edgeId"],
        ).create(foreign_edge)
        crossed_obligation = (
            self.module.build_contact_fanout_obligation_document(
                user_scope_hash=context["scope"],
                fanout_id=obligation["fanoutId"],
                row_id=foreign_row_id,
                contact_row_edge_hash=foreign_edge["contactRowEdgeHash"],
                expected_contact_settlement_hash=obligation[
                    "expectedContactSettlementHash"
                ],
                outcome="apply",
                created_at=obligation["createdAt"],
            )
        )
        crossed_claim = self.module.build_claim_set_document(
            user_scope_hash=context["scope"],
            authority_origin="contact_fanout",
            authority_link=origin["claim"]["authorityLink"],
            operator_action_document=None,
            fanout_id=obligation["fanoutId"],
            row_ids=[foreign_row_id],
            primary_row_id=foreign_row_id,
            planned_writes=3,
            outcome="accepted",
            row_decisions=[
                {
                    "rowId": foreign_row_id,
                    "decision": "accepted",
                    "plannedGeneration": 1,
                    "winnerGenerationHash": None,
                    "winnerSettlementHash": None,
                }
            ],
            created_at=obligation["createdAt"],
            canonical_mailbox_identity_hash=context["canonicalHash"],
            contact_settlement_hash=obligation[
                "expectedContactSettlementHash"
            ],
        )
        crossed_generation = self.module.build_owner_generation_document(
            claim_set_document=crossed_claim,
            row_id=foreign_row_id,
            generation=1,
            predecessor_head_hash=foreign_head["headHash"],
            predecessor_settlement_hash=None,
            lease_epoch=1,
            first_fencing_token=1,
            created_at=obligation["createdAt"],
        )
        crossed_settlement = self.module.build_owner_settlement_document(
            generation_document=crossed_generation,
            claim_set_document=crossed_claim,
            fencing_token=1,
            outcome="contact_optout",
            settled_at=result["createdAt"],
            superseded_effective_settlement_hash=None,
        )
        crossed_result = self.module.build_contact_fanout_result_document(
            user_scope_hash=context["scope"],
            fanout_id=result["fanoutId"],
            row_id=foreign_row_id,
            obligation_hash=crossed_obligation[
                "contactFanoutObligationHash"
            ],
            outcome="apply",
            disposition="applied",
            reason_code="claim_accepted",
            observed_row_head_hash=foreign_head["headHash"],
            claim_request_id=crossed_claim["requestId"],
            claim_set_hash=crossed_claim["claimSetHash"],
            row_generation=crossed_generation["generation"],
            row_settlement_hash=crossed_settlement["settlementHash"],
            released_row_generation=None,
            released_row_settlement_hash=None,
            restored_effective_generation=None,
            restored_effective_settlement_hash=None,
            created_at=result["createdAt"],
        )
        self.release._reference(
            context,
            "rowClaimSets",
            crossed_claim["requestId"],
        ).create(crossed_claim)
        self.release._reference(
            context,
            "rowOwnerGenerations",
            f"{foreign_row_id}--1",
        ).create(crossed_generation)
        self.release._reference(
            context,
            "rowOwnerSettlements",
            f"{foreign_row_id}--1",
        ).create(crossed_settlement)
        foreign_document_id = f"{obligation['fanoutId']}--{foreign_row_id}"
        self.release._reference(
            context,
            "contactOptOutFanoutObligations",
            foreign_document_id,
        ).create(crossed_obligation)
        self.release._reference(
            context,
            "contactOptOutFanoutResults",
            foreign_document_id,
        ).create(crossed_result)
        context["store"].events.clear()
        self.assertEqual(
            foreign_edge,
            self.module.validate_contact_row_binding_document(
                document=foreign_edge
            ),
        )
        self.release._assert_owner_lineage_self_valid(
            crossed_claim,
            crossed_generation,
            crossed_settlement,
        )
        self.assertEqual(
            crossed_obligation,
            self.module.validate_contact_fanout_obligation_document(
                document=crossed_obligation
            ),
        )
        self.assertEqual(
            crossed_result,
            self.module.validate_contact_fanout_result_document(
                document=crossed_result
            ),
        )
        return {
            "obligation": crossed_obligation,
            "result": crossed_result,
        }

    def _corrupt_origin_evidence(
        self,
        context,
        *,
        evidence_kind,
        corruption,
    ):
        collection = {
            "obligation": "contactOptOutFanoutObligations",
            "result": "contactOptOutFanoutResults",
        }[evidence_kind]
        validator = {
            "obligation": self.module.validate_contact_fanout_obligation_document,
            "result": self.module.validate_contact_fanout_result_document,
        }[evidence_kind]
        origin = context["selectedOrigin"]
        document_id = origin["documentId"]
        original = deepcopy(origin[evidence_kind])

        if corruption == "missing":
            self.release._delete(context, collection, document_id)
            self.assertNotIn(
                document_id,
                self.release._documents(context, collection),
            )
            return
        if corruption == "malformed":
            malformed = deepcopy(original)
            malformed["unexpectedField"] = None
            with self.assertRaises(self.module.RowAuthorityConfigError):
                validator(document=malformed)
            self.release._replace(
                context,
                collection,
                document_id,
                malformed,
            )
            return
        if corruption == "wrong_path":
            wrong_document_id = f"wrong-path--{document_id}"
            self.release._delete(context, collection, document_id)
            self.release._reference(
                context,
                collection,
                wrong_document_id,
            ).create(original)
            context["store"].events.clear()
            documents = self.release._documents(context, collection)
            self.assertNotIn(document_id, documents)
            self.assertEqual(original, documents[wrong_document_id])
            return
        if corruption == "crossed":
            crossed = self._crossed_apply_evidence(context)[evidence_kind]
            self.assertNotEqual(context["rowId"], crossed["rowId"])
            self.release._replace(
                context,
                collection,
                document_id,
                crossed,
            )
            return
        if evidence_kind == "result" and corruption in {
            "crossed_claim_address",
            "crossed_generation_address",
            "crossed_settlement_address",
        }:
            crossed = self._crossed_apply_evidence(context)["result"]
            overrides = {}
            if corruption == "crossed_claim_address":
                overrides.update(
                    claim_request_id=crossed["claimRequestId"],
                    claim_set_hash=crossed["claimSetHash"],
                )
            elif corruption == "crossed_generation_address":
                overrides["row_generation"] = crossed["rowGeneration"]
            else:
                overrides["row_settlement_hash"] = crossed[
                    "rowSettlementHash"
                ]
            rebuilt = self.module.build_contact_fanout_result_document(
                user_scope_hash=original["userScopeHash"],
                fanout_id=original["fanoutId"],
                row_id=original["rowId"],
                obligation_hash=original["obligationHash"],
                outcome=original["outcome"],
                disposition=original["disposition"],
                reason_code=original["reasonCode"],
                observed_row_head_hash=original["observedRowHeadHash"],
                claim_request_id=overrides.get(
                    "claim_request_id", original["claimRequestId"]
                ),
                claim_set_hash=overrides.get(
                    "claim_set_hash", original["claimSetHash"]
                ),
                row_generation=overrides.get(
                    "row_generation", original["rowGeneration"]
                ),
                row_settlement_hash=overrides.get(
                    "row_settlement_hash", original["rowSettlementHash"]
                ),
                released_row_generation=None,
                released_row_settlement_hash=None,
                restored_effective_generation=None,
                restored_effective_settlement_hash=None,
                created_at=original["createdAt"],
            )
            self.assertEqual(
                rebuilt,
                self.module.validate_contact_fanout_result_document(
                    document=rebuilt
                ),
            )
            self.release._replace(
                context,
                collection,
                document_id,
                rebuilt,
            )
            return
        raise AssertionError(f"unsupported origin corruption: {corruption}")

    def _assert_origin_validation_rejected_without_writes(self, context):
        before = deepcopy(context["store"].data)
        context["store"].events.clear()
        with self.assertRaises(self.module.RowAuthorityError) as raised:
            self.release._process(
                context,
                processed_at=context["releaseProcessedAt"],
            )
        self.assertEqual(before, context["store"].data)
        self.assertEqual([], self.release._writes(context["store"]))
        self.assertIsInstance(
            raised.exception,
            self.module.RowAuthorityAmbiguous,
            "stored originating APPLY corruption must be classified as "
            "zero-write ambiguity, not caller conflict or missing release dispatch",
        )

    def _replace_apply_result(
        self,
        context,
        result,
        *,
        obligation_hash=None,
        created_at=None,
        claim_request_id=None,
        claim_set_hash=None,
    ):
        rebuilt = self.module.build_contact_fanout_result_document(
            user_scope_hash=result["userScopeHash"],
            fanout_id=result["fanoutId"],
            row_id=result["rowId"],
            obligation_hash=(
                result["obligationHash"]
                if obligation_hash is None
                else obligation_hash
            ),
            outcome="apply",
            disposition=result["disposition"],
            reason_code=result["reasonCode"],
            observed_row_head_hash=result["observedRowHeadHash"],
            claim_request_id=(
                result["claimRequestId"]
                if claim_request_id is None
                else claim_request_id
            ),
            claim_set_hash=(
                result["claimSetHash"]
                if claim_set_hash is None
                else claim_set_hash
            ),
            row_generation=result["rowGeneration"],
            row_settlement_hash=result["rowSettlementHash"],
            released_row_generation=None,
            released_row_settlement_hash=None,
            restored_effective_generation=None,
            restored_effective_settlement_hash=None,
            created_at=(
                result["createdAt"] if created_at is None else created_at
            ),
        )
        self.release._replace(
            context,
            "contactOptOutFanoutResults",
            f"{result['fanoutId']}--{result['rowId']}",
            rebuilt,
        )
        return rebuilt

    def _cross_row_history_head(self, context):
        head = deepcopy(self.release._row_head(context))
        head.update(
            {
                "latestSettlementHash": "f" * 64,
                "latestOptOutReleaseResultHash": "e" * 64,
            }
        )
        crossed = context["transition"].fixture._rehash_head(head)
        self.assertEqual(
            crossed,
            self.module.validate_row_authority_head(document=crossed),
        )
        self.release._replace(
            context,
            "rowAuthorityHeads",
            context["rowId"],
            crossed,
        )
        context["beforeReleaseHead"] = deepcopy(crossed)
        return crossed

    def test_release_rejects_crossed_bounded_history_before_restore_or_noop(
        self,
    ):
        cases = (
            ("restore", self._seed_current_release_origin),
            (
                "noop",
                lambda: self.release._seed_release(
                    apply_outcome="dominated"
                ),
            ),
        )
        for disposition, seed in cases:
            with self.subTest(disposition=disposition):
                context = seed()
                context["releaseProcessedAt"] = self.release.processed_at
                self._cross_row_history_head(context)
                self._assert_origin_validation_rejected_without_writes(
                    context
                )

    def test_release_rejects_origin_obligation_that_predates_its_fanout(self):
        context = self._seed_current_release_origin()
        origin = context["selectedOrigin"]
        obligation = self.module.build_contact_fanout_obligation_document(
            user_scope_hash=origin["obligation"]["userScopeHash"],
            fanout_id=origin["obligation"]["fanoutId"],
            row_id=origin["obligation"]["rowId"],
            contact_row_edge_hash=origin["obligation"][
                "contactRowEdgeHash"
            ],
            expected_contact_settlement_hash=origin["obligation"][
                "expectedContactSettlementHash"
            ],
            outcome="apply",
            created_at="2026-08-04T12:00:02.000000Z",
        )
        self.assertLess(
            obligation["createdAt"], origin["fanout"]["createdAt"]
        )
        self.release._replace(
            context,
            "contactOptOutFanoutObligations",
            origin["documentId"],
            obligation,
        )
        self._replace_apply_result(
            context,
            origin["result"],
            obligation_hash=obligation["contactFanoutObligationHash"],
        )
        self._assert_origin_validation_rejected_without_writes(context)

    def test_release_rejects_dominated_origin_with_missing_exact_claim(self):
        context = self.release._seed_release(apply_outcome="dominated")
        context["releaseProcessedAt"] = self.release.processed_at
        result = context["applyResult"]
        self.release._delete(
            context,
            "rowClaimSets",
            result["claimRequestId"],
        )
        self._assert_origin_validation_rejected_without_writes(context)

    def test_release_rejects_dominated_origin_with_crossed_claim_or_winner(
        self,
    ):
        for corruption in ("crossed_claim", "crossed_winner"):
            with self.subTest(corruption=corruption):
                context = self.release._seed_release(
                    apply_outcome="dominated"
                )
                context["releaseProcessedAt"] = self.release.processed_at
                result = context["applyResult"]
                claims = self.release._documents(context, "rowClaimSets")
                original = deepcopy(claims[result["claimRequestId"]])
                if corruption == "crossed_claim":
                    forged = deepcopy(
                        next(
                            claim
                            for request_id, claim in claims.items()
                            if request_id != result["claimRequestId"]
                        )
                    )
                else:
                    decision = deepcopy(original["rowDecisions"][0])
                    decision.update(
                        {
                            "winnerGenerationHash": "f" * 64,
                            "winnerSettlementHash": "e" * 64,
                        }
                    )
                    forged = self.module.build_claim_set_document(
                        user_scope_hash=context["scope"],
                        authority_origin="contact_fanout",
                        authority_link=original["authorityLink"],
                        operator_action_document=None,
                        fanout_id=original["fanoutId"],
                        row_ids=[context["rowId"]],
                        primary_row_id=context["rowId"],
                        planned_writes=1,
                        outcome="dominated",
                        row_decisions=[decision],
                        created_at=original["createdAt"],
                        canonical_mailbox_identity_hash=original["ownerKey"],
                        contact_settlement_hash=original["payloadHash"],
                    )
                    self.assertEqual(
                        original["requestId"], forged["requestId"]
                    )
                    self._replace_apply_result(
                        context,
                        result,
                        claim_set_hash=forged["claimSetHash"],
                    )
                self.assertEqual(
                    forged,
                    self.module.validate_claim_set_document(document=forged),
                )
                self.release._replace(
                    context,
                    "rowClaimSets",
                    result["claimRequestId"],
                    forged,
                )
                self._assert_origin_validation_rejected_without_writes(
                    context
                )

    def test_release_rejects_dominated_origin_with_lower_priority_winner(
        self,
    ):
        context = self.release._seed_release(
            prior_owner="terminal",
            apply_outcome="dominated",
        )
        context["releaseProcessedAt"] = self.release.processed_at
        result = context["applyResult"]
        original = deepcopy(
            self.release._documents(context, "rowClaimSets")[
                result["claimRequestId"]
            ]
        )
        prior = context["prior"]
        decision = deepcopy(original["rowDecisions"][0])
        decision.update(
            {
                "winnerGenerationHash": prior["generation"][
                    "generationHash"
                ],
                "winnerSettlementHash": prior["settlement"][
                    "settlementHash"
                ],
            }
        )
        forged = self.module.build_claim_set_document(
            user_scope_hash=context["scope"],
            authority_origin="contact_fanout",
            authority_link=original["authorityLink"],
            operator_action_document=None,
            fanout_id=original["fanoutId"],
            row_ids=[context["rowId"]],
            primary_row_id=context["rowId"],
            planned_writes=1,
            outcome="dominated",
            row_decisions=[decision],
            created_at=original["createdAt"],
            canonical_mailbox_identity_hash=original["ownerKey"],
            contact_settlement_hash=original["payloadHash"],
        )
        self.assertEqual(original["requestId"], forged["requestId"])
        self.assertGreater(
            forged["derivedPriority"],
            prior["generation"]["priority"],
        )
        self.release._replace(
            context,
            "rowClaimSets",
            result["claimRequestId"],
            forged,
        )
        self._replace_apply_result(
            context,
            result,
            claim_set_hash=forged["claimSetHash"],
        )

        self._assert_origin_validation_rejected_without_writes(context)

    def test_release_rejects_dominated_origin_naming_released_winner(self):
        context = self.release._seed_release(prior_owner="terminal")
        released_apply = deepcopy(context["applyResult"])
        released_generation_id = (
            f"{context['rowId']}--{released_apply['rowGeneration']}"
        )
        released_generation = deepcopy(
            self.release._documents(context, "rowOwnerGenerations")[
                released_generation_id
            ]
        )
        released_settlement = deepcopy(
            self.release._documents(context, "rowOwnerSettlements")[
                released_generation_id
            ]
        )
        released_a = self.release._process(context)
        self.assertEqual("restore", released_a["disposition"])
        self.assertEqual(
            context["prior"]["settlement"]["settlementHash"],
            self.release._row_head(context)["effectiveSettlementHash"],
        )

        bundle, _link = context["transition"]._seed_bundle(
            context["store"],
            "source-release-origin-stale-dominated-winner",
            exact_hash=context["activeSettlement"]["exactIdentityHash"],
        )
        epoch_b = context["transition"]._record(
            context["store"],
            bundle,
            requested_at="2026-08-04T12:06:40.000000Z",
        )
        self.release._lease_and_discover(
            context,
            epoch_b,
            leased_at="2026-08-04T12:06:50.000000Z",
            discovered_at="2026-08-04T12:07:00.000000Z",
        )
        before_forged_claim = deepcopy(self.release._row_head(context))
        decision = {
            "rowId": context["rowId"],
            "decision": "dominated",
            "plannedGeneration": None,
            "winnerGenerationHash": released_generation["generationHash"],
            "winnerSettlementHash": released_settlement["settlementHash"],
        }
        claim = self.module.build_claim_set_document(
            user_scope_hash=context["scope"],
            authority_origin="contact_fanout",
            authority_link=epoch_b["settlement"]["authorityLink"],
            operator_action_document=None,
            fanout_id=context["fanout"]["fanoutId"],
            row_ids=[context["rowId"]],
            primary_row_id=context["rowId"],
            planned_writes=1,
            outcome="dominated",
            row_decisions=[decision],
            created_at="2026-08-04T12:07:10.000000Z",
            canonical_mailbox_identity_hash=context["canonicalHash"],
            contact_settlement_hash=epoch_b["settlement"][
                "contactSettlementHash"
            ],
        )
        result = self.module.build_contact_fanout_result_document(
            user_scope_hash=context["scope"],
            fanout_id=context["fanout"]["fanoutId"],
            row_id=context["rowId"],
            obligation_hash=context["obligation"][
                "contactFanoutObligationHash"
            ],
            outcome="apply",
            disposition="dominated",
            reason_code="claim_dominated",
            observed_row_head_hash=before_forged_claim["headHash"],
            claim_request_id=claim["requestId"],
            claim_set_hash=claim["claimSetHash"],
            row_generation=None,
            row_settlement_hash=None,
            released_row_generation=None,
            released_row_settlement_hash=None,
            restored_effective_generation=None,
            restored_effective_settlement_hash=None,
            created_at=claim["createdAt"],
        )
        advanced_apply = self.module._build_contact_fanout_processing_head(
            expected_document=context["fanout"],
            result_count=1,
            discovery_cursor_row_id=context["rowId"],
            cursor_processed_count=1,
            processed_at=claim["createdAt"],
        )
        self.release._reference(
            context,
            "rowClaimSets",
            claim["requestId"],
        ).create(claim)
        self.release._reference(
            context,
            "contactOptOutFanoutResults",
            f"{result['fanoutId']}--{context['rowId']}",
        ).create(result)
        self.release._replace(
            context,
            "contactOptOutFanoutHeads",
            advanced_apply["fanoutId"],
            advanced_apply,
        )
        self.assertLess(
            released_a["result"]["createdAt"],
            claim["createdAt"],
        )
        self.assertEqual(
            released_settlement["settlementHash"],
            decision["winnerSettlementHash"],
        )

        context.update(
            {
                "activeSettlement": epoch_b["settlement"],
                "activeReceipt": epoch_b["transitionRequest"],
                "activeFanout": advanced_apply,
                "fanout": advanced_apply,
                "applyResult": result,
            }
        )
        release_b = self.release._release_transition(
            context,
            requested_at="2026-08-04T12:07:20.000000Z",
        )
        self.release._lease_and_discover(
            context,
            release_b,
            leased_at="2026-08-04T12:07:30.000000Z",
            discovered_at="2026-08-04T12:07:40.000000Z",
        )
        context.update(
            {
                "beforeReleaseHead": deepcopy(
                    self.release._row_head(context)
                ),
                "beforeFanout": deepcopy(context["fanout"]),
                "releaseProcessedAt": "2026-08-04T12:07:50.000000Z",
            }
        )
        context["store"].events.clear()

        self._assert_origin_validation_rejected_without_writes(context)

    def test_release_rejects_apply_origin_after_fanout_update(self):
        context = self.release._seed_release(apply_outcome="dominated")
        context["releaseProcessedAt"] = self.release.processed_at
        result = context["applyResult"]
        original = deepcopy(
            self.release._documents(context, "rowClaimSets")[
                result["claimRequestId"]
            ]
        )
        late_at = "2026-08-04T12:06:15.000000Z"
        late_claim = self.module.build_claim_set_document(
            user_scope_hash=context["scope"],
            authority_origin="contact_fanout",
            authority_link=original["authorityLink"],
            operator_action_document=None,
            fanout_id=original["fanoutId"],
            row_ids=[context["rowId"]],
            primary_row_id=context["rowId"],
            planned_writes=original["plannedWrites"],
            outcome=original["outcome"],
            row_decisions=deepcopy(original["rowDecisions"]),
            created_at=late_at,
            canonical_mailbox_identity_hash=original["ownerKey"],
            contact_settlement_hash=original["payloadHash"],
        )
        self.release._delete(
            context,
            "rowClaimSets",
            original["requestId"],
        )
        self.release._reference(
            context,
            "rowClaimSets",
            late_claim["requestId"],
        ).create(late_claim)
        late_result = self._replace_apply_result(
            context,
            result,
            claim_request_id=late_claim["requestId"],
            claim_set_hash=late_claim["claimSetHash"],
            created_at=late_at,
        )
        origin_fanout = self.release._documents(
            context,
            "contactOptOutFanoutHeads",
        )[result["fanoutId"]]
        self.assertLess(origin_fanout["updatedAt"], late_result["createdAt"])
        self.assertLess(
            late_result["createdAt"],
            context["releaseProcessedAt"],
        )

        self._assert_origin_validation_rejected_without_writes(context)

    def test_release_rejects_origin_fanout_updated_after_worker_time(self):
        context = self.release._seed_release(prior_owner="human_decision")
        context["releaseProcessedAt"] = self.release.processed_at
        apply_result = context["applyResult"]
        stored = self.release._documents(
            context,
            "contactOptOutFanoutHeads",
        )[apply_result["fanoutId"]]
        future = self.release.discovery._fanout(
            stored,
            updated_at="2026-08-04T12:07:00.000000Z",
        )
        self.assertGreater(
            future["updatedAt"],
            context["releaseProcessedAt"],
        )
        self.release._replace(
            context,
            "contactOptOutFanoutHeads",
            stored["fanoutId"],
            future,
        )

        self._assert_origin_validation_rejected_without_writes(context)

    def test_release_rejects_origin_fanout_rolled_back_after_release(self):
        context = self.release._seed_release(prior_owner="human_decision")
        context["releaseProcessedAt"] = self.release.processed_at
        result = context["applyResult"]
        original_apply_fanout = context["activeFanout"]
        rolled_back = self.module._build_contact_fanout_processing_head(
            expected_document=original_apply_fanout,
            result_count=original_apply_fanout["resultCount"] + 1,
            discovery_cursor_row_id=context["rowId"],
            cursor_processed_count=1,
            processed_at=result["createdAt"],
        )
        self.assertEqual("applying", rolled_back["state"])
        self.release._replace(
            context,
            "contactOptOutFanoutHeads",
            result["fanoutId"],
            rolled_back,
        )

        self._assert_origin_validation_rejected_without_writes(context)

    def test_release_rejects_current_origin_fanout_with_wrong_successor(self):
        context = self.release._seed_release(prior_owner="human_decision")
        context["releaseProcessedAt"] = self.release.processed_at
        apply_result = context["applyResult"]
        stored = self.release._documents(
            context,
            "contactOptOutFanoutHeads",
        )[apply_result["fanoutId"]]
        self.assertEqual("superseding", stored["state"])
        crossed = self.release.discovery._fanout(
            stored,
            superseding_contact_settlement_hash="f" * 64,
        )
        self.release._replace(
            context,
            "contactOptOutFanoutHeads",
            stored["fanoutId"],
            crossed,
        )

        self._assert_origin_validation_rejected_without_writes(context)

    def test_release_rejects_apply_origin_completed_after_release(self):
        context = self.release._seed_release(prior_owner="human_decision")
        context["releaseProcessedAt"] = self.release.processed_at
        apply_result = context["applyResult"]
        stored = self.release._documents(
            context,
            "contactOptOutFanoutHeads",
        )[apply_result["fanoutId"]]
        self.assertEqual("superseding", stored["state"])
        completed_at = "2026-08-04T12:06:05.000000Z"
        self.assertGreater(
            completed_at,
            context["releaseTransition"]["settlement"]["settledAt"],
        )
        impossible = self.release.discovery._fanout(
            stored,
            state="complete",
            lease_owner_hash=None,
            lease_until=None,
            discovery_cursor_row_id=None,
            cursor_processed_count=0,
            superseding_contact_settlement_hash=None,
            completion_binding_revision=stored["bindingRevision"],
            completion_binding_head_hash=stored["bindingHeadHash"],
            completion_binding_association_count=stored[
                "bindingAssociationCount"
            ],
            completion_obligation_count=stored["obligationCount"],
            completion_result_count=stored["resultCount"],
            completed_at=completed_at,
            updated_at=completed_at,
        )
        self.release._replace(
            context,
            "contactOptOutFanoutHeads",
            stored["fanoutId"],
            impossible,
        )

        self._assert_origin_validation_rejected_without_writes(context)

    def test_release_rejects_apply_origin_timestamp_drift(self):
        cases = (
            ("applied", self._seed_current_release_origin),
            (
                "dominated",
                lambda: self.release._seed_release(
                    apply_outcome="dominated"
                ),
            ),
        )
        for disposition, seed in cases:
            with self.subTest(disposition=disposition):
                context = seed()
                context["releaseProcessedAt"] = self.release.processed_at
                result = (
                    context["selectedOrigin"]["result"]
                    if disposition == "applied"
                    else context["applyResult"]
                )
                self._replace_apply_result(
                    context,
                    result,
                    created_at="2026-08-04T12:05:46.000000Z",
                )
                self._assert_origin_validation_rejected_without_writes(
                    context
                )

    def _seed_forged_row_deleted_origin(self, *, deleted_at=None):
        context = self.release.apply._seed_apply()
        context["activeSettlement"] = context["settlement"]
        context["activeReceipt"] = context["receipt"]
        context["activeFanout"] = context["fanout"]
        context["preApplyHead"] = deepcopy(context["rowHead"])
        apply_fanout = context["fanout"]
        document_id = f"{apply_fanout['fanoutId']}--{context['rowId']}"
        obligation = context["obligation"]
        forged_at = "2026-08-04T12:05:45.000000Z"
        live_head = self.release._row_head(context)
        advanced_fanout = self.module._build_contact_fanout_processing_head(
            expected_document=apply_fanout,
            result_count=apply_fanout["resultCount"] + 1,
            discovery_cursor_row_id=context["rowId"],
            cursor_processed_count=1,
            processed_at=forged_at,
        )
        self.release._replace(
            context,
            "contactOptOutFanoutHeads",
            apply_fanout["fanoutId"],
            advanced_fanout,
        )
        forged = self.module.build_contact_fanout_result_document(
            user_scope_hash=context["scope"],
            fanout_id=apply_fanout["fanoutId"],
            row_id=context["rowId"],
            obligation_hash=obligation["contactFanoutObligationHash"],
            outcome="apply",
            disposition="noop",
            reason_code="row_deleted",
            observed_row_head_hash=live_head["headHash"],
            claim_request_id=None,
            claim_set_hash=None,
            row_generation=None,
            row_settlement_hash=None,
            released_row_generation=None,
            released_row_settlement_hash=None,
            restored_effective_generation=None,
            restored_effective_settlement_hash=None,
            created_at=forged_at,
        )
        self.release._reference(
            context,
            "contactOptOutFanoutResults",
            document_id,
        ).create(forged)
        context["fanout"] = advanced_fanout
        context["applyResult"] = forged

        if deleted_at is not None:
            identity = self.release._documents(context, "rowIdentities")[
                context["rowId"]
            ]
            deletion = self.module.build_row_location_revision_document(
                identity_document=identity,
                revision=live_head["currentLocationRevision"] + 1,
                lifecycle="deleted",
                observations=(),
                previous_revision_hash=live_head["currentLocationHash"],
                observed_at=deleted_at,
            )
            current_head = self.module.build_location_advanced_head(
                expected_head=live_head,
                location_revision_document=deletion,
            )
            self.release._reference(
                context,
                "rowLocationRevisions",
                f"{context['rowId']}--{deletion['revision']}",
            ).create(deletion)
            self.release._replace(
                context,
                "rowAuthorityHeads",
                context["rowId"],
                current_head,
            )
            context["rowHead"] = current_head

        context["beforeReleaseHead"] = deepcopy(
            self.release._row_head(context)
        )
        transition = self.release._release_transition(context)
        context["releaseTransition"] = transition
        self.release._lease_and_discover(context, transition)
        context["beforeFanout"] = deepcopy(context["fanout"])
        context["releaseProcessedAt"] = self.release.processed_at
        context["store"].events.clear()
        return context

    def test_release_rejects_row_deleted_origin_for_live_row(self):
        context = self._seed_forged_row_deleted_origin()
        self.assertNotEqual(
            "deleted",
            self.release._row_head(context)["currentLocationLifecycle"],
        )

        self._assert_origin_validation_rejected_without_writes(context)

    def test_release_rejects_row_deleted_origin_that_predates_deletion(self):
        context = self._seed_forged_row_deleted_origin(
            deleted_at="2026-08-04T12:05:50.000000Z"
        )

        self._assert_origin_validation_rejected_without_writes(context)

    def test_release_accepts_row_deleted_origin_after_bounded_deleted_successor(
        self,
    ):
        context = self.release.apply._seed_apply()
        identity = self.release._documents(context, "rowIdentities")[
            context["rowId"]
        ]
        active_head = self.release._row_head(context)
        deletion = self.module.build_row_location_revision_document(
            identity_document=identity,
            revision=active_head["currentLocationRevision"] + 1,
            lifecycle="deleted",
            observations=(),
            previous_revision_hash=active_head["currentLocationHash"],
            observed_at="2026-08-04T12:05:40.000000Z",
        )
        deleted_head = self.module.build_location_advanced_head(
            expected_head=active_head,
            location_revision_document=deletion,
        )
        self.release._reference(
            context,
            "rowLocationRevisions",
            f"{context['rowId']}--{deletion['revision']}",
        ).create(deletion)
        self.release._replace(
            context,
            "rowAuthorityHeads",
            context["rowId"],
            deleted_head,
        )
        context["rowHead"] = deleted_head
        context.update(
            {
                "activeSettlement": context["settlement"],
                "activeReceipt": context["receipt"],
                "activeFanout": context["fanout"],
                "preApplyHead": deepcopy(context["rowHead"]),
            }
        )
        applied = self.release.apply._process(context)
        self.assertEqual("noop", applied["disposition"])
        self.assertEqual("row_deleted", applied["result"]["reasonCode"])
        context["activeFanout"] = applied["fanoutHead"]
        successor = deepcopy(self.release._row_head(context))
        successor.update(
            {
                "stateRevision": successor["stateRevision"] + 1,
                "projectionBacklogCount": successor[
                    "projectionBacklogCount"
                ]
                + 1,
                "updatedAt": "2026-08-04T12:05:50.000000Z",
            }
        )
        successor = context["transition"].fixture._rehash_head(successor)
        self.assertEqual("deleted", successor["currentLocationLifecycle"])
        self.release._replace(
            context,
            "rowAuthorityHeads",
            context["rowId"],
            successor,
        )
        transition = self.release._release_transition(context)
        self.release._lease_and_discover(context, transition)
        context["releaseProcessedAt"] = self.release.processed_at
        context["store"].events.clear()

        processed = self.release._process(context)

        self.assertEqual("noop", processed["disposition"])
        self.assertEqual(
            "row_optout_not_applied", processed["result"]["reasonCode"]
        )
        self.assertEqual(successor, self.release._row_head(context))
        self.assertEqual(2, len(self.release._writes(context["store"])))

    def _assert_corruption_matrix(self, seed):
        for evidence_kind in ("obligation", "result"):
            corruptions = [
                "missing",
                "malformed",
                "wrong_path",
                "crossed",
            ]
            if evidence_kind == "result":
                corruptions.extend(
                    (
                        "crossed_claim_address",
                        "crossed_generation_address",
                        "crossed_settlement_address",
                    )
                )
            for corruption in corruptions:
                with self.subTest(
                    evidence_kind=evidence_kind,
                    corruption=corruption,
                ):
                    context = seed()
                    self._corrupt_origin_evidence(
                        context,
                        evidence_kind=evidence_kind,
                        corruption=corruption,
                    )
                    self._assert_origin_validation_rejected_without_writes(
                        context
                    )

    def _assert_crossed_origin_lineage_rejected(self, seed):
        for corruption in ("fanout", "contact_settlement", "creating_receipt"):
            with self.subTest(corruption=corruption):
                context = seed()
                origin = context["selectedOrigin"]
                if corruption == "fanout":
                    origin_fanout_id = origin["result"]["fanoutId"]
                    replacement = deepcopy(context["fanout"])
                    self.assertNotEqual(origin_fanout_id, replacement["fanoutId"])
                    self.assertEqual(
                        replacement,
                        self.module.validate_contact_fanout_head_document(
                            document=replacement
                        ),
                    )
                    self.release._replace(
                        context,
                        "contactOptOutFanoutHeads",
                        origin_fanout_id,
                        replacement,
                    )
                else:
                    settlements = self.release._documents(
                        context,
                        "contactOptOutSettlements",
                    )
                    origin_settlement = next(
                        item
                        for item in settlements.values()
                        if item["contactSettlementHash"]
                        == origin["obligation"][
                            "expectedContactSettlementHash"
                        ]
                    )
                    if corruption == "contact_settlement":
                        replacement = deepcopy(context["settlement"])
                        self.assertEqual(
                            replacement,
                            self.module.validate_contact_settlement_document(
                                document=replacement
                            ),
                        )
                        self.assertNotEqual(
                            origin_settlement["contactSettlementHash"],
                            replacement["contactSettlementHash"],
                        )
                        settlement_id = (
                            f"{context['canonicalHash']}--"
                            f"{origin_settlement['generation']}"
                        )
                        self.release._replace(
                            context,
                            "contactOptOutSettlements",
                            settlement_id,
                            replacement,
                        )
                    else:
                        receipts = self.release._documents(
                            context,
                            "contactOptOutTransitionRequests",
                        )
                        replacement = deepcopy(context["receipt"])
                        self.assertEqual(
                            replacement,
                            self.module.validate_contact_transition_request_document(
                                document=replacement
                            ),
                        )
                        self.assertNotEqual(
                            origin_settlement["contactTransitionId"],
                            replacement["contactTransitionId"],
                        )
                        self.assertIn(
                            replacement["contactTransitionId"], receipts
                        )
                        self.release._replace(
                            context,
                            "contactOptOutTransitionRequests",
                            origin_settlement["contactTransitionId"],
                            replacement,
                        )
                self._assert_origin_validation_rejected_without_writes(context)

    def test_current_release_epoch_requires_exact_originating_apply_evidence(
        self,
    ):
        self._assert_corruption_matrix(self._seed_current_release_origin)
        self._assert_crossed_origin_lineage_rejected(
            self._seed_current_release_origin
        )

    def test_older_same_canonical_epoch_requires_its_own_originating_apply_evidence(
        self,
    ):
        self._assert_corruption_matrix(
            self._seed_older_same_canonical_release_origin
        )
        self._assert_crossed_origin_lineage_rejected(
            self._seed_older_same_canonical_release_origin
        )

    def test_release_selects_older_same_canonical_effective_settlement(self):
        context = self._seed_older_same_canonical_release_origin()
        selected = context["selectedOrigin"]["result"]
        prior = context["prior"]

        processed = self.release._process(
            context,
            processed_at=context["releaseProcessedAt"],
        )

        result = self.release._result(context)
        head = self.release._row_head(context)
        self.assertEqual("restore", processed["disposition"])
        self.assertEqual(
            selected["rowGeneration"],
            result["releasedRowGeneration"],
        )
        self.assertEqual(
            selected["rowSettlementHash"],
            result["releasedRowSettlementHash"],
        )
        self.assertEqual(
            prior["settlement"]["settlementHash"],
            result["restoredEffectiveSettlementHash"],
        )
        self.assertEqual(
            prior["settlement"]["settlementHash"],
            head["effectiveSettlementHash"],
        )
        self.assertEqual(3, len(self.release._writes(context["store"])))

    def test_stale_release_cannot_clear_later_same_canonical_epoch(self):
        context = self.release._seed_release(apply_outcome="not_applied")
        release_a = {
            key: deepcopy(context[key])
            for key in (
                "settlement",
                "receipt",
                "contactHead",
                "fanout",
                "obligation",
            )
        }
        bundle, _link = context["transition"]._seed_bundle(
            context["store"],
            "source-release-stale-a-future-b",
            exact_hash=context["activeSettlement"]["exactIdentityHash"],
        )
        epoch_b = context["transition"]._record(
            context["store"],
            bundle,
            requested_at="2026-08-04T12:06:40.000000Z",
        )
        self.release._lease_and_discover(
            context,
            epoch_b,
            leased_at="2026-08-04T12:06:50.000000Z",
            discovered_at="2026-08-04T12:07:00.000000Z",
        )
        applied_b = self.release.apply._process(
            context,
            processed_at="2026-08-04T12:07:10.000000Z",
        )
        self.assertEqual("applied", applied_b["disposition"])
        context["fanout"] = applied_b["fanoutHead"]
        completed_b = self.release.completion._certify(
            context,
            certified_at="2026-08-04T12:07:15.000000Z",
        )
        self.assertEqual("certification_complete", completed_b["disposition"])
        self.assertEqual("complete", completed_b["fanoutHead"]["state"])
        future_b_row_head = deepcopy(self.release._row_head(context))
        claim_b = self.release._documents(context, "rowClaimSets")[
            applied_b["result"]["claimRequestId"]
        ]
        self.assertEqual(
            epoch_b["settlement"]["contactSettlementHash"],
            claim_b["payloadHash"],
        )
        self.assertGreater(
            epoch_b["settlement"]["generation"],
            release_a["settlement"]["generation"],
        )
        self.assertGreater(
            epoch_b["settlement"]["settledAt"],
            release_a["settlement"]["settledAt"],
        )

        self.release._replace(
            context,
            "contactOptOutHeads",
            context["canonicalHash"],
            release_a["contactHead"],
        )
        self.release._replace(
            context,
            "contactOptOutFanoutHeads",
            release_a["fanout"]["fanoutId"],
            release_a["fanout"],
        )
        context.update(
            {
                "settlement": release_a["settlement"],
                "receipt": release_a["receipt"],
                "contactHead": release_a["contactHead"],
                "fanout": release_a["fanout"],
                "obligation": release_a["obligation"],
                "beforeReleaseHead": future_b_row_head,
                "releaseProcessedAt": "2026-08-04T12:07:20.000000Z",
            }
        )
        context["store"].events.clear()

        self._assert_origin_validation_rejected_without_writes(context)
        self.assertEqual(future_b_row_head, self.release._row_head(context))

    def test_stale_release_cannot_restore_after_dominated_later_epoch(self):
        context = self.release._seed_release(prior_owner="human_decision")
        release_a = {
            key: deepcopy(context[key])
            for key in (
                "settlement",
                "receipt",
                "contactHead",
                "fanout",
                "obligation",
            )
        }
        release_a_row = deepcopy(self.release._row_head(context))
        bundle, _link = context["transition"]._seed_bundle(
            context["store"],
            "source-stale-release-a-dominated-epoch-b",
            exact_hash=context["activeSettlement"]["exactIdentityHash"],
        )
        epoch_b = context["transition"]._record(
            context["store"],
            bundle,
            requested_at="2026-08-04T12:06:40.000000Z",
        )
        self.release._lease_and_discover(
            context,
            epoch_b,
            leased_at="2026-08-04T12:06:50.000000Z",
            discovered_at="2026-08-04T12:07:00.000000Z",
        )
        epoch_b_apply = self.release.apply._process(
            context,
            processed_at="2026-08-04T12:07:10.000000Z",
        )
        self.assertEqual("dominated", epoch_b_apply["disposition"])
        self.assertEqual(release_a_row, self.release._row_head(context))

        self.release._replace(
            context,
            "contactOptOutHeads",
            context["canonicalHash"],
            release_a["contactHead"],
        )
        self.release._replace(
            context,
            "contactOptOutFanoutHeads",
            release_a["fanout"]["fanoutId"],
            release_a["fanout"],
        )
        context.update(
            {
                "settlement": release_a["settlement"],
                "receipt": release_a["receipt"],
                "contactHead": release_a["contactHead"],
                "fanout": release_a["fanout"],
                "obligation": release_a["obligation"],
                "beforeReleaseHead": release_a_row,
                "releaseProcessedAt": "2026-08-04T12:07:20.000000Z",
            }
        )
        context["store"].events.clear()

        self._assert_origin_validation_rejected_without_writes(context)
        self.assertEqual(release_a_row, self.release._row_head(context))

    def test_release_rejects_direct_different_canonical_contact_successor(
        self,
    ):
        context = self._seed_older_same_canonical_release_origin()
        different = self.release._install_different_contact_successor(context)
        context["beforeReleaseHead"] = deepcopy(
            self.release._row_head(context)
        )
        before_head = deepcopy(context["beforeReleaseHead"])
        self.assertNotEqual(
            context["activeSettlement"]["canonicalMailboxIdentityHash"],
            different["claim"]["ownerKey"],
        )

        self._assert_origin_validation_rejected_without_writes(context)
        self.assertEqual(before_head, self.release._row_head(context))
        self.assertEqual(
            different["settlement"]["settlementHash"],
            self.release._row_head(context)["effectiveSettlementHash"],
        )
        self.assertEqual([], self.release._writes(context["store"]))
