"""Independently instrumented OpenAI transport for sanitized claim replay."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from importlib import metadata
from time import monotonic
from typing import Any, Callable

from openai import OpenAI

from email_automation.claim_pipeline.provider_replay import (
    PINNED_MODEL_ID,
    PINNED_PROVIDER_ID,
    ProviderTransportResult,
)
from email_automation.claim_pipeline.replay import (
    ProposalUsage,
    ProviderTelemetrySnapshot,
)


PINNED_OPENAI_SDK_VERSION = "2.45.0"
REQUEST_TIMEOUT_SECONDS = 60.0
MAX_OUTPUT_TOKENS = 8_000
PRICING_REVISION = "openai-gpt-5.2-2026-07-22"
INPUT_USD_PER_MILLION = Decimal("1.75")
CACHED_INPUT_USD_PER_MILLION = Decimal("0.175")
OUTPUT_USD_PER_MILLION = Decimal("14.00")


def _value(value: object, name: str, default: object = None) -> object:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _nonnegative_token(value: object) -> tuple[int, bool]:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0, False
    return value, True


def _cost_microusd(
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> int:
    uncached = max(input_tokens - cached_input_tokens, 0)
    amount = (
        Decimal(uncached) * INPUT_USD_PER_MILLION
        + Decimal(cached_input_tokens) * CACHED_INPUT_USD_PER_MILLION
        + Decimal(output_tokens) * OUTPUT_USD_PER_MILLION
    )
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class OpenAIClaimReplayTransport:
    provider_id = PINNED_PROVIDER_ID
    model_id = PINNED_MODEL_ID

    def __init__(
        self,
        *,
        api_key: str,
        client: Any = None,
        sdk_version: str | None = None,
        clock: Callable[[], float] = monotonic,
    ):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("OPENAI_API_KEY is required for provider replay")
        observed_sdk = sdk_version or metadata.version("openai")
        if observed_sdk != PINNED_OPENAI_SDK_VERSION:
            raise ValueError(
                "OpenAI SDK version does not match the pinned dependency lock"
            )
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock
        self._client = client or OpenAI(
            api_key=api_key.strip(),
            max_retries=0,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        self._attempts: list[ProposalUsage] = []

    def snapshot(self) -> ProviderTelemetrySnapshot:
        return ProviderTelemetrySnapshot(
            attempts=len(self._attempts),
            billed_calls=sum(
                item.provider_calls for item in self._attempts if item.provider_billed
            ),
            input_tokens=sum(item.input_tokens for item in self._attempts),
            output_tokens=sum(item.output_tokens for item in self._attempts),
            latency_ms=sum(item.latency_ms for item in self._attempts),
            cost_microusd=sum(item.cost_microusd for item in self._attempts),
            incomplete_attempts=sum(
                item.provider_calls
                for item in self._attempts
                if not item.usage_complete
            ),
        )

    def _latency_ms(self, started_at: float) -> int:
        return max(0, int(round((self._clock() - started_at) * 1_000)))

    def invoke(
        self,
        *,
        case_id: str,
        instructions: str,
        payload: str,
    ) -> ProviderTransportResult:
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("case_id must be non-empty")
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError("instructions must be non-empty")
        if not isinstance(payload, str) or not payload.strip():
            raise ValueError("payload must be non-empty")
        started_at = self._clock()
        try:
            response = self._client.responses.create(
                model=self.model_id,
                instructions=instructions,
                input=f"JSON request:\n{payload}",
                text={"format": {"type": "json_object"}},
                max_output_tokens=MAX_OUTPUT_TOKENS,
                store=False,
            )
        except Exception:
            self._attempts.append(
                ProposalUsage(
                    latency_ms=self._latency_ms(started_at),
                    provider_calls=1,
                    provider_billed=False,
                    usage_complete=False,
                )
            )
            raise

        usage = _value(response, "usage")
        input_tokens, input_complete = _nonnegative_token(
            _value(usage, "input_tokens")
        )
        output_tokens, output_complete = _nonnegative_token(
            _value(usage, "output_tokens")
        )
        details = _value(usage, "input_tokens_details")
        cached_raw = _value(details, "cached_tokens", 0)
        cached_tokens, cached_complete = _nonnegative_token(cached_raw)
        usage_complete = (
            usage is not None
            and input_complete
            and output_complete
            and cached_complete
            and cached_tokens <= input_tokens
            and input_tokens + output_tokens > 0
        )
        cost = (
            _cost_microusd(
                input_tokens=input_tokens,
                cached_input_tokens=cached_tokens,
                output_tokens=output_tokens,
            )
            if usage_complete
            else 0
        )
        observed = ProposalUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=self._latency_ms(started_at),
            cost_microusd=cost,
            provider_calls=1,
            provider_billed=True,
            usage_complete=usage_complete,
        )
        self._attempts.append(observed)
        return ProviderTransportResult(
            model_output=_value(response, "output_text", ""),
            usage=observed,
        )


__all__ = [
    "MAX_OUTPUT_TOKENS",
    "OpenAIClaimReplayTransport",
    "PINNED_MODEL_ID",
    "PINNED_OPENAI_SDK_VERSION",
    "PINNED_PROVIDER_ID",
    "PRICING_REVISION",
    "ProviderTelemetrySnapshot",
    "REQUEST_TIMEOUT_SECONDS",
]
