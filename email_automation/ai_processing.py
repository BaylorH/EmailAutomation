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


def _latest_inbound_text(conversation: List[dict]) -> str:
    for message in reversed(conversation or []):
        if (message.get("direction") or "").lower() == "inbound":
            return message.get("content") or message.get("body") or message.get("preview") or ""
    return ""


def _looks_like_requirements_mismatch_nonviable(text: str) -> bool:
    """Detect broker replies saying the property fails the client's physical requirements."""
    latest_text = (text or "").lower()
    if not latest_text:
        return False

    fit_rejection = bool(
        re.search(
            r"\b(?:this\s+)?(?:space|property|building|suite)?\s*"
            r"(?:wouldn[’']t|would\s+not|won[’']t|will\s+not|isn[’']t|is\s+not|"
            r"doesn[’']t|does\s+not)\s+(?:be\s+)?(?:a\s+)?(?:good\s+)?fit\b",
            latest_text,
        )
        or re.search(r"\bnot\s+(?:a\s+)?(?:good\s+)?fit\s+for\s+(?:your|the)\s+client\b", latest_text)
        or re.search(r"\bnot\s+(?:the\s+)?right\s+fit\s+for\s+(?:your|the)\s+client\b", latest_text)
        or re.search(r"\bfails?\s+(?:your\s+|the\s+)?client(?:'s)?\s+requirements\b", latest_text)
        or re.search(r"\bdoes\s+not\s+(?:meet|satisfy)\s+(?:your\s+|the\s+)?client(?:'s)?\s+requirements\b", latest_text)
        or re.search(r"\bdoesn[’']t\s+(?:meet|satisfy)\s+(?:your\s+|the\s+)?client(?:'s)?\s+requirements\b", latest_text)
    )
    property_context = bool(re.search(r"\b(?:space|property|building|suite|warehouse|client)\b", latest_text))
    physical_mismatches = [
        re.search(r"\b(?:too|more|mostly|primarily)\s+office[-\s]?heavy\b", latest_text),
        re.search(r"\b(?:too|more|mostly|primarily)\s+office\b", latest_text),
        re.search(r"\boffice[-\s]?heavy\s+as\s+opposed\s+to\s+(?:a\s+)?(?:true\s+)?warehouse\b", latest_text),
        re.search(r"\bnot\s+(?:a\s+)?true\s+warehouse\b", latest_text),
        re.search(r"\blacks?\s+(?:enough\s+|sufficient\s+)?(?:warehouse|industrial|industrial\s+warehouse)\s+(?:space|area)?\b", latest_text),
        re.search(r"\b(?:no|without|lacks?|doesn[’']t\s+have|does\s+not\s+have)\s+(?:any\s+)?(?:drive[-\s]?in|grade[-\s]?level)\s+(?:doors?|space|access)?\b", latest_text),
        re.search(r"\bnot\s+(?:enough|sufficient)\s+(?:warehouse|industrial|drive[-\s]?in)\b", latest_text),
    ]
    mismatch_count = sum(1 for match in physical_mismatches if match)

    return bool(
        (fit_rejection and (property_context or mismatch_count > 0))
        or mismatch_count >= 2
    )


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

    reply_signal = re.search(
        r"\b(?:that\s+time|that\s+slot|the\s+slot|requested\s+time|works?|confirmed|"
        r"does\s+not\s+work|doesn[’']t\s+work|can't\s+do|cannot\s+do|won[’']t\s+work|"
        r"could\s+do|available\s+(?:at|around|after|before)|instead|works\s+better|"
        r"see\s+you|no\s+longer\s+available)\b",
        latest,
    )
    time_signal = re.search(r"\b(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)|morning|afternoon|noon)\b", latest)
    return bool(reply_signal or time_signal)


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
    r"|\bhop\s+on\s+a(?:\s+\w+){0,3}\s+call\b|\bcan\s+(?:you|we)\s+(?:call|talk)\b"
    r"|\bcall\s+me\s+at\b|\breach\s+me\s+at\b|\bschedule\s+a\s+call\b"
    r"|\blet'?s\s+(?:talk|chat|call)\b|\blet'?s\s+hop\s+on\b|\bset\s+up\s+a\s+call\b",
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
    r"|\blimited\s+(?:email\s+)?access\b"
    r"|\blimited\s+access\s+to\s+(?:my\s+)?email\b"
    r"|\baway\s+from\s+(?:my\s+)?(?:email|office|desk)\b"
    r"|\bback\s+in\s+the\s+office\b"
    r"|\breturn(?:ing)?\s+to\s+the\s+office\b",
    re.IGNORECASE,
)


