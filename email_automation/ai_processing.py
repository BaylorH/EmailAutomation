import json
import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from google.cloud.firestore import SERVER_TIMESTAMP
from .clients import client, _sheets_client, _fs
from .budget_guard import BudgetDeferredError, should_block_openai_call
from .messaging import build_conversation_payload
from .sheets import _header_index_map, _get_first_tab_title, _col_letter, _execute_with_retry
from .app_config import REQUIRED_FIELDS_FOR_CLOSE
from .column_config import (
    CANONICAL_FIELDS,
    get_default_column_config,
    build_column_rules_prompt,
    get_required_fields_for_close,
    find_notes_comment_column_index,
    REQUIRED_FOR_CLOSE,
)
from .notification_payloads import sanitize_new_property_referral_response
from .openai_usage import track_openai_usage_safely
from .tour_scheduling import looks_like_tour_only_unavailable
from .outbound_safety import find_unresolved_placeholders

logger = logging.getLogger(__name__)


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
            + r"\s+(?:any\s+)?(?:drive[-\s]?in|grade[-\s]?level|dock)"
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

    physical_mismatch = (
        office_mismatch or warehouse_mismatch or access_mismatch or height_mismatch
    )

    return bool(fit_rejection or physical_mismatch)


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

    # Strong, unambiguous scheduling-reply signals (self-sufficient).
    strong_reply_signal = re.search(
        r"\b(?:that\s+time|that\s+slot|the\s+slot|requested\s+time|confirmed|"
        r"does\s+not\s+work|doesn[’']t\s+work|can't\s+do|cannot\s+do|won[’']t\s+work|"
        r"could\s+do|available\s+(?:at|around|after|before)|works\s+better|"
        r"reschedule|see\s+you|no\s+longer\s+available)\b",
        latest,
    )
    time_signal = re.search(r"\b(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)|morning|afternoon|noon)\b", latest)
    day_signal = re.search(
        r"\b(?:mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)(?:day|nesday|rsday|urday)?\b"
        r"|\b(?:today|tomorrow|tonight|next\s+week|this\s+week)\b",
        latest,
    )
    # Bare "works" / "instead" is only a scheduling reply when a concrete
    # time or day anchors it — otherwise the idiom "the works" or "works for the
    # client" falsely stripped a correct non-viable classification (A′ misread M04).
    weak_reply_signal = re.search(r"\b(?:works?|instead)\b", latest)
    return bool(
        strong_reply_signal
        or time_signal
        or (weak_reply_signal and (time_signal or day_signal))
    )


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


def _address_binding_numbers(text: str) -> set:
    """Digit runs usable as an address-binding proxy, EXCLUDING size/price/rate
    figures that are not street numbers (CodeRabbit PR#15).

    The terminal detector uses raw 3-6 digit tokens to decide whether a terminal
    phrase is bound to the TARGET address or a competing one. A size or price
    figure sharing the terminal sentence ("It's been leased, 42,000 SF at
    $8.75/SF NNN") must not be mistaken for a competing street address, which
    would drop a genuine property_unavailable signal. Grouped thousands
    separators are collapsed first ("42,000" -> one token) and figures adjacent
    to $, #, SF/PSF, %, /SF, NNN, ' (clear height), or a range dash are dropped.
    """
    collapsed = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", (text or "").lower())
    numbers = set()
    for m in re.finditer(r"\b(\d{3,6})\b", collapsed):
        start, end = m.start(1), m.end(1)
        before = collapsed[max(0, start - 1):start]
        after = collapsed[end:end + 8]
        if before in ("$", "#"):
            continue  # price / suite number, not a street address
        if re.match(
            r"\s*(?:sf\b|s\.?\s*f\.?|sq\b|square|psf\b|/\s*sf|per\s+sf|%|k\b|"
            r"nnn\b|'|’|-\s*\d)",
            after,
        ):
            continue  # size / rate / clear-height / numeric-range figure
        numbers.add(m.group(1))
    return numbers


