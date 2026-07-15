"""
Pressure test: broker_attachment_or_link_only

Broker replies whose entire payload is an attachment or a link
("See attached flyer and floor plan.", "All details are in this Dropbox link.").

SAFETY contract we are probing (stopIf):
  1. A message must NOT be marked processed after an extraction FAILURE
     (broken/protected link, failed PDF text extraction) — the broker's data
     would be silently lost and never retried / never surfaced to an operator.
  2. A wrong-property PDF must NOT populate the row.

Deterministic guards driven here (REAL functions, only external boundaries faked):
  - property_images.build_download_candidate  -> decides whether a broker link
    becomes a downloadable asset (link-only detection).
  - property_images.is_blocked_listing_url    -> costar/loopnet block.
  - file_handling.fetch_and_process_linked_assets -> orchestrates link download.
  - file_handling.fetch_and_process_pdfs / process_pdf_for_ai -> attachment extraction.
  - processing._should_mark_processed_after_error -> the mark-processed gate.

The actual "which numbers go in which cell" extraction is GPT-5.2 (LLM-only);
we test only the deterministic SAFETY handling around it.

Run:
  E2E_TEST_MODE=true GOOGLE_APPLICATION_CREDENTIALS=.../service-account.json \
    python -m pytest tests/test_broker_language_broker_attachment_or_link_only.py
"""
import os
import re
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

# The exact URL detector used inside processing.process_inbox_message
SOURCE_URL_PATTERN = r'https?://[^\s<>"\']+'

from email_automation import file_handling as fh
from email_automation import ai_processing as ai
from email_automation import column_config as cc
from email_automation import property_images as pi
from email_automation import processing as proc
from email_automation.campaign_safety import CampaignAutomationDecision
from email_automation import sheets as sh
from email_automation.utils import _sanitize_url


def urls_in(message_text):
    """Replicate processing.py's inline URL discovery + sanitize step."""
    found = re.findall(SOURCE_URL_PATTERN, message_text)
    return [_sanitize_url(u) for u in found[:3]]


# ---------------------------------------------------------------------------
# Real-threat phrasings: broker payload is ONLY an attachment/link.
# Each is (label, message_body, expected_host_family)
# where expected_host_family is 'supported' (drive/dropbox/direct-pdf) or the
# common broker file-share host that the guard currently drops.
# ---------------------------------------------------------------------------
SUPPORTED_LINK_PHRASINGS = [
    ("terse_dropbox",
     "All details are in this Dropbox link. https://www.dropbox.com/s/ab12/flyer.pdf?dl=0"),
    ("verbose_dropbox",
     "Thanks for reaching out! I've put the full marketing package together for you "
     "and everything you need is right here: https://www.dropbox.com/s/xy9/OM.pdf?dl=0 "
     "Let me know if you have any questions. Best, Dana"),
    ("drive_file",
     "See attached — flyer is here https://drive.google.com/file/d/1A2B3C4D5E/view?usp=sharing"),
    ("direct_pdf",
     "Floor plan attached: https://cdn.brokersite.com/assets/2024/suite-200-floorplan.pdf"),
    ("caps_dropbox",
     "PLEASE REVIEW THE ATTACHED BROCHURE: HTTPS://WWW.DROPBOX.COM/S/QQ/BROCHURE.PDF?DL=0".replace(
         "HTTPS://WWW.DROPBOX.COM/S/QQ/BROCHURE.PDF?DL=0",
         "https://www.dropbox.com/s/qq/brochure.pdf?dl=0")),
    ("glued_signoff_dropbox",
     "Details attached here https://www.dropbox.com/s/zz/deck.pdf?dl=0Thanks"),
    ("trailing_punct_drive",
     "Grab the flyer (https://drive.google.com/file/d/9Z8Y7X/view)."),
    ("drive_image",
     "Photos of the space: https://drive.google.com/file/d/img777/view — flyer to follow"),
    ("multi_intent_link",
     "We're at 500/mo gross. Full OM here https://www.dropbox.com/s/oo/om.pdf?dl=0 — "
     "happy to set a tour next week."),
    ("quoted_history_link",
     "See attached brochure.\n\n> On Mon, broker wrote:\n> here is the flyer "
     "https://www.dropbox.com/s/hist/old.pdf?dl=0"),
]

# Extremely common CRE broker file-share hosts that are NOT drive/dropbox and
# carry no direct file extension. build_download_candidate returns None for these,
# so a link-ONLY broker email yields zero extraction and (see stopIf) the message
# is silently marked processed.
UNSUPPORTED_BROKER_HOST_PHRASINGS = [
    ("sharepoint",
     "All the details are in this link: "
     "https://acmecre-my.sharepoint.com/:b:/g/personal/dana_acmecre_com/EفGh123"
     .replace("ف", "E")),
    ("onedrive",
     "Flyer + floor plan here https://1drv.ms/b/s!AkLmN0pQrs"),
    ("box",
     "Please review the attached brochure: https://acmecre.box.com/s/9a8b7c6d5e"),
    ("wetransfer",
     "Sent the full package via WeTransfer: https://we.tl/t-Zx9Q1w2E3r"),
    ("drive_folder",
     "Everything is in this Drive folder https://drive.google.com/drive/folders/1FolderIdABC"),
]

