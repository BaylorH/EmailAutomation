import unittest
from datetime import datetime, timezone
from pathlib import Path
from datetime import datetime, timezone

from email_automation import automation_runtime as ar
from types import SimpleNamespace
from unittest.mock import patch

from email_automation.openai_usage import (
    PRICING_VERSION,
    estimate_openai_cost,
    record_openai_usage,
    track_openai_usage_safely,
)


class FakeDocRef:
    def __init__(self, path):
        self.path = path
        self.writes = []
        self.collections = {}

    def collection(self, name):
        self.collections.setdefault(name, FakeCollectionRef(f"{self.path}/{name}"))
        return self.collections[name]

    def set(self, payload, merge=False):
        self.writes.append(("set", payload, merge))


class FakeCollectionRef:
    def __init__(self, path):
        self.path = path
        self.added = []
        self.docs = {}

    def document(self, doc_id):
        self.docs.setdefault(doc_id, FakeDocRef(f"{self.path}/{doc_id}"))
        return self.docs[doc_id]

    def add(self, payload):
        self.added.append(payload)


class FakeFirestore:
    def __init__(self):
        self.collections = {}

    def collection(self, name):
        self.collections.setdefault(name, FakeCollectionRef(name))
        return self.collections[name]


class OpenAIUsageTrackingTests(unittest.TestCase):
    def test_estimate_openai_cost_uses_model_rates_and_cached_discount(self):
        usage = SimpleNamespace(
            input_tokens=1000,
            output_tokens=2000,
            total_tokens=3000,
            input_tokens_details=SimpleNamespace(cached_tokens=250),
            output_tokens_details=SimpleNamespace(reasoning_tokens=125),
        )

        result = estimate_openai_cost("gpt-5.2", usage)

        self.assertEqual(result["pricingVersion"], PRICING_VERSION)
        self.assertEqual(result["usage"]["inputTokens"], 1000)
        self.assertEqual(result["usage"]["cachedInputTokens"], 250)
        self.assertEqual(result["usage"]["billableInputTokens"], 750)
        self.assertEqual(result["usage"]["outputTokens"], 2000)
        self.assertEqual(result["usage"]["reasoningOutputTokens"], 125)
        self.assertEqual(result["usage"]["totalTokens"], 3000)
        self.assertAlmostEqual(
            result["cost"]["totalUsd"],
            ((750 * 1.75) + (250 * 0.175) + (2000 * 14.0)) / 1_000_000,
            places=10,
        )

    def test_record_openai_usage_writes_event_and_user_client_rollups_without_prompt_text(self):
        fake_db = FakeFirestore()
        usage = {
            "prompt_tokens": 150,
            "completion_tokens": 50,
            "total_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": 20},
        }
        now = datetime(2026, 5, 27, 19, 15, tzinfo=timezone.utc)

        with patch("email_automation.openai_usage.firestore.Increment", side_effect=lambda value: ("inc", value)), \
                patch("email_automation.openai_usage.SERVER_TIMESTAMP", "SERVER_TIME"):
            event = record_openai_usage(
                db=fake_db,
                user_id="user-123",
                operation="script.generate_all",
                model="gpt-4o-mini",
                usage=usage,
                client_id="client-456",
                thread_id="thread-789",
                request_id="resp_123",
                metadata={
                    "sheetId": "sheet-abc",
                    "propertyAddress": "2455 W Cheyenne Ave",
                    "prompt": "do not persist this",
                    "messages": ["do not persist this either"],
                },
                now=now,
            )

        self.assertEqual(event["userId"], "user-123")
        self.assertEqual(event["clientId"], "client-456")
        self.assertEqual(event["threadId"], "thread-789")
        self.assertEqual(event["requestId"], "resp_123")
        self.assertEqual(event["date"], "2026-05-27")
        self.assertEqual(event["metadata"], {
            "sheetId": "sheet-abc",
            "propertyAddress": "2455 W Cheyenne Ave",
        })
        self.assertNotIn("prompt", str(event))
        self.assertNotIn("messages", str(event))

        user_ref = fake_db.collections["users"].docs["user-123"]
        events_collection = user_ref.collection("openaiUsageEvents")
        daily_ref = user_ref.collection("openaiUsageDaily").docs["2026-05-27"]
        client_daily_ref = user_ref.collection("clients").docs["client-456"].collection("openaiUsageDaily").docs["2026-05-27"]

        self.assertEqual(len(events_collection.added), 1)
        self.assertEqual(events_collection.added[0]["operation"], "script.generate_all")
        self.assertEqual(daily_ref.writes[0][2], True)
        self.assertEqual(client_daily_ref.writes[0][2], True)
        self.assertEqual(daily_ref.writes[0][1]["calls"], ("inc", 1))
        self.assertGreater(daily_ref.writes[0][1]["totalCostUsd"][1], 0)

    def test_track_openai_usage_safely_swallows_metering_failures(self):
        class BrokenDb:
            def collection(self, _name):
                raise RuntimeError("firestore unavailable")

        with patch("email_automation.openai_usage.logger") as logger:
            result = track_openai_usage_safely(
                db=BrokenDb(),
                user_id="user-123",
                operation="ai.extract_reply",
                model="gpt-5.2",
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            )

        self.assertIsNone(result)
        logger.warning.assert_called_once()

    def test_sheet_update_extraction_records_openai_usage_after_model_call(self):
        source = Path("email_automation/ai_processing.py").read_text()
        # Anchored on the request-scoped transport rather than a raw client, which
        # is what the extraction call became in Task 7F. The property under test
        # is unchanged and is an ORDERING one: usage is recorded after the model
        # call, never before it, so a refused or failed call records nothing.
        model_call = source.index("ai_for(runtime, client).create_response")
        usage_call = source.find("track_openai_usage_safely", model_call)

        self.assertNotEqual(usage_call, -1, "OpenAI extraction usage is not tracked after the model call")
        self.assertIn("operation=\"ai.extract_sheet_updates\"", source[usage_call:usage_call + 700])
        self.assertIn("model=\"gpt-5.2\"", source[usage_call:usage_call + 700])
        self.assertNotIn("\"prompt\"", source[usage_call:usage_call + 700])


