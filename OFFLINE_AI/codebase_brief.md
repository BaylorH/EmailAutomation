# EmailAutomation — Codebase Brief (offline AI grounding file)
This file is a condensed map of the EmailAutomation backend, generated from the source. Use it as context for an offline LLM to ask questions about the system. For deep dives on one module, paste that file's full source from codebase_full.md.

---

## Module Map (signatures + docstrings)

### `app.py`
```
  def get_base_url()
  def check_token_status()  — Check if we have a valid token
  def auto_upload_token()  — Automatically upload token to Firebase if valid
  def run_scheduler()  — Run the full scheduler for all users - same logic as main.py
  def index()
  def api_status()
  def api_upload()
  def api_clear()
  def api_refresh()
  def api_trigger_scheduler()  — API endpoint to manually trigger the email scheduler.
  def api_scheduler_status()  — Get the current status of the scheduler
  def api_decline_property()  — Delete a property row from a Google Sheet when user declines a new property suggestion.
  def api_accept_new_property()  — Accept a new property suggestion - creates the row in the sheet.
  def api_resume_conversation()  — Resume monitoring a paused conversation thread.
  def api_stop_conversation()  — Stop monitoring a conversation thread.
  def api_clear_optout()  — Clear an opt-out record for a contact, allowing emails to be sent again.
  def api_list_optouts()  — List all opted-out contacts for a user.
  def api_check_sheet_completion()  — Check if all rows in a sheet have all required fields filled.
  def api_debug_inbox()  — Debug endpoint to check inbox status and email processing
  def api_debug_thread_matching()  — Debug endpoint to check thread matching for specific conversation
  def api_console_logs()  — Read browser console logs forwarded from frontend to Firestore.
  def api_console_logs_clear()  — Clear all console logs for a user.
  def auth_login()
  def auth_callback()
  def api_firestore_inspect()  — Inspect Firestore database structure and count documents.
  def api_firestore_cleanup()  — Clean up stale Firestore data.
  def api_clear_outlook_emails()  — Clear campaign-related emails from Outlook (SentItems and optionally Inbox).
```

### `auth_service/auth_service.py`
```
  def start_flow()
  def complete_flow()
```

### `auth_service/firebase_helpers.py`
```
  def download_token(api_key, output_file, user_id)
  def upload_token(api_key, input_file, user_id)
  def upload_excel(api_key, input_file, user_id)
```

### `auth_service/msal_test/get_token_and_upload.py`
(no top-level functions/classes)

### `config.py`
(no top-level functions/classes)

### `email_automation/__init__.py`
(no top-level functions/classes)

### `email_automation/ai_processing.py`
```
  def _find_header_name(header, target)
  def _proposal_updates_column(proposal, column_name)
  def _row_value_for_column(rowvals, header, column_name)
  def _latest_inbound_text(conversation)
  def _extract_rent_sf_yr_from_text(text)  — Best-effort deterministic fallback for common asking-rent phrases.
  def _augment_proposal_with_deterministic_extractions(proposal, rowvals, header, effective_config, conversation)  — Add high-confidence values from simple broker text patterns the LLM missed.
  def _filter_config_by_extraction_fields(column_config, extraction_fields)  — Filter column_config to only include fields specified in extraction_fields.
  def get_row_anchor(rowvals, header)  — Create a brief row anchor from property address and city.
  def check_missing_required_fields(rowvals, header, column_config)  — Check which required fields are missing from the row.
  def _ensure_ai_meta_tab(sheets, spreadsheet_id)  — Ensure AI_META tab exists with proper headers.
  def _read_ai_meta_row(sheets, spreadsheet_id, rownum, column)  — Read AI_META record for specific row/column.
  def _append_ai_meta(sheets, spreadsheet_id, rownum, column, value, override)  — Append new AI_META record.
  def _append_notes_to_comments(sheets, spreadsheet_id, tab_title, header, rownum, notes)  — Append notes to the comments field (Listing Brokers Comments or Jill and Clients Comments).
  def apply_proposal_to_sheet(uid, client_id, sheet_id, header, rownum, current_rowvals, proposal)  — Applies proposal['updates'] to the sheet row with AI write guards.
  def propose_sheet_updates(uid, client_id, email, sheet_id, header, rownum, rowvals, thread_id, pdf_manifest, url_texts, contact_name, headers, conversation, column_config, extraction_fields, dry_run)  — Uses OpenAI Responses API to propose sheet updates.
```

### `email_automation/app_config.py`
(no top-level functions/classes)

### `email_automation/clients.py`
```
  def _helper_google_creds()
  def _sheets_client()
  def _get_sheet_id_or_fail(uid, client_id)
  def _get_client_config(uid, client_id)  — Retrieve sheet_id and column_config from client doc.
  def list_user_ids()
  def decode_token_payload(token)
```

### `email_automation/column_config.py`
> Dynamic Column Configuration System
```
  def get_default_column_config()  — Returns default column configuration using standard aliases.
  def get_default_mode_for_canonical(canonical)  — Get the default column mode for a canonical field.
  def detect_column_mapping(headers, use_ai)  — Detect column mappings from sheet headers.
  def _ai_match_columns(headers, canonicals)  — Use AI to semantically match remaining headers to canonical fields.
  def build_column_rules_prompt(column_config)  — Build the COLUMN_RULES section of the AI prompt dynamically
  def get_required_fields_for_close(column_config)  — Get the list of required fields for closing a conversation,
  def get_all_extractable_columns(column_config)  — Get all columns that the AI can extract values for.
  def translate_canonical_to_actual(canonical_name, column_config)  — Translate a canonical field name to the actual column name.
  def translate_actual_to_canonical(actual_name, column_config)  — Translate an actual column name to its canonical field name.
```

### `email_automation/email.py`
```
  def _has_existing_thread_for_property(user_id, recipient_email, property_address)  — Check if we've already sent an email to this recipient about this property.
  def _claim_outbox_item(doc_ref, data)  — Attempt to claim an outbox item for processing using a transaction.
  def _release_claim(doc_ref)  — Release claim on an outbox item (called on failure to allow retry).
  def _normalize_email(value)
  def _get_reply_message_sender(headers, reply_to_msg_id)  — Fetch the sender address of the message a dashboard reply targets.
  def _assigned_emails_match_reply_sender(assigned_emails, reply_sender)  — True when Graph /reply would send to the same single recipient shown in the UI.
  def _get_thread_row_number(user_id, thread_id)  — Return the stored Sheet row number for a known thread, if present.
  def _save_outbox_reply_message(user_id, thread_id, assigned_emails, subject, body, user_signature, signature_mode)  — Persist a dashboard-approved Graph /reply send into the conversation history.
  def _send_outbox_as_reply(user_id, headers, body, reply_to_msg_id, thread_id, user_signature, signature_mode)  — Send an outbox item as a reply to an existing message in a thread.
  def get_contact_email_count(user_id, recipient_email)  — Count how many outbound emails have been sent to this contact.
  def _extract_requirements_from_primary(primary_script)  — Extract the requirements section from a primary script for reuse in fallback scenarios.
  def _select_script_for_recipient(user_id, recipient_email, scripts, contact_name)  — Select appropriate script based on contact history.
  def _should_use_exact_outbox_script(data)  — True when the outbox item contains approved copy that must not be re-selected.
  def _is_cancelled_outbox_item(data)  — True when the dashboard has requested cancellation before the worker sends.
  def _delete_cancelled_outbox_item_if_needed(doc_ref, data)
  def _must_process_outbox_item_individually(data)  — Dashboard-approved replies/exact-copy items must not be bundled into campaign outreach.
  def _get_current_outbox_data(doc_ref)
  def _finalize_successful_outbox_item(user_id, doc_ref, data, row_number, client_id)  — Delete sent outbox and apply post-send dashboard state only after send success.
  def _subject_for_recipient(uid, client_id, recipient_email)  — Look up the row by email and return 'property address, city' as subject.
  def send_and_index_email(user_id, headers, script, recipients, client_id_or_none, row_number, user_signature, subject_override, signature_mode, followup_config, contact_name)  — Send email and immediately index it in Firestore for reply tracking.
  def _move_to_dead_letter(user_id, doc_ref, data, reason)  — Move a failed outbox item to the dead-letter queue for manual review.
  def send_outboxes(user_id, headers)  — Process outbox items: read script content (generated by frontend LLM), append footer, and send.
  def _send_multi_property_email(user_id, headers, recipient_email, items, user_signature, signature_mode)  — Send SEPARATE emails for multiple properties to the same broker.
  def _send_single_outbox_item(user_id, headers, item, user_signature, signature_mode)  — Send a single outbox item with smart script selection based on contact history.
  def _extract_property_from_script(script)  — Try to extract property address from email script.
  def send_email(headers, script, emails, client_id)  — Legacy function - redirects to send_and_index_email
```

### `email_automation/email_operations.py`
```
  def _get_user_signature_settings(uid)  — Fetch user's signature settings from Firestore.
  def _add_signature_attachments_to_draft(headers, draft_id, signature_mode)  — Add signature image attachments to a draft message.
  def send_remaining_questions_email(uid, client_id, headers, recipient, missing_fields, thread_id, row_number, row_anchor)  — Send a remaining questions email in the same thread (idempotent).
  def send_closing_email(uid, client_id, headers, recipient, thread_id, row_number, row_anchor)  — Send polite closing email when all required fields are complete.
  def send_new_property_email(uid, client_id, headers, recipient, address, city, row_number)  — Send a new thread email for a new property suggestion.
  def send_thankyou_closing_with_new_property(uid, client_id, headers, recipient, thread_id, row_number, row_anchor, new_property_address)  — Send thank you when property is unavailable but they suggested a new one.
  def send_thankyou_ask_alternatives(uid, client_id, headers, recipient, thread_id, row_number, row_anchor)  — Send thank you + ask for alternatives when property is unavailable.
```

### `email_automation/file_handling.py`
```
  def extract_pdf_text(content, filename)  — Extract text from PDF using multiple strategies for maximum coverage.
  def clean_extracted_text(text)  — Clean up extracted PDF text for better model comprehension.
  def process_pdf_for_ai(content, filename)  — Process a PDF and prepare it for AI consumption.
  def fetch_pdf_attachments(headers, graph_msg_id)  — Fetch PDF attachments from current message only.
  def ensure_drive_folder()  — Ensure Drive folder exists and return folder ID.
  def upload_pdf_to_drive(name, content, folder_id)  — Upload PDF to Drive and return webViewLink.
  def upload_pdf_user_data(filename, content)  — Upload PDF to OpenAI with purpose='user_data' and return file_id.
  def fetch_and_process_pdfs(headers, graph_msg_id)  — Fetch PDF attachments and process them for AI consumption.
```

