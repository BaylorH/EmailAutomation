import json
import hashlib
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from google.cloud.firestore import SERVER_TIMESTAMP
from .clients import client, _sheets_client, _fs
from .messaging import build_conversation_payload
from .sheets import _header_index_map, _get_first_tab_title, _col_letter
from .app_config import REQUIRED_FIELDS_FOR_CLOSE
from .column_config import (
    CANONICAL_FIELDS,
    get_default_column_config,
    build_column_rules_prompt,
    get_required_fields_for_close,
    REQUIRED_FOR_CLOSE,
)

def get_row_anchor(rowvals: List[str], header: List[str]) -> str:
    """Create a brief row anchor from property address and city."""
    try:
        idx_map = _header_index_map(header)
        
        # Try to find address and city
        addr_keys = ["property address", "address", "street address", "property"]
        city_keys = ["city", "town", "municipality"]
        
        def _get_val(keys: List[str]) -> str:
            for k in keys:
                if k in idx_map:
                    i = idx_map[k] - 1  # 0-based for rowvals
                    if 0 <= i < len(rowvals):
                        v = (rowvals[i] or "").strip()
                        if v:
                            return v
            return ""
        
        addr = _get_val(addr_keys)
        city = _get_val(city_keys)
        
        if addr and city:
            return f"{addr}, {city}"
        elif addr:
            return addr
        elif city:
            return city
        else:
            return f"Row data incomplete"
    except Exception:
        return "Unknown property"

def check_missing_required_fields(rowvals: List[str], header: List[str], column_config: dict = None) -> List[str]:
    """
    Check which required fields are missing from the row.
    Uses dynamic column config if provided, otherwise falls back to defaults.
    """
    try:
        idx_map = _header_index_map(header)
        missing = []

        # Get required fields from config or use defaults
        if column_config:
            required_fields = get_required_fields_for_close(column_config)
        else:
            required_fields = REQUIRED_FIELDS_FOR_CLOSE

        for field in required_fields:
            key = field.strip().lower()
            if key in idx_map:
                i = idx_map[key] - 1  # 0-based
                if i >= len(rowvals) or not (rowvals[i] or "").strip():
                    missing.append(field)
            else:
                missing.append(field)  # Column doesn't exist

        return missing
    except Exception as e:
        print(f"❌ Failed to check missing fields: {e}")
        return REQUIRED_FIELDS_FOR_CLOSE  # Assume all missing on error

def _ensure_ai_meta_tab(sheets, spreadsheet_id: str) -> None:
    """Ensure AI_META tab exists with proper headers."""
    try:
        meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheet_names = [sheet["properties"]["title"] for sheet in meta["sheets"]]
        
        if "AI_META" in sheet_names:
            return
        
        # Create AI_META tab
        request = {
            "requests": [{
                "addSheet": {
                    "properties": {
                        "title": "AI_META",
                        "hidden": True  # Hidden tab
                    }
                }
            }]
        }
        sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=request).execute()
        
        # Add headers
        headers = ["rowNumber", "columnName", "last_ai_value", "last_ai_write_iso", "human_override"]
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range="AI_META!A1:E1",
            valueInputOption="RAW",
            body={"values": [headers]}
        ).execute()
        
        print("📋 Created 'AI_META' tab")
        
    except Exception as e:
        print(f"⚠️ Could not create AI_META tab: {e}")

def _read_ai_meta_row(sheets, spreadsheet_id: str, rownum: int, column: str) -> Optional[Dict]:
    """Read AI_META record for specific row/column."""
    try:
        _ensure_ai_meta_tab(sheets, spreadsheet_id)
        
        # Read all AI_META data
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="AI_META!A:E"
        ).execute()
        
        rows = resp.get("values", [])
        if len(rows) <= 1:  # Only header or empty
            return None
        
        # Find matching row
        for row in rows[1:]:  # Skip header
            if len(row) >= 2 and str(row[0]) == str(rownum) and row[1].lower() == column.lower():
                return {
                    "rowNumber": row[0],
                    "columnName": row[1],
                    "last_ai_value": row[2] if len(row) > 2 else None,
                    "last_ai_write_iso": row[3] if len(row) > 3 else None,
                    "human_override": row[4] if len(row) > 4 else False
                }
        
        return None
        
    except Exception as e:
        print(f"⚠️ Failed to read AI_META for row {rownum}, column {column}: {e}")
        return None

