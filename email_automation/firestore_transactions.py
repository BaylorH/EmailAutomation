"""Supported Firestore transaction execution with local-test compatibility."""

from typing import Callable, TypeVar

from google.cloud.firestore_v1.transaction import (
    Transaction as FirestoreTransaction,
    transactional,
)


_Result = TypeVar("_Result")


def run_firestore_transaction(
    firestore_client,
    operation: Callable[[object], _Result],
    *,
    max_attempts: int | None = None,
    read_only: bool = False,
) -> _Result:
    """Run one callback in an official transaction or a strict local fake.

    Production ``Transaction`` instances must be activated and committed by
    Google's supported runner.  The repository's small synchronous fakes do
    not implement that lifecycle, so the compatibility path invokes the same
    commit-free callback and then commits the fake once.
    """
    if type(read_only) is not bool:
        raise ValueError("transaction read_only must be a boolean")
    transaction_options = {"read_only": read_only} if read_only else {}
    if max_attempts is not None:
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("transaction max_attempts must be a positive integer")
        transaction_options["max_attempts"] = max_attempts
    if not transaction_options:
        transaction = firestore_client.transaction()
    else:
        try:
            transaction = firestore_client.transaction(**transaction_options)
        except TypeError:
            # The repository's local fakes intentionally expose only the
            # minimal zero-argument transaction factory.
            transaction = firestore_client.transaction()
    if isinstance(transaction, FirestoreTransaction):
        return transactional(operation)(transaction)

    result = operation(transaction)
    if not read_only:
        transaction.commit()
    return result