### `email_automation/followup.py`
> Automatic Follow-Up Email System
```
  def _claim_followup(user_id, thread_id, current_index)  — Atomically claim a follow-up for processing to prevent duplicate sends.
  def _release_followup_claim(user_id, thread_id)  — Release claim on a follow-up (called on failure to allow retry).
  def _save_followup_message(user_id, thread_id, recipient, subject, body, user_signature, signature_mode)  — Persist a sent follow-up into thread history for dashboard reconciliation.
  def _clear_followup_row_highlight(user_id, thread_id)  — Clear Sheet highlight when a follow-up sequence reaches a terminal state.
  def _is_graph_backed_outbound_message(message_data)  — True when an outbound history entry can be found again through Microsoft Graph.
  def _select_reply_anchor_message(outbound_message_docs)  — Pick the newest outbound message that has a real Graph internetMessageId.
  def check_and_send_followups(user_id, headers)  — Main entry point: scan threads needing follow-ups and send them.
  def _send_followup_email(user_id, headers, thread_id, thread_data, followup_config, followup_index)  — Send a follow-up email for a specific thread.
  def _schedule_next_followup(user_id, thread_id, followup_config, just_sent_index)  — Schedule the next follow-up in the sequence.
  def schedule_followup_after_auto_response(user_id, thread_id)  — Resume follow-up tracking after the system sends an automatic mid-thread reply.
  def _pause_followup(user_id, thread_id)  — Pause follow-up sequence when broker responds.
  def _mark_followup_complete(user_id, thread_id, reason)  — Mark follow-up sequence as complete.
  def schedule_followup_for_thread(user_id, thread_id, followup_config)  — Schedule follow-ups for a newly sent thread.
  def cancel_followup_on_response(user_id, thread_id)  — Pause pending follow-up when broker responds.
  def resume_followup_if_silent(user_id, thread_id, silence_threshold_days)  — Resume follow-up sequence if broker went silent after responding.
  def _get_default_followup_message(index)  — Return default follow-up message based on sequence position.
```

### `email_automation/logging.py`
```
  def _ensure_log_tab_exists(sheets, spreadsheet_id)  — Ensure 'Log' tab exists and return its title.
  def _get_last_logged_message_id(sheets, spreadsheet_id, tab_title, thread_id)  — Get the last message ID that was logged for this thread.
  def write_message_order_test(uid, thread_id, sheet_id)  — 1) Read the first tab title.
```

### `email_automation/messaging.py`
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
  def update_thread_status(user_id, thread_id, status, reason)  — Update the status of a thread.
  def get_thread_status(user_id, thread_id)  — Get the current status of a thread.
  def save_thread_root(user_id, root_id, meta)  — Save or update thread root document. Returns True on success, False on failure.
  def save_message(user_id, thread_id, message_id, payload)  — Save message to thread. Returns True on success, False on failure.
  def _message_index_candidates(message_id)  — Return normalized msgIndex keys, with legacy raw-key fallback.
  def index_message_id(user_id, message_id, thread_id)  — Index message ID for O(1) lookup. Returns True on success, False on failure.
  def lookup_thread_by_message_id(user_id, message_id)  — Look up thread ID by message ID.
  def index_conversation_id(user_id, conversation_id, thread_id)  — Index conversation ID for fallback lookup. Returns True on success, False on failure.
  def lookup_thread_by_conversation_id(user_id, conversation_id)  — Look up thread ID by conversation ID (fallback).
  def lookup_thread_by_conversation_id_exhaustive(user_id, conversation_id)  — Exhaustive search for thread by conversation ID.
  def _get_thread_messages_chronological(uid, thread_id)  — Get all messages in thread in chronological order.
  def _message_body_content_and_preview(data)  — Normalize legacy string-body docs and current dict-body docs.
  def build_conversation_payload(uid, thread_id, limit, headers)  — Return last N messages in chronological order. Each item includes:
  def dump_thread_from_firestore(user_id, thread_id)  — Console dump of thread conversation in chronological order.
  def _processed_ref(user_id, key)  — Get reference to processed message document.
  def has_processed(user_id, key)  — Check if a message has already been processed.
  def mark_processed(user_id, key)  — Mark a message as processed.
  def _sync_ref(user_id)  — Get reference to sync document.
  def get_last_scan_iso(user_id)  — Get the last scan timestamp.
  def set_last_scan_iso(user_id, iso_str)  — Set the last scan timestamp.
  def get_handled_events(user_id, thread_id)  — Get all previously handled events for a thread.
  def is_event_handled(user_id, thread_id, event_key)  — Check if a specific event has already been handled for this thread.
  def mark_event_handled(user_id, thread_id, event_key, msg_id, notif_id)  — Record that an event has been handled for this thread.
  def build_event_key(event_type, event, thread_id)  — Build a unique event key for deduplication.
```

### `email_automation/notification_payloads.py`
```
  def _first_name(name_or_email)
  def build_wrong_contact_suggested_email()  — Build the InlineReplyComposer-compatible payload for wrong-contact referrals.
  def build_new_property_suggested_email()  — Build a fresh first-touch outreach for a broker-suggested replacement property.
  def should_skip_original_reply_for_new_property_referral()  — Avoid asking the original broker to connect us when they already gave a new contact.
  def sanitize_new_property_referral_response(proposal)  — Clear reply-back drafts when a new-property referral includes a different direct email.
```

### `email_automation/notifications.py`
```
  def write_notification(uid, client_id)  — Write notification and bump counters atomically.
  def add_client_notifications(uid, client_id, email, thread_id, applied_updates, notes, address)  — UPDATED: Writes one notification doc per applied field change.
```

### `email_automation/openai_usage.py`
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

### `email_automation/pending_responses.py`
> Pending Responses Queue
```
  def queue_pending_response(user_id, thread_id, msg_id, recipient, response_body, client_id, error)  — Queue a failed response for later retry.
  def get_pending_responses(user_id)  — Get all pending responses that haven't exceeded max attempts.
  def process_pending_responses(user_id, headers)  — Retry sending all pending responses.
  def clear_pending_response(user_id, thread_id)  — Remove a pending response (called after successful manual send or when no longer needed).
```

### `email_automation/processing.py`
```
  class RetryableProcessingError  — Raised when a message should remain unprocessed so the next scan can retry it.
  def _should_mark_processed_after_error(error)
  def _record_ai_processing_failure(user_id, client_id, thread_id, message_id, reason)
  def _should_skip_processing_for_terminal_thread(thread_status)
  def _close_reason_from_event(event)
  def _close_event_can_bypass_missing_fields(event)
  def _response_mentions_missing_fields(response_body, missing_fields)  — Detect whether an LLM response is actually asking for the missing fields.
  def _format_event_property(event)
  def _build_property_unavailable_comment(current_date, found_keyword, events)
  def _store_contact_optout(user_id, email, reason, thread_id)  — Store a contact's opt-out status in Firestore.
  def is_contact_opted_out(user_id, email)  — Check if a contact has opted out of communications.
  def _build_greeting(contact_name)  — Build a personalized greeting using the contact's first name, or generic 'Hi,' if no name.
  def send_reply_in_thread(user_id, headers, body, current_msg_id, recipient, thread_id)  — Send a reply to the current message being processed and index it for future replies
  def _find_client_id_by_email(uid, email)  — Search through all clients (active and archived) to find which one has a sheet
  def fetch_and_log_sheet_for_thread(uid, thread_id, counterparty_email)
  def process_inbox_message(user_id, headers, msg)  — ENHANCED: Process a single inbox message with full pipeline including events.
  def scan_inbox_against_index(user_id, headers, only_unread, top)  — Idempotent scan of inbox for replies with early exit on processed messages.
  def _match_message_to_thread(user_id, msg, headers)  — Try to match an inbox message to an existing thread.
  def _save_message_to_thread(user_id, thread_id, msg, headers)  — Save a message to a thread without full processing.
  def scan_sent_items_for_manual_replies(user_id, headers, top)  — Scan SentItems for Jill's manual replies to conversations we're tracking.
```

### `email_automation/service_providers.py`
> Service Providers Abstraction Layer
```
  def set_test_mode(enabled, mock_services)  — Enable or disable test mode with optional mock service implementations.
  def is_test_mode()  — Check if test mode is enabled.
  def get_provider(service_name)  — Get the appropriate provider for a service (real or mock).
  class EmailMessage  — Represents an email message.
  class EmailProvider  — Abstract interface for email operations.
    .list_messages(self, folder, filter_query, top)
    .get_message(self, message_id)
    .create_draft(self, subject, body, to_recipients, cc_recipients, headers)
    .send_draft(self, draft_id)
    .reply_to_message(self, message_id, body)
    .send_new_message(self, subject, body, to_recipients, cc_recipients)
    .get_attachments(self, message_id)
    .lookup_message_by_internet_id(self, internet_message_id)
  class RealEmailProvider  — Real implementation using Microsoft Graph API.
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
  class SheetsProvider  — Abstract interface for Google Sheets operations.
    .get_values(self, sheet_id, range_notation)
    .update_values(self, sheet_id, range_notation, values, value_input_option)
    .batch_update_values(self, sheet_id, data)
    .append_values(self, sheet_id, range_notation, values, value_input_option)
    .get_sheet_metadata(self, sheet_id)
  class RealSheetsProvider  — Real implementation using Google Sheets API.
    .__init__(self)
    .get_values(self, sheet_id, range_notation)
    .update_values(self, sheet_id, range_notation, values, value_input_option)
    .batch_update_values(self, sheet_id, data)
    .append_values(self, sheet_id, range_notation, values, value_input_option)
    .get_sheet_metadata(self, sheet_id)
  class FirestoreDocument  — Represents a Firestore document.
    .to_dict(self)
  class FirestoreProvider  — Abstract interface for Firestore operations.
    .get_document(self, path)
    .set_document(self, path, data, merge)
    .update_document(self, path, data)
    .delete_document(self, path)
    .query_collection(self, path, filters, order_by, limit)
    .list_subcollection(self, path)
  class RealFirestoreProvider  — Real implementation using Google Cloud Firestore.
    .__init__(self)
    ._path_to_ref(self, path)
    .get_document(self, path)
    .set_document(self, path, data, merge)
    .update_document(self, path, data)
    .delete_document(self, path)
    .query_collection(self, path, filters, order_by, limit)
    .list_subcollection(self, path)
  class DriveProvider  — Abstract interface for Google Drive operations.
    .list_files(self, query, page_size)
    .create_folder(self, name, parent_id)
    .upload_file(self, name, content, mime_type, parent_id)
    .set_public_permission(self, file_id)
  class RealDriveProvider  — Real implementation using Google Drive API.
    .__init__(self)
    .list_files(self, query, page_size)
    .create_folder(self, name, parent_id)
    .upload_file(self, name, content, mime_type, parent_id)
    .set_public_permission(self, file_id)
  class OpenAIProvider  — Abstract interface for OpenAI operations.
    .chat_completion(self, messages, model, temperature, response_format)
    .upload_file(self, content, filename, purpose)
  class RealOpenAIProvider  — Real implementation using OpenAI API.
    .__init__(self)
    .chat_completion(self, messages, model, temperature, response_format)
    .upload_file(self, content, filename, purpose)
  def generate_id(prefix)  — Generate a unique ID for testing.
  def generate_message_id()  — Generate a realistic internet message ID.
  def generate_conversation_id()  — Generate a conversation ID.
