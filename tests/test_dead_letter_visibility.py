"""Rail 4 — Dead-letter visibility.

Two defects this locks down:

1. The Firestore debug/inspect view rendered every dead-letter item with a
   BLANK reason because it read the ``reason`` key, but dead-letters store the
   human-readable failure under ``failureReason`` / ``lastError``
   (email.py:2213-2266, pending_responses.py:32). An operator saw the stuck
   item but not *why* it failed, so triage stalled.

2. Dead-letter / needs_reconciliation backlog was pull-only with no active
   alert — systemHealth tops out at "warning" on backlog, so a growing pile of
   misdirected/stuck sends looked like routine queue depth and paged nobody.
   The inspect view now surfaces an error-severity alert when active
   dead-letter items are present. Fail-closed: the alert threshold defaults to
   1 (any active item alerts) and can never be configured below 1, so absence
   or corruption of config can never silently disable the rail.
"""

import importlib.util
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

if importlib.util.find_spec("flask"):
    import app
else:  # pragma: no cover - flask always present in CI
    app = None


class FakeDoc:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = dict(data)

    def to_dict(self):
        return dict(self._data)


class FakeCollection:
    def __init__(self, docs):
        self._docs = docs

    def limit(self, count):
        return self

    def stream(self):
        return list(self._docs)


class FakeDocRef:
    def __init__(self, collections):
        # collections: dict[name] -> list[FakeDoc]
        self._collections = collections

    def collection(self, name):
        return FakeCollection(self._collections.get(name, []))


class FakeUsersCollection:
    def __init__(self, users):
        # users: dict[uid] -> dict[collection_name] -> list[FakeDoc]
        self._users = users

    def document(self, uid):
        return FakeDocRef(self._users.get(uid, {}))


class FakeFirestore:
    def __init__(self, users):
        self._users = users

    def collection(self, name):
        assert name == "users"
        return FakeUsersCollection(self._users)


class _RaisingCollection:
    """A collection whose read fails — models Firestore being unreachable."""

    def limit(self, count):
        return self

    def stream(self):
        raise RuntimeError("Firestore unavailable: deadLetterQueue read failed")


class _EmptyCollection:
    def limit(self, count):
        return self

    def stream(self):
        return []


class _RaisingDocRef:
    def collection(self, name):
        if name == "deadLetterQueue":
            return _RaisingCollection()
        return _EmptyCollection()


class _RaisingUsersCollection:
    def document(self, uid):
        return _RaisingDocRef()


class _RaisingFirestore:
    """Firestore double whose deadLetterQueue read raises for every user, to
    prove the inspect endpoint fails CLOSED (active error alert) rather than
    reporting a clean board when a queue cannot be read."""

    def collection(self, name):
        assert name == "users"
        return _RaisingUsersCollection()


class DeadLetterReasonTests(unittest.TestCase):
    """The reason must never render blank when the real keys are populated."""

    def test_reads_failure_reason_key(self):
        data = {"failureReason": "Recipient rejected: 550 no such user"}
        self.assertEqual(
            "Recipient rejected: 550 no such user",
            app._dead_letter_reason(data),
        )

    def test_falls_back_to_last_error(self):
        data = {"lastError": "Graph 429 rate limited"}
        self.assertEqual("Graph 429 rate limited", app._dead_letter_reason(data))

    def test_falls_back_to_legacy_reason_key(self):
        data = {"reason": "legacy reason string"}
        self.assertEqual("legacy reason string", app._dead_letter_reason(data))

    def test_prefers_failure_reason_over_others(self):
        data = {
            "failureReason": "primary",
            "lastError": "secondary",
            "reason": "tertiary",
        }
        self.assertEqual("primary", app._dead_letter_reason(data))

    def test_blank_only_when_no_reason_present(self):
        self.assertEqual("", app._dead_letter_reason({}))

    def test_old_reason_key_lookup_would_have_been_blank(self):
        # Guards against regressing to `data.get("reason")` — the real shape
        # has no `reason` key, so the old code returned "".
        data = {"failureReason": "the real reason"}
        self.assertEqual("", data.get("reason", ""))
        self.assertNotEqual("", app._dead_letter_reason(data))


