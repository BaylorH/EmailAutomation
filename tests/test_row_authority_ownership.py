"""Provider-free B2-B row ownership contracts."""

from __future__ import annotations

import hashlib
import importlib
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
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


if __name__ == "__main__":
    unittest.main()