```

### `email_automation/sheet_operations.py`
```
  def sync_thread_row_numbers_after_move(user_id, src_row, divider_row, new_row, client_id)  — Update thread rowNumbers after a row is moved below the NON-VIABLE divider.
  def sync_thread_row_numbers_after_insert(user_id, insert_row, client_id)  — Update thread rowNumbers after a new sheet row is inserted.
  def _find_header_position(header, aliases)
  def _build_gross_rent_formula_for_row(header, rownum)
  def _apply_gross_rent_formula_for_row(sheets, sheet_id, tab_title, header, rownum)
  def _find_nonviable_divider_row(sheets, spreadsheet_id, tab_title)  — Return the divider row index if it exists, else None (no creation).
  def _is_row_below_nonviable(sheets, spreadsheet_id, tab_title, rownum)  — Stateless check: is this row visually below the 'NON-VIABLE' divider?
  def _ensure_divider_conditional_formatting(sheets, spreadsheet_id)  — Add a conditional formatting rule that paints ANY row red + bold white text
  def ensure_nonviable_divider(sheets, spreadsheet_id, tab_title)  — Ensure a NON-VIABLE divider row exists. Returns the divider row number.
  def move_row_below_divider(sheets, spreadsheet_id, tab_title, src_row, divider_row)  — Move src_row to immediately below the divider *and* keep the divider as the boundary.
  def insert_property_row_above_divider(sheets, sheet_id, tab_title, values_by_header)  — Insert a new property row one row above the divider (or at end if no divider).
  def _find_row_by_anchor(uid, thread_id, sheets, spreadsheet_id, tab_title, header, fallback_email)
```

### `email_automation/sheets.py`
```
  def _execute_with_retry(request, operation_name)  — Execute a Google Sheets API request with exponential backoff retry on rate limits.
  def _header_index_map(header)  — Normalize headers for exact match regardless of spacing/case.
  def _col_letter(n)  — 1-indexed column number -> A1 letter (1->A).
  def _get_first_tab_title(sheets, spreadsheet_id)
  def _read_header_row2(sheets, spreadsheet_id, tab_title)
  def _first_sheet_props(sheets, spreadsheet_id)
  def _guess_email_col_idx(header)
  def _find_row_by_email(sheets, spreadsheet_id, tab_title, header, email)  — Returns (row_number, row_values) where row_number is the 1-based sheet row.
  def _find_row_by_address_city(sheets, spreadsheet_id, tab_title, header, address, city)
  def format_sheet_columns_autosize_with_exceptions(spreadsheet_id, header)  — Auto-size all columns to the longest visible value + padding, with exceptions:
  def append_links_to_flyer_link_column(sheets, spreadsheet_id, header, rownum, links)  — Find/create Flyer / Link column and append unique links. Returns newly added links.
  def append_links_to_floorplan_column(sheets, spreadsheet_id, header, rownum, links)  — Find/create Floorplan column and append unique links. Returns newly added links.
  def is_floorplan_filename(filename)  — Detect if a PDF filename indicates it's a floorplan/building plan.
  def highlight_row(spreadsheet_id, rownum, color)  — Apply background color highlight to an entire row.
  def clear_row_highlight(spreadsheet_id, rownum)  — Remove background color from an entire row (set to white/default).
  def highlight_rows_batch(spreadsheet_id, rownums, color)  — Apply background color highlight to multiple rows in a single API call.
```

### `email_automation/utils.py`
```
  def _body_kind(script)
  def _normalize_email(s)
  def strip_email_quotes(text)  — Strip quoted content from email replies to get just the new message content.
  def is_valid_email(email)  — Validate email address format.
  def validate_recipient_emails(emails)  — Validate a list of email addresses.
  def _norm_txt(x)
  def b64url_id(message_id)  — Encode message ID for safe use as Firestore document key.
  def normalize_message_id(msg_id)  — Normalize message ID - strip whitespace and angle brackets.
  def parse_references_header(references)  — Parse References header into list of message IDs.
  def strip_html_tags(html)  — Strip HTML tags for preview.
  def clean_email_content(content)  — Clean email content for AI processing.
  def safe_preview(content, max_len)  — Create safe preview of email content.
  def exponential_backoff_request(func, max_retries)  — Execute request with exponential backoff on rate limits.
  def fetch_url_as_text(url)  — Try to fetch URL content and extract visible text using BeautifulSoup.
  def _sanitize_url(u)
  def _subject_to_address_city(subject)
  def _upload_logo_to_drive(image_filename)  — Get or upload image to Google Drive and return public direct image URL.
  def _image_to_base64(image_path)  — Convert image file to base64 data URI for email embedding.
  def get_signature_attachments()  — Get signature images as inline attachments for Microsoft Graph API.
  def convert_plain_text_signature_to_html(plain_text_signature)  — Converts a plain text email signature to HTML format.
  def get_email_footer(custom_signature, signature_mode)  — Returns HTML formatted email footer.
  def needs_signature_attachments(signature_mode)  — Check if the signature mode requires inline image attachments.
  def format_email_body_with_footer(body, custom_signature, signature_mode)  — Converts plain text email body to HTML and appends footer.
```

### `firebase_helpers.py`
```
  def download_token(api_key, output_file, user_id)
  def upload_token(api_key, input_file, user_id)
  def upload_excel(api_key, input_file, user_id)
```

### `main.py`
```
  def auto_cleanup_firestore(user_id)  — Automatically clean up Firestore collections if they exceed thresholds.
  def refresh_and_process_user(user_id)
```

### `noPopup_signin_emails_to_excel.py`
```
  def _save_cache()
  def send_weekly_email(to_addresses)
  def process_replies()
  def debug_api()  — Test basic API connectivity and permissions
```

### `resend_failed_responses.py`
> One-time script to resend failed response emails.
```
  def get_headers_for_user(user_id)  — Get auth headers for a specific user (same as main.py).
  def get_pending_responses(user_id, date_filter)  — Find sheetChangeLog entries with unsent responses from a specific date.
  def get_latest_inbound_message_id(user_id, thread_id)  — Get the most recent inbound message ID in a thread (to reply to).
  def resend_responses(user_id, dry_run, date_filter)  — Resend all pending responses for a user.
