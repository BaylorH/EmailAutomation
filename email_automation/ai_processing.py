import json
import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from google.cloud.firestore import SERVER_TIMESTAMP
from .clients import client, _sheets_client, _fs
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
    height_mismatch = bool(
        re.search(height_term + r"[^.]{0,45}?\b" + below_term + r"\b", latest_text)
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


def _looks_like_out_of_office(text: str) -> bool:
    """A temporary-absence auto/hand-typed reply (OOO with return + assistant) is
    NOT a wrong_contact redirect (A′ misread M08)."""
    blob = (text or "").lower()
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


def _detect_target_terminal_reason(latest_text: str, target_anchor: Optional[str]) -> Optional[str]:
    """Return a terminal reason ONLY when a terminal phrase binds to the TARGET
    property — negation-aware and target-grounded (A′ FIX-01, CodeRabbit PR#15).

    A terminal phrase is ignored when it is negated, bound to an ancillary asset /
    tour slot, or attributed to a DIFFERENT named address than the target. A bare
    terminal (no address in its sentence) is deferred when the message elsewhere
    asserts the target remains viable.
    """
    text = (latest_text or "").lower()
    target_numbers = set(re.findall(r"\b(\d{3,6})\b", (target_anchor or "").lower()))
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
            sentence_numbers = set(re.findall(r"\b(\d{3,6})\b", sentence))
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
    latest_text_raw = _latest_inbound_text(conversation)
    latest_text = latest_text_raw.lower()
    if not latest_text:
        return proposal

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

    property_unavailable_reason = None
    if not looks_like_tour_only_unavailable(latest_text_raw):
        if _looks_like_requirements_mismatch_nonviable(latest_text):
            property_unavailable_reason = "requirements_mismatch"
        else:
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


def _extract_rent_sf_yr_from_text(text: str) -> Optional[str]:
    """Best-effort deterministic fallback for common asking-rent phrases.

    Captures a broker-stated asking rate expressed either with an explicit /SF
    token or with a bare rate basis suffix ('$9.75 gross', '$0.82 NNN'). Never
    treats a non-rent $/SF figure (TI allowance, taxes, parking, opex, buildout)
    as the asking rent.
    """
    if not text:
        return None

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
        r"\$\s*([0-9]{1,3}(?:\.[0-9]{1,2})?)\s*"
        r"(?:gross|nnn|net|modified\s+gross|full\s+service|fsg|ig|industrial\s+gross|mg)\b",
        re.IGNORECASE,
    )
    # Dollar-SIGN-LESS rate with an explicit lease basis, e.g. "8.75 nnn",
    # "8.75 a foot nnn" (A′ misread M33 — terse broker shorthand). A decimal is
    # required to keep this conservative; an optional psf/per-foot token may sit
    # between the figure and the basis word.
    dollar_less_basis = re.compile(
        r"(?<![$\d])([0-9]{1,2}\.[0-9]{2})\s*"
        r"(?:p\.?s\.?f\.?|per\s+(?:sq\.?\s*)?f(?:oo)?t|a\s+(?:sq\.?\s*)?f(?:oo)?t|/\s*sf|per\s+sf)?\s*"
        r"(?:gross|nnn|net|modified\s+gross|full\s+service|fsg|ig|industrial\s+gross|mg)\b",
        re.IGNORECASE,
    )
    monthly_unit = re.compile(r"(?:/|\bper\s+)(?:mo|mos|month|monthly)\b|\bmonthly\b", re.IGNORECASE)
    annual_unit = re.compile(r"(?:/|\bper\s+)(?:yr|year|annum|annual|annually)\b", re.IGNORECASE)

    for pattern in (rent_context, dollar_per_sf, dollar_rate_basis, dollar_less_basis):
        for match in pattern.finditer(text):
            # rent_context already required an explicit rent keyword, so trust it.
            # The keyword-less patterns must screen out non-rent cost figures
            # (TI allowance/credit, taxes, parking, opex, buildout) in a $/SF shape.
            if pattern is not rent_context:
                # Basis-bearing figures already carry an explicit lease basis, so a
                # trailing opex/tax labels a different figure — screen only the lead.
                check_after = pattern not in (dollar_rate_basis, dollar_less_basis)
                if _figure_is_non_rent(text, match.start(), match.end(), check_after=check_after):
                    continue
            value = float(match.group(1))
            unit_context = text[max(0, match.start() - 40): min(len(text), match.end() + 50)]
            is_monthly = bool(monthly_unit.search(unit_context)) and not bool(annual_unit.search(unit_context))
            if pattern in (dollar_rate_basis, dollar_less_basis) and not is_monthly:
                # A bare per-SF basis rate under ~$3 is a monthly figure (e.g.
                # "$0.82 NNN" -> $9.84/yr); annual industrial rates are far higher.
                is_monthly = value < 3.0
            annual_value = value * 12 if is_monthly else value
            if annual_value < 1:
                continue
            return f"{annual_value:.2f}"

    return None


