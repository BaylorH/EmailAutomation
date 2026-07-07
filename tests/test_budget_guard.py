"""Unit tests for the flag-gated global monthly OpenAI budget guard. No live API,
no Firestore — an injected fake db supplies per-user openaiUsageDaily rollups."""
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from email_automation import budget_guard as bg  # noqa: E402


# --- fake Firestore surface (only what the guard touches) ------------------
class _DaySnap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._d = data
    def to_dict(self):
        return self._d


class _Coll:
    def __init__(self, docs):
        self._docs = docs
    def stream(self):
        return iter(self._docs)


class _Ref:
    def __init__(self, daily):
        self._daily = daily
    def collection(self, name):
        return _Coll(self._daily if name == "openaiUsageDaily" else [])


class _UserSnap:
    def __init__(self, daily):
        self.reference = _Ref(daily)


class FakeDb:
    """users = list of {date_key: {"totalCostUsd": float}} dicts, one per user."""
    def __init__(self, users, raise_on_users=False):
        self._users = users
        self._raise = raise_on_users
    def collection(self, name):
        if name == "users":
            if self._raise:
                raise RuntimeError("firestore down")
            return _Coll([_UserSnap([_DaySnap(k, v) for k, v in u.items()]) for u in self._users])
        return _Coll([])


NOW = datetime(2026, 7, 7, tzinfo=timezone.utc)


def _db_july_spend(total_across_users):
    # one user, one july day carrying the whole spend
    return FakeDb([{"2026-07-01": {"totalCostUsd": total_across_users}}])


class FlagAndLimitTests(unittest.TestCase):
    def test_enforcement_default_off(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENFORCE_OPENAI_BUDGET", None)
            self.assertFalse(bg.budget_enforcement_enabled())

    def test_enforcement_truthy(self):
        for v in ("1", "true", "TRUE", "Yes", "on"):
            with mock.patch.dict(os.environ, {"ENFORCE_OPENAI_BUDGET": v}):
                self.assertTrue(bg.budget_enforcement_enabled(), v)

    def test_limit_parse(self):
        with mock.patch.dict(os.environ, {"USAGE_MONTHLY_BUDGET_USD": "250.5"}):
            self.assertEqual(bg.monthly_budget_limit_usd(), 250.5)
        for bad in ("", "abc"):
            with mock.patch.dict(os.environ, {"USAGE_MONTHLY_BUDGET_USD": bad}):
                self.assertEqual(bg.monthly_budget_limit_usd(), 0.0)


class BlockDecisionTests(unittest.TestCase):
    def test_off_never_blocks_even_over_budget(self):
        with mock.patch.dict(os.environ, {"USAGE_MONTHLY_BUDGET_USD": "10"}):
            os.environ.pop("ENFORCE_OPENAI_BUDGET", None)
            self.assertFalse(bg.should_block_openai_call(_db_july_spend(999), now=NOW))

    def test_no_limit_never_blocks(self):
        with mock.patch.dict(os.environ, {"ENFORCE_OPENAI_BUDGET": "1"}):
            os.environ.pop("USAGE_MONTHLY_BUDGET_USD", None)
            self.assertFalse(bg.should_block_openai_call(_db_july_spend(999), now=NOW))

    def test_under_budget_allows(self):
        with mock.patch.dict(os.environ, {"ENFORCE_OPENAI_BUDGET": "1", "USAGE_MONTHLY_BUDGET_USD": "10"}):
            self.assertFalse(bg.should_block_openai_call(_db_july_spend(9.99), now=NOW))

    def test_at_or_over_budget_blocks(self):
        with mock.patch.dict(os.environ, {"ENFORCE_OPENAI_BUDGET": "1", "USAGE_MONTHLY_BUDGET_USD": "10"}):
            self.assertTrue(bg.should_block_openai_call(_db_july_spend(10.0), now=NOW))
            self.assertTrue(bg.should_block_openai_call(_db_july_spend(10.01), now=NOW))

    def test_fail_open_on_db_error(self):
        with mock.patch.dict(os.environ, {"ENFORCE_OPENAI_BUDGET": "1", "USAGE_MONTHLY_BUDGET_USD": "10"}):
            self.assertFalse(bg.should_block_openai_call(FakeDb([], raise_on_users=True), now=NOW))


class AggregationTests(unittest.TestCase):
    def test_sums_across_users_current_month_only(self):
        db = FakeDb([
            {"2026-07-01": {"totalCostUsd": 3.0}, "2026-07-05": {"totalCostUsd": 2.0},
             "2026-06-30": {"totalCostUsd": 100.0}},   # prior month -> excluded
            {"2026-07-02": {"totalCostUsd": 1.5}},
            {"2026-05-01": {"totalCostUsd": 50.0}},     # prior month -> excluded
        ])
        self.assertAlmostEqual(bg.global_month_spend_usd(db, now=NOW), 6.5)

    def test_handles_missing_or_garbage_cost(self):
        db = FakeDb([{"2026-07-01": {}, "2026-07-02": {"totalCostUsd": None},
                      "2026-07-03": {"totalCostUsd": "oops"}, "2026-07-04": {"totalCostUsd": 4.0}}])
        self.assertAlmostEqual(bg.global_month_spend_usd(db, now=NOW), 4.0)

    def test_status_shape(self):
        with mock.patch.dict(os.environ, {"ENFORCE_OPENAI_BUDGET": "1", "USAGE_MONTHLY_BUDGET_USD": "10"}):
            s = bg.budget_status(_db_july_spend(7.25), now=NOW)
        self.assertEqual(s["enforced"], True)
        self.assertEqual(s["limitUsd"], 10.0)
        self.assertAlmostEqual(s["spentUsd"], 7.25)
        self.assertFalse(s["overBudget"])
        self.assertAlmostEqual(s["remainingUsd"], 2.75)

    def test_status_fail_open(self):
        s = bg.budget_status(FakeDb([], raise_on_users=True), now=NOW)
        self.assertIsNone(s["spentUsd"])
        self.assertFalse(s["overBudget"])
        self.assertIn("error", s)


if __name__ == "__main__":
    unittest.main()