```

### `scheduler_runner.py`
```
  def _body_kind(script)
  def _helper_google_creds()
  def _sheets_client()
  def _get_sheet_id_or_fail(uid, client_id)
  def _get_first_tab_title(sheets, spreadsheet_id)
  def _read_header_row2(sheets, spreadsheet_id, tab_title)
  def _normalize_email(s)
  def _guess_email_col_idx(header)
  def _first_sheet_props(sheets, spreadsheet_id)
  def _approx_header_px(text)
  def _subject_to_address_city(subject)
  def _norm_txt(x)
  def _find_row_by_address_city(sheets, spreadsheet_id, tab_title, header, address, city)
  def build_new_property_email_payload(address, city, to_email, client_id, row_number)
  def _find_nonviable_divider_row(sheets, spreadsheet_id, tab_title)  — Return the divider row index if it exists, else None (no creation).
  def _is_row_below_nonviable(sheets, spreadsheet_id, tab_title, rownum)  — Stateless check: is this row visually below the 'NON-VIABLE' divider?
  def _ensure_divider_conditional_formatting(sheets, spreadsheet_id)
  def format_sheet_columns_autosize_with_exceptions(spreadsheet_id, header)  — Auto-size all columns to the longest visible value + padding, with exceptions:
  def _col_letter(n)  — 1-indexed column number -> A1 letter (1->A).
  def _header_index_map(header)  — Normalize headers for exact match regardless of spacing/case.
  def write_notification(uid, client_id)  — Write notification and bump counters atomically.
  def fetch_url_as_text(url)  — Try to fetch URL content and extract visible text using BeautifulSoup.
  def _ensure_divider_conditional_formatting(sheets, spreadsheet_id)  — Add a conditional formatting rule that paints ANY row red + bold white text
  def ensure_nonviable_divider(sheets, spreadsheet_id, tab_title)  — Ensure a NON-VIABLE divider row exists. Returns the divider row number.
  def move_row_below_divider(sheets, spreadsheet_id, tab_title, src_row, divider_row)  — Move src_row to immediately below the divider *and* keep the divider as the boundary.
  def insert_property_row_above_divider(sheets, spreadsheet_id, tab_title, values_by_header)  — Insert a new property row one row above the divider (or at end if no divider).
  def get_row_anchor(rowvals, header)  — Create a brief row anchor from property address and city.
  def check_missing_required_fields(rowvals, header)  — Check which required fields are missing from the row.
  def send_remaining_questions_email(uid, client_id, headers, recipient, missing_fields, thread_id, row_number, row_anchor)  — Send a remaining questions email in the same thread (idempotent).
  def send_closing_email(uid, client_id, headers, recipient, thread_id, row_number, row_anchor)  — Send polite closing email when all required fields are complete.
  def send_new_property_email(uid, client_id, headers, recipient, address, city, row_number)  — Send a new thread email for a new property suggestion.
  def _ensure_ai_meta_tab(sheets, spreadsheet_id)  — Ensure AI_META tab exists with proper headers.
  def _read_ai_meta_row(sheets, spreadsheet_id, rownum, column)  — Read AI_META record for specific row/column.
  def _append_ai_meta(sheets, spreadsheet_id, rownum, column, value, override)  — Append new AI_META record.
  def fetch_pdf_attachments(headers, graph_msg_id)  — Fetch PDF attachments from current message only.
  def ensure_drive_folder()  — Ensure Drive folder exists and return folder ID.
  def upload_pdf_to_drive(name, content, folder_id)  — Upload PDF to Drive and return webViewLink.
  def upload_pdf_user_data(filename, content)  — Upload PDF to OpenAI with purpose='user_data' and return file_id.
  def append_links_to_flyer_link_column(sheets, spreadsheet_id, header, rownum, links)  — Find/create Flyer / Link column and append unique links (no duplicates).
  def append_url_to_comments(sheets, spreadsheet_id, header, rownum, url)  — Always append URL to Listing Brokers Comments column.
  def apply_proposal_to_sheet(uid, client_id, sheet_id, header, rownum, current_rowvals, proposal)  — Applies proposal['updates'] to the sheet row with AI write guards.
  def _find_row_by_email(sheets, spreadsheet_id, tab_title, header, email)  — Returns (row_number, row_values) where row_number is the 1-based sheet row.
  def _find_row_by_anchor(uid, thread_id, sheets, spreadsheet_id, tab_title, header, fallback_email)
  def _ensure_log_tab_exists(sheets, spreadsheet_id)  — Ensure 'Log' tab exists and return its title.
  def _get_thread_messages_chronological(uid, thread_id)  — Get all messages in thread in chronological order.
  def _get_last_logged_message_id(sheets, spreadsheet_id, tab_title, thread_id)  — Get the last message ID that was logged for this thread.
  def write_message_order_test(uid, thread_id, sheet_id)  — 1) Read the first tab title.
  def build_conversation_payload(uid, thread_id, limit)  — Return last N messages in chronological order. Each item includes:
  def propose_sheet_updates(uid, client_id, email, sheet_id, header, rownum, rowvals, thread_id, file_manifest, url_texts)  — Uses OpenAI Responses API to propose sheet updates.
  def fetch_and_log_sheet_for_thread(uid, thread_id, counterparty_email)
  def b64url_id(message_id)  — Encode message ID for safe use as Firestore document key.
  def normalize_message_id(msg_id)  — Normalize message ID - keep as-is but strip whitespace.
  def parse_references_header(references)  — Parse References header into list of message IDs.
  def strip_html_tags(html)  — Strip HTML tags for preview.
  def safe_preview(content, max_len)  — Create safe preview of email content.
  def save_thread_root(user_id, root_id, meta)  — Save or update thread root document.
  def save_message(user_id, thread_id, message_id, payload)  — Save message to thread.
  def index_message_id(user_id, message_id, thread_id)  — Index message ID for O(1) lookup.
  def lookup_thread_by_message_id(user_id, message_id)  — Look up thread ID by message ID.
  def index_conversation_id(user_id, conversation_id, thread_id)  — Index conversation ID for fallback lookup.
  def lookup_thread_by_conversation_id(user_id, conversation_id)  — Look up thread ID by conversation ID (fallback).
  def exponential_backoff_request(func, max_retries)  — Execute request with exponential backoff on rate limits.
  def _subject_for_recipient(uid, client_id, recipient_email)  — Look up the row by email and return 'property address, city' as subject.
  def send_and_index_email(user_id, headers, script, recipients, client_id_or_none, row_number)  — Send email and immediately index it in Firestore for reply tracking.
  def add_client_notifications(uid, client_id, email, thread_id, applied_updates, notes)  — UPDATED: Writes one notification doc per applied field change.
  def _processed_ref(user_id, key)  — Get reference to processed message document.
  def has_processed(user_id, key)  — Check if a message has already been processed.
  def mark_processed(user_id, key)  — Mark a message as processed.
  def _sync_ref(user_id)  — Get reference to sync document.
  def get_last_scan_iso(user_id)  — Get the last scan timestamp.
  def set_last_scan_iso(user_id, iso_str)  — Set the last scan timestamp.
  def scan_inbox_against_index(user_id, headers, only_unread, top)  — Idempotent scan of inbox for replies with early exit on processed messages.
  def _sanitize_url(u)
  def process_inbox_message(user_id, headers, msg)  — ENHANCED: Process a single inbox message with full pipeline including events.
  def dump_thread_from_firestore(user_id, thread_id)  — Console dump of thread conversation in chronological order.
  def send_outboxes(user_id, headers)  — Modified to use send_and_index_email instead of send_email.
  def send_email(headers, script, emails, client_id)  — Legacy function - redirects to send_and_index_email
  def list_user_ids()
  def decode_token_payload(token)
  def send_weekly_email(headers, to_addresses)
  def process_replies(headers, user_id)
  def refresh_and_process_user(user_id)
```

### `scripts/analyze_overnight_campaign.py`
> Overnight Campaign Analysis Script
```
  def get_access_token()  — Download token from Firebase and get access token.
  def fetch_emails(token, folder, top)  — Fetch emails from a folder.
  def extract_property_from_subject(subject)  — Extract property address from email subject.
  def parse_datetime(dt_str)  — Parse ISO datetime string.
  def format_duration(seconds)  — Format seconds as human-readable duration.
  def analyze_campaign()
```

### `scripts/analyze_production.py`
> Production Analysis Script
```
  def init_firestore()  — Initialize Firestore client.
  def count_collection(db, path)  — Count documents in a collection (up to 1000).
  def analyze_user(db, uid)  — Analyze data for a single user.
  def main()
```

### `scripts/e2e_tools.py`
> E2E Test Monitoring Tools
```
  def _get_outlook_token()  — Get Outlook access token from Firebase cache.
  def _get_sheets_client()  — Get Google Sheets client.
  def check_outlook_sent(limit)  — Check sent emails in Outlook.
  def check_outlook_inbox(limit)  — Check inbox in Outlook.
  def get_email_body(message_id)  — Get full email body by ID.
  def check_firestore_all(client_id)  — Check all Firestore collections for the user.
  def check_threads_detail()  — Get detailed thread information.
  def check_notifications(client_id)  — Get all notifications for a client.
  def get_client_id()  — Get the active client ID.
  def check_sheet(sheet_id, include_values)  — Check Google Sheet state including formatting.
  def get_sheet_row_values(sheet_id, row_num)  — Get all values from a specific row.
  def trigger_scheduler()  — Manually trigger the GitHub Actions scheduler workflow.
  def get_workflow_runs(limit)  — List recent GitHub Actions workflow runs.
  def get_workflow_logs(run_id)  — Fetch logs from a specific workflow run (or latest).
  def save_workflow_logs(run_id, filename)  — Save full workflow logs to a file for later review.
  def run_local(save_log)  — Run main.py locally and capture output.
  def list_logs()  — List all saved log files.
  def review_logs()  — Review all logs for issues, errors, or cleanup needed.
  def snapshot_all(client_id, sheet_id)  — Take a full snapshot of everything.
```

### `scripts/production_reset.py`
> Production Reset Script
```
  def get_firestore_client()  — Initialize Firestore client.
  def delete_collection_batched(db, collection_ref, batch_size, dry_run)  — Delete all documents in a collection using batched deletes.
  def wipe_user_data(db, user_id, dry_run)  — Wipe all operational data for a specific user.
  def list_users(db)  — List all users in the database.
  def confirm_action(message)  — Ask for user confirmation.
  def main()
```

### `scripts/verify_production.py`
> Production Verification Script
```
  class CheckpointResult  — Result of a verification checkpoint.
  def checkpoint(name, description)  — Decorator to register a checkpoint function.
  class ProductionVerifier  — Runs verification checkpoints against production systems.
    .__init__(self, user_id)
    ._register_checkpoints(self)
    .list_checkpoints(self)
    .run_checkpoint(self, name)
    .run_all(self)
    ._get_user_id(self)
    .check_firestore_schema(self)
    .check_column_mapping(self)
    .check_column_config(self)
    .check_ai_extraction(self)
    .check_notifications(self)
  def main()
```

### `tests/__init__.py`
(no top-level functions/classes)

### `tests/analyze_results.py`
> Test Results Analyzer
```
  class AnalysisResult  — Analysis result for a test run.
  def load_results(results_path)  — Load summary and all results from a results directory.
  def analyze_field_extraction(results)  — Analyze which fields are being extracted and their accuracy.
  def analyze_events(results)  — Analyze event type distribution.
  def analyze_response_emails(results)  — Analyze response email patterns.
  def analyze_issues(results)  — Analyze common issues from failed tests.
  def analyze_latency_distribution(results)  — Analyze latency distribution in detail.
  def analyze_by_test_type(results)  — Analyze results grouped by test type.
  def compare_runs(results1, results2, summary1, summary2)  — Compare two test runs.
  def generate_html_report(analysis, output_path)  — Generate an HTML report from analysis results.
  def main()
```

### `tests/batch_runner.py`
> Batch Test Runner
```
  class TestResult  — Result of a single test.
  class BatchResults  — Results from a batch run.
  class BatchRunner  — Runs test cases and collects results.
    .__init__(self, suite_path, output_path)
    .load_test_cases(self, category)
    .build_rowvals(self, prop)
    .build_conversation_payload(self, prop, conversation)
    .run_single_test(self, test_case)
    .validate_result(self, result, expected, forbidden)
    .run_batch(self, test_cases, parallel)
    .save_results(self)
    .print_summary(self)
  def main()
