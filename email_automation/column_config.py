"""
Dynamic Column Configuration System
====================================
Allows flexible column naming in client sheets by mapping canonical field names
to the actual column headers in each client's sheet.

FLOW:
1. When a client is added, the frontend calls analyzeSheetColumns()
2. This uses AI to match their column headers to our canonical fields
3. User confirms/adjusts the mapping in the UI
4. Mapping is stored in client.columnConfig in Firestore
5. Backend uses get_column_config() to build dynamic prompts

CANONICAL FIELDS:
- Each field has a semantic meaning that's independent of the column name
- AI extraction uses these canonical names internally
- When writing to sheets, we translate back to actual column names
"""

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Any


LISTING_COMMENT_COLUMN_ALIASES = (
    "listing broker comments",
    "listing brokers comments",
    "listing broker comment",
    "broker comments",
    "broker notes",
    "comments",
    "notes",
)

CLIENT_COMMENT_COLUMN_ALIASES = (
    "client / team comments",
    "client/team comments",
    "client and team comments",
    "team and client comments",
    "team comments",
    "client comments",
    "clients comments",
    "internal notes",
    "our comments",
)

LEGACY_CLIENT_COMMENT_COLUMN_ALIASES = (
    "jills comments",
    "jill's comments",
    "jill comments",
    "jill and client comments",
    "jill/client comments",
    "jill and clients comments",
)

# ============================================================================
# CANONICAL FIELD DEFINITIONS
# ============================================================================