# Near-misses: these must NOT resolve to a usable asset silently.
BLOCKED_LISTING_PHRASINGS = [
    ("costar", "Here's the listing: https://www.costar.com/property/12345"),
    ("loopnet", "See the LoopNet page https://www.loopnet.com/Listing/999/"),
]


class TestLinkDetectionFires(unittest.TestCase):
    """No false negative: a broker link-only message on a SUPPORTED host must
    deterministically resolve to a download candidate."""

    def test_supported_link_phrasings_build_candidate(self):
        misses = []
        for label, body in SUPPORTED_LINK_PHRASINGS:
            urls = urls_in(body)
            candidate = None
            for u in urls:
                candidate = pi.build_download_candidate(u, "")
                if candidate:
                    break
            if not candidate:
                misses.append((label, urls))
        self.assertEqual(
            misses, [],
            f"FALSE NEGATIVE: link-only broker phrasings produced no download "
            f"candidate (extraction silently skipped): {misses}")


class TestBlockedListingNearMiss(unittest.TestCase):
    """No false positive: scraping-protected listing hosts must be blocked so we
    do not fetch them, but the block must be explicit (is_blocked_listing_url)."""

    def test_costar_loopnet_blocked(self):
        for label, body in BLOCKED_LISTING_PHRASINGS:
            urls = urls_in(body)
            self.assertTrue(urls, f"{label}: no URL parsed")
            for u in urls:
                self.assertTrue(
                    pi.is_blocked_listing_url(u),
                    f"{label}: {u} should be a blocked listing host")
                self.assertIsNone(
                    pi.build_download_candidate(u, ""),
                    f"{label}: {u} must not yield a download candidate")


class TestCommonBrokerHostSilentlyDropped(unittest.TestCase):
    """FALSE NEGATIVE (safety hole): SharePoint/OneDrive/Box/WeTransfer/Drive-folder
    links are the daily reality of CRE brokerage. build_download_candidate returns
    None for all of them -> a link-ONLY broker email yields zero extraction and the
    pipeline marks the message processed (stopIf #1). A link the broker clearly
    intends as the payload must be resolved OR surfaced, never silently dropped.

    Asserted to the CORRECT behavior so it fails RED and pins the bug."""

    def test_common_broker_hosts_are_not_silently_dropped(self):
        dropped = []
        for label, body in UNSUPPORTED_BROKER_HOST_PHRASINGS:
            urls = urls_in(body)
            candidate = None
            for u in urls:
                candidate = pi.build_download_candidate(u, "")
                if candidate:
                    break
            if not candidate:
                dropped.append((label, urls))
        self.assertEqual(
            dropped, [],
            f"FALSE NEGATIVE: link-only broker emails on common file-share hosts "
            f"are silently dropped (None -> no extraction -> message marked "
            f"processed, data lost): {dropped}")


class TestBrokenOrProtectedLinkStaysVisible(unittest.TestCase):
    """Near-miss: 'Broken link or protected Drive file should stay visible, not
    silently complete.' When the download of a broker-supplied link fails
    (403 protected Drive, dead link), fetch_and_process_linked_assets swallows the
    exception and returns [] -> no failure signal reaches the caller -> the message
    completes and is marked processed (stopIf #1).

    Control (happy path) proves the function CAN return an entry, so [] is a real
    silent drop, not just 'nothing to do'."""

    def _patch_boundaries(self):
        patchers = [
            mock.patch.object(
                pi, "build_download_candidate",
                return_value={
                    "downloadUrl": "https://drive.google.com/uc?export=download&id=ABC",
                    "sourceType": "google_drive_pdf",
                    "sourceLabel": "Broker flyer: flyer.pdf",
                    "sourceUrl": "https://drive.google.com/file/d/ABC/view",
                }),
            mock.patch.object(fh, "upload_pdf_to_drive", return_value="https://drive/x"),
            mock.patch.object(fh, "_attach_pdf_property_preview", return_value=None),
        ]
        return patchers

    def test_happy_path_returns_entry_control(self):
        patchers = self._patch_boundaries()
        patchers.append(mock.patch.object(
            fh, "_download_linked_asset",
            return_value=(b"%PDF-1.4 fake", "application/pdf")))
        patchers.append(mock.patch.object(
            fh, "process_pdf_for_ai",
            return_value={"text": "50,000 SF available", "images": [],
                          "method": "local_extraction", "file_id": None,
                          "id": None, "filename": "flyer.pdf"}))
        with contextlib_nested(patchers):
            out = fh.fetch_and_process_linked_assets(
                ["https://drive.google.com/file/d/ABC/view"])
        self.assertTrue(
            out, "control: a valid broker PDF link should return a manifest entry")

    def test_protected_link_is_surfaced_not_silently_dropped(self):
        patchers = self._patch_boundaries()
        # Simulate protected Drive file / broken link -> download raises.
        patchers.append(mock.patch.object(
            fh, "_download_linked_asset",
            side_effect=ValueError("403 Forbidden (protected Drive file)")))
        with contextlib_nested(patchers):
            out = fh.fetch_and_process_linked_assets(
                ["https://drive.google.com/file/d/ABC/view"])
        # CORRECT behavior: the failure must be surfaced so the caller can keep the
        # message unprocessed. Current code returns [] -> RED (documents the bug).
        self.assertTrue(
            out,
            "FALSE NEGATIVE: a broken/protected broker link is silently dropped "
            "(returns []), so process_inbox_message sees no error and marks the "
            "message processed — broker payload lost with no retry/visibility.")


