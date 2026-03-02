import re
import time
import random
from typing import Optional, List, Dict, Any
from googleapiclient.errors import HttpError
from .clients import _sheets_client
from .utils import _norm_txt, _normalize_email

# Rate limit handling configuration
MAX_RETRIES = 5
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 60.0


def _execute_with_retry(request, operation_name: str = "Sheets API"):
    """
    Execute a Google Sheets API request with exponential backoff retry on rate limits.

    Args:
        request: The prepared API request (before .execute())
        operation_name: Human-readable name for logging

    Returns:
        The API response

    Raises:
        HttpError: If all retries are exhausted or non-retryable error occurs
    """
    for attempt in range(MAX_RETRIES):
        try:
            return request.execute()
        except HttpError as e:
            if e.resp.status == 429:
                # Rate limit hit - calculate backoff with jitter
                delay = min(BASE_DELAY_SECONDS * (2 ** attempt), MAX_DELAY_SECONDS)
                jitter = random.uniform(0, delay * 0.25)
                total_delay = delay + jitter

                if attempt < MAX_RETRIES - 1:
                    print(f"⏳ Rate limit hit on {operation_name}, retrying in {total_delay:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})")
                    time.sleep(total_delay)
                else:
                    print(f"❌ Rate limit exceeded for {operation_name} after {MAX_RETRIES} attempts")
                    raise
            else:
                # Non-rate-limit error, don't retry
                raise

    # Should not reach here, but just in case
    raise Exception(f"Unexpected error in retry loop for {operation_name}")

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
    meta = _execute_with_retry(
        sheets.spreadsheets().get(spreadsheetId=spreadsheet_id),
        "get_first_tab_title"
    )
    return meta["sheets"][0]["properties"]["title"]

def _read_header_row2(sheets, spreadsheet_id: str, tab_title: str) -> list[str]:
    # Entire row 2 regardless of width
    resp = _execute_with_retry(
        sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_title}!2:2"
        ),
        "read_header_row2"
    )
    vals = resp.get("values", [[]])
    return vals[0] if vals else []

def _first_sheet_props(sheets, spreadsheet_id):
    meta = _execute_with_retry(
        sheets.spreadsheets().get(spreadsheetId=spreadsheet_id),
        "first_sheet_props"
    )
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
    resp = _execute_with_retry(
        sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_title}!A2:ZZZ"
        ),
        "find_row_by_email"
    )
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

    resp = _execute_with_retry(
        sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_title}!A2:ZZZ"
        ),
        "find_row_by_address_city"
    )
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
    meta = _execute_with_retry(
        sheets.spreadsheets().get(spreadsheetId=spreadsheet_id),
        "format_columns_get_meta"
    )
    first_sheet = meta["sheets"][0]
    grid_id     = first_sheet["properties"]["sheetId"]
    tab_title   = first_sheet["properties"]["title"]

    # Get A1 (client name) so col A can respect it
    a1_val = ""
    try:
        a1_resp = _execute_with_retry(
            sheets.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=f"{tab_title}!A1:A1"
            ),
            "format_columns_get_a1"
        )
        a1_vals = a1_resp.get("values", [])
        if a1_vals and a1_vals[0]:
            a1_val = str(a1_vals[0][0]) or ""
    except Exception as _:
        a1_val = ""

    a1_px = len(a1_val) * CHAR_PX + BASE_PADDING_PX + EXTRA_FUDGE_PX if a1_val else 0

    # Read header row (2) + data (rows 3+)
    values_resp = _execute_with_retry(
        sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_title}!A2:ZZZ"
        ),
        "format_columns_get_values"
    )
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
        _execute_with_retry(
            sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": requests}
            ),
            "format_columns_batch_update"
        )

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
            _execute_with_retry(
                sheets.spreadsheets().values().update(
                    spreadsheetId=spreadsheet_id,
                    range=f"{tab_title}!{col_letter}2",
                    valueInputOption="RAW",
                    body={"values": [["Flyer / Link"]]}
                ),
                "append_links_create_column"
            )
            print(f"📋 Created 'Flyer / Link' column at {col_letter}")

        # Cell range for this row/column
        col_letter = _col_letter(col_idx)
        cell_range = f"{tab_title}!{col_letter}{rownum}"

        # Current cell value
        resp = _execute_with_retry(
            sheets.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=cell_range
            ),
            "append_links_get_current"
        )
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

        _execute_with_retry(
            sheets.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=cell_range,
                valueInputOption="RAW",
                body={"values": [[updated_value]]}
            ),
            "append_links_update"
        )

        print(f"🔗 Appended {len(additions)} new link(s) to Flyer / Link")

    except Exception as e:
        print(f"❌ Failed to append links to Flyer / Link column: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Row Highlighting - Visual indicator of row status in sheet
# ─────────────────────────────────────────────────────────────────────────────
# Yellow = system is actively managing (conversation in progress)
# No highlight = needs user attention, complete, or non-viable

# Light yellow RGB values (0-1 scale for Sheets API)
ROW_HIGHLIGHT_COLOR = {"red": 1.0, "green": 0.95, "blue": 0.6}  # Soft yellow


def highlight_row(spreadsheet_id: str, rownum: int, color: dict = None) -> bool:
    """
    Apply background color highlight to an entire row.

    Args:
        spreadsheet_id: Google Sheets ID
        rownum: 1-based row number to highlight
        color: RGB dict with values 0-1 (defaults to light yellow)

    Returns:
        True on success, False on failure
    """
    if color is None:
        color = ROW_HIGHLIGHT_COLOR

    try:
        sheets = _sheets_client()
        meta = _execute_with_retry(
            sheets.spreadsheets().get(spreadsheetId=spreadsheet_id),
            "highlight_row_get_meta"
        )
        grid_id = meta["sheets"][0]["properties"]["sheetId"]

        # Apply background color to entire row
        request = {
            "repeatCell": {
                "range": {
                    "sheetId": grid_id,
                    "startRowIndex": rownum - 1,  # 0-indexed
                    "endRowIndex": rownum,
                    "startColumnIndex": 0,
                    "endColumnIndex": 50  # Cover plenty of columns
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": color
                    }
                },
                "fields": "userEnteredFormat.backgroundColor"
            }
        }

        _execute_with_retry(
            sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [request]}
            ),
            "highlight_row_update"
        )

        print(f"🟡 Highlighted row {rownum}")
        return True

    except Exception as e:
        print(f"⚠️ Failed to highlight row {rownum}: {e}")
        return False