CANONICAL_FIELDS = {
    # IDENTIFICATION (required for row matching)
    "property_address": {
        "label": "Property Address",
        "description": "Street address of the property",
        "required_for_matching": True,
        "default_aliases": ["property address", "address", "street address", "property", "location"],
        "extraction_hints": "The street address of the property being discussed",
        "format": "text",
    },
    "city": {
        "label": "City",
        "description": "City where property is located",
        "required_for_matching": True,
        "default_aliases": ["city", "town", "municipality", "location"],
        "extraction_hints": "City name",
        "format": "text",
    },

    # PROPERTY INFO
    "property_name": {
        "label": "Property Name",
        "description": "Name of the property or building",
        "required_for_matching": False,
        "default_aliases": ["property name", "building name", "name"],
        "extraction_hints": "Named property or complex (e.g., 'Commerce Park Building C')",
        "format": "text",
    },

    # CONTACT INFO
    "leasing_company": {
        "label": "Leasing Company",
        "description": "Company handling leasing",
        "required_for_matching": False,
        "default_aliases": ["leasing company", "company", "brokerage", "listing company"],
        "extraction_hints": "The real estate company or brokerage",
        "format": "text",
    },
    "leasing_contact": {
        "label": "Leasing Contact",
        "description": "Contact person name",
        "required_for_matching": False,
        "default_aliases": ["leasing contact", "contact", "broker name", "agent name", "contact name"],
        "extraction_hints": "Name of the contact person",
        "format": "text",
    },
    "email": {
        "label": "Email",
        "description": "Contact email address",
        "required_for_matching": True,
        "default_aliases": ["email", "email address", "contact email", "e-mail", "e mail"],
        "extraction_hints": "Email address for correspondence",
        "format": "email",
    },

    # PROPERTY SPECS (extractable from conversations)
    "total_sf": {
        "label": "Total SF",
        "description": "Total square footage",
        "required_for_matching": False,
        "default_aliases": ["total sf", "square footage", "sq ft", "size", "sf", "sqft", "square feet"],
        "extraction_hints": "Total leasable square footage. Output plain number only (e.g., '15000' not '15,000 SF')",
        "format": "number",
        "extractable": True,
        "ai_synonyms": ["sq footage", "square feet", "SF", "size", "space", "leasable area"],
    },
    "rent_sf_yr": {
        "label": "Rent/SF /Yr",
        "description": "Base rent per square foot per year",
        "required_for_matching": False,
        "default_aliases": ["rent/sf /yr", "rent/sf/yr", "asking rent", "base rent", "rent", "$/sf/yr", "asking"],
        "extraction_hints": "Base/asking rent per SF per YEAR. Output plain decimal (e.g., '8.50' not '$8.50/SF NNN')",
        "format": "currency",
        "extractable": True,
        "required_for_close": True,
        "ai_synonyms": ["asking", "base rent", "$/SF/yr", "rent per foot"],
    },
    "ops_ex_sf": {
        "label": "Ops Ex /SF",
        "description": "Operating expenses per SF per year (NNN/CAM)",
        "required_for_matching": False,
        "default_aliases": ["ops ex /sf", "ops ex/sf", "nnn", "cam", "operating expenses", "opex", "triple net", "nnn/cam"],
        "extraction_hints": "NNN/CAM/Operating Expenses per SF per YEAR. Output plain decimal.",
        "format": "currency",
        "extractable": True,
        "required_for_close": True,
        "ai_synonyms": ["NNN", "CAM", "OpEx", "operating expenses", "triple net", "common area maintenance"],
    },
    "gross_rent": {
        "label": "Gross Rent",
        "description": "Calculated gross rent (FORMULA - never write)",
        "required_for_matching": False,
        "default_aliases": ["gross rent", "total rent", "all-in rent"],
        "extraction_hints": None,  # Never extract
        "format": "currency",
        "extractable": False,
        "is_formula": True,  # NEVER write to this column
        "formula_note": "Auto-calculates from Rent/SF + Ops Ex",
    },
    "drive_ins": {
        "label": "Drive Ins",
        "description": "Number of drive-in doors",
        "required_for_matching": False,
        "default_aliases": ["drive ins", "drive-ins", "drive in doors", "loading doors", "grade doors", "gl doors"],
        "extraction_hints": "Number of drive-in/grade-level doors. Output just the number (e.g., '2' not '2 doors')",
        "format": "number",
        "extractable": True,
        "required_for_close": True,
        "ai_synonyms": ["drive in doors", "loading doors", "grade level doors"],
    },
    "docks": {
        "label": "Docks",
        "description": "Number of dock doors",
        "required_for_matching": False,
        "default_aliases": ["docks", "dock doors", "loading docks", "dock positions", "dock bays"],
        "extraction_hints": "Number of dock-high doors. Output just the number.",
        "format": "number",
        "extractable": True,
        "required_for_close": True,
        "ai_synonyms": ["dock doors", "loading docks", "dock positions", "dock bays", "truck docks"],
    },
    "ceiling_ht": {
        "label": "Ceiling Ht",
        "description": "Clear ceiling height",
        "required_for_matching": False,
        "default_aliases": ["ceiling ht", "ceiling height", "clear height", "clearance", "ceiling"],
        "extraction_hints": "Clear height in feet. Output just the number (e.g., '24' not '24 feet')",
        "format": "number",
        "extractable": True,
        "required_for_close": True,
        "ai_synonyms": ["clear height", "ceiling clearance", "warehouse height"],
    },
    "power": {
        "label": "Power",
        "description": "Electrical power specifications",
        "required_for_matching": False,
        "default_aliases": ["power", "electrical", "electric", "amps", "voltage", "electrical service"],
        "extraction_hints": "Electrical specs as provided (e.g., '400A 3-phase', '208V', '200 amps')",
        "format": "text",
        "extractable": True,
        "required_for_close": True,
        "ai_synonyms": ["electrical", "power capacity", "amperage", "voltage", "electrical service"],
    },

    # NOTES & LINKS
    "listing_comments": {
        "label": "Listing Brokers Comments",
        "description": "Broker's notes and comments",
        "required_for_matching": False,
        "default_aliases": ["listing broker comments", "listing brokers comments", "broker comments", "comments", "notes", "broker notes"],
        "extraction_hints": None,  # Use 'notes' field in AI output instead
        "format": "text",
        "extractable": False,  # AI writes to 'notes' field, which gets appended here
        "append_mode": True,  # Don't overwrite, append with separator
    },
    "flyer_link": {
        "label": "Flyer / Link",
        "description": "Links to flyers or listings",
        "required_for_matching": False,
        "default_aliases": ["flyer / link", "flyer/link", "flyer", "flyers", "link", "links", "brochure", "listing link"],
        "extraction_hints": "URLs to property flyers or listings",
        "format": "url",
        "extractable": True,
        "never_request": True,
        "append_mode": True,
    },
    "floorplan": {
        "label": "Floorplan",
        "description": "Links to floor plans",
        "required_for_matching": False,
        "default_aliases": ["floorplan", "floor plan", "floor plans", "layout"],
        "extraction_hints": "URLs to floor plan documents",
        "format": "url",
        "extractable": True,
        "append_mode": True,
    },
    "client_comments": {
        "label": "Client / Team Comments",
        "description": "Internal client notes",
        "required_for_matching": False,
        "default_aliases": list(CLIENT_COMMENT_COLUMN_ALIASES),
        "legacy_aliases": list(LEGACY_CLIENT_COMMENT_COLUMN_ALIASES),
        "extraction_hints": None,  # Internal use only
        "format": "text",
        "extractable": False,
    },
}

