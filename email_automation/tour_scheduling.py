import re
from datetime import datetime
from typing import Any, Dict, List, Optional


DEFAULT_TOUR_DURATION_MINUTES = 30
DEFAULT_BUFFER_MINUTES = 5
TOUR_DAY_START_MINUTES = 8 * 60
TOUR_DAY_END_MINUTES = 17 * 60


_GENERIC_TOUR_INVITATION_RE = re.compile(
    r"\b(?:please\s+|feel\s+free\s+to\s+)?let\s+(?:me|us)\s+know\s+if\s+"
    r"(?:you|your\s+client|they)(?:\s+would|['’]d)?\s+"
    r"(?:need(?:s)?|want(?:s)?|like|(?:are|is)\s+interested\s+in)\b"
    r"[^.!?;\n]{0,60}\b(?:tours?|showings?|walk[-\s]?throughs?|walkthroughs?|"
    r"see\s+(?:it|the\s+space|the\s+property))\b",
    re.IGNORECASE,
)
_PASSIVE_TOUR_INVITATION_RE = re.compile(
    r"\b(?:tours?|showings?|walk[-\s]?throughs?|walkthroughs?)\s+"
    r"(?:are|is)\s+available\s+(?:upon|by)\s+request\b|"
    r"\b(?:(?:happy|glad|able|available)\s+to|(?:i|we)\s+(?:can|could))\s+"
    r"(?:arrange|schedule|coordinate|show|tour|walk)\b[^.!?;\n]{0,60}"
    r"\b(?:upon\s+request|if\s+(?:needed|useful|helpful|desired|"
    r"you(?:['’]d|\s+would)?\s+(?:like|want)|your\s+client\s+(?:wants?|needs?)))\b",
    re.IGNORECASE,
)
_VIRTUAL_TOUR_RESOURCE_RE = re.compile(
    r"\b(?:virtual|online|video|3d|360(?:-degree)?)\s+(?:tours?|showings?|viewings?|walk[-\s]?throughs?|walkthroughs?)\b|"
    r"\b(?:tours?|showings?|viewings?|walk[-\s]?throughs?|walkthroughs?)\s+(?:video|link|url|online)\b|"
    r"\b(?:tours?|showings?|viewings?|walk[-\s]?throughs?|walkthroughs?)\s+"
    r"(?:is|are)\s+(?:available\s+)?(?:online|virtual|video|via\s+(?:a\s+)?link)\b|"
    r"\bsee\b[^.!?;\n]{0,50}\b(?:attached\s+(?:photos?|images?)|photos?|images?|video|link|url|online)\b",
    re.IGNORECASE,
)
_TOUR_DAY_TOKEN = (
    r"(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:sday)?)?|"
    r"fri(?:day)?|sat(?:urday)?|sun(?:day)?|today|tomorrow)"
)
_TOUR_CLOCK_TOKEN = r"(?:\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|morning|afternoon|noon)"
_CONCRETE_TOUR_LOGISTICS_RE = re.compile(
    rf"\b(?:{_TOUR_DAY_TOKEN}|{_TOUR_CLOCK_TOKEN})\b|"
    r"\b(?:which|what)\s+(?:day|time|window)\b",
    re.IGNORECASE,
)
_TOUR_SEMANTIC_SEGMENT_RE = re.compile(
    r"(?<=[.!?;])\s+|\n+|\s+[—–]\s+|"
    r",\s*(?:and|but|however|so|or)\s+|"
    r",\s+(?=(?:i|we|the\s+owner|ownership|please|feel\s+free|let|"
    r"would|can|could)\b)|"
    r"\s+(?:and|but|so|or)\s+(?=(?:i|we|the\s+owner|ownership|"
    r"please|feel\s+free|let|would|can|could)\b)",
    re.IGNORECASE,
)

TOUR_INTENT_ACTIONABLE = "actionable"
TOUR_INTENT_COURTESY = "courtesy"
TOUR_INTENT_UNKNOWN = "unknown"