def _detect_target_terminal_reason(latest_text: str, target_anchor: Optional[str]) -> Optional[str]:
    """Return a terminal reason ONLY when a terminal phrase binds to the TARGET
    property — negation-aware and target-grounded (A′ FIX-01, CodeRabbit PR#15).

    A terminal phrase is ignored when it is negated, bound to an ancillary asset /
    tour slot, or attributed to a DIFFERENT named address than the target. A bare
    terminal (no address in its sentence) is deferred when the message elsewhere
    asserts the target remains viable.
    """
    text = (latest_text or "").lower()
    target_numbers = _address_binding_numbers(target_anchor or "")
    has_global_viability = bool(_VIABILITY_RE.search(text))
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)

    for sentence in sentences:
        for reason, pattern in _UNAVAILABLE_PATTERNS:
            match = re.search(pattern, sentence)
            if not match:
                continue
            pre = sentence[max(0, match.start() - 14): match.start()]
            if re.search(r"\b(?:not|isn'?t|aren'?t|no)\s*$", pre):
                continue  # negated terminal
            if _ANCILLARY_SUBJECT_RE.search(sentence) or re.search(r"\bleased\s+separately\b", sentence):
                continue  # lease bound to an ancillary asset / tour slot
            sentence_numbers = _address_binding_numbers(sentence)
            if target_numbers and (target_numbers & sentence_numbers):
                return reason  # terminal explicitly about the TARGET address
            if sentence_numbers and target_numbers and not (target_numbers & sentence_numbers):
                continue  # terminal about a competing named address
            if sentence_numbers and not target_numbers:
                if has_global_viability:
                    continue
                return reason
            # Bare terminal (no address in this sentence): defer to a viability claim.
            if has_global_viability:
                continue
            return reason
    return None


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

        # (e) new_property whose own notes self-contradict the referral
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


