"""Provider-free B2-B row ownership contracts."""

from __future__ import annotations

import hashlib
import importlib
import json
import unittest
from copy import deepcopy
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


if __name__ == "__main__":
    unittest.main()