```

### `tests/campaign_lifecycle_test.py`
> Campaign Lifecycle E2E Test Suite
```
  class PropertyStatus  — Status of a property in the campaign.
  class PropertyState  — Current state of a property in the simulated sheet.
  class CampaignState  — Full state of a simulated campaign.
  class BrokerResponseGenerator  — Generates realistic broker responses for different scenarios.
    .complete_info(prop)
    .partial_info_turn1(prop)
    .partial_info_turn2(prop)
    .property_unavailable(prop)
    .unavailable_with_alternative(prop)
    .new_property_different_contact(prop)
    .call_requested(prop)
    .tour_offered(prop)
    .identity_question(prop)
    .budget_question(prop)
    .negotiation_attempt(prop)
    .close_conversation(prop)
  class CampaignSimulator  — Simulates a full campaign lifecycle.
    .__init__(self)
    .add_property(self, address, city, contact, email, row)
    .build_rowvals(self, prop)
    .build_conversation_payload(self, prop)
    .process_broker_response(self, address, broker_response)
    .is_row_complete(self, prop)
    .check_campaign_complete(self)
    .get_campaign_summary(self)
    .simulate_user_response(self, address, user_message)
    .is_property_paused(self, address)
    .get_paused_properties(self)
    .get_active_properties(self)
    .get_resolved_properties(self)
  class CampaignScenario  — Defines a complete campaign test scenario.
  def run_campaign_scenario(scenario, verbose)  — Run a complete campaign scenario.
  def run_all_scenarios(verbose)  — Run all campaign scenarios.
  def main()
```

### `tests/conversation_generator.py`
> Conversation Generator
```
  class Scenario  — A test scenario definition.
  def load_properties()  — Load properties from Scrub Excel file.
  def generate_conversation(property_address, property_data, scenario)  — Generate a conversation file for a property and scenario.
  def save_conversation(conversation, scenario, output_dir)  — Save a conversation to a JSON file.
  def generate_all_conversations(output_dir)  — Generate conversations for all properties and scenarios.
  def list_scenarios()  — List all available scenarios.
  def main()
```

### `tests/e2e_full_simulation.py`
> Full E2E Simulation Tests
```
  def create_ai_response(updates, events, response_email)  — Helper to create AI response JSON.
  class TestResults  — Track test results.
    .__init__(self)
    .record(self, name, passed, error)
  def test_outbox_email_processing(harness, results)  — Test that outbox emails get sent correctly.
  def test_inbox_processing_complete_info(harness, results)  — Test processing a broker reply with complete info.
  def test_property_unavailable_flow(harness, results)  — Test property unavailable event handling.
  def test_new_property_suggestion(harness, results)  — Test new property suggestion event handling.
  def test_call_request_with_phone(harness, results)  — Test call request event with phone number provided.
  def test_contact_optout(harness, results)  — Test contact opt-out handling.
  def test_escalation_needs_user_input(harness, results)  — Test escalation when user input is needed.
  def test_multi_turn_conversation(harness, results)  — Test multi-turn conversation with cumulative data extraction.
  def test_sheet_update_writes(harness, results)  — Test that sheet updates are written correctly.
  def test_firestore_thread_indexing(harness, results)  — Test thread indexing for message lookup.
  def test_notification_writing(harness, results)  — Test notification writing to Firestore.
  def test_drive_file_upload(harness, results)  — Test Drive file upload simulation.
  def test_auto_reply_detection(harness, results)  — Test that auto-replies are detected and skipped.
  def test_batch_sheet_updates(harness, results)  — Test batch sheet update operations.
  def test_email_reply_in_thread(harness, results)  — Test replying to an email maintains thread continuity.
  def run_all_tests()  — Run all E2E simulation tests.
```

### `tests/e2e_harness.py`
> E2E Test Harness for EmailAutomation Backend
```
  class MockGraphAPI  — Mock Microsoft Graph API for email operations.
    .__init__(self)
    .inject_inbox_message(self, from_email, from_name, subject, body, in_reply_to, conversation_id)
    .mock_request(self, method, url)
  class MockOpenAI  — Mock OpenAI API for AI extraction operations.
    .__init__(self)
    .mock_completion(self)
    .set_expected_response(self, response)
  class E2EHarness  — E2E Test Harness for running real backend code with mocked external APIs.
    .__init__(self, user_id, mock_openai)
    .__enter__(self)
    .__exit__(self, exc_type, exc_val, exc_tb)
    ._start_mocks(self)
    ._stop_mocks(self)
    .inject_broker_reply(self, from_email, body, from_name, subject, in_reply_to, conversation_id)
    .get_sent_emails(self)
    .set_expected_ai_response(self, response)
    .process_outbox(self)
    .process_inbox(self)
    .process_single_message(self, message)
    .process_user(self)
  def create_test_thread(user_id, client_id, property_address, broker_email, internet_message_id, conversation_id)  — Create a thread in Firestore for testing reply matching.
  def main()  — CLI interface for E2E harness.
```

### `tests/e2e_helpers.py`
> E2E Test Helper Scripts
```
  def get_client()  — Get the active test client
  def check_threads(client_id)  — Check all threads for a client
  def check_outbox()  — Check outbox items
  def check_notifications(client_id)  — Check notifications for a client
  def clear_all()  — Clear all test data
  def status_report()  — Full status report
  def trigger_workflow()  — Trigger the GitHub Actions workflow
  def workflow_status()  — Check recent workflow runs
  def fetch_outlook_conversations()  — Fetch full email conversations from Outlook using the same method as main.py.
  def _clean_html(html_content)  — Strip HTML tags and clean up content
  def get_collection_counts()  — Get document counts for all relevant collections.
  def get_thread_details()  — Get detailed thread information.
  def get_outbox_details()  — Get outbox item details.
  def take_snapshot(label)  — Take a snapshot of current Firebase state.
  def snapshot_report()  — Generate a comparison report of all snapshots.
  def clear_snapshots()  — Clear all snapshots.
```

### `tests/e2e_monitor.py`
> E2E Campaign Monitoring Tools
```
  def get_db()
  def get_sheets_client()
  def get_outlook_token()
  def get_active_client_id()  — Get the first active client ID for this user.
  def get_client_sheet_id(client_id)  — Get the sheet ID for a client.
  def take_snapshot(phase)  — Take a snapshot of all Firebase state.
  def compare_snapshots()  — Compare before/after snapshots.
  def show_outlook()  — Show Outlook sent items and inbox for E2E test.
  def show_firebase()  — Show current Firebase state.
  def show_sheet()  — Show sheet state with highlighting.
  def watch_changes()  — Watch for Firebase changes in real-time.
  def main()
```

### `tests/e2e_server.py`
> E2E Test Server for EmailAutomation Backend
```
  class E2ERequestHandler  — HTTP request handler for E2E test operations.
    ._set_headers(self, status, content_type)
    ._read_body(self)
    .do_OPTIONS(self)
    .do_GET(self)
    .do_POST(self)
    .log_message(self, format)
    ._get_campaign_state(self)
    ._handle_campaign_grade(self, body)
    ._handle_simulate_response(self, body)
  def run_server(port)  — Run the E2E test server.
```

### `tests/e2e_test.py`
> End-to-End Integration Test Framework
```
  def reset_captures()  — Reset all captured outputs between tests.
  def load_scrub_file(filepath)  — Load the Scrub Excel file and return property data.
  def get_conversations_dir()  — Get the conversations directory path.
  def load_conversation(property_address, subdir)  — Load a conversation file for a property.
  def load_all_edge_case_conversations()  — Load all edge case conversation files.
  def list_available_conversations()  — List all available conversation files.
  def load_generated_conversations(category)  — Load generated conversation files from tests/conversations/generated/.
  class E2ETestResult  — Result of an E2E test.
  def run_e2e_test(property_address, property_data, conversation)  — Run a full E2E test for a single property.
  def display_result(result, header, verbose)  — Display a test result with full details.
  def main()
```

### `tests/e2e_test_harness.py`
> E2E Test Harness for Full Production Simulation
```
  class MockFirestoreClient  — Mock Firestore client that mimics google.cloud.firestore.Client interface.
    .__init__(self, provider)
    .collection(self, name)
    .transaction(self)
  class MockCollectionRef  — Mock Firestore collection reference.
    .__init__(self, provider, path)
    .document(self, doc_id)
    .where(self)
    .order_by(self, field, direction)
    .limit(self, count)
    .stream(self)
    .get(self)
  class MockDocumentRef  — Mock Firestore document reference.
    .__init__(self, provider, path)
    .collection(self, name)
    .get(self, transaction)
    .set(self, data, merge)
    .update(self, data)
    .delete(self)
    ._process_timestamps(self, data)
  class MockDocumentSnapshot  — Mock Firestore document snapshot.
    .__init__(self, doc_id, data, exists)
    .to_dict(self)
    .get(self, field)
  class MockQuery  — Mock Firestore query.
    .__init__(self, provider, path, filters, order_by, limit)
    .where(self)
    .order_by(self, field, direction)
    .limit(self, count)
    .stream(self)
    .get(self)
  class MockTransaction  — Mock Firestore transaction.
    .__init__(self, provider)
    .get(self, doc_ref)
    .set(self, doc_ref, data, merge)
    .update(self, doc_ref, data)
    .delete(self, doc_ref)
  class MockSheetsService  — Mock Google Sheets API service.
    .__init__(self, provider)
    .spreadsheets(self)
  class MockSpreadsheets  — Mock spreadsheets resource.
    .__init__(self, provider)
    .values(self)
    .get(self, spreadsheetId)
    .batchUpdate(self, spreadsheetId, body)
  class MockValues  — Mock spreadsheets.values resource.
    .__init__(self, provider)
    .get(self, spreadsheetId, range)
    .update(self, spreadsheetId, range, valueInputOption, body)
    .batchUpdate(self, spreadsheetId, body)
    .append(self, spreadsheetId, range, valueInputOption, body)
  class MockRequest  — Mock Google API request.
    .__init__(self, executor)
    .execute(self)
  class MockDriveService  — Mock Google Drive API service.
    .__init__(self, provider)
    .files(self)
    .permissions(self)
  class MockFiles  — Mock drive.files resource.
    .__init__(self, provider)
    .list(self, q, spaces, fields, pageSize)
    .create(self, body, media_body, fields)
  class MockPermissions  — Mock drive.permissions resource.
    .__init__(self, provider)
    .create(self, fileId, body)
  class TestHarness  — Context manager that patches all external services with mocks.
    .__init__(self, ai_response_generator)
    .__enter__(self)
    .__exit__(self, exc_type, exc_val, exc_tb)
    ._create_mock_openai_client(self)
    ._create_mock_requests(self)
    ._handle_graph_request(self, method, url, kwargs)
    .setup_sheet(self, sheet_id, headers, rows, sheet_name)
    .inject_email(self, from_address, from_name, subject, body, to_recipients, conversation_id)
    .setup_client(self, user_id, client_id, client_name, sheet_id, emails, criteria)
    .setup_user(self, user_id, signature)
    .setup_thread(self, user_id, thread_id, client_id, property_address, row_number, internet_message_id, conversation_id)
    .set_ai_response(self, generator)
    .set_ai_response_json(self, response)
    .get_sheet_cell(self, sheet_id, row, col, sheet_name)
    .get_sheet_row(self, sheet_id, row, sheet_name)
    .get_sent_emails(self)
    .get_notifications(self, user_id, client_id)
    .reset(self)
