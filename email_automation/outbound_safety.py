import re
from dataclasses import dataclass
from typing import List, Optional


UNRESOLVED_BRACKET_PLACEHOLDER_RE = re.compile(r"\[[^\]\n]{1,80}\]")
SAFE_BRACKET_TOKENS = {
    "[sic]",
}
PLACEHOLDER_HINT_RE = re.compile(
    r"\b(name|first|last|contact|broker|recipient|email|phone|company|property|"
    r"address|tenant|client|date|time|day|city|state|title|role)\b",
    re.IGNORECASE,
)

# Non-bracket merge-field syntaxes that real mail-merge tools emit. A raw,
# unresolved merge tag in ANY of these shapes must never reach the outbox, so the
# placeholder guard has to understand all of them -- not just square brackets.
DOUBLE_CURLY_PLACEHOLDER_RE = re.compile(r"\{\{+\s*([^{}\n]{1,80}?)\s*\}\}+")
SINGLE_CURLY_PLACEHOLDER_RE = re.compile(r"(?<!\{)\{\s*([^{}\n]{1,80}?)\s*\}(?!\})")
ANGLE_PLACEHOLDER_RE = re.compile(r"<<\s*([^<>\n]{1,80}?)\s*>>")
# A percent merge field is a single token (%First%, %FIELD%, %First_Name%). The
# captured span must not contain spaces, or rent-escalation prose spanning two
# percent signs ("3% annual property increase and a 5% fee") would false-trigger.
PERCENT_PLACEHOLDER_RE = re.compile(r"%\s*([A-Za-z][A-Za-z0-9_]{0,79}?)\s*%")
DOLLAR_BRACE_PLACEHOLDER_RE = re.compile(r"\$\{\s*([^{}\n]{1,80}?)\s*\}")

# Split camelCase merge names (FirstName -> "First Name") so the placeholder-hint
# lexicon can recognise them.
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

_NON_BRACKET_PLACEHOLDER_RES = (
    DOUBLE_CURLY_PLACEHOLDER_RE,
    SINGLE_CURLY_PLACEHOLDER_RE,
    ANGLE_PLACEHOLDER_RE,
    PERCENT_PLACEHOLDER_RE,
    DOLLAR_BRACE_PLACEHOLDER_RE,
)


UNREVIEWED_SCHEDULING_LANGUAGE_RE = re.compile(
    r"(?:"
    r"(?i:\btour\s+scheduling\b)|"
    r"(?i:\btour\s+is\s+being\s+scheduled\b)|"
    r"(?i:\bbefore\s+we\s+proceed\s+with\s+tour\b)|"
    r"(?i:\binclude\s+(?:it|the\s+space|this\s+space)\s+as\s+(?:a\s+)?tour\s+option\b)|"
    r"(?i:\binclude\s+the\s+space\s+for\s+tours\b)|"
    r"(?i:\bproceed\s+with\s+.*\bLOIs?\b)|"
    r"\bLOIs?\b"
    r")",
)

# --- Tour / showing commitment detection ----------------------------------
# A tour-type noun (tour, showing, walkthrough, viewing) -- plus common typos.
_TOUR_NOUN = (
    r"(?:tours?|tuors?|showings?|walk[-\s]?throughs?|walkthroughs?|viewings?)"
)
# Verbs that offer or commit to setting one up.
_SCHEDULE_VERB = (
    r"(?:schedul(?:e|ed|ing)|shedul(?:e|ed|ing)|scedul(?:e|ed|ing)|"
    r"book(?:ing)?|arrang(?:e|ing)|coordinat(?:e|ing)|set[-\s]?up|setup|"
    r"host(?:ing)?|line\s+up|do|offer(?:ing)?|plan(?:ning)?)"
)
# "schedule a tour", "set up a showing", "book a tour", "do a walkthrough" ...
TOUR_COMMIT_RE = re.compile(
    r"(?i:\b" + _SCHEDULE_VERB + r"\b"
    r"(?:\s+(?:a|an|the|another|your|you\s+a|us\s+a|for\s+a))?"
    r"\s+" + _TOUR_NOUN + r"\b)"
)
# Committing to physically visiting the space: "show it", "walk the space",
# "see the space", "show you the space".
SPACE_VISIT_RE = re.compile(
    r"(?i:\b(?:show|see|walk|view|tour)\b"
    r"(?:\s+(?:you|us))?"
    r"\s+(?:it\b|the\s+(?:space|unit|property|suite|building)\b))"
)
# Terse "Tour Fri 2pm?" -- a tour noun pinned to a day/time is a commitment.
_DAY_TIME = (
    r"(?:mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|\d{1,2}\s?(?:am|pm)|\d{1,2}:\d{2})"
)
TOUR_WHEN_RE = re.compile(
    r"(?i:\b" + _TOUR_NOUN + r"\b[^.\n]{0,12}?\b" + _DAY_TIME + r"\b)"
)

