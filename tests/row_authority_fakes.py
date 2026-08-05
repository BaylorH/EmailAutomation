"""B2-only fakes layered on the retained B1 Firestore fake."""

from __future__ import annotations

from copy import deepcopy

from tests.source_coordinator_fakes import (
    FakeFirestore,
    FakeTransaction,
    FakeTransactionAborted,
)


MARKER_KEY = "sitesift_row_id_v1"
MARKER_VISIBILITY = "DOCUMENT"
ROW_LOCATION_TYPE = "ROW"
ROW_DIMENSION = "ROWS"


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


def run_bounded_transaction(transaction, callback):
    """Execute a B2 fake transaction with fresh-snapshot abort retries."""

    if not isinstance(transaction, BoundedFakeTransaction):
        raise TypeError("bounded transaction executor requires the B2 fake")
    if not callable(callback):
        raise TypeError("bounded transaction callback must be callable")

    for retry_id in range(transaction._max_attempts):
        transaction._begin(retry_id=retry_id)
        try:
            result = callback(transaction)
            transaction._commit()
            return result
        except FakeTransactionAborted:
            if transaction.in_progress:
                transaction._rollback()
            if retry_id + 1 >= transaction._max_attempts:
                raise
        except Exception:
            if transaction.in_progress:
                transaction._rollback()
            raise
    raise AssertionError("bounded transaction retry loop exhausted")


class MarkerAwareSheet:
    """Provider-shaped row metadata that follows its owning fake row."""

    def __init__(self, *, sheet_id, rows=()):
        if type(sheet_id) is not int or sheet_id < 0:
            raise ValueError("fake sheet ID must be a nonnegative integer")
        if type(rows) not in {list, tuple}:
            raise TypeError("fake sheet rows must be a list or tuple")
        self.sheet_id = sheet_id
        self._rows = []
        self._metadata_by_row_token = {}
        self._next_row_token = 1
        self._next_metadata_id = 1
        for cells in rows:
            self._rows.append(self._new_row(cells))

    @staticmethod
    def _normalize_cells(cells):
        if type(cells) not in {list, tuple} or any(
            type(value) is not str for value in cells
        ):
            raise TypeError("fake row cells must be a list or tuple of strings")
        return tuple(cells)

    @staticmethod
    def _require_index(value, *, length, allow_end=False):
        maximum = length if allow_end else length - 1
        if type(value) is not int or value < 0 or value > maximum:
            raise IndexError("fake row index is out of bounds")
        return value

    def _new_row(self, cells):
        row = {
            "token": self._next_row_token,
            "cells": self._normalize_cells(cells),
        }
        self._next_row_token += 1
        self._metadata_by_row_token[row["token"]] = []
        return row

    def _metadata_response(self, metadata, *, provider_row_index):
        return {
            "metadataId": metadata["metadataId"],
            "metadataKey": metadata["metadataKey"],
            "metadataValue": metadata["metadataValue"],
            "location": {
                "locationType": ROW_LOCATION_TYPE,
                "dimensionRange": {
                    "sheetId": self.sheet_id,
                    "dimension": ROW_DIMENSION,
                    "startIndex": provider_row_index,
                    "endIndex": provider_row_index + 1,
                },
            },
            "visibility": metadata["visibility"],
        }

    def __len__(self):
        return len(self._rows)

    def row_cells(self, provider_row_index):
        index = self._require_index(
            provider_row_index,
            length=len(self._rows),
        )
        return tuple(self._rows[index]["cells"])

    def insert_row(self, provider_row_index, cells):
        index = self._require_index(
            provider_row_index,
            length=len(self._rows),
            allow_end=True,
        )
        self._rows.insert(index, self._new_row(cells))

    def move_row(self, source_index, destination_index):
        source = self._require_index(source_index, length=len(self._rows))
        destination = self._require_index(
            destination_index,
            length=len(self._rows),
        )
        row = self._rows.pop(source)
        self._rows.insert(destination, row)

    def sort_rows(self, *, key):
        if not callable(key):
            raise TypeError("fake row sort key must be callable")
        self._rows.sort(key=lambda row: key(tuple(row["cells"])))

    def delete_row(self, provider_row_index):
        index = self._require_index(
            provider_row_index,
            length=len(self._rows),
        )
        row = self._rows.pop(index)
        self._metadata_by_row_token.pop(row["token"], None)
        return tuple(row["cells"])

    def create_row_marker(self, *, provider_row_index, row_id):
        index = self._require_index(
            provider_row_index,
            length=len(self._rows),
        )
        if type(row_id) is not str or not row_id:
            raise ValueError("fake row marker value must be a nonempty string")
        metadata = {
            "metadataId": self._next_metadata_id,
            "metadataKey": MARKER_KEY,
            "metadataValue": row_id,
            "visibility": MARKER_VISIBILITY,
        }
        self._next_metadata_id += 1
        token = self._rows[index]["token"]
        self._metadata_by_row_token[token].append(metadata)
        return self._metadata_response(metadata, provider_row_index=index)

    def search_row_markers(self, row_id):
        if type(row_id) is not str or not row_id:
            raise ValueError("fake row marker lookup must be a nonempty string")
        matches = []
        for index, row in enumerate(self._rows):
            for metadata in self._metadata_by_row_token[row["token"]]:
                if metadata["metadataValue"] == row_id:
                    matches.append(
                        self._metadata_response(
                            metadata,
                            provider_row_index=index,
                        )
                    )
        matches.sort(
            key=lambda item: (
                item["location"]["dimensionRange"]["startIndex"],
                item["metadataId"],
            )
        )
        return tuple(deepcopy(matches))

    def restart(self):
        restarted = type(self)(sheet_id=self.sheet_id)
        restarted._rows = deepcopy(self._rows)
        restarted._metadata_by_row_token = deepcopy(
            self._metadata_by_row_token
        )
        restarted._next_row_token = self._next_row_token
        restarted._next_metadata_id = self._next_metadata_id
        return restarted
