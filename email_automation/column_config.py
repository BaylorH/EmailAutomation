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
    if mapped - set(CANONICAL_FIELDS):
        return "columnConfig.mappings contains unknown canonical fields"
    canonical_formulas = {
        canonical
        for canonical in mapped
        if CANONICAL_FIELDS.get(canonical, {}).get("is_formula")
    }

    if not required <= extraction:
        return "columnConfig.requiredFields must be included in extractionFields"
    if not never_request <= extraction:
        return "columnConfig.neverRequest must be included in extractionFields"
    if not canonical_formulas <= formulas or (canonical_formulas & extraction):
        return "columnConfig canonical formula fields must remain formula-only"
    if (required & never_request) or (required & formulas):
        return "columnConfig required fields cannot be Note or formula fields"
    if not (extraction | formulas) <= mapped:
        return "columnConfig configured fields must have mappings"

    return None


def _canonical_field_reference_terms(
    canonical: str,
    configured_header: Optional[str],
) -> List[str]:
    """Return the configured name and supported aliases for one canonical field."""
    field = CANONICAL_FIELDS.get(canonical, {})
    terms = [
        configured_header,
        field.get("label"),
        *field.get("default_aliases", []),
        *field.get("legacy_aliases", []),
        *field.get("ai_synonyms", []),
    ]
    normalized_terms = list(dict.fromkeys(
        term.strip().lower()
        for term in terms
        if isinstance(term, str) and term.strip()
    ))
    reference_terms = []
    for term in normalized_terms:
        reference_terms.append(term)
        base_term = re.sub(
            r"\s*(?:/|\bper\s+)(?:sf|sq\.?\s*ft\.?|yr|year|mo|month)\s*$",
            "",
            term,
            flags=re.IGNORECASE,
        ).strip()
        if (
            base_term != term
            and len(re.findall(r"[A-Za-z0-9$]+", base_term)) >= 2
        ):
            reference_terms.append(base_term)
    return list(dict.fromkeys(reference_terms))


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
        | {
            canonical
            for canonical in mappings
            if get_default_mode_for_canonical(canonical) == "skip"
        }
        | ({"listing_comments", "client_comments"} & set(mappings))
    )

    groups = []
    for canonical in non_requestable:
        normalized = _canonical_field_reference_terms(
            canonical,
            mappings.get(canonical),
        )
        if normalized:
            groups.append(normalized)

    for header, config in column_config["customFields"].items():
        if config.get("mode") in {"accept_only", "note", "skip"}:
            terms = [header.strip().lower()]
            terms.extend(_custom_field_paraphrase_terms(header))
            groups.append(list(dict.fromkeys(terms)))

    return groups


def _contextual_skip_field_term_groups(
    column_config: Dict[str, Any],
) -> List[List[str]]:
    """Return default Skip identity groups that can also occur as context."""
    formula_fields = set(column_config["formulaFields"])
    return [
        _canonical_field_reference_terms(canonical, configured_header)
        for canonical, configured_header in column_config["mappings"].items()
        if canonical not in formula_fields
        and get_default_mode_for_canonical(canonical) == "skip"
    ]


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
    r"|^\s*let\s+(?:me|us|you|him|her|them)\s+know\b"
    r"|^\s*(?:i|we|you|they|he|she)(?:\s+would|['\u2019]d)"
    r"\s+like\s+to\s+know\b"
    r"|^\s*(?:is|are|was|were)\b"
    r"|^\s*what\s+(?:is|are|was|were)\b"
    r"|^\s*how\s+many\b"
    r"|^\s*(?:do|does|did)\s+(?:you|we|they)\s+have\b"
    r"|^\s*(?:do|does|did)\s+(?!not\b)"
    r"(?:(?:the|this|that|these|those|your|our|their)\s+)?"
    r"(?:[a-z][a-z'-]*\s+){1,3}have\b"
    r"|^\s*what\s+about\b"
    r"|\b(?:i\s+am|we\s+are|the\s+client\s+is|our\s+team\s+is)\s+interested\s+in\b"
    r")",
    re.IGNORECASE,
)

