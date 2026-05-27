import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from google.cloud import firestore
from google.cloud.firestore import SERVER_TIMESTAMP

logger = logging.getLogger(__name__)

PRICING_VERSION = "2026-05-27"

MODEL_PRICING_PER_MILLION = {
    "gpt-5.2": {"input": 1.75, "cached_input": 0.175, "output": 14.0},
    "gpt-5.2-2025-12-11": {"input": 1.75, "cached_input": 0.175, "output": 14.0},
    "gpt-5.2-chat-latest": {"input": 1.75, "cached_input": 0.175, "output": 14.0},
    "gpt-4o-mini": {"input": 0.15, "cached_input": 0.075, "output": 0.60},
    "gpt-4o-mini-2024-07-18": {"input": 0.15, "cached_input": 0.075, "output": 0.60},
}

SENSITIVE_METADATA_KEYS = {
    "body",
    "content",
    "currentEmailDraft",
    "email",
    "extractionPrompt",
    "input",
    "message",
    "messages",
    "pdfText",
    "primaryScript",
    "prompt",
    "raw",
    "response",
    "systemPrompt",
    "text",
    "transcript",
    "userPrompt",
}


def _read_value(obj: Any, *path: str, default: Any = 0) -> Any:
    current = obj
    for part in path:
        if current is None:
            return default
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
    return default if current is None else current


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _pricing_for(model: str) -> Dict[str, float]:
    normalized = (model or "").strip()
    if normalized in MODEL_PRICING_PER_MILLION:
        return MODEL_PRICING_PER_MILLION[normalized]
    if normalized.startswith("gpt-5.2"):
        return MODEL_PRICING_PER_MILLION["gpt-5.2"]
    if normalized.startswith("gpt-4o-mini"):
        return MODEL_PRICING_PER_MILLION["gpt-4o-mini"]
    return {"input": 0.0, "cached_input": 0.0, "output": 0.0}


def _usage_metrics(usage: Any) -> Dict[str, int]:
    input_tokens = _as_int(_read_value(usage, "input_tokens", default=None))
    if input_tokens == 0:
        input_tokens = _as_int(_read_value(usage, "prompt_tokens", default=0))

    output_tokens = _as_int(_read_value(usage, "output_tokens", default=None))
    if output_tokens == 0:
        output_tokens = _as_int(_read_value(usage, "completion_tokens", default=0))

    total_tokens = _as_int(_read_value(usage, "total_tokens", default=0))
    cached_input_tokens = _as_int(_read_value(usage, "input_tokens_details", "cached_tokens", default=None))
    if cached_input_tokens == 0:
        cached_input_tokens = _as_int(_read_value(usage, "prompt_tokens_details", "cached_tokens", default=0))

    reasoning_output_tokens = _as_int(_read_value(usage, "output_tokens_details", "reasoning_tokens", default=None))
    if reasoning_output_tokens == 0:
        reasoning_output_tokens = _as_int(_read_value(usage, "completion_tokens_details", "reasoning_tokens", default=0))

    billable_input_tokens = max(input_tokens - cached_input_tokens, 0)
    if total_tokens == 0:
        total_tokens = input_tokens + output_tokens

    return {
        "inputTokens": input_tokens,
        "cachedInputTokens": cached_input_tokens,
        "billableInputTokens": billable_input_tokens,
        "outputTokens": output_tokens,
        "reasoningOutputTokens": reasoning_output_tokens,
        "totalTokens": total_tokens,
    }


def estimate_openai_cost(model: str, usage: Any) -> Dict[str, Any]:
    metrics = _usage_metrics(usage)
    pricing = _pricing_for(model)
    input_usd = metrics["billableInputTokens"] * pricing["input"] / 1_000_000
    cached_input_usd = metrics["cachedInputTokens"] * pricing["cached_input"] / 1_000_000
    output_usd = metrics["outputTokens"] * pricing["output"] / 1_000_000

    return {
        "pricingVersion": PRICING_VERSION,
        "usage": metrics,
        "cost": {
            "inputUsd": input_usd,
            "cachedInputUsd": cached_input_usd,
            "outputUsd": output_usd,
            "totalUsd": input_usd + cached_input_usd + output_usd,
        },
        "pricing": pricing,
    }