_PHYSICAL_PROPERTY_TERM = r"(?:space|property|suite|building|unit)"
_PHYSICAL_LOGISTICS_TAIL = (
    rf"(?:\s+(?:(?:for|with)\s+(?:you|your\s+client|the\s+client|them|"
    rf"the\s+tenant|your\s+broker|the\s+broker)|"
    rf"(?:to|at|inside)\s+(?:the\s+)?{_PHYSICAL_PROPERTY_TERM}|"
    r"at\s+(?:the\s+)?(?:front|main)\s+entrance|at\s+(?:the\s+)?lobby))?"
    r"\s*[.!?]?\s*$"
)
_PHYSICAL_SHOW_TERM = (
    rf"show\s+(?:(?:you|your\s+client)\s+(?:around|(?:the\s+)?{_PHYSICAL_PROPERTY_TERM})|"
    rf"(?:the\s+)?{_PHYSICAL_PROPERTY_TERM}(?:\s+to\s+(?:you|your\s+client))?)"
)
_DIRECT_PHYSICAL_SHOW_SEE_RE = re.compile(
    rf"\b(?:{_PHYSICAL_SHOW_TERM}|see\s+(?:the\s+)?{_PHYSICAL_PROPERTY_TERM})\b"
    r"(?P<tail>[^;]*)$",
    re.IGNORECASE,
)
_DIRECT_PHYSICAL_SHOW_SEE_TAIL_RE = re.compile(
    rf"\s*,?\s*(?:(?:in\s+person|on[-\s]?site)\s+)?"
    rf"(?:"
    rf"(?:(?:on|at)\s+)?{_TOUR_DAY_TOKEN}(?:\s+(?:at\s+)?{_TOUR_CLOCK_TOKEN})?|"
    rf"(?:at\s+)?{_TOUR_CLOCK_TOKEN}|"
    r"(?:next|this)\s+week|"
    r"(?:when|whenever)\s+(?:it\s+)?works?\s+for\s+(?:you|your\s+client)|"
    rf"if\s+(?:{_TOUR_DAY_TOKEN}|{_TOUR_CLOCK_TOKEN})\s+works?\s+for\s+"
    r"(?:you|your\s+client)|at\s+your\s+convenience"
    r")?"
    r"(?:\s+(?:in\s+person|on[-\s]?site))?"
    r"(?:\s+(?:with|for|to)\s+(?:you|your\s+client|the\s+client|them|"
    r"your\s+broker|the\s+broker))?"
    rf"(?:\s+(?:at|inside)\s+(?:the\s+)?{_PHYSICAL_PROPERTY_TERM})?"
    r"\s*[.!?]?\s*$",
    re.IGNORECASE,
)


def _physical_tour_action_tail_is_bounded(tail: str) -> bool:
    """Accept only scheduling/person/location tails after a physical tour action."""
    return bool(_DIRECT_PHYSICAL_SHOW_SEE_TAIL_RE.fullmatch(str(tail or "")))


def _direct_physical_show_or_see_has_bounded_tail(text: str) -> bool:
    """Reject explicit show/see clauses that end in a nonphysical qualifier."""
    match = _DIRECT_PHYSICAL_SHOW_SEE_RE.search(str(text or ""))
    return bool(
        match is None
        or _physical_tour_action_tail_is_bounded(match.group("tail") or "")
    )