_FIELD_NEGATED_REQUEST_INTENT_RE = re.compile(
    r"\b(?:"
    r"no\s+need"
    r"|(?:please\s+)?(?:"
    r"do(?:es)?\s+not"
    r"|don['\u2019]t"
    r"|doesn['\u2019]t"
    r"|not"
    r")\s+(?:need|request|ask)"
    r")"
    r"(?:\s+to\s+(?:ask|request|confirm))?\b",
    re.IGNORECASE,
)
_FIELD_REQUEST_SF_UNIT_PREFIX_RE = re.compile(
    r"(?:/|\bper\s+)\s*$",
    re.IGNORECASE,
)
_FIELD_REFERENCE_TOKEN_RE = re.compile(r"[A-Za-z0-9$]+")
_FIELD_TERM_SEPARATOR_PATTERN = (
    r"(?:"
    r"[^\S\r\n]*[-\u00ad\u2010\u2011\u2013\u2014./_'\u2019]+"
    r"[^\S\r\n]*(?:\r?\n[^\S\r\n]*)?"
    r"|[^\S\r\n]*\r?\n[^\S\r\n]*"
    r"|[^\S\r\n]+"
    r")"
)
_FIELD_MENTION_CONTEXT_BOUNDARY_RE = re.compile(
    r"(?<!\d)\.|\.(?!\d)|[!?;,:]+|\r?\n+|[\u2013\u2014]+|\s+-\s+|\bbut\b"
    r"|(?<=[A-Za-z])-(?=[A-Za-z])"
    r"|(?:,\s*|\b(?:and|or)\s+)(?=(?:"
    r"i|we|you|they|he|she|it|can|could|would|will|may|"
    r"is|are|do|does|did|what|how|please|kindly|tell|let"
    r")\b)",
    re.IGNORECASE,
)
_FIELD_URL_RE = re.compile(
    r"\bhttps?://[^\s<>()]*[A-Za-z0-9/#]",
    re.IGNORECASE,
)
_FIELD_CLEAR_ACKNOWLEDGEMENT_RE = re.compile(
    r"(?:"
    r"\b(?:thanks|thank\s+you)\s+for\s+"
    r"(?:confirming|providing|sending|sharing)\b"
    r"|\bplease\s+note(?:\s+that)?\b"
    r"|\b(?:i|we|you|they|he|she)\s+already\s+"
    r"(?:have|had|received|confirmed|provided)\b"
    r"|\balready\s+"
    r"(?:confirmed|provided|received|sent|included|attached|shared|forwarded|supplied)\b"
    r")",
    re.IGNORECASE,
)
_FIELD_CLEAR_INFORMATIONAL_RE = re.compile(
    r"(?:"
    r"\b(?:happy|glad)\s+to\s+(?:send|share|provide|forward)\b"
    r"|\bhere(?:\s+(?:is|are)|['\u2019]s)\b"
    r"|^\s*(?:i|we)\s+know\b"
    r"|^\s*(?:i|we)\s+could\s+(?:get|send|share|provide|forward)\b"
    r"|^\s*it\s+would\s+be\s+possible\s+to\s+"
    r"(?:send|share|provide|forward)\b"
    r"|^\s*(?:i|we)\s+appreciate\b"
    r")",
    re.IGNORECASE,
)
_FIELD_CLEAR_ACKNOWLEDGEMENT_SUFFIX_RE = re.compile(
    r"^\s+(?:(?:is|are|was|were|has|have|had)\s+(?:already\s+)?"
    r"(?:been\s+)?)?"
    r"(?:confirmed|provided|received|sent|included|attached|shared|forwarded|supplied)\b",
    re.IGNORECASE,
)
_FIELD_CLEAR_NEGATED_SUFFIX_RE = re.compile(
    r"^\s+(?:(?:is|are|was|were)\s+not|(?:isn|aren|wasn|weren)['\u2019]t)\s+"
    r"(?:needed|required|requested)\b",
    re.IGNORECASE,
)
_FIELD_CLEAR_FACTUAL_PREFIX_RE = re.compile(
    r"(?:"
    r"^\s*there\s+(?:is|are|was|were)\b"
    r"|^\s*(?:"
    r"(?:(?:the|this|that|these|those|our|your|their)\s+)"
    r"(?:[a-z][a-z'-]*\s+){1,3}"
    r"|[a-z][a-z'-]*\s+"
    r")(?:do|does|did)\s+have\s*$"
    r")",
    re.IGNORECASE,
)
_FIELD_CLEAR_FACTUAL_VALUE_SUFFIX_RE = re.compile(
    r"^\s*(?::|(?:is|are|was|were|equals?|runs?)\b)\s*"
    r"(?:[$€£]?\s*\d|yes\b|no\b|none\b|unknown\b|available\b|unavailable\b)",
    re.IGNORECASE,
)
_FIELD_DIRECT_MARKER_BRIDGE_RE = re.compile(
    r"\s*(?:(?:about|over)\s+)?"
    r"(?:(?:the|this|that|these|those|our|your|their)\s+)?",
    re.IGNORECASE,
)
_FIELD_REQUEST_EXCEPTION_RE = re.compile(r"\b(?:just|only)\b", re.IGNORECASE)
_FIELD_FACTUAL_VALUE_CONTEXT_RE = re.compile(
    r"\s*(?:"
    r"[$€£]?\s*\d[\d,.]*"
    r"(?:\s*(?:/|\bper\s+)\s*(?:sf|sq\.?\s*ft\.?|yr|year|mo|month))*"
    r"|yes\b|no\b|none\b|unknown\b|available\b|unavailable\b"
    r")",
    re.IGNORECASE,
)
_FIELD_CONTEXT_DECLARATIVE_RE = re.compile(
    r"^\s*(?:"
    r"(?:the|this|that|these|those|it|i|we|you|they|he|she)\b"
    r".*\b(?:is|are|was|were|has|have|had|do|does|did|looks?|seems?|appears?)\b"
    r"|(?:that|it|this)['\u2019]s\b"
    r")",
    re.IGNORECASE,
)
_FIELD_CONTEXT_DEONTIC_RE = re.compile(
    r"\b(?:have|has|had|am|is|are|was|were)\s+to\b",
    re.IGNORECASE,
)
_FIELD_PLURAL_ANAPHOR_RE = re.compile(
    r"\b(?:both|these|those|them)(?:\s+values?)?\b",
    re.IGNORECASE,
)
_FIELD_SINGULAR_ANAPHOR_RE = re.compile(
    r"\b(?:that|this|it)\b",
    re.IGNORECASE,
)
_FIELD_LIST_BULLET_RE = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+")