def _looks_like_out_of_office(text: str) -> bool:
    """High-precision detector for out-of-office / auto-reply messages.

    OOO auto-replies routinely list a backup or assistant address ("for urgent
    matters, contact X", "please contact my assistant X"). The LLM classifier
    intermittently reads that backup address as a wrong_contact handoff and
    escalates the WRONG person. An auto-reply is not an intentional human
    handoff, so callers use this to strip the false wrong_contact deterministically.
    """
    return bool(_OUT_OF_OFFICE_RE.search(text or ""))


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
    """Latest inbound text with any quoted prior-thread history stripped off."""
    fresh, _ = _split_fresh_and_quoted(_latest_inbound_text(conversation))
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
    fresh, quoted = _split_fresh_and_quoted(_latest_inbound_text(conversation))
    if not quoted.strip():
        return proposal
    fresh_lower = fresh.lower()
    quoted_lower = quoted.lower()
    proposal["events"] = [
        event for event in events
        if not _event_is_quote_only(event, fresh_lower, quoted_lower)
    ]
    return proposal


def _augment_events_with_deterministic_signals(proposal: dict, conversation: List[dict]) -> dict:
    """Add high-confidence event signals from broker phrases the model can miss."""
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

    unavailable_patterns = [
        ("no_longer_available", r"\bno\s+longer\s+available\b"),
        ("signed_loi", r"\bsigned\s+(?:an?\s+)?(?:loi|letter\s+of\s+intent)\b"),
        ("signed_lease", r"\bsigned\s+(?:a\s+)?lease\b"),
        ("no_longer_represented", r"\bno\s+longer\s+represent(?:s|ed|ing)?\s+(?:this\s+|the\s+)?property\b"),
        ("no_space_available", r"\b(?:no|not\s+any|do(?:es)?\s+not\s+have\s+any)\s+space\s+available\b"),
        ("no_availability", r"\bno\s+availability\b"),
        ("fully_leased", r"\bfully\s+leased\b"),
    ]

    property_unavailable_reason = None
    if not looks_like_tour_only_unavailable(latest_text_raw):
        if _looks_like_requirements_mismatch_nonviable(latest_text):
            property_unavailable_reason = "requirements_mismatch"
        else:
            for reason, pattern in unavailable_patterns:
                if re.search(pattern, latest_text):
                    property_unavailable_reason = reason
                    break

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
        return proposal

    tour_reply_reason = None
    if looks_like_tour_only_unavailable(latest_text_raw):
        if _has_tour_scheduling_context(conversation) or _looks_like_tour_slot_reply(conversation, latest_text):
            tour_reply_reason = "tour_unavailable"
    elif _looks_like_tour_slot_reply(conversation, latest_text):
        tour_reply_reason = "tour_slot_reply"

    if tour_reply_reason:
        proposal["events"] = [
            event for event in events
            if (event or {}).get("type") != "property_unavailable"
        ]
        if not any((event or {}).get("type") == "tour_requested" for event in proposal["events"]):
            proposal["events"].append({
                "type": "tour_requested",
                "reason": tour_reply_reason,
                "question": latest_text_raw[:500],
                "suggestedEmail": "",
            })
        return proposal

    if any((event or {}).get("type") == "property_unavailable" for event in events):
        return proposal

    return proposal


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
_CONCESSION_MARKER_RE = re.compile(
    r"\b(?:allowance|concession|abatement|free\s+rent|tenant\s+improvement)\b",
    re.IGNORECASE,
)


def _is_monthly_context(window: str) -> bool:
    return bool(_MONTHLY_UNIT_RE.search(window)) and not bool(_ANNUAL_UNIT_RE.search(window))


