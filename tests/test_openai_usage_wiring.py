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


def _fake_response(payload_json):
    """A minimal Responses-API-shaped object."""
    return SimpleNamespace(
        output_text=json.dumps(payload_json),
        usage=SimpleNamespace(input_tokens=100, output_tokens=40, total_tokens=140),
        id="resp_fake_123",
    )


class SchedulerRunnerMeteringTests(unittest.TestCase):
    def test_propose_sheet_updates_meters_with_expected_operation_and_model(self):
        import scheduler_runner

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
        self.assertEqual(kwargs["request_id"], "resp_fake_123")
        self.assertIsNotNone(kwargs["usage"])


class ColumnConfigMeteringTests(unittest.TestCase):
    def _run_match(self, db, user_id):
        from email_automation import column_config

        fake_client = SimpleNamespace(
            responses=SimpleNamespace(
                create=MagicMock(return_value=_fake_response({}))
            )
        )
        with patch("email_automation.clients.client", fake_client), \
                patch("email_automation.openai_usage.track_openai_usage_safely") as track:
            column_config._ai_match_columns(
                ["Some Header"], ["total_sf"], db=db, user_id=user_id
            )
        return track

    def test_meters_when_db_and_user_id_supplied(self):
        track = self._run_match(db=FakeFirestore(), user_id="user-123")
        track.assert_called_once()
        kwargs = track.call_args.kwargs
        self.assertEqual(kwargs["operation"], "sheet.ai_match_columns")
        self.assertEqual(kwargs["model"], "gpt-4o-mini")

    def test_no_op_when_db_or_user_id_missing(self):
        track = self._run_match(db=None, user_id=None)
        track.assert_not_called()


class ServiceProviderMeteringTests(unittest.TestCase):
    def _make_provider(self):
        from email_automation import service_providers

        provider = service_providers.RealOpenAIProvider.__new__(
            service_providers.RealOpenAIProvider
        )
        message = SimpleNamespace(content="hello")
        choice = SimpleNamespace(message=message)
        provider.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=MagicMock(return_value=SimpleNamespace(
                        choices=[choice],
                        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
                        id="chatcmpl_fake",
                    ))
                )
            )
        )
        provider.model = "gpt-4o"
        return provider

    def test_meters_when_db_and_user_id_supplied(self):
        provider = self._make_provider()
        with patch("email_automation.openai_usage.track_openai_usage_safely") as track:
            out = provider.chat_completion(
                [{"role": "user", "content": "hi"}],
                db=FakeFirestore(),
                user_id="user-123",
            )
        self.assertEqual(out, "hello")
        track.assert_called_once()
        kwargs = track.call_args.kwargs
        self.assertEqual(kwargs["operation"], "provider.chat_completion")
        self.assertEqual(kwargs["model"], "gpt-4o")

    def test_no_op_when_db_or_user_id_missing(self):
        provider = self._make_provider()
        with patch("email_automation.openai_usage.track_openai_usage_safely") as track:
            provider.chat_completion([{"role": "user", "content": "hi"}])
        track.assert_not_called()


class AiProcessingDryRunMeteringTests(unittest.TestCase):
    """The ai.extract_sheet_updates call bills even under dry_run, so metering
    must fire regardless of dry_run (latent-bug fix)."""

    def test_metering_is_not_gated_behind_not_dry_run(self):
        from pathlib import Path

        source = Path("email_automation/ai_processing.py").read_text()
        model_call = source.index("response = client.responses.create")
        usage_call = source.find("track_openai_usage_safely", model_call)
        self.assertNotEqual(usage_call, -1)

        # The metering call must appear, and it must NOT be nested under an
        # `if not dry_run:` guard between the model call and the metering call.
        between = source[model_call:usage_call]
        self.assertNotIn("if not dry_run:", between)
        self.assertIn('operation="ai.extract_sheet_updates"', source[usage_call:usage_call + 800])


if __name__ == "__main__":
    unittest.main()
