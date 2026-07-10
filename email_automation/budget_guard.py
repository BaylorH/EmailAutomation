"""Global monthly OpenAI budget guard (flag-gated, default OFF).

Enforcement is OFF unless ENFORCE_OPENAI_BUDGET is truthy. When ON and
USAGE_MONTHLY_BUDGET_USD (>0) is set, ``should_block_openai_call()`` returns True
once the current calendar-month cross-user spend reaches the limit, so callers
can SKIP the paid call (defer the turn) instead of overspending.

Spend is aggregated from the per-user ``openaiUsageDaily`` rollups (``totalCostUsd``)
that ``record_openai_usage`` already maintains — no new writes, and consistent
with the admin dashboard's own aggregation. Enforcement checks cache that total
briefly per database/month so a multi-message scheduler run does not rescan every
user and every day for each extraction.

Cost note: the first enforced check is O(users x month-days); subsequent checks
within the short cache window are O(1). A ``systemMetrics`` monthly counter is
still the long-term path for high-volume scale.

Failure policy: FAIL-OPEN. A budget-check error (Firestore hiccup, etc.) must not
break extraction; actual spend is still metered and visible on the dashboard, so
a brief overshoot is preferable to halting the pipeline. Flip to fail-closed only
if hard cost containment is required over availability.
"""
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

_TRUTHY = {"1", "true", "yes", "on"}
_DEFAULT_SPEND_CACHE_TTL_SECONDS = 60.0
_SPEND_CACHE = {}
_SPEND_CACHE_LOCK = threading.Lock()


class BudgetDeferredError(RuntimeError):
    """Paid AI work was intentionally deferred and must remain retryable."""


def reset_budget_spend_cache() -> None:
    """Clear cached spend totals (primarily for deterministic tests and run setup)."""
    with _SPEND_CACHE_LOCK:
        _SPEND_CACHE.clear()


def _spend_cache_ttl_seconds() -> float:
    raw = os.getenv("OPENAI_BUDGET_CACHE_TTL_SECONDS", "")
    try:
        parsed = float(raw or _DEFAULT_SPEND_CACHE_TTL_SECONDS)
    except (TypeError, ValueError):
        return _DEFAULT_SPEND_CACHE_TTL_SECONDS
    return max(0.0, parsed)


def budget_enforcement_enabled() -> bool:
    """True only when ENFORCE_OPENAI_BUDGET is explicitly truthy (default OFF)."""
    return os.getenv("ENFORCE_OPENAI_BUDGET", "").strip().lower() in _TRUTHY


def monthly_budget_limit_usd() -> float:
    """USAGE_MONTHLY_BUDGET_USD as a float; 0.0 (or unset/garbage) means no limit."""
    raw = os.getenv("USAGE_MONTHLY_BUDGET_USD", "")
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _month_prefix(now: Optional[datetime] = None) -> str:
    dt = now or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m")


def global_month_spend_usd(db: Any, *, now: Optional[datetime] = None) -> float:
    """Sum ``totalCostUsd`` across every user's ``openaiUsageDaily`` docs whose
    id (a YYYY-MM-DD date key) falls in the current calendar month. Reads only the
    user-level rollups (NOT the per-client subcollection) to avoid double counting.
    """
    month = _month_prefix(now)
    total = 0.0
    for user in db.collection("users").stream():
        for day in user.reference.collection("openaiUsageDaily").stream():
            if not str(getattr(day, "id", "")).startswith(month):
                continue
            data = day.to_dict() or {}
            try:
                total += float(data.get("totalCostUsd") or 0.0)
            except (TypeError, ValueError):
                continue
    return total


def _cached_global_month_spend_usd(db: Any, *, now: Optional[datetime] = None) -> float:
    month = _month_prefix(now)
    cache_key = (id(db), month)
    monotonic_now = time.monotonic()
    ttl_seconds = _spend_cache_ttl_seconds()

    if ttl_seconds > 0:
        with _SPEND_CACHE_LOCK:
            cached = _SPEND_CACHE.get(cache_key)
            if cached and cached[0] > monotonic_now:
                return cached[1]

    spent = global_month_spend_usd(db, now=now)
    if ttl_seconds > 0:
        with _SPEND_CACHE_LOCK:
            _SPEND_CACHE[cache_key] = (monotonic_now + ttl_seconds, spent)
    return spent


def budget_status(db: Any, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Full status for logging / the dashboard: enforced flag, limit, spend,
    over-budget, remaining. Fail-open: on a read error, spentUsd=None + not over."""
    enforced = budget_enforcement_enabled()
    limit = monthly_budget_limit_usd()
    try:
        spent = global_month_spend_usd(db, now=now)
    except Exception as e:  # noqa: BLE001 — fail-open, never raise from a status read
        print(f"⚠️ budget_guard: spend read failed (fail-open): {e}")
        return {"enforced": enforced, "limitUsd": limit, "spentUsd": None,
                "overBudget": False, "remainingUsd": None, "error": str(e)}
    over = bool(limit > 0 and spent >= limit)
    return {
        "enforced": enforced,
        "limitUsd": limit,
        "spentUsd": round(spent, 6),
        "overBudget": over,
        "remainingUsd": round(max(0.0, limit - spent), 6) if limit > 0 else None,
    }


def should_block_openai_call(db: Any, *, now: Optional[datetime] = None) -> bool:
    """True ONLY when enforcement is ON, a positive limit is set, and current-month
    spend has reached it. Fail-OPEN (returns False) on any error."""
    try:
        if not budget_enforcement_enabled():
            return False
        limit = monthly_budget_limit_usd()
        if limit <= 0:
            return False
        return _cached_global_month_spend_usd(db, now=now) >= limit
    except Exception as e:  # noqa: BLE001 — never block extraction due to a check error
        print(f"⚠️ budget_guard: check failed (fail-open, allowing call): {e}")
        return False
