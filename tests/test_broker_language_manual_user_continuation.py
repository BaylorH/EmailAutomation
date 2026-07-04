"""Pressure test: manual_user_continuation broker-event class.

Deterministic guard under test:
    email_automation.sent_mail_guard.find_sent_conversation_continuation_for_retry

Safety contract (from the guard docstring + call sites in email.py /
followup.py / pending_responses.py / processing.py):

  * When the user has manually continued a conversation (i.e. a message exists
    in the sender's Sent Items in the SAME conversation, sent at/after the
    failed/queued retry point), the guard MUST return a non-None identity so
    the scheduler / retry loop STOPS and moves the stale draft to manual review.
    Missing this == stopIf "scheduler sends stale draft after manual reply"
    (a HIGH false-negative safety hole).

  * When there is NO genuine user continuation after the failure point
    (only a system send from before the retry point, or quoted text living in
    an unrelated conversation), the guard MUST return None so legitimate
    automation is not blocked.

Everything external (Microsoft Graph via requests.get) is faked. No real
network / Firestore / Sheets / Graph calls.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

# Ensure the package import works even outside the repo runner.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from email_automation import sent_mail_guard
from email_automation.sent_mail_guard import (
    find_sent_conversation_continuation_for_retry,
    SentMailGuardLookupError,
)

TARGET_CONV = "AAQkAGManualContinuationTARGET=="
OTHER_CONV = "AAQkAGSomeUnrelatedBrokerThread=="
# The failed/queued retry point. A real user continuation is anything the user
# sent at/after this instant in TARGET_CONV.
SENT_AFTER = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def msg(
    *,
    conversation_id=TARGET_CONV,
    sent_dt="2026-07-01T12:05:00Z",
    subject="Re: 123 Main St availability",
    to=("broker@acme.com",),
    body=None,
    include_body=False,
):
    """Build a Sent Items message the way Graph returns it (metadata-shaped)."""
    m = {
        "id": "msg-" + str(abs(hash((conversation_id, sent_dt, subject)))),
        "internetMessageId": "<gen@contoso.com>",
        "conversationId": conversation_id,
        "subject": subject,
        "sentDateTime": sent_dt,
        "toRecipients": [
            {"emailAddress": {"address": addr}} for addr in to
        ],
    }
    if include_body:
        m["body"] = {"contentType": "text", "content": body or ""}
    return m


class FakeResponse:
    def __init__(self, value, status_code=200):
        self._value = value
        self.status_code = status_code
        self.headers = {}
        self.text = ""

    def json(self):
        return {"value": self._value}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests as _rq

            raise _rq.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)


class _Graph:
    """Fake Graph endpoint that records the query params it was called with."""

    def __init__(self, value):
        self.value = value
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        return FakeResponse(self.value)


def run_guard(value, *, conversation_id=TARGET_CONV, sent_after=SENT_AFTER):
    """Drive the REAL guard with a faked Sent Items response set."""
    graph = _Graph(value)
    with mock.patch.object(sent_mail_guard.requests, "get", graph.get):
        result = find_sent_conversation_continuation_for_retry(
            {"Authorization": "Bearer fake"},
            conversation_id=conversation_id,
            sent_after=sent_after,
        )
    return result, graph


# ---------------------------------------------------------------------------
# 15+ realistic real-threat phrasings. Each is a genuine manual continuation:
# the user sent SOMETHING in the target conversation at/after the failure point.
# Metadata-only guard => we vary subject shape, recipients, timestamp format,
# body presence, etc. All of these MUST fire (return non-None).
# ---------------------------------------------------------------------------
REAL_THREAT_PHRASINGS = [
    ("terse reply",
     msg(subject="Re: availability", sent_dt="2026-07-01T12:05:00Z")),
    ("verbose stacked prefixes",
     msg(subject="RE: RE: Fwd: 123 Main St availability & pricing details",
         sent_dt="2026-07-01T13:00:00Z")),
    ("lowercase typo'd subject",
     msg(subject="re: availabilty  ", sent_dt="2026-07-01T12:30:00Z")),
    ("ALL CAPS subject",
     msg(subject="RE: PLEASE SEND THE LEASE ASAP",
         sent_dt="2026-07-01T14:22:00Z")),
    ("reply-all copying a teammate (multi recipient)",
     msg(to=("broker@acme.com", "teammate@myfirm.com", "assistant@myfirm.com"),
         sent_dt="2026-07-01T12:07:00Z")),
    ("no subject at all",
     msg(subject=None, sent_dt="2026-07-01T12:10:00Z")),
    ("empty-string subject",
     msg(subject="", sent_dt="2026-07-01T12:10:00Z")),
    ("ISO timestamp with +00:00 offset",
     msg(sent_dt="2026-07-01T12:05:00+00:00")),
    ("ISO timestamp with non-UTC offset (EST)",
     msg(sent_dt="2026-07-01T09:05:00-05:00")),  # == 14:05Z, after failure
    ("correction sent minutes after the failure",
     msg(subject="Re: correction on my last note",
         sent_dt="2026-07-01T12:15:00Z")),
    ("sent EXACTLY at the retry boundary",
     msg(sent_dt="2026-07-01T12:00:00Z")),
    ("message carrying a quoted body + signature block",
     msg(sent_dt="2026-07-01T12:09:00Z",
         include_body=True,
         body="Hi, adding my note.\n\n> On Jul 1 you wrote...\n\n-- \nJill\nBroker")),
    ("subject with unicode / regional chars",
     msg(subject="Re: Disponibilité 123 Main St",
         sent_dt="2026-07-01T12:11:00Z")),
    ("conversationId with special chars matched exactly",
     msg(sent_dt="2026-07-01T12:20:00Z")),
    ("missing sentDateTime but same conversation (must fail safe -> fire)",
     msg(sent_dt=None)),
    ("bcc-style single recipient terse correction",
     msg(to=("broker@acme.com",), subject="Re: scratch that",
         sent_dt="2026-07-01T12:03:00Z")),
    ("long delayed same-conversation continuation (hours later)",
     msg(sent_dt="2026-07-01T20:45:00Z")),
]


# ---------------------------------------------------------------------------
# Near-miss controls. Each MUST NOT fire (return None) or the guard blocks
# legitimate automation.
# ---------------------------------------------------------------------------
NEAR_MISS_CONTROLS = [
    ("system's own send from BEFORE the retry point (no user continuation)",
     [msg(sent_dt="2026-07-01T11:59:30Z")]),
    ("quoted user text lives only in an UNRELATED broker thread",
     [msg(conversation_id=OTHER_CONV, sent_dt="2026-07-01T13:00:00Z",
          subject="Re: totally different deal")]),
    ("empty Sent Items",
     []),
    ("only sends in other conversations after the failure point",
     [msg(conversation_id=OTHER_CONV, sent_dt="2026-07-01T12:05:00Z"),
      msg(conversation_id="AAQkAnother==", sent_dt="2026-07-01T12:06:00Z")]),
    ("system send in-conversation but a full second before boundary",
     [msg(sent_dt="2026-07-01T11:59:59Z")]),
]


class ManualContinuationRealThreatTests(unittest.TestCase):
    def test_every_real_threat_phrasing_fires(self):
        misses = []
        for label, m in REAL_THREAT_PHRASINGS:
            result, _ = run_guard([m])
            if result is None:
                misses.append(label)
        self.assertEqual(
            [], misses,
            "FALSE NEGATIVE: guard failed to detect manual continuation for "
            "phrasings (scheduler would send stale draft): " + "; ".join(misses),
        )

    def test_fires_when_user_reply_buried_under_own_prefixed_reply(self):
        # Two sends in target conv after failure; guard should still fire.
        value = [
            msg(subject="RE: Re: nudge", sent_dt="2026-07-01T15:00:00Z"),
            msg(subject="Re: availability", sent_dt="2026-07-01T12:05:00Z"),
        ]
        result, _ = run_guard(value)
        self.assertIsNotNone(
            result,
            "FALSE NEGATIVE: manual continuation not detected among multiple sends",
        )


class ManualContinuationNearMissTests(unittest.TestCase):
    def test_near_misses_do_not_fire(self):
        false_positives = []
        for label, value in NEAR_MISS_CONTROLS:
            result, _ = run_guard(value)
            if result is not None:
                false_positives.append(label)
        self.assertEqual(
            [], false_positives,
            "FALSE POSITIVE: guard blocked legitimate automation for: "
            + "; ".join(false_positives),
        )


class ManualContinuationHardEdgeTests(unittest.TestCase):
    """Edges that pin real safety defects. Where current behavior is WRONG,
    the assertion states the CORRECT behavior and is expected to fail RED."""

    def test_guard_scopes_sent_items_query_to_conversation(self):
        # BUG (HIGH false-negative): the Graph query filters ONLY by
        # sentDateTime and caps at $top=10 with no pagination and no
        # server-side conversationId filter. A broker/user who sends >10 emails
        # to OTHER conversations after the failure point buries the real
        # in-conversation continuation past the returned page, so the guard
        # returns None and the scheduler sends the stale draft.
        #
        # CORRECT behavior: constrain the lookup to the target conversation
        # (server-side $filter on conversationId) OR follow @odata.nextLink,
        # so a busy Sent Items cannot hide the continuation.
        _, graph = run_guard([msg()])
        params = graph.calls[-1]["params"]
        filt = params.get("$filter", "")
        top = int(params.get("$top", "0") or 0)
        scoped_by_conversation = "conversationid" in filt.lower()
        self.assertTrue(
            scoped_by_conversation,
            "SAFETY HOLE: Sent Items continuation lookup is not scoped to the "
            "conversation. filter=" + repr(filt) + ", top=" + str(top) + ". "
            "With no conversationId filter / pagination, a user who sends more "
            "than $top emails to other threads after the failure buries the real "
            "reply and the guard misses it -> scheduler sends the stale draft.",
        )

    def test_busy_sent_items_page_hides_continuation(self):
        # Concretely simulate the truncation: Graph returns the newest 10 sends,
        # all in OTHER conversations (the real continuation is on page 2 that the
        # code never fetches). Guard returns None == the stale-draft safety hole.
        # This documents the FALSE NEGATIVE produced by the unscoped/unpaged query.
        newest_ten = [
            msg(conversation_id=f"AAQkOther{i}==",
                sent_dt=_iso(SENT_AFTER + timedelta(minutes=30 + i)))
            for i in range(10)
        ]
        result, _ = run_guard(newest_ten)
        # The real Sent Items ALSO contains a TARGET_CONV continuation, but it
        # was pushed off the returned page. The safe outcome would be to detect
        # it; the guard cannot, because it never asked Graph to scope/paginate.
        # We assert the SAFE expectation so the defect surfaces RED.
        self.assertIsNotNone(
            result,
            "FALSE NEGATIVE: a real manual continuation is hidden when the user "
            "has >10 newer sends in other threads; the unscoped $top=10 query "
            "truncates it and the scheduler will send the stale draft.",
        )

    def test_fail_open_on_unusable_sent_after(self):
        # If the retry metadata yields an unusable timestamp, coerce returns None
        # and the guard returns None (fail-OPEN) -> a manual continuation that
        # DOES exist is never checked, so the stale draft can go out.
        # Correct behavior for a safety guard is fail-CLOSED: raise so the caller
        # moves the item to manual review rather than silently retrying.
        graph = _Graph([msg()])
        with mock.patch.object(sent_mail_guard.requests, "get", graph.get):
            with self.assertRaises(
                SentMailGuardLookupError,
                msg="FAIL-OPEN: unusable sent_after silently returns None; the "
                "guard should fail closed so a real manual continuation is not "
                "skipped and the stale draft is not sent.",
            ):
                find_sent_conversation_continuation_for_retry(
                    {"Authorization": "Bearer fake"},
                    conversation_id=TARGET_CONV,
                    sent_after="not-a-real-timestamp",
                )

    def test_lookup_failure_raises_not_silent(self):
        # Graph HTTP failure must raise SentMailGuardLookupError so callers fail
        # closed (move to manual review) rather than treating "no continuation
        # found" as safe. This pins the fail-closed contract that IS honored.
        def boom(url, headers=None, params=None, timeout=None):
            return FakeResponse([], status_code=503)

        with mock.patch.object(sent_mail_guard.requests, "get", boom):
            with self.assertRaises(SentMailGuardLookupError):
                find_sent_conversation_continuation_for_retry(
                    {"Authorization": "Bearer fake"},
                    conversation_id=TARGET_CONV,
                    sent_after=SENT_AFTER,
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
