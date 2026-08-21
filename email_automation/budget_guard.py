"""Global monthly OpenAI budget guard (flag-gated, default OFF).

Enforcement is OFF unless ENFORCE_OPENAI_BUDGET is truthy. When ON and
USAGE_MONTHLY_BUDGET_USD (>0) is set, ``should_block_openai_call()`` returns True
once the current calendar-month cross-user spend reaches the limit, so callers
can SKIP the paid call (defer the turn) instead of overspending.

Spend is aggregated from the per-user ``openaiUsageDaily`` rollups (``totalCostUsd``)
that ``record_openai_usage`` already maintains — no new writes, and consistent
with the admin dashboard's own aggregation.

Cost note: O(users x month-days) reads per check — fine for the current small
beta. For scale, maintain a ``systemMetrics`` monthly counter and read it O(1).

Failure policy: FAIL-OPEN. A budget-check error (Firestore hiccup, etc.) must not
break extraction; actual spend is still metered and visible on the dashboard, so
a brief overshoot is preferable to halting the pipeline. Flip to fail-closed only
if hard cost containment is required over availability.
"""
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

_TRUTHY = {"1", "true", "yes", "on"}


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
        return global_month_spend_usd(db, now=now) >= limit
    except Exception as e:  # noqa: BLE001 — never block extraction due to a check error
        print(f"⚠️ budget_guard: check failed (fail-open, allowing call): {e}")
        return False
