# Surface A — broker-language robustness bug inventory (2026-07-03)

464 realistic phrasings across 20 broker-event classes → **54 bugs (35 high)**. Each `tests/test_broker_language_<event>.py` is RED where it pins a defect. 7 events are LLM-only (need OpenAI creds) but still had deterministic guard bugs.

## By source file (fix targets)

### `email_automation/tour_scheduling.py` — 15 bugs
- **[HIGH/false]** (broker_property_unavailable) Seed phrasing 'We just leased the building.' does not create a property_unavailable event. The augment layer's unavailable_patterns only matches 'signed a lease'/'fully leased', no
- **[HIGH/false]** (broker_property_unavailable) Seed phrasing 'Property is off market for now.' does not create a property_unavailable event. looks_like_tour_only_unavailable correctly returns False (off-market), but unavailable
- **[HIGH/false]** (broker_property_unavailable) 'The space is under contract.' is not caught. 'under contract' is a documented terminal keyword in processing.PROPERTY_UNAVAILABLE_KEYWORDS but is absent from the augment layer's u
- **[HIGH/false]** (broker_property_unavailable) Past-tense 'been leased' phrasings are missed. augment only matches 'fully leased' / 'signed a lease'; 'has been leased'/'already leased'/bare 'leased' are not covered even though 
- **[HIGH/false]** (broker_property_unavailable) 'taken off the market' is missed. The augment layer has no off-market pattern at all, and the tour-only guard's off[-\s]?market regex does not match 'off the market' (words in betw
- **[MEDIUM/false]** (broker_property_unavailable) 'We accepted an offer on that space.' is missed by the augment layer despite 'accepted an offer' being a listed terminal keyword in processing.PROPERTY_UNAVAILABLE_KEYWORDS.
- **[LOW/false]** (broker_property_unavailable) Single-character typo 'availabe' defeats the deterministic backstop entirely (regex requires exact 'available'). Brokers routinely typo; the safety backstop should tolerate near-sp
- **[HIGH/wrong]** (broker_alternate_tour_time) When a broker rejects a time and proposes a different one ('X does not work, do Y instead'), the pipeline evaluates and auto-confirms the REJECTED time X, not the proposed alternat
- **[MEDIUM/false]** (broker_alternate_tour_time) _classify_tour_invite_reply's confirmation_signal regex matches the bare substring 'confirmed', so a broker message that merely mentions another confirmed appointment is classified
- **[LOW/wrong]** (broker_alternate_tour_time) A common typo phrasing of an alternate-time request is not classified as alternate_requested and is downgraded to generic operator-review, losing the schedule-aware feasibility che
- **[HIGH/false]** (broker_tour_unavailable) 'No availability to show ...' trips the property-level no_availability pattern while looks_like_tour_only_unavailable misses it, so the guard ACTIVELY invents a property_unavailabl
- **[HIGH/false]** (broker_tour_unavailable) Detector misses the extremely common 'No tours ...' / 'No showings ...' construction because the negation is the bare word 'no' before the tour noun, which is not in the negation a
- **[HIGH/false]** (broker_tour_unavailable) Negated-availability contractions and 'no <noun> are available' are missed: the regex only fires on 'not available/unavailable' AFTER the tour noun, so positive-verb phrasings that
- **[HIGH/false]** (broker_tour_unavailable) Temporary showing restrictions phrased with the verb 'show'/'show it' are missed because tour_context only matches the noun forms (tours/showings/walk-throughs), not the verb 'show
- **[MEDIUM/false]** (broker_tour_unavailable) The verb 'suspended' is not in the post-noun unavailability phrase list ('not available|unavailable|cancelled|canceled|not being offered'), so a suspended-tours message is missed.