_FIELD_MENTION_REQUEST = "request"
_FIELD_MENTION_BENIGN = "benign"
_FIELD_MENTION_UNKNOWN = "unknown"


def _field_mention_contexts(text: str, mention_spans: List[tuple]) -> List[tuple]:
    """Return intent contexts without splitting inside a discovered field span."""
    contexts = []
    context_start = 0
    protected_spans = [
        *mention_spans,
        *((match.start(), match.end()) for match in _FIELD_URL_RE.finditer(text)),
    ]
    for boundary in _FIELD_MENTION_CONTEXT_BOUNDARY_RE.finditer(text):
        if any(
            start < boundary.end() and boundary.start() < end
            for start, end in protected_spans
        ):
            continue
        if text[context_start:boundary.start()].strip():
            contexts.append((
                context_start,
                boundary.start(),
                "?" in boundary.group(0),
            ))
        context_start = boundary.end()

    if text[context_start:].strip():
        contexts.append((context_start, len(text), False))
    return contexts


def _column_field_term_matches(
    text: str,
    term: str,
    *,
    disambiguate_sf: bool = False,
) -> List[Any]:
    """Return word-bounded matches for a field term, excluding unit-only SF."""
    normalized = (term or "").strip()
    if not normalized:
        return []
    parts = _FIELD_REFERENCE_TOKEN_RE.findall(normalized)
    if not parts:
        return []
    token_patterns = []
    for part in parts:
        token = part.lower()
        if token in _CUSTOM_FIELD_GENERIC_TOKENS:
            token_patterns.append(re.escape(token))
        elif token in {"foot", "feet"}:
            token_patterns.append(r"(?:foot|feet)")
        elif len(token) > 4 and token.endswith("ies"):
            token_patterns.append(
                rf"(?:{re.escape(token[:-3])}y|{re.escape(token)})"
            )
        elif len(token) > 3 and re.search(r"[^aeiou]y$", token):
            token_patterns.append(rf"{re.escape(token[:-1])}(?:y|ies)")
        elif len(token) > 2 and token.endswith("s") and not token.endswith("ss"):
            token_patterns.append(rf"{re.escape(token[:-1])}s?")
        elif len(token) > 3 and token not in {"sq", "sf", "ft", "yr", "mo"}:
            token_patterns.append(rf"{re.escape(token)}s?")
        else:
            token_patterns.append(re.escape(token))
    pattern = _FIELD_TERM_SEPARATOR_PATTERN.join(token_patterns)
    if (
        len(parts) >= 2
        and all(part.isalpha() and len(part) <= 3 for part in parts)
    ):
        pattern += r"(?:\.(?=[ \t]+(?-i:[a-z])))?"
    matches = list(re.finditer(
        rf"(?<![A-Za-z0-9]){pattern}(?![A-Za-z0-9])",
        text or "",
        re.IGNORECASE,
    ))
    normalized_reference = " ".join(parts).lower()
    if not disambiguate_sf or normalized_reference not in {
        "sf",
        "sq ft",
        "square foot",
        "square feet",
    }:
        return matches
    return [
        match
        for match in matches
        if not _FIELD_REQUEST_SF_UNIT_PREFIX_RE.search((text or "")[:match.start()])
    ]


def contains_column_field_term(text: str, term: str) -> bool:
    """Match a configured field term as words, never inside another word."""
    return bool(_column_field_term_matches(text, term))


def _field_group_match_spans(text: str, terms: List[str]) -> List[tuple]:
    """Return maximal, de-duplicated spans for aliases of one configured field."""
    spans = {
        (match.start(), match.end())
        for term in terms
        for match in _column_field_term_matches(text, term, disambiguate_sf=True)
    }
    maximal_spans = []
    for start, end in sorted(spans, key=lambda span: (span[0] - span[1], span[0])):
        if any(
            kept_start <= start and end <= kept_end
            for kept_start, kept_end in maximal_spans
        ):
            continue
        maximal_spans.append((start, end))
    return sorted(maximal_spans)


def _configured_field_mentions(text: str, field_groups: List[tuple]) -> List[tuple]:
    """Find configured fields, with Note/Skip/formula spans taking precedence."""
    raw_mentions = {
        (start, end, group_index)
        for group_index, (_kind, _header, terms) in enumerate(field_groups)
        for start, end in _field_group_match_spans(text, terms)
    }
    nonrequestable_spans = [
        (start, end)
        for start, end, group_index in raw_mentions
        if field_groups[group_index][0] != "ask"
    ]
    mentions = []
    for start, end, group_index in sorted(
        raw_mentions,
        key=lambda mention: (mention[0] - mention[1], mention[0], mention[2]),
    ):
        kind = field_groups[group_index][0]
        if kind == "ask" and any(
            start < nonrequestable_end and nonrequestable_start < end
            for nonrequestable_start, nonrequestable_end in nonrequestable_spans
        ):
            continue
        if any(
            kept_start <= start
            and end <= kept_end
            and (kept_start, kept_end) != (start, end)
            and (
                field_groups[kept_group][0] == kind
                or (
                    field_groups[kept_group][0] != "ask"
                    and kind != "ask"
                )
            )
            for kept_start, kept_end, kept_group in mentions
        ):
            continue
        mentions.append((start, end, group_index))
    return sorted(mentions)


def _last_pattern_match(pattern: re.Pattern, text: str) -> Optional[Any]:
    return next(reversed(list(pattern.finditer(text))), None)