# Fields required for conversation to be considered "complete" (default)
DEFAULT_REQUIRED_FOR_CLOSE = [k for k, v in CANONICAL_FIELDS.items() if v.get("required_for_close")]

# Fields that AI can extract from conversations
EXTRACTABLE_FIELDS = [k for k, v in CANONICAL_FIELDS.items() if v.get("extractable")]

# Fields that should never be written (formula columns)
FORMULA_FIELDS = [k for k, v in CANONICAL_FIELDS.items() if v.get("is_formula")]

# Fields we accept but never request
NEVER_REQUEST_FIELDS = [k for k, v in CANONICAL_FIELDS.items() if v.get("never_request")]
ASSET_CANONICAL_FIELDS = frozenset({"flyer_link", "floorplan"})

# Legacy alias for backward compatibility
REQUIRED_FOR_CLOSE = DEFAULT_REQUIRED_FOR_CLOSE

# ============================================================================
# COLUMN MODES - Used by frontend dropdown
# ============================================================================
COLUMN_MODES = {
    "ask_required": {
        "label": "Ask (Required)",
        "description": "AI will request if missing. Required for row completion.",
        "extractable": True,
        "required": True,
    },
    "ask_optional": {
        "label": "Ask (Optional)",
        "description": "AI will request if missing. Not required for completion.",
        "extractable": True,
        "required": False,
    },
    "accept_only": {
        "label": "Accept Only",
        "description": "AI extracts if provided but never asks for it.",
        "extractable": True,
        "required": False,
        "never_request": True,
    },
    "note": {
        "label": "Note",
        "description": "AI appends contextual information. Never requests.",
        "extractable": False,
        "append_mode": True,
    },
    "skip": {
        "label": "Skip",
        "description": "Column is ignored by the system.",
        "extractable": False,
    },
}


def get_default_column_config() -> Dict[str, Any]:
    """
    Returns default column configuration using standard aliases.
    This is a canonical template for setup and tests. Persisted campaigns must
    provide their own complete columnConfig rather than falling back to it.
    """
    return {
        "mappings": {
            canonical: field["default_aliases"][0]  # Use first alias as default
            for canonical, field in CANONICAL_FIELDS.items()
        },
        "requiredFields": DEFAULT_REQUIRED_FOR_CLOSE.copy(),
        "formulaFields": FORMULA_FIELDS,
        "neverRequest": NEVER_REQUEST_FIELDS,
        "extractionFields": EXTRACTABLE_FIELDS.copy(),
        "customFields": {},  # {columnHeader: {mode, description}}
    }


def get_field_aliases(canonical: str) -> List[str]:
    """Return current aliases first, then legacy aliases for existing sheets."""
    field = CANONICAL_FIELDS.get(canonical, {})
    aliases = list(field.get("default_aliases", []))
    aliases.extend(field.get("legacy_aliases", []))
    return aliases


