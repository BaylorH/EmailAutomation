"""Focused provider-free contracts for B2-C contact compliance."""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from unittest.mock import patch

from tests.row_authority_fakes import (
    BoundedFakeFirestore,
    run_bounded_transaction,
)


class StringSubclass(str):
    pass


class ContactQueryFakeTests(unittest.TestCase):
    def setUp(self):
        self.store = BoundedFakeFirestore()
        self.items = self.store.collection("orderedItems")

    def _create(self, document_id, **payload):
        self.items.document(document_id).create(payload)

    def _ids(self, query):
        return [snapshot.id for snapshot in query.stream()]

    def _order_by(self, query, field_path, direction):
        try:
            return query.order_by(field_path, direction=direction)
        except TypeError as exc:
            self.fail(
                "fake order_by must accept the approved direction argument: "
                f"{exc}"
            )

    def _start_after(self, query, cursor):
        try:
            return query.start_after(cursor)
        except (TypeError, ValueError) as exc:
            self.fail(
                "fake start_after must accept the approved field-value cursor: "
                f"{exc}"
            )

    def test_order_by_direction_defaults_ascending_and_supports_descending(self):
        self._create("path-a", generation=2)
        self._create("path-z", generation=1)
        self._create("path-m", generation=3)

        self.assertEqual(
            ["path-z", "path-a", "path-m"],
            self._ids(self.items.order_by("generation")),
        )
        self.assertEqual(
            ["path-z", "path-a", "path-m"],
            self._ids(
                self._order_by(
                    self.items,
                    "generation",
                    "ASCENDING",
                )
            ),
        )
        self.assertEqual(
            ["path-m", "path-a", "path-z"],
            self._ids(
                self._order_by(
                    self.items,
                    "generation",
                    "DESCENDING",
                )
            ),
        )

    def test_order_by_excludes_documents_missing_the_ordered_field(self):
        self._create("matching-null", kind="target", generation=None)
        self._create("matching-missing", kind="target")
        self._create("other-missing", kind="other")

        query = self.items.where("kind", "==", "target").order_by("generation")

        try:
            observed = self._ids(query)
        except ValueError as exc:
            self.fail(
                "ordered-field absence must exclude the document, not fail the "
                f"query: {exc}"
            )
        self.assertEqual(["matching-null"], observed)

    def test_start_after_uses_ordered_field_tuple_not_document_path(self):
        self._create("path-z", generation=1, rowId="row-010")
        self._create("path-a", generation=1, rowId="row-020")
        self._create("path-m", generation=2, rowId="row-001")
        query = self.items.order_by("generation").order_by("rowId")

        expected = ["path-a", "path-m"]
        cursors = (
            {"rowId": "row-010", "generation": 1},
            [1, "row-010"],
            (1, "row-010"),
        )
        for cursor in cursors:
            with self.subTest(cursor=cursor):
                self.assertEqual(
                    expected,
                    self._ids(self._start_after(query, cursor)),
                )

        prefix_cursors = (
            {"generation": 1},
            [1],
            (1,),
        )
        for cursor in prefix_cursors:
            with self.subTest(prefix_cursor=cursor):
                self.assertEqual(
                    ["path-m"],
                    self._ids(self._start_after(query, cursor)),
                )

        name_query = self.items.order_by("__name__")
        name_reference = self.items.document("path-m")
        for cursor in (
            {"__name__": "path-m"},
            ["path-m"],
            ("path-m",),
            {"__name__": name_reference.path},
            (name_reference,),
        ):
            with self.subTest(name_cursor=cursor):
                self.assertEqual(
                    ["path-z"],
                    self._ids(self._start_after(name_query, cursor)),
                )

    def test_order_ties_use_document_path_and_cursor_is_exclusive(self):
        self._create("path-a", generation=7)
        self._create("path-m", generation=7)
        self._create("path-z", generation=7)
        query = self._order_by(
            self.items,
            "generation",
            "DESCENDING",
        )

        first_page = list(query.limit(1).stream())

        self.assertEqual(["path-z"], [item.id for item in first_page])
        self.assertEqual(
            ["path-m", "path-a"],
            self._ids(query.start_after(first_page[-1])),
        )

    def test_query_phantom_retries_transaction(self):
        self._create("path-z", kind="target", generation=2)
        self._create("path-a", kind="target", generation=1)
        attempts = []

        def read_page(transaction):
            query = self.items.where("kind", "==", "target")
            query = self._order_by(query, "generation", "DESCENDING")
            query = self._start_after(query, {"generation": 3})
            result = tuple(snapshot.id for snapshot in transaction.get(query))
            attempts.append(result)
            return result

        self.store.before_next_commit_hook = lambda: self.items.document(
            "path-m"
        ).create({"kind": "target", "generation": 0})
        self.store.events.clear()

        result = run_bounded_transaction(
            self.store.transaction(max_attempts=2),
            read_page,
        )

        self.assertEqual(
            ("path-z", "path-a", "path-m"),
            result,
        )
        self.assertEqual(2, len(attempts))
        self.assertIn(
            (
                "commit_aborted_stale_query",
                self.items.path,
            ),
            self.store.events,
        )
        self.assertIn(("transaction_began", 0), self.store.events)
        self.assertIn(("transaction_began", 1), self.store.events)

    def test_invalid_direction_or_cursor_shape_fails_before_writes(self):
        self._create("path-a", generation=1, rowId="row-001")
        before = deepcopy(self.store.data)
        self.store.events.clear()

        try:
            self.items.order_by("generation", direction="SIDEWAYS")
        except ValueError:
            pass
        except TypeError as exc:
            self.fail(
                "fake order_by must validate the direction value after accepting "
                f"the approved argument: {exc}"
            )
        else:
            self.fail("fake order_by accepted an invalid direction")

        query = self._order_by(self.items, "generation", "ASCENDING")
        invalid_cursors = (
            {},
            {"rowId": "row-001"},
            [],
            [1, "extra"],
            object(),
        )
        for cursor in invalid_cursors:
            with self.subTest(cursor=cursor), self.assertRaises(
                (TypeError, ValueError)
            ):
                query.start_after(cursor)

        foreign_store = BoundedFakeFirestore()
        foreign_reference = foreign_store.collection("orderedItems").document(
            "path-a"
        )
        foreign_reference.create({"generation": 1})
        with self.assertRaises((TypeError, ValueError)):
            query.start_after(foreign_reference.get())

        self.assertEqual(before, self.store.data)
        self.assertEqual(
            [],
            [
                event
                for event in self.store.events
                if event[0] in {"create", "set", "update", "delete"}
            ],
        )