def _last_direct_prefix_match(pattern: re.Pattern, prefix: str) -> Optional[Any]:
    for match in reversed(list(pattern.finditer(prefix))):
        if _FIELD_DIRECT_MARKER_BRIDGE_RE.fullmatch(prefix[match.end():]):
            return match
    return None


def _context_index_for_mention(
    mention_start: int,
    mention_end: int,
    contexts: List[tuple],
) -> Optional[int]:
    for index, (context_start, context_end, _is_question) in enumerate(contexts):
        if context_start <= mention_start and mention_end <= context_end:
            return index
    return None


def _context_contains_field_mention(
    context_index: int,
    mentions: List[tuple],
    contexts: List[tuple],
) -> bool:
    context_start, context_end, _is_question = contexts[context_index]
    return any(
        context_start <= mention_start and mention_end <= context_end
        for mention_start, mention_end, _group_index in mentions
    )


def _field_groups_in_context(
    context_index: int,
    mentions: List[tuple],
    contexts: List[tuple],
) -> set:
    context_start, context_end, _is_question = contexts[context_index]
    return {
        group_index
        for mention_start, mention_end, group_index in mentions
        if context_start <= mention_start and mention_end <= context_end
    }


def _is_adjacent_context_separator(separator: str) -> bool:
    normalized = (separator or "").replace("\r\n", "\n")
    if re.search(r"\n[ \t]*\n", normalized):
        return False
    return bool(re.fullmatch(
        r"(?:[ \t.!?;,:\-\u2013\u2014]*"
        r"(?:\n[ \t]*)?|[ \t]*(?:and|or)[ \t]*)",
        normalized,
        re.IGNORECASE,
    ))


def _is_compound_context_separator(separator: str) -> bool:
    normalized = (separator or "").replace("\r\n", "\n")
    if re.search(r"\n[ \t]*\n", normalized) or "." in normalized:
        return False
    return bool(re.fullmatch(
        r"[ \t,;:]*(?:(?:and|or)\b[ \t,;:]*)?(?:\n[ \t,;:]*)?",
        normalized,
        re.IGNORECASE,
    ))


def _request_anaphor_number(context_text: str) -> Optional[str]:
    if _FIELD_PLURAL_ANAPHOR_RE.search(context_text):
        return "plural"
    if _FIELD_SINGULAR_ANAPHOR_RE.search(context_text):
        return "singular"
    return None


def _is_recent_field_proposition_separator(separator: str) -> bool:
    """Allow sentence punctuation, but never a paragraph boundary."""
    normalized = (separator or "").replace("\r\n", "\n")
    if re.search(r"\n[ \t]*\n", normalized):
        return False
    return bool(re.fullmatch(
        r"[ \t.!?;,:\-\u2013\u2014]*"
        r"(?:(?:and|or)\b[ \t.!?;,:\-\u2013\u2014]*)?"
        r"(?:\n[ \t.!?;,:\-\u2013\u2014]*)?",
        normalized,
        re.IGNORECASE,
    ))


def _is_bounded_singular_hop_separator(separator: str) -> bool:
    """Allow one explicit-pronoun hop over a single blank line."""
    normalized = (separator or "").replace("\r\n", "\n")
    return (
        normalized.count("\n") <= 2
        and len(normalized) <= 16
        and re.fullmatch(r"[ \t.!?;,:\-\u2013\u2014\n]*", normalized) is not None
    )


def _context_is_factual_value(
    text: str,
    context_index: int,
    mentions: List[tuple],
    contexts: List[tuple],
) -> bool:
    if _context_contains_field_mention(context_index, mentions, contexts):
        return False
    context_start, context_end, is_question = contexts[context_index]
    return (
        not is_question
        and _FIELD_FACTUAL_VALUE_CONTEXT_RE.fullmatch(
            text[context_start:context_end]
        ) is not None
    )


def _fieldless_context_is_proven_benign(
    context_text: str,
    is_question: bool,
) -> bool:
    if is_question:
        return False
    if re.fullmatch(r"\s*(?:correct|right)\s*", context_text, re.IGNORECASE):
        return True
    if _FIELD_URL_RE.fullmatch(context_text.strip()) is not None:
        return True
    if _FIELD_FACTUAL_VALUE_CONTEXT_RE.fullmatch(context_text) is not None:
        return True
    if any(pattern.search(context_text) for pattern in (
        _FIELD_NEGATED_REQUEST_INTENT_RE,
        _FIELD_CLEAR_ACKNOWLEDGEMENT_RE,
        _FIELD_CLEAR_INFORMATIONAL_RE,
    )):
        return True
    if _FIELD_CONTEXT_DEONTIC_RE.search(context_text) is not None:
        return False
    return _FIELD_CONTEXT_DECLARATIVE_RE.search(context_text) is not None


def _fieldless_context_is_request_like(
    context_text: str,
    is_question: bool,
) -> bool:
    if is_question:
        return True
    please_matches = list(re.finditer(r"\bplease\b", context_text, re.IGNORECASE))
    clear_matches = [
        match
        for pattern in (
            _FIELD_NEGATED_REQUEST_INTENT_RE,
            _FIELD_CLEAR_ACKNOWLEDGEMENT_RE,
            _FIELD_CLEAR_INFORMATIONAL_RE,
        )
        for match in pattern.finditer(context_text)
    ]
    if any(
        not any(clear.start() <= request.start() < clear.end() for clear in clear_matches)
        for request in please_matches
    ):
        return True
    if _fieldless_context_is_proven_benign(context_text, is_question):
        return False
    return bool(context_text.strip())