def canonical_field_for_column(
    actual_name: str,
    column_config: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Resolve a physical sheet header to its canonical field definition."""
    normalized = _normalized_column_name(actual_name)
    if not normalized:
        return None

    mappings = column_config.get("mappings", {}) if isinstance(column_config, dict) else {}
    for canonical, configured_name in mappings.items():
        if _normalized_column_name(configured_name) == normalized:
            return canonical

    for canonical, field in CANONICAL_FIELDS.items():
        known_names = [
            canonical,
            field.get("label"),
            *field.get("default_aliases", []),
            *field.get("legacy_aliases", []),
        ]
        if normalized in {
            _normalized_column_name(name)
            for name in known_names
            if isinstance(name, str) and name.strip()
        }:
            return canonical
    return None


_SHEET_NUMBER_RE = re.compile(
    r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$"
)


def coerce_sheet_value_for_column(
    actual_name: str,
    value: Any,
    column_config: Optional[Dict[str, Any]] = None,
) -> Any:
    """Return a JSON numeric value for recognized number/currency columns."""
    canonical = canonical_field_for_column(actual_name, column_config)
    field_format = CANONICAL_FIELDS.get(canonical or "", {}).get("format")
    if field_format not in {"number", "currency"} or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return value

    candidate = value.strip()
    if field_format == "currency" and candidate.startswith("$"):
        candidate = candidate[1:].strip()
    if not _SHEET_NUMBER_RE.fullmatch(candidate):
        return value

    try:
        parsed = Decimal(candidate.replace(",", ""))
    except InvalidOperation:
        return value
    return float(parsed) if "." in candidate else int(parsed)


def sheet_values_equal_for_column(
    actual_name: str,
    left: Any,
    right: Any,
    column_config: Optional[Dict[str, Any]] = None,
) -> bool:
    """Compare formatted and raw numeric values without false override flags."""
    left_typed = coerce_sheet_value_for_column(actual_name, left, column_config)
    right_typed = coerce_sheet_value_for_column(actual_name, right, column_config)
    numeric_types = (int, float)
    if (
        isinstance(left_typed, numeric_types)
        and not isinstance(left_typed, bool)
        and isinstance(right_typed, numeric_types)
        and not isinstance(right_typed, bool)
    ):
        return Decimal(str(left_typed)) == Decimal(str(right_typed))
    left_text = "" if left is None else str(left)
    right_text = "" if right is None else str(right)
    return left_text == right_text


def is_asset_column_name(
    actual_name: str,
    column_config: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether a physical sheet column belongs to the asset pipeline."""
    normalized = _normalized_column_name(actual_name)
    if not normalized:
        return False

    candidate_names = {normalized}
    numbered_match = re.fullmatch(r"(.+?)\s+(\d+)", normalized)
    if numbered_match and int(numbered_match.group(2)) >= 2:
        candidate_names.add(numbered_match.group(1).strip())

    mappings = column_config.get("mappings", {}) if isinstance(column_config, dict) else {}
    for canonical in ASSET_CANONICAL_FIELDS:
        configured_name = mappings.get(canonical)
        if _normalized_column_name(configured_name) in candidate_names:
            return True

        field = CANONICAL_FIELDS.get(canonical, {})
        known_names = [canonical, field.get("label"), *field.get("default_aliases", [])]
        if candidate_names & {_normalized_column_name(name) for name in known_names if name}:
            return True

    return False


def _normalized_column_name(name: str) -> str:
    normalized = " ".join((name or "").strip().lower().split())
    return re.sub(r"\s*/\s*", "/", normalized)


def _find_header_index_for_aliases(header: List[str], aliases: List[str]) -> Optional[int]:
    alias_set = {_normalized_column_name(alias) for alias in aliases}
    for index, column in enumerate(header, start=1):
        if _normalized_column_name(column) in alias_set:
            return index
    return None


def find_listing_comment_column_index(header: List[str]) -> Optional[int]:
    return _find_header_index_for_aliases(header, list(LISTING_COMMENT_COLUMN_ALIASES))


def find_client_comment_column_index(header: List[str]) -> Optional[int]:
    current_index = _find_header_index_for_aliases(header, list(CLIENT_COMMENT_COLUMN_ALIASES))
    if current_index:
        return current_index
    return _find_header_index_for_aliases(header, list(LEGACY_CLIENT_COMMENT_COLUMN_ALIASES))


def find_notes_comment_column_index(header: List[str]) -> Optional[int]:
    return find_listing_comment_column_index(header) or find_client_comment_column_index(header)


def is_wrapped_notes_column(header_name: str) -> bool:
    normalized = _normalized_column_name(header_name)
    wrap_aliases = set(LISTING_COMMENT_COLUMN_ALIASES)
    wrap_aliases.update(CLIENT_COMMENT_COLUMN_ALIASES)
    wrap_aliases.update(LEGACY_CLIENT_COMMENT_COLUMN_ALIASES)
    return normalized in wrap_aliases


def get_default_mode_for_canonical(canonical: str) -> str:
    """
    Get the default column mode for a canonical field.
    """
    if canonical not in CANONICAL_FIELDS:
        return "skip"

    field = CANONICAL_FIELDS[canonical]

    if field.get("is_formula"):
        return "skip"  # Formula fields should be skipped
    elif field.get("never_request") and field.get("append_mode"):
        return "note"
    elif field.get("never_request"):
        return "accept_only"
    elif field.get("required_for_close"):
        return "ask_required"
    elif field.get("extractable"):
        return "ask_optional"
    elif field.get("append_mode"):
        return "note"
    else:
        return "skip"


def get_column_config_error(column_config: Any) -> Optional[str]:
    """Return a reason when a persisted campaign column contract is unsafe."""
    if not isinstance(column_config, dict):
        return "columnConfig must be an object"

    list_fields = (
        "extractionFields",
        "requiredFields",
        "formulaFields",
        "neverRequest",
    )
    for name in list_fields:
        values = column_config.get(name)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            return f"columnConfig.{name} must be a list of strings"

    mappings = column_config.get("mappings")
    if not isinstance(mappings, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) or not value.strip()
        for key, value in mappings.items()
    ):
        return "columnConfig.mappings must map string keys to non-empty headers"

    custom_fields = column_config.get("customFields")
    if not isinstance(custom_fields, dict):
        return "columnConfig.customFields must be an object"
    for header, config in custom_fields.items():
        if not isinstance(header, str) or not header.strip() or not isinstance(config, dict):
            return "columnConfig.customFields entries must be objects keyed by non-empty headers"
        if config.get("mode") not in COLUMN_MODES:
            return f"columnConfig custom field {header!r} has an invalid mode"

    extraction = set(column_config["extractionFields"])
    required = set(column_config["requiredFields"])
    formulas = set(column_config["formulaFields"])
    never_request = set(column_config["neverRequest"])
    mapped = set(mappings)

    if not required <= extraction:
        return "columnConfig.requiredFields must be included in extractionFields"
    if not never_request <= extraction:
        return "columnConfig.neverRequest must be included in extractionFields"
    if (required & never_request) or (required & formulas):
        return "columnConfig required fields cannot be Note or formula fields"
    if not (extraction | formulas) <= mapped:
        return "columnConfig configured fields must have mappings"

    return None


