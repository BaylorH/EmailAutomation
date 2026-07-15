import re
import time
import random
from typing import Optional, List, Dict, Any
from googleapiclient.errors import HttpError
from .clients import _sheets_client
from .column_config import (
    CANONICAL_FIELDS,
    canonical_field_for_column,
    coerce_sheet_value_for_column,
    is_wrapped_notes_column,
)
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


ASSET_LINK_COLUMN_ALIASES = {
    "Flyer / Link": {
        "flyer / link",
        "flyer/link",
        "flyer link",
        "flyer",
        "flyers",
        "brochure",
        "brochures",
    },
    "Floorplan": {
        "floorplan",
        "floorplans",
        "floor plan",
        "floor plans",
        "floor plan / link",
        "floorplan / link",
    },
}


class AssetLinkWriteError(RuntimeError):
    """Expose partial asset writes so callers can reconcile before retrying."""

    def __init__(
        self,
        canonical_column: str,
        cause: Exception,
        *,
        applied_updates: Optional[dict[str, list[str]]] = None,
        created_columns: Optional[list[str]] = None,
    ) -> None:
        super().__init__(f"Failed to write {canonical_column} links: {cause}")
        self.canonical_column = canonical_column
        self.applied_updates = {
            column: list(values or [])
            for column, values in (applied_updates or {}).items()
        }
        self.created_columns = list(created_columns or [])


def _asset_header_base(value: str) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return re.sub(r"\s+\d+$", "", normalized).strip()


def _asset_columns(header: list[str], canonical_column: str) -> list[tuple[int, str]]:
    aliases = ASSET_LINK_COLUMN_ALIASES[canonical_column]
    return [
        (index, str(label or "").strip())
        for index, label in enumerate(header or [], start=1)
        if _asset_header_base(label) in aliases
    ]

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
      - broker and client/team note columns -> WRAP and be reasonably wide
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
            range=f"{tab_title}!A2:ZZZ",
            valueRenderOption="UNFORMATTED_VALUE",
        ),
        "format_columns_get_values"
    )
    rows = values_resp.get("values", [])
    hdr  = rows[0] if rows else header
    data = rows[1:] if len(rows) > 1 else []

    num_cols = max(len(hdr), len(header))
    requests = []
    numeric_value_updates = []

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
        canonical = canonical_field_for_column(header_text)
        field = CANONICAL_FIELDS.get(canonical or "", {})

        # --- width/wrap policy by column type
        if col_key in LINK_KEYS:
            width_px = int(auto_px * LINK_HALF_FACTOR)
            width_px = max(width_px, max(header_px, LINK_MIN_PX))
            width_px = min(width_px, LINK_CAP_PX)
            wrap_mode = "CLIP"

        elif is_wrapped_notes_column(header_text):
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
        user_entered_format = {"wrapStrategy": wrap_mode}
        format_fields = ["userEnteredFormat.wrapStrategy"]
        if field.get("format") == "currency":
            user_entered_format["numberFormat"] = {
                "type": "CURRENCY",
                "pattern": "$#,##0.00",
            }
            format_fields.append("userEnteredFormat.numberFormat")

        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": grid_id,
                    "startRowIndex": 2,
                    "startColumnIndex": c,
                    "endColumnIndex": c + 1
                },
                "cell": {"userEnteredFormat": user_entered_format},
                "fields": ",".join(format_fields),
            }
        })

        if field.get("format") == "currency" and not field.get("is_formula"):
            for rownum, row in enumerate(data, start=3):
                if c >= len(row) or not isinstance(row[c], str):
                    continue
                typed_value = coerce_sheet_value_for_column(header_text, row[c])
                if isinstance(typed_value, (int, float)) and not isinstance(typed_value, bool):
                    numeric_value_updates.append({
                        "range": f"{tab_title}!{_col_letter(c + 1)}{rownum}",
                        "values": [[typed_value]],
                    })

    if requests:
        _execute_with_retry(
            sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": requests}
            ),
            "format_columns_batch_update"
        )

    if numeric_value_updates:
        _execute_with_retry(
            sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "valueInputOption": "RAW",
                    "data": numeric_value_updates,
                },
            ),
            "format_columns_numeric_values",
        )

