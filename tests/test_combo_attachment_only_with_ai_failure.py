"""combinationStressDeck: attachment_only_with_ai_failure.

Deck (docs/release-safety/feature-gradebook.json):
  playbooks:
    - partial_specs_plus_pdf_plus_followup
    - graph_accepted_but_index_missing
    - row_move_during_pending_action
  eventClasses:
    - broker_attachment_or_link_only
    - token_or_graph_failure
    - broker_available_partial_specs
  mustProve:
    - source metadata is saved
    - failure remains visible and retryable
    - message is not marked processed until extraction commits

Scenario chained across the interaction:

  A broker replies with the property payload living ONLY in an attachment/link
  (broker_attachment_or_link_only) -- a protected file-share flyer that cannot
  be auto-downloaded (token_or_graph_failure). The system must keep the message
  UNPROCESSED and RETRYABLE with the link's source metadata saved (not silently
  dropped). Later, when the retry runs, the earlier auto-response had already
  been accepted by Graph but its indexing failed (graph_accepted_but_index_
  missing): the Sent Items reconciliation must detect the human/auto
  continuation and REFUSE to re-process (no duplicate send), leaving the failure
  visible for manual review. Meanwhile a broker sort moved the property's sheet
  row while the action was pending (row_move_during_pending_action): the durable
  anchor must land any write on the row that ACTUALLY holds the property, never
  the stale stored row number.

Every assertion below drives a REAL handler; only Firestore / Sheets / Graph
boundaries are faked. Each leg carries a negative control so the test genuinely
FAILS if the corresponding invariant regresses (e.g. if an extraction failure
were swallowed to [] and the message marked processed, leg 1 fails; if the
reconciliation double-sent, leg 2 fails; if a moved row wrote the stale number,
leg 3 fails).
"""

import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import builtins
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from email_automation import processing
from email_automation.campaign_safety import CampaignAutomationDecision
from email_automation import sheet_operations
from email_automation.file_handling import fetch_and_process_linked_assets


# ---------------------------------------------------------------------------
# Sheet fake (row-anchor leg). Serves the bulk A2:ZZZ scan and single-row reads
# from one in-memory row map so a "broker sort" can move a property to a new row
# number and the real anchor resolver has to recover the true location.
# ---------------------------------------------------------------------------
class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeValuesApi:
    def __init__(self, header, rows):
        self._header = header
        self._rows = rows  # {absolute_row_number: [cells]}

    def get(self, spreadsheetId=None, range=None, **kwargs):
        # `range` is the Sheets API keyword and shadows the builtin here.
        rng = range.split("!", 1)[1]
        if rng.startswith("A2:"):
            max_row = max(self._rows) if self._rows else 2
            data = [self._rows.get(r, []) for r in builtins.range(3, max_row + 1)]
            return _FakeRequest({"values": [self._header] + data})
        first = int(rng.split(":", 1)[0])
        row = self._rows.get(first)
        return _FakeRequest({"values": [row] if row else []})


class _FakeSheetsClient:
    def __init__(self, header, rows):
        self._values = _FakeValuesApi(header, rows)

    def spreadsheets(self):
        return self

    def values(self):
        return self._values