```

### `tests/e2e_test_suite.py`
> End-to-End Test Suite
```
  class TestCategory
  class ExpectedNotification
  class TestScenario
  class TestResult
  def build_conversation(scenario)  — Build conversation payload for the AI.
  def derive_notifications(updates, events, row_data, header)  — Derive what notifications would fire.
  def run_scenario(scenario, verbose)  — Run a single test scenario.
  def validate_result(scenario, result, row_data, header)  — Validate result against expectations.
  def run_all(category, verbose)  — Run all test scenarios.
  def test_column_detection()  — Test the column detection system.
```

### `tests/email_integration_test.py`
> Email Integration Test Suite
```
  def load_dotenv()  — Load environment variables from .env file.
  class EmailTestClient  — Client for testing email functionality with real Graph API.
    .__init__(self, user_id)
    ._get_default_user(self)
    ._authenticate(self)
    .raw_get(self, endpoint, params)
    .raw_post(self, endpoint, payload)
    .list_messages(self, folder, top, filter_query)
    .get_message_full(self, message_id)
    .send_test_email(self, to, subject, body)
    .reply_to_message(self, message_id, body)
    .inspect_conversation_ids(self, top)
    .test_filter_by_conversation_id(self, conversation_id)
  def cmd_list_users()  — List all available user accounts.
  def cmd_inspect_inbox(args)  — Inspect inbox messages and show raw structure.
  def cmd_inspect_ids(args)  — Analyze ID patterns across messages.
  def cmd_send_test(args)  — Send a test email.
  def cmd_test_filter(args)  — Test filtering by conversation ID.
  def cmd_full_message(args)  — Get full message details including headers.
  def cmd_conversation_test(args)  — Run a full conversation test flow.
  class GmailSender  — Send emails via Gmail SMTP to simulate broker replies.
    .__init__(self, email, app_password)
    .send_reply(self, to, subject, body, in_reply_to, references)
  def cmd_full_e2e_test(args)  — Run a complete end-to-end email conversation test.
  def cmd_send_gmail_reply(args)  — Send a simulated broker reply via Gmail.
  def main()
```

### `tests/frontend_integration_campaign.py`
> Frontend Integration Campaign Test
```
  class FrontendTestCase  — A test case for frontend interaction.
  def run_frontend_test(test_case)  — Run a single frontend integration test.
  def derive_notifications(result)  — Derive what notifications would be sent to frontend.
  def validate_test(test_case, result)  — Validate a test result against expectations.
  def run_all_frontend_tests(verbose)  — Run all frontend integration tests.
  def main()
```

### `tests/full_flow_test.py`
> Full Flow E2E Test - Simulates complete user interaction sequences
```
  class MockGraphAPI  — Mock Microsoft Graph API for email operations.
    .__init__(self)
    .inject_reply(self, from_email, body, subject)
    .mock_request(self, method, url)
  class FullFlowTest  — Full flow E2E test that simulates complete user interaction sequences.
    .__init__(self, user_id)
    .__enter__(self)
    .__exit__(self, exc_type, exc_val, exc_tb)
    ._start_mocks(self)
    ._stop_mocks(self)
    .create_client(self, name, properties)
    .launch_campaign(self, client_id, script, properties)
    .run_backend_send(self)
    .inject_broker_reply(self, from_email, body, subject)
    .run_backend_process(self)
    .get_notifications(self, client_id)
    .get_sheet_updates(self)
    .user_approves_new_property(self, notification_id, client_id)
    .cleanup(self)
  def run_scenario(scenario_name, test)  — Run a specific test scenario.
  def main()
```

### `tests/full_pipeline_test.py`
> Full Pipeline E2E Test
```
  class NotificationCapture  — Captures all notification calls for validation.
    .__init__(self)
    .reset(self)
    .write_notification(self, uid, client_id)
    .add_client_notifications(self, uid, client_id, email, thread_id, applied_updates, notes, address)
  def save_frontend_fixtures()  — Save fixtures for frontend testing.
  def build_conversation(messages)  — Build conversation payload.
  def run_pipeline_test(name, messages, initial_data, expect_notifications, expect_updates)  — Run full pipeline test and capture results.
  def test_complete_info()  — All fields provided - should fire sheet_update + row_completed.
  def test_partial_info()  — Only some fields - should fire sheet_update but not row_completed.
  def test_escalate_scheduling()  — Broker asks to schedule - should fire action_needed, NO response.
  def test_escalate_negotiation()  — Broker makes counteroffer - should fire action_needed.
  def test_escalate_client_question()  — Broker asks about client - should fire action_needed.
  def test_property_unavailable()  — Property no longer available - should fire property_unavailable.
  def test_new_property_suggestion()  — Broker suggests alternative - should fire new_property_suggestion.
  def test_call_requested()  — Broker wants to call - should fire action_needed.
  def test_contact_optout()  — Contact opts out - should fire contact_optout.
  def test_wrong_contact()  — Wrong contact - should fire wrong_contact.
  def test_property_issue()  — Property has issues - should fire property_issue.
  def test_mixed_info_and_escalation()  — Info provided but also asks question - should fire BOTH.
  def test_unavailable_with_alternative()  — Property gone but alternative offered - should fire BOTH.
  def generate_column_analysis_fixture()  — Generate fixture for ColumnMappingStep component.
  def run_all_tests()  — Run all pipeline tests.
```

### `tests/generate_test_suite.py`
> Test Suite Generator
```
  def generate_property(index)  — Generate a random test property.
  def generate_property_data()  — Generate random property specifications.
  class TestCase  — A single test case.
  def generate_complete_info_test(prop, index)  — Generate a complete_info test case.
  def generate_partial_info_test(prop, index)  — Generate a partial_info test case.
  def generate_unavailable_test(prop, index)  — Generate a property_unavailable test case.
  def generate_escalation_test(prop, escalation_type, index)  — Generate an escalation test case.
  def generate_edge_case_test(prop, edge_type, index)  — Generate an edge case test.
  def generate_new_property_test(prop, diff_contact, index)  — Generate a new_property test case.
  def generate_full_suite(output_dir, properties_per_category, templates_per_type)  — Generate the complete test suite.
  def main()
```

### `tests/integration_test.py`
> Integration Test Suite
```
  class MockFirestore  — Mock Firestore that tracks all operations.
    .__init__(self)
    .collection(self, name)
    .transaction(self)
    .record(self, op_type, path, data)
  class MockCollection
    .__init__(self, fs, path)
    .document(self, doc_id)
    .where(self)
  class MockDocument
    .__init__(self, fs, path)
    .collection(self, name)
    .get(self, transaction)
    .set(self, data, merge)
    .update(self, data)
    .delete(self)
  class MockSnapshot
    .__init__(self, path, data)
    .to_dict(self)
  class MockQuery
    .__init__(self, fs, path)
    .get(self)
    .limit(self, n)
    .order_by(self)
  class MockTransaction
    .__init__(self, fs)
    .get(self, doc_ref)
    .set(self, doc_ref, data, merge)
    .update(self, doc_ref, data)
  class MockSheetsClient  — Mock Google Sheets API client.
    .__init__(self)
    .spreadsheets(self)
    .values(self)
    .get(self, spreadsheetId, range)
    .update(self, spreadsheetId, range, body, valueInputOption)
    .batchUpdate(self, spreadsheetId, body)
    ._get_values(self, sheet_id, range_str)
    .execute(self)
    .setup_sheet(self, sheet_id, tab_name, data)
  class MockSheetsResponse
    .__init__(self, data)
    .execute(self)
  class TestResult
  def test_header_index_map()  — Test header index mapping with various edge cases.
  def test_apply_proposal_basic()  — Test applying a basic proposal to sheet.
  def test_notification_deduplication()  — Test that duplicate notifications are not created.
  def test_row_anchor_generation()  — Test row anchor generation for various inputs.
  def test_thread_message_indexing()  — Test thread and message index operations.
  def test_client_notification_counters()  — Test that notification counters are updated correctly.
  def test_contact_optout_storage()  — Test contact opt-out storage and lookup.
  def test_wrong_contact_handling()  — Test handling of wrong contact events.
  def test_property_issue_severity()  — Test property issue severity handling.
  def test_new_property_with_link()  — Test new property suggestion with link extraction.
  def test_sheet_formula_protection()  — Test that Gross Rent (formula column) is never written to.
  def test_escalation_no_response()  — Test that escalation events don't generate auto-responses.
  def test_multi_event_handling()  — Test handling multiple events in single response.
  def test_row_completion_detection()  — Test detection of all required fields complete.
  def run_all()  — Run all integration tests.
```

### `tests/mock_data.py`
> Mock data for testing the email automation system.
```
  def create_mock_sheet()  — Create a mock sheet structure matching real format.
  def get_row_by_email(sheet_data, email)  — Find row by email address.
  def get_header_index_map(header)  — Create normalized header -> index mapping (1-based).
  class ConversationScenario  — Represents a test scenario with conversation history and expected outcomes.
    .__init__(self, name, description, email, contact_name, property_address, city, messages, expected_updates, expected_events, expected_response_type, initial_row_data)
  def get_scenario_by_name(name)  — Get a specific scenario by name.
  def get_all_scenarios()  — Get all test scenarios.