def _field_context_before_factual_value(
    text: str,
    value_context_index: int,
    mentions: List[tuple],
    contexts: List[tuple],
) -> Optional[int]:
    if value_context_index <= 0 or not _context_is_factual_value(
        text,
        value_context_index,
        mentions,
        contexts,
    ):
        return None
    field_context_index = value_context_index - 1
    if not _context_contains_field_mention(
        field_context_index,
        mentions,
        contexts,
    ):
        return None
    field_end = contexts[field_context_index][1]
    value_start = contexts[value_context_index][0]
    if not re.fullmatch(r"\s*:\s*", text[field_end:value_start]):
        return None
    return field_context_index


def _antecedent_field_group_indices(
    text: str,
    request_context_index: int,
    mentions: List[tuple],
    contexts: List[tuple],
) -> set:
    """Return the complete local field proposition governed by a follow-up."""
    request_start, request_end, _is_question = contexts[request_context_index]
    request_text = text[request_start:request_end]
    anaphor_number = _request_anaphor_number(request_text)
    anchor_field_index = None

    if request_context_index > 0:
        previous_index = request_context_index - 1
        previous_end = contexts[previous_index][1]
        separator = text[previous_end:request_start]
        if (
            _is_adjacent_context_separator(separator)
            or (
                anaphor_number == "singular"
                and _is_bounded_singular_hop_separator(separator)
            )
        ):
            if _context_contains_field_mention(
                previous_index,
                mentions,
                contexts,
            ):
                anchor_field_index = previous_index
            else:
                anchor_field_index = _field_context_before_factual_value(
                    text,
                    previous_index,
                    mentions,
                    contexts,
                )

    if (
        anchor_field_index is None
        and request_context_index > 0
        and _FIELD_FACTUAL_VALUE_CONTEXT_RE.match(request_text) is not None
    ):
        previous_index = request_context_index - 1
        previous_end = contexts[previous_index][1]
        if (
            _context_contains_field_mention(previous_index, mentions, contexts)
            and re.fullmatch(r"\s*:\s*", text[previous_end:request_start])
        ):
            anchor_field_index = previous_index

    if anchor_field_index is None:
        return set()

    antecedent_groups = _field_groups_in_context(
        anchor_field_index,
        mentions,
        contexts,
    )
    cursor = anchor_field_index
    sentence_hops = 0
    while cursor > 0:
        previous_index = cursor - 1
        previous_end = contexts[previous_index][1]
        cursor_start = contexts[cursor][0]
        separator = text[previous_end:cursor_start]
        is_compound = _is_compound_context_separator(separator)
        is_recent_plural = (
            anaphor_number == "plural"
            and _is_recent_field_proposition_separator(separator)
        )
        if not (is_compound or is_recent_plural):
            break
        if is_recent_plural and "." in separator:
            if sentence_hops >= 1:
                break
            sentence_hops += 1

        previous_field_index = None
        if _context_contains_field_mention(previous_index, mentions, contexts):
            previous_field_index = previous_index
        else:
            previous_field_index = _field_context_before_factual_value(
                text,
                previous_index,
                mentions,
                contexts,
            )
        if previous_field_index is None:
            break
        antecedent_groups.update(_field_groups_in_context(
            previous_field_index,
            mentions,
            contexts,
        ))
        cursor = previous_field_index
    return antecedent_groups


def _structural_followup_request_groups(
    text: str,
    mentions: List[tuple],
    contexts: List[tuple],
    field_groups: List[tuple],
) -> set:
    request_groups = set()
    for context_index, (context_start, context_end, is_question) in enumerate(contexts):
        context_mentions = [
            mention
            for mention in mentions
            if context_start <= mention[0] and mention[1] <= context_end
        ]
        if context_mentions:
            if any(
                field_groups[group_index][0] != "contextual_nonrequestable"
                for _mention_start, _mention_end, group_index in context_mentions
            ):
                continue
            if any(
                _contextual_skip_mention_is_request(
                    text,
                    mention,
                    mentions,
                    contexts,
                )
                for mention in context_mentions
            ):
                continue
        context_text = text[context_start:context_end]
        line_start = text.rfind("\n", 0, context_start) + 1
        line_end = text.find("\n", context_end)
        if line_end < 0:
            line_end = len(text)
        if (
            _line_is_request_list_leadin(text[line_start:line_end])
            and _request_leadin_has_forward_field_cluster(
                text,
                line_end,
                mentions,
            )
        ):
            continue
        if not _fieldless_context_is_request_like(context_text, is_question):
            continue
        antecedent_groups = _antecedent_field_group_indices(
            text,
            context_index,
            mentions,
            contexts,
        )
        for group_index in antecedent_groups:
            if field_groups[group_index][0] != "contextual_nonrequestable":
                request_groups.add(group_index)
                continue
            prior_mentions = [
                (mention_start, mention_end)
                for mention_start, mention_end, mention_group in mentions
                if mention_group == group_index and mention_end <= context_start
            ]
            if not prior_mentions:
                continue
            mention_start, mention_end = max(
                prior_mentions,
                key=lambda span: span[1],
            )
            if len(_FIELD_REFERENCE_TOKEN_RE.findall(
                text[mention_start:mention_end]
            )) >= 2:
                request_groups.add(group_index)
    return request_groups