# --- Confidential client/tenant identity disclosure -----------------------
_IDENTITY_NOUN = (
    r"(?:clients?|tenants?|end[\s-]?users?|users?|occupiers?|occupants?|"
    r"lessees?|covenant|prospects?)"
)
# "<identity noun> ... is/are <ProperNoun>" -- names the confidential party.
# Case-insensitive on the noun/verb; the target must be a Capitalised or
# ALL-CAPS proper noun (kept case-sensitive) so ordinary lowercase prose such as
# "our client is looking to lease" does not trip it.
# The leading-article group is case-INSENSITIVE so it swallows an
# article that is part of the proper noun itself ("The Related Companies",
# "A Plus Logistics"). The target then falls on the next capitalised token,
# which the negative lookahead still screens for filler words. Without this,
# an article-led company name ("... is The Related Companies") slipped through
# because the lookahead rejected the capitalised "The".
IDENTITY_DISCLOSURE_RE = re.compile(
    r"(?i:\b" + _IDENTITY_NOUN + r"\b)"
    r"[^.?!\n]{0,60}?"
    r"(?i:\b(?:is|are|is\s+called|are\s+called|will\s+be|would\s+be)\b)\s+"
    r"(?:(?i:the|a|an|our)\s+)?"
    r"(?!(?:Not|No|None|Confidential|Undisclosed|Unknown|Pending|Tbd|TBD|"
    r"Available|Still|Ready|Happy|Yes|Thanks|Sorry|Sure|Our|We|An?|The|On|"
    r"Before|After|Looking|Actively|Currently)\b)"
    r"[A-Z][A-Za-z0-9&.'\-]+"
)
# "we represent Acme", "we act for Northstar", "we represent The RMR Group".
REPRESENTATION_DISCLOSURE_RE = re.compile(
    r"(?i:\bwe\s+(?:represent|are\s+representing|rep|act\s+for)\b)\s+"
    r"(?:(?i:the|a|an)\s+)?"
    r"(?!(?:Not|No|None|Confidential|Undisclosed|Unknown|Pending|A|An|The|"
    r"Growing|Great|Strong)\b)"
    r"[A-Z][A-Za-z0-9&.'\-]+"
)
# Disclosing credit / covenant strength ("their credit is strong ...").
CREDIT_DISCLOSURE_RE = re.compile(
    r"(?i:\b(?:credit|covenant)\s+"
    r"(?:rating\s+|profile\s+|strength\s+)?(?:is|are)\b)"
)

# Words that can legitimately follow an identity noun (or a representation verb)
# in a system-generated reply and are Capitalised without being a client name:
# pronouns, determiners, conjunctions, sentence-lead verbs, and the safe-deferral
# vocabulary. A capitalised token in this set is NOT treated as a disclosed name,
# so a safe deferral ("check with my client Before ...", "our client Is reviewing")
# is never falsely flagged.
_IDENTITY_NAME_STOP = (
    r"(?!(?:Not|No|None|Confidential|Undisclosed|Unknown|Pending|Tbd|TBD|"
    r"Available|Still|Ready|Happy|Yes|Thanks|Thank|Sorry|Sure|Our|Ours|We|Us|"
    r"An?|The|On|In|At|Of|For|Before|After|Once|When|While|If|So|And|But|Or|"
    r"Who|Whom|Whose|Will|Would|Is|Are|Was|Were|Has|Have|Had|Can|Could|Should|"
    r"May|Might|Please|Let|Looking|Actively|Currently|Growing|Great|Strong|"
    r"Regarding|Re|I|They|Their|Them|You|Your|He|She|It|Its|This|That|These|"
    r"Those|My|Mine|His|Her|Hers)\b)"
)