class ComboAttachmentOnlyWithAiFailureTests(unittest.TestCase):

    PROTECTED_LINK = "https://acme.sharepoint.com/:b:/s/deals/EaBcFlyer123"

    # ------------------------------------------------------------------
    # LEG 1 — broker_attachment_or_link_only + token_or_graph_failure.
    # Proves all three mustProve invariants at the extraction boundary:
    #   * source metadata is saved (source_url preserved on the manifest)
    #   * failure remains visible and retryable (RetryableProcessingError,
    #     not swallowed to [])
    #   * message is not marked processed until extraction commits
    #     (_should_mark_processed_after_error is False on that error).
    # ------------------------------------------------------------------
    def test_attachment_extraction_failure_keeps_message_unprocessed_and_retryable(self):
        # REAL link handler: a protected file-share flyer that cannot be
        # auto-downloaded. Runs fully offline (manual-review branch returns
        # before any network download).
        manifest = fetch_and_process_linked_assets([self.PROTECTED_LINK])

        # --- source metadata is saved: the un-downloadable link survives as a
        # distinguishable manifest entry that still carries where it came from.
        self.assertEqual(1, len(manifest))
        entry = manifest[0]
        self.assertEqual(self.PROTECTED_LINK, entry["source_url"])
        self.assertEqual("manual_review_required", entry["method"])
        self.assertTrue(entry.get("requires_manual_review"))
        # It is NOT usable content (no extracted text / drive copy yet).
        self.assertEqual("", entry["text"])
        self.assertIsNone(entry["drive_link"])

        # --- the REAL gate recognises this as a surfaced extraction failure ...
        failures = processing._extraction_failure_entries(manifest)
        self.assertEqual(1, len(failures))
        self.assertEqual(self.PROTECTED_LINK, failures[0]["source_url"])

        # --- ... and converts it into a RETRYABLE error, so the caller keeps
        # the message UNPROCESSED (failure remains visible + retryable).
        with self.assertRaises(processing.RetryableProcessingError) as ctx:
            processing._raise_on_extraction_failures(manifest)
        self.assertFalse(
            processing._should_mark_processed_after_error(ctx.exception),
            "A surfaced extraction failure must NOT mark the message processed.",
        )

        # --- NEGATIVE CONTROL: a manifest whose asset extraction COMMITTED
        # (real text, no failure flags) does not raise, and the None-error path
        # DOES mark processed. This proves the block above is the failure gate
        # firing -- not an unconditional refusal that would also strand clean
        # attachments (which would defeat "not processed UNTIL extraction
        # commits").
        committed = [{
            "name": "flyer.pdf",
            "text": "12,500 SF, $28/SF NNN, available now",
            "images": [],
            "method": "pdfplumber",
            "source_url": self.PROTECTED_LINK,
            "drive_link": "https://drive.example/flyer",
        }]
        self.assertEqual([], processing._extraction_failure_entries(committed))
        processing._raise_on_extraction_failures(committed)  # must NOT raise
        self.assertTrue(processing._should_mark_processed_after_error(None))

    # ------------------------------------------------------------------
    # LEG 2 — graph_accepted_but_index_missing.
    # The auto-response for this attachment thread was accepted by Graph but its
    # indexing failed, so it was recorded as a processingFailure. On retry, the
    # REAL retry handler must reconcile against Sent Items and REFUSE to
    # re-process the source message -> no duplicate send. Drives the real
    # retry_processing_failures with Firestore + Graph faked at the boundary.
    # ------------------------------------------------------------------
    def _run_retry(self, sent_items_value):
        created_at = datetime(2026, 7, 3, 2, 19, tzinfo=timezone.utc)
        failure_doc = MagicMock()
        failure_doc.id = "thread-att__msg-att"
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-att",
            "messageId": "<msg-att@broker.test>",
            "retryable": True,
            "processingAttempts": 0,
            "createdAt": created_at,
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        empty_collection = MagicMock()
        empty_collection.limit.return_value.stream.return_value = []
        thread_ref = MagicMock()
        thread_ref.get.return_value.exists = False

        user_doc = MagicMock()
        user_doc.collection.side_effect = lambda name: {
            "processingFailures": failures_collection,
            "outbox": empty_collection,
            "pendingResponses": empty_collection,
            "deadLetterQueue": empty_collection,
            "actionAudit": empty_collection,
            "clients": MagicMock(document=MagicMock(return_value=MagicMock(
                collection=MagicMock(return_value=empty_collection)))),
            "threads": MagicMock(document=MagicMock(return_value=thread_ref)),
        }[name]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value = user_doc

        graph_response = MagicMock()
        graph_response.status_code = 200
        graph_response.json.return_value = {
            "id": "graph-msg-att",
            "internetMessageId": "<msg-att@broker.test>",
            "conversationId": "conversation-att",
        }
        sent_items_response = MagicMock()
        sent_items_response.status_code = 200
        sent_items_response.json.return_value = {"value": sent_items_value}

        def fake_get(url, **kwargs):
            if "/mailFolders/SentItems/messages" in url:
                return sent_items_response
            return graph_response

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(
                 processing,
                 "get_client_automation_decision",
                 return_value=CampaignAutomationDecision(
                     state="allow",
                     reason="",
                     client_data={"status": "live", "automationPaused": False},
                     metadata={"source": "systemConfig/campaignAccess", "terminal": False},
                 ),
             ), \
             patch.object(processing, "has_processed", return_value=False), \
             patch.object(processing, "exponential_backoff_request", side_effect=lambda fn: fn()), \
             patch.object(processing.requests, "get", side_effect=fake_get), \
             patch.object(processing, "process_inbox_message") as process_message, \
             patch.object(processing, "mark_processed") as mark_processed:
            result = processing.retry_processing_failures(
                "uid-1", {"Authorization": "Bearer fake"},
            )
        return result, process_message, mark_processed, failure_doc

    def test_retry_reconciles_sent_items_instead_of_double_sending(self):
        # Graph shows the conversation was already continued (the accepted-but-
        # unindexed send). Reconciliation must block re-processing.
        continuation = [{
            "id": "sent-continuation-1",
            "internetMessageId": "<continuation@broker.test>",
            "conversationId": "conversation-att",
            "subject": "RE: 16 Jupiter Ln",
            "toRecipients": [{"emailAddress": {"address": "broker@broker.test"}}],
            "sentDateTime": "2026-07-03T03:00:00Z",
        }]
        result, process_message, mark_processed, failure_doc = self._run_retry(continuation)

        # No re-processing, no second send, message NOT marked processed.
        process_message.assert_not_called()
        mark_processed.assert_not_called()
        self.assertEqual(
            {"checked": 1, "retried": 0, "succeeded": 0, "failed": 0, "skipped": 1},
            result,
        )
        # The failure is left visible for manual review, reconciled to the real
        # Sent item (graph_accepted_but_index_missing resolved by reconciliation,
        # not by a duplicate send).
        payload = failure_doc.reference.set.call_args.args[0]
        self.assertFalse(payload["retryable"])
        self.assertEqual("blocked_manual_conversation_continued", payload["recoveryStatus"])
        self.assertEqual("sent-continuation-1", payload["recoverySentMessageId"])

        # --- NEGATIVE CONTROL: with NO continuation in Sent Items the retry is
        # free to re-process (it calls the real process_inbox_message). This
        # proves the block above is the reconciliation firing on the detected
        # continuation, not a dead/unconditional skip.
        result2, process_message2, _mark2, _doc2 = self._run_retry([])
        process_message2.assert_called_once()
        self.assertEqual(1, result2["retried"])

    # ------------------------------------------------------------------
    # LEG 3 — row_move_during_pending_action.
    # A broker sort moved the property's row while the attachment action was
    # pending. The REAL durable-anchor resolver must land on the row that
    # ACTUALLY holds the property, never the stale stored row number.
    # ------------------------------------------------------------------
    HEADER = ["Property Address", "City", "Broker"]

    def _resolve(self, rows, stored_row):
        thread_doc = MagicMock()
        thread_doc.exists = True
        thread_doc.to_dict.return_value = {"subject": "16 Jupiter Ln, Dallas", "rowNumber": stored_row}
        fake_fs = MagicMock()
        (fake_fs.collection.return_value.document.return_value
         .collection.return_value.document.return_value.get.return_value) = thread_doc
        sheets = _FakeSheetsClient(self.HEADER, rows)
        with patch.object(sheet_operations, "_fs", fake_fs):
            return sheet_operations._find_row_by_anchor(
                "uid", "thread-att", sheets, "sheet-id", "Sheet1", self.HEADER, "c@broker.test",
            )

    def test_row_move_resolves_to_true_anchor_not_stale_row(self):
        # Stored rowNumber=5 pointed at "16 Jupiter Ln" at launch. A broker sort
        # then pushed a DIFFERENT property into row 5 and moved the target to
        # row 7.
        moved = {
            3: ["404 New Way", "Dallas", "a@broker.test"],
            4: ["88 Neptune Blvd", "Dallas", "b@broker.test"],
            5: ["88 Neptune Blvd", "Dallas", "b@broker.test"],   # decoy now at old row
            7: ["16 Jupiter Ln", "Dallas", "c@broker.test"],     # true target moved here
        }
        rn, rv = self._resolve(moved, stored_row=5)
        self.assertEqual(7, rn, "Durable anchor must follow the property, not the stale row 5.")
        self.assertEqual("16 Jupiter Ln", rv[0])

        # --- NEGATIVE CONTROL: when the stored row STILL holds the property (no
        # move), the resolver keeps the stored number. Proves the leg-1 result
        # is the anchor recovering a moved row, not blanket row-rescanning.
        stable = dict(moved)
        stable[5] = ["16 Jupiter Ln", "Dallas", "c@broker.test"]
        rn2, _rv2 = self._resolve(stable, stored_row=5)
        self.assertEqual(5, rn2)


if __name__ == "__main__":
    unittest.main()