class TestFailedPdfExtractionNotSilentlyComplete(unittest.TestCase):
    """A broker sends 'See attached flyer' but PDF text extraction AND the OpenAI
    upload fallback both fail (method='failed'). fetch_and_process_pdfs still
    returns the entry with a drive_link, so downstream a flyer link is written to
    the row and the message is marked processed — while NONE of the broker's specs
    were extracted. The 'handled' row hides a total extraction failure (stopIf #1)."""

    def test_failed_extraction_pdf_not_returned_as_success(self):
        attachment = {"name": "OfferingMemo.pdf", "bytes": b"%PDF-1.4 xxx"}
        failed_result = {"text": "", "images": [], "method": "failed",
                         "file_id": None, "id": None, "filename": "OfferingMemo.pdf"}
        with mock.patch.object(fh, "fetch_pdf_attachments", return_value=[attachment]), \
             mock.patch.object(fh, "process_pdf_for_ai", return_value=dict(failed_result)), \
             mock.patch.object(fh, "upload_pdf_to_drive", return_value="https://drive/link"), \
             mock.patch.object(fh, "_attach_pdf_property_preview", return_value=None):
            manifest = fh.fetch_and_process_pdfs({}, "msg-1")

        failed_entries = [p for p in manifest if p.get("method") == "failed"]
        # CORRECT behavior: a fully-failed extraction must not be handed downstream
        # as a normal manifest entry that lets the pipeline mark the message
        # processed. Current code appends it verbatim -> RED (documents the bug).
        self.assertEqual(
            failed_entries, [],
            "FALSE NEGATIVE: PDF with method='failed' is returned in the manifest "
            "(with a drive_link) as if usable; the row gets a flyer link and the "
            "message is marked processed though zero specs were extracted.")


