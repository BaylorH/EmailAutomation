import re
from typing import Optional, List, Dict, Any
from .clients import _sheets_client
from .utils import _norm_txt, _normalize_email

def _header_index_map(header: list[str]) -> dict:
    """Normalize headers for exact match regardless of spacing/case."""
    return {(h or "").strip().lower(): i for i, h in enumerate(header, start=1)}  # 1-based

def _col_letter(n: int) -> str:
    """1-indexed column number -> A1 letter (1->A)."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s

def _get_first_tab_title(sheets, spreadsheet_id: str) -> str:
    meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    return meta["sheets"][0]["properties"]["title"]

def _read_header_row2(sheets, spreadsheet_id: str, tab_title: str) -> list[str]:
    # Entire row 2 regardless of width
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_title}!2:2"
    ).execute()
    vals = resp.get("values", [[]])
    return vals[0] if vals else []

def _first_sheet_props(sheets, spreadsheet_id):
    meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    p = meta["sheets"][0]["properties"]
    return p["sheetId"], p["title"]

def _guess_email_col_idx(header: list[str]) -> int:
    candidates = {"email", "email address", "contact email", "e-mail", "e mail"}
    for i, h in enumerate(header):
        if _normalize_email(h) in candidates:
            return i
    return -1

def _find_row_by_email(sheets, spreadsheet_id: str, tab_title: str, header: list[str], email: str):
    """
    Returns (row_number, row_values) where row_number is the 1-based sheet row.
    Header is row 2, data starts at row 3.
    """
    if not email:
        return None, None

    # Pull header + all data rows with a very wide column cap
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_title}!A2:ZZZ"
    ).execute()
    rows = resp.get("values", [])
    if not rows:
        return None, None

    # rows[0] is header (row 2), rows[1:] are data starting row 3
    data_rows = rows[1:]
    email_idx = _guess_email_col_idx(header)
    needle = _normalize_email(email)

    for offset, row in enumerate(data_rows, start=3):  # sheet row numbers
        # pad row to header length so indexing is safe
        padded = row + [""] * (max(0, len(header) - len(row)))

        candidate = None
        if email_idx >= 0:
            candidate = padded[email_idx]
            if _normalize_email(candidate) == needle:
                return offset, padded

        # fallback: scan all cells for an exact email match
        if email_idx < 0:
            for cell in padded:
                if _normalize_email(cell) == needle:
                    return offset, padded

    return None, None

def _find_row_by_address_city(sheets, spreadsheet_id: str, tab_title: str,
                              header: list[str], address: str, city: str):
    if not address:
        return None, None

    idx_map = _header_index_map(header)  # lowercased header -> 1-based idx
    addr_idx = idx_map.get("property address") or idx_map.get("address") or idx_map.get("street address") or 0
    city_idx = idx_map.get("city") or 0
    if not addr_idx:
        return None, None

    resp = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_title}!A2:ZZZ"
    ).execute()
    rows = resp.get("values", [])
    data_rows = rows[1:] if rows else []  # row 3+

    want_addr = _norm_txt(address)
    want_city = _norm_txt(city)

    for sheet_rownum, row in enumerate(data_rows, start=3):
        row = row + [""] * (max(0, len(header) - len(row)))
        got_addr = _norm_txt(row[addr_idx-1]) if addr_idx else ""
        if got_addr != want_addr:
            continue
        if city_idx:
            got_city = _norm_txt(row[city_idx-1])
            if want_city and got_city != want_city:
                continue
        return sheet_rownum, row

    return None, None

def format_sheet_columns_autosize_with_exceptions(spreadsheet_id: str, header: list[str]) -> None:
    """
    Auto-size all columns to the longest visible value + padding, with exceptions:
      - 'Listing Brokers Comments ' and 'Jill and Clients Comments' -> WRAP and be reasonably wide
      - 'Flyer / Link' and 'Floorplan' -> CLIP and keep width small (ignore huge URLs)
    Header row (row 2) is NOT wrapped.
    Column A additionally respects the width of cell A1 (client name).
    """
    # --- Tunables --------------------------------------------------------------
    CHAR_PX          = 8
    BASE_PADDING_PX  = 24
    EXTRA_FUDGE_PX   = 6

    MIN_WRAP_PX      = 280
    MAX_WRAP_PX      = 600

    LINK_MIN_PX      = 140
    LINK_CAP_PX      = 240
    LINK_HALF_FACTOR = 0.5

    MIN_ANY_PX       = 80
    MAX_ANY_PX       = 900
    # --------------------------------------------------------------------------

    def _norm(name: str) -> str:
        return (name or "").strip().lower()

    WRAP_KEYS = {
        "listing brokers comments",   # trailing space in sheet header is normalized out
        "jill and clients comments",
    }
    LINK_KEYS = {"flyer / link", "floorplan", "floor plan"}

    sheets = _sheets_client()
    meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    first_sheet = meta["sheets"][0]
    grid_id     = first_sheet["properties"]["sheetId"]
    tab_title   = first_sheet["properties"]["title"]

    # Get A1 (client name) so col A can respect it
    a1_val = ""
    try:
        a1_resp = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_title}!A1:A1"
        ).execute()
        a1_vals = a1_resp.get("values", [])
        if a1_vals and a1_vals[0]:
            a1_val = str(a1_vals[0][0]) or ""
    except Exception as _:
        a1_val = ""

    a1_px = len(a1_val) * CHAR_PX + BASE_PADDING_PX + EXTRA_FUDGE_PX if a1_val else 0

    # Read header row (2) + data (rows 3+)
    values_resp = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_title}!A2:ZZZ"
    ).execute()
    rows = values_resp.get("values", [])
    hdr  = rows[0] if rows else header
    data = rows[1:] if len(rows) > 1 else []

    num_cols = max(len(hdr), len(header))
    requests = []

    for c in range(num_cols):
        header_text = (hdr[c] if c < len(hdr) else (header[c] if c < len(header) else "")) or ""
        header_len  = len(header_text)
        header_px   = header_len * CHAR_PX + BASE_PADDING_PX + EXTRA_FUDGE_PX

        # Longest content in data rows
        max_len = header_len
        for r in data:
            if c < len(r) and r[c]:
                L = len(str(r[c]))
                if L > max_len:
                    max_len = L

        auto_px = max_len * CHAR_PX + BASE_PADDING_PX + EXTRA_FUDGE_PX
        col_key = _norm(header_text)

        # --- width/wrap policy by column type
        if col_key in LINK_KEYS:
            width_px = int(auto_px * LINK_HALF_FACTOR)
            width_px = max(width_px, max(header_px, LINK_MIN_PX))
            width_px = min(width_px, LINK_CAP_PX)
            wrap_mode = "CLIP"

        elif col_key in WRAP_KEYS:
            width_px = max(MIN_WRAP_PX, min(auto_px, MAX_WRAP_PX))
            wrap_mode = "WRAP"

        else:
            width_px = max(header_px, auto_px)
            wrap_mode = "OVERFLOW_CELL"

        # NEW: ensure column A is at least wide enough for A1 (client name)
        if c == 0:
            width_px = max(width_px, a1_px)

        # clamp final width
        width_px = max(MIN_ANY_PX, min(int(width_px), MAX_ANY_PX))

        # 1) set column width
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": grid_id, "dimension": "COLUMNS", "startIndex": c, "endIndex": c + 1},
                "properties": {"pixelSize": int(width_px)},
                "fields": "pixelSize"
            }
        })

        # 2) wrap strategy for DATA ONLY (row 3+); header row stays unwrapped
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": grid_id,
                    "startRowIndex": 2,
                    "startColumnIndex": c,
                    "endColumnIndex": c + 1
                },
                "cell": {"userEnteredFormat": {"wrapStrategy": wrap_mode}},
                "fields": "userEnteredFormat.wrapStrategy"
            }
        })

    if requests:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests}
        ).execute()

def append_links_to_flyer_link_column(sheets, spreadsheet_id: str, header: list[str], rownum: int, links: list[str]):
    """Find/create Flyer / Link column and append unique links (no duplicates)."""
    try:
        tab_title = _get_first_tab_title(sheets, spreadsheet_id)
        idx_map = _header_index_map(header)

        # Find 'Flyer / Link' (case-insensitive, trimmed)
        target_key = "flyer / link"
        col_idx = None
        for key, idx in idx_map.items():
            if key == target_key:
                col_idx = idx
                break

        # Create column if missing
        if col_idx is None:
            col_idx = len(header) + 1  # add at end
            col_letter = _col_letter(col_idx)
            sheets.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{tab_title}!{col_letter}2",
                valueInputOption="RAW",
                body={"values": [["Flyer / Link"]]}
            ).execute()
            print(f"📋 Created 'Flyer / Link' column at {col_letter}")

        # Cell range for this row/column
        col_letter = _col_letter(col_idx)
        cell_range = f"{tab_title}!{col_letter}{rownum}"

        # Current cell value
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=cell_range
        ).execute()
        current_value = ""
        values = resp.get("values", [])
        if values and values[0]:
            current_value = values[0][0]

        # Existing links (normalized by stripping whitespace)
        existing_lines = [l.strip() for l in (current_value.splitlines() if current_value else []) if l.strip()]
        existing = set(existing_lines)

        # Clean + dedupe incoming links
        additions = []
        for raw in links or []:
            if not raw:
                continue
            clean = raw.strip()
            if not clean:
                continue
            if clean not in existing:
                additions.append(clean)
                existing.add(clean)

        if not additions:
            print("ℹ️ All links already present in Flyer / Link")
            return

        # Build updated cell content (preserve prior order, append new)
        updated_lines = existing_lines + additions
        updated_value = "\n".join(updated_lines)

        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=cell_range,
            valueInputOption="RAW",
            body={"values": [[updated_value]]}
        ).execute()

        print(f"🔗 Appended {len(additions)} new link(s) to Flyer / Link")

    except Exception as e:
        print(f"❌ Failed to append links to Flyer / Link column: {e}")