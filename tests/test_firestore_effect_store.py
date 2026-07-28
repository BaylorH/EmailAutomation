import copy
import json
import threading
import unittest
from types import SimpleNamespace

from email_automation.effect_gateway import (
    AttemptLimits,
    AuthorityState,
    ProviderEffectRequest,
    ReceiptState,
)
from email_automation.firestore_effect_store import FirestoreEffectReceiptStore


class FakeSnapshot:
    def __init__(self, data):
        self.exists = data is not None
        self._data = copy.deepcopy(data)

    def to_dict(self):
        return copy.deepcopy(self._data)


class FakeDocumentReference:
    def __init__(self, client, path):
        self._client = client
        self.path = path

    def collection(self, name):
        return FakeCollectionReference(self._client, f"{self.path}/{name}")

    def get(self, transaction=None):
        if transaction is not None:
            return transaction.get(self)
        return FakeSnapshot(self._client.documents.get(self.path))


class FakeCollectionReference:
    def __init__(self, client, path):
        self._client = client
        self.path = path

    def document(self, document_id):
        return FakeDocumentReference(
            self._client,
            f"{self.path}/{document_id}",
        )


class FakeTransaction:
    def __init__(self, client):
        self._client = client

    def get(self, reference):
        return FakeSnapshot(self._client.documents.get(reference.path))

    def set(self, reference, payload, merge=False):
        if merge and reference.path in self._client.documents:
            current = copy.deepcopy(self._client.documents[reference.path])
            current.update(copy.deepcopy(payload))
            self._client.documents[reference.path] = current
            return
        self._client.documents[reference.path] = copy.deepcopy(payload)


class FakeFirestore:
    def __init__(self):
        self.documents = {}
        self.lock = threading.RLock()

    def collection(self, name):
        return FakeCollectionReference(self, name)

    def transaction(self):
        return FakeTransaction(self)


def serialized_transactional(client):
    def decorate(callback):
        def run(transaction):
            with client.lock:
                return callback(transaction)

        return run

    return decorate


def effect_request(
    effect_key="thread-1:reply-1",
    *,
    run_id="run-1",
    user_id="uid-exact",
    client_id="client-exact",
    content=None,
):
    return ProviderEffectRequest.create(
        run_id=run_id,
        user_id=user_id,
        provider="graph",
        effect_type="mail.reply",
        effect_key=effect_key,
        authority_client_id=client_id,
        content=content
        or {
            "to": ["broker-secret@example.test"],
            "body": "token=secret private body",
        },
    )


