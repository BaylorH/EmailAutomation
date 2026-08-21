"""Metering-wiring tests for the paid OpenAI call sites.

Each test drives the real code path with a mocked OpenAI client and a mocked
`track_openai_usage_safely`, then asserts the metering call fired with the
expected operation + model. Mirrors the style of
tests/test_openai_usage_tracking.py (FakeFirestore) plus runtime mocking.
"""

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Env vars must exist before importing scheduler_runner (module-level guards).
os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault("AZURE_API_APP_ID", "test-client-id")
os.environ.setdefault("AZURE_API_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("FIREBASE_API_KEY", "test-firebase-api-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-api-key")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)


class FakeDocRef:
    def __init__(self):
        self.writes = []
        self._collections = {}

    def collection(self, name):
        return self._collections.setdefault(name, FakeCollectionRef())

    def set(self, payload, merge=False):
        self.writes.append((payload, merge))


class FakeCollectionRef:
    def __init__(self):
        self.added = []
        self._docs = {}

    def document(self, doc_id):
        return self._docs.setdefault(doc_id, FakeDocRef())

    def add(self, payload):
        self.added.append(payload)


class FakeFirestore:
    def __init__(self):
        self._collections = {}

    def collection(self, name):
        return self._collections.setdefault(name, FakeCollectionRef())


# Imported at COLLECTION time, deliberately. pytest imports every test module
# before running any test, so this binding is taken while sys.modules is still
# clean. Importing inside the test body instead makes this test the first
# casualty of an unrelated suite-wide problem: some earlier test replaces
# sys.modules["google.cloud"] with a non-package object, after which
# scheduler_runner's `from google.cloud.firestore_v1 import FieldFilter` raises
# ModuleNotFoundError. That pollution already breaks tests/test_scheduler_user_listing.py
# and tests/test_dashboard_escalation_actions.py on an untouched tree, so it is a
# pre-existing environment fault, not something this module should re-discover.
import scheduler_runner


def _fake_response(payload_json):
    """A minimal Responses-API-shaped object."""
    return SimpleNamespace(
        output_text=json.dumps(payload_json),
        usage=SimpleNamespace(input_tokens=100, output_tokens=40, total_tokens=140),
        id="resp_fake_123",
    )


class SchedulerRunnerMeteringTests(unittest.TestCase):
    def test_propose_sheet_updates_meters_with_expected_operation_and_model(self):
        header = ["Property Address", "City", "Total SF"]
        rowvals = ["1 Randolph Ct", "Evans", ""]
        fake_client = SimpleNamespace(
            responses=SimpleNamespace(
                create=MagicMock(return_value=_fake_response({"updates": [], "events": []}))
            )
        )

        with patch.object(scheduler_runner, "build_conversation_payload", return_value=[]), \
                patch.object(scheduler_runner, "client", fake_client), \
                patch.object(scheduler_runner, "_fs", FakeFirestore()), \
                patch.object(scheduler_runner, "track_openai_usage_safely") as track:
            result = scheduler_runner.propose_sheet_updates(
                uid="user-123",
                client_id="client-456",
                email="broker@example.com",
                sheet_id="sheet-abc",
                header=header,
                rownum=3,
                rowvals=rowvals,
                thread_id="thread-789",
            )

        self.assertIsNotNone(result)
        fake_client.responses.create.assert_called_once()
        track.assert_called_once()
        kwargs = track.call_args.kwargs
        self.assertEqual(kwargs["operation"], "ai.propose_sheet_updates")
        self.assertEqual(kwargs["model"], scheduler_runner.OPENAI_ASSISTANT_MODEL)
        self.assertEqual(kwargs["user_id"], "user-123")
        self.assertEqual(kwargs["client_id"], "client-456")
        self.assertEqual(kwargs["thread_id"], "thread-789")

class AiProcessingDryRunMeteringTests(unittest.TestCase):
    """A dry run still BILLS. dry_run only skips the sheetChangeLog write.

    budget_guard sums the metered rollups, so any billed-but-unmetered call makes
    the guard under-count and overshoot its limit. This asserts the extraction
    metering is not sitting inside an `if not dry_run:` block. It is a source
    assertion rather than a behavioural one because driving the full extraction
    path would require standing up the whole prompt/attachment pipeline; the
    thing that can silently regress is the gate, so the gate is what is pinned.
    """

    def test_extraction_metering_is_not_gated_behind_not_dry_run(self):
        from pathlib import Path

        source = Path(__file__).resolve().parent.parent / "email_automation" / "ai_processing.py"
        text = source.read_text()

        marker = 'operation="ai.extract_sheet_updates"'
        self.assertIn(marker, text, "extraction metering call site not found")

        # Walk back from the metering call to its enclosing statement and prove
        # no `if not dry_run:` guard sits between the paid call and the meter.
        before = text[: text.index(marker)]
        call_idx = before.rindex("track_openai_usage_safely(")
        window = before[call_idx - 400 : call_idx]
        self.assertNotIn(
            "if not dry_run:",
            window,
            "extraction metering is gated behind `if not dry_run:` — a dry run still "
            "bills, so this under-counts spend and lets budget_guard overshoot",
        )


if __name__ == "__main__":
    unittest.main()