def _augment_proposal_with_deterministic_extractions(
    proposal: dict,
    rowvals: List[str],
    header: List[str],
    effective_config: dict,
    conversation: List[dict],
    pdf_manifest: List[dict] = None,
) -> dict:
    """Add high-confidence values from simple broker text patterns the LLM missed."""
    if not proposal:
        return proposal

    mappings = (effective_config or {}).get("mappings", {})
    rent_col = mappings.get("rent_sf_yr") or _find_header_name(header, "Rent/SF /Yr")
    if not rent_col or not _find_header_name(header, rent_col):
        return proposal

    if (_row_value_for_column(rowvals, header, rent_col) or "").strip():
        return proposal

    rent_value = _extract_rent_sf_yr_from_text(_latest_inbound_text(conversation))
    if not rent_value:
        # FIX-16 (M35): the accept-new-property path passes rent only inside the
        # PDF manifest text (the inbound body is a synthetic stub), so scan those.
        for pdf in (pdf_manifest or []):
            rent_value = _extract_rent_sf_yr_from_text(pdf.get("text") or "")
            if rent_value:
                break
    if not rent_value:
        return proposal

    deterministic_update = {
        "column": rent_col,
        "value": rent_value,
        "confidence": 0.92,
        "reason": "Deterministic fallback parsed asking rent per SF per year from the latest broker message.",
    }
    # FIX-15: the TI-credit / rent-context guards above ensure the parsed value is
    # the asking rent, so correcting a monthly-misread LLM figure is safe; but the
    # deterministic parse is only trusted to REPLACE a differing LLM value, never to
    # invent one when the model already agreed.
    existing_update = _proposal_update_for_column(proposal, rent_col)
    if existing_update:
        if str(existing_update.get("value") or "").strip() != rent_value:
            existing_update.clear()
            existing_update.update(deterministic_update)
        return proposal

    proposal.setdefault("updates", []).append(deterministic_update)
    return proposal

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


def check_missing_required_fields(rowvals: List[str], header: List[str], column_config: dict = None) -> List[str]:
    """
    Check which required fields are missing from the row.
    Uses dynamic column config if provided, otherwise falls back to defaults.
    """
    try:
        idx_map = _header_index_map(header)
        missing = []

        # Get required fields from config or use defaults
        if column_config:
            required_fields = get_required_fields_for_close(column_config)
        else:
            required_fields = REQUIRED_FIELDS_FOR_CLOSE

        for field in required_fields:
            key = field.strip().lower()
            if key in idx_map:
                i = idx_map[key] - 1  # 0-based
                cell = (rowvals[i] or "").strip() if i < len(rowvals) else ""
                # A placeholder ('TBD', 'pending', '?', 'ask landlord', ...) is not a
                # real spec value — treat it as missing so the row cannot close on it.
                if i >= len(rowvals) or not cell or cell.lower() in _PLACEHOLDER_CELL_VALUES:
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

        # Combine existing and new notes
        if existing_comments:
            # Append with separator if there's existing content
            combined = f"{existing_comments} • {notes}"
        else:
            combined = notes

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
            # This is the same leak class the outbound-email path already blocks
            # via outbound_safety.find_unresolved_placeholders - never write a
            # literal placeholder into a client sheet cell.
            if find_unresolved_placeholders(new_val):
                skipped.append({"column": col_name, "reason": "placeholder-value"})
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
        dry_run: If True, skips Firestore logging (useful for testing).
    """
    try:
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
                    # Include extracted text (truncate if too long)
                    if len(text) > 8000:
                        prompt_parts.append(text[:8000] + "\n... [text truncated] ...")
                    else:
                        prompt_parts.append(text)
                else:
                    prompt_parts.append("[No text extracted - see images below if available]")

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
                    "hasPdfManifest": bool(pdf_manifest),
                    "pdfCount": len(pdf_manifest or []),
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
        proposal = _augment_proposal_with_deterministic_extractions(
            proposal, rowvals, header, effective_config, conversation, pdf_manifest=pdf_manifest
        )
        proposal = _augment_events_with_deterministic_signals(
            proposal,
            conversation,
            target_anchor=target_anchor,
            sender_email=sender_email,
            sender_name=sender_display_name,
            contact_name=contact_name,
        )
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

    except Exception as e:
        print(f"❌ Failed to propose sheet updates: {e}")
        return None