_PHYSICAL_TOUR_TERM = (
    r"(?:tours?|showings?|viewings?|walk-?throughs?|"
    rf"walk\s+(?:(?:through\s+)?(?:the\s+)?{_PHYSICAL_PROPERTY_TERM})|"
    rf"see\s+(?:the\s+)?{_PHYSICAL_PROPERTY_TERM}|"
    rf"{_PHYSICAL_SHOW_TERM}|come\s+(?:by|see\s+(?:the\s+)?{_PHYSICAL_PROPERTY_TERM})|"
    r"stop\s+by|take\s+a\s+look)"
)
_ACTIONABLE_TOUR_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    # Direct offers and questions.
    r"\bwould\s+(?:you|your\s+client)\s+like\s+(?:a\s+)?"
    r"(?:tour|showing|viewing|walk[-\s]?through)\b",
    r"\b(?:do|does)\s+(?:you|your\s+client)\s+want\s+(?:a\s+)?"
    r"(?:tour|showing|viewing|walk[-\s]?through)\b",
    rf"\bcome\s+see\s+(?:the\s+)?{_PHYSICAL_PROPERTY_TERM}\b",
    rf"\b(?:would\s+(?:you|your\s+client)\s+like|"
    rf"(?:do|does)\s+(?:you|your\s+client)\s+want)\s+to\s+"
    rf"(?:schedule\s+)?{_PHYSICAL_TOUR_TERM}\b",
    rf"\b(?:happy|glad|able)\s+to\s+{_PHYSICAL_TOUR_TERM}\b",
    rf"\b(?:i|we|the\s+owner|ownership)\s+can\s+{_PHYSICAL_TOUR_TERM}\b",
    rf"\b(?:can|could)\s+(?:i|we|the\s+owner|ownership)\s+{_PHYSICAL_TOUR_TERM}\b",
    rf"\b(?:can|could)\s+(?:you|your\s+client|they|we)\s+(?:tour|walk|come\s+by|stop\s+by|see\s+(?:the\s+)?(?:space|property|suite))\b",
    rf"\b(?:you|your\s+client)\s+(?:are|is)\s+welcome\s+to\s+visit\s+"
    rf"(?:the\s+)?{_PHYSICAL_PROPERTY_TERM}\s+(?:(?:on|at)\s+)?"
    rf"(?:{_TOUR_DAY_TOKEN}|{_TOUR_CLOCK_TOKEN})\b{_PHYSICAL_LOGISTICS_TAIL}",
    rf"\b(?:i|we)\s+(?:can|could|will)\s+let\s+(?:you|your\s+client|them)\s+"
    rf"(?:in|into)\s+(?:the\s+)?{_PHYSICAL_PROPERTY_TERM}\s+"
    rf"(?:(?:on|at)\s+)?(?:{_TOUR_DAY_TOKEN}|{_TOUR_CLOCK_TOKEN})\b"
    rf"{_PHYSICAL_LOGISTICS_TAIL}",
    rf"\b(?:i|we)\s+(?:can|could|will)\s+accommodate\s+(?:a\s+)?visit\s+"
    rf"(?:(?:on|at)\s+)?(?:{_TOUR_DAY_TOKEN}|{_TOUR_CLOCK_TOKEN})\b"
    rf"{_PHYSICAL_LOGISTICS_TAIL}",
    rf"\b(?:i|we)\s+(?:can|could|will)\s+provide\s+access\s+"
    rf"(?:(?:on|at)\s+)?(?:{_TOUR_DAY_TOKEN}|{_TOUR_CLOCK_TOKEN})\b"
    rf"{_PHYSICAL_LOGISTICS_TAIL}",
    r"\b(?:i|we)?\s*(?:can|could)\s+do\s+(?:a\s+)?(?:tour|showing|viewing|walk[-\s]?through)\b",
    r"\b(?:can|could)\s+your\s+client\s+make\s+(?:a\s+)?(?:tour|showing|walk[-\s]?through)\b",
    rf"\bwhen\s+(?:would|can|could|should)\s+(?:you|your\s+client|they|we)\s+(?:like\s+to\s+)?{_PHYSICAL_TOUR_TERM}\b",
    rf"\b(?:what|which)\s+(?:day|date|time|window)\b[^.!?;]{{0,65}}\b{_PHYSICAL_TOUR_TERM}\b",
    rf"\bare\s+there\s+(?:any\s+)?(?:dates?|times?|windows?)\b[^.!?;]{{0,65}}\b(?:work|works|available)\b[^.!?;]{{0,35}}\b{_PHYSICAL_TOUR_TERM}\b",
    r"\bwhich\s+of\s+these\s+tour\s+windows?\b[^.!?;]{0,65}\b(?:work|works|available)\b",
    rf"\bdoes\b[^.!?;]{{0,45}}\b(?:work|works)\b[^.!?;]{{0,35}}\b{_PHYSICAL_TOUR_TERM}\b",

    # Concrete scheduling instructions or logistics.
    rf"\b(?:schedule|arrange|set(?:\s+up)?|book|coordinate)\s+(?:(?:a|the)\s+)?{_PHYSICAL_TOUR_TERM}\b",
    r"\b(?:offered|sent|provided|gave)\b[^.!?;]{0,45}\b"
    r"(?:tour|showing|viewing|walk[-\s]?through)\s+(?:dates?|times?|windows?|slots?|availability)\b",
    rf"\b(?:send|propose|share|provide)\b[^.!?;]{{0,55}}\b(?:availability|dates?|times?|windows?|slots?)\b[^.!?;]{{0,65}}\b(?:schedule|arrange|book|coordinate|{_PHYSICAL_TOUR_TERM})\b",
    rf"\b(?:pick|choose|confirm)\b[^.!?;]{{0,85}}\b{_PHYSICAL_TOUR_TERM}\b",
    rf"\blet\s+(?:me|us)\s+know\s+if\s+(?:you|your\s+client|they)\s+can\s+{_PHYSICAL_TOUR_TERM}\b",
    rf"\blet\s+(?:me|us)\s+know\s+when\s+(?:you|your\s+client|they)\s+(?:want|wants|would\s+like)\s+to\s+{_PHYSICAL_TOUR_TERM}\b",
    rf"\blet\s+(?:me|us)\s+know\s+if\b[^.!?;]{{0,60}}\b(?:works?|available)\b[^.!?;]{{0,35}}\b{_PHYSICAL_TOUR_TERM}\b",

    # Stated physical-tour availability and proposed windows.
    rf"\b(?:tours?|showings?|walk[-\s]?throughs?|walkthroughs?)\s+(?:are|is)\s+(?:available|offered|open)\b",
    rf"\b(?:i|we|the\s+owner)\s+(?:have|has)\b[^.!?;]{{0,55}}\b(?:open|available)\b[^.!?;]{{0,35}}\b(?:for\s+)?{_PHYSICAL_TOUR_TERM}\b",
    rf"\b(?:the\s+owner|i|we)\s+(?:is|am|are)\s+(?:offering|proposing)\b[^.!?;]{{0,65}}\b{_PHYSICAL_TOUR_TERM}\b",
    rf"\b(?:i|we)\s+will\s+(?:schedule|arrange|book|coordinate)\b[^.!?;]{{0,45}}\b{_PHYSICAL_TOUR_TERM}\b",
    rf"\b(?:tour|showing|viewing|walk-?through|"
    rf"walk\s+(?:through\s+)?(?:the\s+)?{_PHYSICAL_PROPERTY_TERM})\b"
    r"[^.!?;]{0,35}\b(?:"
    r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|"
    r"mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|"
    r"sat(?:urday)?|sun(?:day)?|today|tomorrow|morning|afternoon|noon)\b",

    # On-site meeting logistics must mention a physical property location so
    # ordinary uses such as "meet the asking rate" cannot self-validate.
    r"\bwhen\s+should\s+we\s+meet\s+(?:at|on)\s+(?:the\s+)?(?:property|site|suite|building)\b",
    r"\b(?:can|could|should)\s+we\s+meet\s+at\s+(?:the\s+)?(?:property|site|suite|building|front\s+entrance|lobby|main\s+office)\b",
))

