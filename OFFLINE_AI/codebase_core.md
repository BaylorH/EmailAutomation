# EmailAutomation — CORE Brief (small, for 8K-context phone models)
Map of the core pipeline package `email_automation/`. Pair with CLAUDE.md for architecture.

## email_automation/__init__.py

## email_automation/ai_processing.py
```
def _find_header_name(header, target)
def _proposal_updates_column(proposal, column_name)
def _row_value_for_column(rowvals, header, column_name)
def _latest_inbound_text(conversation)
def _extract_rent_sf_yr_from_text(text) — Best-effort deterministic fallback for common asking-rent phrases.
def _augment_proposal_with_deterministic_extractions(proposal, rowvals, header, effective_config, conversation) — Add high-confidence values from simple broker text patterns the LLM missed.
def _filter_config_by_extraction_fields(column_config, extraction_fields) — Filter column_config to only include fields specified in extraction_fields.
def get_row_anchor(rowvals, header) — Create a brief row anchor from property address and city.
def check_missing_required_fields(rowvals, header, column_config) — Check which required fields are missing from the row.
def _ensure_ai_meta_tab(sheets, spreadsheet_id) — Ensure AI_META tab exists with proper headers.
def _read_ai_meta_row(sheets, spreadsheet_id, rownum, column) — Read AI_META record for specific row/column.
def _append_ai_meta(sheets, spreadsheet_id, rownum, column, value, override) — Append new AI_META record.
def _append_notes_to_comments(sheets, spreadsheet_id, tab_title, header, rownum, notes) — Append notes to the comments field (Listing Brokers Comments or Jill and Clients Comments).
def apply_proposal_to_sheet(uid, client_id, sheet_id, header, rownum, current_rowvals, proposal) — Applies proposal['updates'] to the sheet row with AI write guards.
def propose_sheet_updates(uid, client_id, email, sheet_id, header, rownum, rowvals, thread_id, pdf_manifest, url_texts, contact_name, headers, conversation, column_config, extraction_fields, dry_run) — Uses OpenAI Responses API to propose sheet updates.
```

## email_automation/app_config.py

## email_automation/clients.py
```
def _helper_google_creds()
def _sheets_client()
def _get_sheet_id_or_fail(uid, client_id)
def _get_client_config(uid, client_id) — Retrieve sheet_id and column_config from client doc.
def list_user_ids()
def decode_token_payload(token)
```

## email_automation/column_config.py
Dynamic Column Configuration System
```
def get_default_column_config() — Returns default column configuration using standard aliases.
def get_default_mode_for_canonical(canonical) — Get the default column mode for a canonical field.
def detect_column_mapping(headers, use_ai) — Detect column mappings from sheet headers.
def _ai_match_columns(headers, canonicals) — Use AI to semantically match remaining headers to canonical fields.
def build_column_rules_prompt(column_config) — Build the COLUMN_RULES section of the AI prompt dynamically
def get_required_fields_for_close(column_config) — Get the list of required fields for closing a conversation,
def get_all_extractable_columns(column_config) — Get all columns that the AI can extract values for.
def translate_canonical_to_actual(canonical_name, column_config) — Translate a canonical field name to the actual column name.
def translate_actual_to_canonical(actual_name, column_config) — Translate an actual column name to its canonical field name.
```

