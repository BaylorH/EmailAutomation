"""Pressure test: token_or_graph_failure event class.

This event class covers MSAL/Graph infrastructure failures that must NEVER
silently drop or hide a broker reply, and must NEVER let the scheduler widen
to or blindly retry unsafe users. Three deterministic guards decide/guard it:

1. sent_mail_guard.find_matching_sent_message_for_retry
   Seed: "Graph send returns accepted but readback fails."
   Safety contract: when the Sent Items readback itself fails (401 expired
   token, 403, 5xx, network error, malformed body) the guard MUST fail CLOSED
   by raising SentMailGuardLookupError so the caller routes to manual review
   instead of blindly re-sending (duplicate) or dropping the reply. When a
   prior send IS found it MUST return an identity (so the caller stops and does
   not re-send). A clean empty readback (no prior send) MUST return None so a
   legitimate first send can proceed -- that is the near-miss control.

2. scheduler_scope.resolve_scheduler_user_ids
   stopIf: "scheduler widens or retries unsafe users."
   Safety contract: manual/dev runs must not widen to non-allowlisted users or
   users without token caches; normal scheduled runs stay all-user (one user's
   missing token does not break the rest -- near-miss control).

3. file_handling.fetch_pdf_attachments
   Seed: "Graph attachment download returns 401."
   Near-miss: "Attachment failure should not mark message fully processed."
   Safety contract: a 401/token failure fetching attachments must be
   DISTINGUISHABLE from the genuine no-attachments case, so downstream does not
   mark the message fully processed while an attachment silently went missing.

Everything external (requests.get / time.sleep) is faked. No real Graph,
Firestore, or Sheets calls. Assertions that reflect the CORRECT behavior are
left RED where current behavior is unsafe; those are recorded as bugs.
"""

import unittest
from unittest.mock import patch

import requests

from email_automation import sent_mail_guard, scheduler_scope, file_handling
from email_automation.sent_mail_guard import (
    find_matching_sent_message_for_retry,
    SentMailGuardLookupError,
)
from email_automation.scheduler_scope import (
    resolve_scheduler_user_ids,
    SchedulerScopeError,
)


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None,
                 text="", json_exc=None):
        self.status_code = status_code
        self._json = {} if json_data is None else json_data
        self.headers = headers or {}
        self.text = text
        self._json_exc = json_exc

    def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"HTTP {self.status_code}", response=self
            )


def _sent_items_payload(msg):
    return FakeResponse(200, {"value": [msg]})


# A realistic "prior send" as it would appear in Sent Items.
GOOD_MATCH_MESSAGE = {
    "id": "AAMkSENT-abc-123",
    "internetMessageId": "<sent-abc-123@graph.microsoft.com>",
    "conversationId": "CONV-REALTHREAT-1",
    "subject": "RE: 123 Main St - Suite 400 availability",
    "toRecipients": [{"emailAddress": {"address": "broker@acmecre.com"}}],
    "body": {"content": (
        "<p>Yes, Suite 400 is still available. Happy to set up a tour next "
        "week -- would Tuesday or Wednesday work better for you?</p>"
    )},
    "bodyPreview": "Yes, Suite 400 is still available. Happy to set up a tour",
    "sentDateTime": "2026-07-01T15:00:00Z",
}

MATCH_BODY = ("Yes, Suite 400 is still available. Happy to set up a tour next "
              "week -- would Tuesday or Wednesday work better for you?")
MATCH_SUBJECT = "RE: 123 Main St - Suite 400 availability"
MATCH_CONV = "CONV-REALTHREAT-1"

HEADERS = {"Authorization": "Bearer faketoken"}


