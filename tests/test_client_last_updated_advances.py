"""The client-facing "Last updated" timestamp has to advance when things happen.

LIVE break, 2026-08-06 production campaign, PROD-0806-8: "stale client
`lastUpdated` despite `completedAt`" / "the client-facing last-updated timestamp
remained stale because `lastUpdated` did not advance with completion". The
record filed it as claimed by BOTH lanes with the ownership split unresolved.

It resolves to a field-name mismatch across the boundary, and the backend owns
the whole of it:

  * The dashboard renders `client.lastUpdated` (ClientRow.jsx). That value comes
    from the `getUserData` callable, which reads
    `data.lastUpdated?.toDate?.() || createdAt` -- so a client document with no
    `lastUpdated` silently displays its CREATION time. "Stayed at launch" is not
    a stuck clock; it is the fallback branch, working exactly as written.
  * The only writer of `lastUpdated` is AddClientModal, at campaign creation.
  * The backend writes `updatedAt`, `statusUpdatedAt`, `completedAt` and
    `lastNotificationAt` on the client document, and never once `lastUpdated`.

So the campaign runs, the sheet fills in, the campaign completes, and the column
the operator actually looks at reports the moment they pressed launch. This is
the same defect class as PROD-0806-6, where the alias list said `ops ex /sf` and
the real header said `Ops Ex / SF`: a silent name mismatch that leaves a value
looking finished.

`SERVER_TIMESTAMP` is the right type -- it is what AddClientModal already writes
and what `.toDate?.()` on the read side already expects.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import notifications, processing


class _Snapshot:
    def __init__(self, data=None, exists=True):
        self._data, self.exists = data or {}, exists

    def to_dict(self):
        return dict(self._data)


class _Query:
    def __init__(self, docs=()):
        self._docs = list(docs)

    def where(self, *args, **kwargs):
        return self

    def stream(self):
        return iter(self._docs)


class _ClientRef:
    """Records every payload merged onto the client document."""

    def __init__(self, data=None, notifications_docs=()):
        self.writes = []
        self._data = data or {}
        self._notifications = list(notifications_docs)

    def get(self, transaction=None):
        return _Snapshot(self._data)

    def collection(self, _name):
        return _Query(self._notifications)

    def set(self, payload, merge=False):
        self.writes.append(payload)

    def merged(self):
        merged = {}
        for payload in self.writes:
            merged.update(payload)
        return merged


class CompletionAdvancesTheDisplayedTimestamp(unittest.TestCase):
    def _complete(self, thread_status="completed"):
        client_ref = _ClientRef()
        threads = _Query([_Snapshot({"status": thread_status, "clientId": "client-1"})])
        with patch.object(processing, "_fs", MagicMock()):
            done = processing._maybe_mark_client_completed(
                "uid-1", "client-1",
                client_ref=client_ref,
                threads_ref=threads,
                notifications_ref=_Query(),
                outbox_ref=_Query(),
                pending_responses_ref=_Query(),
                dead_letter_ref=_Query(),
            )
        return done, client_ref

    def test_completion_writes_the_field_the_dashboard_reads(self):
        done, client_ref = self._complete()
        self.assertTrue(done, "guard setup wrong: the campaign should have completed")
        written = client_ref.merged()
        self.assertIn(
            "lastUpdated", written,
            "getUserData reads `lastUpdated` and falls back to createdAt when it is "
            "absent, so a completion that writes only `updatedAt` leaves the operator "
            "looking at the moment they pressed launch",
        )
        self.assertIs(written["lastUpdated"], processing.SERVER_TIMESTAMP)

    def test_completion_still_writes_everything_it_did_before(self):
        _, client_ref = self._complete()
        written = client_ref.merged()
        for key in ("status", "completedAt", "statusUpdatedAt", "updatedAt", "completionSummary"):
            with self.subTest(key=key):
                self.assertIn(key, written)
        self.assertEqual(written["status"], "completed")

    def test_an_incomplete_campaign_writes_nothing(self):
        done, client_ref = self._complete(thread_status="active")
        self.assertFalse(done)
        self.assertEqual(client_ref.writes, [], "an active campaign must not be stamped completed")


class SheetActivityAdvancesTheDisplayedTimestamp(unittest.TestCase):
    """Between launch and completion, applied sheet updates are the campaign's pulse."""

    def test_applied_updates_advance_the_field(self):
        client_ref = _ClientRef()
        fs = MagicMock()
        fs.collection.return_value.document.return_value.collection.return_value.document.return_value = client_ref
        with patch.object(notifications, "write_notification", return_value="notif-1"), \
             patch.object(notifications, "_fs", fs):
            notifications.add_client_notifications(
                uid="uid-1", client_id="client-1",
                email="broker@example.invalid", thread_id="thread-1",
                applied_updates=[{
                    "range": "Campaign!G42", "column": "Asking Rent",
                    "oldValue": "", "newValue": "$12.50",
                    "reason": "Broker replied", "confidence": 0.92,
                }],
                address="Row anchor",
            )
        written = client_ref.merged()
        self.assertIn(
            "lastUpdated", written,
            "a sheet column changing is the most common thing that happens to a live "
            "campaign; if it does not move the timestamp, the dashboard reports launch "
            "for the campaign's entire working life",
        )
        self.assertIs(written["lastUpdated"], notifications.SERVER_TIMESTAMP)
        self.assertIn("lastNotificationSummary", written, "the existing summary must survive")

    def test_no_applied_updates_writes_nothing(self):
        client_ref = _ClientRef()
        fs = MagicMock()
        fs.collection.return_value.document.return_value.collection.return_value.document.return_value = client_ref
        with patch.object(notifications, "write_notification", return_value="notif-1"), \
             patch.object(notifications, "_fs", fs):
            notifications.add_client_notifications(
                uid="uid-1", client_id="client-1",
                email="broker@example.invalid", thread_id="thread-1",
                applied_updates=[],
            )
        self.assertEqual(client_ref.writes, [])


class NotificationsAdvanceTheDisplayedTimestamp(unittest.TestCase):
    def test_writing_a_notification_advances_the_field(self):
        fs = MagicMock()
        notif_ref = MagicMock()
        notif_ref.get.return_value = _Snapshot(exists=False)
        fs.collection.return_value.document.return_value.collection.return_value.document.return_value = notif_ref
        with patch.object(notifications, "_fs", fs):
            notifications.write_notification(
                "uid-1", "client-1", kind="action_needed", priority="important",
                email="broker@example.invalid", thread_id="thread-1",
            )
        payloads = [
            call.args[1] for call in fs.transaction.return_value.set.call_args_list
            if len(call.args) > 1 and isinstance(call.args[1], dict)
        ]
        counter_writes = [p for p in payloads if "notificationsUnread" in p]
        self.assertTrue(counter_writes, "setup wrong: the counter write did not happen")
        self.assertIn(
            "lastUpdated", counter_writes[-1],
            "a notification the operator has to act on is the clearest possible "
            "evidence that the campaign is not sitting where it was at launch",
        )


if __name__ == "__main__":
    unittest.main()