## email_automation/email.py
```
def _has_existing_thread_for_property(user_id, recipient_email, property_address) — Check if we've already sent an email to this recipient about this property.
def _claim_outbox_item(doc_ref, data) — Attempt to claim an outbox item for processing using a transaction.
def _release_claim(doc_ref) — Release claim on an outbox item (called on failure to allow retry).
def _normalize_email(value)
def _get_reply_message_sender(headers, reply_to_msg_id) — Fetch the sender address of the message a dashboard reply targets.
def _assigned_emails_match_reply_sender(assigned_emails, reply_sender) — True when Graph /reply would send to the same single recipient shown in the UI.
def _get_thread_row_number(user_id, thread_id) — Return the stored Sheet row number for a known thread, if present.
def _save_outbox_reply_message(user_id, thread_id, assigned_emails, subject, body, user_signature, signature_mode) — Persist a dashboard-approved Graph /reply send into the conversation history.
def _send_outbox_as_reply(user_id, headers, body, reply_to_msg_id, thread_id, user_signature, signature_mode) — Send an outbox item as a reply to an existing message in a thread.
def get_contact_email_count(user_id, recipient_email) — Count how many outbound emails have been sent to this contact.
def _extract_requirements_from_primary(primary_script) — Extract the requirements section from a primary script for reuse in fallback scenarios.
def _select_script_for_recipient(user_id, recipient_email, scripts, contact_name) — Select appropriate script based on contact history.
def _should_use_exact_outbox_script(data) — True when the outbox item contains approved copy that must not be re-selected.
def _is_cancelled_outbox_item(data) — True when the dashboard has requested cancellation before the worker sends.
def _delete_cancelled_outbox_item_if_needed(doc_ref, data)
def _must_process_outbox_item_individually(data) — Dashboard-approved replies/exact-copy items must not be bundled into campaign outreach.
def _get_current_outbox_data(doc_ref)
def _finalize_successful_outbox_item(user_id, doc_ref, data, row_number, client_id) — Delete sent outbox and apply post-send dashboard state only after send success.
def _subject_for_recipient(uid, client_id, recipient_email) — Look up the row by email and return 'property address, city' as subject.
def send_and_index_email(user_id, headers, script, recipients, client_id_or_none, row_number, user_signature, subject_override, signature_mode, followup_config, contact_name) — Send email and immediately index it in Firestore for reply tracking.
def _move_to_dead_letter(user_id, doc_ref, data, reason) — Move a failed outbox item to the dead-letter queue for manual review.
def send_outboxes(user_id, headers) — Process outbox items: read script content (generated by frontend LLM), append footer, and send.
def _send_multi_property_email(user_id, headers, recipient_email, items, user_signature, signature_mode) — Send SEPARATE emails for multiple properties to the same broker.
def _send_single_outbox_item(user_id, headers, item, user_signature, signature_mode) — Send a single outbox item with smart script selection based on contact history.
def _extract_property_from_script(script) — Try to extract property address from email script.
def send_email(headers, script, emails, client_id) — Legacy function - redirects to send_and_index_email
```

## email_automation/email_operations.py
```
def _get_user_signature_settings(uid) — Fetch user's signature settings from Firestore.
def _add_signature_attachments_to_draft(headers, draft_id, signature_mode) — Add signature image attachments to a draft message.
def send_remaining_questions_email(uid, client_id, headers, recipient, missing_fields, thread_id, row_number, row_anchor) — Send a remaining questions email in the same thread (idempotent).
def send_closing_email(uid, client_id, headers, recipient, thread_id, row_number, row_anchor) — Send polite closing email when all required fields are complete.
def send_new_property_email(uid, client_id, headers, recipient, address, city, row_number) — Send a new thread email for a new property suggestion.
def send_thankyou_closing_with_new_property(uid, client_id, headers, recipient, thread_id, row_number, row_anchor, new_property_address) — Send thank you when property is unavailable but they suggested a new one.
def send_thankyou_ask_alternatives(uid, client_id, headers, recipient, thread_id, row_number, row_anchor) — Send thank you + ask for alternatives when property is unavailable.
```

## email_automation/file_handling.py
```
def extract_pdf_text(content, filename) — Extract text from PDF using multiple strategies for maximum coverage.
def clean_extracted_text(text) — Clean up extracted PDF text for better model comprehension.
def process_pdf_for_ai(content, filename) — Process a PDF and prepare it for AI consumption.
def fetch_pdf_attachments(headers, graph_msg_id) — Fetch PDF attachments from current message only.
def ensure_drive_folder() — Ensure Drive folder exists and return folder ID.
def upload_pdf_to_drive(name, content, folder_id) — Upload PDF to Drive and return webViewLink.
def upload_pdf_user_data(filename, content) — Upload PDF to OpenAI with purpose='user_data' and return file_id.
def fetch_and_process_pdfs(headers, graph_msg_id) — Fetch PDF attachments and process them for AI consumption.
```