# --------------------------------------------------------------------------- #
# GUARD 1: sent_mail_guard.find_matching_sent_message_for_retry               #
# Real-threat readback failures -> MUST fail closed (raise).                   #
# --------------------------------------------------------------------------- #
@patch("time.sleep", lambda *a, **k: None)
class TestReadbackFailsClosed(unittest.TestCase):
    """Every way the Graph readback can fail must raise (fail closed)."""

    # (label, requests.get side_effect / return)
    HTTP_STATUS_THREATS = [
        ("MSAL token expired mid-run -> 401 on Sent Items readback", 401),
        ("graph readback 403 forbidden", 403),
        ("graph readback 429 rate limited (persistent)", 429),
        ("graph readback 500 internal error", 500),
        ("graph readback 503 service unavailable", 503),
        ("graph readback 502 bad gateway", 502),
        ("graph readback 404 mailbox not found", 404),
    ]

    def _run_guard(self):
        return find_matching_sent_message_for_retry(
            HEADERS,
            recipient="broker@acmecre.com",
            body=MATCH_BODY,
            subject=MATCH_SUBJECT,
            conversation_id=MATCH_CONV,
            attempts=1,
        )

    def test_http_error_statuses_fail_closed(self):
        for label, status in self.HTTP_STATUS_THREATS:
            with self.subTest(label=label):
                resp = FakeResponse(status_code=status,
                                    headers={"Retry-After": "0"})
                with patch.object(sent_mail_guard.requests, "get",
                                  return_value=resp):
                    with self.assertRaises(
                        SentMailGuardLookupError,
                        msg=f"{label!r}: readback failure must fail CLOSED, "
                            f"not return None (would let caller re-send/drop)",
                    ):
                        self._run_guard()

    def test_network_exceptions_fail_closed(self):
        exc_threats = [
            ("connection reset mid-readback",
             requests.exceptions.ConnectionError("connection reset")),
            ("graph readback timeout",
             requests.exceptions.Timeout("read timed out")),
            ("TLS/SSL error talking to graph",
             requests.exceptions.SSLError("ssl handshake failed")),
        ]
        for label, exc in exc_threats:
            with self.subTest(label=label):
                with patch.object(sent_mail_guard.requests, "get",
                                  side_effect=exc):
                    with self.assertRaises(
                        SentMailGuardLookupError,
                        msg=f"{label!r}: network failure must fail CLOSED",
                    ):
                        self._run_guard()

    def test_malformed_200_body_fails_closed(self):
        # 200 OK but body is not valid JSON (proxy/garbage) -> cannot verify.
        resp = FakeResponse(200, json_exc=ValueError("No JSON object could be decoded"))
        with patch.object(sent_mail_guard.requests, "get", return_value=resp):
            with self.assertRaises(
                SentMailGuardLookupError,
                msg="malformed 200 readback body must fail CLOSED",
            ):
                self._run_guard()


# --------------------------------------------------------------------------- #
# GUARD 1: identity gate -- cannot verify without unique identity.            #
# --------------------------------------------------------------------------- #
@patch("time.sleep", lambda *a, **k: None)
class TestReadbackIdentityGate(unittest.TestCase):
    def test_insufficient_identity_fails_closed(self):
        # No subject, no conversation id, short body -> not enough to prove a
        # prior send; guard must refuse rather than proceed blindly.
        resp = _sent_items_payload(GOOD_MATCH_MESSAGE)
        with patch.object(sent_mail_guard.requests, "get", return_value=resp):
            with self.assertRaises(
                SentMailGuardLookupError,
                msg="no subject/conv + short body must fail CLOSED",
            ):
                find_matching_sent_message_for_retry(
                    HEADERS,
                    recipient="broker@acmecre.com",
                    body="Yes.",
                    subject=None,
                    conversation_id=None,
                    attempts=1,
                )