# Appositive / possessive naming WITHOUT a copula: "our client Acme Logistics",
# "the tenant, Northstar Robotics,", "Client Northstar loves it". This is the
# natural way an auto-reply names the client while discussing shared specs -- the
# copula IDENTITY_DISCLOSURE_RE ("client IS Acme") does not cover it. The gap
# between the noun and the name may only be a possessive, a comma/dash, and an
# optional article -- never a sentence break -- so cross-sentence prose does not
# stitch an unrelated proper noun onto an identity noun.
IDENTITY_APPOSITION_RE = re.compile(
    r"(?i:\b" + _IDENTITY_NOUN + r"\b)"
    r"(?:'s)?"
    r"\s*[,–-]?\s*"
    r"(?:the\s+|a\s+|an\s+)?"
    + _IDENTITY_NAME_STOP +
    r"[A-Z][A-Za-z0-9&.'\-]+(?:\s+[A-Z][A-Za-z0-9&.'\-]+){0,3}"
)

# Bare representation naming the party without the "we ..." lead the copula-style
# REPRESENTATION_DISCLOSURE_RE requires: "Representing Acme Logistics, ...",
# "acting for Northstar", "on behalf of Delta Manufacturing Corp".
REPRESENTATION_BARE_RE = re.compile(
    r"(?i:\b(?:representing|acting\s+for|on\s+behalf\s+of)\b)\s+"
    r"(?:the\s+|a\s+|an\s+)?"
    + _IDENTITY_NAME_STOP +
    r"[A-Z][A-Za-z0-9&.'\-]+"
)

# --- Fabricated approval / budget / financing details ---------------------
APPROVAL_BUDGET_RE = re.compile(
    r"(?i:"
    r"\bpre[-\s]?approved\b|"
    r"\bfully\s+approved\b|"
    r"\bapproved\s+(?:budget|the\s+budget|this\s+lease|the\s+lease|a\s+lease|"
    r"lease|terms|financing|to\s+spend)\b|"
    # Passive "approved by" ONLY when it is a completed assertion (been/was/were/
    # is/are/already approved by ...). Conditional/future forms use the bare
    # infinitive "be approved by" (would need to be approved by our board) and
    # must NOT fire -- that is routine broker phrasing, not a fabricated approval.
    r"\b(?:been|was|were|is|are|already)\s+approved\s+by\b|"
    r"\bapproved\s+budget\b|"
    r"\bboard\s+has\s+(?:already\s+)?approved\b|"
    r"\bhas\s+(?:already\s+)?approved\s+(?:this|the)\b|"
    r"\bsigned\s+off\s+on\s+(?:terms|the\s+lease|pricing)\b|"
    r"\bbudget\s+of\s+\$|"
    r"\bspend\s+up\s+to\s+\$|"
    r"\bapproved\s+to\s+spend\b|"
    r"\bfinancing\s+is\b[^.?!\n]{0,40}\bsecured\b|"
    r"\bfinancing\s+(?:is\s+)?fully\s+secured\b"
    r")"
)


@dataclass(frozen=True)
class OutboundBodyValidation:
    is_safe: bool
    placeholders: List[str]
    reason: Optional[str] = None


def _looks_like_placeholder_token(inner: str, *, bracketed: bool = False) -> bool:
    """True when the text inside a merge delimiter looks like a merge field.

    ``bracketed`` marks a square-bracket source. Square brackets routinely carry
    legitimate acronyms in broker prose ([TBD], [ASAP], [FYI], [N/A]), so a bare
    all-caps run is NOT sufficient there -- only placeholder vocabulary or a
    structural merge-token shape (underscore-joined field name) counts. The other
    merge syntaxes (%FIELD%, <<X>>, {{X}}, ${X}) never appear in prose, so any
    all-caps token in those is still treated as a real placeholder.
    """
    inner = (inner or "").strip()
    if not inner:
        return False
    normalized = _CAMEL_SPLIT_RE.sub(" ", inner).replace("_", " ")
    if PLACEHOLDER_HINT_RE.search(normalized):
        return True
    if inner.isupper():
        if not bracketed:
            return True
        # Bracketed all-caps: only a merge-field shape (underscore-joined token)
        # counts; bare acronyms like [TBD] / [ASAP] / [FYI] / [N/A] pass through.
        if "_" in inner and re.fullmatch(r"[A-Z0-9_]+", inner):
            return True
    return False


