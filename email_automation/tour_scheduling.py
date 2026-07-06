import re
from datetime import datetime
from typing import Any, Dict, List, Optional


DEFAULT_TOUR_DURATION_MINUTES = 30
DEFAULT_BUFFER_MINUTES = 5
TOUR_DAY_START_MINUTES = 8 * 60
TOUR_DAY_END_MINUTES = 17 * 60


def parse_tour_time_minutes(value) -> Optional[int]:
    text = re.sub(r"[\s.]+", "", str(value or "").strip().lower())
    if text == "noon":
        return 12 * 60

    match = re.fullmatch(r"0?(\d{1,2})(?::?(\d{2}))?(am|pm)", text)
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    if hour < 1 or hour > 12 or minute > 59:
        return None

    if match.group(3) == "pm" and hour != 12:
        hour += 12
    if match.group(3) == "am" and hour == 12:
        hour = 0
    return hour * 60 + minute


def format_tour_time(minutes) -> str:
    total = int(minutes) % (24 * 60)
    hour_24 = total // 60
    minute = total % 60
    suffix = "AM" if hour_24 < 12 else "PM"
    hour_12 = hour_24 % 12 or 12
    return f"{hour_12}:{minute:02d} {suffix}"


def format_tour_date_label(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            parsed = datetime.strptime(text, fmt)
            return f"{parsed:%A}, {parsed:%B} {parsed.day}, {parsed.year}"
        except ValueError:
            continue
    return text


# A tours/showings subject: the nouns plus the verb "show" ("cannot show it").
_TOUR_NOUN = r"(?:tours?|showings?|walk[-\s]?throughs?|walkthroughs?)"
# Slot-scoped nouns (A′ FIX-06 / M20): brokers say a *slot/window/time/appointment*
# "is no longer available" to decline one tour time — that is tour-scoped, never a
# property terminal. Treat them as tour subjects so the guard reads them correctly.
_TOUR_SLOT_NOUN = r"(?:time\s*slots?|slots?|windows?|times?|appointments?)"
_TOUR_SUBJECT = rf"(?:{_TOUR_NOUN}|{_TOUR_SLOT_NOUN}|show(?:ing|n|s|ed)?)"

# Negations that scope a *tours-only* restriction. Bare "no" ("no tours"),
# contractions ("won't"/"aren't"/"isn't"/"can't"), and the verb-first forms all
# count — brokers phrase the same restriction many ways.
_TOUR_NEGATION = (
    r"(?:no\s+longer|not\s+able|not|no|unavailable|cannot|can\s*not|can't|cant|"
    r"won't|wont|will\s+not|aren't|arent|isn't|isnt|couldn't|couldnt|unable)"
)

# Post-subject phrases that read as "tours are off" ("suspended", contraction+available).
_TOUR_UNAVAIL_PHRASE = (
    r"(?:no\s+longer\s+available|not\s+available|unavailable|cancelled|canceled|"
    r"not\s+being\s+offered|suspended|aren't\s+available|arent\s+available|"
    r"isn't\s+available|isnt\s+available|won't\s+be\s+available)"
)

# Property-level terminal signals: if any of these appear (and they are NOT scoped
# to a tour/slot) the message is about the PROPERTY being gone, not merely tours —
# it must never be treated as tours-only.
#
# CodeRabbit PR#15: this list previously drifted behind ai_processing's terminal
# taxonomy (e.g. "no longer available", bare "leased", "no availability" were
# missing), so a dead-property reply that also mentioned tours slipped through as
# tour-only and skipped the property_unavailable path. We now bind to the ONE
# canonical list — ai_processing._UNAVAILABLE_PATTERNS — imported lazily to dodge
# the tour_scheduling <-> ai_processing circular import at module load. The literal
# fallback below is used only if that import is unavailable (keeps this guard
# importable standalone); keep it in sync with the canonical list.
_PROPERTY_TERMINAL_FALLBACK = [
    r"\bno\s+longer\s+availab(?:le|e)\b",
    r"\bsigned\s+(?:an?\s+)?(?:loi|letter\s+of\s+intent)\b",
    r"\bsigned\s+(?:a\s+)?lease\b",
    r"\bno\s+longer\s+represent(?:s|ed|ing)?\s+(?:this\s+|the\s+)?property\b",
    r"\b(?:no|not\s+any|do(?:es)?\s+not\s+have\s+any)\s+space\s+available\b",
    r"\bno\s+availability\b",
    r"\bfully\s+leased\b",
    r"\bjust\s+leased\b",
    r"\balready\s+leased\b",
    r"\bbeen\s+leased\b",
    r"\btaken\s+off\s+(?:the\s+)?market\b",
    r"\boff\s+(?:the\s+)?market\b",
    r"\bunder\s+contract\b",
    r"\baccepted\s+an?\s+offer\b",
    r"\bleased\b",
]

# A terminal phrase is *tour-scoped* (not a property terminal) when a tour/slot
# subject is its grammatical subject just before it ("that window is no longer
# available", "tours are cancelled") OR the availability is scoped to touring just
# after it ("available for tours", "availability to show"). Both readings keep the
# property alive, so they must not trip the property early-out (M20, and the
# existing 'no longer available for tours' near-miss).
_TOUR_SCOPE_PRE_RE = re.compile(rf"{_TOUR_SUBJECT}\b[^.!?]{{0,18}}$")
_TOUR_SCOPE_POST_RE = re.compile(
    rf"^\s*[,;-]*\s*(?:for|to)\s+(?:a\s+|any\s+|the\s+|another\s+)?{_TOUR_SUBJECT}\b"
)


_CANONICAL_IMPORT_WARNED = False
_CANONICAL_PATTERNS_CACHE: Optional[List[str]] = None


def _canonical_terminal_patterns() -> List[str]:
    """The single canonical property-terminal regex list (CodeRabbit PR#15).

    Imported lazily from ai_processing so the two surfaces never drift; falls back
    to the literal copy above ONLY if that module can't be imported. We catch
    ImportError specifically (not bare Exception) so a genuine bug inside
    ai_processing surfaces loudly instead of silently reintroducing the list drift
    this bridge exists to eliminate (CodeRabbit PR#15).

    The successful import is memoized (this runs on the hot inbound-email path,
    multiple times per email); the fallback path is intentionally left uncached so
    a transient import failure can't permanently pin the drift-prone fallback."""
    global _CANONICAL_IMPORT_WARNED, _CANONICAL_PATTERNS_CACHE
    if _CANONICAL_PATTERNS_CACHE is not None:
        return _CANONICAL_PATTERNS_CACHE
    try:
        from .ai_processing import _UNAVAILABLE_PATTERNS
        _CANONICAL_PATTERNS_CACHE = [pattern for _reason, pattern in _UNAVAILABLE_PATTERNS]
        return _CANONICAL_PATTERNS_CACHE
    except ImportError as exc:
        if not _CANONICAL_IMPORT_WARNED:
            print(
                f"⚠️ tour_scheduling: could not import ai_processing._UNAVAILABLE_PATTERNS "
                f"({exc}); using literal terminal-phrase fallback (may drift)."
            )
            _CANONICAL_IMPORT_WARNED = True
        return _PROPERTY_TERMINAL_FALLBACK


def _terminal_is_tour_scoped(latest: str, start: int, end: int) -> bool:
    pre = latest[max(0, start - 22):start]
    post = latest[end:end + 26]
    return bool(_TOUR_SCOPE_PRE_RE.search(pre) or _TOUR_SCOPE_POST_RE.match(post))


def _has_property_scoped_terminal(latest: str) -> bool:
    """True when a canonical terminal phrase appears that is NOT scoped to a tour
    or slot — i.e. the PROPERTY itself is gone."""
    for pattern in _canonical_terminal_patterns():
        for match in re.finditer(pattern, latest):
            if not _terminal_is_tour_scoped(latest, match.start(), match.end()):
                return True
    return False


def looks_like_tour_only_unavailable(text: str = "") -> bool:
    latest = str(text or "").strip().lower()
    if not latest:
        return False

    if _has_property_scoped_terminal(latest):
        return False

    return bool(
        # negation ... <tour subject>: "no tours", "can't show it", "won't ... a tour"
        re.search(
            rf"\b{_TOUR_NEGATION}\b.{{0,80}}\b(?:for\s+|to\s+)?{_TOUR_SUBJECT}\b",
            latest,
        )
        # <tour subject> ... off: "tours aren't available", "tours ... suspended"
        or re.search(
            rf"\b{_TOUR_SUBJECT}\b.{{0,60}}\b{_TOUR_UNAVAIL_PHRASE}\b",
            latest,
        )
        # "no tour(s) availability"
        or re.search(rf"\bno\s+{_TOUR_NOUN}\s+availability\b", latest)
        # "no availability to show" / "no availability for tours"
        or re.search(rf"\bno\s+availability\s+(?:for|to)\s+{_TOUR_SUBJECT}\b", latest)
    )


def tour_date_from_thread_data(thread_data: Dict[str, Any]) -> str:
    data = thread_data or {}
    invite = _tour_invite(data)
    for source in (invite, data):
        for key in ("tourDate", "tourDay", "scheduledDate", "date"):
            value = source.get(key)
            if value:
                return str(value).strip()
    return ""


def _thread_id(stop: Dict[str, Any]) -> str:
    return str(stop.get("id") or stop.get("threadId") or stop.get("thread_id") or "")


def _tour_invite(stop: Dict[str, Any]) -> Dict[str, Any]:
    invite = stop.get("tourInvite")
    return invite if isinstance(invite, dict) else {}


def _stop_address(stop: Dict[str, Any]) -> str:
    invite = _tour_invite(stop)
    if invite.get("address"):
        return str(invite.get("address")).strip()

    property_value = stop.get("property")
    if isinstance(property_value, dict):
        for key in ("address", "propertyAddress", "rowAnchor"):
            if property_value.get(key):
                return str(property_value.get(key)).strip()

    for key in ("propertyAddress", "rowAnchor", "row_anchor", "subject", "address"):
        if stop.get(key):
            return str(stop.get(key)).strip()
    return "the property"


def _first_present(*values):
    for value in values:
        if value is not None:
            return value
    return None


def _parse_buffer_minutes(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _scheduled_stop(stop: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    invite = _tour_invite(stop)
    arrival = parse_tour_time_minutes(invite.get("arrivalTime") or stop.get("arrivalTime"))
    departure = parse_tour_time_minutes(invite.get("departureTime") or stop.get("departureTime"))
    if arrival is None or departure is None or departure <= arrival:
        return None
    buffer_minutes = _parse_buffer_minutes(
        _first_present(invite.get("travelBufferMinutes"), stop.get("travelBufferMinutes"))
    )
    return {
        "id": _thread_id(stop),
        "address": _stop_address(stop),
        "arrival": arrival,
        "departure": departure,
        "arrivalTime": format_tour_time(arrival),
        "departureTime": format_tour_time(departure),
        "tourDate": tour_date_from_thread_data(stop),
        "bufferMinutes": buffer_minutes,
        "scheduleComplete": stop.get("scheduleComplete", True),
    }


def _interval_conflicts(
    start: int,
    end: int,
    other_start: int,
    other_end: int,
    buffer_minutes: int,
) -> bool:
    return start < other_end + buffer_minutes and end + buffer_minutes > other_start


def _open_slot_suggestions(
    stops: List[Dict[str, Any]],
    duration: int,
    *,
    after_minutes: int,
    buffer_minutes: int,
    limit: int = 6,
) -> List[str]:
    suggestions = []
    start = max(TOUR_DAY_START_MINUTES, after_minutes)
    if start % 15:
        start += 15 - (start % 15)

    candidate = start
    suggestion_step = max(15, duration + 15)
    while candidate <= TOUR_DAY_END_MINUTES - duration:
        candidate_end = candidate + duration
        if any(
            _interval_conflicts(candidate, candidate_end, stop["arrival"], stop["departure"], buffer_minutes)
            for stop in stops
        ):
            candidate += 15
            continue
        suggestions.append(format_tour_time(candidate))
        if len(suggestions) >= limit:
            break
        candidate += suggestion_step
    return suggestions


def evaluate_alternate_tour_time(
    schedule,
    current_thread_id,
    alternate_time,
    *,
    buffer_minutes: int = DEFAULT_BUFFER_MINUTES,
) -> Dict[str, Any]:
    alternate_minutes = parse_tour_time_minutes(alternate_time)
    arrival_time = format_tour_time(alternate_minutes) if alternate_minutes is not None else str(alternate_time or "")
    decision = {
        "feasibility": "needs_review",
        "requestedTime": str(alternate_time or "").strip(),
        "arrivalTime": arrival_time,
        "departureTime": None,
        "tourDate": None,
        "previousSlot": None,
        "conflicts": [],
        "suggestedOpenSlots": [],
    }

    if alternate_minutes is None:
        decision["reviewReason"] = "Alternate tour time could not be parsed."
        return decision

    current_id = str(current_thread_id or "")
    raw_stops = [stop for stop in (schedule or []) if isinstance(stop, dict)]
    scheduled_stops = [stop for stop in (_scheduled_stop(raw) for raw in raw_stops) if stop]
    current_stop = next((stop for stop in scheduled_stops if stop["id"] == current_id), None)
    if not current_stop:
        decision["reviewReason"] = "Current tour stop is missing from the schedule."
        return decision
    if any(stop.get("scheduleComplete") is False for stop in scheduled_stops):
        decision["reviewReason"] = "Full tour schedule could not be loaded."
        return decision

    duration = current_stop["departure"] - current_stop["arrival"]
    decision["tourDate"] = current_stop.get("tourDate") or None
    if duration <= 0:
        duration = DEFAULT_TOUR_DURATION_MINUTES

    departure = alternate_minutes + duration
    decision["departureTime"] = format_tour_time(departure)

    other_stops = [stop for stop in scheduled_stops if stop["id"] != current_id]
    explicit_buffers = [
        stop["bufferMinutes"]
        for stop in [current_stop, *other_stops]
        if stop.get("bufferMinutes") is not None
    ]
    effective_buffer = max(explicit_buffers) if explicit_buffers else buffer_minutes
    previous = [
        stop for stop in other_stops
        if stop["departure"] + effective_buffer <= alternate_minutes
    ]
    if previous:
        decision["previousSlot"] = max(previous, key=lambda stop: stop["departure"])

    conflicts = [
        stop for stop in other_stops
        if _interval_conflicts(alternate_minutes, departure, stop["arrival"], stop["departure"], effective_buffer)
    ]
    decision["conflicts"] = conflicts

    if conflicts:
        decision["feasibility"] = "conflict"
        decision["suggestedOpenSlots"] = _open_slot_suggestions(
            other_stops,
            duration,
            after_minutes=alternate_minutes + 15,
            buffer_minutes=effective_buffer,
        )
    else:
        decision["feasibility"] = "fits"

    return decision


def _safe_greeting_name(contact_name: str = "", recipient_email: str = "") -> str:
    candidate = str(contact_name or "").strip()
    recipient_local = str(recipient_email or "").split("@", 1)[0].strip().lower()
    compact_candidate = re.sub(r"[^a-z0-9]", "", candidate.lower())
    compact_local = re.sub(r"[^a-z0-9]", "", recipient_local)
    if not candidate or "@" in candidate or (compact_local and compact_candidate == compact_local):
        return "there"
    return candidate


def _decision_address(thread_data: Dict[str, Any]) -> str:
    return _stop_address(thread_data or {})


def _decision_tour_date_label(thread_data: Dict[str, Any], decision: Dict[str, Any]) -> str:
    return format_tour_date_label(
        (decision or {}).get("tourDate") or tour_date_from_thread_data(thread_data or {})
    )


def _date_time_phrase(thread_data: Dict[str, Any], decision: Dict[str, Any], arrival: str) -> str:
    date_label = _decision_tour_date_label(thread_data, decision)
    arrival_text = str(arrival or "").strip()
    if date_label and arrival_text and date_label.lower() not in arrival_text.lower():
        return f"{date_label} at {arrival_text}"
    return arrival_text or date_label or "that time"


def build_tour_unavailable_reply(
    contact_name: str,
    recipient_email: str,
    thread_data: Dict[str, Any],
    tour_date: str = "",
) -> str:
    greeting = _safe_greeting_name(contact_name, recipient_email)
    address = _decision_address(thread_data)
    date_label = format_tour_date_label(tour_date or tour_date_from_thread_data(thread_data or {}))
    date_phrase = f" on {date_label}" if date_label else ""

    return (
        f"Hi {greeting},\n\n"
        f"Thanks for letting me know. Understood that tours are unavailable for {address}{date_phrase}.\n\n"
        "I'll keep the property information in the package and follow up if we need anything else."
    )


def build_schedule_aware_tour_reply(
    contact_name: str,
    recipient_email: str,
    thread_data: Dict[str, Any],
    decision: Dict[str, Any],
) -> str:
    greeting = _safe_greeting_name(contact_name, recipient_email)
    address = _decision_address(thread_data)
    arrival = str((decision or {}).get("arrivalTime") or "").strip() or "that time"
    arrival_phrase = _date_time_phrase(thread_data, decision, arrival)
    feasibility = str((decision or {}).get("feasibility") or "").strip().lower()

    if feasibility == "fits":
        return (
            f"Hi {greeting},\n\n"
            f"{arrival_phrase} works on our end for {address}.\n\n"
            "Please consider that confirmed."
        )

    if feasibility == "conflict":
        suggestions = [slot for slot in (decision or {}).get("suggestedOpenSlots") or [] if slot]
        if len(suggestions) >= 2:
            offer = f"Could we do {suggestions[0]} or {suggestions[1]} instead?"
        elif suggestions:
            offer = f"Could we do {suggestions[0]} instead?"
        else:
            offer = "Could you send a couple of later windows that might work?"
        return (
            f"Hi {greeting},\n\n"
            f"Thanks for offering {arrival_phrase} for {address}. Another tour is already scheduled "
            f"around that window.\n\n"
            f"{offer}"
        )

    return (
        f"Hi {greeting},\n\n"
        f"I need to review the tour schedule before confirming {arrival_phrase} for {address}.\n\n"
        "I'll follow up once I can confirm a workable time."
    )