_TOUR_SLOT_REFERENCE_RE = re.compile(
    r"\b(?:that|the|this|requested|scheduled)\s+(?:time|slot|window|appointment)\b|"
    r"\bthat\s+(?:no\s+longer\s+)?works?\b|"
    r"\b(?:tour|showing|viewing)\s+(?:time|slot|window|date|appointment)\b|"
    r"\brequested\s+(?:arrival|departure)\b",
    re.IGNORECASE,
)
_TOUR_LIFECYCLE_SIGNAL_RE = re.compile(
    r"\b(?:confirmed|confirming|works?|instead|reschedul(?:e|ed|ing)|"
    r"double[-\s]?booked|see\s+you|sounds\s+good|"
    r"(?:i(?:\s+am|['’]m)|we(?:\s+are|['’]re))\s+available|"
    r"does\s+not\s+work|doesn[’']t\s+work|do(?:es)?n[’']t\s+work|"
    r"will\s+not\s+work|won[’']t\s+work|can[’']t\s+do|cannot\s+do|"
    r"can[’']t\s+show|cannot\s+show|not\s+able\s+to\s+show|"
    r"could\s+(?:(?:you|we)\s+)?do|can\s+(?:you|we)\s+do|not\s+available|unavailable|"
    r"no\s+longer\s+available|no\s+(?:tours?|showings?|availability)|"
    r"(?:isn[’']t|aren[’']t)\s+available|not\s+(?:being\s+)?offered|"
    r"not\s+(?:offering|touring)|suspended|cancel(?:led|ed)?|declin(?:e|ed))\b",
    re.IGNORECASE,
)
_BARE_TOUR_SLOT_REPLY_RE = re.compile(
    r"\s*(?:(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|"
    r"thu(?:r(?:sday)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?|"
    r"today|tomorrow)\s+(?:at\s+)?)?"
    r"(?:\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|morning|afternoon|noon)"
    r"[.!]?\s*",
    re.IGNORECASE,
)
_SHORT_TOUR_CONFIRMATION_RE = re.compile(
    r"\s*(?:(?:(?:we|i)\s+(?:are|am)\s+|we['’]re\s+|i['’]m\s+)?"
    r"(?:confirmed|good\s+to\s+go)|sounds\s+good|see\s+you\s+(?:then|there))"
    r"[.!]?\s*",
    re.IGNORECASE,
)
_PHYSICAL_TOUR_REFERENCE_RE = re.compile(
    rf"\b(?:{_PHYSICAL_TOUR_TERM})\b",
    re.IGNORECASE,
)
_LEADING_EXPLICIT_SUBJECT_RE = re.compile(
    r"^\s*(?:(?P<determiner>the|this|that|our|my|your)\s+)?"
    r"(?P<subject>[a-z0-9][\w'’/-]*(?:\s+[a-z0-9][\w'’/-]*){0,6}?)\s+"
    r"(?:is|are|was|were|has|have|had|"
    r"does(?:\s+not|n['’]?t)?|do(?:\s+not|n['’]?t)?|did|"
    r"won['’]?t|will|would|can|could|moved|changed|works?|confirmed|scheduled)\b",
    re.IGNORECASE,
)
_PHYSICAL_SUBJECT_HEADS = frozenset({
    "space", "property", "suite", "building", "unit", "site",
    "tour", "tours", "showing", "showings", "viewing", "viewings",
    "walkthrough", "walkthroughs", "appointment", "appointments",
    "time", "slot", "window", "date", "day", "arrival", "departure",
})
_PERSON_SUBJECT_HEADS = frozenset({
    "owner", "ownership", "broker", "agent", "team", "client", "tenant",
    "representative", "manager",
})
_NON_NOMINAL_CLAUSE_STARTERS = frozenset({
    "i", "we", "you", "they", "happy", "glad", "able", "welcome",
    "can", "could", "would", "do", "does", "come", "please",
    "when", "what", "which", "where", "how", "are", "is",
    "there", "need", "also", "following", "unfortunately",
    "i'm", "i’m", "we're", "we’re", "can't", "can’t", "won't", "won’t",
    "let's", "let’s", "lets",
})
_OBJECT_BOUNDARY_WORDS = frozenset({
    "at", "on", "until", "by", "before", "after", "during", "instead",
    "if", "when", "whenever",
    "today", "tomorrow", "morning", "afternoon", "noon", "monday",
    "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat",
    "sun", "next", "this",
    "right", "currently", "now",
})
_WORD_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-'’][a-z0-9]+)?", re.IGNORECASE)
_PRONOUN_PHYSICAL_OFFER_RE = re.compile(
    r"\b(?:i|we|you|they|your\s+client|the\s+owner|ownership)\s+"
    r"(?:can|could|will|would|am\s+able\s+to|are\s+able\s+to)\s+"
    r"(?:show|walk|see)\b",
    re.IGNORECASE,
)
_PROPOSED_TOUR_OPTION_RE = re.compile(
    rf"\b(?:can|could|would)\s+(?:you|we)\s+do\s+"
    rf"(?P<option>{_TOUR_DAY_TOKEN}(?:\s+at\s+{_TOUR_CLOCK_TOKEN})?|{_TOUR_CLOCK_TOKEN})\b",
    re.IGNORECASE,
)
_EXPLICIT_SLOT_NEGOTIATION_RE = re.compile(
    rf"\b(?:{_TOUR_DAY_TOKEN}|{_TOUR_CLOCK_TOKEN})\s+"
    r"(?:instead\b|works?\s+better\b)|"
    rf"\b(?:{_TOUR_DAY_TOKEN}|{_TOUR_CLOCK_TOKEN})\b[^.!?;]{{0,45}}"
    r"\b(?:does\s+not\s+work|doesn['’]t\s+work|will\s+not\s+work|won['’]t\s+work)\b",
    re.IGNORECASE,
)
_REJECTED_TOUR_SLOT_RE = re.compile(
    rf"\b(?:{_TOUR_DAY_TOKEN}|{_TOUR_CLOCK_TOKEN})\b[^.!?;]{{0,45}}"
    r"\b(?:does\s+not\s+work|doesn['’]t\s+work|will\s+not\s+work|won['’]t\s+work)\b",
    re.IGNORECASE,
)
_TENANT_STATE_PREDICATE = (
    r"(?:the\s+)?(?:current|existing)?\s*tenant\s+"
    r"(?:"
    r"(?:(?:will|would|may|should)\s+(?:be\s+)?)?vacat(?:e|es|ed|ing)|"
    r"(?:(?:will|would|may|should|has|had|is)\s+)?mov(?:e|es|ed|ing)\s+out|"
    r"(?:is|remains?)\s+(?:still\s+)?in\s+place"
    r")\b"
)
_LEADING_TENANT_OCCUPANCY_CONTEXT_RE = re.compile(
    rf"^\s*(?:(?:once|when|after|until)\s+)?{_TENANT_STATE_PREDICATE}",
    re.IGNORECASE,
)
_SUBORDINATE_TENANT_OCCUPANCY_CONTEXT_RE = re.compile(
    rf"\b(?:once|when|after|until)\s+{_TENANT_STATE_PREDICATE}",
    re.IGNORECASE,
)
_PHYSICAL_MAIN_PREDICATE_RE = re.compile(
    r"^\s*(?:i|we|you|they|your\s+client|the\s+owner|ownership)\s+"
    r"(?:(?:can|could|will|would)(?:\s+not)?|cannot|can[’']t|"
    r"(?:am|are)\s+(?:not\s+)?able\s+to)\b"
    r"\s+(?:show|walk|see|visit|provide\s+access|let)\b",
    re.IGNORECASE,
)
_PHYSICAL_PREDICATE_TERM = (
    r"(?:space|property|suite|building|unit|site|warehouse)"
    r"(?=\s*(?:throughout\b|overall\b|inside\b|[,.;!?—–-]|$))"
)
_PHYSICAL_DISCOURSE_CONTEXT_RE = re.compile(
    rf"\b(?:this|that)\s+one\s+(?:is|was)\s+"
    rf"(?:(?:a|an|the|true)\s+)*{_PHYSICAL_PREDICATE_TERM}|"
    rf"\bit(?:\s+(?:is|was)|['’]s)\s+"
    rf"(?:(?:a|an|the|true)\s+)*{_PHYSICAL_PREDICATE_TERM}",
    re.IGNORECASE,
)