## email_automation/followup.py
Automatic Follow-Up Email System
```
def _claim_followup(user_id, thread_id, current_index) — Atomically claim a follow-up for processing to prevent duplicate sends.
def _release_followup_claim(user_id, thread_id) — Release claim on a follow-up (called on failure to allow retry).
def _save_followup_message(user_id, thread_id, recipient, subject, body, user_signature, signature_mode) — Persist a sent follow-up into thread history for dashboard reconciliation.
def _clear_followup_row_highlight(user_id, thread_id) — Clear Sheet highlight when a follow-up sequence reaches a terminal state.
def _is_graph_backed_outbound_message(message_data) — True when an outbound history entry can be found again through Microsoft Graph.
def _select_reply_anchor_message(outbound_message_docs) — Pick the newest outbound message that has a real Graph internetMessageId.
def check_and_send_followups(user_id, headers) — Main entry point: scan threads needing follow-ups and send them.
def _send_followup_email(user_id, headers, thread_id, thread_data, followup_config, followup_index) — Send a follow-up email for a specific thread.
def _schedule_next_followup(user_id, thread_id, followup_config, just_sent_index) — Schedule the next follow-up in the sequence.
def schedule_followup_after_auto_response(user_id, thread_id) — Resume follow-up tracking after the system sends an automatic mid-thread reply.
def _pause_followup(user_id, thread_id) — Pause follow-up sequence when broker responds.
def _mark_followup_complete(user_id, thread_id, reason) — Mark follow-up sequence as complete.
def schedule_followup_for_thread(user_id, thread_id, followup_config) — Schedule follow-ups for a newly sent thread.
def cancel_followup_on_response(user_id, thread_id) — Pause pending follow-up when broker responds.
def resume_followup_if_silent(user_id, thread_id, silence_threshold_days) — Resume follow-up sequence if broker went silent after responding.
def _get_default_followup_message(index) — Return default follow-up message based on sequence position.
```

## email_automation/logging.py
```
def _ensure_log_tab_exists(sheets, spreadsheet_id) — Ensure 'Log' tab exists and return its title.
def _get_last_logged_message_id(sheets, spreadsheet_id, tab_title, thread_id) — Get the last message ID that was logged for this thread.
def write_message_order_test(uid, thread_id, sheet_id) — 1) Read the first tab title.
```

## email_automation/messaging.py
```
def _normalize_history_text(value)
def _normalize_history_subject(value)
def _message_preview(payload)
def _message_recipients(payload)
def _message_ts_seconds(payload)
def _is_synthetic_outbound(message_id, payload)
def _is_real_graph_outbound(message_id, payload)
def _outbound_history_match(real_payload, synthetic_payload)
def _delete_synthetic_outbound_duplicates(user_id, thread_id, message_id, payload)
def update_thread_status(user_id, thread_id, status, reason) — Update the status of a thread.
def get_thread_status(user_id, thread_id) — Get the current status of a thread.
def save_thread_root(user_id, root_id, meta) — Save or update thread root document. Returns True on success, False on failure.
def save_message(user_id, thread_id, message_id, payload) — Save message to thread. Returns True on success, False on failure.
def _message_index_candidates(message_id) — Return normalized msgIndex keys, with legacy raw-key fallback.
def index_message_id(user_id, message_id, thread_id) — Index message ID for O(1) lookup. Returns True on success, False on failure.
def lookup_thread_by_message_id(user_id, message_id) — Look up thread ID by message ID.
def index_conversation_id(user_id, conversation_id, thread_id) — Index conversation ID for fallback lookup. Returns True on success, False on failure.
def lookup_thread_by_conversation_id(user_id, conversation_id) — Look up thread ID by conversation ID (fallback).
def lookup_thread_by_conversation_id_exhaustive(user_id, conversation_id) — Exhaustive search for thread by conversation ID.
def _get_thread_messages_chronological(uid, thread_id) — Get all messages in thread in chronological order.
def _message_body_content_and_preview(data) — Normalize legacy string-body docs and current dict-body docs.
def build_conversation_payload(uid, thread_id, limit, headers) — Return last N messages in chronological order. Each item includes:
def dump_thread_from_firestore(user_id, thread_id) — Console dump of thread conversation in chronological order.
def _processed_ref(user_id, key) — Get reference to processed message document.
def has_processed(user_id, key) — Check if a message has already been processed.
def mark_processed(user_id, key) — Mark a message as processed.
def _sync_ref(user_id) — Get reference to sync document.
def get_last_scan_iso(user_id) — Get the last scan timestamp.
def set_last_scan_iso(user_id, iso_str) — Set the last scan timestamp.
def get_handled_events(user_id, thread_id) — Get all previously handled events for a thread.
def is_event_handled(user_id, thread_id, event_key) — Check if a specific event has already been handled for this thread.
def mark_event_handled(user_id, thread_id, event_key, msg_id, notif_id) — Record that an event has been handled for this thread.
def build_event_key(event_type, event, thread_id) — Build a unique event key for deduplication.
```