def _explicit_request_intent_match(value: str) -> Optional[Any]:
    if not (value or "").strip():
        return None
    request_text = _FIELD_NEGATED_REQUEST_INTENT_RE.sub(
        lambda match: " " * (match.end() - match.start()),
        value,
    )
    request_match = _last_pattern_match(_FIELD_REQUEST_INTENT_RE, request_text)
    if request_match is None:
        return None
    clear_matches = [
        match
        for pattern in (
            _FIELD_CLEAR_ACKNOWLEDGEMENT_RE,
            _FIELD_CLEAR_INFORMATIONAL_RE,
        )
        for match in pattern.finditer(value)
    ]
    if any(
        clear.start() <= request_match.start() < clear.end()
        for clear in clear_matches
    ):
        return None
    return request_match


def _line_is_request_list_leadin(line: str) -> bool:
    return _explicit_request_intent_match(line) is not None


def _line_is_list_heading(line: str) -> bool:
    stripped = (line or "").strip()
    if not stripped or stripped.endswith(":"):
        return bool(stripped)
    if re.search(r"[.!?;]$", stripped):
        return False
    tokens = re.findall(r"[A-Za-z0-9]+", stripped)
    if not 1 <= len(tokens) <= 6:
        return False
    return not any(
        token.lower() in {
            "is", "are", "was", "were", "has", "have", "had",
            "do", "does", "did", "can", "could", "would", "will",
        }
        for token in tokens
    )


def _request_leadin_has_forward_field_cluster(
    text: str,
    start: int,
    mentions: List[tuple],
) -> bool:
    """Return whether the next bounded non-heading line names a field."""
    offset = start
    tail = text[start:]
    if tail.startswith("\r\n"):
        offset += 2
        tail = tail[2:]
    elif tail.startswith("\n"):
        offset += 1
        tail = tail[1:]
    for raw_line in tail.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        line_start = offset
        line_end = line_start + len(line)
        offset += len(raw_line)
        stripped = line.strip()
        line_has_field = any(
            mention_start < line_end and line_start < mention_end
            for mention_start, mention_end, _group_index in mentions
        )
        if not stripped:
            continue
        if line_has_field and (
            _FIELD_LIST_BULLET_RE.match(line) is not None
            or not stripped.endswith(":")
        ):
            return True
        if _line_is_list_heading(line):
            continue
        return False
    return False


def _request_list_field_group_indices(
    text: str,
    mentions: List[tuple],
) -> set:
    """Propagate an explicit lead-in through its bounded field cluster."""
    request_groups = set()
    pending = False
    active = False
    active_is_bulleted = False
    active_after_blank = False
    offset = 0

    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        line_start = offset
        line_end = line_start + len(line)
        offset += len(raw_line)
        stripped = line.strip()
        is_bullet = _FIELD_LIST_BULLET_RE.match(line) is not None
        line_groups = {
            group_index
            for mention_start, mention_end, group_index in mentions
            if mention_start < line_end and line_start < mention_end
        }
        line_mention_starts = [
            mention_start
            for mention_start, mention_end, _group_index in mentions
            if mention_start < line_end and line_start < mention_end
        ]
        starts_with_field = bool(line_mention_starts) and not text[
            line_start:min(line_mention_starts)
        ].strip()

        if active:
            if not stripped:
                if active_is_bulleted:
                    active_after_blank = True
                else:
                    active = False
                    pending = False
                continue
            if active_after_blank and not is_bullet:
                active = False
                pending = False
                active_after_blank = False
            elif line_groups and (is_bullet or starts_with_field):
                request_groups.update(line_groups)
                active_is_bulleted = active_is_bulleted or is_bullet
                active_after_blank = False
                continue
            elif is_bullet:
                active_after_blank = False
                continue
            else:
                active = False
                pending = False
                active_after_blank = False

        if _line_is_request_list_leadin(line):
            last_mention_end = max(
                (
                    mention_end
                    for mention_start, mention_end, _group_index in mentions
                    if mention_start < line_end and line_start < mention_end
                ),
                default=None,
            )
            open_field_cluster_tail = (
                last_mention_end is not None
                and re.match(
                    r"\s*,?\s*(?:and|or)\b\s+\S+",
                    text[last_mention_end:line_end],
                    re.IGNORECASE,
                ) is not None
            )
            if not line_groups or open_field_cluster_tail:
                pending = True
            continue

        if not pending:
            continue
        if not stripped:
            continue
        if (
            _line_is_list_heading(line)
            and not is_bullet
            and (not line_groups or stripped.endswith(":"))
        ):
            continue
        if line_groups:
            request_groups.update(line_groups)
            active = True
            active_is_bulleted = is_bullet
            active_after_blank = False
            continue
        pending = False

    return request_groups