def _append_ai_meta(sheets, spreadsheet_id: str, rownum: int, column: str, value: str, override: bool = False):
    """Append new AI_META record."""
    try:
        _ensure_ai_meta_tab(sheets, spreadsheet_id)
        
        now_iso = datetime.now(timezone.utc).isoformat()
        
        row_data = [rownum, column, value, now_iso, override]
        
        sheets.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range="AI_META!A:E",
            valueInputOption="RAW",
            body={"values": [row_data]}
        ).execute()
        
    except Exception as e:
        print(f"⚠️ Failed to append AI_META record: {e}")

def _append_notes_to_comments(sheets, spreadsheet_id: str, tab_title: str, header: List[str], rownum: int, notes: str):
    """
    Append notes to the comments field (Listing Brokers Comments or Jill and Clients Comments).
    Prefers 'Listing Brokers Comments' if available, otherwise uses 'Jill and Clients Comments'.
    Appends to existing comments with a separator.
    """
    try:
        idx_map = _header_index_map(header)
        
        # Try to find comments column (prefer Listing Brokers Comments, fallback to Jill and Clients Comments)
        comments_col_idx = None
        comments_col_name = None
        
        # First try "Listing Brokers Comments"
        for key in ["listing brokers comments", "listing brokers comments "]:
            if key in idx_map:
                comments_col_idx = idx_map[key]
                comments_col_name = key
                break
        
        # Fallback to "Jill and Clients Comments"
        if not comments_col_idx:
            for key in ["jill and clients comments", "jill and clients comments "]:
                if key in idx_map:
                    comments_col_idx = idx_map[key]
                    comments_col_name = key
                    break
        
        if not comments_col_idx:
            print(f"⚠️ Could not find comments column to append notes")
            return
        
        # Get existing comments
        col_letter = _col_letter(comments_col_idx)
        existing_resp = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_title}!{col_letter}{rownum}"
        ).execute()
        
        existing_comments = ""
        if existing_resp.get("values") and len(existing_resp["values"]) > 0:
            existing_comments = (existing_resp["values"][0][0] or "").strip()
        
        # Combine existing and new notes
        if existing_comments:
            # Append with separator if there's existing content
            combined = f"{existing_comments} • {notes}"
        else:
            combined = notes
        
        # Update the comments cell
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_title}!{col_letter}{rownum}",
            valueInputOption="RAW",
            body={"values": [[combined]]}
        ).execute()
        
        print(f"📝 Appended notes to {comments_col_name} column: {notes[:100]}...")
        
    except Exception as e:
        print(f"⚠️ Failed to append notes to comments: {e}")

