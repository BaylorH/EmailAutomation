import csv
import json
import hashlib
import ipaddress
import logging
import math
import re
import socket
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import unquote, urlsplit, urlunsplit
from google.cloud.firestore import SERVER_TIMESTAMP
from .clients import client, _sheets_client, _fs
from .automation_runtime import ai_for
from .messaging import build_conversation_payload
from .sheets import _header_index_map, _get_first_tab_title, _col_letter, _execute_with_retry
from .column_config import (
    CANONICAL_FIELDS,
    build_column_rules_prompt,
    canonical_field_for_column,
    get_required_fields_for_close,
    get_column_config_error,
    find_notes_comment_column_index,
    REQUIRED_FOR_CLOSE,
    coerce_sheet_value_for_column,
    is_asset_column_name,
    sheet_values_equal_for_column,
)
from .notification_payloads import sanitize_new_property_referral_response
from .openai_usage import track_openai_usage_safely
from . import file_handling as _file_handling
from .file_handling import project_safe_native_image_manifest
from .property_images import STREET_SUFFIX_TOKENS
from .tour_scheduling import (
    TOUR_INTENT_COURTESY,
    classify_tour_intent,
    extract_proposed_tour_options,
    looks_like_tour_scheduling_reply,
    looks_like_tour_only_unavailable,
    subject_bound_tour_segments,
)
from .outbound_safety import find_unresolved_placeholders

logger = logging.getLogger(__name__)

REQUIRED_FIELDS_FOR_CLOSE = [CANONICAL_FIELDS[field]["label"] for field in REQUIRED_FOR_CLOSE]


def _find_header_name(header: List[str], target: str) -> Optional[str]:
    target_key = (target or "").strip().lower()
    for column in header:
        if (column or "").strip().lower() == target_key:
            return column
    return None


# Data placeholders a broker (or the model) may emit in lieu of a real value.
# These are NOT data and must never be written verbatim into a client sheet cell.
_DATA_PLACEHOLDER_VALUES = frozenset({
    "tbd", "t.b.d", "t.b.d.", "tba", "t.b.a", "t.b.a.", "tbc", "t.b.c", "t.b.c.",
    "n/a", "na", "n.a.", "n.a", "n/a.", "none", "null",
    "pending", "unknown", "unk", "?", "-", "--",
    "to follow", "to be determined", "to be confirmed", "to be advised",
    "to be provided", "to be verified", "to come",
})

# "ask <the> landlord/broker/owner/agent/pm" — a deferral, not a value.
_ASK_SOMEONE_RE = re.compile(
    r"^ask\s+(?:the\s+|our\s+|their\s+)?(?:landlord|landl|broker|owner|agent|pm|property\s+manager|seller|lessor)",
    re.IGNORECASE,
)


def _is_placeholder_data_value(value: str) -> bool:
    """True if ``value`` is a data placeholder (TBD / N/A / pending / TBC /
    'To follow' / 'ask landlord' ...) rather than an actual extracted value.

    Matching is on the whole trimmed value (case- and trailing-punctuation-
    insensitive) so a legitimate value that merely CONTAINS one of these tokens
    (e.g. an address 'Pending Ave') is never falsely suppressed.
    """
    if value is None:
        return False
    norm = str(value).strip().lower()
    # Strip a single trailing sentence punctuation ('N/A.', 'TBD!') before matching.
    stripped = norm.rstrip(".!")
    if norm in _DATA_PLACEHOLDER_VALUES or stripped in _DATA_PLACEHOLDER_VALUES:
        return True
    return bool(_ASK_SOMEONE_RE.match(norm))


def _proposal_updates_column(proposal: dict, column_name: str) -> bool:
    target_key = (column_name or "").strip().lower()
    for update in (proposal or {}).get("updates", []) or []:
        if (update.get("column") or "").strip().lower() == target_key:
            return True
    return False


def _proposal_update_for_column(proposal: dict, column_name: str) -> Optional[dict]:
    target_key = (column_name or "").strip().lower()
    for update in (proposal or {}).get("updates", []) or []:
        if (update.get("column") or "").strip().lower() == target_key:
            return update
    return None


def _row_value_for_column(rowvals: List[str], header: List[str], column_name: str) -> str:
    idx_map = _header_index_map(header)
    key = (column_name or "").strip().lower()
    if key not in idx_map:
        return ""
    idx = idx_map[key] - 1
    return rowvals[idx] if idx < len(rowvals) else ""


def _strip_quoted_history(text: str) -> str:
    """Return only the newest message body, dropping quoted reply history.

    Broker replies routinely quote the prior thread ('> On Jul 1 I wrote: ...' or
    an 'On Mon, Broker wrote:' attribution line followed by '>'-prefixed lines).
    Pattern guards must judge only the NEW message, otherwise a fresh positive
    reply that quotes an old rejection re-triggers non-viable/unavailable events.
    """
    if not text:
        return ""
    kept: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            break
        if re.match(r"^on\b.*\bwrote\s*:", stripped, re.IGNORECASE):
            break
        if re.match(r"^-{2,}\s*original message\s*-{2,}", stripped, re.IGNORECASE):
            break
        # Gmail/Outlook/Apple forwarded-message dividers. Their absence let a
        # forwarded PM note about a DIFFERENT property be scanned as live text and
        # terminalize the target row (A′ finding M25, a verbatim catalog trigger).
        if re.match(r"^-{2,}\s*forwarded message\s*-{2,}", stripped, re.IGNORECASE):
            break
        if re.match(r"^begin\s+forwarded\s+message\s*:?", stripped, re.IGNORECASE):
            break
        kept.append(line)
    result = "\n".join(kept).strip()
    # If the message was entirely quoted, fall back to the raw text so a genuinely
    # new-but-unusually-formatted reply is not lost.
    return result or text.strip()


_HONORIFICS = {"dr", "mr", "mrs", "ms", "prof", "sir", "madam", "mx"}
# Tokens that signal a company / org name rather than a person, so a greeting must
# fall back to neutral rather than "Hi <Company>," (A′ misread M31).
_COMPANY_TOKENS = {
    "international", "inc", "inc.", "llc", "l.l.c.", "corp", "corp.", "corporation",
    "company", "co", "co.", "group", "realty", "associates", "partners", "properties",
    "commercial", "industrial", "advisors", "advisory", "capital", "holdings",
    "cbre", "colliers", "jll", "cushman", "wakefield", "savills", "newmark",
}


def _resolve_greeting_first_name(
    contact_name: Optional[str],
    sender_email: Optional[str] = None,
    sender_signature_name: Optional[str] = None,
) -> Optional[str]:
    """Resolve a usable, human first name for greetings (A′ FIX-13 / FIX-14).

    - Strips honorifics ("Dr. Angela ..." -> "Angela").
    - Returns None (=> neutral greeting) for company names ("Colliers International").
    - Reconciles the mapped name against the LIVE sender (from-address local part or
      signature). On disagreement it returns None so the model greets neutrally
      rather than dead-naming the stale mapped person into a different inbox.
    """
    raw = str(contact_name or "").strip()
    if not raw or "@" in raw:
        return None

    tokens = [t for t in re.split(r"\s+", raw) if t]
    lowered_tokens = [t.lower().strip(".,") for t in tokens]

    # Company name -> neutral greeting.
    if any(tok in _COMPANY_TOKENS for tok in lowered_tokens):
        return None

    # Strip leading honorifics.
    name_tokens = list(tokens)
    while name_tokens and name_tokens[0].lower().strip(".,") in _HONORIFICS:
        name_tokens.pop(0)
    if not name_tokens:
        return None

    first = name_tokens[0].strip(".,")
    if not first or not re.search(r"[a-zA-Z]", first):
        return None

    # Reconcile against the live sender identity.
    sender_local = str(sender_email or "").split("@", 1)[0]
    sig = str(sender_signature_name or "")
    compact_first = re.sub(r"[^a-z]", "", first.lower())
    compact_local = re.sub(r"[^a-z]", "", sender_local.lower())
    compact_sig = re.sub(r"[^a-z]", "", sig.lower())
    if sender_local or sig:
        agrees = False
        if compact_first and compact_local and (
            compact_first in compact_local or compact_local.startswith(compact_first[:4] or compact_first)
        ):
            agrees = True
        if compact_first and compact_sig and compact_first in compact_sig:
            agrees = True
        # Also agree when the mapped LAST name shows up in the sender identity.
        for tok in name_tokens[1:]:
            c = re.sub(r"[^a-z]", "", tok.lower())
            if c and (c in compact_local or c in compact_sig):
                agrees = True
                break
        if not agrees:
            return None

    return first


def _raw_latest_inbound(conversation: List[dict]) -> str:
    """Return the UNstripped body of the newest inbound message (quotes intact)."""
    for message in reversed(conversation or []):
        if (message.get("direction") or "").lower() == "inbound":
            return message.get("content") or message.get("body") or message.get("preview") or ""
    return ""


def _quoted_region(raw_text: str) -> str:
    """Return only the QUOTED portion of a message body (reply history).

    A line is quoted when it is '>'-prefixed, OR it sits below a standalone
    forwarded/original-message divider (Outlook/Gmail bottom-quote convention
    where the original is appended verbatim without '>' prefixes). An inline
    '> On ... wrote:' attribution does NOT swallow bottom-posted new text — only
    the '>'-prefixed lines themselves are treated as quoted, so a broker who
    types fresh content below an inline quote is not misread as quoting.
    """
    if not raw_text:
        return ""
    quoted: List[str] = []
    after_divider = False
    for line in raw_text.splitlines():
        stripped = line.strip()
        if after_divider:
            quoted.append(line)
            continue
        if stripped.startswith(">"):
            quoted.append(line)
            continue
        if re.match(r"^-{2,}\s*original message\s*-{2,}", stripped, re.IGNORECASE) or \
           re.match(r"^-{2,}\s*forwarded message\s*-{2,}", stripped, re.IGNORECASE) or \
           re.match(r"^begin\s+forwarded\s+message\s*:?", stripped, re.IGNORECASE):
            after_divider = True
            continue
    return "\n".join(quoted)


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _significant_words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9']{4,}", (text or "").lower())}


def _event_evidence_only_in_quote(event: dict, newest_text: str, quoted_region: str) -> bool:
    """True when the event's supporting text (notes/question) lives ONLY in the
    quoted reply history and NOT in the newest human-authored segment.

    This is the belt to FIX-08's suspender: even if the model reads a quoted
    rejection/opt-out/referral as live signal, we strip the event when its own
    evidence is quote-exclusive (A′ misreads M02, M05, M09, M16, M17, M21, M27).
    """
    quoted_norm = _norm_ws(quoted_region)
    newest_norm = _norm_ws(newest_text)
    if not quoted_norm:
        return False

    question = _norm_ws(event.get("question"))
    if question and len(question) >= 12 and question in quoted_norm and question not in newest_norm:
        return True

    candidate = " ".join(
        str(event.get(field) or "") for field in ("question", "notes", "address")
    )
    words = _significant_words(candidate)
    if not words:
        return False
    quoted_words = _significant_words(quoted_region)
    newest_words = _significant_words(newest_text)
    quote_exclusive = {w for w in words if w in quoted_words and w not in newest_words}
    return len(quote_exclusive) >= 2


def _latest_inbound_text(conversation: List[dict]) -> str:
    for message in reversed(conversation or []):
        if (message.get("direction") or "").lower() == "inbound":
            raw = message.get("content") or message.get("body") or message.get("preview") or ""
            return _strip_quoted_history(raw)
    return ""


def _looks_like_access_remediation(text: str) -> bool:
    latest_text = _strip_quoted_history(text or "").lower()
    if not latest_text:
        return False

    access_re = re.compile(
        r"\b(?:dock|door|opening|drive[-\s]?in|grade[-\s]?level|access)\b"
    )
    remediation_re = re.compile(
        r"\b(?:rampable|convertible|ramp(?:ed|ing)?|convert(?:ed|ing)?|"
        r"modify|modified|add(?:ed|ing)?|install(?:ed|ing)?)\b"
    )
    capability_re = re.compile(
        r"\b(?:can|could|may|might|possible\s+to|able\s+to|"
        r"(?:owner|landlord|seller)\s+(?:can|could|will|would)|will)\b"
    )
    negated_before_re = re.compile(
        r"\b(?:not|never|cannot|can[’']?t|could\s+not|couldn[’']?t|"
        r"will\s+not|won[’']?t|unable\s+to|not\s+able\s+to)\s+"
        r"(?:be\s+)?$"
    )
    negated_after_re = re.compile(
        r"^\s*(?:is\s+|would\s+be\s+)?(?:not\s+possible|impossible|prohibited)\b"
    )

    for clause in re.split(r"(?<=[.!?;])\s+|\n+", latest_text):
        if not access_re.search(clause):
            continue
        for match in remediation_re.finditer(clause):
            before = clause[max(0, match.start() - 55):match.start()]
            after = clause[match.end():match.end() + 35]
            if negated_before_re.search(before) or negated_after_re.search(after):
                continue
            term = match.group(0).lower()
            if term in {"rampable", "convertible"} or capability_re.search(before):
                return True
    return False


def _looks_like_requirements_mismatch_nonviable(text: str) -> bool:
    """Detect broker replies saying the property fails the client's physical
    requirements (office-heavy, not a true warehouse, no drive-in / grade-level
    access, clear/ceiling height below spec, warehouse requirement unmet).

    A single clear physical non-fit reason is enough to flag the property
    non-viable; two independent mismatches are not required. Quoted reply history
    is stripped first so an old rejection re-quoted under a new positive reply
    does not fire.
    """
    latest_text = _strip_quoted_history(text or "").lower()
    if not latest_text:
        return False

    # --- explicit "not a (good/right) fit for the client" style rejections ---
    fit_rejection = bool(
        re.search(
            r"\b(?:won[’']?t|wont|would\s*n[’']?t|will\s+not|is\s+not|isn[’']?t|"
            r"are\s+not|aren[’']?t|does\s+not|doesn[’']?t)\s+(?:be\s+)?(?:a\s+|the\s+)?"
            r"(?:good\s+|right\s+)?fit\b",
            latest_text,
        )
        or re.search(r"\bnot\s+(?:a\s+|the\s+)?(?:good\s+|right\s+)?fit\s+for\s+(?:your|the)\s+client\b", latest_text)
        # Casual / apostrophe-less non-fit phrasings: "not the right fit",
        # "isnt the right fit", "not a good fit" (no trailing "for the client").
        or re.search(r"\b(?:isn[’']?t|is\s+not|not)\s+(?:a\s+|the\s+)?(?:good|right)\s+fit\b", latest_text)
        or re.search(r"\bwon[’']?t\s+work\s+for\s+(?:them|you|your\s+client|the\s+client)\b", latest_text)
        or re.search(r"\bfails?\s+(?:to\s+meet\s+)?(?:your\s+|the\s+)?client(?:['’]?s)?\s+(?:warehouse\s+)?(?:requirements?|needs?|specs?)\b", latest_text)
        or re.search(r"\b(?:does\s+not|doesn[’']?t)\s+(?:meet|satisfy|fit)\s+(?:your\s+|the\s+)?client", latest_text)
    )

    # --- property is too office-oriented for an industrial/warehouse requirement ---
    # Negation-aware: "NOT office-heavy -- it's true warehouse throughout" is a
    # POSITIVE pitch, not a mismatch (A′ misread M06). A negator immediately
    # before the descriptor flips the meaning, so those must not fire.
    office_heavy_positive = False
    for match in re.finditer(r"\boffice[-\s]?heavy\b", latest_text):
        pre = latest_text[max(0, match.start() - 12): match.start()]
        if not re.search(r"\b(?:not|isn'?t|aren'?t|no)\s*$", pre):
            office_heavy_positive = True
            break
    office_mismatch = bool(
        office_heavy_positive
        or re.search(r"\b(?:too|more|mostly|primarily|all)\s+office\b", latest_text)
        or re.search(r"\boffice\s+fit[-\s]?out\b", latest_text)
        or re.search(r"\boffice\s+(?:use\s+)?only\b", latest_text)
    )

    # --- warehouse / industrial space is missing or insufficient ---
    warehouse_mismatch = bool(
        re.search(r"\bnot\s+(?:a\s+)?(?:true|real|proper|actual)\s+warehouse\b", latest_text)
        or re.search(r"\bno\s+(?:true|real|proper)\s+warehouse\b", latest_text)
        or re.search(r"\bnot\s+(?:a\s+)?warehouse\b", latest_text)
        or re.search(r"\bno\s+(?:proper\s+|real\s+|true\s+)?warehouse\s+to\s+speak\s+of\b", latest_text)
        or re.search(r"\blacks?\s+(?:enough\s+|sufficient\s+)?(?:warehouse|industrial)\s+(?:space|area)?\b", latest_text)
        or re.search(r"\bnot\s+(?:enough|sufficient)\s+(?:warehouse|industrial)\b", latest_text)
        or re.search(r"\bwarehouse\s+(?:requirement|requirements|spec|specs|need|needs)\s+(?:remains?\s+|still\s+)?(?:unmet|not\s+met|isn[’']?t\s+met)\b", latest_text)
    )

    # --- required drive-in / grade-level / dock access is absent ---
    negation = (
        r"(?:no|without|lacks?|has\s+no|have\s+no|do\s+not\s+have|does\s+not\s+have|"
        r"don[’']?t\s+have|doesn[’']?t\s+have)"
    )
    access_mismatch = bool(
        re.search(
            negation
            + r"\s+(?:any\s+)?(?:drive[-\s]?ins?|grade[-\s]?level|dock)"
            r"(?:\s+(?:doors?|access|space|loading))?\b",
            latest_text,
        )
    )

    # --- clear / ceiling height below the client's spec ---
    height_term = r"(?:clear\s+height|ceiling\s+height|ceiling\s+clearance|clear\s+ceiling|clearance)"
    below_term = r"(?:below|under|beneath|less\s+than|short\s+of)"
    # "under joist" / "under the roof deck" is the MEASUREMENT reference point for
    # a clear height ("22 ft 9 in under joist"), not a below-spec complaint. A
    # structural member immediately after the below-term flips it back to benign.
    structural_ref = (
        r"(?:the\s+)?(?:bar\s+)?(?:joists?|beams?|deck(?:ing)?|roof(?:\s+deck)?|"
        r"steel|structure|truss(?:es)?|purlins?|canopy|ceiling)\b"
    )
    height_mismatch = bool(
        re.search(
            height_term + r"[^.]{0,45}?\b" + below_term + r"\b(?!\s+" + structural_ref + r")",
            latest_text,
        )
    )

    access_remediation = _looks_like_access_remediation(latest_text)
    physical_mismatch = (
        office_mismatch
        or warehouse_mismatch
        or (access_mismatch and not access_remediation)
        or height_mismatch
    )

    # A generic fit rejection is not terminal when the only stated defect is an
    # access condition the broker says can be remediated. Independent office,
    # warehouse, or height mismatches remain terminal through physical_mismatch.
    return bool((fit_rejection and not access_remediation) or physical_mismatch)


def _looks_like_tour_slot_reply(conversation: List[dict], latest_text: str) -> bool:
    latest = (latest_text or "").lower()
    if not latest:
        return False

    recent_thread_text = "\n".join(
        str((message or {}).get("content") or (message or {}).get("body") or (message or {}).get("preview") or "")
        for message in (conversation or [])[-4:]
    ).lower()
    tour_context = re.search(
        r"\b(?:tour|showing|walk[-\s]?through|tour\s+slot|requested\s+arrival|expected\s+departure)\b",
        f"{recent_thread_text}\n{latest}",
    )
    if not tour_context:
        return False
    return looks_like_tour_scheduling_reply(latest_text)


def _has_tour_scheduling_context(conversation: List[dict]) -> bool:
    """Return true for actual tour-scheduling threads, not generic outreach asking for tour availability."""
    outbound_texts = []
    for message in reversed(conversation or []):
        if (message or {}).get("direction") == "outbound":
            outbound_texts.append(str(
                (message or {}).get("content")
                or (message or {}).get("body")
                or (message or {}).get("preview")
                or ""
            ).lower())

    if not outbound_texts:
        return False

    return any(
        re.search(r"\btour\s+date\b", outbound_text)
        or re.search(r"\brequested\s+arrival\b", outbound_text)
        or re.search(r"\btour\s+slot\b", outbound_text)
        or re.search(r"\bconfirm\s+whether\s+(?:this\s+)?tour\b", outbound_text)
        or re.search(r"\bschedule\s+(?:a\s+)?tour\b", outbound_text)
        or re.search(r"\btour\s+(?:at|on)\s+\d", outbound_text)
        for outbound_text in outbound_texts
    )


# Canonical deterministic terminal-signal list. Each entry is (reason, regex).
# This is the single source of truth the tour_scheduling terminal list is aligned
# to; keep it in sync with processing.PROPERTY_UNAVAILABLE_KEYWORDS.
# "availab(?:le|e)" tolerates the common single-char typo "availabe".
_UNAVAILABLE_PATTERNS = [
    ("no_longer_available", r"\bno\s+longer\s+availab(?:le|e)\b"),
    ("signed_loi", r"\bsigned\s+(?:an?\s+)?(?:loi|letter\s+of\s+intent)\b"),
    ("signed_lease", r"\bsigned\s+(?:a\s+)?lease\b"),
    ("no_longer_represented", r"\bno\s+longer\s+represent(?:s|ed|ing)?\s+(?:this\s+|the\s+)?property\b"),
    ("no_space_available", r"\b(?:no|not\s+any|do(?:es)?\s+not\s+have\s+any)\s+space\s+available\b"),
    ("no_availability", r"\bno\s+availability\b"),
    ("fully_leased", r"\bfully\s+leased\b"),
    ("just_leased", r"\bjust\s+leased\b"),
    ("already_leased", r"\balready\s+leased\b"),
    ("been_leased", r"\bbeen\s+leased\b"),
    ("taken_off_market", r"\btaken\s+off\s+(?:the\s+)?market\b"),
    ("off_market", r"\boff\s+(?:the\s+)?market\b"),
    ("under_contract", r"\bunder\s+contract\b"),
    ("accepted_an_offer", r"\baccepted\s+an?\s+offer\b"),
    # Bare "leased" is terminal too, but only when bound to the TARGET property and
    # not to an ancillary asset ("trailer lot is leased separately") or a comps
    # reference ("what recently leased along the corridor").
    ("leased", r"\bleased\b"),
]

# Positive-viability signals: an explicit statement that the TARGET listing is
# alive. When present, an ambiguous terminal phrase (another building leased, a
# comps reference, an ancillary lease, a slot conflict) must NOT terminalize the
# row (A′ misreads M01, M03, M19, M20, M24).
_VIABILITY_RE = re.compile(
    r"\b(?:still\s+available|remains?\s+available|remains?\s+viable|remains?\s+active|"
    r"remains?\s+open|still\s+active|still\s+on\s+the\s+market|still\s+viable|"
    r"nothing\s+has\s+changed|shows?\s+(?:really\s+)?well|"
    r"(?:is|are)\s+totally\s+fine|totally\s+fine|"
    r"very\s+much\s+(?:still\s+)?available)\b",
    re.IGNORECASE,
)

_VIABILITY_NEGATOR_PATTERN = (
    r"(?:not|never|no\s+longer|hardly|barely|scarcely|"
    r"isn['’]?t|aren['’]?t|wasn['’]?t|weren['’]?t|"
    r"doesn['’]?t|don['’]?t|didn['’]?t|"
    r"hasn['’]?t|haven['’]?t|hadn['’]?t|"
    r"cannot|can['’]?t|couldn['’]?t|wouldn['’]?t|shouldn['’]?t)"
)
_VIABILITY_DIRECT_MODIFIER_PATTERN = r"(?:all|both|currently|necessarily)"
_VIABILITY_QUALIFIER_WORDS = frozenset({
    "anticipated", "expected", "likely", "projected", "scheduled",
})
_VIABILITY_NEGATOR_LINK_WORDS = frozenset({
    "cannot", "longer", "no", "not",
})
_VIABILITY_QUALIFIER_PATTERN = (
    r"(?:expected|likely|anticipated|projected|scheduled)"
)
_VIABILITY_QUALIFIER_AUXILIARY_PATTERN = (
    r"(?:am|is|are|was|were|be|been|being|"
    r"do|does|did|have|has|had|"
    r"can|could|will|would|shall|should|may|might|must|ought|to|"
    r"appear|appears|appeared|seem|seems|seemed)"
)
_VIABILITY_QUALIFIER_BRIDGE_PATTERN = (
    rf"(?:[a-z]+ly|all|both|{_VIABILITY_QUALIFIER_AUXILIARY_PATTERN})"
)
_VIABILITY_QUALIFIED_SCOPE_ATOM_PATTERN = (
    rf"(?:{_VIABILITY_NEGATOR_PATTERN}|{_VIABILITY_QUALIFIER_BRIDGE_PATTERN})"
)
_VIABILITY_DIRECT_SCOPE_ATOM_PATTERN = (
    rf"(?:{_VIABILITY_NEGATOR_PATTERN}|"
    rf"{_VIABILITY_DIRECT_MODIFIER_PATTERN})"
)
_VIABILITY_QUALIFIED_NEGATION_SCOPE_RE = re.compile(
    rf"\b(?P<scope>"
    rf"(?:{_VIABILITY_QUALIFIED_SCOPE_ATOM_PATTERN}\s+){{0,8}}"
    rf"(?:{_VIABILITY_QUALIFIER_PATTERN}|unlikely)\s+"
    rf"(?:{_VIABILITY_QUALIFIED_SCOPE_ATOM_PATTERN}\s+){{0,4}}"
    rf"to\s+)$",
    re.IGNORECASE,
)
_VIABILITY_POST_INFINITIVE_NEGATION_SCOPE_RE = re.compile(
    rf"\b(?P<scope>"
    rf"(?:{_VIABILITY_QUALIFIED_SCOPE_ATOM_PATTERN}\s+){{0,8}}"
    rf"(?:{_VIABILITY_QUALIFIER_PATTERN}|unlikely)\s+to\s+"
    rf"(?:{_VIABILITY_QUALIFIED_SCOPE_ATOM_PATTERN}\s+){{1,4}})$",
    re.IGNORECASE,
)
_VIABILITY_DIRECT_INFINITIVE_NEGATION_SCOPE_RE = re.compile(
    rf"\b(?P<scope>"
    rf"(?:{_VIABILITY_DIRECT_SCOPE_ATOM_PATTERN}\s+){{1,6}}"
    rf"to\s+)$",
    re.IGNORECASE,
)
_VIABILITY_DIRECT_NEGATION_SCOPE_RE = re.compile(
    rf"\b(?P<scope>"
    rf"(?:{_VIABILITY_DIRECT_SCOPE_ATOM_PATTERN}\s+){{1,6}})$",
    re.IGNORECASE,
)
_VIABILITY_NEGATOR_RE = re.compile(
    rf"\b{_VIABILITY_NEGATOR_PATTERN}\b",
    re.IGNORECASE,
)
_VIABILITY_UNLIKELY_WORD_RE = re.compile(
    r"\bunlikely\b",
    re.IGNORECASE,
)


def _viability_lexical_negator_count(text: str) -> int:
    normalized = text or ""
    return sum(
        1 for _match in _VIABILITY_NEGATOR_RE.finditer(normalized)
    ) + sum(
        1 for _match in _VIABILITY_UNLIKELY_WORD_RE.finditer(normalized)
    )


def _viability_prefix_negation_count(prefix: str) -> Optional[int]:
    """Count negators in one bounded direct or qualified viability scope."""
    normalized = prefix or ""
    matched_scope = None
    for scope_pattern in (
        _VIABILITY_QUALIFIED_NEGATION_SCOPE_RE,
        _VIABILITY_POST_INFINITIVE_NEGATION_SCOPE_RE,
        _VIABILITY_DIRECT_INFINITIVE_NEGATION_SCOPE_RE,
        _VIABILITY_DIRECT_NEGATION_SCOPE_RE,
    ):
        scope_match = scope_pattern.search(normalized)
        if scope_match:
            matched_scope = scope_match.group("scope")
            break
    if matched_scope is None:
        return None
    return _viability_lexical_negator_count(matched_scope)


def _viability_prefix_is_lexically_negated(prefix: str) -> bool:
    """Evaluate bounded direct and qualified negators by odd/even parity."""
    negator_count = _viability_prefix_negation_count(prefix)
    return negator_count is not None and negator_count % 2 == 1

# Ancillary / non-target subjects a lease reference may bind to. A lease about one
# of these (or a tour slot/window) is not the property going away (M15, M19, M20).
_ANCILLARY_SUBJECT_RE = re.compile(
    r"\b(?:trailer\s+lot|parking\s+lot|trailer\s+storage|trailer|parking|"
    r"outparcel|out-?lot|yard|corridor|window|slot|appointment)\b",
    re.IGNORECASE,
)

# new_property notes that self-contradict the referral (the model's own notes admit
# the property is not on the market / not a fit / not the target) — reject those
# events post-hoc (A′ misreads M11, M12, M24, M25, M29).
_NEW_PROP_CONTRADICTION_RE = re.compile(
    r"not\s+available|not\s+on\s+offer|isn'?t\s+on\s+offer|not\s+a\s+fit|"
    r"not\s+the\s+target|already\s+leased|fully\s+leased|just\s+leased|"
    r"has\s+been\s+leased|off\s+market|off\s+the\s+market|not\s+what\s+you'?re\s+after|"
    r"won'?t\s+waste|keep\s+it\s+quiet|not\s+on\s+the\s+market|"
    r"relocat|build-?to-?suit|separate\s+client",
    re.IGNORECASE,
)