def _append_links_to_asset_columns(
    sheets,
    spreadsheet_id: str,
    header: list[str],
    rownum: int,
    links: list[str],
    *,
    canonical_column: str,
) -> dict[str, list[str]]:
    """Write unique asset links one per cell and keep the shared header current."""
    applied: dict[str, list[str]] = {}
    created_columns: list[str] = []
    try:
        tab_title = _get_first_tab_title(sheets, spreadsheet_id)
        live_header = _read_header_row2(sheets, spreadsheet_id, tab_title)
        if isinstance(header, list):
            if live_header:
                header[:] = live_header
            working_header = header
        else:
            working_header = list(live_header or header or [])
        columns = _asset_columns(working_header, canonical_column)
        existing: set[str] = set()
        blank_columns: list[tuple[int, str]] = []

        for col_idx, column_label in columns:
            cell_range = f"{tab_title}!{_col_letter(col_idx)}{rownum}"
            resp = _execute_with_retry(
                sheets.spreadsheets().values().get(
                    spreadsheetId=spreadsheet_id,
                    range=cell_range,
                ),
                "asset_link_get_current",
            )
            values = resp.get("values", [])
            current_value = str(values[0][0] or "").strip() if values and values[0] else ""
            if current_value:
                existing.update(line.strip() for line in current_value.splitlines() if line.strip())
            else:
                blank_columns.append((col_idx, column_label))

        additions: list[str] = []
        for raw in links or []:
            if not raw:
                continue
            clean = str(raw).strip()
            if not clean:
                continue
            if clean not in existing:
                additions.append(clean)
                existing.add(clean)

        if not additions:
            print(f"ℹ️ All links already present in {canonical_column}")
            return {}

        primary_label = columns[0][1] if columns else canonical_column
        suffix = 2

        for link in additions:
            if blank_columns:
                col_idx, column_label = blank_columns.pop(0)
            else:
                col_idx = len(working_header) + 1
                if not columns and not applied:
                    column_label = canonical_column
                else:
                    used_labels = {str(label or "").strip().lower() for label in working_header}
                    while f"{primary_label} {suffix}".lower() in used_labels:
                        suffix += 1
                    column_label = f"{primary_label} {suffix}"
                    suffix += 1

                col_letter = _col_letter(col_idx)
                _execute_with_retry(
                    sheets.spreadsheets().values().update(
                        spreadsheetId=spreadsheet_id,
                        range=f"{tab_title}!{col_letter}2",
                        valueInputOption="RAW",
                        body={"values": [[column_label]]},
                    ),
                    "asset_link_create_column",
                )
                working_header.append(column_label)
                columns.append((col_idx, column_label))
                created_columns.append(column_label)
                print(f"📋 Created '{column_label}' column at {col_letter}")

            cell_range = f"{tab_title}!{_col_letter(col_idx)}{rownum}"
            try:
                _execute_with_retry(
                    sheets.spreadsheets().values().update(
                        spreadsheetId=spreadsheet_id,
                        range=cell_range,
                        valueInputOption="RAW",
                        body={"values": [[link]]},
                    ),
                    "asset_link_update",
                )
            except Exception:
                try:
                    readback = _execute_with_retry(
                        sheets.spreadsheets().values().get(
                            spreadsheetId=spreadsheet_id,
                            range=cell_range,
                        ),
                        "asset_link_failure_readback",
                    )
                    readback_values = readback.get("values", [])
                    readback_value = (
                        str(readback_values[0][0] or "").strip()
                        if readback_values and readback_values[0]
                        else ""
                    )
                    if readback_value == link:
                        applied.setdefault(column_label, []).append(link)
                except Exception:
                    pass
                raise
            applied.setdefault(column_label, []).append(link)

        print(f"🔗 Wrote {len(additions)} {canonical_column} link(s) to separate cells")
        return applied

    except Exception as e:
        print(f"❌ Failed to write {canonical_column} links: {e}")
        raise AssetLinkWriteError(
            canonical_column,
            e,
            applied_updates=applied,
            created_columns=created_columns,
        ) from e