def get_non_requestable_field_terms(column_config: Dict[str, Any]) -> List[List[str]]:
    """Return aliases grouped by each configured Note, Skip, or formula field."""
    mappings = column_config["mappings"]
    extraction = set(column_config["extractionFields"])
    skipped_extractable = {
        canonical
        for canonical in mappings
        if CANONICAL_FIELDS.get(canonical, {}).get("extractable")
        and canonical not in extraction
    }
    non_requestable = (
        set(column_config["neverRequest"])
        | set(column_config["formulaFields"])
        | skipped_extractable
        | ({"listing_comments", "client_comments"} & set(mappings))
    )

    groups = []
    for canonical in non_requestable:
        field = CANONICAL_FIELDS.get(canonical, {})
        terms = [mappings.get(canonical), field.get("label")]
        if canonical in (
            set(column_config["neverRequest"])
            | set(column_config["formulaFields"])
            | {"listing_comments", "client_comments"}
        ):
            terms.extend(field.get("default_aliases", []))
            terms.extend(field.get("legacy_aliases", []))
        normalized = list(dict.fromkeys(
            term.strip().lower() for term in terms if isinstance(term, str) and term.strip()
        ))
        if normalized:
            groups.append(normalized)

    for header, config in column_config["customFields"].items():
        if config.get("mode") in {"accept_only", "note", "skip"}:
            terms = [header.strip().lower()]
            terms.extend(_custom_field_paraphrase_terms(header))
            groups.append(list(dict.fromkeys(terms)))

    return groups


_CUSTOM_FIELD_STOPWORDS = {
    "a", "an", "and", "for", "in", "of", "on", "or", "the", "to", "with",
}
_CUSTOM_FIELD_GENERIC_TOKENS = {
    "column", "columns", "comment", "comments", "detail", "details", "field",
    "fields", "info", "information", "note", "notes",
}