### `email_automation/ai_processing.py` — 14 bugs
- **[HIGH/false]** (broker_property_non_viable) Guard misses a clear non-viable reason because 'do not have'/'don't have' drive-in verb forms are absent from the pattern (only 'does not have'/'doesn't have'), and a single reason
- **[HIGH/false]** (broker_property_non_viable) No pattern exists for clear/ceiling height below the client's requirement, so height-based non-viability is never caught by the backstop.
- **[HIGH/false]** (broker_property_non_viable) The single clearest non-viable phrase ('not a true warehouse fit') alone counts as one mismatch and fails the >=2 threshold, and the fit_rejection regex won't span 'is not a true w
- **[HIGH/false]** (broker_property_non_viable) _latest_inbound_text returns the full inbound body including quoted email history, so a NEW positive reply that quotes an OLD rejection re-triggers property_unavailable.
- **[MEDIUM/false]** (broker_property_non_viable) Single-reason 'lacks sufficient warehouse space' non-viable message fails the >=2 threshold with no fit-rejection phrase.
- **[MEDIUM/false]** (broker_property_non_viable) No grade-level/drive-in access is a single mismatch and does not fire without a second signal or explicit fit-rejection phrase.
- **[HIGH/false]** (broker_available_full_specs) The deterministic asking-rent parser has no rent-keyword gating on its second regex (dollar_per_sf), so ANY '$X/SF' figure >= $1 that is NOT rent (TI allowance, taxes, parking, bui
- **[MEDIUM/false]** (broker_available_full_specs) Both literal full-spec seed phrasings state an asking rate using a rate suffix ('gross' / 'NNN') without an adjacent '/SF' token, and the parser returns None because it requires an
- **[MEDIUM/false]** (broker_available_full_specs) The requirements-mismatch near-miss 'Available for office use only while warehouse requirement remains unmet' is not detected by _looks_like_requirements_mismatch_nonviable (return
- **[MEDIUM/wrong]** (broker_wrong_contact) _filter_reply_all_draft_recipients only strips the operator's own address when user_email is truthy; if user_email is None/empty the operator filter is silently skipped and the mai
- **[HIGH/false]** (broker_attachment_or_link_only) A broken or access-protected broker link (403 protected Drive file, dead link) is silently dropped: fetch_and_process_linked_assets swallows the download exception and returns [], 
- **[HIGH/false]** (broker_attachment_or_link_only) build_download_candidate returns None for the everyday CRE file-share hosts SharePoint/OneDrive (1drv.ms), Box, WeTransfer, and Google Drive *folder* links. A broker email whose en
- **[HIGH/false]** (broker_attachment_or_link_only) When PDF text extraction AND the OpenAI upload fallback both fail (process_pdf_for_ai returns method='failed', empty text), fetch_and_process_pdfs still appends the entry with a dr
- **[MEDIUM/wrong]** (broker_attachment_or_link_only) No deterministic property/address guard exists: build_download_candidate accepts a forwarded flyer link for a DIFFERENT property and produces a valid candidate whose preview image 

### `email_automation/outbound_safety.py` — 10 bugs
- **[HIGH/false]** (broker_tour_available) contains_unreviewed_scheduling_language only matches a tiny whitelist ('tour scheduling', 'tour is being scheduled', 'before we proceed with tour', two 'include ... tour option/for
- **[HIGH/false]** (broker_tour_available) The whitelist regex is defeated by trivial phrasing shifts: the verb 'schedule a tour' (verb-before-noun) is not matched even though the noun-phrase 'tour scheduling' is, and commo
- **[HIGH/false]** (broker_confidential_question) validate_outbound_body (the final send gate in send_reply_in_thread) has no confidential-disclosure check, so an auto-reply that reveals the confidential client/tenant identity pas
- **[MEDIUM/false]** (broker_confidential_question) validate_outbound_body does not detect fabricated approval/budget language, so an auto-reply claiming the client is approved or naming a budget passes the send gate. This is stop c
- **[HIGH/false]** (launch_with_variable_mapping) validate_outbound_body / find_unresolved_placeholders only detect square-bracket [..] placeholders, so every other merge-field syntax (double-curly {{name}}, single-brace {name}, a
- **[HIGH/false]** (launch_with_variable_mapping) _personalize_name_placeholders (NAME_PLACEHOLDER_RE) substitutes only bracket [NAME]-style tokens, so a resolved contact name never replaces a {{name}}/{name} merge field; combined
- **[LOW/false]** (launch_with_variable_mapping) Personalization treats a company name in a name-like column as a human first name: _safe_greeting_first_name('Acme Realty LLC') -> 'Acme', producing a 'Hi Acme,' greeting on a non-
- **[HIGH/false]** (broker_available_partial_specs) Completion guard accepts free-text placeholder values ('TBD', 'pending', 'TBC', 'N/A', '?', 'to follow', 'ask landlord') as satisfied required fields, so partial data can mark the 
- **[HIGH/false]** (broker_available_partial_specs) A stale 'no longer available' line inside quoted email history overrides the newer 'available again' facts and marks the property non-viable.
- **[MEDIUM/false]** (broker_available_partial_specs) Non-fit detection misses common casual phrasings, so 'partial specs + clear non-fit' is NOT marked non-viable and the pipeline keeps chasing specs on a rejected property (near-miss

### `email_automation/processing.py` — 6 bugs
- **[HIGH/false]** (broker_opt_out) is_contact_opted_out fails OPEN: it swallows any exception and returns None ('not opted out'), and 3 of its 4 callers (reply-all CC filter email.py:890, send_and_index email.py:179
- **[HIGH/false]** (followup_due) The terminal-block guard omits status="paused" (the escalated/needs-user-action state) from its blocking set, so a due follow-up can be auto-sent on a thread that was escalated to 
- **[MEDIUM/false]** (reply_all_cc_context) Opt-out check is keyed on the exact address hash, so an opted-out mailbox reached via a plus alias bypasses the block and stays on the reply-all audience.
- **[LOW/false]** (reply_all_cc_context) Operator self-removal compares only the exact normalized address, so the operator's own mailbox under a plus alias survives and gets a reply-all (self-send / auto-processing loop r
- **[HIGH/false]** (manual_user_continuation) The Sent Items continuation lookup is not scoped to the conversation server-side and caps at $top=10 with no pagination (@odata.nextLink is never followed). Graph is queried only w
- **[MEDIUM/false]** (manual_user_continuation) Fail-open on an unusable sent_after: coerce_utc_datetime returns None for an unparseable/None timestamp and the guard returns None WITHOUT querying Sent Items, instead of failing c

### `email_automation/email.py` — 5 bugs
- **[HIGH/false]** (dashboard_action_resolution) _is_cancelled_outbox_item uses an identity check `cancelRequested is True`, so a truthy-but-not-True cancel flag (string "true" from a loosely-typed JS toggle / Firestore REST / fo
- **[MEDIUM/false]** (dashboard_action_resolution) The recognized status set is exactly {cancel_requested, cancelled, canceled}, so a delimiter variant of the canonical status ('cancel-requested' with a hyphen instead of underscore
- **[LOW/false]** (dashboard_action_resolution) Optimistic in-progress cancel statuses set by the UI on click ('cancelling' BrE / 'canceling' AmE) are outside the recognized set, so if the worker claims the item during the cance
- **[HIGH/false]** (retry_after_uncertain_send) _normalize_subject() only strips English reply prefixes (re|fw|fwd); regional Outlook reply prefixes on the already-sent copy (German 'AW:', Swedish 'SV:', French 'TR:') are not no
- **[HIGH/false]** (retry_after_uncertain_send) The subject veto overrides even a matching conversationId (strong identity): a Sent Items message with the same recipient, body AND conversationId is still discarded solely because

### `email_automation/sheet_operations.py` — 3 bugs
- **[HIGH/false]** (sheet_row_moved) When a thread subject parses to an EMPTY address (blank subject, or a subject that is only a reply prefix / bracket run-tag), _row_matches_subject_anchor returns True for ANY row (
- **[HIGH/false]** (sheet_row_moved) Address matching in _row_matches_subject_anchor accepts containment in EITHER direction (`want_addr in got_addr or got_addr in want_addr`), so a shorter decoy address is a substrin
- **[HIGH/false]** (sheet_row_moved) Same either-direction substring flaw causes the guard to CLAIM a match when only a decoy substring row exists and the real property is absent, instead of abstaining (returning None

### `email_automation/file_handling.py` — 1 bugs
- **[HIGH/false]** (token_or_graph_failure) fetch_pdf_attachments swallows a Graph 401/403/5xx/network error and returns an empty list [], which is indistinguishable from a message that genuinely has no PDF attachments; the 