## email_automation/notification_payloads.py
```
def _first_name(name_or_email)
def build_wrong_contact_suggested_email() — Build the InlineReplyComposer-compatible payload for wrong-contact referrals.
def build_new_property_suggested_email() — Build a fresh first-touch outreach for a broker-suggested replacement property.
def should_skip_original_reply_for_new_property_referral() — Avoid asking the original broker to connect us when they already gave a new contact.
def sanitize_new_property_referral_response(proposal) — Clear reply-back drafts when a new-property referral includes a different direct email.
```

## email_automation/notifications.py
```
def write_notification(uid, client_id) — Write notification and bump counters atomically.
def add_client_notifications(uid, client_id, email, thread_id, applied_updates, notes, address) — UPDATED: Writes one notification doc per applied field change.
```

## email_automation/openai_usage.py
```
def _read_value(obj)
def _as_int(value)
def _pricing_for(model)
def _usage_metrics(usage)
def estimate_openai_cost(model, usage)
def _safe_metadata(metadata)
def _rollup_payload(operation, model, estimate)
def record_openai_usage()
def track_openai_usage_safely()
```

## email_automation/pending_responses.py
Pending Responses Queue
```
def queue_pending_response(user_id, thread_id, msg_id, recipient, response_body, client_id, error) — Queue a failed response for later retry.
def get_pending_responses(user_id) — Get all pending responses that haven't exceeded max attempts.
def process_pending_responses(user_id, headers) — Retry sending all pending responses.
def clear_pending_response(user_id, thread_id) — Remove a pending response (called after successful manual send or when no longer needed).
```

## email_automation/processing.py
```
class RetryableProcessingError
def _should_mark_processed_after_error(error)
def _record_ai_processing_failure(user_id, client_id, thread_id, message_id, reason)
def _should_skip_processing_for_terminal_thread(thread_status)
def _close_reason_from_event(event)
def _close_event_can_bypass_missing_fields(event)
def _response_mentions_missing_fields(response_body, missing_fields) — Detect whether an LLM response is actually asking for the missing fields.
def _format_event_property(event)
def _build_property_unavailable_comment(current_date, found_keyword, events)
def _store_contact_optout(user_id, email, reason, thread_id) — Store a contact's opt-out status in Firestore.
def is_contact_opted_out(user_id, email) — Check if a contact has opted out of communications.
def _build_greeting(contact_name) — Build a personalized greeting using the contact's first name, or generic 'Hi,' if no name.
def send_reply_in_thread(user_id, headers, body, current_msg_id, recipient, thread_id) — Send a reply to the current message being processed and index it for future replies
def _find_client_id_by_email(uid, email) — Search through all clients (active and archived) to find which one has a sheet
def fetch_and_log_sheet_for_thread(uid, thread_id, counterparty_email)
def process_inbox_message(user_id, headers, msg) — ENHANCED: Process a single inbox message with full pipeline including events.
def scan_inbox_against_index(user_id, headers, only_unread, top) — Idempotent scan of inbox for replies with early exit on processed messages.
def _match_message_to_thread(user_id, msg, headers) — Try to match an inbox message to an existing thread.
def _save_message_to_thread(user_id, thread_id, msg, headers) — Save a message to a thread without full processing.
def scan_sent_items_for_manual_replies(user_id, headers, top) — Scan SentItems for Jill's manual replies to conversations we're tracking.
```