# --------------------------------------------------------------------------- #
# GUARD 1: prior-send FOUND -> must return identity (stop, do not re-send).   #
# These are the phrasing variations of "accepted but readback (eventually)    #
# proves the send landed".                                                     #
# --------------------------------------------------------------------------- #
@patch("time.sleep", lambda *a, **k: None)
class TestReadbackFindsPriorSend(unittest.TestCase):
    def _find(self, body=MATCH_BODY, subject=MATCH_SUBJECT, conv=MATCH_CONV,
              message=GOOD_MATCH_MESSAGE):
        resp = _sent_items_payload(message)
        with patch.object(sent_mail_guard.requests, "get", return_value=resp):
            return find_matching_sent_message_for_retry(
                HEADERS,
                recipient="broker@acmecre.com",
                body=body,
                subject=subject,
                conversation_id=conv,
                attempts=1,
            )

    def test_exact_match_returns_identity(self):
        result = self._find()
        self.assertIsNotNone(
            result, "exact prior send in Sent Items must be detected (else "
                    "caller re-sends a duplicate to the broker)")
        self.assertEqual(result.get("id"), "AAMkSENT-abc-123")

    def test_match_with_signature_appended(self):
        # Real sends often have a signature block appended after our body.
        msg = dict(GOOD_MATCH_MESSAGE)
        msg["body"] = {"content": GOOD_MATCH_MESSAGE["body"]["content"]
                       + "<p>--<br>Jane Agent<br>Acme CRE</p>"}
        result = self._find(message=msg)
        self.assertIsNotNone(
            result, "prior send with signature appended must still match")

    def test_match_by_subject_with_re_prefix_drift(self):
        # We stored subject without RE:, Sent Items has RE: prefix.
        result = self._find(subject="123 Main St - Suite 400 availability")
        self.assertIsNotNone(
            result, "RE:/FW: prefix drift must not defeat prior-send match")


# --------------------------------------------------------------------------- #
# GUARD 1: NEAR-MISS controls -- must NOT falsely claim a prior send, and     #
# a clean empty readback must return None so a first send can proceed.        #
# --------------------------------------------------------------------------- #
@patch("time.sleep", lambda *a, **k: None)
class TestReadbackNearMisses(unittest.TestCase):
    def _find(self, message_list, body=MATCH_BODY, subject=MATCH_SUBJECT,
              conv=MATCH_CONV, recipient="broker@acmecre.com"):
        resp = FakeResponse(200, {"value": message_list})
        with patch.object(sent_mail_guard.requests, "get", return_value=resp):
            return find_matching_sent_message_for_retry(
                HEADERS,
                recipient=recipient,
                body=body,
                subject=subject,
                conversation_id=conv,
                attempts=1,
            )

    def test_empty_sent_items_returns_none(self):
        # No prior send at all -> legitimate first send should proceed.
        self.assertIsNone(
            self._find([]),
            "clean empty readback must return None (not fail closed, not "
            "falsely claim sent) so the reply can actually be sent")

    def test_different_recipient_not_matched(self):
        other = dict(GOOD_MATCH_MESSAGE)
        other["toRecipients"] = [
            {"emailAddress": {"address": "someoneelse@other.com"}}]
        self.assertIsNone(
            self._find([other]),
            "a sent message to a DIFFERENT recipient must not be treated as "
            "our prior send")

    def test_different_conversation_not_matched(self):
        other = dict(GOOD_MATCH_MESSAGE)
        other["conversationId"] = "CONV-SOMETHING-ELSE"
        self.assertIsNone(
            self._find([other]),
            "a sent message in a DIFFERENT conversation must not match")

    def test_different_body_not_matched(self):
        other = dict(GOOD_MATCH_MESSAGE)
        other["conversationId"] = MATCH_CONV
        other["body"] = {"content": "<p>Sorry, that space just leased.</p>"}
        other["bodyPreview"] = "Sorry, that space just leased."
        self.assertIsNone(
            self._find([other]),
            "an unrelated reply body in the same thread must not be treated "
            "as our prior send (would hide that our reply never sent)")