def apply_proposal_to_sheet(
    uid: str,
    client_id: str,
    sheet_id: str,
    header: List[str],
    rownum: int,
    current_rowvals: List[str],
    proposal: dict,
) -> dict:
    """
    Applies proposal['updates'] to the sheet row with AI write guards.
    Returns {"applied":[...], "skipped":[...]} items with old/new values.
    """
    try:
        sheets = _sheets_client()
        tab_title = _get_first_tab_title(sheets, sheet_id)
        
        _ensure_ai_meta_tab(sheets, sheet_id)

        if not proposal or not isinstance(proposal.get("updates"), list):
            return {"applied": [], "skipped": [{"reason":"no-updates"}]}

        idx_map = _header_index_map(header)

        data_payload = []
        applied, skipped = [], []

        for upd in proposal["updates"]:
            col_name = (upd.get("column") or "").strip()
            new_val  = "" if upd.get("value") is None else str(upd.get("value"))
            conf     = upd.get("confidence")
            reason   = upd.get("reason")

            key = col_name.strip().lower()
            if key not in idx_map:
                skipped.append({"column": col_name, "reason": "unknown header"})
                continue

            col_idx = idx_map[key]                     # 1-based
            col_letter = _col_letter(col_idx)          # A1
            rng = f"{tab_title}!{col_letter}{rownum}"

            old_val = current_rowvals[col_idx-1] if (col_idx-1) < len(current_rowvals) else ""

            # 1) no-op
            if (old_val or "") == (new_val or ""):
                skipped.append({"column": col_name, "reason": "no-change"})
                continue

            # Check AI_META for write guards
            meta = _read_ai_meta_row(sheets, sheet_id, rownum, col_name)

            # 2) prior AI write and human changed it
            if meta and meta.get("last_ai_value") is not None and str(old_val) != str(meta["last_ai_value"]):
                skipped.append({"column": col_name, "reason": "human-override"})
                continue

            # 3) no prior AI write but cell already has a value → check if we should still update
            if not meta and (old_val or "").strip() != "":
                # Allow updates in these cases:
                # a) AI has high confidence (≥ 0.8)
                # b) Existing value looks incomplete/placeholder (short, vague, or contains "TBD", "?", etc.)
                old_val_clean = (old_val or "").strip().lower()
                is_placeholder = any(marker in old_val_clean for marker in ["tbd", "?", "n/a", "na", "unknown", "pending"])
                is_short_incomplete = len(old_val_clean) <= 3 and old_val_clean.isdigit() == False
                has_high_confidence = conf and float(conf) >= 0.8
                
                if not (has_high_confidence or is_placeholder or is_short_incomplete):
                    skipped.append({"column": col_name, "reason": "existing-human-value", "oldValue": old_val, "confidence": conf})
                    continue

            # 4) otherwise proceed to write...
            data_payload.append({"range": rng, "values": [[new_val]]})
            applied.append({
                "column": col_name,
                "range": rng,
                "oldValue": old_val,
                "newValue": new_val,
                "confidence": conf,
                "reason": reason,
            })

        if not data_payload:
            return {"applied": [], "skipped": skipped}

        # Execute batch update
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "valueInputOption": "RAW",
                "data": data_payload
            }
        ).execute()

        # Update AI_META for each applied change
        for a in applied:
            _append_ai_meta(sheets, sheet_id, rownum, a["column"], a["newValue"], override=False)

        # Write notes to comments field if provided
        notes = proposal.get("notes")
        if notes and notes.strip():
            _append_notes_to_comments(sheets, sheet_id, tab_title, header, rownum, notes.strip())

        # Enhanced logging for debugging
        print(f"\n✅ Applied {len(applied)} updates, skipped {len(skipped)}")
        if applied:
            print("   Applied updates:")
            for a in applied:
                print(f"     • {a['column']}: '{a['oldValue']}' → '{a['newValue']}' (confidence: {a.get('confidence', 'N/A')})")
        if skipped:
            print("   Skipped updates:")
            for s in skipped:
                reason = s.get('reason', 'unknown')
                old_val = s.get('oldValue', '')
                conf = s.get('confidence', 'N/A')
                print(f"     • {s.get('column', 'Unknown')}: '{old_val}' (reason: {reason}, confidence: {conf})")

        return {"applied": applied, "skipped": skipped}

    except Exception as e:
        print(f"❌ Failed to apply proposal to sheet: {e}")
        return {"applied": [], "skipped": [{"reason": f"exception: {e}"}]}

