import hashlib
import json
import copy
import unittest
from datetime import datetime, timezone

from email_automation import pending_responses, send_permits, system_health
from tests.test_pending_responses import FakeDoc
from tests import test_system_health as health_fixtures
from tests import test_post_settlement_completion_obligations as completion_fixtures


COLLECTION = "pendingResponseCompletionObligations"


class PendingCompletionHealthTests(unittest.TestCase):
    def _record(self, *, status="owed", malformed=False):
        immutable = {
            "version": 1,
            "kind": "pending_response_client_completion",
            "userId": "uid-1",
            "clientId": "client-1",
            "threadId": "thread-1",
            "pendingDocumentId": "thread-1",
            "sourceGraphMessageId": "source-1",
            "pendingEnvelopeHash": "a" * 64,
            "permitId": "graph-send-permit-1",
            "permitImmutableHash": "b" * 64,
            "sentEvidenceHash": "c" * 64,
            "completeClientAfterReply": True,
        }
        immutable_hash = hashlib.sha256(
            json.dumps(immutable, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        obligation_id = f"pending-completion-{immutable_hash}"
        now = datetime(2026, 8, 2, 20, 0, tzinfo=timezone.utc)
        return health_fixtures.FakeHealthDoc({
            "version": 1,
            "obligationId": obligation_id,
            "immutable": immutable,
            "immutableHash": "d" * 64 if malformed else immutable_hash,
            "status": status,
            "completionOutcome": (
                "client_completed" if status == "settled" else None
            ),
            "settledAt": now if status == "settled" else None,
            "createdAt": now,
            "updatedAt": now,
        }, doc_id=obligation_id)

    def _health(self, records):
        fs = health_fixtures.FakeFirestore(
            {
                "outbox": 0,
                "deadLetterQueue": 0,
                "pendingResponses": 0,
                "processingFailures": 0,
                "terminalGraphSendReviews": 0,
                "threads": 0,
            },
            docs_by_collection={COLLECTION: records},
        )
        return system_health.collect_user_health(
            "uid-1",
            fs_client=fs,
            token_state={"status": "healthy"},
            graph_state={"status": "healthy"},
        )

    def test_owed_completion_obligation_is_visible_and_degrades_health(self):
        payload = self._health([self._record()])

        self.assertIn(COLLECTION, payload["queues"])
        self.assertEqual(1, payload["queues"][COLLECTION])
        self.assertEqual("warning", payload["status"])

    def test_malformed_or_over_bound_completion_visibility_fails_closed(self):
        for label, records in (
            ("malformed", [self._record(malformed=True)]),
            ("over_bound", [self._record() for _ in range(501)]),
        ):
            with self.subTest(case=label):
                payload = self._health(records)
                self.assertIn(COLLECTION, payload["queues"])
                self.assertEqual(-1, payload["queues"][COLLECTION])
                self.assertIn(COLLECTION, payload["countErrors"])
                self.assertEqual("error", payload["status"])

    def test_valid_settled_completion_tombstone_is_not_active(self):
        payload = self._health([self._record(status="settled")])

        self.assertIn(COLLECTION, payload["queues"])
        self.assertEqual(0, payload["queues"][COLLECTION])
        self.assertEqual("healthy", payload["status"])

    def test_settled_history_does_not_consume_owed_visibility_bound(self):
        records = [self._record(status="settled") for _ in range(501)]
        records.append(self._record())

        payload = self._health(records)

        self.assertEqual(1, payload["queues"][COLLECTION])
        self.assertEqual("warning", payload["status"])


class PendingCompletionWorkerBoundTests(unittest.TestCase):
    def test_settled_history_does_not_starve_one_owed_replay(self):
        fixture = completion_fixtures.PendingSuccessCompletionObligationTests(
            methodName="runTest"
        )
        fake_fs, _pending, _data, _capability, _evidence, obligation = (
            fixture._seed_manual_obligation("settled-history")
        )
        for index in range(501):
            fake_fs.collections[COLLECTION].docs.append(FakeDoc(
                f"settled-history-{index}",
                {"status": "settled"},
            ))

        states, send, exact, heuristic, continuation = fixture._process(
            fake_fs,
            completion_effect=True,
        )

        fixture._last_state(states, "healthy")
        self.assertEqual("settled", obligation.to_dict()["status"])
        send.assert_not_called()
        exact.assert_not_called()
        heuristic.assert_not_called()
        continuation.assert_not_called()

    def test_clientless_accepted_send_settles_not_required_without_client_access(self):
        fixture = completion_fixtures.PendingSuccessCompletionObligationTests(
            methodName="runTest"
        )
        data = fixture._pending_data("clientless")
        data.pop("clientId")
        pending_doc = FakeDoc(data["threadId"], copy.deepcopy(data))
        fake_fs = fixture._firestore(pending_doc)
        thread_ref = fake_fs.collections["threads"].document(data["threadId"])
        thread_ref._doc._data["clientId"] = fixture.CLIENT_ID

        with fixture._clients_patch(fake_fs):
            claim = pending_responses._claim_pending_response_for_send(
                fixture.USER_ID,
                pending_doc,
                copy.deepcopy(data),
            )
            capability = pending_responses._final_pending_response_send_fence(
                fixture.USER_ID,
                pending_doc,
                copy.deepcopy(data),
                claim,
            )
            evidence = fixture._prepare_and_accept(capability)
            pending_responses._cas_pending_success(
                fixture.USER_ID,
                pending_doc,
                copy.deepcopy(data),
                claim,
                capability,
                evidence,
            )

        obligations = fixture._all_obligations(fake_fs)
        self.assertEqual(1, len(obligations))
        immutable = obligations[0].to_dict()["immutable"]
        self.assertEqual("", immutable["clientId"])
        self.assertIs(immutable["completeClientAfterReply"], False)

        states, send, exact, heuristic, continuation = fixture._process(
            fake_fs,
            completion_effect=AssertionError(
                "clientless obligation must not access client completion"
            ),
        )

        fixture._last_state(states, "healthy")
        self.assertEqual("settled", obligations[0].to_dict()["status"])
        self.assertEqual(
            "not_required",
            obligations[0].to_dict()["completionOutcome"],
        )
        send.assert_not_called()
        exact.assert_not_called()
        heuristic.assert_not_called()
        continuation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