# --------------------------------------------------------------------------- #
# GUARD 2: scheduler_scope.resolve_scheduler_user_ids                         #
# stopIf: scheduler widens or retries unsafe users.                           #
# --------------------------------------------------------------------------- #
class TestSchedulerScope(unittest.TestCase):
    AVAILABLE = ["userA", "userB", "NO7lVYVp6BaplKYEfMlWCgBnpdh2"]

    def _env(self, **overrides):
        base = {
            "GITHUB_EVENT_NAME": "",
            "SITESIFT_DEV_SCOPED_SCHEDULER": "",
            "SITESIFT_SCHEDULER_ALLOWED_USER_IDS": "",
            "SITESIFT_SCHEDULER_TARGET_USER_IDS": "",
        }
        base.update(overrides)
        return base

    def test_manual_dispatch_without_guard_is_blocked(self):
        # Real threat: a manual workflow_dispatch that is NOT dev-scoped must
        # not run (could widen to all users unsafely).
        with patch.dict("os.environ",
                        self._env(GITHUB_EVENT_NAME="workflow_dispatch"),
                        clear=True):
            with self.assertRaises(
                SchedulerScopeError,
                msg="unscoped manual dispatch must be blocked"):
                resolve_scheduler_user_ids(self.AVAILABLE)

    def test_dev_scoped_rejects_non_allowlisted_user(self):
        # Real threat: dev-scoped run requesting a user NOT on the allowlist
        # must be rejected (do not widen to unsafe users).
        with patch.dict("os.environ", self._env(
                GITHUB_EVENT_NAME="workflow_dispatch",
                SITESIFT_DEV_SCOPED_SCHEDULER="1",
                SITESIFT_SCHEDULER_TARGET_USER_IDS="userA",  # not allowlisted
        ), clear=True):
            with self.assertRaises(
                SchedulerScopeError,
                msg="dev-scoped run must reject a non-allowlisted target user"):
                resolve_scheduler_user_ids(self.AVAILABLE)

    def test_dev_scoped_rejects_user_without_token_cache(self):
        # Real threat: requested (allowlisted) user has no available token
        # cache -> must not fabricate a run for them.
        with patch.dict("os.environ", self._env(
                GITHUB_EVENT_NAME="workflow_dispatch",
                SITESIFT_DEV_SCOPED_SCHEDULER="1",
                SITESIFT_SCHEDULER_ALLOWED_USER_IDS="ghostUser",
                SITESIFT_SCHEDULER_TARGET_USER_IDS="ghostUser",
        ), clear=True):
            with self.assertRaises(
                SchedulerScopeError,
                msg="dev-scoped run must reject a user with no token cache"):
                resolve_scheduler_user_ids(self.AVAILABLE)

    def test_dev_scoped_requires_explicit_targets(self):
        with patch.dict("os.environ", self._env(
                GITHUB_EVENT_NAME="workflow_dispatch",
                SITESIFT_DEV_SCOPED_SCHEDULER="1",
        ), clear=True):
            with self.assertRaises(
                SchedulerScopeError,
                msg="dev-scoped run must require explicit target user ids"):
                resolve_scheduler_user_ids(self.AVAILABLE)

    # --- Near-miss controls -------------------------------------------------
    def test_normal_scheduled_run_stays_all_users(self):
        # Near-miss: a normal GitHub Actions scheduled cron run is all-user; one
        # user's missing token does not collapse the whole run. #17 hardening:
        # the all-user default only survives in the TRUSTED GitHub Actions
        # runtime (scope env pinned in a git-reviewed workflow file), so the run
        # must carry the GitHub Actions markers to keep mode=all.
        with patch.dict("os.environ", self._env(
                GITHUB_ACTIONS="true",
                GITHUB_EVENT_NAME="schedule",
        ), clear=True):
            scope = resolve_scheduler_user_ids(self.AVAILABLE)
        self.assertEqual(scope.mode, "all")
        self.assertEqual(scope.user_ids, self.AVAILABLE)

    def test_unrecognized_runtime_fails_closed(self):
        # #17 hardening: outside GitHub Actions / Cloud Run (e.g. a locally-run
        # image with prod secrets), the scheduler must NOT silently widen to
        # every live user — it fails closed before any user is touched.
        with patch.dict("os.environ", self._env(), clear=True):
            with self.assertRaises(
                SchedulerScopeError,
                msg="unrecognized runtime must fail closed, not default to all-users"):
                resolve_scheduler_user_ids(self.AVAILABLE)

    def test_dev_scoped_valid_allowlisted_user_runs_scoped(self):
        # Control: a correctly scoped dev run for an available allowlisted user
        # must be permitted and scoped to exactly that user.
        with patch.dict("os.environ", self._env(
                GITHUB_EVENT_NAME="workflow_dispatch",
                SITESIFT_DEV_SCOPED_SCHEDULER="1",
                SITESIFT_SCHEDULER_ALLOWED_USER_IDS="userA,userB",
                SITESIFT_SCHEDULER_TARGET_USER_IDS="userB",
        ), clear=True):
            scope = resolve_scheduler_user_ids(self.AVAILABLE)
        self.assertEqual(scope.mode, "dev_scoped")
        self.assertEqual(scope.user_ids, ["userB"])