class ContactContractTests(unittest.TestCase):
    CONTACT_DOMAINS = {
        "CONTACT_ALIAS_HASH_DOMAIN": "sitesift.contact.optout_alias.v1",
        "CONTACT_TRANSITION_ID_DOMAIN": (
            "sitesift.contact.optout_transition_id.v1"
        ),
        "CONTACT_TRANSITION_REQUEST_HASH_DOMAIN": (
            "sitesift.contact.optout_transition_request.v1"
        ),
        "CONTACT_SETTLEMENT_HASH_DOMAIN": (
            "sitesift.contact.optout_settlement.v1"
        ),
        "CONTACT_HEAD_HASH_DOMAIN": "sitesift.contact.optout_head.v1",
        "CONTACT_ROW_EDGE_ID_DOMAIN": "sitesift.contact.row_edge_id.v1",
        "CONTACT_ROW_EDGE_HASH_DOMAIN": "sitesift.contact.row_edge.v1",
        "CONTACT_ROW_EVIDENCE_ID_DOMAIN": (
            "sitesift.contact.row_evidence_id.v1"
        ),
        "CONTACT_ROW_EVIDENCE_HASH_DOMAIN": (
            "sitesift.contact.row_evidence.v1"
        ),
        "CONTACT_ROW_BINDING_HEAD_HASH_DOMAIN": (
            "sitesift.contact.row_binding_head.v1"
        ),
        "CONTACT_FANOUT_ID_DOMAIN": "sitesift.contact.optout_fanout_id.v1",
        "CONTACT_FANOUT_HEAD_HASH_DOMAIN": (
            "sitesift.contact.optout_fanout_head.v1"
        ),
        "CONTACT_FANOUT_OBLIGATION_HASH_DOMAIN": (
            "sitesift.contact.optout_fanout_obligation.v1"
        ),
        "CONTACT_FANOUT_RESULT_HASH_DOMAIN": (
            "sitesift.contact.optout_fanout_result.v1"
        ),
    }
    CONTACT_CONTRACTS = (
        "build_contact_alias_document",
        "validate_contact_alias_document",
        "build_contact_transition_request_document",
        "validate_contact_transition_request_document",
        "build_contact_settlement_document",
        "validate_contact_settlement_document",
        "build_contact_head_document",
        "validate_contact_head_document",
        "build_contact_fanout_head_document",
        "validate_contact_fanout_head_document",
        "build_contact_fanout_obligation_document",
        "validate_contact_fanout_obligation_document",
        "build_contact_fanout_result_document",
        "validate_contact_fanout_result_document",
    )
    RECORD_KEYS = {
        "alias": frozenset(
            {
                "schemaVersion",
                "userScopeHash",
                "exactIdentityHash",
                "canonicalMailboxIdentityHash",
                "contactAliasHash",
                "createdAt",
            }
        ),
        "transition": frozenset(
            {
                "schemaVersion",
                "userScopeHash",
                "contactTransitionId",
                "transitionKind",
                "exactIdentityHash",
                "canonicalMailboxIdentityHash",
                "authorityLinkHash",
                "hardOptOutEvidenceHash",
                "actorScopeHash",
                "clientRequestHash",
                "expectedActiveOptOutSettlementHash",
                "reasonCode",
                "outcome",
                "resultingContactGeneration",
                "resultingContactSettlementHash",
                "resultingFanoutId",
                "resultingContactHeadHash",
                "resultingFanoutHeadHash",
                "requestedAt",
                "contactTransitionRequestHash",
            }
        ),
        "settlement": frozenset(
            {
                "schemaVersion",
                "userScopeHash",
                "canonicalMailboxIdentityHash",
                "generation",
                "predecessorSettlementHash",
                "transitionKind",
                "contactTransitionId",
                "exactIdentityHash",
                "authorityLink",
                "authorityLinkHash",
                "hardOptOutEvidenceHash",
                "actorScopeHash",
                "reasonCode",
                "contactSettlementHash",
                "settledAt",
            }
        ),
        "head": frozenset(
            {
                "schemaVersion",
                "userScopeHash",
                "canonicalMailboxIdentityHash",
                "stateRevision",
                "latestGeneration",
                "latestSettlementHash",
                "activeOptOutSettlementHash",
                "state",
                "activeFanoutId",
                "contactHeadHash",
                "createdAt",
                "updatedAt",
            }
        ),
        "fanout_head": frozenset(
            {
                "schemaVersion",
                "userScopeHash",
                "fanoutId",
                "outcome",
                "expectedContactSettlementHash",
                "stateRevision",
                "state",
                "bindingRevision",
                "bindingHeadHash",
                "bindingAssociationCount",
                "discoveryCursorRowId",
                "cursorProcessedCount",
                "obligationCount",
                "resultCount",
                "leaseOwnerHash",
                "leaseUntil",
                "fencingToken",
                "supersedingContactSettlementHash",
                "completionBindingRevision",
                "completionBindingHeadHash",
                "completionBindingAssociationCount",
                "completionObligationCount",
                "completionResultCount",
                "completedAt",
                "contactFanoutHeadHash",
                "createdAt",
                "updatedAt",
            }
        ),
        "obligation": frozenset(
            {
                "schemaVersion",
                "userScopeHash",
                "fanoutId",
                "rowId",
                "contactRowEdgeHash",
                "expectedContactSettlementHash",
                "outcome",
                "contactFanoutObligationHash",
                "createdAt",
            }
        ),
        "result": frozenset(
            {
                "schemaVersion",
                "userScopeHash",
                "fanoutId",
                "rowId",
                "obligationHash",
                "outcome",
                "disposition",
                "reasonCode",
                "observedRowHeadHash",
                "claimRequestId",
                "claimSetHash",
                "rowGeneration",
                "rowSettlementHash",
                "releasedRowGeneration",
                "releasedRowSettlementHash",
                "restoredEffectiveGeneration",
                "restoredEffectiveSettlementHash",
                "contactFanoutResultHash",
                "createdAt",
            }
        ),
    }
    HASH_DOMAINS = {
        "alias": "sitesift.contact.optout_alias.v1",
        "transition": "sitesift.contact.optout_transition_request.v1",
        "settlement": "sitesift.contact.optout_settlement.v1",
        "head": "sitesift.contact.optout_head.v1",
        "fanout_head": "sitesift.contact.optout_fanout_head.v1",
        "obligation": "sitesift.contact.optout_fanout_obligation.v1",
        "result": "sitesift.contact.optout_fanout_result.v1",
    }
    HASH_OUTPUT_FIELDS = {
        "alias": "contactAliasHash",
        "transition": "contactTransitionRequestHash",
        "settlement": "contactSettlementHash",
        "head": "contactHeadHash",
        "fanout_head": "contactFanoutHeadHash",
        "obligation": "contactFanoutObligationHash",
        "result": "contactFanoutResultHash",
    }
    HASH_PAYLOAD_FIELDS = {
        "alias": (
            "exactIdentityHash",
            "canonicalMailboxIdentityHash",
            "createdAt",
        ),
        "transition": (
            "contactTransitionId",
            "transitionKind",
            "exactIdentityHash",
            "canonicalMailboxIdentityHash",
            "authorityLinkHash",
            "hardOptOutEvidenceHash",
            "actorScopeHash",
            "clientRequestHash",
            "expectedActiveOptOutSettlementHash",
            "reasonCode",
            "outcome",
            "resultingContactGeneration",
            "resultingContactSettlementHash",
            "resultingFanoutId",
            "resultingContactHeadHash",
            "resultingFanoutHeadHash",
            "requestedAt",
        ),
        "settlement": (
            "canonicalMailboxIdentityHash",
            "generation",
            "predecessorSettlementHash",
            "transitionKind",
            "contactTransitionId",
            "exactIdentityHash",
            "authorityLinkHash",
            "hardOptOutEvidenceHash",
            "actorScopeHash",
            "reasonCode",
            "settledAt",
        ),
        "head": (
            "canonicalMailboxIdentityHash",
            "stateRevision",
            "latestGeneration",
            "latestSettlementHash",
            "activeOptOutSettlementHash",
            "state",
            "activeFanoutId",
            "createdAt",
            "updatedAt",
        ),
        "fanout_head": (
            "fanoutId",
            "outcome",
            "expectedContactSettlementHash",
            "stateRevision",
            "state",
            "bindingRevision",
            "bindingHeadHash",
            "bindingAssociationCount",
            "discoveryCursorRowId",
            "cursorProcessedCount",
            "obligationCount",
            "resultCount",
            "leaseOwnerHash",
            "leaseUntil",
            "fencingToken",
            "supersedingContactSettlementHash",
            "completionBindingRevision",
            "completionBindingHeadHash",
            "completionBindingAssociationCount",
            "completionObligationCount",
            "completionResultCount",
            "completedAt",
            "createdAt",
            "updatedAt",
        ),
        "obligation": (
            "fanoutId",
            "rowId",
            "contactRowEdgeHash",
            "expectedContactSettlementHash",
            "outcome",
        ),
        "result": (
            "fanoutId",
            "rowId",
            "obligationHash",
            "outcome",
            "disposition",
            "reasonCode",
            "observedRowHeadHash",
            "claimRequestId",
            "claimSetHash",
            "rowGeneration",
            "rowSettlementHash",
            "releasedRowGeneration",
            "releasedRowSettlementHash",
            "restoredEffectiveGeneration",
            "restoredEffectiveSettlementHash",
            "createdAt",
        ),
    }
    TRANSITION_ID_FIELDS = (
        "transitionKind",
        "exactIdentityHash",
        "canonicalMailboxIdentityHash",
        "authorityLinkHash",
        "hardOptOutEvidenceHash",
        "actorScopeHash",
        "clientRequestHash",
        "expectedActiveOptOutSettlementHash",
        "reasonCode",
    )

    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")

    def setUp(self):
        self.scope = "a" * 64
        self.other_scope = "0" * 64
        self.exact_hash = "b" * 64
        self.canonical_hash = "c" * 64
        self.actor_hash = "d" * 64
        self.client_request_hash = "e" * 64
        self.hard_evidence_hash = "8" * 64
        self.row_id = "sr1_00000000000140018000000000000001"
        self.created_at = "2026-08-04T12:00:00.000000Z"
        self.later_at = "2026-08-04T12:00:01.000000Z"

    def _require_symbols(self, *names):
        missing = [name for name in names if not hasattr(self.module, name)]
        self.assertEqual(
            [],
            missing,
            f"missing B2-C contact contracts: {missing}",
        )

    @staticmethod
    def _independent_hash(domain, payload, *, user_scope_hash):
        material = {
            **deepcopy(payload),
            "schemaVersion": 1,
            "userScopeHash": user_scope_hash,
        }
        canonical = json.dumps(
            material,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(
            domain.encode("utf-8") + b"\0" + canonical
        ).hexdigest()

    def _v2_link(self, **overrides):
        material = {
            "canonicalSourceId": "source-1",
            "snapshotImmutableHash": "1" * 64,
            "selectionHash": "2" * 64,
            "ownerDecisionHash": "3" * 64,
            "ledgerHash": "4" * 64,
            "ownerKind": "contact_optout",
            "ownerKey": "5" * 64,
            "workKey": "6" * 64,
            "payloadHash": "7" * 64,
            "hardOptOutEvidenceHash": self.hard_evidence_hash,
            "exactIdentityHash": self.exact_hash,
            "canonicalMailboxIdentityHash": self.canonical_hash,
        }
        material.update(overrides)
        return {
            **material,
            "authorityLinkHash": self._independent_hash(
                "sitesift.row.b1_authority_link.v2",
                material,
                user_scope_hash=self.scope,
            ),
        }

    @staticmethod
    def _snake_transition_payload(values):
        return {
            "transitionKind": values["transition_kind"],
            "exactIdentityHash": values["exact_identity_hash"],
            "canonicalMailboxIdentityHash": values[
                "canonical_mailbox_identity_hash"
            ],
            "authorityLinkHash": values["authority_link_hash"],
            "hardOptOutEvidenceHash": values[
                "hard_optout_evidence_hash"
            ],
            "actorScopeHash": values["actor_scope_hash"],
            "clientRequestHash": values["client_request_hash"],
            "expectedActiveOptOutSettlementHash": values[
                "expected_active_optout_settlement_hash"
            ],
            "reasonCode": values["reason_code"],
        }

    def _transition_id(self, values):
        return self._independent_hash(
            "sitesift.contact.optout_transition_id.v1",
            self._snake_transition_payload(values),
            user_scope_hash=values["user_scope_hash"],
        )

    def _verified_transition_values(self, **overrides):
        link = self._v2_link()
        values = {
            "user_scope_hash": self.scope,
            "transition_kind": "verified_optout",
            "exact_identity_hash": self.exact_hash,
            "canonical_mailbox_identity_hash": self.canonical_hash,
            "authority_link_hash": link["authorityLinkHash"],
            "hard_optout_evidence_hash": self.hard_evidence_hash,
            "actor_scope_hash": None,
            "client_request_hash": None,
            "expected_active_optout_settlement_hash": None,
            "reason_code": None,
            "outcome": "created",
            "resulting_contact_generation": 1,
            "resulting_contact_settlement_hash": "1" * 64,
            "resulting_contact_head_hash": "3" * 64,
            "resulting_fanout_head_hash": "4" * 64,
            "requested_at": self.created_at,
        }
        values.update(overrides)
        if "resulting_fanout_id" not in overrides:
            values["resulting_fanout_id"] = self._fanout_id(
                settlement_hash=values[
                    "resulting_contact_settlement_hash"
                ],
                outcome="apply",
            )
        return values

    def _release_transition_values(self, **overrides):
        values = {
            "user_scope_hash": self.scope,
            "transition_kind": "authenticated_release",
            "exact_identity_hash": self.exact_hash,
            "canonical_mailbox_identity_hash": self.canonical_hash,
            "authority_link_hash": None,
            "hard_optout_evidence_hash": None,
            "actor_scope_hash": self.actor_hash,
            "client_request_hash": self.client_request_hash,
            "expected_active_optout_settlement_hash": "1" * 64,
            "reason_code": "authenticated_release",
            "outcome": "created",
            "resulting_contact_generation": 2,
            "resulting_contact_settlement_hash": "4" * 64,
            "resulting_contact_head_hash": "6" * 64,
            "resulting_fanout_head_hash": "7" * 64,
            "requested_at": self.later_at,
        }
        values.update(overrides)
        if "resulting_fanout_id" not in overrides:
            values["resulting_fanout_id"] = self._fanout_id(
                settlement_hash=values[
                    "resulting_contact_settlement_hash"
                ],
                outcome="release",
            )
        return values

    def _build_transition(self, **overrides):
        values = self._verified_transition_values(**overrides)
        return self.module.build_contact_transition_request_document(**values)

    def _build_release_transition(self, **overrides):
        values = self._release_transition_values(**overrides)
        return self.module.build_contact_transition_request_document(**values)

    def _build_alias(self, **overrides):
        values = {
            "user_scope_hash": self.scope,
            "exact_identity_hash": self.exact_hash,
            "canonical_mailbox_identity_hash": self.canonical_hash,
            "created_at": self.created_at,
        }
        values.update(overrides)
        return self.module.build_contact_alias_document(**values)

    def _build_verified_settlement(self, **overrides):
        transition_values = self._verified_transition_values()
        values = {
            "user_scope_hash": self.scope,
            "canonical_mailbox_identity_hash": self.canonical_hash,
            "generation": 1,
            "predecessor_settlement_hash": None,
            "transition_kind": "verified_optout",
            "contact_transition_id": self._transition_id(transition_values),
            "exact_identity_hash": self.exact_hash,
            "authority_link": self._v2_link(),
            "actor_scope_hash": None,
            "reason_code": None,
            "settled_at": self.created_at,
        }
        values.update(overrides)
        return self.module.build_contact_settlement_document(**values)

    def _build_release_settlement(self, predecessor_hash, **overrides):
        transition_values = self._release_transition_values(
            expected_active_optout_settlement_hash=predecessor_hash
        )
        values = {
            "user_scope_hash": self.scope,
            "canonical_mailbox_identity_hash": self.canonical_hash,
            "generation": 2,
            "predecessor_settlement_hash": predecessor_hash,
            "transition_kind": "authenticated_release",
            "contact_transition_id": self._transition_id(transition_values),
            "exact_identity_hash": self.exact_hash,
            "authority_link": None,
            "actor_scope_hash": self.actor_hash,
            "reason_code": "authenticated_release",
            "settled_at": self.later_at,
        }
        values.update(overrides)
        return self.module.build_contact_settlement_document(**values)

    def _fanout_id(self, *, settlement_hash, outcome):
        return self._independent_hash(
            "sitesift.contact.optout_fanout_id.v1",
            {
                "contactSettlementHash": settlement_hash,
                "outcome": outcome,
            },
            user_scope_hash=self.scope,
        )

    def _build_fanout_head(self, **overrides):
        expected_settlement = overrides.pop(
            "expected_contact_settlement_hash",
            "1" * 64,
        )
        outcome = overrides.pop("outcome", "apply")
        values = {
            "user_scope_hash": self.scope,
            "fanout_id": self._fanout_id(
                settlement_hash=expected_settlement,
                outcome=outcome,
            ),
            "outcome": outcome,
            "expected_contact_settlement_hash": expected_settlement,
            "state_revision": 1,
            "state": "discovering",
            "binding_revision": 0,
            "binding_head_hash": None,
            "binding_association_count": 0,
            "discovery_cursor_row_id": None,
            "cursor_processed_count": 0,
            "obligation_count": 0,
            "result_count": 0,
            "lease_owner_hash": None,
            "lease_until": None,
            "fencing_token": 1,
            "superseding_contact_settlement_hash": None,
            "completion_binding_revision": None,
            "completion_binding_head_hash": None,
            "completion_binding_association_count": None,
            "completion_obligation_count": None,
            "completion_result_count": None,
            "completed_at": None,
            "created_at": self.created_at,
            "updated_at": self.created_at,
        }
        values.update(overrides)
        return self.module.build_contact_fanout_head_document(**values)

    def _build_contact_head(self, settlement_hash, fanout_id, **overrides):
        values = {
            "user_scope_hash": self.scope,
            "canonical_mailbox_identity_hash": self.canonical_hash,
            "state_revision": 1,
            "latest_generation": 1,
            "latest_settlement_hash": settlement_hash,
            "active_optout_settlement_hash": settlement_hash,
            "state": "active",
            "active_fanout_id": fanout_id,
            "created_at": self.created_at,
            "updated_at": self.created_at,
        }
        values.update(overrides)
        return self.module.build_contact_head_document(**values)

    def _build_obligation(self, *, settlement_hash, fanout_id, **overrides):
        values = {
            "user_scope_hash": self.scope,
            "fanout_id": fanout_id,
            "row_id": self.row_id,
            "contact_row_edge_hash": "9" * 64,
            "expected_contact_settlement_hash": settlement_hash,
            "outcome": "apply",
            "created_at": self.created_at,
        }
        values.update(overrides)
        return self.module.build_contact_fanout_obligation_document(**values)

    def _build_result(self, **overrides):
        outcome = overrides.pop("outcome", "apply")
        settlement_hash = overrides.pop(
            "expected_contact_settlement_hash",
            "1" * 64,
        )
        values = {
            "user_scope_hash": self.scope,
            "fanout_id": self._fanout_id(
                settlement_hash=settlement_hash,
                outcome=outcome,
            ),
            "row_id": self.row_id,
            "obligation_hash": "f" * 64,
            "outcome": outcome,
            "disposition": "applied",
            "reason_code": "claim_accepted",
            "observed_row_head_hash": "0" * 64,
            "claim_request_id": "1" * 64,
            "claim_set_hash": "2" * 64,
            "row_generation": 1,
            "row_settlement_hash": "3" * 64,
            "released_row_generation": None,
            "released_row_settlement_hash": None,
            "restored_effective_generation": None,
            "restored_effective_settlement_hash": None,
            "created_at": self.later_at,
        }
        values.update(overrides)
        return self.module.build_contact_fanout_result_document(**values)

    def _rehash(self, kind, document, *, domain=None):
        rewritten = deepcopy(document)
        payload = {
            field: deepcopy(rewritten[field])
            for field in self.HASH_PAYLOAD_FIELDS[kind]
        }
        rewritten[self.HASH_OUTPUT_FIELDS[kind]] = self._independent_hash(
            domain or self.HASH_DOMAINS[kind],
            payload,
            user_scope_hash=rewritten["userScopeHash"],
        )
        return rewritten

    def _build_graph(self):
        alias = self._build_alias()
        settlement = self._build_verified_settlement()
        settlement_hash = settlement["contactSettlementHash"]
        fanout_id = self._fanout_id(
            settlement_hash=settlement_hash,
            outcome="apply",
        )
        fanout_head = self._build_fanout_head(
            expected_contact_settlement_hash=settlement_hash,
        )
        contact_head = self._build_contact_head(
            settlement_hash,
            fanout_id,
        )
        obligation = self._build_obligation(
            settlement_hash=settlement_hash,
            fanout_id=fanout_id,
        )
        result = self._build_result(
            fanout_id=fanout_id,
            obligation_hash=obligation["contactFanoutObligationHash"],
        )
        transition = self._build_transition(
            resulting_contact_settlement_hash=settlement_hash,
            resulting_fanout_id=fanout_id,
            resulting_contact_head_hash=contact_head["contactHeadHash"],
            resulting_fanout_head_hash=fanout_head["contactFanoutHeadHash"],
        )
        return {
            "alias": alias,
            "transition": transition,
            "settlement": settlement,
            "head": contact_head,
            "fanout_head": fanout_head,
            "obligation": obligation,
            "result": result,
        }

    def test_transition_id_and_receipt_match_independent_vectors(self):
        self._require_symbols(
            "CONTACT_TRANSITION_ID_DOMAIN",
            "CONTACT_TRANSITION_REQUEST_HASH_DOMAIN",
            "build_contact_transition_request_document",
            "validate_contact_transition_request_document",
        )
        values = self._verified_transition_values()
        receipt = self.module.build_contact_transition_request_document(
            **values
        )
        expected_id = self._transition_id(values)
        self.assertEqual(
            "9271bf511e21e9e2a2da989aa03a4a8e68d424607fe175b297d033b1b09d0153",
            expected_id,
        )
        self.assertEqual(expected_id, receipt["contactTransitionId"])
        expected_receipt_hash = self._independent_hash(
            "sitesift.contact.optout_transition_request.v1",
            {
                field: receipt[field]
                for field in self.HASH_PAYLOAD_FIELDS["transition"]
            },
            user_scope_hash=self.scope,
        )
        self.assertEqual(
            expected_receipt_hash,
            receipt["contactTransitionRequestHash"],
        )
        self.assertEqual(
            self._fanout_id(
                settlement_hash=receipt[
                    "resultingContactSettlementHash"
                ],
                outcome="apply",
            ),
            receipt["resultingFanoutId"],
        )
        self.assertEqual(
            receipt,
            self.module.validate_contact_transition_request_document(
                document=receipt
            ),
        )

        later = self._build_transition(requested_at=self.later_at)
        self.assertEqual(
            receipt["contactTransitionId"],
            later["contactTransitionId"],
        )
        self.assertNotEqual(
            receipt["contactTransitionRequestHash"],
            later["contactTransitionRequestHash"],
        )

        release_values = self._release_transition_values()
        release = self.module.build_contact_transition_request_document(
            **release_values
        )
        self.assertEqual(
            "9b04f32b2ad2c55059d08e2bf527b647f1b672072ed7e4130c1d5dc3e288ba99",
            release["contactTransitionId"],
        )
        self.assertEqual(
            "717443db98a815b0d130fe7b42c4ac13ec8425cea4585057ece3e1438627a964",
            release["contactTransitionRequestHash"],
        )
        self.assertEqual(
            self._fanout_id(
                settlement_hash=release[
                    "resultingContactSettlementHash"
                ],
                outcome="release",
            ),
            release["resultingFanoutId"],
        )

    def test_alias_settlement_head_and_fanout_hashes_match_independent_vectors(self):
        self._require_symbols(*self.CONTACT_CONTRACTS)
        graph = self._build_graph()
        expected_hashes = {
            "alias": (
                "contactAliasHash",
                "ffc19cbe6d3671f4d5b4036b6b7f9822b90d045a2e09ddf5abc6884d0488d236",
            ),
            "transition": (
                "contactTransitionRequestHash",
                "dc25ae1c930b61c513aa35ae35f569b72eeb46819d579a813bc23067fba60073",
            ),
            "settlement": (
                "contactSettlementHash",
                "2e899c291ee173b40ae3a6866634f59ddaf9223937dddbafe8e4516dca210777",
            ),
            "head": (
                "contactHeadHash",
                "0988eab1f96b664fbc4fc496ce2caaef9a1070000f72fbfc2877478d72e7cbdb",
            ),
            "fanout_head": (
                "contactFanoutHeadHash",
                "1832888e4f608c0170cbf5cdf356ca05bb772cc10a6ccad131989f58fd5b468b",
            ),
            "obligation": (
                "contactFanoutObligationHash",
                "735471e73ebe779af632b132625c50e80587c056418db4f36429cfc0a6cf5b93",
            ),
            "result": (
                "contactFanoutResultHash",
                "1027818f2aec359476d013f48ae8a672ee5f4e840c6c4bce6897aed0d225e36f",
            ),
        }
        validators = {
            "alias": self.module.validate_contact_alias_document,
            "transition": (
                self.module.validate_contact_transition_request_document
            ),
            "settlement": self.module.validate_contact_settlement_document,
            "head": self.module.validate_contact_head_document,
            "fanout_head": self.module.validate_contact_fanout_head_document,
            "obligation": (
                self.module.validate_contact_fanout_obligation_document
            ),
            "result": self.module.validate_contact_fanout_result_document,
        }
        for kind, document in graph.items():
            with self.subTest(kind=kind):
                output_field, frozen_digest = expected_hashes[kind]
                self.assertEqual(self.RECORD_KEYS[kind], set(document))
                self.assertEqual(frozen_digest, document[output_field])
                self.assertEqual(document, validators[kind](document=document))

        self.assertEqual(
            "49ccfe2ee7d9143f87ebad0a2f0d779f238ba0336fba86b8de377de38dd7aee6",
            graph["settlement"]["authorityLinkHash"],
        )
        settlement_hash = graph["settlement"]["contactSettlementHash"]
        fanout_id = graph["fanout_head"]["fanoutId"]
        self.assertEqual(
            "ba613f469a2fdc4946d684d7053005df298363d7785a44fd08bb7dfc7f56a475",
            fanout_id,
        )
        self.assertEqual(
            self.exact_hash,
            graph["alias"]["exactIdentityHash"],
        )
        self.assertEqual(
            f"{self.canonical_hash}--1",
            f"{graph['settlement']['canonicalMailboxIdentityHash']}--"
            f"{graph['settlement']['generation']}",
        )
        self.assertEqual(
            f"{fanout_id}--{self.row_id}",
            f"{graph['obligation']['fanoutId']}--"
            f"{graph['obligation']['rowId']}",
        )
        self.assertEqual(
            settlement_hash,
            graph["fanout_head"]["expectedContactSettlementHash"],
        )

        later_obligation = self._build_obligation(
            settlement_hash=settlement_hash,
            fanout_id=fanout_id,
            created_at=self.later_at,
        )
        self.assertEqual(
            graph["obligation"]["contactFanoutObligationHash"],
            later_obligation["contactFanoutObligationHash"],
            "createdAt is deliberately outside the frozen obligation hash",
        )

        self_alias = self._build_alias(
            exact_identity_hash=self.canonical_hash
        )
        self.assertEqual(
            self.canonical_hash,
            self_alias["exactIdentityHash"],
        )
        self.assertEqual(
            self_alias,
            self.module.validate_contact_alias_document(
                document=self_alias
            ),
        )

        release_settlement = self._build_release_settlement(
            settlement_hash
        )
        release_fanout_id = self._fanout_id(
            settlement_hash=release_settlement["contactSettlementHash"],
            outcome="release",
        )
        released_head = self._build_contact_head(
            release_settlement["contactSettlementHash"],
            release_fanout_id,
            state_revision=2,
            latest_generation=2,
            active_optout_settlement_hash=None,
            state="released",
            updated_at=self.later_at,
        )
        self.assertEqual(
            released_head,
            self.module.validate_contact_head_document(
                document=released_head
            ),
        )
        release_obligation = self._build_obligation(
            settlement_hash=release_settlement["contactSettlementHash"],
            fanout_id=release_fanout_id,
            outcome="release",
        )
        self.assertEqual(
            release_obligation,
            self.module.validate_contact_fanout_obligation_document(
                document=release_obligation
            ),
        )
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self._build_obligation(
                settlement_hash=settlement_hash,
                fanout_id="f" * 64,
            )
        invalid_heads = (
            {"state": StringSubclass("active")},
            {"state_revision": 2},
            {"latest_generation": 2},
            {"state_revision": 2, "latest_generation": 2},
            {"active_optout_settlement_hash": None},
            {"active_optout_settlement_hash": "f" * 64},
            {"state": "released"},
            {"active_fanout_id": "f" * 64},
            {"latest_generation": True},
            {"updated_at": "2026-08-04T11:59:59.000000Z"},
        )
        for overrides in invalid_heads:
            with self.subTest(head=overrides), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self._build_contact_head(
                    settlement_hash,
                    fanout_id,
                    **overrides,
                )
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self._build_contact_head(
                release_settlement["contactSettlementHash"],
                "f" * 64,
                state_revision=2,
                latest_generation=2,
                active_optout_settlement_hash=None,
                state="released",
                updated_at=self.later_at,
            )
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self._build_contact_head(
                release_settlement["contactSettlementHash"],
                release_fanout_id,
                state_revision=3,
                latest_generation=3,
                active_optout_settlement_hash=None,
                state="released",
                updated_at=self.later_at,
            )

    def test_transition_receipt_exact_kind_outcome_head_hash_and_nullability_matrix(self):
        self._require_symbols(
            "build_contact_transition_request_document",
            "validate_contact_transition_request_document",
        )
        created = self._build_transition()
        already_active = self._build_transition(outcome="already_active")
        release = self._build_release_transition()
        for receipt in (created, already_active, release):
            self.assertEqual(
                receipt,
                self.module.validate_contact_transition_request_document(
                    document=receipt
                ),
            )
            self.assertEqual(self.RECORD_KEYS["transition"], set(receipt))
            self.assertGreater(receipt["resultingContactGeneration"], 0)
            for field in (
                "resultingContactSettlementHash",
                "resultingFanoutId",
                "resultingContactHeadHash",
                "resultingFanoutHeadHash",
            ):
                self.assertRegex(receipt[field], r"^[0-9a-f]{64}$")

        invalid_values = (
            self._verified_transition_values(
                transition_kind=StringSubclass("verified_optout")
            ),
            self._verified_transition_values(
                outcome=StringSubclass("created")
            ),
            self._release_transition_values(outcome="already_active"),
            self._verified_transition_values(authority_link_hash=None),
            self._verified_transition_values(actor_scope_hash=self.actor_hash),
            self._release_transition_values(
                authority_link_hash=self._v2_link()["authorityLinkHash"]
            ),
            self._release_transition_values(
                expected_active_optout_settlement_hash=None
            ),
            self._release_transition_values(reason_code=None),
            self._release_transition_values(
                reason_code=StringSubclass("authenticated_release")
            ),
            self._verified_transition_values(
                resulting_contact_generation=True
            ),
            self._verified_transition_values(
                resulting_contact_generation=2
            ),
            self._release_transition_values(
                resulting_contact_generation=3
            ),
            self._verified_transition_values(
                resulting_fanout_id="f" * 64
            ),
            self._release_transition_values(
                resulting_fanout_id="f" * 64
            ),
            self._verified_transition_values(
                requested_at="2026-08-04T12:00:00Z"
            ),
        )
        for values in invalid_values:
            with self.subTest(values=values), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self.module.build_contact_transition_request_document(
                    **values
                )

        crossed_release = deepcopy(release)
        crossed_release["outcome"] = "already_active"
        crossed_release = self._rehash("transition", crossed_release)
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self.module.validate_contact_transition_request_document(
                document=crossed_release
            )

    def test_contact_settlement_requires_transition_id_and_exact_origin(self):
        self._require_symbols(
            "build_contact_settlement_document",
            "validate_contact_settlement_document",
        )
        verified = self._build_verified_settlement()
        release = self._build_release_settlement(
            verified["contactSettlementHash"]
        )
        for settlement in (verified, release):
            self.assertEqual(self.RECORD_KEYS["settlement"], set(settlement))
            self.assertRegex(
                settlement["contactTransitionId"],
                r"^[0-9a-f]{64}$",
            )
            self.assertEqual(
                settlement,
                self.module.validate_contact_settlement_document(
                    document=settlement
                ),
            )

        self.assertEqual(self._v2_link(), verified["authorityLink"])
        self.assertEqual(
            self._v2_link()["authorityLinkHash"],
            verified["authorityLinkHash"],
        )
        self.assertEqual(
            self.hard_evidence_hash,
            verified["hardOptOutEvidenceHash"],
        )
        self.assertIsNone(verified["actorScopeHash"])
        self.assertIsNone(release["authorityLink"])
        self.assertIsNone(release["authorityLinkHash"])
        self.assertIsNone(release["hardOptOutEvidenceHash"])
        self.assertEqual(self.actor_hash, release["actorScopeHash"])

        legacy_link = deepcopy(self._v2_link())
        legacy_link.pop("exactIdentityHash")
        legacy_link.pop("canonicalMailboxIdentityHash")
        legacy_material = {
            key: value
            for key, value in legacy_link.items()
            if key != "authorityLinkHash"
        }
        legacy_link["authorityLinkHash"] = self._independent_hash(
            "sitesift.row.b1_authority_link.v1",
            legacy_material,
            user_scope_hash=self.scope,
        )
        invalid = (
            {"contact_transition_id": None},
            {"contact_transition_id": True},
            {"transition_kind": StringSubclass("verified_optout")},
            {"authority_link": legacy_link},
            {"exact_identity_hash": "f" * 64},
            {"actor_scope_hash": self.actor_hash},
            {"reason_code": "authenticated_release"},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self._build_verified_settlement(**overrides)
        for overrides in (
            {"predecessor_settlement_hash": None},
            {"authority_link": self._v2_link()},
            {"actor_scope_hash": None},
            {"reason_code": None},
            {"reason_code": StringSubclass("authenticated_release")},
            {"generation": 3},
        ):
            with self.subTest(release=overrides), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self._build_release_settlement(
                    verified["contactSettlementHash"],
                    **overrides,
                )

        with self.assertRaises(self.module.RowAuthorityConfigError):
            self._build_verified_settlement(
                contact_transition_id="f" * 64
            )
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self._build_verified_settlement(
                generation=2,
                predecessor_settlement_hash="f" * 64,
            )

        later_link = self._v2_link(workKey="a" * 64)
        later_transition_values = self._verified_transition_values(
            authority_link_hash=later_link["authorityLinkHash"]
        )
        reoptout = self._build_verified_settlement(
            generation=3,
            predecessor_settlement_hash=release["contactSettlementHash"],
            contact_transition_id=self._transition_id(
                later_transition_values
            ),
            authority_link=later_link,
            settled_at="2026-08-04T12:00:02.000000Z",
        )
        self.assertEqual(
            release["contactSettlementHash"],
            reoptout["predecessorSettlementHash"],
        )
        self.assertEqual(
            reoptout,
            self.module.validate_contact_settlement_document(
                document=reoptout
            ),
        )

    def test_fanout_head_state_lease_cursor_and_superseding_matrix(self):
        self._require_symbols(
            "build_contact_fanout_head_document",
            "validate_contact_fanout_head_document",
        )
        active_binding = {
            "binding_revision": 1,
            "binding_head_hash": "9" * 64,
            "binding_association_count": 1,
            "updated_at": self.later_at,
        }
        valid = (
            self._build_fanout_head(),
            self._build_fanout_head(
                state="applying",
                discovery_cursor_row_id=self.row_id,
                cursor_processed_count=1,
                obligation_count=1,
                lease_owner_hash=self.actor_hash,
                lease_until="2026-08-04T12:05:00.000000Z",
                **active_binding,
            ),
            self._build_fanout_head(
                state="superseding",
                discovery_cursor_row_id=self.row_id,
                cursor_processed_count=1,
                obligation_count=1,
                superseding_contact_settlement_hash="f" * 64,
                **active_binding,
            ),
            self._build_fanout_head(
                state="complete",
                obligation_count=1,
                result_count=1,
                completion_binding_revision=1,
                completion_binding_head_hash="9" * 64,
                completion_binding_association_count=1,
                completion_obligation_count=1,
                completion_result_count=1,
                completed_at=self.later_at,
                **active_binding,
            ),
            self._build_fanout_head(
                state="superseded",
                superseding_contact_settlement_hash="f" * 64,
                updated_at=self.later_at,
            ),
            self._build_fanout_head(
                state="ambiguous",
                updated_at=self.later_at,
            ),
        )
        for document in valid:
            with self.subTest(state=document["state"]):
                self.assertEqual(self.RECORD_KEYS["fanout_head"], set(document))
                self.assertEqual(
                    document,
                    self.module.validate_contact_fanout_head_document(
                        document=document
                    ),
                )

        invalid = (
            {"lease_owner_hash": self.actor_hash},
            {"lease_until": "2026-08-04T12:05:00.000000Z"},
            {"state_revision": 1, "fencing_token": 2},
            {"state": "complete"},
            {
                "state": "complete",
                "lease_owner_hash": self.actor_hash,
                "lease_until": "2026-08-04T12:05:00.000000Z",
            },
            {"state": "superseding"},
            {
                "state": "superseding",
                "superseding_contact_settlement_hash": "1" * 64,
            },
            {"superseding_contact_settlement_hash": "f" * 64},
            {
                "state": "superseded",
                "superseding_contact_settlement_hash": "f" * 64,
                "obligation_count": 1,
                "result_count": 0,
            },
            {
                "state": "applying",
                "lease_owner_hash": self.actor_hash,
                "lease_until": self.created_at,
                "updated_at": self.later_at,
            },
            {"state": "superseded", "discovery_cursor_row_id": self.row_id},
            {"state": "ambiguous", "discovery_cursor_row_id": self.row_id},
            {"binding_revision": 0, "binding_head_hash": "9" * 64},
            {"binding_revision": 0, "binding_association_count": 1},
            {"binding_revision": 1, "binding_head_hash": None},
            {"obligation_count": 1},
            {"completion_binding_revision": 0},
            {"binding_revision": True},
            {"binding_association_count": True},
            {"fencing_token": True},
            {"discovery_cursor_row_id": "row-1"},
            {"cursor_processed_count": True},
            {"cursor_processed_count": 1},
            {
                "discovery_cursor_row_id": self.row_id,
                "cursor_processed_count": 0,
            },
            {"fanout_id": "f" * 64},
            {
                "state": "superseded",
                "superseding_contact_settlement_hash": "f" * 64,
                "lease_owner_hash": self.actor_hash,
                "lease_until": "2026-08-04T12:05:00.000000Z",
            },
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self._build_fanout_head(**overrides)

    def test_complete_fanout_rejects_crossed_binding_revision_and_count_deltas(self):
        self._require_symbols(
            "build_contact_fanout_head_document",
            "validate_contact_fanout_head_document",
        )
        apply_values = {
            "state": "complete",
            "binding_revision": 3,
            "binding_head_hash": "9" * 64,
            "binding_association_count": 5,
            "obligation_count": 5,
            "result_count": 5,
            "completion_binding_revision": 2,
            "completion_binding_head_hash": "8" * 64,
            "completion_binding_association_count": 4,
            "completion_obligation_count": 4,
            "completion_result_count": 4,
            "completed_at": self.created_at,
            "updated_at": self.later_at,
        }
        complete_apply = self._build_fanout_head(**apply_values)
        self.assertEqual(
            complete_apply,
            self.module.validate_contact_fanout_head_document(
                document=complete_apply
            ),
        )

        release_values = {
            **apply_values,
            "outcome": "release",
            "obligation_count": 4,
            "result_count": 4,
        }
        complete_release = self._build_fanout_head(**release_values)
        self.assertEqual(
            complete_release,
            self.module.validate_contact_fanout_head_document(
                document=complete_release
            ),
        )

        invalid_apply = (
            {"completion_result_count": 3},
            {"completion_obligation_count": 3},
            {"completion_binding_association_count": 3},
            {"completion_binding_revision": 4},
            {"completion_binding_association_count": 6},
            {"binding_revision": 4},
            {"binding_association_count": 6},
            {"obligation_count": 4},
            {"result_count": 4},
            {"completed_at": "2026-08-04T12:00:02.000000Z"},
        )
        for overrides in invalid_apply:
            values = {**apply_values, **overrides}
            with self.subTest(apply=overrides), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self._build_fanout_head(**values)

        crossed_equal_revision = {
            **apply_values,
            "binding_revision": 2,
            "binding_association_count": 4,
            "obligation_count": 4,
            "result_count": 4,
            "completion_binding_revision": 2,
        }
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self._build_fanout_head(**crossed_equal_revision)

        crossed_zero_completion = {
            **apply_values,
            "binding_revision": 1,
            "binding_association_count": 2,
            "obligation_count": 2,
            "result_count": 2,
            "completion_binding_revision": 0,
            "completion_binding_head_hash": None,
            "completion_binding_association_count": 1,
            "completion_obligation_count": 1,
            "completion_result_count": 1,
        }
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self._build_fanout_head(**crossed_zero_completion)

        for overrides in (
            {"obligation_count": 5},
            {"result_count": 5},
            {"completion_binding_head_hash": None},
        ):
            values = {**release_values, **overrides}
            with self.subTest(release=overrides), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self._build_fanout_head(**values)

    def test_fanout_result_matrix_is_exhaustive(self):
        self._require_symbols(
            "build_contact_fanout_result_document",
            "validate_contact_fanout_result_document",
        )
        empty = {
            "claim_request_id": None,
            "claim_set_hash": None,
            "row_generation": None,
            "row_settlement_hash": None,
            "released_row_generation": None,
            "released_row_settlement_hash": None,
            "restored_effective_generation": None,
            "restored_effective_settlement_hash": None,
        }
        cases = (
            {
                **empty,
                "outcome": "apply",
                "disposition": "applied",
                "reason_code": "claim_accepted",
                "claim_request_id": "1" * 64,
                "claim_set_hash": "2" * 64,
                "row_generation": 1,
                "row_settlement_hash": "3" * 64,
            },
            {
                **empty,
                "outcome": "apply",
                "disposition": "dominated",
                "reason_code": "claim_dominated",
                "claim_request_id": "1" * 64,
                "claim_set_hash": "2" * 64,
            },
            {
                **empty,
                "outcome": "apply",
                "disposition": "noop",
                "reason_code": "row_deleted",
            },
            {
                **empty,
                "outcome": "apply",
                "disposition": "superseded",
                "reason_code": "contact_head_advanced",
            },
            {
                **empty,
                "outcome": "release",
                "disposition": "restore",
                "reason_code": "exact_predecessor",
                "released_row_generation": 2,
                "released_row_settlement_hash": "4" * 64,
                "restored_effective_generation": 1,
                "restored_effective_settlement_hash": "5" * 64,
            },
            {
                **empty,
                "outcome": "release",
                "disposition": "noop",
                "reason_code": "row_optout_not_applied",
            },
            {
                **empty,
                "outcome": "release",
                "disposition": "noop",
                "reason_code": "different_effective_owner",
                "released_row_generation": 2,
                "released_row_settlement_hash": "4" * 64,
            },
            {
                **empty,
                "outcome": "release",
                "disposition": "superseded",
                "reason_code": "contact_head_advanced",
            },
        )
        for values in cases:
            with self.subTest(
                outcome=values["outcome"],
                disposition=values["disposition"],
                reason=values["reason_code"],
            ):
                result = self._build_result(**values)
                self.assertEqual(self.RECORD_KEYS["result"], set(result))
                self.assertEqual(
                    result,
                    self.module.validate_contact_fanout_result_document(
                        document=result
                    ),
                )

        clear_restore = self._build_result(
            **{
                **empty,
                "outcome": "release",
                "disposition": "restore",
                "reason_code": "exact_predecessor",
                "released_row_generation": 2,
                "released_row_settlement_hash": "4" * 64,
            }
        )
        self.assertIsNone(clear_restore["restoredEffectiveGeneration"])
        self.assertIsNone(clear_restore["restoredEffectiveSettlementHash"])

        invalid = (
            {
                **empty,
                "outcome": "release",
                "disposition": "noop",
                "reason_code": "already_restored",
            },
            {**cases[0], "claim_request_id": None},
            {**cases[0], "claim_set_hash": None},
            {**cases[0], "row_generation": None},
            {**cases[0], "row_settlement_hash": None},
            {
                **cases[1],
                "row_generation": 1,
                "row_settlement_hash": "3" * 64,
            },
            {
                **cases[1],
                "released_row_generation": 2,
                "released_row_settlement_hash": "4" * 64,
            },
            {
                **cases[2],
                "claim_request_id": "1" * 64,
                "claim_set_hash": "2" * 64,
            },
            {
                **cases[2],
                "row_generation": 1,
                "row_settlement_hash": "3" * 64,
            },
            {
                **cases[3],
                "claim_request_id": "1" * 64,
                "claim_set_hash": "2" * 64,
            },
            {**cases[4], "released_row_generation": None},
            {**cases[4], "released_row_settlement_hash": None},
            {**cases[4], "restored_effective_generation": None},
            {**cases[4], "restored_effective_settlement_hash": None},
            {
                **cases[5],
                "released_row_generation": 2,
                "released_row_settlement_hash": "4" * 64,
            },
            {**cases[6], "released_row_generation": None},
            {**cases[6], "released_row_settlement_hash": None},
            {
                **cases[6],
                "restored_effective_generation": 1,
                "restored_effective_settlement_hash": "5" * 64,
            },
            {
                **cases[7],
                "released_row_generation": 2,
                "released_row_settlement_hash": "4" * 64,
            },
            {**cases[0], "reason_code": "claim_dominated"},
            {**cases[1], "reason_code": "claim_accepted"},
            {
                **cases[0],
                "disposition": StringSubclass("applied"),
            },
            {
                **cases[0],
                "reason_code": StringSubclass("claim_accepted"),
            },
            {**cases[0], "disposition": "dominated"},
            {**cases[4], "disposition": "noop"},
            {**cases[0], "observed_row_head_hash": None},
            {**cases[0], "obligation_hash": None},
            {**cases[0], "row_generation": True},
            {**cases[4], "released_row_generation": True},
            {**cases[4], "restored_effective_generation": True},
            {**cases[4], "restored_effective_generation": 2},
            {**cases[4], "restored_effective_generation": 3},
        )
        for values in invalid:
            with self.subTest(invalid=values), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self._build_result(**values)

    def test_every_contact_record_rejects_path_hash_schema_and_user_drift(self):
        self._require_symbols(*self.CONTACT_CONTRACTS)
        graph = self._build_graph()
        validators = {
            "alias": self.module.validate_contact_alias_document,
            "transition": (
                self.module.validate_contact_transition_request_document
            ),
            "settlement": self.module.validate_contact_settlement_document,
            "head": self.module.validate_contact_head_document,
            "fanout_head": self.module.validate_contact_fanout_head_document,
            "obligation": (
                self.module.validate_contact_fanout_obligation_document
            ),
            "result": self.module.validate_contact_fanout_result_document,
        }
        timestamp_fields = {
            "alias": "createdAt",
            "transition": "requestedAt",
            "settlement": "settledAt",
            "head": "updatedAt",
            "fanout_head": "updatedAt",
            "obligation": "createdAt",
            "result": "createdAt",
        }
        for kind, document in graph.items():
            validator = validators[kind]
            output_field = self.HASH_OUTPUT_FIELDS[kind]
            invalid_documents = []
            missing = deepcopy(document)
            del missing[output_field]
            invalid_documents.append(missing)
            unknown = deepcopy(document)
            unknown["unknown"] = None
            invalid_documents.append(unknown)
            schema = deepcopy(document)
            schema["schemaVersion"] = 2
            invalid_documents.append(schema)
            user_drift = deepcopy(document)
            user_drift["userScopeHash"] = self.other_scope
            invalid_documents.append(user_drift)
            wrong_domain = self._rehash(
                kind,
                document,
                domain="sitesift.contact.wrong_domain.v1",
            )
            invalid_documents.append(wrong_domain)
            bad_time = deepcopy(document)
            bad_time[timestamp_fields[kind]] = "2026-08-04T12:00:00Z"
            invalid_documents.append(bad_time)
            for invalid_document in invalid_documents:
                with self.subTest(kind=kind, invalid=invalid_document), self.assertRaises(
                    self.module.RowAuthorityConfigError
                ):
                    validator(document=invalid_document)

            original = deepcopy(document)
            validated = validator(document=document)
            validated[output_field] = "mutated"
            self.assertEqual(original, document)

        paths = {
            "alias": self.exact_hash,
            "transition": graph["transition"]["contactTransitionId"],
            "settlement": f"{self.canonical_hash}--1",
            "head": self.canonical_hash,
            "fanout_head": graph["fanout_head"]["fanoutId"],
            "obligation": (
                f"{graph['obligation']['fanoutId']}--{self.row_id}"
            ),
            "result": f"{graph['result']['fanoutId']}--{self.row_id}",
        }
        self.assertEqual(self.exact_hash, paths["alias"])
        self.assertRegex(paths["transition"], r"^[0-9a-f]{64}$")
        self.assertEqual(f"{self.canonical_hash}--1", paths["settlement"])
        self.assertTrue(paths["obligation"].endswith(f"--{self.row_id}"))
        self.assertTrue(paths["result"].endswith(f"--{self.row_id}"))

        cross_user = deepcopy(graph["settlement"])
        cross_user["userScopeHash"] = self.other_scope
        cross_user = self._rehash("settlement", cross_user)
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self.module.validate_contact_settlement_document(
                document=cross_user
            )

        validated_settlement = (
            self.module.validate_contact_settlement_document(
                document=graph["settlement"]
            )
        )
        validated_settlement["authorityLink"]["ownerKey"] = "mutated"
        self.assertEqual(
            self._v2_link(),
            graph["settlement"]["authorityLink"],
        )

    def test_all_contact_domains_are_registered_and_runtime_contained(self):
        self._require_symbols(
            *self.CONTACT_DOMAINS,
            *self.CONTACT_CONTRACTS,
        )
        self.assertEqual(
            self.CONTACT_DOMAINS,
            {
                name: getattr(self.module, name)
                for name in self.CONTACT_DOMAINS
            },
        )
        for name in self.CONTACT_CONTRACTS:
            self.assertTrue(callable(getattr(self.module, name)), name)

        contract_tests = importlib.import_module(
            "tests.test_row_authority_contracts"
        )
        tree = ast.parse(
            contract_tests.ROW_AUTHORITY_PATH.read_text(encoding="utf-8"),
            filename=str(contract_tests.ROW_AUTHORITY_PATH),
        )
        self.assertEqual(
            set(),
            contract_tests._direct_import_roots(tree)
            - contract_tests.ROW_AUTHORITY_STANDARD_LIBRARY_IMPORTS,
        )
        self.assertEqual([], contract_tests._literal_dynamic_imports(tree))
        importers = [
            relative.as_posix()
            for relative in contract_tests._application_python_paths()
            if relative.as_posix()
            not in contract_tests.ROW_AUTHORITY_IMPORTER_ALLOWLIST
            and contract_tests._tree_imports_row_authority(
                ast.parse(
                    (contract_tests.REPO_ROOT / relative).read_text(
                        encoding="utf-8"
                    ),
                    filename=str(relative),
                )
            )
        ]
        self.assertEqual([], importers)


class ContactSuppressionTests(unittest.TestCase):
    """Immediate provider-free contact suppression reads."""

    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")

    def setUp(self):
        self.user_id = "uid-contact-suppression"
        self.raw_mailbox = "Broker+Known@Example.test"
        self.created_at = "2026-08-04T12:00:00.000000Z"
        self.released_at = "2026-08-04T12:00:01.000000Z"

    def _context(self, raw_mailbox=None):
        raw = self.raw_mailbox if raw_mailbox is None else raw_mailbox
        exact, canonical = self.module.normalize_contact_mailbox(raw)
        scope = self.module.user_scope_hash(self.user_id)
        store = BoundedFakeFirestore()
        return {
            "raw": raw,
            "exact": exact,
            "canonical": canonical,
            "exactHash": self.module.contact_identity_hash(
                exact,
                user_scope_hash=scope,
            ),
            "canonicalHash": self.module.contact_identity_hash(
                canonical,
                user_scope_hash=scope,
            ),
            "scope": scope,
            "store": store,
            "authority": self.module.RowAuthorityStore(
                store,
                transaction_executor=run_bounded_transaction,
            ),
        }

    def _reference(self, context, collection, document_id):
        return (
            context["store"]
            .collection("users")
            .document(self.user_id)
            .collection(collection)
            .document(document_id)
        )

    def _alias(self, context, *, exact_hash=None, canonical_hash=None, scope=None):
        return self.module.build_contact_alias_document(
            user_scope_hash=context["scope"] if scope is None else scope,
            exact_identity_hash=(
                context["exactHash"] if exact_hash is None else exact_hash
            ),
            canonical_mailbox_identity_hash=(
                context["canonicalHash"]
                if canonical_hash is None
                else canonical_hash
            ),
            created_at=self.created_at,
        )

    def _v2_link(
        self,
        context,
        *,
        exact_hash=None,
        canonical_source_id="source-contact-suppression",
    ):
        material = {
            "canonicalSourceId": canonical_source_id,
            "snapshotImmutableHash": "1" * 64,
            "selectionHash": "2" * 64,
            "ownerDecisionHash": "3" * 64,
            "ledgerHash": "4" * 64,
            "ownerKind": "contact_optout",
            "ownerKey": context["canonicalHash"],
            "workKey": "6" * 64,
            "payloadHash": "7" * 64,
            "hardOptOutEvidenceHash": "8" * 64,
            "exactIdentityHash": (
                context["exactHash"] if exact_hash is None else exact_hash
            ),
            "canonicalMailboxIdentityHash": context["canonicalHash"],
        }
        return {
            **material,
            "authorityLinkHash": self.module.domain_hash(
                self.module.B1_CONTACT_AUTHORITY_LINK_HASH_DOMAIN,
                material,
                user_scope_hash=context["scope"],
            ),
        }

    def _reactivated_graph(self, context, *, predecessor):
        reactivated_at = "2026-08-04T12:00:02.000000Z"
        link = self._v2_link(
            context,
            canonical_source_id="source-contact-suppression-reactivated",
        )
        transition_material = {
            "transitionKind": "verified_optout",
            "exactIdentityHash": context["exactHash"],
            "canonicalMailboxIdentityHash": context["canonicalHash"],
            "authorityLinkHash": link["authorityLinkHash"],
            "hardOptOutEvidenceHash": link["hardOptOutEvidenceHash"],
            "actorScopeHash": None,
            "clientRequestHash": None,
            "expectedActiveOptOutSettlementHash": None,
            "reasonCode": None,
        }
        transition_id = self.module.domain_hash(
            self.module.CONTACT_TRANSITION_ID_DOMAIN,
            transition_material,
            user_scope_hash=context["scope"],
        )
        settlement = self.module.build_contact_settlement_document(
            user_scope_hash=context["scope"],
            canonical_mailbox_identity_hash=context["canonicalHash"],
            generation=3,
            predecessor_settlement_hash=predecessor[
                "contactSettlementHash"
            ],
            transition_kind="verified_optout",
            contact_transition_id=transition_id,
            exact_identity_hash=context["exactHash"],
            authority_link=link,
            actor_scope_hash=None,
            reason_code=None,
            settled_at=reactivated_at,
        )
        fanout_id = self._fanout_id(
            context,
            settlement["contactSettlementHash"],
            "apply",
        )
        head = self.module.build_contact_head_document(
            user_scope_hash=context["scope"],
            canonical_mailbox_identity_hash=context["canonicalHash"],
            state_revision=3,
            latest_generation=3,
            latest_settlement_hash=settlement["contactSettlementHash"],
            active_optout_settlement_hash=settlement[
                "contactSettlementHash"
            ],
            state="active",
            active_fanout_id=fanout_id,
            created_at=self.created_at,
            updated_at=reactivated_at,
        )
        receipt = self.module.build_contact_transition_request_document(
            user_scope_hash=context["scope"],
            transition_kind="verified_optout",
            exact_identity_hash=context["exactHash"],
            canonical_mailbox_identity_hash=context["canonicalHash"],
            authority_link_hash=link["authorityLinkHash"],
            hard_optout_evidence_hash=link["hardOptOutEvidenceHash"],
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
            resulting_fanout_head_hash="b" * 64,
            requested_at=reactivated_at,
        )
        return {
            "settlement": settlement,
            "head": head,
            "receipt": receipt,
        }

    def _fanout_id(self, context, settlement_hash, outcome):
        return self.module.domain_hash(
            self.module.CONTACT_FANOUT_ID_DOMAIN,
            {
                "contactSettlementHash": settlement_hash,
                "outcome": outcome,
            },
            user_scope_hash=context["scope"],
        )

    def _active_graph(self, context):
        link = self._v2_link(context)
        transition_material = {
            "transitionKind": "verified_optout",
            "exactIdentityHash": context["exactHash"],
            "canonicalMailboxIdentityHash": context["canonicalHash"],
            "authorityLinkHash": link["authorityLinkHash"],
            "hardOptOutEvidenceHash": link["hardOptOutEvidenceHash"],
            "actorScopeHash": None,
            "clientRequestHash": None,
            "expectedActiveOptOutSettlementHash": None,
            "reasonCode": None,
        }
        transition_id = self.module.domain_hash(
            self.module.CONTACT_TRANSITION_ID_DOMAIN,
            transition_material,
            user_scope_hash=context["scope"],
        )
        settlement = self.module.build_contact_settlement_document(
            user_scope_hash=context["scope"],
            canonical_mailbox_identity_hash=context["canonicalHash"],
            generation=1,
            predecessor_settlement_hash=None,
            transition_kind="verified_optout",
            contact_transition_id=transition_id,
            exact_identity_hash=context["exactHash"],
            authority_link=link,
            actor_scope_hash=None,
            reason_code=None,
            settled_at=self.created_at,
        )
        fanout_id = self._fanout_id(
            context,
            settlement["contactSettlementHash"],
            "apply",
        )
        head = self.module.build_contact_head_document(
            user_scope_hash=context["scope"],
            canonical_mailbox_identity_hash=context["canonicalHash"],
            state_revision=1,
            latest_generation=1,
            latest_settlement_hash=settlement["contactSettlementHash"],
            active_optout_settlement_hash=settlement[
                "contactSettlementHash"
            ],
            state="active",
            active_fanout_id=fanout_id,
            created_at=self.created_at,
            updated_at=self.created_at,
        )
        receipt = self.module.build_contact_transition_request_document(
            user_scope_hash=context["scope"],
            transition_kind="verified_optout",
            exact_identity_hash=context["exactHash"],
            canonical_mailbox_identity_hash=context["canonicalHash"],
            authority_link_hash=link["authorityLinkHash"],
            hard_optout_evidence_hash=link["hardOptOutEvidenceHash"],
            actor_scope_hash=None,
            client_request_hash=None,
            expected_active_optout_settlement_hash=None,
            reason_code=None,
            outcome="created",
            resulting_contact_generation=1,
            resulting_contact_settlement_hash=settlement[
                "contactSettlementHash"
            ],
            resulting_fanout_id=fanout_id,
            resulting_contact_head_hash=head["contactHeadHash"],
            resulting_fanout_head_hash="f" * 64,
            requested_at=self.created_at,
        )
        return {
            "exactAlias": self._alias(context),
            "selfAlias": self._alias(
                context,
                exact_hash=context["canonicalHash"],
            ),
            "settlement": settlement,
            "head": head,
            "receipt": receipt,
        }

    def _release_graph(self, context, *, client_request_hash="e" * 64):
        predecessor = self._active_graph(context)
        predecessor_hash = predecessor["settlement"][
            "contactSettlementHash"
        ]
        actor_hash = "d" * 64
        transition_material = {
            "transitionKind": "authenticated_release",
            "exactIdentityHash": context["exactHash"],
            "canonicalMailboxIdentityHash": context["canonicalHash"],
            "authorityLinkHash": None,
            "hardOptOutEvidenceHash": None,
            "actorScopeHash": actor_hash,
            "clientRequestHash": client_request_hash,
            "expectedActiveOptOutSettlementHash": predecessor_hash,
            "reasonCode": "authenticated_release",
        }
        transition_id = self.module.domain_hash(
            self.module.CONTACT_TRANSITION_ID_DOMAIN,
            transition_material,
            user_scope_hash=context["scope"],
        )
        settlement = self.module.build_contact_settlement_document(
            user_scope_hash=context["scope"],
            canonical_mailbox_identity_hash=context["canonicalHash"],
            generation=2,
            predecessor_settlement_hash=predecessor_hash,
            transition_kind="authenticated_release",
            contact_transition_id=transition_id,
            exact_identity_hash=context["exactHash"],
            authority_link=None,
            actor_scope_hash=actor_hash,
            reason_code="authenticated_release",
            settled_at=self.released_at,
        )
        fanout_id = self._fanout_id(
            context,
            settlement["contactSettlementHash"],
            "release",
        )
        head = self.module.build_contact_head_document(
            user_scope_hash=context["scope"],
            canonical_mailbox_identity_hash=context["canonicalHash"],
            state_revision=2,
            latest_generation=2,
            latest_settlement_hash=settlement["contactSettlementHash"],
            active_optout_settlement_hash=None,
            state="released",
            active_fanout_id=fanout_id,
            created_at=self.created_at,
            updated_at=self.released_at,
        )
        receipt = self.module.build_contact_transition_request_document(
            user_scope_hash=context["scope"],
            transition_kind="authenticated_release",
            exact_identity_hash=context["exactHash"],
            canonical_mailbox_identity_hash=context["canonicalHash"],
            authority_link_hash=None,
            hard_optout_evidence_hash=None,
            actor_scope_hash=actor_hash,
            client_request_hash=client_request_hash,
            expected_active_optout_settlement_hash=predecessor_hash,
            reason_code="authenticated_release",
            outcome="created",
            resulting_contact_generation=2,
            resulting_contact_settlement_hash=settlement[
                "contactSettlementHash"
            ],
            resulting_fanout_id=fanout_id,
            resulting_contact_head_hash=head["contactHeadHash"],
            resulting_fanout_head_hash="a" * 64,
            requested_at=self.released_at,
        )
        return {
            "exactAlias": self._alias(context),
            "selfAlias": self._alias(
                context,
                exact_hash=context["canonicalHash"],
            ),
            "predecessorSettlement": predecessor["settlement"],
            "predecessorReceipt": predecessor["receipt"],
            "settlement": settlement,
            "head": head,
            "receipt": receipt,
        }

    def _seed_graph(
        self,
        context,
        graph,
        *,
        exact_alias=True,
        self_alias=True,
        receipt=True,
    ):
        documents = []
        if exact_alias:
            documents.append(
                (
                    "contactOptOutAliases",
                    context["exactHash"],
                    graph["exactAlias"],
                )
            )
        if self_alias and (
            not exact_alias
            or context["canonicalHash"] != context["exactHash"]
        ):
            documents.append(
                (
                    "contactOptOutAliases",
                    context["canonicalHash"],
                    graph["selfAlias"],
                )
            )
        predecessor_settlement = graph.get("predecessorSettlement")
        if predecessor_settlement is not None:
            documents.append(
                (
                    "contactOptOutSettlements",
                    f"{context['canonicalHash']}--"
                    f"{predecessor_settlement['generation']}",
                    predecessor_settlement,
                )
            )
        predecessor_receipt = graph.get("predecessorReceipt")
        if predecessor_receipt is not None:
            documents.append(
                (
                    "contactOptOutTransitionRequests",
                    predecessor_settlement["contactTransitionId"],
                    predecessor_receipt,
                )
            )
        documents.extend(
            (
                (
                    "contactOptOutHeads",
                    context["canonicalHash"],
                    graph["head"],
                ),
                (
                    "contactOptOutSettlements",
                    f"{context['canonicalHash']}--"
                    f"{graph['settlement']['generation']}",
                    graph["settlement"],
                ),
            )
        )
        if receipt:
            documents.append(
                (
                    "contactOptOutTransitionRequests",
                    graph["settlement"]["contactTransitionId"],
                    graph["receipt"],
                )
            )
        for collection, document_id, document in documents:
            self._reference(context, collection, document_id).create(document)
        context["store"].events.clear()

    def _read(self, context, *, raw_mailbox=None):
        method = getattr(
            context["authority"],
            "read_contact_optout_suppression",
            None,
        )
        self.assertTrue(
            callable(method),
            "RowAuthorityStore.read_contact_optout_suppression is missing",
        )
        return method(
            verified_user_id=self.user_id,
            raw_mailbox=(context["raw"] if raw_mailbox is None else raw_mailbox),
        )

    def test_alias_creation_validation_and_conflict_table(self):
        context = self._context()
        exact_alias = self._alias(context)
        self_alias = self._alias(
            context,
            exact_hash=context["canonicalHash"],
        )
        self.assertEqual(
            exact_alias,
            self.module.validate_contact_alias_document(
                document=exact_alias
            ),
        )
        self.assertEqual(
            self_alias,
            self.module.validate_contact_alias_document(document=self_alias),
        )

        graph = self._active_graph(context)
        self._seed_graph(context, graph, exact_alias=False)
        self.assertEqual(
            {"decision": "suppress", "reason": "active"},
            self._read(context),
        )

        for exact_alias_present, self_alias_present in ((True, False),):
            with self.subTest(
                exact=exact_alias_present,
                self_alias=self_alias_present,
            ):
                partial = self._context()
                partial_graph = self._active_graph(partial)
                self._seed_graph(
                    partial,
                    partial_graph,
                    exact_alias=exact_alias_present,
                    self_alias=self_alias_present,
                )
                self.assertEqual(
                    {"decision": "suppress", "reason": "ambiguous"},
                    self._read(partial),
                )

        conflicting = self._context()
        conflicting_graph = self._active_graph(conflicting)
        self._seed_graph(conflicting, conflicting_graph)
        other_canonical = "0" * 64
        conflict_document = self._alias(
            conflicting,
            canonical_hash=other_canonical,
        )
        conflict_path = self._reference(
            conflicting,
            "contactOptOutAliases",
            conflicting["exactHash"],
        ).path
        conflicting["store"].data[conflict_path] = conflict_document
        self.assertEqual(
            {"decision": "suppress", "reason": "ambiguous"},
            self._read(conflicting),
        )

        same = self._context("Broker@Example.test")
        same_graph = self._active_graph(same)
        self._seed_graph(same, same_graph)
        alias_paths = [
            path
            for path in same["store"].data
            if "/contactOptOutAliases/" in path
        ]
        self.assertEqual(1, len(alias_paths))
        self.assertEqual(
            {"decision": "suppress", "reason": "active"},
            self._read(same),
        )

    def test_active_contact_suppresses_exact_and_unseen_plus_variant(self):
        context = self._context()
        self._seed_graph(context, self._active_graph(context))
        self.assertEqual(
            {"decision": "suppress", "reason": "active"},
            self._read(context),
        )
        self.assertEqual(
            {"decision": "suppress", "reason": "active"},
            self._read(
                context,
                raw_mailbox="Broker+Unseen@Example.test",
            ),
        )

    def test_valid_released_or_absent_contact_allows(self):
        released = self._context()
        self._seed_graph(
            released,
            self._release_graph(released),
            exact_alias=False,
        )
        self.assertEqual(
            {"decision": "allow", "reason": "released"},
            self._read(released),
        )

        absent = self._context()
        self.assertEqual(
            {"decision": "allow", "reason": "absent"},
            self._read(absent),
        )
        self.assertEqual(
            {"decision": "allow", "reason": "absent"},
            self._read(
                absent,
                raw_mailbox="Broker+Unseen@Example.test",
            ),
        )

    def test_rolled_back_released_head_cannot_allow_later_active_epoch(self):
        context = self._context()
        released = self._release_graph(context)
        self._seed_graph(context, released)
        reactivated = self._reactivated_graph(
            context,
            predecessor=released["settlement"],
        )
        head_ref = self._reference(
            context,
            "contactOptOutHeads",
            context["canonicalHash"],
        )
        self._reference(
            context,
            "contactOptOutSettlements",
            f"{context['canonicalHash']}--3",
        ).create(reactivated["settlement"])
        self._reference(
            context,
            "contactOptOutTransitionRequests",
            reactivated["settlement"]["contactTransitionId"],
        ).create(reactivated["receipt"])
        head_ref.set(reactivated["head"], merge=False)
        self.assertEqual(
            {"decision": "suppress", "reason": "active"},
            self._read(context),
        )

        head_ref.set(released["head"], merge=False)
        before = deepcopy(context["store"].data)
        context["store"].events.clear()

        self.assertEqual(
            {"decision": "suppress", "reason": "ambiguous"},
            self._read(context),
        )
        self.assertEqual(before, context["store"].data)
        self.assertFalse(
            any(
                event[0] in {"create", "set", "update", "delete"}
                for event in context["store"].events
            )
        )

    def test_every_alias_head_and_settlement_failure_is_fail_closed(self):
        context = self._context()
        graph = self._active_graph(context)
        self._seed_graph(context, graph)
        paths = (
            self._reference(
                context,
                "contactOptOutAliases",
                context["exactHash"],
            ).path,
            self._reference(
                context,
                "contactOptOutAliases",
                context["canonicalHash"],
            ).path,
            self._reference(
                context,
                "contactOptOutHeads",
                context["canonicalHash"],
            ).path,
            self._reference(
                context,
                "contactOptOutSettlements",
                f"{context['canonicalHash']}--1",
            ).path,
            self._reference(
                context,
                "contactOptOutTransitionRequests",
                graph["settlement"]["contactTransitionId"],
            ).path,
        )
        reference_type = type(
            self._reference(context, "contactOptOutHeads", "placeholder")
        )
        original_get = reference_type.get
        for failed_path in paths:
            def fail_selected(reference, *args, **kwargs):
                if reference.path == failed_path:
                    raise RuntimeError("configured read failure")
                return original_get(reference, *args, **kwargs)

            with self.subTest(failed_path=failed_path), patch.object(
                reference_type,
                "get",
                new=fail_selected,
            ):
                self.assertEqual(
                    {"decision": "suppress", "reason": "ambiguous"},
                    self._read(context),
                )

        for malformed_path in paths:
            with self.subTest(malformed_path=malformed_path):
                original = deepcopy(context["store"].data[malformed_path])
                context["store"].data[malformed_path] = {
                    **original,
                    "unknown": None,
                }
                self.assertEqual(
                    {"decision": "suppress", "reason": "ambiguous"},
                    self._read(context),
                )
                context["store"].data[malformed_path] = original

        cross_scope_alias = self._alias(
            context,
            scope="0" * 64,
        )
        context["store"].data[paths[0]] = cross_scope_alias
        self.assertEqual(
            {"decision": "suppress", "reason": "ambiguous"},
            self._read(context),
        )

    def test_missing_or_mismatched_creating_receipt_cannot_allow_release(self):
        missing = self._context()
        missing_graph = self._release_graph(missing)
        self._seed_graph(missing, missing_graph, receipt=False)
        self.assertEqual(
            {"decision": "suppress", "reason": "ambiguous"},
            self._read(missing),
        )

        mismatched = self._context()
        release_graph = self._release_graph(mismatched)
        self._seed_graph(mismatched, release_graph)
        different_receipt = self._release_graph(
            mismatched,
            client_request_hash="7" * 64,
        )["receipt"]
        receipt_path = self._reference(
            mismatched,
            "contactOptOutTransitionRequests",
            release_graph["settlement"]["contactTransitionId"],
        ).path
        mismatched["store"].data[receipt_path] = different_receipt
        self.assertEqual(
            {"decision": "suppress", "reason": "ambiguous"},
            self._read(mismatched),
        )

    def test_impossible_canonical_alias_chronology_cannot_allow_release(self):
        context = self._context()
        graph = self._release_graph(context)
        self._seed_graph(context, graph)
        self_alias_path = self._reference(
            context,
            "contactOptOutAliases",
            context["canonicalHash"],
        ).path
        context["store"].data[self_alias_path] = (
            self.module.build_contact_alias_document(
                user_scope_hash=context["scope"],
                exact_identity_hash=context["canonicalHash"],
                canonical_mailbox_identity_hash=context["canonicalHash"],
                created_at="2026-08-04T12:00:02.000000Z",
            )
        )

        self.assertEqual(
            {"decision": "suppress", "reason": "ambiguous"},
            self._read(context),
        )

    def test_impossible_exact_alias_chronology_cannot_allow_release(self):
        context = self._context()
        graph = self._release_graph(context)
        self._seed_graph(context, graph)
        self.assertNotEqual(context["exactHash"], context["canonicalHash"])
        exact_alias_path = self._reference(
            context,
            "contactOptOutAliases",
            context["exactHash"],
        ).path
        context["store"].data[exact_alias_path] = (
            self.module.build_contact_alias_document(
                user_scope_hash=context["scope"],
                exact_identity_hash=context["exactHash"],
                canonical_mailbox_identity_hash=context["canonicalHash"],
                created_at="2026-08-04T12:00:02.000000Z",
            )
        )

        self.assertEqual(
            {"decision": "suppress", "reason": "ambiguous"},
            self._read(context),
        )

    def test_suppression_read_bounds_cover_each_semantic_branch(self):
        cases = []

        absent = self._context()
        cases.append(
            (
                "absent-distinct",
                absent,
                {"decision": "allow", "reason": "absent"},
                3,
            )
        )

        same_active = self._context("Broker@Example.test")
        self._seed_graph(same_active, self._active_graph(same_active))
        cases.append(
            (
                "active-same-hash",
                same_active,
                {"decision": "suppress", "reason": "active"},
                4,
            )
        )

        released = self._context()
        self._seed_graph(
            released,
            self._release_graph(released),
            exact_alias=False,
        )
        cases.append(
            (
                "released-distinct",
                released,
                {"decision": "allow", "reason": "released"},
                5,
            )
        )

        ambiguous = self._context()
        ambiguous_graph = self._active_graph(ambiguous)
        self._seed_graph(ambiguous, ambiguous_graph, receipt=False)
        cases.append(
            (
                "ambiguous-missing-receipt",
                ambiguous,
                {"decision": "suppress", "reason": "ambiguous"},
                5,
            )
        )

        for label, context, expected_result, expected_reads in cases:
            with self.subTest(label=label):
                before = deepcopy(context["store"].data)
                context["store"].events.clear()
                query_type = type(
                    context["store"]
                    .collection("probe")
                    .where("field", "==", "value")
                )
                observed_limits = []
                original_limit = query_type.limit

                def observing_limit(query, count):
                    if query._collection.path.endswith(
                        "/contactOptOutSettlements"
                    ):
                        observed_limits.append(count)
                    return original_limit(query, count)

                with patch.object(
                    query_type,
                    "limit",
                    observing_limit,
                ):
                    self.assertEqual(expected_result, self._read(context))
                self.assertEqual(before, context["store"].data)
                self.assertEqual(
                    expected_reads,
                    sum(
                        event[0] == "get"
                        for event in context["store"].events
                    ),
                )
                self.assertFalse(
                    any(
                        event[0]
                        in {"create", "set", "update", "delete"}
                        for event in context["store"].events
                    )
                )
                self.assertEqual([2], observed_limits)
                settlement_queries = [
                    event
                    for event in context["store"].events
                    if event[0] == "query"
                    and event[1].endswith(
                        "/contactOptOutSettlements"
                    )
                ]
                self.assertEqual(1, len(settlement_queries))
                self.assertEqual(
                    (
                        (
                            "canonicalMailboxIdentityHash",
                            "==",
                            context["canonicalHash"],
                        ),
                    ),
                    settlement_queries[0][2],
                )
                self.assertEqual(
                    ("generation",),
                    settlement_queries[0][3],
                )
                self.assertEqual(
                    ("DESCENDING",),
                    settlement_queries[0][4],
                )

    def test_suppression_is_zero_write_and_persists_no_raw_identity(self):
        context = self._context()
        self._seed_graph(context, self._active_graph(context))
        before = deepcopy(context["store"].data)
        context["store"].events.clear()

        result = self._read(context)

        self.assertEqual(
            {"decision": "suppress", "reason": "active"},
            result,
        )
        signature = inspect.signature(
            self.module.RowAuthorityStore.read_contact_optout_suppression
        )
        self.assertEqual(
            ["self", "verified_user_id", "raw_mailbox"],
            list(signature.parameters),
        )
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.KEYWORD_ONLY
                for name, parameter in signature.parameters.items()
                if name != "self"
            )
        )
        self.assertEqual(before, context["store"].data)
        self.assertFalse(
            any(
                event[0] in {"create", "set", "update", "delete"}
                for event in context["store"].events
            )
        )
        get_events = [
            event
            for event in context["store"].events
            if event[0] == "get"
        ]
        self.assertEqual(5, len(get_events))
        settlement_queries = [
            event
            for event in context["store"].events
            if event[0] == "query"
            and event[1].endswith("/contactOptOutSettlements")
        ]
        self.assertEqual(1, len(settlement_queries))
        persisted = json.dumps(
            {
                "data": context["store"].data,
                "events": context["store"].events,
                "result": result,
            },
            sort_keys=True,
        ).lower()
        for raw_fragment in (
            context["raw"].strip().lower(),
            context["exact"],
            context["canonical"],
        ):
            self.assertNotIn(raw_fragment, persisted)


class ContactFanoutLeaseTests(unittest.TestCase):
    """Leased contact fan-out state-machine contracts."""

    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")
        transitions = importlib.import_module(
            "tests.test_row_authority_contact_transitions"
        )
        cls.transition_type = transitions.ContactTransitionTests
        cls.transition_type.setUpClass()

    def setUp(self):
        self.transition = self.transition_type(methodName="runTest")
        self.transition.setUp()
        self.owner_a = "a" * 64
        self.owner_b = "b" * 64
        self.acquired_at = "2026-08-04T12:05:00.000000Z"
        self.lease_until = "2026-08-04T12:06:00.000000Z"

    def _method(self):
        method = getattr(
            self.module.RowAuthorityStore,
            "acquire_contact_fanout_lease",
            None,
        )
        self.assertTrue(
            callable(method),
            "RowAuthorityStore.acquire_contact_fanout_lease is missing",
        )
        signature = inspect.signature(method)
        self.assertEqual(
            [
                "self",
                "verified_user_id",
                "fanout_id",
                "expected_fanout_head",
                "lease_owner_hash",
                "lease_until",
                "acquired_at",
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

    def _user(self, store):
        return store.collection("users").document(
            self.transition.fixture.user_id
        )

    def _fanout_reference(self, store, fanout_id):
        return self._user(store).collection(
            "contactOptOutFanoutHeads"
        ).document(fanout_id)

    @staticmethod
    def _writes(store):
        return [
            event
            for event in store.events
            if event[0] in {"create", "set", "update", "delete"}
        ]

    def _seed_fanout(self):
        store = self.transition.fixture._store()
        bundle, _link = self.transition._seed_bundle(
            store,
            "source-contact-fanout-lease",
        )
        self.transition._record(store, bundle)
        _aliases, _receipts, _settlement, _head, fanout = (
            self.transition._assert_one_active_epoch(store)
        )
        store.events.clear()
        return store, fanout

    def _acquire(
        self,
        store,
        expected,
        *,
        owner=None,
        lease_until=None,
        acquired_at=None,
    ):
        self._method()
        authority = self.transition._authority(store)
        return authority.acquire_contact_fanout_lease(
            verified_user_id=self.transition.fixture.user_id,
            fanout_id=expected["fanoutId"],
            expected_fanout_head=expected,
            lease_owner_hash=owner or self.owner_a,
            lease_until=lease_until or self.lease_until,
            acquired_at=acquired_at or self.acquired_at,
        )

    def _replace_fanout(self, store, current, **overrides):
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
            "cursor_processed_count": current["cursorProcessedCount"],
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
            "completion_result_count": current["completionResultCount"],
            "completed_at": current["completedAt"],
            "created_at": current["createdAt"],
            "updated_at": current["updatedAt"],
        }
        values.update(overrides)
        replacement = self.module.build_contact_fanout_head_document(**values)
        self._fanout_reference(store, current["fanoutId"]).set(
            replacement,
            merge=False,
        )
        store.events.clear()
        return replacement

    def _assert_one_head_write(self, store, result):
        self.assertEqual({"disposition", "fanoutHead"}, set(result))
        writes = self._writes(store)
        self.assertEqual(1, len(writes))
        self.assertEqual("set", writes[0][0])
        self.assertEqual(
            self._fanout_reference(store, result["fanoutHead"]["fanoutId"]).path,
            writes[0][1],
        )
        self.assertEqual(result["fanoutHead"], writes[0][2])
        self.assertFalse(writes[0][3])

    def _assert_rejected_without_write(self, store, operation):
        before = deepcopy(store.data)
        store.events.clear()
        with self.assertRaises(self.module.RowAuthorityError):
            operation()
        self.assertEqual(before, store.data)
        self.assertEqual([], self._writes(store))

    def test_nonterminal_mutation_requires_exact_unexpired_lease_and_fence(self):
        store, initial = self._seed_fanout()
        acquired = self._acquire(store, initial)
        current = acquired["fanoutHead"]
        self.assertEqual(self.owner_a, current["leaseOwnerHash"])
        self.assertEqual(self.lease_until, current["leaseUntil"])
        self.assertEqual(initial["fencingToken"] + 1, current["fencingToken"])

        self._assert_rejected_without_write(
            store,
            lambda: self._acquire(
                store,
                current,
                owner=self.owner_b,
                lease_until="2026-08-04T12:07:00.000000Z",
                acquired_at="2026-08-04T12:05:30.000000Z",
            ),
        )
        self._assert_rejected_without_write(
            store,
            lambda: self._acquire(
                store,
                initial,
                acquired_at="2026-08-04T12:05:30.000000Z",
            ),
        )

    def test_new_fanout_is_unleased_at_fence_one(self):
        self._method()
        _store, fanout = self._seed_fanout()
        self.assertEqual("discovering", fanout["state"])
        self.assertEqual(1, fanout["stateRevision"])
        self.assertEqual(1, fanout["fencingToken"])
        self.assertIsNone(fanout["leaseOwnerHash"])
        self.assertIsNone(fanout["leaseUntil"])
        self.assertIsNone(fanout["discoveryCursorRowId"])
        self.assertEqual(0, fanout["cursorProcessedCount"])

    def test_acquisition_and_renewal_increment_revision_and_fence(self):
        store, initial = self._seed_fanout()
        acquired = self._acquire(store, initial)
        self.assertEqual("acquired", acquired["disposition"])
        self._assert_one_head_write(store, acquired)
        first = acquired["fanoutHead"]
        self.assertEqual(initial["stateRevision"] + 1, first["stateRevision"])
        self.assertEqual(initial["fencingToken"] + 1, first["fencingToken"])
        self.assertEqual(
            initial["cursorProcessedCount"],
            first["cursorProcessedCount"],
        )

        store.events.clear()
        renewed = self._acquire(
            store,
            first,
            lease_until="2026-08-04T12:07:00.000000Z",
            acquired_at="2026-08-04T12:05:30.000000Z",
        )
        self.assertEqual("renewed", renewed["disposition"])
        self._assert_one_head_write(store, renewed)
        second = renewed["fanoutHead"]
        self.assertEqual(first["stateRevision"] + 1, second["stateRevision"])
        self.assertEqual(first["fencingToken"] + 1, second["fencingToken"])
        self.assertEqual(
            first["cursorProcessedCount"],
            second["cursorProcessedCount"],
        )

    def test_exact_lease_request_retry_returns_same_zero_write_after_image(self):
        store, initial = self._seed_fanout()
        first = self._acquire(store, initial)
        store.events.clear()

        try:
            retry = self._acquire(store, initial)
        except self.module.RowAuthorityError as exc:
            self.fail(
                "exact lease request retry must return its deterministic "
                f"after-image, not {type(exc).__name__}: {exc}"
            )

        self.assertEqual(first, retry)
        self.assertEqual([], self._writes(store))
        self.assertEqual(1, store.events.count(("commit_applied", 0)))

    def test_two_worker_same_lease_request_race_writes_one_identical_after_image(self):
        store, initial = self._seed_fanout()
        store.before_commit_barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self._acquire, store, initial)
                for _worker in range(2)
            ]
            results = []
            errors = []
            for future in futures:
                try:
                    results.append(future.result(timeout=10))
                except self.module.RowAuthorityError as exc:
                    errors.append(exc)

        self.assertEqual(1, len(self._writes(store)))
        self.assertEqual(
            [],
            errors,
            "same-request loser must read back the winning after-image",
        )
        self.assertEqual(2, len(results))
        self.assertEqual(results[0], results[1])
        self.assertEqual(1, store.events.count(("commit_applied", 1)))
        self.assertEqual(1, store.events.count(("commit_applied", 0)))
        self.assertTrue(
            any(
                event[0] == "commit_aborted_stale_read"
                for event in store.events
            )
        )

    def test_same_owner_request_strictly_after_expiry_is_taken_over(self):
        store, initial = self._seed_fanout()
        acquired = self._acquire(store, initial)["fanoutHead"]
        store.events.clear()

        takeover = self._acquire(
            store,
            acquired,
            owner=self.owner_a,
            lease_until="2026-08-04T12:07:00.000000Z",
            acquired_at="2026-08-04T12:06:00.000001Z",
        )

        self.assertEqual("taken_over", takeover["disposition"])
        self._assert_one_head_write(store, takeover)
        current = takeover["fanoutHead"]
        self.assertEqual(acquired["stateRevision"] + 1, current["stateRevision"])
        self.assertEqual(acquired["fencingToken"] + 1, current["fencingToken"])
        self.assertEqual(self.owner_a, current["leaseOwnerHash"])

    def test_expired_takeover_increments_revision_and_fence(self):
        store, initial = self._seed_fanout()
        acquired = self._acquire(store, initial)["fanoutHead"]
        self._assert_rejected_without_write(
            store,
            lambda: self._acquire(
                store,
                acquired,
                owner=self.owner_b,
                lease_until="2026-08-04T12:07:00.000000Z",
                acquired_at=acquired["leaseUntil"],
            ),
        )

        takeover = self._acquire(
            store,
            acquired,
            owner=self.owner_b,
            lease_until="2026-08-04T12:07:00.000000Z",
            acquired_at="2026-08-04T12:06:00.000001Z",
        )
        self.assertEqual("taken_over", takeover["disposition"])
        self._assert_one_head_write(store, takeover)
        current = takeover["fanoutHead"]
        self.assertEqual(acquired["stateRevision"] + 1, current["stateRevision"])
        self.assertEqual(acquired["fencingToken"] + 1, current["fencingToken"])
        self.assertEqual(self.owner_b, current["leaseOwnerHash"])

    def test_stale_worker_cannot_write_after_takeover_or_superseding(self):
        store, initial = self._seed_fanout()
        acquired = self._acquire(store, initial)["fanoutHead"]
        takeover = self._acquire(
            store,
            acquired,
            owner=self.owner_b,
            lease_until="2026-08-04T12:08:00.000000Z",
            acquired_at="2026-08-04T12:06:00.000001Z",
        )["fanoutHead"]
        self._assert_rejected_without_write(
            store,
            lambda: self._acquire(
                store,
                acquired,
                lease_until="2026-08-04T12:09:00.000000Z",
                acquired_at="2026-08-04T12:07:00.000000Z",
            ),
        )

        self._replace_fanout(
            store,
            takeover,
            state_revision=takeover["stateRevision"] + 1,
            state="superseding",
            lease_owner_hash=None,
            lease_until=None,
            fencing_token=takeover["fencingToken"] + 1,
            superseding_contact_settlement_hash="f" * 64,
            discovery_cursor_row_id=None,
            updated_at="2026-08-04T12:07:30.000000Z",
        )
        self._assert_rejected_without_write(
            store,
            lambda: self._acquire(
                store,
                takeover,
                owner=self.owner_b,
                lease_until="2026-08-04T12:09:00.000000Z",
                acquired_at="2026-08-04T12:08:00.000000Z",
            ),
        )

    def test_terminal_fanout_rejects_takeover_and_has_null_lease_cursor(self):
        store, initial = self._seed_fanout()
        terminal = self._replace_fanout(
            store,
            initial,
            state_revision=initial["stateRevision"] + 1,
            state="complete",
            completion_binding_revision=initial["bindingRevision"],
            completion_binding_head_hash=initial["bindingHeadHash"],
            completion_binding_association_count=initial[
                "bindingAssociationCount"
            ],
            completion_obligation_count=initial["obligationCount"],
            completion_result_count=initial["resultCount"],
            completed_at="2026-08-04T12:07:00.000000Z",
            updated_at="2026-08-04T12:07:00.000000Z",
        )
        self.assertIsNone(terminal["leaseOwnerHash"])
        self.assertIsNone(terminal["leaseUntil"])
        self.assertIsNone(terminal["discoveryCursorRowId"])
        self.assertEqual(0, terminal["cursorProcessedCount"])
        self._assert_rejected_without_write(
            store,
            lambda: self._acquire(
                store,
                terminal,
                owner=self.owner_b,
                lease_until="2026-08-04T12:09:00.000000Z",
                acquired_at="2026-08-04T12:08:00.000000Z",
            ),
        )