def find_unresolved_placeholders(body: Optional[str]) -> List[str]:
    """Find unresolved template placeholders that must not be sent to brokers.

    Covers every merge-field syntax a real template can carry: square brackets
    ``[NAME]``, double/single curly ``{{name}}`` / ``{name}``, angle
    ``<<Name>>``, percent ``%First%`` and shell ``${name}`` -- not just brackets.
    """
    text = body or ""
    found: List[str] = []
    seen = set()

    def _add(token: str) -> None:
        token = (token or "").strip()
        if token and token not in seen:
            seen.add(token)
            found.append(token)

    for match in UNRESOLVED_BRACKET_PLACEHOLDER_RE.finditer(text):
        token = match.group(0).strip()
        if token.lower() in SAFE_BRACKET_TOKENS:
            continue
        inner = token[1:-1].strip()
        if inner and _looks_like_placeholder_token(inner, bracketed=True):
            _add(token)

    for regex in _NON_BRACKET_PLACEHOLDER_RES:
        for match in regex.finditer(text):
            inner = (match.group(1) or "").strip()
            if inner and _looks_like_placeholder_token(inner):
                _add(match.group(0))

    return found


def contains_unreviewed_scheduling_language(body: Optional[str]) -> bool:
    """Return True when normal outreach copy drifts into tour/LOI scheduling."""
    text = body or ""
    if UNREVIEWED_SCHEDULING_LANGUAGE_RE.search(text):
        return True
    if TOUR_COMMIT_RE.search(text):
        return True
    if SPACE_VISIT_RE.search(text):
        return True
    if TOUR_WHEN_RE.search(text):
        return True
    return False


def contains_confidential_disclosure(body: Optional[str]) -> bool:
    """Return True when a reply names/discloses the confidential client or tenant.

    Guards the two stop conditions for a broker's confidential question:
    revealing client/tenant identity, and disclosing protected credit/covenant.
    """
    text = body or ""
    if IDENTITY_DISCLOSURE_RE.search(text):
        return True
    if REPRESENTATION_DISCLOSURE_RE.search(text):
        return True
    if IDENTITY_APPOSITION_RE.search(text):
        return True
    if REPRESENTATION_BARE_RE.search(text):
        return True
    if CREDIT_DISCLOSURE_RE.search(text):
        return True
    return False


def contains_fabricated_approval_or_budget(body: Optional[str]) -> bool:
    """Return True when a reply asserts approval / budget / financing details."""
    return bool(APPROVAL_BUDGET_RE.search(body or ""))


def validate_outbound_body(
    body: Optional[str],
    *,
    allow_scheduling_language: bool = False,
) -> OutboundBodyValidation:
    placeholders = find_unresolved_placeholders(body)
    if placeholders:
        return OutboundBodyValidation(
            is_safe=False,
            placeholders=placeholders,
            reason=f"Unresolved outbound placeholder(s): {', '.join(placeholders)}",
        )
    if contains_confidential_disclosure(body):
        return OutboundBodyValidation(
            is_safe=False,
            placeholders=[],
            reason=(
                "Outbound reply appears to disclose confidential client/tenant "
                "identity or protected credit details; manual review required"
            ),
        )
    if contains_fabricated_approval_or_budget(body):
        return OutboundBodyValidation(
            is_safe=False,
            placeholders=[],
            reason=(
                "Outbound reply asserts client approval/budget/financing details "
                "that must not be fabricated or disclosed; manual review required"
            ),
        )
    if not allow_scheduling_language and contains_unreviewed_scheduling_language(body):
        return OutboundBodyValidation(
            is_safe=False,
            placeholders=[],
            reason=(
                "Tour/LOI scheduling language is not allowed in normal outreach "
                "or follow-up automation; manual review required"
            ),
        )
    return OutboundBodyValidation(is_safe=True, placeholders=[])
