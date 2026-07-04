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
import unittest
from unittest import mock

# The exact URL detector used inside processing.process_inbox_message
SOURCE_URL_PATTERN = r'https?://[^\s<>"\']+'

from email_automation import file_handling as fh
from email_automation import property_images as pi
from email_automation import processing as proc
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
    """Composite pin on the actual mark-processed gate. Extraction-failure paths
    above raise NOTHING, so process_inbox_message finishes with error=None and
    _should_mark_processed_after_error(None) -> True -> processed.

    There is no deterministic error class that an extraction failure maps to, so
    the gate cannot protect the broker's data. This documents the missing guard."""

    def test_extraction_failure_should_not_map_to_processed(self):
        # Reproduce the error value process_inbox_message would carry after the
        # link-download failure silently returned [] (no exception).
        error_after_silent_link_failure = None  # nothing raised by the extraction layer
        marks_processed = proc._should_mark_processed_after_error(
            error_after_silent_link_failure)
        # CORRECT behavior: an extraction failure must NOT mark the message
        # processed. Because the failure never surfaces as an exception, the gate
        # returns True -> RED (documents the missing RetryableProcessingError path).
        self.assertFalse(
            marks_processed,
            "FALSE NEGATIVE: extraction failure does not surface as an error, so "
            "_should_mark_processed_after_error(None)=True marks the message "
            "processed — the broker's attachment/link payload is lost forever.")

    def test_genuine_retryable_error_is_respected_control(self):
        # Control: when an error DOES surface, the gate correctly keeps it unprocessed.
        self.assertFalse(
            proc._should_mark_processed_after_error(RuntimeError("graph 503")),
            "control: a surfaced error must keep the message unprocessed")


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
