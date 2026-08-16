# Scanned PDF Substantive-Text Fallback Design

**Status:** Approved for source-only implementation on 2026-08-16.

**Deliverable:** code.

## Problem

`extract_pdf_text()` prefixes every page with an internal marker such as
`--- Page 7 ---`, even when a page contains no extractable text.
`process_pdf_for_ai()` currently uses the total length of that marked string to
decide whether local extraction succeeded. A seven-page image-only PDF therefore
crosses the 100-character threshold using markers alone, skips the existing
full-file upload fallback, and leaves downstream extraction with only the first
three rendered page previews. Facts on later scanned pages are unreachable.

## Decision

Keep the page markers in `extract_pdf_text()` output because they are useful
provenance, but exclude only exact internally generated page-marker lines when
evaluating whether local extraction found more than 100 characters of
substantive text.
The existing fallback remains authoritative:

- marker-only or otherwise sparse scans use `openai_upload` or
  `openai_upload+images`, retain the uploaded `file_id`, and keep bounded page
  previews;
- native-text PDFs over the existing threshold remain on the local extraction
  path and are not uploaded;
- upload failure keeps the existing fail-closed `failed` result.

The marker removal is line-bound and exact (`--- Page <positive integer> ---`).
It must not broadly delete user text that merely contains the words “page” or a
similar substring.

## Data Flow

1. Render and extract the PDF exactly as today.
2. Preserve the original cleaned extraction output, including page markers. A
   native-text manifest continues to carry it; the existing upload fallback
   continues to leave its manifest `text` empty rather than introducing a
   second behavior change in this slice.
3. Derive a threshold-only substantive projection by removing exact generated
   marker lines and trimming whitespace.
4. Use that projection for the existing `> 100` local-success decision.
5. If it is sparse, call the existing full-file upload fallback and retain both
   `file_id` and the bounded page images.
6. The existing AI request builder supplies up to three previews plus one
   `input_file`, allowing the whole scanned document to remain reachable.

## Tests

- Build a real seven-page PDF in memory with PyMuPDF, drawing visible page
  content as raster pixels so no text extractor can recover it.
- Prove local extraction returns all seven page markers but zero substantive
  text.
- Prove `process_pdf_for_ai()` calls the upload seam once, returns the file ID,
  reports `openai_upload+images` with the existing five-image manifest cap, and
  preserves the existing empty-text upload-manifest contract.
- Pass that real manifest through `propose_sheet_updates()` with only the
  OpenAI Responses boundary replaced; prove the request contains exactly one
  `input_file`, three bounded `input_image` previews, and one `input_text`.
- Build a real native-text PDF above the threshold and prove it remains
  `local_extraction` without upload.
- Keep the existing mixed-property PDF quarantine suite green.

## Non-Goals

- No OCR implementation, image-count expansion, prompt rewrite, JPG/PNG
  ingestion, natural-language variation, CE-Q1 scorer work, or manual-reply
  delivery work.
- No provider, mailbox, send, Firestore, Sheets, browser, deployment, or live
  action.

## Refutation Conditions

Stop and redesign if the fix requires changing the PDF manifest schema, if a
native-text PDF is newly uploaded, if downstream input loses the whole-file
reference, or if the mixed-property quarantine starts accepting competing facts.
