import json
import hashlib
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from google.cloud.firestore import SERVER_TIMESTAMP
from .clients import client, _sheets_client, _fs
from .messaging import build_conversation_payload
from .sheets import _header_index_map, _get_first_tab_title, _col_letter
from .app_config import REQUIRED_FIELDS_FOR_CLOSE

def get_row_anchor(rowvals: list[str], header: list[str]) -> str:
    """Create a brief row anchor from property address and city."""
    try:
        idx_map = _header_index_map(header)
        
        # Try to find address and city
        addr_keys = ["property address", "address", "street address", "property"]
        city_keys = ["city", "town", "municipality"]
        
        def _get_val(keys: list[str]) -> str:
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

def check_missing_required_fields(rowvals: list[str], header: list[str]) -> list[str]:
    """Check which required fields are missing from the row."""
    try:
        idx_map = _header_index_map(header)
        missing = []
        
        for field in REQUIRED_FIELDS_FOR_CLOSE:
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

def _read_ai_meta_row(sheets, spreadsheet_id: str, rownum: int, column: str) -> dict | None:
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

def apply_proposal_to_sheet(
    uid: str,
    client_id: str,
    sheet_id: str,
    header: list[str],
    rownum: int,
    current_rowvals: list[str],
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
                          header: list[str],
                          rownum: int,
                          rowvals: list[str],
                          thread_id: str,
                          file_manifest: list[dict] = None,   # [{"id": "...", "name": "..."}]
                          url_texts: list[dict] = None) -> dict | None:
    """
    Uses OpenAI Responses API to propose sheet updates.
    - Grounds on the current row's (address, city) as TARGET PROPERTY.
    - Shows the model the attachment names so it can pick the right PDF.
    - Enforces strict event and document-selection rules.
    """
    try:
        # Build conversation payload (chronological; latest last)
        conversation = build_conversation_payload(uid, thread_id, limit=10)

        # ---- Rules sections ---------------------------------------------------
        COLUMN_RULES = """
COLUMN SEMANTICS & MAPPING (use EXACT header names):
- "Rent/SF /Yr": Base/asking rent per square foot per YEAR. Synonyms: asking, base rent, $/SF/yr.
- "Ops Ex /SF": NNN/CAM/Operating Expenses per square foot per YEAR. Synonyms: NNN, CAM, OpEx, operating expenses.
- "Gross Rent": If BOTH base rent and NNN are present, set to (Rent/SF /Yr + Ops Ex /SF), rounded to 2 decimals. Else leave unchanged.
- "Total SF": Total square footage. Synonyms: sq footage, square feet, SF, size.
- "Drive Ins": Number of drive-in doors. Synonyms: drive in doors, loading doors.
- "Ceiling Ht": Ceiling height. Synonyms: max ceiling height, ceiling clearance.
- "Power": Electrical power specifications. Synonyms: electrical, power capacity, amperage, voltage, electrical service, power supply, electrical load, electrical capacity, power requirements, electrical specs.
- "Listing Brokers Comments ": Short, non-numeric broker/client notes not covered by other columns. Use terse fragments separated by " • ".
  Do NOT put numeric data like square footage, rent, or ceiling height here if it belongs in dedicated columns.

FORMATTING:
- For money/area fields, output plain decimals (no "$", "SF", commas). Examples: "30", "14.29", "2400".
- For square footage, output just the number: "2000" not "2000 SF".
- For ceiling height, output just the number: "9" not "9 feet" or "9'".
- For drive-ins, output just the number: "3" not "3 doors".
- For power, output the electrical specification as provided: "200A", "480V", "100A 3-phase", "208V/120V", "400A service", etc.
"""

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
- Drive Ins / Docks: count numerical values for the matched space.
- Power: look for "200A", "480V", "100A 3-phase", "208V/120V", "400A service", "electrical service", "power capacity", "amperage", "voltage", "electrical load", "power supply", "electrical specs", "electrical requirements".
- Gross Rent: only compute if BOTH Rent/SF /Yr and Ops Ex /SF are present (sum, 2 decimals).
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

- "call_requested": Only when someone explicitly asks for a call/phone conversation.

- "close_conversation": When conversation appears complete and the sender indicates they're done.

CRITICAL EXAMPLES:
- "Below is the only current space we have" + URL = new_property event
- "Here's an alternative location" = new_property event  
- "This property isn't available" = property_unavailable event
- "Can you call me?" = call_requested event
"""

        # ---- Build prompt -----------------------------------------------------
        target_anchor = get_row_anchor(rowvals, header)  # e.g., "1 Randolph Ct, Evans"

        prompt_parts = [f"""
You are analyzing a conversation thread to suggest updates to ONE Google Sheet row and detect key events.

TARGET PROPERTY (canonical identity for matching): {target_anchor}

{COLUMN_RULES}
{DOC_SELECTION_RULES}
{EVENT_RULES}

SHEET HEADER (row 2):
{json.dumps(header)}

CURRENT ROW VALUES (row {rownum}):
{json.dumps(rowvals)}

CONVERSATION HISTORY (latest last):
{json.dumps(conversation, indent=2)}
""".rstrip()]

        # Attachment index (names only) helps the model choose the right file
        if file_manifest:
            prompt_parts.append("\nATTACHMENTS (names shown for grounding):")
            for f in file_manifest:
                # defensive: handle dicts with/without name
                name = f.get("name") or "<unnamed.pdf>"
                prompt_parts.append(f" - {name}")

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
      "type": "call_requested | property_unavailable | new_property | close_conversation",
      "address": "<for new_property: extract property name, address, or identifier>",
      "city": "<for new_property: infer city/location if possible>",
      "email": "<for new_property if different email needed>",
      "link": "<for new_property: include URL if mentioned>",
      "notes": "<for new_property: additional context about the property>"
    }
  ],
  "notes": "<optional general notes about the conversation>"
}
""")

        prompt = "".join(prompt_parts)

        # ---- Prepare inputs (files first, then text) --------------------------
        input_content = []
        if file_manifest:
            for f in file_manifest:
                # Each file is actual content; the earlier index gives the model the names.
                if "id" in f and f["id"]:
                    input_content.append({"type": "input_file", "file_id": f["id"]})

        input_content.append({"type": "input_text", "text": prompt})

        # ---- Call OpenAI (low temperature for determinism) --------------------
        response = client.responses.create(
            model="gpt-4o",  # Using the model from config
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
            "fileManifest": file_manifest or [],
            "fileIds": [f["id"] for f in (file_manifest or [])],  # keep old field for compatibility
            "urlTexts": url_texts or [],
            "createdAt": SERVER_TIMESTAMP
        })
        print(f"💾 Stored proposal in sheetChangeLog/{log_doc_id}")

        return proposal

    except Exception as e:
        print(f"❌ Failed to propose sheet updates: {e}")
        return None