import os

os.environ.setdefault("E2E_TEST_MODE", "true")

import unittest
from unittest.mock import patch

from email_automation import sent_mail_guard


# ─────────────────────────────────────────────────────────────────────────────
# Rubric cell: core.reply_all_cc / duplicate_retry
# Behavior: sent_mail_guard — a reply whose match already exists in SentItems
#           reconciles without resending.
#
# Only the Graph HTTP boundary (`sent_mail_guard.requests.get`) is faked. The
# functions under test — find_matching_sent_message_for_retry and
# send_result_from_sent_match — are the REAL production code, exercised end to
# end: query building, recipient/subject/body matching, and reconciliation.
#
# reply_all_cc angle: the retry recipient appears ONLY on the ccRecipients line
# of the already-sent reply-all (not the To line). The guard must still match on
# that CC'd party via the real _message_recipients() union, so a reply-all with
# CC parties is recognized as already-sent and reconciled instead of resent.
# ─────────────────────────────────────────────────────────────────────────────


class FakeGraphResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {}
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _sent_reply_all_message(*, cc_recipient, subject, body):
    """A Sent Items message representing an already-sent reply-all whose CC line
    includes `cc_recipient` (the party we're about to retry a send to)."""
    return {
        "id": "AAMkSENT-reply-all-1",
        "internetMessageId": "<reply-all-1@tenant.example.test>",
        "conversationId": "conv-reply-all-1",
        "subject": subject,
        "sentDateTime": "2026-07-02T10:15:00Z",
        "toRecipients": [
            {"emailAddress": {"address": "broker@brokerage.example.test"}}
        ],
        "ccRecipients": [
            {"emailAddress": {"address": cc_recipient}},
            {"emailAddress": {"address": "assistant@brokerage.example.test"}},
        ],
        "bccRecipients": [],
        "body": {"contentType": "text", "content": body},
        "bodyPreview": body,
    }


class CoreReplyAllCcDuplicateRetryTests(unittest.TestCase):
    """Rubric cell core.reply_all_cc / duplicate_retry.

    Proves the sent_mail_guard reconciles a duplicate retry: when the reply-all
    (with the retry recipient on its CC line) already sits in Sent Items, the
    real guard returns that message's identity and produces a send-result that
    marks the recipient as already-sent — so the caller reconciles instead of
    firing a second send. A negative control (Sent Items lacking the reply)
    returns None, i.e. a real send would proceed — making the match meaningful.
    """

    HEADERS = {"Authorization": "Bearer test-token"}
    CC_RECIPIENT = "cc-party@client.example.test"
    SUBJECT = "Re: Coverage terms for account #4471"
    BODY = (
        "Confirming the bound coverage terms and effective date for account "
        "#4471 as discussed on the reply-all thread. Please let us know if the "
        "CC'd parties need anything further before we proceed."
    )

    def _run_guard(self, sent_items_value):
        captured = {}

        def fake_get(url, headers=None, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            captured["calls"] = captured.get("calls", 0) + 1
            return FakeGraphResponse({"value": sent_items_value})

        with patch.object(sent_mail_guard.requests, "get", side_effect=fake_get):
            match = sent_mail_guard.find_matching_sent_message_for_retry(
                self.HEADERS,
                recipient=self.CC_RECIPIENT,
                body=self.BODY,
                subject=self.SUBJECT,
                conversation_id="conv-reply-all-1",
            )
        return match, captured

    def test_existing_sent_reply_all_reconciles_without_resending(self):
        # ── Positive: the reply-all (CC'd recipient) already exists in SentItems.
        sent_items = [
            _sent_reply_all_message(
                cc_recipient=self.CC_RECIPIENT,
                subject=self.SUBJECT,
                body=self.BODY,
            )
        ]
        match, captured = self._run_guard(sent_items)

        # The real HTTP boundary was actually exercised (query was built + issued).
        self.assertGreaterEqual(captured.get("calls", 0), 1)
        self.assertIn("SentItems", captured["url"])

        # A match is found even though the recipient is only on the CC line.
        self.assertIsNotNone(
            match,
            "an already-sent reply-all whose CC line contains the retry "
            "recipient must be recognized as a Sent Items match",
        )
        self.assertEqual(match["sentMessageId"], "AAMkSENT-reply-all-1")

        # Reconciliation: the real send-result marks the recipient as already
        # sent (carrying the existing Sent message id) — no resend is needed.
        result = sent_mail_guard.send_result_from_sent_match(match, self.CC_RECIPIENT)
        self.assertEqual(result["sent"], [self.CC_RECIPIENT])
        self.assertEqual(
            result["sentMessageIds"][self.CC_RECIPIENT], "AAMkSENT-reply-all-1"
        )

        # ── Negative control: same code path, but Sent Items does NOT contain
        # this reply (different body). The guard returns None → the caller would
        # actually resend. This is what makes the positive assertion discriminating.
        other_body = "Totally unrelated message body that shares no reply text."
        no_match, _ = self._run_guard(
            [
                _sent_reply_all_message(
                    cc_recipient=self.CC_RECIPIENT,
                    subject=self.SUBJECT,
                    body=other_body,
                )
            ]
        )
        self.assertIsNone(
            no_match,
            "when Sent Items has no matching reply, the guard must NOT report a "
            "match — otherwise reconciliation would suppress a genuinely-needed send",
        )
        self.assertEqual(
            {},
            sent_mail_guard.send_result_from_sent_match(no_match, self.CC_RECIPIENT),
            "no match must yield no reconciled send-result",
        )


if __name__ == "__main__":
    unittest.main()
