from typing import Optional, List, Dict, Any
from .clients import _fs, _sheets_client
from .sheets import _get_first_tab_title, _read_header_row2, _header_index_map, _first_sheet_props, _find_row_by_address_city, _find_row_by_email
from .utils import _subject_to_address_city

def _find_nonviable_divider_row(sheets, spreadsheet_id: str, tab_title: str) -> int | None:
    """Return the divider row index if it exists, else None (no creation)."""
    try:
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=f"{tab_title}!A:A"
        ).execute()
        rows = resp.get("values", [])
        for i, row in enumerate(rows, start=1):
            if row and str(row[0]).strip().upper() == "NON-VIABLE":
                return i
        return None
    except Exception:
        return None

def _is_row_below_nonviable(sheets, spreadsheet_id: str, tab_title: str, rownum: int) -> bool:
    """Stateless check: is this row visually below the 'NON-VIABLE' divider?"""
    div = _find_nonviable_divider_row(sheets, spreadsheet_id, tab_title)
    return bool(div and rownum > div)

def _ensure_divider_conditional_formatting(sheets, spreadsheet_id: str) -> None:
    """
    Add a conditional formatting rule that paints ANY row red + bold white text
    when column A equals 'NON-VIABLE'. Idempotent enough for repeated calls.
    """
    # Figure out sheet + a reasonable column span
    meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    first = meta["sheets"][0]
    sheet_id = first["properties"]["sheetId"]
    tab_title = first["properties"]["title"]

    # Use header width to decide how many columns to cover (fallback to 26)
    try:
        header_resp = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_title}!2:2"
        ).execute()
        header = header_resp.get("values", [[]])[0] if header_resp.get("values") else []
        num_cols = max(26, len(header) or 0)
    except Exception:
        num_cols = 26

    # Apply rule from row 3 downward (data rows), across detected columns
    add_rule = {
        "requests": [{
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": sheet_id,
                        "startRowIndex": 2,          # row 3 (0-based)
                        "startColumnIndex": 0,
                        "endColumnIndex": num_cols
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": '=$A3="NON-VIABLE"'}]
                        },
                        "format": {
                            "backgroundColor": {"red": 0.8, "green": 0.0, "blue": 0.0},
                            "textFormat": {
                                "bold": True,
                                "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}
                            }
                        }
                    }
                },
                "index": 0
            }
        }]
    }

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body=add_rule
    ).execute()

def ensure_nonviable_divider(sheets, spreadsheet_id: str, tab_title: str) -> int:
    """
    Ensure a NON-VIABLE divider row exists. Returns the divider row number.
    Creates if missing by writing 'NON-VIABLE' in column A only and
    ensures conditional formatting is installed (no hard painting).
    """
    try:
        # Scan column A for existing divider
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_title}!A:A"
        ).execute()
        rows = resp.get("values", [])

        for i, row in enumerate(rows, start=1):
            if row and str(row[0]).strip().upper() == "NON-VIABLE":
                # Make sure CF rule exists even if divider already present
                _ensure_divider_conditional_formatting(sheets, spreadsheet_id)
                print(f"📍 Found existing NON-VIABLE divider at row {i}")
                return i

        # Not found: create at the end by setting ONLY column A
        divider_row = (len(rows) + 1) if rows else 1
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_title}!A{divider_row}",
            valueInputOption="RAW",
            body={"values": [["NON-VIABLE"]]}
        ).execute()

        # Ensure the conditional formatting (styling follows the text)
        _ensure_divider_conditional_formatting(sheets, spreadsheet_id)

        print(f"🔴 Created NON-VIABLE divider at row {divider_row}")
        return divider_row

    except Exception as e:
        print(f"❌ Failed to ensure NON-VIABLE divider: {e}")
        raise