def propose_sheet_updates(uid: str,
                          client_id: str,
                          email: str,
                          sheet_id: str,
                          header: List[str],
                          rownum: int,
                          rowvals: List[str],
                          thread_id: str,
                          pdf_manifest: List[dict] = None,   # [{"name": "...", "text": "...", "images": [...], "id": "..."}]
                          url_texts: List[dict] = None,
                          contact_name: str = None,
                          headers: dict = None,
                          conversation: List[dict] = None,   # Optional: pass conversation directly (for testing)
                          column_config: dict = None,        # Optional: dynamic column configuration
                          dry_run: bool = False) -> Optional[Dict]:
    """
    Uses OpenAI Responses API to propose sheet updates.
    - Grounds on the current row's (address, city) as TARGET PROPERTY.
    - Shows the model the attachment names so it can pick the right PDF.
    - Enforces strict event and document-selection rules.

    Args:
        conversation: Optional pre-built conversation payload. If provided, skips Firestore fetch.
                     Format: [{"direction": "inbound/outbound", "from": "...", "to": [...],
                              "subject": "...", "timestamp": "...", "content": "..."}]
        dry_run: If True, skips Firestore logging (useful for testing).
    """
    try:
        # Build conversation payload (chronological; latest last)
        # If conversation is provided directly (e.g., from tests), use it; otherwise fetch from Firestore
        if conversation is None:
            # Pass headers to fetch from Graph API (includes manual emails we didn't index)
            conversation = build_conversation_payload(uid, thread_id, limit=10, headers=headers)

        # ---- Rules sections ---------------------------------------------------
        # Use dynamic column config if provided, otherwise use defaults
        effective_config = column_config or get_default_column_config()
        COLUMN_RULES = build_column_rules_prompt(effective_config)

        DOC_SELECTION_RULES = """
DOCUMENT SELECTION & EXTRACTION (strict):
- Trust ATTACHMENTS (PDFs) over the email body when numbers conflict.
- Extract values ONLY for the TARGET PROPERTY. If a PDF shows multiple buildings/addresses, use the page/section
  that explicitly matches the TARGET PROPERTY (address/city). If no exact match, do not use that PDF for updates.
- If an attachment clearly refers to a different address, ignore it unless the LAST HUMAN message explicitly proposes
  it as an additional property (then you may emit a new_property event).
- If a brochure lists multiple options (e.g., Building C & D), pick the option that most clearly matches the TARGET
  PROPERTY/suite. If ambiguous, SKIP that field rather than guessing.

FIELD MINING HINTS:
- Rent/SF /Yr: look for "$14/SF NNN", "Asking: $15.00/sf/yr (NNN)".
- Ops Ex /SF: look for "NNN", "CAM", "Operating Expenses" as $/SF/YR. If only monthly is given, multiply by 12.
- Total SF: prefer the leasable area of the matched suite/building (not total park size).
- Ceiling Ht: "clear height", "clearance" → output just the number.
- Drive Ins: count numerical values for drive-in doors/loading doors.
- Docks: look for "4 dock doors", "6 loading docks", "8 dock positions", "12 dock doors", "dock doors: 6", "loading docks: 4", "dock bays: 8".
- Power: look for "200A", "480V", "100A 3-phase", "208V/120V", "400A service", "electrical service", "power capacity", "amperage", "voltage", "electrical load", "power supply", "electrical specs", "electrical requirements".
- NEVER write to "Gross Rent" - it's a formula column.
"""

        EVENT_RULES = """
EVENTS DETECTION (analyze ONLY the LAST HUMAN message for these events):

- "property_unavailable": ONLY when the CURRENT TARGET PROPERTY is explicitly stated as unavailable/leased/off-market/no longer available.

- "new_property": Emit when the LAST HUMAN message suggests or mentions a DIFFERENT property than the TARGET PROPERTY.
  • Look for phrases like: "we have another", "different location", "alternative property", "other space available"
  • Look for URLs pointing to different properties/listings
  • Look for property names, addresses, or locations mentioned that are NOT the TARGET PROPERTY
  • If mentioning "forestville", "centre", "woodmore" or other location names different from TARGET, this likely indicates new_property
  • Extract the property identifier (address, name, or URL) as the "address" field
  • Try to infer city/location from context or URL
  • IMPORTANT: If a DIFFERENT contact person is mentioned (e.g., "email Joe at joe@email.com", "contact Sarah", "reach out to Mike"):
    - Extract the contact NAME as "contactName" (e.g., "Joe", "Sarah", "Mike")
    - Extract their email as "email" field
  • The contactName is CRITICAL for personalized outreach - extract first name when available

- "call_requested": Only when someone explicitly asks for a call/phone conversation. Use this event (NOT needs_user_input) for phone call requests.

- "close_conversation": When conversation appears complete and the sender indicates they're done.

- "tour_requested": Emit when broker offers or requests a property tour/showing. This is DIFFERENT from needs_user_input.
  • Look for: "schedule a tour", "would you like to see it", "happy to show you", "can arrange a tour",
    "want to come by", "stop by and take a look", "walk through the property", "showing available"
  • The user needs to decide whether to schedule the tour, so DO NOT auto-respond
  • Instead, GENERATE a suggested response email in the "suggestedEmail" field that the user can approve/edit
  • Example suggestedEmail: "Hi [broker], Thank you for the offer! I'd like to schedule a tour. Are you available [suggest a few time options]? Looking forward to seeing the space."
  • Include "question" field with the specific tour offer/request
  • Set response_email to null (user will send the approved email)

- "needs_user_input": CRITICAL - Emit when the AI CANNOT or SHOULD NOT respond automatically. Use this when:
  • Client asks questions about the user's requirements (size needed, budget, timeline, move-in date, industry)
  • Negotiation attempts (counteroffers, "would you consider X price", lease term negotiations)
  • Questions about client identity ("who is your client?", "what company?")
  • Legal/contract questions ("when can you sign?", "send LOI", "what terms do you want?")
  • Confusing or unclear messages where appropriate response is uncertain
  • Messages requiring decisions the AI shouldn't make on behalf of the user
  • NOTE: Tour/meeting requests should use "tour_requested" event instead

  Include "reason" field explaining WHY user input is needed:
  • "client_question" - broker asking about client's requirements
  • "negotiation" - price or term negotiation
  • "confidential" - asking for client identity/info
  • "legal_contract" - contract/LOI/lease questions
  • "unclear" - message is confusing or unclear

- "contact_optout": Emit when the contact explicitly indicates they don't want further communication.
  • Look for: "not interested", "no thanks", "please stop", "unsubscribe", "remove me from your list",
    "don't contact me", "stop emailing", "opt out", "take me off your list", "no longer interested"
  • Also detect professional refusals: "I don't work with tenant rep brokers", "we only deal direct with tenants",
    "we don't work with buyer's agents", "not taking inquiries"
  • Include "reason" field:
    - "not_interested" - general disinterest
    - "unsubscribe" - explicit removal request
    - "do_not_contact" - firm request to stop contact
    - "no_tenant_reps" - policy against working with tenant reps
    - "direct_only" - only deals directly with tenants
    - "hostile" - rude or aggressive response

- "wrong_contact": Emit when the message indicates this person is NOT the right contact for this property.
  • Look for: "I don't handle that property", "wrong person", "contact [name] instead", "no longer with [company]",
    "I'm not the leasing agent", "forwarding to", "you should reach out to", "try [name/email]"
  • Extract suggested contact info if provided:
    - "suggestedContact" - name of correct person
    - "suggestedEmail" - email if provided
    - "suggestedPhone" - phone if provided
  • Include "reason" field:
    - "no_longer_handles" - used to handle but doesn't anymore
    - "wrong_person" - never handled this property
    - "forwarded" - forwarding to correct person
    - "left_company" - no longer with the company

- "property_issue": CRITICAL - Emit when the broker mentions ANY negative condition, problem, or concern about the property.
  • Physical condition issues: "smells bad", "odor", "mold", "water damage", "roof leak", "foundation issues",
    "structural problems", "pest issues", "rat problem", "contamination", "asbestos", "needs repairs"
  • Environmental concerns: "flood zone", "environmental issues", "soil contamination", "hazmat", "UST"
  • Building problems: "HVAC not working", "electrical issues", "plumbing problems", "fire damage"
  • Site issues: "drainage problems", "parking issues", "access problems", "security concerns"
  • Compliance issues: "code violations", "permit issues", "zoning problems", "ADA non-compliant"
  • Landlord/tenant issues: "difficult landlord", "tenant disputes", "eviction in progress"
  • Include "issue" field with the specific problem mentioned
  • Include "severity" field: "critical" (health/safety), "major" (significant repair), "minor" (cosmetic/inconvenience)
  • This event is IMPORTANT because it flags properties that may need additional consideration before proceeding

CRITICAL EXAMPLES:
- "Below is the only current space we have" + URL = new_property event
- "Here's an alternative location" = new_property event
- "This property isn't available" = property_unavailable event
- "Can you call me?" = call_requested event
- "What size space does your client need?" = needs_user_input (reason: client_question)
- "Can you tour Tuesday at 2pm?" = tour_requested event (with suggestedEmail)
- "Would you like to see the space?" = tour_requested event (with suggestedEmail)
- "Would you consider $7/SF instead?" = needs_user_input (reason: negotiation)
- "Who is your client?" = needs_user_input (reason: confidential)
- "When can you sign the lease?" = needs_user_input (reason: legal_contract)
- "Not interested, thanks" = contact_optout (reason: not_interested)
- "Please remove me from your mailing list" = contact_optout (reason: unsubscribe)
- "We don't work with tenant reps" = contact_optout (reason: no_tenant_reps)
- "I don't handle that property anymore, contact John Smith" = wrong_contact (reason: no_longer_handles)
- "Wrong person - try sarah@broker.com" = wrong_contact (reason: wrong_person)
- "The property smells bad" = property_issue (issue: "odor problem", severity: major)
- "There's some water damage in the warehouse" = property_issue (issue: "water damage", severity: major)
- "FYI there was a small roof leak last year but it's been fixed" = property_issue (issue: "previous roof leak", severity: minor)
- "The building has asbestos that needs abatement" = property_issue (issue: "asbestos", severity: critical)
- "The HVAC system is old and needs replacement" = property_issue (issue: "HVAC needs replacement", severity: major)
"""

        NOTES_RULES = """
NOTES FIELD (IMPORTANT - always look for these):
The "notes" field captures valuable information that doesn't fit in the standard columns. This helps the user understand the property without re-reading emails.

ALWAYS capture these when mentioned:
- Availability timing: "available immediately", "available March 1st", "60 days notice"
- Lease terms: "flexible on term", "3-5 year lease preferred", "month-to-month available"
- Zoning: "zoned M-1", "heavy industrial", "light manufacturing"
- Special features: "fenced yard", "rail spur", "sprinklered", "ESFR", "food grade"
- Parking: "10 trailer spots", "employee parking for 50"
- Landlord notes: "owner motivated", "firm on price", "willing to do TI"
- Building details not in columns: "built 2020", "renovated 2023", "tilt-up construction"
- Location context: "near I-20", "airport adjacent", "in industrial park"
- Divisibility: "can subdivide to 5,000 SF", "must take full space"
- HVAC/Climate: "climate controlled", "AC in office only"
- Office space: "1,500 SF office buildout", "includes 2 private offices"

FORMAT: Use terse fragments separated by " • "
EXAMPLE: "available immediately • 3-5 yr preferred • fenced yard • near I-20 • can subdivide"

IMPORTANT: If the broker mentions ANY of these details, capture them. Don't leave notes empty if useful info exists.
"""

        RESPONSE_EMAIL_RULES = """
RESPONSE EMAIL GENERATION:
You must generate a professional, contextual response email based on the conversation history and current situation.

CRITICAL: The email footer is automatically appended and includes:
- "Best," (closing)
- Full signature with logo, contact info, and LinkedIn icon

Therefore, your response email body should:
- Start with a greeting (e.g., "Hi,")
- Contain the main message content
- End with your content - DO NOT include "Best," or "Best regards" or any closing - the footer will add "Best," automatically
- DO NOT include any signature, contact information, or footer content

GUIDELINES:
- Write in a professional, friendly tone matching Jill Ames' communication style
- Vary your greetings naturally - don't always use the same format
- GREETING VARIATION RULES:
  * When to use the contact name: Use it in longer messages, first messages in a thread, or when acknowledging specific information they provided
  * When to omit the name: Use in brief requests, quick follow-ups, or when the message is very short
  * Rotate greeting styles when using the name: Mix between "Hi [Name],", "Thanks [Name],", "[Name],", "Hi [Name] -" (with dash)
  * Rotate greeting styles when NOT using the name: Mix between "Hi,", "Thanks,", "Thank you,"
  * Examples of good variation:
    - "Hi Scott, Thank you for confirming..."
    - "Thanks Scott, I received..."
    - "Hi, Could you please provide..."
    - "Scott, To complete the property details..."
    - "Thanks, I appreciate the update..."
- Reference specific details from the conversation to show you're paying attention
- Avoid repeating the same message - vary your wording based on conversation context
- Keep responses concise and to the point - short and direct
- If missing fields are identified, politely request them in a natural way
- If all required information is complete, acknowledge and close appropriately
- If property is unavailable, acknowledge and ask for alternatives if appropriate
- If new property is suggested, thank them and indicate you'll review it
- DO NOT use phrases like "Looking forward to your response" or "Looking forward to hearing from you" - instead, simply end with "Thanks" or similar brief closing

SCENARIOS:
1. Missing required fields: Thank them for the information, then list the missing fields naturally in a bulleted format.
   EXAMPLE FORMAT:
   "Thank you for confirming the number of drive-in doors. To complete the property details, could you please provide:

   - Total SF
   - Ops Ex /SF
   - Docks
   - Ceiling Ht
   - Power

   Thanks."

   IMPORTANT:
   - NEVER request "Rent/SF /Yr" - this field should never be asked for
   - NEVER request "Gross Rent" - this is a formula column that calculates automatically
   - Keep it short and concise
   - End with a simple "Thanks" - do NOT use "Looking forward to your response" or similar phrases
   
2. All fields complete: Thank them and indicate you have everything needed
3. Property unavailable + new property suggested: Thank them for both pieces of information
4. Property unavailable (no alternative): Thank them and ask if they have other properties
5. Call requested:
   - If phone number is provided in the message: DO NOT generate a response_email (system will handle notification only)
   - If no phone number: Keep response brief - just ask for their phone number
   - Keep it short and direct, avoid wordy responses
6. General acknowledgment: Thank them for their message and respond appropriately to their content
7. Needs user input (CRITICAL):
   - If emitting "needs_user_input" event, set response_email to null or empty string
   - The system will notify the user and let THEM respond
   - DO NOT attempt to answer questions about client requirements, budgets, or timelines
   - DO NOT commit to tours, meetings, or schedules
   - DO NOT engage in negotiation
   - DO NOT reveal client information
8. Tour requested (CRITICAL):
   - If emitting "tour_requested" event, set response_email to null
   - The user must approve/edit the suggested email before it's sent
   - DO NOT auto-respond to tour offers - the user decides whether to schedule

IMPORTANT: The response should feel natural and conversational, not robotic or templated. Reference specific details from their message when possible. Remember: NO closing/signature - just end with your content, the footer will add "Best," and signature automatically.
"""

        # ---- Build prompt -----------------------------------------------------
        target_anchor = get_row_anchor(rowvals, header)  # e.g., "1 Randolph Ct, Evans"

        # Check missing required fields to inform response email generation
        missing_fields = check_missing_required_fields(rowvals, header, effective_config)
        
        # Build contact name context (provided as info for optional use, not required)
        contact_context = ""
        if contact_name:
            contact_context = f"\nCONTACT NAME (optional - use contextually, not in every message): {contact_name}"
        
        prompt_parts = [f"""
You are analyzing a conversation thread to suggest updates to ONE Google Sheet row, detect key events, and generate an appropriate response email.

TARGET PROPERTY (canonical identity for matching): {target_anchor}
{contact_context}

{COLUMN_RULES}
{DOC_SELECTION_RULES}
{EVENT_RULES}
{NOTES_RULES}
{RESPONSE_EMAIL_RULES}

SHEET HEADER (row 2):
{json.dumps(header)}

CURRENT ROW VALUES (row {rownum}):
{json.dumps(rowvals)}

MISSING REQUIRED FIELDS (if any):
{json.dumps(missing_fields)}

CONVERSATION HISTORY (latest last):
{json.dumps(conversation, indent=2)}
""".rstrip()]

        # PDF attachments - include extracted text directly in prompt
        if pdf_manifest:
            prompt_parts.append("\n\n=== PDF ATTACHMENTS ===")
            for pdf in pdf_manifest:
                name = pdf.get("name") or "<unnamed.pdf>"
                text = pdf.get("text") or ""
                method = pdf.get("method", "unknown")

                prompt_parts.append(f"\n--- PDF: {name} (extraction method: {method}) ---")
                if text:
                    # Include extracted text (truncate if too long)
                    if len(text) > 8000:
                        prompt_parts.append(text[:8000] + "\n... [text truncated] ...")
                    else:
                        prompt_parts.append(text)
                else:
                    prompt_parts.append("[No text extracted - see images below if available]")

        # URL content (already fetched)
        if url_texts:
            prompt_parts.append("\nURL CONTENT FETCHED:")
            for url_info in url_texts:
                prompt_parts.append(f"\nURL: {url_info['url']}")
                prompt_parts.append(f"Content: {url_info['text'][:1000]}...")

        # Output contract
        prompt_parts.append("""
Be conservative: only suggest changes you can cite from the text, attachments, or fetched URLs.

OUTPUT ONLY valid JSON in this exact format:
{
  "updates": [
    {
      "column": "<exact header name>",
      "value": "<new value as string>",
      "confidence": 0.85,
      "reason": "<brief explanation why this update is suggested>"
    }
  ],
  "events": [
    {
      "type": "call_requested | property_unavailable | new_property | close_conversation | needs_user_input | contact_optout | wrong_contact | property_issue | tour_requested",
      "address": "<for new_property: extract property name, address, or identifier>",
      "city": "<for new_property: infer city/location if possible>",
      "email": "<for new_property if different email/contact needed>",
      "contactName": "<for new_property: first name of the new contact if mentioned, e.g., 'Joe' from 'email Joe at joe@email.com'>",
      "link": "<for new_property: include URL if mentioned>",
      "notes": "<for new_property: additional context about the property>",
      "reason": "<for needs_user_input: client_question | scheduling | negotiation | confidential | legal_contract | unclear> OR <for contact_optout: not_interested | unsubscribe | do_not_contact | no_tenant_reps | direct_only | hostile> OR <for wrong_contact: no_longer_handles | wrong_person | forwarded | left_company>",
      "question": "<for needs_user_input: the specific question/request that needs user attention>",
      "suggestedContact": "<for wrong_contact: name of correct person to contact>",
      "suggestedEmail": "<for wrong_contact: email of correct person if provided>",
      "suggestedPhone": "<for wrong_contact: phone of correct person if provided>",
      "issue": "<for property_issue: specific description of the problem/concern>",
      "severity": "<for property_issue: critical | major | minor>"
    }
  ],
  "response_email": "<Generate a professional response email body (plain text only). Start with greeting (e.g., 'Hi,'), include main message content, and end with your content - DO NOT include 'Best,' or any closing/signature as the footer will add 'Best,' and full signature automatically. Should be contextual to the conversation, reference specific details when possible, and vary wording to avoid repetition. SET TO NULL when: (1) call_requested with phone number provided, (2) needs_user_input event detected, (3) contact_optout event detected, (4) wrong_contact event detected. The system will notify the user instead of auto-responding.>",
  "notes": "<IMPORTANT: Capture useful details not in columns - availability timing, lease terms, zoning, special features, parking, landlord notes, building age, location context, divisibility, HVAC, office space. Format: terse fragments separated by ' • '. Example: 'available immediately • 3-5 yr preferred • fenced yard'. Leave empty ONLY if conversation has no such details.>"
}
""")

        prompt = "".join(prompt_parts)

        # ---- Prepare inputs (images for vision, files as fallback, then text) --------------------------
        input_content = []

        # Add PDF page images for vision processing (scanned PDFs, complex layouts)
        if pdf_manifest:
            for pdf in pdf_manifest:
                images = pdf.get("images") or []
                name = pdf.get("name", "PDF")

                # Add images for vision (pages with little extractable text)
                for i, img_b64 in enumerate(images[:3]):  # Max 3 pages per PDF
                    input_content.append({
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{img_b64}"
                    })
                    print(f"📷 Added page {i+1} image from {name} for vision analysis")

                # Add file_id as fallback if we have it and extraction was poor
                if pdf.get("id") and pdf.get("method") in ("openai_upload", "openai_upload+images", "failed"):
                    input_content.append({"type": "input_file", "file_id": pdf["id"]})

        input_content.append({"type": "input_text", "text": prompt})

        # ---- Call OpenAI (low temperature for determinism) --------------------
        response = client.responses.create(
            model="gpt-5.2",  # GPT-5.2 Thinking for complex extraction
            input=[{"role": "user", "content": input_content}],
            temperature=0.1
        )

        raw_response = (response.output_text or "").strip()

        # ---- Parse JSON safely ------------------------------------------------
        try:
            # Strip code fences if present
            if raw_response.startswith("```"):
                lines = raw_response.split("\n")
                json_lines = []
                in_json = False
                for line in lines:
                    if line.strip().startswith("```"):
                        in_json = not in_json
                        continue
                    if in_json:
                        json_lines.append(line)
                raw_response = "\n".join(json_lines)

            proposal = json.loads(raw_response)
        except json.JSONDecodeError as e:
            print(f"❌ Failed to parse OpenAI JSON response: {e}")
            print(f"Raw response: {raw_response}")
            return None

        if not isinstance(proposal, dict):
            print(f"❌ Invalid proposal structure: {proposal}")
            return None

        proposal.setdefault("updates", [])
        proposal.setdefault("events", [])
        proposal.setdefault("response_email", None)  # LLM-generated response email

        # ---- Log + store in sheetChangeLog -----------------------------------
        print(f"\n🤖 OpenAI Proposal for {client_id}__{email}:")
        print(json.dumps(proposal, indent=2))
        
        # Log what updates were suggested for debugging
        if proposal.get("updates"):
            print(f"\n📝 Proposed {len(proposal['updates'])} field updates:")
            for upd in proposal["updates"]:
                print(f"   • {upd.get('column', 'Unknown')}: '{upd.get('value', '')}' (confidence: {upd.get('confidence', 'N/A')})")
        else:
            print(f"\n📝 No field updates proposed")
        
        # Log response email if generated
        if proposal.get("response_email"):
            print(f"\n📧 LLM-generated response email:")
            print(f"   {proposal['response_email'][:200]}..." if len(proposal['response_email']) > 200 else f"   {proposal['response_email']}")
        else:
            print(f"\n📧 No LLM-generated response email (will use template fallback)")

        # Log to Firestore (skip in dry_run mode for testing)
        if not dry_run:
            now_utc = datetime.now(timezone.utc)
            log_doc_id = f"{thread_id}__{now_utc.isoformat().replace(':','-').replace('.','-').replace('+00:00','Z')}"

            proposal_hash = hashlib.sha256(
                json.dumps(proposal, sort_keys=True).encode('utf-8')
            ).hexdigest()[:16]

            _fs.collection("users").document(uid).collection("sheetChangeLog").document(log_doc_id).set({
                "clientId": client_id,
                "email": email,
                "sheetId": sheet_id,
                "rowNumber": rownum,
                "targetAnchor": target_anchor,
                "proposalJson": proposal,
                "proposalHash": proposal_hash,
                "status": "proposed",
                "threadId": thread_id,
                "pdfManifest": [{k: v for k, v in p.items() if k != 'images'} for p in (pdf_manifest or [])],  # exclude images from log
                "fileIds": [p["id"] for p in (pdf_manifest or []) if p.get("id")],  # keep old field for compatibility
                "urlTexts": url_texts or [],
                "createdAt": SERVER_TIMESTAMP
            })
            print(f"💾 Stored proposal in sheetChangeLog/{log_doc_id}")
        else:
            print(f"🧪 Dry run - skipped Firestore logging")

        return proposal

    except Exception as e:
        print(f"❌ Failed to propose sheet updates: {e}")
        return None