def split_tour_semantic_segments(text: str = "") -> List[str]:
    """Split independent tour clauses without separating day/time lists."""
    return [
        segment.strip(" \t,")
        for segment in _TOUR_SEMANTIC_SEGMENT_RE.split(str(text or ""))
        if segment.strip(" \t,")
    ]


def looks_like_concrete_tour_logistics(text: str = "") -> bool:
    return bool(_CONCRETE_TOUR_LOGISTICS_RE.search(str(text or "")))


def _leading_subject_kind(segment: str) -> Optional[str]:
    """Classify a grammatical leading subject without enumerating business nouns."""
    match = _LEADING_EXPLICIT_SUBJECT_RE.search(str(segment or ""))
    if not match:
        return "physical" if _TOUR_SLOT_REFERENCE_RE.search(str(segment or "")) else None
    subject_text = match.group("subject").lower()
    subject_words = _WORD_TOKEN_RE.findall(subject_text)
    if not subject_words:
        return None
    bare_concrete_slot = bool(re.fullmatch(
        rf"(?:{_TOUR_DAY_TOKEN}(?:\s+at\s+{_TOUR_CLOCK_TOKEN})?|"
        rf"{_TOUR_CLOCK_TOKEN})",
        subject_text,
        re.IGNORECASE,
    ))
    if bare_concrete_slot:
        return "temporal"
    if not match.group("determiner"):
        if subject_words[0] in _NON_NOMINAL_CLAUSE_STARTERS:
            return (
                "physical"
                if _TOUR_SLOT_REFERENCE_RE.search(str(segment or ""))
                else None
            )
        if subject_words[0] in {"virtual", "online", "video"}:
            return "non_tour"
    elif (
        match.group("determiner").lower() in {"this", "that"}
        and subject_words[0] in {"no", "not", "still", "now", "already"}
        and _TOUR_SLOT_REFERENCE_RE.search(str(segment or ""))
    ):
        return "slot_pronoun"
    if (
        match.group("determiner")
        and match.group("determiner").lower() in {"this", "that"}
        and subject_text in {"time", "slot", "window", "appointment"}
    ):
        return "slot_pronoun"
    slot_adjunct = re.fullmatch(
        r"(?P<core>.+?)\s+(?:at|during|for)\s+"
        r"(?:that|the|this|requested|scheduled)\s+"
        r"(?:time|slot|window|appointment)",
        subject_text,
        re.IGNORECASE,
    )
    if slot_adjunct:
        core_text = slot_adjunct.group("core").strip()
        core_words = _WORD_TOKEN_RE.findall(core_text)
        if re.fullmatch(
            rf"(?:{_TOUR_DAY_TOKEN}|{_TOUR_CLOCK_TOKEN})",
            core_text,
            re.IGNORECASE,
        ):
            return "physical"
        if core_words and core_words[-1] in _PHYSICAL_SUBJECT_HEADS:
            return "physical"
        return "non_tour"
    head = subject_words[-1]
    if head in {"time", "slot", "window", "date", "day", "arrival", "departure", "appointment", "appointments"}:
        temporal_slot_subject = bool(re.fullmatch(
            rf"(?:{_TOUR_DAY_TOKEN}(?:\s+(?:at\s+)?{_TOUR_CLOCK_TOKEN})?|"
            rf"{_TOUR_CLOCK_TOKEN})\s+(?:time|slot|window|date|day|appointment)",
            subject_text,
            re.IGNORECASE,
        ))
        return "physical" if (
            len(subject_words) == 1
            or any(word in _PHYSICAL_SUBJECT_HEADS - {head} for word in subject_words)
            or any(word in {"requested", "scheduled"} for word in subject_words)
            or temporal_slot_subject
        ) else "non_tour"
    if head in _PHYSICAL_SUBJECT_HEADS:
        return "physical"
    if head in _PERSON_SUBJECT_HEADS:
        return None
    return "non_tour"


