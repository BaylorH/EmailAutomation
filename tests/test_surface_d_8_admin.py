"""Surface D-8 -- admin.usage_readonly state-permutation coverage.

Closes the Base-V1 rubric ``needs_fixture`` cells for the read-only OpenAI
usage admin view (feature ``admin.usage_readonly``).

The Usage admin view is a READ-ONLY report: it renders month-to-date spend
aggregates (per user / day / workflow) straight from the Firestore documents
that ``record_openai_usage`` persists under each user's own subcollection tree.
It has no send path, no template rendering, no draft, and no retry surface.

Because of that, three of the four send-path state columns are genuinely
NOT APPLICABLE and are reported as such by the orchestrator (bad_placeholder,
manual_continuation, duplicate_retry). The one column that DOES have a
meaningful read-only analog is ``wrong_recipient`` -- for a per-user usage
report the safety question is "can the caller ever see a different principal's
usage or PII?". That is a real read-scope / isolation boundary, so it gets a
real test here (replacing the prior BORROWED GREEN that reused a results-admin
UI-gate test).

Only the Firestore datastore boundary is faked; ``record_openai_usage`` and its
metadata-scrubbing / path-scoping logic run for real. ZERO live sends.
"""

import os

os.environ.setdefault("E2E_TEST_MODE", "true")

import unittest
from datetime import datetime, timezone

from google.cloud.firestore import Increment

from email_automation.openai_usage import record_openai_usage


# ---------------------------------------------------------------------------
# In-memory Firestore double. Models ONLY the datastore boundary; it records
# the full document path of every write so tests can assert per-user scoping.
# ---------------------------------------------------------------------------
class _FakeDoc:
    def __init__(self, path, store):
        self._path = path
        self._store = store  # the shared registry dict

    def collection(self, name):
        return _FakeCollection(f"{self._path}/{name}", self._store)

    def set(self, payload, merge=False):
        existing = self._store["docs"].setdefault(self._path, {})
        if merge:
            existing.update(payload)
        else:
            self._store["docs"][self._path] = dict(payload)


class _FakeCollection:
    def __init__(self, path, store):
        self._path = path
        self._store = store

    def document(self, doc_id):
        return _FakeDoc(f"{self._path}/{doc_id}", self._store)

    def add(self, event):
        # Auto-id: append an event and record its parent collection path so
        # tests can enumerate exactly which user's tree it landed under.
        self._store["events"].append({"path": self._path, "event": event})
        return None


class _FakeFirestore:
    def __init__(self):
        # events: list of {path, event}; docs: path -> merged rollup payload
        self._store = {"events": [], "docs": {}}

    def collection(self, name):
        return _FakeCollection(name, self._store)

    # --- read helpers used by assertions (simulate the read-only view) ------
    def events_under_user(self, user_id):
        prefix = f"users/{user_id}/"
        return [
            e["event"]
            for e in self._store["events"]
            if e["path"].startswith(prefix)
        ]

    def all_event_paths(self):
        return [e["path"] for e in self._store["events"]]

    def rollup_paths(self):
        return list(self._store["docs"].keys())


_USAGE = {
    "input_tokens": 1200,
    "output_tokens": 300,
    "total_tokens": 1500,
    "input_tokens_details": {"cached_tokens": 200},
}


class UsageReadonlyReadScopeTest(unittest.TestCase):
    """wrong_recipient (read-only analog): the usage report is strictly
    scoped to the calling principal -- one user's spend can never surface
    under another user's tree, and broker PII / prompt text never reaches the
    persisted rows the read-only view renders."""

    def test_usage_is_partitioned_per_user_no_cross_recipient_read(self):
        db = _FakeFirestore()

        record_openai_usage(
            db=db,
            user_id="operatorA",
            operation="conversation_handling",
            model="gpt-5.2",
            usage=_USAGE,
            now=datetime(2026, 7, 2, tzinfo=timezone.utc),
        )
        record_openai_usage(
            db=db,
            user_id="operatorB",
            operation="conversation_handling",
            model="gpt-5.2",
            usage=_USAGE,
            now=datetime(2026, 7, 2, tzinfo=timezone.utc),
        )

        a_events = db.events_under_user("operatorA")
        b_events = db.events_under_user("operatorB")

        # Each principal sees exactly one event -- its own.
        self.assertEqual(len(a_events), 1)
        self.assertEqual(len(b_events), 1)
        self.assertEqual(a_events[0]["userId"], "operatorA")
        self.assertEqual(b_events[0]["userId"], "operatorB")

        # Read-scope invariant: NOTHING belonging to A is reachable from B's
        # tree and vice-versa. A read-only view rooted at users/{caller} thus
        # cannot leak another operator's usage.
        for path in db.all_event_paths():
            self.assertRegex(path, r"^users/(operatorA|operatorB)/openaiUsageEvents$")
        self.assertNotIn(
            "operatorB",
            {e["userId"] for e in a_events},
            "operatorB usage must never appear under operatorA's tree",
        )

        # Daily rollup docs are likewise partitioned per user.
        for rollup_path in db.rollup_paths():
            self.assertRegex(rollup_path, r"^users/(operatorA|operatorB)/")

    def test_persisted_event_strips_broker_email_and_prompt_text(self):
        """The read-only view renders what is persisted. Sensitive metadata
        (broker email, prompt / draft text) must be scrubbed at write time so
        it is impossible for the usage report to expose another party's PII."""
        db = _FakeFirestore()

        record_openai_usage(
            db=db,
            user_id="operatorA",
            operation="conversation_handling",
            model="gpt-5.2",
            usage=_USAGE,
            metadata={
                "email": "broker@example.com",      # sensitive -> stripped
                "prompt": "secret system prompt",    # sensitive -> stripped
                "currentEmailDraft": "draft body",   # sensitive -> stripped
                "clientName": "Acme Realty",         # benign -> kept
                "attempt": 2,                        # benign -> kept
            },
            now=datetime(2026, 7, 2, tzinfo=timezone.utc),
        )

        events = db.events_under_user("operatorA")
        self.assertEqual(len(events), 1)
        meta = events[0]["metadata"]

        # Privacy boundary holds: no broker email / prompt / draft text.
        for leaked in ("email", "prompt", "currentEmailDraft"):
            self.assertNotIn(leaked, meta)
        # Benign, non-identifying fields survive so the report stays useful.
        self.assertEqual(meta.get("clientName"), "Acme Realty")
        self.assertEqual(meta.get("attempt"), 2)

    def test_read_only_view_records_no_write_to_foreign_user(self):
        """Negative control: recording usage for operatorA writes ZERO
        documents anywhere under operatorB, so a compromised/incorrect caller
        id cannot silently attribute spend to -- or read -- another account."""
        db = _FakeFirestore()

        record_openai_usage(
            db=db,
            user_id="operatorA",
            operation="map_and_tour",
            model="gpt-4o-mini",
            usage=_USAGE,
            client_id="clientX",
            now=datetime(2026, 7, 2, tzinfo=timezone.utc),
        )

        foreign = [p for p in db.all_event_paths() if "operatorB" in p]
        foreign += [p for p in db.rollup_paths() if "operatorB" in p]
        self.assertEqual(foreign, [], "no operatorA call may touch operatorB's tree")

        # Everything written is anchored under operatorA (including the nested
        # per-client rollup that the read-only view drills into).
        for path in db.all_event_paths() + db.rollup_paths():
            self.assertTrue(
                path.startswith("users/operatorA/"),
                f"unexpected write outside caller's tree: {path}",
            )


if __name__ == "__main__":
    unittest.main()