def _inline_request_field_group_indices(
    text: str,
    mentions: List[tuple],
) -> set:
    """Propagate `request: field, field` within one bounded sentence."""
    request_groups = set()
    offset = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        match = _explicit_request_intent_match(line)
        if match is None:
            offset += len(raw_line)
            continue
        scope_start = line.find(":", match.end())
        if scope_start < 0 or re.search(r"[.!?;]", line[match.end():scope_start]):
            offset += len(raw_line)
            continue
        scope_start += 1
        scope_end = len(line)
        for boundary in re.finditer(r"(?<!\d)\.|\.(?!\d)|[!?]", line[scope_start:]):
            boundary_start = scope_start + boundary.start()
            absolute_boundary = offset + boundary_start
            if any(
                mention_start < absolute_boundary < mention_end
                for mention_start, mention_end, _group_index in mentions
            ):
                continue
            scope_end = boundary_start
            break
        absolute_start = offset + scope_start
        absolute_end = offset + scope_end
        request_groups.update(
            group_index
            for mention_start, mention_end, group_index in mentions
            if absolute_start <= mention_start and mention_end <= absolute_end
        )
        offset += len(raw_line)
    return request_groups


def _has_immediately_following_factual_value(
    text: str,
    mention_end: int,
    mentions: List[tuple],
    contexts: List[tuple],
    context_index: int,
) -> bool:
    _context_start, context_end, _is_question = contexts[context_index]
    if text[mention_end:context_end].strip() or context_index + 1 >= len(contexts):
        return False

    next_start, next_end, next_is_question = contexts[context_index + 1]
    if next_is_question or not re.fullmatch(r"\s*:\s*", text[context_end:next_start]):
        return False
    if any(
        next_start <= other_start and other_end <= next_end
        for other_start, other_end, _other_group in mentions
    ):
        return False
    return bool(_FIELD_FACTUAL_VALUE_CONTEXT_RE.match(text[next_start:next_end]))


def _contextual_skip_mention_is_request(
    text: str,
    mention: tuple,
    mentions: List[tuple],
    contexts: List[tuple],
) -> bool:
    """Require a default-Skip identity mention to be a direct field target."""
    mention_start, mention_end, _group_index = mention
    context_index = _context_index_for_mention(
        mention_start,
        mention_end,
        contexts,
    )
    if context_index is None:
        return False
    context_start, context_end, is_question = contexts[context_index]
    other_mentions = sorted({
        (other_start, other_end)
        for other_start, other_end, _other_group in mentions
        if context_start <= other_start
        and other_end <= context_end
        and (other_start, other_end) != (mention_start, mention_end)
    })
    coordinated = any(
        re.fullmatch(
            r"\s*(?:,|and\b|or\b)\s*(?:the\s+)?",
            text[left_end:right_start],
            re.IGNORECASE,
        ) is not None
        for left_end, right_start in (
            *(
                (other_end, mention_start)
                for other_start, other_end in other_mentions
                if other_end <= mention_start
            ),
            *(
                (mention_end, other_start)
                for other_start, other_end in other_mentions
                if mention_end <= other_start
            ),
        )
    )
    context_text = text[context_start:context_end]
    explicit_request = _explicit_request_intent_match(context_text)
    request_context = (
        is_question
        or (
            explicit_request is not None
            and _FIELD_CONTEXT_DECLARATIVE_RE.search(context_text) is None
        )
    )
    if coordinated:
        return request_context
    if other_mentions:
        return False
    if re.fullmatch(
        r"\s*(?:(?:please|again)\s*)?",
        text[mention_end:context_end],
        re.IGNORECASE,
    ) is None:
        return False
    if is_question:
        return True
    return (
        explicit_request is not None
        and explicit_request.end() <= mention_start - context_start
        and _FIELD_CONTEXT_DECLARATIVE_RE.search(context_text) is None
    )


def _classify_field_mention(
    text: str,
    mention: tuple,
    mentions: List[tuple],
    contexts: List[tuple],
    field_groups: List[tuple],
) -> str:
    """Classify one configured-field mention; uncertain mentions remain UNKNOWN."""
    mention_start, mention_end, _group_index = mention
    context_index = _context_index_for_mention(
        mention_start,
        mention_end,
        contexts,
    )
    if context_index is None:
        return _FIELD_MENTION_UNKNOWN

    context_start, context_end, is_question = contexts[context_index]
    prefix = text[context_start:mention_start]
    suffix = text[mention_end:context_end]
    if field_groups[_group_index][0] == "contextual_nonrequestable":
        return (
            _FIELD_MENTION_REQUEST
            if _contextual_skip_mention_is_request(
                text,
                mention,
                mentions,
                contexts,
            )
            else _FIELD_MENTION_BENIGN
        )
    direct_clear_matches = [
        match
        for match in (
            _last_direct_prefix_match(_FIELD_NEGATED_REQUEST_INTENT_RE, prefix),
            _last_direct_prefix_match(_FIELD_CLEAR_ACKNOWLEDGEMENT_RE, prefix),
            _last_direct_prefix_match(_FIELD_CLEAR_INFORMATIONAL_RE, prefix),
        )
        if match is not None
    ]
    last_direct_clear = max(
        direct_clear_matches,
        key=lambda match: match.start(),
        default=None,
    )
    intent_prefix = _FIELD_NEGATED_REQUEST_INTENT_RE.sub(
        lambda match: " " * (match.end() - match.start()),
        prefix,
    )
    last_request = _last_pattern_match(
        _FIELD_REQUEST_INTENT_RE,
        intent_prefix,
    )
    last_request_exception = _last_pattern_match(
        _FIELD_REQUEST_EXCEPTION_RE,
        intent_prefix,
    )

    if is_question:
        return _FIELD_MENTION_REQUEST
    request_is_inside_clear = (
        last_request is not None
        and last_direct_clear is not None
        and last_direct_clear.start() <= last_request.start() < last_direct_clear.end()
    )
    if last_request is not None and not request_is_inside_clear:
        return _FIELD_MENTION_REQUEST
    suffix_intent = _FIELD_NEGATED_REQUEST_INTENT_RE.sub(
        lambda match: " " * (match.end() - match.start()),
        suffix,
    )
    if re.search(r"\bplease\b", suffix_intent, re.IGNORECASE):
        return _FIELD_MENTION_REQUEST
    if last_direct_clear is not None:
        return _FIELD_MENTION_BENIGN
    if _FIELD_CLEAR_NEGATED_SUFFIX_RE.search(suffix):
        return _FIELD_MENTION_BENIGN
    if _FIELD_CLEAR_ACKNOWLEDGEMENT_SUFFIX_RE.search(suffix):
        return _FIELD_MENTION_BENIGN
    if _FIELD_CLEAR_FACTUAL_VALUE_SUFFIX_RE.search(suffix):
        return _FIELD_MENTION_BENIGN
    if _has_immediately_following_factual_value(
        text,
        mention_end,
        mentions,
        contexts,
        context_index,
    ):
        return _FIELD_MENTION_BENIGN
    has_prior_field = any(
        context_start <= other_start
        and other_end <= mention_start
        and (other_start, other_end) != (mention_start, mention_end)
        for other_start, other_end, _other_group in mentions
    )
    if not has_prior_field and _FIELD_CLEAR_FACTUAL_PREFIX_RE.search(prefix):
        return _FIELD_MENTION_BENIGN
    if last_request_exception is not None:
        return _FIELD_MENTION_REQUEST
    return _FIELD_MENTION_UNKNOWN