class TestMarkProcessedGateOnExtractionFailure(unittest.TestCase):
    """Pins the REAL mark-processed gate connection, end to end through
    process_inbox_message:

      file_handling surfaces an extraction failure (failed PDF extraction /
      broken-protected broker link)  ->  process_inbox_message must carry a
      non-None retryable error (RetryableProcessingError)  ->
      _should_mark_processed_after_error(error) is False  ->  the caller leaves
      the message UNPROCESSED (that exact caller decision is pinned by
      tests/test_processing_retryability.py::
      test_scan_records_unexpected_processing_crash_without_marking_processed).

    Historically the failure paths raised NOTHING, error stayed None, the gate
    returned True and the broker's attachment/link payload was silently lost."""

    USER_ID = "user-extraction-gate"
    THREAD_ID = "thread-extraction-gate"

    def _drive_real_process_inbox_message(
        self,
        *,
        body,
        has_attachments,
        fh_patches,
        proposal=None,
        apply_result_override=None,
    ):
        """Run the REAL process_inbox_message (and the REAL file_handling
        manifest builders) with only external boundaries faked. Returns the
        exception it raised, or None if it completed silently."""
        from tests.test_compound_nonviable_processing import (
            FakeDocumentRef,
            FakeFirestore,
        )

        thread_ref = FakeDocumentRef({
            "status": "active",
            "clientId": "client-1",
            "email": ["broker@example.test"],
        })
        client_ref = FakeDocumentRef({"criteria": "Industrial search"})

        msg = {
            "id": "msg-extraction-gate",
            "subject": "RE: 4402 Rex Rd",
            "from": {"emailAddress": {"address": "broker@example.test", "name": "Dana"}},
            "toRecipients": [{"emailAddress": {"address": "me@ourdomain.test"}}],
            "internetMessageId": "<extraction-gate@example.test>",
            "conversationId": "conv-extraction-gate",
            "receivedDateTime": "2026-07-01T15:00:00Z",
            "bodyPreview": body[:200],
            "hasAttachments": has_attachments,
            "internetMessageHeaders": [
                {"name": "In-Reply-To", "value": "<our-outbound@example.test>"},
            ],
        }
        header = ["Property Address", "City", "Leasing Contact", "Email", "Total SF"]
        rowvals = ["4402 Rex Rd", "Houston", "Dana", "broker@example.test", ""]

        full_body_response = mock.MagicMock()
        full_body_response.json.return_value = {
            "body": {"content": body, "contentType": "Text"},
            "hasAttachments": has_attachments,
        }
        me_response = mock.MagicMock(status_code=200)
        me_response.json.return_value = {"mail": "me@ourdomain.test"}

        send_reply = mock.MagicMock(return_value=True)
        self.asset_warning_recorder = mock.MagicMock()
        apply_result = (
            apply_result_override
            if apply_result_override is not None
            else {"applied": (proposal or {}).get("updates") or [], "skipped": []}
        )
        self.apply_proposal = mock.MagicMock(return_value=apply_result)
        self.propose_sheet_updates = mock.MagicMock(
            return_value={"skip_response": True} if proposal is None else proposal
        )

        patchers = [
            mock.patch.object(proc, "_fs", FakeFirestore(thread_ref, client_ref)),
            mock.patch.object(
                proc,
                "get_client_automation_decision",
                return_value=CampaignAutomationDecision(
                    state="allow",
                    reason="",
                    client_data={"status": "live", "automationPaused": False},
                    metadata={"source": "systemConfig/campaignAccess", "terminal": False},
                ),
            ),
            mock.patch.object(proc, "exponential_backoff_request", return_value=full_body_response),
            mock.patch.object(proc.requests, "get", return_value=me_response),
            mock.patch.object(proc, "lookup_thread_by_message_id", return_value=self.THREAD_ID),
            mock.patch.object(proc, "lookup_thread_by_conversation_id", return_value=None),
            mock.patch.object(proc, "get_thread_status", return_value=proc.THREAD_STATUS["active"]),
            mock.patch.object(proc, "save_message", return_value=True),
            mock.patch.object(proc, "index_message_id", return_value=True),
            mock.patch.object(proc, "dump_thread_from_firestore"),
            mock.patch("email_automation.followup.cancel_followup_on_response"),
            mock.patch.object(
                proc,
                "fetch_and_log_sheet_for_thread",
                return_value=("client-1", "sheet-1", header, 3, rowvals, None, []),
            ),
            mock.patch.object(
                proc,
                "_resolve_reply_identity",
                return_value={
                    "recipient_email": "broker@example.test",
                    "contact_name": "Dana",
                    "original_email": "broker@example.test",
                    "source": "test",
                },
            ),
            mock.patch.object(proc, "write_message_order_test"),
            mock.patch.object(proc, "fetch_url_as_text", return_value=None),
            # If the extraction failure is NOT surfaced, processing continues to
            # the proposal; skip_response makes that continuation terminate
            # cleanly with error=None (the historical silent-loss outcome).
            mock.patch.object(
                proc,
                "propose_sheet_updates",
                side_effect=self.propose_sheet_updates,
            ),
            mock.patch.object(
                proc,
                "apply_proposal_to_sheet",
                side_effect=self.apply_proposal,
            ),
            mock.patch.object(proc, "add_client_notifications"),
            mock.patch.object(
                proc,
                "_record_asset_extraction_warning",
                side_effect=self.asset_warning_recorder,
            ),
            mock.patch.object(proc, "_sheets_client", return_value=mock.MagicMock()),
            mock.patch.object(proc, "_get_first_tab_title", return_value="Sheet1"),
            mock.patch.object(proc, "check_missing_required_fields", return_value=[]),
            mock.patch.object(proc, "send_reply_in_thread", side_effect=send_reply),
        ] + list(fh_patches)

        raised = None
        with contextlib_nested(patchers):
            try:
                proc.process_inbox_message(
                    self.USER_ID, {"Authorization": "Bearer fake"}, msg
                )
            except Exception as e:  # noqa: BLE001 - the raised type IS the assertion target
                raised = e
        return raised, send_reply

    def test_extraction_failure_should_not_map_to_processed(self):
        # A broker attachment whose text extraction AND OpenAI-upload fallback
        # both fail: the REAL fetch_and_process_pdfs surfaces it as a
        # failure entry (method='failed_extraction', extraction_failed=True).
        attachment = {"name": "OfferingMemo.pdf", "bytes": b"%PDF-1.4 xxx"}
        failed_result = {"text": "", "images": [], "method": "failed",
                         "file_id": None, "id": None, "filename": "OfferingMemo.pdf"}
        fh_patches = [
            mock.patch.object(fh, "fetch_pdf_attachments", return_value=[attachment]),
            mock.patch.object(fh, "process_pdf_for_ai", return_value=dict(failed_result)),
            mock.patch.object(fh, "upload_pdf_to_drive", return_value="https://drive/link"),
            mock.patch.object(fh, "_attach_pdf_property_preview", return_value=None),
        ]

        error, send_reply = self._drive_real_process_inbox_message(
            body="Hi, please see the attached offering memo for the full specs.",
            has_attachments=True,
            fh_patches=fh_patches,
        )

        # CORRECT behavior: the surfaced extraction failure must become a
        # non-None retryable error out of process_inbox_message...
        self.assertIsNotNone(
            error,
            "FALSE NEGATIVE: total PDF extraction failure raised nothing, so the "
            "caller sees error=None, marks the message processed, and the "
            "broker's attachment payload is lost forever.")
        self.assertIsInstance(
            error, proc.RetryableProcessingError,
            f"extraction failure must surface as RetryableProcessingError, got: {error!r}")
        self.assertIn("OfferingMemo.pdf", str(error))
        # ...so the mark-processed gate keeps the message UNPROCESSED.
        self.assertFalse(
            proc._should_mark_processed_after_error(error),
            "the surfaced extraction error must keep the message unprocessed")
        # And no auto-reply may be sent off a message whose payload was lost.
        send_reply.assert_not_called()

    def test_broken_broker_link_download_failure_keeps_message_unprocessed(self):
        # A broker link-only message whose download fails (403 protected Drive
        # file / dead link): the REAL fetch_and_process_linked_assets surfaces
        # it as a failure entry (method='failed', download_failed=True).
        fh_patches = [
            mock.patch.object(fh, "fetch_pdf_attachments", return_value=[]),
            mock.patch.object(
                fh, "_download_linked_asset",
                side_effect=ValueError("403 Forbidden (protected Drive file)")),
        ]

        error, send_reply = self._drive_real_process_inbox_message(
            body=("All details are in this Dropbox link. "
                  "https://www.dropbox.com/s/ab12/flyer.pdf?dl=0"),
            has_attachments=False,
            fh_patches=fh_patches,
        )

        self.assertIsNotNone(
            error,
            "FALSE NEGATIVE: broken/protected broker link raised nothing, so the "
            "caller sees error=None, marks the message processed, and the "
            "broker's link payload is lost forever.")
        self.assertIsInstance(
            error, proc.RetryableProcessingError,
            f"link download failure must surface as RetryableProcessingError, got: {error!r}")
        self.assertFalse(
            proc._should_mark_processed_after_error(error),
            "the surfaced link-download error must keep the message unprocessed")
        send_reply.assert_not_called()

    def test_broken_link_with_independent_specs_processes_text_and_records_warning(self):
        fh_patches = [
            mock.patch.object(fh, "fetch_pdf_attachments", return_value=[]),
            mock.patch.object(
                fh,
                "_download_linked_asset",
                side_effect=ValueError("404 Not Found"),
            ),
        ]
        proposal = {
            "updates": [
                {"column": "Total SF", "value": "18,500"},
                {"column": "Rent/SF/Yr", "value": "$12.50"},
            ],
            "events": [],
            "skip_response": True,
        }

        error, send_reply = self._drive_real_process_inbox_message(
            body=(
                "The space is available. It is 18,500 SF at $12.50/SF/Yr. "
                "The old flyer link is https://example.com/dead-flyer.pdf"
            ),
            has_attachments=False,
            fh_patches=fh_patches,
            proposal=proposal,
        )

        self.assertIsNone(error)
        self.asset_warning_recorder.assert_called_once()
        warning_args = self.asset_warning_recorder.call_args.args
        self.assertEqual("client-1", warning_args[1])
        self.assertEqual(self.THREAD_ID, warning_args[2])
        self.assertEqual("<extraction-gate@example.test>", warning_args[3])
        self.assertEqual("dead-flyer.pdf", warning_args[4][0]["name"])
        self.assertEqual("404 Not Found", warning_args[4][0]["error"])
        self.apply_proposal.assert_called_once()
        self.assertEqual(proposal, self.apply_proposal.call_args.args[-1])
        proposal_manifest = self.propose_sheet_updates.call_args.kwargs["pdf_manifest"]
        self.assertEqual([], proposal_manifest)
        send_reply.assert_not_called()

    def test_broken_link_with_event_only_proposal_stays_retryable(self):
        fh_patches = [
            mock.patch.object(fh, "fetch_pdf_attachments", return_value=[]),
            mock.patch.object(
                fh,
                "_download_linked_asset",
                side_effect=ValueError("404 Not Found"),
            ),
        ]

        error, send_reply = self._drive_real_process_inbox_message(
            body="All details are at https://example.com/dead-flyer.pdf",
            has_attachments=False,
            fh_patches=fh_patches,
            proposal={
                "updates": [],
                "events": [{"type": "unsupported_event"}],
                "skip_response": True,
            },
        )

        self.assertIsInstance(error, proc.RetryableProcessingError)
        self.asset_warning_recorder.assert_not_called()
        send_reply.assert_not_called()

    def test_broken_link_with_rejected_placeholder_update_stays_retryable(self):
        fh_patches = [
            mock.patch.object(fh, "fetch_pdf_attachments", return_value=[]),
            mock.patch.object(
                fh,
                "_download_linked_asset",
                side_effect=ValueError("404 Not Found"),
            ),
        ]

        error, send_reply = self._drive_real_process_inbox_message(
            body=(
                "The total square footage is TBD. "
                "The old flyer is https://example.com/dead-flyer.pdf"
            ),
            has_attachments=False,
            fh_patches=fh_patches,
            proposal={
                "updates": [{"column": "Total SF", "value": "TBD"}],
                "events": [],
                "skip_response": True,
            },
            apply_result_override={
                "applied": [],
                "skipped": [{"column": "Total SF", "reason": "placeholder"}],
            },
        )

        self.assertIsInstance(error, proc.RetryableProcessingError)
        self.asset_warning_recorder.assert_not_called()
        send_reply.assert_not_called()

    def test_genuine_retryable_error_is_respected_control(self):
        # Control: when an error DOES surface, the gate correctly keeps it unprocessed.
        self.assertFalse(
            proc._should_mark_processed_after_error(RuntimeError("graph 503")),
            "control: a surfaced error must keep the message unprocessed")