def clear_row_highlight(spreadsheet_id: str, rownum: int) -> bool:
    """
    Remove background color from an entire row (set to white/default).

    Args:
        spreadsheet_id: Google Sheets ID
        rownum: 1-based row number to clear

    Returns:
        True on success, False on failure
    """
    try:
        sheets = _sheets_client()
        meta = _execute_with_retry(
            sheets.spreadsheets().get(spreadsheetId=spreadsheet_id),
            "clear_highlight_get_meta"
        )
        grid_id = meta["sheets"][0]["properties"]["sheetId"]

        # Set background to white (removing highlight)
        request = {
            "repeatCell": {
                "range": {
                    "sheetId": grid_id,
                    "startRowIndex": rownum - 1,  # 0-indexed
                    "endRowIndex": rownum,
                    "startColumnIndex": 0,
                    "endColumnIndex": 50  # Cover plenty of columns
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}  # White
                    }
                },
                "fields": "userEnteredFormat.backgroundColor"
            }
        }

        _execute_with_retry(
            sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [request]}
            ),
            "clear_highlight_update"
        )

        print(f"⬜ Cleared highlight from row {rownum}")
        return True

    except Exception as e:
        print(f"⚠️ Failed to clear row highlight {rownum}: {e}")
        return False


def highlight_rows_batch(spreadsheet_id: str, rownums: List[int], color: dict = None) -> bool:
    """
    Apply background color highlight to multiple rows in a single API call.
    More efficient than calling highlight_row multiple times.

    Args:
        spreadsheet_id: Google Sheets ID
        rownums: List of 1-based row numbers to highlight
        color: RGB dict with values 0-1 (defaults to light yellow)

    Returns:
        True on success, False on failure
    """
    if not rownums:
        return True

    if color is None:
        color = ROW_HIGHLIGHT_COLOR

    try:
        sheets = _sheets_client()
        meta = _execute_with_retry(
            sheets.spreadsheets().get(spreadsheetId=spreadsheet_id),
            "highlight_rows_batch_get_meta"
        )
        grid_id = meta["sheets"][0]["properties"]["sheetId"]

        requests = []
        for rownum in rownums:
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": grid_id,
                        "startRowIndex": rownum - 1,  # 0-indexed
                        "endRowIndex": rownum,
                        "startColumnIndex": 0,
                        "endColumnIndex": 50
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": color
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor"
                }
            })

        _execute_with_retry(
            sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": requests}
            ),
            "highlight_rows_batch_update"
        )

        print(f"🟡 Highlighted {len(rownums)} rows")
        return True

    except Exception as e:
        print(f"⚠️ Failed to highlight rows: {e}")
        return False