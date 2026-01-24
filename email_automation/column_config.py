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
from typing import Dict, List, Optional, Any

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
        "never_request": True,  # We accept it if provided but never ask for it
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
        "default_aliases": ["listing brokers comments", "listing brokers comments ", "broker comments", "comments", "notes", "broker notes"],
        "extraction_hints": None,  # Use 'notes' field in AI output instead
        "format": "text",
        "extractable": False,  # AI writes to 'notes' field, which gets appended here
        "append_mode": True,  # Don't overwrite, append with separator
    },
    "flyer_link": {
        "label": "Flyer / Link",
        "description": "Links to flyers or listings",
        "required_for_matching": False,
        "default_aliases": ["flyer / link", "flyer/link", "flyer", "link", "links", "brochure", "listing link"],
        "extraction_hints": "URLs to property flyers or listings",
        "format": "url",
        "extractable": True,
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
        "label": "Jill and Clients comments",
        "description": "Internal client notes",
        "required_for_matching": False,
        "default_aliases": ["jill and clients comments", "client comments", "internal notes", "our comments"],
        "extraction_hints": None,  # Internal use only
        "format": "text",
        "extractable": False,
    },
}

# Fields required for conversation to be considered "complete"
REQUIRED_FOR_CLOSE = [k for k, v in CANONICAL_FIELDS.items() if v.get("required_for_close")]

# Fields that AI can extract from conversations
EXTRACTABLE_FIELDS = [k for k, v in CANONICAL_FIELDS.items() if v.get("extractable")]

# Fields that should never be written (formula columns)
FORMULA_FIELDS = [k for k, v in CANONICAL_FIELDS.items() if v.get("is_formula")]

# Fields we accept but never request
NEVER_REQUEST_FIELDS = [k for k, v in CANONICAL_FIELDS.items() if v.get("never_request")]


def get_default_column_config() -> Dict[str, Any]:
    """
    Returns default column configuration using standard aliases.
    This is used when a client doesn't have custom column mappings.
    """
    return {
        "mappings": {
            canonical: field["default_aliases"][0]  # Use first alias as default
            for canonical, field in CANONICAL_FIELDS.items()
        },
        "requiredFields": REQUIRED_FOR_CLOSE,
        "formulaFields": FORMULA_FIELDS,
        "neverRequest": NEVER_REQUEST_FIELDS,
    }


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
        for alias in field.get("default_aliases", []):
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

    return {
        "mappings": mappings,
        "confidence": confidence,
        "unmapped": unmapped,
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
    """
    mappings = column_config.get("mappings", {})

    lines = ["COLUMN SEMANTICS & MAPPING (use EXACT header names from this sheet):"]

    for canonical, actual_col in mappings.items():
        if canonical not in CANONICAL_FIELDS:
            continue

        field = CANONICAL_FIELDS[canonical]

        # Skip non-extractable fields
        if not field.get("extractable") and not field.get("is_formula"):
            continue

        # Build the rule line
        if field.get("is_formula"):
            lines.append(f'- "{actual_col}": DO NOT WRITE TO THIS COLUMN. It contains a formula.')
        elif field.get("never_request"):
            lines.append(f'- "{actual_col}": {field["description"]}. Accept if provided but NEVER request.')
        else:
            hints = field.get("extraction_hints", field["description"])
            synonyms = field.get("ai_synonyms", [])
            if synonyms:
                lines.append(f'- "{actual_col}": {hints} Synonyms: {", ".join(synonyms)}.')
            else:
                lines.append(f'- "{actual_col}": {hints}')

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
    lines.append("- You may ONLY propose updates to columns listed above in COLUMN SEMANTICS.")
    lines.append("- DO NOT update: Property Address, City, Property Name, Leasing Company, Leasing Contact, Email, or any other column not listed above.")
    lines.append("- These fields contain pre-existing client data that should NEVER be changed based on email content.")
    lines.append("- Even if someone signs their email differently than the Leasing Contact field, DO NOT change it.")

    return "\n".join(lines)


def get_required_fields_for_close(column_config: Dict[str, Any]) -> List[str]:
    """
    Get the list of required fields for closing a conversation,
    translated to actual column names.
    """
    mappings = column_config.get("mappings", {})
    required_canonicals = column_config.get("requiredFields", REQUIRED_FOR_CLOSE)

    return [mappings[c] for c in required_canonicals if c in mappings]


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