class ScopedUsageAccountingTests(unittest.TestCase):
    """Task 7F step 3: usage accounting belongs to the run that caused it.

    Cost is state. A certification run that recorded its spend into a real
    user's rollups would corrupt that user's billing view, and one that read
    them would see another tenant's numbers - so the accounting has to land in
    the run's own store, on the run's own clock, and nowhere else.
    """

    FIXTURE_UID = "cert-usage-uid"
    PREFIX = "users/cert-usage-uid"

    def _fixture_store(self):
        from tests.test_production_certification import FixtureFirestore

        return FixtureFirestore()

    def _runtime(self, store, *, now):
        return ar.certification_runtime(
            run_id="cert-run-usage",
            scope="usage-accounting",
            firestore=store,
            firestore_prefix=self.PREFIX,
            now=now,
        )

    def test_accounting_lands_in_the_runs_own_store_not_the_ambient_one(self):
        class Exploding:
            def __getattr__(self, name):
                raise AssertionError(f"ambient Firestore was reached: .{name}")

        store = self._fixture_store()
        fixed = datetime(2026, 8, 18, 9, 30, tzinfo=timezone.utc)
        runtime = self._runtime(store, now=lambda: fixed)

        record_openai_usage(
            db=ar.firestore_for(runtime, Exploding()),
            user_id=self.FIXTURE_UID,
            operation="ai.extract_sheet_updates",
            model="gpt-5.2",
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            now=ar.clock_for(runtime, lambda: datetime.now(timezone.utc))(),
        )

        paths = [path for _kind, path, _payload, _merge in store.writes]
        self.assertTrue(
            any(p.startswith(f"{self.PREFIX}/openaiUsageEvents/") for p in paths),
            f"no usage event landed in the run's store: {paths}",
        )
        self.assertIn(f"{self.PREFIX}/openaiUsageDaily/2026-08-18", paths)
        self.assertEqual(runtime.effect_scope.violations, [])

    def test_the_request_clock_decides_the_rollup_day(self):
        """Otherwise a replayed run lands on whatever day it happens to run."""
        store = self._fixture_store()
        fixed = datetime(2026, 1, 2, 23, 59, tzinfo=timezone.utc)
        runtime = self._runtime(store, now=lambda: fixed)

        record_openai_usage(
            db=ar.firestore_for(runtime, None),
            user_id=self.FIXTURE_UID,
            operation="ai.extract_sheet_updates",
            model="gpt-5.2",
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            now=ar.clock_for(runtime, lambda: datetime.now(timezone.utc))(),
        )

        paths = [path for _kind, path, _payload, _merge in store.writes]
        self.assertIn(f"{self.PREFIX}/openaiUsageDaily/2026-01-02", paths)

    def test_accounting_for_another_user_is_refused_by_the_fence(self):
        """The isolation that makes a shared process safe."""
        store = self._fixture_store()
        runtime = self._runtime(store, now=lambda: datetime(2026, 8, 18, tzinfo=timezone.utc))

        with self.assertRaises(Exception):
            record_openai_usage(
                db=ar.firestore_for(runtime, None),
                user_id="someone-elses-uid",
                operation="ai.extract_sheet_updates",
                model="gpt-5.2",
                usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                now=datetime(2026, 8, 18, tzinfo=timezone.utc),
            )
        self.assertTrue(runtime.effect_scope.violations)
        self.assertEqual(store.writes, [])

    def test_readback_sees_exactly_what_the_run_wrote(self):
        store = self._fixture_store()
        fixed = datetime(2026, 8, 18, 9, 30, tzinfo=timezone.utc)
        runtime = self._runtime(store, now=lambda: fixed)
        fenced = ar.firestore_for(runtime, None)

        record_openai_usage(
            db=fenced,
            user_id=self.FIXTURE_UID,
            operation="ai.extract_sheet_updates",
            model="gpt-5.2",
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            now=fixed,
        )

        rollup = (
            fenced.collection("users")
            .document(self.FIXTURE_UID)
            .collection("openaiUsageDaily")
            .document("2026-08-18")
            .get()
        )
        self.assertTrue(rollup.exists)
        self.assertEqual(runtime.effect_scope.violations, [])

    def test_the_extraction_call_site_resolves_both_through_the_runtime(self):
        """Structural: the seam exists where the model call actually happens."""
        source = Path("email_automation/ai_processing.py").read_text()
        usage_call = source.index("track_openai_usage_safely(")
        window = source[usage_call:usage_call + 400]
        self.assertIn("firestore_for(runtime", window)
        self.assertIn("clock_for(runtime", window)


if __name__ == "__main__":
    unittest.main()