class ReleaseAwareRowHistoryTests(unittest.TestCase):
    """Executable contract for bounded, release-aware row allocation."""

    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")
        cls.ownership = importlib.import_module(
            "tests.test_row_authority_ownership"
        )
        cls.fixture_type = cls.ownership.RowClaimStoreTests
        cls.fixture_type.setUpClass()

    def setUp(self):
        # Compose the retained fixture instead of inheriting its TestCase. That
        # keeps this module's discovery surface limited to the selected tests.
        self.fixture = self.fixture_type(methodName="runTest")
        self.fixture.setUp()

    def _seed(self, *, owner_kind="terminal"):
        store = self.fixture._store()
        bundle, binding = self.fixture._seed_prerequisites(
            store,
            owner_kind=owner_kind,
        )
        return store, bundle, binding

    def _contact_link(self, *, source_id):
        return self._b1_link(
            owner_kind="contact_optout",
            source_id=source_id,
        )

    def _b1_link(self, *, owner_kind, source_id):
        bundle = self.ownership.RowOwnershipContractTests._b1_bundle(
            self.fixture,
            owner_kind=owner_kind,
            contact_evidence_version=(
                2 if owner_kind == "contact_optout" else None
            ),
            source_id=source_id,
        )
        return self.module.build_b1_authority_link(
            user_scope_hash=self.fixture.scope,
            source_identity_document=bundle["identity"],
            source_classification_document=bundle["classification"],
            source_owner_document=bundle["owner"],
            source_ledger_document=bundle["ledger"],
            work_key=bundle["work_key"],
        )

    def _forged_accepted_generation(
        self,
        *,
        expected_head,
        owner_kind,
        generation_number,
        first_fencing_token,
        predecessor_settlement_hash,
        created_at,
        lease_owner_hash="d" * 64,
        lease_until="2026-08-04T13:00:00.000000Z",
        row_id=None,
    ):
        row_id = row_id or self.fixture.first
        authority_link = self._b1_link(
            owner_kind=owner_kind,
            source_id=f"forged-{owner_kind}-{generation_number}",
        )
        claim = self.module.build_claim_set_document(
            user_scope_hash=self.fixture.scope,
            authority_origin="b1_source",
            authority_link=authority_link,
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
                    "plannedGeneration": generation_number,
                    "winnerGenerationHash": None,
                    "winnerSettlementHash": None,
                }
            ],
            created_at=created_at,
        )
        generation = self.module.build_owner_generation_document(
            claim_set_document=claim,
            row_id=row_id,
            generation=generation_number,
            predecessor_head_hash=expected_head["headHash"],
            predecessor_settlement_hash=predecessor_settlement_hash,
            lease_epoch=1,
            first_fencing_token=first_fencing_token,
            created_at=created_at,
        )
        forged_head = {
            key: deepcopy(value)
            for key, value in expected_head.items()
            if key != "headHash"
        }
        forged_head.update(
            {
                "stateRevision": expected_head["stateRevision"] + 1,
                "effectiveOwnerGeneration": generation_number,
                "effectiveOwnerGenerationHash": generation[
                    "generationHash"
                ],
                "effectiveOwnerKind": owner_kind,
                "effectivePriority": generation["priority"],
                "state": (
                    "review_pending"
                    if owner_kind == "human_decision"
                    else "claimed"
                ),
                "leaseOwnerHash": lease_owner_hash,
                "leaseUntil": lease_until,
                "fencingToken": first_fencing_token,
                "effectiveSettlementHash": predecessor_settlement_hash,
                "updatedAt": created_at,
            }
        )
        return claim, generation, self.fixture._rehash_head(forged_head)

    def _install_contact_settlement(
        self,
        store,
        *,
        expected_head,
        generation_number,
        first_fence,
        predecessor_settlement,
        created_at,
        settled_at,
        cycle,
        row_id=None,
    ):
        row_id = row_id or self.fixture.first
        fanout_id = f"{cycle + 10:064x}"
        contact_settlement_hash = f"{cycle + 20:064x}"
        link = self._contact_link(source_id=f"contact-cycle-{cycle}")
        claim = self.module.build_claim_set_document(
            user_scope_hash=self.fixture.scope,
            authority_origin="contact_fanout",
            authority_link=link,
            operator_action_document=None,
            fanout_id=fanout_id,
            row_ids=[row_id],
            primary_row_id=row_id,
            planned_writes=3,
            outcome="accepted",
            row_decisions=[
                {
                    "rowId": row_id,
                    "decision": "accepted",
                    "plannedGeneration": generation_number,
                    "winnerGenerationHash": None,
                    "winnerSettlementHash": None,
                }
            ],
            created_at=created_at,
            canonical_mailbox_identity_hash=link[
                "canonicalMailboxIdentityHash"
            ],
            contact_settlement_hash=contact_settlement_hash,
        )
        generation = self.module.build_owner_generation_document(
            claim_set_document=claim,
            row_id=row_id,
            generation=generation_number,
            predecessor_head_hash=expected_head["headHash"],
            predecessor_settlement_hash=(
                predecessor_settlement["settlementHash"]
                if predecessor_settlement is not None
                else None
            ),
            lease_epoch=1,
            first_fencing_token=first_fence,
            created_at=created_at,
        )
        claimed = {
            key: deepcopy(value)
            for key, value in expected_head.items()
            if key != "headHash"
        }
        claimed.update(
            {
                "stateRevision": expected_head["stateRevision"] + 1,
                "effectiveOwnerGeneration": generation_number,
                "effectiveOwnerGenerationHash": generation["generationHash"],
                "effectiveOwnerKind": "contact_optout",
                "effectivePriority": 3,
                "state": "claimed",
                "leaseOwnerHash": f"{cycle + 40:064x}",
                "leaseUntil": "2026-08-04T13:00:00.000000Z",
                "fencingToken": first_fence,
                "effectiveSettlementHash": (
                    predecessor_settlement["settlementHash"]
                    if predecessor_settlement is not None
                    else None
                ),
                "updatedAt": created_at,
            }
        )
        claimed = self.fixture._rehash_head(claimed)
        settlement = self.module.build_owner_settlement_document(
            generation_document=generation,
            claim_set_document=claim,
            fencing_token=first_fence,
            outcome="contact_optout",
            settled_at=settled_at,
            superseded_effective_settlement_hash=(
                predecessor_settlement["settlementHash"]
                if predecessor_settlement is not None
                else None
            ),
        )
        settled_head = self.module._build_settlement_advanced_head(
            expected_head=claimed,
            generation_document=generation,
            settlement_document=settlement,
        )
        self.fixture._claim_reference(store, claim["requestId"]).create(claim)
        self.fixture._generation_reference(
            store,
            row_id,
            generation_number,
        ).create(generation)
        self.fixture._settlement_reference(
            store,
            row_id,
            generation_number,
        ).create(settlement)
        self.fixture._row_references(store, row_id)[1].set(
            settled_head,
            merge=False,
        )
        return claim, generation, settlement, settled_head, fanout_id

    def _release_to(
        self,
        store,
        *,
        released_generation,
        released_settlement,
        settled_head,
        fanout_id,
        restored_generation,
        restored_settlement,
        released_at,
        cycle,
        row_id=None,
    ):
        row_id = row_id or self.fixture.first
        result = self.module.build_contact_fanout_result_document(
            user_scope_hash=self.fixture.scope,
            fanout_id=fanout_id,
            row_id=row_id,
            obligation_hash=f"{cycle + 50:064x}",
            outcome="release",
            disposition="restore",
            reason_code="exact_predecessor",
            observed_row_head_hash=settled_head["headHash"],
            claim_request_id=None,
            claim_set_hash=None,
            row_generation=None,
            row_settlement_hash=None,
            released_row_generation=released_generation["generation"],
            released_row_settlement_hash=released_settlement[
                "settlementHash"
            ],
            restored_effective_generation=(
                restored_generation["generation"]
                if restored_generation is not None
                else None
            ),
            restored_effective_settlement_hash=(
                restored_settlement["settlementHash"]
                if restored_settlement is not None
                else None
            ),
            created_at=released_at,
        )
        released_head = {
            key: deepcopy(value)
            for key, value in settled_head.items()
            if key != "headHash"
        }
        if restored_generation is None:
            owner_values = {
                "effectiveOwnerGeneration": None,
                "effectiveOwnerGenerationHash": None,
                "effectiveOwnerKind": None,
                "effectivePriority": None,
                "state": "clear",
                "fencingToken": None,
                "effectiveSettlementHash": None,
            }
        else:
            owner_values = {
                "effectiveOwnerGeneration": restored_generation["generation"],
                "effectiveOwnerGenerationHash": restored_generation[
                    "generationHash"
                ],
                "effectiveOwnerKind": restored_generation["ownerKind"],
                "effectivePriority": restored_generation["priority"],
                "state": "settled",
                "fencingToken": restored_settlement["fencingToken"],
                "effectiveSettlementHash": restored_settlement[
                    "settlementHash"
                ],
            }
        released_head.update(
            {
                **owner_values,
                "stateRevision": settled_head["stateRevision"] + 1,
                "leaseOwnerHash": None,
                "leaseUntil": None,
                "latestSettlementHash": released_settlement[
                    "settlementHash"
                ],
                "latestOptOutReleaseResultHash": result[
                    "contactFanoutResultHash"
                ],
                "updatedAt": released_at,
            }
        )
        released_head = self.fixture._rehash_head(released_head)
        user = self.fixture._user_reference(store)
        user.collection("contactOptOutFanoutResults").document(
            f"{fanout_id}--{row_id}"
        ).create(result)
        self.fixture._row_references(store, row_id)[1].set(
            released_head,
            merge=False,
        )
        return result, released_head

    def _seed_released_human(self, *, cycles=1):
        store, bundle, binding = self._seed(owner_kind="terminal")
        (
            _action,
            _human_claim,
            human_generation,
            human_settlement,
            head,
        ) = self.fixture._install_settled_human_owner(
            store,
            self.fixture.first,
        )
        for cycle in range(1, cycles + 1):
            base_minute = 3 + ((cycle - 1) * 3)
            (
                _claim,
                contact_generation,
                contact_settlement,
                settled_head,
                fanout_id,
            ) = self._install_contact_settlement(
                store,
                expected_head=head,
                generation_number=cycle + 1,
                first_fence=cycle + 1,
                predecessor_settlement=human_settlement,
                created_at=(
                    f"2026-08-04T12:{base_minute:02d}:00.000000Z"
                ),
                settled_at=(
                    f"2026-08-04T12:{base_minute + 1:02d}:00.000000Z"
                ),
                cycle=cycle,
            )
            _result, head = self._release_to(
                store,
                released_generation=contact_generation,
                released_settlement=contact_settlement,
                settled_head=settled_head,
                fanout_id=fanout_id,
                restored_generation=human_generation,
                restored_settlement=human_settlement,
                released_at=(
                    f"2026-08-04T12:{base_minute + 2:02d}:00.000000Z"
                ),
                cycle=cycle,
            )
        return store, bundle, binding, human_generation, human_settlement, head

    def _install_human_owner_for_row(
        self,
        store,
        *,
        row_id,
        suffix,
        issued_at,
    ):
        binding = self.module.build_thread_row_binding_document(
            user_scope_hash=self.fixture.scope,
            thread_id=f"thread-human-{suffix}",
            client_id="client-1",
            row_ids=[row_id],
            primary_row_id=row_id,
            created_at=self.fixture.binding_at,
        )
        user = self.fixture._user_reference(store)
        user.collection("threadRowBindings").document(
            binding["threadId"]
        ).create(binding)
        for edge in self.module.build_row_thread_binding_documents(
            thread_binding_document=binding
        ):
            user.collection("rowThreadBindings").document(
                edge["edgeId"]
            ).create(edge)
        result = self.fixture._authority(store).record_operator_decline(
            verified_user_id=self.fixture.user_id,
            thread_id=binding["threadId"],
            actor_scope_hash="5" * 64,
            client_request_id=f"human-{suffix}",
            issued_at=issued_at,
        )
        return (
            result["generations"][0],
            result["settlements"][0],
            result["heads"][0],
        )

    def _claim_must_succeed(self, store, *, bundle, **overrides):
        try:
            return self.fixture._claim(store, bundle=bundle, **overrides)
        except self.module.RowAuthorityError as exc:
            self.fail(
                "valid bounded release-aware history was rejected instead of "
                f"allocating monotonically: {type(exc).__name__}: {exc}"
            )

    def _assert_ambiguous_without_writes(self, store, *, bundle, **overrides):
        before = deepcopy(store.data)
        store.events.clear()
        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.fixture._claim(store, bundle=bundle, **overrides)
        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_next_generation_uses_max_effective_and_latest_historical_generation(self):
        store, bundle, _binding, _generation, _settlement, _head = (
            self._seed_released_human(cycles=1)
        )
        result = self._claim_must_succeed(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:07:00.000000Z",
            lease_until="2026-08-04T12:12:00.000000Z",
        )

        self.assertEqual(3, result["generations"][0]["generation"])
        self.assertEqual(3, result["generations"][0]["firstFencingToken"])

    def test_repeated_release_cycles_never_reuse_generation_or_fence(self):
        store, bundle, _binding, _generation, _settlement, _head = (
            self._seed_released_human(cycles=3)
        )
        result = self._claim_must_succeed(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:12:00.000000Z",
            lease_until="2026-08-04T12:17:00.000000Z",
        )

        self.assertEqual(5, result["generations"][0]["generation"])
        self.assertEqual(5, result["generations"][0]["firstFencingToken"])

    def test_latest_settlement_query_is_descending_bounded_to_two(self):
        store, bundle, _binding = self._seed(owner_kind="terminal")
        query_type = type(
            store.collection("probe").where("rowId", "==", self.fixture.first)
        )
        observed_limits = []
        original_limit = query_type.limit

        def observing_limit(query, count):
            if query._collection.path.endswith("/rowOwnerSettlements"):
                observed_limits.append(count)
            return original_limit(query, count)

        store.events.clear()
        with patch.object(query_type, "limit", observing_limit):
            result = self._claim_must_succeed(store, bundle=bundle)

        self.assertEqual(1, result["generations"][0]["generation"])
        self.assertEqual([2], observed_limits)
        settlement_queries = [
            event
            for event in store.events
            if event[0] == "query"
            and event[1].endswith("/rowOwnerSettlements")
        ]
        self.assertEqual(1, len(settlement_queries))
        self.assertEqual(
            (("rowId", "==", self.fixture.first),),
            settlement_queries[0][2],
        )
        self.assertEqual(("generation",), settlement_queries[0][3])
        self.assertEqual(("DESCENDING",), settlement_queries[0][4])

    def test_duplicate_or_malformed_latest_generation_is_ambiguous(self):
        for case in ("duplicate", "malformed_path"):
            with self.subTest(case=case):
                store, bundle, _binding = self._seed(owner_kind="terminal")
                (
                    _action,
                    _claim,
                    _generation,
                    settlement,
                    _head,
                ) = self.fixture._install_settled_human_owner(
                    store,
                    self.fixture.first,
                )
                user = self.fixture._user_reference(store)
                document_id = (
                    f"{self.fixture.first}--duplicate"
                    if case == "duplicate"
                    else f"malformed--{self.fixture.first}--1"
                )
                user.collection("rowOwnerSettlements").document(
                    document_id
                ).create(settlement)

                self._assert_ambiguous_without_writes(
                    store,
                    bundle=bundle,
                    created_at="2026-08-04T12:00:04.000000Z",
                    lease_until="2026-08-04T12:06:00.000000Z",
                )

    def _history_queries(self, store):
        return [
            event
            for event in store.events
            if event[0] == "query"
            and event[1].endswith("/rowOwnerSettlements")
        ]

    @staticmethod
    def _exact_equality_queries(store, *, collection_name, equality_field):
        return [
            event
            for event in store.events
            if event[0] == "query"
            and event[1].endswith(f"/{collection_name}")
            and any(
                field_path == equality_field
                for field_path, _operator, _expected in event[2]
            )
        ]

    def _assert_rejected_after_history_query(
        self,
        store,
        *,
        bundle,
        created_at="2026-08-04T12:10:00.000000Z",
        lease_until="2026-08-04T12:15:00.000000Z",
    ):
        before = deepcopy(store.data)
        store.events.clear()
        with self.assertRaises(self.module.RowAuthorityError):
            self.fixture._claim(
                store,
                bundle=bundle,
                created_at=created_at,
                lease_until=lease_until,
            )
        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))
        self.assertEqual(
            1,
            len(self._history_queries(store)),
            "row-history ambiguity must be decided from one bounded query",
        )

    def _seed_latest_pair_violation(self, *, case):
        store, bundle, _binding = self._seed(owner_kind="terminal")
        _identity_ref, head_ref = self.fixture._row_references(
            store,
            self.fixture.first,
        )
        initial_head = deepcopy(store.data[head_ref.path])
        (
            _claim,
            _generation,
            first_settlement,
            first_head,
            _fanout_id,
        ) = self._install_contact_settlement(
            store,
            expected_head=initial_head,
            generation_number=1,
            first_fence=1,
            predecessor_settlement=None,
            created_at="2026-08-04T12:00:03.000000Z",
            settled_at="2026-08-04T12:00:04.000000Z",
            cycle=61,
        )
        generation_number = 3 if case == "generation_gap" else 2
        first_fence = 2 if case == "generation_gap" else 1
        self._install_contact_settlement(
            store,
            expected_head=first_head,
            generation_number=generation_number,
            first_fence=first_fence,
            predecessor_settlement=first_settlement,
            created_at="2026-08-04T12:00:05.000000Z",
            settled_at="2026-08-04T12:00:06.000000Z",
            cycle=62,
        )
        return store, bundle

    def _seed_invalid_unsettled_current(self, *, case):
        store, bundle, _binding = self._seed(owner_kind="terminal")
        (
            _action,
            _claim,
            _human_generation,
            human_settlement,
            human_head,
        ) = self.fixture._install_settled_human_owner(
            store,
            self.fixture.first,
        )
        generation_number = 3 if case == "generation_gap" else 2
        first_fence = 1 if case == "stale_generation_fence" else 2
        (
            _contact_claim,
            contact_generation,
            _contact_settlement,
            settled_head,
            _fanout_id,
        ) = self._install_contact_settlement(
            store,
            expected_head=human_head,
            generation_number=generation_number,
            first_fence=first_fence,
            predecessor_settlement=human_settlement,
            created_at="2026-08-04T12:00:04.000000Z",
            settled_at="2026-08-04T12:00:05.000000Z",
            cycle=70,
        )
        self.fixture._settlement_reference(
            store,
            self.fixture.first,
            generation_number,
        ).delete()
        pending_head = {
            key: deepcopy(value)
            for key, value in settled_head.items()
            if key != "headHash"
        }
        pending_head.update(
            {
                "state": "claimed",
                "leaseOwnerHash": "7" * 64,
                "leaseUntil": "2026-08-04T13:00:00.000000Z",
                "fencingToken": (
                    1 if case == "stale_head_fence" else first_fence
                ),
                "latestSettlementHash": human_settlement["settlementHash"],
                "effectiveSettlementHash": human_settlement[
                    "settlementHash"
                ],
            }
        )
        pending_head = self.fixture._rehash_head(pending_head)
        self.fixture._row_references(store, self.fixture.first)[1].set(
            pending_head,
            merge=False,
        )
        self.assertEqual(
            generation_number,
            contact_generation["generation"],
        )
        return store, bundle

    def _seed_active_dominated_bridge(self):
        store, bundle, _binding = self._seed(owner_kind="terminal")
        self.fixture._install_owner(
            store,
            self.fixture.first,
            owner_kind="human_decision",
        )
        first = self.fixture._claim(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:00:03.000000Z",
            lease_until="2026-08-04T12:06:00.000000Z",
        )
        self.assertEqual("created", first["disposition"])
        self.assertEqual(2, first["generations"][0]["generation"])
        self.assertEqual(
            "dominated",
            first["predecessorSettlements"][0]["outcome"],
        )
        second_bundle = self.fixture._seed_b1_bundle(
            store,
            owner_kind="terminal",
            source_id="second-terminal-source",
        )
        return store, second_bundle, first

    def test_latest_pair_rejects_generation_gap_or_fence_regression(self):
        for case in ("generation_gap", "fence_regression"):
            with self.subTest(case=case):
                store, bundle = self._seed_latest_pair_violation(case=case)
                self._assert_rejected_after_history_query(
                    store,
                    bundle=bundle,
                    created_at="2026-08-04T12:00:07.000000Z",
                    lease_until="2026-08-04T12:12:00.000000Z",
                )

    def test_current_unsettled_generation_rejects_gap_or_stale_fence(self):
        for case in (
            "generation_gap",
            "stale_generation_fence",
            "stale_head_fence",
        ):
            with self.subTest(case=case):
                store, bundle = self._seed_invalid_unsettled_current(case=case)
                self._assert_rejected_after_history_query(
                    store,
                    bundle=bundle,
                    created_at="2026-08-04T12:00:06.000000Z",
                    lease_until="2026-08-04T12:11:00.000000Z",
                )

    def test_released_head_requires_exact_result_bridge(self):
        for case in (
            "missing",
            "duplicate",
            "hash_drift",
            "restored_owner_mismatch",
        ):
            with self.subTest(case=case):
                (
                    store,
                    bundle,
                    _binding,
                    _human_generation,
                    _human_settlement,
                    _head,
                ) = self._seed_released_human(cycles=1)
                result_paths = [
                    path
                    for path in store.data
                    if "/contactOptOutFanoutResults/" in path
                ]
                self.assertEqual(1, len(result_paths))
                result_path = result_paths[0]
                result = deepcopy(store.data[result_path])
                _identity_ref, head_ref = self.fixture._row_references(
                    store,
                    self.fixture.first,
                )
                if case == "missing":
                    del store.data[result_path]
                elif case == "duplicate":
                    user = self.fixture._user_reference(store)
                    user.collection("contactOptOutFanoutResults").document(
                        f"duplicate--{self.fixture.first}"
                    ).create(result)
                elif case == "hash_drift":
                    drifted = deepcopy(store.data[head_ref.path])
                    drifted["latestOptOutReleaseResultHash"] = "f" * 64
                    head_ref.set(
                        self.fixture._rehash_head(drifted),
                        merge=False,
                    )
                else:
                    mismatch = self.module.build_contact_fanout_result_document(
                        user_scope_hash=result["userScopeHash"],
                        fanout_id=result["fanoutId"],
                        row_id=result["rowId"],
                        obligation_hash=result["obligationHash"],
                        outcome="release",
                        disposition="restore",
                        reason_code="exact_predecessor",
                        observed_row_head_hash=result["observedRowHeadHash"],
                        claim_request_id=None,
                        claim_set_hash=None,
                        row_generation=None,
                        row_settlement_hash=None,
                        released_row_generation=result[
                            "releasedRowGeneration"
                        ],
                        released_row_settlement_hash=result[
                            "releasedRowSettlementHash"
                        ],
                        restored_effective_generation=None,
                        restored_effective_settlement_hash=None,
                        created_at=result["createdAt"],
                    )
                    store.data[result_path] = mismatch
                    drifted = deepcopy(store.data[head_ref.path])
                    drifted["latestOptOutReleaseResultHash"] = mismatch[
                        "contactFanoutResultHash"
                    ]
                    head_ref.set(
                        self.fixture._rehash_head(drifted),
                        merge=False,
                    )
                self._assert_rejected_after_history_query(
                    store,
                    bundle=bundle,
                    created_at="2026-08-04T12:07:00.000000Z",
                    lease_until="2026-08-04T12:12:00.000000Z",
                )

    def test_normal_pending_supersession_uses_dominated_bridge_not_release_bridge(self):
        store, bundle, first = self._seed_active_dominated_bridge()
        store.events.clear()

        result = self._claim_must_succeed(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:00:04.000000Z",
            lease_until="2026-08-04T12:07:00.000000Z",
        )

        self.assertEqual("dominated", result["disposition"])
        self.assertEqual(first["heads"], result["heads"])
        self.assertEqual(1, len(self._history_queries(store)))
        self.assertEqual(
            [],
            [
                event
                for event in store.events
                if event[0] == "query"
                and event[1].endswith("/contactOptOutFanoutResults")
            ],
        )

    def test_current_pending_generation_can_exceed_latest_settlement(self):
        (
            store,
            bundle,
            _binding,
            human_generation,
            human_settlement,
            released_head,
        ) = self._seed_released_human(cycles=3)
        (
            _claim,
            pending_generation,
            _settlement,
            settled_head,
            _fanout_id,
        ) = self._install_contact_settlement(
            store,
            expected_head=released_head,
            generation_number=5,
            first_fence=5,
            predecessor_settlement=human_settlement,
            created_at="2026-08-04T12:12:00.000000Z",
            settled_at="2026-08-04T12:13:00.000000Z",
            cycle=80,
        )
        self.fixture._settlement_reference(
            store,
            self.fixture.first,
            5,
        ).delete()
        pending_head = {
            key: deepcopy(value)
            for key, value in settled_head.items()
            if key != "headHash"
        }
        pending_head.update(
            {
                "state": "claimed",
                "leaseOwnerHash": "8" * 64,
                "leaseUntil": "2026-08-04T13:00:00.000000Z",
                "fencingToken": 5,
                "latestSettlementHash": released_head[
                    "latestSettlementHash"
                ],
                "effectiveSettlementHash": human_settlement[
                    "settlementHash"
                ],
                "latestOptOutReleaseResultHash": released_head[
                    "latestOptOutReleaseResultHash"
                ],
            }
        )
        pending_head = self.fixture._rehash_head(pending_head)
        self.fixture._row_references(store, self.fixture.first)[1].set(
            pending_head,
            merge=False,
        )
        self.assertEqual(5, pending_generation["generation"])
        self.assertEqual(1, human_generation["generation"])
        store.events.clear()

        result = self._claim_must_succeed(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:14:00.000000Z",
            lease_until="2026-08-04T12:19:00.000000Z",
        )

        self.assertEqual("dominated", result["disposition"])
        self.assertEqual(pending_head, result["heads"][0])
        self.assertEqual(1, len(self._history_queries(store)))

    def test_candidate_generation_or_settlement_collision_writes_nothing(self):
        for collection in ("rowOwnerGenerations", "rowOwnerSettlements"):
            with self.subTest(collection=collection):
                (
                    store,
                    bundle,
                    _binding,
                    _generation,
                    _settlement,
                    _head,
                ) = self._seed_released_human(cycles=1)
                user = self.fixture._user_reference(store)
                candidate = user.collection(collection).document(
                    f"{self.fixture.first}--3"
                )
                candidate.create({"partial": "candidate collision"})
                before = deepcopy(store.data)
                store.events.clear()

                try:
                    self.fixture._claim(
                        store,
                        bundle=bundle,
                        created_at="2026-08-04T12:07:00.000000Z",
                        lease_until="2026-08-04T12:12:00.000000Z",
                    )
                except self.module.RowAuthorityAmbiguous:
                    pass
                except self.module.RowAuthorityError as exc:
                    self.fail(
                        "candidate collision must be rejected from the "
                        "release-aware candidate paths, not by rejecting the "
                        f"valid restored history first: {type(exc).__name__}: "
                        f"{exc}"
                    )
                else:
                    self.fail("candidate ownership collision was accepted")

                self.assertEqual(before, store.data)
                self.assertEqual([], self.fixture._write_events(store))
                candidate_paths = {
                    self.fixture._generation_reference(
                        store,
                        self.fixture.first,
                        3,
                    ).path,
                    self.fixture._settlement_reference(
                        store,
                        self.fixture.first,
                        3,
                    ).path,
                }
                observed_gets = {
                    event[1]
                    for event in store.events
                    if event[0] == "get"
                }
                self.assertTrue(
                    candidate_paths.issubset(observed_gets),
                    "release-aware candidate generation and settlement must "
                    "both be read before any write",
                )

    def _settlement_store_fixture(self):
        fixture_type = self.ownership.RowSettlementStoreTests
        fixture_type.setUpClass()
        fixture = fixture_type(methodName="runTest")
        fixture.setUp()
        return fixture

    def _source_link_store_fixture(self):
        fixture_type = self.ownership.RowSourceSettlementLinkTests
        fixture_type.setUpClass()
        fixture = fixture_type(methodName="runTest")
        fixture.setUp()
        return fixture

    def _append_release_cycles(
        self,
        store,
        *,
        starting_head,
        restored_generation,
        restored_settlement,
        cycles,
    ):
        head = starting_head
        for cycle in range(1, cycles + 1):
            base_minute = 4 + ((cycle - 1) * 3)
            (
                _claim,
                released_generation,
                released_settlement,
                settled_head,
                fanout_id,
            ) = self._install_contact_settlement(
                store,
                expected_head=head,
                generation_number=cycle + 1,
                first_fence=cycle + 1,
                predecessor_settlement=restored_settlement,
                created_at=(
                    f"2026-08-04T12:{base_minute:02d}:00.000000Z"
                ),
                settled_at=(
                    f"2026-08-04T12:{base_minute + 1:02d}:00.000000Z"
                ),
                cycle=100 + cycle,
            )
            _result, head = self._release_to(
                store,
                released_generation=released_generation,
                released_settlement=released_settlement,
                settled_head=settled_head,
                fanout_id=fanout_id,
                restored_generation=restored_generation,
                restored_settlement=restored_settlement,
                released_at=(
                    f"2026-08-04T12:{base_minute + 2:02d}:00.000000Z"
                ),
                cycle=100 + cycle,
            )
        return head

    def _install_pending_contact_after_release(
        self,
        store,
        *,
        released_head,
        predecessor_settlement,
        generation_number,
        created_at,
        settled_at,
    ):
        (
            _claim,
            generation,
            _discarded_settlement,
            discarded_settled_head,
            _fanout_id,
        ) = self._install_contact_settlement(
            store,
            expected_head=released_head,
            generation_number=generation_number,
            first_fence=generation_number,
            predecessor_settlement=predecessor_settlement,
            created_at=created_at,
            settled_at=settled_at,
            cycle=120 + generation_number,
        )
        self.fixture._settlement_reference(
            store,
            self.fixture.first,
            generation_number,
        ).delete()
        pending = {
            key: deepcopy(value)
            for key, value in discarded_settled_head.items()
            if key != "headHash"
        }
        pending.update(
            {
                "state": "claimed",
                "leaseOwnerHash": "d" * 64,
                "leaseUntil": "2026-08-04T13:00:00.000000Z",
                "fencingToken": generation_number,
                "latestSettlementHash": released_head[
                    "latestSettlementHash"
                ],
                "effectiveSettlementHash": released_head[
                    "effectiveSettlementHash"
                ],
                "latestOptOutReleaseResultHash": released_head[
                    "latestOptOutReleaseResultHash"
                ],
            }
        )
        pending = self.fixture._rehash_head(pending)
        self.fixture._row_references(store, self.fixture.first)[1].set(
            pending,
            merge=False,
        )
        return generation, pending

    def _release_linkable_state(self, link_fixture, *, cycles=1):
        state = link_fixture._seed_linkable()
        released_head = self._append_release_cycles(
            state["store"],
            starting_head=state["head"],
            restored_generation=state["generation"],
            restored_settlement=state["b2Settlement"],
            cycles=cycles,
        )
        return state, released_head

    def _assert_link_ambiguity_after_history_query(
        self,
        link_fixture,
        state,
        *,
        linked_at,
    ):
        store = state["store"]
        before = deepcopy(store.data)
        store.events.clear()
        try:
            link_fixture._link(state, linked_at=linked_at)
        except self.module.RowAuthorityAmbiguous:
            pass
        except self.module.RowAuthorityError as exc:
            self.fail(
                "invalid historical proof must be zero-write ambiguity, not "
                f"{type(exc).__name__}: {exc}"
            )
        else:
            self.fail("invalid historical source-link proof was accepted")
        self.assertEqual(before, store.data)
        self.assertEqual([], link_fixture._writes(store))
        self.assertEqual(
            1,
            len(self._history_queries(store)),
            "historical link proof must include one bounded settlement query",
        )

    def test_settlement_retry_after_release_reads_historical_generation(self):
        settlement_fixture = self._settlement_store_fixture()
        store, _binding, claimed = settlement_fixture._seed_terminal()
        expected_head = claimed["heads"][0]
        settled = settlement_fixture._settle(store, expected_head)
        released_head = self._append_release_cycles(
            store,
            starting_head=settled["head"],
            restored_generation=settled["generation"],
            restored_settlement=settled["settlement"],
            cycles=1,
        )
        _pending_generation, pending_head = (
            self._install_pending_contact_after_release(
                store,
                released_head=released_head,
                predecessor_settlement=settled["settlement"],
                generation_number=3,
                created_at="2026-08-04T12:07:00.000000Z",
                settled_at="2026-08-04T12:08:00.000000Z",
            )
        )
        before = deepcopy(store.data)
        store.events.clear()

        try:
            replay = settlement_fixture._settle(store, expected_head)
        except self.module.RowAuthorityError as exc:
            self.fail(
                "exact terminal settlement retry must read generation 1 as "
                f"historical authority after release: {type(exc).__name__}: "
                f"{exc}"
            )

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(settled["generation"], replay["generation"])
        self.assertEqual(settled["settlement"], replay["settlement"])
        self.assertEqual(pending_head, replay["head"])
        self.assertEqual(before, store.data)
        self.assertEqual([], settlement_fixture._writes(store))
        self.assertEqual(1, len(self._history_queries(store)))
        exact_paths = {
            self.fixture._generation_reference(
                store,
                self.fixture.first,
                1,
            ).path,
            self.fixture._claim_reference(
                store,
                settled["generation"]["requestId"],
            ).path,
            self.fixture._settlement_reference(
                store,
                self.fixture.first,
                1,
            ).path,
        }
        self.assertTrue(
            exact_paths.issubset(
                {
                    event[1]
                    for event in store.events
                    if event[0] == "get"
                }
            )
        )

    def test_settlement_retry_immediately_after_release_keeps_released_head(self):
        settlement_fixture = self._settlement_store_fixture()
        store, _binding, claimed = settlement_fixture._seed_terminal()
        expected_head = claimed["heads"][0]
        settled = settlement_fixture._settle(store, expected_head)
        released_head = self._append_release_cycles(
            store,
            starting_head=settled["head"],
            restored_generation=settled["generation"],
            restored_settlement=settled["settlement"],
            cycles=1,
        )
        before = deepcopy(store.data)
        store.events.clear()

        replay = settlement_fixture._settle(store, expected_head)

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(settled["generation"], replay["generation"])
        self.assertEqual(settled["settlement"], replay["settlement"])
        self.assertEqual(released_head, replay["head"])
        self.assertEqual(before, store.data)
        self.assertEqual([], settlement_fixture._writes(store))

    def test_operator_decline_retry_after_clear_release_uses_exact_bridge(self):
        fixture_type = self.ownership.RowOperatorDeclineStoreTests
        fixture_type.setUpClass()
        operator_fixture = fixture_type(methodName="runTest")
        operator_fixture.setUp()
        store, _binding = operator_fixture._seed_clear()
        head_ref = self.fixture._row_references(
            store,
            self.fixture.first,
        )[1]
        clear_head = deepcopy(store.data[head_ref.path])
        (
            _claim,
            released_generation,
            released_settlement,
            settled_head,
            fanout_id,
        ) = self._install_contact_settlement(
            store,
            expected_head=clear_head,
            generation_number=1,
            first_fence=1,
            predecessor_settlement=None,
            created_at="2026-08-04T12:00:03.000000Z",
            settled_at="2026-08-04T12:00:04.000000Z",
            cycle=170,
        )
        self._release_to(
            store,
            released_generation=released_generation,
            released_settlement=released_settlement,
            settled_head=settled_head,
            fanout_id=fanout_id,
            restored_generation=None,
            restored_settlement=None,
            released_at="2026-08-04T12:00:05.000000Z",
            cycle=170,
        )
        first = operator_fixture._decline(
            store,
            issued_at="2026-08-04T12:00:06.000000Z",
        )
        before = deepcopy(store.data)
        store.events.clear()

        replay = operator_fixture._decline(
            store,
            issued_at="2026-08-04T12:00:06.000000Z",
        )

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(first["claimSet"], replay["claimSet"])
        self.assertEqual(first["generations"], replay["generations"])
        self.assertEqual(first["settlements"], replay["settlements"])
        self.assertEqual(first["heads"], replay["heads"])
        self.assertEqual(before, store.data)
        self.assertEqual([], operator_fixture._writes(store))

    def test_dominated_claim_replay_winner_query_orders_by_document_name(self):
        store, bundle, _binding = self._seed(owner_kind="terminal")
        self.fixture._install_contact_owner(store, self.fixture.first)
        first = self.fixture._claim(store, bundle=bundle)
        self.assertEqual("dominated", first["disposition"])
        store.events.clear()

        replay = self.fixture._claim(store, bundle=bundle)

        self.assertEqual("already_applied", replay["disposition"])
        queries = self._exact_equality_queries(
            store,
            collection_name="rowOwnerGenerations",
            equality_field="generationHash",
        )
        self.assertEqual(1, len(queries))
        self.assertEqual(("__name__",), queries[0][3])

    def test_settlement_predecessor_query_orders_by_document_name(self):
        store, bundle, _binding, _generation, _settlement, _head = (
            self._seed_released_human(cycles=2)
        )
        claimed = self.fixture._claim(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:09:00.000000Z",
            lease_until="2026-08-04T12:14:00.000000Z",
        )
        settlement_fixture = self._settlement_store_fixture()
        store.events.clear()

        result = settlement_fixture._settle(
            store,
            claimed["heads"][0],
            settled_at="2026-08-04T12:10:00.000000Z",
        )

        self.assertEqual("settled", result["disposition"])
        queries = self._exact_equality_queries(
            store,
            collection_name="rowOwnerSettlements",
            equality_field="settlementHash",
        )
        self.assertEqual(1, len(queries))
        self.assertEqual(("__name__",), queries[0][3])

    def test_settlement_predecessor_query_rejects_wrong_snapshot_path(self):
        store, bundle, _binding, _generation, predecessor, _head = (
            self._seed_released_human(cycles=2)
        )
        claimed = self.fixture._claim(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:09:00.000000Z",
            lease_until="2026-08-04T12:14:00.000000Z",
        )
        settlement_fixture = self._settlement_store_fixture()
        wrong_reference = self.fixture._user_reference(store).collection(
            "rowOwnerSettlements"
        ).document(f"{self.fixture.first}--wrong-predecessor")
        wrong_reference.create(deepcopy(predecessor))
        transaction_type = type(store.transaction())
        original_get_query = transaction_type.get_query

        def return_only_wrong_predecessor(transaction, query):
            snapshots = original_get_query(transaction, query)
            if query._collection.path.endswith(
                "/rowOwnerSettlements"
            ) and any(
                field_path == "settlementHash"
                for field_path, _operator, _expected in query._filters
            ):
                return [
                    snapshot
                    for snapshot in snapshots
                    if snapshot.reference.path == wrong_reference.path
                ]
            return snapshots

        before = deepcopy(store.data)
        store.events.clear()

        with patch.object(
            transaction_type,
            "get_query",
            return_only_wrong_predecessor,
        ), self.assertRaises(self.module.RowAuthorityAmbiguous):
            settlement_fixture._settle(
                store,
                claimed["heads"][0],
                settled_at="2026-08-04T12:10:00.000000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], settlement_fixture._writes(store))

    def test_operator_action_settlement_query_orders_by_document_name(self):
        fixture_type = self.ownership.RowOperatorDeclineStoreTests
        fixture_type.setUpClass()
        operator_fixture = fixture_type(methodName="runTest")
        operator_fixture.setUp()
        store, _binding = operator_fixture._seed_clear()
        store.events.clear()

        result = operator_fixture._decline(store)

        self.assertEqual("declined", result["disposition"])
        queries = self._exact_equality_queries(
            store,
            collection_name="rowOwnerSettlements",
            equality_field="operatorActionHash",
        )
        self.assertEqual(1, len(queries))
        self.assertEqual(("__name__",), queries[0][3])

    def test_operator_dominated_replay_winner_query_orders_by_document_name(self):
        fixture_type = self.ownership.RowOperatorDeclineStoreTests
        fixture_type.setUpClass()
        operator_fixture = fixture_type(methodName="runTest")
        operator_fixture.setUp()
        store, _binding, _terminal = operator_fixture._seed_terminal()
        first = operator_fixture._decline(store)
        self.assertEqual("dominated", first["disposition"])
        store.events.clear()

        replay = operator_fixture._decline(store)

        self.assertEqual("already_applied", replay["disposition"])
        queries = self._exact_equality_queries(
            store,
            collection_name="rowOwnerGenerations",
            equality_field="generationHash",
        )
        self.assertEqual(1, len(queries))
        self.assertEqual(("__name__",), queries[0][3])

    def test_settlement_commit_uncertainty_rechecks_bounded_query_snapshot(self):
        settlement_fixture = self._settlement_store_fixture()
        store, _binding, claimed = settlement_fixture._seed_terminal()
        expected_head = claimed["heads"][0]
        settlement_ref = self.fixture._settlement_reference(
            store,
            self.fixture.first,
            1,
        )
        duplicate_ref = self.fixture._user_reference(store).collection(
            "rowOwnerSettlements"
        ).document(f"{self.fixture.first}--settlement-commit-uncertainty-duplicate")

        def apply_then_drift_query(transaction, callback):
            transaction._begin()
            callback(transaction)
            transaction._commit()
            duplicate_ref.create(deepcopy(store.data[settlement_ref.path]))
            raise RuntimeError("settlement commit outcome query drifted")

        try:
            settlement_fixture._settle(
                store,
                expected_head,
                executor=apply_then_drift_query,
            )
        except self.module.RowAuthorityAmbiguous:
            pass
        except self.module.RowAuthorityError as exc:
            self.fail(
                "post-commit bounded-query drift must be ambiguity, not "
                f"{type(exc).__name__}: {exc}"
            )
        else:
            self.fail(
                "settlement commit readback ignored a changed bounded query "
                "snapshot"
            )
        self.assertIn(duplicate_ref.path, store.data)

    def test_source_link_after_release_updates_only_latest_link_pointer(self):
        link_fixture = self._source_link_store_fixture()
        state, released_head = self._release_linkable_state(link_fixture)
        store = state["store"]
        linked_at = "2026-08-04T12:07:00.000000Z"
        expected_link = link_fixture._expected_link(
            state,
            linked_at=linked_at,
        )
        store.events.clear()

        try:
            result = link_fixture._link(state, linked_at=linked_at)
        except self.module.RowAuthorityError as exc:
            self.fail(
                "delayed source link after release must validate historical "
                f"authority: {type(exc).__name__}: {exc}"
            )

        self.assertEqual("linked", result["disposition"])
        self.assertEqual(expected_link, result["sourceSettlementLink"])
        changed_fields = {
            key
            for key in released_head
            if released_head[key] != result["head"][key]
        }
        self.assertEqual(
            {
                "stateRevision",
                "latestSourceSettlementLinkHash",
                "headHash",
                "updatedAt",
            },
            changed_fields,
        )
        self.assertEqual(
            expected_link["sourceSettlementLinkHash"],
            result["head"]["latestSourceSettlementLinkHash"],
        )
        self.assertEqual(
            released_head["latestOptOutReleaseResultHash"],
            result["head"]["latestOptOutReleaseResultHash"],
        )
        self.assertEqual(1, len(self._history_queries(store)))
        self.assertEqual(
            [
                (
                    "create",
                    link_fixture._link_reference(store).path,
                    expected_link,
                    False,
                ),
                (
                    "set",
                    link_fixture._head_reference(store).path,
                    result["head"],
                    False,
                ),
            ],
            link_fixture._writes(store),
        )

    def test_source_link_after_newer_generation_validates_exact_old_artifacts(self):
        link_fixture = self._source_link_store_fixture()
        state, released_head = self._release_linkable_state(
            link_fixture,
            cycles=3,
        )
        store = state["store"]
        store.events.clear()

        try:
            result = link_fixture._link(
                state,
                linked_at="2026-08-04T12:13:00.000000Z",
            )
        except self.module.RowAuthorityError as exc:
            self.fail(
                "generation-1 source link must validate exact old artifacts "
                f"after generation 4: {type(exc).__name__}: {exc}"
            )

        self.assertEqual("linked", result["disposition"])
        self.assertEqual(1, result["sourceSettlementLink"]["generation"])
        self.assertEqual(
            released_head["effectiveOwnerGeneration"],
            result["head"]["effectiveOwnerGeneration"],
        )
        self.assertEqual(
            released_head["latestSettlementHash"],
            result["head"]["latestSettlementHash"],
        )
        self.assertEqual(1, len(self._history_queries(store)))
        observed_gets = {
            event[1] for event in store.events if event[0] == "get"
        }
        exact_old_paths = {
            self.fixture._generation_reference(
                store,
                self.fixture.first,
                1,
            ).path,
            self.fixture._claim_reference(
                store,
                state["generation"]["requestId"],
            ).path,
            self.fixture._settlement_reference(
                store,
                self.fixture.first,
                1,
            ).path,
        }
        self.assertTrue(exact_old_paths.issubset(observed_gets))

    def test_source_link_accepts_released_contact_before_equal_priority_successor(self):
        link_fixture = self._source_link_store_fixture()
        state = link_fixture._seed_linkable(origin="contact_fanout")
        store = state["store"]
        _release_result, released_head = self._release_to(
            store,
            released_generation=state["generation"],
            released_settlement=state["b2Settlement"],
            settled_head=state["head"],
            fanout_id=state["claim"]["fanoutId"],
            restored_generation=None,
            restored_settlement=None,
            released_at="2026-08-04T12:00:04.000000Z",
            cycle=180,
        )
        (
            _claim,
            successor_generation,
            successor_settlement,
            successor_head,
            _fanout_id,
        ) = self._install_contact_settlement(
            store,
            expected_head=released_head,
            generation_number=2,
            first_fence=2,
            predecessor_settlement=None,
            created_at="2026-08-04T12:00:05.000000Z",
            settled_at="2026-08-04T12:00:06.000000Z",
            cycle=181,
        )
        store.events.clear()

        result = link_fixture._link(
            state,
            linked_at="2026-08-04T12:00:07.000000Z",
        )

        self.assertEqual("linked", result["disposition"])
        self.assertEqual(1, result["sourceSettlementLink"]["generation"])
        self.assertEqual(
            successor_generation["generation"],
            result["head"]["effectiveOwnerGeneration"],
        )
        self.assertEqual(
            successor_settlement["settlementHash"],
            result["head"]["effectiveSettlementHash"],
        )
        self.assertEqual(
            successor_head["latestOptOutReleaseResultHash"],
            result["head"]["latestOptOutReleaseResultHash"],
        )
        self.assertEqual(1, len(self._history_queries(store)))

    def test_source_link_rejects_release_skipping_pair_without_exact_result(self):
        link_fixture = self._source_link_store_fixture()
        state = link_fixture._seed_linkable(origin="contact_fanout")
        store = state["store"]
        release_result, released_head = self._release_to(
            store,
            released_generation=state["generation"],
            released_settlement=state["b2Settlement"],
            settled_head=state["head"],
            fanout_id=state["claim"]["fanoutId"],
            restored_generation=None,
            restored_settlement=None,
            released_at="2026-08-04T12:00:04.000000Z",
            cycle=182,
        )
        self._install_contact_settlement(
            store,
            expected_head=released_head,
            generation_number=2,
            first_fence=2,
            predecessor_settlement=None,
            created_at="2026-08-04T12:00:05.000000Z",
            settled_at="2026-08-04T12:00:06.000000Z",
            cycle=183,
        )
        release_paths = [
            path
            for path, document in store.data.items()
            if "/contactOptOutFanoutResults/" in path
            and document.get("contactFanoutResultHash")
            == release_result["contactFanoutResultHash"]
        ]
        self.assertEqual(1, len(release_paths))
        del store.data[release_paths[0]]
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            link_fixture._link(
                state,
                linked_at="2026-08-04T12:00:07.000000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], link_fixture._writes(store))

    def test_historical_link_missing_duplicate_or_future_proof_is_ambiguous(self):
        for case in ("missing", "duplicate", "future"):
            with self.subTest(case=case):
                link_fixture = self._source_link_store_fixture()
                if case == "future":
                    state = link_fixture._seed_linkable(
                        b1_settled_at=self.ownership.datetime(
                            2026,
                            8,
                            4,
                            12,
                            20,
                            tzinfo=self.ownership.timezone.utc,
                        )
                    )
                    self._append_release_cycles(
                        state["store"],
                        starting_head=state["head"],
                        restored_generation=state["generation"],
                        restored_settlement=state["b2Settlement"],
                        cycles=1,
                    )
                else:
                    state, _released_head = self._release_linkable_state(
                        link_fixture
                    )
                store = state["store"]
                if case == "missing":
                    del store.data[
                        self.fixture._generation_reference(
                            store,
                            self.fixture.first,
                            1,
                        ).path
                    ]
                elif case == "duplicate":
                    latest = deepcopy(
                        store.data[
                            self.fixture._settlement_reference(
                                store,
                                self.fixture.first,
                                2,
                            ).path
                        ]
                    )
                    self.fixture._user_reference(store).collection(
                        "rowOwnerSettlements"
                    ).document(
                        f"{self.fixture.first}--duplicate-2"
                    ).create(latest)
                self._assert_link_ambiguity_after_history_query(
                    link_fixture,
                    state,
                    linked_at="2026-08-04T12:13:00.000000Z",
                )

    def test_nonreleased_b2b_claim_and_link_vectors_are_byte_identical(self):
        link_fixture = self._source_link_store_fixture()
        state = link_fixture._seed_linkable()
        store = state["store"]
        store.events.clear()

        result = link_fixture._link(state)

        vectors = {
            "claim": state["claim"],
            "generation": state["generation"],
            "settlement": state["b2Settlement"],
            "link": result["sourceSettlementLink"],
        }
        expected_byte_hashes = {
            "claim": (
                "4fe070f5f190b4b6b306fe1a03b7d73b2834cd37c30bf7f2800b887439aaeb4f"
            ),
            "generation": (
                "8d8b844ea203d6c6273041cb85c3d326190a3d844734df9e5c5d9d183fab264d"
            ),
            "settlement": (
                "6c3b6356dd687c395f85b8440ddec6034282ff98f313e6e5cf1fdf3f4dec45ab"
            ),
            "link": (
                "ba6c87eb84491c61550c5462917c46092b03c68f0e5a2e26705030d7881592fc"
            ),
        }
        self.assertEqual(
            expected_byte_hashes,
            {
                name: hashlib.sha256(
                    json.dumps(
                        document,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                ).hexdigest()
                for name, document in vectors.items()
            },
        )
        self.assertEqual(1, len(self._history_queries(store)))

    def _seed_released_clear(self):
        store, bundle, binding = self._seed(owner_kind="terminal")
        _identity_ref, head_ref = self.fixture._row_references(
            store,
            self.fixture.first,
        )
        clear_head = deepcopy(store.data[head_ref.path])
        (
            _claim,
            released_generation,
            released_settlement,
            settled_head,
            fanout_id,
        ) = self._install_contact_settlement(
            store,
            expected_head=clear_head,
            generation_number=1,
            first_fence=1,
            predecessor_settlement=None,
            created_at="2026-08-04T12:00:03.000000Z",
            settled_at="2026-08-04T12:00:04.000000Z",
            cycle=141,
        )
        result, released_head = self._release_to(
            store,
            released_generation=released_generation,
            released_settlement=released_settlement,
            settled_head=settled_head,
            fanout_id=fanout_id,
            restored_generation=None,
            restored_settlement=None,
            released_at="2026-08-04T12:00:05.000000Z",
            cycle=141,
        )
        return {
            "store": store,
            "bundle": bundle,
            "binding": binding,
            "releasedGeneration": released_generation,
            "releasedSettlement": released_settlement,
            "releaseResult": result,
            "head": released_head,
        }

    def _bounded_state_from_store(self, store):
        row_id = self.fixture.first
        identity_ref, head_ref = self.fixture._row_references(store, row_id)
        identity = deepcopy(store.data[identity_ref.path])
        head = deepcopy(store.data[head_ref.path])
        user = self.fixture._user_reference(store)
        settlements_path = user.collection("rowOwnerSettlements").path
        settlement_entries = [
            {"path": path, "document": deepcopy(document)}
            for path, document in store.data.items()
            if path.rsplit("/", 1)[0] == settlements_path
            and type(document) is dict
            and document.get("rowId") == row_id
        ]
        settlement_entries.sort(
            key=lambda entry: (
                entry["document"]["generation"],
                entry["path"],
            ),
            reverse=True,
        )
        current_generation = None
        current_claim = None
        current_settlement = None
        current_number = head["effectiveOwnerGeneration"]
        if current_number is not None:
            current_generation = deepcopy(
                store.data[
                    self.fixture._generation_reference(
                        store,
                        row_id,
                        current_number,
                    ).path
                ]
            )
            current_claim = deepcopy(
                store.data[
                    self.fixture._claim_reference(
                        store,
                        current_generation["requestId"],
                    ).path
                ]
            )
            current_settlement = deepcopy(
                store.data.get(
                    self.fixture._settlement_reference(
                        store,
                        row_id,
                        current_number,
                    ).path
                )
            )
        release_result = None
        release_result_path = None
        pointer = head["latestOptOutReleaseResultHash"]
        if pointer is not None:
            matches = [
                (path, deepcopy(document))
                for path, document in store.data.items()
                if "/contactOptOutFanoutResults/" in path
                and type(document) is dict
                and document.get("contactFanoutResultHash") == pointer
            ]
            self.assertEqual(1, len(matches))
            release_result_path, release_result = matches[0]
        released_authority = None
        if release_result is not None:
            released_number = release_result["releasedRowGeneration"]
            released_generation = deepcopy(
                store.data[
                    self.fixture._generation_reference(
                        store,
                        row_id,
                        released_number,
                    ).path
                ]
            )
            released_settlement_ref = self.fixture._settlement_reference(
                store,
                row_id,
                released_number,
            )
            released_authority = {
                "path": released_settlement_ref.path,
                "generation": released_generation,
                "claimSet": deepcopy(
                    store.data[
                        self.fixture._claim_reference(
                            store,
                            released_generation["requestId"],
                        ).path
                    ]
                ),
                "settlement": deepcopy(
                    store.data[released_settlement_ref.path]
                ),
            }
        restored_authority = None
        if (
            release_result is not None
            and release_result["restoredEffectiveGeneration"] is not None
        ):
            restored_number = release_result["restoredEffectiveGeneration"]
            restored_generation = deepcopy(
                store.data[
                    self.fixture._generation_reference(
                        store,
                        row_id,
                        restored_number,
                    ).path
                ]
            )
            restored_authority = {
                "generation": restored_generation,
                "claimSet": deepcopy(
                    store.data[
                        self.fixture._claim_reference(
                            store,
                            restored_generation["requestId"],
                        ).path
                    ]
                ),
                "settlement": deepcopy(
                    store.data[
                        self.fixture._settlement_reference(
                            store,
                            row_id,
                            restored_number,
                        ).path
                    ]
                ),
            }
        latest = settlement_entries[:2]
        latest_authorities = []
        for entry in latest:
            settlement_generation = entry["document"]["generation"]
            generation = deepcopy(
                store.data[
                    self.fixture._generation_reference(
                        store,
                        row_id,
                        settlement_generation,
                    ).path
                ]
            )
            latest_authorities.append(
                {
                    "generation": generation,
                    "claimSet": deepcopy(
                        store.data[
                            self.fixture._claim_reference(
                                store,
                                generation["requestId"],
                            ).path
                        ]
                    ),
                }
            )
        latest_predecessor_release_matches = []
        latest_predecessor_restored_authority = None
        if len(latest) == 2:
            newer_generation = latest_authorities[0]["generation"]
            older_settlement = latest[1]["document"]
            if (
                older_settlement["outcome"] != "dominated"
                and newer_generation["predecessorSettlementHash"]
                != older_settlement["settlementHash"]
            ):
                result_collection_path = user.collection(
                    "contactOptOutFanoutResults"
                ).path
                latest_predecessor_release_matches = [
                    {"path": path, "document": deepcopy(document)}
                    for path, document in store.data.items()
                    if path.rsplit("/", 1)[0] == result_collection_path
                    and type(document) is dict
                    and document.get("rowId") == row_id
                    and document.get("releasedRowSettlementHash")
                    == older_settlement["settlementHash"]
                ]
                latest_predecessor_release_matches.sort(
                    key=lambda entry: entry["path"]
                )
                if len(latest_predecessor_release_matches) == 1:
                    predecessor_release = (
                        latest_predecessor_release_matches[0]["document"]
                    )
                    restored_number = predecessor_release[
                        "restoredEffectiveGeneration"
                    ]
                    if restored_number is not None:
                        predecessor_restored_generation = deepcopy(
                            store.data[
                                self.fixture._generation_reference(
                                    store,
                                    row_id,
                                    restored_number,
                                ).path
                            ]
                        )
                        latest_predecessor_restored_authority = {
                            "generation": predecessor_restored_generation,
                            "claimSet": deepcopy(
                                store.data[
                                    self.fixture._claim_reference(
                                        store,
                                        predecessor_restored_generation[
                                            "requestId"
                                        ],
                                    ).path
                                ]
                            ),
                            "settlement": deepcopy(
                                store.data[
                                    self.fixture._settlement_reference(
                                        store,
                                        row_id,
                                        restored_number,
                                    ).path
                                ]
                            ),
                        }
        next_generation = max(
            [
                current_number or 0,
                *(
                    entry["document"]["generation"]
                    for entry in latest
                ),
            ]
        ) + 1
        return {
            "rowId": row_id,
            "identity": identity,
            "head": head,
            "currentGeneration": current_generation,
            "currentClaimSet": current_claim,
            "currentSettlement": current_settlement,
            "latestSettlements": latest,
            "latestSettlementAuthorities": latest_authorities,
            "latestPredecessorReleaseMatches": (
                latest_predecessor_release_matches
            ),
            "latestPredecessorRestoredAuthority": (
                latest_predecessor_restored_authority
            ),
            "releaseResult": release_result,
            "releaseResultPath": release_result_path,
            "releasedAuthority": released_authority,
            "restoredAuthority": restored_authority,
            "candidateGeneration": deepcopy(
                store.data.get(
                    self.fixture._generation_reference(
                        store,
                        row_id,
                        next_generation,
                    ).path
                )
            ),
            "candidateSettlement": deepcopy(
                store.data.get(
                    self.fixture._settlement_reference(
                        store,
                        row_id,
                        next_generation,
                    ).path
                )
            ),
        }

    def _apply_claim_plan(self, store, plan):
        self.fixture._claim_reference(
            store,
            plan["claimSet"]["requestId"],
        ).create(plan["claimSet"])
        for generation in plan["generations"]:
            self.fixture._generation_reference(
                store,
                generation["rowId"],
                generation["generation"],
            ).create(generation)
        for settlement in plan["predecessorSettlements"]:
            self.fixture._settlement_reference(
                store,
                settlement["rowId"],
                settlement["generation"],
            ).create(settlement)
        for head in plan["heads"]:
            self.fixture._row_references(store, head["rowId"])[1].set(
                head,
                merge=False,
            )

    def _assert_claim_ambiguity_without_writes(
        self,
        store,
        *,
        bundle,
        created_at,
        lease_until,
    ):
        before = deepcopy(store.data)
        store.events.clear()
        try:
            self.fixture._claim(
                store,
                bundle=bundle,
                created_at=created_at,
                lease_until=lease_until,
            )
        except self.module.RowAuthorityAmbiguous:
            pass
        except self.module.RowAuthorityError as exc:
            self.fail(
                "missing or drifted restored-owner proof must be ambiguity, "
                f"not {type(exc).__name__}: {exc}"
            )
        else:
            self.fail("invalid restored-owner proof was accepted")
        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_restored_clear_allocation_uses_historical_generation_and_fence(self):
        state = self._seed_released_clear()

        result = self._claim_must_succeed(
            state["store"],
            bundle=state["bundle"],
            created_at="2026-08-04T12:00:06.000000Z",
            lease_until="2026-08-04T12:11:00.000000Z",
        )

        self.assertEqual("created", result["disposition"])
        self.assertEqual(2, result["generations"][0]["generation"])
        self.assertEqual(2, result["generations"][0]["firstFencingToken"])
        self.assertIsNone(
            result["generations"][0]["predecessorSettlementHash"]
        )
        self.assertEqual(
            state["releasedSettlement"]["settlementHash"],
            result["heads"][0]["latestSettlementHash"],
        )

    def test_restored_terminal_contact_allocation_uses_historical_generation_and_fence(self):
        link_fixture = self._source_link_store_fixture()
        state, released_head = self._release_linkable_state(link_fixture)
        store = state["store"]
        row_state = self._bounded_state_from_store(store)
        authority_link = self._contact_link(
            source_id="restored-terminal-contact"
        )

        plan = self.module._plan_contact_fanout_row_claim(
            user_scope_hash=self.fixture.scope,
            authority_link=authority_link,
            fanout_id="e" * 64,
            canonical_mailbox_identity_hash=authority_link[
                "canonicalMailboxIdentityHash"
            ],
            contact_settlement_hash="f" * 64,
            thread_binding_document=None,
            canonical_row_id=self.fixture.first,
            row_states=[row_state],
            stored_claim_set_document=None,
            created_at="2026-08-04T12:07:00.000000Z",
            lease_owner_hash="d" * 64,
            lease_until="2026-08-04T12:12:00.000000Z",
        )

        self.assertEqual("created", plan["disposition"])
        self.assertEqual(3, plan["generations"][0]["generation"])
        self.assertEqual(3, plan["generations"][0]["firstFencingToken"])
        self.assertEqual(
            state["b2Settlement"]["settlementHash"],
            plan["generations"][0]["predecessorSettlementHash"],
        )
        self.assertEqual(
            released_head["latestSettlementHash"],
            plan["heads"][0]["latestSettlementHash"],
        )

    def test_combined_release_and_active_dominated_bridges_are_accepted(self):
        (
            store,
            bundle,
            binding,
            _human_generation,
            human_settlement,
            released_head,
        ) = self._seed_released_human(cycles=1)
        terminal = self._claim_must_succeed(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:07:00.000000Z",
            lease_until="2026-08-04T12:12:00.000000Z",
        )
        self.assertEqual(3, terminal["generations"][0]["generation"])
        authority_link = self._contact_link(
            source_id="combined-bridge-contact"
        )
        contact_plan = self.module._plan_contact_fanout_row_claim(
            user_scope_hash=self.fixture.scope,
            authority_link=authority_link,
            fanout_id="e" * 64,
            canonical_mailbox_identity_hash=authority_link[
                "canonicalMailboxIdentityHash"
            ],
            contact_settlement_hash="f" * 64,
            thread_binding_document=None,
            canonical_row_id=self.fixture.first,
            row_states=[self._bounded_state_from_store(store)],
            stored_claim_set_document=None,
            created_at="2026-08-04T12:08:00.000000Z",
            lease_owner_hash="d" * 64,
            lease_until="2026-08-04T12:13:00.000000Z",
        )
        self._apply_claim_plan(store, contact_plan)
        self.assertEqual(4, contact_plan["generations"][0]["generation"])
        self.assertEqual(
            "dominated",
            contact_plan["predecessorSettlements"][0]["outcome"],
        )
        self.assertEqual(
            contact_plan["generations"][0]["generationHash"],
            contact_plan["predecessorSettlements"][0][
                "dominantGenerationHash"
            ],
        )
        self.assertEqual(
            human_settlement["settlementHash"],
            contact_plan["heads"][0]["effectiveSettlementHash"],
        )
        second_bundle = self.fixture._seed_b1_bundle(
            store,
            owner_kind="terminal",
            source_id="combined-bridge-loser",
        )

        result = self._claim_must_succeed(
            store,
            bundle=second_bundle,
            created_at="2026-08-04T12:09:00.000000Z",
            lease_until="2026-08-04T12:14:00.000000Z",
        )

        self.assertEqual("dominated", result["disposition"])
        self.assertEqual(contact_plan["heads"][0], result["heads"][0])

    def test_combined_release_bridge_must_predate_its_first_successor(self):
        (
            store,
            bundle,
            binding,
            _human_generation,
            _human_settlement,
            _released_head,
        ) = self._seed_released_human(cycles=1)
        self._claim_must_succeed(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:07:00.000000Z",
            lease_until="2026-08-04T12:12:00.000000Z",
        )
        authority_link = self._contact_link(
            source_id="late-combined-bridge-contact"
        )
        contact_plan = self.module._plan_contact_fanout_row_claim(
            user_scope_hash=self.fixture.scope,
            authority_link=authority_link,
            fanout_id="e" * 64,
            canonical_mailbox_identity_hash=authority_link[
                "canonicalMailboxIdentityHash"
            ],
            contact_settlement_hash="f" * 64,
            thread_binding_document=None,
            canonical_row_id=self.fixture.first,
            row_states=[self._bounded_state_from_store(store)],
            stored_claim_set_document=None,
            created_at="2026-08-04T12:08:00.000000Z",
            lease_owner_hash="d" * 64,
            lease_until="2026-08-04T12:13:00.000000Z",
        )
        self._apply_claim_plan(store, contact_plan)
        result_paths = [
            path
            for path in store.data
            if "/contactOptOutFanoutResults/" in path
        ]
        self.assertEqual(1, len(result_paths))
        result_path = result_paths[0]
        original_result = store.data[result_path]
        late_result = self.module.build_contact_fanout_result_document(
            user_scope_hash=original_result["userScopeHash"],
            fanout_id=original_result["fanoutId"],
            row_id=original_result["rowId"],
            obligation_hash=original_result["obligationHash"],
            outcome=original_result["outcome"],
            disposition=original_result["disposition"],
            reason_code=original_result["reasonCode"],
            observed_row_head_hash=original_result["observedRowHeadHash"],
            claim_request_id=original_result["claimRequestId"],
            claim_set_hash=original_result["claimSetHash"],
            row_generation=original_result["rowGeneration"],
            row_settlement_hash=original_result["rowSettlementHash"],
            released_row_generation=original_result[
                "releasedRowGeneration"
            ],
            released_row_settlement_hash=original_result[
                "releasedRowSettlementHash"
            ],
            restored_effective_generation=original_result[
                "restoredEffectiveGeneration"
            ],
            restored_effective_settlement_hash=original_result[
                "restoredEffectiveSettlementHash"
            ],
            created_at="2026-08-04T12:07:30.000000Z",
        )
        store.data[result_path] = late_result
        head_ref = self.fixture._row_references(
            store,
            self.fixture.first,
        )[1]
        rewritten_head = deepcopy(store.data[head_ref.path])
        rewritten_head["latestOptOutReleaseResultHash"] = late_result[
            "contactFanoutResultHash"
        ]
        rewritten_head = self.fixture._rehash_head(rewritten_head)
        head_ref.set(rewritten_head, merge=False)

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.module._validate_bounded_row_history(
                scope=self.fixture.scope,
                row_id=self.fixture.first,
                head=rewritten_head,
                row_state=self._bounded_state_from_store(store),
            )

    def test_release_result_wrong_document_path_is_ambiguous(self):
        store, bundle, _binding, _generation, _settlement, _head = (
            self._seed_released_human(cycles=1)
        )
        result_paths = [
            path
            for path in store.data
            if "/contactOptOutFanoutResults/" in path
        ]
        self.assertEqual(1, len(result_paths))
        payload = store.data.pop(result_paths[0])
        self.fixture._user_reference(store).collection(
            "contactOptOutFanoutResults"
        ).document("malformed-release-result-path").create(payload)

        self._assert_ambiguous_without_writes(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:07:00.000000Z",
            lease_until="2026-08-04T12:12:00.000000Z",
        )

    def test_release_result_cannot_clear_a_nonnull_contact_predecessor(self):
        (
            store,
            _bundle,
            _binding,
            _generation,
            _settlement,
            released_head,
        ) = self._seed_released_human(cycles=1)
        result_paths = [
            path
            for path in store.data
            if "/contactOptOutFanoutResults/" in path
        ]
        self.assertEqual(1, len(result_paths))
        result_path = result_paths[0]
        original = store.data[result_path]
        forged = self.module.build_contact_fanout_result_document(
            user_scope_hash=original["userScopeHash"],
            fanout_id=original["fanoutId"],
            row_id=original["rowId"],
            obligation_hash=original["obligationHash"],
            outcome=original["outcome"],
            disposition=original["disposition"],
            reason_code=original["reasonCode"],
            observed_row_head_hash=original["observedRowHeadHash"],
            claim_request_id=original["claimRequestId"],
            claim_set_hash=original["claimSetHash"],
            row_generation=original["rowGeneration"],
            row_settlement_hash=original["rowSettlementHash"],
            released_row_generation=original["releasedRowGeneration"],
            released_row_settlement_hash=original[
                "releasedRowSettlementHash"
            ],
            restored_effective_generation=None,
            restored_effective_settlement_hash=None,
            created_at=original["createdAt"],
        )
        store.data[result_path] = forged
        head_ref = self.fixture._row_references(
            store,
            self.fixture.first,
        )[1]
        forged_head = deepcopy(released_head)
        forged_head.update(
            {
                "effectiveOwnerGeneration": None,
                "effectiveOwnerGenerationHash": None,
                "effectiveOwnerKind": None,
                "effectivePriority": None,
                "state": "clear",
                "fencingToken": None,
                "effectiveSettlementHash": None,
                "latestOptOutReleaseResultHash": forged[
                    "contactFanoutResultHash"
                ],
            }
        )
        forged_head = self.fixture._rehash_head(forged_head)
        head_ref.set(forged_head, merge=False)

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.module._validate_bounded_row_history(
                scope=self.fixture.scope,
                row_id=self.fixture.first,
                head=forged_head,
                row_state=self._bounded_state_from_store(store),
            )

    def test_release_result_lookup_is_exact_user_row_hash_and_limit_two(self):
        store, bundle, _binding, _generation, _settlement, head = (
            self._seed_released_human(cycles=1)
        )
        query_type = type(
            store.collection("probe").where("rowId", "==", self.fixture.first)
        )
        original_limit = query_type.limit
        observed_limits = []

        def observing_limit(query, count):
            if query._collection.path.endswith(
                "/contactOptOutFanoutResults"
            ):
                observed_limits.append(count)
            return original_limit(query, count)

        store.events.clear()
        with patch.object(query_type, "limit", observing_limit):
            self._claim_must_succeed(
                store,
                bundle=bundle,
                created_at="2026-08-04T12:07:00.000000Z",
                lease_until="2026-08-04T12:12:00.000000Z",
            )

        queries = [
            event
            for event in store.events
            if event[0] == "query"
            and event[1].endswith("/contactOptOutFanoutResults")
        ]
        self.assertEqual(1, len(queries))
        self.assertEqual(
            (
                ("rowId", "==", self.fixture.first),
                (
                    "contactFanoutResultHash",
                    "==",
                    head["latestOptOutReleaseResultHash"],
                ),
            ),
            queries[0][2],
        )
        self.assertEqual(("__name__",), queries[0][3])
        self.assertEqual([2], observed_limits)

    def test_bounded_latest_authority_rejects_cross_scope_generation(self):
        store, bundle, _binding = self._seed(owner_kind="terminal")
        (
            _action,
            _claim,
            generation_one,
            settlement_one,
            _settled_head,
        ) = self.fixture._install_settled_human_owner(
            store,
            self.fixture.first,
        )
        pending = self.fixture._claim(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:00:04.000000Z",
            lease_until="2026-08-04T12:05:00.000000Z",
        )
        generation_two = pending["generations"][0]
        pending_head = pending["heads"][0]

        foreign_generation = deepcopy(generation_one)
        foreign_generation["userScopeHash"] = "f" * 64
        foreign_generation = self.fixture._rehash_generation(
            foreign_generation
        )
        rewritten_settlement = deepcopy(settlement_one)
        rewritten_settlement["generationHash"] = foreign_generation[
            "generationHash"
        ]
        rewritten_settlement = self.fixture._rehash_settlement(
            rewritten_settlement
        )
        rewritten_successor = deepcopy(generation_two)
        rewritten_successor["predecessorSettlementHash"] = (
            rewritten_settlement["settlementHash"]
        )
        rewritten_successor = self.fixture._rehash_generation(
            rewritten_successor
        )
        rewritten_head = deepcopy(pending_head)
        rewritten_head.update(
            {
                "latestSettlementHash": rewritten_settlement[
                    "settlementHash"
                ],
                "effectiveSettlementHash": rewritten_settlement[
                    "settlementHash"
                ],
                "effectiveOwnerGenerationHash": rewritten_successor[
                    "generationHash"
                ],
            }
        )
        rewritten_head = self.fixture._rehash_head(rewritten_head)
        self.fixture._generation_reference(
            store,
            self.fixture.first,
            1,
        ).set(foreign_generation, merge=False)
        self.fixture._settlement_reference(
            store,
            self.fixture.first,
            1,
        ).set(rewritten_settlement, merge=False)
        self.fixture._generation_reference(
            store,
            self.fixture.first,
            2,
        ).set(rewritten_successor, merge=False)
        self.fixture._row_references(
            store,
            self.fixture.first,
        )[1].set(rewritten_head, merge=False)
        row_state = self._bounded_state_from_store(store)

        with self.assertRaises(self.module.RowAuthorityError):
            self.module._validate_bounded_row_history(
                scope=self.fixture.scope,
                row_id=self.fixture.first,
                head=rewritten_head,
                row_state=row_state,
            )

    def test_bounded_latest_pair_rejects_regressed_successor_first_fence(self):
        state = self._seed_released_clear()
        store = state["store"]
        (
            _claim,
            generation,
            settlement,
            settled_head,
            _fanout_id,
        ) = self._install_contact_settlement(
            store,
            expected_head=state["head"],
            generation_number=2,
            first_fence=2,
            predecessor_settlement=None,
            created_at="2026-08-04T12:00:06.000000Z",
            settled_at="2026-08-04T12:00:07.000000Z",
            cycle=190,
        )
        regressed_generation = deepcopy(generation)
        regressed_generation["firstFencingToken"] = 1
        regressed_generation = self.fixture._rehash_generation(
            regressed_generation
        )
        rewritten_settlement = deepcopy(settlement)
        rewritten_settlement["generationHash"] = regressed_generation[
            "generationHash"
        ]
        rewritten_settlement = self.fixture._rehash_settlement(
            rewritten_settlement
        )
        rewritten_head = deepcopy(settled_head)
        rewritten_head.update(
            {
                "effectiveOwnerGenerationHash": regressed_generation[
                    "generationHash"
                ],
                "latestSettlementHash": rewritten_settlement[
                    "settlementHash"
                ],
                "effectiveSettlementHash": rewritten_settlement[
                    "settlementHash"
                ],
            }
        )
        rewritten_head = self.fixture._rehash_head(rewritten_head)
        self.fixture._generation_reference(
            store,
            self.fixture.first,
            2,
        ).set(regressed_generation, merge=False)
        self.fixture._settlement_reference(
            store,
            self.fixture.first,
            2,
        ).set(rewritten_settlement, merge=False)
        self.fixture._row_references(
            store,
            self.fixture.first,
        )[1].set(rewritten_head, merge=False)

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.module._validate_bounded_row_history(
                scope=self.fixture.scope,
                row_id=self.fixture.first,
                head=rewritten_head,
                row_state=self._bounded_state_from_store(store),
            )

    def test_restored_owner_artifact_missing_or_drift_is_ambiguous(self):
        for artifact in ("generation", "claim", "settlement"):
            for mutation in ("missing", "drift"):
                with self.subTest(artifact=artifact, mutation=mutation):
                    (
                        store,
                        bundle,
                        _binding,
                        generation,
                        _settlement,
                        _head,
                    ) = self._seed_released_human(cycles=1)
                    if artifact == "generation":
                        reference = self.fixture._generation_reference(
                            store,
                            self.fixture.first,
                            1,
                        )
                        drift_field = "generationHash"
                    elif artifact == "claim":
                        reference = self.fixture._claim_reference(
                            store,
                            generation["requestId"],
                        )
                        drift_field = "claimSetHash"
                    else:
                        reference = self.fixture._settlement_reference(
                            store,
                            self.fixture.first,
                            1,
                        )
                        drift_field = "settlementHash"
                    if mutation == "missing":
                        del store.data[reference.path]
                    else:
                        store.data[reference.path][drift_field] = "f" * 64
                    self._assert_claim_ambiguity_without_writes(
                        store,
                        bundle=bundle,
                        created_at="2026-08-04T12:07:00.000000Z",
                        lease_until="2026-08-04T12:12:00.000000Z",
                    )

    def _seed_contact_release_history(self, *, cycles):
        (
            store,
            bundle,
            binding,
            restored_generation,
            restored_settlement,
            head,
        ) = self._seed_released_human(cycles=0)
        releases = []
        for cycle in range(1, cycles + 1):
            base_minute = 3 + ((cycle - 1) * 3)
            (
                claim,
                generation,
                settlement,
                settled_head,
                fanout_id,
            ) = self._install_contact_settlement(
                store,
                expected_head=head,
                generation_number=cycle + 1,
                first_fence=cycle + 1,
                predecessor_settlement=restored_settlement,
                created_at=(
                    f"2026-08-04T12:{base_minute:02d}:00.000000Z"
                ),
                settled_at=(
                    f"2026-08-04T12:{base_minute + 1:02d}:00.000000Z"
                ),
                cycle=cycle,
            )
            result, head = self._release_to(
                store,
                released_generation=generation,
                released_settlement=settlement,
                settled_head=settled_head,
                fanout_id=fanout_id,
                restored_generation=restored_generation,
                restored_settlement=restored_settlement,
                released_at=(
                    f"2026-08-04T12:{base_minute + 2:02d}:00.000000Z"
                ),
                cycle=cycle,
            )
            releases.append(
                {
                    "claim": claim,
                    "generation": generation,
                    "settlement": settlement,
                    "result": result,
                    "head": head,
                }
            )
        return {
            "store": store,
            "bundle": bundle,
            "binding": binding,
            "restoredGeneration": restored_generation,
            "restoredSettlement": restored_settlement,
            "head": head,
            "releases": releases,
        }

    def _bounded_replay_state_from_store(self, store, claim):
        state = self._bounded_state_from_store(store)
        decisions = [
            decision
            for decision in claim["rowDecisions"]
            if decision["rowId"] == self.fixture.first
        ]
        self.assertEqual(1, len(decisions))
        self.assertEqual("accepted", decisions[0]["decision"])
        generation_number = decisions[0]["plannedGeneration"]
        generation = deepcopy(
            store.data[
                self.fixture._generation_reference(
                    store,
                    self.fixture.first,
                    generation_number,
                ).path
            ]
        )
        state.update(
            {
                "candidateGeneration": generation,
                "candidateSettlement": deepcopy(
                    store.data.get(
                        self.fixture._settlement_reference(
                            store,
                            self.fixture.first,
                            generation_number,
                        ).path
                    )
                ),
                "candidatePredecessorGeneration": None,
                "candidatePredecessorClaimSet": None,
                "candidatePredecessorSettlement": None,
                "candidatePredecessorReleaseMatches": [],
                "candidatePredecessorRestoredAuthority": None,
            }
        )
        if generation_number > 1:
            predecessor_generation = deepcopy(
                store.data[
                    self.fixture._generation_reference(
                        store,
                        self.fixture.first,
                        generation_number - 1,
                    ).path
                ]
            )
            predecessor_settlement = deepcopy(
                store.data[
                    self.fixture._settlement_reference(
                        store,
                        self.fixture.first,
                        generation_number - 1,
                    ).path
                ]
            )
            result_collection_path = self.fixture._user_reference(
                store
            ).collection("contactOptOutFanoutResults").path
            release_matches = [
                {"path": path, "document": deepcopy(document)}
                for path, document in store.data.items()
                if path.rsplit("/", 1)[0] == result_collection_path
                and type(document) is dict
                and document.get("rowId") == self.fixture.first
                and document.get("releasedRowSettlementHash")
                == predecessor_settlement["settlementHash"]
            ]
            release_matches.sort(key=lambda entry: entry["path"])
            predecessor_restored_authority = None
            if len(release_matches) == 1:
                restored_number = release_matches[0]["document"][
                    "restoredEffectiveGeneration"
                ]
                if restored_number is not None:
                    restored_generation = deepcopy(
                        store.data[
                            self.fixture._generation_reference(
                                store,
                                self.fixture.first,
                                restored_number,
                            ).path
                        ]
                    )
                    predecessor_restored_authority = {
                        "generation": restored_generation,
                        "claimSet": deepcopy(
                            store.data[
                                self.fixture._claim_reference(
                                    store,
                                    restored_generation["requestId"],
                                ).path
                            ]
                        ),
                        "settlement": deepcopy(
                            store.data[
                                self.fixture._settlement_reference(
                                    store,
                                    self.fixture.first,
                                    restored_number,
                                ).path
                            ]
                        ),
                    }
            state.update(
                {
                    "candidatePredecessorGeneration": predecessor_generation,
                    "candidatePredecessorClaimSet": deepcopy(
                        store.data[
                            self.fixture._claim_reference(
                                store,
                                predecessor_generation["requestId"],
                            ).path
                        ]
                    ),
                    "candidatePredecessorSettlement": predecessor_settlement,
                    "candidatePredecessorReleaseMatches": release_matches,
                    "candidatePredecessorRestoredAuthority": (
                        predecessor_restored_authority
                    ),
                }
            )
        return state

    def _plan_contact_replay(self, release_state, claim):
        return self.module._plan_contact_fanout_row_claim(
            user_scope_hash=self.fixture.scope,
            authority_link=claim["authorityLink"],
            fanout_id=claim["fanoutId"],
            canonical_mailbox_identity_hash=claim["ownerKey"],
            contact_settlement_hash=claim["payloadHash"],
            thread_binding_document=None,
            canonical_row_id=claim["primaryRowId"],
            row_states=[
                self._bounded_replay_state_from_store(
                    release_state["store"],
                    claim,
                )
            ],
            stored_claim_set_document=claim,
            created_at=claim["createdAt"],
            lease_owner_hash="e" * 64,
            lease_until="2026-08-04T13:00:00.000000Z",
        )

    def test_released_replay_rejects_head_older_than_candidate_generation(self):
        state = self._seed_contact_release_history(cycles=1)
        store = state["store"]
        candidate = state["releases"][0]
        head_ref = self.fixture._row_references(
            store,
            self.fixture.first,
        )[1]
        regressed_head = deepcopy(store.data[head_ref.path])
        regressed_head["updatedAt"] = "2026-08-04T12:02:59.000000Z"
        head_ref.set(
            self.fixture._rehash_head(regressed_head),
            merge=False,
        )
        before = deepcopy(store.data)

        with self.assertRaises(self.module.RowAuthorityError):
            self._plan_contact_replay(state, candidate["claim"])

        self.assertEqual(before, store.data)

    def test_old_contact_replay_survives_later_release_cycles(self):
        state = self._seed_contact_release_history(cycles=3)
        store = state["store"]
        candidate = state["releases"][0]
        retained_result = state["releases"][-1]["result"]
        self.assertEqual(2, candidate["generation"]["generation"])
        self.assertEqual(4, retained_result["releasedRowGeneration"])
        self.assertNotEqual(
            candidate["settlement"]["settlementHash"],
            retained_result["releasedRowSettlementHash"],
        )
        before = deepcopy(store.data)

        replay = self._plan_contact_replay(state, candidate["claim"])

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(candidate["claim"], replay["claimSet"])
        self.assertEqual((state["head"],), replay["heads"])
        self.assertEqual((), replay["mutations"])
        self.assertEqual(before, store.data)

    def test_accepted_replay_rejects_ordinary_predecessor_hash_mismatch(self):
        (
            store,
            bundle,
            _binding,
            _human_generation,
            human_settlement,
            _head,
        ) = self._seed_released_human(cycles=0)
        candidate = self.fixture._claim(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:04:00.000000Z",
            lease_until="2026-08-04T12:09:00.000000Z",
        )
        generation = deepcopy(candidate["generations"][0])
        self.assertEqual(
            human_settlement["settlementHash"],
            generation["predecessorSettlementHash"],
        )
        generation["predecessorSettlementHash"] = "f" * 64
        generation = self.fixture._rehash_generation(generation)
        claimed_head = deepcopy(candidate["heads"][0])
        claimed_head.update(
            {
                "effectiveOwnerGenerationHash": generation[
                    "generationHash"
                ],
                "effectiveSettlementHash": "f" * 64,
            }
        )
        claimed_head = self.fixture._rehash_head(claimed_head)
        candidate_settlement = self.module.build_owner_settlement_document(
            generation_document=generation,
            claim_set_document=candidate["claimSet"],
            fencing_token=claimed_head["fencingToken"],
            outcome="terminal",
            settled_at="2026-08-04T12:05:00.000000Z",
        )
        settled_head = self.module._build_settlement_advanced_head(
            expected_head=claimed_head,
            generation_document=generation,
            settlement_document=candidate_settlement,
        )
        self.fixture._generation_reference(
            store,
            self.fixture.first,
            2,
        ).set(generation, merge=False)
        self.fixture._settlement_reference(
            store,
            self.fixture.first,
            2,
        ).create(candidate_settlement)
        self.fixture._row_references(store, self.fixture.first)[1].set(
            settled_head,
            merge=False,
        )
        self._install_contact_settlement(
            store,
            expected_head=settled_head,
            generation_number=3,
            first_fence=3,
            predecessor_settlement=candidate_settlement,
            created_at="2026-08-04T12:06:00.000000Z",
            settled_at="2026-08-04T12:07:00.000000Z",
            cycle=221,
        )
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self.fixture._claim(
                store,
                bundle=bundle,
                created_at="2026-08-04T12:04:00.000000Z",
                lease_until="2026-08-04T12:09:00.000000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_accepted_replay_rejects_equal_priority_direct_predecessor(self):
        (
            store,
            _bundle,
            _binding,
            predecessor_generation,
            predecessor_settlement,
            settled_head,
        ) = self._seed_released_human(cycles=0)
        predecessor_claim = deepcopy(
            store.data[
                self.fixture._claim_reference(
                    store,
                    predecessor_generation["requestId"],
                ).path
            ]
        )
        claim, generation, forged_head = self._forged_accepted_generation(
            expected_head=settled_head,
            owner_kind="human_decision",
            generation_number=2,
            first_fencing_token=2,
            predecessor_settlement_hash=predecessor_settlement[
                "settlementHash"
            ],
            created_at="2026-08-04T12:04:00.000000Z",
        )
        state = {
            "rowId": self.fixture.first,
            "head": forged_head,
            "boundedHistory": {
                "latestSettlements": (predecessor_settlement,),
            },
            "candidateGeneration": generation,
            "candidateSettlement": None,
            "candidatePredecessorGeneration": predecessor_generation,
            "candidatePredecessorClaimSet": predecessor_claim,
            "candidatePredecessorSettlement": predecessor_settlement,
            "candidatePredecessorReleaseMatches": [],
            "candidatePredecessorRestoredAuthority": None,
        }

        with self.assertRaises(self.module.RowAuthorityConflict):
            self.module._validate_bounded_accepted_replay(
                scope=self.fixture.scope,
                state=state,
                decision=claim["rowDecisions"][0],
                stored_claim=claim,
                lease_owner="d" * 64,
                deadline="2026-08-04T13:00:00.000000Z",
            )

    def test_accepted_replay_after_release_must_outrank_restored_owner(self):
        (
            store,
            _bundle,
            _binding,
            restored_generation,
            restored_settlement,
            released_head,
        ) = self._seed_released_human(cycles=1)
        released_generation = deepcopy(
            store.data[
                self.fixture._generation_reference(
                    store,
                    self.fixture.first,
                    2,
                ).path
            ]
        )
        released_claim = deepcopy(
            store.data[
                self.fixture._claim_reference(
                    store,
                    released_generation["requestId"],
                ).path
            ]
        )
        released_settlement = deepcopy(
            store.data[
                self.fixture._settlement_reference(
                    store,
                    self.fixture.first,
                    2,
                ).path
            ]
        )
        restored_claim = deepcopy(
            store.data[
                self.fixture._claim_reference(
                    store,
                    restored_generation["requestId"],
                ).path
            ]
        )
        release_matches = [
            {"path": path, "document": deepcopy(document)}
            for path, document in store.data.items()
            if "/contactOptOutFanoutResults/" in path
            and document.get("releasedRowSettlementHash")
            == released_settlement["settlementHash"]
        ]
        self.assertEqual(1, len(release_matches))
        claim, generation, forged_head = self._forged_accepted_generation(
            expected_head=released_head,
            owner_kind="human_decision",
            generation_number=3,
            first_fencing_token=3,
            predecessor_settlement_hash=restored_settlement[
                "settlementHash"
            ],
            created_at="2026-08-04T12:07:00.000000Z",
        )
        state = {
            "rowId": self.fixture.first,
            "head": forged_head,
            "boundedHistory": {
                "latestSettlements": (
                    released_settlement,
                    restored_settlement,
                ),
            },
            "candidateGeneration": generation,
            "candidateSettlement": None,
            "candidatePredecessorGeneration": released_generation,
            "candidatePredecessorClaimSet": released_claim,
            "candidatePredecessorSettlement": released_settlement,
            "candidatePredecessorReleaseMatches": release_matches,
            "candidatePredecessorRestoredAuthority": {
                "generation": restored_generation,
                "claimSet": restored_claim,
                "settlement": restored_settlement,
            },
        }

        with self.assertRaises(self.module.RowAuthorityConflict):
            self.module._validate_bounded_accepted_replay(
                scope=self.fixture.scope,
                state=state,
                decision=claim["rowDecisions"][0],
                stored_claim=claim,
                lease_owner="d" * 64,
                deadline="2026-08-04T13:00:00.000000Z",
            )

    def test_contact_predecessor_bridge_requires_its_exact_release_result(self):
        for old_result_present in (True, False):
            with self.subTest(old_result_present=old_result_present):
                (
                    store,
                    bundle,
                    _binding,
                    _human_generation,
                    _human_settlement,
                    _released_head,
                ) = self._seed_released_human(cycles=1)
                candidate = self.fixture._claim(
                    store,
                    bundle=bundle,
                    created_at="2026-08-04T12:07:00.000000Z",
                    lease_until="2026-08-04T12:12:00.000000Z",
                )
                settled = self.fixture._authority(
                    store
                ).settle_owner_generation(
                    verified_user_id=self.fixture.user_id,
                    row_id=self.fixture.first,
                    expected_head=candidate["heads"][0],
                    settled_at="2026-08-04T12:08:00.000000Z",
                )
                (
                    _claim,
                    later_contact_generation,
                    later_contact_settlement,
                    later_contact_head,
                    later_fanout_id,
                ) = self._install_contact_settlement(
                    store,
                    expected_head=settled["head"],
                    generation_number=4,
                    first_fence=4,
                    predecessor_settlement=settled["settlement"],
                    created_at="2026-08-04T12:09:00.000000Z",
                    settled_at="2026-08-04T12:10:00.000000Z",
                    cycle=231,
                )
                retained_result, _final_head = self._release_to(
                    store,
                    released_generation=later_contact_generation,
                    released_settlement=later_contact_settlement,
                    settled_head=later_contact_head,
                    fanout_id=later_fanout_id,
                    restored_generation=settled["generation"],
                    restored_settlement=settled["settlement"],
                    released_at="2026-08-04T12:11:00.000000Z",
                    cycle=231,
                )
                candidate_generation = candidate["generations"][0]
                predecessor_settlement = deepcopy(
                    store.data[
                        self.fixture._settlement_reference(
                            store,
                            self.fixture.first,
                            2,
                        ).path
                    ]
                )
                self.assertEqual("contact_optout", predecessor_settlement["outcome"])
                self.assertEqual(
                    predecessor_settlement[
                        "supersededEffectiveSettlementHash"
                    ],
                    candidate_generation["predecessorSettlementHash"],
                )
                self.assertNotEqual(
                    predecessor_settlement["settlementHash"],
                    candidate_generation["predecessorSettlementHash"],
                )
                self.assertEqual(4, retained_result["releasedRowGeneration"])
                historical_result_paths = [
                    path
                    for path, document in store.data.items()
                    if "/contactOptOutFanoutResults/" in path
                    and document["releasedRowGeneration"] == 2
                    and document["releasedRowSettlementHash"]
                    == predecessor_settlement["settlementHash"]
                ]
                self.assertEqual(1, len(historical_result_paths))
                if not old_result_present:
                    del store.data[historical_result_paths[0]]
                before = deepcopy(store.data)
                store.events.clear()

                if old_result_present:
                    replay = self.fixture._claim(
                        store,
                        bundle=bundle,
                        created_at="2026-08-04T12:07:00.000000Z",
                        lease_until="2026-08-04T12:12:00.000000Z",
                    )
                    self.assertEqual("already_applied", replay["disposition"])
                else:
                    with self.assertRaises(self.module.RowAuthorityError):
                        self.fixture._claim(
                            store,
                            bundle=bundle,
                            created_at="2026-08-04T12:07:00.000000Z",
                            lease_until="2026-08-04T12:12:00.000000Z",
                        )

                self.assertEqual(before, store.data)
                self.assertEqual([], self.fixture._write_events(store))

    def test_predecessor_restored_authority_read_failure_is_retryable(self):
        (
            store,
            bundle,
            _binding,
            _human_generation,
            human_settlement,
            restored_head,
        ) = self._seed_released_human(cycles=2)
        self._install_contact_settlement(
            store,
            expected_head=restored_head,
            generation_number=4,
            first_fence=4,
            predecessor_settlement=human_settlement,
            created_at="2026-08-04T12:09:00.000000Z",
            settled_at="2026-08-04T12:10:00.000000Z",
            cycle=460,
        )
        restored_generation_path = self.fixture._generation_reference(
            store,
            self.fixture.first,
            1,
        ).path
        fake_module = importlib.import_module(
            "tests.source_coordinator_fakes"
        )
        original_get = fake_module.FakeDocumentReference.get

        def fail_exact_restored_read(reference, *, transaction=None):
            if (
                transaction is not None
                and reference.path == restored_generation_path
            ):
                raise RuntimeError("restored authority read unavailable")
            return original_get(reference, transaction=transaction)

        before = deepcopy(store.data)
        store.events.clear()
        with patch.object(
            fake_module.FakeDocumentReference,
            "get",
            new=fail_exact_restored_read,
        ), self.assertRaises(self.module.RowAuthorityRetryable) as caught:
            self.fixture._claim(
                store,
                bundle=bundle,
                created_at="2026-08-04T12:11:00.000000Z",
                lease_until="2026-08-04T12:16:00.000000Z",
            )

        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_multirow_blocked_decision_exact_replay_remains_valid(self):
        store = self.fixture._store()
        bundle, _binding = self.fixture._seed_prerequisites(
            store,
            row_ids=[self.fixture.first, self.fixture.second],
        )
        self.fixture._install_contact_owner(store, self.fixture.first)
        original = self.fixture._claim(store, bundle=bundle)
        self.assertEqual(
            ["dominated", "blocked_by_claim_set"],
            [
                decision["decision"]
                for decision in original["claimSet"]["rowDecisions"]
            ],
        )
        before = deepcopy(store.data)
        store.events.clear()

        replay = self.fixture._claim(store, bundle=bundle)

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(original["claimSet"], replay["claimSet"])
        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_blocked_replay_survives_settled_first_owner_created_after_claim(self):
        store = self.fixture._store()
        bundle, _binding = self.fixture._seed_prerequisites(
            store,
            row_ids=[self.fixture.first, self.fixture.second],
        )
        self.fixture._install_contact_owner(store, self.fixture.first)
        claim_at = "2026-08-04T12:00:03.000000Z"
        original = self.fixture._claim(
            store,
            bundle=bundle,
            created_at=claim_at,
            lease_until="2026-08-04T12:10:00.000000Z",
        )
        self.assertEqual(
            ["dominated", "blocked_by_claim_set"],
            [
                decision["decision"]
                for decision in original["claimSet"]["rowDecisions"]
            ],
        )
        clear_head = deepcopy(
            store.data[
                self.fixture._row_references(
                    store,
                    self.fixture.second,
                )[1].path
            ]
        )
        self._install_contact_settlement(
            store,
            expected_head=clear_head,
            generation_number=1,
            first_fence=1,
            predecessor_settlement=None,
            created_at="2026-08-04T12:00:04.000000Z",
            settled_at="2026-08-04T12:00:05.000000Z",
            cycle=440,
            row_id=self.fixture.second,
        )
        before = deepcopy(store.data)
        store.events.clear()

        replay = self.fixture._claim(
            store,
            bundle=bundle,
            created_at=claim_at,
            lease_until="2026-08-04T12:10:00.000000Z",
        )

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(original["claimSet"], replay["claimSet"])
        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_blocked_replay_survives_direct_postclaim_successor(self):
        store = self.fixture._store()
        bundle, _binding = self.fixture._seed_prerequisites(
            store,
            row_ids=[self.fixture.first, self.fixture.second],
        )
        self.fixture._install_contact_owner(store, self.fixture.first)
        human_generation, human_settlement, human_head = (
            self._install_human_owner_for_row(
                store,
                row_id=self.fixture.second,
                suffix="direct-successor",
                issued_at="2026-08-04T12:00:03.000000Z",
            )
        )
        self.assertEqual(1, human_generation["generation"])
        claim_at = "2026-08-04T12:00:04.000000Z"
        original = self.fixture._claim(
            store,
            bundle=bundle,
            created_at=claim_at,
            lease_until="2026-08-04T12:10:00.000000Z",
        )
        self.assertEqual(
            ["dominated", "blocked_by_claim_set"],
            [
                decision["decision"]
                for decision in original["claimSet"]["rowDecisions"]
            ],
        )
        successor_claim, successor_generation, successor_head = (
            self._forged_accepted_generation(
                expected_head=human_head,
                owner_kind="terminal",
                generation_number=2,
                first_fencing_token=2,
                predecessor_settlement_hash=human_settlement[
                    "settlementHash"
                ],
                created_at="2026-08-04T12:00:05.000000Z",
                row_id=self.fixture.second,
            )
        )
        self.fixture._claim_reference(
            store,
            successor_claim["requestId"],
        ).create(successor_claim)
        self.fixture._generation_reference(
            store,
            self.fixture.second,
            2,
        ).create(successor_generation)
        self.fixture._row_references(
            store,
            self.fixture.second,
        )[1].set(successor_head, merge=False)
        before = deepcopy(store.data)
        store.events.clear()

        replay = self.fixture._claim(
            store,
            bundle=bundle,
            created_at=claim_at,
            lease_until="2026-08-04T12:10:00.000000Z",
        )

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(original["claimSet"], replay["claimSet"])
        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_multirow_blocked_replay_rejects_equal_time_release_order(self):
        store = self.fixture._store()
        bundle, _binding = self.fixture._seed_prerequisites(
            store,
            row_ids=[self.fixture.first, self.fixture.second],
        )
        self.fixture._install_contact_owner(store, self.fixture.first)
        original = self.fixture._claim(store, bundle=bundle)
        self.assertEqual(
            ["dominated", "blocked_by_claim_set"],
            [
                decision["decision"]
                for decision in original["claimSet"]["rowDecisions"]
            ],
        )
        (
            blocked_claim,
            blocked_generation,
            blocked_head,
        ) = self.fixture._install_contact_owner(
            store,
            self.fixture.second,
        )
        event_at = original["claimSet"]["createdAt"]
        blocked_settlement = self.module.build_owner_settlement_document(
            generation_document=blocked_generation,
            claim_set_document=blocked_claim,
            fencing_token=blocked_head["fencingToken"],
            outcome="contact_optout",
            settled_at=event_at,
            superseded_effective_settlement_hash=None,
        )
        settled_head = self.module._build_settlement_advanced_head(
            expected_head=blocked_head,
            generation_document=blocked_generation,
            settlement_document=blocked_settlement,
        )
        self.fixture._settlement_reference(
            store,
            self.fixture.second,
            1,
        ).create(blocked_settlement)
        self.fixture._row_references(
            store,
            self.fixture.second,
        )[1].set(settled_head, merge=False)
        self._release_to(
            store,
            released_generation=blocked_generation,
            released_settlement=blocked_settlement,
            settled_head=settled_head,
            fanout_id=blocked_claim["fanoutId"],
            restored_generation=None,
            restored_settlement=None,
            released_at=event_at,
            cycle=252,
            row_id=self.fixture.second,
        )
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.fixture._claim(store, bundle=bundle)

        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_current_claim_cohort_rejects_unrelated_lower_release_history(self):
        store = self.fixture._store()
        bundle, _binding = self.fixture._seed_prerequisites(
            store,
            row_ids=[self.fixture.first, self.fixture.second],
        )
        released_heads = {}
        for index, row_id in enumerate(
            (self.fixture.first, self.fixture.second),
            start=1,
        ):
            contact_claim, contact_generation, contact_head = (
                self.fixture._install_contact_owner(store, row_id)
            )
            contact_settlement = (
                self.module.build_owner_settlement_document(
                    generation_document=contact_generation,
                    claim_set_document=contact_claim,
                    fencing_token=contact_head["fencingToken"],
                    outcome="contact_optout",
                    settled_at="2026-08-04T12:00:03.000000Z",
                    superseded_effective_settlement_hash=None,
                )
            )
            settled_head = self.module._build_settlement_advanced_head(
                expected_head=contact_head,
                generation_document=contact_generation,
                settlement_document=contact_settlement,
            )
            self.fixture._settlement_reference(
                store,
                row_id,
                1,
            ).create(contact_settlement)
            self.fixture._row_references(store, row_id)[1].set(
                settled_head,
                merge=False,
            )
            _release, released_heads[row_id] = self._release_to(
                store,
                released_generation=contact_generation,
                released_settlement=contact_settlement,
                settled_head=settled_head,
                fanout_id=contact_claim["fanoutId"],
                restored_generation=None,
                restored_settlement=None,
                released_at="2026-08-04T12:00:04.000000Z",
                cycle=420 + index,
                row_id=row_id,
            )
        cohort = self.fixture._claim(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:00:05.000000Z",
            lease_until="2026-08-04T12:10:00.000000Z",
        )
        self.assertEqual("created", cohort["disposition"])
        self.assertEqual(
            [2, 2],
            [
                generation["generation"]
                for generation in cohort["generations"]
            ],
        )
        del store.data[
            self.fixture._generation_reference(
                store,
                self.fixture.second,
                2,
            ).path
        ]
        self.fixture._row_references(
            store,
            self.fixture.second,
        )[1].set(released_heads[self.fixture.second], merge=False)
        losing_bundle = self.fixture._seed_b1_bundle(
            store,
            owner_kind="terminal",
            source_id="cohort-release-mask-loser",
        )
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self.fixture._claim(
                store,
                bundle=losing_bundle,
                created_at="2026-08-04T12:00:06.000000Z",
                lease_until="2026-08-04T12:11:00.000000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_current_claim_cohort_rejects_foreign_direct_boundary(self):
        store = self.fixture._store()
        bundle, _binding = self.fixture._seed_prerequisites(
            store,
            row_ids=[self.fixture.first, self.fixture.second],
        )
        _identity_ref, second_head_ref = self.fixture._row_references(
            store,
            self.fixture.second,
        )
        second_clear_head = deepcopy(store.data[second_head_ref.path])
        cohort = self.fixture._claim(store, bundle=bundle)
        self.assertEqual("created", cohort["disposition"])
        del store.data[
            self.fixture._generation_reference(
                store,
                self.fixture.second,
                1,
            ).path
        ]
        second_head_ref.set(second_clear_head, merge=False)
        foreign_claim, foreign_generation, foreign_head = (
            self.fixture._install_contact_owner(
                store,
                self.fixture.second,
            )
        )
        foreign_settlement = self.module.build_owner_settlement_document(
            generation_document=foreign_generation,
            claim_set_document=foreign_claim,
            fencing_token=foreign_head["fencingToken"],
            outcome="contact_optout",
            settled_at="2026-08-04T12:00:03.000000Z",
            superseded_effective_settlement_hash=None,
        )
        foreign_settled_head = self.module._build_settlement_advanced_head(
            expected_head=foreign_head,
            generation_document=foreign_generation,
            settlement_document=foreign_settlement,
        )
        self.fixture._settlement_reference(
            store,
            self.fixture.second,
            1,
        ).create(foreign_settlement)
        second_head_ref.set(foreign_settled_head, merge=False)
        self._release_to(
            store,
            released_generation=foreign_generation,
            released_settlement=foreign_settlement,
            settled_head=foreign_settled_head,
            fanout_id=foreign_claim["fanoutId"],
            restored_generation=None,
            restored_settlement=None,
            released_at="2026-08-04T12:00:04.000000Z",
            cycle=423,
            row_id=self.fixture.second,
        )
        losing_bundle = self.fixture._seed_b1_bundle(
            store,
            owner_kind="terminal",
            source_id="foreign-boundary-cohort-loser",
        )
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self.fixture._claim(
                store,
                bundle=losing_bundle,
                created_at="2026-08-04T12:00:05.000000Z",
                lease_until="2026-08-04T12:10:00.000000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_multirow_blocked_replay_rejects_released_historical_owner(self):
        store = self.fixture._store()
        bundle, _binding = self.fixture._seed_prerequisites(
            store,
            row_ids=[self.fixture.first, self.fixture.second],
        )
        self.fixture._install_contact_owner(store, self.fixture.first)
        (
            blocked_claim,
            blocked_generation,
            blocked_head,
        ) = self.fixture._install_contact_owner(
            store,
            self.fixture.second,
        )
        original = self.fixture._claim(store, bundle=bundle)
        self.assertEqual(
            ["dominated", "dominated"],
            [
                decision["decision"]
                for decision in original["claimSet"]["rowDecisions"]
            ],
        )
        claim_ref = self.fixture._claim_reference(
            store,
            original["claimSet"]["requestId"],
        )
        forged = deepcopy(store.data[claim_ref.path])
        forged["rowDecisions"][1].update(
            {
                "decision": "blocked_by_claim_set",
                "winnerGenerationHash": None,
                "winnerSettlementHash": None,
            }
        )
        forged = self.fixture._rehash_claim(forged)
        claim_ref.set(forged, merge=False)

        blocked_settlement = self.module.build_owner_settlement_document(
            generation_document=blocked_generation,
            claim_set_document=blocked_claim,
            fencing_token=blocked_head["fencingToken"],
            outcome="contact_optout",
            settled_at="2026-08-04T12:00:03.000000Z",
            superseded_effective_settlement_hash=None,
        )
        settled_head = self.module._build_settlement_advanced_head(
            expected_head=blocked_head,
            generation_document=blocked_generation,
            settlement_document=blocked_settlement,
        )
        self.fixture._settlement_reference(
            store,
            self.fixture.second,
            1,
        ).create(blocked_settlement)
        self.fixture._row_references(
            store,
            self.fixture.second,
        )[1].set(settled_head, merge=False)
        _release_result, released_head = self._release_to(
            store,
            released_generation=blocked_generation,
            released_settlement=blocked_settlement,
            settled_head=settled_head,
            fanout_id=blocked_claim["fanoutId"],
            restored_generation=None,
            restored_settlement=None,
            released_at="2026-08-04T12:00:04.000000Z",
            cycle=251,
            row_id=self.fixture.second,
        )
        self.assertEqual("clear", released_head["state"])
        self.assertIsNone(released_head["effectiveOwnerGeneration"])
        self.assertEqual(
            blocked_settlement["settlementHash"],
            released_head["latestSettlementHash"],
        )
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityError):
            self.fixture._claim(store, bundle=bundle)

        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_multirow_blocked_replay_survives_earlier_and_later_releases(self):
        store = self.fixture._store()
        bundle, _binding = self.fixture._seed_prerequisites(
            store,
            row_ids=[self.fixture.first, self.fixture.second],
        )
        first_claim, first_generation, first_claimed_head = (
            self.fixture._install_contact_owner(
                store,
                self.fixture.first,
            )
        )
        first_settlement = self.module.build_owner_settlement_document(
            generation_document=first_generation,
            claim_set_document=first_claim,
            fencing_token=first_claimed_head["fencingToken"],
            outcome="contact_optout",
            settled_at="2026-08-04T12:00:03.000000Z",
        )
        first_settled_head = self.module._build_settlement_advanced_head(
            expected_head=first_claimed_head,
            generation_document=first_generation,
            settlement_document=first_settlement,
        )
        self.fixture._settlement_reference(
            store,
            self.fixture.first,
            1,
        ).create(first_settlement)
        self.fixture._row_references(
            store,
            self.fixture.first,
        )[1].set(first_settled_head, merge=False)
        _first_release, clear_head = self._release_to(
            store,
            released_generation=first_generation,
            released_settlement=first_settlement,
            settled_head=first_settled_head,
            fanout_id=first_claim["fanoutId"],
            restored_generation=None,
            restored_settlement=None,
            released_at="2026-08-04T12:00:04.000000Z",
            cycle=331,
        )
        self.fixture._install_contact_owner(store, self.fixture.second)
        original = self.fixture._claim(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:00:05.000000Z",
            lease_until="2026-08-04T12:10:00.000000Z",
        )
        self.assertEqual(
            ["blocked_by_claim_set", "dominated"],
            [
                decision["decision"]
                for decision in original["claimSet"]["rowDecisions"]
            ],
        )
        (
            _second_claim,
            second_generation,
            second_settlement,
            second_settled_head,
            second_fanout_id,
        ) = self._install_contact_settlement(
            store,
            expected_head=clear_head,
            generation_number=2,
            first_fence=2,
            predecessor_settlement=None,
            created_at="2026-08-04T12:00:06.000000Z",
            settled_at="2026-08-04T12:00:07.000000Z",
            cycle=332,
        )
        self._release_to(
            store,
            released_generation=second_generation,
            released_settlement=second_settlement,
            settled_head=second_settled_head,
            fanout_id=second_fanout_id,
            restored_generation=None,
            restored_settlement=None,
            released_at="2026-08-04T12:00:08.000000Z",
            cycle=332,
        )
        before = deepcopy(store.data)
        store.events.clear()

        replay = self.fixture._claim(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:00:05.000000Z",
            lease_until="2026-08-04T12:10:00.000000Z",
        )

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(original["claimSet"], replay["claimSet"])
        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_blocked_replay_survives_later_active_owner_after_restoration(self):
        store = self.fixture._store()
        bundle, _binding = self.fixture._seed_prerequisites(
            store,
            row_ids=[self.fixture.first, self.fixture.second],
        )
        self.fixture._install_contact_owner(store, self.fixture.first)
        human_generation, human_settlement, human_head = (
            self._install_human_owner_for_row(
                store,
                row_id=self.fixture.second,
                suffix="restored-active",
                issued_at="2026-08-04T12:00:03.000000Z",
            )
        )
        (
            _first_claim,
            first_generation,
            first_settlement,
            first_head,
            first_fanout_id,
        ) = self._install_contact_settlement(
            store,
            expected_head=human_head,
            generation_number=2,
            first_fence=2,
            predecessor_settlement=human_settlement,
            created_at="2026-08-04T12:00:03.500000Z",
            settled_at="2026-08-04T12:00:04.000000Z",
            cycle=441,
            row_id=self.fixture.second,
        )
        _release, restored_head = self._release_to(
            store,
            released_generation=first_generation,
            released_settlement=first_settlement,
            settled_head=first_head,
            fanout_id=first_fanout_id,
            restored_generation=human_generation,
            restored_settlement=human_settlement,
            released_at="2026-08-04T12:00:05.000000Z",
            cycle=441,
            row_id=self.fixture.second,
        )
        claim_at = "2026-08-04T12:00:06.000000Z"
        original = self.fixture._claim(
            store,
            bundle=bundle,
            created_at=claim_at,
            lease_until="2026-08-04T12:11:00.000000Z",
        )
        self.assertEqual(
            ["dominated", "blocked_by_claim_set"],
            [
                decision["decision"]
                for decision in original["claimSet"]["rowDecisions"]
            ],
        )
        self._install_contact_settlement(
            store,
            expected_head=restored_head,
            generation_number=3,
            first_fence=3,
            predecessor_settlement=human_settlement,
            created_at="2026-08-04T12:00:07.000000Z",
            settled_at="2026-08-04T12:00:08.000000Z",
            cycle=442,
            row_id=self.fixture.second,
        )
        before = deepcopy(store.data)
        store.events.clear()

        replay = self.fixture._claim(
            store,
            bundle=bundle,
            created_at=claim_at,
            lease_until="2026-08-04T12:11:00.000000Z",
        )

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(original["claimSet"], replay["claimSet"])
        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_blocked_replay_prefers_direct_preclaim_owner_over_old_restoration(self):
        store = self.fixture._store()
        bundle, _binding = self.fixture._seed_prerequisites(
            store,
            row_ids=[self.fixture.first, self.fixture.second],
        )
        self.fixture._install_contact_owner(store, self.fixture.first)
        human_generation, human_settlement, human_head = (
            self._install_human_owner_for_row(
                store,
                row_id=self.fixture.second,
                suffix="preclaim-direct-owner",
                issued_at="2026-08-04T12:00:03.000000Z",
            )
        )
        (
            contact_claim,
            contact_generation,
            contact_settlement,
            contact_head,
            contact_fanout_id,
        ) = self._install_contact_settlement(
            store,
            expected_head=human_head,
            generation_number=2,
            first_fence=2,
            predecessor_settlement=human_settlement,
            created_at="2026-08-04T12:00:03.500000Z",
            settled_at="2026-08-04T12:00:04.000000Z",
            cycle=445,
            row_id=self.fixture.second,
        )
        _release, restored_head = self._release_to(
            store,
            released_generation=contact_generation,
            released_settlement=contact_settlement,
            settled_head=contact_head,
            fanout_id=contact_fanout_id,
            restored_generation=human_generation,
            restored_settlement=human_settlement,
            released_at="2026-08-04T12:00:05.000000Z",
            cycle=445,
            row_id=self.fixture.second,
        )
        terminal_claim, terminal_generation, terminal_head = (
            self._forged_accepted_generation(
                expected_head=restored_head,
                owner_kind="terminal",
                generation_number=3,
                first_fencing_token=3,
                predecessor_settlement_hash=human_settlement[
                    "settlementHash"
                ],
                created_at="2026-08-04T12:00:05.500000Z",
                row_id=self.fixture.second,
            )
        )
        self.fixture._claim_reference(
            store,
            terminal_claim["requestId"],
        ).create(terminal_claim)
        self.fixture._generation_reference(
            store,
            self.fixture.second,
            3,
        ).create(terminal_generation)
        self.fixture._row_references(
            store,
            self.fixture.second,
        )[1].set(terminal_head, merge=False)

        claim_at = "2026-08-04T12:00:06.000000Z"
        original = self.fixture._claim(
            store,
            bundle=bundle,
            created_at=claim_at,
            lease_until="2026-08-04T12:11:00.000000Z",
        )
        self.assertEqual(
            ["dominated", "dominated"],
            [
                decision["decision"]
                for decision in original["claimSet"]["rowDecisions"]
            ],
        )
        claim_ref = self.fixture._claim_reference(
            store,
            original["claimSet"]["requestId"],
        )
        forged_stored_claim = deepcopy(store.data[claim_ref.path])
        forged_stored_claim["rowDecisions"][1].update(
            {
                "decision": "blocked_by_claim_set",
                "winnerGenerationHash": None,
                "winnerSettlementHash": None,
            }
        )
        claim_ref.set(
            self.fixture._rehash_claim(forged_stored_claim),
            merge=False,
        )

        successor_at = "2026-08-04T12:00:07.000000Z"
        successor_link = self._contact_link(
            source_id="blocked-preclaim-successor"
        )
        successor_claim = self.module.build_claim_set_document(
            user_scope_hash=self.fixture.scope,
            authority_origin="contact_fanout",
            authority_link=successor_link,
            operator_action_document=None,
            fanout_id=f"{455:064x}",
            row_ids=[self.fixture.second],
            primary_row_id=self.fixture.second,
            planned_writes=4,
            outcome="accepted",
            row_decisions=[
                {
                    "rowId": self.fixture.second,
                    "decision": "accepted",
                    "plannedGeneration": 4,
                    "winnerGenerationHash": None,
                    "winnerSettlementHash": None,
                }
            ],
            created_at=successor_at,
            canonical_mailbox_identity_hash=successor_link[
                "canonicalMailboxIdentityHash"
            ],
            contact_settlement_hash=f"{456:064x}",
        )
        successor_generation = self.module.build_owner_generation_document(
            claim_set_document=successor_claim,
            row_id=self.fixture.second,
            generation=4,
            predecessor_head_hash=terminal_head["headHash"],
            predecessor_settlement_hash=human_settlement[
                "settlementHash"
            ],
            lease_epoch=1,
            first_fencing_token=4,
            created_at=successor_at,
        )
        terminal_dominated = self.module.build_owner_settlement_document(
            generation_document=terminal_generation,
            claim_set_document=terminal_claim,
            fencing_token=terminal_head["fencingToken"],
            outcome="dominated",
            dominant_generation_hash=successor_generation[
                "generationHash"
            ],
            settled_at=successor_at,
        )
        successor_head = self.module._build_claim_advanced_head(
            expected_head=terminal_head,
            generation_document=successor_generation,
            lease_owner_hash="d" * 64,
            lease_until="2026-08-04T12:12:00.000000Z",
            dominated_predecessor_settlement_hash=terminal_dominated[
                "settlementHash"
            ],
            claimed_at=successor_at,
        )
        self.fixture._settlement_reference(
            store,
            self.fixture.second,
            3,
        ).create(terminal_dominated)
        self.fixture._claim_reference(
            store,
            successor_claim["requestId"],
        ).create(successor_claim)
        self.fixture._generation_reference(
            store,
            self.fixture.second,
            4,
        ).create(successor_generation)
        self.fixture._row_references(
            store,
            self.fixture.second,
        )[1].set(successor_head, merge=False)
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self.fixture._claim(
                store,
                bundle=bundle,
                created_at=claim_at,
                lease_until="2026-08-04T12:11:00.000000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_blocked_replay_rejects_equal_time_predecessor_release(self):
        store = self.fixture._store()
        bundle, _binding = self.fixture._seed_prerequisites(
            store,
            row_ids=[self.fixture.first, self.fixture.second],
        )
        self.fixture._install_contact_owner(store, self.fixture.first)
        human_generation, human_settlement, human_head = (
            self._install_human_owner_for_row(
                store,
                row_id=self.fixture.second,
                suffix="equal-predecessor-release",
                issued_at="2026-08-04T12:00:03.000000Z",
            )
        )
        (
            _first_claim,
            first_generation,
            first_settlement,
            first_head,
            first_fanout_id,
        ) = self._install_contact_settlement(
            store,
            expected_head=human_head,
            generation_number=2,
            first_fence=2,
            predecessor_settlement=human_settlement,
            created_at="2026-08-04T12:00:03.500000Z",
            settled_at="2026-08-04T12:00:04.000000Z",
            cycle=443,
            row_id=self.fixture.second,
        )
        claim_at = "2026-08-04T12:00:06.000000Z"
        _release, restored_head = self._release_to(
            store,
            released_generation=first_generation,
            released_settlement=first_settlement,
            settled_head=first_head,
            fanout_id=first_fanout_id,
            restored_generation=human_generation,
            restored_settlement=human_settlement,
            released_at=claim_at,
            cycle=443,
            row_id=self.fixture.second,
        )
        original = self.fixture._claim(
            store,
            bundle=bundle,
            created_at=claim_at,
            lease_until="2026-08-04T12:11:00.000000Z",
        )
        self.assertEqual(
            ["dominated", "blocked_by_claim_set"],
            [
                decision["decision"]
                for decision in original["claimSet"]["rowDecisions"]
            ],
        )
        self._install_contact_settlement(
            store,
            expected_head=restored_head,
            generation_number=3,
            first_fence=3,
            predecessor_settlement=human_settlement,
            created_at="2026-08-04T12:00:07.000000Z",
            settled_at="2026-08-04T12:00:08.000000Z",
            cycle=444,
            row_id=self.fixture.second,
        )
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.fixture._claim(
                store,
                bundle=bundle,
                created_at=claim_at,
                lease_until="2026-08-04T12:11:00.000000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_blocked_replay_survives_later_release_to_prior_human(self):
        store = self.fixture._store()
        bundle, _binding = self.fixture._seed_prerequisites(
            store,
            row_ids=[self.fixture.first, self.fixture.second],
        )
        self.fixture._install_contact_owner(store, self.fixture.first)
        human_binding = self.module.build_thread_row_binding_document(
            user_scope_hash=self.fixture.scope,
            thread_id="thread-human-row-two",
            client_id="client-1",
            row_ids=[self.fixture.second],
            primary_row_id=self.fixture.second,
            created_at=self.fixture.binding_at,
        )
        user = self.fixture._user_reference(store)
        user.collection("threadRowBindings").document(
            human_binding["threadId"]
        ).create(human_binding)
        for edge in self.module.build_row_thread_binding_documents(
            thread_binding_document=human_binding
        ):
            user.collection("rowThreadBindings").document(
                edge["edgeId"]
            ).create(edge)
        human = self.fixture._authority(store).record_operator_decline(
            verified_user_id=self.fixture.user_id,
            thread_id=human_binding["threadId"],
            actor_scope_hash="5" * 64,
            client_request_id="blocked-restored-human",
            issued_at="2026-08-04T12:00:03.000000Z",
        )
        human_generation = human["generations"][0]
        human_settlement = human["settlements"][0]
        human_settled_head = human["heads"][0]
        (
            _first_contact_claim,
            first_contact_generation,
            first_contact_settlement,
            first_contact_head,
            first_fanout_id,
        ) = self._install_contact_settlement(
            store,
            expected_head=human_settled_head,
            generation_number=2,
            first_fence=2,
            predecessor_settlement=human_settlement,
            created_at="2026-08-04T12:00:03.500000Z",
            settled_at="2026-08-04T12:00:04.000000Z",
            cycle=430,
            row_id=self.fixture.second,
        )
        _release, restored_head = self._release_to(
            store,
            released_generation=first_contact_generation,
            released_settlement=first_contact_settlement,
            settled_head=first_contact_head,
            fanout_id=first_fanout_id,
            restored_generation=human_generation,
            restored_settlement=human_settlement,
            released_at="2026-08-04T12:00:05.000000Z",
            cycle=430,
            row_id=self.fixture.second,
        )
        original = self.fixture._claim(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:00:06.000000Z",
            lease_until="2026-08-04T12:11:00.000000Z",
        )
        self.assertEqual(
            ["dominated", "blocked_by_claim_set"],
            [
                decision["decision"]
                for decision in original["claimSet"]["rowDecisions"]
            ],
        )
        (
            _second_contact_claim,
            second_contact_generation,
            second_contact_settlement,
            second_contact_head,
            second_fanout_id,
        ) = self._install_contact_settlement(
            store,
            expected_head=restored_head,
            generation_number=3,
            first_fence=3,
            predecessor_settlement=human_settlement,
            created_at="2026-08-04T12:00:07.000000Z",
            settled_at="2026-08-04T12:00:08.000000Z",
            cycle=431,
            row_id=self.fixture.second,
        )
        self._release_to(
            store,
            released_generation=second_contact_generation,
            released_settlement=second_contact_settlement,
            settled_head=second_contact_head,
            fanout_id=second_fanout_id,
            restored_generation=human_generation,
            restored_settlement=human_settlement,
            released_at="2026-08-04T12:00:09.000000Z",
            cycle=431,
            row_id=self.fixture.second,
        )
        before = deepcopy(store.data)
        store.events.clear()

        replay = self.fixture._claim(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:00:06.000000Z",
            lease_until="2026-08-04T12:11:00.000000Z",
        )

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(original["claimSet"], replay["claimSet"])
        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_dominated_replay_requires_exact_winner_successor_bracket(self):
        store = self.fixture._store()
        bundle, _binding = self.fixture._seed_prerequisites(store)
        winner_claim, winner_generation, winner_head = (
            self.fixture._install_contact_owner(
                store,
                self.fixture.first,
            )
        )
        original = self.fixture._claim(store, bundle=bundle)
        self.assertEqual("dominated", original["disposition"])
        self.assertEqual(
            winner_generation["generationHash"],
            original["claimSet"]["rowDecisions"][0][
                "winnerGenerationHash"
            ],
        )
        winner_settlement = self.module.build_owner_settlement_document(
            generation_document=winner_generation,
            claim_set_document=winner_claim,
            fencing_token=winner_head["fencingToken"],
            outcome="contact_optout",
            settled_at="2026-08-04T12:00:03.000000Z",
            superseded_effective_settlement_hash=None,
        )
        winner_settled_head = self.module._build_settlement_advanced_head(
            expected_head=winner_head,
            generation_document=winner_generation,
            settlement_document=winner_settlement,
        )
        self.fixture._settlement_reference(
            store,
            self.fixture.first,
            1,
        ).create(winner_settlement)
        self.fixture._row_references(
            store,
            self.fixture.first,
        )[1].set(winner_settled_head, merge=False)
        _release, clear_head = self._release_to(
            store,
            released_generation=winner_generation,
            released_settlement=winner_settlement,
            settled_head=winner_settled_head,
            fanout_id=winner_claim["fanoutId"],
            restored_generation=None,
            restored_settlement=None,
            released_at="2026-08-04T12:00:04.000000Z",
            cycle=401,
        )
        for generation_number, minute, cycle in (
            (2, 5, 402),
            (3, 8, 403),
        ):
            (
                cycle_claim,
                cycle_generation,
                cycle_settlement,
                cycle_settled_head,
                cycle_fanout_id,
            ) = self._install_contact_settlement(
                store,
                expected_head=clear_head,
                generation_number=generation_number,
                first_fence=generation_number,
                predecessor_settlement=None,
                created_at=(
                    f"2026-08-04T12:00:{minute:02d}.000000Z"
                ),
                settled_at=(
                    f"2026-08-04T12:00:{minute + 1:02d}.000000Z"
                ),
                cycle=cycle,
            )
            _release, clear_head = self._release_to(
                store,
                released_generation=cycle_generation,
                released_settlement=cycle_settlement,
                settled_head=cycle_settled_head,
                fanout_id=cycle_fanout_id,
                restored_generation=None,
                restored_settlement=None,
                released_at=(
                    f"2026-08-04T12:00:{minute + 2:02d}.000000Z"
                ),
                cycle=cycle,
            )
            self.assertEqual(
                cycle_claim["requestId"],
                cycle_generation["requestId"],
            )
        self._install_contact_settlement(
            store,
            expected_head=clear_head,
            generation_number=4,
            first_fence=4,
            predecessor_settlement=None,
            created_at="2026-08-04T12:00:11.000000Z",
            settled_at="2026-08-04T12:00:12.000000Z",
            cycle=404,
        )
        del store.data[
            self.fixture._generation_reference(
                store,
                self.fixture.first,
                2,
            ).path
        ]
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self.fixture._claim(store, bundle=bundle)

        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_dominated_replay_rejects_orphan_winner_ahead_of_history(self):
        store, _bundle, _binding = self._seed(owner_kind="terminal")
        _identity_ref, head_ref = self.fixture._row_references(
            store,
            self.fixture.first,
        )
        winner_claim, winner_generation, _forged_head = (
            self._forged_accepted_generation(
                expected_head=deepcopy(store.data[head_ref.path]),
                owner_kind="terminal",
                generation_number=5,
                first_fencing_token=5,
                predecessor_settlement_hash=None,
                created_at="2026-08-04T12:00:03.000000Z",
            )
        )
        self.fixture._claim_reference(
            store,
            winner_claim["requestId"],
        ).create(winner_claim)
        self.fixture._generation_reference(
            store,
            self.fixture.first,
            5,
        ).create(winner_generation)
        losing_bundle = self.fixture._seed_b1_bundle(
            store,
            owner_kind="human_decision",
            source_id="orphan-winner-loser",
        )
        authority_link = self.module.build_b1_authority_link(
            user_scope_hash=self.fixture.scope,
            source_identity_document=losing_bundle["identity"],
            source_classification_document=losing_bundle[
                "classification"
            ],
            source_owner_document=losing_bundle["owner"],
            source_ledger_document=losing_bundle["ledger"],
            work_key=losing_bundle["work_key"],
        )
        claim_at = "2026-08-04T12:00:04.000000Z"
        forged = self.module.build_claim_set_document(
            user_scope_hash=self.fixture.scope,
            authority_origin="b1_source",
            authority_link=authority_link,
            operator_action_document=None,
            fanout_id=None,
            row_ids=[self.fixture.first],
            primary_row_id=self.fixture.first,
            planned_writes=1,
            outcome="dominated",
            row_decisions=[
                {
                    "rowId": self.fixture.first,
                    "decision": "dominated",
                    "plannedGeneration": None,
                    "winnerGenerationHash": winner_generation[
                        "generationHash"
                    ],
                    "winnerSettlementHash": None,
                }
            ],
            created_at=claim_at,
        )
        self.fixture._claim_reference(
            store,
            forged["requestId"],
        ).create(forged)
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self.fixture._claim(
                store,
                bundle=losing_bundle,
                created_at=claim_at,
                lease_until="2026-08-04T12:09:00.000000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_dominated_replay_accepts_winner_restored_before_claim(self):
        (
            store,
            _terminal_bundle,
            _binding,
            restored_generation,
            restored_settlement,
            _released_head,
        ) = self._seed_released_human(cycles=1)
        losing_bundle = self.fixture._seed_b1_bundle(
            store,
            owner_kind="human_decision",
            source_id="restored-winner-loser",
        )
        original = self.fixture._claim(
            store,
            bundle=losing_bundle,
            created_at="2026-08-04T12:06:00.000000Z",
            lease_until="2026-08-04T12:11:00.000000Z",
        )
        self.assertEqual("dominated", original["disposition"])
        decision = original["claimSet"]["rowDecisions"][0]
        self.assertEqual(
            restored_generation["generationHash"],
            decision["winnerGenerationHash"],
        )
        self.assertEqual(
            restored_settlement["settlementHash"],
            decision["winnerSettlementHash"],
        )
        before = deepcopy(store.data)
        store.events.clear()

        replay = self.fixture._claim(
            store,
            bundle=losing_bundle,
            created_at="2026-08-04T12:06:00.000000Z",
            lease_until="2026-08-04T12:11:00.000000Z",
        )

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(original["claimSet"], replay["claimSet"])
        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_dominated_replay_accepts_latest_repeated_restoration(self):
        store, _bundle, _binding, _generation, _settlement, _head = (
            self._seed_released_human(cycles=2)
        )
        losing_bundle = self.fixture._seed_b1_bundle(
            store,
            owner_kind="human_decision",
            source_id="repeated-restored-winner-loser",
        )
        original = self.fixture._claim(
            store,
            bundle=losing_bundle,
            created_at="2026-08-04T12:09:00.000000Z",
            lease_until="2026-08-04T12:14:00.000000Z",
        )
        self.assertEqual("dominated", original["disposition"])
        before = deepcopy(store.data)
        store.events.clear()

        replay = self.fixture._claim(
            store,
            bundle=losing_bundle,
            created_at="2026-08-04T12:09:00.000000Z",
            lease_until="2026-08-04T12:14:00.000000Z",
        )

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(original["claimSet"], replay["claimSet"])
        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_dominated_replay_accepts_postclaim_exit_from_restored_winner(self):
        (
            store,
            _bundle,
            _binding,
            _restored_generation,
            restored_settlement,
            restored_head,
        ) = self._seed_released_human(cycles=1)
        losing_bundle = self.fixture._seed_b1_bundle(
            store,
            owner_kind="human_decision",
            source_id="postclaim-restored-winner-loser",
        )
        original = self.fixture._claim(
            store,
            bundle=losing_bundle,
            created_at="2026-08-04T12:06:00.000000Z",
            lease_until="2026-08-04T12:11:00.000000Z",
        )
        self.assertEqual("dominated", original["disposition"])
        self._install_contact_settlement(
            store,
            expected_head=restored_head,
            generation_number=3,
            first_fence=3,
            predecessor_settlement=restored_settlement,
            created_at="2026-08-04T12:07:00.000000Z",
            settled_at="2026-08-04T12:08:00.000000Z",
            cycle=406,
        )
        before = deepcopy(store.data)
        store.events.clear()

        replay = self.fixture._claim(
            store,
            bundle=losing_bundle,
            created_at="2026-08-04T12:06:00.000000Z",
            lease_until="2026-08-04T12:11:00.000000Z",
        )

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(original["claimSet"], replay["claimSet"])
        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_dominated_replay_survives_later_released_contact_cycle(self):
        (
            store,
            _bundle,
            _binding,
            restored_generation,
            restored_settlement,
            restored_head,
        ) = self._seed_released_human(cycles=1)
        losing_bundle = self.fixture._seed_b1_bundle(
            store,
            owner_kind="human_decision",
            source_id="later-released-restored-winner-loser",
        )
        original = self.fixture._claim(
            store,
            bundle=losing_bundle,
            created_at="2026-08-04T12:06:00.000000Z",
            lease_until="2026-08-04T12:11:00.000000Z",
        )
        (
            _contact_claim,
            contact_generation,
            contact_settlement,
            contact_head,
            fanout_id,
        ) = self._install_contact_settlement(
            store,
            expected_head=restored_head,
            generation_number=3,
            first_fence=3,
            predecessor_settlement=restored_settlement,
            created_at="2026-08-04T12:07:00.000000Z",
            settled_at="2026-08-04T12:08:00.000000Z",
            cycle=407,
        )
        self._release_to(
            store,
            released_generation=contact_generation,
            released_settlement=contact_settlement,
            settled_head=contact_head,
            fanout_id=fanout_id,
            restored_generation=restored_generation,
            restored_settlement=restored_settlement,
            released_at="2026-08-04T12:09:00.000000Z",
            cycle=407,
        )
        before = deepcopy(store.data)
        store.events.clear()

        replay = self.fixture._claim(
            store,
            bundle=losing_bundle,
            created_at="2026-08-04T12:06:00.000000Z",
            lease_until="2026-08-04T12:11:00.000000Z",
        )

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(original["claimSet"], replay["claimSet"])
        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_dominated_replay_uses_exact_restoration_exit_before_later_cycle(self):
        (
            store,
            _bundle,
            _binding,
            _restored_generation,
            restored_settlement,
            restored_head,
        ) = self._seed_released_human(cycles=1)
        losing_bundle = self.fixture._seed_b1_bundle(
            store,
            owner_kind="human_decision",
            source_id="deep-restored-winner-loser",
        )
        original = self.fixture._claim(
            store,
            bundle=losing_bundle,
            created_at="2026-08-04T12:06:00.000000Z",
            lease_until="2026-08-04T12:11:00.000000Z",
        )
        self.assertEqual("dominated", original["disposition"])

        terminal_bundle = self.fixture._seed_b1_bundle(
            store,
            owner_kind="terminal",
            source_id="deep-restored-winner-exit",
        )
        terminal_claim = self.fixture._claim(
            store,
            bundle=terminal_bundle,
            created_at="2026-08-04T12:07:00.000000Z",
            lease_until="2026-08-04T12:12:00.000000Z",
        )
        self.assertEqual("created", terminal_claim["disposition"])
        terminal_generation = terminal_claim["generations"][0]
        self.assertEqual(3, terminal_generation["generation"])
        terminal_settlement, terminal_head = (
            self.fixture._settle_terminal_owner(
                store,
                terminal_claim["claimSet"],
                terminal_generation,
                terminal_claim["heads"][0],
                settled_at="2026-08-04T12:08:00.000000Z",
            )
        )

        (
            _contact_claim,
            contact_generation,
            contact_settlement,
            contact_head,
            fanout_id,
        ) = self._install_contact_settlement(
            store,
            expected_head=terminal_head,
            generation_number=4,
            first_fence=4,
            predecessor_settlement=terminal_settlement,
            created_at="2026-08-04T12:09:00.000000Z",
            settled_at="2026-08-04T12:10:00.000000Z",
            cycle=408,
        )
        self._release_to(
            store,
            released_generation=contact_generation,
            released_settlement=contact_settlement,
            settled_head=contact_head,
            fanout_id=fanout_id,
            restored_generation=terminal_generation,
            restored_settlement=terminal_settlement,
            released_at="2026-08-04T12:11:00.000000Z",
            cycle=408,
        )
        before = deepcopy(store.data)
        store.events.clear()

        replay = self.fixture._claim(
            store,
            bundle=losing_bundle,
            created_at="2026-08-04T12:06:00.000000Z",
            lease_until="2026-08-04T12:11:00.000000Z",
        )

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(original["claimSet"], replay["claimSet"])
        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_dominated_replay_orders_same_time_restorations_by_generation(self):
        (
            store,
            _bundle,
            _binding,
            restored_generation,
            restored_settlement,
            restored_head,
        ) = self._seed_released_human(cycles=1)
        (
            _contact_claim,
            contact_generation,
            contact_settlement,
            contact_head,
            fanout_id,
        ) = self._install_contact_settlement(
            store,
            expected_head=restored_head,
            generation_number=3,
            first_fence=3,
            predecessor_settlement=restored_settlement,
            created_at="2026-08-04T12:05:00.000000Z",
            settled_at="2026-08-04T12:05:00.000000Z",
            cycle=9,
        )
        second_release, _released_head = self._release_to(
            store,
            released_generation=contact_generation,
            released_settlement=contact_settlement,
            settled_head=contact_head,
            fanout_id=fanout_id,
            restored_generation=restored_generation,
            restored_settlement=restored_settlement,
            released_at="2026-08-04T12:05:00.000000Z",
            cycle=9,
        )
        self.assertGreater(
            restored_head["latestOptOutReleaseResultHash"],
            second_release["contactFanoutResultHash"],
        )
        losing_bundle = self.fixture._seed_b1_bundle(
            store,
            owner_kind="human_decision",
            source_id="same-time-restored-winner-loser",
        )
        original = self.fixture._claim(
            store,
            bundle=losing_bundle,
            created_at="2026-08-04T12:06:00.000000Z",
            lease_until="2026-08-04T12:11:00.000000Z",
        )
        self.assertEqual("dominated", original["disposition"])
        before = deepcopy(store.data)
        store.events.clear()

        replay = self.fixture._claim(
            store,
            bundle=losing_bundle,
            created_at="2026-08-04T12:06:00.000000Z",
            lease_until="2026-08-04T12:11:00.000000Z",
        )

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(original["claimSet"], replay["claimSet"])
        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_dominated_replay_rejects_winner_restored_after_claim(self):
        (
            store,
            _terminal_bundle,
            binding,
            restored_generation,
            restored_settlement,
            restored_head,
        ) = self._seed_released_human(cycles=0)
        (
            _contact_claim,
            contact_generation,
            contact_settlement,
            contact_head,
            fanout_id,
        ) = self._install_contact_settlement(
            store,
            expected_head=restored_head,
            generation_number=2,
            first_fence=2,
            predecessor_settlement=restored_settlement,
            created_at="2026-08-04T12:04:00.000000Z",
            settled_at="2026-08-04T12:05:00.000000Z",
            cycle=405,
        )
        losing_bundle = self.fixture._seed_b1_bundle(
            store,
            owner_kind="human_decision",
            source_id="late-restored-winner-loser",
        )
        authority_link = self.module.build_b1_authority_link(
            user_scope_hash=self.fixture.scope,
            source_identity_document=losing_bundle["identity"],
            source_classification_document=losing_bundle[
                "classification"
            ],
            source_owner_document=losing_bundle["owner"],
            source_ledger_document=losing_bundle["ledger"],
            work_key=losing_bundle["work_key"],
        )
        claim_at = "2026-08-04T12:05:30.000000Z"
        forged = self.module.build_claim_set_document(
            user_scope_hash=self.fixture.scope,
            authority_origin="b1_source",
            authority_link=authority_link,
            operator_action_document=None,
            fanout_id=None,
            row_ids=[self.fixture.first],
            primary_row_id=self.fixture.first,
            planned_writes=1,
            outcome="dominated",
            row_decisions=[
                {
                    "rowId": self.fixture.first,
                    "decision": "dominated",
                    "plannedGeneration": None,
                    "winnerGenerationHash": restored_generation[
                        "generationHash"
                    ],
                    "winnerSettlementHash": restored_settlement[
                        "settlementHash"
                    ],
                }
            ],
            created_at=claim_at,
        )
        self.assertEqual(
            binding["rowBindingsHash"],
            forged["rowBindingsHash"],
        )
        self.fixture._claim_reference(
            store,
            forged["requestId"],
        ).create(forged)
        self._release_to(
            store,
            released_generation=contact_generation,
            released_settlement=contact_settlement,
            settled_head=contact_head,
            fanout_id=fanout_id,
            restored_generation=restored_generation,
            restored_settlement=restored_settlement,
            released_at="2026-08-04T12:06:00.000000Z",
            cycle=405,
        )
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self.fixture._claim(
                store,
                bundle=losing_bundle,
                created_at=claim_at,
                lease_until="2026-08-04T12:10:30.000000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_dominated_replay_rejects_winner_released_before_claim(self):
        state = self._seed_released_clear()
        store = state["store"]
        bundle = state["bundle"]
        winner_generation = state["releasedGeneration"]
        winner_settlement = state["releasedSettlement"]
        authority_link = self.module.build_b1_authority_link(
            user_scope_hash=self.fixture.scope,
            source_identity_document=bundle["identity"],
            source_classification_document=bundle["classification"],
            source_owner_document=bundle["owner"],
            source_ledger_document=bundle["ledger"],
            work_key=bundle["work_key"],
        )
        claim_at = "2026-08-04T12:00:06.000000Z"
        forged = self.module.build_claim_set_document(
            user_scope_hash=self.fixture.scope,
            authority_origin="b1_source",
            authority_link=authority_link,
            operator_action_document=None,
            fanout_id=None,
            row_ids=[self.fixture.first],
            primary_row_id=self.fixture.first,
            planned_writes=1,
            outcome="dominated",
            row_decisions=[
                {
                    "rowId": self.fixture.first,
                    "decision": "dominated",
                    "plannedGeneration": None,
                    "winnerGenerationHash": winner_generation[
                        "generationHash"
                    ],
                    "winnerSettlementHash": winner_settlement[
                        "settlementHash"
                    ],
                }
            ],
            created_at=claim_at,
        )
        self.fixture._claim_reference(
            store,
            forged["requestId"],
        ).create(forged)
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self.fixture._claim(
                store,
                bundle=bundle,
                created_at=claim_at,
                lease_until="2026-08-04T12:11:00.000000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_release_bridge_survives_two_later_dominated_settlements(self):
        state = self._seed_released_clear()
        store = state["store"]
        human_bundle = self.fixture._seed_b1_bundle(
            store,
            owner_kind="human_decision",
            source_id="deep-release-human",
        )
        human = self.fixture._claim(
            store,
            bundle=human_bundle,
            created_at="2026-08-04T12:00:06.000000Z",
            lease_until="2026-08-04T12:05:06.000000Z",
        )
        terminal_bundle = self.fixture._seed_b1_bundle(
            store,
            owner_kind="terminal",
            source_id="deep-release-terminal",
        )
        terminal = self.fixture._claim(
            store,
            bundle=terminal_bundle,
            created_at="2026-08-04T12:00:07.000000Z",
            lease_until="2026-08-04T12:05:07.000000Z",
        )
        authority_link = self._contact_link(
            source_id="deep-release-contact"
        )
        contact = self.module._plan_contact_fanout_row_claim(
            user_scope_hash=self.fixture.scope,
            authority_link=authority_link,
            fanout_id="e" * 64,
            canonical_mailbox_identity_hash=authority_link[
                "canonicalMailboxIdentityHash"
            ],
            contact_settlement_hash="f" * 64,
            thread_binding_document=None,
            canonical_row_id=self.fixture.first,
            row_states=[self._bounded_state_from_store(store)],
            stored_claim_set_document=None,
            created_at="2026-08-04T12:00:08.000000Z",
            lease_owner_hash="d" * 64,
            lease_until="2026-08-04T12:05:08.000000Z",
        )
        self._apply_claim_plan(store, contact)
        latest = self._bounded_state_from_store(store)["latestSettlements"]
        self.assertEqual([3, 2], [item["document"]["generation"] for item in latest])
        self.assertEqual(
            ["dominated", "dominated"],
            [item["document"]["outcome"] for item in latest],
        )
        self.assertEqual(2, human["generations"][0]["generation"])
        self.assertEqual(3, terminal["generations"][0]["generation"])
        self.assertEqual(4, contact["generations"][0]["generation"])
        losing_bundle = self.fixture._seed_b1_bundle(
            store,
            owner_kind="terminal",
            source_id="deep-release-loser",
        )

        result = self.fixture._claim(
            store,
            bundle=losing_bundle,
            created_at="2026-08-04T12:00:09.000000Z",
            lease_until="2026-08-04T12:05:09.000000Z",
        )

        self.assertEqual("dominated", result["disposition"])
        self.assertEqual(contact["heads"][0], result["heads"][0])

    def test_lease_takeover_rejects_missing_historical_release_bridge(self):
        state = self._seed_released_clear()
        store = state["store"]
        claimed = self.fixture._claim(
            store,
            bundle=state["bundle"],
            created_at="2026-08-04T12:00:06.000000Z",
            lease_until="2026-08-04T12:05:06.000000Z",
        )
        result_paths = [
            path
            for path in store.data
            if "/contactOptOutFanoutResults/" in path
        ]
        self.assertEqual(1, len(result_paths))
        del store.data[result_paths[0]]
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityError):
            self.fixture._authority(store).take_over_expired_lease(
                verified_user_id=self.fixture.user_id,
                row_id=self.fixture.first,
                expected_head=claimed["heads"][0],
                new_lease_owner_hash="e" * 64,
                new_lease_until="2026-08-04T12:11:00.000000Z",
                taken_at="2026-08-04T12:06:00.000000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], self.fixture._write_events(store))

    def test_claim_commit_uncertainty_rechecks_bounded_query_snapshot(self):
        store, bundle, _binding = self._seed(owner_kind="terminal")
        (
            _action,
            _claim,
            _generation,
            human_settlement,
            _head,
        ) = self.fixture._install_settled_human_owner(
            store,
            self.fixture.first,
        )
        duplicate_ref = self.fixture._user_reference(store).collection(
            "rowOwnerSettlements"
        ).document(f"{self.fixture.first}--commit-uncertainty-duplicate")

        def apply_then_drift_query(transaction, callback):
            transaction._begin()
            callback(transaction)
            transaction._commit()
            duplicate_ref.create(deepcopy(human_settlement))
            raise RuntimeError("claim commit outcome query drifted")

        try:
            self.fixture._claim(
                store,
                bundle=bundle,
                executor=apply_then_drift_query,
                created_at="2026-08-04T12:00:04.000000Z",
                lease_until="2026-08-04T12:09:00.000000Z",
            )
        except self.module.RowAuthorityAmbiguous:
            pass
        except self.module.RowAuthorityError as exc:
            self.fail(
                "post-commit bounded-query drift must be ambiguity, not "
                f"{type(exc).__name__}: {exc}"
            )
        else:
            self.fail(
                "claim commit readback ignored a changed bounded query snapshot"
            )
        self.assertIn(duplicate_ref.path, store.data)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
