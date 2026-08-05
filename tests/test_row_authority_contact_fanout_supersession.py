"""RED contracts for bounded contact fan-out supersession."""

from __future__ import annotations

import importlib
import inspect
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from unittest.mock import patch

from tests.row_authority_fakes import BoundedFakeTransaction
from tests import test_row_authority_contact_fanout_completion as completion_tests
from tests.test_row_authority_contact_fanout_discovery import (
    ContactFanoutDiscoveryTests,
    _row_id,
)


class ContactFanoutSupersessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")
        ContactFanoutDiscoveryTests.setUpClass()
        completion_tests.ContactFanoutCompletionTests.setUpClass()

    def setUp(self):
        self.discovery = ContactFanoutDiscoveryTests(methodName="runTest")
        self.discovery.setUp()
        self.lease_owner = "e" * 64
        self.released_at = "2026-08-04T12:06:00.000000Z"
        self.acquired_at = "2026-08-04T12:06:10.000000Z"
        self.superseded_at = "2026-08-04T12:06:30.000000Z"
        self.lease_until = "2026-08-04T12:12:00.000000Z"
        self.completion = completion_tests.ContactFanoutCompletionTests(
            methodName="runTest"
        )
        self.completion.setUp()

    def _method(self):
        method = getattr(
            self.module.RowAuthorityStore,
            "supersede_contact_fanout_page",
            None,
        )
        self.assertTrue(
            callable(method),
            "RowAuthorityStore.supersede_contact_fanout_page is missing",
        )
        signature = inspect.signature(method)
        self.assertEqual(
            [
                "self",
                "verified_user_id",
                "fanout_id",
                "expected_fanout_head",
                "lease_owner_hash",
                "superseded_at",
            ],
            list(signature.parameters),
        )
        self.assertTrue(
            all(
                value.kind is inspect.Parameter.KEYWORD_ONLY
                for name, value in signature.parameters.items()
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

    def _reference(self, context, collection, document_id):
        return self.discovery._reference(context, collection, document_id)

    def _result_reference(self, context, row_id):
        return self._reference(
            context,
            "contactOptOutFanoutResults",
            f"{context['fanout']['fanoutId']}--{row_id}",
        )

    def _row_head_reference(self, context, row_id):
        return self.discovery._user(context).collection(
            "rowAuthorityHeads"
        ).document(row_id)

    def _result(self, context, obligation, *, finished, created_at=None):
        row_head = context["rowHeads"][obligation["rowId"]]
        if finished:
            if context["fanout"]["outcome"] == "apply":
                disposition, reason = "noop", "row_deleted"
            else:
                disposition, reason = "noop", "row_optout_not_applied"
        else:
            disposition, reason = "superseded", "contact_head_advanced"
        return self.module.build_contact_fanout_result_document(
            user_scope_hash=context["scope"],
            fanout_id=context["fanout"]["fanoutId"],
            row_id=obligation["rowId"],
            obligation_hash=obligation["contactFanoutObligationHash"],
            outcome=context["fanout"]["outcome"],
            disposition=disposition,
            reason_code=reason,
            observed_row_head_hash=row_head["headHash"],
            claim_request_id=None,
            claim_set_hash=None,
            row_generation=None,
            row_settlement_hash=None,
            released_row_generation=None,
            released_row_settlement_hash=None,
            restored_effective_generation=None,
            restored_effective_settlement_hash=None,
            created_at=(
                created_at
                or (
                    "2026-08-04T12:05:45.000000Z"
                    if finished
                    else self.superseded_at
                )
            ),
        )

    def _seed_superseding(
        self,
        count,
        *,
        finished_indexes=(),
        source_id="source-contact-fanout-discovery",
        outcome="apply",
        finished_lifecycle="deleted",
    ):
        rows = [_row_id(index) for index in range(1, count + 1)]
        finished_indexes = frozenset(finished_indexes)
        if outcome == "apply":
            context = self.discovery._seed(rows, source_id=source_id)
            phase_time = self.discovery.discovered_at
        else:
            self.assertEqual("release", outcome)
            context = self.discovery._seed_release(rows)
            phase_time = "2026-08-04T12:06:03.000000Z"
        context["rowHeads"] = {}
        for index, row_id in enumerate(rows):
            _identity, head = context["transition"].fixture._seed_row(
                context["store"],
                row_id,
                lifecycle=(
                    finished_lifecycle
                    if index in finished_indexes
                    else "active"
                ),
            )
            context["rowHeads"][row_id] = head

        obligations = [
            self.discovery._obligation(
                context,
                edge,
                created_at=phase_time,
            )
            for edge in context["edges"]
        ]
        finished = {}
        for index in finished_indexes:
            result = self._result(context, obligations[index], finished=True)
            self._result_reference(context, result["rowId"]).create(result)
            finished[result["rowId"]] = result

        current = context["fanout"]
        applying = self.discovery._fanout(
            current,
            state_revision=current["stateRevision"] + 1,
            state="applying",
            discovery_cursor_row_id=None,
            obligation_count=count,
            result_count=len(finished),
            updated_at=phase_time,
        )
        self.discovery._store_fanout(context, applying)
        if outcome == "apply":
            context["transition"]._install_release_after_image(
                context["store"],
                released_at=self.released_at,
            )
        else:
            bundle, link = context["transition"]._seed_bundle(
                context["store"],
                f"{source_id}-successor",
            )
            self.assertEqual(
                context["canonicalHash"],
                link["canonicalMailboxIdentityHash"],
            )
            context["transition"]._record(
                context["store"],
                bundle,
                requested_at="2026-08-04T12:06:05.000000Z",
            )
        fanouts = context["transition"]._documents(
            context["store"],
            "contactOptOutFanoutHeads",
        )
        superseding = fanouts[applying["fanoutId"]]
        authority = context["transition"]._authority(context["store"])
        leased = authority.acquire_contact_fanout_lease(
            verified_user_id=context["transition"].fixture.user_id,
            fanout_id=superseding["fanoutId"],
            expected_fanout_head=superseding,
            lease_owner_hash=self.lease_owner,
            lease_until=self.lease_until,
            acquired_at=self.acquired_at,
        )["fanoutHead"]
        settlements = context["transition"]._documents(
            context["store"],
            "contactOptOutSettlements",
        )
        newer = next(
            document
            for document in settlements.values()
            if document["contactSettlementHash"]
            == leased["supersedingContactSettlementHash"]
        )
        receipts = context["transition"]._documents(
            context["store"],
            "contactOptOutTransitionRequests",
        )
        heads = context["transition"]._documents(
            context["store"],
            "contactOptOutHeads",
        )
        context.update(
            {
                "fanout": leased,
                "rows": rows,
                "obligations": obligations,
                "finishedResults": finished,
                "newerSettlement": newer,
                "newerReceipt": receipts[newer["contactTransitionId"]],
                "currentContactHead": heads[context["canonicalHash"]],
            }
        )
        context["store"].events.clear()
        return context

    def _supersede(
        self,
        context,
        expected=None,
        *,
        owner=None,
        superseded_at=None,
        executor=None,
    ):
        self._method()
        return context["transition"]._authority(
            context["store"],
            executor=executor,
        ).supersede_contact_fanout_page(
            verified_user_id=context["transition"].fixture.user_id,
            fanout_id=context["fanout"]["fanoutId"],
            expected_fanout_head=expected or context["fanout"],
            lease_owner_hash=owner or self.lease_owner,
            superseded_at=superseded_at or self.superseded_at,
        )

    def _assert_result(self, result, disposition):
        self.assertEqual(
            {"disposition", "fanoutHead", "results"},
            set(result),
        )
        self.assertEqual(disposition, result["disposition"])
        self.module.validate_contact_fanout_head_document(
            document=result["fanoutHead"]
        )
        for document in result["results"]:
            self.module.validate_contact_fanout_result_document(
                document=document
            )
        self.assertEqual(
            sorted(document["rowId"] for document in result["results"]),
            [document["rowId"] for document in result["results"]],
        )

    def _assert_new_superseded_results(self, context, result):
        optional_fields = (
            "claimRequestId",
            "claimSetHash",
            "rowGeneration",
            "rowSettlementHash",
            "releasedRowGeneration",
            "releasedRowSettlementHash",
            "restoredEffectiveGeneration",
            "restoredEffectiveSettlementHash",
        )
        obligations = {
            document["rowId"]: document
            for document in context["obligations"]
        }
        new_results = [
            document
            for document in result["results"]
            if document["rowId"] not in context["finishedResults"]
        ]
        for document in new_results:
            obligation = obligations[document["rowId"]]
            self.assertEqual("superseded", document["disposition"])
            self.assertEqual("contact_head_advanced", document["reasonCode"])
            self.assertEqual(self.superseded_at, document["createdAt"])
            self.assertEqual(
                obligation["contactFanoutObligationHash"],
                document["obligationHash"],
            )
            self.assertEqual(
                context["rowHeads"][document["rowId"]]["headHash"],
                document["observedRowHeadHash"],
            )
            self.assertTrue(
                all(document[field] is None for field in optional_fields)
            )
        return new_results

    def _assert_new_result_writes(self, context, new_results):
        writes = [
            event
            for event in self._writes(context["store"])
            if "/contactOptOutFanoutResults/" in event[1]
        ]
        self.assertEqual(len(new_results), len(writes))
        expected = {
            self._result_reference(context, document["rowId"]).path: document
            for document in new_results
        }
        for operation, path, payload, merge in writes:
            self.assertEqual(("create", False), (operation, merge))
            self.assertEqual(expected[path], payload)

    def _expected_head(self, before, *, added, cursor, terminal):
        overrides = {
            "state_revision": before["stateRevision"] + 1,
            "discovery_cursor_row_id": cursor,
            "cursor_processed_count": (
                0
                if cursor is None
                else before["cursorProcessedCount"] + 128
            ),
            "result_count": before["resultCount"] + added,
            "updated_at": self.superseded_at,
        }
        if terminal:
            overrides.update(
                {
                    "state": "superseded",
                    "lease_owner_hash": None,
                    "lease_until": None,
                }
            )
        return self.discovery._fanout(before, **overrides)

    def _assert_failure_without_write(self, context, operation):
        before = deepcopy(context["store"].data)
        context["store"].events.clear()
        with self.assertRaises(self.module.RowAuthorityError):
            operation()
        self.assertEqual(before, context["store"].data)
        self.assertEqual([], self._writes(context["store"]))

    def _replace_finished_result(self, context, result):
        self._result_reference(context, result["rowId"]).set(
            result,
            merge=False,
        )
        context["finishedResults"][result["rowId"]] = result
        context["store"].events.clear()
        return result

    def test_supersession_rejects_missing_obligation_membership(self):
        with self.subTest(case="matched-existing-control"):
            context = self._seed_superseding(1, finished_indexes=(0,))
            completed = self._supersede(context)
            self._assert_result(completed, "supersession_complete")

        with self.subTest(case="missing-obligation-orphan-result"):
            context = self._seed_superseding(1, finished_indexes=(0,))
            obligation = context["obligations"][0]
            obligation_ref = self._reference(
                context,
                "contactOptOutFanoutObligations",
                f"{context['fanout']['fanoutId']}--{obligation['rowId']}",
            )
            context["store"].data.pop(obligation_ref.path)
            self._assert_failure_without_write(
                context,
                lambda: self._supersede(context),
            )

        with self.subTest(case="matched-missing-obligation-result"):
            context = self._seed_superseding(1, finished_indexes=(0,))
            obligation = context["obligations"][0]
            obligation_ref = self._reference(
                context,
                "contactOptOutFanoutObligations",
                f"{context['fanout']['fanoutId']}--{obligation['rowId']}",
            )
            result_ref = self._result_reference(context, obligation["rowId"])
            context["store"].data.pop(obligation_ref.path)
            context["store"].data.pop(result_ref.path)
            self._assert_failure_without_write(
                context,
                lambda: self._supersede(context),
            )

        with self.subTest(case="matched-post-cursor-deletion"):
            context = self._seed_superseding(
                129,
                finished_indexes=(128,),
            )
            first = self._supersede(context)
            self._assert_result(first, "page_superseded")
            trailing = context["obligations"][128]
            obligation_ref = self._reference(
                context,
                "contactOptOutFanoutObligations",
                f"{context['fanout']['fanoutId']}--{trailing['rowId']}",
            )
            result_ref = self._result_reference(context, trailing["rowId"])
            context["store"].data.pop(obligation_ref.path)
            context["store"].data.pop(result_ref.path)
            self._assert_failure_without_write(
                context,
                lambda: self._supersede(context, first["fanoutHead"]),
            )

    def test_supersession_resolves_existing_result_evidence(self):
        with self.subTest(matrix="apply-applied-reachable"):
            context = self._seed_superseding(
                1,
                finished_indexes=(0,),
                finished_lifecycle="active",
            )
            obligation = context["obligations"][0]
            lineage = self.completion._install_contact_lineage(
                context,
                context["rowHeads"][obligation["rowId"]],
                materialize_head=True,
            )
            result = self.completion._build_result(
                context,
                obligation,
                lineage["observedHead"],
                disposition="applied",
                reason_code="claim_accepted",
                claim_request_id=lineage["claim"]["requestId"],
                claim_set_hash=lineage["claim"]["claimSetHash"],
                row_generation=lineage["generation"]["generation"],
                row_settlement_hash=lineage["settlement"]["settlementHash"],
            )
            self._replace_finished_result(context, result)
            self._assert_result(
                self._supersede(context),
                "supersession_complete",
            )

        with self.subTest(matrix="apply-applied-missing-lineage"):
            context = self._seed_superseding(
                1,
                finished_indexes=(0,),
                finished_lifecycle="active",
            )
            obligation = context["obligations"][0]
            result = self.completion._build_result(
                context,
                obligation,
                context["rowHeads"][obligation["rowId"]],
                observed_row_head_hash="a" * 64,
                disposition="applied",
                reason_code="claim_accepted",
                claim_request_id="b" * 64,
                claim_set_hash="c" * 64,
                row_generation=1,
                row_settlement_hash="d" * 64,
            )
            self._replace_finished_result(context, result)
            self._assert_failure_without_write(
                context,
                lambda: self._supersede(context),
            )

        for valid_winner in (True, False):
            with self.subTest(
                matrix="apply-dominated",
                valid=valid_winner,
            ):
                context = self._seed_superseding(
                    1,
                    finished_indexes=(0,),
                    finished_lifecycle="active",
                )
                row_id = context["rows"][0]
                fixture = context["transition"].fixture
                winner_claim, winner_generation, claimed_head = (
                    fixture._install_owner(
                        context["store"],
                        row_id,
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
                claim = self.completion._install_dominated_claim(
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
                result = self.completion._build_result(
                    context,
                    context["obligations"][0],
                    winner_head,
                    disposition="dominated",
                    reason_code="claim_dominated",
                    claim_request_id=claim["requestId"],
                    claim_set_hash=claim["claimSetHash"],
                )
                self._replace_finished_result(context, result)
                if valid_winner:
                    self._assert_result(
                        self._supersede(context),
                        "supersession_complete",
                    )
                else:
                    self._assert_failure_without_write(
                        context,
                        lambda: self._supersede(context),
                    )

        with self.subTest(matrix="apply-noop-deleted-control"):
            context = self._seed_superseding(1, finished_indexes=(0,))
            self._assert_result(
                self._supersede(context),
                "supersession_complete",
            )

        with self.subTest(matrix="apply-noop-deleted-active-row"):
            context = self._seed_superseding(
                1,
                finished_indexes=(0,),
                finished_lifecycle="active",
            )
            self._assert_failure_without_write(
                context,
                lambda: self._supersede(context),
            )

        for valid_lineage in (True, False):
            with self.subTest(
                matrix="release-restore",
                valid=valid_lineage,
            ):
                context = self._seed_superseding(
                    1,
                    finished_indexes=(0,),
                    outcome="release",
                    finished_lifecycle="active",
                )
                obligation = context["obligations"][0]
                lineage = self.completion._install_contact_lineage(
                    context,
                    context["rowHeads"][obligation["rowId"]],
                    materialize_head=True,
                    canonical_hash=(None if valid_lineage else "c" * 64),
                )
                result = self.completion._build_result(
                    context,
                    obligation,
                    lineage["settledHead"],
                    disposition="restore",
                    reason_code="exact_predecessor",
                    released_row_generation=lineage["generation"]["generation"],
                    released_row_settlement_hash=lineage["settlement"][
                        "settlementHash"
                    ],
                    created_at="2026-08-04T12:06:04.000000Z",
                )
                self._replace_finished_result(context, result)
                self.completion._install_clear_release_after_image(
                    context,
                    lineage,
                    result,
                )
                if valid_lineage:
                    self._assert_result(
                        self._supersede(context),
                        "supersession_complete",
                    )
                else:
                    self._assert_failure_without_write(
                        context,
                        lambda: self._supersede(context),
                    )

    def test_release_noop_evidence_uses_result_time_row_authority(self):
        with self.subTest(
            case="not-applied-rejects-effective-same-canonical-optout"
        ):
            context = self._seed_superseding(
                1,
                finished_indexes=(0,),
                outcome="release",
                finished_lifecycle="active",
            )
            obligation = context["obligations"][0]
            lineage = self.completion._install_contact_lineage(
                context,
                context["rowHeads"][obligation["rowId"]],
                materialize_head=True,
            )
            self.assertEqual(
                context["canonicalHash"],
                lineage["generation"]["ownerKey"],
            )
            self.assertEqual(
                lineage["settlement"]["settlementHash"],
                lineage["settledHead"]["effectiveSettlementHash"],
            )
            result = self.completion._build_result(
                context,
                obligation,
                lineage["settledHead"],
                disposition="noop",
                reason_code="row_optout_not_applied",
                created_at=self.completion.release_result_at,
            )
            self._replace_finished_result(context, result)
            self._assert_failure_without_write(
                context,
                lambda: self._supersede(context),
            )

        with self.subTest(
            case="different-owner-rejects-missing-lineage-on-clear-row"
        ):
            context = self._seed_superseding(
                1,
                finished_indexes=(0,),
                outcome="release",
                finished_lifecycle="active",
            )
            obligation = context["obligations"][0]
            clear_head = context["rowHeads"][obligation["rowId"]]
            self.assertEqual("clear", clear_head["state"])
            self.assertIsNone(clear_head["effectiveOwnerGeneration"])
            result = self.completion._build_result(
                context,
                obligation,
                clear_head,
                disposition="noop",
                reason_code="different_effective_owner",
                released_row_generation=99,
                released_row_settlement_hash="f" * 64,
                created_at=self.completion.release_result_at,
            )
            self._replace_finished_result(context, result)
            self._assert_failure_without_write(
                context,
                lambda: self._supersede(context),
            )

        with self.subTest(
            case="historical-not-applied-allows-proven-newer-successor"
        ):
            context = self._seed_superseding(
                1,
                finished_indexes=(0,),
                outcome="release",
                finished_lifecycle="active",
            )
            obligation = context["obligations"][0]
            clear_head = context["rowHeads"][obligation["rowId"]]
            self.assertEqual("clear", clear_head["state"])
            self.assertIsNone(clear_head["effectiveSettlementHash"])
            historical = self.completion._build_result(
                context,
                obligation,
                clear_head,
                disposition="noop",
                reason_code="row_optout_not_applied",
                created_at=self.completion.release_result_at,
            )
            self._replace_finished_result(context, historical)

            newer_context = {
                **context,
                "settlement": context["newerSettlement"],
            }
            successor = self.completion._install_contact_lineage(
                newer_context,
                clear_head,
                materialize_head=True,
                claimed_at="2026-08-04T12:06:21.000000Z",
                settled_at="2026-08-04T12:06:22.000000Z",
            )
            self.assertEqual(
                clear_head["headHash"],
                successor["generation"]["predecessorHeadHash"],
            )
            self.assertEqual(
                context["newerSettlement"]["contactSettlementHash"],
                successor["claim"]["payloadHash"],
            )
            self.assertEqual(
                context["newerReceipt"]["resultingFanoutId"],
                successor["claim"]["fanoutId"],
            )
            self.assertEqual(
                successor["settlement"]["settlementHash"],
                successor["settledHead"]["effectiveSettlementHash"],
            )
            self.assertNotEqual(
                historical["observedRowHeadHash"],
                successor["settledHead"]["headHash"],
            )
            try:
                completed = self._supersede(context)
            except self.module.RowAuthorityError as exc:
                self.fail(
                    "historical not-applied evidence with an exact newer "
                    f"authorized successor was rejected: {exc}"
                )
            self._assert_result(completed, "supersession_complete")

        with self.subTest(
            case="historical-contact-successor-rejects-crossed-authority-link"
        ):
            context = self._seed_superseding(
                1,
                finished_indexes=(0,),
                outcome="release",
                finished_lifecycle="active",
            )
            obligation = context["obligations"][0]
            clear_head = context["rowHeads"][obligation["rowId"]]
            historical = self.completion._build_result(
                context,
                obligation,
                clear_head,
                disposition="noop",
                reason_code="row_optout_not_applied",
                created_at=self.completion.release_result_at,
            )
            self._replace_finished_result(context, historical)

            _bundle, crossed_link = context["transition"]._seed_bundle(
                context["store"],
                "historical-contact-successor-crossed-link",
            )
            self.assertEqual(
                context["canonicalHash"],
                crossed_link["canonicalMailboxIdentityHash"],
            )
            self.assertNotEqual(
                context["newerSettlement"]["authorityLinkHash"],
                crossed_link["authorityLinkHash"],
            )
            newer_context = {
                **context,
                "settlement": context["newerSettlement"],
            }
            successor = self.completion._install_contact_lineage(
                newer_context,
                clear_head,
                materialize_head=True,
                authority_link=crossed_link,
                claimed_at="2026-08-04T12:06:21.000000Z",
                settled_at="2026-08-04T12:06:22.000000Z",
            )
            self.assertEqual(
                context["newerSettlement"]["contactSettlementHash"],
                successor["claim"]["payloadHash"],
            )
            self.assertEqual(
                context["newerReceipt"]["resultingFanoutId"],
                successor["claim"]["fanoutId"],
            )
            self.assertNotEqual(
                context["newerSettlement"]["authorityLinkHash"],
                successor["claim"]["authorityLinkHash"],
            )
            self._assert_failure_without_write(
                context,
                lambda: self._supersede(context),
            )

        with self.subTest(
            case="historical-successor-rejects-future-contact-authority"
        ):
            context = self._seed_superseding(
                1,
                finished_indexes=(0,),
                outcome="release",
                finished_lifecycle="active",
            )
            obligation = context["obligations"][0]
            clear_head = context["rowHeads"][obligation["rowId"]]
            historical = self.completion._build_result(
                context,
                obligation,
                clear_head,
                disposition="noop",
                reason_code="row_optout_not_applied",
                created_at="2026-08-04T12:06:04.000000Z",
            )
            self._replace_finished_result(context, historical)

            newer_context = {
                **context,
                "settlement": context["newerSettlement"],
            }
            successor = self.completion._install_contact_lineage(
                newer_context,
                clear_head,
                materialize_head=True,
                claimed_at="2026-08-04T12:06:04.100000Z",
                settled_at="2026-08-04T12:06:04.200000Z",
            )
            self.assertLess(
                successor["claim"]["createdAt"],
                context["newerReceipt"]["requestedAt"],
            )
            self.assertLess(
                successor["settlement"]["settledAt"],
                context["newerSettlement"]["settledAt"],
            )
            self._assert_failure_without_write(
                context,
                lambda: self._supersede(context),
            )

        with self.subTest(
            case=(
                "historical-not-applied-allows-independent-direct-"
                "successor-before-newer-contact-receipt"
            )
        ):
            context = self._seed_superseding(
                1,
                finished_indexes=(0,),
                outcome="release",
                finished_lifecycle="active",
            )
            obligation = context["obligations"][0]
            clear_head = context["rowHeads"][obligation["rowId"]]
            historical = self.completion._build_result(
                context,
                obligation,
                clear_head,
                disposition="noop",
                reason_code="row_optout_not_applied",
                created_at="2026-08-04T12:06:04.000000Z",
            )
            self._replace_finished_result(context, historical)

            fixture = context["transition"].fixture
            original_claimed_at = fixture.claimed_at
            original_lease_until = fixture.lease_until
            try:
                fixture.claimed_at = "2026-08-04T12:06:04.100000Z"
                fixture.lease_until = "2026-08-04T12:09:00.000000Z"
                claim, generation, claimed_head = fixture._install_owner(
                    context["store"],
                    obligation["rowId"],
                    owner_kind="terminal",
                )
                settlement, settled_head = fixture._settle_terminal_owner(
                    context["store"],
                    claim,
                    generation,
                    claimed_head,
                    settled_at="2026-08-04T12:06:04.200000Z",
                )
            finally:
                fixture.claimed_at = original_claimed_at
                fixture.lease_until = original_lease_until
            context["store"].events.clear()
            self.assertEqual(
                clear_head["headHash"],
                generation["predecessorHeadHash"],
            )
            self.assertEqual("b1_source", claim["authorityOrigin"])
            self.assertEqual("terminal", settlement["outcome"])
            self.assertLess(
                settled_head["updatedAt"],
                context["newerReceipt"]["requestedAt"],
            )
            self._assert_result(
                self._supersede(context),
                "supersession_complete",
            )

    def test_superseding_pages_only_discovered_unfinished_obligations(self):
        self._method()
        for count in (0, 1, 128, 129):
            with self.subTest(count=count):
                finished_indexes = (0, 64) if count == 129 else ()
                context = self._seed_superseding(
                    count,
                    finished_indexes=finished_indexes,
                )
                expected = context["fanout"]
                first = self._supersede(context)
                first_count = min(count, 128)
                disposition = (
                    "page_superseded"
                    if count == 129
                    else "supersession_complete"
                )
                self._assert_result(first, disposition)
                self.assertEqual(
                    "superseding" if count == 129 else "superseded",
                    first["fanoutHead"]["state"],
                )
                self.assertEqual(
                    _row_id(128) if count == 129 else None,
                    first["fanoutHead"]["discoveryCursorRowId"],
                )
                self.assertEqual(
                    128 if count == 129 else 0,
                    first["fanoutHead"]["cursorProcessedCount"],
                )
                self.assertEqual(
                    [_row_id(index) for index in range(1, first_count + 1)],
                    [document["rowId"] for document in first["results"]],
                )
                new_results = self._assert_new_superseded_results(
                    context,
                    first,
                )
                created = len(new_results)
                expected_first = self._expected_head(
                    expected,
                    added=created,
                    cursor=(_row_id(128) if count == 129 else None),
                    terminal=(count != 129),
                )
                self.assertEqual(expected_first, first["fanoutHead"])
                self.assertEqual(created + 1, len(self._writes(context["store"])))
                self.assertLessEqual(len(self._writes(context["store"])), 129)
                self._assert_new_result_writes(context, new_results)
                if count == 129:
                    self.assertEqual(
                        expected["leaseOwnerHash"],
                        first["fanoutHead"]["leaseOwnerHash"],
                    )
                    self.assertEqual(
                        expected["leaseUntil"],
                        first["fanoutHead"]["leaseUntil"],
                    )
                self.assertEqual(
                    expected["fencingToken"],
                    first["fanoutHead"]["fencingToken"],
                )

                context["store"].events.clear()
                retry = self._supersede(context, expected)
                self.assertEqual(first, retry)
                self.assertEqual([], self._writes(context["store"]))

                if count == 129:
                    context["store"].events.clear()
                    final = self._supersede(context, first["fanoutHead"])
                    self._assert_result(final, "supersession_complete")
                    self.assertEqual(
                        [_row_id(129)],
                        [r["rowId"] for r in final["results"]],
                    )
                    self.assertEqual(129, final["fanoutHead"]["resultCount"])
                    self.assertEqual(
                        0,
                        final["fanoutHead"]["cursorProcessedCount"],
                    )
                    final_new = self._assert_new_superseded_results(
                        context,
                        final,
                    )
                    expected_final = self._expected_head(
                        first["fanoutHead"],
                        added=len(final_new),
                        cursor=None,
                        terminal=True,
                    )
                    self.assertEqual(expected_final, final["fanoutHead"])
                    self._assert_new_result_writes(context, final_new)
                    context["store"].events.clear()
                    self.assertEqual(
                        final,
                        self._supersede(context, first["fanoutHead"]),
                    )
                    self.assertEqual([], self._writes(context["store"]))

    def test_superseding_never_discovers_edges_or_mutates_rows(self):
        self._method()
        context = self._seed_superseding(3, finished_indexes=(1,))
        immutable_prefixes = (
            "/contactRowBindings/",
            "/contactRowBindingHeads/",
            "/contactOptOutFanoutObligations/",
            "/rowIdentities/",
            "/rowAuthorityHeads/",
        )
        immutable_before = {
            path: deepcopy(payload)
            for path, payload in context["store"].data.items()
            if any(marker in path for marker in immutable_prefixes)
        }
        observed = []
        original = BoundedFakeTransaction.get_query

        def inspect_query(transaction, query):
            observed.append(
                (
                    query._collection.path,
                    query._filters,
                    query._ordering,
                    query._directions,
                    query._limit_count,
                    query._start_after_values,
                    query._start_after_path,
                )
            )
            return original(transaction, query)

        with patch.object(BoundedFakeTransaction, "get_query", new=inspect_query):
            result = self._supersede(context)
        self._assert_result(result, "supersession_complete")
        obligation_queries = [
            item
            for item in observed
            if item[0].endswith("/contactOptOutFanoutObligations")
        ]
        self.assertEqual(
            [
                (
                    self.discovery._user(context)
                    .collection("contactOptOutFanoutObligations")
                    .path,
                    (("fanoutId", "==", context["fanout"]["fanoutId"]),),
                    ("rowId",),
                    ("ASCENDING",),
                    129,
                    None,
                    None,
                )
            ],
            obligation_queries,
        )
        settlement_queries = [
            item
            for item in observed
            if item[0].endswith("/contactOptOutSettlements")
        ]
        self.assertEqual(
            [
                (
                    self.discovery._user(context)
                    .collection("contactOptOutSettlements")
                    .path,
                    (
                        (
                            "contactSettlementHash",
                            "==",
                            context["fanout"][
                                "supersedingContactSettlementHash"
                            ],
                        ),
                    ),
                    ("__name__",),
                    ("ASCENDING",),
                    2,
                    None,
                    None,
                )
            ],
            settlement_queries,
        )
        self.assertEqual(2, len(observed))
        self.assertFalse(
            any(path.endswith("/contactRowBindings") for path, *_rest in observed)
        )
        immutable_after = {
            path: deepcopy(payload)
            for path, payload in context["store"].data.items()
            if any(marker in path for marker in immutable_prefixes)
        }
        self.assertEqual(immutable_before, immutable_after)
        self.assertTrue(
            all(
                "/contactOptOutFanoutResults/" in event[1]
                or "/contactOptOutFanoutHeads/" in event[1]
                for event in self._writes(context["store"])
            )
        )

        for case in (
            "owner",
            "deadline",
            "expired",
            "obligation",
            "row-head",
            "result",
        ):
            with self.subTest(rejected=case):
                rejected = self._seed_superseding(
                    1,
                    finished_indexes=((0,) if case == "result" else ()),
                )
                operation = lambda: self._supersede(rejected)
                if case == "owner":
                    operation = lambda: self._supersede(
                        rejected, owner="f" * 64
                    )
                elif case == "deadline":
                    operation = lambda: self._supersede(
                        rejected, superseded_at=self.lease_until
                    )
                elif case == "expired":
                    operation = lambda: self._supersede(
                        rejected,
                        superseded_at="2026-08-04T12:12:00.000001Z",
                    )
                elif case == "obligation":
                    obligation = rejected["obligations"][0]
                    reference = self._reference(
                        rejected,
                        "contactOptOutFanoutObligations",
                        f"{rejected['fanout']['fanoutId']}--{obligation['rowId']}",
                    )
                    rejected["store"].data[reference.path][
                        "contactFanoutObligationHash"
                    ] = "0" * 64
                elif case == "row-head":
                    reference = self._row_head_reference(rejected, _row_id(1))
                    rejected["store"].data[reference.path]["unknown"] = None
                else:
                    reference = self._result_reference(rejected, _row_id(1))
                    rejected["store"].data[reference.path][
                        "contactFanoutResultHash"
                    ] = "0" * 64
                self._assert_failure_without_write(rejected, operation)

        with self.subTest(rejected="unleased"):
            rejected = self._seed_superseding(1)
            unleased = self.discovery._fanout(
                rejected["fanout"],
                state_revision=rejected["fanout"]["stateRevision"] + 1,
                lease_owner_hash=None,
                lease_until=None,
                updated_at="2026-08-04T12:06:20.000000Z",
            )
            self.discovery._store_fanout(rejected, unleased)
            self._assert_failure_without_write(
                rejected,
                lambda: self._supersede(rejected),
            )

        with self.subTest(rejected="wrong-input-state"):
            rejected = self._seed_superseding(1)
            applying = self.discovery._fanout(
                rejected["fanout"],
                state_revision=rejected["fanout"]["stateRevision"] + 1,
                state="applying",
                superseding_contact_settlement_hash=None,
                updated_at="2026-08-04T12:06:20.000000Z",
            )
            self.discovery._store_fanout(rejected, applying)
            self._assert_failure_without_write(
                rejected,
                lambda: self._supersede(rejected),
            )

        with self.subTest(rejected="terminal-input-state"):
            rejected = self._seed_superseding(0)
            terminal = self._expected_head(
                rejected["fanout"],
                added=0,
                cursor=None,
                terminal=True,
            )
            self.discovery._store_fanout(rejected, terminal)
            self._assert_failure_without_write(
                rejected,
                lambda: self._supersede(rejected),
            )

        with self.subTest(rejected="stale-fence"):
            rejected = self._seed_superseding(1)
            stale = rejected["fanout"]
            renewed = rejected["transition"]._authority(
                rejected["store"]
            ).acquire_contact_fanout_lease(
                verified_user_id=rejected["transition"].fixture.user_id,
                fanout_id=stale["fanoutId"],
                expected_fanout_head=stale,
                lease_owner_hash=self.lease_owner,
                lease_until="2026-08-04T12:13:00.000000Z",
                acquired_at="2026-08-04T12:06:20.000000Z",
            )["fanoutHead"]
            rejected["fanout"] = renewed
            self._assert_failure_without_write(
                rejected,
                lambda: self._supersede(rejected, stale),
            )

    def test_exact_exhaustion_creates_linked_terminal_superseded_head(self):
        self._method()
        context = self._seed_superseding(1)
        before = context["fanout"]
        result = self._supersede(context)
        self._assert_result(result, "supersession_complete")
        terminal = result["fanoutHead"]
        new_results = self._assert_new_superseded_results(context, result)
        self.assertEqual(
            self._expected_head(
                before,
                added=len(new_results),
                cursor=None,
                terminal=True,
            ),
            terminal,
        )
        self._assert_new_result_writes(context, new_results)
        self.assertEqual("superseded", terminal["state"])
        self.assertEqual(before["stateRevision"] + 1, terminal["stateRevision"])
        self.assertEqual(before["obligationCount"], terminal["resultCount"])
        self.assertEqual(
            context["newerSettlement"]["contactSettlementHash"],
            terminal["supersedingContactSettlementHash"],
        )
        self.assertIsNone(terminal["leaseOwnerHash"])
        self.assertIsNone(terminal["leaseUntil"])
        self.assertIsNone(terminal["discoveryCursorRowId"])
        self.assertEqual(0, terminal["cursorProcessedCount"])
        self.assertEqual(before["fencingToken"], terminal["fencingToken"])
        self.assertEqual(self.superseded_at, terminal["updatedAt"])
        self.assertEqual(
            context["newerSettlement"]["contactSettlementHash"],
            context["currentContactHead"]["latestSettlementHash"],
        )
        self.assertEqual(
            context["newerSettlement"]["contactTransitionId"],
            context["newerReceipt"]["contactTransitionId"],
        )

        for artifact, collection, document_id in (
            (
                "settlement",
                "contactOptOutSettlements",
                f"{context['canonicalHash']}--{context['newerSettlement']['generation']}",
            ),
            (
                "receipt",
                "contactOptOutTransitionRequests",
                context["newerSettlement"]["contactTransitionId"],
            ),
            ("head", "contactOptOutHeads", context["canonicalHash"]),
        ):
            with self.subTest(missing=artifact):
                rejected = self._seed_superseding(1)
                reference = self._reference(rejected, collection, document_id)
                rejected["store"].data.pop(reference.path)
                self._assert_failure_without_write(
                    rejected,
                    lambda: self._supersede(rejected),
                )

        authority_cases = (
            (
                "settlement",
                "contactOptOutSettlements",
                lambda item: (
                    f"{item['canonicalHash']}--"
                    f"{item['newerSettlement']['generation']}"
                ),
                "newerSettlement",
            ),
            (
                "receipt",
                "contactOptOutTransitionRequests",
                lambda item: item["newerSettlement"]["contactTransitionId"],
                "newerReceipt",
            ),
            (
                "head",
                "contactOptOutHeads",
                lambda item: item["canonicalHash"],
                "currentContactHead",
            ),
        )
        for artifact, collection, document_id, context_key in authority_cases:
            with self.subTest(malformed=artifact):
                rejected = self._seed_superseding(1)
                reference = self._reference(
                    rejected,
                    collection,
                    document_id(rejected),
                )
                rejected["store"].data[reference.path]["unknown"] = None
                self._assert_failure_without_write(
                    rejected,
                    lambda: self._supersede(rejected),
                )

            with self.subTest(crossed=artifact):
                rejected = self._seed_superseding(1)
                foreign = self._seed_superseding(
                    1,
                    source_id=f"source-crossed-supersession-{artifact}",
                )
                reference = self._reference(
                    rejected,
                    collection,
                    document_id(rejected),
                )
                rejected["store"].data[reference.path] = deepcopy(
                    foreign[context_key]
                )
                self._assert_failure_without_write(
                    rejected,
                    lambda: self._supersede(rejected),
                )

        with self.subTest(advanced="newer-authority"):
            rejected = self._seed_superseding(1)
            bundle, _link = rejected["transition"]._seed_bundle(
                rejected["store"],
                "source-after-superseding",
            )
            rejected["transition"]._record(
                rejected["store"],
                bundle,
                requested_at="2026-08-04T12:06:20.000000Z",
            )
            self._assert_failure_without_write(
                rejected,
                lambda: self._supersede(rejected),
            )

        with self.subTest(commit="apply-then-raise"):
            uncertain = self._seed_superseding(1)
            uncertain["store"].apply_then_raise_next_commit = RuntimeError(
                "unknown supersession commit"
            )
            applied = self._supersede(uncertain)
            self._assert_result(applied, "supersession_complete")
            self.assertIn(("commit_raised_after_apply",), uncertain["store"].events)

        with self.subTest(commit="partial-readback"):
            uncertain = self._seed_superseding(1)

            def partial_executor(transaction, callback):
                transaction._begin()
                callback(transaction)
                operation, reference, payload, merge = transaction._operations[0]
                transaction._rollback()
                self.assertEqual(("create", False), (operation, merge))
                reference.create(payload)
                raise RuntimeError("partial supersession apply")

            with self.assertRaises(self.module.RowAuthorityAmbiguous):
                self._supersede(uncertain, executor=partial_executor)

        with self.subTest(commit="same-page-race"):
            raced = self._seed_superseding(1)
            raced["store"].before_commit_barrier = Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self._supersede, raced) for _ in range(2)]
                results = [future.result(timeout=10) for future in futures]
            self.assertEqual(results[0], results[1])
            self.assertEqual(1, raced["store"].events.count(("commit_applied", 2)))
            self.assertEqual(1, raced["store"].events.count(("commit_applied", 0)))


if __name__ == "__main__":
    unittest.main()