def _augment_events_with_deterministic_signals(
    proposal: dict,
    conversation: List[dict],
    target_anchor: Optional[str] = None,
    sender_email: Optional[str] = None,
    sender_name: Optional[str] = None,
    contact_name: Optional[str] = None,
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
            e for e in events if (e or {}).get("type") != "wrong_contact"
        ]
        # BUG-A (pressure test): an OOO auto-reply is a machine bounce, not a human
        # response. The model intermittently drafts a reply ("I'll follow up after
        # you're back") — suppress it AND skip the send entirely (not a template
        # fallback) so we never ping the auto-responder (noise / possible loop).
        # Wait for the human's real reply on a later scan.
        proposal["response_email"] = None
        proposal["skip_response"] = True
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
    if _looks_like_requirements_mismatch_nonviable(latest_text):
        property_unavailable_reason = "requirements_mismatch"
    elif not looks_like_tour_only_unavailable(latest_text_raw):
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
        if not any((event or {}).get("type") == "property_unavailable" for event in retained_events):
            retained_events.insert(0, {
                "type": "property_unavailable",
                "reason": property_unavailable_reason,
            })
        proposal["events"] = retained_events
        # FIX-03: a genuine terminal injection must not leave a live response_email
        # (row marked dead while the outbound keeps chatting with the broker).
        proposal["response_email"] = None
        return proposal

    tour_reply_reason = None
    if looks_like_tour_only_unavailable(latest_text_raw):
        if _has_tour_scheduling_context(conversation) or _looks_like_tour_slot_reply(conversation, latest_text):
            tour_reply_reason = "tour_unavailable"
    elif _looks_like_tour_slot_reply(conversation, latest_text):
        tour_reply_reason = "tour_slot_reply"

    if tour_reply_reason:
        # FIX-02: never delete an LLM property_unavailable carrying a substantive
        # (requirements-fit) reason — the tour-slot idiom must not erase a correct
        # non-viable classification.
        def _is_substantive_pu(event: dict) -> bool:
            return (
                (event or {}).get("type") == "property_unavailable"
                and str((event or {}).get("reason") or "").strip() == "requirements_mismatch"
            )

        existing_tour = [e for e in events if (e or {}).get("type") == "tour_requested"]
        proposal["events"] = [
            event for event in events
            if (event or {}).get("type") != "property_unavailable" or _is_substantive_pu(event)
        ]
        if existing_tour:
            # FIX-05: repair a model-emitted tour_requested carrying a wrong reason
            # instead of only appending-when-absent (A′ misread M18).
            if tour_reply_reason == "tour_unavailable":
                for event in proposal["events"]:
                    if (event or {}).get("type") == "tour_requested":
                        event["reason"] = "tour_unavailable"
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
_COMBINED_RENT_OPEX_RE = re.compile(
    r"\$\s*([0-9]{1,3}(?:\.[0-9]{1,2})?)\s*(?:/?\s*(?:psf|sf|sq\.?\s*ft))?\s*(?:nnn|net|gross)?"
    r"\s*\+\s*"
    r"\$\s*([0-9]{1,3}(?:\.[0-9]{1,2})?)\s*(?:/?\s*(?:psf|sf|sq\.?\s*ft))?\s*(?:in\s+)?"
    r"(?:opex|op\s*ex|nnn|cam|net|operating\s+expense)",
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
# A rent keyword immediately preceding a $ figure marks that figure as the RENT
# line. "nnn" is the ONLY lease-basis word in the OpEx label set above, so a
# figure-first hit ending in "nnn" ("Rent $0.82 NNN") is ambiguous: the NNN is
# the rent's triple-net BASIS, not a separate OpEx figure. Guard #19's extractor
# from mistaking such a rent line for OpEx (which would overwrite a valid LLM
# OpEx and land rent+opex on mixed bases before #15 annualizes).
_RENT_KW_BEFORE_FIGURE_RE = re.compile(
    r"(?:asking|base\s+rent|lease\s+rate|rent|rate)\b[^\d$]{0,10}$",
    re.IGNORECASE,
)


def _opex_match_is_rent_basis_line(text: str, m: "re.Match") -> bool:
    """True when a figure-first OpEx hit is really the rent line's NNN basis.

    Only the bare "nnn" label is ambiguous (cam/tmi/opex/operating-expense are
    unambiguous OpEx labels). When such a figure is immediately preceded by a
    rent keyword, it is the asking rent stated on a triple-net basis, so it must
    not be mined as OpEx.
    """
    if m.group(1) is None:
        return False  # keyword-first hit ($ after the label) is a genuine OpEx figure
    if not m.group(0).rstrip().lower().endswith("nnn"):
        return False
    # BUG-B (pressure test): a bare figure-first "$X NNN" is a RENT quote on a
    # triple-net BASIS, not an OpEx figure. Previously it was only skipped when a
    # rent keyword preceded it, so "$9.25 NNN" was mined as OpEx — and "$8.50 NNN
    # with $3.50 opex" returned 8.50 (the rent), clobbering the real $3.50. Treat
    # any bare "$X NNN" as rent-basis; it is OpEx ONLY when the NNN figure is
    # DIRECTLY qualified as an estimate/charge ("$3.50 NNN est"). A separate later
    # "$Y opex" belongs to that figure, so restrict the lookahead to before the
    # next "$".
    directly_after = text[m.end(): m.end() + 12].lower().split("$", 1)[0]
    if re.search(r"\b(?:est|estimate[ds]?|charges?)\b", directly_after):
        return False  # "$X NNN est/charges" -> genuine OpEx estimate
    return True  # bare "$X NNN" -> rent-basis line, do not mine as OpEx
# Total SF as an area (thousands-grouped or 4+ digits), not a $/SF rate figure.
_TOTAL_SF_RE = re.compile(
    r"(?<![\w$/.])((?:\d{1,3}(?:,\d{3})+)|\d{4,})\s*(?:sf|sq\.?\s*ft|square\s*f(?:ee|oo)t)\b",
    re.IGNORECASE,
)
_MONTHLY_UNIT_RE = re.compile(r"(?:/\s*|\bper\s+)(?:mo|mos|month)\b|\bmonthly\b|\bpsf\s*/?\s*mo(?:nth)?\b", re.IGNORECASE)
_ANNUAL_UNIT_RE = re.compile(r"(?:/\s*|\bper\s+)(?:yr|year|annum|annual|annually)\b", re.IGNORECASE)
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
        r"(?:asking|base\s+rent|rent|rate)[^\d$]{0,24}\$?\s*([0-9]{1,3}(?:\.[0-9]{1,2})?)\s*"
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
    for pattern in (rent_context, dollar_per_sf, dollar_rate_basis, dollar_less_basis, cents_basis):
        for match in pattern.finditer(text):
            # rent_context already required an explicit rent keyword, so trust it.
            # The keyword-less patterns must screen out non-rent cost figures
            # (TI allowance/credit, taxes, parking, opex, buildout) in a $/SF shape.
            if pattern is not rent_context:
                # Basis-bearing figures already carry an explicit lease basis, so a
                # trailing opex/tax labels a different figure — screen only the lead.
                check_after = pattern not in basis_patterns
                if _figure_is_non_rent(text, match.start(), match.end(), check_after=check_after):
                    continue
            # Cents figures are expressed in cents/SF; convert to dollars/SF.
            value = float(match.group(1)) / 100.0 if pattern is cents_basis else float(match.group(1))
            unit_context = text[max(0, match.start() - 40): min(len(text), match.end() + 50)]
            is_monthly = bool(monthly_unit.search(unit_context)) and not bool(annual_unit.search(unit_context))
            if pattern in basis_patterns and not is_monthly:
                # A bare per-SF basis rate under ~$3 is a monthly figure (e.g.
                # "$0.82 NNN" -> $9.84/yr); annual industrial rates are far higher.
                is_monthly = value < 3.0
            annual_value = value * 12 if is_monthly else value
            if annual_value < 1:
                continue
            # #19 concession / opex / hypothetical screens for the keyword-less
            # generic patterns (the basis patterns already carry an explicit lease
            # basis and are screened by _figure_is_non_rent above).
            if pattern not in basis_patterns:
                before = text[max(0, match.start() - 30): match.start()].lower()
                if any(marker in before for marker in ("nnn", "cam", "ops", "opex", "operating expense")):
                    continue
                # A TI allowance / concession figure is not the asking rent. The
                # concession word may follow ("$30/SF in TI allowance") or precede it
                # ("TI allowance of $30/SF"). Match-local: truncate each side at the
                # nearest OTHER $ figure so a concession bound to a different figure
                # can't suppress this one ("Asking $20/SF with a $25/SF TI
                # allowance." → 20.00).
                after_ctx = text[match.end(): match.end() + 40].split("$", 1)[0]
                before_ctx = text[max(0, match.start() - 22): match.start()].rsplit("$", 1)[-1]
                if _CONCESSION_MARKER_RE.search(after_ctx) or _CONCESSION_MARKER_RE.search(before_ctx):
                    continue
            # Past-tense hypothetical rent ("rent would've been $16/SF") is not a
            # current asking figure. The conditional phrase often sits INSIDE the
            # match span, so the window must reach match.end().
            if _HYPOTHETICAL_RENT_RE.search(text[max(0, match.start() - 40): match.end()]):
                continue
            return f"{annual_value:.2f}"

    return None


def _extract_ops_ex_sf_from_text(text: str) -> Optional[str]:
    """Deterministic OpEx / NNN / CAM per-SF-per-year fallback (annualized)."""
    if not text:
        return None
    if _looks_like_requirements_mismatch_nonviable(text):
        return None

    # Past-tense hypothetical ("opex would have been $8/sf, but it's leased now")
    # is not a current figure — mirror the rent extractor's match-local guard on
    # every return path so a fabricated OpEx is never written to the sheet.
    combined = _COMBINED_RENT_OPEX_RE.search(text)
    if combined:
        if not _HYPOTHETICAL_RENT_RE.search(text[max(0, combined.start() - 40): combined.end()]):
            opex = float(combined.group(2))
            window = text[max(0, combined.start() - 10): min(len(text), combined.end() + 30)]
            annual = opex * 12 if _is_monthly_context(window) else opex
            if annual >= 0.01:
                return f"{annual:.2f}"

    for m in _OPS_EX_RE.finditer(text):
        if _HYPOTHETICAL_RENT_RE.search(text[max(0, m.start() - 40): m.end()]):
            continue
        # Skip a rent-basis line ("Rent $0.82 NNN") so the rent figure is never
        # mined as OpEx; keep scanning for a genuine OpEx figure downstream.
        if _opex_match_is_rent_basis_line(text, m):
            continue
        raw = m.group(1) or m.group(2)
        if raw is None:
            continue
        val = float(raw)
        window = text[max(0, m.start() - 15): min(len(text), m.end() + 25)]
        annual = val * 12 if _is_monthly_context(window) else val
        if annual >= 0.01:
            return f"{annual:.2f}"
    return None


def _extract_total_sf_from_text(text: str) -> Optional[str]:
    """Deterministic Total SF fallback; tolerates '+/- 9,000 SF' style approximations."""
    if not text:
        return None
    for m in _TOTAL_SF_RE.finditer(text):
        raw = m.group(1).replace(",", "")
        try:
            val = int(raw)
        except ValueError:
            continue
        if val >= 1000:
            return str(val)
    return None


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

    # LIVE break (900 Alt Suggest St): when the reply kills the current row
    # (property_unavailable) and/or pitches an ALTERNATE property (new_property),
    # the specs in the fresh message describe the alternate — mining them into
    # the CURRENT row is a cross-property write ("1100 Fresh Listing Ave is
    # 30,000 SF at $10.50" landed on the dying 900 row). The fallback is
    # best-effort only, so skip it entirely for these proposals; the alternate's
    # specs travel via the new-property approval flow instead.
    event_types = {
        (e or {}).get("type") for e in (proposal.get("events") or [])
    }
    if event_types & {"new_property", "property_unavailable"}:
        return proposal

    mappings = (effective_config or {}).get("mappings", {})
    # Only mine the broker's FRESH message; quoted history must not seed values.
    fresh_text = _fresh_inbound_text(conversation)
    # Flyer/linked-PDF text is legitimate evidence for fields the message body
    # omits ("all the specs are in the attached flyer"). Used for the loading
    # counts below; the rent/SF extractors stay message-scoped because a flyer
    # can carry stale pricing superseded by the email body.
    evidence_texts = [fresh_text] + [t for t in (extra_texts or []) if t]

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
            if str(existing.get("value") or "").strip() != value:
                existing.clear()
                existing.update(update)
            return
        proposal.setdefault("updates", []).append(update)

    rent_value = _extract_rent_sf_yr_from_text(fresh_text)
    if not rent_value:
        # FIX-16 (M35, HEAD): the accept-new-property path passes rent only inside
        # the PDF manifest text (the inbound body is a synthetic stub), so scan the
        # manifest as a LAST resort when the fresh message carries no rent. This does
        # not prefer stale flyer pricing over an email rate — it only fills the gap.
        for _pdf in (pdf_manifest or []):
            rent_value = _extract_rent_sf_yr_from_text((_pdf or {}).get("text") or "")
            if rent_value:
                break
    _fill(
        mappings.get("rent_sf_yr") or _find_header_name(header, "Rent/SF /Yr"),
        rent_value,
        "Deterministic fallback parsed asking rent per SF per year from the latest broker message.",
    )
    _fill(
        mappings.get("ops_ex_sf") or _find_header_name(header, "Ops Ex /SF"),
        _extract_ops_ex_sf_from_text(fresh_text),
        "Deterministic fallback parsed operating expenses per SF per year from the latest broker message.",
    )
    _fill(
        mappings.get("total_sf") or _find_header_name(header, "Total SF"),
        _extract_total_sf_from_text(fresh_text),
        "Deterministic fallback parsed total square footage from the latest broker message.",
    )
    # Loading counts: mined from the fresh message OR flyer/linked-PDF text
    # (LIVE break 600 Flyer Facts Blvd: "1 drive-in ramp" lived only in the
    # flyer PDF and was never written). Explicit numeric counts only — the
    # fabricated-door-count guard philosophy holds: no number, no write.
    drive_ins_col = (
        mappings.get("drive_ins")
        or _find_header_name(header, "Drive Ins")
        or _find_header_name(header, "Drive-Ins")
    )
    docks_col = (
        mappings.get("docks")
        or _find_header_name(header, "Docks")
        or _find_header_name(header, "Loading Docks")
    )
    for text in evidence_texts:
        _fill(
            drive_ins_col,
            _extract_drive_in_count_from_text(text),
            "Deterministic fallback parsed drive-in count from the broker's message or flyer.",
        )
        _fill(
            docks_col,
            _extract_dock_count_from_text(text),
            "Deterministic fallback parsed loading-dock count from the broker's message or flyer.",
        )
    return proposal


# OPEX stated on an explicitly MONTHLY basis, e.g. "opex $0.21/SF/mo".
_OPEX_MONTHLY_RE = re.compile(
    r"(?:opex|ops\s*ex|op\s*ex|operating\s+expenses?|cam|n\.?n\.?n\.?)\b[^\d$]{0,20}\$?\s*"
    r"([0-9]{1,3}(?:\.[0-9]{1,2})?)\s*"
    r"(?:/\s*sf|per\s+sf|psf|/\s*sq\.?\s*ft)?\s*/?\s*(?:mo|mos|month|monthly)\b",
    re.IGNORECASE,
)

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

    text = _latest_inbound_text(conversation) or ""
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

    # BASIS guard — annualize a monthly opex figure to match the annual rent basis.
    monthly_match = _OPEX_MONTHLY_RE.search(text)
    if monthly_match:
        try:
            monthly_val = float(monthly_match.group(1))
            current_val = float(current) if current else None
        except ValueError:
            return proposal
        annual_str = f"{monthly_val * 12:.2f}"
        # Only rewrite when the update is still carrying the monthly figure.
        if current_val is not None and abs(current_val - monthly_val) < 1e-9 and current != annual_str:
            opex_update["value"] = annual_str
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
    if not extraction_fields:
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

        # Read all AI_META data
        resp = _execute_with_retry(
            sheets.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range="AI_META!A:F"
            ),
            "read_ai_meta"
        )

        rows = resp.get("values", [])
        if len(rows) <= 1:  # Only header or empty
            return None
        
        # Find the newest matching row. AI_META is append-only, so older records
        # for the same row/column can exist after retries or row moves.
        for row in reversed(rows[1:]):  # Skip header
            if len(row) >= 2 and str(row[0]) == str(rownum) and row[1].lower() == column.lower():
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
):
    """Append new AI_META record."""
    try:
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
            row_anchor = get_row_anchor(current_rowvals, header)
            return {
                "applied": [],
                "skipped": [{"reason": "no-updates"}],
                "rowNumber": rownum,
                "targetAnchor": row_anchor,
                "rowSnapshotBefore": _build_row_snapshot(header, current_rowvals),
                "rowSnapshotAfter": _build_row_snapshot(header, current_rowvals),
            }

        idx_map = _header_index_map(header)
        row_anchor = get_row_anchor(current_rowvals, header)
        row_snapshot_before = _build_row_snapshot(header, current_rowvals)
        row_after = list(current_rowvals or [])
        if len(row_after) < len(header or []):
            row_after.extend([""] * (len(header or []) - len(row_after)))

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

            # Skip formula columns (e.g. Gross Rent) - computed on the sheet; writing a raw
            # value clobbers the live formula cell. Deterministic code guard, not prompt-only.
            if _is_formula_column(col_name):
                skipped.append({"column": col_name, "reason": "formula-column"})
                continue

            # Skip Flyer/Floorplan columns - these are handled directly via Drive upload
            # AI sometimes proposes local file:// paths from PDF metadata which we don't want
            if key in ("flyer / link", "floorplan", "flyer/link", "flyer"):
                skipped.append({"column": col_name, "reason": "handled-by-drive-upload"})
                continue

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

            # 1) no-op
            if (old_val or "") == (new_val or ""):
                skipped.append({"column": col_name, "reason": "no-change"})
                continue

            # Check AI_META for write guards
            meta = _read_ai_meta_row(sheets, sheet_id, rownum, col_name, row_anchor=row_anchor)

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

        # Update AI_META for each applied change
        for a in applied:
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
            _append_ai_meta(
                sheets,
                sheet_id,
                rownum,
                a["column"],
                a["newValue"],
                override=False,
                row_anchor=row_anchor,
            )

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
                          extraction_fields: List[str] = None,  # Optional: list of canonical field keys user wants extracted
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
        extraction_fields: Optional list of canonical field keys (e.g., ["total_sf", "ops_ex_sf"]) that the user
                          wants extracted. If provided, only these fields will be included in extraction rules.
                          If None, all extractable fields are used.
        dry_run: If True, skips the sheetChangeLog Firestore write. OpenAI usage
                 metering still writes because the model call is still billed.
    """
    try:
        # Hard OpenAI budget stop (flag-gated: ENFORCE_OPENAI_BUDGET, default OFF).
        # When enforcement is ON and global current-month spend has reached
        # USAGE_MONTHLY_BUDGET_USD, DEFER this turn with an explicit exception so
        # every caller leaves the source message visible/retryable. dry_run still
        # bills the model, so this guards that path too. No-op when the flag is off.
        if should_block_openai_call(_fs):
            message = (
                "OpenAI monthly budget reached; extraction deferred until the "
                "budget is raised or the next monthly window begins"
            )
            print(f"⛔ {message}")
            raise BudgetDeferredError(message)
        # Build conversation payload (chronological; latest last)
        # If conversation is provided directly (e.g., from tests), use it; otherwise fetch from Firestore
        if conversation is None:
            # Pass headers to fetch from Graph API (includes manual emails we didn't index)
            conversation = build_conversation_payload(uid, thread_id, limit=10, headers=headers)

        # ---- Rules sections ---------------------------------------------------
        # Use dynamic column config if provided, otherwise use defaults
        effective_config = column_config or get_default_column_config()

        # If extraction_fields is specified, filter the config to only include those fields
        if extraction_fields:
            effective_config = _filter_config_by_extraction_fields(effective_config, extraction_fields)

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

- "property_unavailable": Emit when the CURRENT TARGET PROPERTY is explicitly stated as unavailable/leased/off-market/no longer available OR when the broker clearly says the property is non-viable for the client's requirements.
  • Treat requirements-fit failures as non-viable when the broker says the space/property is not a good fit because it is office-heavy, not a true warehouse, lacks drive-in/grade-level access, lacks required industrial use, or otherwise fails the requested physical requirements.
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
   - NEVER request "Rent/SF /Yr" - this field should never be asked for
   - NEVER request "Gross Rent" - this is a formula column that calculates automatically
   - For "Flyer / Link", phrase it naturally: "flyer", "brochure", "marketing materials", or "property flyer"
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

        # PDF attachments - include extracted text directly in prompt
        if pdf_manifest:
            prompt_parts.append("\n\n=== PDF ATTACHMENTS ===")
            for pdf in pdf_manifest:
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
      "reason": "<for needs_user_input: client_question | negotiation | confidential | legal_contract | unclear> OR <for contact_optout: not_interested | unsubscribe | do_not_contact | no_tenant_reps | direct_only | hostile> OR <for wrong_contact: no_longer_handles | wrong_person | forwarded | left_company>",
      "question": "<for needs_user_input: the specific question/request that needs user attention>",
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

                # Add file_id as fallback if we have it and extraction was poor.
                file_id = pdf.get("file_id") or pdf.get("id")
                if file_id and pdf.get("method") in ("openai_upload", "openai_upload+images", "failed"):
                    input_content.append({"type": "input_file", "file_id": file_id})

        input_content.append({"type": "input_text", "text": prompt})

        # ---- Call OpenAI (low temperature for determinism) --------------------
        response = client.responses.create(
            model="gpt-5.2",  # GPT-5.2 Thinking for complex extraction
            input=[{"role": "user", "content": input_content}],
            temperature=0.1
        )
        # The OpenAI call above ALWAYS bills, even under dry_run (dry_run only
        # skips the sheetChangeLog Firestore write below, not the paid API call).
        # Meter unconditionally so dry-run spend (e.g. the app.py new-property
        # extraction path) is captured.
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
                "hasPdfManifest": bool(pdf_manifest),
                "pdfCount": len(pdf_manifest or []),
                "urlTextCount": len(url_texts or []),
                "configuredExtractionFieldCount": len(extraction_fields or []),
                "dryRun": bool(dry_run),
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
            (pdf or {}).get("text") or "" for pdf in (pdf_manifest or [])
        ] + [
            (u or {}).get("text") or "" for u in (url_texts or [])
        ]
        proposal = _augment_proposal_with_deterministic_extractions(
            proposal, rowvals, header, effective_config, conversation,
            pdf_manifest=pdf_manifest, extra_texts=_evidence_extra_texts,
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
        )
        # A genuine contact opt-out must never write the opted-out row (LIVE break
        # adv_optout_with_specs). Runs AFTER the event augmenter so the engaged-
        # alternative guard has already dropped any scoped over-fired opt-out.
        proposal = _suppress_updates_on_contact_optout(proposal)
        proposal = sanitize_new_property_referral_response(
            proposal,
            original_contact_email=email,
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
                "pdfManifest": [{k: v for k, v in p.items() if k != 'images'} for p in (pdf_manifest or [])],  # exclude images from log
                "fileIds": [p["id"] for p in (pdf_manifest or []) if p.get("id")],  # keep old field for compatibility
                "urlTexts": url_texts or [],
                "createdAt": SERVER_TIMESTAMP
            })
            print(f"💾 Stored proposal in sheetChangeLog/{log_doc_id}")
        else:
            print(f"🧪 Dry run - skipped Firestore logging")

        return proposal

    except BudgetDeferredError:
        raise
    except Exception as e:
        print(f"❌ Failed to propose sheet updates: {e}")
        return None