class TestBrokenAssetGracefulDegradation(unittest.TestCase):
    """A dead flyer link must not discard independently extracted broker facts."""

    def test_applied_sheet_updates_allow_text_processing_to_continue(self):
        apply_result = {
            "applied": [
                {"column": "Total SF", "newValue": "18,500"},
                {"column": "Rent/SF/Yr", "newValue": "$12.50"},
            ],
            "skipped": [],
        }

        self.assertTrue(proc._sheet_updates_committed_non_asset_evidence(apply_result))

    def test_rejected_or_missing_updates_remain_retryable(self):
        apply_result = {"applied": [], "skipped": [{"reason": "placeholder"}]}

        self.assertFalse(proc._sheet_updates_committed_non_asset_evidence(apply_result))

    def test_asset_alias_only_is_not_non_asset_evidence(self):
        column_config = {
            "mappings": {
                "flyer_link": "Brochure",
                "floorplan": "Floor Plans",
            }
        }
        apply_result = {
            "applied": [
                {"column": "Brochure", "newValue": "https://example.com/dead.pdf"},
            ],
            "skipped": [],
        }

        self.assertFalse(
            proc._sheet_updates_committed_non_asset_evidence(
                apply_result,
                column_config,
            )
        )

    def test_mixed_asset_and_spec_updates_have_non_asset_evidence(self):
        column_config = {"mappings": {"flyer_link": "Offering Materials"}}
        apply_result = {
            "applied": [
                {"column": "Offering Materials", "newValue": "https://example.com/dead.pdf"},
                {"column": "Total SF", "newValue": "18,500"},
            ],
            "skipped": [],
        }

        self.assertTrue(
            proc._sheet_updates_committed_non_asset_evidence(
                apply_result,
                column_config,
            )
        )

    def test_matching_no_change_spec_allows_warning_recovery_after_partial_commit(self):
        apply_result = {
            "applied": [],
            "skipped": [
                {
                    "column": "Total SF",
                    "reason": "no-change",
                    "oldValue": "18,500",
                    "newValue": "18,500",
                }
            ],
        }

        self.assertTrue(
            proc._sheet_updates_committed_non_asset_evidence(apply_result, {"mappings": {}})
        )

    def test_apply_sheet_rejects_custom_mapped_asset_column(self):
        sheets = mock.MagicMock()
        column_config = {"mappings": {"flyer_link": "Offering Materials"}}
        with mock.patch.object(ai, "_sheets_client", return_value=sheets), mock.patch.object(
            ai, "_get_first_tab_title", return_value="Sheet1"
        ), mock.patch.object(ai, "_ensure_ai_meta_tab"), mock.patch.object(
            ai, "_read_ai_meta_row", return_value=None
        ), mock.patch.object(ai, "_append_ai_meta"), mock.patch.object(
            ai, "_append_notes_to_comments"
        ), mock.patch(
            "email_automation.sheet_operations._apply_gross_rent_formula_for_row",
            return_value=False,
        ), mock.patch.object(ai, "_execute_with_retry", return_value={}):
            result = ai.apply_proposal_to_sheet(
                uid="user-1",
                client_id="client-1",
                sheet_id="sheet-1",
                header=["Property Address", "Offering Materials"],
                rownum=3,
                current_rowvals=["912-930 Gemini St", ""],
                proposal={
                    "updates": [
                        {
                            "column": "Offering Materials",
                            "value": "https://example.com/dead.pdf",
                        }
                    ]
                },
                column_config=column_config,
            )

        self.assertEqual([], result["applied"])
        self.assertIn(
            ("Offering Materials", "handled-by-asset-pipeline"),
            {(item.get("column"), item.get("reason")) for item in result["skipped"]},
        )
        sheets.spreadsheets.return_value.values.return_value.batchUpdate.assert_not_called()

    def test_apply_sheet_rejects_plural_flyers_under_legacy_note_config(self):
        sheets = mock.MagicMock()
        column_config = {
            "mappings": {},
            "customFields": {
                "Flyers": {
                    "mode": "note",
                    "description": "Extract value for Flyers",
                }
            },
        }
        with mock.patch.object(ai, "_sheets_client", return_value=sheets), mock.patch.object(
            ai, "_get_first_tab_title", return_value="Sheet1"
        ), mock.patch.object(ai, "_ensure_ai_meta_tab"), mock.patch.object(
            ai, "_read_ai_meta_row", return_value=None
        ), mock.patch.object(ai, "_append_ai_meta"), mock.patch.object(
            ai, "_append_notes_to_comments"
        ), mock.patch(
            "email_automation.sheet_operations._apply_gross_rent_formula_for_row",
            return_value=False,
        ), mock.patch.object(ai, "_execute_with_retry", return_value={}):
            result = ai.apply_proposal_to_sheet(
                uid="user-1",
                client_id="client-1",
                sheet_id="sheet-1",
                header=["Property Address", "Flyers"],
                rownum=3,
                current_rowvals=["912-930 Gemini St", ""],
                proposal={
                    "updates": [
                        {
                            "column": "Flyers",
                            "value": "Attached flyer provided (broker-flyer.pdf).",
                        }
                    ]
                },
                column_config=column_config,
            )

        self.assertEqual([], result["applied"])
        self.assertIn(
            ("Flyers", "handled-by-asset-pipeline"),
            {(item.get("column"), item.get("reason")) for item in result["skipped"]},
        )
        sheets.spreadsheets.return_value.values.return_value.batchUpdate.assert_not_called()

    def test_no_change_logging_omits_existing_sheet_value(self):
        sheets = mock.MagicMock()
        output = io.StringIO()
        with mock.patch.object(ai, "_sheets_client", return_value=sheets), mock.patch.object(
            ai, "_get_first_tab_title", return_value="Sheet1"
        ), mock.patch.object(ai, "_ensure_ai_meta_tab"), mock.patch.object(
            ai, "_read_ai_meta_row", return_value=None
        ), mock.patch.object(ai, "_append_ai_meta"), mock.patch.object(
            ai, "_append_notes_to_comments"
        ), mock.patch(
            "email_automation.sheet_operations._apply_gross_rent_formula_for_row",
            return_value=False,
        ), mock.patch.object(ai, "_execute_with_retry", return_value={}), redirect_stdout(output):
            ai.apply_proposal_to_sheet(
                uid="user-1",
                client_id="client-1",
                sheet_id="sheet-1",
                header=["Property Address", "Power", "Total SF"],
                rownum=3,
                current_rowvals=["912-930 Gemini St", "PRIVATE-800A-3PH", ""],
                proposal={
                    "updates": [
                        {"column": "Power", "value": "PRIVATE-800A-3PH"},
                        {"column": "Total SF", "value": "18,500"},
                    ]
                },
            )

        self.assertNotIn("PRIVATE-800A-3PH", output.getvalue())

    def test_warning_persistence_failure_does_not_block_text_processing(self):
        failing_fs = mock.MagicMock()
        failing_fs.collection.return_value.document.return_value.collection.return_value.document.return_value.set.side_effect = RuntimeError(
            "Firestore unavailable"
        )

        with mock.patch.object(proc, "_fs", failing_fs), mock.patch.object(
            proc, "_record_ai_processing_failure"
        ) as record_failure:
            proc._record_asset_extraction_warning(
                "user-1",
                "client-1",
                "thread-1",
                "message-1",
                [{"name": "dead.pdf", "method": "failed", "error": "404"}],
            )

        record_failure.assert_called_once_with(
            "user-1",
            "client-1",
            "thread-1",
            "message-1",
            "Asset warning persistence failed: Firestore unavailable",
            retryable=False,
            recovery_status="asset_warning_persistence_failed",
            record_key_suffix="asset_warning_persistence",
            metadata={
                "assetWarnings": [
                    {
                        "name": "dead.pdf",
                        "sourceUrl": None,
                        "sourceType": None,
                        "method": "failed",
                        "error": "404",
                    }
                ]
            },
        )

    def test_warning_fallback_failure_keeps_message_retryable(self):
        failing_fs = mock.MagicMock()
        failing_fs.collection.return_value.document.return_value.collection.return_value.document.return_value.set.side_effect = RuntimeError(
            "Firestore unavailable"
        )

        with mock.patch.object(proc, "_fs", failing_fs), mock.patch.object(
            proc, "_record_ai_processing_failure", return_value=False
        ):
            with self.assertRaises(proc.RetryableProcessingError):
                proc._record_asset_extraction_warning(
                    "user-1",
                    "client-1",
                    "thread-1",
                    "message-1",
                    [{"name": "dead.pdf", "method": "failed", "error": "404"}],
                )

    def test_warning_fallback_record_uses_distinct_cleanup_key(self):
        fs = mock.MagicMock()
        with mock.patch.object(proc, "_fs", fs):
            self.assertTrue(
                proc._record_ai_processing_failure(
                    "user-1",
                    "client-1",
                    "thread-1",
                    "message-1",
                    "warning fallback",
                    retryable=False,
                    recovery_status="asset_warning_persistence_failed",
                    record_key_suffix="asset_warning_persistence",
                    metadata={"assetWarnings": [{"name": "dead.pdf", "error": "404"}]},
                )
            )
            proc._clear_ai_processing_failure("user-1", "thread-1", "message-1")

        nested_document = (
            fs.collection.return_value.document.return_value.collection.return_value.document
        )
        document_calls = [call.args[0] for call in nested_document.call_args_list]
        self.assertIn("thread-1__message-1__asset_warning_persistence", document_calls)
        self.assertIn("thread-1__message-1", document_calls)
        fallback_set = nested_document.return_value.set.call_args_list[0]
        self.assertEqual(
            {"assetWarnings": [{"name": "dead.pdf", "error": "404"}]},
            fallback_set.args[0]["metadata"],
        )

    def test_asset_column_classifier_uses_default_and_custom_mappings(self):
        self.assertTrue(cc.is_asset_column_name("Brochure"))
        self.assertTrue(
            cc.is_asset_column_name(
                "Offering Materials",
                {"mappings": {"flyer_link": "Offering Materials"}},
            )
        )
        legacy_config = {
            "mappings": {},
            "customFields": {
                "Flyers": {
                    "mode": "note",
                    "description": "Extract value for Flyers",
                }
            },
        }
        self.assertTrue(cc.is_asset_column_name("Flyers", legacy_config))
        self.assertFalse(cc.is_asset_column_name("Total SF"))

    def test_usable_manifest_filters_failures_by_identity_not_value(self):
        failed = {"name": "same.pdf", "method": "failed", "error": "404"}
        distinct_but_equal = dict(failed)
        usable = {"name": "good.pdf", "method": "pdfplumber", "text": "18,500 SF"}

        result = proc._without_extraction_failures(
            [failed, distinct_but_equal, usable],
            [failed],
        )

        self.assertEqual(2, len(result))
        self.assertIs(distinct_but_equal, result[0])
        self.assertIs(usable, result[1])


