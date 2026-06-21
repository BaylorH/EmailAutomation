import re
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


def _scheduled_stop(stop: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    invite = _tour_invite(stop)
    arrival = parse_tour_time_minutes(invite.get("arrivalTime") or stop.get("arrivalTime"))
    departure = parse_tour_time_minutes(invite.get("departureTime") or stop.get("departureTime"))
    if arrival is None or departure is None or departure <= arrival:
        return None
    stop_buffer = invite.get("travelBufferMinutes") or stop.get("travelBufferMinutes")
    try:
        buffer_minutes = int(stop_buffer)
    except (TypeError, ValueError):
        buffer_minutes = DEFAULT_BUFFER_MINUTES
    return {
        "id": _thread_id(stop),
        "address": _stop_address(stop),
        "arrival": arrival,
        "departure": departure,
        "arrivalTime": format_tour_time(arrival),
        "departureTime": format_tour_time(departure),
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
    if duration <= 0:
        duration = DEFAULT_TOUR_DURATION_MINUTES

    departure = alternate_minutes + duration
    decision["departureTime"] = format_tour_time(departure)

    other_stops = [stop for stop in scheduled_stops if stop["id"] != current_id]
    effective_buffer = max(
        buffer_minutes,
        current_stop.get("bufferMinutes") or DEFAULT_BUFFER_MINUTES,
        *[stop.get("bufferMinutes") or DEFAULT_BUFFER_MINUTES for stop in other_stops],
    )
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


def build_schedule_aware_tour_reply(
    contact_name: str,
    recipient_email: str,
    thread_data: Dict[str, Any],
    decision: Dict[str, Any],
) -> str:
    greeting = _safe_greeting_name(contact_name, recipient_email)
    address = _decision_address(thread_data)
    arrival = str((decision or {}).get("arrivalTime") or "").strip() or "that time"
    feasibility = str((decision or {}).get("feasibility") or "").strip().lower()

    if feasibility == "fits":
        return (
            f"Hi {greeting},\n\n"
            f"{arrival} works on our end for {address}.\n\n"
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
            f"Thanks for offering {arrival} for {address}. Another tour is already scheduled "
            f"around that window.\n\n"
            f"{offer}"
        )

    return (
        f"Hi {greeting},\n\n"
        f"I need to review the tour schedule before confirming {arrival} for {address}.\n\n"
        "I'll follow up once I can confirm a workable time."
    )