def _extract_rent_sf_yr_from_text(text: str) -> Optional[str]:
    """Best-effort deterministic fallback for common asking-rent phrases.

    Returns annualized $/SF/yr as a 2-decimal string, or None. Refuses to guess a
    rent when the broker has ruled the property non-viable on physical
    requirements or is only floating a past-tense hypothetical, and never mistakes
    an OpEx / NNN figure for the base rent.
    """
    if not text:
        return None

    # Broker just called the property a non-fit — do not mine a rent from it.
    if _looks_like_requirements_mismatch_nonviable(text):
        return None

    # 1) Combined "base + opex" line — the base rent is the FIRST figure.
    combined = _COMBINED_RENT_OPEX_RE.search(text)
    if combined:
        base = float(combined.group(1))
        window = text[max(0, combined.start() - 20): min(len(text), combined.end() + 30)]
        annual = base * 12 if _is_monthly_context(window) else base
        if annual >= 1:
            return f"{annual:.2f}"

    # 2) Range — take the low end as a conservative asking rent.
    rng = _RENT_RANGE_RE.search(text)
    if rng:
        low = float(rng.group(1))
        window = text[max(0, rng.start() - 20): min(len(text), rng.end() + 40)]
        annual = low * 12 if _is_monthly_context(window) else low
        if annual >= 1:
            return f"{annual:.2f}"

    # 3) Recency / "now" preference — a current asking rate ("...it is now $26/SF")
    # supersedes a stale prior quote on the same line. The generic loop below is
    # first-match, so without this the superseded figure wins; scan for the latest
    # recency-marked figure and prefer it.
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

    # 4) Generic asking-rent patterns.
    for pattern in (_RENT_CONTEXT_RE, _DOLLAR_PER_SF_RE):
        for match in pattern.finditer(text):
            value = float(match.group(1))
            unit_context = text[max(0, match.start() - 40): min(len(text), match.end() + 50)]
            annual_value = value * 12 if _is_monthly_context(unit_context) else value
            if annual_value < 1:
                continue
            before = text[max(0, match.start() - 30): match.start()].lower()
            if any(marker in before for marker in ("nnn", "cam", "ops", "opex", "operating expense")):
                continue
            # A TI allowance / concession figure is not the asking rent. The
            # concession word may follow the figure ("$30/SF in TI allowance") or
            # precede it ("TI allowance of $30/SF"). Keep the check MATCH-LOCAL:
            # truncate each side at the nearest OTHER $ figure so a concession word
            # bound to a different figure can't suppress this one. That preserves a
            # real asking rate quoted alongside a give-back
            # ("Asking $20/SF with a $25/SF TI allowance." → 20.00).
            after_ctx = text[match.end(): match.end() + 40].split("$", 1)[0]
            before_ctx = text[max(0, match.start() - 22): match.start()].rsplit("$", 1)[-1]
            if _CONCESSION_MARKER_RE.search(after_ctx) or _CONCESSION_MARKER_RE.search(before_ctx):
                continue
            # Past-tense hypothetical rent ("rent would've been $16/SF") is not a
            # current asking figure.
            # The conditional phrase ("rent would have been ...") often sits INSIDE
            # the match span — between the rent keyword and the $ figure — so the
            # window must reach match.end(), not stop at match.start().
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

    combined = _COMBINED_RENT_OPEX_RE.search(text)
    if combined:
        opex = float(combined.group(2))
        window = text[max(0, combined.start() - 10): min(len(text), combined.end() + 30)]
        annual = opex * 12 if _is_monthly_context(window) else opex
        if annual >= 0.01:
            return f"{annual:.2f}"

    m = _OPS_EX_RE.search(text)
    if m:
        raw = m.group(1) or m.group(2)
        if raw is not None:
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
) -> dict:
    """Add high-confidence values from simple broker text patterns the LLM missed."""
    if not proposal:
        return proposal

    mappings = (effective_config or {}).get("mappings", {})
    # Only mine the broker's FRESH message; quoted history must not seed values.
    fresh_text = _fresh_inbound_text(conversation)

    def _fill(col_name: Optional[str], value: Optional[str], reason: str) -> None:
        if not value or not col_name or not _find_header_name(header, col_name):
            return
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

    _fill(
        mappings.get("rent_sf_yr") or _find_header_name(header, "Rent/SF /Yr"),
        _extract_rent_sf_yr_from_text(fresh_text),
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
    return proposal


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


def _suppress_fabricated_door_counts(
    proposal: dict,
    conversation: List[dict],
    header: List[str],
    effective_config: dict,
) -> dict:
    """Drop invented Drive Ins / Docks counts when the broker stated no number."""
    if not proposal:
        return proposal
    updates = proposal.get("updates") or []
    if not updates:
        return proposal
    mappings = (effective_config or {}).get("mappings", {})
    fresh = _fresh_inbound_text(conversation)
    checks = [
        (mappings.get("drive_ins") or _find_header_name(header, "Drive Ins"), _DRIVE_IN_KW),
        (mappings.get("docks") or _find_header_name(header, "Docks"), _DOCK_KW),
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
                if i >= len(rowvals) or not (rowvals[i] or "").strip():
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

  Include "reason" field explaining WHY user input is needed:
  • "client_question" - broker asking about client's requirements (e.g., "what size does your client need?", "what's your budget?")
  • "negotiation" - price or term negotiation
  • "confidential" - asking for client identity/info
  • "legal_contract" - contract/LOI/lease questions
  • "unclear" - message is confusing or unclear

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
        
        # Build contact name context with explicit first name for AI to use in greetings
        contact_context = ""
        if contact_name:
            first_name = contact_name.split()[0] if contact_name else None
            contact_context = f"\nCONTACT NAME: {contact_name}\nFIRST NAME FOR GREETINGS: {first_name} (use this in greetings like 'Hi {first_name},')"
        
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

CONVERSATION HISTORY (latest last):
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
      "reason": "<for needs_user_input: client_question | scheduling | negotiation | confidential | legal_contract | unclear> OR <for contact_optout: not_interested | unsubscribe | do_not_contact | no_tenant_reps | direct_only | hostile> OR <for wrong_contact: no_longer_handles | wrong_person | forwarded | left_company>",
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
            proposal, rowvals, header, effective_config, conversation
        )
        proposal = _suppress_fabricated_door_counts(
            proposal, conversation, header, effective_config
        )
        proposal = _augment_proposal_with_flyer_link(
            proposal, url_texts, rowvals, header, effective_config
        )
        # Strip events that only fire off quoted prior-thread history BEFORE the
        # deterministic event augmenter re-evaluates the fresh message.
        proposal = _suppress_quote_only_events(proposal, conversation)
        proposal = _augment_events_with_deterministic_signals(proposal, conversation)
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