```

### `tests/mock_services.py`
> Mock Service Implementations for E2E Testing
```
  class MockEmailProvider  — In-memory mock for Microsoft Graph email operations.
    .__init__(self)
    .inject_incoming_email(self, from_address, from_name, subject, body, to_recipients, conversation_id, in_reply_to)
    .list_messages(self, folder, filter_query, top)
    .get_message(self, message_id)
    .create_draft(self, subject, body, to_recipients, cc_recipients, headers)
    .send_draft(self, draft_id)
    .reply_to_message(self, message_id, body)
    .send_new_message(self, subject, body, to_recipients, cc_recipients)
    .get_attachments(self, message_id)
    .lookup_message_by_internet_id(self, internet_message_id)
    .get_conversation_thread(self, conversation_id)
    .clear(self)
  class MockSheetsProvider  — In-memory mock for Google Sheets operations.
    .__init__(self)
    .create_spreadsheet(self, sheet_id, sheet_name, headers, rows)
    ._parse_range(self, range_notation)
    .get_values(self, sheet_id, range_notation)
    .update_values(self, sheet_id, range_notation, values, value_input_option)
    .batch_update_values(self, sheet_id, data)
    .append_values(self, sheet_id, range_notation, values, value_input_option)
    .get_sheet_metadata(self, sheet_id)
    .get_cell(self, sheet_id, sheet_name, row, col)
    .clear(self)
  class MockFirestoreProvider  — In-memory mock for Firestore operations.
    .__init__(self)
    ._parse_path(self, path)
    .get_document(self, path)
    .set_document(self, path, data, merge)
    .update_document(self, path, data)
    .delete_document(self, path)
    .query_collection(self, path, filters, order_by, limit)
    .list_subcollection(self, path)
    .clear(self)
  class MockDriveProvider  — In-memory mock for Google Drive operations.
    .__init__(self)
    .list_files(self, query, page_size)
    .create_folder(self, name, parent_id)
    .upload_file(self, name, content, mime_type, parent_id)
    .set_public_permission(self, file_id)
    .clear(self)
  class MockOpenAIProvider  — Mock for OpenAI operations.
    .__init__(self, response_generator)
    .set_response(self, pattern, response)
    .chat_completion(self, messages, model, temperature, response_format)
    .upload_file(self, content, filename, purpose)
    .clear(self)
  def create_mock_services(openai_generator)  — Create a complete set of mock services for testing.
  def reset_all_mocks(mocks)  — Reset all mock services to initial state.
```

### `tests/multi_turn_live_test.py`
> Multi-Turn Live Email Integration Test
```
  def load_dotenv()
  class TurnResult
  class ScenarioResult
  class RunState
    .save(self)
    .load(cls)
    .clear(cls)
  class MultiTurnTestRunner
    .__init__(self, wait_seconds)
    ._init_clients(self)
    ._create_outbox_entry(self, scenario, client_id, row_index)
    ._run_pipeline(self)
    ._find_thread_for_scenario(self, scenario)
    ._get_thread_messages(self, thread_id)
    ._get_latest_internet_message_id(self, thread_id)
    ._get_notifications_since(self, client_id, since)
    ._find_notifications_for_thread(self, thread_id, since, property_address)
    ._count_sent_emails_for_thread(self, thread_id, direction)
    ._read_sheet_values(self, sheet_id, property_address, city, fallback_row)
    ._insert_test_row(self, sheet_id, address, city, contact_name, contact_email)
    ._delete_test_rows(self, sheet_id, addresses)
    ._find_real_client(self)
    ._assess_comments_quality(self, comments, sheet_values, conversation_bodies)
    ._execute_outreach(self, scenario, client_id, row_index)
    ._execute_broker_reply(self, scenario, turn, turn_index, thread_id, in_reply_to, client_id, sheet_id, row_index)
    ._execute_user_input(self, scenario, turn, turn_index, thread_id, in_reply_to, client_id, sheet_id, row_index)
    .run_scenario(self, scenario, state)
    .run(self, scenario_names, resume)
    ._generate_report(self, results, run_id)
    ._save_report(self, report, run_id)
  def cleanup_test_data()  — Remove test clients, threads, and notifications created by tests.
  def main()
```

### `tests/multi_turn_scenarios.py`
> Multi-Turn Live Email Test Scenarios
```
  class TurnAction
  class PropertyStatus
  class ExpectedNotification
  class TurnSpec  — Specification for a single turn in a multi-turn conversation.
  class MultiTurnScenario  — A complete multi-turn test scenario.
```

### `tests/outlook_helper.py`
> Outlook Inbox Helper
```
  def list_user_ids()  — List available user IDs from Firebase Storage.
  def get_access_token(user_id)  — Get access token for the specified user.
  def list_inbox(user_id, limit)  — List recent inbox messages.
  def get_attachments(msg_id, user_id, save_dir)  — Download attachments from a message.
  def show_users()  — List all available user IDs.
  def clear_mailbox(user_id, folders)  — Clear all messages from specified folders.
```

### `tests/persona_campaign_tester.py`
> Persona-Based Campaign Testing Framework
```
  class TestProperty  — A property in a test campaign.
  class PersonaFeedback  — Feedback from a testing persona.
  class Severity
  class TestingPersona  — Base class for testing personas.
    .run_tests(self, campaign_results)
    ._create_feedback(self, checks, details)
  class DataExtractionTester  — Validates AI field extraction accuracy.
    .run_tests(self, campaign_results)
  class UXNotificationTester  — Validates frontend notification correctness.
    .run_tests(self, campaign_results)
    ._derive_notifications(self, result)
  class ThreadingTester  — Validates conversation threading and state management.
    .run_tests(self, campaign_results)
  class EdgeCaseTester  — Validates handling of unusual/edge case scenarios.
    .run_tests(self, campaign_results)
  class CampaignLifecycleTester  — Validates complete campaign lifecycle.
    .run_tests(self, campaign_results)
    ._determine_state(self, result)
  def run_campaign_scenario(properties, scenario_name)  — Run a campaign scenario and return results.
  def run_all_personas(verbose)  — Run all campaigns through all personas and collect feedback.
  def main()
```

### `tests/production_test.py`
> Production End-to-End Test Suite
```
  def load_excel_data()  — Load the actual Excel file and extract headers + data.
  class TestResult
  class TestStats
    .__init__(self)
    .add(self, result)
    .summary(self)
  def build_conversation(prop, messages)  — Build conversation payload from messages.
  def run_test(name, prop, messages, expected_updates, expected_events, should_escalate, should_complete_row, initial_data, verbose)  — Run a single test case.
  def test_column_detection()  — Test column detection with the actual Excel headers.
  def test_complete_info_single_message()  — Broker provides all info in one message.
  def test_partial_info_needs_followup()  — Broker provides only some fields.
  def test_multi_turn_conversation()  — Information gathered across multiple turns.
  def test_property_unavailable()  — Property is no longer available.
  def test_unavailable_with_alternative()  — Property unavailable but broker suggests alternative.
  def test_new_property_suggestion()  — Broker proactively suggests additional property.
  def test_call_requested_with_phone()  — Broker requests a call and provides phone.
  def test_call_requested_no_phone()  — Broker requests call without providing number.
  def test_escalate_client_requirements()  — Broker asks about client's requirements - must escalate.
  def test_escalate_scheduling()  — Broker wants to schedule a tour - must escalate.
  def test_escalate_negotiation()  — Broker makes counteroffer - must escalate.
  def test_escalate_client_identity()  — Broker asks who the client is - must escalate.
  def test_escalate_legal_contract()  — Broker asks about LOI/contract - must escalate.
  def test_mixed_info_and_question()  — Broker provides info but also asks question requiring user input.
  def test_contact_optout()  — Contact says not interested / unsubscribe.
  def test_wrong_contact()  — Contact says they're not the right person.
  def test_property_issue()  — Broker mentions a problem with the property.
  def test_close_conversation()  — Natural conversation ending.
  def test_vague_response()  — Broker gives vague response with no concrete data.
  def test_notes_capture()  — Test that additional details are captured in notes.
  def test_formatting_validation()  — Verify values are formatted correctly (no $, SF, etc).
  def test_conflicting_info()  — Broker provides conflicting information - should use corrected value.
  def test_budget_question()  — Broker asks about budget - must escalate.
  def test_full_row_completion()  — Simulate filling an entire row through multiple conversations.
  def run_all_tests(quick, verbose)  — Run all production tests.
```

### `tests/quality_benchmark.py`
> Quality Benchmark Framework
```
  class QualityScore  — Quality scores for a single test.
  class BenchmarkCase  — A benchmark test case with gold standard expected output.
  def score_field_accuracy(actual_updates, expected_updates)  — Score how accurately fields were extracted.
  def score_field_completeness(actual_updates, expected_updates)  — Score how many available fields were captured.
  def score_notes_quality(actual_notes, expected_notes, forbidden)  — Score notes quality - contextual info without redundancy.
  def score_response_quality(response, expected_type, should_mention)  — Score response email quality.
  def score_event_accuracy(actual_events, expected_events)  — Score event detection accuracy.
  def run_benchmark(case, verbose)  — Run a single benchmark and return quality scores.
  def run_all_benchmarks(verbose)  — Run all benchmarks and return summary.
  def generate_html_report(data, output_path)  — Generate an HTML quality report.
  def main()
```

### `tests/results_manager.py`
> Results Manager - Save and load E2E test results
```
  def get_results_dir()  — Get the results directory path.
  def create_run_directory()  — Create a timestamped run directory.
  def get_file_hash(filepath)  — Get MD5 hash of a file for change detection.
  def create_manifest(run_dir, scrub_filepath, properties)  — Create manifest.json with run metadata.
  def save_result(run_dir, result, property_data, conversation, headers)  — Save a single test result to a JSON file.
  def save_summary(run_dir, results, manifest)  — Save summary.json with campaign-level results.
  def list_runs()  — List all previous test runs with their summaries.
  def load_run(run_name)  — Load all results from a specific run.
  def compare_runs(run1_name, run2_name)  — Compare results between two runs.
```

### `tests/run_full_test.py`
> Full End-to-End Test Runner
```
  class ScenarioResult  — Detailed result for a single scenario test.
  class FullTestRunner  — Runs complete end-to-end tests with OpenAI.
    .__init__(self, verbose)
    ._init_openai(self)
    ._get_row_data(self, scenario)
    ._build_conversation_payload(self, scenario)
    ._build_prompt(self, scenario, row_data, conversation)
    ._call_openai(self, prompt)
    ._validate_updates(self, expected, actual)
    ._validate_events(self, expected, actual)
    ._validate_response(self, response_email, expected_type, missing_fields)
    .test_scenario(self, scenario)
    .run_all(self, scenarios)
    .print_summary(self)
    .generate_detailed_report(self)
    .save_report(self, filename)
  def main()  — Main entry point.