def _stem_custom_field_token(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return f"{token[:-3]}y"
    if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _custom_field_paraphrase_terms(header: str) -> List[str]:
    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", (header or "").lower())
        if token not in _CUSTOM_FIELD_STOPWORDS
        and token not in _CUSTOM_FIELD_GENERIC_TOKENS
    ]
    if len(tokens) < 2:
        return []

    raw_phrase = " ".join(tokens)
    stemmed_phrase = " ".join(_stem_custom_field_token(token) for token in tokens)
    return list(dict.fromkeys((raw_phrase, stemmed_phrase)))


_FIELD_REQUEST_INTENT_RE = re.compile(
    r"(?:"
    r"\b(?:ask|request|need)\b"
    r"|\bplease\b"
    r"|^\s*(?:send|share|provide|confirm|attach|include|supply|forward)\b"
    r"|\b(?:can|could|would|will|may)\s+(?:i|you|we|they)\b"
    r"|^\s*any\s+chance\s+(?:that\s+)?(?:i|you|we|they)\s+can\b"
    r"|^\s*do\s+you\s+know\b"
    r"|^\s*would\s+it\s+be\s+possible\s+to\b"
    r"|^\s*i\s+would\s+appreciate\b"
    r"|^\s*(?:is|are|was|were)\b"
    r"|^\s*(?:do|does|did)\s+(?:you|we|they)\s+have\b"
    r"|^\s*what\s+about\b"
    r"|\b(?:i\s+am|we\s+are|the\s+client\s+is|our\s+team\s+is)\s+interested\s+in\b"
    r")",
    re.IGNORECASE,
)


def contains_column_field_term(text: str, term: str) -> bool:
    """Match a configured field term as words, never inside another word."""
    normalized = (term or "").strip()
    if not normalized:
        return False
    pattern = re.escape(normalized).replace(r"\ ", r"\s+")
    return bool(re.search(
        rf"(?<![A-Za-z0-9]){pattern}(?![A-Za-z0-9])",
        text or "",
        re.IGNORECASE,
    ))


def response_requests_nonrequestable_fields(
    response_body: str,
    column_config: Optional[dict],
) -> bool:
    """Return True when request language targets a configured Note/Skip field."""
    body = (response_body or "").strip()
    if not body:
        return False
    if get_column_config_error(column_config):
        return True

    request_clauses = [
        clause
        for clause in re.split(r"[\n.!?;,]+", body)
        if _FIELD_REQUEST_INTENT_RE.search(clause)
    ]
    return any(
        contains_column_field_term(clause, term)
        for terms in get_non_requestable_field_terms(column_config)
        for term in terms
        for clause in request_clauses
    )


def detect_column_mapping(headers: List[str], use_ai: bool = True) -> Dict[str, Any]:
    """
    Detect column mappings from sheet headers.

    Args:
        headers: List of column header strings from the sheet
        use_ai: If True, uses AI for semantic matching. If False, uses simple alias matching.

    Returns:
        {
            "mappings": {"canonical_name": "actual_column_name", ...},
            "confidence": {"canonical_name": 0.95, ...},
            "unmapped": ["column1", "column2"],  # Headers we couldn't map
            "requiredFields": [...],
            "formulaFields": [...],
        }
    """
    # Normalize headers for comparison
    normalized_headers = {h.strip().lower(): h for h in headers if h}

    mappings = {}
    confidence = {}
    mapped_headers = set()

    # First pass: exact alias matching
    for canonical, field in CANONICAL_FIELDS.items():
        for alias in get_field_aliases(canonical):
            alias_norm = alias.strip().lower()
            if alias_norm in normalized_headers:
                actual_header = normalized_headers[alias_norm]
                if actual_header not in mapped_headers:
                    mappings[canonical] = actual_header
                    confidence[canonical] = 1.0  # Exact match
                    mapped_headers.add(actual_header)
                    break

    # Second pass: AI semantic matching for remaining headers (if enabled)
    if use_ai:
        unmapped_headers = [h for h in headers if h and h not in mapped_headers]
        unmapped_canonicals = [c for c in CANONICAL_FIELDS if c not in mappings]

        if unmapped_headers and unmapped_canonicals:
            ai_mappings = _ai_match_columns(unmapped_headers, unmapped_canonicals)
            for canonical, (header, conf) in ai_mappings.items():
                if header not in mapped_headers:
                    mappings[canonical] = header
                    confidence[canonical] = conf
                    mapped_headers.add(header)

    # Identify unmapped headers
    unmapped = [h for h in headers if h and h not in mapped_headers]

    # Build extractionFields - all extractable canonical fields that were mapped
    extraction_fields = [
        f for f in EXTRACTABLE_FIELDS
        if f in mappings and f not in FORMULA_FIELDS
    ]

    return {
        "mappings": mappings,
        "confidence": confidence,
        "unmapped": unmapped,
        "extractionFields": extraction_fields,
        "requiredFields": [f for f in REQUIRED_FOR_CLOSE if f in mappings],
        "formulaFields": [f for f in FORMULA_FIELDS if f in mappings],
        "neverRequest": [f for f in NEVER_REQUEST_FIELDS if f in mappings],
    }


