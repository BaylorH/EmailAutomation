import importlib.util
import types
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
TRANSPORT_PATH = REPO_ROOT / "scripts" / "claim_pipeline_openai_transport.py"


def _load_transport_module():
    spec = importlib.util.spec_from_file_location(
        "claim_pipeline_openai_transport_under_test",
        TRANSPORT_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load OpenAI claim replay transport")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Responses:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class _Client:
    def __init__(self, responses):
        self.responses = responses


class OpenAIClaimReplayTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_transport_module()

    def _response(self, *, usage=True):
        details = types.SimpleNamespace(cached_tokens=40)
        response_usage = None
        if usage:
            response_usage = types.SimpleNamespace(
                input_tokens=100,
                output_tokens=20,
                input_tokens_details=details,
            )
        return types.SimpleNamespace(
            id="resp_safe_123",
            output_text='{"claims":[],"review":[]}',
            usage=response_usage,
        )

    def test_constructor_pins_sdk_retry_and_timeout_configuration(self):
        fake_client = _Client(_Responses(response=self._response()))
        with patch.object(self.module, "OpenAI", return_value=fake_client) as factory:
            transport = self.module.OpenAIClaimReplayTransport(
                api_key="test-key",
                sdk_version=self.module.PINNED_OPENAI_SDK_VERSION,
            )

        factory.assert_called_once_with(
            api_key="test-key",
            max_retries=0,
            timeout=self.module.REQUEST_TIMEOUT_SECONDS,
        )
        self.assertEqual(self.module.PINNED_MODEL_ID, transport.model_id)

    def test_invoke_records_complete_independent_usage_and_fixed_cost(self):
        responses = _Responses(response=self._response())
        ticks = iter((10.0, 10.125))
        transport = self.module.OpenAIClaimReplayTransport(
            api_key="test-key",
            client=_Client(responses),
            sdk_version=self.module.PINNED_OPENAI_SDK_VERSION,
            clock=lambda: next(ticks),
        )

        result = transport.invoke(
            case_id="safe-case",
            instructions="safe instructions",
            payload='{"safe":true}',
        )

        self.assertEqual('{"claims":[],"review":[]}', result.model_output)
        self.assertEqual(1, result.usage.provider_calls)
        self.assertTrue(result.usage.provider_billed)
        self.assertTrue(result.usage.usage_complete)
        self.assertEqual(100, result.usage.input_tokens)
        self.assertEqual(20, result.usage.output_tokens)
        self.assertEqual(125, result.usage.latency_ms)
        self.assertEqual(392, result.usage.cost_microusd)
        self.assertEqual(
            self.module.ProviderTelemetrySnapshot(
                attempts=1,
                billed_calls=1,
                input_tokens=100,
                output_tokens=20,
                latency_ms=125,
                cost_microusd=392,
                incomplete_attempts=0,
            ),
            transport.snapshot(),
        )
        self.assertEqual(1, len(responses.calls))
        call = responses.calls[0]
        self.assertEqual(self.module.PINNED_MODEL_ID, call["model"])
        self.assertEqual("safe instructions", call["instructions"])
        self.assertEqual('JSON request:\n{"safe":true}', call["input"])
        self.assertFalse(call["store"])
        self.assertEqual({"format": {"type": "json_object"}}, call["text"])

    def test_provider_error_records_one_incomplete_attempt_without_retry(self):
        responses = _Responses(error=RuntimeError("private response detail"))
        ticks = iter((3.0, 3.01))
        transport = self.module.OpenAIClaimReplayTransport(
            api_key="test-key",
            client=_Client(responses),
            sdk_version=self.module.PINNED_OPENAI_SDK_VERSION,
            clock=lambda: next(ticks),
        )

        with self.assertRaises(RuntimeError):
            transport.invoke(
                case_id="safe-case",
                instructions="safe instructions",
                payload='{"safe":true}',
            )

        self.assertEqual(1, len(responses.calls))
        snapshot = transport.snapshot()
        self.assertEqual(1, snapshot.attempts)
        self.assertEqual(0, snapshot.billed_calls)
        self.assertEqual(1, snapshot.incomplete_attempts)

    def test_missing_usage_is_billed_but_incomplete(self):
        responses = _Responses(response=self._response(usage=False))
        ticks = iter((2.0, 2.001))
        transport = self.module.OpenAIClaimReplayTransport(
            api_key="test-key",
            client=_Client(responses),
            sdk_version=self.module.PINNED_OPENAI_SDK_VERSION,
            clock=lambda: next(ticks),
        )

        result = transport.invoke(
            case_id="safe-case",
            instructions="safe instructions",
            payload='{"safe":true}',
        )

        self.assertTrue(result.usage.provider_billed)
        self.assertFalse(result.usage.usage_complete)
        self.assertEqual(1, transport.snapshot().incomplete_attempts)

    def test_rejects_unpinned_sdk_or_blank_key(self):
        with self.assertRaises(ValueError):
            self.module.OpenAIClaimReplayTransport(
                api_key="test-key",
                client=_Client(_Responses()),
                sdk_version="2.46.0",
            )
        with self.assertRaises(ValueError):
            self.module.OpenAIClaimReplayTransport(
                api_key="",
                client=_Client(_Responses()),
                sdk_version=self.module.PINNED_OPENAI_SDK_VERSION,
            )


if __name__ == "__main__":
    unittest.main()