# --------------------------------------------------------------------------- #
# GUARD 3: file_handling.fetch_pdf_attachments                               #
# Seed: "Graph attachment download returns 401."                              #
# Near-miss: "Attachment failure should not mark message fully processed."    #
# --------------------------------------------------------------------------- #
class TestAttachmentDownloadFailure(unittest.TestCase):
    """A 401/failure fetching attachments must be DISTINGUISHABLE from the
    genuine no-attachments case. Otherwise a token failure silently drops the
    attachment and the message proceeds as if fully processed."""

    ATTACH_THREATS = [
        ("attachment download 401 (expired token)", 401),
        ("attachment download 403 forbidden", 403),
        ("attachment download 500 server error", 500),
        ("attachment download 503 unavailable", 503),
    ]

    def test_attachment_download_failure_is_not_silently_empty(self):
        # CORRECT behavior: on a download failure the function must SIGNAL the
        # failure (raise an HTTP/Graph error) rather than return [] -- because
        # [] is exactly what a message with genuinely no PDFs returns, and the
        # caller (fetch_and_process_pdfs -> downstream) then proceeds and marks
        # the message fully processed with the attachment silently missing.
        #
        # EXPECTED RED: current code wraps the whole body in `except Exception`
        # and returns []. assertRaises therefore fails, pinning the bug.
        for label, status in self.ATTACH_THREATS:
            with self.subTest(label=label):
                resp = FakeResponse(status_code=status)
                with patch.object(file_handling.requests, "get",
                                  return_value=resp):
                    with self.assertRaises(
                        requests.exceptions.RequestException,
                        msg=f"{label!r}: attachment download failure must be "
                            f"surfaced, not swallowed into an empty list "
                            f"(near-miss: must not mark message fully "
                            f"processed)",
                    ):
                        file_handling.fetch_pdf_attachments(
                            HEADERS, "AAMkMSG-with-attachment")

    def test_network_exception_not_silently_empty(self):
        # EXPECTED RED for the same reason: a connection error while fetching
        # attachments must propagate, not collapse into [].
        with patch.object(file_handling.requests, "get",
                          side_effect=requests.exceptions.ConnectionError("reset")):
            with self.assertRaises(
                requests.exceptions.RequestException,
                msg="attachment fetch network failure must be surfaced, not "
                    "swallowed into []",
            ):
                file_handling.fetch_pdf_attachments(
                    HEADERS, "AAMkMSG-with-attachment")

    # --- Near-miss control --------------------------------------------------
    def test_genuine_no_attachments_returns_empty(self):
        # Control: a healthy 200 response with no attachments legitimately
        # returns [] and must NOT be treated as a failure.
        resp = FakeResponse(200, {"value": []})
        with patch.object(file_handling.requests, "get", return_value=resp):
            self.assertEqual(
                file_handling.fetch_pdf_attachments(HEADERS, "AAMkMSG-clean"),
                [],
                "a genuine no-attachments message must return [] cleanly")


if __name__ == "__main__":
    unittest.main(verbosity=2)