```

### `tests/standalone_test.py`
> Standalone Test Runner
```
  def show_simulated_sheet_row(scenario_name, property_address, updates, header)  — Display a simulated sheet row showing before/after state.
  def show_full_email_response(response_email, contact_name)  — Display the full email response with proper formatting.
  class ExpectedNotification  — Expected notification definition.
  class TestScenario  — A test scenario definition.
  class TestResult  — Result of running a test.
  def derive_notifications(updates, events, row_data, header)  — Derive what notifications WOULD fire based on AI results.
  def build_conversation(scenario)  — Build a conversation payload in the format expected by propose_sheet_updates().
  def call_production_function(scenario)  — Call the production propose_sheet_updates() function.
  def validate_result(scenario, result, row_data)  — Validate result against expectations. Returns (passed, issues, warnings, derived_notifications).
  def run_test(scenario, verbose)  — Run a single test scenario.
  def run_all(verbose)  — Run all test scenarios.
  def save_report(results, filename)  — Save test results to JSON file.
```

### `tests/test_ai_integration.py`
> Integration tests that call the actual OpenAI API to validate extraction behavior.
```
  class AIIntegrationTester  — Tests the actual AI extraction logic.
    .__init__(self, verbose)
    ._build_prompt_from_scenario(self, scenario, row_data)
    .call_openai(self, prompt)
    .test_scenario(self, scenario)
    ._validate_result(self, result, scenario)
    .run_all(self, skip_auto_reply)
    .generate_report(self, filename)
  def quick_test()  — Quick test with just a couple scenarios.
```

### `tests/test_followup_terminal_state.py`
```
  class FakeThreadRef
    .__init__(self, data)
    .update(self, data)
    .get(self)
  class FakeThreadSnapshot
    .__init__(self, data)
    .to_dict(self)
  class FakeFirestore
    .__init__(self, thread_ref)
    .collection(self, _name)
    .document(self, _name)
    .update(self, data)
    .get(self)
  class FakeMessageDoc
    .__init__(self, data)
    .to_dict(self)
  class FollowupTerminalStateTests
    .test_max_reached_stops_thread_and_clears_highlight(self, clear_highlight)
    .test_reply_anchor_skips_synthetic_followup_history(self)
    .test_reply_anchor_returns_none_when_only_synthetic_history_exists(self)
    .test_auto_response_reschedules_paused_active_thread(self)
```

### `tests/test_harness.py`
> Test harness for email automation system.
```
  class TestResult  — Result of a single test scenario.
  class MockSheetState  — Simulates Google Sheets state for testing.
    .__init__(self, header, initial_rows)
    .get_row(self, row_num)
    .update_cell(self, row_num, column_name, value, is_ai)
    .check_human_override(self, row_num, column_name)
    .add_divider(self, row_num)
    .move_row_below_divider(self, src_row)
  def build_conversation_from_scenario(scenario)  — Build conversation payload from scenario messages.
  def simulate_ai_extraction(scenario, conversation)  — Simulate what the AI should extract from the conversation.
  def apply_proposal_simulation(sheet_state, row_num, proposal, check_guards)  — Simulate applying proposal to sheet with AI write guards.
  def check_missing_fields(row_data, header)  — Check which required fields are missing.
  def run_scenario(scenario, verbose)  — Run a single test scenario and return results.
  def run_all_scenarios(verbose)  — Run all test scenarios.
```

### `tests/test_message_history_dedupe.py`
```
  class FakeDocSnapshot
    .__init__(self, doc_id, data, collection)
    .to_dict(self)
  class FakeMessageDocRef
    .__init__(self, collection, doc_id)
    .set(self, payload, merge)
    .delete(self)
  class FakeMessagesCollection
    .__init__(self, existing)
    .document(self, doc_id)
    .stream(self)
  class FakeChain
    .__init__(self, messages)
    .collection(self, name)
    .document(self, _doc_id)
  class MessageHistoryDedupeTests
    .test_real_graph_outbound_replaces_matching_synthetic_dashboard_message(self)
    .test_real_graph_outbound_keeps_unrelated_synthetic_message(self)
    .test_real_graph_followup_replaces_synthetic_when_only_reply_prefix_differs(self)
```

### `tests/test_messaging_conversation_payload.py`
```
  class MessagingConversationPayloadTests
    .test_build_conversation_payload_tolerates_string_body_messages(self)
```

### `tests/test_openai_usage_tracking.py`
```
  class FakeDocRef
    .__init__(self, path)
    .collection(self, name)
    .set(self, payload, merge)
  class FakeCollectionRef
    .__init__(self, path)
    .document(self, doc_id)
    .add(self, payload)
  class FakeFirestore
    .__init__(self)
    .collection(self, name)
  class OpenAIUsageTrackingTests
    .test_estimate_openai_cost_uses_model_rates_and_cached_discount(self)
    .test_record_openai_usage_writes_event_and_user_client_rollups_without_prompt_text(self)
    .test_track_openai_usage_safely_swallows_metering_failures(self)
    .test_sheet_update_extraction_records_openai_usage_after_model_call(self)
```

### `tests/test_outbox_reply_recipient_routing.py`
```
  class FakeDocRef
    .__init__(self)
    .delete(self)
    .set(self)
  class FakeDoc
    .__init__(self, data)
    .to_dict(self)
  class FakeThreadDoc
    .__init__(self, data)
    .to_dict(self)
  class FakeThreadQuery
    .__init__(self, docs)
    .stream(self)
  class FakeThreadsCollection
    .__init__(self, docs)
    .where(self)
  class FakeUserDoc
    .__init__(self, docs)
    .collection(self, name)
    .assert_threads_collection(self, name)
  class FakeUsersCollection
    .__init__(self, docs)
    .document(self, _uid)
  class FakeFirestoreForThreads
    .__init__(self, docs)
    .collection(self, name)
  class OutboxReplyRecipientRoutingTests
    ._thread_reply_outbox(self, assigned_email)
    .test_thread_reply_with_different_assigned_email_sends_new_indexed_message(self, _highlight_row, _get_sheet_id_or_fail, send_and_index_email, send_outbox_as_reply, _get_reply_message_sender, _claim_outbox_item)
    .test_new_outreach_duplicate_check_is_scoped_to_client(self)
    .test_new_outreach_duplicate_check_blocks_same_client_match(self)
    .test_thread_reply_with_same_assigned_email_uses_graph_reply(self, _highlight_row, _get_sheet_id_or_fail, send_and_index_email, save_outbox_reply_message, send_outbox_as_reply, _get_reply_message_sender, _claim_outbox_item)
    .test_thread_reply_without_row_number_uses_thread_row_before_email_lookup(self, highlight_row, _get_sheet_id_or_fail, send_and_index_email, _save_outbox_reply_message, _send_outbox_as_reply, _get_reply_message_sender, _find_row_by_email, _get_thread_row_number, _claim_outbox_item)
```

### `tests/test_outbox_safety.py`
```
  class FakeDocRef
    .__init__(self)
    .delete(self)
    .set(self)
    .update(self, data)
  class FakeDoc
    .__init__(self, data, doc_id)
    .to_dict(self)
  class FakeFirestoreNode
    .__init__(self, root, path)
    .collection(self, name)
    .document(self, name)
    .delete(self)
    .set(self, data, merge)
  class FakeFirestore
    .__init__(self)
    .collection(self, name)
  class OutboxSafetyTests
    .test_cancel_requested_item_is_deleted_without_sending(self)
    .test_exact_or_threaded_dashboard_items_are_not_grouped_with_campaign_outreach(self)
    .test_successful_dashboard_outbox_finalizes_notification_and_thread_after_send(self)
```

### `tests/test_processing_completion_guards.py`
```
  class ProcessingCompletionGuardTests
    .test_closing_copy_does_not_satisfy_missing_field_response(self)
    .test_missing_field_response_must_reference_requested_detail(self)
    .test_all_info_close_event_requires_complete_required_fields(self)
    .test_terminal_non_info_close_reason_can_bypass_missing_fields(self)
    .test_deterministic_rent_fallback_extracts_asking_rent_not_nnn(self)
    .test_deterministic_rent_fallback_augments_blank_rent_cell(self)
```

### `tests/test_processing_retryability.py`
```
  class ProcessingRetryabilityTests
    .test_retryable_ai_failures_do_not_mark_messages_processed(self)
```

### `tests/test_sheet_operations_formula.py`
```
  class GrossRentFormulaTests
    .test_formula_handles_single_rent_values_and_rent_ranges(self)
```

### `tests/test_terminal_thread_processing.py`
```
  class TerminalThreadProcessingTests
    .test_completed_threads_are_terminal_for_inbox_processing(self)
    .test_stopped_threads_are_terminal_for_inbox_processing(self)
    .test_active_and_paused_threads_still_process(self)
```

### `tests/test_utils_email_validation.py`
```
  class EmailValidationTests
    .test_reserved_test_domains_are_not_valid_send_recipients(self)
    .test_validate_recipient_emails_separates_reserved_domains(self)
```

### `tests/test_wrong_contact_payload_shape.py`
```
  class WrongContactPayloadShapeTests
    .test_wrong_contact_suggested_email_uses_frontend_payload_shape(self)
    .test_wrong_contact_without_suggested_email_does_not_fall_back_to_original_contact(self)
    .test_new_property_suggested_email_reads_like_fresh_outreach(self)
    .test_new_property_referral_to_different_contact_skips_original_auto_reply(self)
    .test_new_property_referral_to_different_contact_suppresses_reply_draft(self)
    .test_new_property_same_contact_preserves_reply_draft(self)
```

### `tests/verify_firebase_msal_token.py`
> Test script to verify Firebase Functions-created MSAL tokens work with Python MSAL.
```
  def init_firebase()  — Initialize Firebase Admin SDK.
  def download_token_from_firebase()  — Download MSAL token cache from Firebase Storage.
  def test_token_format(cache_path)  — Verify the token cache has Python-compatible format.
  def test_acquire_token_silent(cache_path)  — Test that Python MSAL can use the token with acquire_token_silent.
  def test_graph_api_call(access_token)  — Test making an actual Microsoft Graph API call.
  def test_mailbox_access(access_token)  — Test accessing the mailbox (the actual use case).
  def main()
```