def _object_head_kind(tokens: List[str], physical_antecedent: bool) -> Optional[str]:
    while tokens and tokens[0] in {"the", "this", "that", "our", "my", "your", "a", "an"}:
        tokens = tokens[1:]
    if not tokens:
        return None
    if tokens[0] in {"it", "there"}:
        return "physical" if physical_antecedent else "non_tour"

    object_tokens = []
    for token in tokens:
        if token in _OBJECT_BOUNDARY_WORDS or token in {"to", "of", "for", "with"}:
            break
        if token.isdigit():
            break
        object_tokens.append(token)
    if not object_tokens:
        return None
    return "physical" if object_tokens[-1] in _PHYSICAL_SUBJECT_HEADS else "non_tour"


def _show_or_walk_object_kind(segment: str, physical_antecedent: bool) -> Optional[str]:
    """Resolve physical-action objects; unknown named objects fail closed."""
    tokens = [token.lower() for token in _WORD_TOKEN_RE.findall(str(segment or ""))]
    for index, token in enumerate(tokens):
        if token not in {"show", "walk", "see", "visit", "review", "access"}:
            continue
        cursor = index + 1
        if (
            token == "see"
            and cursor + 1 < len(tokens)
            and tokens[cursor] in {"you", "us", "them"}
            and tokens[cursor + 1] in {"then", "there"}
        ):
            # Lifecycle idiom ("See you then"), not a request to view a
            # non-tour object named "then" or "there".
            return None
        if cursor + 1 < len(tokens) and tokens[cursor:cursor + 2] == ["your", "client"]:
            cursor += 2
        elif cursor < len(tokens) and tokens[cursor] in {"you", "me", "us", "them"}:
            cursor += 1
        if token == "walk" and cursor < len(tokens) and tokens[cursor] in {"through", "around"}:
            cursor += 1
        elif token == "show" and cursor < len(tokens) and tokens[cursor] == "around":
            return "physical"
        if token == "access" and cursor < len(tokens) and tokens[cursor] == "to":
            cursor += 1
        if cursor >= len(tokens) or tokens[cursor] in _OBJECT_BOUNDARY_WORDS:
            return None
        return _object_head_kind(tokens[cursor:], physical_antecedent)
    return None


