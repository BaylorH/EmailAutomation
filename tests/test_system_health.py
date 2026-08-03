import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import send_permits, system_health


class FakeCollection:
    def __init__(self, count=0, docs=None):
        self.count = count
        self.docs = docs

    def limit(self, count):
        return self

    def where(self, *, filter):
        return FakeFilteredCollection(self, filters=(filter,))

    def stream(self):
        if self.docs is not None:
            return self.docs
        return [object() for _ in range(self.count)]


class FakeFilteredCollection:
    def __init__(self, source, *, filters=(), query_limit=None):
        self.source = source
        self.filters = tuple(filters)
        self.query_limit = query_limit

    def where(self, *, filter):
        return FakeFilteredCollection(
            self.source,
            filters=(*self.filters, filter),
            query_limit=self.query_limit,
        )

    def limit(self, count):
        return FakeFilteredCollection(
            self.source,
            filters=self.filters,
            query_limit=count,
        )

    def stream(self):
        docs = list(self.source.stream())
        for field_filter in self.filters:
            docs = [
                doc
                for doc in docs
                if getattr(doc, "to_dict", lambda: {})().get(
                    field_filter.field_path
                ) == field_filter.value
            ]
        if self.query_limit is not None:
            docs = docs[:self.query_limit]
        return docs


class FakeHealthDoc:
    def __init__(self, data, *, doc_id=None, reference=None):
        self._data = dict(data)
        self.id = doc_id
        self.reference = reference

    def to_dict(self):
        return dict(self._data)


class FakeDocRef:
    def __init__(self, root, path):
        self.root = root
        self.path = tuple(path)

    def collection(self, name):
        if name in self.root.docs_by_collection:
            return FakeCollection(docs=self.root.docs_by_collection[name])
        if name in self.root.counts:
            return FakeCollection(self.root.counts[name])
        return FakeNode(self.root, list(self.path) + ["collection", name])

    def set(self, data, merge=False):
        self.root.set_calls.append((self.path, data, merge))


class FakeNode:
    def __init__(self, root, path=None):
        self.root = root
        self.path = path or []

    def collection(self, name):
        key = name
        if key in self.root.docs_by_collection:
            return FakeCollection(docs=self.root.docs_by_collection[key])
        if key in self.root.counts:
            return FakeCollection(self.root.counts[key])
        return FakeNode(self.root, self.path + ["collection", name])

    def document(self, name):
        return FakeDocRef(self.root, self.path + ["document", name])


class FakeFirestore:
    def __init__(self, counts, docs_by_collection=None):
        self.counts = dict(counts)
        self.counts.setdefault("graphSendDraftReviews", 0)
        self.counts.setdefault(
            "pendingResponseCompletionObligations",
            0,
        )
        self.docs_by_collection = docs_by_collection or {}
        self.set_calls = []

    def collection(self, name):
        return FakeNode(self, ["collection", name])


class _BoomStream:
    """Iterating this raises, simulating a Firestore read outage mid-stream."""

    def __iter__(self):
        raise RuntimeError("firestore read failed")


