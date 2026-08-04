"""B2-only fakes layered on the retained B1 Firestore fake."""

from __future__ import annotations

from tests.source_coordinator_fakes import FakeFirestore, FakeTransaction


class BoundedFakeTransaction(FakeTransaction):
    """Reject an oversized B2 transaction before barriers or writes apply."""

    def _apply_buffered(self):
        ceiling = self._store.max_writes_per_commit
        write_count = len(self._operations)
        if write_count > ceiling:
            self._store.events.append(
                ("commit_refused_write_ceiling", write_count, ceiling)
            )
            raise RuntimeError(
                f"fake transaction exceeds {ceiling}-write ceiling"
            )
        return super()._apply_buffered()


class BoundedFakeFirestore(FakeFirestore):
    """FakeFirestore with a required positive per-commit write ceiling."""

    def __init__(self, *, max_writes_per_commit=400):
        if (
            isinstance(max_writes_per_commit, bool)
            or type(max_writes_per_commit) is not int
            or max_writes_per_commit < 1
        ):
            raise ValueError(
                "fake write ceiling must be a positive integer"
            )
        super().__init__()
        self.max_writes_per_commit = max_writes_per_commit

    def transaction(self, max_attempts=5):
        return BoundedFakeTransaction(self, max_attempts=max_attempts)