def _lifecycle_object_kind(segment: str) -> Optional[str]:
    """Resolve named objects of confirm/do/available-for scheduling language."""
    tokens = [token.lower() for token in _WORD_TOKEN_RE.findall(str(segment or ""))]
    for index, token in enumerate(tokens):
        if token in {"confirm", "confirmed", "confirming", "do"}:
            kind = _object_head_kind(tokens[index + 1:], physical_antecedent=False)
            if kind:
                return kind
    for index, token in enumerate(tokens):
        if token != "for" or index + 1 >= len(tokens):
            continue
        following = tokens[index + 1:]
        while following and following[0] in {"a", "an", "the", "our", "my", "your"}:
            following = following[1:]
        if not following or following[0] in {"us", "me", "you", "them", "team", "client"}:
            continue
        kind = _object_head_kind(following, physical_antecedent=False)
        if kind:
            return kind
    return None


def _base_clause_tour_intent(clause: str) -> str:
    passive_or_virtual = (
        _PASSIVE_TOUR_INVITATION_RE.search(clause)
        or _VIRTUAL_TOUR_RESOURCE_RE.search(clause)
    )
    if passive_or_virtual:
        return TOUR_INTENT_COURTESY

    generic_invitation = _GENERIC_TOUR_INVITATION_RE.search(clause)
    if generic_invitation:
        invitation_tail = clause[generic_invitation.end():]
        if (
            looks_like_concrete_tour_logistics(invitation_tail)
            and _physical_tour_action_tail_is_bounded(invitation_tail)
            and any(pattern.search(clause) for pattern in _ACTIONABLE_TOUR_PATTERNS)
        ):
            return TOUR_INTENT_ACTIONABLE
        return TOUR_INTENT_COURTESY
    if not _direct_physical_show_or_see_has_bounded_tail(clause):
        return TOUR_INTENT_UNKNOWN
    if any(pattern.search(clause) for pattern in _ACTIONABLE_TOUR_PATTERNS):
        return TOUR_INTENT_ACTIONABLE
    return TOUR_INTENT_UNKNOWN