def append_links_to_flyer_link_column(
    sheets, spreadsheet_id: str, header: list[str], rownum: int, links: list[str]
) -> dict[str, list[str]]:
    return _append_links_to_asset_columns(
        sheets,
        spreadsheet_id,
        header,
        rownum,
        links,
        canonical_column="Flyer / Link",
    )


def append_links_to_floorplan_column(
    sheets, spreadsheet_id: str, header: list[str], rownum: int, links: list[str]
) -> dict[str, list[str]]:
    return _append_links_to_asset_columns(
        sheets,
        spreadsheet_id,
        header,
        rownum,
        links,
        canonical_column="Floorplan",
    )


def write_property_image_columns(
    sheets,
    spreadsheet_id: str,
    header: list[str],
    rownum: int,
    updates_by_column: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Find/create property image columns and write values only into blank cells."""
    applied: dict[str, list[str]] = {}
    try:
        tab_title = _get_first_tab_title(sheets, spreadsheet_id)
        working_header = list(header or [])

        for canonical_column, values in (updates_by_column or {}).items():
            value = ""
            for candidate in values or []:
                candidate = str(candidate or "").strip()
                if candidate:
                    value = candidate
                    break
            if not value:
                continue

            idx_map = _header_index_map(working_header)
            col_idx = idx_map.get(canonical_column.strip().lower())
            if col_idx is None:
                continue

            col_letter = _col_letter(col_idx)
            cell_range = f"{tab_title}!{col_letter}{rownum}"
            resp = _execute_with_retry(
                sheets.spreadsheets().values().get(
                    spreadsheetId=spreadsheet_id,
                    range=cell_range,
                ),
                "property_image_get_current",
            )
            current_value = ""
            values_resp = resp.get("values", [])
            if values_resp and values_resp[0]:
                current_value = str(values_resp[0][0] or "").strip()
            if current_value:
                continue

            _execute_with_retry(
                sheets.spreadsheets().values().update(
                    spreadsheetId=spreadsheet_id,
                    range=cell_range,
                    valueInputOption="RAW",
                    body={"values": [[value]]},
                ),
                "property_image_update",
            )
            applied[canonical_column] = [value]

        if applied:
            print(f"🖼️ Wrote property image metadata columns: {', '.join(applied.keys())}")
        return applied
    except Exception as e:
        print(f"❌ Failed to write property image columns: {e}")
        return applied


def is_floorplan_filename(filename: str) -> bool:
    """
    Detect if a PDF filename indicates it's a floorplan/building plan.

    Returns True for filenames containing:
    - floor plan, floorplan, floor-plan
    - layout
    - site plan, siteplan
    - sealed (sealed architectural drawings)
    - blueprint
    - bldg (building abbreviation, often used for building plans)
    """
    if not filename:
        return False

    name_lower = filename.lower()
    floorplan_patterns = [
        "floor plan", "floorplan", "floor-plan", "floor_plan",
        "layout",
        "site plan", "siteplan", "site-plan", "site_plan",
        "sealed",      # Sealed architectural drawings
        "blueprint",
        "bldg",        # Building abbreviation (e.g., "Sealed Bldg C")
        "building plan"
    ]

    return any(pattern in name_lower for pattern in floorplan_patterns)


# ─────────────────────────────────────────────────────────────────────────────
# Row Highlighting - Visual indicator of row status in sheet
# ─────────────────────────────────────────────────────────────────────────────
# Yellow = system is actively managing (conversation in progress)
# No highlight = needs user attention, complete, or non-viable

# Row highlight colors (RGB values 0-1 scale for Sheets API)
ROW_HIGHLIGHT_YELLOW = {"red": 1.0, "green": 0.95, "blue": 0.6}  # Active - system monitoring
ROW_HIGHLIGHT_BLUE = {"red": 0.7, "green": 0.85, "blue": 1.0}    # Paused - awaiting user action
ROW_HIGHLIGHT_COLOR = ROW_HIGHLIGHT_YELLOW  # Default for backwards compatibility


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

        # Use appropriate emoji based on color
        if color == ROW_HIGHLIGHT_BLUE:
            print(f"🔵 Highlighted row {rownum} (paused/awaiting user)")
        else:
            print(f"🟡 Highlighted row {rownum} (active)")
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
