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


@dataclass(frozen=True)
class OutboundBodyValidation:
    is_safe: bool
    placeholders: List[str]
    reason: Optional[str] = None


def find_unresolved_placeholders(body: Optional[str]) -> List[str]:
    """Find unresolved template placeholders that must not be sent to brokers."""
    text = body or ""
    found: List[str] = []
    seen = set()
    for match in UNRESOLVED_BRACKET_PLACEHOLDER_RE.finditer(text):
        token = match.group(0).strip()
        token_lower = token.lower()
        if token_lower in SAFE_BRACKET_TOKENS:
            continue
        inner = token[1:-1].strip()
        if not inner:
            continue
        looks_like_placeholder = (
            inner.isupper()
            or " " not in inner and PLACEHOLDER_HINT_RE.search(inner)
            or PLACEHOLDER_HINT_RE.search(inner.replace("_", " "))
        )
        if looks_like_placeholder and token not in seen:
            seen.add(token)
            found.append(token)
    return found


def validate_outbound_body(body: Optional[str]) -> OutboundBodyValidation:
    placeholders = find_unresolved_placeholders(body)
    if placeholders:
        return OutboundBodyValidation(
            is_safe=False,
            placeholders=placeholders,
            reason=f"Unresolved outbound placeholder(s): {', '.join(placeholders)}",
        )
    return OutboundBodyValidation(is_safe=True, placeholders=[])