def _tour_segment_analysis(text: str = "") -> List[Dict[str, Any]]:
    """Bind each scheduling clause to its explicit physical or non-tour subject."""
    analysis: List[Dict[str, Any]] = []
    active_subject: Optional[str] = None
    segments = split_tour_semantic_segments(text)
    single_segment_message = len(segments) == 1
    for segment in segments:
        base_intent = _base_clause_tour_intent(segment)
        leading_subject = _leading_subject_kind(segment)
        physical_occupancy_context = bool(
            _LEADING_TENANT_OCCUPANCY_CONTEXT_RE.search(segment)
            or (
                _PHYSICAL_MAIN_PREDICATE_RE.search(segment)
                and _SUBORDINATE_TENANT_OCCUPANCY_CONTEXT_RE.search(segment)
            )
        )
        physical_discourse_context = bool(
            _PHYSICAL_DISCOURSE_CONTEXT_RE.search(segment)
        )
        physical_context = physical_occupancy_context or physical_discourse_context
        object_kind = _show_or_walk_object_kind(
            segment,
            physical_antecedent=active_subject == "physical" or physical_context,
        )
        if object_kind is None:
            object_kind = _lifecycle_object_kind(segment)

        # An explicit action object is authoritative. A generic noun object such
        # as a floor plan, model, schedule, or projection cannot inherit tour
        # context merely because it is being "shown" at a day/time.
        if object_kind == "physical":
            active_subject = "physical"
        elif object_kind == "non_tour":
            active_subject = "non_tour_object"
        elif physical_discourse_context:
            active_subject = "physical"
        elif leading_subject == "non_tour":
            active_subject = "non_tour_subject"
        elif leading_subject == "physical" or physical_occupancy_context:
            active_subject = "physical"
        elif (
            leading_subject == "temporal"
            and active_subject not in {"non_tour_subject", "non_tour_object"}
        ):
            active_subject = "physical"
        elif (
            leading_subject == "slot_pronoun"
            and (
                single_segment_message
                or active_subject not in {"non_tour_subject", "non_tour_object"}
            )
        ):
            active_subject = "physical"

        proposed_slot = bool(_PROPOSED_TOUR_OPTION_RE.search(segment))
        rejected_slot = bool(_EXPLICIT_SLOT_NEGOTIATION_RE.search(segment))
        strongly_rejected_slot = bool(_REJECTED_TOUR_SLOT_RE.search(segment))
        explicit_slot_negotiation = proposed_slot or rejected_slot
        slot_can_reset_subject = bool(
            active_subject not in {"non_tour_subject", "non_tour_object"}
            or (
                active_subject == "non_tour_subject"
                and proposed_slot
                and strongly_rejected_slot
            )
            or (
                proposed_slot
                and _PHYSICAL_TOUR_REFERENCE_RE.search(segment)
            )
        )
        if explicit_slot_negotiation and slot_can_reset_subject:
            active_subject = "physical"

        actionable = base_intent == TOUR_INTENT_ACTIONABLE
        if (
            object_kind == "physical"
            and _PRONOUN_PHYSICAL_OFFER_RE.search(segment)
            and _direct_physical_show_or_see_has_bounded_tail(segment)
        ):
            actionable = True

        courtesy = base_intent == TOUR_INTENT_COURTESY and not actionable
        explicit_physical_reference = bool(
            not courtesy
            and object_kind != "non_tour"
            and leading_subject != "non_tour"
            and (
                object_kind == "physical"
                or _PHYSICAL_TOUR_REFERENCE_RE.search(segment)
            )
        )
        if (
            explicit_physical_reference
            and _TOUR_LIFECYCLE_SIGNAL_RE.search(segment)
        ):
            active_subject = "physical"
        slot_negotiation_bound = bool(
            explicit_slot_negotiation
            and active_subject not in {"non_tour_subject", "non_tour_object"}
        )

        tour_bound = False
        if object_kind == "non_tour" or leading_subject == "non_tour":
            tour_bound = False
        elif courtesy:
            tour_bound = False
        elif actionable or slot_negotiation_bound:
            tour_bound = True
        elif active_subject not in {"non_tour_subject", "non_tour_object"}:
            if (
                _BARE_TOUR_SLOT_REPLY_RE.fullmatch(segment)
                or _SHORT_TOUR_CONFIRMATION_RE.fullmatch(segment)
            ):
                tour_bound = True
            else:
                reply_signal = _TOUR_LIFECYCLE_SIGNAL_RE.search(segment)
                tour_bound = bool(
                    reply_signal
                    and (
                        looks_like_concrete_tour_logistics(segment)
                        or _TOUR_SLOT_REFERENCE_RE.search(segment)
                        or explicit_physical_reference
                        or re.search(r"\b(?:show|tour|showing|walk[-\s]?through)\b", segment, re.IGNORECASE)
                    )
                )

        # A bare mention such as "tour schedule" is not a physical discourse
        # antecedent by itself. Carry physical context forward only when this
        # segment was actually bound as a tour action/lifecycle statement.
        if tour_bound and (actionable or explicit_physical_reference):
            active_subject = "physical"

        analysis.append({
            "text": segment,
            "intent": TOUR_INTENT_ACTIONABLE if actionable else base_intent,
            "tourBound": tour_bound,
        })
    return analysis


def subject_bound_tour_segments(text: str = "") -> List[str]:
    """Return only clauses whose scheduling semantics are bound to a physical tour."""
    return [item["text"] for item in _tour_segment_analysis(text) if item["tourBound"]]


def looks_like_tour_scheduling_reply(text: str = "") -> bool:
    """Return true only when scheduling language is bound to a tour/slot subject."""
    return bool(subject_bound_tour_segments(text))


def classify_tour_intent(text: str = "") -> str:
    """Classify only high-confidence tour intent from broker-authored text.

    ``unknown`` is deliberate: callers may safely ignore an unrecognized model
    event without erasing it during normalization. Only proven boilerplate or
    virtual-resource language is labeled ``courtesy``.
    """
    latest = str(text or "").strip()
    if not latest:
        return TOUR_INTENT_UNKNOWN

    analysis = _tour_segment_analysis(latest)
    if any(
        item["intent"] == TOUR_INTENT_ACTIONABLE and item["tourBound"]
        for item in analysis
    ):
        return TOUR_INTENT_ACTIONABLE

    if any(item["intent"] == TOUR_INTENT_COURTESY for item in analysis):
        return TOUR_INTENT_COURTESY
    return TOUR_INTENT_UNKNOWN


def looks_like_explicit_tour_offer_or_request(text: str = "") -> bool:
    return classify_tour_intent(text) == TOUR_INTENT_ACTIONABLE


def extract_proposed_tour_options(text: str = "") -> List[str]:
    """Extract explicit `can/could we do ...` tour alternates from bound clauses."""
    options = []
    seen = set()
    for segment in subject_bound_tour_segments(text):
        for match in _PROPOSED_TOUR_OPTION_RE.finditer(segment):
            option = re.sub(r"\s+", " ", match.group("option").strip())
            option = re.sub(r"\b(am|pm)\b", lambda value: value.group(1).upper(), option, flags=re.IGNORECASE)
            option = re.sub(
                rf"^({_TOUR_DAY_TOKEN})\b",
                lambda value: value.group(1).title(),
                option,
                flags=re.IGNORECASE,
            )
            key = option.lower()
            if key in seen:
                continue
            seen.add(key)
            options.append(option)
    return options[:4]


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