def _ai_match_columns(headers: List[str], canonicals: List[str]) -> Dict[str, tuple]:
    """
    Use AI to semantically match remaining headers to canonical fields.
    Returns: {"canonical": ("header", confidence), ...}
    """
    try:
        from .clients import client  # OpenAI client

        # Build context about canonical fields
        field_descriptions = []
        for c in canonicals:
            field = CANONICAL_FIELDS[c]
            desc = f"- {c}: {field['description']}"
            if field.get("ai_synonyms"):
                desc += f" (synonyms: {', '.join(field['ai_synonyms'])})"
            field_descriptions.append(desc)

        prompt = f"""Given these sheet column headers that haven't been mapped yet:
{json.dumps(headers)}

And these canonical fields we're looking for:
{chr(10).join(field_descriptions)}

Match each canonical field to the most appropriate header (if any match).
Return JSON: {{"canonical_name": {{"header": "matched_header", "confidence": 0.85}}, ...}}
Only include matches you're confident about (>0.7). Skip fields with no good match.
"""

        response = client.responses.create(
            model="gpt-4o-mini",  # Fast model for simple matching
            input=[{"role": "user", "content": prompt}],
            temperature=0.1
        )

        raw = response.output_text.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:-1])

        result = json.loads(raw)
        return {k: (v["header"], v["confidence"]) for k, v in result.items()}

    except Exception as e:
        print(f"AI column matching failed: {e}")
        return {}