def _requestable_field_groups(column_config: dict) -> List[tuple]:
    mappings = column_config["mappings"]
    extraction = set(column_config["extractionFields"])
    nonrequestable = (
        set(column_config["neverRequest"])
        | set(column_config["formulaFields"])
        | {"listing_comments", "client_comments"}
    )
    groups = []

    for canonical, configured_header in mappings.items():
        field = CANONICAL_FIELDS.get(canonical, {})
        if (
            canonical not in extraction
            or canonical in nonrequestable
            or not field.get("extractable")
        ):
            continue

        groups.append((
            configured_header,
            _canonical_field_reference_terms(canonical, configured_header),
        ))

    for header, config in column_config["customFields"].items():
        if config.get("mode") not in {"ask_required", "ask_optional"}:
            continue
        terms = [header.strip().lower(), *_custom_field_paraphrase_terms(header)]
        groups.append((header, list(dict.fromkeys(terms))))

    return groups


def _classify_configured_field_requests(
    response_body: str,
    column_config: dict,
) -> tuple:
    """Return requested Ask headers and whether any nonrequestable field is requested."""
    ask_groups = _requestable_field_groups(column_config)
    field_groups = [
        ("ask", header, terms)
        for header, terms in ask_groups
    ]
    contextual_skip_groups = {
        frozenset(terms)
        for terms in _contextual_skip_field_term_groups(column_config)
    }
    field_groups.extend(
        (
            "contextual_nonrequestable"
            if frozenset(terms) in contextual_skip_groups
            else "nonrequestable",
            None,
            terms,
        )
        for terms in get_non_requestable_field_terms(column_config)
    )
    mentions = _configured_field_mentions(response_body, field_groups)
    mention_spans = list({(start, end) for start, end, _group in mentions})
    contexts = _field_mention_contexts(response_body, mention_spans)
    request_like_groups = {
        group_index
        for mention in mentions
        for group_index in (mention[2],)
        if _classify_field_mention(
            response_body,
            mention,
            mentions,
            contexts,
            field_groups,
        ) != _FIELD_MENTION_BENIGN
    }
    request_like_groups.update(_structural_followup_request_groups(
        response_body,
        mentions,
        contexts,
        field_groups,
    ))
    request_like_groups.update(_request_list_field_group_indices(
        response_body,
        mentions,
    ))
    request_like_groups.update(_inline_request_field_group_indices(
        response_body,
        mentions,
    ))

    requested = []
    requested_headers = set()
    for group_index, (kind, header, _terms) in enumerate(field_groups):
        if kind != "ask" or group_index not in request_like_groups:
            continue
        normalized_header = _normalized_column_name(header)
        if normalized_header in requested_headers:
            continue
        requested.append(header)
        requested_headers.add(normalized_header)

    requests_nonrequestable = any(
        kind != "ask" and group_index in request_like_groups
        for group_index, (kind, _header, _terms) in enumerate(field_groups)
    )
    return requested, requests_nonrequestable


def get_requested_ask_fields(
    response_body: str,
    column_config: Optional[dict],
) -> List[str]:
    """Return configured Ask headers targeted by request-like field mentions."""
    body = (response_body or "").strip()
    if not body or get_column_config_error(column_config):
        return []
    requested, _requests_nonrequestable = _classify_configured_field_requests(
        body,
        column_config,
    )
    return requested


def response_requests_nonrequestable_fields(
    response_body: str,
    column_config: Optional[dict],
) -> bool:
    """Return True when request-like language targets a Note, Skip, or formula field."""
    body = (response_body or "").strip()
    if not body:
        return False
    if get_column_config_error(column_config):
        return True

    _requested, requests_nonrequestable = _classify_configured_field_requests(
        body,
        column_config,
    )
    return requests_nonrequestable


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