_REDIRECT_PHRASE_RE = re.compile(
    r"\b(?:my\s+colleague|loop\s+(?:\w+\s+)?in\b|reach\s+out\s+to|"
    r"actually\s+handles?|handles?\s+(?:the|our|all|that)\b|"
    r"is\s+the\s+(?:right|better)\s+(?:person|contact)|"
    r"will\s+be\s+your\s+(?:point\s+of\s+)?contact|"
    r"redirect(?:ing)?\s+you\s+to|forward(?:ing)?\s+(?:you|this)\s+to|"
    r"you\s+(?:should|may\s+want\s+to|can)\s+(?:loop\s+in|contact|reach\s+out\s+to))\b",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# A broker asking for a phone conversation (LIVE break: call_lets_hop). Matches the
# same intent surface as the classifier's call_requested signals but tolerates the
# "hop on a quick call" filler the quote-signal list misses. Used only over the
# broker's FRESH message so quoted prior-thread call asks never re-fire.
_CALL_REQUEST_RE = re.compile(
    r"\bcall\s+me\b|\bgive\s+me\s+a\s+call\b|\bcall\s+you\b|\bphone\s+call\b"
    r"|\bhop\s+on\s+a(?:\s+\w+){0,3}\s+call\b|\bcan\s+(?:you|we)\s+call\b"
    r"|\bcall\s+me\s+at\b|\breach\s+me\s+at\b|\bschedule\s+a\s+call\b"
    r"|\blet'?s\s+call\b|\blet'?s\s+hop\s+on\b|\bset\s+up\s+a\s+call\b"
    # "talk"/"chat"/"speak" are call requests ONLY with explicit phone context —
    # a bare "let's chat about the terms" or "can we talk pricing over email" is
    # an ordinary reply, and forcing call_requested there nulls a valid auto-reply.
    r"|\b(?:talk|chat|speak|connect)\b[^.!?\n]{0,25}\b(?:on|over|by)\s+(?:the\s+)?phone\b"
    r"|\bover\s+the\s+phone\b|\bon\s+a\s+quick\s+call\b",
    re.IGNORECASE,
)


def _looks_like_call_request(text: str) -> bool:
    return bool(text and _CALL_REQUEST_RE.search(text))

_OUT_OF_OFFICE_RE = re.compile(
    r"\bout\s+of\s+(?:the\s+)?office\b"
    r"|\booo\b"
    r"|\bauto(?:mated|matic)?[-\s]?reply\b"
    r"|\bautoreply\b"
    r"|\bon\s+(?:vacation|holiday|leave|pto|sabbatical)\b"
    r"|\b(?:parental|maternity|paternity|medical|sick|annual)\s+leave\b"
    # "limited access" alone is a common property description ("site has limited
    # access after hours"); only an explicit email-access phrase signals OOO.
    r"|\blimited\s+email\s+access\b"
    r"|\blimited\s+access\s+to\s+(?:my\s+)?email\b"
    r"|\baway\s+from\s+(?:my\s+)?(?:email|office|desk)\b",
    # NOTE: bare "back in the office" / "returning to the office" were removed —
    # a live human handoff ("I was traveling, back in the office Monday, in the
    # meantime contact Dana at dana@x.com") is a genuine wrong_contact, not an
    # auto-reply. Real OOO banners still match via the strong markers above
    # (out of office / OOO / automatic reply / on vacation|leave / away from ...).
    re.IGNORECASE,
)


def _looks_like_out_of_office(text: str) -> bool:
    """A temporary-absence auto/hand-typed reply (OOO) is NOT a wrong_contact
    redirect. Combined detector (A′ misread M08 + #19 LIVE breaks E1/E3): fires on
    either the broad OOO/auto-reply banner set (`_OUT_OF_OFFICE_RE`) OR an OOO phrase
    paired with an explicit return signal. An auto-reply that lists a backup or
    assistant address must never be read as an intentional human handoff."""
    blob = (text or "").lower()
    if _OUT_OF_OFFICE_RE.search(text or ""):
        return True
    return bool(
        re.search(
            r"\b(?:out\s+of\s+(?:the\s+)?office|automatic\s+reply|auto[-\s]?reply|"
            r"on\s+vacation|away\s+from\s+(?:my\s+)?(?:email|desk)|"
            r"for\s+urgent\s+matters|limited\s+access\s+to\s+email)\b",
            blob,
        )
        and re.search(
            r"\b(?:until|back\s+(?:on|in)|returning\s+on|return\s+on|"
            r"back\s+in\s+the\s+office)\b",
            blob,
        )
    )


_TERMINAL_SUBJECT_SEPARATOR_RE = re.compile(
    r",\s*(?:and|but|while|whereas)\s+|\s+(?:and|but|while|whereas)\s+",
    re.IGNORECASE,
)


def _contains_unavailable_signal(text: str) -> bool:
    return any(re.search(pattern, text or "", re.IGNORECASE) for _reason, pattern in _UNAVAILABLE_PATTERNS)


def _terminal_subject_clauses(text: str) -> List[str]:
    """Split conjunctions only when each side owns a terminal assertion."""
    clauses = [
        clause.strip()
        for clause in re.split(r"(?<=[.!?;])\s+|\n+", text or "")
        if clause.strip()
    ]
    while True:
        changed = False
        expanded = []
        for clause in clauses:
            for separator in _TERMINAL_SUBJECT_SEPARATOR_RE.finditer(clause):
                left = clause[:separator.start()].strip(" ,")
                right = clause[separator.end():].strip(" ,")
                if _contains_unavailable_signal(left) and _contains_unavailable_signal(right):
                    expanded.extend((left, right))
                    changed = True
                    break
            else:
                expanded.append(clause)
        clauses = expanded
        if not changed:
            return clauses


def _detect_target_terminal_reason(latest_text: str, target_anchor: Optional[str]) -> Optional[str]:
    """Return a terminal reason ONLY when a terminal phrase binds to the TARGET
    property — negation-aware and target-grounded (A′ FIX-01, CodeRabbit PR#15).

    A terminal phrase is ignored when it is negated, bound to an ancillary asset /
    tour slot, or attributed to a DIFFERENT named address than the target. A bare
    terminal (no address in its sentence) is deferred when the message elsewhere
    asserts the target remains viable.
    """
    text = (latest_text or "").lower()
    target_identity = _target_street_identity(target_anchor or "")
    has_global_viability = any(
        not _viability_prefix_is_lexically_negated(text[:match.start()])
        for match in _VIABILITY_RE.finditer(text)
    )
    sentences = _terminal_subject_clauses(text)

    # Pattern order is canonical reason precedence; evaluate it across every
    # subject clause before falling through to a less-specific reason.
    for reason, pattern in _UNAVAILABLE_PATTERNS:
        for sentence in sentences:
            match = re.search(pattern, sentence)
            if not match:
                continue
            pre = sentence[max(0, match.start() - 14): match.start()]
            if re.search(r"\b(?:not|isn'?t|aren'?t|no)\s*$", pre):
                continue  # negated terminal
            if _ANCILLARY_SUBJECT_RE.search(sentence) or re.search(r"\bleased\s+separately\b", sentence):
                continue  # lease bound to an ancillary asset / tour slot
            sentence_claims = _street_claim_spans(sentence)
            if target_identity and any(
                _claim_identity(claim) == target_identity
                for claim in sentence_claims
            ):
                return reason  # terminal explicitly about the TARGET address
            if sentence_claims and target_identity:
                continue  # terminal about a competing named address
            if sentence_claims and not target_identity:
                if has_global_viability:
                    continue
                return reason
            # Bare terminal (no address in this sentence): defer to a viability claim.
            if has_global_viability:
                continue
            return reason
    return None


def _detect_target_requirements_mismatch(
    latest_text: str,
    target_anchor: Optional[str],
) -> bool:
    """Bind a physical non-fit to the target, not an ancillary/competing asset."""
    # Preserve message-wide remediation semantics before splitting into binding
    # clauses; a later "the dock can be ramped" cures an earlier access absence.
    if not _looks_like_requirements_mismatch_nonviable(latest_text):
        return False
    clauses = [
        clause.strip()
        for clause in re.split(
            r"(?<=[.!?;])\s+|\n+|"
            r",\s*(?:while|whereas)\s+|\s+(?:while|whereas)\s+|"
            r",?\s+(?:and|but|or)\s+(?=\d{1,6}\s+[a-z])",
            latest_text or "",
            flags=re.IGNORECASE,
        )
        if clause.strip()
    ]
    last_explicit_binding = None
    for clause in clauses:
        explicit_binding = None
        if target_anchor:
            if _source_mentions_target_property(clause, target_anchor):
                explicit_binding = "target"
            elif _street_claim_spans(clause):
                explicit_binding = "competing"

        if _looks_like_requirements_mismatch_nonviable(clause):
            if _ANCILLARY_SUBJECT_RE.search(clause):
                if explicit_binding:
                    last_explicit_binding = explicit_binding
                continue
            binding = explicit_binding or last_explicit_binding
            if binding != "competing":
                return True

        if explicit_binding:
            last_explicit_binding = explicit_binding
    return False


def _apply_event_retention_guards(
    events: List[dict],
    *,
    newest_text: str,
    quoted_region: str,
    alternate_remains_viable: bool,
    sender_email: Optional[str],
    sender_name: Optional[str],
    contact_name: Optional[str],
) -> List[dict]:
    """Symmetric RETENTION guards for LLM-emitted events (A′ FIX-04, FIX-09, FIX-10).

    The deterministic layer historically gated only INJECTION; nothing removed a
    wrong LLM event. These guards strip events whose evidence is quote-only, whose
    subject is a third party, or which self-contradict.
    """
    def _norm(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", str(value or "").lower())

    sender_email_norm = (sender_email or "").strip().lower()
    identities = {_norm(sender_name), _norm(contact_name)}
    identities.discard("")

    kept: List[dict] = []
    for event in events:
        etype = (event or {}).get("type")

        # (a) evidence lives only in the stripped-away quoted history
        if etype in {
            "property_unavailable", "tour_requested", "contact_optout",
            "wrong_contact", "needs_user_input", "new_property",
        } and _event_evidence_only_in_quote(event, newest_text, quoted_region):
            continue

        # (b) LLM property_unavailable while the alternate/listing remains viable
        if etype == "property_unavailable" and alternate_remains_viable:
            continue

        # (c) wrong_contact redirect loop: suggestedContact/email == sender or row contact
        if etype == "wrong_contact":
            suggested = _norm(event.get("suggestedContact"))
            suggested_email = (event.get("suggestedEmail") or "").strip().lower()
            if suggested and suggested in identities:
                continue
            if suggested_email and sender_email_norm and suggested_email == sender_email_norm:
                continue
            # temporary-absence (OOO) is not a redirect
            if _looks_like_out_of_office(newest_text):
                continue

        # (d) contact_optout attributed to a named third party (not the sender)
        if etype == "contact_optout":
            opt_email = (event.get("email") or event.get("suggestedEmail") or "").strip().lower()
            opt_name = _norm(event.get("contactName"))
            if opt_email and sender_email_norm and opt_email != sender_email_norm:
                continue
            if opt_name and identities and opt_name not in identities:
                continue

        # (e) Courtesy is the only proven-negative tour intent. Preserve
        # UNKNOWN model events for downstream, conversation-aware processing;
        # that layer still fails closed when no subject-bound clause exists.
        if (
            etype == "tour_requested"
            and classify_tour_intent(newest_text) == TOUR_INTENT_COURTESY
        ):
            continue

        # (f) new_property whose own notes self-contradict the referral
        if etype == "new_property" and _NEW_PROP_CONTRADICTION_RE.search(str(event.get("notes") or "")):
            continue

        kept.append(event)
    return kept


def _latest_inbound_sender(conversation: List[dict]) -> str:
    for msg in reversed(conversation or []):
        if (msg or {}).get("direction") == "inbound":
            return str((msg or {}).get("from") or "").lower()
    return ""


def _detect_colleague_redirect(latest_text_raw: str, sender_email: str):
    """High-precision deterministic detector for a broker handing the thread to a
    DIFFERENT person ("my colleague Dana (dana@x.com) actually handles the south
    submarket, loop her in"). Requires BOTH a redirect phrase AND a distinct
    third-party email so it does not false-fire on a broker mentioning their own
    name. Returns {suggestedContact, suggestedEmail} or None.

    The LLM classifier drops this intermittently (nondeterministic wrong_contact),
    which lets a multi-intent reply auto-respond and silently lose the redirect;
    this guard forces the wrong_contact escalation deterministically.
    """
    text = latest_text_raw or ""
    if not _REDIRECT_PHRASE_RE.search(text):
        return None
    sender = (sender_email or "").lower()
    for email in _EMAIL_RE.findall(text):
        el = email.lower()
        if el == sender:
            continue
        name_m = re.search(r"colleague[,\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", text)
        return {"suggestedContact": name_m.group(1) if name_m else "", "suggestedEmail": email}
    return None


# --- Engaged-alternative guard (LIVE break B9) ------------------------------
# A broker who scopes "not interested" to ONE suite while asking to see more
# ("not interested in that particular suite, but show me what else you have
# nearby") is an ACTIVE lead, not an opt-out. The LLM intermittently widens the
# scoped rejection to the whole contact and fires contact_optout, which silently
# STOPS the thread. These deterministic detectors strip that false opt-out while
# never touching a genuine opt-out (unsubscribe / stop emailing / remove me).
_SCOPED_NOT_INTERESTED_RE = re.compile(
    r"\bnot\s+interested\s+in\s+(?:the\s+|this\s+|that\s+)?"
    r"(?:particular\s+|specific\s+|current\s+)?"
    r"(?:suite|space|unit|property|building|listing|option|location|one|deal|place)\b"
    r"|\b(?:this|that)\s+(?:particular\s+|specific\s+)?"
    r"(?:suite|space|unit|property|building|listing|option|location|one)\s+"
    r"(?:doesn[’']t|does\s+not|won[’']t|will\s+not|isn[’']t|is\s+not)\s+"
    r"(?:work|fit|suit|(?:a\s+)?(?:good\s+)?(?:fit|match)\s+for\s+us|for\s+us|right\s+for\s+us)\b",
    re.IGNORECASE,
)
_ALTERNATIVES_REQUEST_RE = re.compile(
    r"\b(?:show|send|share)\s+me\s+(?:what\s+else|others?|other\s+\w+|anything\s+else|"
    r"the\s+others?|different\s+\w+)\b"
    r"|\bwhat\s+else\s+(?:do\s+)?you\s+(?:have|got|offer)\b"
    r"|\bother\s+(?:options?|spaces?|suites?|properties|listings?|availabilit\w+)\b"
    r"|\banything\s+else\s+(?:you\s+have|available|nearby|in\s+the\s+area|around)\b"
    r"|\b(?:got|have)\s+anything\s+else\b"
    r"|\bsomething\s+else\b"
    r"|\b(?:any\s+)?other\s+(?:options?|availabilit\w+)\b",
    re.IGNORECASE,
)
# Hard opt-out phrases: if any of these are present the reply is a genuine
# opt-out and must NEVER be suppressed, even if it also mentions alternatives.
_HARD_OPTOUT_RE = re.compile(
    r"\bunsubscribe\b"
    r"|\bremove\s+me\b|\btake\s+me\s+off\b"
    r"|\bstop\s+(?:emailing|contacting|reaching|messaging)\b"
    r"|\b(?:do\s+not|don[’']t)\s+(?:contact|email|message)\s+me\b"
    r"|\bno\s+longer\s+interested\b|\bnot\s+interested\s+at\s+all\b"
    r"|\bopt(?:ing)?\s+out\b|\boff\s+your\s+(?:list|mailing)\b",
    re.IGNORECASE,
)


def _looks_like_engaged_alternative_request(text: str) -> bool:
    """True when a broker scopes disinterest to a specific property/suite AND asks
    to see alternatives — an engaged lead, not a contact opt-out. Returns False
    for any reply carrying a hard opt-out phrase so genuine opt-outs are preserved.
    """
    t = text or ""
    if not t or _HARD_OPTOUT_RE.search(t):
        return False
    return bool(_SCOPED_NOT_INTERESTED_RE.search(t) and _ALTERNATIVES_REQUEST_RE.search(t))


# ---- Quoted-history awareness ------------------------------------------------
# Broker replies frequently carry the entire prior thread quoted below the fresh
# reply ("> 8200 Trade Center Dr is no longer available", "On Mon ... wrote:",
# forwarded "From:" blocks). Classifying that quoted history as if it were the
# broker's CURRENT message kills live deals ("no longer available" from an old
# quote), redirects to the wrong contact, or schedules stale tours. These helpers
# split the fresh top-of-message from the quoted tail so guards can reason about
# what the broker actually just said.
_QUOTE_LINE_RE = re.compile(r"^\s*>+")
# Standard client attribution: "On <date>, <name> wrote:" ending the line.
_QUOTE_ATTRIBUTION_RE = re.compile(r"^\s*On\b.*\bwrote:\s*$", re.IGNORECASE)
# Broader attribution: "On <date/time> ... wrote[:] <maybe trailing text>". Gmail /
# Apple / Outlook variants where "wrote" is NOT at line end ("...wrote the
# following:", or the quote text glued onto the same line). Gated on a date/time
# token between "On" and "wrote" so casual prose ("On our call I wrote up ...")
# is not mistaken for a quote marker.
_QUOTE_ATTRIBUTION_DATED_RE = re.compile(
    r"^\s*On\b.*?"
    r"(?:\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"|\b(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)"
    r"|\b\d{1,2}[/\-]\d{1,2}"
    r"|\b20\d{2}\b"
    r"|\b\d{1,2}:\d{2}\b"
    r"|\b[ap]\.?m\.?\b)"
    r".*\bwrote\b",
    re.IGNORECASE,
)
_QUOTE_FWD_MARKER_RE = re.compile(
    r"^\s*(?:-{2,}\s*(?:original\s+message|forwarded\s+message)\s*-{2,}"
    r"|begin\s+forwarded\s+message:"
    r"|_{5,})\s*$",
    re.IGNORECASE,
)
# Single forwarded header line carrying a bracketed <email> — matches on its own.
_QUOTE_FWD_HEADER_RE = re.compile(r"^\s*From:\s+.*<[^>]+@[^>]+>", re.IGNORECASE)
# Outlook block-header fields. A bare "From:" line (no <email>) only marks a quote
# when it opens a contiguous Outlook header block (From: + Sent:/Date: + To:/Cc:/
# Subject:), so a prose line like "From: my perspective ..." is never split.
_OUTLOOK_FROM_RE = re.compile(r"^\s*From:\s+\S", re.IGNORECASE)
_OUTLOOK_SENT_RE = re.compile(r"^\s*(?:Sent|Date):\s+\S", re.IGNORECASE)
_OUTLOOK_RECIP_RE = re.compile(r"^\s*(?:To|Cc|Subject):\s+\S", re.IGNORECASE)


def _is_outlook_forward_header(lines, idx: int) -> bool:
    """True when ``lines[idx]`` is the ``From:`` line opening an Outlook-style
    forwarded header block — a bare From: (no <email>) followed within a few lines
    by a Sent:/Date: line and a To:/Cc:/Subject: line."""
    if not _OUTLOOK_FROM_RE.match(lines[idx]):
        return False
    window = lines[idx + 1: idx + 5]
    has_sent = any(_OUTLOOK_SENT_RE.match(l) for l in window)
    has_recip = any(_OUTLOOK_RECIP_RE.match(l) for l in window)
    return has_sent and has_recip


def _split_fresh_and_quoted(text: str):
    """Return (fresh, quoted) for an inbound message body.

    `fresh` is everything above the first quoted-history / forwarded marker;
    `quoted` is that marker line and everything after it. If no quoted history is
    found, `quoted` is empty and `fresh` is the whole text.
    """
    if not text:
        return "", ""
    lines = text.split("\n")
    for idx, line in enumerate(lines):
        if (_QUOTE_LINE_RE.match(line)
                or _QUOTE_ATTRIBUTION_RE.match(line)
                or _QUOTE_ATTRIBUTION_DATED_RE.match(line)
                or _QUOTE_FWD_MARKER_RE.match(line)
                or _QUOTE_FWD_HEADER_RE.match(line)
                or _is_outlook_forward_header(lines, idx)):
            return "\n".join(lines[:idx]), "\n".join(lines[idx:])
    return text, ""


def _fresh_inbound_text(conversation: List[dict]) -> str:
    """Latest inbound text with any quoted prior-thread history stripped off.

    Sources from _raw_latest_inbound (which preserves quoted history) rather than
    _latest_inbound_text, because #15's _latest_inbound_text already strips quotes —
    feeding it here would leave _split_fresh_and_quoted nothing to separate (#15×#19).
    """
    fresh, _ = _split_fresh_and_quoted(_raw_latest_inbound(conversation))
    return fresh


# Per-event-type text signals used to decide whether an LLM-emitted event was
# actually triggered by the broker's fresh message or bled in from quoted history.
_EVENT_QUOTE_SIGNALS = {
    "property_unavailable": [
        r"\bno\s+longer\s+available\b", r"\bnot\s+available\b", r"\boff\s+the\s+market\b",
        r"\bfully\s+leased\b", r"\bhas\s+been\s+leased\b", r"\b(?:is|was|just|now)\s+leased\b",
        r"\bleased\b", r"\bunder\s+contract\b", r"\bsigned\s+(?:a\s+)?lease\b",
        r"\bsigned\s+(?:an?\s+)?(?:loi|letter\s+of\s+intent)\b", r"\bno\s+longer\s+represent",
        r"\bno\s+availability\b", r"\bno\s+space\s+available\b", r"\bno\s+longer\s+on\s+the\s+market\b",
        r"\btaken\b", r"\bwithdrawn\b",
    ],
    "wrong_contact": [
        r"\bno\s+longer\s+handle", r"\bdon'?t\s+handle\b", r"\bdo\s+not\s+handle\b",
        r"\bwrong\s+(?:person|contact)\b", r"\bnot\s+the\s+(?:right\s+)?(?:leasing\s+)?(?:agent|contact|person)\b",
        r"\bplease\s+contact\b", r"\breach\s+out\s+to\b", r"\bno\s+longer\s+with\b",
        r"\bleft\s+the\s+company\b", r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
    ],
    "tour_requested": [
        r"\bschedule\s+a\s+tour\b", r"\btour\b", r"\bshowing\b", r"\bwalk[-\s]?through\b",
        r"\bwould\s+you\s+like\s+to\s+(?:see|tour|view|come)\b", r"\bhappy\s+to\s+show\b",
        r"\bcome\s+(?:by|take\s+a\s+look)\b", r"\bstop\s+by\b", r"\bshow\s+you\s+(?:the|around|it)\b",
    ],
    "contact_optout": [
        r"\bremove\s+me\b", r"\bunsubscribe\b", r"\bnot\s+interested\b", r"\bno\s+thanks\b",
        r"\bplease\s+stop\b", r"\bstop\s+emailing\b", r"\bopt\s+out\b", r"\btake\s+me\s+off\b",
        r"\bdo\s+not\s+contact\b", r"\bdon'?t\s+contact\b", r"\bwe\s+do\s+not\s+work\s+with\b",
        r"\bdon'?t\s+work\s+with\b", r"\bno\s+longer\s+interested\b", r"\bnot\s+taking\s+inquiries\b",
        r"\btenant\s+rep", r"\bdeal\s+direct\b",
    ],
    "call_requested": [
        r"\bcall\s+me\b", r"\bgive\s+me\s+a\s+call\b", r"\bcall\s+you\b", r"\bphone\s+call\b",
        r"\bhop\s+on\s+a\s+call\b", r"\bcan\s+(?:you|we)\s+(?:call|talk)\b", r"\bcall\s+me\s+at\b",
        r"\breach\s+me\s+at\b", r"\bschedule\s+a\s+call\b", r"\blet'?s\s+(?:talk|chat|call)\b",
    ],
    "close_conversation": [
        r"\bgoing\s+exclusive\b", r"\bexclusive\s+with\b", r"\bclose\s+(?:out|the\s+loop|this\s+out)\b",
        r"\bnot\s+a\s+fit\s+to\s+work\b", r"\bgood\s+luck\s+with\s+your\s+search\b",
        r"\bwe'?re\s+going\s+(?:exclusive|with)\b", r"\bin\s+negotiations\s+with\b",
        r"\bsigning\s+next\s+week\b", r"\bdeal\s+pending\b",
    ],
    "property_issue": [
        r"\bsmell", r"\bodor", r"\bmold\b", r"\bwater\s+damage\b", r"\broof\s+(?:leak|damage)\b",
        r"\bfoundation\b", r"\bstructural\b", r"\bpest", r"\bcontamination\b", r"\basbestos\b",
        r"\bflood\s+zone\b", r"\benvironmental\b", r"\bphase\s+(?:ii|2)\b", r"\bhazmat\b",
        r"\bhvac\b", r"\belectrical\s+issue", r"\bplumbing\b", r"\bfire\s+damage\b",
        r"\bcode\s+violation", r"\bzoning\s+problem", r"\bada\s+non", r"\bneeds\s+repair",
        r"\bdamage\b", r"\bleak\b",
    ],
}


def _event_is_quote_only(event: dict, fresh_lower: str, quoted_lower: str) -> bool:
    """True when an LLM event's supporting signal is present in quoted history but
    absent from the broker's fresh message — i.e., it bled in from a prior thread.

    Conservative by design: only returns True when the signal can be affirmatively
    located in the quoted tail AND is missing from the fresh text. If neither
    region carries a recognizable signal, the event is left untouched.
    """
    etype = (event or {}).get("type")

    if etype == "new_property":
        addr = re.sub(r"\[tbd\]", "", (event.get("address") or "").lower()).strip()
        if not addr:
            return False
        key = " ".join(addr.split()[:2])
        return bool(key) and key not in fresh_lower and key in quoted_lower

    if etype == "close_conversation":
        notes = " ".join(str((event or {}).get(k) or "") for k in ("notes", "reason")).lower()
        # "all required fields gathered" closes are not text-signal driven.
        if "all_info_gathered" in notes or "all info" in notes:
            return False

    signals = _EVENT_QUOTE_SIGNALS.get(etype)
    if not signals:
        return False
    fresh_hit = any(re.search(p, fresh_lower) for p in signals)
    quoted_hit = any(re.search(p, quoted_lower) for p in signals)
    return quoted_hit and not fresh_hit


def _suppress_quote_only_events(proposal: dict, conversation: List[dict]) -> dict:
    """Drop LLM events whose only trigger lives in quoted prior-thread history.

    The classifier intermittently reads the quoted tail of a reply as the broker's
    current intent — killing live listings ("no longer available" from an old
    quote), redirecting to a stale contact, scheduling dead tours, or suppressing a
    cooperating broker from an old opt-out. This deterministic guard removes those
    events when their supporting phrase is absent from the fresh message but
    present in the quoted region.
    """
    if not proposal:
        return proposal
    events = proposal.get("events") or []
    if not events:
        return proposal
    # Source raw text (quotes preserved); #15's _latest_inbound_text pre-strips quotes.
    fresh, quoted = _split_fresh_and_quoted(_raw_latest_inbound(conversation))
    if not quoted.strip():
        return proposal
    fresh_lower = fresh.lower()
    quoted_lower = quoted.lower()
    proposal["events"] = [
        event for event in events
        if not _event_is_quote_only(event, fresh_lower, quoted_lower)
    ]
    return proposal


_DriveEvidence = NamedTuple("_DriveEvidence", [("value", Optional[str]), ("saw_drive_language", bool), ("allow_terminal_mismatch", bool), ("requires_review", bool)])


_DRIVE_FEATURE = r"(?:drive[-\s]?ins?(?:\s+doors?)?|grade[-\s]?level(?:\s+(?:doors?|access))?)"
_DRIVE_TERM_RE = re.compile(rf"\b{_DRIVE_FEATURE}\b", re.IGNORECASE)
_CURRENT_DRIVE_ZERO_RE = re.compile(
    rf"\b(?:has|have|with|there\s+(?:is|are))\s+(?:absolutely\s+)?no\s+(?:current\s+)?{_DRIVE_FEATURE}\b|"
    rf"\b(?:does\s+not|doesn['’]?t|do\s+not|don['’]?t)\s+have\s+(?:any\s+)?{_DRIVE_FEATURE}\b|"
    rf"\bwithout\s+(?:any\s+)?{_DRIVE_FEATURE}\b|\bno\s+(?:current\s+)?{_DRIVE_FEATURE}\b|"
    rf"\b(?:0|zero)\s+{_DRIVE_FEATURE}\b", re.IGNORECASE)
_NONCURRENT_DRIVE_RE = re.compile(
    r"\?|\b(?:if|whether|would|could|may|might|proposed|planned|potential|hypothetical|"
    r"unknown|not\s+known|do\s+not\s+know|don['’]?t\s+know|information\s+(?:is\s+)?unavailable|previously|formerly|before\s+(?:renovation|construction|redevelopment|conversion|improvements?|upgrades?))\b|"
    r"\b(?:old|prior|previous|earlier)\s+(?:plan|proposal|layout|listing|info(?:rmation)?|specs?|specifications?|notes?|details?|data)\b", re.IGNORECASE)
_DRIVE_REQUIREMENT_RE = re.compile(r"\b(?:requires?|requirements?|needs?|minimum|at\s+least|no\s+fewer\s+than|must\s+have)\b", re.IGNORECASE)
_POSITIVE_REQUIREMENT_COUNT_RE = re.compile(r"\b(?:requires?|needs?|minimum(?:\s+of)?|at\s+least|no\s+fewer\s+than|must\s+have)"
    r"[^.!?;\n]{0,35}?\b(?:[1-9]\d{0,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b", re.IGNORECASE)
_EXPLICIT_DRIVE_NONFIT_RE = re.compile(
    r"\b(?:not\s+(?:a\s+|the\s+)?(?:good\s+|right\s+)?fit|won['’]?t\s+work|"
    r"will\s+not\s+work|wouldn['’]?t\s+work|doesn['’]?t\s+meet|does\s+not\s+meet|"
    r"fails?\s+(?:the\s+)?requirements?|could\s+not\s+be\s+ramped|couldn['’]?t\s+be\s+ramped|"
    r"not\s+rampable)\b", re.IGNORECASE)
_INDEPENDENT_DRIVE_NONFIT_RE = re.compile(
    r"\b(?:office[-\s]?heavy|too\s+office|mostly\s+office|primarily\s+office|"
    r"not\s+(?:a\s+)?(?:(?:true|real|proper)\s+)?warehouse|no\s+(?:proper\s+|real\s+|true\s+)?"
    r"warehouse|lacks?\s+(?:sufficient\s+)?warehouse|clear\s+height[^.!?]{0,35}"
    r"(?:below|under|less\s+than))\b", re.IGNORECASE)
_OTHER_DRIVE_SUBJECT_RE = re.compile(
    r"\b(?:another|different|other)\s+(?:one|property|building|suite|space|unit|listing)\b|"
    r"\bthe\s+other\s+one\b|\b(?:the\s+)?(?:(?:parking|trailer)\s+lot|trailer|yard|"
    r"outparcel|out-?lot)\s+(?:has|have|with|contains?|is|are|lacks?|without)\b", re.IGNORECASE)
_CURRENT_TARGET_SUBJECT_RE = re.compile(r"\b(?:target|subject|current|this)\s+(?:property|building|suite|space|unit|listing)\b", re.IGNORECASE)
_COLLATERAL_SUBJECT_RE = re.compile(r"\b(?:optional\s+)?(?:flyer|brochure|attachment|document|pdf|website|webpage|link|url)\b", re.IGNORECASE)
_ATTACHMENT_REFERENCE_RE = re.compile(r"\b(?:attached|attachment|flyer|brochure|pdf|document|spec\s+sheet)\b", re.IGNORECASE)
_TARGET_ZERO_UPDATE_REASON = "Deterministic current-target drive evidence: explicit zero."


def _drive_units(text: str) -> List[str]:
    units = re.split(
        r"(?<=[.!?;])\s+|\n+|,\s*(?:but|while|whereas)\s+|"
        r"\s+(?:and|but|while|whereas)\s+(?=\d{1,6}\s+[A-Za-z])|\s*(?:,|—|-)?\s*\b(?:correction|actually|scratch\s+that|i\s+mean)\b\s*[:,]?\s*",
        text or "", flags=re.IGNORECASE)
    return [unit.strip(" ,") for unit in units if unit.strip(" ,")]


def _has_positive_drive_requirement(text: str) -> bool:
    for unit in _drive_units(text):
        if ((match := _POSITIVE_REQUIREMENT_COUNT_RE.search(unit)) and
            (_DRIVE_TERM_RE.search(unit) or not re.search(
                r"\b(?:days?|hours?|weeks?|months?|tours?|docks?|parking|sf|square\s+feet|"
                r"feet|foot|amps?|volts?)\b", unit[match.start():], re.IGNORECASE))):
            return True
    return False


def _drive_property_claims(text: str) -> List[tuple]:
    return [claim for claim in _street_claim_spans(text or "") if not (claim[4] == "drive"
            and re.match(r"[-\s]*in\b", (text or "")[claim[1]:], re.IGNORECASE))]


def _drive_count_in_unit(unit: str) -> Optional[str]:
    unit = re.sub(rf"\bnot\s+(?:0|zero)\s+{_DRIVE_FEATURE}\b", "", unit or "", flags=re.IGNORECASE)
    if _CURRENT_DRIVE_ZERO_RE.search(unit):
        return "0"
    return (_parse_feature_count(match.group(1)) if (match := _DRIVE_IN_COUNT_RE.search(unit or ""))
            else _extract_dimensioned_singular_drive_in_count(unit or ""))


def _current_target_drive_evidence(text: str, target_anchor: Optional[str]) -> _DriveEvidence:
    fresh, target_identity = _strip_quoted_history(text or ""), _target_street_identity(target_anchor or "")
    last_binding = last_value = None
    target_explicit_nonfit = target_independent_nonfit = False
    saw_drive = False
    for unit in _drive_units(fresh):
        claims = _drive_property_claims(unit)
        identities = {_claim_identity(claim) for claim in claims}
        if target_identity and target_identity in identities and len(identities) == 1:
            binding = "target"
        elif identities:
            binding = "competing"
        elif _CURRENT_TARGET_SUBJECT_RE.search(unit):
            binding = "target"
        elif _COLLATERAL_SUBJECT_RE.search(unit) and re.search(r"\b(?:has|contains?|lists?|shows?|provides?)\s+no\b[^.!?;]{0,30}\bdrive", unit, re.IGNORECASE): binding = "competing"
        elif _OTHER_DRIVE_SUBJECT_RE.search(unit):
            binding = "competing"
        else:
            binding = last_binding or "target"
        last_binding = binding
        if binding == "target":
            target_explicit_nonfit |= bool(_EXPLICIT_DRIVE_NONFIT_RE.search(unit))
            target_independent_nonfit |= bool(_INDEPENDENT_DRIVE_NONFIT_RE.search(unit))
        if not _DRIVE_TERM_RE.search(unit):
            continue
        saw_drive = True
        value = _drive_count_in_unit(unit)
        value_match = _CURRENT_DRIVE_ZERO_RE.search(unit) or _DRIVE_IN_COUNT_RE.search(unit)
        before = unit[:value_match.end()] if value_match else unit
        after = unit[value_match.end():] if value_match else ""
        if (value is None or binding != "target" or "?" in unit
                or _NONCURRENT_DRIVE_RE.search(before) or _NONCURRENT_FEATURE_AFTER_RE.search(after) or (value_match and _NONCURRENT_FEATURE_BEFORE_RE.search(unit[:value_match.start()]))
                or re.search(r"\bbefore\s+(?:renovation|construction|redevelopment|conversion|improvements?|upgrades?)\b", after, re.IGNORECASE)
                or re.search(r"\b(?:under\s+(?:the\s+)?proposed|planned|hypothetical)\b",
                             after[:80], re.IGNORECASE)):
            continue
        direct_zero = re.search(
            r"\b(?:has|have|with|there\s+(?:is|are))\s+(?:absolutely\s+)?no\b|"
            r"\b(?:does\s+not|doesn['’]?t|do\s+not|don['’]?t)\s+have\b|\bwithout\b",
            unit, re.IGNORECASE)
        if not (_DRIVE_REQUIREMENT_RE.search(unit) and not direct_zero):
            last_value = value
    remediation = bool(last_value == "0" and _looks_like_access_remediation(fresh))
    positive_requirement = bool(last_value == "0" and _has_positive_drive_requirement(fresh))
    allow_terminal = bool(target_independent_nonfit or (target_explicit_nonfit and not remediation))
    return _DriveEvidence(last_value, saw_drive, allow_terminal,
                          bool((remediation or positive_requirement) and not target_independent_nonfit))


def _drive_reason_alias(reason: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(reason or "").lower())
    aliases = {"requirementsmismatch", "physicalnonfit", "badfit", "nodriveins",
               "missingdriveins", "nogradelevelaccess", "zerodriveins"}
    return normalized in aliases or bool(
        ("drivein" in normalized or "gradelevel" in normalized)
        and any(token in normalized for token in ("no", "zero", "missing", "lack")))


def _availability_reason_alias(reason: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(reason or "").lower())
    return normalized in ({re.sub(r"[^a-z0-9]", "", value.lower()) for value, _ in _UNAVAILABLE_PATTERNS}
                          | {"unavailable", "propertyunavailable"})


def _normalized_close_event_reason(event: dict) -> Optional[str]:
    if (event or {}).get("type") != "close_conversation":
        return None
    reason = (
        event.get("notes")
        or event.get("reason")
        or event.get("closeReason")
        or "all_info_gathered"
    )
    return re.sub(r"[^a-z0-9]", "", str(reason).lower())


def _is_all_info_close_event(event: dict) -> bool:
    return _normalized_close_event_reason(event) == "allinfogathered"


_INDEPENDENT_TERMINAL_CLOSE_REASONS = {
    "exclusivewithanother",
    "dealpending",
    "naturalend",
    "notafit",
}


def _collateral_only_unavailable(text: str, target_anchor: Optional[str]) -> bool:
    collateral = _COLLATERAL_SUBJECT_RE.search(text or "")
    if not collateral:
        return False
    target_identity = _target_street_identity(target_anchor or "")
    segments = ((text or "")[:collateral.start()], (text or "")[collateral.end():])
    if any((generic := re.search(r"\b(?:target|subject|current|this|the)\s+(?:property|building|suite|space|unit|listing)\b", segment, re.IGNORECASE)) and _contains_unavailable_signal(segment[generic.start():]) for segment in segments):
        return False
    claims = []
    for claim in _drive_property_claims(text or ""):
        between = (text or "")[collateral.end():claim[0]]
        if (target_identity and _claim_identity(claim) == target_identity
                and not re.search(r"\b(?:for|of)\s*$", between, re.IGNORECASE)):
            claims.append(claim)
    for claim in claims:
        suffix = (text or "")[claim[0]:]
        if any(re.search(pattern, suffix, re.IGNORECASE) for _, pattern in _UNAVAILABLE_PATTERNS):
            return False
    boundary = min((claim[0] for claim in claims), default=len(text or ""))
    window = (text or "")[collateral.start():boundary]
    return bool(re.search(
        r"\b(?:no\s+longer\s+available|not\s+available|isn['’]?t\s+available|unavailable|off\s+(?:the\s+)?market|expired)\b",
        window, re.IGNORECASE))


def _reconcile_target_current_drive(
    proposal: dict, conversation: List[dict], target_anchor: Optional[str],
    header: Optional[List[str]], effective_config: Optional[dict],
    extra_texts: Optional[List[str]] = None,
) -> _DriveEvidence:
    fresh = _fresh_inbound_text(conversation)
    evidence = _current_target_drive_evidence(fresh, target_anchor)
    if evidence.value is None and _ATTACHMENT_REFERENCE_RE.search(fresh or ""):
        sources = []
        for source in extra_texts or []:
            claims = _drive_property_claims(source or "")
            if claims and not _source_mentions_target_property(source, target_anchor or ""):
                continue
            source_evidence = _current_target_drive_evidence(source, target_anchor)
            if source_evidence.value is not None:
                sources.append(source_evidence)
        values = {source.value for source in sources}
        if len(values) == 1:
            value = values.pop()
            terminal = evidence.allow_terminal_mismatch or any(source.allow_terminal_mismatch for source in sources)
            review = (evidence.requires_review or any(source.requires_review for source in sources) or bool(value == "0" and (_looks_like_access_remediation(fresh) or _has_positive_drive_requirement(fresh))))
            evidence = evidence._replace(value=value, saw_drive_language=True, allow_terminal_mismatch=terminal, requires_review=bool(review and not terminal))
    mappings = (effective_config or {}).get("mappings", {})
    drive_col = mappings.get("drive_ins")
    fallback_drive_col = (_find_header_name(header or [], "Drive Ins") or _find_header_name(header or [], "Drive-Ins"))
    if drive_col is None and effective_config is None:
        drive_col = fallback_drive_col
    elif drive_col is None:
        _remove_proposal_update(proposal, fallback_drive_col)
    if drive_col:
        update = _proposal_update_for_column(proposal, drive_col)
        if evidence.value == "0":
            if update is None:
                proposal.setdefault("updates", []).append({
                    "column": drive_col, "value": "0", "confidence": 1.0,
                    "reason": _TARGET_ZERO_UPDATE_REASON})
            else:
                update.update(value="0", reason=_TARGET_ZERO_UPDATE_REASON)
        elif update is not None and (evidence.value is None
                                     or str(update.get("value") or "").strip() != evidence.value):
            _remove_proposal_update(proposal, drive_col)
    detected_terminal = _detect_target_terminal_reason(fresh, target_anchor)
    target_viable = any(_VIABILITY_RE.search(unit) and _source_mentions_target_property(unit, target_anchor or "") for unit in _drive_units(fresh))
    suppress_availability = (_collateral_only_unavailable(fresh, target_anchor)
                             or (target_viable and detected_terminal is None))
    original_events = proposal.get("events") or []
    proposal["events"] = [event for event in original_events if not (
        (event or {}).get("type") == "property_unavailable" and (
            (evidence.saw_drive_language and not evidence.allow_terminal_mismatch
             and _drive_reason_alias((event or {}).get("reason")))
            or (suppress_availability and _availability_reason_alias((event or {}).get("reason")))))]
    if len(proposal["events"]) != len(original_events): proposal["response_email"] = None
    if evidence.requires_review:
        proposal["events"] = [
            event for event in proposal["events"]
            if not _is_all_info_close_event(event)
        ]
        has_independent_terminal_close = any(
            _normalized_close_event_reason(event) in _INDEPENDENT_TERMINAL_CLOSE_REASONS
            for event in proposal["events"]
        )
        if has_independent_terminal_close:
            proposal["events"] = [
                event for event in proposal["events"]
                if (event or {}).get("type") != "needs_user_input"
            ]
        elif not any((event or {}).get("type") == "needs_user_input" for event in proposal["events"]):
            proposal["events"].append({"type": "needs_user_input",
                                       "reason": "drive_access_requires_review",
                                       "question": fresh[:500]})
        proposal["response_email"] = None
    return evidence


def _augment_events_with_deterministic_signals(
    proposal: dict,
    conversation: List[dict],
    target_anchor: Optional[str] = None,
    sender_email: Optional[str] = None,
    sender_name: Optional[str] = None,
    contact_name: Optional[str] = None,
    header: Optional[List[str]] = None,
    effective_config: Optional[dict] = None,
    extra_texts: Optional[List[str]] = None,
) -> dict:
    """Add high-confidence event signals from broker phrases the model can miss,
    and strip wrong LLM-emitted events (retention guards)."""
    if not proposal:
        return proposal

    events = proposal.setdefault("events", [])
    # Reason only over the broker's FRESH message; quoted prior-thread history must
    # not deterministically fire property_unavailable / redirect / tour signals.
    latest_text_raw = _fresh_inbound_text(conversation)
    latest_text = latest_text_raw.lower()
    if not latest_text:
        return proposal

    # Out-of-office / auto-reply guard (LIVE breaks E1/E3): an OOO auto-reply that
    # lists a backup or assistant address ("for urgent matters, contact X",
    # "please contact my assistant X") is NOT an intentional human handoff. The LLM
    # intermittently reads that backup address as a wrong_contact redirect and
    # escalates the WRONG person. Strip any wrong_contact and do not force a redirect
    # so the auto-reply is treated as ignore/continue, model-independently.
    if _looks_like_out_of_office(latest_text_raw):
        proposal["events"] = [
            e for e in events
            if (e or {}).get("type") != "wrong_contact"
            and not (
                (e or {}).get("type") == "tour_requested"
                and classify_tour_intent(latest_text_raw) == TOUR_INTENT_COURTESY
            )
        ]
        return proposal

    # Engaged-alternative guard (LIVE break B9): a scoped "not interested in that
    # suite, but show me what else you have" is an active lead, not an opt-out.
    # Strip any contact_optout the LLM over-fired so the thread is not silently
    # stopped. Genuine opt-outs (unsubscribe / stop emailing / remove me) are
    # excluded by _looks_like_engaged_alternative_request and survive untouched.
    if _looks_like_engaged_alternative_request(latest_text_raw):
        kept = [e for e in events if (e or {}).get("type") != "contact_optout"]
        if len(kept) != len(events):
            proposal["events"] = kept
            events = proposal["events"]

    # Colleague/third-party redirect → force wrong_contact + escalate (no auto-reply).
    # Runs BEFORE the property_unavailable early-return so it survives a multi-intent
    # reply ("just leased, but try 4400 Referral Way, and loop in my colleague Dana").
    redirect = _detect_colleague_redirect(latest_text_raw, _latest_inbound_sender(conversation))
    if redirect and not any((e or {}).get("type") == "wrong_contact" for e in events):
        events.append({
            "type": "wrong_contact",
            "reason": "colleague_redirect",
            "suggestedContact": redirect.get("suggestedContact", ""),
            "suggestedEmail": redirect.get("suggestedEmail", ""),
        })
    # A wrong_contact redirect must escalate to the operator, never auto-commit to
    # looping in an unapproved third party.
    if any((e or {}).get("type") == "wrong_contact" for e in events):
        proposal["response_email"] = None

    # Call request → escalate to operator, never auto-send (LIVE break: call_lets_hop).
    # A broker asking to "hop on a call" must reach a human whether or not a phone
    # number is included; the prompt intermittently drafts an auto-reply asking for a
    # number/time instead of escalating. Deterministically fire call_requested from the
    # fresh text (so it holds even when the model mislabels) and suppress any drafted
    # response_email so a live call ask always notifies the operator, model-independently.
    if _looks_like_call_request(latest_text):
        if not any((e or {}).get("type") == "call_requested" for e in events):
            events.append({"type": "call_requested", "reason": "call_request_phrase"})
    if any((e or {}).get("type") == "call_requested" for e in events):
        proposal["response_email"] = None

    # HEAD retention/terminal layer reasons over the FULL latest inbound plus its
    # quoted region. Target-grounded terminal detection (_detect_target_terminal_reason,
    # below) supersedes the flat unavailable-pattern list #19 used here.
    raw_latest = _raw_latest_inbound(conversation)
    quoted_region = _quoted_region(raw_latest)

    # Near-miss guard: "one suite is leased but an alternate suite remains viable"
    # must not terminalize the row. CodeRabbit PR#15: the alternate-reference and the
    # still-viable signal must live in the SAME sentence/clause — otherwise a separate
    # "we have another suite that is still available" would mask a terminal signal on
    # the current listing. Used for the LLM-PU RETENTION guard (M01); injection relies
    # instead on target-grounded detection so an explicit TARGET terminal still fires.
    _alt_ref = re.compile(r"\b(?:alternate|another|different|other)\s+(?:suite|space|unit|option|property|listing)\b")
    _alt_viable = re.compile(r"\b(?:remains?|still|is|are)\s+(?:viable|available|open|active)\b")
    alternate_remains_viable = any(
        _alt_ref.search(sentence) and _alt_viable.search(sentence)
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", latest_text)
    )
    target_requirements_mismatch = _detect_target_requirements_mismatch(
        latest_text,
        target_anchor,
    )

    # ---- Retention guards run first (on the LLM's own events) -----------------
    events = _apply_event_retention_guards(
        events,
        newest_text=latest_text_raw,
        quoted_region=quoted_region,
        alternate_remains_viable=alternate_remains_viable,
        sender_email=sender_email,
        sender_name=sender_name,
        contact_name=contact_name,
    )
    proposal["events"] = events
    if target_anchor:
        drive_evidence = _reconcile_target_current_drive(
            proposal,
            conversation,
            target_anchor,
            header,
            effective_config,
            extra_texts,
        )
        if drive_evidence.saw_drive_language and not drive_evidence.allow_terminal_mismatch:
            target_requirements_mismatch = False
        events = proposal["events"]

    # A physical non-fit (office-heavy / not-a-warehouse / no drive-in / below-spec
    # clear height) is a statement about the PROPERTY itself, not about touring, so
    # it must be detected even when the SAME reply ALSO declines a tour. Otherwise a
    # broker who writes "we can't tour right now AND it's too office-heavy for your
    # client" is read as merely tour-unavailable and the genuinely non-viable row is
    # kept alive with a live tour response_email (combination deck
    # jill_nonviable_vs_unavailable). requirements_mismatch is high-precision and
    # quoted-history-stripped, so promoting it ahead of the tour-only short-circuit
    # does not terminalize a viable row. Terminal (leased / off-market / no-longer-
    # available) detection STAYS gated behind the tour-only check, because
    # "no longer available for tours" is a legitimately tour-scoped phrase that
    # looks_like_tour_only_unavailable owns.
    property_unavailable_reason = None
    if target_requirements_mismatch:
        property_unavailable_reason = "requirements_mismatch"
    elif (
        not looks_like_tour_only_unavailable(latest_text_raw)
        and not _collateral_only_unavailable(latest_text_raw, target_anchor)
    ):
        property_unavailable_reason = _detect_target_terminal_reason(latest_text, target_anchor)

    if property_unavailable_reason:
        has_replacement_property = any((event or {}).get("type") == "new_property" for event in events)
        conflicting_event_types = {"close_conversation"}
        if not has_replacement_property:
            conflicting_event_types.add("tour_requested")

        retained_events = [
            event for event in events
            if (event or {}).get("type") not in conflicting_event_types
        ]
        existing_unavailable = False
        for event in retained_events:
            if (event or {}).get("type") != "property_unavailable":
                continue
            event["reason"] = property_unavailable_reason
            existing_unavailable = True
        if not existing_unavailable:
            retained_events.insert(0, {
                "type": "property_unavailable",
                "reason": property_unavailable_reason,
            })
        proposal["events"] = retained_events
        # FIX-03: a genuine terminal injection must not leave a live response_email
        # (row marked dead while the outbound keeps chatting with the broker).
        proposal["response_email"] = None
        return proposal

    # FIX-02: never delete an LLM property_unavailable carrying a substantive
    # (requirements-fit) reason — a tour-only idiom must not erase a correct
    # non-viable classification.
    def _is_substantive_pu(event: dict) -> bool:
        return (
            (event or {}).get("type") == "property_unavailable"
            and str((event or {}).get("reason") or "").strip() == "requirements_mismatch"
        )

    tour_reply_reason = None
    tour_reply_text = " ".join(subject_bound_tour_segments(latest_text_raw))
    proposed_tour_options = extract_proposed_tour_options(tour_reply_text)
    tour_only_unavailable = looks_like_tour_only_unavailable(tour_reply_text)
    if tour_only_unavailable and not proposed_tour_options:
        if (
            _has_tour_scheduling_context(conversation)
            and _looks_like_tour_slot_reply(conversation, latest_text)
        ):
            tour_reply_reason = "tour_unavailable"
        else:
            # A tour restriction must never stop the property. Initial outreach
            # merely asking whether tours exist is not an active scheduling
            # lifecycle, so scrub a model over-fire without injecting a tour event.
            proposal["events"] = [
                event for event in events
                if (event or {}).get("type") != "property_unavailable"
                or _is_substantive_pu(event)
            ]
            return proposal
    elif _looks_like_tour_slot_reply(conversation, latest_text):
        tour_reply_reason = "tour_slot_reply"

    if tour_reply_reason:
        existing_tour = [e for e in events if (e or {}).get("type") == "tour_requested"]
        proposal["events"] = [
            event for event in events
            if (event or {}).get("type") != "property_unavailable" or _is_substantive_pu(event)
        ]
        if existing_tour:
            # FIX-05: the deterministic subject-bound lifecycle verdict is
            # authoritative for both an unavailable tour and a proposed
            # alternate; repair any model-emitted stale/wrong reason in place.
            for event in proposal["events"]:
                if (event or {}).get("type") == "tour_requested":
                    event["reason"] = tour_reply_reason
        else:
            proposal["events"].append({
                "type": "tour_requested",
                "reason": tour_reply_reason,
                "question": latest_text_raw[:500],
                "suggestedEmail": "",
            })
        return proposal

    return proposal


# Cost figures that live in a "$X/SF" shape but are NOT the asking rent. If one of
# these labels sits immediately adjacent to a matched figure the figure is skipped
# so we never invent an asking rate from a TI allowance, tax, parking, or opex line.
_NON_RENT_COST_MARKERS = (
    "ti allowance",
    "t.i. allowance",
    # A "$X/SF TI credit" (or bare "$X TI") is a concession, not the asking rent
    # (A′ misread M13 wrote a $2/SF TI credit into the rent column).
    "ti credit",
    "t.i. credit",
    "tenant improvement",
    "improvement allowance",
    "buildout",
    "build-out",
    "build out",
    "parking",
    "tax",
    "cam",
    "opex",
    "ops ex",
    "operating expense",
    "insurance",
    "utilities",
)

# Bare "TI" / "T.I." token immediately bound to a figure (word-boundary matched so
# it never fires inside words like "notification" or "estimated").
_BARE_TI_RE = re.compile(r"\bt\.?\s?i\.?\b", re.IGNORECASE)


def _figure_is_non_rent(text: str, start: int, end: int, check_after: bool = True) -> bool:
    """True if a non-rent cost label (TI/taxes/parking/opex/...) binds to this figure.

    Only the text immediately adjacent to the figure is inspected — bounded by the
    previous/next figure or clause delimiter — so a genuine rate that merely sits
    near an unrelated opex line ('$0.82 NNN, $0.21 opex') is not falsely dropped.

    When ``check_after`` is False the trailing segment is ignored — used for figures
    that already carry an explicit lease basis (e.g. "8.75 nnn opex 2.10"), where a
    trailing opex/tax labels a SEPARATE figure, not this rent.
    """
    lowered = text.lower()

    before = lowered[:start]
    cut = max(before.rfind("$"), before.rfind(","), before.rfind(";"), before.rfind("."))
    before_segment = before[cut + 1:] if cut >= 0 else before

    after_segment = ""
    if check_after:
        after = lowered[end:]
        stops = [pos for pos in (after.find("$"), after.find(","), after.find(";"), after.find(".")) if pos >= 0]
        after_segment = after[: min(stops)] if stops else after

    adjacent = f"{before_segment} {after_segment}"
    if any(marker in adjacent for marker in _NON_RENT_COST_MARKERS):
        return True
    return bool(_BARE_TI_RE.search(adjacent))


# Lease-basis suffix vocabulary. Multi-word forms ("triple net") are listed
# before their single-word substrings ("net") so the alternation prefers the
# longer match. Shared by every basis-bearing rent pattern below.
_LEASE_BASIS = (
    r"(?:triple\s+net|double\s+net|single\s+net|modified\s+gross|full\s+service|"
    r"industrial\s+gross|gross|nnn|net|fsg|ig|mg)"
)

# Total ANNUAL rent (>= $1,000, comma-grouped or 4+ digits) stated per year.
_TOTAL_ANNUAL_RENT_RE = re.compile(
    r"\$\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{4,})(?:\.[0-9]{2})?\s*"
    r"(?:/|\bper\s+)?\s*(?:yr|year|annum|annually|/yr)\b",
    re.IGNORECASE,
)
# A building/suite area figure (>= 1,000 SF), used as the divisor.
_AREA_SF_RE = re.compile(
    r"([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{4,})\s*"
    r"(?:sf|sq\.?\s*ft|square\s*f(?:ee|oo)t)\b",
    re.IGNORECASE,
)
# $/SF unit vocabulary, incl. the "psf" abbreviation brokers use inline.
_RENT_CONTEXT_RE = re.compile(
    r"(?:asking|base\s+rent|rent|rate)[^\d$]{0,24}\$?\s*([0-9]{1,3}(?:\.[0-9]{1,2})?)\s*"
    r"(?:(?:/|\s+per\s+)?\s*(?:sf|sq\.?\s*ft|square\s*foot)|/?\s*psf)(?:\s*/?\s*(?:yr|year|annum))?",
    re.IGNORECASE,
)
_DOLLAR_PER_SF_RE = re.compile(
    r"\$?\s*([0-9]{1,3}(?:\.[0-9]{1,2})?)\s*"
    r"(?:(?:/|\s+per\s+)\s*(?:sf|sq\.?\s*ft|square\s*foot)|/?\s*psf)"
    r"(?:\s*/?\s*(?:yr|year|annum))?",
    re.IGNORECASE,
)
# Combined "base + opex" line: "$24 + $8/sf opex", "$1.25 NNN + $0.34 OPEX".
# Group 1 is the base rent; group 2 is OpEx/NNN and must never be read as rent.
_OPS_EX_RATE_UNIT = r"(?:psf|sf|sq\.?\s*ft\.?|square\s+foot)"
_COMBINED_EQUATION_SUFFIX = (
    rf"(?:\s*=\s*\$\s*\d+(?:\.\d+)?\s*"
    rf"(?:(?:/|\bper(?:\s+|-))\s*)?{_OPS_EX_RATE_UNIT})?"
)
_COMBINED_RENT_OPEX_RE = re.compile(
    r"\$\s*([0-9]{1,3}(?:\.[0-9]{1,2})?)\s*(?:/?\s*(?:psf|sf|sq\.?\s*ft))?\s*(?:nnn|net|gross)?"
    r"\s*\+\s*"
    r"\$\s*([0-9]{1,3}(?:\.[0-9]{1,2})?)\s*(?:/?\s*(?:psf|sf|sq\.?\s*ft))?\s*(?:in\s+)?"
    rf"(?:opex|op\s*ex|nnn|cam|net|operating\s+expense){_COMBINED_EQUATION_SUFFIX}",
    re.IGNORECASE,
)
# Range: "rates are between $20.00 - $22.00" → low end is a defensible asking rent.
_RENT_RANGE_RE = re.compile(
    r"(?:asking|base\s+rent|rents?|rates?|quoted\s+rates?)[^\d$]{0,30}"
    r"\$\s*([0-9]{1,3}(?:\.[0-9]{1,2})?)\s*(?:/?\s*(?:psf|sf))?\s*"
    r"(?:-|to|–|—)\s*\$\s*([0-9]{1,3}(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)
# Standalone OpEx/NNN/CAM figure in either order.
_OPS_EX_RE = re.compile(
    r"\$\s*([0-9]{1,3}(?:\.[0-9]{1,2})?)\s*(?:/?\s*(?:psf|sf|sq\.?\s*ft))?\s*(?:in\s+)?"
    # tmi = Canadian Taxes/Maintenance/Insurance, the CA equivalent of NNN/CAM OpEx.
    r"(?:opex|op\s*ex|nnn|cam|tmi|operating\s+expense)"
    # keyword-first: allow a short linking clause ("is", "charges are", "of",
    # "runs", "estimated at") between the keyword and the figure so
    # "OpEx is $16/SF" and "NNN charges are $7.25/SF/yr" parse. The gap forbids
    # digits/$/newlines so an unrelated later rent figure can't be captured.
    r"|(?:opex|op\s*ex|nnn|cam|tmi|operating\s+expense)"
    r"(?:[^\d$\n]{0,18}?\b(?:is|are|of|at|runs?|estimated|approx(?:imately)?|about|around)\b)?"
    r"\s*[:\-=~]?\s*"
    r"\$\s*([0-9]{1,3}(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)
_OPS_EX_EXPLICIT_LABEL = r"(?:opex|op\s*ex|cam|tmi|operating\s+expenses?)"
_OPS_EX_DOLLAR_VALUE = (
    r"\$\s*(?P<value>[0-9]{1,3}(?:\.[0-9]{1,2})?)\s*"
    r"(?:(?:/\s*|\bper\s+)(?:sf|psf|sq\.?\s*ft|square\s+foot))?"
    r"(?:\s*/?\s*(?:monthly|annually|annual|yearly|month|annum|year|mos|mo|yr)\b)?"
)
_OPS_EX_COMPONENT_LIST_RE = re.compile(
    rf"\b{_OPS_EX_EXPLICIT_LABEL}\b"
    r"\s*,\s*(?:property\s+)?tax(?:es)?\s*,?\s*(?:and|&)\s+insurance\b"
    r"\s+(?:is|are)\s+"
    r"(?:(?:running|estimated)(?:\s+(?:roughly|approximately|about|around))?|"
    r"(?:roughly|approximately|about|around))\s*"
    rf"{_OPS_EX_DOLLAR_VALUE}",
    re.IGNORECASE,
)
_OPS_EX_RENT_MODIFIER = (
    r"(?:on\s+top\s+of|in\s+addition\s+to)\s+"
    r"(?:the\s+)?(?:base\s+)?rent"
)
_OPS_EX_RENT_MODIFIER_RE = re.compile(
    rf"\b{_OPS_EX_EXPLICIT_LABEL}\b\s*"
    rf"(?:,\s*{_OPS_EX_RENT_MODIFIER}\s*,|"
    rf"\(\s*{_OPS_EX_RENT_MODIFIER}\s*\))"
    rf"\s*(?:is|are)\s*{_OPS_EX_DOLLAR_VALUE}",
    re.IGNORECASE,
)
_COMBINED_TOTAL_RENT_LABEL = (
    r"(?:(?:base|asking)\s+rent|rent|lease\s+rate|rental\s+rate)"
)
_OPS_EX_COMPETING_RENT_SUBJECT = (
    rf"(?:{_COMBINED_TOTAL_RENT_LABEL}|(?:base|asking|quoted)\s+rate|"
    r"(?:lease|asking)\s+price)"
)
_OPS_EX_COMPETING_BASIS_SUBJECT = (
    rf"(?:{_OPS_EX_COMPETING_RENT_SUBJECT}|parking|reports?|"
    r"utilities?|invoices?|statements?|summar(?:y|ies))"
)
_OPS_EX_TAX_SUBJECT = (
    r"(?:(?:property|real\s+estate)\s+)?tax(?:es)?"
)
_OPS_EX_INSURANCE_SUBJECT = (
    r"(?:(?:property|real\s+estate)\s+)?insurance"
)
_OPS_EX_TAX_INSURANCE_SUBJECT = (
    rf"(?:{_OPS_EX_TAX_SUBJECT}|{_OPS_EX_INSURANCE_SUBJECT})"
)
_OPS_EX_SUPPORTING_BASIS_QUALIFIER = (
    rf"(?:(?:{_OPS_EX_TAX_SUBJECT}\s+(?:and|&)\s+"
    rf"{_OPS_EX_INSURANCE_SUBJECT})|"
    rf"(?:{_OPS_EX_INSURANCE_SUBJECT}\s+(?:and|&)\s+"
    rf"{_OPS_EX_TAX_SUBJECT}))"
)
_OPS_EX_DIRECT_BASIS_SUBJECT = (
    rf"(?:{_OPS_EX_COMPETING_BASIS_SUBJECT}|{_OPS_EX_TAX_INSURANCE_SUBJECT})"
)
_COMBINED_TOTAL_RELATION = (
    r"(?:plus|and|on\s+top\s+of|in\s+addition\s+to)"
)
_COMBINED_TOTAL_PREDICATE = (
    r"(?:(?:(?:combined(?:\s+total)?|all[-\s]?in|gross)"
    r"(?:\s+(?:rent|rate|cost))?\s+)?"
    r"(?:is|are|equals?|totals?|comes?\s+to|amounts?\s+to))"
)
_COMBINED_TOTAL_OPEX_RES = (
    re.compile(
        rf"\b{_OPS_EX_EXPLICIT_LABEL}\b\s+{_COMBINED_TOTAL_RELATION}\s+"
        rf"(?:the\s+)?{_COMBINED_TOTAL_RENT_LABEL}\b\s+"
        rf"{_COMBINED_TOTAL_PREDICATE}\s*{_OPS_EX_DOLLAR_VALUE}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_COMBINED_TOTAL_RENT_LABEL}\b\s+{_COMBINED_TOTAL_RELATION}\s+"
        rf"(?:the\s+)?{_OPS_EX_EXPLICIT_LABEL}\b\s+{_COMBINED_TOTAL_PREDICATE}\s*"
        rf"{_OPS_EX_DOLLAR_VALUE}",
        re.IGNORECASE,
    ),
)
_OPS_EX_STALE_EVIDENCE_MARKER = (
    r"(?:prior|stale|previous|historical|old|former)"
)
_OPS_EX_CURRENT_EVIDENCE_MARKER = (
    r"(?:current(?:ly)?|now|revised|updated|correction|corrected|correct|actually)"
)
_OPS_EX_EVIDENCE_DESCRIPTOR = (
    r"(?:estimate|quote|quoted\s+rate|rate|figure|amount|value)"
)
_OPS_EX_STALE_EVIDENCE_PREFIX_RE = re.compile(
    rf"\b{_OPS_EX_STALE_EVIDENCE_MARKER}\b"
    rf"(?:\s+{_OPS_EX_EVIDENCE_DESCRIPTOR})?(?:\s+for)?[\s:,-]{{0,4}}$",
    re.IGNORECASE,
)
_OPS_EX_CURRENT_EVIDENCE_PREFIX_RE = re.compile(
    rf"\b{_OPS_EX_CURRENT_EVIDENCE_MARKER}\b"
    rf"(?:\s+{_OPS_EX_EVIDENCE_DESCRIPTOR})?(?:\s+for)?[\s:,-]{{0,4}}$",
    re.IGNORECASE,
)
_OPS_EX_STALE_EVIDENCE_LOCAL_RE = re.compile(
    rf"\b{_OPS_EX_STALE_EVIDENCE_MARKER}\b",
    re.IGNORECASE,
)
_OPS_EX_CURRENT_EVIDENCE_LOCAL_RE = re.compile(
    rf"\b{_OPS_EX_CURRENT_EVIDENCE_MARKER}\b",
    re.IGNORECASE,
)
_RENT_NNN_EXPLICIT_OWNER = (
    r"(?:asking\s+(?:rent|(?:rental\s+)?rate)|asking|base\s+rent|"
    r"lease\s+rate|rental\s+rate|rent|rate)"
)
_RENT_NNN_EXPLICIT_OWNER_RE = re.compile(
    rf"\b{_RENT_NNN_EXPLICIT_OWNER}\b",
    re.IGNORECASE,
)
_OPS_EX_NNN_OWNER = (
    r"(?:operating\s+(?:expenses?|costs?)|expenses?|opex|op\s*ex|cam|tmi|"
    r"pass[\s-]?throughs?)"
)
_OPS_EX_NNN_OWNER_RE = re.compile(
    rf"\b{_OPS_EX_NNN_OWNER}\b",
    re.IGNORECASE,
)
_OPS_EX_RATE_COMPOUND_RE = re.compile(
    rf"\b{_OPS_EX_NNN_OWNER}\s+(?P<rate>rate)\b",
    re.IGNORECASE,
)
_NNN_RELATIONAL_OBJECT_RE = re.compile(
    rf"\b(?:before|excluding|exclusive\s+of|net[\s-]+of|"
    rf"(?:does|do|did)\s+not\s+include|not\s+including|separate\s+from|"
    rf"on\s+top\s+of|in\s+addition\s+to)\s+"
    rf"(?:the\s+)?(?P<object>{_RENT_NNN_EXPLICIT_OWNER}|"
    rf"{_OPS_EX_NNN_OWNER})\b",
    re.IGNORECASE,
)
_NNN_DIRECT_COOWNER_RE = re.compile(
    rf"(?:\b{_RENT_NNN_EXPLICIT_OWNER}\b\s*(?:and|&|plus|/)\s*"
    rf"(?:the\s+)?\b{_OPS_EX_NNN_OWNER}\b|"
    rf"\b{_OPS_EX_NNN_OWNER}\b\s*(?:and|&|plus|/)\s*"
    rf"(?:the\s+)?\b{_RENT_NNN_EXPLICIT_OWNER}\b)[^\d$]{{0,16}}$",
    re.IGNORECASE,
)
_NNN_POSTPOSITIVE_RENT_OWNER_RE = re.compile(
    rf"^\s*{_RENT_NNN_EXPLICIT_OWNER}\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_NNN_POSTPOSITIVE_EXPENSE_OWNER_RE = re.compile(
    rf"^\s*{_OPS_EX_NNN_OWNER}(?:\s+(?:charges?|figure|rate))?\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_RENT_OFFER_AVAILABILITY_CONTEXT = (
    r"(?:offer(?:s|ed|ing)?|avail(?:able|ability)?)"
)
_RENT_RATE_SEPARATOR = r"(?:\b(?:at|for)\b|[@＠﹫,|:–—-])"
_RENT_RATE_MODIFIER = (
    r"(?:(?:approximately|approx\.?|about|around|roughly)\s+)?"
)
_CONTEXTUAL_RENT_NNN_OWNER_RES = (
    re.compile(
        rf"\b{_RENT_OFFER_AVAILABILITY_CONTEXT}\b[^$]{{0,80}}"
        rf"{_RENT_RATE_SEPARATOR}\s*{_RENT_RATE_MODIFIER}$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:\d{1,3}(?:,\d{3})*|\d{4,})\s*"
        r"(?:sf|sq\.?\s*ft\.?|square\s+f(?:ee|oo)t)"
        rf"\s*{_RENT_RATE_SEPARATOR}\s*{_RENT_RATE_MODIFIER}$",
        re.IGNORECASE,
    ),
)
_PENDING_OPS_EX_CONTEXT_RE = re.compile(
    r"\b(?:pending|tbd|unknown|not\s+(?:yet\s+)?finalized)\b",
    re.IGNORECASE,
)
_STRUCTURAL_FIELD_SEPARATOR_RE = re.compile(r"[|:–—-]")

_RATE_NUMBER = r"[0-9]{1,3}(?:\.[0-9]{1,2})?"
_RATE_UNIT_SUFFIX = rf"(?:(?:/\s*|\bper\s+)?{_OPS_EX_RATE_UNIT})"
_RATE_BASIS_WORD = (
    r"(?:monthly|annually|annual|yearly|month|annum|year|mos|mo|yr)"
)
_RATE_BASIS_SUFFIX = (
    rf"(?:\s*(?:/\s*|\bper\s+){_RATE_BASIS_WORD}\b|"
    rf"\s+{_RATE_BASIS_WORD}\b|"
    r"\s*,?\s*\bbilled\s+"
    r"(?:(?:on\s+(?:a|the)\s+)?monthly(?:\s+basis)?)\b)?"
)
_RATE_NNN_SUFFIX = r"\s*\bnnn\b"
_RATE_NNN_FIRST_SUFFIX = rf"{_RATE_NNN_SUFFIX}{_RATE_BASIS_SUFFIX}"
_RATE_TRAILING_SUFFIX = (
    rf"(?:{_RATE_NNN_FIRST_SUFFIX}|"
    rf"{_RATE_BASIS_SUFFIX}(?:{_RATE_NNN_SUFFIX})?)"
)
_RATE_REQUIRED_NNN_TRAILING_SUFFIX = (
    rf"(?:{_RATE_NNN_FIRST_SUFFIX}|"
    rf"{_RATE_BASIS_SUFFIX}{_RATE_NNN_SUFFIX})"
)
_PRIOR_RATE_FIGURE = (
    rf"\$\s*(?P<prior_value>{_RATE_NUMBER})\s*{_RATE_UNIT_SUFFIX}"
    rf"{_RATE_TRAILING_SUFFIX}"
)
_CURRENT_RATE_FIGURE = (
    rf"(?P<current_evidence>\$\s*(?P<value>{_RATE_NUMBER})\s*"
    rf"{_RATE_UNIT_SUFFIX}{_RATE_TRAILING_SUFFIX})"
)
_OPS_EX_OWNER_NNN_RATE_RE = re.compile(
    rf"\b{_OPS_EX_NNN_OWNER}\b[^\d$\n]{{0,18}}?"
    rf"(?P<current_evidence>\$\s*(?P<value>{_RATE_NUMBER})\s*"
    rf"{_RATE_UNIT_SUFFIX}{_RATE_REQUIRED_NNN_TRAILING_SUFFIX})",
    re.IGNORECASE,
)
_OPS_EX_ELLIPTICAL_CORRECTION_RE = re.compile(
    rf"\b{_OPS_EX_NNN_OWNER}\b[^\n;!?]{{0,40}}?{_PRIOR_RATE_FIGURE}"
    r"\s*(?:,\s*(?:corrected\s+to|now)|"
    r";\s*(?:correction\s*:|actually))\s*"
    rf"{_CURRENT_RATE_FIGURE}",
    re.IGNORECASE,
)
_PRONOMINAL_RATE_CORRECTION_RE = re.compile(
    rf"(?:(?P<opex_owner>\b{_OPS_EX_NNN_OWNER}\b)|"
    rf"(?P<rent_owner>\b{_RENT_NNN_EXPLICIT_OWNER}\b))"
    rf"[^\d$\n;!?]{{0,24}}?\bnot\s+{_PRIOR_RATE_FIGURE}"
    rf"\s*;\s*it\s+is\s+{_CURRENT_RATE_FIGURE}",
    re.IGNORECASE,
)
_NEGATED_RATE_RE = re.compile(
    rf"\bnot\s+\$\s*(?P<value>{_RATE_NUMBER})\s*{_RATE_UNIT_SUFFIX}"
    rf"{_RATE_TRAILING_SUFFIX}",
    re.IGNORECASE,
)


def _correction_figure_owner(
    text: str,
    start: int,
    end: Optional[int],
) -> Optional[str]:
    """Return the field inherited by a tightly bound correction figure."""
    figure_end = end if end is not None else start
    for match in _OPS_EX_ELLIPTICAL_CORRECTION_RE.finditer(text):
        numeric_start, numeric_end = match.span("value")
        if start <= numeric_start and numeric_end <= figure_end:
            prior_start, prior_end = match.span("prior_value")
            prior_owner = _figure_field_owner(
                text,
                _currency_figure_start(text, prior_start),
                prior_end,
            )
            return "opex" if prior_owner == "opex" else None
    for match in _PRONOMINAL_RATE_CORRECTION_RE.finditer(text):
        numeric_start, numeric_end = match.span("value")
        if start <= numeric_start and numeric_end <= figure_end:
            prior_start, prior_end = match.span("prior_value")
            prior_owner = _figure_field_owner(
                text,
                _currency_figure_start(text, prior_start),
                prior_end,
            )
            return prior_owner if prior_owner in {"opex", "rent"} else None
    return None


def _figure_is_negated(text: str, numeric_start: int) -> bool:
    """Return whether ``numeric_start`` is immediately governed by ``not``."""
    prefix = text[max(0, numeric_start - 24):numeric_start]
    return bool(re.search(r"\bnot\s+\$?\s*$", prefix, re.IGNORECASE))


def _nnn_clause_start(text: str, start: int) -> int:
    """Return the prior clause boundary, ignoring recognized abbreviations."""
    unit_abbreviation_periods = {
        position
        for abbreviation in re.finditer(
            r"\b(?:sq\.?\s*ft\.?|approx\.)",
            text,
            re.IGNORECASE,
        )
        for position in range(abbreviation.start(), abbreviation.end())
        if text[position] == "."
    }
    for position in range(start - 1, -1, -1):
        character = text[position]
        if character in "!?;\n":
            return position
        if character == "." and position not in unit_abbreviation_periods:
            return position
    return -1


def _figure_field_owner(text: str, start: int, end: Optional[int] = None) -> str:
    """Resolve the explicit field subject governing a nearby rate figure."""
    correction_owner = _correction_figure_owner(text, start, end)
    if correction_owner is not None:
        return correction_owner

    clause_start = _nnn_clause_start(text, start)
    prefix = text[clause_start + 1:start]
    relational_objects = [
        match.span("object")
        for match in _NNN_RELATIONAL_OBJECT_RE.finditer(prefix)
    ]
    expense_rate_spans = [
        match.span("rate")
        for match in _OPS_EX_RATE_COMPOUND_RE.finditer(prefix)
    ]

    def _is_relational_object(match: "re.Match") -> bool:
        return any(
            object_start <= match.start() and match.end() <= object_end
            for object_start, object_end in relational_objects
        )

    explicit_owners = [
        (match.start(), "opex")
        for match in _OPS_EX_NNN_OWNER_RE.finditer(prefix)
        if not _is_relational_object(match)
    ]
    explicit_owners.extend(
        (match.start(), "rent")
        for match in _RENT_NNN_EXPLICIT_OWNER_RE.finditer(prefix)
        if not _is_relational_object(match)
        and not (
            match.group(0).lower() == "rate"
            and any(
                rate_start <= match.start() and match.end() <= rate_end
                for rate_start, rate_end in expense_rate_spans
            )
        )
    )
    prefix_owner = max(explicit_owners)[1] if explicit_owners else "neutral"
    contextual_rent_matches = [
        match
        for pattern in _CONTEXTUAL_RENT_NNN_OWNER_RES
        if (match := pattern.search(prefix)) is not None
    ]
    contextual_rent = bool(contextual_rent_matches)
    if prefix_owner == "opex" and contextual_rent_matches:
        latest_expense_owner = max(
            (
                match
                for match in _OPS_EX_NNN_OWNER_RE.finditer(prefix)
                if not _is_relational_object(match)
            ),
            key=lambda match: match.start(),
            default=None,
        )
        latest_contextual_rent = max(
            contextual_rent_matches,
            key=lambda match: match.start(),
        )
        if (
            latest_expense_owner is not None
            and latest_contextual_rent.start() > latest_expense_owner.end()
        ):
            intervening = prefix[
                latest_expense_owner.end():latest_contextual_rent.start()
            ]
            if (
                _PENDING_OPS_EX_CONTEXT_RE.search(intervening)
                and _STRUCTURAL_FIELD_SEPARATOR_RE.search(intervening)
            ):
                prefix_owner = "rent"
    remainder = text[end:] if end is not None else ""
    postpositive_expense = bool(
        end is not None
        and _NNN_POSTPOSITIVE_EXPENSE_OWNER_RE.fullmatch(remainder)
    )
    postpositive_rent = bool(
        end is not None
        and _NNN_POSTPOSITIVE_RENT_OWNER_RE.fullmatch(remainder)
    )
    expense_suffix = bool(
        end is not None
        and re.match(
            r"\s*/\s*(?:cam|opex|op\s*ex|tmi|operating\s+expenses?)\b",
            text[end:],
            re.IGNORECASE,
        )
    )
    if _NNN_DIRECT_COOWNER_RE.search(prefix):
        return "conflict"
    if postpositive_expense and postpositive_rent:
        return "conflict"
    if postpositive_expense:
        return "conflict" if prefix_owner == "rent" else "opex"
    if postpositive_rent:
        return "conflict" if prefix_owner == "opex" else "rent"
    if expense_suffix and prefix_owner == "rent":
        return "conflict"
    if expense_suffix:
        return "opex"
    if prefix_owner != "neutral":
        return prefix_owner
    if contextual_rent:
        return "rent"
    return "neutral"


def _nnn_figure_owner(text: str, start: int, end: Optional[int] = None) -> str:
    """Classify ambiguous NNN evidence through the shared field resolver."""
    return _figure_field_owner(text, start, end)


_NNN_AFTER_NUMERIC_RE = re.compile(
    r"\s*(?:(?:(?:/|\bper\s+)\s*)?"
    r"(?:psf|sf|sq\.?\s*ft\.?|square\s+foot))?"
    r"\s*(?:(?:/|\bper\s+)(?:mo|mos|month|monthly|yr|year|annum)\b|"
    r"\b(?:monthly|annually|annual|yearly)\b)?"
    r"\s*\bnnn\b",
    re.IGNORECASE,
)


def _currency_figure_start(text: str, numeric_start: int) -> int:
    """Include an adjacent dollar sign when resolving a numeric owner."""
    start = numeric_start
    while start > 0 and text[start - 1].isspace():
        start -= 1
    if start > 0 and text[start - 1] == "$":
        return start - 1
    return numeric_start


def _nnn_suffix_end(text: str, numeric_end: int) -> Optional[int]:
    """Return the end of a rate-unit-plus-NNN suffix after a numeric figure."""
    match = _NNN_AFTER_NUMERIC_RE.match(text[numeric_end:])
    return numeric_end + match.end() if match else None


def _opex_match_is_rent_basis_line(text: str, m: "re.Match") -> bool:
    """True when an ambiguous NNN hit lacks explicit expense ownership.

    Only the bare "nnn" label is ambiguous (cam/tmi/opex/operating-expense are
    unambiguous OpEx labels). A figure-first NNN value is accepted as OpEx only
    when its clause carries a nearer explicit expense owner. Rent-owned and
    neutral values remain separate negative evidence, never magnitude guesses.
    """
    matched_text = m.group(0).lower()
    if m.group(1) is None:
        # "NNN lease rate is $14" is keyword-first syntactically, but the words
        # between NNN and the figure bind it to rent rather than operating costs.
        return bool(
            re.match(r"\s*nnn\b", matched_text)
            and re.search(r"\b(?:asking\s+|base\s+|lease\s+|rental\s+)?rate\b|\brent\b", matched_text)
        )
    if not m.group(0).rstrip().lower().endswith("nnn"):
        return False
    return _nnn_figure_owner(text, m.start(), m.end()) != "opex"
# Total SF as an area (thousands-grouped or 4+ digits), not a $/SF rate figure.
_TOTAL_SF_RE = re.compile(
    r"(?<![\w$/.])((?:\d{1,3}(?:,\d{3})+)|\d{4,})\s*(?:sf|sq\.?\s*ft|square\s*f(?:ee|oo)t)\b",
    re.IGNORECASE,
)

_EXPLICIT_TOTAL_SF_RE = re.compile(
    r"(?:\btotal\b[^\d]{0,24}(\d{1,3}(?:,\d{3})+|\d{4,})\s*"
    r"(?:sq\.?\s*ft\.?|square\s+feet|sf)\b"
    r"|(\d{1,3}(?:,\d{3})+|\d{4,})\s*"
    r"(?:sq\.?\s*ft\.?|square\s+feet|sf)\s*(?:in\s+)?total\b)",
    re.IGNORECASE,
)
_COMPONENT_SF_AFTER_RE = re.compile(
    r"^\s*(?:of|is|as|dedicated\s+to|allocated\s+to|used\s+for|"
    r"consisting\s+of)\s+(?:the\s+)?"
    r"(?:office|warehouse|showroom|mezzanine|yard)\b",
    re.IGNORECASE,
)
_COMPONENT_SF_BEFORE_RE = re.compile(
    r"(?:office|warehouse|showroom|mezzanine|yard)"
    r"(?:\s+(?:area|space|component|portion))?\s*"
    r"(?:is|was|has|of|comprises?|contains?|totals?|:|=)?\s*"
    r"(?:about|approximately|approx\.?|roughly)?\s*$",
    re.IGNORECASE,
)
_MONTHLY_UNIT_RE = re.compile(
    r"\bbilled\s+(?:(?:on\s+(?:a|the)\s+)?monthly(?:\s+basis)?)\b"
    r"|(?:/\s*|\bper\s+)(?:mo|mos|month)\b"
    r"|\bmonthly\b|\bpsf\s*/?\s*mo(?:nth)?\b",
    re.IGNORECASE,
)
_ANNUAL_UNIT_RE = re.compile(r"(?:/\s*|\bper\s+)(?:yr|year|annum|annual|annually)\b", re.IGNORECASE)
_OPS_EX_ANNUAL_BASIS_RE = re.compile(
    r"(?:/\s*|\bper\s+)(?:yr|year|annum|annual|annually|yearly)\b"
    r"|\b(?:annual|annually|yearly)\b",
    re.IGNORECASE,
)
_HYPOTHETICAL_RENT_RE = re.compile(
    r"would(?:'ve| have)?\s+(?:have\s+)?been|would\s+be\b|could\s+have\s+been|might\s+have\s+been",
    re.IGNORECASE,
)
# Current asking rate that supersedes a stale prior quote on the same line:
# "we had quoted $22/SF but it is now $26/SF", "current asking is $26/SF".
# A recency marker immediately (<=25 non-figure chars) preceding a $/SF figure
# marks the CURRENT asking rent, which must win over an earlier superseded quote.
_CURRENT_ASKING_RE = re.compile(
    r"(?:\bnow\b|\bcurrently\b|\bcurrent\s+asking\b|\bincreased\s+to\b|\braised\s+to\b"
    r"|\brevised\s+to\b|\bupdated\s+to\b|\bbumped\s+(?:up\s+)?to\b|\bmoved\s+(?:up\s+)?to\b)"
    r"[^\d$]{0,25}\$?\s*([0-9]{1,3}(?:\.[0-9]{1,2})?)\s*"
    r"(?:(?:/|\s+per\s+)?\s*(?:sf|sq\.?\s*ft|square\s*foot)|/?\s*psf)(?:\s*/?\s*(?:yr|year|annum))?",
    re.IGNORECASE,
)
# TI / tenant-improvement allowances and other landlord concessions are NOT the
# asking rent — a "$30/SF in TI allowance" figure is money the landlord GIVES the
# tenant, and must never be mined as base rent. The concession word can sit on
# either side of the figure ("$30/SF TI allowance" or "TI allowance of $30/SF").
# A "credit" is a give-back ONLY when qualified by an improvement word ("TI
# credit", "improvement credit", "construction credit"). A BARE "credit" in these
# emails means tenant CREDITWORTHINESS ("strength of credit", "depending on term,
# credit and additional TI needs") and must NOT suppress a real asking rate — so
# "credit" is matched only in the qualified alternation, never on its own.
_CONCESSION_MARKER_RE = re.compile(
    r"\b(?:allowance|concession|abatement|free\s+rent|tenant\s+improvement)\b"
    r"|\b(?:t\.?i\.?|tenant\s+improvement|improvement|rent|moving|relocation"
    r"|construction|build[\s-]?out)\s+credit\b",
    re.IGNORECASE,
)


def _extract_total_annual_rent_over_sf(text: str) -> Optional[str]:
    """Derive $/SF/yr from a stated TOTAL annual rent divided by the area.

    e.g. '$105,000/yr gross on 12,000 SF' -> 105000 / 12000 = 8.75/SF/yr. Only
    fires when BOTH a large ($1k+) annual dollar total AND a ($1k+) SF area are
    present, so a normal per-SF quote ('$8.75/SF NNN') never triggers it.
    """
    if not text:
        return None
    rent_match = _TOTAL_ANNUAL_RENT_RE.search(text)
    if not rent_match:
        return None
    area_match = _AREA_SF_RE.search(text)
    if not area_match:
        return None
    try:
        total = float(rent_match.group(1).replace(",", ""))
        area = float(area_match.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if area <= 0:
        return None
    per_sf = total / area
    if per_sf < 1:
        return None
    return f"{per_sf:.2f}"


def _is_monthly_context(window: str) -> bool:
    return bool(_MONTHLY_UNIT_RE.search(window)) and not bool(_ANNUAL_UNIT_RE.search(window))


def _extract_rent_sf_yr_from_text(text: str) -> Optional[str]:
    """Best-effort deterministic fallback for common asking-rent phrases.

    Returns annualized $/SF/yr as a 2-decimal string, or None. Captures a
    broker-stated asking rate expressed with an explicit /SF token, a bare rate
    basis suffix ('$9.75 gross', '$0.82 NNN'), a combined base+opex line, a range,
    a stated total-annual-over-area, or a recency-marked "now" figure. Refuses to
    guess a rent when the broker has ruled the property non-viable on physical
    requirements or is only floating a past-tense hypothetical, and never treats a
    non-rent $/SF figure (TI allowance, taxes, parking, opex, NNN, buildout) as the
    asking rent.
    """
    if not text:
        return None

    # Broker just called the property a non-fit — do not mine a rent from it (#19).
    if _looks_like_requirements_mismatch_nonviable(text):
        return None

    # A stated total annual rent + area ('$105,000/yr gross on 12,000 SF') is a
    # rate expressed indirectly — resolve it before the per-SF patterns (HEAD).
    total_over_area = _extract_total_annual_rent_over_sf(text)
    if total_over_area:
        return total_over_area

    # 1) Combined "base + opex" line — the base rent is the FIRST figure (#19).
    combined = _COMBINED_RENT_OPEX_RE.search(text)
    if combined:
        # Past-tense hypothetical ("rent would have been $24 + $8 opex, but it's
        # leased now") is not a current asking figure.
        if not _HYPOTHETICAL_RENT_RE.search(text[max(0, combined.start() - 40): combined.end()]):
            base = float(combined.group(1))
            window = text[max(0, combined.start() - 20): min(len(text), combined.end() + 30)]
            annual = base * 12 if _is_monthly_context(window) else base
            if annual >= 1:
                return f"{annual:.2f}"

    # 2) Range — take the low end as a conservative asking rent (#19).
    rng = _RENT_RANGE_RE.search(text)
    if rng:
        if not _HYPOTHETICAL_RENT_RE.search(text[max(0, rng.start() - 40): rng.end()]):
            low = float(rng.group(1))
            window = text[max(0, rng.start() - 20): min(len(text), rng.end() + 40)]
            annual = low * 12 if _is_monthly_context(window) else low
            if annual >= 1:
                return f"{annual:.2f}"

    # 3) Recency / "now" preference — a current asking rate ("...it is now $26/SF")
    # supersedes a stale prior quote on the same line (#19).
    current = None
    for cm in _CURRENT_ASKING_RE.finditer(text):
        numeric_start, _ = cm.span(1)
        if _figure_is_negated(text, numeric_start):
            continue
        figure_owner = _figure_field_owner(
            text,
            _currency_figure_start(text, numeric_start),
            cm.end(),
        )
        if figure_owner in {"opex", "conflict"}:
            continue
        value = float(cm.group(1))
        window = text[max(0, cm.start() - 20): min(len(text), cm.end() + 30)]
        annual_value = value * 12 if _is_monthly_context(window) else value
        if annual_value < 1:
            continue
        concession_window = text[max(0, cm.start() - 30): min(len(text), cm.end() + 40)]
        if _CONCESSION_MARKER_RE.search(concession_window):
            continue
        current = f"{annual_value:.2f}"
    if current is not None:
        return current

    # 4) Generic asking-rent patterns (HEAD pattern set).
    # Rent stated with a leading rent keyword, e.g. "asking $9.75/SF/yr".
    rent_context = re.compile(
        r"\b(?:asking|base\s+rent|rent|rate)\b[^\d$]{0,24}\$?\s*([0-9]{1,3}(?:\.[0-9]{1,2})?)\s*"
        r"(?:/|\s+per\s+)?\s*(?:sf|sq\.?\s*ft|square\s*foot)(?:\s*/?\s*(?:yr|year|annum))?",
        re.IGNORECASE,
    )
    # Any "$X/SF" figure (rent keyword optional); non-rent labels are filtered below.
    # "psf" is the fused per-square-foot token brokers use (A′ FIX-16).
    dollar_per_sf = re.compile(
        r"\$?\s*([0-9]{1,3}(?:\.[0-9]{1,2})?)\s*"
        r"(?:(?:/\s*|\s+per\s+)(?:sf|sq\.?\s*ft|square\s*foot)|psf)"
        r"(?:\s*/?\s*(?:yr|year|annum))?",
        re.IGNORECASE,
    )
    # Bare rate with a lease-basis suffix but no /SF token, e.g. "$9.75 gross".
    dollar_rate_basis = re.compile(
        r"\$\s*([0-9]{1,3}(?:\.[0-9]{1,2})?)\s*" + _LEASE_BASIS + r"\b",
        re.IGNORECASE,
    )
    # Dollar-SIGN-LESS rate with an explicit lease basis, e.g. "8.75 nnn",
    # "8.75 a foot nnn" (A′ misread M33 — terse broker shorthand). A decimal is
    # required to keep this conservative; an optional psf/per-foot token may sit
    # between the figure and the basis word.
    dollar_less_basis = re.compile(
        r"(?<![$\d])([0-9]{1,2}\.[0-9]{2})\s*"
        r"(?:p\.?s\.?f\.?|per\s+(?:sq\.?\s*)?f(?:oo)?t|a\s+(?:sq\.?\s*)?f(?:oo)?t|/\s*sf|per\s+sf)?\s*"
        + _LEASE_BASIS + r"\b",
        re.IGNORECASE,
    )
    # Cents-per-SF rate with an explicit lease basis, e.g. "82 cents triple net"
    # ($0.82/SF/mo NNN). Brokers quote low industrial rates in cents/SF/month;
    # the value is inherently monthly, so it is annualized below.
    cents_basis = re.compile(
        r"(?<![$\d.])([0-9]{1,3})\s*(?:cents?|¢)\s*"
        r"(?:p\.?s\.?f\.?|per\s+(?:sq\.?\s*)?f(?:oo)?t|a\s+(?:sq\.?\s*)?f(?:oo)?t|/\s*sf|per\s+sf)?\s*"
        + _LEASE_BASIS + r"\b",
        re.IGNORECASE,
    )
    monthly_unit = re.compile(r"(?:/|\bper\s+)(?:mo|mos|month|monthly)\b|\bmonthly\b", re.IGNORECASE)
    annual_unit = re.compile(r"(?:/|\bper\s+)(?:yr|year|annum|annual|annually)\b", re.IGNORECASE)

    basis_patterns = (dollar_rate_basis, dollar_less_basis, cents_basis)
    explicit_rate_unit = re.compile(
        r"(?:p\.?s\.?f\.?|per\s+(?:sq\.?\s*)?f(?:oo)?t|"
        r"a\s+(?:sq\.?\s*)?f(?:oo)?t|/\s*sf)",
        re.IGNORECASE,
    )
    # Classify figure-local $/SF matches before the wider keyword-first pattern.
    # Otherwise "...$12.75/SF asking rent, $3.95/SF operating expenses" lets
    # "asking rent" reach across the comma and incorrectly bind to the OpEx figure.
    for pattern in (dollar_per_sf, rent_context, dollar_rate_basis, dollar_less_basis, cents_basis):
        for match in pattern.finditer(text):
            numeric_start, numeric_end = match.span(1)
            if _figure_is_negated(text, numeric_start):
                continue
            figure_start = _currency_figure_start(text, numeric_start)
            nnn_end = (
                match.end()
                if re.search(r"\bnnn\b", match.group(0), re.IGNORECASE)
                else _nnn_suffix_end(text, match.end())
            )
            figure_owner = _figure_field_owner(
                text,
                figure_start,
                nnn_end if nnn_end is not None else match.end(),
            )
            if (
                nnn_end is not None
                and figure_owner == "neutral"
                and pattern is rent_context
            ):
                # Structured attachment text can put an explicit "Asking Rate"
                # header on the line above its value. The bounded rent-context
                # match remains valid even though newline clause scoping makes
                # the figure-local owner neutral.
                figure_owner = "rent"
            if nnn_end is not None and figure_owner == "neutral" and (
                pattern in (dollar_per_sf, cents_basis)
                or (
                    pattern is dollar_less_basis
                    and explicit_rate_unit.search(match.group(0))
                )
            ):
                # A figure-first NNN rate with a real per-SF unit is established
                # broker rent shorthand even without a separate rent noun.
                figure_owner = "rent"
            if nnn_end is not None and figure_owner != "rent":
                continue
            if nnn_end is None and figure_owner in {"opex", "conflict"}:
                continue
            # Every per-SF path, including the rent-keyword pattern, must screen
            # adjacent non-rent ownership. Basis-bearing figures ignore trailing
            # labels because those may describe a separate following figure.
            check_after = pattern not in basis_patterns
            if figure_owner == "neutral" and _figure_is_non_rent(
                text,
                match.start(),
                match.end(),
                check_after=check_after,
            ):
                continue
            clause_start = _nnn_clause_start(text, figure_start)
            if (
                figure_owner == "neutral"
                and re.search(
                    r"\bnnn\b",
                    text[clause_start + 1:figure_start],
                    re.IGNORECASE,
                )
            ):
                continue
            # Cents figures are expressed in cents/SF; convert to dollars/SF.
            value = float(match.group(1)) / 100.0 if pattern is cents_basis else float(match.group(1))
            before_unit_context = text[max(0, match.start() - 40):match.start()]
            prior_figure = list(_RENT_NUMERIC_VALUE_RE.finditer(before_unit_context))
            if prior_figure:
                before_unit_context = before_unit_context[prior_figure[-1].end():]
            after_unit_context = text[match.end():min(len(text), match.end() + 50)]
            next_figure = _RENT_NUMERIC_VALUE_RE.search(after_unit_context)
            if next_figure:
                after_unit_context = after_unit_context[:next_figure.start()]
            unit_context = before_unit_context + match.group(0) + after_unit_context
            is_monthly = bool(monthly_unit.search(unit_context)) and not bool(annual_unit.search(unit_context))
            if pattern in basis_patterns and not is_monthly:
                # A bare per-SF basis rate under ~$3 is a monthly figure (e.g.
                # "$0.82 NNN" -> $9.84/yr); annual industrial rates are far higher.
                is_monthly = value < 3.0
            annual_value = value * 12 if is_monthly else value
            if annual_value < 1:
                continue
            # A TI allowance / concession figure is never the asking rent, even
            # when an explicit field subject appears elsewhere in the clause.
            # Truncate at another figure so an unrelated allowance cannot suppress
            # the actual rent ("Asking $20/SF with a $25/SF TI allowance").
            after_ctx = text[match.end(): match.end() + 40].split("$", 1)[0]
            before_ctx = text[max(0, match.start() - 22): match.start()].rsplit("$", 1)[-1]
            if (
                _CONCESSION_MARKER_RE.search(after_ctx)
                or _CONCESSION_MARKER_RE.search(before_ctx)
            ):
                continue
            # Past-tense hypothetical rent ("rent would've been $16/SF") is not a
            # current asking figure. The conditional phrase often sits INSIDE the
            # match span, so the window must reach match.end().
            if _HYPOTHETICAL_RENT_RE.search(text[max(0, match.start() - 40): match.end()]):
                continue
            return f"{annual_value:.2f}"

    return None


class _OpsExCandidate(NamedTuple):
    raw_value: Decimal
    annualized_value: Decimal
    basis: str
    numeric_span: Tuple[int, int]
    owned_span: Tuple[int, int]
    precedence: int
    source: str


def _ops_ex_basis_values(
    text: str,
    start: int,
    end: int,
    raw: str,
    numeric_span: Optional[Tuple[int, int]] = None,
    context_before: int = 15,
    context_after: int = 25,
) -> Optional[Tuple[Decimal, Decimal, str, Tuple[int, int]]]:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None

    unit_abbreviation_periods = {
        position
        for abbreviation in re.finditer(r"\bsq\.?\s*ft\.?", text, re.IGNORECASE)
        for position in range(abbreviation.start(), abbreviation.end())
        if text[position] == "."
    }

    def _is_clause_boundary(position: int) -> bool:
        character = text[position]
        if character != ".":
            return character in ";!?\n"
        if position in unit_abbreviation_periods:
            return False
        return not (
            position > 0
            and position + 1 < len(text)
            and text[position - 1].isdigit()
            and text[position + 1].isdigit()
        )

    window_start = max(0, start - context_before)
    for position in range(start - 1, window_start - 1, -1):
        if _is_clause_boundary(position):
            window_start = position + 1
            break

    window_end = min(len(text), end + context_after)
    for position in range(end, window_end):
        if _is_clause_boundary(position):
            window_end = position
            break

    window = text[window_start:window_end]
    numeric_start, numeric_end = numeric_span or (start, end)

    unit = _OPS_EX_RATE_UNIT

    def _basis_gap_is_attached(gap: str) -> bool:
        billed = re.search(r"\bbilled\s*$", gap, re.IGNORECASE)
        if billed:
            gap = gap[:billed.start()]
        gap = _MONTHLY_UNIT_RE.sub("", gap)
        gap = _OPS_EX_ANNUAL_BASIS_RE.sub("", gap)
        attached_unit = (
            rf"\s*(?:(?:(?:/|\bper(?:\s+|-))\s*)?{unit})?"
            r"[\s,:()/\-]*"
        )
        combined_equation = (
            rf"\s*=\s*\$\s*\d+(?:\.\d+)?\s*"
            rf"(?:(?:(?:/|\bper(?:\s+|-))\s*)?{unit})?"
            r"[\s,:()/\-]*"
        )
        return bool(
            re.fullmatch(attached_unit, gap, re.IGNORECASE)
            or re.fullmatch(combined_equation, gap, re.IGNORECASE)
        )

    def _owned_basis_spans(pattern: "re.Pattern", basis: str) -> List[Tuple[int, int]]:
        spans = []
        for marker_match in pattern.finditer(window):
            marker_start = window_start + marker_match.start()
            marker_end = window_start + marker_match.end()
            marker = marker_match.group(0).strip().lower()
            bare_markers = {"monthly", "annual", "annually", "yearly"}
            following_context_end = min(len(text), marker_end + 60)
            for position in range(marker_end, following_context_end):
                if _is_clause_boundary(position):
                    following_context_end = position
                    break
            following_context = text[marker_end:following_context_end]

            following_subject = re.match(
                rf"\s*(?:(?:[-:/]\s*)|\(\s*)?"
                rf"(?:for\s+)?{_OPS_EX_DIRECT_BASIS_SUBJECT}\b",
                following_context,
                re.IGNORECASE,
            )
            supporting_qualifier = re.match(
                rf"\s*(?:(?:[-:/]\s*)|\(\s*)?for\s+"
                rf"{_OPS_EX_SUPPORTING_BASIS_QUALIFIER}\b",
                following_context,
                re.IGNORECASE,
            )
            if following_subject and not supporting_qualifier:
                continue

            if marker_end <= start:
                leading_markers = (
                    {"monthly"}
                    if basis == "monthly"
                    else {"annual", "annually", "yearly"}
                )
                if marker in leading_markers and not text[marker_end:start].strip():
                    spans.append((marker_start, marker_end))
                continue

            if (
                marker in bare_markers
                and marker_start < numeric_start
                and marker_end > start
            ):
                before_marker = text[start:marker_start]
                parenthetical = (
                    bool(re.search(r"\(\s*$", before_marker))
                    and bool(re.match(r"\s*\)", text[marker_end:numeric_start]))
                )
                if not parenthetical:
                    before_marker = re.sub(
                        r"\bbilled\s*$",
                        "",
                        before_marker,
                        flags=re.IGNORECASE,
                    )
                    directly_follows_label = re.search(
                        rf"(?:{_OPS_EX_EXPLICIT_LABEL}|n\.?n\.?n\.?)"
                        r"[\s.,:()/-]*$",
                        before_marker,
                        re.IGNORECASE,
                    )
                    linking_words = re.findall(
                        r"[a-z]+",
                        text[marker_end:numeric_start],
                        re.IGNORECASE,
                    )
                    allowed_linking_words = {
                        "is", "are", "of", "at", "run", "runs", "estimated",
                        "approx", "approximately", "about", "around",
                    }
                    if (
                        not directly_follows_label
                        or any(
                            word.lower() not in allowed_linking_words
                            for word in linking_words
                        )
                    ):
                        continue

            if marker_start >= end:
                gap = text[end:marker_start]
                if re.search(
                    rf"\b{_COMBINED_TOTAL_RENT_LABEL}\b",
                    gap,
                    re.IGNORECASE,
                ):
                    continue
                if not _basis_gap_is_attached(gap):
                    continue
                if marker in bare_markers:
                    followed_by_prose = re.match(
                        r"\s*(?:(?:[-:]\s*)|\(\s*)?[a-z]",
                        following_context,
                        re.IGNORECASE,
                    )
                    if followed_by_prose and not supporting_qualifier:
                        continue

            spans.append((marker_start, marker_end))
        return spans

    monthly_spans = _owned_basis_spans(_MONTHLY_UNIT_RE, "monthly")
    annual_spans = _owned_basis_spans(_OPS_EX_ANNUAL_BASIS_RE, "annual")
    if monthly_spans and annual_spans:
        return None

    basis = "monthly" if monthly_spans else "annual"
    owned_basis_spans = monthly_spans or annual_spans
    annualized = value * Decimal("12") if basis == "monthly" else value
    if annualized < Decimal("0.01"):
        return None
    owned_start = min([start] + [span[0] for span in owned_basis_spans])
    owned_end = max([end] + [span[1] for span in owned_basis_spans])
    return (
        value,
        annualized,
        basis,
        (owned_start, owned_end),
    )


def _negative_evidence_has_monthly_basis(
    basis_values: Optional[Tuple[Decimal, Decimal, str, Tuple[int, int]]],
    raw_value: Decimal,
) -> bool:
    """Keep owned monthly evidence even when monthly and annual conflict."""
    if basis_values is not None:
        return basis_values[2] == "monthly"
    # A parsed value at or above the accepted floor reaches None only when the
    # basis resolver owns both monthly and annual markers and abstains.
    return raw_value >= Decimal("0.01")


def _combined_total_opex_evidence(text: str) -> List[tuple]:
    evidence = []
    for pattern in _COMBINED_TOTAL_OPEX_RES:
        for match in pattern.finditer(text or ""):
            start, end = match.span("value")
            try:
                raw_value = Decimal(match.group("value"))
            except InvalidOperation:
                continue
            if raw_value < Decimal("0.01"):
                continue
            basis_values = _ops_ex_basis_values(
                text,
                match.start(),
                match.end(),
                match.group("value"),
                numeric_span=(start, end),
            )
            annualized_value = (
                raw_value * Decimal("12")
                if _negative_evidence_has_monthly_basis(
                    basis_values,
                    raw_value,
                )
                else raw_value
            )
            evidence.append((start, end, raw_value, annualized_value))
    return evidence


def _combined_base_rent_evidence(text: str) -> List[tuple]:
    """Return combined-equation base rent as negative OpEx evidence."""
    evidence = []
    for match in _COMBINED_RENT_OPEX_RE.finditer(text or ""):
        start, end = match.span(1)
        try:
            raw_value = Decimal(match.group(1))
            opex_raw_value = Decimal(match.group(2))
        except InvalidOperation:
            continue
        if raw_value < Decimal("0.01"):
            continue
        opex_basis_values = _ops_ex_basis_values(
            text,
            match.start(),
            match.end(),
            match.group(2),
            numeric_span=match.span(2),
            context_before=10,
            context_after=30,
        )
        annualized_value = (
            raw_value * Decimal("12")
            if _negative_evidence_has_monthly_basis(
                opex_basis_values,
                opex_raw_value,
            )
            else raw_value
        )
        evidence.append((start, end, raw_value, annualized_value))
    return evidence


def _negated_rate_evidence(text: str) -> List[tuple]:
    """Return rate figures explicitly rejected by ``not``."""
    evidence = []
    for match in _NEGATED_RATE_RE.finditer(text or ""):
        start, end = match.span("value")
        try:
            raw_value = Decimal(match.group("value"))
        except InvalidOperation:
            continue
        if raw_value < Decimal("0.01"):
            continue
        basis_values = _ops_ex_basis_values(
            text,
            match.start(),
            match.end(),
            match.group("value"),
            numeric_span=(start, end),
            context_before=10,
            context_after=20,
        )
        annualized_value = basis_values[1] if basis_values is not None else raw_value
        evidence.append((start, end, raw_value, annualized_value))
    return evidence


def _rejected_nnn_evidence(text: str) -> List[tuple]:
    """Return non-expense NNN values separately from accepted OpEx evidence."""
    evidence = []
    for match in _OPS_EX_RE.finditer(text or ""):
        group = 1 if match.group(1) is not None else 2
        start, end = match.span(group)
        nnn_end = (
            match.end()
            if group == 1 and match.group(0).rstrip().lower().endswith("nnn")
            else _nnn_suffix_end(text, end)
        )
        if nnn_end is None:
            continue
        owner = _nnn_figure_owner(
            text,
            _currency_figure_start(text, start),
            nnn_end,
        )
        if owner == "opex":
            continue
        try:
            raw_value = Decimal(match.group(group))
        except InvalidOperation:
            continue
        basis_values = _ops_ex_basis_values(
            text,
            match.start(),
            max(match.end(), nnn_end),
            match.group(group),
            numeric_span=(start, end),
            context_after=30,
        )
        annualized_value = basis_values[1] if basis_values is not None else raw_value
        evidence.append((start, end, raw_value, annualized_value))
    return evidence


def _ops_ex_candidate_recency(text: str, match: "re.Match") -> str:
    clause_start = max(
        text.rfind(delimiter, 0, match.start())
        for delimiter in (".", "!", "?", ";", "\n")
    )
    prefix = text[clause_start + 1:match.start()]
    previous_figure = prefix.rfind("$")
    if previous_figure >= 0:
        prefix = prefix[previous_figure + 1:]

    suffix_end = min(
        (
            position
            for delimiter in (".", "!", "?", ";", "\n", "$")
            if (position := text.find(delimiter, match.end())) >= 0
        ),
        default=len(text),
    )
    suffix = text[match.end():suffix_end]
    candidate = match.group(0)

    current = (
        _OPS_EX_CURRENT_EVIDENCE_PREFIX_RE.search(prefix)
        or _OPS_EX_CURRENT_EVIDENCE_LOCAL_RE.search(candidate)
        or re.match(
            rf"^\s*{_OPS_EX_CURRENT_EVIDENCE_MARKER}\b",
            suffix,
            re.IGNORECASE,
        )
    )
    if current:
        return "current"

    stale = (
        _OPS_EX_STALE_EVIDENCE_PREFIX_RE.search(prefix)
        or _OPS_EX_STALE_EVIDENCE_LOCAL_RE.search(candidate)
        or re.match(
            rf"^\s*{_OPS_EX_STALE_EVIDENCE_MARKER}\b",
            suffix,
            re.IGNORECASE,
        )
    )
    return "stale" if stale else "ordinary"


def _ops_ex_candidates(text: str) -> List[_OpsExCandidate]:
    """Return accepted OpEx evidence ranked by recency and source specificity."""
    text = text or ""
    rejected = _combined_total_opex_evidence(text)
    candidates = []
    source_rank = {"combined": 0, "narrow": 1, "legacy": 2}

    def _append(
        match: "re.Match",
        group: Any,
        source: str,
        context_before: int = 15,
        context_after: int = 25,
    ) -> None:
        recency = _ops_ex_candidate_recency(text, match)
        if recency == "stale":
            return
        if _HYPOTHETICAL_RENT_RE.search(
            text[max(0, match.start() - 40):match.end()]
        ):
            return
        numeric_start, numeric_end = match.span(group)
        if _figure_is_negated(text, numeric_start):
            return
        if any(
            numeric_start < rejected_end and rejected_start < numeric_end
            for rejected_start, rejected_end, _, _ in rejected
        ):
            return

        if source == "narrow" and _figure_field_owner(
            text,
            _currency_figure_start(text, numeric_start),
            match.end(),
        ) != "opex":
            return

        legacy_owner = None
        if source == "legacy" and group == 2:
            nnn_end = _nnn_suffix_end(text, numeric_end)
            legacy_owner = _figure_field_owner(
                text,
                _currency_figure_start(text, numeric_start),
                nnn_end if nnn_end is not None else match.end(),
            )
            if legacy_owner in {"rent", "conflict"}:
                return

        if source == "legacy" and group == 2:
            label_text = text[match.start():numeric_start]
            rent_labels = list(re.finditer(
                rf"\b{_COMBINED_TOTAL_RENT_LABEL}\b",
                label_text,
                re.IGNORECASE,
            ))
            opex_labels = list(re.finditer(
                rf"\b(?:{_OPS_EX_EXPLICIT_LABEL}|n\.?n\.?n\.?)\b",
                label_text,
                re.IGNORECASE,
            ))
            if legacy_owner == "neutral" and rent_labels and (
                not opex_labels
                or rent_labels[-1].start() > opex_labels[-1].start()
            ):
                return

        basis_start, basis_end = match.start(), match.end()
        basis_context_before = context_before
        basis_context_after = context_after
        if match.re in (
            _OPS_EX_ELLIPTICAL_CORRECTION_RE,
            _PRONOMINAL_RATE_CORRECTION_RE,
        ):
            basis_start, basis_end = match.span("current_evidence")
            basis_context_before = 0
            basis_context_after = 0

        basis_values = _ops_ex_basis_values(
            text,
            basis_start,
            basis_end,
            match.group(group),
            numeric_span=(numeric_start, numeric_end),
            context_before=basis_context_before,
            context_after=basis_context_after,
        )
        if basis_values is not None:
            raw_value, annualized_value, basis, owned_span = basis_values
            candidates.append(
                _OpsExCandidate(
                    raw_value=raw_value,
                    annualized_value=annualized_value,
                    basis=basis,
                    numeric_span=(numeric_start, numeric_end),
                    owned_span=owned_span,
                    precedence=(0 if recency == "current" else 10)
                    + source_rank[source],
                    source=source,
                )
            )

    combined_matches = list(_COMBINED_RENT_OPEX_RE.finditer(text))
    combined_spans = [match.span() for match in combined_matches]
    for match in combined_matches:
        _append(
            match,
            2,
            "combined",
            context_before=10,
            context_after=30,
        )

    narrow_matches = sorted(
        [
            match
            for pattern in (
                _OPS_EX_COMPONENT_LIST_RE,
                _OPS_EX_RENT_MODIFIER_RE,
                _OPS_EX_OWNER_NNN_RATE_RE,
                _OPS_EX_ELLIPTICAL_CORRECTION_RE,
            )
            for match in pattern.finditer(text)
        ]
        + [
            match
            for match in _PRONOMINAL_RATE_CORRECTION_RE.finditer(text)
        ],
        key=lambda match: match.start(),
    )
    for match in narrow_matches:
        _append(match, "value", "narrow")

    legacy_matches = list(_OPS_EX_RE.finditer(text))
    legacy_matches.sort(key=lambda match: match.group(2) is None)
    for match in legacy_matches:
        if _opex_match_is_rent_basis_line(text, match):
            continue
        group = 1 if match.group(1) is not None else 2
        if match.group(group) is not None:
            start, end = match.span(group)
            if any(
                start < combined_end and combined_start < end
                for combined_start, combined_end in combined_spans
            ):
                continue
            _append(match, group, "legacy", context_after=50)
    # Current/corrected candidates use precedence 0-2. When equally specific,
    # the last correction governs; ordinary evidence preserves source order.
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate.precedence,
            (
                -candidate.numeric_span[0]
                if candidate.precedence < 10
                else candidate.numeric_span[0]
            ),
        ),
    )


def _ops_ex_winner(text: str) -> Optional[_OpsExCandidate]:
    candidates = _ops_ex_candidates(text)
    return candidates[0] if candidates else None


def _extract_ops_ex_sf_from_text(text: str) -> Optional[str]:
    """Deterministic OpEx / NNN / CAM per-SF-per-year fallback (annualized)."""
    if not text:
        return None
    if _looks_like_requirements_mismatch_nonviable(text):
        return None

    winner = _ops_ex_winner(text)
    return f"{winner.annualized_value:.2f}" if winner else None


def _sf_match_is_component(text: str, match: "re.Match") -> bool:
    before = text[max(0, match.start() - 50):match.start()]
    after = text[match.end():match.end() + 30]
    return bool(
        _COMPONENT_SF_BEFORE_RE.search(before)
        or _COMPONENT_SF_AFTER_RE.search(after)
    )


_NUMERIC_VALUE_RE = re.compile(
    r"(?<![\d.])\$?\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*([kK]?)(?![\d.])"
)
_RENT_NUMERIC_VALUE_RE = re.compile(
    r"\$?\s*((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*"
    r"(?:(?:/|per\s+)?\s*(?:sf|psf|sq\.?\s*ft|square\s+foot)|a\s+foot)",
    re.IGNORECASE,
)
_AMBIGUOUS_FACT_CUE_RE = re.compile(
    r"\b(?:old|outdated|obsolete|superseded|former|prior|previous|historical|"
    r"corrected?|correction|revised|updated|instead)\b|"
    r"\b(?:was|were)\b[^\d$]{0,32}\$?\d[\s\S]{0,160}\bnow\b[^\d$]{0,32}\$?\d|"
    r"\b(?:isn't|wasn't|aren't|weren't|not)\b\s*(?:(?:about|approximately|roughly|"
    r"around|exactly|actually|currently)\s+)?\$?\d", re.IGNORECASE)


def _normalized_numeric_value(value: Any) -> Optional[Decimal]:
    match = _NUMERIC_VALUE_RE.search(str(value or ""))
    if not match:
        return None
    try:
        normalized = Decimal(match.group(1).replace(",", ""))
        return normalized * 1000 if match.group(2) else normalized
    except InvalidOperation:
        return None


def _normalized_rent_value(value: Any) -> Optional[Decimal]:
    match = _RENT_NUMERIC_VALUE_RE.search(str(value or ""))
    if match:
        try:
            return Decimal(match.group(1).replace(",", ""))
        except InvalidOperation:
            return None
    return _normalized_numeric_value(value)


def _component_sf_values(text: str) -> set[Decimal]:
    values = set()
    for match in _TOTAL_SF_RE.finditer(text or ""):
        if _sf_match_is_component(text, match):
            normalized = _normalized_numeric_value(match.group(1))
            if normalized is not None:
                values.add(normalized)
    return values


def _extract_total_sf_from_text(text: str) -> Optional[str]:
    """Deterministic Total SF fallback; tolerates '+/- 9,000 SF' style approximations."""
    if not text:
        return None
    for explicit_total in _EXPLICIT_TOTAL_SF_RE.finditer(text):
        area_match = _TOTAL_SF_RE.search(text, explicit_total.start(), explicit_total.end())
        if area_match and _sf_match_is_component(text, area_match):
            continue
        raw_total = next(group for group in explicit_total.groups() if group)
        return str(int(raw_total.replace(",", "")))
    for m in _TOTAL_SF_RE.finditer(text):
        # A component area is not the whole property's leasable area. The model
        # can reason over component facts, but this deterministic fallback must
        # not turn "2,000 SF of office" into Total SF.
        if _sf_match_is_component(text, m):
            continue
        raw = m.group(1).replace(",", "")
        try:
            val = int(raw)
        except ValueError:
            continue
        if val >= 1000:
            return str(val)
    return None


_STREET_SUFFIX_CANONICAL = {
    "st": "street", "street": "street",
    "ave": "avenue", "av": "avenue", "avenue": "avenue",
    "rd": "road", "road": "road",
    "blvd": "boulevard", "boulevard": "boulevard",
    "dr": "drive", "drive": "drive",
    "ln": "lane", "lane": "lane",
    "ct": "court", "court": "court",
    "pl": "place", "place": "place",
    "hwy": "highway", "highway": "highway",
    "fwy": "freeway", "freeway": "freeway",
    "pkwy": "parkway", "parkway": "parkway",
}


def _street_claim_spans(text: str) -> List[tuple]:
    tokens = list(re.finditer(r"[a-z0-9]+", (text or "").lower()))
    claims = []
    for index, token_match in enumerate(tokens):
        number = token_match.group(0)
        if not number.isdigit() or not 1 <= len(number) <= 6:
            continue
        for suffix_index in range(index + 2, min(index + 6, len(tokens))):
            suffix = tokens[suffix_index].group(0)
            if suffix in STREET_SUFFIX_TOKENS:
                names = tuple(
                    match.group(0) for match in tokens[index + 1:suffix_index]
                )
                if any(name.isdigit() for name in names):
                    break
                claims.append((
                    token_match.start(),
                    tokens[suffix_index].end(),
                    number,
                    names,
                    _STREET_SUFFIX_CANONICAL.get(suffix, suffix),
                ))
                break
            if suffix.isdigit():
                break
    return claims


def _claim_identity(claim: tuple) -> tuple:
    return claim[2], claim[3], claim[4]


def _target_street_identity(target_anchor: str) -> Optional[tuple]:
    claims = _street_claim_spans((target_anchor or "").split(",", 1)[0])
    return _claim_identity(claims[0]) if claims else None


def _source_mentions_target_property(source_text: str, target_anchor: str) -> bool:
    target_identity = _target_street_identity(target_anchor)
    if bool(
        target_identity
        and any(
            _claim_identity(claim) == target_identity
            for claim in _street_claim_spans(source_text)
        )
    ):
        return True

    # Some architectural PDFs expose a completely reversed address in their
    # text layer (for example "RD AZALP GNILRETS 002"). Treat that exact
    # reversed street anchor as target evidence instead of losing the permit.
    street_tokens = re.findall(r"[a-z0-9]+", (target_anchor or "").split(",", 1)[0].lower())
    if len(street_tokens) < 3:
        return False
    normalized_anchor = " ".join(street_tokens)
    normalized_source = " ".join(re.findall(r"[a-z0-9]+", (source_text or "").lower()))
    if normalized_anchor in normalized_source:
        return True
    reversed_anchor = " ".join(token[::-1] for token in reversed(street_tokens))
    return reversed_anchor in normalized_source


_NUMERIC_PROPERTY_VALUE_RE = re.compile(r"\$?\s*\d")
_PROPERTY_CLAUSE_BREAK_RE = re.compile(r"(?<!\d)[.!?;](?!\d)|\n+")
_MARKDOWN_TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")
# Canonical subtractive Roman numerals from 1 through 3999.
_DOCUMENT_CAPTION_ROMAN_PATTERN = (
    r"(?=[ivxlcdm])m{0,3}(?:cm|cd|d?c{0,3})"
    r"(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})"
)
_DOCUMENT_CAPTION_DESIGNATOR_PATTERN = (
    r"(?:\d+(?:[.-]\d+)*(?:-?[a-z])?|[a-z](?:[-.]\d+)+|[a-z]\d+|"
    rf"{_DOCUMENT_CAPTION_ROMAN_PATTERN}|[a-z])"
)
_DOCUMENT_CAPTION_RE = re.compile(
    r"^\s*(?:table|figure|page|section|schedule|exhibit|version|revision)\s+"
    r"(?:#\s*)?"
    r"(?P<open_wrapper>\(|\[)?\s*"
    rf"{_DOCUMENT_CAPTION_DESIGNATOR_PATTERN}\s*"
    r"(?P<close_wrapper>\)|\])?"
    r"(?=$|\s|[:(–—-]|\.(?=\s))(?P<suffix>.*)$",
    re.IGNORECASE,
)
_DOCUMENT_CAPTION_LIKE_RE = re.compile(
    r"^\s*(?:table|figure|page|section|schedule|exhibit|version|revision)\s+"
    r"(?:#\s*)?(?:"
    rf"(?:[\[(]\s*)+{_DOCUMENT_CAPTION_DESIGNATOR_PATTERN}"
    r"(?=$|\s|[)\]]|[:(–—-]|\.(?=\s))|"
    rf"{_DOCUMENT_CAPTION_DESIGNATOR_PATTERN}\s*(?=[)\]])"
    r")",
    re.IGNORECASE,
)
_DOCUMENT_CAPTION_ALPHA_TOKEN_RE = re.compile(
    r"^\s*(?:table|figure|page|section|schedule|exhibit|version|revision)\s+"
    r"(?:#\s*)?(?:"
    r"(?:[\[(]\s*)+[a-z]{2,}"
    r"(?=\s*(?:[)\]]|[:(–—-])|$|\.(?=\s))|"
    r"[a-z]{2,}\s*(?=[)\]]|[:(–—-])"
    r")",
    re.IGNORECASE,
)
_UNBOUND_IDENTITY_PREFIX_RE = re.compile(
    r"^\s*(?P<label>[a-z][a-z0-9&'’/-]*"
    r"(?:\s+[a-z][a-z0-9&'’/-]*){0,7})"
    r"(?:\s*[^a-z0-9$\s]+\s*|\s+(?=\$?\d))",
    re.IGNORECASE,
)
_UNBOUND_IDENTITY_POSTFIX_RE = re.compile(
    r"(?:[|•·,;.!?>→~)\]}]+|\t+|"
    r"(?:(?<=\s)[-–—/:]+|[-–—/:]+(?=\s)))"
    r"\s*(?P<label>[a-z][a-z0-9&'’/-]*"
    r"(?:\s+[a-z][a-z0-9&'’/-]*){0,7})\s*$",
    re.IGNORECASE,
)
_KNOWN_PROPERTY_FACT_OR_SECTION_LABELS = {
    "available space", "building", "building size", "ceiling",
    "building facts", "building overview", "building summary",
    "ceiling clearance", "ceiling height", "ceiling ht", "clearance",
    "clear height", "clear ht", "dock", "dock doors", "docks",
    "drive in", "drive in doors", "drive ins", "electrical service",
    "height", "lease rate", "loading docks", "op ex", "opex",
    "operating expense", "operating expenses", "power", "power service",
    "rent", "rental rate", "size", "square footage", "total area",
    "total sf", "total size", "total space", "warehouse",
    "warehouse area", "warehouse size",
    "building details", "building highlights", "building specifications",
    "details", "features", "highlights", "key features", "property details",
    "loading details", "property facts", "property features",
    "property highlights", "property overview", "property specifications",
    "property summary",
    "specifications",
}
_PROPERTY_NAME_SUFFIX_LABEL_TOKENS = {
    "building", "campus", "center", "centre", "commons", "complex",
    "crossing", "exchange", "facility", "heights", "hub", "landing",
    "park", "place", "plaza", "point", "square", "station", "terrace",
    "tower", "village", "warehouse", "works",
}
_PROPERTY_FACT_LABEL_FAMILIES = (
    (
        {"area", "footage", "ft", "sf", "size", "space"},
        {"area", "available", "building", "feet", "foot", "footage", "ft",
         "office", "sf", "size", "space", "sq", "square", "total",
         "warehouse"},
    ),
    (
        {"ceiling", "clearance", "height", "ht"},
        {"ceiling", "clear", "clearance", "height", "ht"},
    ),
    (
        {"dock", "docks", "positions"},
        {"count", "dock", "docks", "doors", "high", "loading", "number",
         "positions", "total", "truck"},
    ),
    (
        {"drive", "grade"},
        {"count", "doors", "drive", "grade", "in", "level", "number",
         "positions", "total"},
    ),
    (
        {"amperage", "amps", "electrical", "power", "voltage"},
        {"amperage", "amps", "capacity", "electrical", "phase", "power",
         "service", "supply", "voltage", "volts"},
    ),
    (
        {"lease", "rate", "rent"},
        {"annual", "asking", "lease", "monthly", "nnn", "per", "psf",
         "rate", "rent", "sf", "year", "yr"},
    ),
    (
        {"cam", "charges", "expense", "expenses", "op", "opex", "ops"},
        {"annual", "cam", "charges", "ex", "expense", "expenses", "nnn",
         "op", "opex", "operating", "ops", "per", "psf", "sf"},
    ),
)
_STANDALONE_IDENTITY_LINE_RE = re.compile(
    r"^\s*(?P<label>[a-z][a-z0-9&'’/-]*"
    r"(?:\s+[a-z][a-z0-9&'’/-]*){0,7})"
    r"\s*(?:[^a-z0-9$\s]+)?\s*$",
    re.IGNORECASE,
)


def _is_property_fact_or_section_label(label: str) -> bool:
    """Recognize mapped-field synonyms without blessing property names."""
    normalized = " ".join(re.findall(r"[a-z0-9]+", (label or "").lower()))
    if normalized in _KNOWN_PROPERTY_FACT_OR_SECTION_LABELS:
        return True
    tokens = normalized.split()
    if not tokens or tokens[-1] in _PROPERTY_NAME_SUFFIX_LABEL_TOKENS:
        return False
    token_set = set(tokens)
    return any(
        token_set & required and token_set <= allowed
        for required, allowed in _PROPERTY_FACT_LABEL_FAMILIES
    )


def _contains_property_fact_label(text: str) -> bool:
    """Find a semantic mapped-field label on either side of its value."""
    tokens = re.findall(r"(?<![a-z0-9])[a-z][a-z0-9]*", (text or "").lower())
    return any(
        _is_property_fact_or_section_label(" ".join(tokens[start:end]))
        for start in range(len(tokens))
        for end in range(start + 1, min(start + 8, len(tokens)) + 1)
    )


def _merge_comma_address_cells(
    cells: List[str],
    expected_count: int,
) -> List[str]:
    """Rejoin address-location fragments while preserving table columns."""
    street_cell_count = sum(bool(_street_claim_spans(cell)) for cell in cells)
    if (
        expected_count < 1
        or len(cells) <= expected_count
        or street_cell_count < 1
        or street_cell_count > expected_count
    ):
        return cells

    merged = []
    cursor = 0
    while len(merged) < expected_count and cursor < len(cells):
        columns_left = expected_count - len(merged)
        if columns_left == 1:
            merged.append(", ".join(cells[cursor:]).strip())
            cursor = len(cells)
            break

        end = cursor + 1
        if _street_claim_spans(cells[cursor]):
            # Consume city/state/ZIP fragments, but stop at the next address
            # and reserve at least one fragment for every remaining column.
            latest_end = len(cells) - (columns_left - 1)
            while (
                end < latest_end
                and not _street_claim_spans(cells[end])
            ):
                end += 1
        merged.append(", ".join(cells[cursor:end]).strip())
        cursor = end

    if cursor != len(cells) or len(merged) != expected_count:
        return cells
    return merged


def _property_table_cells(
    row: str,
    expected_count: Optional[int] = None,
    identity_row: bool = False,
) -> List[str]:
    """Split an extracted table row without splitting numeric commas."""
    row = (row or "").strip()
    parsed_csv = False
    merge_address_fragments = False
    if "|" in row:
        cells = row.split("|")
    elif "\t" in row:
        cells = re.split(r"\t+", row)
    elif '"' in row and "," in row:
        cells = next(csv.reader([row], skipinitialspace=True))
        parsed_csv = True
    elif re.search(r",\s+", row):
        cells = re.split(r",\s+", row)
        merge_address_fragments = True
    else:
        return [row]
    if parsed_csv:
        cells = [
            " ".join(cell.replace('"', " ").split())
            for cell in cells
        ]
    else:
        cells = [cell.strip() for cell in cells]
    while cells and not cells[0]:
        cells.pop(0)
    while cells and not cells[-1]:
        cells.pop()
    if identity_row and expected_count is not None and merge_address_fragments:
        cells = _merge_comma_address_cells(cells, expected_count)
    return cells


def _postfixed_unbound_identity(fragment: str, target_anchor: str) -> bool:
    """Whether a fact is followed by an unknown, non-address identity."""
    identity = _UNBOUND_IDENTITY_POSTFIX_RE.search(fragment)
    if not identity:
        return False
    if _source_mentions_target_property(fragment[identity.start():], target_anchor):
        return False
    fact_text = fragment[:identity.start()]
    if (
        not _NUMERIC_PROPERTY_VALUE_RE.search(fact_text)
        or not _contains_property_fact_label(fact_text)
    ):
        return False
    label = " ".join(re.findall(r"[a-z0-9]+", identity.group("label").lower()))
    return not _is_property_fact_or_section_label(label)


def _property_clause_spans(text: str) -> List[tuple]:
    """Split prose without treating decimal or street-suffix periods as boundaries."""
    text = text or ""
    street_claim_ends = {claim[1] for claim in _street_claim_spans(text)}
    spans = []
    start = 0
    for boundary in _PROPERTY_CLAUSE_BREAK_RE.finditer(text):
        if boundary.group(0) == "." and boundary.start() in street_claim_ends:
            continue
        if start < boundary.start():
            spans.append((start, boundary.start()))
        start = boundary.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _property_table_clause_spans(text: str) -> List[tuple]:
    """Return logical table rows without Markdown alignment separators."""
    spans = _property_clause_spans(text)
    structural_caption_spans = _structural_caption_span_set(text, spans)
    return [
        span for span in spans
        if span not in structural_caption_spans and not (
            "|" in text[span[0]:span[1]]
            and (cells := _property_table_cells(text[span[0]:span[1]]))
            and all(
                _MARKDOWN_TABLE_SEPARATOR_CELL_RE.fullmatch(cell)
                for cell in cells
            )
        )
    ]


_DOCUMENT_CAPTION_QUOTE_PAIRS = (
    ('\\"', '\\"'),
    ("\\'", "\\'"),
    ("\\“", "\\”"),
    ("\\‘", "\\’"),
    ('"', '"'),
    ("'", "'"),
    ("“", "”"),
    ("‘", "’"),
)


def _normalize_caption_quote_edges(text: str) -> tuple:
    """Strip quote layers and report unmatched or mixed edge syntax."""
    text = (text or "").strip()
    malformed = False
    while text:
        opening = next((
            token for token, _ in _DOCUMENT_CAPTION_QUOTE_PAIRS
            if text.startswith(token)
        ), None)
        closing = next((
            token for _, token in _DOCUMENT_CAPTION_QUOTE_PAIRS
            if text.endswith(token)
        ), None)
        if not opening and not closing:
            break

        valid_pair = any(
            opening == expected_opening and closing == expected_closing
            for expected_opening, expected_closing
            in _DOCUMENT_CAPTION_QUOTE_PAIRS
        )
        if not valid_pair or len(text) < len(opening) + len(closing):
            malformed = True

        start = len(opening) if opening else 0
        end = len(text) - len(closing) if closing else len(text)
        text = text[start:max(start, end)].strip()
    return text, malformed


def _document_caption_candidate_text(text: str) -> tuple:
    """Extract one safe logical table cell for caption classification."""
    raw_text = (text or "").strip()
    unquoted_text, malformed_quotes = _normalize_caption_quote_edges(raw_text)
    cells = _property_table_cells(unquoted_text)
    if len(cells) != 1:
        if "|" in unquoted_text:
            return None, False
        return raw_text, malformed_quotes
    caption_text, malformed_cell_quotes = _normalize_caption_quote_edges(cells[0])
    return caption_text, malformed_quotes or malformed_cell_quotes


def _document_caption_verdict(text: str) -> Optional[str]:
    """Classify a document caption by its designator and residual title."""
    caption_text, malformed_quotes = _document_caption_candidate_text(text)
    if caption_text is None:
        return None
    caption = _DOCUMENT_CAPTION_RE.match(caption_text)
    caption_like = bool(
        caption
        or _DOCUMENT_CAPTION_LIKE_RE.match(caption_text)
        or _DOCUMENT_CAPTION_ALPHA_TOKEN_RE.match(caption_text)
    )
    if malformed_quotes:
        return "competing" if caption_like else None
    if not caption:
        return "competing" if caption_like else None
    wrapper_pair = (
        caption.group("open_wrapper"),
        caption.group("close_wrapper"),
    )
    if wrapper_pair not in {(None, None), ("(", ")"), ("[", "]")}:
        return "competing"
    suffix = caption.group("suffix").strip()
    if not suffix or re.fullmatch(
        r"(?:of|/)\s*[a-z0-9]+(?:[-.][a-z0-9]+)*",
        suffix,
        re.IGNORECASE,
    ):
        return "structural"

    parenthesized = suffix.startswith("(")
    title = re.sub(r"^(?::|[-–—]|\.(?=\s)|\()\s*", "", suffix).strip()
    if parenthesized and title.endswith(")"):
        title = title[:-1].rstrip()
    if not title or _is_property_fact_or_section_label(title):
        return "structural"
    return "competing"


def _structural_caption_span_set(text: str, spans: List[tuple]) -> set:
    """Return complete spans whose caption syntax is verified structural."""
    return {
        span for span in spans
        if _document_caption_verdict(text[span[0]:span[1]]) == "structural"
    }


def _span_within_any(span: tuple, containing_spans: set) -> bool:
    """Whether a span is wholly contained by one of the supplied spans."""
    return any(
        start <= span[0] and span[1] <= end
        for start, end in containing_spans
    )


def _aligned_property_table_cells(text: str) -> List[tuple]:
    """Return aligned label/value/identity cells from three-row tables."""
    clause_spans = _property_table_clause_spans(text)
    aligned = []
    for label_span, value_span, identity_span in zip(
        clause_spans,
        clause_spans[1:],
        clause_spans[2:],
    ):
        label_cells = _property_table_cells(text[label_span[0]:label_span[1]])
        value_cells = _property_table_cells(text[value_span[0]:value_span[1]])
        if not label_cells or len(label_cells) != len(value_cells):
            continue
        identity_cells = _property_table_cells(
            text[identity_span[0]:identity_span[1]],
            expected_count=len(label_cells),
            identity_row=True,
        )
        if (
            len(identity_cells) != len(label_cells)
            or any(
                not cell
                for cells in (label_cells, value_cells, identity_cells)
                for cell in cells
            )
        ):
            continue
        aligned.extend(
            (label_span, identity_span, label, value, identity)
            for label, value, identity in zip(
                label_cells,
                value_cells,
                identity_cells,
            )
        )
    return aligned


def _property_identity_cell_verdict(
    identity_text: str,
    target_anchor: str,
) -> str:
    """Classify one table identity cell as target, competing, or other."""
    claims = _street_claim_spans(identity_text)
    target_identity = _target_street_identity(target_anchor)
    if claims:
        if not target_identity or any(
            _claim_identity(claim) != target_identity for claim in claims
        ):
            return "competing"
        residual = []
        cursor = 0
        for claim in claims:
            residual.append(identity_text[cursor:claim[0]])
            cursor = claim[1]
        residual.append(identity_text[cursor:])
        residual_tokens = set(re.findall(r"[a-z0-9]+", " ".join(residual).lower()))
        location_tokens = set(re.findall(
            r"[a-z0-9]+",
            (target_anchor or "").partition(",")[2].lower(),
        ))
        return "target" if residual_tokens <= location_tokens else "competing"
    if _source_mentions_target_property(identity_text, target_anchor):
        return "target"
    identity = _STANDALONE_IDENTITY_LINE_RE.match(identity_text)
    if identity and not _is_property_fact_or_section_label(identity.group("label")):
        return "competing"
    return "other"


def _property_fact_label_cell(cell: str) -> bool:
    """Whether one parsed table cell is a mapped property-fact label."""
    label_line = _STANDALONE_IDENTITY_LINE_RE.match(cell or "")
    return bool(
        label_line
        and _is_property_fact_or_section_label(label_line.group("label"))
    )


def _split_target_location_identity(
    identity_cells: List[str],
    target_anchor: str,
) -> bool:
    """Detect a target address whose city was mistaken for another column."""
    target_identity = _target_street_identity(target_anchor)
    location_tokens = set(re.findall(
        r"[a-z0-9]+",
        (target_anchor or "").partition(",")[2].lower(),
    ))
    if not target_identity or not location_tokens:
        return False
    for current, following in zip(identity_cells, identity_cells[1:]):
        claims = _street_claim_spans(current)
        following_tokens = set(re.findall(r"[a-z0-9]+", following.lower()))
        if (
            any(_claim_identity(claim) == target_identity for claim in claims)
            and following_tokens
            and following_tokens <= location_tokens
        ):
            return True
    return False


def _property_table_shape_spans(
    text: str,
    target_anchor: str,
) -> tuple:
    """Return structured label rows and malformed table spans."""
    clause_spans = _property_table_clause_spans(text)
    structured_labels = set()
    malformed_spans = []
    table_prefixes = {}
    for label_span, value_span in zip(clause_spans, clause_spans[1:]):
        label_cells = _property_table_cells(text[label_span[0]:label_span[1]])
        value_cells = _property_table_cells(text[value_span[0]:value_span[1]])
        recognized_labels = sum(
            _property_fact_label_cell(cell)
            for cell in label_cells
            if cell
        )
        if not recognized_labels or not any(
            _NUMERIC_PROPERTY_VALUE_RE.search(cell)
            for cell in value_cells
            if cell
        ):
            continue
        structured_labels.add(label_span)
        table_prefixes[label_span] = (label_cells, value_cells)
        if (
            len(label_cells) != len(value_cells)
            or any(
                not cell
                for cells in (label_cells, value_cells)
                for cell in cells
            )
        ):
            malformed_spans.append((label_span[0], value_span[1]))

    for label_span, _, identity_span in zip(
        clause_spans,
        clause_spans[1:],
        clause_spans[2:],
    ):
        if label_span not in table_prefixes:
            continue
        label_cells, value_cells = table_prefixes[label_span]

        identity_cells = _property_table_cells(
            text[identity_span[0]:identity_span[1]],
            expected_count=len(label_cells),
            identity_row=True,
        )
        identity_verdicts = [
            _property_identity_cell_verdict(cell, target_anchor)
            for cell in identity_cells
            if cell
        ]
        if not any(
            verdict in {"target", "competing"}
            for verdict in identity_verdicts
        ):
            continue

        cell_rows = (label_cells, value_cells, identity_cells)
        malformed = (
            len({len(cells) for cells in cell_rows}) != 1
            or any(not cell for cells in cell_rows for cell in cells)
            or _split_target_location_identity(identity_cells, target_anchor)
        )
        if malformed:
            malformed_spans.append((label_span[0], identity_span[1]))
    return structured_labels, malformed_spans


def _unbound_identity_fact_spans(
    text: str,
    target_anchor: str,
) -> List[tuple]:
    """Find fact clauses carrying a new identity without an exact address."""
    text = text or ""
    if not _source_mentions_target_property(text, target_anchor):
        return []
    clause_spans = _property_clause_spans(text)
    structural_caption_spans = _structural_caption_span_set(text, clause_spans)
    scannable_clause_spans = [
        span for span in clause_spans
        if span not in structural_caption_spans
    ]
    structured_table_labels, malformed_table_spans = (
        _property_table_shape_spans(text, target_anchor)
    )
    unbound_spans = list(malformed_table_spans)

    # PDF table extraction often places a property name on one line and its
    # first value on the next. Treat an unknown standalone heading followed by
    # a mapped property fact as a structural identity boundary while preserving
    # explicit field/section labels such as "Clear Height" and "Highlights".
    for current, following in zip(
        scannable_clause_spans,
        scannable_clause_spans[1:],
    ):
        if current in structured_table_labels:
            continue
        current_text = text[current[0]:current[1]]
        following_text = text[following[0]:following[1]]
        identity_line = _STANDALONE_IDENTITY_LINE_RE.match(current_text)
        if (
            not identity_line
            or _source_mentions_target_property(current_text, target_anchor)
            or _source_mentions_target_property(following_text, target_anchor)
            or not _NUMERIC_PROPERTY_VALUE_RE.search(following_text)
        ):
            continue
        line_label = " ".join(
            re.findall(r"[a-z0-9]+", identity_line.group("label").lower())
        )
        if not _is_property_fact_or_section_label(line_label):
            unbound_spans.append((current[0], following[1]))

    # The same table layout can extract in the opposite order: mapped fact
    # first, property heading second. Start the boundary at the fact so its
    # value cannot remain in the preceding target-bound segment.
    for current, following in zip(
        scannable_clause_spans,
        scannable_clause_spans[1:],
    ):
        current_text = text[current[0]:current[1]]
        following_text = text[following[0]:following[1]]
        postfixed_identity = _STANDALONE_IDENTITY_LINE_RE.match(following_text)
        if (
            postfixed_identity
            and not _source_mentions_target_property(following_text, target_anchor)
            and _NUMERIC_PROPERTY_VALUE_RE.search(current_text)
            and _contains_property_fact_label(current_text)
        ):
            postfixed_label = " ".join(re.findall(
                r"[a-z0-9]+",
                postfixed_identity.group("label").lower(),
            ))
            if not _is_property_fact_or_section_label(postfixed_label):
                unbound_spans.append((current[0], following[1]))

    # Extracted tables can put mapped labels, values, and property identities
    # on three aligned rows with one or more columns. Bind each label/value cell
    # before deciding whether its identity cell is an unknown property.
    for label_span, identity_span, label_text, value_text, identity_text in (
        _aligned_property_table_cells(text)
    ):
        label_line = _STANDALONE_IDENTITY_LINE_RE.match(label_text)
        if (
            not label_line
            or not _is_property_fact_or_section_label(label_line.group("label"))
            or not _NUMERIC_PROPERTY_VALUE_RE.search(value_text)
            or _property_identity_cell_verdict(identity_text, target_anchor)
            != "competing"
        ):
            continue
        unbound_spans.append((label_span[0], identity_span[1]))

    for clause_start, clause_end in scannable_clause_spans:
        clause = text[clause_start:clause_end]
        caption_verdict = _document_caption_verdict(clause)
        if _source_mentions_target_property(clause, target_anchor):
            continue
        if caption_verdict == "competing":
            unbound_spans.append((clause_start, clause_end))
            continue
        identity = _UNBOUND_IDENTITY_PREFIX_RE.search(clause)
        if (
            not identity
            or not _NUMERIC_PROPERTY_VALUE_RE.search(clause[identity.end():])
        ):
            continue
        label = " ".join(re.findall(r"[a-z0-9]+", identity.group("label").lower()))
        # Brochure headings are ambiguous: they can be ordinary fact/section
        # labels ("clear height") or property names ("Oak Center" / "Westgate").
        # Preserve only the explicit label set; every unknown heading fails
        # closed as a new property identity.
        if not _is_property_fact_or_section_label(label):
            unbound_spans.append((clause_start, clause_end))

    for clause_start, clause_end in scannable_clause_spans:
        clause = text[clause_start:clause_end]
        if _postfixed_unbound_identity(clause, target_anchor):
            unbound_spans.append((clause_start, clause_end))

    # A period after a street suffix is ambiguous: it may be the abbreviation
    # in "Main St." or a real sentence boundary before another property. Scan
    # punctuation-delimited segments independently so `100 Main St. Oak Center
    # | Docks: 6` cannot inherit the target address merely because the normal
    # clause splitter preserved `St.`.
    for segment in re.finditer(r"(?:^|[.!?;\n])(?P<body>[^.!?;\n]+)", text):
        segment_start, segment_end = segment.span("body")
        segment_text = segment.group("body")
        if _span_within_any(
            (segment_start, segment_end),
            structural_caption_spans,
        ):
            continue
        caption_verdict = _document_caption_verdict(segment_text)
        if (
            _source_mentions_target_property(segment_text, target_anchor)
            or caption_verdict == "structural"
        ):
            continue
        if caption_verdict == "competing":
            unbound_spans.append((segment_start, segment_end))
            continue
        identity = _UNBOUND_IDENTITY_PREFIX_RE.search(segment_text)
        if (
            not identity
            or not _NUMERIC_PROPERTY_VALUE_RE.search(segment_text[identity.end():])
        ):
            continue
        label = " ".join(re.findall(r"[a-z0-9]+", identity.group("label").lower()))
        if not _is_property_fact_or_section_label(label):
            unbound_spans.append((segment_start, segment_end))

    for segment in re.finditer(r"(?:^|[.!?;\n])(?P<body>[^.!?;\n]+)", text):
        segment_start, segment_end = segment.span("body")
        if _span_within_any(
            (segment_start, segment_end),
            structural_caption_spans,
        ):
            continue
        if _postfixed_unbound_identity(segment.group("body"), target_anchor):
            unbound_spans.append((segment_start, segment_end))
    return sorted(set(unbound_spans))


def _attachment_property_verdict(source_text: str, target_anchor: str) -> str:
    """Classify attachment text as target-bound, competing, or addressless."""
    claims = _street_claim_spans(source_text)
    if not claims:
        return "addressless"
    target_identity = _target_street_identity(target_anchor)
    matches = [_claim_identity(claim) == target_identity for claim in claims]
    if all(matches):
        return (
            "mixed"
            if _unbound_identity_fact_spans(source_text, target_anchor)
            else "target"
        )
    if any(matches):
        return "mixed"
    return "competing"


def _attachment_can_supply_target_facts(
    source_text: str,
    target_anchor: str,
    fresh_text: str,
) -> bool:
    verdict = _attachment_property_verdict(source_text, target_anchor)
    if verdict == "target":
        return True
    if verdict in {"competing", "mixed"}:
        return False
    return _source_mentions_target_property(fresh_text, target_anchor)


_PDF_RENT_FIGURE_RE = re.compile(
    r"\$?\s*\d{1,3}(?:\.\d+)?\s*(?:(?:/|per\s+)?\s*(?:sf|psf)|a\s+foot)",
    re.IGNORECASE,
)


def _attachment_can_supply_target_rent(
    source_text: str,
    target_anchor: str,
    fresh_text: str,
) -> bool:
    verdict = _attachment_property_verdict(source_text, target_anchor)
    if verdict != "mixed":
        return _attachment_can_supply_target_facts(source_text, target_anchor, fresh_text)
    rent_match = _PDF_RENT_FIGURE_RE.search(source_text or "")
    target_identity = _target_street_identity(target_anchor)
    preceding_claims = [
        claim for claim in _street_claim_spans(source_text)
        if rent_match and claim[1] <= rent_match.start()
    ]
    return bool(
        target_identity
        and preceding_claims
        and _claim_identity(preceding_claims[-1]) == target_identity
    )


_CURRENT_PROPERTY_AFFIRMATION_RE = re.compile(
    r"\b(?:yes[,\s]+)?this\s+(?:space|property|unit|building)\b",
    re.IGNORECASE,
)

_ALTERNATE_PROPERTY_TRANSITION_RE = re.compile(
    r"\b(?:we|i)\s+also\s+have\b|\b(?:also|another|alternative|replacement|other)\s+"
    r"(?:building|property|space|suite|unit|option)\b",
    re.IGNORECASE,
)


def _event_address_boundary(text: str, events: List[dict]) -> Optional[int]:
    """Find the earliest explicit alternate-property label in the fresh reply."""
    candidates = []
    for event in events:
        address = str((event or {}).get("address") or "")
        tokens = [
            token for token in re.findall(r"[a-z0-9]+", address.lower())
            if token != "tbd"
        ]
        if not tokens:
            continue
        # Try the complete label, then progressively shorter distinctive tails.
        for width in range(len(tokens), 1, -1):
            phrase = r"\b" + r"[^a-z0-9]+".join(map(re.escape, tokens[-width:])) + r"\b"
            match = re.search(phrase, text, re.IGNORECASE)
            if match:
                candidates.append(match.start())
                break
    return min(candidates) if candidates else None


_CEILING_HEIGHT_VALUE_RES = (
    re.compile(
        r"\b(?:clear(?:\s+height)?|ceiling(?:\s+(?:height|ht|clearance))?|clearance)"
        r"\s*(?:is|of|at|:|=|-)?\s*(\d{1,3}(?:\.\d+)?)\s*"
        r"(?:feet|foot|ft\.?|['’])?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(\d{1,3}(?:\.\d+)?)\s*(?:feet|foot|ft\.?|['’])?\s*"
        r"(?:clear(?:\s+height)?|ceiling(?:\s+height)?|clearance)\b",
        re.IGNORECASE,
    ),
)


def _ceiling_height_values(text: str) -> set[Decimal]:
    return {
        normalized
        for pattern in _CEILING_HEIGHT_VALUE_RES
        for match in pattern.finditer(text or "")
        if (normalized := _normalized_numeric_value(match.group(1))) is not None
    }


def _update_supported_in_current_segment(update: dict, text: str) -> bool:
    """Require a proposed current-row value to be evidenced before the alternate."""
    column = str((update or {}).get("column") or "").strip().lower()
    value = (update or {}).get("value")
    if not column or value is None:
        return False

    expected = _normalized_numeric_value(value)
    if "total sf" in column or "square footage" in column:
        return expected == _normalized_numeric_value(_extract_total_sf_from_text(text))
    if "rent" in column and "gross" not in column:
        return expected == _normalized_numeric_value(_extract_rent_sf_yr_from_text(text))
    if "ops" in column or "opex" in column or "operating" in column:
        return expected == _normalized_numeric_value(_extract_ops_ex_sf_from_text(text))
    if "drive" in column or "grade" in column:
        return expected == _normalized_numeric_value(_extract_drive_in_count_from_text(text))
    if "dock" in column:
        return expected == _normalized_numeric_value(_extract_dock_count_from_text(text))
    if "ceiling" in column or "clear height" in column or "clearance" in column:
        return expected in _ceiling_height_values(text)

    normalized_value = " ".join(re.findall(r"[a-z0-9]+", str(value).lower()))
    normalized_text = " ".join(re.findall(r"[a-z0-9]+", text.lower()))
    return bool(normalized_value and normalized_value in normalized_text)


def _exact_target_clause_segments(
    source_text: str,
    target_anchor: str,
) -> List[str]:
    """Return only clauses whose facts carry an exact target-address binding."""
    target_identity = _target_street_identity(target_anchor)
    if target_identity is None:
        return []

    segments = []
    for clause_start, clause_end in _property_clause_spans(source_text):
        clause = source_text[clause_start:clause_end]
        claims = _street_claim_spans(clause)
        if not claims:
            if _source_mentions_target_property(clause, target_anchor):
                segments.append(clause)
            continue
        matches = [_claim_identity(claim) == target_identity for claim in claims]
        if all(matches):
            segments.append(clause)
            continue
        for index, claim in enumerate(claims):
            if not matches[index]:
                continue
            next_claim_start = (
                claims[index + 1][0] if index + 1 < len(claims) else None
            )
            segments.append(clause[claim[0]:next_claim_start])
    return segments


def _target_bound_table_fact_segments(
    source_text: str,
    target_anchor: str,
) -> List[str]:
    """Return aligned table facts whose identity cell is the exact target."""
    segments = []
    for _, _, label_text, value_text, identity_text in (
        _aligned_property_table_cells(source_text)
    ):
        label_line = _STANDALONE_IDENTITY_LINE_RE.match(label_text)
        if (
            label_line
            and _is_property_fact_or_section_label(label_line.group("label"))
            and _NUMERIC_PROPERTY_VALUE_RE.search(value_text)
            and _property_identity_cell_verdict(identity_text, target_anchor)
            == "target"
        ):
            label_tokens = set(re.findall(r"[a-z0-9]+", label_text.lower()))
            is_area_label = bool(
                label_tokens & {"area", "footage", "ft", "sf", "size", "space"}
                and not label_tokens & {
                    "cam", "charges", "expense", "expenses", "lease", "op",
                    "opex", "ops", "rate", "rent",
                }
            )
            segments.append(
                f"{value_text} SF" if is_area_label else f"{value_text} {label_text}"
            )
    return segments


def _target_bound_source_segments(
    source_text: str,
    target_anchor: str,
    verdict: str,
) -> List[str]:
    """Return only source regions whose nearest property claim is the target."""
    if verdict == "target":
        return [source_text]
    if verdict != "mixed":
        return []

    target_identity = _target_street_identity(target_anchor)
    claims = _street_claim_spans(source_text)
    if target_identity is None:
        return []
    property_boundaries = sorted(
        {claim[0] for claim in claims}
        | {
            start
            for start, _ in _unbound_identity_fact_spans(
                source_text,
                target_anchor,
            )
        }
    )
    segments = [
        source_text[
            claim[0]:next(
                (
                    boundary
                    for boundary in property_boundaries
                    if boundary > claim[0]
                ),
                None,
            )
        ]
        for claim in claims
        if _claim_identity(claim) == target_identity
    ]
    segments.extend(_target_bound_table_fact_segments(source_text, target_anchor))
    return segments


def _suppress_cross_property_current_row_updates(
    proposal: dict,
    conversation: List[dict],
    target_anchor: str,
) -> dict:
    """Keep alternate-property facts from being applied to the current row."""
    if not proposal:
        return proposal

    events = proposal.get("events") or []
    event_types = {(event or {}).get("type") for event in events}
    if "property_unavailable" in event_types:
        proposal["updates"] = [
            update for update in (proposal.get("updates") or [])
            if str((update or {}).get("value") or "").strip() == "0"
            and (update or {}).get("reason") == _TARGET_ZERO_UPDATE_REASON
        ]
        return proposal
    if "new_property" not in event_types:
        return proposal

    fresh_text = _fresh_inbound_text(conversation)
    new_property_events = [
        event for event in events if (event or {}).get("type") == "new_property"
    ]
    address_boundary = _event_address_boundary(fresh_text, new_property_events)
    transition = _ALTERNATE_PROPERTY_TRANSITION_RE.search(fresh_text)
    boundary_candidates = [
        position for position in (
            address_boundary,
            transition.start() if transition else None,
        ) if position is not None
    ]
    if not boundary_candidates:
        proposal["updates"] = []
        return proposal

    current_segment = fresh_text[:min(boundary_candidates)]
    explicitly_names_target = _source_mentions_target_property(
        current_segment, target_anchor
    )
    has_transitioned_current_affirmation = bool(
        transition
        and _CURRENT_PROPERTY_AFFIRMATION_RE.search(current_segment)
    )
    if not explicitly_names_target and not has_transitioned_current_affirmation:
        proposal["updates"] = []
        return proposal

    proposal["updates"] = [
        update for update in (proposal.get("updates") or [])
        if _update_supported_in_current_segment(update, current_segment)
    ]
    return proposal


_ROUTE_ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+(?:[a-z]+\s+){0,3}(?:sc|us|fm|sr)[-\s]?\d+\b",
    re.IGNORECASE,
)
_ATTACHMENT_NAME_NOISE = {
    "brochure", "brochures", "flyer", "flyers", "marketing", "package",
    "offering", "memorandum", "om", "pdf", "floorplan", "floorplans",
}
_ATTACHMENT_COPY_SUFFIX_RE = re.compile(
    r"\s*(?:\(\d+\)|(?:version|ver|revision|rev)\s*\d+|v\d+)"
    r"(?=\s*(?:\.[a-z0-9]+)?$)",
    re.IGNORECASE,
)

_NATIVE_IMAGE_MANIFEST_UNIQUE_KEYS = frozenset({
    "property_binding",
    "binding_method",
    "image_meta",
})
_ATTACHMENT_ROUTING_KEYS = frozenset({
    "source_type",
    "method",
    *_NATIVE_IMAGE_MANIFEST_UNIQUE_KEYS,
})
_ATTACHMENT_ROUTING_KEY_TOKENS = frozenset({
    "sourcetype",
    "method",
    "propertybinding",
    "bindingmethod",
    "imagemeta",
})
_ATTACHMENT_ROUTING_TOKEN_MAX_CHARS = 96
_NATIVE_IMAGE_GENERIC_NAME_TOKEN = "brokerpropertyimage"
_NATIVE_IMAGE_GENERIC_NAME_MAX_CHARS = 96
_NATIVE_IMAGE_GENERIC_SUFFIX_TOKENS = (
    "", "pdf", "png", "jpg", "jpeg", "webp", "gif",
)
_LEGACY_LINKED_ATTACHMENT_SOURCE_METHODS = {
    "google_drive_pdf": frozenset({
        "local_extraction",
        "local_extraction+images",
        "openai_upload",
        "openai_upload+images",
    }),
    "dropbox_pdf": frozenset({
        "local_extraction",
        "local_extraction+images",
        "openai_upload",
        "openai_upload+images",
    }),
    "public_pdf": frozenset({
        "local_extraction",
        "local_extraction+images",
        "openai_upload",
        "openai_upload+images",
    }),
    "direct_image": frozenset({"direct_image_link"}),
}
_LEGACY_ATTACHMENT_METHODS_WITHOUT_SOURCE_TYPE = frozenset({
    "local_extraction",
    "local_extraction+images",
    "openai_upload",
    "openai_upload+images",
    # Historical successful manifest shapes retained by existing workflows.
    "pdfplumber",
    "local",
    "text",
    "production-replay",
})
_LEGACY_DIRECT_IMAGE_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
)
_DIRECT_IMAGE_FALLBACK_NAME = "broker property image.png"
_LINKED_PDF_PRODUCER_KEYS = frozenset({
    "name", "filename", "text", "images", "method", "file_id", "id",
    "source_url", "source_type", "drive_link",
})
_LINKED_PDF_PREVIEW_KEYS = frozenset({
    "property_image_url", "property_image_source",
    "property_image_source_type", "property_image_meta",
})
_DIRECT_IMAGE_PRODUCER_KEYS = frozenset({
    "name", "text", "images", "method", "source_url", "source_type",
    "drive_link", "property_image_url", "property_image_source",
    "property_image_source_type", "property_image_meta",
})
_LINKED_PDF_PREVIEW_META_KEYS = frozenset({
    "pageNumber", "pageCount", "strategy", "selectionReason", "score",
    "signals", "contentType", "byteCount", "sha256", "driveLink",
})
_LINKED_PDF_PREVIEW_SIGNAL_KEYS = frozenset({
    "imageAreaRatio", "textChars", "positiveTerms", "negativeTerms",
})
_DIRECT_IMAGE_META_KEYS = frozenset({
    "strategy", "selectionReason", "contentType", "byteCount", "sha256",
    "driveLink",
})


def _bounded_attachment_routing_token(value: Any) -> Optional[str]:
    """Collapse bounded ASCII routing markers without invoking protocols."""
    if type(value) is not str:
        return None
    if len(value) > _ATTACHMENT_ROUTING_TOKEN_MAX_CHARS:
        return None
    if not str.isascii(value):
        return None
    folded = str.casefold(value)
    return "".join(
        character
        for character in folded
        if "a" <= character <= "z" or "0" <= character <= "9"
    )


def _is_reserved_native_image_generic_name(value: Any) -> bool:
    """Recognize bounded canonical and confusable forms of the native name."""
    if type(value) is not str:
        return False
    if len(value) > _NATIVE_IMAGE_GENERIC_NAME_MAX_CHARS:
        # The bounded recognizer cannot safely classify an over-limit marker;
        # route it through strict validation instead of trusting it as legacy.
        return True
    folded = unicodedata.normalize(
        "NFKD",
        str.casefold(unicodedata.normalize("NFKC", value)),
    )
    skeleton: List[Optional[str]] = []
    for character in folded:
        if str.isascii(character):
            if (
                "a" <= character <= "z"
                or "0" <= character <= "9"
            ):
                skeleton.append(character)
            continue
        category = unicodedata.category(character)
        if category[:1] in {"L", "N"}:
            # Do not maintain a partial homoglyph table.  A remaining Unicode
            # letter or number is an unknown positional lookalike.
            skeleton.append(None)
        elif category[:1] in {"C", "M", "P", "S", "Z"}:
            # Separators, marks, controls, punctuation, and symbols cannot
            # make a reserved generic marker into a trusted PDF filename.
            continue
        else:
            return False

    for suffix in _NATIVE_IMAGE_GENERIC_SUFFIX_TOKENS:
        expected = f"{_NATIVE_IMAGE_GENERIC_NAME_TOKEN}{suffix}"
        if len(skeleton) == len(expected) and all(
            actual is None or actual == expected_character
            for actual, expected_character in zip(skeleton, expected)
        ):
            return True
    return False


def _legacy_attachment_filename(manifest: dict) -> Optional[str]:
    """Return an exact producer filename without invoking caller protocols."""
    if dict.__contains__(manifest, "name"):
        filename = dict.get(manifest, "name")
    else:
        filename = dict.get(manifest, "filename")
    return filename if type(filename) is str else None


def _has_legacy_attachment_filename_shape(
    manifest: dict,
    *,
    direct_image: bool = False,
) -> bool:
    """Grant legacy routing only to the bounded real producer file shapes."""
    filename = _legacy_attachment_filename(manifest)
    if filename is None:
        return False
    folded = str.casefold(filename)
    if direct_image:
        return str.endswith(folded, _LEGACY_DIRECT_IMAGE_EXTENSIONS)
    return str.endswith(folded, ".pdf")


def _linked_source_url_identity(
    source_url: Any,
    *,
    empty_basename_fallback: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """Return the exact HTTPS host and producer-derived basename."""
    if type(source_url) is not str or not source_url:
        return None
    try:
        parsed = urlsplit(source_url)
        host = parsed.hostname
        if (
            str.casefold(parsed.scheme) != "https"
            or type(host) is not str
            or not host
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        basename = unquote(parsed.path).rsplit("/", 1)[-1].strip()
    except (TypeError, ValueError):
        return None
    if not basename:
        if type(empty_basename_fallback) is not str:
            return None
        basename = empty_basename_fallback
    return str.casefold(host), basename


def _has_exact_plain_dict_keys(
    value: Any,
    allowed_keys: frozenset,
    *,
    require_all: bool = True,
) -> bool:
    """Validate exact dict/string keys without invoking custom protocols."""
    if type(value) is not dict:
        return False
    keys = list(dict.keys(value))
    if any(type(key) is not str or key not in allowed_keys for key in keys):
        return False
    return not require_all or len(keys) == len(allowed_keys)


def _is_exact_positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _is_exact_finite_number(value: Any) -> bool:
    return type(value) is int or (
        type(value) is float and math.isfinite(value)
    )


def _is_exact_sha256(value: Any) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _has_linked_pdf_preview_signals_shape(signals: Any) -> bool:
    if not _has_exact_plain_dict_keys(
        signals,
        _LINKED_PDF_PREVIEW_SIGNAL_KEYS,
        require_all=False,
    ):
        return False
    for key in dict.keys(signals):
        value = dict.get(signals, key)
        if key == "imageAreaRatio":
            if not _is_exact_finite_number(value) or not 0 <= value <= 1:
                return False
        elif key == "textChars":
            if type(value) is not int or value < 0:
                return False
        elif (
            type(value) is not list
            or len(value) > 12
            or any(type(item) is not str for item in value)
        ):
            return False
    return True


def _has_linked_pdf_preview_meta_shape(meta: Any) -> bool:
    if not _has_exact_plain_dict_keys(meta, _LINKED_PDF_PREVIEW_META_KEYS):
        return False
    page_number = dict.get(meta, "pageNumber")
    page_count = dict.get(meta, "pageCount")
    strategy = dict.get(meta, "strategy")
    selection_reason = dict.get(meta, "selectionReason")
    score = dict.get(meta, "score")
    drive_link = dict.get(meta, "driveLink")
    return (
        _is_exact_positive_int(page_number)
        and _is_exact_positive_int(page_count)
        and page_number <= page_count
        and type(strategy) is str
        and strategy in {
            "property_preview_heuristic_v1",
            "first_page_preview_fallback",
        }
        and type(selection_reason) is str
        and bool(selection_reason)
        and selection_reason == str.strip(selection_reason)
        and _is_exact_finite_number(score)
        and _has_linked_pdf_preview_signals_shape(dict.get(meta, "signals"))
        and type(dict.get(meta, "contentType")) is str
        and dict.get(meta, "contentType") == "image/png"
        and _is_exact_positive_int(dict.get(meta, "byteCount"))
        and _is_exact_sha256(dict.get(meta, "sha256"))
        and type(drive_link) is str
        and _linked_source_url_identity(drive_link) is not None
    )


def _has_direct_image_meta_shape(meta: Any) -> bool:
    if not _has_exact_plain_dict_keys(meta, _DIRECT_IMAGE_META_KEYS):
        return False
    drive_link = dict.get(meta, "driveLink")
    return (
        type(dict.get(meta, "strategy")) is str
        and dict.get(meta, "strategy") == "direct_image_link_v1"
        and type(dict.get(meta, "selectionReason")) is str
        and dict.get(meta, "selectionReason")
        == "broker-provided public image link"
        and type(dict.get(meta, "contentType")) is str
        and dict.get(meta, "contentType") == "image/png"
        and _is_exact_positive_int(dict.get(meta, "byteCount"))
        and _is_exact_sha256(dict.get(meta, "sha256"))
        and type(drive_link) is str
        and _linked_source_url_identity(drive_link) is not None
    )


def _has_linked_pdf_producer_shape(
    manifest: dict,
    source_type: str,
) -> bool:
    """Validate the immutable fields emitted by linked-PDF production."""
    manifest_keys = frozenset(dict.keys(manifest))
    if manifest_keys not in {
        _LINKED_PDF_PRODUCER_KEYS,
        _LINKED_PDF_PRODUCER_KEYS | _LINKED_PDF_PREVIEW_KEYS,
    }:
        return False

    name = dict.get(manifest, "name")
    filename = dict.get(manifest, "filename")
    text = dict.get(manifest, "text")
    images = dict.get(manifest, "images")
    file_id = dict.get(manifest, "file_id")
    legacy_id = dict.get(manifest, "id")
    drive_link = dict.get(manifest, "drive_link")
    method = dict.get(manifest, "method")
    empty_basename_fallback = (
        "broker flyer.pdf"
        if source_type in {"google_drive_pdf", "dropbox_pdf"}
        else None
    )
    source_identity = _linked_source_url_identity(
        dict.get(manifest, "source_url"),
        empty_basename_fallback=empty_basename_fallback,
    )
    if (
        type(name) is not str
        or not name
        or name != str.strip(name)
        or type(filename) is not str
        or filename != name
        or type(text) is not str
        or type(images) is not list
        or type(file_id) not in (type(None), str)
        or type(legacy_id) not in (type(None), str)
        or legacy_id != file_id
        or type(drive_link) not in (type(None), str)
        or type(source_type) is not str
        or type(method) is not str
        or method not in _LEGACY_LINKED_ATTACHMENT_SOURCE_METHODS.get(
            source_type,
            (),
        )
        or source_identity is None
    ):
        return False
    has_file_fallback = method.startswith("openai_upload")
    has_preview_images = method.endswith("+images")
    if (
        has_file_fallback != bool(file_id)
        or has_preview_images != bool(images)
    ):
        return False

    if manifest_keys == (
        _LINKED_PDF_PRODUCER_KEYS | _LINKED_PDF_PREVIEW_KEYS
    ):
        property_image_url = dict.get(manifest, "property_image_url")
        property_image_source = dict.get(
            manifest,
            "property_image_source",
        )
        property_image_source_type = dict.get(
            manifest,
            "property_image_source_type",
        )
        property_image_meta = dict.get(manifest, "property_image_meta")
        if (
            type(property_image_url) is not str
            or not property_image_url
            or _linked_source_url_identity(property_image_url) is None
            or type(property_image_source) is not str
            or type(property_image_source_type) is not str
            or property_image_source_type != "broker_pdf_link_preview"
            or not _has_linked_pdf_preview_meta_shape(property_image_meta)
            or property_image_source
            != (
                f"Broker flyer link preview: {name}, page "
                f"{dict.get(property_image_meta, 'pageNumber')}"
            )
        ):
            return False

    host, source_name = source_identity
    if source_name != name:
        return False
    if source_type == "google_drive_pdf":
        return host == "drive.google.com" or host.endswith(
            ".drive.google.com"
        )
    if source_type == "dropbox_pdf":
        return host == "dropbox.com" or host.endswith(".dropbox.com")
    return source_type == "public_pdf"


def _has_direct_image_producer_shape(manifest: dict) -> bool:
    """Validate the non-transport manifest emitted for a linked image."""
    if frozenset(dict.keys(manifest)) != _DIRECT_IMAGE_PRODUCER_KEYS:
        return False

    name = dict.get(manifest, "name")
    images = dict.get(manifest, "images")
    method = dict.get(manifest, "method")
    source_type = dict.get(manifest, "source_type")
    raw_source_identity = _linked_source_url_identity(
        dict.get(manifest, "source_url")
    )
    source_identity = _linked_source_url_identity(
        dict.get(manifest, "source_url"),
        empty_basename_fallback=_DIRECT_IMAGE_FALLBACK_NAME,
    )
    source_host = source_identity[0] if source_identity is not None else None
    source_name = source_identity[1] if source_identity is not None else None
    raw_source_name = (
        raw_source_identity[1]
        if raw_source_identity is not None
        else None
    )
    source_has_image_extension = (
        type(raw_source_name) is str
        and str.endswith(
            str.casefold(raw_source_name),
            _LEGACY_DIRECT_IMAGE_EXTENSIONS,
        )
    )
    reserved_generic_name = _is_reserved_native_image_generic_name(name)
    property_image_url = dict.get(manifest, "property_image_url")
    property_image_source_type = dict.get(
        manifest,
        "property_image_source_type",
    )
    property_image_meta = dict.get(manifest, "property_image_meta")
    if (
        type(name) is not str
        or not name
        or name != str.strip(name)
        or len(name) > _NATIVE_IMAGE_GENERIC_NAME_MAX_CHARS
        or type(method) is not str
        or method != "direct_image_link"
        or type(source_type) is not str
        or source_type != "direct_image"
        or not str.endswith(
            str.casefold(name),
            _LEGACY_DIRECT_IMAGE_EXTENSIONS,
        )
        or type(dict.get(manifest, "text")) is not str
        or dict.get(manifest, "text") != ""
        or type(images) is not list
        or len(images) != 0
        or source_identity is None
        or (
            reserved_generic_name
            and not (
                name == _DIRECT_IMAGE_FALLBACK_NAME
                and (
                    source_host == "googleusercontent.com"
                    or source_host.endswith(".googleusercontent.com")
                )
                and not source_has_image_extension
            )
        )
        or (not reserved_generic_name and source_name != name)
        or dict.get(manifest, "drive_link") is not None
        or type(property_image_url) is not str
        or not property_image_url
        or _linked_source_url_identity(property_image_url) is None
        or type(dict.get(manifest, "property_image_source")) is not str
        or dict.get(manifest, "property_image_source")
        != f"Broker image link: {name}"
        or type(property_image_source_type) is not str
        or property_image_source_type != "broker_image_link"
        or not _has_direct_image_meta_shape(property_image_meta)
    ):
        return False
    return True


def _is_native_image_manifest_candidate(manifest: Any) -> bool:
    """Recognize canonical or malformed entries that claim the native channel."""
    if type(manifest) is not dict:
        return True
    for key in dict.keys(manifest):
        if type(key) is not str:
            return True
        key_token = _bounded_attachment_routing_token(key)
        if key_token is None:
            return True
        if (
            key_token in _ATTACHMENT_ROUTING_KEY_TOKENS
            and key not in _ATTACHMENT_ROUTING_KEYS
        ):
            return True
    if any(
        dict.__contains__(manifest, key)
        for key in _NATIVE_IMAGE_MANIFEST_UNIQUE_KEYS
    ):
        return True

    method = dict.get(manifest, "method")
    source_type = dict.get(manifest, "source_type")
    has_reserved_native_name = any(
        _is_reserved_native_image_generic_name(dict.get(manifest, key))
        for key in ("name", "filename")
        if dict.__contains__(manifest, key)
    )
    if has_reserved_native_name:
        if (
            type(source_type) is str
            and source_type == "direct_image"
            and type(method) is str
            and method == "direct_image_link"
            and _has_direct_image_producer_shape(manifest)
        ):
            return False
        return True

    if dict.__contains__(manifest, "source_type"):
        if (
            type(source_type) is str
            and type(method) is str
            and method in _LEGACY_LINKED_ATTACHMENT_SOURCE_METHODS.get(
                source_type,
                (),
            )
        ):
            if source_type == "direct_image":
                return not _has_direct_image_producer_shape(manifest)
            return not _has_linked_pdf_producer_shape(
                manifest,
                source_type,
            )
        return True

    if not dict.__contains__(manifest, "method"):
        return not _has_legacy_attachment_filename_shape(manifest)
    if type(method) is not str:
        return True
    return (
        method not in _LEGACY_ATTACHMENT_METHODS_WITHOUT_SOURCE_TYPE
        or not _has_legacy_attachment_filename_shape(manifest)
    )


_LEGACY_ATTACHMENT_SNAPSHOT_MAX_DEPTH = 16
_ATTACHMENT_SNAPSHOT_MAX_CONTAINER_ITEMS = 64
_ATTACHMENT_SNAPSHOT_MAX_NODES = 4096


class _UnsafeLegacyAttachmentSnapshot(ValueError):
    pass


@dataclass
class _AttachmentSnapshotBudget:
    remaining_nodes: int
    native_asset_slots: int = 0

    def reserve_nodes(self, count: int) -> None:
        if count < 0 or count > self.remaining_nodes:
            raise _UnsafeLegacyAttachmentSnapshot()
        self.remaining_nodes -= count

    def reserve_native_asset_slots(self, count: int) -> None:
        if (
            count < 0
            or self.native_asset_slots + count
            > _file_handling.NATIVE_IMAGE_MAX_COUNT
        ):
            raise _UnsafeLegacyAttachmentSnapshot()
        self.native_asset_slots += count


@dataclass(frozen=True)
class _FrozenLegacyList:
    items: Tuple[Any, ...]


@dataclass(frozen=True)
class _FrozenLegacyDict:
    items: Tuple[Tuple[str, Any], ...]


@dataclass(frozen=True)
class _FrozenUnsafeAttachmentLeaf:
    pass


_FROZEN_UNSAFE_ATTACHMENT_LEAF = _FrozenUnsafeAttachmentLeaf()


def _reserve_snapshot_native_asset_slots(
    items: Tuple[Tuple[str, Any], ...],
    snapshot_budget: _AttachmentSnapshotBudget,
) -> None:
    """Bound strong native envelopes before visiting their child values."""
    values = {key: item for key, item in items}
    exact_markers = (
        ("name", "Broker property image"),
        ("text", ""),
        ("method", "native_image_normalized"),
        ("source_type", "native_image"),
        ("property_binding", "target"),
        ("binding_method", "structured_filename_address"),
    )
    if any(
        type(values.get(key)) is not str
        or values.get(key) != expected
        for key, expected in exact_markers
    ):
        return

    images = values.get("images")
    image_meta = values.get("image_meta")
    if type(images) is not list or type(image_meta) is not list:
        return
    image_count = list.__len__(images)
    metadata_count = list.__len__(image_meta)
    if (
        image_count > _file_handling.NATIVE_IMAGE_MAX_COUNT
        or metadata_count > _file_handling.NATIVE_IMAGE_MAX_COUNT
    ):
        raise _UnsafeLegacyAttachmentSnapshot()
    snapshot_budget.reserve_native_asset_slots(
        max(image_count, metadata_count)
    )


def _freeze_legacy_json_value(
    value: Any,
    *,
    depth: int = 0,
    active_container_ids: Optional[set] = None,
    allow_unsafe_leaf: bool = False,
    snapshot_budget: Optional[_AttachmentSnapshotBudget] = None,
) -> Any:
    """Freeze exact JSON-safe types without invoking caller protocols."""
    if snapshot_budget is None:
        snapshot_budget = _AttachmentSnapshotBudget(
            remaining_nodes=_ATTACHMENT_SNAPSHOT_MAX_NODES,
        )
        snapshot_budget.reserve_nodes(1)
    if depth > _LEGACY_ATTACHMENT_SNAPSHOT_MAX_DEPTH:
        raise _UnsafeLegacyAttachmentSnapshot()
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            if allow_unsafe_leaf:
                return _FROZEN_UNSAFE_ATTACHMENT_LEAF
            raise _UnsafeLegacyAttachmentSnapshot()
        return value

    if active_container_ids is None:
        active_container_ids = set()
    container_id = id(value)
    if container_id in active_container_ids:
        raise _UnsafeLegacyAttachmentSnapshot()

    if type(value) is list:
        active_container_ids.add(container_id)
        try:
            item_count = list.__len__(value)
            if item_count > _ATTACHMENT_SNAPSHOT_MAX_CONTAINER_ITEMS:
                raise _UnsafeLegacyAttachmentSnapshot()
            snapshot_budget.reserve_nodes(item_count)
            items = tuple(list.__iter__(value))
            if len(items) != item_count:
                raise _UnsafeLegacyAttachmentSnapshot()
            return _FrozenLegacyList(tuple(
                _freeze_legacy_json_value(
                    item,
                    depth=depth + 1,
                    active_container_ids=active_container_ids,
                    allow_unsafe_leaf=allow_unsafe_leaf,
                    snapshot_budget=snapshot_budget,
                )
                for item in items
            ))
        finally:
            active_container_ids.remove(container_id)

    if type(value) is dict:
        active_container_ids.add(container_id)
        try:
            item_count = dict.__len__(value)
            if item_count > _ATTACHMENT_SNAPSHOT_MAX_CONTAINER_ITEMS:
                raise _UnsafeLegacyAttachmentSnapshot()
            snapshot_budget.reserve_nodes(item_count)
            items = tuple(dict.items(value))
            if len(items) != item_count:
                raise _UnsafeLegacyAttachmentSnapshot()
            if any(type(key) is not str for key, _ in items):
                raise _UnsafeLegacyAttachmentSnapshot()
            if depth == 1:
                _reserve_snapshot_native_asset_slots(
                    items,
                    snapshot_budget,
                )
            frozen_items = []
            for key, item in items:
                frozen_items.append((
                    key,
                    _freeze_legacy_json_value(
                        item,
                        depth=depth + 1,
                        active_container_ids=active_container_ids,
                        allow_unsafe_leaf=allow_unsafe_leaf,
                        snapshot_budget=snapshot_budget,
                    ),
                ))
            return _FrozenLegacyDict(tuple(frozen_items))
        finally:
            active_container_ids.remove(container_id)

    if allow_unsafe_leaf:
        return _FROZEN_UNSAFE_ATTACHMENT_LEAF
    raise _UnsafeLegacyAttachmentSnapshot()


def _frozen_attachment_contains_unsafe_leaf(value: Any) -> bool:
    """Inspect only internal frozen values; never invoke caller protocols."""
    if type(value) is _FrozenUnsafeAttachmentLeaf:
        return True
    if type(value) is _FrozenLegacyList:
        return any(
            _frozen_attachment_contains_unsafe_leaf(item)
            for item in value.items
        )
    if type(value) is _FrozenLegacyDict:
        return any(
            _frozen_attachment_contains_unsafe_leaf(item)
            for _, item in value.items
        )
    return False


def _thaw_legacy_json_value(value: Any) -> Any:
    """Materialize fresh plain containers from an internal frozen tree."""
    if type(value) is _FrozenLegacyList:
        return [
            _thaw_legacy_json_value(item)
            for item in value.items
        ]
    if type(value) is _FrozenLegacyDict:
        return {
            key: _thaw_legacy_json_value(item)
            for key, item in value.items
        }
    return value


@dataclass(frozen=True)
class _PreparedAIAttachment:
    legacy: Optional[_FrozenLegacyDict] = None
    native: Optional[Any] = None

    @property
    def is_native(self) -> bool:
        return self.native is not None

    def fresh_analysis_manifest(self) -> dict:
        if self.native is not None:
            return self.native.safe_projection()
        return self.fresh_legacy_manifest()

    def fresh_legacy_manifest(self) -> dict:
        if self.legacy is None:
            raise _UnsafeLegacyAttachmentSnapshot()
        return _thaw_legacy_json_value(self.legacy)

    def fresh_persisted_manifest(self) -> dict:
        if self.native is not None:
            return self.native.safe_projection()
        return {
            key: value
            for key, value in self.fresh_legacy_manifest().items()
            if key != "images"
        }

    def legacy_file_id(self) -> Any:
        if self.native is not None:
            return None
        return self.fresh_legacy_manifest().get("id")


def _prepare_ai_attachment_manifest(
    pdf_manifest: Optional[List[dict]],
) -> Optional[List[_PreparedAIAttachment]]:
    """Seal native and legacy entries without retaining caller aliases."""
    if pdf_manifest is None:
        raw_attachments = []
    elif type(pdf_manifest) is list:
        raw_attachments = pdf_manifest
    else:
        return None
    try:
        frozen_attachments = _freeze_legacy_json_value(
            raw_attachments,
            allow_unsafe_leaf=True,
        )
    except _UnsafeLegacyAttachmentSnapshot:
        return None
    if type(frozen_attachments) is not _FrozenLegacyList:
        return None

    native_positions = []
    native_manifests = []
    legacy_by_position = {}
    for position, frozen_attachment in enumerate(frozen_attachments.items):
        if type(frozen_attachment) is not _FrozenLegacyDict:
            return None
        sealed_attachment = _thaw_legacy_json_value(frozen_attachment)
        if _is_native_image_manifest_candidate(sealed_attachment):
            native_positions.append(position)
            native_manifests.append(sealed_attachment)
            continue
        if _frozen_attachment_contains_unsafe_leaf(frozen_attachment):
            return None
        legacy_by_position[position] = frozen_attachment

    prepared_natives = _file_handling._prepare_safe_native_image_manifests(
        native_manifests
    )
    if prepared_natives is None:
        return None
    prepared_by_position = dict(zip(
        native_positions,
        prepared_natives,
    ))
    return [
        (
            _PreparedAIAttachment(native=prepared_by_position[position])
            if position in prepared_by_position
            else _PreparedAIAttachment(legacy=legacy_by_position[position])
        )
        for position in range(len(frozen_attachments.items))
    ]


def _canonicalize_native_multi_property_attachment(proposal: dict) -> dict:
    """Fail a native-bearing ambiguous proposal closed for operator review."""
    contact_optout = next((
        event for event in (proposal.get("events") or [])
        if (event or {}).get("type") == "contact_optout"
    ), None)
    proposal["updates"] = []
    proposal["events"] = [contact_optout or {
        "type": "needs_user_input",
        "reason": "multi_property_attachment",
        "question": (
            "The broker offered multiple properties or suites in an attachment, "
            "but the details could not be bound safely to one row."
        ),
    }]
    proposal["response_email"] = None
    proposal["notes"] = ""
    return proposal


def _attachment_name_tokens(name: str) -> List[str]:
    normalized_name = _ATTACHMENT_COPY_SUFFIX_RE.sub("", name or "")
    return [
        token for token in re.findall(r"[a-z0-9]+", normalized_name.lower())
        if token not in _ATTACHMENT_NAME_NOISE
    ]


def _attachment_name_spans(name: str, text: str) -> List[tuple]:
    tokens = _attachment_name_tokens(name)
    if not tokens or (len(tokens) == 1 and len(tokens[0]) < 5):
        return []
    pattern = r"\b" + r"[^a-z0-9]+".join(map(re.escape, tokens)) + r"\b"
    return [
        (match.start(), match.end())
        for match in re.finditer(pattern, text or "", re.IGNORECASE)
    ]


def _attachment_name_matches_text(name: str, text: str) -> bool:
    return bool(_attachment_name_spans(name, text))


def _attachment_name_bound_to_target(
    name: str,
    text: str,
    target_anchor: str,
) -> bool:
    text = text or ""
    for clause_start, clause_end in _property_clause_spans(text):
        clause = text[clause_start:clause_end]
        if (
            _attachment_name_matches_text(name, clause)
            and _source_mentions_target_property(clause, target_anchor)
        ):
            return True
    return False


def _suppress_competing_attachment_updates(
    proposal: dict,
    conversation: List[dict],
    target_anchor: str,
    pdf_manifest: Optional[List[dict]],
    prepared_attachment_manifest: Optional[List[_PreparedAIAttachment]] = None,
) -> dict:
    """Keep mixed attachment evidence from leaking onto the current row.

    Each attachment is classified independently. When any source is competing
    or mixed, every proposed value must also occur in a target-bound source
    segment (or target-bound fresh message segment) before it can survive.
    """
    if prepared_attachment_manifest is not None:
        attachment_entries = [
            (
                None,
                prepared_attachment.native,
            )
            if prepared_attachment.is_native
            else (
                prepared_attachment.fresh_legacy_manifest(),
                None,
            )
            for prepared_attachment in prepared_attachment_manifest
        ]
    else:
        attachment_entries = [(pdf, None) for pdf in (pdf_manifest or [])]
    if not proposal or not attachment_entries:
        return proposal

    fresh_text = _fresh_inbound_text(conversation)
    multiple_attachments = len(attachment_entries) > 1
    classified_sources = []
    has_valid_native = False
    for pdf, prepared_native in attachment_entries:
        if prepared_native is not None:
            has_valid_native = True
            classified_sources.append(("", "target"))
            continue

        if _is_native_image_manifest_candidate(pdf):
            native_projection = project_safe_native_image_manifest(pdf)
            has_valid_native = has_valid_native or native_projection is not None
            classified_sources.append((
                "",
                "target" if native_projection is not None else "mixed",
            ))
            continue

        name = str((pdf or {}).get("name") or "")
        source = "\n".join((
            name,
            str((pdf or {}).get("text") or ""),
        ))
        verdict = _attachment_property_verdict(source, target_anchor)
        if verdict == "addressless":
            if _attachment_name_bound_to_target(
                name,
                fresh_text,
                target_anchor,
            ):
                verdict = "target"
            elif multiple_attachments:
                verdict = "competing"
        classified_sources.append((
            source,
            verdict,
        ))
    explicitly_other = any(
        verdict in {"competing", "mixed"}
        or (
            _ROUTE_ADDRESS_RE.search(source)
            and not _source_mentions_target_property(source, target_anchor)
        )
        for source, verdict in classified_sources
    )
    already_escalated = any(
        (event or {}).get("type") == "needs_user_input"
        and (event or {}).get("reason") == "multi_property_attachment"
        for event in proposal.get("events") or []
    )
    if has_valid_native and (explicitly_other or already_escalated):
        return _canonicalize_native_multi_property_attachment(proposal)

    if any(
        (event or {}).get("type") == "new_property"
        for event in proposal.get("events") or []
    ):
        return proposal
    if not explicitly_other and not already_escalated:
        return proposal

    target_evidence_sources = [
        segment
        for source, verdict in classified_sources
        for segment in _target_bound_source_segments(
            source,
            target_anchor,
            verdict,
        )
    ]
    target_evidence_sources.extend(
        _exact_target_clause_segments(fresh_text, target_anchor)
    )
    proposal["updates"] = [
        update for update in (proposal.get("updates") or [])
        if any(
            _update_supported_in_current_segment(update, source)
            for source in target_evidence_sources
        )
    ]
    # Mixed/competing attachment evidence makes every model event untrusted for
    # this row except an already-validated contact opt-out. Never-recontact wins;
    # otherwise emit one canonical operator handoff and no competing side effect.
    contact_optout = next((
        event for event in (proposal.get("events") or [])
        if (event or {}).get("type") == "contact_optout"
    ), None)
    proposal["events"] = [contact_optout or {
        "type": "needs_user_input",
        "reason": "multi_property_attachment",
        "question": (
            "The broker offered multiple properties or suites in an attachment, "
            "but the details could not be bound safely to one row."
        ),
    }]
    proposal["response_email"] = None
    return proposal


def _remove_proposal_update(proposal: dict, column_name: Optional[str]) -> None:
    if not column_name:
        return
    key = column_name.strip().lower()
    proposal["updates"] = [
        update for update in (proposal.get("updates") or [])
        if (update.get("column") or "").strip().lower() != key
    ]


def _rent_rejected_opex_values(
    text: str,
    extracted_rent: Optional[str],
) -> set[Decimal]:
    """Return annual rent and any explicitly monthly raw rate it came from."""
    annual_value = _normalized_numeric_value(extracted_rent)
    if annual_value is None:
        return set()

    values = {annual_value}
    monthly_raw_value = annual_value / Decimal("12")
    for match in _RENT_NUMERIC_VALUE_RE.finditer(text or ""):
        try:
            raw_value = Decimal(match.group(1).replace(",", ""))
        except InvalidOperation:
            continue
        if raw_value != monthly_raw_value:
            continue

        before = text[max(0, match.start() - 40):match.start()]
        prior_figure = list(_RENT_NUMERIC_VALUE_RE.finditer(before))
        if prior_figure:
            before = before[prior_figure[-1].end():]
        after = text[match.end():min(len(text), match.end() + 50)]
        next_figure = _RENT_NUMERIC_VALUE_RE.search(after)
        if next_figure:
            after = after[:next_figure.start()]
        if _is_monthly_context(before + match.group(0) + after):
            values.add(raw_value)
    return values


def _strip_unsupported_opex_update(
    proposal: dict,
    opex_col: Optional[str],
    text: str,
    extracted_rent: Optional[str] = None,
) -> None:
    update = _proposal_update_for_column(proposal, opex_col) if opex_col else None
    if update is None:
        return
    proposed = _normalized_numeric_value(update.get("value"))
    rejected_values = {
        value
        for _, _, raw_value, annualized_value in (
            _combined_total_opex_evidence(text)
            + _combined_base_rent_evidence(text)
            + _negated_rate_evidence(text)
            + _rejected_nnn_evidence(text)
        )
        for value in (raw_value, annualized_value)
    }
    rejected_values.update(
        _rent_rejected_opex_values(text, extracted_rent)
    )
    supported_values = {
        value
        for candidate in _ops_ex_candidates(text)
        for value in (candidate.raw_value, candidate.annualized_value)
    }
    if proposed in rejected_values and proposed not in supported_values:
        _remove_proposal_update(proposal, opex_col)


def _augment_proposal_with_deterministic_extractions(
    proposal: dict,
    rowvals: List[str],
    header: List[str],
    effective_config: dict,
    conversation: List[dict],
    pdf_manifest: List[dict] = None,
    extra_texts: Optional[List[str]] = None,
) -> dict:
    """Add high-confidence values from simple broker text patterns the LLM missed."""
    if not proposal:
        return proposal

    mappings = (effective_config or {}).get("mappings", {})
    # Only mine the broker's FRESH message; quoted history must not seed values.
    fresh_text = _fresh_inbound_text(conversation)
    target_anchor = get_row_anchor(rowvals, header)
    rent_col = mappings.get("rent_sf_yr") or _find_header_name(header, "Rent/SF /Yr")
    opex_col = mappings.get("ops_ex_sf") or _find_header_name(header, "Ops Ex /SF")
    total_sf_col = mappings.get("total_sf") or _find_header_name(header, "Total SF")
    event_types = {
        (event or {}).get("type") for event in (proposal.get("events") or [])
    }

    has_ambiguous_fact_cue = bool(_AMBIGUOUS_FACT_CUE_RE.search(fresh_text))
    fresh_rent = _extract_rent_sf_yr_from_text(fresh_text)
    trusted_pdf_rents = []
    competing_pdf_rents = set()
    for pdf in (pdf_manifest or []):
        pdf_source = "\n".join((
            (pdf or {}).get("name") or "",
            (pdf or {}).get("text") or "",
        ))
        pdf_rent = _extract_rent_sf_yr_from_text((pdf or {}).get("text") or "")
        if not pdf_rent:
            continue
        normalized_pdf_rent = _normalized_rent_value(pdf_rent)
        if not has_ambiguous_fact_cue and _attachment_can_supply_target_rent(
            pdf_source, target_anchor, fresh_text
        ):
            trusted_pdf_rents.append(pdf_rent)
        elif (
            _attachment_property_verdict(pdf_source, target_anchor) in {"competing", "mixed"}
            and normalized_pdf_rent is not None
        ):
            competing_pdf_rents.add(normalized_pdf_rent)

    trusted_pdf_total_sfs = []
    competing_pdf_total_sfs = set()
    for pdf in (pdf_manifest or []):
        pdf_source = "\n".join((
            (pdf or {}).get("name") or "",
            (pdf or {}).get("text") or "",
        ))
        pdf_total_sf = _extract_total_sf_from_text((pdf or {}).get("text") or "")
        normalized_pdf_total_sf = _normalized_numeric_value(pdf_total_sf)
        if not has_ambiguous_fact_cue and pdf_total_sf and _attachment_can_supply_target_facts(
            pdf_source, target_anchor, fresh_text
        ):
            trusted_pdf_total_sfs.append(pdf_total_sf)
        elif (
            _attachment_property_verdict(pdf_source, target_anchor) in {"competing", "mixed"}
            and normalized_pdf_total_sf is not None
        ):
            competing_pdf_total_sfs.add(normalized_pdf_total_sf)

    # Validate model-proposed facts before any event-specific early return. A
    # terminal/new-property proposal must not carry an unsafe current-row write.
    existing_rent = _proposal_update_for_column(proposal, rent_col) if rent_col else None
    if existing_rent:
        model_owned_rent = bool(str(existing_rent.get("value") or "").strip()) and not event_types & {"new_property", "property_unavailable"}
        proposed_rent = _normalized_rent_value(existing_rent.get("value"))
        trusted_rents = {
            normalized
            for normalized in (
                [_normalized_rent_value(fresh_rent)]
                + [_normalized_rent_value(value) for value in trusted_pdf_rents]
            )
            if normalized is not None
        }
        negated_rate_values = {
            value
            for _, _, raw_value, annualized_value in _negated_rate_evidence(
                fresh_text
            )
            for value in (raw_value, annualized_value)
        }
        if (
            (not model_owned_rent and trusted_rents and proposed_rent not in trusted_rents)
            or (
                proposed_rent in competing_pdf_rents
                and proposed_rent not in trusted_rents
            )
            or (
                proposed_rent
                in {
                    value
                    for candidate in _ops_ex_candidates(fresh_text)
                    for value in (
                        candidate.raw_value,
                        candidate.annualized_value,
                    )
                }
                and proposed_rent not in trusted_rents
            )
            or (
                proposed_rent in negated_rate_values
                and proposed_rent not in trusted_rents
            )
        ):
            _remove_proposal_update(proposal, rent_col)

    total_sf_value = _extract_total_sf_from_text(fresh_text)
    existing_total_sf = (
        _proposal_update_for_column(proposal, total_sf_col) if total_sf_col else None
    )
    if existing_total_sf:
        model_owned_total_sf = bool(str(existing_total_sf.get("value") or "").strip()) and not event_types & {"new_property", "property_unavailable"}
        proposed_total_sf = _normalized_numeric_value(existing_total_sf.get("value"))
        normalized_total_sf = _normalized_numeric_value(total_sf_value)
        trusted_total_sfs = {
            normalized
            for normalized in (
                [normalized_total_sf]
                + [
                    _normalized_numeric_value(value)
                    for value in trusted_pdf_total_sfs
                ]
            )
            if normalized is not None
        }
        if not model_owned_total_sf and trusted_total_sfs and proposed_total_sf not in trusted_total_sfs:
            _remove_proposal_update(proposal, total_sf_col)
        elif (
            proposed_total_sf in competing_pdf_total_sfs
            and proposed_total_sf not in trusted_total_sfs
        ):
            _remove_proposal_update(proposal, total_sf_col)
        elif (
            proposed_total_sf in _component_sf_values(fresh_text)
            and proposed_total_sf != normalized_total_sf
            and (not model_owned_total_sf or normalized_total_sf is None)
        ):
            _remove_proposal_update(proposal, total_sf_col)

    _strip_unsupported_opex_update(
        proposal,
        opex_col,
        fresh_text,
        extracted_rent=fresh_rent,
    )

    # LIVE break (900 Alt Suggest St): when the reply kills the current row or
    # pitches an alternate property, do not mine fallback specs into this row.
    if event_types & {"new_property", "property_unavailable"}:
        return proposal

    def _fill(col_name: Optional[str], value: Optional[str], reason: str) -> None:
        # Resolve to the canonical sheet header spelling (#15 wrote canonical names;
        # #19's mapping values may be lowercase, e.g. "total sf" vs header "Total SF").
        canonical = _find_header_name(header, col_name) if col_name else None
        if not value or not canonical:
            return
        col_name = canonical
        if (_row_value_for_column(rowvals, header, col_name) or "").strip():
            return
        update = {"column": col_name, "value": value, "confidence": 0.92, "reason": reason}
        existing = _proposal_update_for_column(proposal, col_name)
        if existing:
            rent_header = _find_header_name(header, rent_col)
            protected = col_name in {rent_header, _find_header_name(header, total_sf_col)}
            normalize = _normalized_rent_value if col_name == rent_header else _normalized_numeric_value
            if protected and normalize(existing.get("value")) != normalize(value) and not (
                col_name == rent_header
                and normalize(existing.get("value")) is not None
                and normalize(value) == normalize(existing.get("value")) * 12
            ):
                return
            if str(existing.get("value") or "").strip() != value:
                existing.clear()
                existing.update(update)
            return
        proposal.setdefault("updates", []).append(update)

    rent_value = None if has_ambiguous_fact_cue else fresh_rent
    if not rent_value and trusted_pdf_rents:
        # FIX-16 (M35, HEAD): the accept-new-property path passes rent only inside
        # the PDF manifest text (the inbound body is a synthetic stub), so scan the
        # manifest as a LAST resort when the fresh message carries no rent. This does
        # not prefer stale flyer pricing over an email rate — it only fills the gap.
        rent_value = trusted_pdf_rents[0]
    _fill(
        rent_col,
        rent_value,
        "Deterministic fallback parsed asking rent per SF per year from the latest broker message.",
    )
    _fill(
        opex_col,
        _extract_ops_ex_sf_from_text(fresh_text),
        "Deterministic fallback parsed operating expenses per SF per year from the latest broker message.",
    )
    _fill(
        total_sf_col,
        None if has_ambiguous_fact_cue else total_sf_value,
        "Deterministic fallback parsed total square footage from the latest broker message.",
    )
    _fill(
        mappings.get("drive_ins") or _find_header_name(header, "Drive Ins"),
        _extract_dimensioned_singular_drive_in_count(fresh_text),
        "Deterministic fallback parsed one explicitly singular drive-in door from the latest broker message.",
    )
    # Loading counts are semantic: entity binding, current-vs-hypothetical state,
    # dimensions, subtotals, and multi-property attachments cannot be resolved by
    # proximity regexes safely. The source-aware model owns these updates; the
    # deterministic fabricated-count guard below may reject unsupported values,
    # but no regex path may create, overwrite, or veto a loading-count update.
    return proposal


# Broker states there is NO separate opex figure (gross / all-in / no pass-through).
_NO_SEPARATE_OPEX_RE = re.compile(
    r"\bno\s+(?:separate\s+)?(?:opex|op\s*ex|operating\s+expenses?|cam)\b"
    r"|\bno\s+(?:separate\s+)?(?:opex\s+|cam\s+)?pass[\s-]?through\b"
    r"|\ball[\s-]?in\b[^.]{0,40}?\bno\s+separate\b",
    re.IGNORECASE,
)

# ---- Fabricated door-count guard --------------------------------------------
_DRIVE_IN_KW = r"(?:drive[-\s]?in|grade[-\s]?level|drive\s+in\s+door)"
_DOCK_KW = r"(?:dock|loading\s+dock)"


# Spelled-out counts (broker text often says "two docks", not "2 docks").
_WORD_NUMBER_RE = (
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety)"
)
# Digits OR spelled-out numbers; \b anchors so "twenty" matches but "twentyish" and
# substrings inside larger words never do.
_FEATURE_COUNT_RE = r"\b(?:\d{1,3}|" + _WORD_NUMBER_RE + r")\b"


def _has_explicit_feature_count(text: str, keyword_re: str) -> bool:
    """True only when a numeric count sits next to a loading-feature keyword.

    Counts may be digits ("2 docks") or spelled out ("two docks") — the broker
    uses both. Excludes electrical specs ("3-phase power", "three-phase power")
    so a qualitative phrase like "grade-level loading" never fabricates a count.
    """
    if not text:
        return False
    if (
        keyword_re == _DRIVE_IN_KW
        and _extract_dimensioned_singular_drive_in_count(text)
    ):
        return True
    for m in re.finditer(keyword_re, text, re.IGNORECASE):
        lo, hi = m.start() - 16, m.end() + 16
        for nm in re.finditer(_FEATURE_COUNT_RE, text, re.IGNORECASE):
            if nm.end() < lo or nm.start() > hi:
                continue
            after = text[nm.end(): nm.end() + 8].lower()
            if re.match(r"\s*-?\s*(?:phase|amp|volt|kv|v\b|a\b|ph\b|%|percent)", after):
                continue
            return True
    return False


_WORD_TO_NUMBER = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
# "<count> drive-in(s)/grade-level (ramp|door)s" — count immediately precedes the
# keyword so unrelated numbers ("suite 3, drive-in access") never bind.
_DRIVE_IN_COUNT_RE = re.compile(
    r"\b(\d{1,3}|" + _WORD_NUMBER_RE + r")\s*(?:x\s*)?"
    r"(?:drive[-\s]?ins?|grade[-\s]?level)\b"
    r"(?:\s*(?:ramps?|doors?))?",
    re.IGNORECASE,
)
# "<count> (dock-high|loading) dock(s)/door(s)" variants.
_DOCK_COUNT_RE = re.compile(
    r"\b(\d{1,3}|" + _WORD_NUMBER_RE + r")\s*(?:x\s*)?"
    r"(?:dock[-\s]?high\s+doors?|loading\s+docks?|docks?\b(?:\s*doors?)?|dock\s+doors?)",
    re.IGNORECASE,
)
_DIMENSIONED_SINGULAR_DRIVE_IN_RE = re.compile(
    r"\b(?:a|one)\s+(?:\d{1,2}\s*[x×]\s*\d{1,2}\s*)"
    r"(?:ft\.?\s*)?(?:drive[-\s]?in|grade[-\s]?level)\s+(?:door|ramp)\b",
    re.IGNORECASE,
)
_NONCURRENT_FEATURE_BEFORE_RE = re.compile(
    r"(?:does\s+not\s+have|doesn't\s+have|do\s+not\s+have|don't\s+have|"
    r"is\s+not(?:\s+equipped\s+with)?|without|needs?|would\s+need|could\s+(?:add|have)|"
    r"plans?\s+for|proposed)\s*$",
    re.IGNORECASE,
)
_NONCURRENT_FEATURE_AFTER_RE = re.compile(
    r"^\s*(?:(?:is|was|would\s+be)\s+)?"
    r"(?:proposed|planned|required|needed|possible|optional)\b",
    re.IGNORECASE,
)
_CURRENT_FEATURE_BEFORE_RE = re.compile(
    r"(?:\b(?:currently\s+)?(?:has|features|includes|offers)\b|"
    r"\bwe\s+have\b|\bthere\s+(?:is|are)\b|\bequipped\s+with\b)"
    r"[^.!?]{0,60}$",
    re.IGNORECASE,
)


def _augment_proposal_opex_basis(
    proposal: dict,
    rowvals: List[str],
    header: List[str],
    effective_config: dict,
    conversation: List[dict],
) -> dict:
    """Keep the Ops Ex update consistent with the rent basis and un-fabricated.

    Two deterministic guards on the model's Ops Ex proposal:
      * BASIS: when the broker states opex on a MONTHLY basis ('$0.21/SF/mo'),
        annualize the Ops Ex update (x12) so rent and opex never land on mixed
        bases (annual-rent + monthly-opex).
      * FABRICATION: when the broker states there is NO separate opex figure
        (gross / all-in / no pass-through), strip a fabricated zero/blank Ops Ex
        update the model invented. A real opex number is never touched.
    """
    if not proposal:
        return proposal

    updates = proposal.get("updates") or []
    if not updates:
        return proposal

    mappings = (effective_config or {}).get("mappings", {})
    opex_col = mappings.get("ops_ex_sf") or _find_header_name(header, "Ops Ex /SF")
    if not opex_col or not _find_header_name(header, opex_col):
        return proposal

    opex_update = _proposal_update_for_column(proposal, opex_col)
    if opex_update is None:
        return proposal

    text = _fresh_inbound_text(conversation) or ""
    current = str(opex_update.get("value") or "").strip()

    # FABRICATION guard — drop a fabricated zero/blank opex on a gross/all-in quote.
    if _NO_SEPARATE_OPEX_RE.search(text):
        try:
            is_zeroish = current == "" or float(current) == 0.0
        except ValueError:
            is_zeroish = False
        if is_zeroish:
            proposal["updates"] = [u for u in updates if u is not opex_update]
            return proposal

    # BASIS guard — use the same accepted evidence winner as extraction. Rewrite
    # only the raw monthly value; an annualized, unrelated, or unsupported model
    # value is already idempotent and remains untouched.
    winner = _ops_ex_winner(text)
    current_value = _normalized_numeric_value(current)
    if (
        winner is not None
        and winner.basis == "monthly"
        and winner.annualized_value != winner.raw_value
        and current_value == winner.raw_value
    ):
        opex_update["value"] = f"{winner.annualized_value:.2f}"
        opex_update["reason"] = (
            "Deterministic basis normalization: opex stated monthly, "
            "annualized to match the rent basis."
        )

    return proposal


def _parse_feature_count(raw: str) -> Optional[str]:
    raw = (raw or "").strip().lower()
    if raw.isdigit():
        value = int(raw)
    else:
        value = _WORD_TO_NUMBER.get(raw, 0)
    return str(value) if 1 <= value <= 200 else None


def _extract_drive_in_count_from_text(text: str) -> Optional[str]:
    """Explicit drive-in / grade-level door count, or None (never guesses)."""
    if not text:
        return None
    m = _DRIVE_IN_COUNT_RE.search(text)
    return _parse_feature_count(m.group(1)) if m else None


def _extract_dimensioned_singular_drive_in_count(text: str) -> Optional[str]:
    """Return one only for an explicit singular dimensioned drive-in door."""
    matches = list(_DIMENSIONED_SINGULAR_DRIVE_IN_RE.finditer(text or ""))
    if len(matches) != 1:
        return None
    match = matches[0]
    before = (text or "")[max(0, match.start() - 45):match.start()]
    after = (text or "")[match.end():match.end() + 35]
    if _NONCURRENT_FEATURE_BEFORE_RE.search(before):
        return None
    if _NONCURRENT_FEATURE_AFTER_RE.search(after):
        return None
    sentence_before = re.split(r"[.!?\n]", before)[-1]
    is_bare_fact_line = bool(re.fullmatch(r"\s*(?:[-*]\s*)?", sentence_before))
    if not (_CURRENT_FEATURE_BEFORE_RE.search(before) or is_bare_fact_line):
        return None
    remaining = (text or "")[:match.start()] + " " + (text or "")[match.end():]
    if re.search(_DRIVE_IN_KW, remaining, re.IGNORECASE):
        return None
    return "1"


def _extract_dock_count_from_text(text: str) -> Optional[str]:
    """Explicit loading-dock / dock-high door count, or None (never guesses)."""
    if not text:
        return None
    m = _DOCK_COUNT_RE.search(text)
    return _parse_feature_count(m.group(1)) if m else None


def _suppress_updates_on_contact_optout(proposal: dict) -> dict:
    """A genuine contact opt-out is a PURE escalation — never touch the row.

    LIVE break adv_optout_with_specs: a broker replies "Not interested, remove me.
    FYI it was going for $18/SF NNN, 12,000 SF." The classifier correctly fires
    contact_optout and nulls response_email (escalate to the operator), but the
    rent / OpEx / SF specs mentioned in the same breath were still proposed as
    sheet writes — silently editing a row the contact just asked us to stop
    touching. When a contact_optout survives to this point (the engaged-alternative
    guard has already stripped scoped "show me something else" over-fires upstream,
    so any remaining opt-out is genuine), drop every proposed update and null any
    drafted auto-reply so the opt-out escalates cleanly, model-independently.
    """
    if not proposal:
        return proposal
    events = proposal.get("events") or []
    if any((e or {}).get("type") == "contact_optout" for e in events):
        proposal["updates"] = []
        proposal["response_email"] = None
    return proposal


def _suppress_fabricated_door_counts(
    proposal: dict,
    conversation: List[dict],
    header: List[str],
    effective_config: dict,
    extra_texts: Optional[List[str]] = None,
) -> dict:
    """Drop invented Drive Ins / Docks counts when the broker stated no number.

    Evidence includes flyer/linked-PDF text (extra_texts), not just the message
    body — LIVE break 600 Flyer Facts Blvd: "1 drive-in ramp" lived only in the
    flyer PDF, and validating against the email text alone stripped a REAL
    count as fabricated.
    """
    if not proposal:
        return proposal
    updates = proposal.get("updates") or []
    if not updates:
        return proposal
    mappings = (effective_config or {}).get("mappings", {})
    fresh = "\n".join(
        [_fresh_inbound_text(conversation)] + [t for t in (extra_texts or []) if t]
    )
    checks = [
        (
            mappings.get("drive_ins")
            or _find_header_name(header, "Drive Ins")
            or _find_header_name(header, "Drive-Ins"),
            _DRIVE_IN_KW,
        ),
        (
            mappings.get("docks")
            or _find_header_name(header, "Docks")
            or _find_header_name(header, "Loading Docks"),
            _DOCK_KW,
        ),
    ]
    drop_cols = set()
    for col, kw in checks:
        if not col:
            continue
        upd = _proposal_update_for_column(proposal, col)
        if not upd:
            continue
        val = str(upd.get("value") or "").strip()
        if not re.fullmatch(r"\d{1,3}", val):
            continue  # only guard bare numeric counts
        if not _has_explicit_feature_count(fresh, kw):
            drop_cols.add((col or "").strip().lower())
    if drop_cols:
        proposal["updates"] = [
            u for u in updates
            if (u.get("column") or "").strip().lower() not in drop_cols
        ]
    return proposal


# ---- Broken/expired flyer-link surfacing ------------------------------------
_BROKEN_LINK_RE = re.compile(
    r"\b(?:expired|no\s+longer\s+available|not\s+found|404|has\s+been\s+deleted"
    r"|link\s+(?:is\s+)?broken|transfer\s+has\s+expired|page\s+not\s+found|access\s+denied)\b",
    re.IGNORECASE,
)


def _looks_like_broken_link_text(text: str) -> bool:
    return bool(text) and bool(_BROKEN_LINK_RE.search(text))


def _find_flyer_column(header: List[str], mappings: dict) -> Optional[str]:
    col = (mappings or {}).get("flyer_link")
    if col and _find_header_name(header, col):
        return _find_header_name(header, col)
    for name in ("Flyer / Link", "Flyer/Link", "Flyer", "Link"):
        found = _find_header_name(header, name)
        if found:
            return found
    return None


def _augment_proposal_with_flyer_link(
    proposal: dict,
    url_texts: List[dict],
    rowvals: List[str],
    header: List[str],
    effective_config: dict,
) -> dict:
    """Surface a broker flyer/transfer link whose fetched content is broken/expired.

    A dead we.tl / WeTransfer / drive link would otherwise vanish silently — the
    user never learns a flyer was sent. Prefer a Flyer/Link column; else note it.
    """
    if not proposal:
        return proposal
    broken_urls = []
    for u in (url_texts or []):
        url = (u or {}).get("url")
        if url and _looks_like_broken_link_text((u or {}).get("text") or ""):
            broken_urls.append(url)
    if not broken_urls:
        return proposal

    existing_blob = json.dumps(proposal.get("updates", []) or []) + " " + str(proposal.get("notes") or "")
    new_urls = [u for u in dict.fromkeys(broken_urls) if u not in existing_blob]
    if not new_urls:
        return proposal

    mappings = (effective_config or {}).get("mappings", {})
    flyer_col = _find_flyer_column(header, mappings)
    for url in new_urls:
        if (flyer_col
                and not (_row_value_for_column(rowvals, header, flyer_col) or "").strip()
                and not _proposal_update_for_column(proposal, flyer_col)):
            proposal.setdefault("updates", []).append({
                "column": flyer_col,
                "value": url,
                "confidence": 0.9,
                "reason": "Deterministic fallback surfaced broker flyer/transfer link (fetched content indicates it may be expired).",
            })
        else:
            frag = f"flyer link (may be expired): {url}"
            existing_notes = str(proposal.get("notes") or "").strip()
            proposal["notes"] = f"{existing_notes} • {frag}".strip(" •") if existing_notes else frag
    return proposal


# ---- Prompt content clipping (retain deep field data) -----------------------
_URL_TEXT_CHAR_LIMIT = 8000
_PDF_TEXT_CHAR_LIMIT = 16000
_FIELD_HINT_RE = re.compile(
    r"(?:\$|\bsf\b|square\s*f|\bdock|drive[-\s]?in|clear|ceiling|amp|volt|nnn|opex|"
    r"total\s+sf|\bpsf\b|\b\d{3,}\b)",
    re.IGNORECASE,
)


def _clip_for_prompt(text: str, limit: int) -> str:
    """Truncate long fetched content but always retain field-bearing lines from
    beyond the cutoff so a number (Total SF, rent, docks…) is never silently lost.
    """
    if not text:
        return text or ""
    if len(text) <= limit:
        return text
    head = text[:limit]
    tail = text[limit:]
    kept = [ln for ln in tail.splitlines() if _FIELD_HINT_RE.search(ln)]
    result = head + "\n... [text truncated] ..."
    extra = "\n".join(kept)[:4000]
    if extra:
        result += "\n[additional detail lines retained beyond truncation]\n" + extra
    return result


def _filter_config_by_extraction_fields(column_config: dict, extraction_fields: List[str]) -> dict:
    """
    Filter column_config to only include fields specified in extraction_fields.

    This allows users to toggle which fields the AI should extract (e.g., if they don't
    care about Power or Docks, they can disable those fields in their client settings).

    Args:
        column_config: Full column configuration dict with mappings, requiredFields, etc.
        extraction_fields: List of canonical field keys to include (e.g., ["total_sf", "ops_ex_sf"])

    Returns:
        Filtered column_config with only the specified extractable fields in mappings.
    """
    if extraction_fields is None:
        return column_config

    # Create a copy to avoid mutating the original
    filtered = {
        "mappings": {},
        "requiredFields": column_config.get("requiredFields", []),
        "formulaFields": column_config.get("formulaFields", []),
        "neverRequest": column_config.get("neverRequest", []),
        "customFields": column_config.get("customFields", {}),  # Include custom fields
    }

    extraction_set = set(extraction_fields)
    original_mappings = column_config.get("mappings", {})

    # Always include non-extractable fields (matching fields like address, city, email)
    # Only filter extractable fields based on user preference
    for canonical_key, actual_column in original_mappings.items():
        field_def = CANONICAL_FIELDS.get(canonical_key, {})
        is_extractable = field_def.get("extractable", False)

        # Include if: not extractable (always needed for matching), or in extraction_fields list
        if not is_extractable or canonical_key in extraction_set:
            filtered["mappings"][canonical_key] = actual_column

    # Also filter requiredFields to only include fields that are still in mappings
    filtered["requiredFields"] = [
        f for f in filtered["requiredFields"]
        if f in filtered["mappings"]
    ]

    return filtered


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


def _build_row_snapshot(header: List[str], rowvals: List[str]) -> dict:
    """Return a header-keyed row snapshot for report and audit readback."""
    snapshot = {}
    for idx, column_name in enumerate(header or []):
        column = (column_name or "").strip()
        if not column:
            continue
        snapshot[column] = rowvals[idx] if idx < len(rowvals or []) else ""
    return snapshot


# Free-text placeholder cell values that a broker (or the model) may drop into a
# required column while the real number is still outstanding. These are NOT data —
# they must not satisfy the completion guard, or a row closes on non-answers.
_PLACEHOLDER_CELL_VALUES = {
    "tbd", "tbc", "pending", "n/a", "na", "?", "to follow", "ask landlord",
}


def _normalize_required_col_key(name: str) -> str:
    """Collapse a column/field name to alnum-only lowercase so spacing/punctuation
    differences don't matter ('Ops Ex / SF' == 'Ops Ex /SF' == 'opsexsf')."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


# LIVE break (golden campaign): the default REQUIRED_FIELDS_FOR_CLOSE names
# ('Ops Ex /SF', 'Docks') never matched Jill's real sheet headers
# ('Ops Ex / SF', 'Loading Docks'), so check_missing_required_fields reported
# ALREADY-FILLED columns as missing — close_conversation was ignored forever and
# the row could never reach 'completed' (it looped re-requesting filled fields).
# Map each required field to the set of header spellings that satisfy it.
_REQUIRED_FIELD_HEADER_ALIASES = {
    "docks": {"docks", "loadingdocks", "dockdoors", "dockhighdoors", "loadingdockdoors"},
    "driveins": {"driveins", "driveindoors", "gradelevel", "gradeleveldoors", "driveindoors"},
    "opsexsf": {"opsexsf", "opexsf", "opex", "opsex", "nnnsf", "camsf"},
    "flyerlink": {"flyerlink", "flyer", "link", "flyerbrochure", "brochurelink"},
    "ceilinght": {"ceilinght", "ceilingheight", "clearheight", "clearht"},
}


def check_missing_required_fields(rowvals: List[str], header: List[str], column_config: dict = None) -> List[str]:
    """
    Check which required fields are missing from the row.
    Uses dynamic column config if provided, otherwise falls back to defaults.

    Header matching is whitespace/punctuation-insensitive and alias-aware so a
    required field named 'Docks' is satisfied by a 'Loading Docks' column and
    'Ops Ex /SF' by 'Ops Ex / SF' (see _REQUIRED_FIELD_HEADER_ALIASES).
    """
    try:
        # normalized header key -> (0-based index, raw value getter)
        norm_headers = {}
        for i, h in enumerate(header):
            norm_headers.setdefault(_normalize_required_col_key(h), i)

        missing = []

        # Get required fields from config or use defaults
        if column_config:
            required_fields = get_required_fields_for_close(column_config)
        else:
            required_fields = REQUIRED_FIELDS_FOR_CLOSE

        for field in required_fields:
            fkey = _normalize_required_col_key(field)
            candidate_keys = {fkey} | _REQUIRED_FIELD_HEADER_ALIASES.get(fkey, set())
            matched_idx = next((norm_headers[k] for k in candidate_keys if k in norm_headers), None)
            if matched_idx is None:
                missing.append(field)  # No column on the sheet satisfies this field
                continue
            cell = (rowvals[matched_idx] or "").strip() if matched_idx < len(rowvals) else ""
            # A placeholder ('TBD', 'pending', '?', 'ask landlord', ...) is not a real
            # spec value — treat it as missing so the row cannot close on it (HEAD).
            if matched_idx >= len(rowvals) or not cell or cell.lower() in _PLACEHOLDER_CELL_VALUES:
                missing.append(field)

        return missing
    except Exception as e:
        print(f"❌ Failed to check missing fields: {e}")
        return REQUIRED_FIELDS_FOR_CLOSE  # Assume all missing on error

def _ensure_ai_meta_tab(sheets, spreadsheet_id: str) -> None:
    """Ensure AI_META tab exists with proper headers."""
    try:
        meta = _execute_with_retry(
            sheets.spreadsheets().get(spreadsheetId=spreadsheet_id),
            "ensure_ai_meta_get"
        )
        ai_meta_sheet = next(
            (
                sheet.get("properties", {})
                for sheet in meta["sheets"]
                if sheet.get("properties", {}).get("title") == "AI_META"
            ),
            None,
        )

        if ai_meta_sheet:
            if not ai_meta_sheet.get("hidden") and ai_meta_sheet.get("sheetId") is not None:
                request = {
                    "requests": [{
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": ai_meta_sheet["sheetId"],
                                "hidden": True,
                            },
                            "fields": "hidden",
                        }
                    }]
                }
                _execute_with_retry(
                    sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=request),
                    "ensure_ai_meta_hide_existing"
                )
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
        _execute_with_retry(
            sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=request),
            "ensure_ai_meta_create"
        )

        # Add headers
        headers = ["rowNumber", "columnName", "last_ai_value", "last_ai_write_iso", "human_override", "rowAnchor"]
        _execute_with_retry(
            sheets.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range="AI_META!A1:F1",
                valueInputOption="RAW",
                body={"values": [headers]}
            ),
            "ensure_ai_meta_headers"
        )

        print("📋 Created 'AI_META' tab")

    except Exception as e:
        print(f"⚠️ Could not create AI_META tab: {e}")

def _normalize_ai_meta_anchor(anchor: str) -> str:
    return " ".join((anchor or "").strip().lower().replace(",", " ").split())


def _load_ai_meta_rows(sheets, spreadsheet_id: str) -> List[List[Any]]:
    resp = _execute_with_retry(
        sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="AI_META!A:F",
        ),
        "read_ai_meta",
    )
    return resp.get("values", [])


def _find_ai_meta_row(
    rows: List[List[Any]],
    rownum: int,
    column: str,
    row_anchor: str = None,
) -> Optional[Dict]:
    if len(rows) <= 1:
        return None

    for row in reversed(rows[1:]):
        if len(row) < 2 or str(row[0]) != str(rownum) or row[1].lower() != column.lower():
            continue
        stored_anchor = row[5] if len(row) > 5 else ""
        if stored_anchor and row_anchor:
            if _normalize_ai_meta_anchor(stored_anchor) != _normalize_ai_meta_anchor(row_anchor):
                print(
                    f"⚠️ Ignoring AI_META row {rownum}/{column}: "
                    f"anchor changed from '{stored_anchor}' to '{row_anchor}'"
                )
                continue
        elif row_anchor and not stored_anchor:
            print(
                f"⚠️ Ignoring AI_META row {rownum}/{column}: "
                f"missing row anchor for current row '{row_anchor}'"
            )
            continue
        return {
            "rowNumber": row[0],
            "columnName": row[1],
            "last_ai_value": row[2] if len(row) > 2 else None,
            "last_ai_write_iso": row[3] if len(row) > 3 else None,
            "human_override": row[4] if len(row) > 4 else False,
            "rowAnchor": stored_anchor,
        }
    return None


def _read_ai_meta_row(
    sheets,
    spreadsheet_id: str,
    rownum: int,
    column: str,
    row_anchor: str = None,
) -> Optional[Dict]:
    """Read AI_META record for specific row/column."""
    try:
        _ensure_ai_meta_tab(sheets, spreadsheet_id)
        return _find_ai_meta_row(
            _load_ai_meta_rows(sheets, spreadsheet_id),
            rownum,
            column,
            row_anchor=row_anchor,
        )
    except Exception as e:
        print(f"⚠️ Failed to read AI_META for row {rownum}, column {column}: {e}")
        return None

def _append_ai_meta(
    sheets,
    spreadsheet_id: str,
    rownum: int,
    column: str,
    value: str,
    override: bool = False,
    row_anchor: str = None,
    ensure_tab: bool = True,
):
    """Append new AI_META record."""
    try:
        if ensure_tab:
            _ensure_ai_meta_tab(sheets, spreadsheet_id)

        now_iso = datetime.now(timezone.utc).isoformat()

        row_data = [rownum, column, value, now_iso, override, row_anchor or ""]
        logger.debug(
            "sheet.ai_meta_append",
            extra={
                "spreadsheet_id": spreadsheet_id,
                "rownum": rownum,
                "column": column,
                "value": value,
                "override": override,
                "row_anchor": row_anchor,
                "timestamp": now_iso,
            },
        )

        _execute_with_retry(
            sheets.spreadsheets().values().append(
                spreadsheetId=spreadsheet_id,
                range="AI_META!A:F",
                valueInputOption="RAW",
                body={"values": [row_data]}
            ),
            "append_ai_meta"
        )

    except Exception as e:
        print(f"⚠️ Failed to append AI_META record: {e}")
        raise


def _ai_meta_confirms_value(
    rows: List[List[Any]],
    rownum: int,
    column: str,
    value: str,
    row_anchor: str,
) -> bool:
    meta = _find_ai_meta_row(
        rows,
        rownum,
        column,
        row_anchor=row_anchor,
    )
    return bool(
        meta
        and sheet_values_equal_for_column(
            column,
            meta.get("last_ai_value"),
            value,
        )
    )

def _normalize_comment_bullet(bullet: str) -> str:
    """Normalize a bullet fact for dedup comparison: lowercase, collapse
    whitespace, drop a trailing stray CR and surrounding punctuation. Two
    bullets that normalize equal are treated as the same fact."""
    b = (bullet or "").replace("\r", " ").strip().strip(".;,").lower()
    return re.sub(r"\s+", " ", b)


def _merge_comment_bullets(existing_comments: str, notes: str) -> str:
    """Append `notes` to `existing_comments` as ' • '-joined bullets WITHOUT
    re-adding a fact that is already present. Preserves the original order and
    the first-seen surface form of each bullet; timestamped/dated append lines
    (e.g. "[06/09/2026] Property marked unavailable ...") are always kept since
    they are event-specific, not repeatable spec facts.

    Fixes the real MOHR sheet defect where every reply re-appended the same
    facts, producing "NNN • ... • NNN • ... • NNN" and
    "100% HVAC • available now • available now" noise; Jill's ideal cell is a
    clean, de-duplicated fact list.
    """
    existing_comments = (existing_comments or "").strip()
    notes = (notes or "").strip()
    if not existing_comments:
        return notes
    if not notes:
        return existing_comments

    def _is_dated(bullet: str) -> bool:
        return bool(re.match(r"\s*\[[0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4}\]", bullet or ""))

    seen = set()
    ordered = []
    for chunk in (existing_comments, notes):
        for raw in chunk.split(" • "):
            raw = raw.strip()
            if not raw:
                continue
            key = _normalize_comment_bullet(raw)
            if _is_dated(raw):
                ordered.append(raw)  # event lines always kept
                continue
            if key in seen:
                continue
            seen.add(key)
            ordered.append(raw)
    return " • ".join(ordered)


def _append_notes_to_comments(sheets, spreadsheet_id: str, tab_title: str, header: List[str], rownum: int, notes: str):
    """
    Append notes to the comments field.
    Prefers listing-broker comments if available, otherwise uses user/client notes.
    Appends to existing comments with a separator.
    """
    try:
        comments_col_idx = find_notes_comment_column_index(header)

        if comments_col_idx is None:
            print(f"⚠️ Could not find comments column to append notes")
            return

        comments_col_name = header[comments_col_idx - 1]
        
        # Get existing comments
        col_letter = _col_letter(comments_col_idx)
        existing_resp = _execute_with_retry(
            sheets.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=f"{tab_title}!{col_letter}{rownum}"
            ),
            "append_notes_get_existing"
        )

        existing_comments = ""
        if existing_resp.get("values") and len(existing_resp["values"]) > 0:
            existing_comments = (existing_resp["values"][0][0] or "").strip()

        # Combine existing and new notes, de-duplicating bullet facts so the
        # cell doesn't accumulate "NNN • ... • NNN • ... • NNN" or
        # "100% HVAC • available now • available now" on every reply/update.
        combined = _merge_comment_bullets(existing_comments, notes)

        # Update the comments cell
        _execute_with_retry(
            sheets.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{tab_title}!{col_letter}{rownum}",
                valueInputOption="RAW",
                body={"values": [[combined]]}
            ),
            "append_notes_update"
        )

        print(f"📝 Appended notes to {comments_col_name} column: {notes[:100]}...")
        
    except Exception as e:
        print(f"⚠️ Failed to append notes to comments: {e}")

# Formula columns are computed on the sheet (e.g. Gross Rent = (Rent/SF + Ops Ex) * SF / 12)
# and must NEVER be overwritten by an AI proposal — a raw value clobbers the live formula
# cell. The LLM is told this in the prompt, but LIVE testing showed it still proposes
# {column:'Gross Rent', value:'32.00'} occasionally, so this is a deterministic code guard,
# not a prompt hope. Aliases come from the canonical field registry so the guard stays in
# sync with column_config; "monthly gross rent" is the header variant the formula builder
# (sheet_operations._build_gross_rent_formula_for_row) also matches.
_FORMULA_COLUMN_ALIASES = frozenset(
    alias.strip().lower()
    for field in CANONICAL_FIELDS.values()
    if field.get("is_formula")
    for alias in ([field.get("label")] + list(field.get("default_aliases") or []))
    if alias and alias.strip()
) | {"monthly gross rent"}


def _is_formula_column(col_name: str) -> bool:
    """True if col_name is a formula column that apply must never write (clobbers the cell)."""
    return (col_name or "").strip().lower() in _FORMULA_COLUMN_ALIASES


def _normalize_safe_broker_flyer_url(value: Any) -> Optional[str]:
    """Return a canonical public HTTP(S) URL, or ``None`` when unsafe."""
    text = str(value or "").strip()
    if (
        not text
        or "\\" in text
        or any(
            char.isspace() or unicodedata.category(char).startswith("C")
            for char in text
        )
        or re.search(r"%(?![0-9A-Fa-f]{2})", text)
    ):
        return None

    try:
        parsed = urlsplit(text)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").strip().lower()
        port = parsed.port
    except (TypeError, ValueError):
        return None

    host = host[:-1] if host.endswith(".") else host
    if (
        scheme not in {"http", "https"}
        or not host
        or "%" in host
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
    ):
        return None

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None

    if address is not None:
        if not address.is_global or address.is_multicast:
            return None
        ascii_host = address.compressed
    else:
        # Reject legacy numeric IPv4 spellings (127.1, 2130706433, 0x7f000001)
        # that URL consumers may resolve to a local address.
        try:
            socket.inet_aton(host)
        except OSError:
            pass
        else:
            return None

        try:
            ascii_host = host.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            return None
        local_suffixes = ("localhost", "local", "internal", "lan", "home", "home.arpa")
        if any(
            ascii_host == suffix or ascii_host.endswith(f".{suffix}")
            for suffix in local_suffixes
        ):
            return None
        if "." not in ascii_host or len(ascii_host) > 253:
            return None
        labels = ascii_host.split(".")
        if any(
            not label
            or len(label) > 63
            or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is None
            for label in labels
        ):
            return None
        if labels[-1].isdigit():
            return None

    normalized_host = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    if port is not None and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        normalized_host = f"{normalized_host}:{port}"
    return urlunsplit((
        scheme,
        normalized_host,
        parsed.path or "/",
        parsed.query,
        parsed.fragment,
    ))


def apply_proposal_to_sheet(
    uid: str,
    client_id: str,
    sheet_id: str,
    header: List[str],
    rownum: int,
    current_rowvals: List[str],
    proposal: dict,
    column_config: Optional[dict] = None,
    broker_flyer_url_evidence: Optional[List[str]] = None,
) -> dict:
    """
    Applies proposal['updates'] to the sheet row with AI write guards.

    ``broker_flyer_url_evidence`` is a trusted caller-supplied allowlist from
    the fresh inbound message. ``None`` leaves every asset column under the
    existing Drive/attachment pipeline; a list opens only the primary
    Flyer/Link fallback and still requires an exact normalized URL match.
    Returns {"applied":[...], "skipped":[...]} items with old/new values.
    """
    if not proposal or not isinstance(proposal.get("updates"), list) or not proposal["updates"]:
        row_anchor = get_row_anchor(current_rowvals, header)
        return {
            "applied": [],
            "skipped": [{"reason": "no-updates"}],
            "rowNumber": rownum,
            "targetAnchor": row_anchor,
            "rowSnapshotBefore": _build_row_snapshot(header, current_rowvals),
            "rowSnapshotAfter": _build_row_snapshot(header, current_rowvals),
        }

    try:
        sheets = _sheets_client()
        tab_title = _get_first_tab_title(sheets, sheet_id)
        
        _ensure_ai_meta_tab(sheets, sheet_id)
        ai_meta_rows = _load_ai_meta_rows(sheets, sheet_id)

        idx_map = _header_index_map(header)
        row_anchor = get_row_anchor(current_rowvals, header)
        row_snapshot_before = _build_row_snapshot(header, current_rowvals)
        row_after = list(current_rowvals or [])
        if len(row_after) < len(header or []):
            row_after.extend([""] * (len(header or []) - len(row_after)))

        data_payload = []
        applied, skipped = [], []
        flyer_fallback_write_guards = {}
        verified_broker_flyer_urls = {
            normalized
            for value in (broker_flyer_url_evidence or [])
            if (normalized := _normalize_safe_broker_flyer_url(value)) is not None
        }

        for upd in proposal["updates"]:
            col_name = (upd.get("column") or "").strip()
            new_val  = "" if upd.get("value") is None else str(upd.get("value"))
            conf     = upd.get("confidence")
            reason   = upd.get("reason")
            is_verified_flyer_fallback = False

            key = col_name.strip().lower()
            if key not in idx_map:
                skipped.append({"column": col_name, "reason": "unknown header"})
                continue

            # Skip formula columns (e.g. Gross Rent) - computed on the sheet; writing a raw
            # value clobbers the live formula cell. Deterministic code guard, not prompt-only.
            if _is_formula_column(col_name):
                skipped.append({"column": col_name, "reason": "formula-column"})
                continue

            # Flyer/Floorplan columns normally remain owned by the Drive upload
            # pipeline. Only the primary Flyer/Link field can use the ordinary
            # broker-URL fallback, and only when the caller supplied evidence.
            if is_asset_column_name(col_name, column_config):
                canonical_asset = canonical_field_for_column(col_name, column_config)
                if canonical_asset != "flyer_link" or broker_flyer_url_evidence is None:
                    skipped.append({"column": col_name, "reason": "handled-by-asset-pipeline"})
                    continue
                normalized_url = _normalize_safe_broker_flyer_url(new_val)
                if normalized_url is None:
                    skipped.append({"column": col_name, "reason": "invalid-asset-url"})
                    continue
                if normalized_url not in verified_broker_flyer_urls:
                    skipped.append({
                        "column": col_name,
                        "reason": "unverified-current-message-url",
                    })
                    continue
                new_val = normalized_url
                is_verified_flyer_fallback = True

            # Reject any file:// URLs - these are local paths that shouldn't be in the sheet
            if new_val.startswith("file://"):
                skipped.append({"column": col_name, "reason": "invalid-local-path"})
                continue

            # Reject unresolved template placeholders (e.g. "[NAME]", "[BROKER]").
            # Same leak class the outbound-email path blocks via
            # outbound_safety.find_unresolved_placeholders - never write a literal
            # placeholder into a client sheet cell (HEAD).
            if find_unresolved_placeholders(new_val):
                skipped.append({"column": col_name, "reason": "placeholder-value"})
                continue

            # Reject data placeholders (TBD / TBA / N/A / pending / unknown / none /
            # "To follow" / "ask landlord" / - ) for ANY cell including empty ones — a
            # deferral is not a spec value (HEAD _is_placeholder_data_value + #19 live
            # breaks E1 TBD->Power, E2 N/A->Docks).
            _new_clean = new_val.strip().strip(".").lower()
            _placeholder_values = {
                "tbd", "tba", "n/a", "na", "?", "unknown", "pending", "none", "-", "--",
            }
            if _is_placeholder_data_value(new_val) or _new_clean in _placeholder_values:
                skipped.append({"column": col_name, "reason": "placeholder-value", "value": new_val})
                continue

            col_idx = idx_map[key]                     # 1-based
            col_letter = _col_letter(col_idx)          # A1
            rng = f"{tab_title}!{col_letter}{rownum}"

            old_val = current_rowvals[col_idx-1] if (col_idx-1) < len(current_rowvals) else ""

            typed_new_val = coerce_sheet_value_for_column(
                col_name,
                new_val,
                column_config,
            )

            # 1) no-op
            if sheet_values_equal_for_column(
                col_name,
                old_val,
                typed_new_val,
                column_config,
            ):
                skipped.append({
                    "column": col_name,
                    "reason": "no-change",
                    "oldValue": old_val,
                    "newValue": new_val,
                })
                continue

            # Ordinary broker links only fill an empty Flyer/Link cell. The
            # generic high-confidence rule below must never replace a curated
            # human URL (and replacing an existing Drive/AI asset is unsafe too).
            if is_verified_flyer_fallback and (old_val or "").strip():
                skipped.append({
                    "column": col_name,
                    "reason": "existing-human-value",
                    "oldValue": old_val,
                    "confidence": conf,
                })
                continue

            # Check AI_META for write guards
            meta = _find_ai_meta_row(
                ai_meta_rows,
                rownum,
                col_name,
                row_anchor=row_anchor,
            )

            # 2) prior AI write and human changed it
            if (
                meta
                and meta.get("last_ai_value") is not None
                and not sheet_values_equal_for_column(
                    col_name,
                    old_val,
                    meta["last_ai_value"],
                    column_config,
                )
            ):
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
            data_payload.append({"range": rng, "values": [[typed_new_val]]})
            if (col_idx - 1) < len(row_after):
                row_after[col_idx - 1] = new_val
            applied.append({
                "column": col_name,
                "range": rng,
                "oldValue": old_val,
                "newValue": new_val,
                "confidence": conf,
                "reason": reason,
            })
            if is_verified_flyer_fallback:
                flyer_fallback_write_guards[rng] = {
                    "column": col_name,
                    "columnIndex": col_idx - 1,
                    "confidence": conf,
                }

        # The proposal snapshot predates attachment/link processing and the
        # model call. Re-read every ordinary Flyer/Link fallback at the latest
        # possible gate, immediately before the batch write is constructed.
        # Any non-empty live value wins regardless of proposal confidence.
        for fallback_range, guard in flyer_fallback_write_guards.items():
            fresh_response = _execute_with_retry(
                sheets.spreadsheets().values().get(
                    spreadsheetId=sheet_id,
                    range=fallback_range,
                ),
                "read_flyer_link_before_fallback",
            )
            fresh_rows = (fresh_response or {}).get("values") or []
            fresh_old_val = (
                fresh_rows[0][0]
                if fresh_rows and fresh_rows[0]
                else ""
            )
            if not str(fresh_old_val or "").strip():
                continue
            data_payload = [
                item for item in data_payload
                if item.get("range") != fallback_range
            ]
            applied = [
                item for item in applied
                if item.get("range") != fallback_range
            ]
            row_after[guard["columnIndex"]] = fresh_old_val
            skipped.append({
                "column": guard["column"],
                "reason": "existing-human-value",
                "oldValue": fresh_old_val,
                "confidence": guard["confidence"],
            })

        if not data_payload:
            return {
                "applied": [],
                "skipped": skipped,
                "rowNumber": rownum,
                "targetAnchor": row_anchor,
                "rowSnapshotBefore": row_snapshot_before,
                "rowSnapshotAfter": _build_row_snapshot(header, row_after),
            }

        # Execute batch update
        _execute_with_retry(
            sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=sheet_id,
                body={
                    "valueInputOption": "RAW",
                    "data": data_payload
                }
            ),
            "apply_proposal_batch_update"
        )

        def _rollback_unprotected(changes: List[dict]) -> None:
            if not changes:
                return
            _execute_with_retry(
                sheets.spreadsheets().values().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={
                        "valueInputOption": "RAW",
                        "data": [
                            {"range": change["range"], "values": [[change["oldValue"]]]}
                            for change in changes
                        ],
                    },
                ),
                "rollback_unprotected_sheet_values",
            )

        # Append each guard record after the value batch. If the append response
        # is lost, read back AI_META before deciding whether a rollback is needed.
        for index, a in enumerate(applied):
            logger.debug(
                "sheet.ai_meta_append",
                extra={
                    "spreadsheet_id": sheet_id,
                    "rownum": rownum,
                    "column": a["column"],
                    "value": a["newValue"],
                    "override": False,
                    "row_anchor": row_anchor,
                    "source": "apply_proposal_to_sheet",
                },
            )
            try:
                _append_ai_meta(
                    sheets,
                    sheet_id,
                    rownum,
                    a["column"],
                    a["newValue"],
                    override=False,
                    row_anchor=row_anchor,
                    ensure_tab=False,
                )
            except Exception as meta_error:
                try:
                    latest_meta_rows = _load_ai_meta_rows(sheets, sheet_id)
                except Exception as readback_error:
                    # Without readback, no changed value may remain potentially
                    # unguarded. Roll back this value and every later value.
                    try:
                        _rollback_unprotected(applied[index:])
                    except Exception as rollback_error:
                        raise RuntimeError(
                            "AI_META append, readback, and rollback all failed"
                        ) from rollback_error
                    raise RuntimeError(
                        "AI_META append outcome could not be reconciled"
                    ) from readback_error

                if _ai_meta_confirms_value(
                    latest_meta_rows,
                    rownum,
                    a["column"],
                    a["newValue"],
                    row_anchor,
                ):
                    continue

                try:
                    _rollback_unprotected(applied[index:])
                except Exception as rollback_error:
                    raise RuntimeError(
                        "AI_META append failed and sheet rollback failed"
                    ) from rollback_error
                raise meta_error

        try:
            from .sheet_operations import _apply_gross_rent_formula_for_row
            if _apply_gross_rent_formula_for_row(sheets, sheet_id, tab_title, header, rownum):
                print(f"✅ Refreshed Gross Rent formula for row {rownum}")
        except Exception as formula_err:
            print(f"⚠️ Could not refresh Gross Rent formula for row {rownum}: {formula_err}")

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
                if reason == "no-change":
                    print(f"     • {s.get('column', 'Unknown')} (reason: no-change)")
                    continue
                old_val = s.get('oldValue', '')
                conf = s.get('confidence', 'N/A')
                print(f"     • {s.get('column', 'Unknown')}: '{old_val}' (reason: {reason}, confidence: {conf})")

        return {
            "applied": applied,
            "skipped": skipped,
            "rowNumber": rownum,
            "targetAnchor": row_anchor,
            "rowSnapshotBefore": row_snapshot_before,
            "rowSnapshotAfter": _build_row_snapshot(header, row_after),
        }

    except Exception as e:
        print(f"❌ Failed to apply proposal to sheet: {e}")
        raise

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
                          extraction_fields: List[str] = None,  # Optional: list of canonical field keys user wants extracted
                          dry_run: bool = False,
                          authenticated_mailbox_email: str = None, runtime=None) -> Optional[Dict]:
    """
    Uses OpenAI Responses API to propose sheet updates.
    - Grounds on the current row's (address, city) as TARGET PROPERTY.
    - Shows the model the attachment names so it can pick the right PDF.
    - Enforces strict event and document-selection rules.

    Args:
        conversation: Optional pre-built conversation payload. If provided, skips Firestore fetch.
                     Format: [{"direction": "inbound/outbound", "from": "...", "to": [...],
                              "subject": "...", "timestamp": "...", "content": "..."}]
        extraction_fields: Optional list of canonical field keys (e.g., ["total_sf", "ops_ex_sf"]) that the user
                          wants extracted. If provided, only these fields will be included in extraction rules.
                          If None, all extractable fields are used.
        dry_run: If True, skips Firestore logging (useful for testing).
    """
    try:
        # Build conversation payload (chronological; latest last)
        # If conversation is provided directly (e.g., from tests), use it; otherwise fetch from Firestore
        if conversation is None:
            # Pass headers to fetch from Graph API (includes manual emails we didn't index)
            conversation = build_conversation_payload(
                uid,
                thread_id,
                limit=10,
                headers=headers,
                authenticated_mailbox_email=authenticated_mailbox_email,
            )

        # ---- Rules sections ---------------------------------------------------
        # A campaign without a complete persisted contract must not reach the model.
        config_error = get_column_config_error(column_config)
        if config_error:
            print(f"❌ Refusing unsafe columnConfig: {config_error}")
            return None
        effective_config = column_config

        # The persisted columnConfig is canonical. A duplicated client-level value
        # may be present for legacy readers, but drift must not re-enable Skip fields.
        if extraction_fields is not None and (
            not isinstance(extraction_fields, list)
            or any(not isinstance(field, str) for field in extraction_fields)
        ):
            print("❌ Refusing unsafe extractionFields: expected a list of strings")
            return None
        configured_extraction_fields = effective_config["extractionFields"]
        if extraction_fields is not None and set(extraction_fields) != set(configured_extraction_fields):
            print("❌ Refusing unsafe extractionFields: value disagrees with columnConfig")
            return None
        effective_config = _filter_config_by_extraction_fields(
            effective_config,
            configured_extraction_fields,
        )

        prepared_attachment_manifest = _prepare_ai_attachment_manifest(
            pdf_manifest
        )
        if prepared_attachment_manifest is None:
            print("❌ Refusing malformed native-image attachment manifest")
            return None
        analysis_attachment_manifest = [
            prepared_attachment.fresh_analysis_manifest()
            for prepared_attachment in prepared_attachment_manifest
        ]

        COLUMN_RULES = build_column_rules_prompt(effective_config)

        DOC_SELECTION_RULES = """
DOCUMENT SELECTION & EXTRACTION (strict):
- FIELD VALUES ONLY: when the latest broker message and an attachment conflict, use the latest broker message.
  Use attachments only to fill field values that the latest broker message does not provide.
- Extract values ONLY for the TARGET PROPERTY. If a PDF shows multiple buildings/addresses, use the page/section
  that explicitly matches the TARGET PROPERTY (address/city). If no exact match, do not use that PDF for updates.
- If an attachment clearly refers to a different address, ignore it unless the LAST HUMAN message explicitly proposes
  it as an additional property (then you may emit a new_property event).
- If a brochure lists multiple options (e.g., Building C & D), pick the option that most clearly matches the TARGET
  PROPERTY/suite. If ambiguous, SKIP that field rather than guessing.
- If the LAST HUMAN message offers multiple buildings/suites that are not the TARGET PROPERTY, emit one new_property
  event per distinct qualifying option. Include the building/suite label in each event address and keep each option's
  own SF, rent, and notes together. Never write an aggregate brochure value or another option's value to the TARGET row.
- If the offered options cannot be bound safely to distinct buildings/suites, emit needs_user_input with reason
  "multi_property_attachment" and do not propose TARGET-row updates.

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

- "property_unavailable": Emit when the CURRENT TARGET PROPERTY is explicitly stated as unavailable/leased/off-market/no longer available OR when the broker clearly says the property is non-viable for the client's requirements.
  • A factual zero (for example, no drive-in doors) is sheet data, not proof the property is unavailable or a bad fit.
  • Emit a requirements mismatch only when the broker explicitly rules out the target as a fit or states an independent physical non-fit such as office-heavy, not a true warehouse, or below-spec clear height.
  • If zero access conflicts with a stated positive minimum or may be remediated (for example by ramping a dock), emit needs_user_input and do not draft a reply.
  • DO NOT emit property_unavailable when the broker says only tours/showings are unavailable. "The space is no longer available for tours" means tour scheduling cannot continue, not that the property/listing is unavailable.
  • Do NOT use this for vague relationship refusals like "we are not a fit to work together" unless the property itself is being ruled out.
  • ALWAYS populate a non-empty "reason" so downstream has an evidence trail: use "requirements_mismatch" for a physical non-fit, otherwise a short terminal reason such as "leased", "off_market", "under_contract", "signed_lease", or "no_longer_available".
  • The terminal signal must be about the TARGET PROPERTY. A different building being leased ("we just closed 9 Center Drive"), a comps reference ("what recently leased along the corridor"), or an ancillary asset ("the trailer lot is leased separately") does NOT make the target unavailable.

- "new_property": Emit when the LAST HUMAN message suggests or mentions a DIFFERENT property than the TARGET PROPERTY.
  • Look for phrases like: "we have another", "different location", "alternative property", "other space available"
  • Look for URLs pointing to different properties/listings
  • Look for property names, addresses, or locations mentioned that are NOT the TARGET PROPERTY
  • If mentioning "forestville", "centre", "woodmore" or other location names different from TARGET, this likely indicates new_property
  • ADDRESS EXTRACTION RULES:
    - If a SPECIFIC street address is mentioned (e.g., "123 Main St", "500 Industrial Pkwy"), use that as the "address"
    - If only a building/property NAME is mentioned (e.g., "The Commerce Center", "Woodmore Plaza"), use that as the "address"
    - If only a VAGUE DESCRIPTION is given (e.g., "new development", "another property nearby", "similar building on X street"):
      * Prefix the address with "[TBD]" to indicate it needs user clarification
      * Example: "[TBD] new development on Trade Center Court"
      * This signals to the user they should get the real address before sending
  • Try to infer city/location from context or URL
  • IMPORTANT: If a DIFFERENT contact person is mentioned (e.g., "email Joe Smith at joe@email.com", "contact Sarah Jones", "reach out to Mike Brown"):
    - Extract the contact FULL NAME as "contactName" (e.g., "Joe Smith", "Sarah Jones", "Mike Brown")
    - If only first name is available, use just the first name
    - Extract their email as "email" field
  • The contactName is CRITICAL for personalized outreach - extract the full name when available, first name if that's all you have
  • REFERRAL-TRIGGERED, NOT MENTION-TRIGGERED: only emit new_property when the other property is actually being OFFERED TO US as a live lead and is plausibly in scope. A second address alone is NOT enough. DO NOT emit new_property for a property that is:
    - described as leased / closed / off-market / not available ("that one's fully leased", "we just closed on it")
    - withdrawn by the broker in the same breath ("won't waste your time with it", "not what you're after")
    - explicitly not-on-offer or confidential ("ignore the chatter about X", "isn't on offer", "keep it quiet")
    - a tenant's own relocation / build-to-suit destination (where the incumbent is GOING, not a space on the market)
    - sourced only from a PDF/attachment rather than the LAST HUMAN message text
  • If your own notes for the event would say it is "not available", "not on offer", "not a fit", or "not the target", DO NOT emit the event at all.

- "call_requested": Only when someone explicitly asks for a call/phone conversation. Use this event (NOT needs_user_input) for phone call requests.

- "close_conversation": Emit when the conversation should end. Use in these situations:
  • ALL REQUIRED FIELDS ARE COMPLETE (MISSING REQUIRED FIELDS is empty) - emit with notes "all_info_gathered"
  • "Going exclusive" with another party/tenant rep - notes "exclusive_with_another"
  • Already have a deal/tenant lined up ("likely signing next week", "in negotiations with someone") - notes "deal_pending"
  • Broker declines to continue without making the property physically non-viable ("can't help right now", "not a fit to work together") - notes "not_a_fit"
  • Natural conversation ending ("thanks for reaching out", "good luck with your search") - notes "natural_end"
  IMPORTANT: When emitting close_conversation with "all_info_gathered", you SHOULD include a brief closing response_email thanking them.
  For other close_conversation reasons, do NOT emit response_email - the conversation is OVER.

- "tour_requested": Emit when broker offers or requests a property tour/showing. This is DIFFERENT from needs_user_input.
  • Look for: "schedule a tour", "would you like to see it", "happy to show you", "can arrange a tour",
    "want to come by", "stop by and take a look", "walk through the property", "showing available"
  • A generic courtesy sign-off such as "let me know if you need/want a tour" or "feel free to let me know if you want to see it" is NOT an actionable tour request unless the broker also gives concrete timing or directly asks/offers to schedule. Do not emit an event for the generic sign-off alone.
  • For a real tour event, copy the exact triggering broker-authored sentence into question. Never paraphrase or strengthen the broker's wording.
  • DO NOT emit when the broker merely sends specs, says a property is available, attaches a flyer, or when quoted
    history/outbound text mentions "tour availability" as one of the requested fields.
  • DO NOT infer a tour offer from "available immediately", "available SF", "tourable", or "attached is the flyer"
    unless the LAST HUMAN message explicitly offers/request a showing or tour.
  • The user needs to decide whether to schedule the tour, so DO NOT auto-respond
  • Instead, GENERATE a suggested response email in the "suggestedEmail" field that the user can approve/edit
  • Example suggestedEmail: "Hi [NAME], Thank you for the offer! I'd like to schedule a tour. Are you available [suggest a few time options]? Looking forward to seeing the space."
  • If this is a reply to a tour invite and the broker says tours/showings are no longer available, still emit tour_requested with reason "tour_unavailable"; do not emit property_unavailable.
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

  IMPORTANT - NOT a client_question:
  • "Let me know if you need anything else" = This is the broker OFFERING to provide more info, NOT asking a question
  • "Happy to help with anything else" = Same - broker offering help
  • "What else do you need?" = Same - broker asking what PROPERTY info is missing
  • For these phrases: Check if required fields are missing and generate a response_email asking for them
  • Do NOT emit needs_user_input for these - they are invitations to continue the conversation

  Include "reason" field explaining WHY user input is needed (use ONLY these values — never invent "scheduling", "", or other off-enum strings):
  • "client_question" - broker asking about client's requirements (e.g., "what size does your client need?", "what's your budget?")
  • "negotiation" - price or term negotiation
  • "confidential" - asking for CLIENT IDENTITY specifically (who is your client / what company). Do NOT use "confidential" for benign tour logistics such as attendee names for a building gate/visitor list — that is not a client-identity question.
  • "legal_contract" - contract/LOI/lease questions
  • "unclear" - message is confusing or unclear
  • "multi_property_attachment" - attachment details cannot be bound safely to one property or suite
  • A request to reschedule or set up a PHONE CALL is call_requested, NOT needs_user_input.

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
  • SUBJECT ATTRIBUTION (critical): the opt-out must be asserted BY or ABOUT the person who SENT this message (the row contact). DO NOT emit contact_optout when the opt-out belongs to:
    - a CC'd third party ("I've cc'd Tom, keep him off your lists — but on the space: still available...") — the sender is still engaged
    - a quoted/forwarded stranger whose removal request sits only in reply history
    - a machine/banner notice ("[AUTOMATED NOTICE] X has OPTED OUT") that the human sender explicitly disclaims ("ignore the robo-banner")
    An opt-out about someone OTHER than the sender must NOT kill this thread; keep the conversation alive.

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
  • DO NOT emit wrong_contact when suggestedContact/suggestedEmail is the SAME person who sent the message or the row Contact Name (a forward-then-introduce hand-off where the sender IS now the right contact — "Alex here, I'm the right contact now" is NOT a redirect).
  • DO NOT emit wrong_contact for a TEMPORARY ABSENCE: an out-of-office / auto-reply that gives a return date and an assistant "for urgent matters" is not a statement that the sender is the wrong contact — wait for their return, do not swap the sheet contact.
  • Redirect signals living only in quoted/forwarded reply history are NOT the live message — ignore them.
  • DO NOT emit wrong_contact for an OUT-OF-OFFICE / AUTO-REPLY. An OOO auto-reply
    (e.g. "I'm out of office until July 10", "OOO: traveling this week") that lists a
    backup or assistant address ("for urgent matters, contact X", "please contact my
    assistant X") is NOT a handoff off this property. Do not surface that backup/assistant
    address as suggestedContact/suggestedEmail. Treat the auto-reply as ignore/continue.

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
- "This space isn't a good fit because it's more office-heavy than warehouse and has no drive-in space" = property_unavailable event (reason: requirements_mismatch)
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
NOTES FIELD (IMPORTANT - avoid redundancy):
The "notes" field captures contextual information that DOES NOT go in other columns.

NEVER INCLUDE IN NOTES (these go in columns):
- Rent amounts (goes in Rent/SF column)
- Square footage (goes in Total SF column)
- Operating expenses (goes in Ops Ex column)
- Dock/drive-in counts (go in Docks/Drive Ins columns)
- Ceiling height (goes in Ceiling Ht column)
- Power specs (goes in Power column)

ALWAYS CAPTURE IN NOTES (context not in columns):
- Lease type: "NNN", "gross lease", "modified gross"
- Availability timing: "available immediately", "60 days notice", "available March 1st"
- Landlord motivation: "owner motivated", "firm on price", "willing to negotiate"
- TI/buildout: "TI allowance available", "$10/SF TI", "as-is condition"
- Special features: "fenced yard", "rail spur", "sprinklered", "ESFR", "food grade"
- Parking/trailer context: parking count, ample parking, truck/trailer parking, fenced trailer parking, trailer storage
- Zoning/use: "zoned M-1", "heavy industrial allowed", "no outdoor storage"
- Location context: "near I-20", "airport adjacent", "in industrial park"
- Divisibility: "can subdivide", "must take full space"
- Building info: "built 2020", "renovated 2023", "tilt-up construction"
- Sublease details: "sublease through 2025", "direct lease preferred"

FORMAT: Terse fragments separated by " • "
GOOD: "NNN • available immediately • owner motivated • fenced yard"
BAD: "40,000 SF • $8.50/SF rent • 2 docks" (these belong in columns, not notes!)
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
- Write in a professional, friendly tone suitable for commercial real estate outreach and the sender's configured profile
- CRITICAL: Vary your language naturally - NEVER use the same phrases repeatedly across emails
- Reference specific details from the conversation to show you're paying attention
- Keep responses concise and to the point - short and direct
- DO NOT use phrases like "Looking forward to your response" or "Looking forward to hearing from you"

PHRASE VARIATION RULES (MANDATORY - rotate through these options):

GREETINGS (pick one based on context and vary across messages):
- With name (use the FIRST NAME FOR GREETINGS provided above):
  * "Hi {FirstName}," | "Thanks {FirstName}," | "{FirstName}," | "Hi {FirstName} -"
- Without name (for brief requests, quick follow-ups, or if no contact name provided):
  * "Hi," | "Thanks," | "Thank you,"

THANKING FOR INFORMATION (rotate - never use same phrase twice in a row):
- "Thank you for sending over the details on [property]"
- "Thanks for the info on [property]"
- "Appreciate you sending this over"
- "Got it - thanks for the breakdown on [property]"
- "Thanks for pulling this together"
- "This is great, thanks"
- "Perfect, thank you"
- "Thanks for getting back to me on [property]"

ACKNOWLEDGING COMPLETE INFO / CLOSING (rotate these phrases):
- "I have everything I need" → "This covers everything I needed"
- "I'll review this with my client" → "I'll go over this with my client" → "I'll pass this along to my client" → "I'll run this by my client"
- "I'll be in touch if we have questions" → "I'll reach out if anything comes up" → "I'll circle back if we need anything else" → "Will follow up if we have any questions"
- "Thanks again" → "Appreciate it" → "Thanks for your help" → "Thanks for the quick response"

REQUESTING MISSING INFO (rotate these patterns):
- "Could you also let me know..." → "One more thing - do you have..." → "To round out the details, could you confirm..."
- "I'm still missing..." → "A few items I still need..." → "To complete the picture, I'd need..."
- "Would you happen to have..." → "Any chance you can share..." → "Do you know the..."

ACKNOWLEDGING UNAVAILABLE PROPERTY (vary these):
- "Understood on [property] being off the market"
- "Got it - thanks for the heads up on [property]"
- "No worries, appreciate the update"
- "Thanks for letting me know about [property]"

ASKING FOR ALTERNATIVES (rotate):
- "Do you have anything else that might work?"
- "Any other spaces you'd recommend?"
- "Anything else in the area that could be a fit?"
- "Are there other options you'd suggest?"

IMPORTANT: Before generating a response, mentally check what phrases you've used in this conversation thread and pick DIFFERENT ones. The goal is to sound like a real person having a natural conversation, not a template.

SCENARIOS:
1. Missing required fields: Thank them for the information, then list the missing fields naturally in a bulleted format.
   EXAMPLE VARIATIONS (rotate these styles):

   Style A: "Thanks for the info on [property]. A few items I still need:
   - Total SF
   - Ops Ex /SF
   - Docks
   Thanks."

   Style B: "Got it - appreciate you sending this over. To round out the details, could you confirm:
   - Ceiling Ht
   - Power
   - Drive Ins
   Thanks."

   Style C: "[Name], Thank you for the breakdown. One more thing - do you have the following?
   - Total SF
   - Ops Ex /SF
   Thanks."

   IMPORTANT:
   - ONLY request fields that are in the MISSING REQUIRED FIELDS list provided above
   - NEVER request fields that are NOT in the missing required fields list
   - NEVER request "Gross Rent" - this is a formula column that calculates automatically
   - Keep it short and concise
   - End with a simple "Thanks" - do NOT use "Looking forward to your response" or similar phrases

2. All required fields complete (MISSING REQUIRED FIELDS is empty):
   - Send a brief closing email thanking them for the information
   - Indicate you have everything you need and will review with your client
   - DO NOT ask for any additional information - the conversation is complete
   EXAMPLE VARIATIONS (use different phrasing each time):
   - "Thanks for pulling this together. This covers everything I needed - I'll run this by my client and reach out if anything comes up."
   - "Perfect, thank you. I have everything I need and will go over this with my client. Will follow up if we have any questions."
   - "Got it - thanks for the quick response. I'll pass this along to my client and circle back if we need anything else."
   - "Appreciate you sending this over. This is everything I need - I'll review with my client and be in touch if questions come up."
3. Property unavailable + new property suggested:
   - Thank them briefly for the update on the original property
   - Show IMMEDIATE INTEREST in the new property - don't be lukewarm or passive
   - Ask for key details on the new property OR acknowledge you'll review their materials and follow up
   - Be enthusiastic - a broker handing you a new lead is valuable
   - IMPORTANT: We will send a separate outreach email to the new property, so this response should express interest and set up that follow-up
   - GOOD EXAMPLES:
     * "Thanks for the heads up on [original]. [New property] looks promising - I'll review what you sent and reach out with a few questions."
     * "Got it on [original]. Thanks for flagging [new property] - that could work well. I'll take a look and follow up."
     * "Understood on [original]. [New property] sounds like it could be a good fit - I'll dig into the details and get back to you."
   - BAD (too passive): "I'll circle back if it looks like a fit" - NO! Always show interest when given a new lead.
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
        
        # Identify the live sender from the newest inbound message (from-address +
        # signature) so the mapped greeting name can be reconciled against it.
        latest_inbound_msg = next(
            (m for m in reversed(conversation or []) if (m.get("direction") or "").lower() == "inbound"),
            {},
        ) or {}
        sender_email = (latest_inbound_msg.get("from") or email or "").strip()
        sender_display_name = (latest_inbound_msg.get("fromName") or latest_inbound_msg.get("senderName") or "").strip()
        raw_latest_human = _raw_latest_inbound(conversation)
        last_human_message = _strip_quoted_history(raw_latest_human)

        # Build contact name context with an ADVISORY first name for greetings.
        # FIX-13/14: reconcile the mapped name against the live sender, strip
        # honorifics, and neutralize company names so the model never dead-names a
        # stale mapped person or greets "Hi Colliers,"/"Hi Dr.,".
        contact_context = ""
        if contact_name:
            greeting_first = _resolve_greeting_first_name(
                contact_name,
                sender_email=sender_email,
                # Use ONLY a real sender name/signature here. Never fall back to
                # the full inbound body: a raw substring match inside
                # _resolve_greeting_first_name would spuriously "agree" (e.g. the
                # mapped first name "Rob" appears inside "problem") and revive a
                # stale/wrong greeting the FIX-13/14 reconciliation exists to block.
                sender_signature_name=sender_display_name or None,
            )
            if greeting_first:
                contact_context = (
                    f"\nCONTACT NAME (from the sheet mapping — advisory, may be stale): {contact_name}"
                    f"\nSUGGESTED GREETING NAME: {greeting_first} (advisory — use 'Hi {greeting_first},' ONLY if it "
                    f"agrees with the live sender's name/signature; otherwise greet neutrally with 'Hi,')."
                )
            else:
                contact_context = (
                    f"\nCONTACT NAME (from the sheet mapping — advisory, may be stale): {contact_name}"
                    "\nGREETING: the mapped name is a company, an honorific, or disagrees with the live "
                    "sender — greet NEUTRALLY ('Hi,') or use the live sender's own name/signature. "
                    "Do NOT greet with the mapped name."
                )

        # FIX-08: give the model the quoted-history-stripped newest segment as the
        # AUTHORITATIVE last human message for EVENT detection. Quoted/forwarded
        # history stays in CONVERSATION HISTORY below as context only.
        last_human_block = ""
        if last_human_message:
            last_human_block = (
                "\nLAST HUMAN MESSAGE (AUTHORITATIVE — detect EVENTS from ONLY this text; "
                "treat quoted/forwarded reply history in the full thread below as context, not live signal):\n"
                f"{json.dumps(last_human_message)}\n"
            )

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
{last_human_block}
CONVERSATION HISTORY (latest last, for CONTEXT — includes quoted/forwarded history):
{json.dumps(conversation, indent=2)}
""".rstrip()]

        prepared_natives = [
            prepared_attachment.native
            for prepared_attachment in prepared_attachment_manifest
            if prepared_attachment.is_native
        ]
        if prepared_natives:
            for attachment_index, prepared_attachment in enumerate(
                prepared_attachment_manifest,
                start=1,
            ):
                if prepared_attachment.is_native:
                    image_count = len(
                        prepared_attachment.native.image_meta
                    )
                    prompt_parts.append(
                        "\n\n=== NATIVE IMAGE ATTACHMENTS ==="
                    )
                    prompt_parts.append(
                        "\nThese are prevalidated native images bound "
                        "explicitly to the TARGET PROPERTY. Treat every "
                        "image in this attachment as target-row visual "
                        "evidence; do not reinterpret it as addressless or "
                        "competing."
                    )
                    prompt_parts.append(
                        f"\n--- Attachment {attachment_index}: "
                        "type=prevalidated_native_target_images; "
                        f"image_count={image_count} ---"
                    )
                    continue

                attachment = prepared_attachment.fresh_legacy_manifest()
                images = attachment.get("images") or []
                file_id = attachment.get("file_id") or attachment.get("id")
                has_file_fallback = bool(
                    file_id
                    and attachment.get("method") in (
                        "openai_upload",
                        "openai_upload+images",
                        "failed",
                    )
                )
                prompt_parts.append("\n\n=== PDF ATTACHMENTS ===")
                prompt_parts.append(
                    f"\n--- Attachment {attachment_index}: "
                    "type=legacy_pdf; "
                    f"preview_image_count={len(images[:3])}; "
                    "input_file_fallback="
                    f"{'yes' if has_file_fallback else 'no'} ---"
                )
                name = attachment.get("name") or "<unnamed.pdf>"
                text = attachment.get("text") or ""
                method = attachment.get("method", "unknown")
                prompt_parts.append(
                    f"\n--- PDF: {name} (extraction method: {method}) ---"
                )
                if text:
                    # Include extracted text; clip but retain deep field-bearing lines.
                    prompt_parts.append(
                        _clip_for_prompt(text, _PDF_TEXT_CHAR_LIMIT)
                    )
                else:
                    prompt_parts.append(
                        "[No text extracted - see images below if available]"
                    )
        else:
            # PDF attachments - include extracted text directly in prompt
            legacy_pdf_entries = [
                prepared_attachment.fresh_legacy_manifest()
                for prepared_attachment in prepared_attachment_manifest
                if not prepared_attachment.is_native
            ]
            if legacy_pdf_entries:
                prompt_parts.append("\n\n=== PDF ATTACHMENTS ===")
                for pdf in legacy_pdf_entries:
                    name = pdf.get("name") or "<unnamed.pdf>"
                    text = pdf.get("text") or ""
                    method = pdf.get("method", "unknown")

                    prompt_parts.append(f"\n--- PDF: {name} (extraction method: {method}) ---")
                    if text:
                        # Include extracted text; clip but retain deep field-bearing lines.
                        prompt_parts.append(_clip_for_prompt(text, _PDF_TEXT_CHAR_LIMIT))
                    else:
                        prompt_parts.append("[No text extracted - see images below if available]")

        # URL content (already fetched)
        if url_texts:
            prompt_parts.append("\nURL CONTENT FETCHED:")
            for url_info in url_texts:
                prompt_parts.append(f"\nURL: {url_info['url']}")
                prompt_parts.append(f"Content: {_clip_for_prompt(url_info.get('text') or '', _URL_TEXT_CHAR_LIMIT)}")

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
      "address": "<for new_property: extract street address or building name. If only vague description available, prefix with [TBD] e.g. '[TBD] new development on Main St'>",
      "city": "<for new_property: infer city/location if possible>",
      "email": "<for new_property if different email/contact needed>",
      "contactName": "<for new_property: full name of the new contact if mentioned, e.g., 'Joe Smith' from 'email Joe Smith at joe@email.com'. Use first name only if that's all available>",
      "link": "<for new_property: include URL if mentioned>",
      "notes": "<for new_property: additional context about the property>",
      "reason": "<for needs_user_input: client_question | negotiation | confidential | legal_contract | unclear | multi_property_attachment> OR <for contact_optout: not_interested | unsubscribe | do_not_contact | no_tenant_reps | direct_only | hostile> OR <for wrong_contact: no_longer_handles | wrong_person | forwarded | left_company> OR <for tour_requested: tour_offer | tour_slot_reply | tour_unavailable>",
      "question": "<for needs_user_input: the specific question/request that needs user attention; for tour_requested: the exact broker-authored sentence that triggered the event, copied verbatim without paraphrasing>",
      "suggestedContact": "<for wrong_contact: name of correct person to contact>",
      "suggestedEmail": "<for wrong_contact: email of correct person if provided>",
      "suggestedPhone": "<for wrong_contact: phone of correct person if provided>",
      "issue": "<for property_issue: specific description of the problem/concern>",
      "severity": "<for property_issue: critical | major | minor>"
    }
  ],
  "response_email": "<Generate a professional response email body (plain text only). Start with greeting (e.g., 'Hi,'), include main message content, and end with your content - DO NOT include 'Best,' or any closing/signature as the footer will add 'Best,' and full signature automatically. Should be contextual to the conversation, reference specific details when possible, and vary wording to avoid repetition. SET TO NULL when: (1) call_requested with phone number provided, (2) needs_user_input event detected, (3) contact_optout event detected, (4) wrong_contact event detected. The system will notify the user instead of auto-responding.>",
  "notes": "<IMPORTANT: Capture contextual details NOT already in columns. NEVER repeat values being written to columns (rent amounts, SF, ops ex, docks, power, etc.). DO capture: lease type (NNN/gross), availability timing, landlord motivation (motivated/firm), building features (fenced yard, rail spur, sprinklered), parking/trailer context such as parking count or trailer parking, zoning, location context, divisibility, TI allowance, sublease terms. Format: terse fragments separated by ' • '. Example: 'NNN • available immediately • owner motivated • fenced yard • 30 parking spaces • near I-20'. Leave empty if no additional context beyond column data.>"
}
""")

        prompt = "".join(prompt_parts)

        # ---- Prepare inputs (images for vision, files as fallback, then text) --------------------------
        input_content = []

        # Add native images inline and retain the existing PDF request behavior.
        if prepared_attachment_manifest:
            for prepared_attachment in prepared_attachment_manifest:
                if prepared_attachment.is_native:
                    for image_number, img_b64 in enumerate(
                        prepared_attachment.native.images,
                        start=1,
                    ):
                        input_content.append({
                            "type": "input_image",
                            "image_url": f"data:image/png;base64,{img_b64}",
                        })
                        print(
                            "📷 Added prevalidated target native image "
                            f"{image_number} for vision analysis"
                        )
                    continue

                pdf = prepared_attachment.fresh_legacy_manifest()
                images = pdf.get("images") or []
                name = pdf.get("name", "PDF")

                # Add images for vision (pages with little extractable text)
                for i, img_b64 in enumerate(images[:3]):  # Max 3 pages per PDF
                    input_content.append({
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{img_b64}"
                    })
                    print(f"📷 Added page {i+1} image from {name} for vision analysis")

                # Add file_id as fallback if we have it and extraction was poor.
                file_id = pdf.get("file_id") or pdf.get("id")
                if file_id and pdf.get("method") in ("openai_upload", "openai_upload+images", "failed"):
                    input_content.append({"type": "input_file", "file_id": file_id})

        input_content.append({"type": "input_text", "text": prompt})

        # ---- Call OpenAI (low temperature for determinism) --------------------
        response = ai_for(runtime, client).create_response({
            "model": "gpt-5.2",  # GPT-5.2 Thinking for complex extraction
            "input": [{"role": "user", "content": input_content}],
            "temperature": 0.1,
        })
        if not dry_run:
            track_openai_usage_safely(
                db=_fs,
                user_id=uid,
                client_id=client_id,
                thread_id=thread_id,
                operation="ai.extract_sheet_updates",
                model="gpt-5.2",
                usage=getattr(response, "usage", None),
                request_id=getattr(response, "id", None),
                endpoint="responses",
                metadata={
                    "sheetId": sheet_id,
                    "rowNumber": rownum,
                    "headerCount": len(header or []),
                    "conversationMessageCount": len(conversation or []),
                    "hasPdfManifest": bool(prepared_attachment_manifest),
                    "pdfCount": len(prepared_attachment_manifest),
                    "urlTextCount": len(url_texts or []),
                    "configuredExtractionFieldCount": len(extraction_fields or []),
                },
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
        # Flyer/linked-PDF text is evidence for extraction + the fabricated-count
        # guard: a count stated only in the flyer is REAL, not invented.
        _evidence_extra_texts = [
            (pdf or {}).get("text") or ""
            for pdf in analysis_attachment_manifest
        ] + [
            (u or {}).get("text") or "" for u in (url_texts or [])
        ]
        proposal = _augment_proposal_with_deterministic_extractions(
            proposal, rowvals, header, effective_config, conversation,
            pdf_manifest=analysis_attachment_manifest,
            extra_texts=_evidence_extra_texts,
        )
        proposal = _augment_proposal_opex_basis(
            proposal, rowvals, header, effective_config, conversation
        )
        proposal = _suppress_fabricated_door_counts(
            proposal, conversation, header, effective_config,
            extra_texts=_evidence_extra_texts,
        )
        proposal = _augment_proposal_with_flyer_link(
            proposal, url_texts, rowvals, header, effective_config
        )
        # Strip events that only fire off quoted prior-thread history BEFORE the
        # deterministic event augmenter re-evaluates the fresh message.
        proposal = _suppress_quote_only_events(proposal, conversation)
        proposal = _augment_events_with_deterministic_signals(
            proposal,
            conversation,
            target_anchor=target_anchor,
            sender_email=sender_email,
            sender_name=sender_display_name,
            contact_name=contact_name,
            header=header,
            effective_config=effective_config,
            extra_texts=_evidence_extra_texts,
        )
        # A genuine contact opt-out must never write the opted-out row (LIVE break
        # adv_optout_with_specs). Runs AFTER the event augmenter so the engaged-
        # alternative guard has already dropped any scoped over-fired opt-out.
        proposal = _suppress_updates_on_contact_optout(proposal)
        proposal = sanitize_new_property_referral_response(
            proposal,
            original_contact_email=email,
        )
        proposal = _suppress_cross_property_current_row_updates(
            proposal,
            conversation,
            target_anchor,
        )
        proposal = _suppress_competing_attachment_updates(
            proposal,
            conversation,
            target_anchor,
            None,
            prepared_attachment_manifest=prepared_attachment_manifest,
        )

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
                "pdfManifest": [
                    prepared_attachment.fresh_persisted_manifest()
                    for prepared_attachment in prepared_attachment_manifest
                ],
                "fileIds": [
                    prepared_attachment.legacy_file_id()
                    for prepared_attachment in prepared_attachment_manifest
                    if prepared_attachment.legacy_file_id()
                ],
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