class SystemHealthTests(unittest.TestCase):
    def test_pending_retained_draft_review_is_visible_in_canonical_health_queue(self):
        now = datetime(2026, 8, 2, 18, 0, tzinfo=timezone.utc)
        fs = FakeFirestore(
            {
                "outbox": 0,
                "deadLetterQueue": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "terminalGraphSendReviews": 0,
                "threads": 0,
            },
            docs_by_collection={
                "graphSendDraftReviews": [
                    FakeHealthDoc({
                        "threadId": "thread-1",
                        "clientId": "client-1",
                        "pendingDocumentId": "pending-1",
                        "status": "manual_review",
                        "source": "pendingGraphSendProtocol",
                        "authoritative": True,
                        "alreadySent": False,
                        "providerSendStarted": False,
                        "sendOutcomeUnknown": False,
                        "retryAllowed": False,
                        "automaticDeleteAttempted": False,
                        "graphSendPermitId": "graph-send-permit-1",
                        "graphSendPermitHash": "a" * 64,
                        "sourceGraphMessageId": "source-1",
                        "draftId": "draft-1",
                        "draftMutationState": "prepared",
                        "draftResolutionEvidenceHash": "b" * 64,
                        "failureReason": "Retained draft requires operator review.",
                        "createdAt": now,
                        "updatedAt": now,
                    }),
                ],
            },
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual("warning", payload["status"])
        self.assertEqual(1, payload["queues"]["graphSendDraftReviews"])

    def test_malformed_or_unreadable_draft_review_queue_fails_health_closed(self):
        malformed = FakeHealthDoc({
            "status": "manual_review",
            "source": "pendingGraphSendProtocol",
            "retryAllowed": True,
        })
        cases = {
            "malformed": [malformed],
            "malformed_resolved": [
                FakeHealthDoc({
                    "status": "resolved_not_actionable",
                    "resolution": "retained_draft_not_actionable",
                    "retryAllowed": True,
                    "providerSendStarted": False,
                    "automaticDeleteAttempted": False,
                    "originalReviewEvidenceHash": "arbitrary-nonempty",
                    "operatorSettlementId": "arbitrary-nonempty",
                    "resolvedBy": "arbitrary-nonempty",
                }),
            ],
            "unreadable": _BoomStream(),
        }
        for label, docs in cases.items():
            with self.subTest(case=label):
                fs = FakeFirestore(
                    {
                        "outbox": 0,
                        "deadLetterQueue": 0,
                        "pendingResponses": 0,
                        "processingFailures": 0,
                        "terminalGraphSendReviews": 0,
                        "threads": 0,
                    },
                    docs_by_collection={"graphSendDraftReviews": docs},
                )

                payload = system_health.collect_user_health(
                    "uid-1",
                    fs_client=fs,
                    token_state={"status": "healthy"},
                    graph_state={"status": "healthy"},
                )

                self.assertEqual("error", payload["status"])
                self.assertEqual(
                    -1,
                    payload["queues"]["graphSendDraftReviews"],
                )
                self.assertIn(
                    "graphSendDraftReviews",
                    payload["countErrors"],
                )

    def test_resolved_draft_review_health_requires_exact_audit_and_permit_linkage(self):
        now = datetime(2026, 8, 2, 18, 0, tzinfo=timezone.utc)

        def linked_health_count(*, drift=None):
            user_ref = MagicMock(name="user_ref")
            user_ref.path = "users/uid-1"
            review_collection = MagicMock(name="draft_review_collection")
            review_collection.limit.return_value = review_collection
            review_ref = MagicMock(name="review_ref")
            review_ref.path = (
                "users/uid-1/graphSendDraftReviews/"
                "pending-graph-send-permit-1"
            )
            review_ref.id = "pending-graph-send-permit-1"
            audit_ref = MagicMock(name="audit_ref")
            audit_ref.path = (
                "users/uid-1/graphSendDraftReviewSettlements/"
                "draft-review-settlement-1"
            )
            audit_ref.id = "draft-review-settlement-1"
            original = {
                "threadId": "thread-1",
                "clientId": "client-1",
                "pendingDocumentId": "pending-1",
                "status": "manual_review",
                "source": "pendingGraphSendProtocol",
                "authoritative": True,
                "alreadySent": False,
                "providerSendStarted": False,
                "sendOutcomeUnknown": False,
                "retryAllowed": False,
                "automaticDeleteAttempted": False,
                "failureReason": "Retained draft requires operator review.",
                "graphSendPermitId": "graph-send-permit-1",
                "graphSendPermitHash": "a" * 64,
                "sourceGraphMessageId": "source-1",
                "preparedEnvelopeHash": "b" * 64,
                "draftId": "draft-1",
                "draftMutationState": "prepared",
                "draftResolutionEvidenceHash": "c" * 64,
                "createdAt": now,
                "updatedAt": now,
            }
            original_hash = send_permits._stable_evidence_hash(original)
            resolved = {
                **original,
                "status": "resolved_not_actionable",
                "resolution": "retained_draft_not_actionable",
                "retryAllowed": True,
                "originalReviewEvidenceHash": original_hash,
                "operatorSettlementAuditRef": audit_ref,
                "operatorSettlementId": "draft-review-settlement-1",
                "resolvedBy": "authenticated-operator-uid",
                "operatorReason": "The retained draft was manually discarded.",
                "resolvedAt": now,
                "updatedAt": now,
            }
            review_hash = send_permits._stable_evidence_hash(resolved)
            audit = {
                "version": 1,
                "settlementId": "draft-review-settlement-1",
                "action": "confirm_retained_draft_not_actionable",
                "operatorId": "authenticated-operator-uid",
                "operatorReason": "The retained draft was manually discarded.",
                "threadId": "thread-1",
                "clientId": "client-1",
                "pendingDocumentId": "pending-1",
                "graphSendPermitId": "graph-send-permit-1",
                "graphSendPermitHash": "a" * 64,
                "reviewEvidenceHash": original_hash,
                "reviewEvidenceRef": review_ref,
                "resolution": "retained_draft_not_actionable",
                "providerSendStarted": False,
                "automaticDeleteAttempted": False,
                "retryAllowed": True,
                "resolvedAt": now,
            }
            permit = {
                "permitId": "graph-send-permit-1",
                "immutableHash": "a" * 64,
                "issuerKind": "pending_response",
                "issuerDocumentId": "pending-1",
                "threadId": "thread-1",
                "clientId": "client-1",
                "status": "settled_draft_review_resolved",
                "draftReviewRequired": False,
                "draftReviewEvidenceRef": review_ref,
                "draftReviewEvidenceHash": review_hash,
                "pendingReconciliationEvidenceHash": original_hash,
                "operatorSettlementAuditRef": audit_ref,
                "operatorSettlementAuditHash": (
                    send_permits._stable_evidence_hash(audit)
                ),
                "operatorOriginalReconciliationEvidenceHash": original_hash,
                "operatorResolvedReviewEvidenceHash": review_hash,
                "operatorResolution": "retained_draft_not_actionable",
            }
            if drift == "audit":
                audit["operatorReason"] = "drifted audit reason"
            elif drift == "permit":
                permit["operatorResolvedReviewEvidenceHash"] = "d" * 64
            elif drift == "timestamp":
                resolved["createdAt"] = "not-an-authoritative-timestamp"

            review_snapshot = FakeHealthDoc(
                resolved,
                doc_id=review_ref.id,
                reference=review_ref,
            )
            review_snapshot.exists = True
            review_collection.stream.return_value = [review_snapshot]
            review_collection.document.return_value = review_ref
            audit_snapshot = FakeHealthDoc(audit)
            audit_snapshot.exists = True
            audit_ref.get.return_value = audit_snapshot
            permit_snapshot = FakeHealthDoc(permit)
            permit_snapshot.exists = True
            permit_ref = MagicMock(name="permit_ref")
            permit_ref.get.return_value = permit_snapshot
            permit_collection = MagicMock(name="permit_collection")
            permit_collection.document.return_value = permit_ref
            thread_ref = MagicMock(name="thread_ref")
            thread_ref.path = "users/uid-1/threads/thread-1"
            thread_ref.collection.return_value = permit_collection
            threads_collection = MagicMock(name="threads_collection")
            threads_collection.document.return_value = thread_ref

            def collection(name):
                return {
                    "graphSendDraftReviews": review_collection,
                    "threads": threads_collection,
                }[name]

            user_ref.collection.side_effect = collection
            return system_health._count_active_pending_draft_reviews(user_ref)

        self.assertEqual(0, linked_health_count())
        self.assertEqual(-1, linked_health_count(drift="audit"))
        self.assertEqual(-1, linked_health_count(drift="permit"))
        self.assertEqual(-1, linked_health_count(drift="timestamp"))

    def test_collect_user_health_warns_on_backlog_counts(self):
        fs = FakeFirestore({
            "outbox": 2,
            "deadLetterQueue": 1,
            "pendingResponses": 0,
            "processingFailures": 3,
            "terminalGraphSendReviews": 0,
            "threads": 0,
        })
        now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy", "source": "cached_access_token"},
            graph_state={"status": "healthy"},
            now=now,
        )

        self.assertEqual("warning", payload["status"])
        self.assertEqual(2, payload["queues"]["outbox"])
        self.assertEqual(1, payload["queues"]["deadLetterQueue"])
        self.assertEqual(3, payload["queues"]["processingFailures"])
        self.assertEqual("healthy", payload["token"]["status"])
        self.assertEqual("healthy", payload["graph"]["status"])

    def test_collect_user_health_ignores_resolved_dead_letters(self):
        fs = FakeFirestore(
            {
                "outbox": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "terminalGraphSendReviews": 0,
                "threads": 0,
            },
            docs_by_collection={
                "deadLetterQueue": [
                    FakeHealthDoc({"status": "requeued", "recoveryStatus": "requeued"}),
                    FakeHealthDoc({"status": "discarded", "resolution": "discard"}),
                    FakeHealthDoc({"status": "dead_lettered", "failureReason": "still needs review"}),
                ],
            },
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual("warning", payload["status"])
        self.assertEqual(1, payload["queues"]["deadLetterQueue"])

    def test_terminal_send_review_stays_visible_after_generic_dead_letter_discard(self):
        fs = FakeFirestore(
            {
                "outbox": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "threads": 0,
            },
            docs_by_collection={
                "deadLetterQueue": [
                    FakeHealthDoc({"status": "discarded", "resolution": "discard"}),
                ],
                "terminalGraphSendReviews": [
                    FakeHealthDoc({
                        "status": "needs_reconciliation",
                        "source": "terminalGraphSendProtocol",
                        "retryAllowed": False,
                    }),
                    FakeHealthDoc({"status": "acknowledged"}),
                    FakeHealthDoc({"status": "reconciled_sent"}),
                ],
            },
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual("warning", payload["status"])
        self.assertEqual(0, payload["queues"]["deadLetterQueue"])
        self.assertEqual(2, payload["queues"]["terminalGraphSendReviews"])

    def test_owed_terminal_thread_warns_with_every_queue_and_review_empty(self):
        fs = FakeFirestore(
            {
                "outbox": 0,
                "deadLetterQueue": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "terminalGraphSendReviews": 0,
            },
            docs_by_collection={
                "threads": [
                    FakeHealthDoc({
                        "clientId": "client-1",
                        "status": "stopped",
                        "terminalReplyOwed": True,
                        "terminalSagaKey": "saga-1",
                    }),
                ],
            },
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual("warning", payload["status"])
        self.assertEqual(1, payload["queues"]["terminalProtocolThreads"])
        self.assertEqual(0, payload["queues"]["terminalGraphSendReviews"])

    def test_unreadable_terminal_thread_scan_fails_health_closed(self):
        fs = FakeFirestore(
            {
                "outbox": 0,
                "deadLetterQueue": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "terminalGraphSendReviews": 0,
            },
            docs_by_collection={"threads": _BoomStream()},
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual("error", payload["status"])
        self.assertEqual(-1, payload["queues"]["terminalProtocolThreads"])
        self.assertEqual(["terminalProtocolThreads"], payload["countErrors"])

    def test_terminal_thread_scan_bound_cannot_hide_later_active_obligation(self):
        clean_threads = [
            FakeHealthDoc({"status": "completed"}) for _ in range(500)
        ]
        fs = FakeFirestore(
            {
                "outbox": 0,
                "deadLetterQueue": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "terminalGraphSendReviews": 0,
            },
            docs_by_collection={
                "threads": clean_threads + [
                    FakeHealthDoc({"terminalNotificationOwed": True}),
                ],
            },
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual("error", payload["status"])
        self.assertEqual(-1, payload["queues"]["terminalProtocolThreads"])
        self.assertIn("terminalProtocolThreads", payload["countErrors"])

    def test_dead_letter_scan_bound_cannot_hide_later_unresolved_work(self):
        resolved = [
            FakeHealthDoc({"status": "discarded"}) for _ in range(500)
        ]
        fs = FakeFirestore(
            {
                "outbox": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "terminalGraphSendReviews": 0,
                "threads": 0,
            },
            docs_by_collection={
                "deadLetterQueue": resolved
                + [FakeHealthDoc({"status": "dead_lettered"})],
            },
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual("error", payload["status"])
        self.assertEqual(-1, payload["queues"]["deadLetterQueue"])
        self.assertIn("deadLetterQueue", payload["countErrors"])

    def test_terminal_review_scan_bound_cannot_hide_later_unresolved_work(self):
        resolved = [
            FakeHealthDoc({"status": "reconciled_sent"}) for _ in range(500)
        ]
        fs = FakeFirestore(
            {
                "outbox": 0,
                "deadLetterQueue": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "threads": 0,
            },
            docs_by_collection={
                "terminalGraphSendReviews": resolved
                + [FakeHealthDoc({"status": "needs_reconciliation"})],
            },
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual("error", payload["status"])
        self.assertEqual(-1, payload["queues"]["terminalGraphSendReviews"])
        self.assertIn("terminalGraphSendReviews", payload["countErrors"])

    def test_collect_user_health_errors_on_token_failure(self):
        fs = FakeFirestore({
            "outbox": 0,
            "deadLetterQueue": 0,
            "pendingResponses": 0,
            "processingFailures": 0,
            "terminalGraphSendReviews": 0,
            "threads": 0,
        })

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "error", "error": "silent_auth_failed"},
            graph_state={"status": "unknown"},
        )

        self.assertEqual("error", payload["status"])
        self.assertEqual("silent_auth_failed", payload["token"]["error"])

    def test_unreadable_queue_count_cannot_report_healthy(self):
        # Firestore read outage: every queue count fails (-1). Token + graph are
        # healthy. Health must NOT report healthy — a queue we cannot read may be
        # hiding an unbounded backlog of stuck / misdirected sends (fail closed).
        fs = FakeFirestore(
            {},
            docs_by_collection={
                "outbox": _BoomStream(),
                "deadLetterQueue": _BoomStream(),
                "pendingResponses": _BoomStream(),
                "processingFailures": _BoomStream(),
                "terminalGraphSendReviews": _BoomStream(),
                "threads": _BoomStream(),
            },
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual(-1, payload["queues"]["outbox"])
        self.assertNotEqual("healthy", payload["status"])
        self.assertEqual("error", payload["status"])
        # Per-queue count-error flags surfaced so the outage is observable.
        self.assertIn("outbox", payload["countErrors"])
        self.assertIn("deadLetterQueue", payload["countErrors"])

    def test_partial_count_error_cannot_report_healthy(self):
        # Only one queue fails to read; the rest are empty. Still must not be healthy.
        fs = FakeFirestore(
            {
                "outbox": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "terminalGraphSendReviews": 0,
                "threads": 0,
            },
            docs_by_collection={
                "deadLetterQueue": _BoomStream(),
            },
        )

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual(-1, payload["queues"]["deadLetterQueue"])
        self.assertEqual("error", payload["status"])
        self.assertEqual(["deadLetterQueue"], payload["countErrors"])

    def test_count_error_severity_env_downgrade_to_warning(self):
        # Operators may downgrade count-error severity to warning, but never to
        # healthy. Absence of the env var defaults to the fail-closed (error) path.
        fs = FakeFirestore(
            {
                "outbox": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "terminalGraphSendReviews": 0,
                "threads": 0,
            },
            docs_by_collection={"deadLetterQueue": _BoomStream()},
        )
        prev = os.environ.get("HEALTH_COUNT_ERROR_SEVERITY")
        os.environ["HEALTH_COUNT_ERROR_SEVERITY"] = "warning"
        try:
            payload = system_health.collect_user_health(
                "uid-1",
                fs_client=fs,
                token_state={"status": "healthy"},
                graph_state={"status": "healthy"},
            )
        finally:
            if prev is None:
                os.environ.pop("HEALTH_COUNT_ERROR_SEVERITY", None)
            else:
                os.environ["HEALTH_COUNT_ERROR_SEVERITY"] = prev

        self.assertEqual("warning", payload["status"])
        self.assertEqual(["deadLetterQueue"], payload["countErrors"])

    def test_no_count_error_leaves_healthy_intact(self):
        # Regression: clean reads with healthy token/graph stay healthy and
        # surface an empty countErrors list.
        fs = FakeFirestore({
            "outbox": 0,
            "deadLetterQueue": 0,
            "pendingResponses": 0,
            "processingFailures": 0,
            "terminalGraphSendReviews": 0,
            "threads": 0,
        })

        payload = system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

        self.assertEqual("healthy", payload["status"])
        self.assertEqual([], payload["countErrors"])

    def test_write_user_health_replaces_dashboard_snapshot(self):
        fs = FakeFirestore({
            "outbox": 0,
            "deadLetterQueue": 0,
            "pendingResponses": 0,
            "processingFailures": 0,
            "terminalGraphSendReviews": 0,
            "threads": 0,
        })
        payload = {"status": "healthy", "queues": {}}

        system_health.write_user_health("uid-1", payload, fs_client=fs)

        self.assertEqual(1, len(fs.set_calls))
        self.assertEqual(
            ("collection", "users", "document", "uid-1", "collection", "systemHealth", "document", "emailAutomation"),
            fs.set_calls[0][0],
        )
        self.assertFalse(fs.set_calls[0][2])


if __name__ == "__main__":
    unittest.main()