class DeadLetterAlertThresholdTests(unittest.TestCase):
    def test_default_threshold_is_one(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(1, app._dead_letter_alert_threshold())

    def test_env_override_raises_bar(self):
        with patch.dict(os.environ, {"DEAD_LETTER_ALERT_THRESHOLD": "5"}):
            self.assertEqual(5, app._dead_letter_alert_threshold())

    def test_zero_clamps_to_one_fail_closed(self):
        with patch.dict(os.environ, {"DEAD_LETTER_ALERT_THRESHOLD": "0"}):
            self.assertEqual(1, app._dead_letter_alert_threshold())

    def test_negative_clamps_to_one_fail_closed(self):
        with patch.dict(os.environ, {"DEAD_LETTER_ALERT_THRESHOLD": "-9"}):
            self.assertEqual(1, app._dead_letter_alert_threshold())

    def test_garbage_clamps_to_one_fail_closed(self):
        with patch.dict(os.environ, {"DEAD_LETTER_ALERT_THRESHOLD": "not-a-number"}):
            self.assertEqual(1, app._dead_letter_alert_threshold())


class DeadLetterAlertTests(unittest.TestCase):
    def test_alert_fires_error_on_single_active_item(self):
        alert = app._dead_letter_alert(active_count=1, needs_reconciliation_count=0, threshold=1)
        self.assertIsNotNone(alert)
        self.assertEqual("error", alert["severity"])
        self.assertEqual(1, alert["activeDeadLetters"])

    def test_no_alert_when_nothing_active(self):
        self.assertIsNone(
            app._dead_letter_alert(active_count=0, needs_reconciliation_count=0, threshold=1)
        )

    def test_no_alert_below_raised_threshold(self):
        self.assertIsNone(
            app._dead_letter_alert(active_count=2, needs_reconciliation_count=0, threshold=5)
        )

    def test_alert_at_raised_threshold(self):
        alert = app._dead_letter_alert(active_count=5, needs_reconciliation_count=2, threshold=5)
        self.assertIsNotNone(alert)
        self.assertEqual(2, alert["needsReconciliation"])

    def test_read_error_forces_alert_fail_closed(self):
        # If we could not read the queue, we must NOT report all-clear.
        alert = app._dead_letter_alert(
            active_count=0, needs_reconciliation_count=0, threshold=1, read_error=True
        )
        self.assertIsNotNone(alert)
        self.assertEqual("error", alert["severity"])


@unittest.skipIf(app is None, "flask is not installed")
class DeadLetterInspectEndpointTests(unittest.TestCase):
    """End-to-end: a stuck send in the dead-letter queue must render its reason
    and raise an active alert through the debug endpoint."""

    def _run(self, users):
        fake_fs = FakeFirestore(users)

        with patch("app.SCHEDULER_AVAILABLE", True), patch.dict(
            os.environ, {"DEAD_LETTER_ALERT_THRESHOLD": "1"}
        ), patch("email_automation.clients._fs", fake_fs), patch(
            "email_automation.clients.list_user_ids", return_value=list(users.keys())
        ):
            with app.app.test_client() as client:
                resp = client.get("/api/firestore-inspect")
        self.assertEqual(200, resp.status_code)
        return resp.get_json()

    def test_stuck_send_reason_and_alert_surface(self):
        users = {
            "uid-1": {
                "deadLetterQueue": [
                    FakeDoc(
                        "dl-1",
                        {
                            "failureReason": "Send blocked: recipient mismatch",
                            "status": "failed",
                        },
                    ),
                ],
            }
        }
        payload = self._run(users)
        self.assertTrue(payload["success"])
        data = payload["data"]

        items = data["users"]["uid-1"]["collections"]["deadLetterQueue"]["items"]
        self.assertEqual("Send blocked: recipient mismatch", items[0]["reason"])
        self.assertNotEqual("", items[0]["reason"])

        alert = data["alert"]
        self.assertIsNotNone(alert)
        self.assertEqual("error", alert["severity"])
        self.assertEqual(1, alert["activeDeadLetters"])

    def test_needs_reconciliation_counted_and_alerts(self):
        users = {
            "uid-1": {
                "deadLetterQueue": [
                    FakeDoc(
                        "dl-recon",
                        {
                            "failureReason": "Graph accepted reply but indexing failed",
                            "status": "needs_reconciliation",
                        },
                    ),
                ],
            }
        }
        payload = self._run(users)
        data = payload["data"]
        coll = data["users"]["uid-1"]["collections"]["deadLetterQueue"]
        self.assertEqual(1, coll["needsReconciliation"])
        self.assertEqual(1, coll["activeCount"])
        self.assertEqual("error", data["alert"]["severity"])

    def test_read_error_forces_alert_fail_closed_at_endpoint(self):
        # CodeRabbit PR#18: prove the "health cannot lie" invariant end-to-end at
        # the endpoint boundary, not just via the _dead_letter_alert helper. A
        # Firestore read failure on deadLetterQueue must surface an active error
        # alert instead of a clean success board.
        fake_fs = _RaisingFirestore()
        with patch("app.SCHEDULER_AVAILABLE", True), patch.dict(
            os.environ, {"DEAD_LETTER_ALERT_THRESHOLD": "1"}
        ), patch("email_automation.clients._fs", fake_fs), patch(
            "email_automation.clients.list_user_ids", return_value=["uid-1"]
        ):
            with app.app.test_client() as client:
                resp = client.get("/api/firestore-inspect")
        self.assertEqual(200, resp.status_code)
        payload = resp.get_json()
        data = payload["data"]

        # The unreadable queue is surfaced as an error, never silently dropped.
        coll = data["users"]["uid-1"]["collections"]["deadLetterQueue"]
        self.assertIn("error", coll)

        # Fail-closed: an error-severity alert is raised despite zero countable
        # active items, because the queue could not be ruled out.
        alert = data["alert"]
        self.assertIsNotNone(alert)
        self.assertEqual("error", alert["severity"])
        self.assertTrue(alert.get("readError"))

    def test_resolved_items_do_not_alert(self):
        users = {
            "uid-1": {
                "deadLetterQueue": [
                    FakeDoc(
                        "dl-done",
                        {"failureReason": "was stuck", "status": "reconciled"},
                    ),
                ],
            }
        }
        payload = self._run(users)
        data = payload["data"]
        coll = data["users"]["uid-1"]["collections"]["deadLetterQueue"]
        # Item still visible with its reason...
        self.assertEqual("was stuck", coll["items"][0]["reason"])
        self.assertEqual(0, coll["activeCount"])
        # ...but no active alert.
        self.assertIsNone(data["alert"])


if __name__ == "__main__":
    unittest.main()