def move_row_below_divider(sheets, spreadsheet_id: str, tab_title: str, src_row: int, divider_row: int) -> int:
    """
    Move src_row to immediately below the divider *and* keep the divider as the boundary.
    Returns the new row number of the moved row (immediately below the divider after the operation).
    All row numbers are 1-based in the function signature.
    """
    try:
        sheet_id = _first_sheet_props(sheets, spreadsheet_id)[0]

        # 1) Insert one blank row immediately BELOW the divider (0-based index = divider_row)
        requests = [{
            "insertDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": divider_row,      # 0-based; divider_row is 1-based → row below divider
                    "endIndex": divider_row + 1
                },
                "inheritFromBefore": False
            }
        }]

        # Count columns so we can copy across all used columns
        header = _read_header_row2(sheets, spreadsheet_id, tab_title)
        num_cols = max(1, len(header))

        # 2) COPY the source row to the new blank row just inserted (at divider_row+1 in 1-based terms)
        requests.append({
            "copyPaste": {
                "source": {
                    "sheetId": sheet_id,
                    "startRowIndex": src_row - 1,
                    "endRowIndex": src_row,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols
                },
                "destination": {
                    "sheetId": sheet_id,
                    "startRowIndex": divider_row,   # the newly inserted blank row (0-based)
                    "endRowIndex": divider_row + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols
                },
                "pasteType": "PASTE_NORMAL"
            }
        })

        # 3) DELETE the original source row (above the divider), which lifts divider up by one
        requests.append({
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": src_row - 1,
                    "endIndex": src_row
                }
            }
        })

        sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()

        # After deletion of a row above the divider, the divider shifts up by 1.
        # The moved row sits immediately below the (new) divider.
        new_row = divider_row  # 1-based index of the moved row after the sequence
        print(f"📍 Moved row {src_row} below divider -> now at {new_row}")
        return new_row

    except Exception as e:
        print(f"❌ Failed to move row below divider: {e}")
        raise

def insert_property_row_above_divider(sheets, sheet_id: str, tab_title: str, values_by_header: dict) -> int:
    """
    Insert a new property row one row above the divider (or at end if no divider).
    Returns the new row number.
    FIXED: All parameter references changed to sheet_id.
    """
    try:
        header = _read_header_row2(sheets, sheet_id, tab_title)
        
        # Find divider
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{tab_title}!A:A"
        ).execute()
        rows = resp.get("values", [])
        
        divider_row = None
        for i, row in enumerate(rows, start=1):
            if row and str(row[0]).strip().upper() == "NON-VIABLE":
                divider_row = i
                break
        
        if divider_row:
            insert_row = divider_row
        else:
            insert_row = len(rows) + 1
        
        # Build values array based on header
        row_values = []
        for col_name in header:
            key = col_name.strip().lower()
            value = values_by_header.get(key, "")
            row_values.append(value)
        
        # Insert the row - FIXED: Use sheet_id for both internal ID lookup and API calls
        sheet_id_internal = _first_sheet_props(sheets, sheet_id)[0]
        
        insert_request = {
            "requests": [{
                "insertRange": {
                    "range": {
                        "sheetId": sheet_id_internal,
                        "startRowIndex": insert_row - 1,
                        "endRowIndex": insert_row
                    },
                    "shiftDimension": "ROWS"
                }
            }]
        }
        
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body=insert_request
        ).execute()
        
        # Fill the new row with values - FIXED: Use sheet_id consistently
        if row_values:
            sheets.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"{tab_title}!{insert_row}:{insert_row}",
                valueInputOption="RAW",
                body={"values": [row_values]}
            ).execute()
        
        print(f"✨ Inserted new property row {insert_row} above divider")
        return insert_row
        
    except Exception as e:
        print(f"❌ Failed to insert property row: {e}")
        raise

def _find_row_by_anchor(uid: str, thread_id: str, sheets, spreadsheet_id: str, tab_title: str, 
                       header: list[str], fallback_email: str):
    try:
        # 1) Prefer explicit stored rowNumber (unchanged)
        thread_doc = _fs.collection("users").document(uid).collection("threads").document(thread_id).get()
        if thread_doc.exists:
            thread_data = thread_doc.to_dict() or {}
            stored_row_num = thread_data.get("rowNumber")
            if stored_row_num:
                resp = sheets.spreadsheets().values().get(
                    spreadsheetId=spreadsheet_id,
                    range=f"{tab_title}!{stored_row_num}:{stored_row_num}"
                ).execute()
                rows = resp.get("values", [])
                if rows and rows[0]:
                    padded = rows[0] + [""] * (max(0, len(header) - len(rows[0])))
                    print(f"📍 Using thread-anchored row {stored_row_num}")
                    return stored_row_num, padded

            # 2) NEW: subject → (address, city) → row
            subj = thread_data.get("subject") or ""
            addr, city = _subject_to_address_city(subj)
            if addr:
                rn, rv = _find_row_by_address_city(sheets, spreadsheet_id, tab_title, header, addr, city)
                if rn is not None:
                    print(f"📍 Using subject-anchored row {rn} for '{addr}{', '+city if city else ''}'")
                    return rn, rv

        # 3) Fallback: email matching (unchanged)
        print(f"📧 Falling back to email matching for {fallback_email}")
        return _find_row_by_email(sheets, spreadsheet_id, tab_title, header, fallback_email)

    except Exception as e:
        print(f"⚠️ Row anchor lookup failed: {e}")
        return _find_row_by_email(sheets, spreadsheet_id, tab_title, header, fallback_email)