## email_automation/service_providers.py
Service Providers Abstraction Layer
```
def set_test_mode(enabled, mock_services) — Enable or disable test mode with optional mock service implementations.
def is_test_mode() — Check if test mode is enabled.
def get_provider(service_name) — Get the appropriate provider for a service (real or mock).
class EmailMessage
class EmailProvider
  .list_messages(self, folder, filter_query, top)
  .get_message(self, message_id)
  .create_draft(self, subject, body, to_recipients, cc_recipients, headers)
  .send_draft(self, draft_id)
  .reply_to_message(self, message_id, body)
  .send_new_message(self, subject, body, to_recipients, cc_recipients)
  .get_attachments(self, message_id)
  .lookup_message_by_internet_id(self, internet_message_id)
class RealEmailProvider
  .__init__(self)
  ._get_headers(self)
  .list_messages(self, folder, filter_query, top)
  .get_message(self, message_id)
  .create_draft(self, subject, body, to_recipients, cc_recipients, headers_extra)
  .send_draft(self, draft_id)
  .reply_to_message(self, message_id, body)
  .send_new_message(self, subject, body, to_recipients, cc_recipients)
  .get_attachments(self, message_id)
  .lookup_message_by_internet_id(self, internet_message_id)
class SheetsProvider
  .get_values(self, sheet_id, range_notation)
  .update_values(self, sheet_id, range_notation, values, value_input_option)
  .batch_update_values(self, sheet_id, data)
  .append_values(self, sheet_id, range_notation, values, value_input_option)
  .get_sheet_metadata(self, sheet_id)
class RealSheetsProvider
  .__init__(self)
  .get_values(self, sheet_id, range_notation)
  .update_values(self, sheet_id, range_notation, values, value_input_option)
  .batch_update_values(self, sheet_id, data)
  .append_values(self, sheet_id, range_notation, values, value_input_option)
  .get_sheet_metadata(self, sheet_id)
class FirestoreDocument
  .to_dict(self)
class FirestoreProvider
  .get_document(self, path)
  .set_document(self, path, data, merge)
  .update_document(self, path, data)
  .delete_document(self, path)
  .query_collection(self, path, filters, order_by, limit)
  .list_subcollection(self, path)
class RealFirestoreProvider
  .__init__(self)
  ._path_to_ref(self, path)
  .get_document(self, path)
  .set_document(self, path, data, merge)
  .update_document(self, path, data)
  .delete_document(self, path)
  .query_collection(self, path, filters, order_by, limit)
  .list_subcollection(self, path)
class DriveProvider
  .list_files(self, query, page_size)
  .create_folder(self, name, parent_id)
  .upload_file(self, name, content, mime_type, parent_id)
  .set_public_permission(self, file_id)
class RealDriveProvider
  .__init__(self)
  .list_files(self, query, page_size)
  .create_folder(self, name, parent_id)
  .upload_file(self, name, content, mime_type, parent_id)
  .set_public_permission(self, file_id)
class OpenAIProvider
  .chat_completion(self, messages, model, temperature, response_format)
  .upload_file(self, content, filename, purpose)
class RealOpenAIProvider
  .__init__(self)
  .chat_completion(self, messages, model, temperature, response_format)
  .upload_file(self, content, filename, purpose)
def generate_id(prefix) — Generate a unique ID for testing.
def generate_message_id() — Generate a realistic internet message ID.
def generate_conversation_id() — Generate a conversation ID.
```