def _safe_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not metadata:
        return {}

    safe: Dict[str, Any] = {}
    for key, value in metadata.items():
        if key in SENSITIVE_METADATA_KEYS:
            continue
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            safe[key] = value
        elif isinstance(value, (list, tuple)):
            safe[key] = [item for item in value if isinstance(item, (str, int, float, bool))][:20]
        elif isinstance(value, dict):
            safe[key] = {
                nested_key: nested_value
                for nested_key, nested_value in value.items()
                if nested_key not in SENSITIVE_METADATA_KEYS
                and isinstance(nested_value, (str, int, float, bool))
            }
    return safe


def _rollup_payload(operation: str, model: str, estimate: Dict[str, Any]) -> Dict[str, Any]:
    usage = estimate["usage"]
    cost = estimate["cost"]
    return {
        "calls": firestore.Increment(1),
        "totalCostUsd": firestore.Increment(cost["totalUsd"]),
        "inputCostUsd": firestore.Increment(cost["inputUsd"]),
        "cachedInputCostUsd": firestore.Increment(cost["cachedInputUsd"]),
        "outputCostUsd": firestore.Increment(cost["outputUsd"]),
        "inputTokens": firestore.Increment(usage["inputTokens"]),
        "cachedInputTokens": firestore.Increment(usage["cachedInputTokens"]),
        "billableInputTokens": firestore.Increment(usage["billableInputTokens"]),
        "outputTokens": firestore.Increment(usage["outputTokens"]),
        "reasoningOutputTokens": firestore.Increment(usage["reasoningOutputTokens"]),
        "totalTokens": firestore.Increment(usage["totalTokens"]),
        f"operations.{operation}.calls": firestore.Increment(1),
        f"operations.{operation}.costUsd": firestore.Increment(cost["totalUsd"]),
        f"models.{model}.calls": firestore.Increment(1),
        f"models.{model}.costUsd": firestore.Increment(cost["totalUsd"]),
        "updatedAt": SERVER_TIMESTAMP,
        "pricingVersion": PRICING_VERSION,
    }


def record_openai_usage(
    *,
    db: Any,
    user_id: str,
    operation: str,
    model: str,
    usage: Any,
    client_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    request_id: Optional[str] = None,
    endpoint: str = "openai",
    metadata: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    if not user_id:
        raise ValueError("user_id is required for OpenAI usage tracking")
    if not operation:
        raise ValueError("operation is required for OpenAI usage tracking")

    event_time = now or datetime.now(timezone.utc)
    date_key = event_time.date().isoformat()
    estimate = estimate_openai_cost(model, usage)

    event = {
        "provider": "openai",
        "endpoint": endpoint,
        "userId": user_id,
        "clientId": client_id,
        "threadId": thread_id,
        "operation": operation,
        "model": model,
        "requestId": request_id,
        "date": date_key,
        "createdAt": SERVER_TIMESTAMP,
        "createdAtIso": event_time.isoformat(),
        "pricingVersion": estimate["pricingVersion"],
        "usage": estimate["usage"],
        "cost": estimate["cost"],
        "metadata": _safe_metadata(metadata),
    }

    user_ref = db.collection("users").document(user_id)
    user_ref.collection("openaiUsageEvents").add(event)

    rollup = _rollup_payload(operation, model, estimate)
    user_ref.collection("openaiUsageDaily").document(date_key).set(rollup, merge=True)
    if client_id:
        user_ref.collection("clients").document(client_id).collection("openaiUsageDaily").document(date_key).set(rollup, merge=True)

    return event


def track_openai_usage_safely(**kwargs: Any) -> Optional[Dict[str, Any]]:
    try:
        return record_openai_usage(**kwargs)
    except Exception as exc:
        logger.warning("OpenAI usage tracking failed: %s", exc)
        return None