def build_column_rules_prompt(column_config: Dict[str, Any]) -> str:
    """
    Build the COLUMN_RULES section of the AI prompt dynamically
    based on the client's column configuration.

    Supports both canonical fields and custom fields.
    """
    mappings = column_config.get("mappings", {})
    custom_fields = column_config.get("customFields", {})
    required_fields = column_config.get("requiredFields", DEFAULT_REQUIRED_FOR_CLOSE)
    never_request = column_config.get("neverRequest", NEVER_REQUEST_FIELDS)

    lines = ["COLUMN SEMANTICS & MAPPING (use EXACT header names from this sheet):"]

    # Process canonical fields
    for canonical, actual_col in mappings.items():
        if canonical not in CANONICAL_FIELDS:
            continue

        field = CANONICAL_FIELDS[canonical]

        # Skip non-extractable fields (unless they have a formula warning)
        if not field.get("extractable") and not field.get("is_formula"):
            continue

        # Build the rule line
        if field.get("is_formula"):
            lines.append(f'- "{actual_col}": DO NOT WRITE TO THIS COLUMN. It contains a formula.')
        elif canonical in never_request:
            # Render the extraction hints alongside the never-request rule so the
            # model still knows HOW to recognize/normalize a value it is allowed to
            # accept. Dropping the hints here read as de-emphasis and caused
            # PDF-sourced asking rent to be silently skipped (FIX-17 / M35).
            hints = field.get("extraction_hints") or field["description"]
            lines.append(f'- "{actual_col}": {hints} Accept if provided but NEVER request.')
        else:
            # `or` (not `.get(key, default)`): several CANONICAL_FIELDS set
            # extraction_hints to None explicitly, so a key-present-but-None value
            # must still fall back to the description instead of emitting "None"
            # into the prompt (CodeRabbit PR#15 — matches the never-request branch).
            hints = field.get("extraction_hints") or field["description"]
            synonyms = field.get("ai_synonyms", [])
            required_marker = " [REQUIRED]" if canonical in required_fields else ""
            if synonyms:
                lines.append(f'- "{actual_col}"{required_marker}: {hints} Synonyms: {", ".join(synonyms)}.')
            else:
                lines.append(f'- "{actual_col}"{required_marker}: {hints}')

    # Process custom fields (user-defined columns)
    if custom_fields:
        lines.append("")
        lines.append("CUSTOM FIELDS (client-specific):")
        for col_header, config in custom_fields.items():
            mode = config.get("mode", "skip")
            description = config.get("description", "Extract any relevant value for this field")

            if mode == "skip":
                continue  # Don't include skipped fields
            elif mode == "accept_only":
                lines.append(f'- "{col_header}": {description}. Accept if provided but NEVER request.')
            elif mode in ("ask_required", "ask_optional"):
                required_marker = " [REQUIRED]" if mode == "ask_required" else ""
                lines.append(f'- "{col_header}"{required_marker}: {description}')
            elif mode == "note":
                lines.append(f'- "{col_header}": Append any relevant contextual notes about {description}.')

    # Add formatting rules
    lines.append("")
    lines.append("FORMATTING:")
    lines.append('- For money/area fields, output plain decimals (no "$", "SF", commas). Examples: "30", "14.29", "2400".')
    lines.append('- For square footage, output just the number: "2000" not "2000 SF".')
    lines.append('- For ceiling height, output just the number: "24" not "24 feet".')
    lines.append('- For drive-ins/docks, output just the number: "3" not "3 doors".')
    lines.append('- For power, output the electrical specification as provided: "200A", "480V", "100A 3-phase".')
    lines.append("")
    lines.append("CRITICAL - ALLOWED COLUMNS ONLY:")
    lines.append("- You may ONLY propose updates to columns listed above in COLUMN SEMANTICS (including CUSTOM FIELDS if present).")
    lines.append("- DO NOT update: Property Address, City, Property Name, Leasing Company, Leasing Contact, Email, or any other column not listed above.")
    lines.append("- These fields contain pre-existing client data that should NEVER be changed based on email content.")
    lines.append("- Even if someone signs their email differently than the Leasing Contact field, DO NOT change it.")

    return "\n".join(lines)


def get_required_fields_for_close(column_config: Dict[str, Any]) -> List[str]:
    """
    Get the list of required fields for closing a conversation,
    translated to actual column names.

    Includes both canonical required fields and custom required fields.
    """
    mappings = column_config.get("mappings", {})
    custom_fields = column_config.get("customFields", {})
    required_canonicals = column_config.get("requiredFields", DEFAULT_REQUIRED_FOR_CLOSE)

    # Canonical fields translated to actual column names
    required = [mappings[c] for c in required_canonicals if c in mappings]

    # Custom fields with mode "ask_required"
    for col_header, config in custom_fields.items():
        if config.get("mode") == "ask_required":
            required.append(col_header)

    return required


def get_all_extractable_columns(column_config: Dict[str, Any]) -> List[str]:
    """
    Get all columns that the AI can extract values for.

    Includes canonical extractable fields + custom ask/accept fields.
    """
    mappings = column_config.get("mappings", {})
    custom_fields = column_config.get("customFields", {})

    # Canonical extractable fields
    extractable = []
    for canonical, actual_col in mappings.items():
        if canonical in CANONICAL_FIELDS:
            field = CANONICAL_FIELDS[canonical]
            if field.get("extractable") and not field.get("is_formula"):
                extractable.append(actual_col)

    # Custom fields that are extractable
    for col_header, config in custom_fields.items():
        mode = config.get("mode", "skip")
        if mode in ("ask_required", "ask_optional", "accept_only"):
            extractable.append(col_header)

    return extractable


def translate_canonical_to_actual(canonical_name: str, column_config: Dict[str, Any]) -> Optional[str]:
    """Translate a canonical field name to the actual column name."""
    return column_config.get("mappings", {}).get(canonical_name)


def translate_actual_to_canonical(actual_name: str, column_config: Dict[str, Any]) -> Optional[str]:
    """Translate an actual column name to its canonical field name."""
    mappings = column_config.get("mappings", {})
    for canonical, actual in mappings.items():
        if actual.lower().strip() == actual_name.lower().strip():
            return canonical
    return None