## email_automation/sheet_operations.py
```
def sync_thread_row_numbers_after_move(user_id, src_row, divider_row, new_row, client_id) — Update thread rowNumbers after a row is moved below the NON-VIABLE divider.
def sync_thread_row_numbers_after_insert(user_id, insert_row, client_id) — Update thread rowNumbers after a new sheet row is inserted.
def _find_header_position(header, aliases)
def _build_gross_rent_formula_for_row(header, rownum)
def _apply_gross_rent_formula_for_row(sheets, sheet_id, tab_title, header, rownum)
def _find_nonviable_divider_row(sheets, spreadsheet_id, tab_title) — Return the divider row index if it exists, else None (no creation).
def _is_row_below_nonviable(sheets, spreadsheet_id, tab_title, rownum) — Stateless check: is this row visually below the 'NON-VIABLE' divider?
def _ensure_divider_conditional_formatting(sheets, spreadsheet_id) — Add a conditional formatting rule that paints ANY row red + bold white text
def ensure_nonviable_divider(sheets, spreadsheet_id, tab_title) — Ensure a NON-VIABLE divider row exists. Returns the divider row number.
def move_row_below_divider(sheets, spreadsheet_id, tab_title, src_row, divider_row) — Move src_row to immediately below the divider *and* keep the divider as the boundary.
def insert_property_row_above_divider(sheets, sheet_id, tab_title, values_by_header) — Insert a new property row one row above the divider (or at end if no divider).
def _find_row_by_anchor(uid, thread_id, sheets, spreadsheet_id, tab_title, header, fallback_email)
```

## email_automation/sheets.py
```
def _execute_with_retry(request, operation_name) — Execute a Google Sheets API request with exponential backoff retry on rate limits.
def _header_index_map(header) — Normalize headers for exact match regardless of spacing/case.
def _col_letter(n) — 1-indexed column number -> A1 letter (1->A).
def _get_first_tab_title(sheets, spreadsheet_id)
def _read_header_row2(sheets, spreadsheet_id, tab_title)
def _first_sheet_props(sheets, spreadsheet_id)
def _guess_email_col_idx(header)
def _find_row_by_email(sheets, spreadsheet_id, tab_title, header, email) — Returns (row_number, row_values) where row_number is the 1-based sheet row.
def _find_row_by_address_city(sheets, spreadsheet_id, tab_title, header, address, city)
def format_sheet_columns_autosize_with_exceptions(spreadsheet_id, header) — Auto-size all columns to the longest visible value + padding, with exceptions:
def append_links_to_flyer_link_column(sheets, spreadsheet_id, header, rownum, links) — Find/create Flyer / Link column and append unique links. Returns newly added links.
def append_links_to_floorplan_column(sheets, spreadsheet_id, header, rownum, links) — Find/create Floorplan column and append unique links. Returns newly added links.
def is_floorplan_filename(filename) — Detect if a PDF filename indicates it's a floorplan/building plan.
def highlight_row(spreadsheet_id, rownum, color) — Apply background color highlight to an entire row.
def clear_row_highlight(spreadsheet_id, rownum) — Remove background color from an entire row (set to white/default).
def highlight_rows_batch(spreadsheet_id, rownums, color) — Apply background color highlight to multiple rows in a single API call.
```

## email_automation/utils.py
```
def _body_kind(script)
def _normalize_email(s)
def strip_email_quotes(text) — Strip quoted content from email replies to get just the new message content.
def is_valid_email(email) — Validate email address format.
def validate_recipient_emails(emails) — Validate a list of email addresses.
def _norm_txt(x)
def b64url_id(message_id) — Encode message ID for safe use as Firestore document key.
def normalize_message_id(msg_id) — Normalize message ID - strip whitespace and angle brackets.
def parse_references_header(references) — Parse References header into list of message IDs.
def strip_html_tags(html) — Strip HTML tags for preview.
def clean_email_content(content) — Clean email content for AI processing.
def safe_preview(content, max_len) — Create safe preview of email content.
def exponential_backoff_request(func, max_retries) — Execute request with exponential backoff on rate limits.
def fetch_url_as_text(url) — Try to fetch URL content and extract visible text using BeautifulSoup.
def _sanitize_url(u)
def _subject_to_address_city(subject)
def _upload_logo_to_drive(image_filename) — Get or upload image to Google Drive and return public direct image URL.
def _image_to_base64(image_path) — Convert image file to base64 data URI for email embedding.
def get_signature_attachments() — Get signature images as inline attachments for Microsoft Graph API.
def convert_plain_text_signature_to_html(plain_text_signature) — Converts a plain text email signature to HTML format.
def get_email_footer(custom_signature, signature_mode) — Returns HTML formatted email footer.
def needs_signature_attachments(signature_mode) — Check if the signature mode requires inline image attachments.
def format_email_body_with_footer(body, custom_signature, signature_mode) — Converts plain text email body to HTML and appends footer.
```

