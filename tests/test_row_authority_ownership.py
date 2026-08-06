"""Provider-free B2-B row ownership contracts."""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from threading import Barrier
from unittest.mock import patch


def _row_id(index):
    return f"sr1_{index:012x}4{index:03x}8{index:015x}"


def _independent_hash(domain, payload, *, user_scope_hash):
    material = {
        **payload,
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


def _independent_b1_hash(value):
    canonical = _independent_b1_bytes(value)
    return hashlib.sha256(canonical).hexdigest()


def _independent_b1_bytes(value):
    return json.dumps(
        value,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class RowBindingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")

    def setUp(self):
        required = (
            "normalize_row_bindings",
            "build_thread_row_binding_document",
            "validate_thread_row_binding_document",
            "build_row_thread_binding_documents",
            "validate_row_thread_binding_document",
        )
        missing = [name for name in required if not hasattr(self.module, name)]
        self.assertEqual([], missing, f"missing binding contracts: {missing}")
        self.scope = "a" * 64
        self.first = _row_id(1)
        self.second = _row_id(2)
        self.created_at = "2026-08-04T12:00:00.000000Z"

    def _build_thread_binding(self, **overrides):
        values = {
            "user_scope_hash": self.scope,
            "thread_id": "thread-1",
            "client_id": "client-1",
            "row_ids": [self.second, self.first],
            "primary_row_id": self.first,
            "created_at": self.created_at,
        }
        values.update(overrides)
        return self.module.build_thread_row_binding_document(**values)

    def test_binding_domains_are_registered_and_match_independent_vectors(self):
        expected_domains = {
            "ROW_BINDINGS_HASH_DOMAIN": "sitesift.row.bindings.v1",
            "THREAD_ROW_BINDING_HASH_DOMAIN": (
                "sitesift.thread.row_binding.v1"
            ),
            "ROW_THREAD_EDGE_ID_DOMAIN": (
                "sitesift.row.thread_edge_id.v1"
            ),
            "ROW_THREAD_EDGE_HASH_DOMAIN": "sitesift.row.thread_edge.v1",
        }
        self.assertEqual(
            expected_domains,
            {
                name: getattr(self.module, name, None)
                for name in expected_domains
            },
        )

        binding = self._build_thread_binding()
        binding_material = {
            "rowBindings": [
                {"rowId": self.first, "role": "primary"},
                {"rowId": self.second, "role": "related"},
            ],
            "primaryRowId": self.first,
            "bindingCount": 2,
        }
        row_bindings_hash = _independent_hash(
            expected_domains["ROW_BINDINGS_HASH_DOMAIN"],
            binding_material,
            user_scope_hash=self.scope,
        )
        self.assertEqual(
            "d8bb718a29cb3f56abb00bd6761871fe1eff75db035d4a08bb62b30ea3d776df",
            row_bindings_hash,
        )
        self.assertEqual(row_bindings_hash, binding["rowBindingsHash"])
        binding_hash = _independent_hash(
            expected_domains["THREAD_ROW_BINDING_HASH_DOMAIN"],
            {
                "threadId": "thread-1",
                "clientId": "client-1",
                "rowBindingsHash": row_bindings_hash,
                "primaryRowId": self.first,
                "bindingCount": 2,
                "createdAt": self.created_at,
            },
            user_scope_hash=self.scope,
        )
        self.assertEqual(
            "13552fc113cefebb069115b9ac7f485673e7b711fe60ddfbc20a60961b13d3e6",
            binding_hash,
        )
        self.assertEqual(binding_hash, binding["bindingHash"])

        reverse = self.module.build_row_thread_binding_documents(
            thread_binding_document=binding
        )
        self.assertEqual(binding["bindingCount"], len(reverse))
        self.assertEqual(
            [self.first, self.second],
            [edge["rowId"] for edge in reverse],
        )
        first_edge_id = _independent_hash(
            expected_domains["ROW_THREAD_EDGE_ID_DOMAIN"],
            {"rowId": self.first, "threadId": "thread-1"},
            user_scope_hash=self.scope,
        )
        self.assertEqual(
            "b311dcaaf971997104be2bb202464f29feb9f43ed3646e11fd69999dce1c880b",
            first_edge_id,
        )
        self.assertEqual(first_edge_id, reverse[0]["edgeId"])
        first_edge_hash = _independent_hash(
            expected_domains["ROW_THREAD_EDGE_HASH_DOMAIN"],
            {
                "edgeId": first_edge_id,
                "rowId": self.first,
                "threadId": "thread-1",
                "role": "primary",
                "threadBindingHash": binding_hash,
                "createdAt": self.created_at,
            },
            user_scope_hash=self.scope,
        )
        self.assertEqual(
            "400d6356aba31f4b5a04f7440bb1f10fdc64c263fded27a0818908d99c6600f4",
            first_edge_hash,
        )
        self.assertEqual(first_edge_hash, reverse[0]["edgeHash"])

        second_edge_id = _independent_hash(
            expected_domains["ROW_THREAD_EDGE_ID_DOMAIN"],
            {"rowId": self.second, "threadId": "thread-1"},
            user_scope_hash=self.scope,
        )
        self.assertEqual(
            "8f5bfb1371cfc486024112c9f87bf1a6e0870fd4295f16ee7625c98f265feb93",
            second_edge_id,
        )
        second_edge_hash = _independent_hash(
            expected_domains["ROW_THREAD_EDGE_HASH_DOMAIN"],
            {
                "edgeId": second_edge_id,
                "rowId": self.second,
                "threadId": "thread-1",
                "role": "related",
                "threadBindingHash": binding_hash,
                "createdAt": self.created_at,
            },
            user_scope_hash=self.scope,
        )
        self.assertEqual(
            "a8a92fe52c1226a0275c913d5bf7d845447ec240f48312f85f637470a00c4a22",
            second_edge_hash,
        )
        self.assertEqual(second_edge_id, reverse[1]["edgeId"])
        self.assertEqual("related", reverse[1]["role"])
        self.assertEqual(second_edge_hash, reverse[1]["edgeHash"])
        self.assertEqual(
            reverse[1],
            self.module.validate_row_thread_binding_document(
                document=reverse[1]
            ),
        )

    def test_binding_hashes_change_for_every_field_scope_null_order_and_domain(self):
        binding = self._build_thread_binding()
        changed_documents = (
            self._build_thread_binding(thread_id="thread-2"),
            self._build_thread_binding(client_id="client-2"),
            self._build_thread_binding(
                row_ids=[self.first, _row_id(3)],
                primary_row_id=self.first,
            ),
            self._build_thread_binding(primary_row_id=self.second),
            self._build_thread_binding(
                created_at="2026-08-04T12:00:01.000000Z"
            ),
            self._build_thread_binding(user_scope_hash="b" * 64),
        )
        self.assertEqual(
            len(changed_documents),
            len({item["bindingHash"] for item in changed_documents}),
        )
        self.assertNotIn(
            binding["bindingHash"],
            {item["bindingHash"] for item in changed_documents},
        )

        payload = {
            "threadId": "thread-1",
            "clientId": "client-1",
            "rowBindingsHash": binding["rowBindingsHash"],
            "primaryRowId": self.first,
            "bindingCount": 2,
            "createdAt": self.created_at,
        }
        variants = (
            _independent_hash(
                "sitesift.thread.row_binding.v2",
                payload,
                user_scope_hash=self.scope,
            ),
            _independent_hash(
                "sitesift.thread.row_binding.v1",
                {**payload, "clientId": None},
                user_scope_hash=self.scope,
            ),
            _independent_hash(
                "sitesift.thread.row_binding.v1",
                payload,
                user_scope_hash="b" * 64,
            ),
            _independent_hash(
                "sitesift.row.bindings.v1",
                {
                    "rowBindings": list(reversed(binding["rowBindings"])),
                    "primaryRowId": self.first,
                    "bindingCount": 2,
                },
                user_scope_hash=self.scope,
            ),
        )
        self.assertEqual(4, len(set(variants)))
        self.assertNotIn(binding["bindingHash"], variants)
        self.assertNotEqual(binding["rowBindingsHash"], variants[-1])

    def test_row_binding_normalization_deduplicates_sorts_and_preserves_one_primary(self):
        actual = self.module.normalize_row_bindings(
            [self.second, self.first, self.second, self.first],
            self.second,
        )
        self.assertEqual(
            [
                {"rowId": self.first, "role": "related"},
                {"rowId": self.second, "role": "primary"},
            ],
            actual,
        )
        self.assertEqual(
            1,
            sum(item["role"] == "primary" for item in actual),
        )
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self.module.normalize_row_bindings([], self.first)
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self.module.normalize_row_bindings([self.first], self.second)

    def test_persisted_binding_rejects_empty_missing_primary_duplicate_unsorted_and_drift(self):
        binding = self._build_thread_binding()
        invalid_documents = []

        empty = deepcopy(binding)
        empty["rowBindings"] = []
        empty["bindingCount"] = 0
        invalid_documents.append(empty)

        missing_primary = deepcopy(binding)
        missing_primary["rowBindings"][0]["role"] = "related"
        invalid_documents.append(missing_primary)

        duplicate = deepcopy(binding)
        duplicate["rowBindings"][1]["rowId"] = self.first
        invalid_documents.append(duplicate)

        unsorted = deepcopy(binding)
        unsorted["rowBindings"].reverse()
        invalid_documents.append(unsorted)

        primary_drift = deepcopy(binding)
        primary_drift["primaryRowId"] = self.second
        invalid_documents.append(primary_drift)

        count_drift = deepcopy(binding)
        count_drift["bindingCount"] = 1
        invalid_documents.append(count_drift)

        hash_drift = deepcopy(binding)
        hash_drift["bindingHash"] = "f" * 64
        invalid_documents.append(hash_drift)

        unknown = deepcopy(binding)
        unknown["extra"] = None
        invalid_documents.append(unknown)

        for document in invalid_documents:
            with self.subTest(document=document), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self.module.validate_thread_row_binding_document(
                    document=document
                )

        reverse = self.module.build_row_thread_binding_documents(
            thread_binding_document=binding
        )
        for field, value in (
            ("edgeId", "f" * 64),
            ("edgeHash", "f" * 64),
            ("role", "other"),
            ("threadBindingHash", "f" * 64),
        ):
            drifted = deepcopy(reverse[0])
            drifted[field] = value
            with self.subTest(reverse_field=field), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self.module.validate_row_thread_binding_document(
                    document=drifted
                )

    def test_128_unique_bindings_succeed_and_129_fail_before_reference_or_transaction(self):
        rows = [_row_id(index) for index in range(1, 130)]
        normalized = self.module.normalize_row_bindings(rows[:128], rows[0])
        self.assertEqual(128, len(normalized))
        self.assertEqual(rows, sorted(rows))
        duplicate_raw = rows[:128] + rows[:128]
        self.assertEqual(
            128,
            len(
                self.module.normalize_row_bindings(
                    duplicate_raw,
                    rows[0],
                )
            ),
        )
        with patch.object(
            self.module,
            "domain_hash",
            side_effect=AssertionError("hash/reference creation was reached"),
        ) as hash_spy, self.assertRaises(
            self.module.RowAuthorityConfigError
        ):
            self._build_thread_binding(
                row_ids=rows,
                primary_row_id=rows[0],
            )
        hash_spy.assert_not_called()

    def test_unsafe_thread_document_ids_fail_before_hash_or_reference_creation(self):
        invalid = (
            "a/b",
            ".",
            "..",
            "__reserved__",
            "thread\n1",
            chr(0xD800),
            "x" * 1501,
        )
        for thread_id in invalid:
            with self.subTest(thread_id=repr(thread_id)), patch.object(
                self.module,
                "domain_hash",
                side_effect=AssertionError("hash/reference creation was reached"),
            ) as hash_spy, self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self._build_thread_binding(thread_id=thread_id)
            hash_spy.assert_not_called()

    def test_binding_builders_and_validators_are_defensive(self):
        raw_rows = [self.second, self.first]
        binding = self._build_thread_binding(row_ids=raw_rows)
        raw_rows.clear()
        self.assertEqual(2, binding["bindingCount"])

        validated = self.module.validate_thread_row_binding_document(
            document=binding
        )
        validated["rowBindings"][0]["role"] = "related"
        self.assertEqual("primary", binding["rowBindings"][0]["role"])

        reverse = self.module.build_row_thread_binding_documents(
            thread_binding_document=binding
        )
        reverse[0]["role"] = "related"
        fresh_reverse = self.module.build_row_thread_binding_documents(
            thread_binding_document=binding
        )
        self.assertEqual("primary", fresh_reverse[0]["role"])
        validated_edge = self.module.validate_row_thread_binding_document(
            document=fresh_reverse[0]
        )
        validated_edge["role"] = "related"
        self.assertEqual("primary", fresh_reverse[0]["role"])


class ContactRowBindingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")

    def setUp(self):
        required = (
            "build_contact_row_binding_document",
            "validate_contact_row_binding_document",
            "build_contact_row_binding_evidence_document",
            "validate_contact_row_binding_evidence_document",
            "build_contact_row_binding_head_document",
            "validate_contact_row_binding_head_document",
        )
        missing = [name for name in required if not hasattr(self.module, name)]
        self.assertEqual([], missing, f"missing contact contracts: {missing}")
        self.scope = "a" * 64
        self.canonical_hash = "b" * 64
        self.exact_hash = "c" * 64
        self.thread_binding_hash = "d" * 64
        self.row_id = _row_id(1)
        self.created_at = "2026-08-04T12:00:00.000000Z"

    def _build_association(self, **overrides):
        values = {
            "user_scope_hash": self.scope,
            "canonical_mailbox_identity_hash": self.canonical_hash,
            "row_id": self.row_id,
            "created_at": self.created_at,
        }
        values.update(overrides)
        return self.module.build_contact_row_binding_document(**values)

    def _build_evidence(self, association=None, **overrides):
        association = association or self._build_association()
        values = {
            "user_scope_hash": self.scope,
            "edge_id": association["edgeId"],
            "thread_id": "thread-1",
            "thread_binding_hash": self.thread_binding_hash,
            "exact_identity_hash": self.exact_hash,
            "created_at": self.created_at,
        }
        values.update(overrides)
        return self.module.build_contact_row_binding_evidence_document(
            **values
        )

    def _build_head(self, association=None, **overrides):
        association = association or self._build_association()
        values = {
            "user_scope_hash": self.scope,
            "canonical_mailbox_identity_hash": self.canonical_hash,
            "state_revision": 1,
            "association_count": 1,
            "last_association_hash": association["contactRowEdgeHash"],
            "created_at": self.created_at,
            "updated_at": self.created_at,
        }
        values.update(overrides)
        return self.module.build_contact_row_binding_head_document(**values)

    def test_contact_binding_domains_match_independent_vectors(self):
        expected_domains = {
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
        }
        self.assertEqual(
            expected_domains,
            {
                name: getattr(self.module, name, None)
                for name in expected_domains
            },
        )
        association = self._build_association()
        expected_edge_id = _independent_hash(
            expected_domains["CONTACT_ROW_EDGE_ID_DOMAIN"],
            {
                "canonicalMailboxIdentityHash": self.canonical_hash,
                "rowId": self.row_id,
            },
            user_scope_hash=self.scope,
        )
        self.assertEqual(
            "81a0efc45fde76ab67173bd47d0039d9e43c6cff1824ab45f6b92fda1a68acd6",
            expected_edge_id,
        )
        self.assertEqual(expected_edge_id, association["edgeId"])
        expected_edge_hash = _independent_hash(
            expected_domains["CONTACT_ROW_EDGE_HASH_DOMAIN"],
            {
                "edgeId": expected_edge_id,
                "canonicalMailboxIdentityHash": self.canonical_hash,
                "rowId": self.row_id,
                "createdAt": self.created_at,
            },
            user_scope_hash=self.scope,
        )
        self.assertEqual(
            "6524938aee3f0f109865f9a4a81238576cb7643057bb40bf01db6fb7ce62187a",
            expected_edge_hash,
        )
        self.assertEqual(
            expected_edge_hash,
            association["contactRowEdgeHash"],
        )

        evidence = self._build_evidence(association)
        expected_evidence_id = _independent_hash(
            expected_domains["CONTACT_ROW_EVIDENCE_ID_DOMAIN"],
            {
                "edgeId": expected_edge_id,
                "threadBindingHash": self.thread_binding_hash,
                "exactIdentityHash": self.exact_hash,
            },
            user_scope_hash=self.scope,
        )
        self.assertEqual(
            "579a4af540e52753cb39d9592797113077389e5f141648d2e870af51d22f7352",
            expected_evidence_id,
        )
        self.assertEqual(expected_evidence_id, evidence["evidenceId"])
        expected_evidence_hash = _independent_hash(
            expected_domains["CONTACT_ROW_EVIDENCE_HASH_DOMAIN"],
            {
                "evidenceId": expected_evidence_id,
                "edgeId": expected_edge_id,
                "threadId": "thread-1",
                "threadBindingHash": self.thread_binding_hash,
                "exactIdentityHash": self.exact_hash,
                "createdAt": self.created_at,
            },
            user_scope_hash=self.scope,
        )
        self.assertEqual(
            "46dbab1814b647be2ab3418903da5610696ea69eaaaae30377d4c41efaeef600",
            expected_evidence_hash,
        )
        self.assertEqual(
            expected_evidence_hash,
            evidence["contactRowEvidenceHash"],
        )

        head = self._build_head(association)
        expected_head_hash = _independent_hash(
            expected_domains["CONTACT_ROW_BINDING_HEAD_HASH_DOMAIN"],
            {
                "canonicalMailboxIdentityHash": self.canonical_hash,
                "stateRevision": 1,
                "associationCount": 1,
                "lastAssociationHash": expected_edge_hash,
                "createdAt": self.created_at,
                "updatedAt": self.created_at,
            },
            user_scope_hash=self.scope,
        )
        self.assertEqual(
            "a03195d984a297c9176ffe491b3a6aff70936aac680a36a6952c2cecee52590a",
            expected_head_hash,
        )
        self.assertEqual(
            expected_head_hash,
            head["contactRowBindingHeadHash"],
        )

    def test_contact_edge_identity_is_stable_across_thread_evidence(self):
        association = self._build_association()
        first_evidence = self._build_evidence(association)
        second_evidence = self._build_evidence(
            association,
            thread_id="thread-2",
            thread_binding_hash="e" * 64,
        )
        rebuilt = self._build_association(
            created_at="2026-08-04T12:00:01.000000Z"
        )
        self.assertEqual(association["edgeId"], rebuilt["edgeId"])
        self.assertNotEqual(
            association["contactRowEdgeHash"],
            rebuilt["contactRowEdgeHash"],
        )
        self.assertEqual(association["edgeId"], first_evidence["edgeId"])
        self.assertEqual(association["edgeId"], second_evidence["edgeId"])
        self.assertNotEqual(
            first_evidence["evidenceId"],
            second_evidence["evidenceId"],
        )

    def test_contact_evidence_identity_changes_with_thread_binding_or_exact_identity(self):
        baseline = self._build_evidence()
        binding_changed = self._build_evidence(thread_binding_hash="e" * 64)
        exact_changed = self._build_evidence(exact_identity_hash="f" * 64)
        self.assertEqual(
            3,
            len(
                {
                    baseline["evidenceId"],
                    binding_changed["evidenceId"],
                    exact_changed["evidenceId"],
                }
            ),
        )
        thread_only_changed = self._build_evidence(thread_id="thread-2")
        self.assertEqual(
            baseline["evidenceId"],
            thread_only_changed["evidenceId"],
        )
        self.assertNotEqual(
            baseline["contactRowEvidenceHash"],
            thread_only_changed["contactRowEvidenceHash"],
        )

    def test_contact_binding_head_accepts_absent_initial_and_exact_empty_shapes(self):
        association = self._build_association()
        first = self._build_head(association)
        self.assertEqual(
            first,
            self.module.validate_contact_row_binding_head_document(
                document=first
            ),
        )
        empty = self._build_head(
            association,
            association_count=0,
            last_association_hash=None,
        )
        self.assertEqual(
            "44c537e55e44e395a02871d594710351963efa57492fdcb28f2e4d21c0a78d99",
            empty["contactRowBindingHeadHash"],
        )
        self.assertEqual(
            empty,
            self.module.validate_contact_row_binding_head_document(
                document=empty
            ),
        )

    def test_contact_binding_head_does_not_invent_revision_count_ordering(self):
        association = self._build_association()
        try:
            head = self._build_head(
                association,
                state_revision=1,
                association_count=2,
            )
        except self.module.RowAuthorityConfigError as exc:
            self.fail(
                "the approved schema does not order associationCount "
                f"against stateRevision: {exc}"
            )
        self.assertEqual(1, head["stateRevision"])
        self.assertEqual(2, head["associationCount"])
        self.assertEqual(
            head,
            self.module.validate_contact_row_binding_head_document(
                document=head
            ),
        )

    def test_contact_schemas_reject_missing_unknown_mistyped_null_count_hash_and_time(self):
        association = self._build_association()
        evidence = self._build_evidence(association)
        head = self._build_head(association)

        invalid_associations = []
        for field in ("edgeId", "canonicalMailboxIdentityHash", "createdAt"):
            missing = deepcopy(association)
            del missing[field]
            invalid_associations.append(missing)
        unknown = deepcopy(association)
        unknown["unknown"] = None
        invalid_associations.append(unknown)
        mistyped = deepcopy(association)
        mistyped["rowId"] = True
        invalid_associations.append(mistyped)
        drifted = deepcopy(association)
        drifted["contactRowEdgeHash"] = "f" * 64
        invalid_associations.append(drifted)
        for document in invalid_associations:
            with self.subTest(kind="association", document=document), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self.module.validate_contact_row_binding_document(
                    document=document
                )

        invalid_evidence = []
        missing = deepcopy(evidence)
        del missing["threadBindingHash"]
        invalid_evidence.append(missing)
        unknown = deepcopy(evidence)
        unknown["unknown"] = None
        invalid_evidence.append(unknown)
        mistyped = deepcopy(evidence)
        mistyped["threadId"] = 1
        invalid_evidence.append(mistyped)
        drifted = deepcopy(evidence)
        drifted["evidenceId"] = "f" * 64
        invalid_evidence.append(drifted)
        for document in invalid_evidence:
            with self.subTest(kind="evidence", document=document), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self.module.validate_contact_row_binding_evidence_document(
                    document=document
                )

        invalid_heads = []
        for field in ("associationCount", "lastAssociationHash", "updatedAt"):
            missing = deepcopy(head)
            del missing[field]
            invalid_heads.append(missing)
        unknown = deepcopy(head)
        unknown["unknown"] = None
        invalid_heads.append(unknown)
        bool_count = deepcopy(head)
        bool_count["associationCount"] = True
        invalid_heads.append(bool_count)
        null_with_count = deepcopy(head)
        null_with_count["lastAssociationHash"] = None
        invalid_heads.append(null_with_count)
        hash_with_zero = deepcopy(head)
        hash_with_zero["associationCount"] = 0
        invalid_heads.append(hash_with_zero)
        backward_time = deepcopy(head)
        backward_time["updatedAt"] = "2026-08-04T11:59:59.000000Z"
        invalid_heads.append(backward_time)
        drifted = deepcopy(head)
        drifted["contactRowBindingHeadHash"] = "f" * 64
        invalid_heads.append(drifted)
        for document in invalid_heads:
            with self.subTest(kind="head", document=document), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self.module.validate_contact_row_binding_head_document(
                    document=document
                )

    def test_contact_builders_and_validators_are_defensive(self):
        association = self._build_association()
        evidence = self._build_evidence(association)
        head = self._build_head(association)
        for document, validator in (
            (association, self.module.validate_contact_row_binding_document),
            (
                evidence,
                self.module.validate_contact_row_binding_evidence_document,
            ),
            (head, self.module.validate_contact_row_binding_head_document),
        ):
            original = deepcopy(document)
            validated = validator(document=document)
            validated[next(iter(validated))] = "mutated"
            self.assertEqual(original, document)


class RowOwnershipContractTests(unittest.TestCase):
    OWNERSHIP_DOMAINS = {
        "B1_AUTHORITY_LINK_HASH_DOMAIN": (
            "sitesift.row.b1_authority_link.v1"
        ),
        "B1_CONTACT_AUTHORITY_LINK_HASH_DOMAIN": (
            "sitesift.row.b1_authority_link.v2"
        ),
        "OPERATOR_ACTION_ID_DOMAIN": "sitesift.row.operator_action_id.v1",
        "OPERATOR_CLIENT_REQUEST_HASH_DOMAIN": (
            "sitesift.row.operator_client_request.v1"
        ),
        "OPERATOR_ACTION_HASH_DOMAIN": "sitesift.row.operator_action.v1",
        "CLAIM_REQUEST_ID_DOMAIN": "sitesift.row.claim_request_id.v1",
        "CLAIM_SET_HASH_DOMAIN": "sitesift.row.claim_set.v1",
        "OWNER_GENERATION_HASH_DOMAIN": (
            "sitesift.row.owner_generation.v1"
        ),
        "LOGICAL_OUTCOME_HASH_DOMAIN": "sitesift.row.logical_outcome.v1",
        "OUTCOME_EVIDENCE_HASH_DOMAIN": (
            "sitesift.row.outcome_evidence.v1"
        ),
        "OWNER_SETTLEMENT_HASH_DOMAIN": (
            "sitesift.row.owner_settlement.v1"
        ),
        "ROW_AUTHORITY_HEAD_HASH_DOMAIN": (
            "sitesift.row.authority_head.v1"
        ),
        "SOURCE_SETTLEMENT_LINK_HASH_DOMAIN": (
            "sitesift.row.source_settlement_link.v1"
        ),
    }

    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")

    def setUp(self):
        self.scope = "a" * 64
        self.other_scope = "b" * 64
        self.first = _row_id(1)
        self.second = _row_id(2)
        self.created_at = "2026-08-04T12:00:00.000000Z"
        self.later_at = "2026-08-04T12:00:01.000000Z"

    def _b1_bundle(
        self,
        *,
        owner_kind="terminal",
        model_contact_optout=False,
        contact_evidence_version=1,
        source_id="source-1",
    ):
        b1_created = datetime(2026, 8, 4, 11, 0, tzinfo=timezone.utc)
        snapshot_at = datetime(2026, 8, 4, 11, 1, tzinfo=timezone.utc)
        if owner_kind == "contact_optout":
            deterministic_evidence = {
                "schemaVersion": contact_evidence_version,
                "evidenceKind": "header_list_unsubscribe",
                "evidenceHash": "7" * 64,
            }
            if contact_evidence_version == 2:
                deterministic_evidence.update(
                    {
                        "exactIdentityHash": "4" * 64,
                        "canonicalMailboxIdentityHash": "5" * 64,
                    }
                )
            elif contact_evidence_version != 1:
                raise AssertionError(
                    "contact evidence fixture version must be 1 or 2"
                )
            deterministic_hash = _independent_b1_hash(
                deterministic_evidence
            )
            candidate = {
                "type": "contact_optout",
                "evidenceHash": deterministic_hash,
            }
            proposal_candidate = deepcopy(candidate)
            model_request_key = None
            model_request_state = "not_applicable"
            request_start_fence = None
            proposal_evidence = None
            proposal_evidence_hash = None
        else:
            deterministic_evidence = None
            deterministic_hash = None
            if owner_kind == "terminal":
                candidate = {
                    "type": "property_unavailable",
                    "property": "A",
                }
            elif owner_kind == "human_decision":
                candidate = {
                    "type": "needs_user_input",
                    "reason": "availability_review",
                }
            else:
                raise AssertionError(f"unsupported fixture owner {owner_kind}")
            model_request_key = "model-request-1"
            model_request_state = "captured"
            request_start_fence = "request-fence-1"
            proposal_evidence = {
                "schemaVersion": 1,
                "evidenceKind": "model_capture",
                "responseHash": "8" * 64,
            }
            proposal_evidence_hash = _independent_b1_hash(
                proposal_evidence
            )
            if model_contact_optout:
                if owner_kind != "human_decision":
                    raise AssertionError(
                        "model opt-out fixture must resolve to human review"
                    )
                proposal_candidate = {
                    "type": "contact_optout",
                    "claimed": True,
                }
                candidate = {
                    "type": "needs_user_input",
                    "reason": "unverified_optout_review",
                    "sourceCandidateHash": _independent_b1_hash(
                        proposal_candidate
                    ),
                }
            else:
                proposal_candidate = deepcopy(candidate)

        transition_candidates = [deepcopy(candidate)]
        ordinary_obligations = []
        complete_proposal = {
            "schemaVersion": 1,
            "transitionCandidates": [deepcopy(proposal_candidate)],
            "ordinaryObligations": deepcopy(ordinary_obligations),
        }
        selected_candidates = [deepcopy(candidate)]
        owner_key = _independent_b1_hash(
            {
                "hashKind": "source-selection-v1",
                "canonicalSourceId": source_id,
                "ownerKind": owner_kind,
                "selectedCandidates": selected_candidates,
            }
        )
        selection_snapshot = {
            "candidateTaxonomyVersion": "source-candidate-taxonomy-v1",
            "ownerKind": owner_kind,
            "ownerKey": owner_key,
            "selectedCandidates": deepcopy(selected_candidates),
            "candidateDominance": [
                {
                    "candidateHash": _independent_b1_hash(candidate),
                    "outcome": "selected",
                }
            ],
            "transitionCandidatesHash": _independent_b1_hash(
                transition_candidates
            ),
            "ordinaryObligationsHash": _independent_b1_hash(
                ordinary_obligations
            ),
        }
        selection_hash = _independent_b1_hash(selection_snapshot)
        complete_proposal_hash = _independent_b1_hash(complete_proposal)
        classification_input_hash = "9" * 64
        snapshot_material = {
            "schemaVersion": 1,
            "hashKind": "source-classification-snapshot-v1",
            "canonicalSourceId": source_id,
            "classificationInputSchemaVersion": 1,
            "classificationInputHash": classification_input_hash,
            "modelRequestKey": model_request_key,
            "completeProposalSnapshot": deepcopy(complete_proposal),
            "completeProposalHash": complete_proposal_hash,
            "transitionCandidates": deepcopy(transition_candidates),
            "ordinaryObligations": deepcopy(ordinary_obligations),
            "selectionSnapshot": deepcopy(selection_snapshot),
            "selectionHash": selection_hash,
            "proposalEvidence": deepcopy(proposal_evidence),
            "proposalEvidenceHash": proposal_evidence_hash,
            "deterministicEvidence": deepcopy(deterministic_evidence),
            "deterministicEvidenceHash": deterministic_hash,
        }
        snapshot_hash = _independent_b1_hash(snapshot_material)
        classification = {
            "schemaVersion": 1,
            "canonicalSourceId": source_id,
            "classificationState": "snapshot_ready",
            "classificationEpoch": 1,
            "classificationClaimId": "classification-claim-1",
            "leaseExpiresAt": datetime(
                2026,
                8,
                4,
                12,
                0,
                tzinfo=timezone.utc,
            ),
            "classificationInputSchemaVersion": 1,
            "classificationInputHash": classification_input_hash,
            "modelRequestKey": model_request_key,
            "modelRequestState": model_request_state,
            "requestStartFence": request_start_fence,
            "completeProposalSnapshot": deepcopy(complete_proposal),
            "completeProposalHash": complete_proposal_hash,
            "transitionCandidates": deepcopy(transition_candidates),
            "ordinaryObligations": deepcopy(ordinary_obligations),
            "selectionSnapshot": deepcopy(selection_snapshot),
            "selectionHash": selection_hash,
            "snapshotImmutableHash": snapshot_hash,
            "proposalEvidence": deepcopy(proposal_evidence),
            "proposalEvidenceHash": proposal_evidence_hash,
            "deterministicEvidence": deepcopy(deterministic_evidence),
            "deterministicEvidenceHash": deterministic_hash,
            "snapshotPersistedAt": snapshot_at,
            "retainedTerminalKind": None,
            "retainedTerminalImmutableHash": None,
            "retainedTerminalRecordHash": None,
            "retainedTerminalBindingHash": None,
            "createdAt": b1_created,
            "updatedAt": snapshot_at,
        }
        owner_immutable = {
            "schemaVersion": 1,
            "canonicalSourceId": source_id,
            "snapshotImmutableHash": snapshot_hash,
            "selectionHash": selection_hash,
            "ownerKind": owner_kind,
            "ownerKey": owner_key,
        }
        owner_hash = _independent_b1_hash(
            {
                "hashKind": "source-transition-owner-v1",
                **owner_immutable,
            }
        )
        owner = {
            **owner_immutable,
            "ownerDecisionHash": owner_hash,
            "revision": 1,
            "createdAt": snapshot_at,
            "updatedAt": snapshot_at,
        }
        payload_hash = _independent_b1_hash(candidate)
        work_key = _independent_b1_hash(
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
        entry = {
            "workKey": work_key,
            "lane": "transition",
            "kind": candidate["type"],
            "payload": deepcopy(candidate),
            "payloadHash": payload_hash,
            "occurrenceOrdinal": 1,
            "selectedOwnerKind": owner_kind,
            "selectedOwnerKey": owner_key,
            "dominanceOutcome": "delegate_owner",
            "completionContract": {
                "schemaVersion": 1,
                "evidenceKind": "owner_delegation",
                "workKind": candidate["type"],
            },
            "state": "pending",
            "resolutionEvidence": None,
            "resolutionEvidenceHash": None,
        }
        immutable_entry_fields = {
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
            field: deepcopy(entry[field])
            for field in sorted(immutable_entry_fields)
        }
        ledger_hash = _independent_b1_hash(
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
        ledger = {
            "schemaVersion": 1,
            "canonicalSourceId": source_id,
            "completeProposalHash": complete_proposal_hash,
            "snapshotImmutableHash": snapshot_hash,
            "selectionHash": selection_hash,
            "ownerDecisionHash": owner_hash,
            "entries": [entry],
            "entryCount": 1,
            "ledgerHash": ledger_hash,
            "revision": 1,
            "createdAt": snapshot_at,
            "updatedAt": snapshot_at,
        }
        identity = {
            "schemaVersion": 1,
            "canonicalSourceId": source_id,
            "creationHash": "1" * 64,
            "verifiedAliases": [
                {
                    "sourceAliasKey": "2" * 64,
                    "aliasType": "graph",
                    "normalizedValueHash": "3" * 64,
                }
            ],
            "threadId": "thread-1",
            "lifecycleState": "pending",
            "createdAt": b1_created,
            "updatedAt": snapshot_at,
        }
        return {
            "identity": identity,
            "classification": classification,
            "owner": owner,
            "ledger": ledger,
            "work_key": work_key,
            "payload_hash": payload_hash,
            "hard_optout_hash": deterministic_hash,
        }

    def _link(
        self,
        owner_kind="terminal",
        *,
        contact_evidence_version=1,
        **overrides,
    ):
        bundle = self._b1_bundle(
            owner_kind=owner_kind,
            contact_evidence_version=contact_evidence_version,
        )
        arguments = {
            "user_scope_hash": self.scope,
            "source_identity_document": bundle["identity"],
            "source_classification_document": bundle["classification"],
            "source_owner_document": bundle["owner"],
            "source_ledger_document": bundle["ledger"],
            "work_key": bundle["work_key"],
        }
        arguments.update(overrides)
        return self.module.build_b1_authority_link(**arguments), bundle

    def _operator(self, **overrides):
        bindings = [
            {
                "rowId": self.first,
                "role": "primary",
            }
        ]
        row_bindings_hash = _independent_hash(
            "sitesift.row.bindings.v1",
            {
                "rowBindings": bindings,
                "primaryRowId": self.first,
                "bindingCount": 1,
            },
            user_scope_hash=self.scope,
        )
        arguments = {
            "user_scope_hash": self.scope,
            "actor_scope_hash": "c" * 64,
            "row_bindings_hash": row_bindings_hash,
            "client_request_id": "request-1",
            "issued_at": self.created_at,
        }
        arguments.update(overrides)
        return self.module.build_operator_action_document(**arguments)

    def _claim(
        self,
        *,
        origin="b1_source",
        outcome="accepted",
        planned_generation=1,
        contact_evidence_version=1,
    ):
        if origin == "b1_source":
            link, _bundle = self._link("terminal")
            operator = None
            fanout_id = None
        elif origin == "authenticated_operator":
            link = None
            operator = self._operator()
            fanout_id = None
        elif origin == "contact_fanout":
            link, _bundle = self._link(
                "contact_optout",
                contact_evidence_version=contact_evidence_version,
            )
            operator = None
            fanout_id = "e" * 64
        else:
            link = None
            operator = None
            fanout_id = None
        if outcome == "accepted":
            decisions = [
                {
                    "rowId": self.first,
                    "decision": "accepted",
                    "plannedGeneration": planned_generation,
                    "winnerGenerationHash": None,
                    "winnerSettlementHash": None,
                }
            ]
            planned_writes = 3
        else:
            decisions = [
                {
                    "rowId": self.first,
                    "decision": "dominated",
                    "plannedGeneration": None,
                    "winnerGenerationHash": "6" * 64,
                    "winnerSettlementHash": None,
                }
            ]
            planned_writes = 1
        return self.module.build_claim_set_document(
            user_scope_hash=self.scope,
            authority_origin=origin,
            authority_link=link,
            operator_action_document=operator,
            fanout_id=fanout_id,
            row_ids=(self.first,),
            primary_row_id=self.first,
            planned_writes=planned_writes,
            outcome=outcome,
            row_decisions=decisions,
            created_at=self.created_at,
            canonical_mailbox_identity_hash=(
                "d" * 64 if origin == "contact_fanout" else None
            ),
            contact_settlement_hash=(
                "4" * 64 if origin == "contact_fanout" else None
            ),
        )

    def _generation(self, *, claim=None, row_id=None, generation=1):
        return self.module.build_owner_generation_document(
            claim_set_document=claim or self._claim(),
            row_id=row_id or self.first,
            generation=generation,
            predecessor_head_hash="7" * 64,
            predecessor_settlement_hash=None,
            lease_epoch=1,
            first_fencing_token=1,
            created_at=self.created_at,
        )

    def _row_head(self):
        identity = self.module.build_row_identity_document(
            user_scope_hash=self.scope,
            row_id=self.first,
            client_id="client-1",
            spreadsheet_id="spreadsheet-1",
            sheet_id=0,
            creation_kind="fresh",
            creation_source_hash="1" * 64,
            created_at=self.created_at,
        )
        observation = self.module.build_row_observation(
            spreadsheet_id="spreadsheet-1",
            marker_observation={
                "rowId": self.first,
                "sheetId": 0,
                "providerRowIndex": 0,
                "displayRowNumber": 1,
                "metadataId": 1,
            },
            ordered_headers=("Email",),
            ordered_cell_values=("row@example.test",),
            user_scope_hash=self.scope,
        )
        revision = self.module.build_row_location_revision_document(
            identity_document=identity,
            revision=1,
            lifecycle="active",
            observations=(observation,),
            previous_revision_hash=None,
            observed_at=self.created_at,
        )
        return self.module.build_initial_row_authority_head(
            identity_document=identity,
            location_revision_document=revision,
            created_at=self.created_at,
        )

    def _ownership_hash_vectors(self):
        authority_link, _bundle = self._link("terminal")
        contact_authority_link, _contact_bundle = self._link(
            "contact_optout",
            contact_evidence_version=2,
        )
        action = self._operator()
        claim = self._claim()
        generation = self._generation(claim=claim)
        settlement = self.module.build_owner_settlement_document(
            generation_document=generation,
            claim_set_document=claim,
            fencing_token=1,
            outcome="terminal",
            settled_at=self.later_at,
        )
        head = self._row_head()
        source_link = self.module.build_source_settlement_link_document(
            user_scope_hash=self.scope,
            row_id=self.first,
            generation=1,
            generation_hash=generation["generationHash"],
            authority_link_hash=claim["authorityLinkHash"],
            b1_identity_hash="6" * 64,
            b1_final_ledger_evidence_hash="7" * 64,
            b1_settlement_revision=1,
            b1_settlement_hash="8" * 64,
            b2_settlement_hash=settlement["settlementHash"],
            linked_at=self.later_at,
        )
        return {
            "B1_AUTHORITY_LINK_HASH_DOMAIN": (
                {
                    "canonicalSourceId": authority_link["canonicalSourceId"],
                    "snapshotImmutableHash": authority_link[
                        "snapshotImmutableHash"
                    ],
                    "selectionHash": authority_link["selectionHash"],
                    "ownerDecisionHash": authority_link["ownerDecisionHash"],
                    "ledgerHash": authority_link["ledgerHash"],
                    "ownerKind": authority_link["ownerKind"],
                    "ownerKey": authority_link["ownerKey"],
                    "workKey": authority_link["workKey"],
                    "payloadHash": authority_link["payloadHash"],
                    "hardOptOutEvidenceHash": authority_link[
                        "hardOptOutEvidenceHash"
                    ],
                },
                authority_link["authorityLinkHash"],
            ),
            "B1_CONTACT_AUTHORITY_LINK_HASH_DOMAIN": (
                {
                    key: deepcopy(value)
                    for key, value in contact_authority_link.items()
                    if key != "authorityLinkHash"
                },
                contact_authority_link["authorityLinkHash"],
            ),
            "OPERATOR_ACTION_ID_DOMAIN": (
                {
                    "actorScopeHash": action["actorScopeHash"],
                    "rowBindingsHash": action["rowBindingsHash"],
                    "clientRequestHash": action["clientRequestHash"],
                    "actionKind": action["actionKind"],
                    "reasonCode": action["reasonCode"],
                    "issuedAt": action["issuedAt"],
                },
                action["actionId"],
            ),
            "OPERATOR_CLIENT_REQUEST_HASH_DOMAIN": (
                {"clientRequestId": "request-1"},
                action["clientRequestHash"],
            ),
            "OPERATOR_ACTION_HASH_DOMAIN": (
                {
                    "actionId": action["actionId"],
                    "actorScopeHash": action["actorScopeHash"],
                    "rowBindingsHash": action["rowBindingsHash"],
                    "clientRequestHash": action["clientRequestHash"],
                    "actionKind": action["actionKind"],
                    "reasonCode": action["reasonCode"],
                    "issuedAt": action["issuedAt"],
                },
                action["operatorActionHash"],
            ),
            "CLAIM_REQUEST_ID_DOMAIN": (
                {
                    "authorityOrigin": claim["authorityOrigin"],
                    "authorityLinkHash": claim["authorityLinkHash"],
                    "operatorActionHash": claim["operatorActionHash"],
                    "fanoutId": claim["fanoutId"],
                    "rowBindingsHash": claim["rowBindingsHash"],
                    "ownerKind": claim["ownerKind"],
                    "ownerKey": claim["ownerKey"],
                    "workKey": claim["workKey"],
                    "payloadHash": claim["payloadHash"],
                },
                claim["requestId"],
            ),
            "CLAIM_SET_HASH_DOMAIN": (
                {
                    "requestId": claim["requestId"],
                    "authorityOrigin": claim["authorityOrigin"],
                    "authorityLinkHash": claim["authorityLinkHash"],
                    "operatorActionHash": claim["operatorActionHash"],
                    "fanoutId": claim["fanoutId"],
                    "rowBindingsHash": claim["rowBindingsHash"],
                    "ownerKind": claim["ownerKind"],
                    "ownerKey": claim["ownerKey"],
                    "derivedPriority": claim["derivedPriority"],
                    "plannedWrites": claim["plannedWrites"],
                    "outcome": claim["outcome"],
                    "rowDecisions": deepcopy(claim["rowDecisions"]),
                    "createdAt": claim["createdAt"],
                },
                claim["claimSetHash"],
            ),
            "OWNER_GENERATION_HASH_DOMAIN": (
                {
                    "rowId": generation["rowId"],
                    "generation": generation["generation"],
                    "requestId": generation["requestId"],
                    "claimSetHash": generation["claimSetHash"],
                    "predecessorHeadHash": generation["predecessorHeadHash"],
                    "predecessorSettlementHash": generation[
                        "predecessorSettlementHash"
                    ],
                    "ownerKind": generation["ownerKind"],
                    "ownerKey": generation["ownerKey"],
                    "priority": generation["priority"],
                    "leaseEpoch": generation["leaseEpoch"],
                    "firstFencingToken": generation["firstFencingToken"],
                    "createdAt": generation["createdAt"],
                },
                generation["generationHash"],
            ),
            "LOGICAL_OUTCOME_HASH_DOMAIN": (
                {
                    "rowId": settlement["rowId"],
                    "generation": settlement["generation"],
                    "ownerKind": generation["ownerKind"],
                    "ownerKey": generation["ownerKey"],
                    "outcome": settlement["outcome"],
                    "outcomeReasonCode": settlement["outcomeReasonCode"],
                    "outcomeEvidenceHash": settlement["outcomeEvidenceHash"],
                },
                settlement["logicalOutcomeHash"],
            ),
            "OUTCOME_EVIDENCE_HASH_DOMAIN": (
                {
                    "authorityLinkHash": claim["authorityLinkHash"],
                    "operatorActionHash": claim["operatorActionHash"],
                    "fanoutId": claim["fanoutId"],
                    "payloadHash": claim["payloadHash"],
                    "outcomeReasonCode": settlement["outcomeReasonCode"],
                },
                settlement["outcomeEvidenceHash"],
            ),
            "OWNER_SETTLEMENT_HASH_DOMAIN": (
                {
                    "rowId": settlement["rowId"],
                    "generation": settlement["generation"],
                    "generationHash": settlement["generationHash"],
                    "fencingToken": settlement["fencingToken"],
                    "outcome": settlement["outcome"],
                    "dominantGenerationHash": settlement[
                        "dominantGenerationHash"
                    ],
                    "supersededEffectiveSettlementHash": settlement[
                        "supersededEffectiveSettlementHash"
                    ],
                    "operatorActionHash": settlement["operatorActionHash"],
                    "outcomeReasonCode": settlement["outcomeReasonCode"],
                    "outcomeEvidenceHash": settlement["outcomeEvidenceHash"],
                    "logicalOutcomeHash": settlement["logicalOutcomeHash"],
                    "settledAt": settlement["settledAt"],
                },
                settlement["settlementHash"],
            ),
            "ROW_AUTHORITY_HEAD_HASH_DOMAIN": (
                {
                    "rowId": head["rowId"],
                    "stateRevision": head["stateRevision"],
                    "currentLocationRevision": head[
                        "currentLocationRevision"
                    ],
                    "currentLocationHash": head["currentLocationHash"],
                    "currentLocationLifecycle": head[
                        "currentLocationLifecycle"
                    ],
                    "effectiveOwnerGeneration": head[
                        "effectiveOwnerGeneration"
                    ],
                    "effectiveOwnerGenerationHash": head[
                        "effectiveOwnerGenerationHash"
                    ],
                    "effectiveOwnerKind": head["effectiveOwnerKind"],
                    "effectivePriority": head["effectivePriority"],
                    "state": head["state"],
                    "leaseOwnerHash": head["leaseOwnerHash"],
                    "leaseUntil": head["leaseUntil"],
                    "fencingToken": head["fencingToken"],
                    "latestSettlementHash": head["latestSettlementHash"],
                    "effectiveSettlementHash": head[
                        "effectiveSettlementHash"
                    ],
                    "latestSourceSettlementLinkHash": head[
                        "latestSourceSettlementLinkHash"
                    ],
                    "latestOptOutReleaseResultHash": head[
                        "latestOptOutReleaseResultHash"
                    ],
                    "projectionBacklogCount": head[
                        "projectionBacklogCount"
                    ],
                    "createdAt": head["createdAt"],
                    "updatedAt": head["updatedAt"],
                },
                head["headHash"],
            ),
            "SOURCE_SETTLEMENT_LINK_HASH_DOMAIN": (
                {
                    "rowId": source_link["rowId"],
                    "generation": source_link["generation"],
                    "generationHash": source_link["generationHash"],
                    "authorityLinkHash": source_link["authorityLinkHash"],
                    "b1IdentityHash": source_link["b1IdentityHash"],
                    "b1FinalLedgerEvidenceHash": source_link[
                        "b1FinalLedgerEvidenceHash"
                    ],
                    "b1SettlementRevision": source_link[
                        "b1SettlementRevision"
                    ],
                    "b1SettlementHash": source_link["b1SettlementHash"],
                    "b2SettlementHash": source_link["b2SettlementHash"],
                    "linkedAt": source_link["linkedAt"],
                },
                source_link["sourceSettlementLinkHash"],
            ),
        }

    def test_all_ownership_domains_are_registered_and_match_independent_vectors(self):
        actual = {
            name: getattr(self.module, name, None)
            for name in self.OWNERSHIP_DOMAINS
        }
        self.assertEqual(self.OWNERSHIP_DOMAINS, actual)
        for index, domain in enumerate(self.OWNERSHIP_DOMAINS.values()):
            payload = {"field": index, "nullable": None}
            expected = _independent_hash(
                domain,
                payload,
                user_scope_hash=self.scope,
            )
            with self.subTest(domain=domain):
                self.assertEqual(
                    expected,
                    self.module.domain_hash(
                        domain,
                        payload,
                        user_scope_hash=self.scope,
                    ),
                )

    def test_every_ownership_hash_changes_for_field_scope_null_time_and_domain_drift(self):
        vectors = self._ownership_hash_vectors()
        self.assertEqual(set(self.OWNERSHIP_DOMAINS), set(vectors))
        self.assertTrue(
            any(
                value is None
                for payload, _actual in vectors.values()
                for value in payload.values()
            )
        )
        self.assertTrue(
            any(
                field.endswith("At")
                for payload, _actual in vectors.values()
                for field in payload
            )
        )

        def drift(value):
            if value is None:
                return "f" * 64
            if type(value) is int:
                return value + 1
            if type(value) is str:
                return f"{value}-drift"
            if type(value) is list:
                return [*deepcopy(value), {"drift": True}]
            if type(value) is dict:
                return {**deepcopy(value), "drift": True}
            raise AssertionError(f"unsupported vector value: {value!r}")

        for name, (payload, actual) in vectors.items():
            domain = self.OWNERSHIP_DOMAINS[name]
            expected = _independent_hash(
                domain,
                payload,
                user_scope_hash=self.scope,
            )
            with self.subTest(name=name, mutation="exact-vector"):
                self.assertEqual(expected, actual)
            for field, value in payload.items():
                mutated = deepcopy(payload)
                mutated[field] = drift(value)
                with self.subTest(name=name, field=field):
                    self.assertNotEqual(
                        actual,
                        _independent_hash(
                            domain,
                            mutated,
                            user_scope_hash=self.scope,
                        ),
                    )
            with self.subTest(name=name, mutation="scope"):
                self.assertNotEqual(
                    actual,
                    _independent_hash(
                        domain,
                        payload,
                        user_scope_hash=self.other_scope,
                    ),
                )
            with self.subTest(name=name, mutation="domain"):
                self.assertNotEqual(
                    actual,
                    _independent_hash(
                        "sitesift.row.domain_drift.v1",
                        payload,
                        user_scope_hash=self.scope,
                    ),
                )

    def test_b1_link_is_derived_from_exact_identity_classification_owner_ledger_bundle(self):
        bundle = self._b1_bundle(owner_kind="terminal")
        original = deepcopy(bundle)

        link, returned_bundle = self._link("terminal")

        expected_without_hash = {
            "canonicalSourceId": "source-1",
            "snapshotImmutableHash": bundle["classification"][
                "snapshotImmutableHash"
            ],
            "selectionHash": bundle["classification"]["selectionHash"],
            "ownerDecisionHash": bundle["owner"]["ownerDecisionHash"],
            "ledgerHash": bundle["ledger"]["ledgerHash"],
            "ownerKind": "terminal",
            "ownerKey": bundle["owner"]["ownerKey"],
            "workKey": bundle["work_key"],
            "payloadHash": bundle["payload_hash"],
            "hardOptOutEvidenceHash": None,
        }
        self.assertEqual(
            {
                **expected_without_hash,
                "authorityLinkHash": _independent_hash(
                    self.OWNERSHIP_DOMAINS[
                        "B1_AUTHORITY_LINK_HASH_DOMAIN"
                    ],
                    expected_without_hash,
                    user_scope_hash=self.scope,
                ),
            },
            link,
        )
        self.assertEqual(original, returned_bundle)

    def test_v1_b1_links_and_downstream_vectors_remain_byte_exact(self):
        expected_link_hashes = {
            "terminal": (
                "1ac2f1ebdaeb99c59d1b3c8a7084d661"
                "65a05fee4c91c02ca8f479379c34c1c3"
            ),
            "human_decision": (
                "27583f112239d94468f657ee57fabd1b7"
                "2f2ca97420899087a7ce1702b770fbf"
            ),
            "contact_optout": (
                "051c1cda498e0a3d08c168e10f80ac5"
                "4f0e29493a1b77e42b53895e92e1147fe"
            ),
        }
        for owner_kind, expected_hash in expected_link_hashes.items():
            with self.subTest(owner_kind=owner_kind):
                link, _bundle = self._link(owner_kind)
                self.assertEqual(expected_hash, link["authorityLinkHash"])
                self.assertEqual(
                    {
                        "canonicalSourceId",
                        "snapshotImmutableHash",
                        "selectionHash",
                        "ownerDecisionHash",
                        "ledgerHash",
                        "ownerKind",
                        "ownerKey",
                        "workKey",
                        "payloadHash",
                        "hardOptOutEvidenceHash",
                        "authorityLinkHash",
                    },
                    set(link),
                )

        claim = self._claim(origin="b1_source")
        generation = self._generation(claim=claim)
        settlement = self.module.build_owner_settlement_document(
            generation_document=generation,
            claim_set_document=claim,
            fencing_token=1,
            outcome="terminal",
            settled_at=self.later_at,
        )
        source_link = self.module.build_source_settlement_link_document(
            user_scope_hash=self.scope,
            row_id=self.first,
            generation=1,
            generation_hash=generation["generationHash"],
            authority_link_hash=claim["authorityLinkHash"],
            b1_identity_hash="6" * 64,
            b1_final_ledger_evidence_hash="7" * 64,
            b1_settlement_revision=1,
            b1_settlement_hash="8" * 64,
            b2_settlement_hash=settlement["settlementHash"],
            linked_at=self.later_at,
        )
        self.assertEqual(
            {
                "requestId": (
                    "e66e01f67bebfa1c78fe2ad0f68c58615"
                    "be1dcf640f2c342a46f532882dfd125"
                ),
                "claimSetHash": (
                    "02af39d11d7fcfb053dd10be309195854c"
                    "d4419c310eefc4efaa79aa17632dd2"
                ),
                "generationHash": (
                    "8cc99b817e9fa2f2165befef364ba60ba"
                    "a83e57d09fb0e130c06c52771b09294"
                ),
                "settlementHash": (
                    "c3d1874800372330d1cabfd7073e4cb41"
                    "bb0714c0bb5d99914f979ef4c172312"
                ),
                "sourceSettlementLinkHash": (
                    "407393167712d3e0bf0d476d4e0884837"
                    "a61e6ceb2f3000784d8393630f89d4e"
                ),
            },
            {
                "requestId": claim["requestId"],
                "claimSetHash": claim["claimSetHash"],
                "generationHash": generation["generationHash"],
                "settlementHash": settlement["settlementHash"],
                "sourceSettlementLinkHash": source_link[
                    "sourceSettlementLinkHash"
                ],
            },
        )

    def test_v2_contact_link_uses_exact_shape_domain_and_bound_hashes(self):
        link, bundle = self._link(
            "contact_optout",
            contact_evidence_version=2,
        )
        evidence = bundle["classification"]["deterministicEvidence"]
        expected_material = {
            "canonicalSourceId": "source-1",
            "snapshotImmutableHash": bundle["classification"][
                "snapshotImmutableHash"
            ],
            "selectionHash": bundle["classification"]["selectionHash"],
            "ownerDecisionHash": bundle["owner"]["ownerDecisionHash"],
            "ledgerHash": bundle["ledger"]["ledgerHash"],
            "ownerKind": "contact_optout",
            "ownerKey": bundle["owner"]["ownerKey"],
            "workKey": bundle["work_key"],
            "payloadHash": bundle["payload_hash"],
            "hardOptOutEvidenceHash": bundle["hard_optout_hash"],
            "exactIdentityHash": evidence["exactIdentityHash"],
            "canonicalMailboxIdentityHash": evidence[
                "canonicalMailboxIdentityHash"
            ],
        }
        self.assertEqual(
            {
                **expected_material,
                "authorityLinkHash": _independent_hash(
                    self.OWNERSHIP_DOMAINS[
                        "B1_CONTACT_AUTHORITY_LINK_HASH_DOMAIN"
                    ],
                    expected_material,
                    user_scope_hash=self.scope,
                ),
            },
            link,
        )
        self.assertEqual(
            "e23bbb1dafe6c155d0781c2ea90600cc"
            "1f02ba8bb7a523f0852d97420079cb8f",
            link["authorityLinkHash"],
        )
        self.assertNotEqual(
            link["authorityLinkHash"],
            _independent_hash(
                self.OWNERSHIP_DOMAINS["B1_AUTHORITY_LINK_HASH_DOMAIN"],
                expected_material,
                user_scope_hash=self.scope,
            ),
        )
        self.assertEqual(
            link,
            self.module.validate_b1_authority_link(
                authority_link=link,
                user_scope_hash=self.scope,
            ),
        )
        self.assertEqual(
            link["hardOptOutEvidenceHash"],
            bundle["classification"]["deterministicEvidenceHash"],
        )
        self.assertEqual(
            link["hardOptOutEvidenceHash"],
            bundle["ledger"]["entries"][0]["payload"]["evidenceHash"],
        )

    def test_b1_link_shape_and_domain_discriminator_is_strict(self):
        legacy, _legacy_bundle = self._link("contact_optout")
        v2, _v2_bundle = self._link(
            "contact_optout",
            contact_evidence_version=2,
        )
        terminal, _terminal_bundle = self._link("terminal")

        legacy_with_v2_keys = {
            **legacy,
            "exactIdentityHash": "4" * 64,
            "canonicalMailboxIdentityHash": "5" * 64,
        }
        v2_material_under_v1 = deepcopy(v2)
        v2_material_under_v1["authorityLinkHash"] = _independent_hash(
            self.OWNERSHIP_DOMAINS["B1_AUTHORITY_LINK_HASH_DOMAIN"],
            {
                key: value
                for key, value in v2_material_under_v1.items()
                if key != "authorityLinkHash"
            },
            user_scope_hash=self.scope,
        )
        non_contact_v2 = {
            **terminal,
            "exactIdentityHash": "4" * 64,
            "canonicalMailboxIdentityHash": "5" * 64,
        }
        non_contact_material = {
            key: value
            for key, value in non_contact_v2.items()
            if key != "authorityLinkHash"
        }
        non_contact_v2["authorityLinkHash"] = _independent_hash(
            self.OWNERSHIP_DOMAINS[
                "B1_CONTACT_AUTHORITY_LINK_HASH_DOMAIN"
            ],
            non_contact_material,
            user_scope_hash=self.scope,
        )
        extra = {**v2, "unknown": None}
        invalid = [
            legacy_with_v2_keys,
            v2_material_under_v1,
            non_contact_v2,
            extra,
        ]
        for identity_field in (
            "exactIdentityHash",
            "canonicalMailboxIdentityHash",
        ):
            missing = deepcopy(v2)
            del missing[identity_field]
            invalid.append(missing)
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self.module.validate_b1_authority_link(
                    authority_link=candidate,
                    user_scope_hash=self.scope,
                )
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self.module.validate_b1_authority_link(
                authority_link=v2,
                user_scope_hash=self.other_scope,
            )

        validated = self.module.validate_b1_authority_link(
            authority_link=v2,
            user_scope_hash=self.scope,
        )
        validated["exactIdentityHash"] = "f" * 64
        self.assertEqual("4" * 64, v2["exactIdentityHash"])

    def test_v2_contact_builder_rejects_bound_evidence_substitution(self):
        link, bundle = self._link(
            "contact_optout",
            contact_evidence_version=2,
        )
        evidence = bundle["classification"]["deterministicEvidence"]
        self.assertEqual(
            evidence["exactIdentityHash"],
            link["exactIdentityHash"],
        )
        self.assertEqual(
            evidence["canonicalMailboxIdentityHash"],
            link["canonicalMailboxIdentityHash"],
        )
        for field in (
            "exactIdentityHash",
            "canonicalMailboxIdentityHash",
        ):
            forged = deepcopy(bundle["classification"])
            forged["deterministicEvidence"][field] = "f" * 64
            with self.subTest(field=field), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self.module.build_b1_authority_link(
                    user_scope_hash=self.scope,
                    source_identity_document=bundle["identity"],
                    source_classification_document=forged,
                    source_owner_document=bundle["owner"],
                    source_ledger_document=bundle["ledger"],
                    work_key=bundle["work_key"],
                )

    def test_v2_contact_fanout_claim_preserves_complete_authority_link(self):
        expected_link, _bundle = self._link(
            "contact_optout",
            contact_evidence_version=2,
        )
        claim = self._claim(
            origin="contact_fanout",
            contact_evidence_version=2,
        )
        self.assertEqual(expected_link, claim["authorityLink"])
        self.assertEqual(
            expected_link["authorityLinkHash"],
            claim["authorityLinkHash"],
        )
        self.assertEqual(
            claim,
            self.module.validate_claim_set_document(document=claim),
        )

    def test_contact_fanout_origin_rejects_legacy_v1_contact_link_before_planning(self):
        legacy, _bundle = self._link("contact_optout")
        with patch.object(
            self.module,
            "_plan_row_claim_set",
            side_effect=AssertionError("generic row planning was reached"),
        ) as generic_planner:
            with self.assertRaises(self.module.RowAuthorityConfigError):
                self.module._plan_contact_fanout_row_claim(
                    user_scope_hash=self.scope,
                    authority_link=legacy,
                    thread_binding_document=None,
                    canonical_row_id=self.first,
                )
        generic_planner.assert_not_called()

    def test_contact_fanout_origin_allows_v2_link_to_reach_generic_planning(self):
        link, _bundle = self._link(
            "contact_optout",
            contact_evidence_version=2,
        )
        sentinel = object()
        with patch.object(
            self.module,
            "_plan_row_claim_set",
            return_value=sentinel,
        ) as generic_planner:
            result = self.module._plan_contact_fanout_row_claim(
                user_scope_hash=self.scope,
                authority_link=link,
                fanout_id="8" * 64,
                canonical_mailbox_identity_hash=link[
                    "canonicalMailboxIdentityHash"
                ],
                thread_binding_document=None,
                canonical_row_id=self.first,
            )
        self.assertIs(sentinel, result)
        generic_planner.assert_called_once_with(
            authority_origin="contact_fanout",
            operator_action_document=None,
            user_scope_hash=self.scope,
            authority_link=link,
            fanout_id="8" * 64,
            canonical_mailbox_identity_hash=link[
                "canonicalMailboxIdentityHash"
            ],
            thread_binding_document=None,
            canonical_row_id=self.first,
        )

    def test_contact_fanout_origin_rejects_caller_selected_thread_binding(self):
        link, _bundle = self._link(
            "contact_optout",
            contact_evidence_version=2,
        )
        binding = self.module.build_thread_row_binding_document(
            user_scope_hash=self.scope,
            thread_id="thread-1",
            client_id="client-1",
            row_ids=[self.first],
            primary_row_id=self.first,
            created_at=self.created_at,
        )
        with patch.object(
            self.module,
            "_plan_row_claim_set",
            side_effect=AssertionError("generic row planning was reached"),
        ) as generic_planner:
            with self.assertRaises(self.module.RowAuthorityConfigError):
                self.module._plan_contact_fanout_row_claim(
                    user_scope_hash=self.scope,
                    authority_link=link,
                    fanout_id="8" * 64,
                    canonical_mailbox_identity_hash=link[
                        "canonicalMailboxIdentityHash"
                    ],
                    thread_binding_document=binding,
                    canonical_row_id=self.first,
                )
        generic_planner.assert_not_called()

    def test_b1_link_exact_schema_owner_correlations_and_hard_optout_requirement(self):
        terminal, _bundle = self._link("terminal")
        validated = self.module.validate_b1_authority_link(
            authority_link=terminal,
            user_scope_hash=self.scope,
        )
        self.assertEqual(terminal, validated)
        invalid = []
        unknown = deepcopy(terminal)
        unknown["unknown"] = None
        invalid.append(unknown)
        wrong_priority_evidence = deepcopy(terminal)
        wrong_priority_evidence["hardOptOutEvidenceHash"] = "f" * 64
        invalid.append(wrong_priority_evidence)
        contact, _contact_bundle = self._link("contact_optout")
        missing_contact_evidence = deepcopy(contact)
        missing_contact_evidence["hardOptOutEvidenceHash"] = None
        invalid.append(missing_contact_evidence)
        wrong_owner_key = deepcopy(terminal)
        wrong_owner_key["ownerKey"] = "f" * 64
        invalid.append(wrong_owner_key)
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self.module.validate_b1_authority_link(
                    authority_link=candidate,
                    user_scope_hash=self.scope,
                )

    def test_b1_link_hash_changes_with_user_scope_and_cross_scope_validation_fails(self):
        link, bundle = self._link("terminal")
        other = self.module.build_b1_authority_link(
            user_scope_hash=self.other_scope,
            source_identity_document=bundle["identity"],
            source_classification_document=bundle["classification"],
            source_owner_document=bundle["owner"],
            source_ledger_document=bundle["ledger"],
            work_key=bundle["work_key"],
        )
        self.assertNotEqual(
            link["authorityLinkHash"],
            other["authorityLinkHash"],
        )
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self.module.validate_b1_authority_link(
                authority_link=link,
                user_scope_hash=self.other_scope,
            )

    def test_hard_optout_link_uses_validated_nonlocal_deterministic_evidence_hash(self):
        link, bundle = self._link("contact_optout")
        self.assertEqual(
            bundle["hard_optout_hash"],
            link["hardOptOutEvidenceHash"],
        )
        self.assertNotEqual(
            bundle["classification"]["deterministicEvidence"][
                "evidenceHash"
            ],
            link["hardOptOutEvidenceHash"],
        )
        self.assertEqual(
            link["hardOptOutEvidenceHash"],
            bundle["ledger"]["entries"][0]["payload"]["evidenceHash"],
        )

    def test_forged_or_model_only_optout_bundle_cannot_build_priority_three_link(self):
        link, bundle = self._link("human_decision")
        self.assertEqual("human_decision", link["ownerKind"])
        forged_classification = deepcopy(bundle["classification"])
        forged_classification["transitionCandidates"] = [
            {"type": "contact_optout", "claimed": True}
        ]
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self.module.build_b1_authority_link(
                user_scope_hash=self.scope,
                source_identity_document=bundle["identity"],
                source_classification_document=forged_classification,
                source_owner_document=bundle["owner"],
                source_ledger_document=bundle["ledger"],
                work_key=bundle["work_key"],
            )

        model_bundle = self._b1_bundle(
            owner_kind="human_decision",
            model_contact_optout=True,
        )
        model_link = self.module.build_b1_authority_link(
            user_scope_hash=self.scope,
            source_identity_document=model_bundle["identity"],
            source_classification_document=model_bundle["classification"],
            source_owner_document=model_bundle["owner"],
            source_ledger_document=model_bundle["ledger"],
            work_key=model_bundle["work_key"],
        )
        self.assertEqual(
            "contact_optout",
            model_bundle["classification"]["completeProposalSnapshot"][
                "transitionCandidates"
            ][0]["type"],
        )
        self.assertEqual(
            "needs_user_input",
            model_bundle["classification"]["transitionCandidates"][0][
                "type"
            ],
        )
        self.assertEqual("human_decision", model_link["ownerKind"])
        self.assertIsNone(model_link["hardOptOutEvidenceHash"])

    def test_b1_canonical_material_rejects_invalid_utf8_dictionary_keys(self):
        for value in (
            {"\ud800": "value"},
            {"nested": [{"\udfff": "value"}]},
        ):
            with self.subTest(value=value), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self.module._b1_canonical_hash(value)

    def test_planned_write_validator_accepts_nonboolean_uint_0_through_400_and_rejects_401(self):
        self.assertEqual(
            0,
            self.module._require_row_authority_planned_writes(0),
        )
        self.assertEqual(
            400,
            self.module._require_row_authority_planned_writes(400),
        )

        class IntSubclass(int):
            pass

        for value in (
            True,
            False,
            -1,
            401,
            1.0,
            "1",
            None,
            IntSubclass(1),
        ):
            with self.subTest(value=value), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self.module._require_row_authority_planned_writes(value)

    def test_priority_is_derived_and_cannot_be_supplied(self):
        self.assertEqual(
            {
                "contact_optout": 3,
                "terminal": 2,
                "human_decision": 1,
            },
            {
                owner: self.module.derive_owner_priority(owner)
                for owner in (
                    "contact_optout",
                    "terminal",
                    "human_decision",
                )
            },
        )
        for value in (None, True, 3, "", "operator", "CONTACT_OPTOUT"):
            with self.subTest(value=value), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self.module.derive_owner_priority(value)
        self.assertNotIn(
            "priority",
            inspect.signature(
                self.module.build_owner_generation_document
            ).parameters,
        )

    def test_claim_and_settlement_fences_are_exactly_monotonic(self):
        head = self._row_head()
        terminal_claim = self._claim()
        invalid_first_generation = self.module.build_owner_generation_document(
            claim_set_document=terminal_claim,
            row_id=self.first,
            generation=1,
            predecessor_head_hash=head["headHash"],
            predecessor_settlement_hash=None,
            lease_epoch=1,
            first_fencing_token=7,
            created_at=self.created_at,
        )
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self.module._build_claim_advanced_head(
                expected_head=head,
                generation_document=invalid_first_generation,
                lease_owner_hash="9" * 64,
                lease_until="2026-08-04T12:00:02.000000Z",
                dominated_predecessor_settlement_hash=None,
                claimed_at=self.created_at,
            )

        human_claim = self._claim(origin="authenticated_operator")
        human_generation = self.module.build_owner_generation_document(
            claim_set_document=human_claim,
            row_id=self.first,
            generation=1,
            predecessor_head_hash=head["headHash"],
            predecessor_settlement_hash=None,
            lease_epoch=1,
            first_fencing_token=1,
            created_at=self.created_at,
        )
        pending = self.module._build_claim_advanced_head(
            expected_head=head,
            generation_document=human_generation,
            lease_owner_hash="9" * 64,
            lease_until="2026-08-04T12:00:02.000000Z",
            dominated_predecessor_settlement_hash=None,
            claimed_at=self.created_at,
        )
        taken_over = self.module._build_lease_takeover_head(
            expected_head=pending,
            generation_document=human_generation,
            new_lease_owner_hash="8" * 64,
            new_lease_until="2026-08-04T12:00:04.000000Z",
            taken_at="2026-08-04T12:00:03.000000Z",
        )
        terminal_claim = self._claim(planned_generation=2)
        for first_fence in (2, 4):
            invalid_generation = self.module.build_owner_generation_document(
                claim_set_document=terminal_claim,
                row_id=self.first,
                generation=2,
                predecessor_head_hash=taken_over["headHash"],
                predecessor_settlement_hash=None,
                lease_epoch=1,
                first_fencing_token=first_fence,
                created_at="2026-08-04T12:00:03.000000Z",
            )
            with self.subTest(first_fence=first_fence), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self.module._build_claim_advanced_head(
                    expected_head=taken_over,
                    generation_document=invalid_generation,
                    lease_owner_hash="7" * 64,
                    lease_until="2026-08-04T12:00:05.000000Z",
                    dominated_predecessor_settlement_hash="6" * 64,
                    claimed_at="2026-08-04T12:00:03.000000Z",
                )

        next_generation = self.module.build_owner_generation_document(
            claim_set_document=terminal_claim,
            row_id=self.first,
            generation=2,
            predecessor_head_hash=taken_over["headHash"],
            predecessor_settlement_hash=None,
            lease_epoch=1,
            first_fencing_token=3,
            created_at="2026-08-04T12:00:03.000000Z",
        )
        superseding = self.module._build_claim_advanced_head(
            expected_head=taken_over,
            generation_document=next_generation,
            lease_owner_hash="7" * 64,
            lease_until="2026-08-04T12:00:05.000000Z",
            dominated_predecessor_settlement_hash="6" * 64,
            claimed_at="2026-08-04T12:00:03.000000Z",
        )
        self.assertEqual(3, superseding["fencingToken"])

        with self.assertRaises(self.module.RowAuthorityConfigError):
            self.module.build_owner_settlement_document(
                generation_document=next_generation,
                claim_set_document=terminal_claim,
                fencing_token=2,
                outcome="terminal",
                settled_at="2026-08-04T12:00:04.000000Z",
            )

    def test_takeover_and_settlement_reject_heads_below_generation_fence_floor(self):
        initial_head = self._row_head()
        claim = self._claim()
        generation = self.module.build_owner_generation_document(
            claim_set_document=claim,
            row_id=self.first,
            generation=1,
            predecessor_head_hash=initial_head["headHash"],
            predecessor_settlement_hash=None,
            lease_epoch=1,
            first_fencing_token=7,
            created_at=self.created_at,
        )
        malformed_head_material = {
            key: deepcopy(value)
            for key, value in initial_head.items()
            if key != "headHash"
        }
        malformed_head_material.update(
            {
                "stateRevision": 2,
                "effectiveOwnerGeneration": 1,
                "effectiveOwnerGenerationHash": generation["generationHash"],
                "effectiveOwnerKind": generation["ownerKind"],
                "effectivePriority": generation["priority"],
                "state": "claimed",
                "leaseOwnerHash": "9" * 64,
                "leaseUntil": "2026-08-04T12:00:02.000000Z",
                "fencingToken": 1,
            }
        )
        malformed_head = self.module._with_head_hash(malformed_head_material)
        self.assertEqual(
            malformed_head,
            self.module.validate_row_authority_head(document=malformed_head),
        )
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self.module._build_lease_takeover_head(
                expected_head=malformed_head,
                generation_document=generation,
                new_lease_owner_hash="8" * 64,
                new_lease_until="2026-08-04T12:00:04.000000Z",
                taken_at="2026-08-04T12:00:03.000000Z",
            )

        settlement = self.module.build_owner_settlement_document(
            generation_document=generation,
            claim_set_document=claim,
            fencing_token=7,
            outcome="terminal",
            settled_at=self.later_at,
        )
        settlement["fencingToken"] = 1
        settlement_payload = {
            "rowId": settlement["rowId"],
            "generation": settlement["generation"],
            "generationHash": settlement["generationHash"],
            "fencingToken": settlement["fencingToken"],
            "outcome": settlement["outcome"],
            "dominantGenerationHash": settlement[
                "dominantGenerationHash"
            ],
            "supersededEffectiveSettlementHash": settlement[
                "supersededEffectiveSettlementHash"
            ],
            "operatorActionHash": settlement["operatorActionHash"],
            "outcomeReasonCode": settlement["outcomeReasonCode"],
            "outcomeEvidenceHash": settlement["outcomeEvidenceHash"],
            "logicalOutcomeHash": settlement["logicalOutcomeHash"],
            "settledAt": settlement["settledAt"],
        }
        settlement["settlementHash"] = _independent_hash(
            self.OWNERSHIP_DOMAINS["OWNER_SETTLEMENT_HASH_DOMAIN"],
            settlement_payload,
            user_scope_hash=self.scope,
        )
        self.assertEqual(
            settlement,
            self.module.validate_owner_settlement_document(
                document=settlement
            ),
        )
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self.module._build_settlement_advanced_head(
                expected_head=malformed_head,
                generation_document=generation,
                settlement_document=settlement,
            )

    def test_head_state_matches_owner_kind_and_settled_heads_retain_fence(self):
        initial = self._row_head()

        def rehash(head, **changes):
            material = {
                key: deepcopy(value)
                for key, value in head.items()
                if key != "headHash"
            }
            material.update(changes)
            return self.module._with_head_hash(material)

        terminal_claim = self._claim()
        terminal_generation = self.module.build_owner_generation_document(
            claim_set_document=terminal_claim,
            row_id=self.first,
            generation=1,
            predecessor_head_hash=initial["headHash"],
            predecessor_settlement_hash=None,
            lease_epoch=1,
            first_fencing_token=1,
            created_at=self.created_at,
        )
        claimed = self.module._build_claim_advanced_head(
            expected_head=initial,
            generation_document=terminal_generation,
            lease_owner_hash="9" * 64,
            lease_until="2026-08-04T12:00:02.000000Z",
            dominated_predecessor_settlement_hash=None,
            claimed_at=self.created_at,
        )
        terminal_as_pending = rehash(claimed, state="review_pending")

        human_claim = self._claim(origin="authenticated_operator")
        human_generation = self.module.build_owner_generation_document(
            claim_set_document=human_claim,
            row_id=self.first,
            generation=1,
            predecessor_head_hash=initial["headHash"],
            predecessor_settlement_hash=None,
            lease_epoch=1,
            first_fencing_token=1,
            created_at=self.created_at,
        )
        pending = self.module._build_claim_advanced_head(
            expected_head=initial,
            generation_document=human_generation,
            lease_owner_hash="9" * 64,
            lease_until="2026-08-04T12:00:02.000000Z",
            dominated_predecessor_settlement_hash=None,
            claimed_at=self.created_at,
        )
        human_as_claimed = rehash(pending, state="claimed")

        terminal_settlement = self.module.build_owner_settlement_document(
            generation_document=terminal_generation,
            claim_set_document=terminal_claim,
            fencing_token=1,
            outcome="terminal",
            settled_at=self.later_at,
        )
        settled = self.module._build_settlement_advanced_head(
            expected_head=claimed,
            generation_document=terminal_generation,
            settlement_document=terminal_settlement,
        )
        settled_without_fence = rehash(settled, fencingToken=None)

        for invalid in (
            terminal_as_pending,
            human_as_claimed,
            settled_without_fence,
        ):
            with self.subTest(invalid=invalid), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self.module.validate_row_authority_head(document=invalid)

    def test_operator_action_id_request_hash_and_action_hash_are_deterministic(self):
        action = self._operator()
        expected_request = _independent_hash(
            self.OWNERSHIP_DOMAINS[
                "OPERATOR_CLIENT_REQUEST_HASH_DOMAIN"
            ],
            {"clientRequestId": "request-1"},
            user_scope_hash=self.scope,
        )
        id_payload = {
            "actorScopeHash": action["actorScopeHash"],
            "rowBindingsHash": action["rowBindingsHash"],
            "clientRequestHash": expected_request,
            "actionKind": "decline",
            "reasonCode": "decline_property",
            "issuedAt": self.created_at,
        }
        expected_id = _independent_hash(
            self.OWNERSHIP_DOMAINS["OPERATOR_ACTION_ID_DOMAIN"],
            id_payload,
            user_scope_hash=self.scope,
        )
        expected_hash = _independent_hash(
            self.OWNERSHIP_DOMAINS["OPERATOR_ACTION_HASH_DOMAIN"],
            {"actionId": expected_id, **id_payload},
            user_scope_hash=self.scope,
        )
        self.assertEqual(expected_request, action["clientRequestHash"])
        self.assertEqual(expected_id, action["actionId"])
        self.assertEqual(expected_hash, action["operatorActionHash"])
        self.assertEqual(
            action,
            self.module.validate_operator_action_document(document=action),
        )
        for overrides in (
            {"client_request_id": "request-2"},
            {"issued_at": self.later_at},
            {"actor_scope_hash": "f" * 64},
            {"user_scope_hash": self.other_scope},
        ):
            with self.subTest(overrides=overrides):
                self.assertNotEqual(action, self._operator(**overrides))

    def test_claim_origin_union_rejects_every_invalid_cross_field_combination(self):
        b1 = self._claim(origin="b1_source")
        operator = self._claim(origin="authenticated_operator")
        fanout = self._claim(origin="contact_fanout")
        self.assertIsNotNone(b1["authorityLink"])
        self.assertIsNone(b1["operatorActionHash"])
        self.assertIsNone(b1["fanoutId"])
        self.assertIsNone(operator["authorityLink"])
        self.assertIsNotNone(operator["operatorActionHash"])
        self.assertEqual("human_decision", operator["ownerKind"])
        self.assertEqual(operator["operatorActionHash"], operator["payloadHash"])
        self.assertEqual("contact_optout", fanout["ownerKind"])
        self.assertIsNotNone(fanout["fanoutId"])
        self.assertEqual("d" * 64, fanout["ownerKey"])
        self.assertEqual(
            f"{'e' * 64}--{self.first}",
            fanout["workKey"],
        )
        self.assertEqual("4" * 64, fanout["payloadHash"])
        self.assertNotEqual(
            fanout["authorityLink"]["ownerKey"],
            fanout["ownerKey"],
        )
        self.assertNotEqual(
            fanout["authorityLink"]["workKey"],
            fanout["workKey"],
        )
        self.assertNotEqual(
            fanout["authorityLink"]["payloadHash"],
            fanout["payloadHash"],
        )
        for document in (b1, operator, fanout):
            self.assertEqual(
                document,
                self.module.validate_claim_set_document(document=document),
            )

        terminal_link, _bundle = self._link("terminal")
        invalid_arguments = (
            {
                "authority_origin": "b1_source",
                "authority_link": terminal_link,
                "operator_action_document": self._operator(),
                "fanout_id": None,
            },
            {
                "authority_origin": "authenticated_operator",
                "authority_link": terminal_link,
                "operator_action_document": self._operator(),
                "fanout_id": None,
            },
            {
                "authority_origin": "contact_fanout",
                "authority_link": terminal_link,
                "operator_action_document": None,
                "fanout_id": "e" * 64,
            },
            {
                "authority_origin": "contact_fanout",
                "authority_link": self._link("contact_optout")[0],
                "operator_action_document": None,
                "fanout_id": None,
            },
            {
                "authority_origin": "invented",
                "authority_link": None,
                "operator_action_document": None,
                "fanout_id": None,
            },
        )
        decisions = [
            {
                "rowId": self.first,
                "decision": "accepted",
                "plannedGeneration": 1,
                "winnerGenerationHash": None,
                "winnerSettlementHash": None,
            }
        ]
        for origin in invalid_arguments:
            with self.subTest(origin=origin), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self.module.build_claim_set_document(
                    user_scope_hash=self.scope,
                    row_ids=(self.first,),
                    primary_row_id=self.first,
                    planned_writes=3,
                    outcome="accepted",
                    row_decisions=decisions,
                    created_at=self.created_at,
                    **origin,
                )

    def test_accepted_and_dominated_claim_decisions_enforce_exact_nullability(self):
        accepted = self._claim(outcome="accepted")
        dominated = self._claim(outcome="dominated")
        self.assertEqual("accepted", accepted["rowDecisions"][0]["decision"])
        self.assertEqual(
            "dominated",
            dominated["rowDecisions"][0]["decision"],
        )
        self.assertIsNone(
            dominated["rowDecisions"][0]["winnerSettlementHash"]
        )

        link, _bundle = self._link("terminal")
        blocked = {
            "rowId": self.second,
            "decision": "blocked_by_claim_set",
            "plannedGeneration": None,
            "winnerGenerationHash": None,
            "winnerSettlementHash": None,
        }
        multi = self.module.build_claim_set_document(
            user_scope_hash=self.scope,
            authority_origin="b1_source",
            authority_link=link,
            operator_action_document=None,
            fanout_id=None,
            row_ids=(self.second, self.first),
            primary_row_id=self.first,
            planned_writes=1,
            outcome="dominated",
            row_decisions=(dominated["rowDecisions"][0], blocked),
            created_at=self.created_at,
        )
        self.assertEqual(
            [self.first, self.second],
            [decision["rowId"] for decision in multi["rowDecisions"]],
        )

        invalid_decisions = (
            {
                "rowId": self.first,
                "decision": "accepted",
                "plannedGeneration": 1,
                "winnerGenerationHash": "f" * 64,
                "winnerSettlementHash": None,
            },
            {
                "rowId": self.first,
                "decision": "dominated",
                "plannedGeneration": 1,
                "winnerGenerationHash": "f" * 64,
                "winnerSettlementHash": None,
            },
            {
                "rowId": self.first,
                "decision": "blocked_by_claim_set",
                "plannedGeneration": None,
                "winnerGenerationHash": None,
                "winnerSettlementHash": "f" * 64,
            },
        )
        for decision in invalid_decisions:
            with self.subTest(decision=decision), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self.module.build_claim_set_document(
                    user_scope_hash=self.scope,
                    authority_origin="b1_source",
                    authority_link=link,
                    operator_action_document=None,
                    fanout_id=None,
                    row_ids=(self.first,),
                    primary_row_id=self.first,
                    planned_writes=3,
                    outcome="accepted",
                    row_decisions=(decision,),
                    created_at=self.created_at,
                )

    def test_generation_settlement_and_source_link_schemas_enforce_correlated_nulls(self):
        terminal_claim = self._claim()
        generation = self._generation(claim=terminal_claim)
        self.assertEqual(2, generation["priority"])
        self.assertEqual(
            generation,
            self.module.validate_owner_generation_document(
                document=generation
            ),
        )
        terminal = self.module.build_owner_settlement_document(
            generation_document=generation,
            claim_set_document=terminal_claim,
            fencing_token=1,
            outcome="terminal",
            settled_at=self.later_at,
        )
        self.assertEqual("terminal_source", terminal["outcomeReasonCode"])
        self.assertIsNone(terminal["dominantGenerationHash"])
        self.assertIsNone(terminal["supersededEffectiveSettlementHash"])
        self.assertIsNone(terminal["operatorActionHash"])
        self.assertEqual(
            terminal,
            self.module.validate_owner_settlement_document(
                document=terminal
            ),
        )

        contact_claim = self._claim(
            origin="contact_fanout",
            planned_generation=2,
        )
        contact_generation = self.module.build_owner_generation_document(
            claim_set_document=contact_claim,
            row_id=self.first,
            generation=2,
            predecessor_head_hash="7" * 64,
            predecessor_settlement_hash="4" * 64,
            lease_epoch=1,
            first_fencing_token=2,
            created_at=self.created_at,
        )
        contact = self.module.build_owner_settlement_document(
            generation_document=contact_generation,
            claim_set_document=contact_claim,
            fencing_token=2,
            outcome="contact_optout",
            superseded_effective_settlement_hash="4" * 64,
            settled_at=self.later_at,
        )
        self.assertEqual("verified_optout", contact["outcomeReasonCode"])
        self.assertEqual(
            "4" * 64,
            contact["supersededEffectiveSettlementHash"],
        )
        for predecessor in (None, "5" * 64):
            with self.subTest(predecessor=predecessor), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self.module.build_owner_settlement_document(
                    generation_document=contact_generation,
                    claim_set_document=contact_claim,
                    fencing_token=2,
                    outcome="contact_optout",
                    superseded_effective_settlement_hash=predecessor,
                    settled_at=self.later_at,
                )

        first_contact_claim = self._claim(origin="contact_fanout")
        first_contact_generation = self._generation(
            claim=first_contact_claim
        )
        first_contact = self.module.build_owner_settlement_document(
            generation_document=first_contact_generation,
            claim_set_document=first_contact_claim,
            fencing_token=1,
            outcome="contact_optout",
            superseded_effective_settlement_hash=None,
            settled_at=self.later_at,
        )
        self.assertIsNone(
            first_contact["supersededEffectiveSettlementHash"]
        )

        operator_claim = self._claim(origin="authenticated_operator")
        operator_generation = self._generation(claim=operator_claim)
        action = self._operator()
        human = self.module.build_owner_settlement_document(
            generation_document=operator_generation,
            claim_set_document=operator_claim,
            fencing_token=1,
            outcome="human_declined",
            operator_action_document=action,
            settled_at=self.later_at,
        )
        self.assertEqual("operator_decline", human["outcomeReasonCode"])
        self.assertEqual(action["operatorActionHash"], human["operatorActionHash"])

        dominated = self.module.build_owner_settlement_document(
            generation_document=generation,
            claim_set_document=terminal_claim,
            fencing_token=1,
            outcome="dominated",
            dominant_generation_hash="5" * 64,
            settled_at=self.later_at,
        )
        self.assertEqual(
            "superseded_by_higher_priority",
            dominated["outcomeReasonCode"],
        )
        self.assertEqual("5" * 64, dominated["dominantGenerationHash"])
        for dominated_generation, dominated_claim, dominant_hash in (
            (
                first_contact_generation,
                first_contact_claim,
                "5" * 64,
            ),
            (generation, terminal_claim, generation["generationHash"]),
        ):
            with self.subTest(
                owner=dominated_generation["ownerKind"],
                dominant_hash=dominant_hash,
            ), self.assertRaises(self.module.RowAuthorityConfigError):
                self.module.build_owner_settlement_document(
                    generation_document=dominated_generation,
                    claim_set_document=dominated_claim,
                    fencing_token=dominated_generation[
                        "firstFencingToken"
                    ],
                    outcome="dominated",
                    dominant_generation_hash=dominant_hash,
                    settled_at=self.later_at,
                )

        link = self.module.build_source_settlement_link_document(
            user_scope_hash=self.scope,
            row_id=self.first,
            generation=1,
            generation_hash=generation["generationHash"],
            authority_link_hash=terminal_claim["authorityLinkHash"],
            b1_identity_hash="6" * 64,
            b1_final_ledger_evidence_hash="7" * 64,
            b1_settlement_revision=1,
            b1_settlement_hash="8" * 64,
            b2_settlement_hash=terminal["settlementHash"],
            linked_at=self.later_at,
        )
        self.assertEqual(
            link,
            self.module.validate_source_settlement_link_document(
                document=link
            ),
        )

        with self.assertRaises(self.module.RowAuthorityConfigError):
            self.module.build_owner_generation_document(
                claim_set_document=terminal_claim,
                row_id=self.first,
                generation=1,
                predecessor_head_hash="7" * 64,
                predecessor_settlement_hash=None,
                lease_epoch=2,
                first_fencing_token=1,
                created_at=self.created_at,
            )
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self.module.build_owner_settlement_document(
                generation_document=generation,
                claim_set_document=terminal_claim,
                fencing_token=1,
                outcome="terminal",
                dominant_generation_hash="5" * 64,
                settled_at=self.later_at,
            )

    def test_settlement_requires_the_claims_exact_accepted_row_generation(self):
        accepted_claim = self._claim()
        valid_generation = self._generation(claim=accepted_claim)
        dominated_claim = self._claim(outcome="dominated")

        def rehash_generation(document):
            payload = {
                "rowId": document["rowId"],
                "generation": document["generation"],
                "requestId": document["requestId"],
                "claimSetHash": document["claimSetHash"],
                "predecessorHeadHash": document["predecessorHeadHash"],
                "predecessorSettlementHash": document[
                    "predecessorSettlementHash"
                ],
                "ownerKind": document["ownerKind"],
                "ownerKey": document["ownerKey"],
                "priority": document["priority"],
                "leaseEpoch": document["leaseEpoch"],
                "firstFencingToken": document["firstFencingToken"],
                "createdAt": document["createdAt"],
            }
            document["generationHash"] = _independent_hash(
                self.OWNERSHIP_DOMAINS["OWNER_GENERATION_HASH_DOMAIN"],
                payload,
                user_scope_hash=self.scope,
            )
            return document

        wrong_row = rehash_generation(
            {**deepcopy(valid_generation), "rowId": self.second}
        )
        wrong_generation = rehash_generation(
            {**deepcopy(valid_generation), "generation": 999}
        )
        dominated_origin = rehash_generation(
            {
                **deepcopy(valid_generation),
                "requestId": dominated_claim["requestId"],
                "claimSetHash": dominated_claim["claimSetHash"],
            }
        )
        for generation, claim in (
            (wrong_row, accepted_claim),
            (wrong_generation, accepted_claim),
            (dominated_origin, dominated_claim),
        ):
            self.assertEqual(
                generation,
                self.module.validate_owner_generation_document(
                    document=generation
                ),
            )
            with self.subTest(generation=generation), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                self.module.build_owner_settlement_document(
                    generation_document=generation,
                    claim_set_document=claim,
                    fencing_token=1,
                    outcome="terminal",
                    settled_at=self.later_at,
                )

    def test_all_ownership_builders_and_validators_are_defensive(self):
        link, bundle = self._link("terminal")
        claim = self._claim()
        generation = self._generation(claim=claim)
        settlement = self.module.build_owner_settlement_document(
            generation_document=generation,
            claim_set_document=claim,
            fencing_token=1,
            outcome="terminal",
            settled_at=self.later_at,
        )
        originals = (
            deepcopy(bundle),
            deepcopy(link),
            deepcopy(claim),
            deepcopy(generation),
            deepcopy(settlement),
        )
        validated_link = self.module.validate_b1_authority_link(
            authority_link=link,
            user_scope_hash=self.scope,
        )
        validated_claim = self.module.validate_claim_set_document(
            document=claim
        )
        validated_generation = self.module.validate_owner_generation_document(
            document=generation
        )
        validated_settlement = self.module.validate_owner_settlement_document(
            document=settlement
        )
        validated_link["ownerKind"] = "mutated"
        validated_claim["rowBindings"][0]["role"] = "mutated"
        validated_claim["rowDecisions"][0]["decision"] = "mutated"
        validated_generation["ownerKind"] = "mutated"
        validated_settlement["outcome"] = "mutated"
        self.assertEqual(originals[0], bundle)
        self.assertEqual(originals[1], link)
        self.assertEqual(originals[2], claim)
        self.assertEqual(originals[3], generation)
        self.assertEqual(originals[4], settlement)

    def test_ownership_head_transition_helpers_preserve_unrelated_state(self):
        head = self._row_head()
        claim = self._claim()
        generation = self.module.build_owner_generation_document(
            claim_set_document=claim,
            row_id=self.first,
            generation=1,
            predecessor_head_hash=head["headHash"],
            predecessor_settlement_hash=None,
            lease_epoch=1,
            first_fencing_token=1,
            created_at=self.created_at,
        )
        claimed = self.module._build_claim_advanced_head(
            expected_head=head,
            generation_document=generation,
            lease_owner_hash="9" * 64,
            lease_until="2026-08-04T12:00:02.000000Z",
            dominated_predecessor_settlement_hash=None,
            claimed_at=self.created_at,
        )
        self.assertEqual(head["stateRevision"] + 1, claimed["stateRevision"])
        self.assertEqual("claimed", claimed["state"])
        self.assertEqual(1, claimed["effectiveOwnerGeneration"])
        for field in (
            "currentLocationRevision",
            "currentLocationHash",
            "currentLocationLifecycle",
            "createdAt",
            "latestSourceSettlementLinkHash",
            "latestOptOutReleaseResultHash",
            "projectionBacklogCount",
        ):
            self.assertEqual(head[field], claimed[field])

        takeover = self.module._build_lease_takeover_head(
            expected_head=claimed,
            generation_document=generation,
            new_lease_owner_hash="8" * 64,
            new_lease_until="2026-08-04T12:00:04.000000Z",
            taken_at="2026-08-04T12:00:03.000000Z",
        )
        self.assertEqual(claimed["stateRevision"] + 1, takeover["stateRevision"])
        self.assertEqual(claimed["fencingToken"] + 1, takeover["fencingToken"])
        self.assertEqual("8" * 64, takeover["leaseOwnerHash"])

        equal_time_settlement = self.module.build_owner_settlement_document(
            generation_document=generation,
            claim_set_document=claim,
            fencing_token=takeover["fencingToken"],
            outcome="terminal",
            settled_at=takeover["updatedAt"],
        )
        self.assertEqual(
            takeover["updatedAt"],
            self.module._build_settlement_advanced_head(
                expected_head=takeover,
                generation_document=generation,
                settlement_document=equal_time_settlement,
            )["updatedAt"],
        )
        backward_settlement = self.module.build_owner_settlement_document(
            generation_document=generation,
            claim_set_document=claim,
            fencing_token=takeover["fencingToken"],
            outcome="terminal",
            settled_at="2026-08-04T12:00:02.000000Z",
        )
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self.module._build_settlement_advanced_head(
                expected_head=takeover,
                generation_document=generation,
                settlement_document=backward_settlement,
            )

        settlement = self.module.build_owner_settlement_document(
            generation_document=generation,
            claim_set_document=claim,
            fencing_token=takeover["fencingToken"],
            outcome="terminal",
            settled_at="2026-08-04T12:00:05.000000Z",
        )
        settled = self.module._build_settlement_advanced_head(
            expected_head=takeover,
            generation_document=generation,
            settlement_document=settlement,
        )
        self.assertEqual("settled", settled["state"])
        self.assertIsNone(settled["leaseOwnerHash"])
        self.assertIsNone(settled["leaseUntil"])
        self.assertEqual(
            settlement["settlementHash"],
            settled["effectiveSettlementHash"],
        )
        source_link = self.module.build_source_settlement_link_document(
            user_scope_hash=self.scope,
            row_id=self.first,
            generation=1,
            generation_hash=generation["generationHash"],
            authority_link_hash=claim["authorityLinkHash"],
            b1_identity_hash="6" * 64,
            b1_final_ledger_evidence_hash="7" * 64,
            b1_settlement_revision=1,
            b1_settlement_hash="8" * 64,
            b2_settlement_hash=settlement["settlementHash"],
            linked_at=settled["updatedAt"],
        )
        linked = self.module._build_source_link_advanced_head(
            expected_head=settled,
            source_link_document=source_link,
        )
        self.assertEqual(settled["stateRevision"] + 1, linked["stateRevision"])
        self.assertEqual(
            source_link["sourceSettlementLinkHash"],
            linked["latestSourceSettlementLinkHash"],
        )
        self.assertEqual(
            self.module.validate_row_authority_head(document=linked),
            linked,
        )
        backward_source_link = self.module.build_source_settlement_link_document(
            user_scope_hash=self.scope,
            row_id=self.first,
            generation=1,
            generation_hash=generation["generationHash"],
            authority_link_hash=claim["authorityLinkHash"],
            b1_identity_hash="6" * 64,
            b1_final_ledger_evidence_hash="7" * 64,
            b1_settlement_revision=1,
            b1_settlement_hash="8" * 64,
            b2_settlement_hash=settlement["settlementHash"],
            linked_at="2026-08-04T12:00:04.000000Z",
        )
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self.module._build_source_link_advanced_head(
                expected_head=settled,
                source_link_document=backward_source_link,
            )
        mutated = deepcopy(linked)
        mutated["state"] = "mutated"
        self.assertEqual("settled", linked["state"])
        for helper, arguments in (
            (
                self.module._build_claim_advanced_head,
                {
                    "expected_head": head,
                    "generation_document": generation,
                    "lease_owner_hash": "9" * 64,
                    "lease_until": "2026-08-04T12:00:02.000000Z",
                    "dominated_predecessor_settlement_hash": None,
                    "claimed_at": "2026-08-04T11:59:59.000000Z",
                },
            ),
            (
                self.module._build_lease_takeover_head,
                {
                    "expected_head": claimed,
                    "generation_document": generation,
                    "new_lease_owner_hash": "8" * 64,
                    "new_lease_until": "2026-08-04T12:00:04.000000Z",
                    "taken_at": "2026-08-04T11:59:59.000000Z",
                },
            ),
        ):
            with self.subTest(helper=helper.__name__), self.assertRaises(
                self.module.RowAuthorityConfigError
            ):
                helper(**arguments)


class ThreadRowBindingStoreTests(unittest.TestCase):
    @staticmethod
    def _fakes():
        return importlib.import_module("tests.row_authority_fakes")

    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")

    def setUp(self):
        self.assertTrue(
            hasattr(self.module.RowAuthorityStore, "bind_thread_rows"),
            "RowAuthorityStore.bind_thread_rows is missing",
        )
        self.user_id = "uid-1"
        self.scope = self.module.user_scope_hash(self.user_id)
        self.first = _row_id(1)
        self.second = _row_id(2)
        self.created_at = "2026-08-04T12:00:00.000000Z"
        self.binding_at = "2026-08-04T12:00:01.000000Z"

    def _store(self):
        return self._fakes().BoundedFakeFirestore()

    def _authority(self, store, *, executor=None):
        return self.module.RowAuthorityStore(
            store,
            transaction_executor=(
                executor or self._fakes().run_bounded_transaction
            ),
        )

    def _row_references(self, store, row_id):
        user = store.collection("users").document(self.user_id)
        return (
            user.collection("rowIdentities").document(row_id),
            user.collection("rowAuthorityHeads").document(row_id),
        )

    def _binding_references(self, store, binding):
        user = store.collection("users").document(self.user_id)
        binding_ref = user.collection("threadRowBindings").document(
            binding["threadId"]
        )
        reverse_documents = self.module.build_row_thread_binding_documents(
            thread_binding_document=binding
        )
        edge_refs = tuple(
            user.collection("rowThreadBindings").document(edge["edgeId"])
            for edge in reverse_documents
        )
        return binding_ref, edge_refs

    def _rehash_head(self, head):
        payload = {
            key: value
            for key, value in head.items()
            if key not in {"schemaVersion", "userScopeHash", "headHash"}
        }
        head["headHash"] = self.module.domain_hash(
            "sitesift.row.authority_head.v1",
            payload,
            user_scope_hash=head["userScopeHash"],
        )
        return self.module.validate_row_authority_head(document=head)

    def _seed_row(
        self,
        store,
        row_id,
        *,
        scope=None,
        client_id="client-1",
        lifecycle="active",
        created_at=None,
    ):
        scope = scope or self.scope
        created_at = created_at or self.created_at
        index = 0 if row_id == self.first else 1
        identity = self.module.build_row_identity_document(
            user_scope_hash=scope,
            row_id=row_id,
            client_id=client_id,
            spreadsheet_id="spreadsheet-1",
            sheet_id=0,
            creation_kind="fresh",
            creation_source_hash="1" * 64,
            created_at=created_at,
        )
        observation = self.module.build_row_observation(
            spreadsheet_id="spreadsheet-1",
            marker_observation={
                "rowId": row_id,
                "sheetId": 0,
                "providerRowIndex": index,
                "displayRowNumber": index + 1,
                "metadataId": index + 1,
            },
            ordered_headers=("Email",),
            ordered_cell_values=(f"row-{index}@example.com",),
            user_scope_hash=scope,
        )
        revision = self.module.build_row_location_revision_document(
            identity_document=identity,
            revision=1,
            lifecycle="active",
            observations=(observation,),
            previous_revision_hash=None,
            observed_at=created_at,
        )
        head = self.module.build_initial_row_authority_head(
            identity_document=identity,
            location_revision_document=revision,
            created_at=created_at,
        )
        if lifecycle == "deleted":
            head.update(
                {
                    "stateRevision": 2,
                    "currentLocationRevision": 2,
                    "currentLocationHash": "2" * 64,
                    "currentLocationLifecycle": "deleted",
                    "updatedAt": self.binding_at,
                }
            )
            head = self._rehash_head(head)
        identity_ref, head_ref = self._row_references(store, row_id)
        identity_ref.create(identity)
        head_ref.create(head)
        return identity, head

    def _seed_two_rows(self, store, **overrides):
        return (
            self._seed_row(store, self.first, **overrides),
            self._seed_row(store, self.second, **overrides),
        )

    def _bind(self, store, *, executor=None, **overrides):
        arguments = {
            "verified_user_id": self.user_id,
            "thread_id": "thread-1",
            "client_id": "client-1",
            "row_ids": [self.second, self.first],
            "primary_row_id": self.first,
            "created_at": self.binding_at,
        }
        arguments.update(overrides)
        return self._authority(store, executor=executor).bind_thread_rows(
            **arguments
        )

    @staticmethod
    def _write_events(store):
        return [
            event
            for event in store.events
            if event[0] in {"create", "set", "update", "delete"}
        ]

    def test_thread_binding_reads_all_binding_identity_head_and_edges_before_writes(self):
        store = self._store()
        self._seed_two_rows(store)
        store.events.clear()
        result = self._bind(store)
        binding = result["threadBinding"]
        binding_ref, edge_refs = self._binding_references(store, binding)
        expected_reads = [binding_ref.path]
        for row_id in (self.first, self.second):
            identity_ref, head_ref = self._row_references(store, row_id)
            expected_reads.extend((identity_ref.path, head_ref.path))
        expected_reads.extend(reference.path for reference in edge_refs)
        observed_reads = [
            event[1] for event in store.events if event[0] == "get"
        ]
        self.assertEqual(expected_reads, observed_reads)
        read_indexes = [
            index
            for index, event in enumerate(store.events)
            if event[0] == "get"
        ]
        write_indexes = [
            index
            for index, event in enumerate(store.events)
            if event[0] in {"create", "set", "update", "delete"}
        ]
        self.assertLess(max(read_indexes), min(write_indexes))
        accessed = "\n".join(observed_reads)
        for forbidden in (
            "rowLocationRevisions",
            "/threads/",
            "provider",
            "campaign",
        ):
            self.assertNotIn(forbidden, accessed)

    def test_thread_binding_creates_one_plus_n_documents_atomically(self):
        store = self._store()
        seeded = self._seed_two_rows(store)
        heads_before = {
            row_id: deepcopy(store.data[self._row_references(store, row_id)[1].path])
            for row_id in (self.first, self.second)
        }
        store.events.clear()
        result = self._bind(store)
        self.assertEqual("created", result["disposition"])
        binding = result["threadBinding"]
        reverse = result["reverseBindings"]
        self.assertEqual(2, binding["bindingCount"])
        self.assertEqual(2, len(reverse))
        binding_ref, edge_refs = self._binding_references(store, binding)
        self.assertEqual(binding, store.data[binding_ref.path])
        self.assertEqual(
            reverse,
            [store.data[reference.path] for reference in edge_refs],
        )
        self.assertEqual(
            [
                ("create", binding_ref.path, binding, False),
                *(
                    ("create", reference.path, document, False)
                    for reference, document in zip(edge_refs, reverse)
                ),
            ],
            self._write_events(store),
        )
        self.assertIn(("commit_applied", 3), store.events)
        for row_id, (_identity, _head) in zip(
            (self.first, self.second), seeded
        ):
            self.assertEqual(
                heads_before[row_id],
                store.data[self._row_references(store, row_id)[1].path],
            )
        result["threadBinding"]["rowBindings"][0]["role"] = "related"
        result["reverseBindings"][0]["role"] = "related"
        self.assertEqual("primary", store.data[binding_ref.path]["rowBindings"][0]["role"])
        self.assertEqual("primary", store.data[edge_refs[0].path]["role"])

    def test_binding_rejects_missing_malformed_scope_client_identity_or_head_with_zero_writes(self):
        cases = (
            ("missing_identity", self.module.RowAuthorityAmbiguous),
            ("malformed_identity", self.module.RowAuthorityAmbiguous),
            ("wrong_scope", self.module.RowAuthorityConflict),
            ("wrong_client", self.module.RowAuthorityConflict),
            ("noncorrelated_head", self.module.RowAuthorityConflict),
            ("missing_head", self.module.RowAuthorityAmbiguous),
            ("malformed_head", self.module.RowAuthorityAmbiguous),
        )
        for mode, error in cases:
            with self.subTest(mode=mode):
                store = self._store()
                self._seed_two_rows(store)
                identity_ref, head_ref = self._row_references(
                    store, self.first
                )
                if mode == "missing_identity":
                    identity_ref.delete()
                elif mode == "malformed_identity":
                    malformed = deepcopy(store.data[identity_ref.path])
                    malformed["unknown"] = None
                    identity_ref.set(malformed, merge=False)
                elif mode == "wrong_scope":
                    identity, head = self._seed_row(
                        self._store(),
                        self.first,
                        scope="f" * 64,
                    )
                    identity_ref.set(identity, merge=False)
                    head_ref.set(head, merge=False)
                elif mode == "wrong_client":
                    identity, _head = self._seed_row(
                        self._store(),
                        self.first,
                        client_id="client-2",
                    )
                    identity_ref.set(identity, merge=False)
                elif mode == "noncorrelated_head":
                    _identity, head = self._seed_row(
                        self._store(),
                        self.first,
                        created_at="2026-08-04T11:59:58.000000Z",
                    )
                    head_ref.set(head, merge=False)
                elif mode == "missing_head":
                    head_ref.delete()
                elif mode == "malformed_head":
                    malformed = deepcopy(store.data[head_ref.path])
                    malformed["unknown"] = None
                    head_ref.set(malformed, merge=False)
                before = deepcopy(store.data)
                store.events.clear()
                with self.assertRaises(error):
                    self._bind(store)
                self.assertEqual(before, store.data)
                self.assertEqual([], self._write_events(store))


    def test_binding_rejects_each_isolated_scope_and_row_correlation_drift(self):
        for mode in (
            "identity_scope",
            "head_scope",
            "identity_row",
            "head_row",
        ):
            with self.subTest(mode=mode):
                store = self._store()
                self._seed_two_rows(store)
                identity_ref, head_ref = self._row_references(
                    store, self.first
                )
                identity = deepcopy(store.data[identity_ref.path])
                head = deepcopy(store.data[head_ref.path])
                if mode == "identity_scope":
                    identity = self.module.build_row_identity_document(
                        user_scope_hash="f" * 64,
                        row_id=identity["rowId"],
                        client_id=identity["clientId"],
                        spreadsheet_id=identity["spreadsheetId"],
                        sheet_id=identity["sheetId"],
                        creation_kind=identity["creationKind"],
                        creation_source_hash=identity["creationSourceHash"],
                        created_at=identity["createdAt"],
                    )
                    identity_ref.set(identity, merge=False)
                elif mode == "head_scope":
                    head["userScopeHash"] = "f" * 64
                    head_ref.set(self._rehash_head(head), merge=False)
                elif mode == "identity_row":
                    identity = self.module.build_row_identity_document(
                        user_scope_hash=identity["userScopeHash"],
                        row_id=_row_id(3),
                        client_id=identity["clientId"],
                        spreadsheet_id=identity["spreadsheetId"],
                        sheet_id=identity["sheetId"],
                        creation_kind=identity["creationKind"],
                        creation_source_hash=identity["creationSourceHash"],
                        created_at=identity["createdAt"],
                    )
                    identity_ref.set(identity, merge=False)
                else:
                    head["rowId"] = _row_id(3)
                    head_ref.set(self._rehash_head(head), merge=False)
                before = deepcopy(store.data)
                store.events.clear()
                with self.assertRaises(self.module.RowAuthorityConflict):
                    self._bind(store)
                self.assertEqual(before, store.data)
                self.assertEqual([], self._write_events(store))

    def test_identical_binding_workers_yield_created_and_already_applied(self):
        store = self._store()
        self._seed_two_rows(store)
        store.events.clear()
        store.before_commit_barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self._bind, store) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(
            ["already_applied", "created"],
            sorted(result["disposition"] for result in results),
        )
        self.assertEqual(
            results[0]["threadBinding"],
            results[1]["threadBinding"],
        )
        self.assertEqual(
            results[0]["reverseBindings"],
            results[1]["reverseBindings"],
        )
        self.assertEqual(1, store.events.count(("commit_applied", 3)))
        self.assertEqual(1, store.events.count(("commit_applied", 0)))
        self.assertTrue(
            any(event[0] == "commit_aborted_stale_read" for event in store.events)
        )

    def test_divergent_binding_workers_preserve_first_commit(self):
        store = self._store()
        self._seed_two_rows(store)
        store.events.clear()
        store.before_commit_barrier = Barrier(2)

        def first_proposal():
            return self._bind(store)

        def second_proposal():
            return self._bind(
                store,
                row_ids=[self.second],
                primary_row_id=self.second,
            )

        outcomes = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(first_proposal), pool.submit(second_proposal)]
            for future in futures:
                try:
                    outcomes.append(future.result(timeout=10))
                except Exception as exc:
                    outcomes.append(exc)
        results = [item for item in outcomes if type(item) is dict]
        failures = [item for item in outcomes if isinstance(item, Exception)]
        self.assertEqual(1, len(results))
        self.assertEqual("created", results[0]["disposition"])
        self.assertEqual(1, len(failures))
        self.assertIsInstance(failures[0], self.module.RowAuthorityConflict)
        user = store.collection("users").document(self.user_id)
        binding_ref = user.collection("threadRowBindings").document("thread-1")
        self.assertEqual(results[0]["threadBinding"], store.data[binding_ref.path])
        stored_edge_paths = {
            path
            for path in store.data
            if "/rowThreadBindings/" in path
        }
        self.assertEqual(
            {
                user.collection("rowThreadBindings").document(edge["edgeId"]).path
                for edge in results[0]["reverseBindings"]
            },
            stored_edge_paths,
        )

    def test_binding_preapply_failure_is_retryable_with_zero_writes(self):
        store = self._store()
        self._seed_two_rows(store)
        before = deepcopy(store.data)
        store.events.clear()
        store.fail_next_commit = RuntimeError("preapply binding failure")
        with self.assertRaises(self.module.RowAuthorityRetryable):
            self._bind(store)
        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))
        self.assertIn(("commit_failed_before_apply",), store.events)

    def test_binding_transactional_first_or_mid_read_failure_is_retryable_without_readback(self):
        for fail_at in (1, 4):
            with self.subTest(fail_at=fail_at):
                store = self._store()
                self._seed_two_rows(store)
                before = deepcopy(store.data)
                store.events.clear()
                reference_type = type(
                    store.collection("probe").document("reference")
                )
                original_get = reference_type.get
                counts = {"transactional": 0, "nontransactional": 0}

                def fail_selected_read(reference, *, transaction=None):
                    if transaction is None:
                        counts["nontransactional"] += 1
                    else:
                        counts["transactional"] += 1
                        if counts["transactional"] == fail_at:
                            raise RuntimeError(f"transaction read {fail_at} failed")
                    return original_get(reference, transaction=transaction)

                with patch.object(
                    reference_type,
                    "get",
                    new=fail_selected_read,
                ), self.assertRaises(
                    self.module.RowAuthorityRetryable
                ) as raised:
                    self._bind(store)
                self.assertIsInstance(raised.exception.__cause__, RuntimeError)
                self.assertEqual(fail_at, counts["transactional"])
                self.assertEqual(0, counts["nontransactional"])
                self.assertEqual(before, store.data)
                self.assertEqual([], self._write_events(store))
                self.assertIn(("transaction_rolled_back",), store.events)

    def test_binding_apply_then_raise_requires_exact_binding_and_all_edges(self):
        for failure in (
            RuntimeError("unknown binding commit"),
            self.module.RowAuthorityRetryable("retryable after apply"),
            self.module.RowAuthorityAmbiguous("ambiguous after apply"),
        ):
            with self.subTest(failure=type(failure).__name__):
                store = self._store()
                self._seed_two_rows(store)
                store.events.clear()
                store.apply_then_raise_next_commit = failure
                result = self._bind(store)
                self.assertEqual("created", result["disposition"])
                binding_ref, edge_refs = self._binding_references(
                    store, result["threadBinding"]
                )
                self.assertEqual(
                    {binding_ref.path, *(reference.path for reference in edge_refs)},
                    {
                        path
                        for path in store.data
                        if "/threadRowBindings/" in path
                        or "/rowThreadBindings/" in path
                    },
                )
                failure_index = store.events.index(
                    ("commit_raised_after_apply",)
                )
                readback_paths = [
                    event[1]
                    for event in store.events[failure_index + 1 :]
                    if event[0] == "get"
                ]
                expected_paths = [binding_ref.path]
                for row_id in (self.first, self.second):
                    expected_paths.extend(
                        reference.path
                        for reference in self._row_references(store, row_id)
                    )
                expected_paths.extend(reference.path for reference in edge_refs)
                self.assertEqual(expected_paths, readback_paths)

        existing_store = self._store()
        self._seed_two_rows(existing_store)
        self._bind(existing_store)

        def observe_existing_then_raise(transaction, callback):
            transaction._begin()
            self.assertEqual("already_applied", callback(transaction))
            transaction._rollback()
            raise RuntimeError("unknown zero-write binding outcome")

        existing_store.events.clear()
        existing = self._bind(
            existing_store,
            executor=observe_existing_then_raise,
        )
        self.assertEqual("already_applied", existing["disposition"])
        self.assertEqual([], self._write_events(existing_store))

    def test_binding_partial_or_malformed_readback_is_ambiguous(self):
        def readback_executor(store, *, mode):
            def execute(transaction, callback):
                transaction._begin()
                callback(transaction)
                operations = [
                    (reference, deepcopy(payload))
                    for _operation, reference, payload, _merge
                    in transaction._operations
                ]
                transaction._rollback()
                selected = operations[:1] if mode == "partial" else operations
                for index, (reference, payload) in enumerate(selected):
                    if mode == "malformed" and index == len(selected) - 1:
                        payload["schemaVersion"] = 2
                    reference.create(payload)
                if mode == "head_drift":
                    _identity_ref, head_ref = self._row_references(
                        store, self.first
                    )
                    head = deepcopy(store.data[head_ref.path])
                    head.update(
                        {
                            "stateRevision": head["stateRevision"] + 1,
                            "currentLocationRevision": (
                                head["currentLocationRevision"] + 1
                            ),
                            "currentLocationHash": "d" * 64,
                            "updatedAt": "2026-08-04T12:00:02.000000Z",
                        }
                    )
                    head_ref.set(self._rehash_head(head), merge=False)
                raise RuntimeError(f"{mode} unknown binding commit")

            return execute

        for mode in ("partial", "malformed", "head_drift"):
            with self.subTest(mode=mode):
                store = self._store()
                self._seed_two_rows(store)
                store.events.clear()
                with self.assertRaises(self.module.RowAuthorityAmbiguous):
                    self._bind(
                        store,
                        executor=readback_executor(store, mode=mode),
                    )

    def test_binding_129_overflow_never_opens_transaction(self):
        rows = [_row_id(index) for index in range(1, 130)]
        success_store = self._store()
        for row_id in rows[:128]:
            self._seed_row(success_store, row_id)
        success_store.events.clear()
        result = self._bind(
            success_store,
            row_ids=rows[:128],
            primary_row_id=rows[0],
        )
        self.assertEqual(128, result["threadBinding"]["bindingCount"])
        self.assertEqual(128, len(result["reverseBindings"]))
        binding_ref, edge_refs = self._binding_references(
            success_store, result["threadBinding"]
        )
        self.assertEqual(
            [
                (
                    "create",
                    binding_ref.path,
                    result["threadBinding"],
                    False,
                ),
                *(
                    ("create", reference.path, document, False)
                    for reference, document in zip(
                        edge_refs, result["reverseBindings"]
                    )
                ),
            ],
            self._write_events(success_store),
        )
        self.assertIn(("commit_applied", 129), success_store.events)

        class NeverReferenceFirestore:
            def __init__(self):
                self.collection_calls = 0
                self.transaction_calls = 0

            def collection(self, _name):
                self.collection_calls += 1
                raise AssertionError("reference creation was reached")

            def transaction(self):
                self.transaction_calls += 1
                raise AssertionError("transaction creation was reached")

        overflow_store = NeverReferenceFirestore()
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self._authority(overflow_store).bind_thread_rows(
                verified_user_id=self.user_id,
                thread_id="thread-overflow",
                client_id="client-1",
                row_ids=rows,
                primary_row_id=rows[0],
                created_at=self.binding_at,
            )
        self.assertEqual(0, overflow_store.collection_calls)
        self.assertEqual(0, overflow_store.transaction_calls)

    def test_deleted_identity_remains_bindable_for_late_root_without_head_mutation(self):
        store = self._store()
        self._seed_row(store, self.first, lifecycle="deleted")
        _identity_ref, head_ref = self._row_references(store, self.first)
        head_before = deepcopy(store.data[head_ref.path])
        store.events.clear()
        result = self._bind(
            store,
            row_ids=[self.first],
            primary_row_id=self.first,
        )
        self.assertEqual("created", result["disposition"])
        self.assertEqual("deleted", head_before["currentLocationLifecycle"])
        self.assertEqual(head_before, store.data[head_ref.path])
        binding_ref, edge_refs = self._binding_references(
            store, result["threadBinding"]
        )
        self.assertEqual(
            [
                (
                    "create",
                    binding_ref.path,
                    result["threadBinding"],
                    False,
                ),
                (
                    "create",
                    edge_refs[0].path,
                    result["reverseBindings"][0],
                    False,
                ),
            ],
            self._write_events(store),
        )
        self.assertIn(("commit_applied", 2), store.events)

    def test_thread_binding_time_equal_latest_prerequisite_is_valid_and_earlier_is_rejected(self):
        equal_store = self._store()
        self._seed_two_rows(equal_store)
        equal_store.events.clear()
        result = self._bind(equal_store, created_at=self.created_at)
        self.assertEqual("created", result["disposition"])

        early_store = self._store()
        self._seed_two_rows(early_store)
        before = deepcopy(early_store.data)
        early_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._bind(
                early_store,
                created_at="2026-08-04T11:59:59.000000Z",
            )
        self.assertEqual(before, early_store.data)
        self.assertEqual([], self._write_events(early_store))

        forged_store = self._store()
        self._seed_two_rows(forged_store)
        forged = self.module.build_thread_row_binding_document(
            user_scope_hash=self.scope,
            thread_id="thread-1",
            client_id="client-1",
            row_ids=[self.second, self.first],
            primary_row_id=self.first,
            created_at="2026-08-04T11:59:59.000000Z",
        )
        binding_ref, edge_refs = self._binding_references(
            forged_store, forged
        )
        binding_ref.create(forged)
        for reference, edge in zip(
            edge_refs,
            self.module.build_row_thread_binding_documents(
                thread_binding_document=forged
            ),
        ):
            reference.create(edge)
        before = deepcopy(forged_store.data)
        forged_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._bind(
                forged_store,
                created_at="2026-08-04T11:59:59.000000Z",
            )
        self.assertEqual(before, forged_store.data)
        self.assertEqual([], self._write_events(forged_store))

    def test_new_binding_must_not_predate_a_valid_advanced_head(self):
        store = self._store()
        self._seed_two_rows(store)
        _identity_ref, head_ref = self._row_references(store, self.first)
        head = deepcopy(store.data[head_ref.path])
        head.update(
            {
                "stateRevision": head["stateRevision"] + 1,
                "currentLocationRevision": head["currentLocationRevision"] + 1,
                "currentLocationHash": "c" * 64,
                "updatedAt": "2026-08-04T12:00:02.000000Z",
            }
        )
        advanced = self._rehash_head(head)
        self.assertLess(advanced["createdAt"], self.binding_at)
        self.assertLess(self.binding_at, advanced["updatedAt"])
        head_ref.set(advanced, merge=False)
        before = deepcopy(store.data)
        store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._bind(store, created_at=self.binding_at)
        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))

    def test_binding_ignores_legacy_row_number_rows_and_thread_order(self):
        store = self._store()
        self._seed_two_rows(store)
        legacy = store.collection("users").document(self.user_id).collection(
            "threads"
        ).document("thread-1")
        legacy.create(
            {
                "rowNumber": 999,
                "rows": [self.second],
                "rowBindings": [{"rowId": self.second, "role": "primary"}],
                "primaryRowId": self.second,
                "rowBindingsHash": "f" * 64,
            }
        )
        store.events.clear()
        result = self._bind(
            store,
            row_ids=[self.second, self.first, self.second],
        )
        self.assertEqual(
            [self.first, self.second],
            [item["rowId"] for item in result["threadBinding"]["rowBindings"]],
        )
        self.assertFalse(
            any("/threads/" in event[1] for event in store.events if event[0] == "get")
        )

    def test_exact_binding_and_edges_retry_is_zero_write_already_applied(self):
        store = self._store()
        self._seed_two_rows(store)
        created = self._bind(store)
        _identity_ref, first_head_ref = self._row_references(store, self.first)
        advanced_head = deepcopy(store.data[first_head_ref.path])
        advanced_head.update(
            {
                "stateRevision": advanced_head["stateRevision"] + 1,
                "currentLocationRevision": (
                    advanced_head["currentLocationRevision"] + 1
                ),
                "currentLocationHash": "e" * 64,
                "updatedAt": "2026-08-04T12:00:02.000000Z",
            }
        )
        first_head_ref.set(self._rehash_head(advanced_head), merge=False)
        before = deepcopy(store.data)
        store.events.clear()
        replay = self._bind(store)
        self.assertEqual("created", created["disposition"])
        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))
        self.assertIn(("commit_applied", 0), store.events)

    def test_partial_presence_is_ambiguous_and_immutable_drift_is_conflict(self):
        for mode, error in (
            ("missing_edge", self.module.RowAuthorityAmbiguous),
            ("binding_drift", self.module.RowAuthorityConflict),
            ("edge_drift", self.module.RowAuthorityConflict),
        ):
            with self.subTest(mode=mode):
                store = self._store()
                self._seed_two_rows(store)
                created = self._bind(store)
                binding_ref, edge_refs = self._binding_references(
                    store, created["threadBinding"]
                )
                if mode == "missing_edge":
                    edge_refs[-1].delete()
                elif mode == "binding_drift":
                    drift = deepcopy(store.data[binding_ref.path])
                    drift["bindingHash"] = "f" * 64
                    binding_ref.set(drift, merge=False)
                else:
                    drift = deepcopy(store.data[edge_refs[-1].path])
                    drift["edgeHash"] = "f" * 64
                    edge_refs[-1].set(drift, merge=False)
                before = deepcopy(store.data)
                store.events.clear()
                with self.assertRaises(error):
                    self._bind(store)
                self.assertEqual(before, store.data)
                self.assertEqual([], self._write_events(store))


class ContactRowAssociationStoreTests(unittest.TestCase):
    @staticmethod
    def _fakes():
        return importlib.import_module("tests.row_authority_fakes")

    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")

    def setUp(self):
        self.assertTrue(
            hasattr(
                self.module.RowAuthorityStore,
                "record_contact_row_association",
            ),
            "RowAuthorityStore.record_contact_row_association is missing",
        )
        self.user_id = "uid-1"
        self.scope = self.module.user_scope_hash(self.user_id)
        self.first = _row_id(1)
        self.second = _row_id(2)
        self.thread_id = "thread-1"
        self.canonical_hash = "c" * 64
        self.exact_hash = "d" * 64
        self.row_created_at = "2026-08-04T12:00:00.000000Z"
        self.binding_at = "2026-08-04T12:00:01.000000Z"
        self.association_at = "2026-08-04T12:00:02.000000Z"

    def _store(self):
        return self._fakes().BoundedFakeFirestore()

    def _authority(self, store, *, executor=None):
        return self.module.RowAuthorityStore(
            store,
            transaction_executor=(
                executor or self._fakes().run_bounded_transaction
            ),
        )

    def _user_reference(self, store):
        return store.collection("users").document(self.user_id)

    def _row_references(self, store, row_id):
        user = self._user_reference(store)
        return (
            user.collection("rowIdentities").document(row_id),
            user.collection("rowAuthorityHeads").document(row_id),
        )

    def _rehash_row_head(self, head):
        payload = {
            key: value
            for key, value in head.items()
            if key not in {"schemaVersion", "userScopeHash", "headHash"}
        }
        head["headHash"] = self.module.domain_hash(
            "sitesift.row.authority_head.v1",
            payload,
            user_scope_hash=head["userScopeHash"],
        )
        return self.module.validate_row_authority_head(document=head)

    def _seed_row(
        self,
        store,
        row_id,
        *,
        lifecycle="active",
        created_at=None,
    ):
        created_at = created_at or self.row_created_at
        index = 0 if row_id == self.first else 1
        identity = self.module.build_row_identity_document(
            user_scope_hash=self.scope,
            row_id=row_id,
            client_id="client-1",
            spreadsheet_id="spreadsheet-1",
            sheet_id=0,
            creation_kind="fresh",
            creation_source_hash="1" * 64,
            created_at=created_at,
        )
        observation = self.module.build_row_observation(
            spreadsheet_id="spreadsheet-1",
            marker_observation={
                "rowId": row_id,
                "sheetId": 0,
                "providerRowIndex": index,
                "displayRowNumber": index + 1,
                "metadataId": index + 1,
            },
            ordered_headers=("Email",),
            ordered_cell_values=(f"row-{index}@example.com",),
            user_scope_hash=self.scope,
        )
        revision = self.module.build_row_location_revision_document(
            identity_document=identity,
            revision=1,
            lifecycle="active",
            observations=(observation,),
            previous_revision_hash=None,
            observed_at=created_at,
        )
        head = self.module.build_initial_row_authority_head(
            identity_document=identity,
            location_revision_document=revision,
            created_at=created_at,
        )
        if lifecycle == "deleted":
            head.update(
                {
                    "stateRevision": 2,
                    "currentLocationRevision": 2,
                    "currentLocationHash": "2" * 64,
                    "currentLocationLifecycle": "deleted",
                    "updatedAt": self.binding_at,
                }
            )
            head = self._rehash_row_head(head)
        identity_ref, head_ref = self._row_references(store, row_id)
        identity_ref.create(identity)
        head_ref.create(head)
        return identity, head

    def _seed_thread_binding(
        self,
        store,
        *,
        row_ids=None,
        thread_id=None,
        created_at=None,
    ):
        row_ids = tuple(row_ids or (self.first,))
        thread_id = thread_id or self.thread_id
        created_at = created_at or self.binding_at
        binding = self.module.build_thread_row_binding_document(
            user_scope_hash=self.scope,
            thread_id=thread_id,
            client_id="client-1",
            row_ids=row_ids,
            primary_row_id=row_ids[0],
            created_at=created_at,
        )
        reverse = self.module.build_row_thread_binding_documents(
            thread_binding_document=binding
        )
        user = self._user_reference(store)
        binding_ref = user.collection("threadRowBindings").document(thread_id)
        binding_ref.create(binding)
        reverse_refs = tuple(
            user.collection("rowThreadBindings").document(document["edgeId"])
            for document in reverse
        )
        for reference, document in zip(reverse_refs, reverse):
            reference.create(document)
        return binding, reverse, binding_ref, reverse_refs

    def _seed_prerequisites(
        self,
        store,
        *,
        row_ids=None,
        lifecycle="active",
        thread_id=None,
        row_created_at=None,
    ):
        row_ids = tuple(row_ids or (self.first,))
        seeded_rows = {
            row_id: self._seed_row(
                store,
                row_id,
                lifecycle=lifecycle,
                created_at=row_created_at,
            )
            for row_id in row_ids
        }
        binding = self._seed_thread_binding(
            store,
            row_ids=row_ids,
            thread_id=thread_id,
        )
        return seeded_rows, binding

    def _association_documents(
        self,
        binding,
        *,
        row_id=None,
        thread_id=None,
        exact_identity_hash=None,
        created_at=None,
    ):
        row_id = row_id or self.first
        thread_id = thread_id or self.thread_id
        exact_identity_hash = exact_identity_hash or self.exact_hash
        created_at = created_at or self.association_at
        association = self.module.build_contact_row_binding_document(
            user_scope_hash=self.scope,
            canonical_mailbox_identity_hash=self.canonical_hash,
            row_id=row_id,
            created_at=created_at,
        )
        evidence = self.module.build_contact_row_binding_evidence_document(
            user_scope_hash=self.scope,
            edge_id=association["edgeId"],
            thread_id=thread_id,
            thread_binding_hash=binding["bindingHash"],
            exact_identity_hash=exact_identity_hash,
            created_at=created_at,
        )
        return association, evidence

    def _association_references(
        self,
        store,
        binding,
        *,
        row_id=None,
        thread_id=None,
        exact_identity_hash=None,
        created_at=None,
    ):
        row_id = row_id or self.first
        thread_id = thread_id or self.thread_id
        association, evidence = self._association_documents(
            binding,
            row_id=row_id,
            thread_id=thread_id,
            exact_identity_hash=exact_identity_hash,
            created_at=created_at,
        )
        reverse_by_row = {
            document["rowId"]: document
            for document in self.module.build_row_thread_binding_documents(
                thread_binding_document=binding
            )
        }
        user = self._user_reference(store)
        references = {
            "optout": user.collection("contactOptOutHeads").document(
                self.canonical_hash
            ),
            "binding": user.collection("threadRowBindings").document(
                thread_id
            ),
            "reverse": user.collection("rowThreadBindings").document(
                reverse_by_row[row_id]["edgeId"]
            ),
            "identity": user.collection("rowIdentities").document(row_id),
            "row_head": user.collection("rowAuthorityHeads").document(row_id),
            "association": user.collection("contactRowBindings").document(
                association["edgeId"]
            ),
            "evidence": user.collection(
                "contactRowBindingEvidence"
            ).document(evidence["evidenceId"]),
            "binding_head": user.collection(
                "contactRowBindingHeads"
            ).document(self.canonical_hash),
        }
        return association, evidence, references

    def _associate(self, store, *, executor=None, **overrides):
        arguments = {
            "verified_user_id": self.user_id,
            "canonical_mailbox_identity_hash": self.canonical_hash,
            "exact_identity_hash": self.exact_hash,
            "row_id": self.first,
            "thread_id": self.thread_id,
            "created_at": self.association_at,
        }
        arguments.update(overrides)
        return self._authority(
            store,
            executor=executor,
        ).record_contact_row_association(**arguments)

    @staticmethod
    def _write_events(store):
        return [
            event
            for event in store.events
            if event[0] in {"create", "set", "update", "delete"}
        ]

    def test_first_contact_association_reads_prerequisites_then_writes_exact_three(self):
        store = self._store()
        _rows, (binding, _reverse, _binding_ref, _reverse_refs) = (
            self._seed_prerequisites(store)
        )
        association, evidence, references = self._association_references(
            store,
            binding,
        )
        store.events.clear()

        result = self._associate(store)

        expected_read_order = [
            references[name].path
            for name in (
                "optout",
                "binding",
                "reverse",
                "identity",
                "row_head",
                "association",
                "evidence",
                "binding_head",
            )
        ]
        self.assertEqual(
            expected_read_order,
            [event[1] for event in store.events if event[0] == "get"],
        )
        read_indexes = [
            index for index, event in enumerate(store.events) if event[0] == "get"
        ]
        write_indexes = [
            index
            for index, event in enumerate(store.events)
            if event[0] in {"create", "set", "update", "delete"}
        ]
        self.assertLess(max(read_indexes), min(write_indexes))
        self.assertEqual("created", result["disposition"])
        self.assertEqual(association, result["association"])
        self.assertEqual(evidence, result["evidence"])
        self.assertEqual(
            [
                ("create", references["association"].path, association, False),
                ("create", references["evidence"].path, evidence, False),
                (
                    "create",
                    references["binding_head"].path,
                    result["bindingHead"],
                    False,
                ),
            ],
            self._write_events(store),
        )
        self.assertEqual(1, result["bindingHead"]["stateRevision"])
        self.assertEqual(1, result["bindingHead"]["associationCount"])
        self.assertEqual(
            association["contactRowEdgeHash"],
            result["bindingHead"]["lastAssociationHash"],
        )

    def test_empty_contact_binding_head_advances_to_one(self):
        store = self._store()
        _rows, (binding, _reverse, _binding_ref, _reverse_refs) = (
            self._seed_prerequisites(store)
        )
        association, _evidence, references = self._association_references(
            store,
            binding,
        )
        empty = self.module.build_contact_row_binding_head_document(
            user_scope_hash=self.scope,
            canonical_mailbox_identity_hash=self.canonical_hash,
            state_revision=4,
            association_count=0,
            last_association_hash=None,
            created_at=self.binding_at,
            updated_at=self.binding_at,
        )
        references["binding_head"].create(empty)
        store.events.clear()

        result = self._associate(store)

        self.assertEqual("created", result["disposition"])
        head = result["bindingHead"]
        self.assertEqual(5, head["stateRevision"])
        self.assertEqual(1, head["associationCount"])
        self.assertEqual(association["contactRowEdgeHash"], head["lastAssociationHash"])
        self.assertEqual(empty["createdAt"], head["createdAt"])
        self.assertEqual(self.association_at, head["updatedAt"])
        self.assertEqual(
            ["create", "create", "set"],
            [event[0] for event in self._write_events(store)],
        )
        self.assertEqual(False, self._write_events(store)[-1][3])

    def test_existing_edge_new_evidence_writes_only_evidence_and_preserves_head(self):
        store = self._store()
        _rows, (binding, _reverse, _binding_ref, _reverse_refs) = (
            self._seed_prerequisites(store)
        )
        created = self._associate(store)
        original_head = deepcopy(created["bindingHead"])
        original_association = deepcopy(created["association"])
        _association, evidence, references = self._association_references(
            store,
            binding,
            exact_identity_hash="e" * 64,
            created_at="2026-08-04T12:00:03.000000Z",
        )
        store.events.clear()

        result = self._associate(
            store,
            exact_identity_hash="e" * 64,
            created_at="2026-08-04T12:00:03.000000Z",
        )

        self.assertEqual("evidence_created", result["disposition"])
        self.assertEqual(original_association, result["association"])
        self.assertEqual(evidence, result["evidence"])
        self.assertEqual(original_head, result["bindingHead"])
        self.assertEqual(
            [("create", references["evidence"].path, evidence, False)],
            self._write_events(store),
        )

    def test_later_recreated_thread_adds_evidence_to_older_stable_edge(self):
        store = self._store()
        self._seed_prerequisites(store)
        first = self._associate(store)
        second_binding, _reverse, _binding_ref, _reverse_refs = (
            self._seed_thread_binding(
                store,
                row_ids=(self.first,),
                thread_id="thread-2",
                created_at="2026-08-04T12:00:04.000000Z",
            )
        )
        _candidate, second_evidence, references = (
            self._association_references(
                store,
                second_binding,
                thread_id="thread-2",
                exact_identity_hash="e" * 64,
                created_at="2026-08-04T12:00:05.000000Z",
            )
        )
        association_before = deepcopy(first["association"])
        head_before = deepcopy(first["bindingHead"])
        store.events.clear()

        result = self._associate(
            store,
            thread_id="thread-2",
            exact_identity_hash="e" * 64,
            created_at="2026-08-04T12:00:05.000000Z",
        )

        self.assertEqual("evidence_created", result["disposition"])
        self.assertEqual(association_before, result["association"])
        self.assertEqual(second_evidence, result["evidence"])
        self.assertEqual(head_before, result["bindingHead"])
        self.assertEqual(
            [
                (
                    "create",
                    references["evidence"].path,
                    second_evidence,
                    False,
                )
            ],
            self._write_events(store),
        )

    def test_exact_edge_evidence_and_head_retry_is_zero_write(self):
        store = self._store()
        self._seed_prerequisites(store)
        created = self._associate(store)
        before = deepcopy(store.data)
        store.events.clear()

        replay = self._associate(store)

        self.assertEqual("created", created["disposition"])
        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))
        self.assertIn(("commit_applied", 0), store.events)

    def test_supporting_thread_binding_and_reverse_edge_are_required_and_exact(self):
        for mode, error in (
            ("missing_binding", self.module.RowAuthorityAmbiguous),
            ("missing_reverse", self.module.RowAuthorityAmbiguous),
            ("binding_drift", self.module.RowAuthorityConflict),
            ("reverse_drift", self.module.RowAuthorityConflict),
        ):
            with self.subTest(mode=mode):
                store = self._store()
                _rows, (binding, _reverse, binding_ref, reverse_refs) = (
                    self._seed_prerequisites(
                        store,
                        row_ids=(self.first, self.second),
                    )
                )
                if mode == "missing_binding":
                    binding_ref.delete()
                elif mode == "missing_reverse":
                    reverse_refs[0].delete()
                elif mode == "binding_drift":
                    drift = self.module.build_thread_row_binding_document(
                        user_scope_hash=self.scope,
                        thread_id=self.thread_id,
                        client_id="client-2",
                        row_ids=(self.first, self.second),
                        primary_row_id=self.first,
                        created_at=self.binding_at,
                    )
                    binding_ref.set(drift, merge=False)
                else:
                    alternate = self.module.build_thread_row_binding_document(
                        user_scope_hash=self.scope,
                        thread_id=self.thread_id,
                        client_id="client-1",
                        row_ids=(self.first, self.second),
                        primary_row_id=self.second,
                        created_at=self.binding_at,
                    )
                    drift = next(
                        document
                        for document in self.module.build_row_thread_binding_documents(
                            thread_binding_document=alternate
                        )
                        if document["rowId"] == self.first
                    )
                    reverse_refs[0].set(drift, merge=False)
                before = deepcopy(store.data)
                store.events.clear()

                with self.assertRaises(error):
                    self._associate(store)

                self.assertEqual(before, store.data)
                self.assertEqual([], self._write_events(store))

    def test_association_rejects_each_isolated_row_identity_and_head_drift(self):
        for mode in (
            "identity_scope",
            "identity_row",
            "identity_client",
            "head_scope",
            "head_row",
            "head_created_at",
        ):
            with self.subTest(mode=mode):
                store = self._store()
                self._seed_prerequisites(store)
                identity_ref, head_ref = self._row_references(
                    store,
                    self.first,
                )
                identity = deepcopy(store.data[identity_ref.path])
                head = deepcopy(store.data[head_ref.path])
                if mode.startswith("identity_"):
                    values = {
                        "user_scope_hash": identity["userScopeHash"],
                        "row_id": identity["rowId"],
                        "client_id": identity["clientId"],
                        "spreadsheet_id": identity["spreadsheetId"],
                        "sheet_id": identity["sheetId"],
                        "creation_kind": identity["creationKind"],
                        "creation_source_hash": identity[
                            "creationSourceHash"
                        ],
                        "created_at": identity["createdAt"],
                    }
                    if mode == "identity_scope":
                        values["user_scope_hash"] = "f" * 64
                    elif mode == "identity_row":
                        values["row_id"] = _row_id(3)
                    else:
                        values["client_id"] = "client-2"
                    identity_ref.set(
                        self.module.build_row_identity_document(**values),
                        merge=False,
                    )
                else:
                    if mode == "head_scope":
                        head["userScopeHash"] = "f" * 64
                    elif mode == "head_row":
                        head["rowId"] = _row_id(3)
                    else:
                        head[
                            "createdAt"
                        ] = "2026-08-04T11:59:59.000000Z"
                    head_ref.set(self._rehash_row_head(head), merge=False)
                before = deepcopy(store.data)
                store.events.clear()

                with self.assertRaises(self.module.RowAuthorityConflict):
                    self._associate(store)

                self.assertEqual(before, store.data)
                self.assertEqual([], self._write_events(store))

    def test_deleted_row_accepts_historical_contact_evidence_but_grants_no_claim(self):
        store = self._store()
        rows, _binding = self._seed_prerequisites(
            store,
            lifecycle="deleted",
        )
        identity_before, head_before = deepcopy(rows[self.first])
        store.events.clear()

        result = self._associate(store)

        identity_ref, head_ref = self._row_references(store, self.first)
        self.assertEqual("created", result["disposition"])
        self.assertEqual(identity_before, store.data[identity_ref.path])
        self.assertEqual(head_before, store.data[head_ref.path])
        accessed = "\n".join(
            event[1]
            for event in store.events
            if event[0] in {"get", "create", "set", "update", "delete"}
        )
        for forbidden in (
            "rowClaimSets",
            "rowOwnerGenerations",
            "rowOwnerSettlements",
        ):
            self.assertNotIn(forbidden, accessed)

    def test_malformed_contact_optout_head_blocks_association_with_zero_writes(self):
        store = self._store()
        _rows, (binding, _reverse, _binding_ref, _reverse_refs) = (
            self._seed_prerequisites(store)
        )
        _association, _evidence, references = self._association_references(
            store,
            binding,
        )
        references["optout"].create(
            {"malformed": "existence alone is the sentinel"}
        )
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self._associate(store)

        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))
        self.assertEqual(
            [references["optout"].path],
            [event[1] for event in store.events if event[0] == "get"],
        )

    def test_malformed_contact_optout_head_blocks_exact_association_replay_first(self):
        store = self._store()
        _rows, (binding, _reverse, _binding_ref, _reverse_refs) = (
            self._seed_prerequisites(store)
        )
        self._associate(store)
        _association, _evidence, references = self._association_references(
            store,
            binding,
        )
        references["optout"].create({"existence": "is the sentinel"})
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self._associate(store)

        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))
        self.assertEqual(
            [references["optout"].path],
            [event[1] for event in store.events if event[0] == "get"],
        )

    def test_association_without_contact_head_skips_contact_fanout_lineage(self):
        store = self._store()
        self._seed_prerequisites(store)
        store.events.clear()

        self._associate(store)

        forbidden = (
            "contactOptOutAliases",
            "contactOptOutSettlements",
            "contactOptOutFanoutHeads",
            "contactOptOutFanoutObligations",
            "contactOptOutFanoutResults",
            "contactOptOutRelease",
        )
        accessed = "\n".join(
            event[1]
            for event in store.events
            if event[0] in {"get", "create", "set", "update", "delete"}
        )
        for collection in forbidden:
            self.assertNotIn(collection, accessed)

    def test_identical_first_association_workers_create_one_edge_and_evidence(self):
        store = self._store()
        self._seed_prerequisites(store)
        store.before_commit_barrier = Barrier(2)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self._associate, store) for _index in range(2)]
            results = [future.result(timeout=10) for future in futures]

        self.assertEqual(
            ["already_applied", "created"],
            sorted(result["disposition"] for result in results),
        )
        association_paths = [
            path for path in store.data if "/contactRowBindings/" in path
        ]
        evidence_paths = [
            path
            for path in store.data
            if "/contactRowBindingEvidence/" in path
        ]
        head_paths = [
            path
            for path in store.data
            if "/contactRowBindingHeads/" in path
        ]
        self.assertEqual((1, 1, 1), (
            len(association_paths),
            len(evidence_paths),
            len(head_paths),
        ))
        self.assertEqual(1, store.data[head_paths[0]]["associationCount"])
        self.assertTrue(
            any(event[0] == "commit_aborted_stale_read" for event in store.events)
        )

    def test_different_evidence_workers_create_one_edge_two_evidence_and_count_one(self):
        store = self._store()
        self._seed_prerequisites(store)
        store.before_commit_barrier = Barrier(2)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(
                self._associate,
                store,
                exact_identity_hash="d" * 64,
            )
            second_future = pool.submit(
                self._associate,
                store,
                exact_identity_hash="e" * 64,
            )
            results = [
                first_future.result(timeout=10),
                second_future.result(timeout=10),
            ]

        self.assertEqual(
            ["created", "evidence_created"],
            sorted(result["disposition"] for result in results),
        )
        association_paths = [
            path for path in store.data if "/contactRowBindings/" in path
        ]
        evidence_paths = [
            path
            for path in store.data
            if "/contactRowBindingEvidence/" in path
        ]
        head_path = next(
            path
            for path in store.data
            if "/contactRowBindingHeads/" in path
        )
        self.assertEqual(1, len(association_paths))
        self.assertEqual(2, len(evidence_paths))
        self.assertEqual(1, store.data[head_path]["associationCount"])
        self.assertEqual(1, store.data[head_path]["stateRevision"])

    def test_different_row_workers_cas_retry_to_count_two(self):
        store = self._store()
        self._seed_prerequisites(
            store,
            row_ids=(self.first, self.second),
        )
        store.before_commit_barrier = Barrier(2)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self._associate, store, row_id=row_id)
                for row_id in (self.first, self.second)
            ]
            results = [future.result(timeout=10) for future in futures]

        self.assertEqual(
            ["created", "created"],
            sorted(result["disposition"] for result in results),
        )
        association_paths = [
            path for path in store.data if "/contactRowBindings/" in path
        ]
        evidence_paths = [
            path
            for path in store.data
            if "/contactRowBindingEvidence/" in path
        ]
        head_path = next(
            path
            for path in store.data
            if "/contactRowBindingHeads/" in path
        )
        head = store.data[head_path]
        self.assertEqual(2, len(association_paths))
        self.assertEqual(2, len(evidence_paths))
        self.assertEqual(2, head["associationCount"])
        self.assertEqual(2, head["stateRevision"])
        self.assertIn(
            head["lastAssociationHash"],
            {
                result["association"]["contactRowEdgeHash"]
                for result in results
            },
        )

    def test_old_association_retry_after_another_row_preserves_advanced_head(self):
        store = self._store()
        self._seed_prerequisites(
            store,
            row_ids=(self.first, self.second),
        )
        first = self._associate(store, row_id=self.first)
        second = self._associate(
            store,
            row_id=self.second,
            created_at="2026-08-04T12:00:03.000000Z",
        )
        advanced_head = deepcopy(second["bindingHead"])
        before = deepcopy(store.data)
        store.events.clear()

        replay = self._associate(store, row_id=self.first)

        self.assertEqual("created", first["disposition"])
        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(2, replay["bindingHead"]["associationCount"])
        self.assertEqual(advanced_head, replay["bindingHead"])
        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))

    def test_contact_binding_head_revision_does_not_order_association_count(self):
        store = self._store()
        _rows, (binding, _reverse, _binding_ref, _reverse_refs) = (
            self._seed_prerequisites(store)
        )
        created = self._associate(store)
        _association, _evidence, references = self._association_references(
            store,
            binding,
        )
        advanced = self.module.build_contact_row_binding_head_document(
            user_scope_hash=self.scope,
            canonical_mailbox_identity_hash=self.canonical_hash,
            state_revision=1,
            association_count=2,
            last_association_hash="f" * 64,
            created_at=created["bindingHead"]["createdAt"],
            updated_at="2026-08-04T12:00:03.000000Z",
        )
        references["binding_head"].set(advanced, merge=False)
        before = deepcopy(store.data)
        store.events.clear()

        replay = self._associate(store)

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(advanced, replay["bindingHead"])
        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))

    def test_same_last_association_hash_cannot_claim_a_later_head_update(self):
        store = self._store()
        _rows, (binding, _reverse, _binding_ref, _reverse_refs) = (
            self._seed_prerequisites(store)
        )
        created = self._associate(store)
        association, _evidence, references = self._association_references(
            store,
            binding,
        )
        unreachable = self.module.build_contact_row_binding_head_document(
            user_scope_hash=self.scope,
            canonical_mailbox_identity_hash=self.canonical_hash,
            state_revision=7,
            association_count=2,
            last_association_hash=association["contactRowEdgeHash"],
            created_at=created["bindingHead"]["createdAt"],
            updated_at="2026-08-04T12:00:03.000000Z",
        )
        references["binding_head"].set(unreachable, merge=False)
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._associate(store)

        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))

    def test_stored_thread_binding_cannot_predate_immutable_row_identity(self):
        store = self._store()
        self._seed_prerequisites(
            store,
            row_created_at="2026-08-04T12:00:02.000000Z",
        )
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self._associate(
                store,
                created_at="2026-08-04T12:00:03.000000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))

    def test_existing_association_cannot_predate_immutable_row_identity(self):
        for evidence_exists in (False, True):
            with self.subTest(evidence_exists=evidence_exists):
                store = self._store()
                self._seed_row(
                    store,
                    self.first,
                    created_at="2026-08-04T12:00:03.000000Z",
                )
                binding, _reverse, _binding_ref, _reverse_refs = (
                    self._seed_thread_binding(
                        store,
                        created_at="2026-08-04T12:00:04.000000Z",
                    )
                )
                stored_association = (
                    self.module.build_contact_row_binding_document(
                        user_scope_hash=self.scope,
                        canonical_mailbox_identity_hash=self.canonical_hash,
                        row_id=self.first,
                        created_at=self.association_at,
                    )
                )
                _proposed_association, evidence, references = (
                    self._association_references(
                        store,
                        binding,
                        created_at="2026-08-04T12:00:05.000000Z",
                    )
                )
                head = self.module.build_contact_row_binding_head_document(
                    user_scope_hash=self.scope,
                    canonical_mailbox_identity_hash=self.canonical_hash,
                    state_revision=1,
                    association_count=1,
                    last_association_hash=stored_association[
                        "contactRowEdgeHash"
                    ],
                    created_at=self.association_at,
                    updated_at=self.association_at,
                )
                references["association"].create(stored_association)
                if evidence_exists:
                    references["evidence"].create(evidence)
                references["binding_head"].create(head)
                before = deepcopy(store.data)
                store.events.clear()

                with self.assertRaises(self.module.RowAuthorityConflict):
                    self._associate(
                        store,
                        created_at="2026-08-04T12:00:05.000000Z",
                    )

                self.assertEqual(before, store.data)
                self.assertEqual([], self._write_events(store))

    def test_same_evidence_id_timestamp_drift_is_conflict(self):
        store = self._store()
        self._seed_prerequisites(store)
        created = self._associate(store)
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self._associate(
                store,
                created_at="2026-08-04T12:00:03.000000Z",
            )

        self.assertEqual("created", created["disposition"])
        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))

    def test_contact_evidence_time_cannot_precede_thread_binding_or_association(self):
        early_store = self._store()
        self._seed_prerequisites(early_store)
        before = deepcopy(early_store.data)
        early_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._associate(
                early_store,
                created_at="2026-08-04T12:00:00.500000Z",
            )
        self.assertEqual(before, early_store.data)
        self.assertEqual([], self._write_events(early_store))

        stable_store = self._store()
        self._seed_prerequisites(stable_store)
        self._associate(stable_store)
        stable_before = deepcopy(stable_store.data)
        stable_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._associate(
                stable_store,
                exact_identity_hash="e" * 64,
                created_at="2026-08-04T12:00:01.500000Z",
            )
        self.assertEqual(stable_before, stable_store.data)
        self.assertEqual([], self._write_events(stable_store))

        future_head_store = self._store()
        _rows, (binding, _reverse, _binding_ref, _reverse_refs) = (
            self._seed_prerequisites(future_head_store)
        )
        _association, _evidence, references = self._association_references(
            future_head_store,
            binding,
        )
        future_head = self.module.build_contact_row_binding_head_document(
            user_scope_hash=self.scope,
            canonical_mailbox_identity_hash=self.canonical_hash,
            state_revision=3,
            association_count=0,
            last_association_hash=None,
            created_at=self.binding_at,
            updated_at="2026-08-04T12:00:03.000000Z",
        )
        references["binding_head"].create(future_head)
        future_before = deepcopy(future_head_store.data)
        future_head_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._associate(future_head_store)
        self.assertEqual(future_before, future_head_store.data)
        self.assertEqual([], self._write_events(future_head_store))

        equal_store = self._store()
        self._seed_prerequisites(equal_store)
        equal = self._associate(equal_store, created_at=self.binding_at)
        self.assertEqual("created", equal["disposition"])

    def test_missing_head_edge_without_evidence_or_evidence_without_edge_is_ambiguous(self):
        for mode in (
            "edge_without_head",
            "evidence_without_edge",
            "edge_and_evidence_without_head",
        ):
            with self.subTest(mode=mode):
                store = self._store()
                _rows, (binding, _reverse, _binding_ref, _reverse_refs) = (
                    self._seed_prerequisites(store)
                )
                self._associate(store)
                _association, _evidence, references = (
                    self._association_references(store, binding)
                )
                if mode == "edge_without_head":
                    references["evidence"].delete()
                    references["binding_head"].delete()
                elif mode == "evidence_without_edge":
                    references["association"].delete()
                else:
                    references["binding_head"].delete()
                before = deepcopy(store.data)
                store.events.clear()

                with self.assertRaises(self.module.RowAuthorityAmbiguous):
                    self._associate(store)

                self.assertEqual(before, store.data)
                self.assertEqual([], self._write_events(store))

    def test_head_pointing_to_missing_candidate_edge_is_ambiguous(self):
        store = self._store()
        _rows, (binding, _reverse, _binding_ref, _reverse_refs) = (
            self._seed_prerequisites(store)
        )
        association, _evidence, references = self._association_references(
            store,
            binding,
        )
        orphaned_head = self.module.build_contact_row_binding_head_document(
            user_scope_hash=self.scope,
            canonical_mailbox_identity_hash=self.canonical_hash,
            state_revision=1,
            association_count=1,
            last_association_hash=association["contactRowEdgeHash"],
            created_at=self.association_at,
            updated_at=self.association_at,
        )
        references["binding_head"].create(orphaned_head)
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._associate(store)

        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))

    def test_malformed_drifted_or_noncorrelated_association_state_fails_closed(self):
        for mode, error in (
            ("malformed_edge", self.module.RowAuthorityConflict),
            ("malformed_evidence", self.module.RowAuthorityConflict),
            ("malformed_head", self.module.RowAuthorityAmbiguous),
            ("empty_head_with_edge", self.module.RowAuthorityAmbiguous),
            ("wrong_last_hash", self.module.RowAuthorityAmbiguous),
            ("head_created_after_edge", self.module.RowAuthorityAmbiguous),
            ("head_updated_before_edge", self.module.RowAuthorityAmbiguous),
            ("wrong_head_scope", self.module.RowAuthorityAmbiguous),
        ):
            with self.subTest(mode=mode):
                store = self._store()
                _rows, (binding, _reverse, _binding_ref, _reverse_refs) = (
                    self._seed_prerequisites(store)
                )
                created = self._associate(store)
                association, _evidence, references = (
                    self._association_references(store, binding)
                )
                if mode == "malformed_edge":
                    drift = deepcopy(store.data[references["association"].path])
                    drift["contactRowEdgeHash"] = "f" * 64
                    references["association"].set(drift, merge=False)
                elif mode == "malformed_evidence":
                    drift = deepcopy(store.data[references["evidence"].path])
                    drift["contactRowEvidenceHash"] = "f" * 64
                    references["evidence"].set(drift, merge=False)
                elif mode == "malformed_head":
                    drift = deepcopy(store.data[references["binding_head"].path])
                    drift["contactRowBindingHeadHash"] = "f" * 64
                    references["binding_head"].set(drift, merge=False)
                else:
                    values = {
                        "user_scope_hash": self.scope,
                        "canonical_mailbox_identity_hash": self.canonical_hash,
                        "state_revision": 1,
                        "association_count": 1,
                        "last_association_hash": association[
                            "contactRowEdgeHash"
                        ],
                        "created_at": created["bindingHead"]["createdAt"],
                        "updated_at": created["bindingHead"]["updatedAt"],
                    }
                    if mode == "empty_head_with_edge":
                        values.update(
                            association_count=0,
                            last_association_hash=None,
                        )
                    elif mode == "wrong_last_hash":
                        values["last_association_hash"] = "f" * 64
                    elif mode == "head_created_after_edge":
                        values.update(
                            created_at="2026-08-04T12:00:03.000000Z",
                            updated_at="2026-08-04T12:00:03.000000Z",
                        )
                    elif mode == "head_updated_before_edge":
                        values.update(
                            created_at=self.binding_at,
                            updated_at=self.binding_at,
                        )
                    else:
                        values["user_scope_hash"] = "f" * 64
                    drift = self.module.build_contact_row_binding_head_document(
                        **values
                    )
                    references["binding_head"].set(drift, merge=False)
                before = deepcopy(store.data)
                store.events.clear()

                with self.assertRaises(error):
                    self._associate(store)

                self.assertEqual(before, store.data)
                self.assertEqual([], self._write_events(store))

    def test_planner_uses_association_time_for_reachable_advanced_head(self):
        store = self._store()
        rows, (binding, reverse, _binding_ref, _reverse_refs) = (
            self._seed_prerequisites(store)
        )
        association = self.module.build_contact_row_binding_document(
            user_scope_hash=self.scope,
            canonical_mailbox_identity_hash=self.canonical_hash,
            row_id=self.first,
            created_at=self.association_at,
        )
        evidence = self.module.build_contact_row_binding_evidence_document(
            user_scope_hash=self.scope,
            edge_id=association["edgeId"],
            thread_id=self.thread_id,
            thread_binding_hash=binding["bindingHash"],
            exact_identity_hash=self.exact_hash,
            created_at="2026-08-04T12:00:03.000000Z",
        )
        prior_head = self.module.build_contact_row_binding_head_document(
            user_scope_hash=self.scope,
            canonical_mailbox_identity_hash=self.canonical_hash,
            state_revision=4,
            association_count=1,
            last_association_hash="f" * 64,
            created_at=self.binding_at,
            updated_at=self.binding_at,
        )
        planner_inputs = {
            "thread_binding_document": binding,
            "reverse_binding_document": reverse[0],
            "row_identity_document": rows[self.first][0],
            "row_head_document": rows[self.first][1],
            "proposed_association_document": association,
            "proposed_evidence_document": evidence,
            "stored_association_document": None,
            "stored_evidence_document": None,
            "contact_binding_head_document": prior_head,
        }

        created = self.module._plan_contact_row_association(**planner_inputs)

        self.assertEqual("created", created["disposition"])
        self.assertEqual(
            association["createdAt"],
            created["bindingHead"]["updatedAt"],
        )
        replay = self.module._plan_contact_row_association(
            **{
                **planner_inputs,
                "stored_association_document": association,
                "stored_evidence_document": evidence,
                "contact_binding_head_document": created["bindingHead"],
            }
        )
        self.assertEqual("already_applied", replay["disposition"])

    def test_private_association_planner_never_opens_or_commits_a_transaction(self):
        self.assertTrue(
            hasattr(self.module, "_plan_contact_row_association"),
            "private contact association planner is missing",
        )
        store = self._store()
        rows, (binding, reverse, _binding_ref, _reverse_refs) = (
            self._seed_prerequisites(store)
        )
        association, evidence = self._association_documents(binding)
        planner = self.module._plan_contact_row_association
        signature_names = set(inspect.signature(planner).parameters)
        for forbidden_name in (
            "store",
            "firestore",
            "transaction",
            "executor",
            "sentinel",
        ):
            self.assertNotIn(forbidden_name, signature_names)
        source = inspect.getsource(planner)
        for forbidden_text in (
            "_firestore",
            ".transaction(",
            ".commit(",
            "contactOptOutHeads",
        ):
            self.assertNotIn(forbidden_text, source)
        store.events.clear()

        with patch.object(
            type(store),
            "transaction",
            side_effect=AssertionError("planner opened a transaction"),
        ):
            plan = planner(
                thread_binding_document=binding,
                reverse_binding_document=reverse[0],
                row_identity_document=rows[self.first][0],
                row_head_document=rows[self.first][1],
                proposed_association_document=association,
                proposed_evidence_document=evidence,
                stored_association_document=None,
                stored_evidence_document=None,
                contact_binding_head_document=None,
            )

        self.assertEqual("created", plan["disposition"])
        self.assertEqual(3, len(plan["mutations"]))
        self.assertEqual([], store.events)

    def test_association_and_planner_results_are_defensive(self):
        store = self._store()
        rows, (binding, reverse, _binding_ref, _reverse_refs) = (
            self._seed_prerequisites(store)
        )
        association, evidence = self._association_documents(binding)
        planner_inputs = {
            "thread_binding_document": binding,
            "reverse_binding_document": reverse[0],
            "row_identity_document": rows[self.first][0],
            "row_head_document": rows[self.first][1],
            "proposed_association_document": association,
            "proposed_evidence_document": evidence,
            "stored_association_document": None,
            "stored_evidence_document": None,
            "contact_binding_head_document": None,
        }
        original_inputs = deepcopy(planner_inputs)
        plan = self.module._plan_contact_row_association(**planner_inputs)
        plan["association"]["rowId"] = self.second
        plan["evidence"]["threadId"] = "mutated"
        plan["bindingHead"]["associationCount"] = 999
        plan["mutations"][0]["document"]["rowId"] = self.second
        self.assertEqual(original_inputs, planner_inputs)

        created = self._associate(store)
        stored = deepcopy(store.data)
        created["association"]["rowId"] = self.second
        created["evidence"]["threadId"] = "mutated"
        created["bindingHead"]["associationCount"] = 999
        self.assertEqual(stored, store.data)
        replay = self._associate(store)
        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(1, replay["bindingHead"]["associationCount"])

    def test_association_preapply_failure_is_retryable(self):
        store = self._store()
        self._seed_prerequisites(store)
        before = deepcopy(store.data)
        store.events.clear()
        store.fail_next_commit = RuntimeError("preapply failure")

        with self.assertRaises(self.module.RowAuthorityRetryable) as caught:
            self._associate(store)

        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))
        self.assertIn(("commit_failed_before_apply",), store.events)

    def test_association_transactional_first_or_mid_read_failure_is_retryable_without_readback(self):
        fake_module = importlib.import_module("tests.source_coordinator_fakes")
        original_get = fake_module.FakeDocumentReference.get
        for fail_at in (1, 5):
            with self.subTest(fail_at=fail_at):
                store = self._store()
                self._seed_prerequisites(store)
                before = deepcopy(store.data)
                store.events.clear()
                counts = {"transactional": 0, "nontransactional": 0}

                def fail_selected_read(reference, *, transaction=None):
                    if transaction is None:
                        counts["nontransactional"] += 1
                    else:
                        counts["transactional"] += 1
                        if counts["transactional"] == fail_at:
                            raise RuntimeError(
                                f"transaction read {fail_at} failed"
                            )
                    return original_get(reference, transaction=transaction)

                with patch.object(
                    fake_module.FakeDocumentReference,
                    "get",
                    new=fail_selected_read,
                ), self.assertRaises(
                    self.module.RowAuthorityRetryable
                ) as caught:
                    self._associate(store)

                self.assertIsInstance(caught.exception.__cause__, RuntimeError)
                self.assertEqual(fail_at, counts["transactional"])
                self.assertEqual(0, counts["nontransactional"])
                self.assertEqual(before, store.data)
                self.assertEqual([], self._write_events(store))
                self.assertIn(("transaction_rolled_back",), store.events)

    def test_association_apply_then_raise_accepts_only_exact_disposition_after_image(self):
        cases = (
            ("created", None, None, "created"),
            (
                "evidence_created",
                "e" * 64,
                "2026-08-04T12:00:03.000000Z",
                "evidence_created",
            ),
            ("already_applied", self.exact_hash, self.association_at, "already_applied"),
        )
        for mode, exact_hash, event_time, expected in cases:
            with self.subTest(mode=mode):
                store = self._store()
                self._seed_prerequisites(store)
                if mode != "created":
                    self._associate(store)
                store.events.clear()
                store.apply_then_raise_next_commit = RuntimeError(
                    f"{mode} raised after apply"
                )
                overrides = {}
                if exact_hash is not None:
                    overrides["exact_identity_hash"] = exact_hash
                if event_time is not None:
                    overrides["created_at"] = event_time

                result = self._associate(store, **overrides)

                self.assertEqual(expected, result["disposition"])
                self.assertIn(("commit_raised_after_apply",), store.events)

        strict_store = self._store()
        _rows, (binding, _reverse, _binding_ref, _reverse_refs) = (
            self._seed_prerequisites(strict_store)
        )
        self._associate(strict_store)
        _association, _evidence, references = self._association_references(
            strict_store,
            binding,
            exact_identity_hash="e" * 64,
            created_at="2026-08-04T12:00:03.000000Z",
        )

        def apply_then_advance_head(transaction, callback):
            transaction._begin()
            callback(transaction)
            transaction._commit()
            advanced = self.module.build_contact_row_binding_head_document(
                user_scope_hash=self.scope,
                canonical_mailbox_identity_hash=self.canonical_hash,
                state_revision=2,
                association_count=2,
                last_association_hash="f" * 64,
                created_at=self.association_at,
                updated_at="2026-08-04T12:00:04.000000Z",
            )
            references["binding_head"].set(advanced, merge=False)
            raise RuntimeError("head advanced after association apply")

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._associate(
                strict_store,
                executor=apply_then_advance_head,
                exact_identity_hash="e" * 64,
                created_at="2026-08-04T12:00:03.000000Z",
            )

    def test_association_partial_readback_is_ambiguous(self):
        def partially_apply_first_mutation(transaction, callback):
            transaction._begin()
            callback(transaction)
            operation, reference, payload, merge = transaction._operations[0]
            transaction._rollback()
            self.assertEqual(("create", False), (operation, merge))
            reference.create(payload)
            raise RuntimeError("only the first mutation applied")

        partial_store = self._store()
        self._seed_prerequisites(partial_store)
        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._associate(
                partial_store,
                executor=partially_apply_first_mutation,
            )

        malformed_store = self._store()
        _rows, (binding, _reverse, _binding_ref, _reverse_refs) = (
            self._seed_prerequisites(malformed_store)
        )
        _association, _evidence, references = self._association_references(
            malformed_store,
            binding,
        )

        def apply_then_malform_evidence(transaction, callback):
            transaction._begin()
            callback(transaction)
            transaction._commit()
            malformed = deepcopy(malformed_store.data[references["evidence"].path])
            malformed["contactRowEvidenceHash"] = "f" * 64
            malformed_store.data[references["evidence"].path] = malformed
            raise RuntimeError("after-image evidence drifted")

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._associate(
                malformed_store,
                executor=apply_then_malform_evidence,
            )

        unreadable_store = self._store()
        self._seed_prerequisites(unreadable_store)
        fake_module = importlib.import_module("tests.source_coordinator_fakes")
        original_get = fake_module.FakeDocumentReference.get

        def fail_nontransaction_readback(reference, *, transaction=None):
            if transaction is None:
                raise RuntimeError("readback unavailable")
            return original_get(reference, transaction=transaction)

        unreadable_store.apply_then_raise_next_commit = RuntimeError(
            "applied then raised"
        )
        with patch.object(
            fake_module.FakeDocumentReference,
            "get",
            new=fail_nontransaction_readback,
        ), self.assertRaises(self.module.RowAuthorityAmbiguous) as caught:
            self._associate(unreadable_store)
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)


class RowClaimStoreTests(unittest.TestCase):
    @staticmethod
    def _fakes():
        return importlib.import_module("tests.row_authority_fakes")

    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")

    def setUp(self):
        self.user_id = "uid-claim-1"
        self.scope = self.module.user_scope_hash(self.user_id)
        self.first = _row_id(1)
        self.second = _row_id(2)
        self.row_created_at = "2026-08-04T12:00:00.000000Z"
        self.binding_at = "2026-08-04T12:00:01.000000Z"
        self.claimed_at = "2026-08-04T12:00:02.000000Z"
        self.lease_until = "2026-08-04T12:05:00.000000Z"
        self.lease_owner_hash = "a" * 64

    def _store(self):
        return self._fakes().BoundedFakeFirestore()

    def _authority(self, store, *, executor=None):
        return self.module.RowAuthorityStore(
            store,
            transaction_executor=(
                executor or self._fakes().run_bounded_transaction
            ),
        )

    def _user_reference(self, store):
        return store.collection("users").document(self.user_id)

    def _row_references(self, store, row_id):
        user = self._user_reference(store)
        return (
            user.collection("rowIdentities").document(row_id),
            user.collection("rowAuthorityHeads").document(row_id),
        )

    def _rehash_head(self, head):
        material = {
            key: value
            for key, value in head.items()
            if key not in {"schemaVersion", "userScopeHash", "headHash"}
        }
        head["headHash"] = self.module.domain_hash(
            self.module.ROW_AUTHORITY_HEAD_HASH_DOMAIN,
            material,
            user_scope_hash=head["userScopeHash"],
        )
        return self.module.validate_row_authority_head(document=head)

    def _rehash_claim(self, claim):
        claim["claimSetHash"] = self.module.domain_hash(
            self.module.CLAIM_SET_HASH_DOMAIN,
            self.module._claim_set_hash_payload(claim),
            user_scope_hash=claim["userScopeHash"],
        )
        return self.module.validate_claim_set_document(document=claim)

    def _rehash_generation(self, generation):
        generation["generationHash"] = self.module.domain_hash(
            self.module.OWNER_GENERATION_HASH_DOMAIN,
            self.module._generation_hash_payload(generation),
            user_scope_hash=generation["userScopeHash"],
        )
        return self.module.validate_owner_generation_document(
            document=generation
        )

    def _rehash_settlement(self, settlement):
        settlement["settlementHash"] = self.module.domain_hash(
            self.module.OWNER_SETTLEMENT_HASH_DOMAIN,
            self.module._settlement_hash_payload(settlement),
            user_scope_hash=settlement["userScopeHash"],
        )
        return self.module.validate_owner_settlement_document(
            document=settlement
        )

    def _seed_row(self, store, row_id, *, lifecycle="active"):
        index = 0 if row_id == self.first else 1
        identity = self.module.build_row_identity_document(
            user_scope_hash=self.scope,
            row_id=row_id,
            client_id="client-1",
            spreadsheet_id="spreadsheet-1",
            sheet_id=0,
            creation_kind="fresh",
            creation_source_hash="1" * 64,
            created_at=self.row_created_at,
        )
        observation = self.module.build_row_observation(
            spreadsheet_id="spreadsheet-1",
            marker_observation={
                "rowId": row_id,
                "sheetId": 0,
                "providerRowIndex": index,
                "displayRowNumber": index + 1,
                "metadataId": index + 1,
            },
            ordered_headers=("Email",),
            ordered_cell_values=(f"row-{index}@example.test",),
            user_scope_hash=self.scope,
        )
        revision = self.module.build_row_location_revision_document(
            identity_document=identity,
            revision=1,
            lifecycle="active",
            observations=(observation,),
            previous_revision_hash=None,
            observed_at=self.row_created_at,
        )
        head = self.module.build_initial_row_authority_head(
            identity_document=identity,
            location_revision_document=revision,
            created_at=self.row_created_at,
        )
        if lifecycle != "active":
            head.update(
                {
                    "stateRevision": 2,
                    "currentLocationRevision": 2,
                    "currentLocationHash": "2" * 64,
                    "currentLocationLifecycle": lifecycle,
                    "updatedAt": self.binding_at,
                }
            )
            head = self._rehash_head(head)
        identity_ref, head_ref = self._row_references(store, row_id)
        identity_ref.create(identity)
        head_ref.create(head)
        return identity, head

    def _seed_thread_binding(self, store, row_ids):
        binding = self.module.build_thread_row_binding_document(
            user_scope_hash=self.scope,
            thread_id="thread-1",
            client_id="client-1",
            row_ids=row_ids,
            primary_row_id=row_ids[0],
            created_at=self.binding_at,
        )
        user = self._user_reference(store)
        user.collection("threadRowBindings").document("thread-1").create(
            binding
        )
        for edge in self.module.build_row_thread_binding_documents(
            thread_binding_document=binding
        ):
            user.collection("rowThreadBindings").document(
                edge["edgeId"]
            ).create(edge)
        return binding

    def _seed_b1_bundle(
        self,
        store,
        *,
        owner_kind="terminal",
        source_id="source-1",
    ):
        bundle = RowOwnershipContractTests._b1_bundle(
            self,
            owner_kind=owner_kind,
            source_id=source_id,
        )
        user = self._user_reference(store)
        source_id = bundle["identity"]["canonicalSourceId"]
        for collection, key in (
            ("sourceIdentities", "identity"),
            ("sourceClassifications", "classification"),
            ("sourceTransitionOwners", "owner"),
            ("sourceWorkLedgers", "ledger"),
        ):
            user.collection(collection).document(source_id).create(
                bundle[key]
            )
        return bundle

    def _seed_prerequisites(
        self,
        store,
        *,
        owner_kind="terminal",
        row_ids=None,
    ):
        rows = list(row_ids or [self.first])
        for row_id in rows:
            self._seed_row(store, row_id)
        binding = self._seed_thread_binding(store, rows)
        bundle = self._seed_b1_bundle(store, owner_kind=owner_kind)
        return bundle, binding

    def _claim(self, store, *, executor=None, bundle=None, **overrides):
        if bundle is None:
            user = self._user_reference(store)
            identity = store.data[
                user.collection("sourceIdentities")
                .document("source-1")
                .path
            ]
            work_key = store.data[
                user.collection("sourceWorkLedgers")
                .document("source-1")
                .path
            ]["entries"][0]["workKey"]
            source_id = identity["canonicalSourceId"]
        else:
            source_id = bundle["identity"]["canonicalSourceId"]
            work_key = bundle["work_key"]
        arguments = {
            "verified_user_id": self.user_id,
            "canonical_source_id": source_id,
            "work_key": work_key,
            "created_at": self.claimed_at,
            "lease_owner_hash": self.lease_owner_hash,
            "lease_until": self.lease_until,
        }
        arguments.update(overrides)
        return self._authority(store, executor=executor).claim_row_set(
            **arguments
        )

    def _install_contact_owner(self, store, row_id):
        identity_ref, head_ref = self._row_references(store, row_id)
        identity = store.data[identity_ref.path]
        predecessor = store.data[head_ref.path]
        bundle = RowOwnershipContractTests._b1_bundle(
            self,
            owner_kind="contact_optout",
        )
        link = self.module.build_b1_authority_link(
            user_scope_hash=self.scope,
            source_identity_document=bundle["identity"],
            source_classification_document=bundle["classification"],
            source_owner_document=bundle["owner"],
            source_ledger_document=bundle["ledger"],
            work_key=bundle["work_key"],
        )
        claim = self.module.build_claim_set_document(
            user_scope_hash=self.scope,
            authority_origin="contact_fanout",
            authority_link=link,
            operator_action_document=None,
            fanout_id="b" * 64,
            row_ids=[row_id],
            primary_row_id=row_id,
            planned_writes=3,
            outcome="accepted",
            row_decisions=[
                {
                    "rowId": row_id,
                    "decision": "accepted",
                    "plannedGeneration": 1,
                    "winnerGenerationHash": None,
                    "winnerSettlementHash": None,
                }
            ],
            created_at=self.claimed_at,
            canonical_mailbox_identity_hash="c" * 64,
            contact_settlement_hash="d" * 64,
        )
        generation = self.module.build_owner_generation_document(
            claim_set_document=claim,
            row_id=row_id,
            generation=1,
            predecessor_head_hash=predecessor["headHash"],
            predecessor_settlement_hash=None,
            lease_epoch=1,
            first_fencing_token=1,
            created_at=self.claimed_at,
        )
        head = self.module._build_claim_advanced_head(
            expected_head=predecessor,
            generation_document=generation,
            lease_owner_hash="e" * 64,
            lease_until=self.lease_until,
            dominated_predecessor_settlement_hash=None,
            claimed_at=self.claimed_at,
        )
        user = self._user_reference(store)
        user.collection("rowClaimSets").document(claim["requestId"]).create(
            claim
        )
        user.collection("rowOwnerGenerations").document(
            f"{row_id}--1"
        ).create(generation)
        head_ref.set(head, merge=False)
        self.assertEqual(identity["rowId"], head["rowId"])
        return claim, generation, head

    @staticmethod
    def _write_events(store):
        return [
            event
            for event in store.events
            if event[0] in {"create", "set", "update", "delete"}
        ]

    def _claim_reference(self, store, request_id):
        return self._user_reference(store).collection("rowClaimSets").document(
            request_id
        )

    def _generation_reference(self, store, row_id, generation):
        return self._user_reference(store).collection(
            "rowOwnerGenerations"
        ).document(f"{row_id}--{generation}")

    def _settlement_reference(self, store, row_id, generation):
        return self._user_reference(store).collection(
            "rowOwnerSettlements"
        ).document(f"{row_id}--{generation}")

    def _install_owner(self, store, row_id, *, owner_kind):
        _identity_ref, head_ref = self._row_references(store, row_id)
        predecessor = deepcopy(store.data[head_ref.path])
        source_id = f"historical-{owner_kind.replace('_', '-')}"
        bundle = RowOwnershipContractTests._b1_bundle(
            self,
            owner_kind=owner_kind,
            source_id=source_id,
        )
        link = self.module.build_b1_authority_link(
            user_scope_hash=self.scope,
            source_identity_document=bundle["identity"],
            source_classification_document=bundle["classification"],
            source_owner_document=bundle["owner"],
            source_ledger_document=bundle["ledger"],
            work_key=bundle["work_key"],
        )
        claim = self.module.build_claim_set_document(
            user_scope_hash=self.scope,
            authority_origin="b1_source",
            authority_link=link,
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
                    "plannedGeneration": 1,
                    "winnerGenerationHash": None,
                    "winnerSettlementHash": None,
                }
            ],
            created_at=self.claimed_at,
        )
        generation = self.module.build_owner_generation_document(
            claim_set_document=claim,
            row_id=row_id,
            generation=1,
            predecessor_head_hash=predecessor["headHash"],
            predecessor_settlement_hash=None,
            lease_epoch=1,
            first_fencing_token=1,
            created_at=self.claimed_at,
        )
        head = self.module._build_claim_advanced_head(
            expected_head=predecessor,
            generation_document=generation,
            lease_owner_hash="f" * 64,
            lease_until=self.lease_until,
            dominated_predecessor_settlement_hash=None,
            claimed_at=self.claimed_at,
        )
        self._claim_reference(store, claim["requestId"]).create(claim)
        self._generation_reference(store, row_id, 1).create(generation)
        head_ref.set(head, merge=False)
        return claim, generation, head

    def _current_row_state(self, store, row_id):
        identity_ref, head_ref = self._row_references(store, row_id)
        identity = deepcopy(store.data[identity_ref.path])
        head = deepcopy(store.data[head_ref.path])
        generation = None
        claim = None
        settlement = None
        predecessor_generation = None
        predecessor_claim = None
        current_predecessor_settlement = None
        owner_lineage = []
        if head["effectiveOwnerGeneration"] is not None:
            number = head["effectiveOwnerGeneration"]
            for lineage_number in range(1, number + 1):
                lineage_generation = deepcopy(
                    store.data.get(
                        self._generation_reference(
                            store,
                            row_id,
                            lineage_number,
                        ).path
                    )
                )
                lineage_claim = (
                    deepcopy(
                        store.data.get(
                            self._claim_reference(
                                store,
                                lineage_generation["requestId"],
                            ).path
                        )
                    )
                    if lineage_generation is not None
                    else None
                )
                lineage_settlement = deepcopy(
                    store.data.get(
                        self._settlement_reference(
                            store,
                            row_id,
                            lineage_number,
                        ).path
                    )
                )
                owner_lineage.append(
                    {
                        "generation": lineage_generation,
                        "claimSet": lineage_claim,
                        "settlement": lineage_settlement,
                    }
                )
            generation = owner_lineage[-1]["generation"]
            claim = owner_lineage[-1]["claimSet"]
            settlement = owner_lineage[-1]["settlement"]
            if number > 1:
                predecessor_generation = owner_lineage[-2]["generation"]
                predecessor_claim = owner_lineage[-2]["claimSet"]
                current_predecessor_settlement = owner_lineage[-2][
                    "settlement"
                ]
        return {
            "rowId": row_id,
            "identity": identity,
            "head": head,
            "currentGeneration": generation,
            "currentClaimSet": claim,
            "currentSettlement": settlement,
            "candidateGeneration": None,
            "candidateSettlement": None,
            "predecessorSettlement": settlement,
            "currentPredecessorGeneration": predecessor_generation,
            "currentPredecessorClaimSet": predecessor_claim,
            "currentPredecessorSettlement": current_predecessor_settlement,
            "ownerLineage": owner_lineage,
        }

    def _contact_plan(
        self,
        store,
        *,
        created_at="2026-08-04T12:00:03.000000Z",
        lease_until="2026-08-04T12:06:00.000000Z",
    ):
        bundle = RowOwnershipContractTests._b1_bundle(
            self,
            owner_kind="contact_optout",
            contact_evidence_version=2,
            source_id="private-contact-authority",
        )
        link = self.module.build_b1_authority_link(
            user_scope_hash=self.scope,
            source_identity_document=bundle["identity"],
            source_classification_document=bundle["classification"],
            source_owner_document=bundle["owner"],
            source_ledger_document=bundle["ledger"],
            work_key=bundle["work_key"],
        )
        return self.module._plan_contact_fanout_row_claim(
            user_scope_hash=self.scope,
            authority_link=link,
            fanout_id="8" * 64,
            canonical_mailbox_identity_hash=link[
                "canonicalMailboxIdentityHash"
            ],
            contact_settlement_hash="7" * 64,
            thread_binding_document=None,
            canonical_row_id=self.first,
            row_states=[self._current_row_state(store, self.first)],
            stored_claim_set_document=None,
            created_at=created_at,
            lease_owner_hash="6" * 64,
            lease_until=lease_until,
        )

    def _settle_terminal_owner(
        self,
        store,
        claim,
        generation,
        head,
        *,
        settled_at="2026-08-04T12:00:03.000000Z",
    ):
        settlement = self.module.build_owner_settlement_document(
            generation_document=generation,
            claim_set_document=claim,
            fencing_token=head["fencingToken"],
            outcome="terminal",
            settled_at=settled_at,
        )
        settled_head = self.module._build_settlement_advanced_head(
            expected_head=head,
            generation_document=generation,
            settlement_document=settlement,
        )
        self._settlement_reference(
            store,
            generation["rowId"],
            generation["generation"],
        ).create(settlement)
        self._row_references(store, generation["rowId"])[1].set(
            settled_head,
            merge=False,
        )
        return settlement, settled_head

    def _install_settled_human_owner(self, store, row_id):
        binding_ref = self._user_reference(store).collection(
            "threadRowBindings"
        ).document("thread-1")
        binding = deepcopy(store.data[binding_ref.path])
        self.assertEqual([row_id], [
            item["rowId"] for item in binding["rowBindings"]
        ])
        result = self._authority(store).record_operator_decline(
            verified_user_id=self.user_id,
            thread_id=binding["threadId"],
            actor_scope_hash="5" * 64,
            client_request_id="settled-human-request",
            issued_at="2026-08-04T12:00:03.000000Z",
        )
        self.assertEqual("declined", result["disposition"])
        return (
            result["action"],
            result["claimSet"],
            result["generations"][0],
            result["settlements"][0],
            result["heads"][0],
        )

    def test_claim_row_set_has_exact_public_surface_and_non_b1_public_claims_do_not_exist(self):
        method = getattr(self.module.RowAuthorityStore, "claim_row_set", None)
        self.assertIsNotNone(method, "RowAuthorityStore.claim_row_set is missing")
        self.assertEqual(
            [
                "self",
                "verified_user_id",
                "canonical_source_id",
                "work_key",
                "created_at",
                "lease_owner_hash",
                "lease_until",
            ],
            list(inspect.signature(method).parameters),
        )
        for forbidden in (
            "claim_operator_row_set",
            "claim_contact_fanout_row_set",
            "claim_authenticated_operator",
            "claim_contact_fanout",
        ):
            self.assertFalse(hasattr(self.module.RowAuthorityStore, forbidden))

    def test_b1_claim_derives_bundle_binding_and_creates_first_terminal_claim_atomically(self):
        store = self._store()
        bundle, binding = self._seed_prerequisites(store)
        store.events.clear()

        result = self._claim(store, bundle=bundle)

        self.assertEqual(
            {
                "disposition",
                "claimSet",
                "generations",
                "heads",
                "predecessorSettlements",
            },
            set(result),
        )
        self.assertEqual("created", result["disposition"])
        self.assertEqual("b1_source", result["claimSet"]["authorityOrigin"])
        self.assertEqual(binding["rowBindingsHash"], result["claimSet"]["rowBindingsHash"])
        self.assertEqual(1, result["generations"][0]["generation"])
        self.assertEqual(1, result["generations"][0]["firstFencingToken"])
        self.assertEqual("claimed", result["heads"][0]["state"])
        self.assertEqual([], result["predecessorSettlements"])
        self.assertIn(("commit_applied", 3), store.events)
        writes = self._write_events(store)
        self.assertEqual(["create", "create", "set"], [event[0] for event in writes])
        reads = [event[1] for event in store.events if event[0] == "get"]
        self.assertEqual(
            [
                "users/uid-claim-1/sourceIdentities/source-1",
                "users/uid-claim-1/sourceClassifications/source-1",
                "users/uid-claim-1/sourceTransitionOwners/source-1",
                "users/uid-claim-1/sourceWorkLedgers/source-1",
                "users/uid-claim-1/threadRowBindings/thread-1",
            ],
            reads[:5],
        )

    def test_public_b1_human_is_review_pending_and_contact_optout_is_zero_write_blocked(self):
        human_store = self._store()
        human_bundle, _binding = self._seed_prerequisites(
            human_store,
            owner_kind="human_decision",
        )
        human_store.events.clear()
        human = self._claim(human_store, bundle=human_bundle)
        self.assertEqual("review_pending", human["heads"][0]["state"])
        self.assertEqual("human_decision", human["claimSet"]["ownerKind"])

        optout_store = self._store()
        optout_bundle, _binding = self._seed_prerequisites(
            optout_store,
            owner_kind="contact_optout",
        )
        before = deepcopy(optout_store.data)
        optout_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(optout_store, bundle=optout_bundle)
        self.assertEqual(before, optout_store.data)
        self.assertEqual([], self._write_events(optout_store))

    def test_multirow_dominated_marks_peer_blocked_and_advances_nothing(self):
        store = self._store()
        bundle, _binding = self._seed_prerequisites(
            store,
            row_ids=[self.first, self.second],
        )
        self._install_contact_owner(store, self.first)
        heads_before = {
            row_id: deepcopy(store.data[self._row_references(store, row_id)[1].path])
            for row_id in (self.first, self.second)
        }
        generations_before = {
            path: deepcopy(payload)
            for path, payload in store.data.items()
            if "/rowOwnerGenerations/" in path
        }
        store.events.clear()

        result = self._claim(store, bundle=bundle)

        self.assertEqual("dominated", result["disposition"])
        self.assertEqual("dominated", result["claimSet"]["outcome"])
        self.assertEqual(
            ["dominated", "blocked_by_claim_set"],
            [item["decision"] for item in result["claimSet"]["rowDecisions"]],
        )
        self.assertEqual([], result["generations"])
        self.assertEqual([], result["predecessorSettlements"])
        self.assertEqual(
            heads_before,
            {
                row_id: store.data[self._row_references(store, row_id)[1].path]
                for row_id in (self.first, self.second)
            },
        )
        self.assertEqual(
            generations_before,
            {
                path: payload
                for path, payload in store.data.items()
                if "/rowOwnerGenerations/" in path
            },
        )
        self.assertEqual(1, len(self._write_events(store)))
        self.assertIn(("commit_applied", 1), store.events)

    def test_b1_claim_derives_link_from_exact_stored_source_bundle_and_thread_binding(self):
        store = self._store()
        bundle, binding = self._seed_prerequisites(store)
        expected_link = self.module.build_b1_authority_link(
            user_scope_hash=self.scope,
            source_identity_document=bundle["identity"],
            source_classification_document=bundle["classification"],
            source_owner_document=bundle["owner"],
            source_ledger_document=bundle["ledger"],
            work_key=bundle["work_key"],
        )
        result = self._claim(store, bundle=bundle)
        self.assertEqual(expected_link, result["claimSet"]["authorityLink"])
        self.assertEqual(
            binding["rowBindings"],
            result["claimSet"]["rowBindings"],
        )
        self.assertEqual(
            binding["primaryRowId"],
            result["claimSet"]["primaryRowId"],
        )

    def test_b1_claim_rejects_missing_malformed_hash_drifted_wrong_thread_or_wrong_work_bundle(self):
        cases = (
            "missing",
            "malformed",
            "hash_drift",
            "wrong_thread",
            "wrong_work",
        )
        for mode in cases:
            with self.subTest(mode=mode):
                store = self._store()
                bundle, _binding = self._seed_prerequisites(store)
                user = self._user_reference(store)
                classification_ref = user.collection(
                    "sourceClassifications"
                ).document("source-1")
                identity_ref = user.collection("sourceIdentities").document(
                    "source-1"
                )
                if mode == "missing":
                    classification_ref.delete()
                elif mode == "malformed":
                    store.data[classification_ref.path]["unexpected"] = True
                elif mode == "hash_drift":
                    store.data[classification_ref.path][
                        "snapshotImmutableHash"
                    ] = "f" * 64
                elif mode == "wrong_thread":
                    store.data[identity_ref.path]["threadId"] = "thread-2"
                before = deepcopy(store.data)
                store.events.clear()
                arguments = {}
                if mode == "wrong_work":
                    arguments["work_key"] = "f" * 64
                with self.assertRaises(self.module.RowAuthorityError):
                    self._claim(store, bundle=bundle, **arguments)
                self.assertEqual(before, store.data)
                self.assertEqual([], self._write_events(store))

    def test_unsafe_canonical_source_or_b1_thread_id_fails_before_reference_or_transaction(self):
        class NeverAccessFirestore:
            def __init__(self):
                self.collection_calls = 0
                self.transaction_calls = 0

            def collection(self, _name):
                self.collection_calls += 1
                raise AssertionError("document reference creation was reached")

            def transaction(self):
                self.transaction_calls += 1
                raise AssertionError("transaction creation was reached")

        never = NeverAccessFirestore()
        authority = self.module.RowAuthorityStore(
            never,
            transaction_executor=lambda _transaction, _callback: None,
        )
        with self.assertRaises(self.module.RowAuthorityConfigError):
            authority.claim_row_set(
                verified_user_id=self.user_id,
                canonical_source_id="../unsafe",
                work_key="a" * 64,
                created_at=self.claimed_at,
                lease_owner_hash=self.lease_owner_hash,
                lease_until=self.lease_until,
            )
        self.assertEqual(0, never.collection_calls)
        self.assertEqual(0, never.transaction_calls)

        store = self._store()
        bundle, _binding = self._seed_prerequisites(store)
        identity_ref = self._user_reference(store).collection(
            "sourceIdentities"
        ).document("source-1")
        store.data[identity_ref.path]["threadId"] = "unsafe/thread"
        store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(store, bundle=bundle)
        accessed = "\n".join(
            event[1] for event in store.events if event[0] == "get"
        )
        self.assertNotIn("threadRowBindings", accessed)
        self.assertEqual([], self._write_events(store))

    def test_well_formed_forged_terminal_or_model_optout_link_cannot_enter_public_claim(self):
        store = self._store()
        bundle, _binding = self._seed_prerequisites(store)
        forged_link = self.module.build_b1_authority_link(
            user_scope_hash=self.scope,
            source_identity_document=bundle["identity"],
            source_classification_document=bundle["classification"],
            source_owner_document=bundle["owner"],
            source_ledger_document=bundle["ledger"],
            work_key=bundle["work_key"],
        )
        with self.assertRaises(TypeError):
            self._authority(store).claim_row_set(
                verified_user_id=self.user_id,
                canonical_source_id="source-1",
                work_key=bundle["work_key"],
                created_at=self.claimed_at,
                lease_owner_hash=self.lease_owner_hash,
                lease_until=self.lease_until,
                authority_link=forged_link,
            )

        model_store = self._store()
        self._seed_row(model_store, self.first)
        self._seed_thread_binding(model_store, [self.first])
        model_bundle = RowOwnershipContractTests._b1_bundle(
            self,
            owner_kind="human_decision",
            model_contact_optout=True,
        )
        user = self._user_reference(model_store)
        for collection, key in (
            ("sourceIdentities", "identity"),
            ("sourceClassifications", "classification"),
            ("sourceTransitionOwners", "owner"),
            ("sourceWorkLedgers", "ledger"),
        ):
            user.collection(collection).document("source-1").create(
                model_bundle[key]
            )
        result = self._claim(model_store, bundle=model_bundle)
        self.assertEqual("human_decision", result["claimSet"]["ownerKind"])
        self.assertEqual(1, result["claimSet"]["derivedPriority"])
        self.assertEqual("review_pending", result["heads"][0]["state"])

    def test_public_b1_contact_optout_claim_is_blocked_until_b2c(self):
        store = self._store()
        bundle, _binding = self._seed_prerequisites(
            store,
            owner_kind="contact_optout",
        )
        before = deepcopy(store.data)
        store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(store, bundle=bundle)
        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))

    def test_public_authenticated_operator_and_contact_fanout_claims_do_not_exist(self):
        public_names = set(vars(self.module.RowAuthorityStore))
        self.assertNotIn("claim_authenticated_operator", public_names)
        self.assertNotIn("claim_operator_row_set", public_names)
        self.assertNotIn("claim_contact_fanout", public_names)
        self.assertNotIn("claim_contact_fanout_row_set", public_names)
        signature = inspect.signature(self.module.RowAuthorityStore.claim_row_set)
        for forbidden in (
            "authority_origin",
            "authority_link",
            "operator_action_document",
            "fanout_id",
            "priority",
            "planned_writes",
            "row_ids",
            "thread_id",
        ):
            self.assertNotIn(forbidden, signature.parameters)

    def test_private_operator_and_contact_planners_never_open_nested_transactions(self):
        for name in (
            "_plan_operator_row_claim",
            "_plan_contact_fanout_row_claim",
        ):
            planner = getattr(self.module, name)
            source = inspect.getsource(planner)
            self.assertNotIn(".transaction(", source)
            self.assertNotIn("_transaction_executor", source)
        store = self._store()
        self._seed_row(store, self.first)
        binding = self._seed_thread_binding(store, [self.first])
        store.events.clear()
        plan = self._contact_plan(store)
        self.assertEqual("created", plan["disposition"])
        self.assertEqual("contact_fanout", plan["claimSet"]["authorityOrigin"])
        self.assertEqual([], store.events)

    def test_first_claim_creates_claim_set_generation_and_head_per_row(self):
        store = self._store()
        bundle, _binding = self._seed_prerequisites(
            store,
            row_ids=[self.first, self.second],
        )
        store.events.clear()
        result = self._claim(store, bundle=bundle)
        self.assertEqual(5, result["claimSet"]["plannedWrites"])
        self.assertEqual(2, len(result["generations"]))
        self.assertEqual(2, len(result["heads"]))
        self.assertEqual([1, 1], [item["generation"] for item in result["generations"]])
        self.assertEqual([1, 1], [item["fencingToken"] for item in result["heads"]])
        self.assertIn(("commit_applied", 5), store.events)

    def test_human_claim_enters_review_pending_without_settlement(self):
        store = self._store()
        bundle, _binding = self._seed_prerequisites(
            store,
            owner_kind="human_decision",
        )
        result = self._claim(store, bundle=bundle)
        self.assertEqual("review_pending", result["heads"][0]["state"])
        self.assertEqual([], result["predecessorSettlements"])
        self.assertFalse(
            any("/rowOwnerSettlements/" in path for path in store.data)
        )

    def test_terminal_claim_enters_claimed(self):
        store = self._store()
        bundle, _binding = self._seed_prerequisites(store)
        result = self._claim(store, bundle=bundle)
        self.assertEqual("terminal", result["claimSet"]["ownerKind"])
        self.assertEqual(2, result["claimSet"]["derivedPriority"])
        self.assertEqual("claimed", result["heads"][0]["state"])

    def test_active_and_nonviable_are_claimable_deleted_and_ambiguous_are_not(self):
        for lifecycle, claimable in (
            ("active", True),
            ("nonviable", True),
            ("deleted", False),
            ("ambiguous", False),
        ):
            with self.subTest(lifecycle=lifecycle):
                store = self._store()
                self._seed_row(store, self.first, lifecycle=lifecycle)
                self._seed_thread_binding(store, [self.first])
                bundle = self._seed_b1_bundle(store)
                before = deepcopy(store.data)
                store.events.clear()
                if claimable:
                    result = self._claim(store, bundle=bundle)
                    self.assertEqual("created", result["disposition"])
                else:
                    with self.assertRaises(self.module.RowAuthorityConflict):
                        self._claim(store, bundle=bundle)
                    self.assertEqual(before, store.data)
                    self.assertEqual([], self._write_events(store))

    def test_claim_reads_b1_bundle_then_binding_then_derived_claim_set_then_rows_before_writes(self):
        store = self._store()
        bundle, _binding = self._seed_prerequisites(
            store,
            row_ids=[self.second, self.first],
        )
        store.events.clear()
        result = self._claim(store, bundle=bundle)
        reads = [event[1] for event in store.events if event[0] == "get"]
        self.assertEqual(
            [
                "users/uid-claim-1/sourceIdentities/source-1",
                "users/uid-claim-1/sourceClassifications/source-1",
                "users/uid-claim-1/sourceTransitionOwners/source-1",
                "users/uid-claim-1/sourceWorkLedgers/source-1",
                "users/uid-claim-1/threadRowBindings/thread-1",
                self._claim_reference(store, result["claimSet"]["requestId"]).path,
                self._row_references(store, self.first)[0].path,
                self._row_references(store, self.first)[1].path,
                self._row_references(store, self.second)[0].path,
                self._row_references(store, self.second)[1].path,
            ],
            reads[:10],
        )
        read_indexes = [
            index for index, event in enumerate(store.events) if event[0] == "get"
        ]
        write_indexes = [
            index
            for index, event in enumerate(store.events)
            if event[0] in {"create", "set", "update", "delete"}
        ]
        self.assertLess(max(read_indexes), min(write_indexes))

    def test_claim_rejects_event_time_before_binding_identity_or_current_head(self):
        binding_store = self._store()
        bundle, _binding = self._seed_prerequisites(binding_store)
        before = deepcopy(binding_store.data)
        binding_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(
                binding_store,
                bundle=bundle,
                created_at=self.row_created_at,
            )
        self.assertEqual(before, binding_store.data)

        head_store = self._store()
        bundle, _binding = self._seed_prerequisites(head_store)
        _identity_ref, head_ref = self._row_references(head_store, self.first)
        head = deepcopy(head_store.data[head_ref.path])
        head.update(
            {
                "stateRevision": 2,
                "currentLocationRevision": 2,
                "currentLocationHash": "3" * 64,
                "updatedAt": "2026-08-04T12:00:03.000000Z",
            }
        )
        head_ref.set(self._rehash_head(head), merge=False)
        before = deepcopy(head_store.data)
        head_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(head_store, bundle=bundle)
        self.assertEqual(before, head_store.data)

    def test_claim_time_must_follow_immutable_b1_identity_snapshot_owner_and_ledger_readiness(self):
        for collection, field in (
            ("sourceIdentities", "createdAt"),
            ("sourceClassifications", "snapshotPersistedAt"),
            ("sourceTransitionOwners", "createdAt"),
            ("sourceWorkLedgers", "createdAt"),
        ):
            with self.subTest(collection=collection, field=field):
                store = self._store()
                bundle, _binding = self._seed_prerequisites(store)
                reference = self._user_reference(store).collection(
                    collection
                ).document("source-1")
                store.data[reference.path][field] = datetime(
                    2026,
                    8,
                    4,
                    12,
                    0,
                    3,
                    tzinfo=timezone.utc,
                )
                before = deepcopy(store.data)
                store.events.clear()
                with self.assertRaises(self.module.RowAuthorityConflict):
                    self._claim(store, bundle=bundle)
                self.assertEqual(before, store.data)
                self.assertEqual([], self._write_events(store))

    def test_claim_validates_385_worst_case_before_executor_and_exact_count_before_write(self):
        store = self._store()
        bundle, _binding = self._seed_prerequisites(store)
        original = self.module._require_row_authority_planned_writes
        with patch.object(
            self.module,
            "_require_row_authority_planned_writes",
            wraps=original,
        ) as validator:
            result = self._claim(store, bundle=bundle)
        observed = [call.args[0] for call in validator.call_args_list]
        self.assertEqual(385, observed[0])
        self.assertIn(3, observed[1:])
        self.assertEqual(3, result["claimSet"]["plannedWrites"])
        self.assertIn(("commit_applied", 3), store.events)

    def test_private_validated_optout_plan_dominates_terminal_and_human(self):
        for owner_kind in ("terminal", "human_decision"):
            with self.subTest(owner_kind=owner_kind):
                store = self._store()
                self._seed_row(store, self.first)
                binding = self._seed_thread_binding(store, [self.first])
                _claim, prior_generation, prior_head = self._install_owner(
                    store,
                    self.first,
                    owner_kind=owner_kind,
                )
                plan = self._contact_plan(store)
                self.assertEqual("created", plan["disposition"])
                self.assertEqual(3, plan["claimSet"]["derivedPriority"])
                self.assertEqual(2, plan["generations"][0]["generation"])
                self.assertEqual(
                    prior_generation["generationHash"],
                    plan["predecessorSettlements"][0]["generationHash"],
                )
                self.assertEqual(
                    prior_head["fencingToken"],
                    plan["predecessorSettlements"][0]["fencingToken"],
                )
                self.assertEqual(
                    plan["generations"][0]["generationHash"],
                    plan["predecessorSettlements"][0][
                        "dominantGenerationHash"
                    ],
                )

    def test_terminal_dominates_human(self):
        store = self._store()
        bundle, _binding = self._seed_prerequisites(store)
        _human_claim, human_generation, _human_head = self._install_owner(
            store,
            self.first,
            owner_kind="human_decision",
        )
        store.events.clear()
        result = self._claim(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:00:03.000000Z",
            lease_until="2026-08-04T12:06:00.000000Z",
        )
        self.assertEqual("created", result["disposition"])
        self.assertEqual(2, result["generations"][0]["generation"])
        self.assertEqual(2, result["heads"][0]["effectivePriority"])
        self.assertEqual(1, len(result["predecessorSettlements"]))
        self.assertEqual(
            human_generation["generationHash"],
            result["predecessorSettlements"][0]["generationHash"],
        )
        self.assertIn(("commit_applied", 4), store.events)

    def test_unverified_model_optout_cannot_exceed_human_priority(self):
        bundle = RowOwnershipContractTests._b1_bundle(
            self,
            owner_kind="human_decision",
            model_contact_optout=True,
        )
        link = self.module.build_b1_authority_link(
            user_scope_hash=self.scope,
            source_identity_document=bundle["identity"],
            source_classification_document=bundle["classification"],
            source_owner_document=bundle["owner"],
            source_ledger_document=bundle["ledger"],
            work_key=bundle["work_key"],
        )
        self.assertEqual("human_decision", link["ownerKind"])
        self.assertEqual(1, self.module.derive_owner_priority(link["ownerKind"]))
        self.assertIsNone(link["hardOptOutEvidenceHash"])

    def test_equal_priority_first_commit_wins_without_lexical_election(self):
        store = self._store()
        first_bundle, _binding = self._seed_prerequisites(
            store,
            owner_kind="human_decision",
        )
        first = self._claim(store, bundle=first_bundle)
        second_bundle = self._seed_b1_bundle(
            store,
            owner_kind="human_decision",
            source_id="000-lexically-first",
        )
        store.events.clear()
        second = self._claim(
            store,
            bundle=second_bundle,
            created_at="2026-08-04T12:00:03.000000Z",
            lease_until="2026-08-04T12:06:00.000000Z",
        )
        self.assertEqual("dominated", second["disposition"])
        self.assertEqual(
            first["generations"][0]["generationHash"],
            second["claimSet"]["rowDecisions"][0][
                "winnerGenerationHash"
            ],
        )
        self.assertIn(("commit_applied", 1), store.events)

    def test_lower_claim_writes_only_dominated_claim_set(self):
        store = self._store()
        bundle, _binding = self._seed_prerequisites(store)
        self._install_contact_owner(store, self.first)
        head_before = deepcopy(
            store.data[self._row_references(store, self.first)[1].path]
        )
        store.events.clear()
        result = self._claim(store, bundle=bundle)
        self.assertEqual("dominated", result["disposition"])
        writes = self._write_events(store)
        self.assertEqual(1, len(writes))
        self.assertEqual("create", writes[0][0])
        self.assertIn("/rowClaimSets/", writes[0][1])
        self.assertEqual(
            head_before,
            store.data[self._row_references(store, self.first)[1].path],
        )

    def test_multirow_dominated_marks_peers_blocked_and_advances_no_generation(self):
        store = self._store()
        bundle, _binding = self._seed_prerequisites(
            store,
            row_ids=[self.first, self.second],
        )
        self._install_contact_owner(store, self.first)
        before_generations = {
            path: deepcopy(payload)
            for path, payload in store.data.items()
            if "/rowOwnerGenerations/" in path
        }
        result = self._claim(store, bundle=bundle)
        self.assertEqual(
            ["dominated", "blocked_by_claim_set"],
            [item["decision"] for item in result["claimSet"]["rowDecisions"]],
        )
        self.assertEqual([], result["generations"])
        self.assertEqual(
            before_generations,
            {
                path: payload
                for path, payload in store.data.items()
                if "/rowOwnerGenerations/" in path
            },
        )

    def test_higher_claim_dominates_unsettled_predecessor_and_preserves_effective_settlement(self):
        store = self._store()
        terminal_bundle, _binding = self._seed_prerequisites(store)
        (
            _action,
            _human_claim,
            _human_generation,
            human_settlement,
            _human_head,
        ) = self._install_settled_human_owner(store, self.first)
        terminal = self._claim(
            store,
            bundle=terminal_bundle,
            created_at="2026-08-04T12:00:04.000000Z",
            lease_until="2026-08-04T12:07:00.000000Z",
        )
        self.assertEqual(
            human_settlement["settlementHash"],
            terminal["heads"][0]["effectiveSettlementHash"],
        )
        contact = self._contact_plan(
            store,
            created_at="2026-08-04T12:00:05.000000Z",
            lease_until="2026-08-04T12:08:00.000000Z",
        )
        self.assertEqual(3, contact["generations"][0]["generation"])
        self.assertEqual(1, len(contact["predecessorSettlements"]))
        self.assertEqual(
            human_settlement["settlementHash"],
            contact["generations"][0]["predecessorSettlementHash"],
        )
        self.assertEqual(
            human_settlement["settlementHash"],
            contact["heads"][0]["effectiveSettlementHash"],
        )
        self.assertEqual(
            contact["predecessorSettlements"][0]["settlementHash"],
            contact["heads"][0]["latestSettlementHash"],
        )

    def test_higher_claim_after_takeover_freezes_current_not_first_fence(self):
        store = self._store()
        terminal_bundle, _binding = self._seed_prerequisites(store)
        _claim, generation, head = self._install_owner(
            store,
            self.first,
            owner_kind="human_decision",
        )
        takeover = self.module._build_lease_takeover_head(
            expected_head=head,
            generation_document=generation,
            new_lease_owner_hash="3" * 64,
            new_lease_until="2026-08-04T12:10:00.000000Z",
            taken_at="2026-08-04T12:06:00.000000Z",
        )
        self._row_references(store, self.first)[1].set(takeover, merge=False)
        result = self._claim(
            store,
            bundle=terminal_bundle,
            created_at="2026-08-04T12:07:00.000000Z",
            lease_until="2026-08-04T12:12:00.000000Z",
        )
        self.assertEqual(1, generation["firstFencingToken"])
        self.assertEqual(2, takeover["fencingToken"])
        self.assertEqual(
            2,
            result["predecessorSettlements"][0]["fencingToken"],
        )
        self.assertEqual(3, result["generations"][0]["firstFencingToken"])

    def test_higher_claim_never_rewrites_settled_lower_generation(self):
        store = self._store()
        self._seed_row(store, self.first)
        binding = self._seed_thread_binding(store, [self.first])
        claim, generation, head = self._install_owner(
            store,
            self.first,
            owner_kind="terminal",
        )
        settlement, _settled_head = self._settle_terminal_owner(
            store,
            claim,
            generation,
            head,
        )
        settlement_before = deepcopy(
            store.data[self._settlement_reference(store, self.first, 1).path]
        )
        plan = self._contact_plan(
            store,
            created_at="2026-08-04T12:00:04.000000Z",
        )
        self.assertEqual((), plan["predecessorSettlements"])
        self.assertFalse(
            any(
                item["target"] == f"predecessor_settlement:{self.first}"
                for item in plan["mutations"]
            )
        )
        self.assertEqual(settlement, settlement_before)
        self.assertEqual(
            settlement["settlementHash"],
            plan["heads"][0]["effectiveSettlementHash"],
        )

    def test_ownerless_historical_postrelease_shape_fails_closed(self):
        store = self._store()
        bundle, _binding = self._seed_prerequisites(store)
        _identity_ref, head_ref = self._row_references(store, self.first)
        head = deepcopy(store.data[head_ref.path])
        head.update(
            {
                "stateRevision": 2,
                "latestSettlementHash": "2" * 64,
                "updatedAt": self.binding_at,
            }
        )
        head_ref.set(self._rehash_head(head), merge=False)
        before = deepcopy(store.data)
        store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(store, bundle=bundle)
        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))

    def test_first_and_next_generation_allocation_are_exact(self):
        first_store = self._store()
        bundle, _binding = self._seed_prerequisites(first_store)
        first = self._claim(first_store, bundle=bundle)
        self.assertEqual(1, first["generations"][0]["generation"])
        self.assertEqual(1, first["generations"][0]["firstFencingToken"])

        next_store = self._store()
        terminal_bundle, _binding = self._seed_prerequisites(next_store)
        self._install_owner(
            next_store,
            self.first,
            owner_kind="human_decision",
        )
        following = self._claim(
            next_store,
            bundle=terminal_bundle,
            created_at="2026-08-04T12:00:03.000000Z",
            lease_until="2026-08-04T12:06:00.000000Z",
        )
        self.assertEqual(2, following["generations"][0]["generation"])
        self.assertEqual(2, following["generations"][0]["firstFencingToken"])

    def test_exact_claim_retry_is_zero_write_already_applied(self):
        store = self._store()
        bundle, _binding = self._seed_prerequisites(store)
        created = self._claim(store, bundle=bundle)
        before = deepcopy(store.data)
        store.events.clear()
        replay = self._claim(store, bundle=bundle)
        self.assertEqual("created", created["disposition"])
        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(created["claimSet"], replay["claimSet"])
        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))
        self.assertIn(("commit_applied", 0), store.events)

    def test_exact_claim_retry_after_location_settlement_or_higher_generation_keeps_later_head(self):
        location_store = self._store()
        location_bundle, _binding = self._seed_prerequisites(location_store)
        created = self._claim(location_store, bundle=location_bundle)
        _identity_ref, location_head_ref = self._row_references(
            location_store,
            self.first,
        )
        location_head = deepcopy(created["heads"][0])
        location_head.update(
            {
                "stateRevision": location_head["stateRevision"] + 1,
                "currentLocationRevision": (
                    location_head["currentLocationRevision"] + 1
                ),
                "currentLocationHash": "a" * 64,
                "updatedAt": "2026-08-04T12:00:03.000000Z",
            }
        )
        location_head = self._rehash_head(location_head)
        location_head_ref.set(location_head, merge=False)
        replay = self._claim(location_store, bundle=location_bundle)
        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(location_head, replay["heads"][0])
        self.assertEqual(location_head, location_store.data[location_head_ref.path])

        settlement_store = self._store()
        settlement_bundle, _binding = self._seed_prerequisites(settlement_store)
        accepted = self._claim(settlement_store, bundle=settlement_bundle)
        settlement, settled_head = self._settle_terminal_owner(
            settlement_store,
            accepted["claimSet"],
            accepted["generations"][0],
            accepted["heads"][0],
        )
        settled_replay = self._claim(settlement_store, bundle=settlement_bundle)
        self.assertEqual("already_applied", settled_replay["disposition"])
        self.assertEqual(settled_head, settled_replay["heads"][0])
        self.assertEqual(
            settlement,
            settlement_store.data[
                self._settlement_reference(settlement_store, self.first, 1).path
            ],
        )

        higher_store = self._store()
        human_bundle, _binding = self._seed_prerequisites(
            higher_store,
            owner_kind="human_decision",
        )
        human = self._claim(higher_store, bundle=human_bundle)
        terminal_bundle = self._seed_b1_bundle(
            higher_store,
            owner_kind="terminal",
            source_id="later-terminal",
        )
        higher = self._claim(
            higher_store,
            bundle=terminal_bundle,
            created_at="2026-08-04T12:00:03.000000Z",
            lease_until="2026-08-04T12:06:00.000000Z",
        )
        higher_head_before = deepcopy(higher["heads"][0])
        human_replay = self._claim(higher_store, bundle=human_bundle)
        self.assertEqual("already_applied", human_replay["disposition"])
        self.assertEqual(higher_head_before, human_replay["heads"][0])
        self.assertEqual(1, human["generations"][0]["generation"])
        self.assertEqual(2, higher["generations"][0]["generation"])

    def test_existing_request_hash_or_timestamp_drift_is_conflict(self):
        timestamp_store = self._store()
        bundle, _binding = self._seed_prerequisites(timestamp_store)
        self._claim(timestamp_store, bundle=bundle)
        before = deepcopy(timestamp_store.data)
        timestamp_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(
                timestamp_store,
                bundle=bundle,
                created_at="2026-08-04T12:00:03.000000Z",
                lease_until="2026-08-04T12:06:00.000000Z",
            )
        self.assertEqual(before, timestamp_store.data)
        self.assertEqual([], self._write_events(timestamp_store))

        hash_store = self._store()
        bundle, _binding = self._seed_prerequisites(hash_store)
        created = self._claim(hash_store, bundle=bundle)
        claim_ref = self._claim_reference(
            hash_store,
            created["claimSet"]["requestId"],
        )
        hash_store.data[claim_ref.path]["claimSetHash"] = "f" * 64
        before = deepcopy(hash_store.data)
        hash_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(hash_store, bundle=bundle)
        self.assertEqual(before, hash_store.data)
        self.assertEqual([], self._write_events(hash_store))

    def test_identical_workers_create_one_claim_and_both_succeed(self):
        store = self._store()
        bundle, _binding = self._seed_prerequisites(store)
        store.events.clear()
        store.before_commit_barrier = Barrier(2)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self._claim, store, bundle=bundle)
                for _index in range(2)
            ]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(
            ["already_applied", "created"],
            sorted(item["disposition"] for item in results),
        )
        self.assertEqual(results[0]["claimSet"], results[1]["claimSet"])
        self.assertEqual(1, store.events.count(("commit_applied", 3)))
        self.assertEqual(1, store.events.count(("commit_applied", 0)))
        self.assertTrue(
            any(event[0] == "commit_aborted_stale_read" for event in store.events)
        )

    def test_different_equal_priority_workers_preserve_first_commit(self):
        store = self._store()
        first_bundle, _binding = self._seed_prerequisites(
            store,
            owner_kind="human_decision",
        )
        second_bundle = self._seed_b1_bundle(
            store,
            owner_kind="human_decision",
            source_id="source-2",
        )
        store.events.clear()
        store.before_commit_barrier = Barrier(2)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = (
                pool.submit(self._claim, store, bundle=first_bundle),
                pool.submit(self._claim, store, bundle=second_bundle),
            )
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(
            ["created", "dominated"],
            sorted(item["disposition"] for item in results),
        )
        winner = next(item for item in results if item["disposition"] == "created")
        loser = next(item for item in results if item["disposition"] == "dominated")
        self.assertEqual(
            winner["generations"][0]["generationHash"],
            loser["claimSet"]["rowDecisions"][0]["winnerGenerationHash"],
        )
        self.assertEqual(1, store.events.count(("commit_applied", 3)))
        self.assertEqual(1, store.events.count(("commit_applied", 1)))

    def test_claim_128_rows_stays_at_or_below_385_writes(self):
        rows = [_row_id(index) for index in range(1, 129)]
        store = self._store()
        bundle, _binding = self._seed_prerequisites(store, row_ids=rows)
        for row_id in rows:
            self._install_owner(
                store,
                row_id,
                owner_kind="human_decision",
            )
        store.events.clear()
        result = self._claim(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:00:03.000000Z",
            lease_until="2026-08-04T12:06:00.000000Z",
        )
        self.assertEqual(128, len(result["generations"]))
        self.assertEqual(128, len(result["predecessorSettlements"]))
        self.assertEqual(385, result["claimSet"]["plannedWrites"])
        self.assertIn(("commit_applied", 385), store.events)
        self.assertFalse(
            any(event[0] == "commit_refused_write_ceiling" for event in store.events)
        )

    def test_claim_rejects_malformed_stored_129_row_binding_with_zero_writes(self):
        rows = [_row_id(index) for index in range(1, 130)]
        store = self._store()
        bundle = self._seed_b1_bundle(store)
        binding = self.module.build_thread_row_binding_document(
            user_scope_hash=self.scope,
            thread_id="thread-1",
            client_id="client-1",
            row_ids=rows[:128],
            primary_row_id=rows[0],
            created_at=self.binding_at,
        )
        malformed = deepcopy(binding)
        malformed["rowBindings"].append(
            {"rowId": rows[128], "role": "related"}
        )
        malformed["bindingCount"] = 129
        self._user_reference(store).collection("threadRowBindings").document(
            "thread-1"
        ).create(malformed)
        before = deepcopy(store.data)
        store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(store, bundle=bundle)
        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))

    def test_claim_preapply_failure_is_retryable_with_zero_writes(self):
        store = self._store()
        bundle, _binding = self._seed_prerequisites(store)
        before = deepcopy(store.data)
        store.events.clear()
        store.fail_next_commit = RuntimeError("preapply claim failure")
        with self.assertRaises(self.module.RowAuthorityRetryable):
            self._claim(store, bundle=bundle)
        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))
        self.assertIn(("commit_failed_before_apply",), store.events)

    def test_claim_apply_then_raise_requires_claim_generations_heads_and_dominated_settlements(self):
        store = self._store()
        bundle, _binding = self._seed_prerequisites(store)
        self._install_owner(
            store,
            self.first,
            owner_kind="human_decision",
        )
        store.events.clear()
        store.apply_then_raise_next_commit = RuntimeError(
            "unknown claim commit outcome"
        )
        result = self._claim(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:00:03.000000Z",
            lease_until="2026-08-04T12:06:00.000000Z",
        )
        self.assertEqual("created", result["disposition"])
        self.assertEqual(1, len(result["generations"]))
        self.assertEqual(1, len(result["predecessorSettlements"]))
        self.assertIn(("commit_applied", 4), store.events)
        failure_index = store.events.index(("commit_raised_after_apply",))
        self.assertTrue(
            any(event[0] == "get" for event in store.events[failure_index + 1 :])
        )

    def test_claim_partial_malformed_or_unreadable_readback_is_ambiguous(self):
        def partial_executor(transaction, callback):
            transaction._begin()
            callback(transaction)
            operation, reference, payload, merge = transaction._operations[0]
            transaction._rollback()
            self.assertEqual(("create", False), (operation, merge))
            reference.create(payload)
            raise RuntimeError("partial claim apply")

        partial_store = self._store()
        partial_bundle, _binding = self._seed_prerequisites(partial_store)
        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._claim(
                partial_store,
                bundle=partial_bundle,
                executor=partial_executor,
            )

        malformed_store = self._store()
        malformed_bundle, _binding = self._seed_prerequisites(malformed_store)

        def malformed_executor(transaction, callback):
            transaction._begin()
            callback(transaction)
            generation_path = next(
                reference.path
                for operation, reference, _payload, _merge in transaction._operations
                if operation == "create" and "/rowOwnerGenerations/" in reference.path
            )
            transaction._commit()
            malformed_store.data[generation_path]["generationHash"] = "f" * 64
            raise RuntimeError("malformed claim after-image")

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._claim(
                malformed_store,
                bundle=malformed_bundle,
                executor=malformed_executor,
            )

        unreadable_store = self._store()
        unreadable_bundle, _binding = self._seed_prerequisites(unreadable_store)
        fake_module = importlib.import_module("tests.source_coordinator_fakes")
        original_get = fake_module.FakeDocumentReference.get

        def fail_readback(reference, *, transaction=None):
            if transaction is None:
                raise RuntimeError("claim readback unavailable")
            return original_get(reference, transaction=transaction)

        unreadable_store.apply_then_raise_next_commit = RuntimeError(
            "applied then raised"
        )
        with patch.object(
            fake_module.FakeDocumentReference,
            "get",
            new=fail_readback,
        ), self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._claim(unreadable_store, bundle=unreadable_bundle)

    def test_transaction_retry_rebuilds_every_decision_from_fresh_reads(self):
        store = self._store()
        bundle, _binding = self._seed_prerequisites(store)
        _identity_ref, head_ref = self._row_references(store, self.first)
        advanced = deepcopy(store.data[head_ref.path])
        advanced.update(
            {
                "stateRevision": 2,
                "currentLocationRevision": 2,
                "currentLocationHash": "b" * 64,
                "updatedAt": "2026-08-04T12:00:02.000000Z",
            }
        )
        advanced = self._rehash_head(advanced)

        def advance_location_before_first_commit():
            head_ref.set(advanced, merge=False)

        store.events.clear()
        store.before_next_commit_hook = advance_location_before_first_commit
        result = self._claim(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:00:03.000000Z",
            lease_until="2026-08-04T12:06:00.000000Z",
        )
        self.assertEqual(
            advanced["headHash"],
            result["generations"][0]["predecessorHeadHash"],
        )
        self.assertIn(("transaction_began", 0), store.events)
        self.assertIn(("transaction_began", 1), store.events)
        self.assertTrue(
            any(event[0] == "commit_aborted_stale_read" for event in store.events)
        )

    def test_dominated_claim_rejects_partial_future_generation_or_settlement_state(self):
        for collection in ("rowOwnerGenerations", "rowOwnerSettlements"):
            with self.subTest(collection=collection):
                store = self._store()
                bundle, _binding = self._seed_prerequisites(store)
                self._install_contact_owner(store, self.first)
                future_ref = self._user_reference(store).collection(
                    collection
                ).document(f"{self.first}--2")
                future_ref.create({"partial": "future ownership state"})
                before = deepcopy(store.data)
                store.events.clear()
                with self.assertRaises(self.module.RowAuthorityAmbiguous):
                    self._claim(store, bundle=bundle)
                self.assertEqual(before, store.data)
                self.assertEqual([], self._write_events(store))

    def test_current_owner_state_revision_and_time_cannot_regress_below_generation(self):
        store = self._store()
        bundle, _binding = self._seed_prerequisites(store)
        _claim, generation, _head = self._install_owner(
            store,
            self.first,
            owner_kind="human_decision",
        )
        _identity_ref, head_ref = self._row_references(store, self.first)
        regressed = deepcopy(store.data[head_ref.path])
        regressed["stateRevision"] = generation["generation"]
        head_ref.set(self._rehash_head(regressed), merge=False)
        before = deepcopy(store.data)
        store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(
                store,
                bundle=bundle,
                created_at="2026-08-04T12:00:03.000000Z",
                lease_until="2026-08-04T12:06:00.000000Z",
            )
        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))

    def test_current_generation_owner_must_match_its_accepted_claim(self):
        store = self._store()
        terminal_bundle, _binding = self._seed_prerequisites(store)
        _human_claim, human_generation, _human_head = self._install_owner(
            store,
            self.first,
            owner_kind="human_decision",
        )
        generation_ref = self._generation_reference(store, self.first, 1)
        forged_generation = deepcopy(human_generation)
        forged_generation.update(
            {
                "ownerKind": "terminal",
                "priority": 2,
            }
        )
        generation_material = {
            key: forged_generation[key]
            for key in (
                "rowId",
                "generation",
                "requestId",
                "claimSetHash",
                "predecessorHeadHash",
                "predecessorSettlementHash",
                "ownerKind",
                "ownerKey",
                "priority",
                "leaseEpoch",
                "firstFencingToken",
                "createdAt",
            )
        }
        forged_generation["generationHash"] = self.module.domain_hash(
            self.module.OWNER_GENERATION_HASH_DOMAIN,
            generation_material,
            user_scope_hash=self.scope,
        )
        generation_ref.set(forged_generation, merge=False)
        _identity_ref, head_ref = self._row_references(store, self.first)
        forged_head = deepcopy(store.data[head_ref.path])
        forged_head.update(
            {
                "effectiveOwnerGenerationHash": forged_generation[
                    "generationHash"
                ],
                "effectiveOwnerKind": "terminal",
                "effectivePriority": 2,
                "state": "claimed",
            }
        )
        head_ref.set(self._rehash_head(forged_head), merge=False)
        before = deepcopy(store.data)
        store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(
                store,
                bundle=terminal_bundle,
                created_at="2026-08-04T12:00:03.000000Z",
                lease_until="2026-08-04T12:06:00.000000Z",
            )
        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))

    def test_active_generation_predecessor_settlement_must_match_effective_head(self):
        store = self._store()
        bundle, _binding = self._seed_prerequisites(store)
        self._install_owner(store, self.first, owner_kind="terminal")
        _identity_ref, head_ref = self._row_references(store, self.first)
        drifted = deepcopy(store.data[head_ref.path])
        drifted.update(
            {
                "latestSettlementHash": "2" * 64,
                "effectiveSettlementHash": "2" * 64,
            }
        )
        head_ref.set(self._rehash_head(drifted), merge=False)
        before = deepcopy(store.data)
        store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(
                store,
                bundle=bundle,
                created_at="2026-08-04T12:00:03.000000Z",
                lease_until="2026-08-04T12:06:00.000000Z",
            )
        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))

    def test_settled_generation_fence_and_time_must_match_head(self):
        for mode in ("fence", "time"):
            with self.subTest(mode=mode):
                store = self._store()
                self._seed_row(store, self.first)
                binding = self._seed_thread_binding(store, [self.first])
                claim, generation, head = self._install_owner(
                    store,
                    self.first,
                    owner_kind="terminal",
                )
                _settlement, settled_head = self._settle_terminal_owner(
                    store,
                    claim,
                    generation,
                    head,
                )
                drifted = deepcopy(settled_head)
                if mode == "fence":
                    drifted["fencingToken"] += 1
                else:
                    drifted["updatedAt"] = self.claimed_at
                self._row_references(store, self.first)[1].set(
                    self._rehash_head(drifted),
                    merge=False,
                )
                with self.assertRaises(self.module.RowAuthorityConflict):
                    self._contact_plan(
                        store,
                        created_at="2026-08-04T12:00:04.000000Z",
                    )

    def test_settlement_derived_hashes_and_contact_supersession_are_correlated(self):
        derived_store = self._store()
        human_bundle, _binding = self._seed_prerequisites(
            derived_store,
            owner_kind="human_decision",
        )
        terminal_claim, terminal_generation, terminal_head = (
            self._install_owner(
                derived_store,
                self.first,
                owner_kind="terminal",
            )
        )
        terminal_settlement, terminal_settled_head = (
            self._settle_terminal_owner(
                derived_store,
                terminal_claim,
                terminal_generation,
                terminal_head,
            )
        )
        drifted_settlement = deepcopy(terminal_settlement)
        drifted_settlement.update(
            {
                "outcomeEvidenceHash": "d" * 64,
                "logicalOutcomeHash": "e" * 64,
            }
        )
        drifted_settlement = self._rehash_settlement(drifted_settlement)
        self._settlement_reference(derived_store, self.first, 1).set(
            drifted_settlement,
            merge=False,
        )
        drifted_head = deepcopy(terminal_settled_head)
        drifted_head.update(
            {
                "latestSettlementHash": drifted_settlement[
                    "settlementHash"
                ],
                "effectiveSettlementHash": drifted_settlement[
                    "settlementHash"
                ],
            }
        )
        self._row_references(derived_store, self.first)[1].set(
            self._rehash_head(drifted_head),
            merge=False,
        )
        before = deepcopy(derived_store.data)
        derived_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(
                derived_store,
                bundle=human_bundle,
                created_at="2026-08-04T12:00:04.000000Z",
                lease_until="2026-08-04T12:07:00.000000Z",
            )
        self.assertEqual(before, derived_store.data)
        self.assertEqual([], self._write_events(derived_store))

        contact_store = self._store()
        terminal_bundle, _binding = self._seed_prerequisites(contact_store)
        contact_claim, contact_generation, contact_head = (
            self._install_contact_owner(contact_store, self.first)
        )
        contact_settlement = self.module.build_owner_settlement_document(
            generation_document=contact_generation,
            claim_set_document=contact_claim,
            fencing_token=contact_head["fencingToken"],
            outcome="contact_optout",
            settled_at="2026-08-04T12:00:03.000000Z",
            superseded_effective_settlement_hash=None,
        )
        contact_settlement["supersededEffectiveSettlementHash"] = "f" * 64
        contact_settlement = self._rehash_settlement(contact_settlement)
        contact_settled_head = self.module._build_settlement_advanced_head(
            expected_head=contact_head,
            generation_document=contact_generation,
            settlement_document=contact_settlement,
        )
        self._settlement_reference(contact_store, self.first, 1).create(
            contact_settlement
        )
        self._row_references(contact_store, self.first)[1].set(
            contact_settled_head,
            merge=False,
        )
        before = deepcopy(contact_store.data)
        contact_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(
                contact_store,
                bundle=terminal_bundle,
                created_at="2026-08-04T12:00:04.000000Z",
                lease_until="2026-08-04T12:07:00.000000Z",
            )
        self.assertEqual(before, contact_store.data)
        self.assertEqual([], self._write_events(contact_store))

    def test_full_lineage_enforces_priority_outcome_and_effective_preservation(self):
        preservation_store = self._store()
        terminal_bundle, _binding = self._seed_prerequisites(
            preservation_store
        )
        self._install_owner(
            preservation_store,
            self.first,
            owner_kind="human_decision",
        )
        terminal = self._claim(
            preservation_store,
            bundle=terminal_bundle,
            created_at="2026-08-04T12:00:03.000000Z",
            lease_until="2026-08-04T12:06:00.000000Z",
        )
        generation_ref = self._generation_reference(
            preservation_store,
            self.first,
            2,
        )
        forged_generation = deepcopy(
            preservation_store.data[generation_ref.path]
        )
        forged_generation["predecessorSettlementHash"] = "f" * 64
        forged_generation = self._rehash_generation(forged_generation)
        generation_ref.set(forged_generation, merge=False)
        settlement_ref = self._settlement_reference(
            preservation_store,
            self.first,
            1,
        )
        forged_settlement = deepcopy(
            preservation_store.data[settlement_ref.path]
        )
        forged_settlement["dominantGenerationHash"] = forged_generation[
            "generationHash"
        ]
        forged_settlement = self._rehash_settlement(forged_settlement)
        settlement_ref.set(forged_settlement, merge=False)
        _identity_ref, head_ref = self._row_references(
            preservation_store,
            self.first,
        )
        forged_head = deepcopy(terminal["heads"][0])
        forged_head.update(
            {
                "effectiveOwnerGenerationHash": forged_generation[
                    "generationHash"
                ],
                "latestSettlementHash": forged_settlement[
                    "settlementHash"
                ],
            }
        )
        head_ref.set(self._rehash_head(forged_head), merge=False)
        before = deepcopy(preservation_store.data)
        preservation_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(preservation_store, bundle=terminal_bundle)
        self.assertEqual(before, preservation_store.data)
        self.assertEqual([], self._write_events(preservation_store))

        priority_store = self._store()
        human_bundle, _binding = self._seed_prerequisites(
            priority_store,
            owner_kind="human_decision",
        )
        first_claim, first_generation, first_head = self._install_owner(
            priority_store,
            self.first,
            owner_kind="terminal",
        )
        first_settlement, settled_head = self._settle_terminal_owner(
            priority_store,
            first_claim,
            first_generation,
            first_head,
        )
        equal_bundle = RowOwnershipContractTests._b1_bundle(
            self,
            owner_kind="terminal",
            source_id="equal-priority-terminal",
        )
        equal_link = self.module.build_b1_authority_link(
            user_scope_hash=self.scope,
            source_identity_document=equal_bundle["identity"],
            source_classification_document=equal_bundle["classification"],
            source_owner_document=equal_bundle["owner"],
            source_ledger_document=equal_bundle["ledger"],
            work_key=equal_bundle["work_key"],
        )
        equal_claim = self.module.build_claim_set_document(
            user_scope_hash=self.scope,
            authority_origin="b1_source",
            authority_link=equal_link,
            operator_action_document=None,
            fanout_id=None,
            row_ids=[self.first],
            primary_row_id=self.first,
            planned_writes=3,
            outcome="accepted",
            row_decisions=[
                {
                    "rowId": self.first,
                    "decision": "accepted",
                    "plannedGeneration": 2,
                    "winnerGenerationHash": None,
                    "winnerSettlementHash": None,
                }
            ],
            created_at="2026-08-04T12:00:04.000000Z",
        )
        equal_generation = self.module.build_owner_generation_document(
            claim_set_document=equal_claim,
            row_id=self.first,
            generation=2,
            predecessor_head_hash=settled_head["headHash"],
            predecessor_settlement_hash=first_settlement["settlementHash"],
            lease_epoch=1,
            first_fencing_token=2,
            created_at="2026-08-04T12:00:04.000000Z",
        )
        equal_head = deepcopy(settled_head)
        equal_head.update(
            {
                "stateRevision": settled_head["stateRevision"] + 1,
                "effectiveOwnerGeneration": 2,
                "effectiveOwnerGenerationHash": equal_generation[
                    "generationHash"
                ],
                "effectiveOwnerKind": "terminal",
                "effectivePriority": 2,
                "state": "claimed",
                "leaseOwnerHash": "b" * 64,
                "leaseUntil": "2026-08-04T12:07:00.000000Z",
                "fencingToken": 2,
                "updatedAt": "2026-08-04T12:00:04.000000Z",
            }
        )
        self._claim_reference(
            priority_store,
            equal_claim["requestId"],
        ).create(equal_claim)
        self._generation_reference(priority_store, self.first, 2).create(
            equal_generation
        )
        self._row_references(priority_store, self.first)[1].set(
            self._rehash_head(equal_head),
            merge=False,
        )
        before = deepcopy(priority_store.data)
        priority_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(
                priority_store,
                bundle=human_bundle,
                created_at="2026-08-04T12:00:05.000000Z",
                lease_until="2026-08-04T12:08:00.000000Z",
            )
        self.assertEqual(before, priority_store.data)
        self.assertEqual([], self._write_events(priority_store))

        outcome_store = self._store()
        terminal_bundle, _binding = self._seed_prerequisites(outcome_store)
        (
            _action,
            human_claim,
            human_generation,
            human_settlement,
            human_settled_head,
        ) = self._install_settled_human_owner(outcome_store, self.first)
        forged_human_settlement = deepcopy(human_settlement)
        forged_human_settlement.update(
            {
                "outcome": "contact_optout",
                "operatorActionHash": None,
                "outcomeReasonCode": "verified_optout",
            }
        )
        forged_human_settlement["outcomeEvidenceHash"] = (
            self.module._outcome_evidence_hash(
                user_scope_hash=self.scope,
                authority_link_hash=human_claim["authorityLinkHash"],
                operator_action_hash=human_claim["operatorActionHash"],
                fanout_id=human_claim["fanoutId"],
                payload_hash=human_claim["payloadHash"],
                outcome_reason_code="verified_optout",
            )
        )
        forged_human_settlement["logicalOutcomeHash"] = (
            self.module._logical_outcome_hash(
                user_scope_hash=self.scope,
                row_id=self.first,
                generation=1,
                owner_kind="human_decision",
                owner_key=human_generation["ownerKey"],
                outcome="contact_optout",
                outcome_reason_code="verified_optout",
                outcome_evidence_hash=forged_human_settlement[
                    "outcomeEvidenceHash"
                ],
            )
        )
        forged_human_settlement = self._rehash_settlement(
            forged_human_settlement
        )
        self._settlement_reference(outcome_store, self.first, 1).set(
            forged_human_settlement,
            merge=False,
        )
        human_settled_head.update(
            {
                "latestSettlementHash": forged_human_settlement[
                    "settlementHash"
                ],
                "effectiveSettlementHash": forged_human_settlement[
                    "settlementHash"
                ],
            }
        )
        forged_predecessor_head = self._rehash_head(human_settled_head)
        terminal_link = self.module.build_b1_authority_link(
            user_scope_hash=self.scope,
            source_identity_document=terminal_bundle["identity"],
            source_classification_document=terminal_bundle["classification"],
            source_owner_document=terminal_bundle["owner"],
            source_ledger_document=terminal_bundle["ledger"],
            work_key=terminal_bundle["work_key"],
        )
        terminal_claim = self.module.build_claim_set_document(
            user_scope_hash=self.scope,
            authority_origin="b1_source",
            authority_link=terminal_link,
            operator_action_document=None,
            fanout_id=None,
            row_ids=[self.first],
            primary_row_id=self.first,
            planned_writes=3,
            outcome="accepted",
            row_decisions=[
                {
                    "rowId": self.first,
                    "decision": "accepted",
                    "plannedGeneration": 2,
                    "winnerGenerationHash": None,
                    "winnerSettlementHash": None,
                }
            ],
            created_at="2026-08-04T12:00:04.000000Z",
        )
        terminal_generation = self.module.build_owner_generation_document(
            claim_set_document=terminal_claim,
            row_id=self.first,
            generation=2,
            predecessor_head_hash=forged_predecessor_head["headHash"],
            predecessor_settlement_hash=forged_human_settlement[
                "settlementHash"
            ],
            lease_epoch=1,
            first_fencing_token=2,
            created_at="2026-08-04T12:00:04.000000Z",
        )
        terminal_head = deepcopy(forged_predecessor_head)
        terminal_head.update(
            {
                "stateRevision": forged_predecessor_head[
                    "stateRevision"
                ]
                + 1,
                "effectiveOwnerGeneration": 2,
                "effectiveOwnerGenerationHash": terminal_generation[
                    "generationHash"
                ],
                "effectiveOwnerKind": "terminal",
                "effectivePriority": 2,
                "state": "claimed",
                "leaseOwnerHash": "b" * 64,
                "leaseUntil": "2026-08-04T12:07:00.000000Z",
                "fencingToken": 2,
                "updatedAt": "2026-08-04T12:00:04.000000Z",
            }
        )
        self._claim_reference(
            outcome_store,
            terminal_claim["requestId"],
        ).create(terminal_claim)
        self._generation_reference(outcome_store, self.first, 2).create(
            terminal_generation
        )
        self._row_references(outcome_store, self.first)[1].set(
            self._rehash_head(terminal_head),
            merge=False,
        )
        before = deepcopy(outcome_store.data)
        outcome_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(
                outcome_store,
                bundle=terminal_bundle,
                created_at="2026-08-04T12:00:04.000000Z",
                lease_until="2026-08-04T12:07:00.000000Z",
            )
        self.assertEqual(before, outcome_store.data)
        self.assertEqual([], self._write_events(outcome_store))

    def test_nonadjacent_replay_requires_complete_bounded_lineage(self):
        store = self._store()
        human_bundle, binding = self._seed_prerequisites(
            store,
            owner_kind="human_decision",
        )
        human = self._claim(store, bundle=human_bundle)
        terminal_bundle = self._seed_b1_bundle(
            store,
            owner_kind="terminal",
            source_id="bounded-terminal",
        )
        self._claim(
            store,
            bundle=terminal_bundle,
            created_at="2026-08-04T12:00:03.000000Z",
            lease_until="2026-08-04T12:06:00.000000Z",
        )
        contact = self._contact_plan(
            store,
            created_at="2026-08-04T12:00:04.000000Z",
            lease_until="2026-08-04T12:07:00.000000Z",
        )
        self._claim_reference(
            store,
            contact["claimSet"]["requestId"],
        ).create(contact["claimSet"])
        for generation in contact["generations"]:
            self._generation_reference(
                store,
                generation["rowId"],
                generation["generation"],
            ).create(generation)
        for settlement in contact["predecessorSettlements"]:
            self._settlement_reference(
                store,
                settlement["rowId"],
                settlement["generation"],
            ).create(settlement)
        for head in contact["heads"]:
            self._row_references(store, head["rowId"])[1].set(
                head,
                merge=False,
            )
        replay = self._claim(store, bundle=human_bundle)
        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(3, replay["heads"][0]["effectiveOwnerGeneration"])
        self.assertEqual(1, human["generations"][0]["generation"])

        store.data.pop(
            self._generation_reference(store, self.first, 1).path
        )
        before = deepcopy(store.data)
        store.events.clear()
        with self.assertRaises(self.module.RowAuthorityError):
            self._claim(store, bundle=human_bundle)
        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))

    def test_full_lineage_rejects_impossible_writes_time_and_lease(self):
        for mode in (
            "planned_writes",
            "historical_time",
            "expired_lease",
            "inflated_fence",
        ):
            with self.subTest(mode=mode):
                store = self._store()
                human_bundle, _binding = self._seed_prerequisites(
                    store,
                    owner_kind="human_decision",
                )
                claim, generation, head = self._install_owner(
                    store,
                    self.first,
                    owner_kind="terminal",
                )
                claim_ref = self._claim_reference(
                    store,
                    claim["requestId"],
                )
                generation_ref = self._generation_reference(
                    store,
                    self.first,
                    1,
                )
                _identity_ref, head_ref = self._row_references(
                    store,
                    self.first,
                )
                if mode == "planned_writes":
                    claim["plannedWrites"] = 400
                    claim = self._rehash_claim(claim)
                    generation["claimSetHash"] = claim["claimSetHash"]
                    generation = self._rehash_generation(generation)
                    head["effectiveOwnerGenerationHash"] = generation[
                        "generationHash"
                    ]
                elif mode == "historical_time":
                    claim["createdAt"] = "2026-08-04T11:00:00.000000Z"
                    claim = self._rehash_claim(claim)
                    generation.update(
                        {
                            "claimSetHash": claim["claimSetHash"],
                            "createdAt": "2026-08-04T11:00:00.000000Z",
                        }
                    )
                    generation = self._rehash_generation(generation)
                    head["effectiveOwnerGenerationHash"] = generation[
                        "generationHash"
                    ]
                elif mode == "expired_lease":
                    head["leaseUntil"] = "2026-08-04T11:00:00.000000Z"
                else:
                    head["fencingToken"] = 99
                claim_ref.set(claim, merge=False)
                generation_ref.set(generation, merge=False)
                head_ref.set(self._rehash_head(head), merge=False)
                before = deepcopy(store.data)
                store.events.clear()
                with self.assertRaises(self.module.RowAuthorityConflict):
                    self._claim(
                        store,
                        bundle=human_bundle,
                        created_at="2026-08-04T12:00:03.000000Z",
                        lease_until="2026-08-04T12:06:00.000000Z",
                    )
                self.assertEqual(before, store.data)
                self.assertEqual([], self._write_events(store))

        exact_store = self._store()
        terminal_bundle, _binding = self._seed_prerequisites(exact_store)
        self._install_settled_human_owner(exact_store, self.first)
        terminal = self._claim(
            exact_store,
            bundle=terminal_bundle,
            created_at="2026-08-04T12:00:04.000000Z",
            lease_until="2026-08-04T12:07:00.000000Z",
        )
        claim_ref = self._claim_reference(
            exact_store,
            terminal["claimSet"]["requestId"],
        )
        drifted_claim = deepcopy(terminal["claimSet"])
        drifted_claim["plannedWrites"] = 4
        drifted_claim = self._rehash_claim(drifted_claim)
        claim_ref.set(drifted_claim, merge=False)
        generation_ref = self._generation_reference(
            exact_store,
            self.first,
            2,
        )
        drifted_generation = deepcopy(
            exact_store.data[generation_ref.path]
        )
        drifted_generation["claimSetHash"] = drifted_claim["claimSetHash"]
        drifted_generation = self._rehash_generation(drifted_generation)
        generation_ref.set(drifted_generation, merge=False)
        _identity_ref, head_ref = self._row_references(
            exact_store,
            self.first,
        )
        drifted_head = deepcopy(exact_store.data[head_ref.path])
        drifted_head["effectiveOwnerGenerationHash"] = drifted_generation[
            "generationHash"
        ]
        head_ref.set(self._rehash_head(drifted_head), merge=False)
        before = deepcopy(exact_store.data)
        exact_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(
                exact_store,
                bundle=terminal_bundle,
                created_at="2026-08-04T12:00:04.000000Z",
                lease_until="2026-08-04T12:07:00.000000Z",
            )
        self.assertEqual(before, exact_store.data)
        self.assertEqual([], self._write_events(exact_store))

    def test_complete_claim_cohort_rejects_missing_peer_generation(self):
        store = self._store()
        first_bundle, _binding = self._seed_prerequisites(
            store,
            owner_kind="human_decision",
            row_ids=[self.first, self.second],
        )
        _identity_ref, second_head_ref = self._row_references(
            store,
            self.second,
        )
        second_clear_head = deepcopy(store.data[second_head_ref.path])
        self._claim(store, bundle=first_bundle)
        store.data.pop(
            self._generation_reference(store, self.second, 1).path
        )
        second_head_ref.set(second_clear_head, merge=False)
        second_bundle = self._seed_b1_bundle(
            store,
            owner_kind="human_decision",
            source_id="second-human-cohort",
        )
        before = deepcopy(store.data)
        store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(
                store,
                bundle=second_bundle,
                created_at="2026-08-04T12:00:03.000000Z",
                lease_until="2026-08-04T12:06:00.000000Z",
            )
        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))

    def test_overlapping_binding_validates_only_the_target_claim_cohort(self):
        store = self._store()
        human_bundle, _binding = self._seed_prerequisites(
            store,
            owner_kind="human_decision",
            row_ids=[self.first, self.second],
        )
        self._claim(store, bundle=human_bundle)
        overlap_binding = self.module.build_thread_row_binding_document(
            user_scope_hash=self.scope,
            thread_id="thread-2",
            client_id="client-1",
            row_ids=[self.first],
            primary_row_id=self.first,
            created_at="2026-08-04T12:00:03.000000Z",
        )
        user = self._user_reference(store)
        user.collection("threadRowBindings").document("thread-2").create(
            overlap_binding
        )
        for edge in self.module.build_row_thread_binding_documents(
            thread_binding_document=overlap_binding
        ):
            user.collection("rowThreadBindings").document(
                edge["edgeId"]
            ).create(edge)
        terminal_bundle = self._seed_b1_bundle(
            store,
            owner_kind="terminal",
            source_id="overlapping-terminal",
        )
        source_identity_ref = user.collection("sourceIdentities").document(
            terminal_bundle["identity"]["canonicalSourceId"]
        )
        store.data[source_identity_ref.path]["threadId"] = "thread-2"
        result = self._claim(
            store,
            bundle=terminal_bundle,
            created_at="2026-08-04T12:00:04.000000Z",
            lease_until="2026-08-04T12:07:00.000000Z",
        )
        self.assertEqual("created", result["disposition"])
        self.assertEqual([self.first], [head["rowId"] for head in result["heads"]])
        self.assertEqual(2, result["generations"][0]["generation"])
        second_head = store.data[
            self._row_references(store, self.second)[1].path
        ]
        self.assertEqual(1, second_head["effectiveOwnerGeneration"])

    def test_dominated_replay_proves_winners_and_blocked_peer_semantics(self):
        winner_store = self._store()
        terminal_bundle, _binding = self._seed_prerequisites(winner_store)
        self._install_contact_owner(winner_store, self.first)
        dominated = self._claim(winner_store, bundle=terminal_bundle)
        dominated_ref = self._claim_reference(
            winner_store,
            dominated["claimSet"]["requestId"],
        )
        drifted_claim = deepcopy(winner_store.data[dominated_ref.path])
        drifted_claim["rowDecisions"][0]["winnerGenerationHash"] = "f" * 64
        drifted_claim = self._rehash_claim(drifted_claim)
        dominated_ref.set(drifted_claim, merge=False)
        before = deepcopy(winner_store.data)
        winner_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(winner_store, bundle=terminal_bundle)
        self.assertEqual(before, winner_store.data)
        self.assertEqual([], self._write_events(winner_store))

        blocked_store = self._store()
        terminal_bundle, _binding = self._seed_prerequisites(
            blocked_store,
            row_ids=[self.first, self.second],
        )
        self._install_contact_owner(blocked_store, self.first)
        self._install_contact_owner(blocked_store, self.second)
        dominated = self._claim(
            blocked_store,
            bundle=terminal_bundle,
            created_at="2026-08-04T12:00:03.000000Z",
            lease_until="2026-08-04T12:06:00.000000Z",
        )
        dominated_ref = self._claim_reference(
            blocked_store,
            dominated["claimSet"]["requestId"],
        )
        drifted_claim = deepcopy(blocked_store.data[dominated_ref.path])
        drifted_claim["rowDecisions"][1].update(
            {
                "decision": "blocked_by_claim_set",
                "winnerGenerationHash": None,
                "winnerSettlementHash": None,
            }
        )
        drifted_claim = self._rehash_claim(drifted_claim)
        dominated_ref.set(drifted_claim, merge=False)
        before = deepcopy(blocked_store.data)
        blocked_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(
                blocked_store,
                bundle=terminal_bundle,
                created_at="2026-08-04T12:00:03.000000Z",
                lease_until="2026-08-04T12:06:00.000000Z",
            )
        self.assertEqual(before, blocked_store.data)
        self.assertEqual([], self._write_events(blocked_store))

    def test_replay_rejects_unproven_numeric_only_higher_generation(self):
        store = self._store()
        human_bundle, binding = self._seed_prerequisites(
            store,
            owner_kind="human_decision",
        )
        original = self._claim(store, bundle=human_bundle)
        terminal_bundle = RowOwnershipContractTests._b1_bundle(
            self,
            owner_kind="terminal",
            source_id="unlinked-terminal",
        )
        terminal_link = self.module.build_b1_authority_link(
            user_scope_hash=self.scope,
            source_identity_document=terminal_bundle["identity"],
            source_classification_document=terminal_bundle["classification"],
            source_owner_document=terminal_bundle["owner"],
            source_ledger_document=terminal_bundle["ledger"],
            work_key=terminal_bundle["work_key"],
        )
        forged_claim = self.module.build_claim_set_document(
            user_scope_hash=self.scope,
            authority_origin="b1_source",
            authority_link=terminal_link,
            operator_action_document=None,
            fanout_id=None,
            row_ids=[self.first],
            primary_row_id=self.first,
            planned_writes=3,
            outcome="accepted",
            row_decisions=[
                {
                    "rowId": self.first,
                    "decision": "accepted",
                    "plannedGeneration": 2,
                    "winnerGenerationHash": None,
                    "winnerSettlementHash": None,
                }
            ],
            created_at="2026-08-04T12:00:03.000000Z",
        )
        forged_generation = self.module.build_owner_generation_document(
            claim_set_document=forged_claim,
            row_id=self.first,
            generation=2,
            predecessor_head_hash="a" * 64,
            predecessor_settlement_hash=None,
            lease_epoch=1,
            first_fencing_token=2,
            created_at="2026-08-04T12:00:03.000000Z",
        )
        forged_head = deepcopy(original["heads"][0])
        forged_head.update(
            {
                "stateRevision": forged_head["stateRevision"] + 1,
                "effectiveOwnerGeneration": 2,
                "effectiveOwnerGenerationHash": forged_generation[
                    "generationHash"
                ],
                "effectiveOwnerKind": "terminal",
                "effectivePriority": 2,
                "state": "claimed",
                "leaseOwnerHash": "b" * 64,
                "leaseUntil": "2026-08-04T12:06:00.000000Z",
                "fencingToken": 2,
                "updatedAt": "2026-08-04T12:00:03.000000Z",
            }
        )
        forged_head = self._rehash_head(forged_head)
        self._claim_reference(store, forged_claim["requestId"]).create(
            forged_claim
        )
        self._generation_reference(store, self.first, 2).create(
            forged_generation
        )
        self._row_references(store, self.first)[1].set(
            forged_head,
            merge=False,
        )
        before = deepcopy(store.data)
        store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._contact_plan(
                store,
                created_at="2026-08-04T12:00:04.000000Z",
            )
        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._claim(store, bundle=human_bundle)
        self.assertEqual(before, store.data)
        self.assertEqual([], self._write_events(store))

    def test_replay_enforces_semantic_planned_write_count(self):
        dominated_store = self._store()
        dominated_bundle, _binding = self._seed_prerequisites(dominated_store)
        self._install_contact_owner(dominated_store, self.first)
        dominated = self._claim(dominated_store, bundle=dominated_bundle)
        dominated_ref = self._claim_reference(
            dominated_store,
            dominated["claimSet"]["requestId"],
        )
        drifted_claim = deepcopy(dominated_store.data[dominated_ref.path])
        drifted_claim["plannedWrites"] = 400
        claim_material = {
            key: deepcopy(drifted_claim[key])
            for key in (
                "requestId",
                "authorityOrigin",
                "authorityLinkHash",
                "operatorActionHash",
                "fanoutId",
                "rowBindingsHash",
                "ownerKind",
                "ownerKey",
                "derivedPriority",
                "plannedWrites",
                "outcome",
                "rowDecisions",
                "createdAt",
            )
        }
        drifted_claim["claimSetHash"] = self.module.domain_hash(
            self.module.CLAIM_SET_HASH_DOMAIN,
            claim_material,
            user_scope_hash=self.scope,
        )
        dominated_ref.set(drifted_claim, merge=False)
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(dominated_store, bundle=dominated_bundle)

        accepted_store = self._store()
        accepted_bundle, _binding = self._seed_prerequisites(accepted_store)
        accepted = self._claim(accepted_store, bundle=accepted_bundle)
        accepted_ref = self._claim_reference(
            accepted_store,
            accepted["claimSet"]["requestId"],
        )
        drifted_claim = deepcopy(accepted_store.data[accepted_ref.path])
        drifted_claim["plannedWrites"] = 400
        claim_material = {
            key: deepcopy(drifted_claim[key])
            for key in (
                "requestId",
                "authorityOrigin",
                "authorityLinkHash",
                "operatorActionHash",
                "fanoutId",
                "rowBindingsHash",
                "ownerKind",
                "ownerKey",
                "derivedPriority",
                "plannedWrites",
                "outcome",
                "rowDecisions",
                "createdAt",
            )
        }
        drifted_claim["claimSetHash"] = self.module.domain_hash(
            self.module.CLAIM_SET_HASH_DOMAIN,
            claim_material,
            user_scope_hash=self.scope,
        )
        accepted_ref.set(drifted_claim, merge=False)
        generation_ref = self._generation_reference(
            accepted_store,
            self.first,
            1,
        )
        drifted_generation = deepcopy(accepted_store.data[generation_ref.path])
        drifted_generation["claimSetHash"] = drifted_claim["claimSetHash"]
        generation_material = {
            key: drifted_generation[key]
            for key in (
                "rowId",
                "generation",
                "requestId",
                "claimSetHash",
                "predecessorHeadHash",
                "predecessorSettlementHash",
                "ownerKind",
                "ownerKey",
                "priority",
                "leaseEpoch",
                "firstFencingToken",
                "createdAt",
            )
        }
        drifted_generation["generationHash"] = self.module.domain_hash(
            self.module.OWNER_GENERATION_HASH_DOMAIN,
            generation_material,
            user_scope_hash=self.scope,
        )
        generation_ref.set(drifted_generation, merge=False)
        _identity_ref, head_ref = self._row_references(
            accepted_store,
            self.first,
        )
        drifted_head = deepcopy(accepted_store.data[head_ref.path])
        drifted_head["effectiveOwnerGenerationHash"] = drifted_generation[
            "generationHash"
        ]
        head_ref.set(self._rehash_head(drifted_head), merge=False)
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._claim(accepted_store, bundle=accepted_bundle)

class RowLeaseTakeoverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")
        RowClaimStoreTests.setUpClass()

    def setUp(self):
        self.fixture = RowClaimStoreTests("test_terminal_claim_enters_claimed")
        self.fixture.setUp()
        self.user_id = self.fixture.user_id
        self.scope = self.fixture.scope
        self.row_id = self.fixture.first
        self.taken_at = "2026-08-04T12:05:01.000000Z"
        self.new_lease_until = "2026-08-04T12:10:00.000000Z"
        self.new_owner = "b" * 64

    def _store(self):
        return self.fixture._store()

    def _seed_claim(self, *, owner_kind="terminal"):
        store = self._store()
        bundle, _binding = self.fixture._seed_prerequisites(
            store,
            owner_kind=owner_kind,
        )
        result = self.fixture._claim(store, bundle=bundle)
        return store, result

    def _takeover(self, store, expected_head, *, executor=None, **overrides):
        arguments = {
            "verified_user_id": self.user_id,
            "row_id": self.row_id,
            "expected_head": expected_head,
            "new_lease_owner_hash": self.new_owner,
            "new_lease_until": self.new_lease_until,
            "taken_at": self.taken_at,
        }
        arguments.update(overrides)
        return self.fixture._authority(
            store,
            executor=executor,
        ).take_over_expired_lease(**arguments)

    def _generation_reference(self, store, generation=1):
        return self.fixture._generation_reference(
            store,
            self.row_id,
            generation,
        )

    def _settlement_reference(self, store, generation=1):
        return self.fixture._settlement_reference(
            store,
            self.row_id,
            generation,
        )

    def _head_reference(self, store):
        return self.fixture._row_references(store, self.row_id)[1]

    @staticmethod
    def _writes(store):
        return RowClaimStoreTests._write_events(store)

    def _location_advance(self, store, expected_head):
        identity_ref, _head_ref = self.fixture._row_references(
            store,
            self.row_id,
        )
        identity = deepcopy(store.data[identity_ref.path])
        observation = self.module.build_row_observation(
            spreadsheet_id=identity["spreadsheetId"],
            marker_observation={
                "rowId": self.row_id,
                "sheetId": identity["sheetId"],
                "providerRowIndex": 3,
                "displayRowNumber": 4,
                "metadataId": 4,
            },
            ordered_headers=("Email",),
            ordered_cell_values=("moved@example.test",),
            user_scope_hash=self.scope,
        )
        revision = self.module.build_row_location_revision_document(
            identity_document=identity,
            revision=expected_head["currentLocationRevision"] + 1,
            lifecycle="active",
            observations=(observation,),
            previous_revision_hash=expected_head["currentLocationHash"],
            observed_at="2026-08-04T12:05:02.000000Z",
        )
        return self.module.build_location_advanced_head(
            expected_head=expected_head,
            location_revision_document=revision,
        )

    def test_expired_claimed_lease_takeover_advances_one_head_and_same_generation(self):
        method = self.module.RowAuthorityStore.take_over_expired_lease
        self.assertEqual(
            [
                "self",
                "verified_user_id",
                "row_id",
                "expected_head",
                "new_lease_owner_hash",
                "new_lease_until",
                "taken_at",
            ],
            list(inspect.signature(method).parameters),
        )
        store, claimed = self._seed_claim()
        expected = claimed["heads"][0]
        generation_ref = self._generation_reference(store)
        generation_before = deepcopy(store.data[generation_ref.path])
        store.events.clear()

        with patch.object(
            self.module,
            "_require_row_authority_planned_writes",
            wraps=self.module._require_row_authority_planned_writes,
        ) as write_bound:
            result = self._takeover(store, expected)

        self.assertEqual((1,), write_bound.call_args_list[0].args)
        self.assertEqual("taken_over", result["disposition"])
        self.assertEqual(generation_before, result["generation"])
        self.assertEqual(generation_before, store.data[generation_ref.path])
        self.assertEqual("claimed", result["head"]["state"])
        writes = self._writes(store)
        self.assertEqual(
            [("set", self._head_reference(store).path, result["head"], False)],
            writes,
        )
        first_write = next(
            index
            for index, event in enumerate(store.events)
            if event[0] in {"create", "set", "update", "delete"}
        )
        self.assertTrue(
            all(
                event[0] in {"get", "query"}
                for event in store.events[1:first_write]
            )
        )
        self.assertGreaterEqual(
            sum(event[0] == "get" for event in store.events[:first_write]),
            4,
        )
        stored_generation = deepcopy(store.data[generation_ref.path])
        stored_head = deepcopy(store.data[self._head_reference(store).path])
        result["generation"]["ownerKey"] = "0" * 64
        result["head"]["leaseOwnerHash"] = "0" * 64
        self.assertEqual(stored_generation, store.data[generation_ref.path])
        self.assertEqual(stored_head, store.data[self._head_reference(store).path])

    def test_expired_review_pending_lease_takeover_preserves_pending_state(self):
        store, claimed = self._seed_claim(owner_kind="human_decision")
        expected = claimed["heads"][0]

        result = self._takeover(store, expected)

        self.assertEqual("review_pending", expected["state"])
        self.assertEqual("review_pending", result["head"]["state"])
        self.assertEqual(
            expected["effectiveOwnerGenerationHash"],
            result["head"]["effectiveOwnerGenerationHash"],
        )

    def test_takeover_increments_fence_and_state_revision_but_not_lease_epoch(self):
        store, claimed = self._seed_claim()
        expected = claimed["heads"][0]
        generation_ref = self._generation_reference(store)
        generation_before = deepcopy(store.data[generation_ref.path])
        settlement_path = self._settlement_reference(store).path

        result = self._takeover(store, expected)

        self.assertEqual(
            expected["stateRevision"] + 1,
            result["head"]["stateRevision"],
        )
        self.assertEqual(
            expected["fencingToken"] + 1,
            result["head"]["fencingToken"],
        )
        self.assertNotEqual(
            expected["fencingToken"],
            store.data[self._head_reference(store).path]["fencingToken"],
        )
        self.assertEqual(1, result["generation"]["leaseEpoch"])
        self.assertEqual(generation_before, store.data[generation_ref.path])
        self.assertNotIn(settlement_path, store.data)

    def test_unexpired_wrong_state_settled_or_malformed_takeover_writes_nothing(self):
        cases = []

        store, claimed = self._seed_claim()
        cases.append(("unexpired", store, claimed["heads"][0], {"taken_at": self.fixture.claimed_at}))

        store = self._store()
        _identity, clear_head = self.fixture._seed_row(store, self.row_id)
        cases.append(("wrong_state", store, clear_head, {}))

        store, claimed = self._seed_claim()
        claim = claimed["claimSet"]
        generation = claimed["generations"][0]
        settlement, settled_head = self.fixture._settle_terminal_owner(
            store,
            claim,
            generation,
            claimed["heads"][0],
        )
        self.assertIsNotNone(settlement)
        cases.append(("settled", store, settled_head, {}))

        store, claimed = self._seed_claim()
        malformed = deepcopy(claimed["heads"][0])
        malformed["headHash"] = "f" * 64
        cases.append(("malformed", store, malformed, {}))

        for label, candidate_store, expected, overrides in cases:
            with self.subTest(label=label):
                before = deepcopy(candidate_store.data)
                candidate_store.events.clear()
                with self.assertRaises(self.module.RowAuthorityError):
                    self._takeover(candidate_store, expected, **overrides)
                self.assertEqual(before, candidate_store.data)
                self.assertEqual([], self._writes(candidate_store))

    def test_takeover_requires_exact_generation_and_absent_settlement(self):
        missing_store, missing_claim = self._seed_claim()
        missing_ref = self._generation_reference(missing_store)
        del missing_store.data[missing_ref.path]
        before = deepcopy(missing_store.data)
        missing_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._takeover(missing_store, missing_claim["heads"][0])
        self.assertEqual(before, missing_store.data)
        self.assertEqual([], self._writes(missing_store))

        drift_store, drift_claim = self._seed_claim()
        drift_ref = self._generation_reference(drift_store)
        drift_store.data[drift_ref.path]["generationHash"] = "f" * 64
        before = deepcopy(drift_store.data)
        drift_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._takeover(drift_store, drift_claim["heads"][0])
        self.assertEqual(before, drift_store.data)
        self.assertEqual([], self._writes(drift_store))

        settled_store, settled_claim = self._seed_claim()
        generation = settled_claim["generations"][0]
        settlement = self.module.build_owner_settlement_document(
            generation_document=generation,
            claim_set_document=settled_claim["claimSet"],
            fencing_token=settled_claim["heads"][0]["fencingToken"],
            outcome="terminal",
            settled_at="2026-08-04T12:05:00.000000Z",
        )
        self._settlement_reference(settled_store).create(settlement)
        before = deepcopy(settled_store.data)
        settled_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._takeover(settled_store, settled_claim["heads"][0])
        self.assertEqual(before, settled_store.data)
        self.assertEqual([], self._writes(settled_store))

    def test_exact_old_head_takeover_replay_is_zero_write_already_applied(self):
        store, claimed = self._seed_claim()
        expected = claimed["heads"][0]
        first = self._takeover(store, expected)
        store.events.clear()

        replay = self._takeover(store, expected)

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(first["generation"], replay["generation"])
        self.assertEqual(first["head"], replay["head"])
        self.assertEqual([], self._writes(store))
        self.assertIn(("commit_applied", 0), store.events)

        race_store, race_claimed = self._seed_claim()
        race_expected = race_claimed["heads"][0]
        race_store.events.clear()
        race_store.before_commit_barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self._takeover, race_store, race_expected)
                for _index in range(2)
            ]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(
            ["already_applied", "taken_over"],
            sorted(item["disposition"] for item in results),
        )
        self.assertEqual(1, race_store.events.count(("commit_applied", 1)))
        self.assertEqual(1, race_store.events.count(("commit_applied", 0)))
        self.assertTrue(
            any(
                event[0] == "commit_aborted_stale_read"
                for event in race_store.events
            )
        )

    def test_takeover_replay_after_location_only_advance_preserves_new_location(self):
        store, claimed = self._seed_claim()
        expected = claimed["heads"][0]
        first = self._takeover(store, expected)
        advanced = self._location_advance(store, first["head"])
        self._head_reference(store).set(advanced, merge=False)
        store.events.clear()

        replay = self._takeover(store, expected)

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(advanced, replay["head"])
        self.assertEqual(
            advanced,
            store.data[self._head_reference(store).path],
        )
        self.assertEqual([], self._writes(store))

    def test_takeover_replay_after_different_takeover_conflicts_without_rewind(self):
        store, claimed = self._seed_claim()
        original = claimed["heads"][0]
        first = self._takeover(store, original)
        second = self._takeover(
            store,
            first["head"],
            new_lease_owner_hash="c" * 64,
            new_lease_until="2026-08-04T12:15:00.000000Z",
            taken_at="2026-08-04T12:10:01.000000Z",
        )
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self._takeover(store, original)

        self.assertEqual(before, store.data)
        self.assertEqual(second["head"], store.data[self._head_reference(store).path])
        self.assertEqual([], self._writes(store))

    def test_takeover_time_equal_to_head_update_is_valid_and_earlier_is_rejected(self):
        store, claimed = self._seed_claim()
        expected = deepcopy(claimed["heads"][0])
        expected.update(
            {
                "leaseUntil": "2026-08-04T12:05:00.000000Z",
                "updatedAt": "2026-08-04T12:05:01.000000Z",
            }
        )
        expected = self.fixture._rehash_head(expected)
        self._head_reference(store).set(expected, merge=False)
        result = self._takeover(store, expected)
        self.assertEqual("taken_over", result["disposition"])

        earlier_store, earlier_claim = self._seed_claim()
        earlier = deepcopy(earlier_claim["heads"][0])
        earlier.update(
            {
                "leaseUntil": "2026-08-04T12:04:59.000000Z",
                "updatedAt": "2026-08-04T12:05:01.000000Z",
            }
        )
        earlier = self.fixture._rehash_head(earlier)
        self._head_reference(earlier_store).set(earlier, merge=False)
        before = deepcopy(earlier_store.data)
        earlier_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self._takeover(
                earlier_store,
                earlier,
                taken_at="2026-08-04T12:05:00.000000Z",
            )
        self.assertEqual(before, earlier_store.data)
        self.assertEqual([], self._writes(earlier_store))

    def test_other_stale_head_takeover_is_conflict(self):
        store, claimed = self._seed_claim()
        stale = claimed["heads"][0]
        current = self._location_advance(store, stale)
        self._head_reference(store).set(current, merge=False)
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self._takeover(store, stale)

        self.assertEqual(before, store.data)
        self.assertEqual(current, store.data[self._head_reference(store).path])
        self.assertEqual([], self._writes(store))

        retry_store, retry_claimed = self._seed_claim()
        retry_expected = retry_claimed["heads"][0]
        competing_location = self._location_advance(
            retry_store,
            retry_expected,
        )
        retry_head_ref = self._head_reference(retry_store)
        retry_store.events.clear()
        retry_store.before_next_commit_hook = lambda: retry_head_ref.set(
            competing_location,
            merge=False,
        )
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._takeover(retry_store, retry_expected)
        self.assertEqual(competing_location, retry_store.data[retry_head_ref.path])
        self.assertIn(("transaction_began", 0), retry_store.events)
        self.assertIn(("transaction_began", 1), retry_store.events)
        self.assertTrue(
            any(
                event[0] == "commit_aborted_stale_read"
                for event in retry_store.events
            )
        )

    def test_takeover_preapply_and_apply_then_raise_classification_is_exact(self):
        preapply_store, preapply_claim = self._seed_claim()
        before = deepcopy(preapply_store.data)
        preapply_store.events.clear()
        preapply_store.fail_next_commit = RuntimeError("preapply takeover failure")
        with self.assertRaises(self.module.RowAuthorityRetryable):
            self._takeover(preapply_store, preapply_claim["heads"][0])
        self.assertEqual(before, preapply_store.data)
        self.assertEqual([], self._writes(preapply_store))

        applied_store, applied_claim = self._seed_claim()
        applied_store.events.clear()
        applied_store.apply_then_raise_next_commit = RuntimeError(
            "unknown takeover commit outcome"
        )
        applied = self._takeover(applied_store, applied_claim["heads"][0])
        self.assertEqual("taken_over", applied["disposition"])
        self.assertIn(("commit_raised_after_apply",), applied_store.events)
        self.assertEqual(
            applied["head"],
            applied_store.data[self._head_reference(applied_store).path],
        )

        drift_store, drift_claim = self._seed_claim()

        def apply_then_drift(transaction, callback):
            transaction._begin()
            callback(transaction)
            transaction._commit()
            head_ref = self._head_reference(drift_store)
            drifted = deepcopy(drift_store.data[head_ref.path])
            drifted["projectionBacklogCount"] += 1
            head_ref.set(self.fixture._rehash_head(drifted), merge=False)
            raise RuntimeError("takeover after-image drifted")

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._takeover(
                drift_store,
                drift_claim["heads"][0],
                executor=apply_then_drift,
            )

        location_store, location_claim = self._seed_claim()
        advanced_after_apply = {}

        def apply_then_advance_location(transaction, callback):
            transaction._begin()
            callback(transaction)
            transaction._commit()
            head_ref = self._head_reference(location_store)
            advanced = self._location_advance(
                location_store,
                location_store.data[head_ref.path],
            )
            head_ref.set(advanced, merge=False)
            advanced_after_apply["head"] = advanced
            raise RuntimeError("takeover head advanced after apply")

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._takeover(
                location_store,
                location_claim["heads"][0],
                executor=apply_then_advance_location,
            )
        self.assertEqual(
            advanced_after_apply["head"],
            location_store.data[self._head_reference(location_store).path],
        )

        fake_module = importlib.import_module("tests.source_coordinator_fakes")
        original_get = fake_module.FakeDocumentReference.get
        read_store, read_claim = self._seed_claim()
        read_counts = {"transactional": 0, "nontransactional": 0}

        def fail_second_transaction_read(reference, *, transaction=None):
            if transaction is None:
                read_counts["nontransactional"] += 1
            else:
                read_counts["transactional"] += 1
                if read_counts["transactional"] == 2:
                    raise RuntimeError("takeover transactional read failed")
            return original_get(reference, transaction=transaction)

        with patch.object(
            fake_module.FakeDocumentReference,
            "get",
            new=fail_second_transaction_read,
        ), self.assertRaises(self.module.RowAuthorityRetryable):
            self._takeover(read_store, read_claim["heads"][0])
        self.assertEqual(2, read_counts["transactional"])
        self.assertEqual(0, read_counts["nontransactional"])

        unreadable_store, unreadable_claim = self._seed_claim()
        unreadable_store.apply_then_raise_next_commit = RuntimeError(
            "takeover applied but readback is unavailable"
        )

        def fail_nontransaction_readback(reference, *, transaction=None):
            if transaction is None:
                raise RuntimeError("takeover readback unavailable")
            return original_get(reference, transaction=transaction)

        with patch.object(
            fake_module.FakeDocumentReference,
            "get",
            new=fail_nontransaction_readback,
        ), self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._takeover(
                unreadable_store,
                unreadable_claim["heads"][0],
            )

class RowSettlementStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")
        RowClaimStoreTests.setUpClass()

    def setUp(self):
        self.fixture = RowClaimStoreTests("test_terminal_claim_enters_claimed")
        self.fixture.setUp()
        self.user_id = self.fixture.user_id
        self.scope = self.fixture.scope
        self.row_id = self.fixture.first
        self.settled_at = "2026-08-04T12:00:03.000000Z"

    def _store(self):
        return self.fixture._store()

    def _authority(self, store, *, executor=None):
        return self.fixture._authority(store, executor=executor)

    def _seed_terminal(self):
        store = self._store()
        bundle, binding = self.fixture._seed_prerequisites(store)
        claim = self.fixture._claim(store, bundle=bundle)
        return store, binding, claim

    def _settle(self, store, expected_head, *, executor=None, **overrides):
        arguments = {
            "verified_user_id": self.user_id,
            "row_id": self.row_id,
            "expected_head": expected_head,
            "settled_at": self.settled_at,
        }
        arguments.update(overrides)
        return self._authority(store, executor=executor).settle_owner_generation(
            **arguments
        )

    def _identity_reference(self, store):
        return self.fixture._row_references(store, self.row_id)[0]

    def _head_reference(self, store):
        return self.fixture._row_references(store, self.row_id)[1]

    def _generation_reference(self, store, generation=1):
        return self.fixture._generation_reference(
            store,
            self.row_id,
            generation,
        )

    def _settlement_reference(self, store, generation=1):
        return self.fixture._settlement_reference(
            store,
            self.row_id,
            generation,
        )

    @staticmethod
    def _writes(store):
        return RowClaimStoreTests._write_events(store)

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

    def _seed_contact_after_human(self):
        store = self._store()
        _bundle, binding = self.fixture._seed_prerequisites(store)
        (
            action,
            human_claim,
            human_generation,
            human_settlement,
            human_head,
        ) = self.fixture._install_settled_human_owner(store, self.row_id)
        contact_plan = self.fixture._contact_plan(
            store,
            created_at="2026-08-04T12:00:04.000000Z",
            lease_until="2026-08-04T12:06:00.000000Z",
        )
        self._apply_claim_plan(store, contact_plan)
        return {
            "store": store,
            "binding": binding,
            "priorAction": action,
            "priorClaim": human_claim,
            "priorGeneration": human_generation,
            "priorSettlement": human_settlement,
            "priorHead": human_head,
            "claimPlan": contact_plan,
            "claim": contact_plan["claimSet"],
            "generation": contact_plan["generations"][0],
            "head": contact_plan["heads"][0],
        }

    def _seed_terminal_after_human(self):
        store = self._store()
        bundle, _binding = self.fixture._seed_prerequisites(store)
        prior = self.fixture._install_settled_human_owner(store, self.row_id)
        terminal = self.fixture._claim(
            store,
            bundle=bundle,
            created_at="2026-08-04T12:00:04.000000Z",
            lease_until="2026-08-04T12:06:00.000000Z",
        )
        return store, prior, terminal

    def _private_plan(
        self,
        store,
        expected_head,
        *,
        actual_head=None,
        generation=None,
        claim=None,
        stored_settlement=None,
        prior_effective_settlement=None,
        operator_action=None,
        settled_at=None,
    ):
        generation_number = expected_head["effectiveOwnerGeneration"]
        if generation is None:
            generation = deepcopy(
                store.data[
                    self._generation_reference(
                        store,
                        generation_number,
                    ).path
                ]
            )
        if claim is None:
            claim = deepcopy(
                store.data[
                    self.fixture._claim_reference(
                        store,
                        generation["requestId"],
                    ).path
                ]
            )
        if stored_settlement is None:
            stored_settlement = deepcopy(
                store.data.get(
                    self._settlement_reference(
                        store,
                        generation_number,
                    ).path
                )
            )
        return self.module._plan_owner_generation_settlement(
            user_scope_hash=self.scope,
            row_id=self.row_id,
            expected_head=expected_head,
            actual_head_document=(
                actual_head if actual_head is not None else expected_head
            ),
            identity_document=store.data[self._identity_reference(store).path],
            generation_document=generation,
            claim_set_document=claim,
            stored_settlement_document=stored_settlement,
            prior_effective_settlement_document=prior_effective_settlement,
            settled_at=settled_at or self.settled_at,
            operator_action_document=operator_action,
        )

    def _location_advance(self, store, expected_head, *, observed_at):
        identity = deepcopy(store.data[self._identity_reference(store).path])
        next_revision = expected_head["currentLocationRevision"] + 1
        observation = self.module.build_row_observation(
            spreadsheet_id=identity["spreadsheetId"],
            marker_observation={
                "rowId": self.row_id,
                "sheetId": identity["sheetId"],
                "providerRowIndex": next_revision + 2,
                "displayRowNumber": next_revision + 3,
                "metadataId": next_revision + 3,
            },
            ordered_headers=("Email",),
            ordered_cell_values=("settled-move@example.test",),
            user_scope_hash=self.scope,
        )
        revision = self.module.build_row_location_revision_document(
            identity_document=identity,
            revision=next_revision,
            lifecycle="active",
            observations=(observation,),
            previous_revision_hash=expected_head["currentLocationHash"],
            observed_at=observed_at,
        )
        return self.module.build_location_advanced_head(
            expected_head=expected_head,
            location_revision_document=revision,
        )

    def test_terminal_settlement_creates_exact_record_and_settled_head(self):
        method = self.module.RowAuthorityStore.settle_owner_generation
        self.assertEqual(
            [
                "self",
                "verified_user_id",
                "row_id",
                "expected_head",
                "settled_at",
            ],
            list(inspect.signature(method).parameters),
        )
        store, _binding, claimed = self._seed_terminal()
        expected = claimed["heads"][0]
        generation_before = deepcopy(claimed["generations"][0])
        store.events.clear()

        with patch.object(
            self.module,
            "_require_row_authority_planned_writes",
            wraps=self.module._require_row_authority_planned_writes,
        ) as write_bound:
            result = self._settle(store, expected)

        self.assertEqual(
            1,
            sum(call.args == (2,) for call in write_bound.call_args_list),
        )
        self.assertEqual("settled", result["disposition"])
        self.assertEqual(generation_before, result["generation"])
        self.assertEqual("terminal", result["settlement"]["outcome"])
        self.assertEqual("settled", result["head"]["state"])
        self.assertIsNone(result["head"]["leaseOwnerHash"])
        self.assertIsNone(result["head"]["leaseUntil"])
        self.assertEqual(expected["fencingToken"], result["head"]["fencingToken"])
        self.assertEqual(
            result["settlement"]["settlementHash"],
            result["head"]["effectiveSettlementHash"],
        )
        self.assertEqual(
            [
                (
                    "create",
                    self._settlement_reference(store).path,
                    result["settlement"],
                    False,
                ),
                ("set", self._head_reference(store).path, result["head"], False),
            ],
            self._writes(store),
        )
        first_write = next(
            index
            for index, event in enumerate(store.events)
            if event[0] in {"create", "set", "update", "delete"}
        )
        self.assertTrue(
            all(
                event[0] in {"get", "query"}
                for event in store.events[1:first_write]
            )
        )

    def test_private_contact_optout_settlement_freezes_prior_effective_settlement(self):
        state = self._seed_contact_after_human()

        plan = self._private_plan(
            state["store"],
            state["head"],
            generation=state["generation"],
            claim=state["claim"],
            prior_effective_settlement=state["priorSettlement"],
            settled_at="2026-08-04T12:00:05.000000Z",
        )

        self.assertEqual("settled", plan["disposition"])
        self.assertEqual("contact_optout", plan["settlement"]["outcome"])
        self.assertEqual(
            state["priorSettlement"]["settlementHash"],
            plan["settlement"]["supersededEffectiveSettlementHash"],
        )
        self.assertEqual(
            plan["settlement"]["settlementHash"],
            plan["head"]["effectiveSettlementHash"],
        )

    def test_public_contact_optout_settlement_is_blocked_until_b2c(self):
        state = self._seed_contact_after_human()
        before = deepcopy(state["store"].data)
        state["store"].events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self._settle(
                state["store"],
                state["head"],
                settled_at="2026-08-04T12:00:05.000000Z",
            )

        self.assertEqual(before, state["store"].data)
        self.assertEqual([], self._writes(state["store"]))

    def test_terminal_cannot_carry_superseded_effective_settlement(self):
        store, prior, terminal = self._seed_terminal_after_human()

        plan = self._private_plan(
            store,
            terminal["heads"][0],
            generation=terminal["generations"][0],
            claim=terminal["claimSet"],
            prior_effective_settlement=prior[3],
            settled_at="2026-08-04T12:00:05.000000Z",
        )

        self.assertEqual("terminal", plan["settlement"]["outcome"])
        self.assertIsNone(
            plan["settlement"]["supersededEffectiveSettlementHash"]
        )

    def test_settlement_derives_outcome_reason_evidence_and_logical_hash(self):
        store, _binding, claimed = self._seed_terminal()
        result = self._settle(store, claimed["heads"][0])
        settlement = result["settlement"]
        expected = self.module.build_owner_settlement_document(
            generation_document=claimed["generations"][0],
            claim_set_document=claimed["claimSet"],
            fencing_token=claimed["heads"][0]["fencingToken"],
            outcome="terminal",
            settled_at=self.settled_at,
        )
        self.assertEqual(expected, settlement)
        self.assertEqual("terminal_source", settlement["outcomeReasonCode"])
        self.assertNotIn("outcome", inspect.signature(
            self.module.RowAuthorityStore.settle_owner_generation
        ).parameters)

    def test_settlement_requires_current_generation_current_fence_and_claimed_state(self):
        drift_store, _binding, drift_claim = self._seed_terminal()
        generation_ref = self._generation_reference(drift_store)
        drift_store.data[generation_ref.path]["generationHash"] = "f" * 64
        before = deepcopy(drift_store.data)
        drift_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._settle(drift_store, drift_claim["heads"][0])
        self.assertEqual(before, drift_store.data)
        self.assertEqual([], self._writes(drift_store))

        pending_store = self._store()
        bundle, _binding = self.fixture._seed_prerequisites(
            pending_store,
            owner_kind="human_decision",
        )
        pending = self.fixture._claim(pending_store, bundle=bundle)
        before = deepcopy(pending_store.data)
        pending_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._settle(pending_store, pending["heads"][0])
        self.assertEqual(before, pending_store.data)
        self.assertEqual([], self._writes(pending_store))

    def test_stale_fence_after_takeover_cannot_settle_or_change_head(self):
        store, _binding, claimed = self._seed_terminal()
        stale = claimed["heads"][0]
        takeover = self._authority(store).take_over_expired_lease(
            verified_user_id=self.user_id,
            row_id=self.row_id,
            expected_head=stale,
            new_lease_owner_hash="b" * 64,
            new_lease_until="2026-08-04T12:10:00.000000Z",
            taken_at="2026-08-04T12:05:01.000000Z",
        )
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self._settle(
                store,
                stale,
                settled_at="2026-08-04T12:05:02.000000Z",
            )

        self.assertEqual(before, store.data)
        self.assertEqual(takeover["head"], store.data[self._head_reference(store).path])
        self.assertEqual([], self._writes(store))
        current = self._settle(
            store,
            takeover["head"],
            settled_at="2026-08-04T12:05:02.000000Z",
        )
        self.assertEqual(2, current["settlement"]["fencingToken"])

    def test_exact_settlement_retry_is_zero_write_already_applied(self):
        store, _binding, claimed = self._seed_terminal()
        expected = claimed["heads"][0]
        first = self._settle(store, expected)
        store.events.clear()

        replay = self._settle(store, expected)

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(first["settlement"], replay["settlement"])
        self.assertEqual(first["head"], replay["head"])
        self.assertEqual([], self._writes(store))

    def test_settlement_retry_after_location_or_higher_generation_preserves_later_head(self):
        location_store, _binding, location_claim = self._seed_terminal()
        expected = location_claim["heads"][0]
        settled = self._settle(location_store, expected)
        advanced = self._location_advance(
            location_store,
            settled["head"],
            observed_at="2026-08-04T12:00:04.000000Z",
        )
        self._head_reference(location_store).set(advanced, merge=False)
        location_store.events.clear()
        replay = self._settle(location_store, expected)
        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(advanced, replay["head"])
        self.assertEqual([], self._writes(location_store))

        higher_store, binding, higher_claim = self._seed_terminal()
        old_head = higher_claim["heads"][0]
        self._settle(higher_store, old_head)
        contact_plan = self.fixture._contact_plan(
            higher_store,
            created_at="2026-08-04T12:00:05.000000Z",
            lease_until="2026-08-04T12:06:00.000000Z",
        )
        self._apply_claim_plan(higher_store, contact_plan)
        later_head = contact_plan["heads"][0]
        higher_store.events.clear()
        replay = self._settle(higher_store, old_head)
        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(later_head, replay["head"])
        self.assertEqual([], self._writes(higher_store))

        later_generation_ref = self._generation_reference(higher_store, 2)
        valid_later_generation = deepcopy(
            higher_store.data[later_generation_ref.path]
        )
        del higher_store.data[later_generation_ref.path]
        before = deepcopy(higher_store.data)
        higher_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._settle(higher_store, old_head)
        self.assertEqual(before, higher_store.data)
        self.assertEqual([], self._writes(higher_store))

        malformed_later_generation = deepcopy(valid_later_generation)
        malformed_later_generation["generationHash"] = "f" * 64
        higher_store.data[later_generation_ref.path] = (
            malformed_later_generation
        )
        before = deepcopy(higher_store.data)
        higher_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._settle(higher_store, old_head)
        self.assertEqual(before, higher_store.data)
        self.assertEqual([], self._writes(higher_store))

    def test_settlement_time_must_follow_claim_generation_and_current_head(self):
        equal_store, _binding, equal_claim = self._seed_terminal()
        equal = self._settle(
            equal_store,
            equal_claim["heads"][0],
            settled_at=equal_claim["heads"][0]["updatedAt"],
        )
        self.assertEqual("settled", equal["disposition"])

        early_store, _binding, early_claim = self._seed_terminal()
        before = deepcopy(early_store.data)
        early_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self._settle(
                early_store,
                early_claim["heads"][0],
                settled_at="2026-08-04T12:00:01.000000Z",
            )
        self.assertEqual(before, early_store.data)
        self.assertEqual([], self._writes(early_store))

        claim_store, _binding, claim_result = self._seed_terminal()
        claim_ref = self.fixture._claim_reference(
            claim_store,
            claim_result["claimSet"]["requestId"],
        )
        drifted_claim = deepcopy(claim_store.data[claim_ref.path])
        drifted_claim["createdAt"] = "2026-08-04T12:06:00.000000Z"
        drifted_claim = self.fixture._rehash_claim(drifted_claim)
        claim_ref.set(drifted_claim, merge=False)
        generation_ref = self._generation_reference(claim_store)
        drifted_generation = deepcopy(claim_store.data[generation_ref.path])
        drifted_generation["claimSetHash"] = drifted_claim["claimSetHash"]
        drifted_generation = self.fixture._rehash_generation(
            drifted_generation
        )
        generation_ref.set(drifted_generation, merge=False)
        head_ref = self._head_reference(claim_store)
        drifted_head = deepcopy(claim_store.data[head_ref.path])
        drifted_head["effectiveOwnerGenerationHash"] = drifted_generation[
            "generationHash"
        ]
        drifted_head = self.fixture._rehash_head(drifted_head)
        head_ref.set(drifted_head, merge=False)
        before = deepcopy(claim_store.data)
        claim_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._settle(
                claim_store,
                drifted_head,
                settled_at="2026-08-04T12:05:01.000000Z",
            )
        self.assertEqual(before, claim_store.data)
        self.assertEqual([], self._writes(claim_store))

    def test_settlement_preapply_apply_then_raise_and_partial_readback_are_classified(self):
        pre_store, _binding, pre_claim = self._seed_terminal()
        before = deepcopy(pre_store.data)
        pre_store.events.clear()
        pre_store.fail_next_commit = RuntimeError("settlement preapply failure")
        with self.assertRaises(self.module.RowAuthorityRetryable):
            self._settle(pre_store, pre_claim["heads"][0])
        self.assertEqual(before, pre_store.data)
        self.assertEqual([], self._writes(pre_store))

        applied_store, _binding, applied_claim = self._seed_terminal()
        applied_store.apply_then_raise_next_commit = RuntimeError(
            "unknown settlement commit outcome"
        )
        applied = self._settle(applied_store, applied_claim["heads"][0])
        self.assertEqual("settled", applied["disposition"])

        def partial_executor(transaction, callback):
            transaction._begin()
            callback(transaction)
            operation, reference, payload, merge = transaction._operations[0]
            transaction._rollback()
            self.assertEqual(("create", False), (operation, merge))
            reference.create(payload)
            raise RuntimeError("partial settlement apply")

        partial_store, _binding, partial_claim = self._seed_terminal()
        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._settle(
                partial_store,
                partial_claim["heads"][0],
                executor=partial_executor,
            )

        location_store, _binding, location_claim = self._seed_terminal()
        advanced_after_apply = {}

        def apply_then_location(transaction, callback):
            transaction._begin()
            callback(transaction)
            transaction._commit()
            head_ref = self._head_reference(location_store)
            advanced = self._location_advance(
                location_store,
                location_store.data[head_ref.path],
                observed_at="2026-08-04T12:00:04.000000Z",
            )
            head_ref.set(advanced, merge=False)
            advanced_after_apply["head"] = advanced
            raise RuntimeError("settlement head advanced after apply")

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._settle(
                location_store,
                location_claim["heads"][0],
                executor=apply_then_location,
            )
        self.assertEqual(
            advanced_after_apply["head"],
            location_store.data[self._head_reference(location_store).path],
        )

    def test_settlement_schema_contains_no_provider_effect_fields(self):
        store, _binding, claimed = self._seed_terminal()
        result = self._settle(store, claimed["heads"][0])
        settlement = result["settlement"]
        self.assertEqual(
            {
                "schemaVersion",
                "userScopeHash",
                "rowId",
                "generation",
                "generationHash",
                "fencingToken",
                "outcome",
                "dominantGenerationHash",
                "supersededEffectiveSettlementHash",
                "operatorActionHash",
                "outcomeReasonCode",
                "outcomeEvidenceHash",
                "logicalOutcomeHash",
                "settledAt",
                "settlementHash",
            },
            set(settlement),
        )
        serialized = json.dumps(settlement, sort_keys=True).lower()
        for forbidden in ("send", "draft", "messageid", "provider", "sheetwrite"):
            self.assertNotIn(forbidden, serialized)

    def test_private_settlement_planner_never_opens_or_commits_a_transaction(self):
        store, _binding, claimed = self._seed_terminal()
        before = deepcopy(store.data)
        store.events.clear()

        plan = self._private_plan(store, claimed["heads"][0])

        self.assertEqual("settled", plan["disposition"])
        self.assertEqual(before, store.data)
        self.assertEqual([], store.events)
        source = inspect.getsource(
            self.module._plan_owner_generation_settlement
        )
        self.assertNotIn("_transaction_executor", source)
        self.assertNotIn(".transaction(", source)
        self.assertNotIn("commit(", source)


class RowOperatorDeclineStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")
        RowClaimStoreTests.setUpClass()

    def setUp(self):
        self.fixture = RowClaimStoreTests(
            "test_human_claim_enters_review_pending_without_settlement"
        )
        self.fixture.setUp()
        self.user_id = self.fixture.user_id
        self.scope = self.fixture.scope
        self.thread_id = "thread-1"
        self.actor_scope_hash = "5" * 64
        self.client_request_id = "decline-request-1"
        self.issued_at = "2026-08-04T12:00:03.000000Z"

    def _store(self):
        return self.fixture._store()

    def _authority(self, store, *, executor=None):
        return self.fixture._authority(store, executor=executor)

    def _decline(self, store, *, executor=None, **overrides):
        arguments = {
            "verified_user_id": self.user_id,
            "thread_id": self.thread_id,
            "actor_scope_hash": self.actor_scope_hash,
            "client_request_id": self.client_request_id,
            "issued_at": self.issued_at,
        }
        arguments.update(overrides)
        return self._authority(
            store,
            executor=executor,
        ).record_operator_decline(**arguments)

    def _seed_clear(self, *, row_ids=None):
        rows = list(row_ids or [self.fixture.first])
        store = self._store()
        for row_id in rows:
            self.fixture._seed_row(store, row_id)
        binding = self.fixture._seed_thread_binding(store, rows)
        return store, binding

    def _seed_pending(self, *, row_ids=None):
        rows = list(row_ids or [self.fixture.first])
        store = self._store()
        bundle, binding = self.fixture._seed_prerequisites(
            store,
            owner_kind="human_decision",
            row_ids=rows,
        )
        claim = self.fixture._claim(store, bundle=bundle)
        return store, binding, claim

    def _seed_terminal(self, *, row_ids=None):
        rows = list(row_ids or [self.fixture.first])
        store = self._store()
        bundle, binding = self.fixture._seed_prerequisites(
            store,
            row_ids=rows,
        )
        claim = self.fixture._claim(store, bundle=bundle)
        return store, binding, claim

    def _action(self, binding, **overrides):
        arguments = {
            "user_scope_hash": self.scope,
            "actor_scope_hash": self.actor_scope_hash,
            "row_bindings_hash": binding["rowBindingsHash"],
            "client_request_id": self.client_request_id,
            "issued_at": self.issued_at,
        }
        arguments.update(overrides)
        return self.module.build_operator_action_document(**arguments)

    def _action_reference(self, store, action):
        return self.fixture._user_reference(store).collection(
            "rowOperatorActions"
        ).document(action["actionId"])

    @staticmethod
    def _writes(store):
        return RowClaimStoreTests._write_events(store)

    def test_pending_decline_creates_action_and_settles_same_generation_without_claim(self):
        method = getattr(
            self.module.RowAuthorityStore,
            "record_operator_decline",
            None,
        )
        self.assertIsNotNone(method)
        self.assertEqual(
            [
                "self",
                "verified_user_id",
                "thread_id",
                "actor_scope_hash",
                "client_request_id",
                "issued_at",
            ],
            list(inspect.signature(method).parameters),
        )
        store, binding, pending = self._seed_pending()
        before_claim_paths = {
            path for path in store.data if "/rowClaimSets/" in path
        }
        store.events.clear()

        result = self._decline(store)

        self.assertEqual("declined", result["disposition"])
        self.assertEqual(self._action(binding), result["action"])
        self.assertEqual(pending["claimSet"], result["claimSet"])
        self.assertEqual(pending["generations"], result["generations"])
        self.assertEqual(1, len(result["settlements"]))
        self.assertEqual("human_declined", result["settlements"][0]["outcome"])
        self.assertEqual(
            result["action"]["operatorActionHash"],
            result["settlements"][0]["operatorActionHash"],
        )
        self.assertEqual("settled", result["heads"][0]["state"])
        self.assertEqual(
            pending["heads"][0]["effectiveOwnerGeneration"],
            result["heads"][0]["effectiveOwnerGeneration"],
        )
        self.assertEqual(
            before_claim_paths,
            {path for path in store.data if "/rowClaimSets/" in path},
        )
        self.assertEqual(
            ["create", "create", "set"],
            [event[0] for event in self._writes(store)],
        )
        self.assertIn(("commit_applied", 3), store.events)

    def test_no_pending_decline_creates_action_claim_generation_settlement_and_head_atomically(self):
        store, binding = self._seed_clear()
        original_head = deepcopy(
            store.data[
                self.fixture._row_references(
                    store,
                    self.fixture.first,
                )[1].path
            ]
        )
        store.events.clear()

        result = self._decline(store)

        self.assertEqual("declined", result["disposition"])
        self.assertEqual(self._action(binding), result["action"])
        self.assertEqual("authenticated_operator", result["claimSet"]["authorityOrigin"])
        self.assertEqual("accepted", result["claimSet"]["outcome"])
        self.assertEqual(5, result["claimSet"]["plannedWrites"])
        self.assertEqual(1, len(result["generations"]))
        self.assertEqual(1, result["generations"][0]["generation"])
        self.assertEqual("human_decision", result["generations"][0]["ownerKind"])
        self.assertEqual("human_declined", result["settlements"][0]["outcome"])
        self.assertEqual("settled", result["heads"][0]["state"])
        self.assertEqual(
            original_head["stateRevision"] + 1,
            result["heads"][0]["stateRevision"],
        )
        self.assertEqual(
            result["generations"][0]["firstFencingToken"],
            result["heads"][0]["fencingToken"],
        )
        self.assertIsNone(result["heads"][0]["leaseOwnerHash"])
        self.assertIsNone(result["heads"][0]["leaseUntil"])
        self.assertEqual(
            ["create", "create", "create", "create", "set"],
            [event[0] for event in self._writes(store)],
        )
        self.assertIn(("commit_applied", 5), store.events)

    def test_higher_owner_dominates_operator_decline_with_only_action_and_claim_set(self):
        store, binding, terminal = self._seed_terminal()
        current_head = deepcopy(terminal["heads"][0])
        store.events.clear()

        result = self._decline(store)

        self.assertEqual("dominated", result["disposition"])
        self.assertEqual(self._action(binding), result["action"])
        self.assertEqual("dominated", result["claimSet"]["outcome"])
        self.assertEqual(2, result["claimSet"]["plannedWrites"])
        self.assertEqual([], result["generations"])
        self.assertEqual([], result["settlements"])
        self.assertEqual([current_head], result["heads"])
        self.assertEqual(
            ["create", "create"],
            [event[0] for event in self._writes(store)],
        )
        self.assertIn(("commit_applied", 2), store.events)

    def test_mixed_pending_and_nonpending_binding_fails_closed(self):
        store, _binding = self._seed_clear(
            row_ids=[self.fixture.first, self.fixture.second]
        )
        self.fixture._install_owner(
            store,
            self.fixture.first,
            owner_kind="human_decision",
        )
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self._decline(store)

        self.assertEqual(before, store.data)
        self.assertEqual([], self._writes(store))

    def test_actor_target_client_request_action_or_timestamp_drift_writes_nothing(self):
        for field, value in (
            ("actorScopeHash", "0" * 64),
            ("rowBindingsHash", "1" * 64),
            ("clientRequestHash", "2" * 64),
            ("actionKind", "dismiss"),
            ("issuedAt", "2026-08-04T12:00:04.000000Z"),
            ("operatorActionHash", "3" * 64),
        ):
            with self.subTest(field=field):
                store, binding = self._seed_clear()
                expected_action = self._action(binding)
                drifted = deepcopy(expected_action)
                drifted[field] = value
                self._action_reference(store, expected_action).create(drifted)
                before = deepcopy(store.data)
                store.events.clear()

                with self.assertRaises(self.module.RowAuthorityConflict):
                    self._decline(store)

                self.assertEqual(before, store.data)
                self.assertEqual([], self._writes(store))

    def test_operator_action_time_equal_current_head_is_valid_and_earlier_is_rejected(self):
        equal_store, _binding, pending = self._seed_pending()
        equal = self._decline(
            equal_store,
            issued_at=pending["heads"][0]["updatedAt"],
        )
        self.assertEqual("declined", equal["disposition"])

        early_store, _binding, _pending = self._seed_pending()
        before = deepcopy(early_store.data)
        early_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._decline(
                early_store,
                issued_at="2026-08-04T12:00:01.999999Z",
            )
        self.assertEqual(before, early_store.data)
        self.assertEqual([], self._writes(early_store))

        binding_store, _binding = self._seed_clear()
        before = deepcopy(binding_store.data)
        binding_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._decline(
                binding_store,
                issued_at="2026-08-04T12:00:00.500000Z",
            )
        self.assertEqual(before, binding_store.data)
        self.assertEqual([], self._writes(binding_store))

    def test_pending_decline_128_rows_plans_257_writes(self):
        rows = [_row_id(index) for index in range(1, 129)]
        store, _binding, pending = self._seed_pending(row_ids=rows)
        store.events.clear()

        result = self._decline(store)

        self.assertEqual(pending["claimSet"], result["claimSet"])
        self.assertEqual(128, len(result["settlements"]))
        self.assertEqual(128, len(result["heads"]))
        self.assertIn(("commit_applied", 257), store.events)
        self.assertFalse(
            any(event[0] == "commit_refused_write_ceiling" for event in store.events)
        )

    def test_no_pending_decline_128_rows_plans_386_writes(self):
        rows = [_row_id(index) for index in range(1, 129)]
        store, _binding = self._seed_clear(row_ids=rows)
        store.events.clear()

        result = self._decline(store)

        self.assertEqual(386, result["claimSet"]["plannedWrites"])
        self.assertEqual(128, len(result["generations"]))
        self.assertEqual(128, len(result["settlements"]))
        self.assertEqual(128, len(result["heads"]))
        self.assertIn(("commit_applied", 386), store.events)
        self.assertFalse(
            any(event[0] == "commit_refused_write_ceiling" for event in store.events)
        )

    def test_decline_validates_386_worst_case_before_executor_and_exact_count_before_write(self):
        rejected_store, _binding = self._seed_clear()
        rejected_store.events.clear()
        with patch.object(
            self.module,
            "_require_row_authority_planned_writes",
            side_effect=self.module.RowAuthorityConfigError("bound rejected"),
        ) as validator, self.assertRaises(self.module.RowAuthorityConfigError):
            self._decline(rejected_store)
        self.assertEqual([((386,), {})], validator.call_args_list)
        self.assertFalse(
            any(event[0] == "transaction_began" for event in rejected_store.events)
        )

        store, _binding = self._seed_clear()
        original = self.module._require_row_authority_planned_writes
        with patch.object(
            self.module,
            "_require_row_authority_planned_writes",
            wraps=original,
        ) as validator:
            result = self._decline(store)
        observed = [call.args[0] for call in validator.call_args_list]
        self.assertEqual(386, observed[0])
        self.assertIn(5, observed[1:])
        self.assertEqual(5, result["claimSet"]["plannedWrites"])

    def test_decline_does_not_expose_dismiss_stop_or_resume_mutations(self):
        method = self.module.RowAuthorityStore.record_operator_decline
        signature = inspect.signature(method)
        for forbidden_parameter in (
            "action_kind",
            "reason_code",
            "outcome",
            "priority",
            "planned_writes",
        ):
            self.assertNotIn(forbidden_parameter, signature.parameters)
        for forbidden_method in (
            "record_operator_dismiss",
            "record_operator_stop",
            "record_operator_resume",
            "dismiss_operator_action",
            "stop_operator_action",
            "resume_operator_action",
        ):
            self.assertFalse(hasattr(self.module.RowAuthorityStore, forbidden_method))

    def test_operator_decline_exact_retry_race_and_readback_are_idempotent(self):
        store, _binding = self._seed_clear()
        first = self._decline(store)
        store.events.clear()
        replay = self._decline(store)
        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(first["action"], replay["action"])
        self.assertEqual(first["claimSet"], replay["claimSet"])
        self.assertEqual(first["settlements"], replay["settlements"])
        self.assertEqual([], self._writes(store))

        pending_store, _binding, _pending = self._seed_pending()
        pending_first = self._decline(pending_store)
        pending_store.events.clear()
        pending_replay = self._decline(pending_store)
        self.assertEqual("already_applied", pending_replay["disposition"])
        self.assertEqual(
            pending_first["settlements"],
            pending_replay["settlements"],
        )
        self.assertEqual([], self._writes(pending_store))

        dominated_store, _binding, _terminal = self._seed_terminal()
        dominated_first = self._decline(dominated_store)
        dominated_store.events.clear()
        dominated_replay = self._decline(dominated_store)
        self.assertEqual("already_applied", dominated_replay["disposition"])
        self.assertEqual(
            dominated_first["claimSet"],
            dominated_replay["claimSet"],
        )
        self.assertEqual([], self._writes(dominated_store))

        race_store, _binding = self._seed_clear()
        race_store.events.clear()
        race_store.before_commit_barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                future.result(timeout=10)
                for future in (
                    pool.submit(self._decline, race_store),
                    pool.submit(self._decline, race_store),
                )
            ]
        self.assertEqual(
            ["already_applied", "declined"],
            sorted(result["disposition"] for result in results),
        )
        self.assertEqual(1, race_store.events.count(("commit_applied", 5)))
        self.assertEqual(1, race_store.events.count(("commit_applied", 0)))

        pending_race_store, _binding, _pending = self._seed_pending()
        pending_race_store.events.clear()
        pending_race_store.before_commit_barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending_results = [
                future.result(timeout=10)
                for future in (
                    pool.submit(self._decline, pending_race_store),
                    pool.submit(self._decline, pending_race_store),
                )
            ]
        self.assertEqual(
            ["already_applied", "declined"],
            sorted(result["disposition"] for result in pending_results),
        )
        self.assertEqual(
            1,
            pending_race_store.events.count(("commit_applied", 3)),
        )
        self.assertEqual(
            1,
            pending_race_store.events.count(("commit_applied", 0)),
        )

        pre_store, _binding = self._seed_clear()
        before = deepcopy(pre_store.data)
        pre_store.events.clear()
        pre_store.fail_next_commit = RuntimeError("decline preapply failure")
        with self.assertRaises(self.module.RowAuthorityRetryable):
            self._decline(pre_store)
        self.assertEqual(before, pre_store.data)
        self.assertEqual([], self._writes(pre_store))

        applied_store, _binding = self._seed_clear()
        applied_store.apply_then_raise_next_commit = RuntimeError(
            "unknown decline commit outcome"
        )
        applied = self._decline(applied_store)
        self.assertEqual("declined", applied["disposition"])

        pending_applied_store, _binding, _pending = self._seed_pending()
        pending_applied_store.apply_then_raise_next_commit = RuntimeError(
            "unknown pending decline commit outcome"
        )
        pending_applied = self._decline(pending_applied_store)
        self.assertEqual("declined", pending_applied["disposition"])

        def partial_executor(transaction, callback):
            transaction._begin()
            callback(transaction)
            operation, reference, payload, merge = transaction._operations[0]
            transaction._rollback()
            self.assertEqual(("create", False), (operation, merge))
            reference.create(payload)
            raise RuntimeError("partial decline apply")

        partial_store, _binding = self._seed_clear()
        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._decline(partial_store, executor=partial_executor)

        pending_partial_store, pending_binding, _pending = (
            self._seed_pending()
        )
        partial_action = self._action(pending_binding)
        self._action_reference(
            pending_partial_store,
            partial_action,
        ).create(partial_action)
        before = deepcopy(pending_partial_store.data)
        pending_partial_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._decline(pending_partial_store)
        self.assertEqual(before, pending_partial_store.data)
        self.assertEqual([], self._writes(pending_partial_store))

    def test_operator_decline_retry_after_higher_transition_preserves_later_head(self):
        def seed_higher(*, settle_higher=False):
            seeded_store, _binding = self._seed_clear()
            terminal_bundle = self.fixture._seed_b1_bundle(seeded_store)
            seeded_decline = self._decline(seeded_store)
            seeded_higher = self.fixture._claim(
                seeded_store,
                bundle=terminal_bundle,
                created_at="2026-08-04T12:00:04.000000Z",
                lease_until="2026-08-04T12:06:00.000000Z",
            )
            if settle_higher:
                settled = self._authority(
                    seeded_store
                ).settle_owner_generation(
                    verified_user_id=self.user_id,
                    row_id=self.fixture.first,
                    expected_head=seeded_higher["heads"][0],
                    settled_at="2026-08-04T12:00:05.000000Z",
                )
                later = settled["head"]
            else:
                later = seeded_higher["heads"][0]
            return seeded_store, seeded_decline, seeded_higher, later

        store, declined, higher, later_head = seed_higher()
        later_head = deepcopy(later_head)
        store.events.clear()

        replay = self._decline(store)

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(declined["settlements"], replay["settlements"])
        self.assertEqual([later_head], replay["heads"])
        self.assertEqual(
            later_head,
            store.data[
                self.fixture._row_references(store, self.fixture.first)[1].path
            ],
        )
        self.assertEqual([], self._writes(store))

        for artifact, mode, error_type in (
            ("generation", "missing", self.module.RowAuthorityAmbiguous),
            ("generation", "malformed", self.module.RowAuthorityConflict),
            ("claim", "missing", self.module.RowAuthorityAmbiguous),
            ("claim", "malformed", self.module.RowAuthorityConflict),
            ("settlement", "missing", self.module.RowAuthorityAmbiguous),
            ("settlement", "malformed", self.module.RowAuthorityConflict),
        ):
            with self.subTest(artifact=artifact, mode=mode):
                corrupt_store, _declined, corrupt_higher, _later = (
                    seed_higher(settle_higher=artifact == "settlement")
                )
                if artifact == "generation":
                    reference = self.fixture._generation_reference(
                        corrupt_store,
                        self.fixture.first,
                        2,
                    )
                    hash_field = "generationHash"
                elif artifact == "claim":
                    reference = self.fixture._claim_reference(
                        corrupt_store,
                        corrupt_higher["claimSet"]["requestId"],
                    )
                    hash_field = "claimSetHash"
                else:
                    reference = self.fixture._settlement_reference(
                        corrupt_store,
                        self.fixture.first,
                        2,
                    )
                    hash_field = "settlementHash"
                if mode == "missing":
                    del corrupt_store.data[reference.path]
                else:
                    corrupt_store.data[reference.path][hash_field] = "f" * 64
                before = deepcopy(corrupt_store.data)
                corrupt_store.events.clear()
                with self.assertRaises(error_type):
                    self._decline(corrupt_store)
                self.assertEqual(before, corrupt_store.data)
                self.assertEqual([], self._writes(corrupt_store))

        forged_store, _binding = self._seed_clear()
        forged_decline = self._decline(forged_store)
        head_ref = self.fixture._row_references(
            forged_store,
            self.fixture.first,
        )[1]
        forged_head = deepcopy(forged_decline["heads"][0])
        forged_head.update(
            {
                "stateRevision": forged_head["stateRevision"] + 1,
                "state": "claimed",
                "effectiveOwnerGeneration": 2,
                "effectiveOwnerGenerationHash": "e" * 64,
                "effectiveOwnerKind": "terminal",
                "effectivePriority": 2,
                "leaseOwnerHash": "d" * 64,
                "leaseUntil": "2026-08-04T12:06:00.000000Z",
                "fencingToken": 2,
                "updatedAt": "2026-08-04T12:00:04.000000Z",
            }
        )
        head_ref.set(self.fixture._rehash_head(forged_head), merge=False)
        before = deepcopy(forged_store.data)
        forged_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._decline(forged_store)
        self.assertEqual(before, forged_store.data)
        self.assertEqual([], self._writes(forged_store))


class RowSourceSettlementLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("email_automation.row_authority")
        RowClaimStoreTests.setUpClass()

    def setUp(self):
        self.fixture = RowClaimStoreTests("test_terminal_claim_enters_claimed")
        self.fixture.setUp()
        self.user_id = self.fixture.user_id
        self.scope = self.fixture.scope
        self.row_id = self.fixture.first
        self.linked_at = "2026-08-04T12:00:04.000000Z"
        self.b1_settled_at = datetime(
            2026,
            8,
            4,
            12,
            0,
            3,
            tzinfo=timezone.utc,
        )

    def _store(self):
        return self.fixture._store()

    def _authority(self, store, *, executor=None):
        return self.fixture._authority(store, executor=executor)

    def _user_reference(self, store):
        return self.fixture._user_reference(store)

    def _head_reference(self, store, *, row_id=None):
        return self.fixture._row_references(
            store,
            row_id or self.row_id,
        )[1]

    def _link_reference(self, store, *, row_id=None, generation=1):
        checked_row_id = row_id or self.row_id
        return self._user_reference(store).collection(
            "rowSourceSettlementLinks"
        ).document(f"{checked_row_id}--{generation}")

    def _b1_reference(self, store, collection, source_id="source-1"):
        return self._user_reference(store).collection(collection).document(
            source_id
        )

    @staticmethod
    def _writes(store):
        return RowClaimStoreTests._write_events(store)

    @staticmethod
    def _b1_timestamp_token(value):
        return (
            value.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    def _settled_b1_bundle(self, bundle, *, settled_at=None):
        settled_time = settled_at or self.b1_settled_at
        settled = deepcopy(bundle)
        identity = settled["identity"]
        classification = settled["classification"]
        owner = settled["owner"]
        ledger = settled["ledger"]
        entry = ledger["entries"][0]

        deferred_material = {
            "schemaVersion": 1,
            "workKey": entry["workKey"],
            "canonicalSourceId": identity["canonicalSourceId"],
            "ledgerHash": ledger["ledgerHash"],
            "entryPayloadHash": entry["payloadHash"],
            "targetOwnerKind": entry["selectedOwnerKind"],
            "targetOwnerKey": entry["selectedOwnerKey"],
            "wakeCondition": "owner_adapter_ready",
            "completionContract": deepcopy(entry["completionContract"]),
        }
        deferred_binding_hash = _independent_b1_hash(
            {
                "hashKind": "source-deferred-work-v1",
                **deferred_material,
            }
        )
        resolution_evidence = {
            "schemaVersion": 1,
            "evidenceKind": "owner_delegation",
            "canonicalSourceId": identity["canonicalSourceId"],
            "ledgerHash": ledger["ledgerHash"],
            "workKey": entry["workKey"],
            "payloadHash": entry["payloadHash"],
            "workKind": entry["kind"],
            "deferredBindingHash": deferred_binding_hash,
        }
        entry.update(
            {
                "state": "delegated",
                "resolutionEvidence": resolution_evidence,
                "resolutionEvidenceHash": _independent_b1_hash(
                    {
                        "hashKind": (
                            "source-work-resolution-evidence-v1"
                        ),
                        "evidence": resolution_evidence,
                    }
                ),
            }
        )
        ledger.update(
            {
                "revision": 2,
                "updatedAt": settled_time,
            }
        )

        aliases = deepcopy(identity["verifiedAliases"])
        thread_head_material = {
            "hashKind": "thread-transition-head-v1",
            "schemaVersion": 1,
            "threadId": identity["threadId"],
            "threadHeadRevision": 1,
            "activeOwnerKey": owner["ownerKey"],
            "activeOwnerKind": owner["ownerKind"],
            "activeCanonicalSourceId": identity["canonicalSourceId"],
            "activeGeneration": 1,
            "activeState": "active",
        }
        thread_head_binding = {
            "canonicalSourceId": identity["canonicalSourceId"],
            "ownerKind": owner["ownerKind"],
            "ownerKey": owner["ownerKey"],
            "generation": 1,
            "threadHeadRevision": 1,
            "headHash": _independent_b1_hash(thread_head_material),
        }
        identity_hash = _independent_b1_hash(
            {
                "hashKind": "source-settlement-identity-v1",
                "schemaVersion": identity["schemaVersion"],
                "canonicalSourceId": identity["canonicalSourceId"],
                "creationHash": identity["creationHash"],
                "threadId": identity["threadId"],
            }
        )
        final_ledger_evidence_hash = _independent_b1_hash(
            {
                "hashKind": "source-final-ledger-evidence-v1",
                "ledgerHash": ledger["ledgerHash"],
                "entries": [
                    {
                        "workKey": item["workKey"],
                        "payloadHash": item["payloadHash"],
                        "state": item["state"],
                        "resolutionEvidenceHash": item[
                            "resolutionEvidenceHash"
                        ],
                    }
                    for item in ledger["entries"]
                ],
            }
        )
        settlement = {
            "schemaVersion": 1,
            "canonicalSourceId": identity["canonicalSourceId"],
            "identityHash": identity_hash,
            "snapshotImmutableHash": classification[
                "snapshotImmutableHash"
            ],
            "selectionHash": classification["selectionHash"],
            "ownerDecisionHash": owner["ownerDecisionHash"],
            "ledgerHash": ledger["ledgerHash"],
            "finalLedgerEvidenceHash": final_ledger_evidence_hash,
            "threadHeadBinding": thread_head_binding,
            "aliases": aliases,
            "aliasSetHash": _independent_b1_hash(
                {
                    "hashKind": "source-settlement-alias-set-v1",
                    "aliases": aliases,
                }
            ),
            "settlementRevision": 1,
        }
        settlement["settlementHash"] = _independent_b1_hash(
            {
                "hashKind": "source-settlement-v1",
                **settlement,
            }
        )
        settlement["settledAt"] = settled_time
        settled["settlement"] = settlement
        return settled

    def _seed_b1_settlement(self, store, bundle, *, settled_at=None):
        settled = self._settled_b1_bundle(
            bundle,
            settled_at=settled_at,
        )
        source_id = settled["identity"]["canonicalSourceId"]
        self._b1_reference(
            store,
            "sourceWorkLedgers",
            source_id,
        ).set(settled["ledger"], merge=False)
        self._b1_reference(
            store,
            "sourceSettlements",
            source_id,
        ).create(settled["settlement"])
        return settled

    def _seed_linkable(
        self,
        *,
        origin="b1_source",
        b1_settled_at=None,
    ):
        store = self._store()
        if origin == "contact_fanout":
            bundle, binding = self.fixture._seed_prerequisites(
                store,
                owner_kind="contact_optout",
            )
            claim, generation, claimed_head = (
                self.fixture._install_contact_owner(store, self.row_id)
            )
            b2_settlement = self.module.build_owner_settlement_document(
                generation_document=generation,
                claim_set_document=claim,
                fencing_token=claimed_head["fencingToken"],
                outcome="contact_optout",
                settled_at="2026-08-04T12:00:03.000000Z",
            )
            head = self.module._build_settlement_advanced_head(
                expected_head=claimed_head,
                generation_document=generation,
                settlement_document=b2_settlement,
            )
            self.fixture._settlement_reference(
                store,
                self.row_id,
                1,
            ).create(b2_settlement)
            self._head_reference(store).set(head, merge=False)
        else:
            bundle, binding = self.fixture._seed_prerequisites(store)
            claimed = self.fixture._claim(store, bundle=bundle)
            settled = self._authority(store).settle_owner_generation(
                verified_user_id=self.user_id,
                row_id=self.row_id,
                expected_head=claimed["heads"][0],
                settled_at="2026-08-04T12:00:03.000000Z",
            )
            claim = claimed["claimSet"]
            generation = claimed["generations"][0]
            b2_settlement = settled["settlement"]
            head = settled["head"]
        b1 = self._seed_b1_settlement(
            store,
            bundle,
            settled_at=b1_settled_at,
        )
        return {
            "store": store,
            "binding": binding,
            "bundle": b1,
            "claim": claim,
            "generation": generation,
            "b2Settlement": b2_settlement,
            "head": head,
        }

    def _expected_link(self, state, *, linked_at=None):
        return self.module.build_source_settlement_link_document(
            user_scope_hash=self.scope,
            row_id=self.row_id,
            generation=state["generation"]["generation"],
            generation_hash=state["generation"]["generationHash"],
            authority_link_hash=state["claim"]["authorityLinkHash"],
            b1_identity_hash=state["bundle"]["settlement"][
                "identityHash"
            ],
            b1_final_ledger_evidence_hash=state["bundle"]["settlement"][
                "finalLedgerEvidenceHash"
            ],
            b1_settlement_revision=state["bundle"]["settlement"][
                "settlementRevision"
            ],
            b1_settlement_hash=state["bundle"]["settlement"][
                "settlementHash"
            ],
            b2_settlement_hash=state["b2Settlement"]["settlementHash"],
            linked_at=linked_at or self.linked_at,
        )

    def _link(self, state, *, executor=None, linked_at=None):
        authority = self._authority(state["store"], executor=executor)
        method = getattr(authority, "link_b1_source_settlement", None)
        self.assertIsNotNone(
            method,
            "RowAuthorityStore.link_b1_source_settlement is missing",
        )
        return method(
            verified_user_id=self.user_id,
            row_id=self.row_id,
            generation=state["generation"]["generation"],
            linked_at=linked_at or self.linked_at,
        )

    def _synthetic_link(
        self,
        *,
        row_id=None,
        generation=2,
        linked_at="2026-08-04T12:00:05.000000Z",
        seed="9",
    ):
        return self.module.build_source_settlement_link_document(
            user_scope_hash=self.scope,
            row_id=row_id or self.row_id,
            generation=generation,
            generation_hash=seed * 64,
            authority_link_hash="8" * 64,
            b1_identity_hash="7" * 64,
            b1_final_ledger_evidence_hash="6" * 64,
            b1_settlement_revision=1,
            b1_settlement_hash="5" * 64,
            b2_settlement_hash="4" * 64,
            linked_at=linked_at,
        )

    def _advance_head_to_link(self, store, source_link):
        head = deepcopy(store.data[self._head_reference(store).path])
        advanced = self.module._build_source_link_advanced_head(
            expected_head=head,
            source_link_document=source_link,
        )
        self._head_reference(store).set(advanced, merge=False)
        return advanced

    def _set_head_pointer(self, store, pointer, *, updated_at=None):
        head_ref = self._head_reference(store)
        head = deepcopy(store.data[head_ref.path])
        head["latestSourceSettlementLinkHash"] = pointer
        if updated_at is not None:
            head["updatedAt"] = updated_at
        head["stateRevision"] += 1
        head_ref.set(self.fixture._rehash_head(head), merge=False)
        return deepcopy(store.data[head_ref.path])

    def _rewrite_b2_lineage(
        self,
        state,
        *,
        claim_created_at=None,
        generation_created_at=None,
        generation_number=None,
        settled_at=None,
    ):
        store = state["store"]
        old_generation = state["generation"]["generation"]
        target_generation = generation_number or old_generation
        claim = deepcopy(state["claim"])
        if claim_created_at is not None:
            claim["createdAt"] = claim_created_at
        if target_generation != old_generation:
            matching = [
                decision
                for decision in claim["rowDecisions"]
                if decision["rowId"] == self.row_id
            ]
            self.assertEqual(1, len(matching))
            matching[0]["plannedGeneration"] = target_generation
        claim = self.fixture._rehash_claim(claim)

        generation = deepcopy(state["generation"])
        generation["generation"] = target_generation
        generation["claimSetHash"] = claim["claimSetHash"]
        if generation_created_at is not None:
            generation["createdAt"] = generation_created_at
        generation = self.fixture._rehash_generation(generation)
        settlement = self.module.build_owner_settlement_document(
            generation_document=generation,
            claim_set_document=claim,
            fencing_token=state["b2Settlement"]["fencingToken"],
            outcome=state["b2Settlement"]["outcome"],
            settled_at=(
                settled_at or state["b2Settlement"]["settledAt"]
            ),
        )

        claim_ref = self.fixture._claim_reference(
            store,
            claim["requestId"],
        )
        claim_ref.set(claim, merge=False)
        old_generation_ref = self.fixture._generation_reference(
            store,
            self.row_id,
            old_generation,
        )
        old_settlement_ref = self.fixture._settlement_reference(
            store,
            self.row_id,
            old_generation,
        )
        if target_generation != old_generation:
            old_generation_ref.delete()
            old_settlement_ref.delete()
        self.fixture._generation_reference(
            store,
            self.row_id,
            target_generation,
        ).set(generation, merge=False)
        self.fixture._settlement_reference(
            store,
            self.row_id,
            target_generation,
        ).set(settlement, merge=False)

        head = deepcopy(store.data[self._head_reference(store).path])
        head.update(
            {
                "effectiveOwnerGeneration": target_generation,
                "effectiveOwnerGenerationHash": generation[
                    "generationHash"
                ],
                "effectiveOwnerKind": generation["ownerKind"],
                "effectivePriority": generation["priority"],
                "state": "settled",
                "leaseOwnerHash": None,
                "leaseUntil": None,
                "fencingToken": settlement["fencingToken"],
                "latestSettlementHash": settlement["settlementHash"],
                "effectiveSettlementHash": settlement[
                    "settlementHash"
                ],
            }
        )
        head = self.fixture._rehash_head(head)
        self._head_reference(store).set(head, merge=False)
        state.update(
            {
                "claim": claim,
                "generation": generation,
                "b2Settlement": settlement,
                "head": head,
            }
        )
        return state

    def _independent_settlement_hash(self, settlement):
        return _independent_b1_hash(
            {
                "hashKind": "source-settlement-v1",
                **{
                    field: deepcopy(settlement[field])
                    for field in (
                        "schemaVersion",
                        "canonicalSourceId",
                        "identityHash",
                        "snapshotImmutableHash",
                        "selectionHash",
                        "ownerDecisionHash",
                        "ledgerHash",
                        "finalLedgerEvidenceHash",
                        "threadHeadBinding",
                        "aliases",
                        "aliasSetHash",
                        "settlementRevision",
                    )
                },
            }
        )

    def test_source_link_reads_exact_b1_identity_classification_owner_ledger_and_settlement(self):
        method = getattr(
            self.module.RowAuthorityStore,
            "link_b1_source_settlement",
            None,
        )
        self.assertIsNotNone(method)
        self.assertEqual(
            [
                "self",
                "verified_user_id",
                "row_id",
                "generation",
                "linked_at",
            ],
            list(inspect.signature(method).parameters),
        )
        state = self._seed_linkable()
        store = state["store"]
        source_id = state["bundle"]["identity"]["canonicalSourceId"]
        store.events.clear()

        self._link(state)

        reads = [event[1] for event in store.events if event[0] == "get"]
        expected = [
            self.fixture._settlement_reference(store, self.row_id, 1).path,
            self.fixture._generation_reference(store, self.row_id, 1).path,
            self.fixture._claim_reference(
                store,
                state["claim"]["requestId"],
            ).path,
            self.fixture._row_references(store, self.row_id)[0].path,
            self._head_reference(store).path,
            self._link_reference(store).path,
            *[
                self._b1_reference(store, collection, source_id).path
                for collection in (
                    "sourceIdentities",
                    "sourceClassifications",
                    "sourceTransitionOwners",
                    "sourceWorkLedgers",
                    "sourceSettlements",
                )
            ],
            self._user_reference(store)
            .collection("threadRowBindings")
            .document(state["bundle"]["identity"]["threadId"])
            .path,
        ]
        self.assertEqual(expected, reads[: len(expected)])
        first_write = next(
            index
            for index, event in enumerate(store.events)
            if event[0] in {"create", "set", "update", "delete"}
        )
        self.assertFalse(
            any(event[0] == "get" for event in store.events[first_write + 1 :])
        )

    def test_source_link_copies_identity_final_ledger_revision_and_hash_from_b1_settlement(self):
        state = self._seed_linkable()
        settlement = deepcopy(state["bundle"]["settlement"])

        result = self._link(state)

        link = result["sourceSettlementLink"]
        self.assertEqual(
            (
                settlement["identityHash"],
                settlement["finalLedgerEvidenceHash"],
                settlement["settlementRevision"],
                settlement["settlementHash"],
            ),
            (
                link["b1IdentityHash"],
                link["b1FinalLedgerEvidenceHash"],
                link["b1SettlementRevision"],
                link["b1SettlementHash"],
            ),
        )
        self.assertEqual(self._expected_link(state), link)

    def test_source_link_reuses_full_b1_bundle_validation_and_reproduces_settlement_hash(self):
        state = self._seed_linkable()
        settlement = state["bundle"]["settlement"]
        self.assertEqual(
            settlement["settlementHash"],
            self._independent_settlement_hash(settlement),
        )

        with patch.object(
            self.module,
            "_validate_b1_source_identity",
            wraps=self.module._validate_b1_source_identity,
        ) as identity_validator, patch.object(
            self.module,
            "_validate_b1_classification",
            wraps=self.module._validate_b1_classification,
        ) as classification_validator, patch.object(
            self.module,
            "_validate_b1_owner",
            wraps=self.module._validate_b1_owner,
        ) as owner_validator, patch.object(
            self.module,
            "_validate_b1_ledger",
            wraps=self.module._validate_b1_ledger,
        ) as ledger_validator:
            result = self._link(state)

        self.assertEqual(
            settlement["settlementHash"],
            result["sourceSettlementLink"]["b1SettlementHash"],
        )
        for validator in (
            identity_validator,
            classification_validator,
            owner_validator,
            ledger_validator,
        ):
            self.assertGreaterEqual(validator.call_count, 1)

    def test_source_link_requires_b1_or_contact_origin_and_matching_work_entry(self):
        for origin in ("b1_source", "contact_fanout"):
            with self.subTest(origin=origin):
                valid = self._seed_linkable(origin=origin)
                self.assertEqual(
                    "linked",
                    self._link(valid)["disposition"],
                )

        operator_store = self._store()
        self.fixture._seed_row(operator_store, self.row_id)
        self.fixture._seed_thread_binding(operator_store, [self.row_id])
        operator = self._authority(operator_store).record_operator_decline(
            verified_user_id=self.user_id,
            thread_id="thread-1",
            actor_scope_hash="5" * 64,
            client_request_id="source-link-operator",
            issued_at="2026-08-04T12:00:03.000000Z",
        )
        operator_state = {
            "store": operator_store,
            "generation": operator["generations"][0],
        }
        operator_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._link(operator_state)
        self.assertEqual([], self._writes(operator_store))

        wrong_work = self._seed_linkable()
        ledger_ref = self._b1_reference(
            wrong_work["store"],
            "sourceWorkLedgers",
        )
        wrong_work["store"].data[ledger_ref.path]["entries"][0][
            "workKey"
        ] = "f" * 64
        before = deepcopy(wrong_work["store"].data)
        wrong_work["store"].events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._link(wrong_work)
        self.assertEqual(before, wrong_work["store"].data)
        self.assertEqual([], self._writes(wrong_work["store"]))

    def test_source_link_accepts_contact_fanout_outside_b1_thread_rows(self):
        store = self._store()
        bundle, source_binding = self.fixture._seed_prerequisites(
            store,
            owner_kind="contact_optout",
        )
        self.assertEqual(
            [self.fixture.first],
            [item["rowId"] for item in source_binding["rowBindings"]],
        )
        self.fixture._seed_row(store, self.fixture.second)
        claim, generation, claimed_head = self.fixture._install_contact_owner(
            store,
            self.fixture.second,
        )
        b2_settlement = self.module.build_owner_settlement_document(
            generation_document=generation,
            claim_set_document=claim,
            fencing_token=claimed_head["fencingToken"],
            outcome="contact_optout",
            settled_at="2026-08-04T12:00:03.000000Z",
        )
        head = self.module._build_settlement_advanced_head(
            expected_head=claimed_head,
            generation_document=generation,
            settlement_document=b2_settlement,
        )
        self.fixture._settlement_reference(
            store,
            self.fixture.second,
            1,
        ).create(b2_settlement)
        self.fixture._row_references(store, self.fixture.second)[1].set(
            head,
            merge=False,
        )
        b1 = self._seed_b1_settlement(store, bundle)
        state = {
            "store": store,
            "binding": source_binding,
            "bundle": b1,
            "claim": claim,
            "generation": generation,
            "b2Settlement": b2_settlement,
            "head": head,
        }
        self.row_id = self.fixture.second
        store.events.clear()

        result = self._link(state)

        self.assertEqual("linked", result["disposition"])
        self.assertEqual(
            self.fixture.second,
            result["sourceSettlementLink"]["rowId"],
        )

    def test_source_link_rejects_rehashed_b1_thread_bound_to_different_rows(self):
        state = self._seed_linkable()
        store = state["store"]
        different_thread_id = "different-thread"
        self.fixture._seed_row(store, self.fixture.second)
        different_binding = self.module.build_thread_row_binding_document(
            user_scope_hash=self.scope,
            thread_id=different_thread_id,
            client_id="client-1",
            row_ids=[self.fixture.second],
            primary_row_id=self.fixture.second,
            created_at=self.fixture.binding_at,
        )
        self._user_reference(store).collection(
            "threadRowBindings"
        ).document(different_thread_id).create(different_binding)

        identity_ref = self._b1_reference(
            store,
            "sourceIdentities",
        )
        identity = deepcopy(store.data[identity_ref.path])
        identity["threadId"] = different_thread_id
        store.data[identity_ref.path] = identity

        settlement_ref = self._b1_reference(
            store,
            "sourceSettlements",
        )
        settlement = deepcopy(store.data[settlement_ref.path])
        settlement["identityHash"] = _independent_b1_hash(
            {
                "hashKind": "source-settlement-identity-v1",
                "schemaVersion": identity["schemaVersion"],
                "canonicalSourceId": identity["canonicalSourceId"],
                "creationHash": identity["creationHash"],
                "threadId": identity["threadId"],
            }
        )
        thread_head = settlement["threadHeadBinding"]
        thread_head["headHash"] = _independent_b1_hash(
            {
                "hashKind": "thread-transition-head-v1",
                "schemaVersion": 1,
                "threadId": identity["threadId"],
                "threadHeadRevision": thread_head["threadHeadRevision"],
                "activeOwnerKey": thread_head["ownerKey"],
                "activeOwnerKind": thread_head["ownerKind"],
                "activeCanonicalSourceId": thread_head[
                    "canonicalSourceId"
                ],
                "activeGeneration": thread_head["generation"],
                "activeState": "active",
            }
        )
        settlement["settlementHash"] = self._independent_settlement_hash(
            settlement
        )
        store.data[settlement_ref.path] = settlement
        before = deepcopy(store.data)
        store.events.clear()

        with self.assertRaises(self.module.RowAuthorityConflict):
            self._link(state)

        self.assertEqual(before, store.data)
        self.assertEqual([], self._writes(store))

    def test_b1_canonical_source_snapshot_selection_owner_ledger_or_hard_evidence_drift_writes_nothing(self):
        cases = (
            ("canonical_source", "sourceIdentities", "canonicalSourceId", "source-2", "b1_source"),
            ("snapshot", "sourceClassifications", "snapshotImmutableHash", "1" * 64, "b1_source"),
            ("selection", "sourceClassifications", "selectionHash", "2" * 64, "b1_source"),
            ("owner", "sourceTransitionOwners", "ownerDecisionHash", "3" * 64, "b1_source"),
            ("ledger", "sourceWorkLedgers", "ledgerHash", "4" * 64, "b1_source"),
            ("hard_evidence", "sourceClassifications", "deterministicEvidenceHash", "5" * 64, "contact_fanout"),
        )
        for name, collection, field, value, origin in cases:
            with self.subTest(case=name):
                state = self._seed_linkable(origin=origin)
                store = state["store"]
                reference = self._b1_reference(store, collection)
                store.data[reference.path][field] = value
                before = deepcopy(store.data)
                store.events.clear()

                with self.assertRaises(self.module.RowAuthorityConflict):
                    self._link(state)

                self.assertEqual(before, store.data)
                self.assertEqual([], self._writes(store))

    def test_source_link_creates_immutable_link_and_cas_advances_head(self):
        state = self._seed_linkable()
        store = state["store"]
        expected_link = self._expected_link(state)
        expected_head = self.module._build_source_link_advanced_head(
            expected_head=state["head"],
            source_link_document=expected_link,
        )
        store.events.clear()

        result = self._link(state)

        self.assertEqual("linked", result["disposition"])
        self.assertEqual(expected_link, result["sourceSettlementLink"])
        self.assertEqual(expected_head, result["head"])
        self.assertEqual(
            [
                ("create", self._link_reference(store).path, expected_link, False),
                ("set", self._head_reference(store).path, expected_head, False),
            ],
            self._writes(store),
        )

    def test_source_link_performs_zero_writes_to_every_b1_collection(self):
        state = self._seed_linkable()
        store = state["store"]
        b1_collections = {
            "sourceIdentities",
            "sourceClassifications",
            "sourceTransitionOwners",
            "sourceWorkLedgers",
            "sourceSettlements",
        }
        b1_before = {
            path: deepcopy(document)
            for path, document in store.data.items()
            if any(f"/{collection}/" in path for collection in b1_collections)
        }
        store.events.clear()

        self._link(state)

        self.assertEqual(
            b1_before,
            {
                path: document
                for path, document in store.data.items()
                if any(
                    f"/{collection}/" in path
                    for collection in b1_collections
                )
            },
        )
        self.assertTrue(
            all(
                not any(f"/{collection}/" in event[1] for collection in b1_collections)
                for event in self._writes(store)
            )
        )

    def test_exact_existing_source_link_is_zero_write_even_after_later_head_link(self):
        state = self._seed_linkable()
        store = state["store"]
        first = self._link(state)
        later = self._synthetic_link()
        self._link_reference(store, generation=2).create(later)
        later_head = self._advance_head_to_link(store, later)
        store.events.clear()

        replay = self._link(state)

        self.assertEqual("already_applied", replay["disposition"])
        self.assertEqual(first["sourceSettlementLink"], replay["sourceSettlementLink"])
        self.assertEqual(later_head, replay["head"])
        self.assertEqual([], self._writes(store))

    def test_new_source_link_after_older_link_validates_old_pointer_then_advances(self):
        state = self._seed_linkable()
        store = state["store"]
        older = self._synthetic_link(
            linked_at="2026-08-04T12:00:03.000000Z"
        )
        self._link_reference(store, generation=2).create(older)
        older_head = self._advance_head_to_link(store, older)
        expected_link = self._expected_link(
            state,
            linked_at="2026-08-04T12:00:05.000000Z",
        )
        expected_head = self.module._build_source_link_advanced_head(
            expected_head=older_head,
            source_link_document=expected_link,
        )
        store.events.clear()

        result = self._link(
            state,
            linked_at="2026-08-04T12:00:05.000000Z",
        )

        self.assertEqual("linked", result["disposition"])
        self.assertEqual(expected_link, result["sourceSettlementLink"])
        self.assertEqual(expected_head, result["head"])
        queries = [event for event in store.events if event[0] == "query"]
        self.assertEqual(2, len(queries))
        self.assertEqual(
            (
                ("rowId", "==", self.row_id),
            ),
            queries[0][2],
        )
        self.assertEqual(("generation",), queries[0][3])
        self.assertEqual(("DESCENDING",), queries[0][4])
        self.assertEqual(
            (("sourceSettlementLinkHash", "==", older["sourceSettlementLinkHash"]),),
            queries[1][2],
        )

    def test_existing_link_with_missing_or_invalid_head_pointer_is_ambiguous(self):
        for pointer in (None, "f" * 64):
            with self.subTest(pointer=pointer):
                state = self._seed_linkable()
                store = state["store"]
                expected = self._expected_link(state)
                self._link_reference(store).create(expected)
                if pointer is not None:
                    self._set_head_pointer(store, pointer)
                before = deepcopy(store.data)
                store.events.clear()

                with self.assertRaises(self.module.RowAuthorityAmbiguous):
                    self._link(state)

                self.assertEqual(before, store.data)
                self.assertEqual([], self._writes(store))

    def test_later_link_hash_query_requires_one_same_row_non_earlier_exact_result(self):
        for later_at in (
            "2026-08-04T12:00:04.000000Z",
            "2026-08-04T12:00:05.000000Z",
        ):
            with self.subTest(later_at=later_at):
                state = self._seed_linkable()
                store = state["store"]
                candidate = self._expected_link(state)
                later = self._synthetic_link(linked_at=later_at)
                self._link_reference(store).create(candidate)
                self._link_reference(store, generation=2).create(later)
                later_head = self._advance_head_to_link(store, later)
                store.events.clear()

                result = self._link(state)

                self.assertEqual("already_applied", result["disposition"])
                self.assertEqual(later_head, result["head"])
                self.assertEqual([], self._writes(store))
                queries = [event for event in store.events if event[0] == "query"]
                self.assertEqual(
                    [
                        (
                            "query",
                            self._user_reference(store)
                            .collection("rowOwnerSettlements")
                            .path,
                            (("rowId", "==", self.row_id),),
                            ("generation",),
                            ("DESCENDING",),
                        ),
                        (
                            "query",
                            self._user_reference(store)
                            .collection("rowSourceSettlementLinks")
                            .path,
                            (
                                (
                                    "sourceSettlementLinkHash",
                                    "==",
                                    later["sourceSettlementLinkHash"],
                                ),
                            ),
                            ("__name__",),
                        )
                    ],
                    queries,
                )

    def test_later_link_query_zero_duplicate_cross_row_or_malformed_result_is_ambiguous(self):
        for case in ("zero", "duplicate", "cross_row", "malformed"):
            with self.subTest(case=case):
                state = self._seed_linkable()
                store = state["store"]
                candidate = self._expected_link(state)
                self._link_reference(store).create(candidate)
                pointer = "f" * 64
                if case == "duplicate":
                    later = self._synthetic_link()
                    pointer = later["sourceSettlementLinkHash"]
                    self._link_reference(store, generation=2).create(later)
                    self._link_reference(store, generation=3).create(later)
                elif case == "cross_row":
                    later = self._synthetic_link(row_id=self.fixture.second)
                    pointer = later["sourceSettlementLinkHash"]
                    self._link_reference(
                        store,
                        row_id=self.fixture.second,
                        generation=2,
                    ).create(later)
                elif case == "malformed":
                    pointer = "e" * 64
                    self._link_reference(store, generation=2).create(
                        {"sourceSettlementLinkHash": pointer}
                    )
                self._set_head_pointer(store, pointer)
                before = deepcopy(store.data)
                store.events.clear()

                with self.assertRaises(self.module.RowAuthorityAmbiguous):
                    self._link(state)

                self.assertEqual(before, store.data)
                self.assertEqual([], self._writes(store))

        existing_wrong_direction = self._seed_linkable()
        existing_store = existing_wrong_direction["store"]
        future_candidate = self._expected_link(
            existing_wrong_direction,
            linked_at="2026-08-04T12:00:05.000000Z",
        )
        earlier_current = self._synthetic_link(
            linked_at="2026-08-04T12:00:04.000000Z"
        )
        self._link_reference(existing_store).create(future_candidate)
        self._link_reference(existing_store, generation=2).create(
            earlier_current
        )
        self._advance_head_to_link(existing_store, earlier_current)
        existing_before = deepcopy(existing_store.data)
        existing_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._link(
                existing_wrong_direction,
                linked_at="2026-08-04T12:00:05.000000Z",
            )
        self.assertEqual(existing_before, existing_store.data)
        self.assertEqual([], self._writes(existing_store))

        new_wrong_direction = self._seed_linkable()
        new_store = new_wrong_direction["store"]
        later_current = self._synthetic_link(
            linked_at="2026-08-04T12:00:05.000000Z"
        )
        self._link_reference(new_store, generation=2).create(later_current)
        self._advance_head_to_link(new_store, later_current)
        new_before = deepcopy(new_store.data)
        new_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._link(
                new_wrong_direction,
                linked_at="2026-08-04T12:00:04.000000Z",
            )
        self.assertEqual(new_before, new_store.data)
        self.assertEqual([], self._writes(new_store))

    def test_later_link_query_completes_before_any_source_link_or_head_write(self):
        state = self._seed_linkable()
        store = state["store"]
        older = self._synthetic_link(
            linked_at="2026-08-04T12:00:03.000000Z"
        )
        self._link_reference(store, generation=2).create(older)
        self._advance_head_to_link(store, older)
        store.events.clear()

        self._link(state, linked_at="2026-08-04T12:00:05.000000Z")

        query_index = next(
            index
            for index, event in enumerate(store.events)
            if event[0] == "query"
        )
        write_indexes = [
            index
            for index, event in enumerate(store.events)
            if event[0] in {"create", "set", "update", "delete"}
        ]
        self.assertEqual(2, len(write_indexes))
        self.assertLess(query_index, min(write_indexes))

    def test_source_link_time_must_follow_b1_b2_settlements_and_current_head(self):
        equal = self._seed_linkable()
        result = self._link(
            equal,
            linked_at="2026-08-04T12:00:03.000000Z",
        )
        self.assertEqual("linked", result["disposition"])

        b1_late = self._seed_linkable(
            b1_settled_at=datetime(
                2026,
                8,
                4,
                12,
                0,
                5,
                tzinfo=timezone.utc,
            )
        )
        b1_before = deepcopy(b1_late["store"].data)
        b1_late["store"].events.clear()
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self._link(b1_late)
        self.assertEqual(b1_before, b1_late["store"].data)
        self.assertEqual([], self._writes(b1_late["store"]))

        b2_late = self._seed_linkable(
            b1_settled_at=datetime(
                2026,
                8,
                4,
                12,
                0,
                1,
                tzinfo=timezone.utc,
            )
        )
        b2_before = deepcopy(b2_late["store"].data)
        b2_late["store"].events.clear()
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self._link(
                b2_late,
                linked_at="2026-08-04T12:00:02.000000Z",
            )
        self.assertEqual(b2_before, b2_late["store"].data)
        self.assertEqual([], self._writes(b2_late["store"]))

        head_late = self._seed_linkable()
        self._set_head_pointer(
            head_late["store"],
            None,
            updated_at="2026-08-04T12:00:05.000000Z",
        )
        head_before = deepcopy(head_late["store"].data)
        head_late["store"].events.clear()
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self._link(head_late)
        self.assertEqual(head_before, head_late["store"].data)
        self.assertEqual([], self._writes(head_late["store"]))

        generation_before_claim = self._rewrite_b2_lineage(
            self._seed_linkable(),
            claim_created_at="2026-08-04T12:00:05.000000Z",
            generation_created_at="2026-08-04T12:00:02.000000Z",
            settled_at="2026-08-04T12:00:03.000000Z",
        )
        generation_before = deepcopy(
            generation_before_claim["store"].data
        )
        generation_before_claim["store"].events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._link(
                generation_before_claim,
                linked_at="2026-08-04T12:00:06.000000Z",
            )
        self.assertEqual(
            generation_before,
            generation_before_claim["store"].data,
        )
        self.assertEqual(
            [],
            self._writes(generation_before_claim["store"]),
        )

        claim_before_row = self._rewrite_b2_lineage(
            self._seed_linkable(),
            claim_created_at="2026-08-04T11:30:00.000000Z",
            generation_created_at="2026-08-04T11:30:00.000000Z",
            settled_at="2026-08-04T11:31:00.000000Z",
        )
        claim_before = deepcopy(claim_before_row["store"].data)
        claim_before_row["store"].events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._link(claim_before_row)
        self.assertEqual(claim_before, claim_before_row["store"].data)
        self.assertEqual([], self._writes(claim_before_row["store"]))

        overbound_generation = self._rewrite_b2_lineage(
            self._seed_linkable(),
            generation_number=4,
        )
        overbound_before = deepcopy(overbound_generation["store"].data)
        overbound_generation["store"].events.clear()
        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._link(overbound_generation)
        self.assertEqual(
            overbound_before,
            overbound_generation["store"].data,
        )
        self.assertEqual(
            [],
            self._writes(overbound_generation["store"]),
        )

    def test_source_link_drift_is_conflict_and_partial_readback_is_ambiguous(self):
        drift = self._seed_linkable()
        drifted_link = self._expected_link(drift)
        drifted_link["b1SettlementHash"] = "f" * 64
        self._link_reference(drift["store"]).create(drifted_link)
        before = deepcopy(drift["store"].data)
        drift["store"].events.clear()
        with self.assertRaises(self.module.RowAuthorityConflict):
            self._link(drift)
        self.assertEqual(before, drift["store"].data)
        self.assertEqual([], self._writes(drift["store"]))

        unrelated_result = self._seed_linkable()
        unrelated_head = deepcopy(unrelated_result["head"])
        unrelated_head.update(
            {
                "latestSourceSettlementLinkHash": "f" * 64,
                "updatedAt": self.linked_at,
            }
        )
        unrelated_head = self.fixture._rehash_head(unrelated_head)
        with self.assertRaises(self.module.RowAuthorityConfigError):
            self.module._source_settlement_link_result(
                disposition="already_applied",
                source_settlement_link=self._expected_link(
                    unrelated_result
                ),
                head=unrelated_head,
            )

        split_head = self._seed_linkable()
        split_store = split_head["store"]
        claimed_head = deepcopy(split_head["head"])
        claimed_head.update(
            {
                "state": "claimed",
                "leaseOwnerHash": "a" * 64,
                "leaseUntil": "2026-08-04T12:05:00.000000Z",
                "latestSettlementHash": None,
                "effectiveSettlementHash": None,
                "updatedAt": self.linked_at,
            }
        )
        self._head_reference(split_store).set(
            self.fixture._rehash_head(claimed_head),
            merge=False,
        )
        split_before = deepcopy(split_store.data)
        split_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._link(split_head)
        self.assertEqual(split_before, split_store.data)
        self.assertEqual([], self._writes(split_store))

        split_latest = self._seed_linkable()
        split_latest_store = split_latest["store"]
        unrelated_latest = deepcopy(split_latest["head"])
        unrelated_latest.update(
            {
                "latestSettlementHash": "f" * 64,
                "updatedAt": self.linked_at,
            }
        )
        self._head_reference(split_latest_store).set(
            self.fixture._rehash_head(unrelated_latest),
            merge=False,
        )
        split_latest_before = deepcopy(split_latest_store.data)
        split_latest_store.events.clear()
        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._link(split_latest)
        self.assertEqual(split_latest_before, split_latest_store.data)
        self.assertEqual([], self._writes(split_latest_store))

        partial = self._seed_linkable()

        def partial_executor(transaction, callback):
            transaction._begin()
            callback(transaction)
            operation, reference, payload, merge = transaction._operations[0]
            transaction._rollback()
            self.assertEqual(("create", False), (operation, merge))
            reference.create(payload)
            raise RuntimeError("partial source-link apply")

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._link(partial, executor=partial_executor)

        query_race = self._seed_linkable()
        query_store = query_race["store"]
        candidate = self._expected_link(query_race)
        current = self._synthetic_link()
        self._link_reference(query_store).create(candidate)
        self._link_reference(query_store, generation=2).create(current)
        self._advance_head_to_link(query_store, current)

        def duplicate_query_executor(transaction, callback):
            transaction._begin()
            callback(transaction)
            transaction._rollback()
            self._link_reference(query_store, generation=3).create(
                current
            )
            raise RuntimeError("source-link query changed before readback")

        with self.assertRaises(self.module.RowAuthorityAmbiguous):
            self._link(query_race, executor=duplicate_query_executor)


if __name__ == "__main__":
    unittest.main()