class TestWrongPropertyPdfNoDeterministicGuard(unittest.TestCase):
    """Near-miss: 'Attachment belongs to a different property in a forwarded chain.'
    build_download_candidate does ZERO property/address matching — a forwarded flyer
    for a DIFFERENT address still becomes a fully valid download candidate whose
    preview image will populate property_image_url on the current row (stopIf #2).
    There is no deterministic address guard; only the LLM prompt is asked to ignore
    mismatched attachments. This asserts the (missing) deterministic guard."""

    def test_forwarded_wrong_property_flyer_still_builds_candidate(self):
        # A flyer link clearly for a different address, in a forwarded chain.
        wrong_prop_url = "https://www.dropbox.com/s/other/123-Main-St-DIFFERENT.pdf?dl=0"
        candidate = pi.build_download_candidate(wrong_prop_url, "123-Main-St-DIFFERENT.pdf")
        # CORRECT behavior: a link whose filename/address does not match the target
        # property must not silently become the row's flyer/preview source. Current
        # code returns a valid candidate with no matching -> RED (documents the gap).
        self.assertIsNone(
            candidate,
            "WRONG-HANDLING: build_download_candidate has no property/address check, "
            "so a forwarded different-property flyer is accepted and its preview "
            "populates the current row (wrong property PDF populates the row).")


# Minimal nested-context helper (py3.9-safe) so we don't depend on ExitStack style.
import contextlib


@contextlib.contextmanager
def contextlib_nested(patchers):
    started = []
    try:
        for p in patchers:
            started.append(p.start())
        yield started
    finally:
        for p in patchers:
            try:
                p.stop()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