class FirestoreEffectStoreContractTests(unittest.TestCase):
    def setUp(self):
        self.firestore = FakeFirestore()
        self.authority_calls = []

        def authority_reader(user_id, client_id, *, firestore_client):
            self.authority_calls.append(
                (user_id, client_id, firestore_client)
            )
            return SimpleNamespace(
                state="allow",
                reason="",
                metadata={"terminal": False},
            )

        self.store = FirestoreEffectReceiptStore(
            self.firestore,
            authority_reader=authority_reader,
            transactional_runner=serialized_transactional(self.firestore),
            server_timestamp="SERVER_TIMESTAMP",
            owner_token_factory=lambda: "owner-exact",
        )
        self.limits = AttemptLimits(
            max_attempts=3,
            max_per_run=2,
            max_per_user=2,
            max_per_provider=2,
        )

    def test_authority_uses_exact_user_and_client_identity_bytes(self):
        request = effect_request()

        decision = self.store.read_authoritative_state(request)

        self.assertEqual(decision.state, AuthorityState.ACTIVE)
        self.assertEqual(
            self.authority_calls,
            [("uid-exact", "client-exact", self.firestore)],
        )
        with self.assertRaisesRegex(ValueError, "exact non-empty string"):
            effect_request(user_id=" uid-exact")
        with self.assertRaisesRegex(ValueError, "exact non-empty string"):
            effect_request(client_id="client-exact ")

    def test_atomic_reservation_allows_only_one_concurrent_owner(self):
        request = effect_request()
        reservations = []

        def reserve():
            reservations.append(
                self.store.reserve_attempt(request, self.limits)
            )

        contenders = [threading.Thread(target=reserve) for _ in range(2)]
        for contender in contenders:
            contender.start()
        for contender in contenders:
            contender.join(timeout=5)

        self.assertFalse(any(contender.is_alive() for contender in contenders))
        self.assertEqual(
            sum(reservation.acquired for reservation in reservations),
            1,
        )
        acquired = next(
            reservation
            for reservation in reservations
            if reservation.acquired
        )
        self.assertEqual(acquired.owner_token, "owner-exact")
        self.assertGreater(acquired.version, 0)
        self.assertEqual(acquired.receipt.state, ReceiptState.CLAIMED)
        self.assertEqual(acquired.receipt.attempts, 1)

    def test_atomic_reservation_enforces_run_cap_across_users(self):
        limits = AttemptLimits(
            max_attempts=3,
            max_per_run=1,
            max_per_user=1,
            max_per_provider=1,
        )
        requests = [
            effect_request("effect-a", user_id="user-a"),
            effect_request("effect-b", user_id="user-b"),
        ]
        stores = [
            FirestoreEffectReceiptStore(
                self.firestore,
                authority_reader=lambda *_args, **_kwargs: None,
                transactional_runner=serialized_transactional(
                    self.firestore
                ),
                server_timestamp="SERVER_TIMESTAMP",
                owner_token_factory=lambda: "owner-a",
            ),
            FirestoreEffectReceiptStore(
                self.firestore,
                authority_reader=lambda *_args, **_kwargs: None,
                transactional_runner=serialized_transactional(
                    self.firestore
                ),
                server_timestamp="SERVER_TIMESTAMP",
                owner_token_factory=lambda: "owner-b",
            ),
        ]
        reservations = []

        contenders = [
            threading.Thread(
                target=lambda index=index: reservations.append(
                    stores[index].reserve_attempt(requests[index], limits)
                )
            )
            for index in range(2)
        ]
        for contender in contenders:
            contender.start()
        for contender in contenders:
            contender.join(timeout=5)

        self.assertEqual(
            sum(reservation.acquired for reservation in reservations),
            1,
        )
        blocked = next(
            reservation
            for reservation in reservations
            if not reservation.acquired
        )
        self.assertEqual(blocked.receipt.state, ReceiptState.BLOCKED)
        self.assertEqual(blocked.receipt.reason, "run_cap_reached")

    def test_monotonic_versioned_transition_never_regresses_success(self):
        request = effect_request()
        reservation = self.store.reserve_attempt(request, self.limits)
        accepted = self.store.transition(
            request,
            ReceiptState.PROVIDER_ACCEPTED,
            provider_reference="provider_ref_" + ("a" * 64),
            expected_owner=reservation.owner_token,
            expected_version=reservation.version,
        )
        succeeded = self.store.transition(
            request,
            ReceiptState.SUCCEEDED,
            provider_reference=accepted.provider_reference,
            expected_owner=accepted.claim_owner,
            expected_version=accepted.version,
        )

        stale = self.store.transition(
            request,
            ReceiptState.RECONCILIATION_REQUIRED,
            reason="stale_finalizer",
            provider_reference=accepted.provider_reference,
            expected_owner=accepted.claim_owner,
            expected_version=accepted.version,
        )

        self.assertEqual(succeeded.state, ReceiptState.SUCCEEDED)
        self.assertEqual(stale.state, ReceiptState.SUCCEEDED)
        self.assertEqual(
            self.store.load_receipt(request).state,
            ReceiptState.SUCCEEDED,
        )

    def test_operator_receipt_is_readable_and_contains_no_raw_effect_data(self):
        request = effect_request(
            effect_key="thread-secret-id:message-secret-id",
        )
        reservation = self.store.reserve_attempt(request, self.limits)

        operator_receipt = self.store.read_operator_receipt(request)
        serialized = json.dumps(operator_receipt, sort_keys=True)

        self.assertEqual(operator_receipt["state"], "claimed")
        self.assertEqual(operator_receipt["provider"], "graph")
        self.assertEqual(operator_receipt["effectType"], "mail.reply")
        self.assertEqual(
            set(operator_receipt),
            {
                "effectId",
                "contentIdempotencyKey",
                "state",
                "attempts",
                "reason",
                "providerReference",
                "provider",
                "effectType",
                "version",
                "updatedAt",
            },
        )
        self.assertNotIn(reservation.owner_token, serialized)
        for secret in (
            "uid-exact",
            "client-exact",
            "thread-secret-id",
            "message-secret-id",
            "broker-secret@example.test",
            "token=secret",
            "private body",
        ):
            self.assertNotIn(secret, serialized)


if __name__ == "__main__":
    unittest.main()
