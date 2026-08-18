"""Canonical message acquisition boundary.

Task 3 of the production automation certification plan. This module isolates
ACQUISITION - how an inbound message and its conversation state are obtained -
from everything downstream. Matching, authority, AI, Sheet, event, and reply policy
stay exactly where they are; only the source of the bytes moves here.

The load-bearing property, and the reason the whole certification program can mean
anything: a Graph-backed source and an approved fixture source MUST produce
byte-equal canonical state for the same logical message. That is guaranteed
structurally rather than by discipline - both source kinds call the SAME
``canonicalize_inbound_message`` and ``canonicalize_conversation_state`` functions
below, and neither can canonicalize on its own. If the two lanes could disagree on
any field, a capability stamp earned through the fixture lane would say nothing
about the Graph lane.

**This module is deliberately PURE.** It imports no provider client and must never
import ``email_automation.clients``, which constructs ``firestore.Client()`` and
``openai.OpenAI(...)`` at module scope. Keeping it pure is what lets the
certification tests collect without a credential. ``utils`` is safe to import: it
holds no client construction, and reusing its ``strip_html_tags`` is required
rather than optional - duplicating body parsing here would let the certification
lane and production diverge in exactly the way this module exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Protocol, Tuple

from bs4 import BeautifulSoup

from .utils import strip_html_tags

# Elements that carry quoted history rather than what the broker just wrote.
QUOTE_ELEMENTS = ("blockquote",)
QUOTE_CLASS_MARKERS = ("gmail_quote", "moz-cite-prefix", "OutlookMessageHeader")


class DeliveryKind(str, Enum):
    NEW = "new"
    REPLY = "reply"
    REPLY_ALL = "reply_all"


@dataclass(frozen=True)
class HydratedInboundMessage:
    summary: Mapping[str, Any]
    full_text: str
    text_for_ai: str
    source_envelope: Mapping[str, Any]
    internet_headers: Tuple[Mapping[str, str], ...]
    attachment_snapshot: Tuple[Mapping[str, Any], ...]


class InboundMessageSource(Protocol):
    def hydrate(self, summary: Mapping[str, Any]) -> HydratedInboundMessage: ...


@dataclass(frozen=True)
class CanonicalConversationState:
    reply_target: HydratedInboundMessage
    prior_messages: Tuple[HydratedInboundMessage, ...]
    sent_receipts: Tuple[Mapping[str, str], ...]


class ConversationStateSource(Protocol):
    def load(self, conversation_key: str) -> CanonicalConversationState: ...


@dataclass(frozen=True)
class OutboundDraft:
    kind: DeliveryKind
    subject: str
    body: str
    to: Tuple[str, ...]
    cc: Tuple[str, ...]
    bcc: Tuple[str, ...]
    reply_to_message_id: Optional[str] = None
    attachments: Tuple[Mapping[str, Any], ...] = ()
    idempotency_key: str = ""


@dataclass(frozen=True)
class DeliveryReceipt:
    status: str
    provider_message_id: str
    internet_message_id: str
    conversation_id: str


class OutboundDraftTransport(Protocol):
    def deliver(self, draft: OutboundDraft) -> DeliveryReceipt: ...


# ---------------------------------------------------------------------------
# canonicalization - the single shared implementation both lanes must use
# ---------------------------------------------------------------------------


def _addresses(entries: Any) -> Tuple[str, ...]:
    """Extract lowercase addresses from a Graph recipient collection.

    Accepts the several shapes Graph actually returns: a list of recipient objects,
    a single recipient object, or an already-flat list of strings.
    """
    if not entries:
        return ()
    if isinstance(entries, Mapping):
        entries = [entries]
    found = []
    for entry in entries:
        if isinstance(entry, str):
            address = entry
        elif isinstance(entry, Mapping):
            address = (entry.get("emailAddress") or {}).get("address") or entry.get("address") or ""
        else:
            address = ""
        address = (address or "").strip().lower()
        if address:
            found.append(address)
    return tuple(found)


def _headers(raw: Mapping[str, Any]) -> Tuple[Mapping[str, str], ...]:
    entries = raw.get("internetMessageHeaders") or ()
    normalized = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or "")
        value = str(entry.get("value") or "")
        if name:
            normalized.append({"name": name, "value": value})
    return tuple(normalized)


def _header_value(headers: Tuple[Mapping[str, str], ...], wanted: str) -> str:
    """Case-insensitive lookup. Header names are not case significant in RFC 5322."""
    for entry in headers:
        if entry["name"].lower() == wanted.lower():
            return entry["value"].strip()
    return ""


def _body_text(raw: Mapping[str, Any]) -> Tuple[str, str]:
    """Return (full_text, text_for_ai).

    ``full_text`` keeps quoted history because thread reconstruction and audit need
    it. ``text_for_ai`` drops it, because a model shown the operator's own earlier
    question will happily "extract" it back as though the broker had answered - the
    exact mechanism behind SiteSift re-asking questions that were already answered.
    """
    body = raw.get("body") or {}
    content = str(body.get("content") or "")
    content_type = str(body.get("contentType") or "Text").strip().upper()

    if content_type != "HTML":
        full = content.strip()
        return full, full

    full = strip_html_tags(content).strip()

    soup = BeautifulSoup(content, "html.parser")
    for element_name in QUOTE_ELEMENTS:
        for element in soup.find_all(element_name):
            element.decompose()
    for marker in QUOTE_CLASS_MARKERS:
        for element in soup.find_all(attrs={"class": marker}):
            element.decompose()
    # Outlook marks the quoted original with this id on the wrapping div.
    for element in soup.find_all(attrs={"id": "divRplyFwdMsg"}):
        element.decompose()

    without_quotes = strip_html_tags(str(soup)).strip()
    return full, without_quotes


def _attachments(raw: Mapping[str, Any]) -> Tuple[Mapping[str, Any], ...]:
    entries = raw.get("attachments") or ()
    return tuple(dict(entry) for entry in entries if isinstance(entry, Mapping))


def canonicalize_inbound_message(
    raw: Mapping[str, Any],
    summary: Optional[Mapping[str, Any]] = None,
) -> HydratedInboundMessage:
    """THE canonical projection. Both lanes call exactly this."""
    headers = _headers(raw)
    full_text, text_for_ai = _body_text(raw)
    references = tuple(
        token for token in _header_value(headers, "References").split() if token
    )

    envelope: Mapping[str, Any] = {
        "graphMessageId": str(raw.get("id") or ""),
        "internetMessageId": str(raw.get("internetMessageId") or ""),
        "conversationId": str(raw.get("conversationId") or ""),
        "subject": str(raw.get("subject") or ""),
        "receivedDateTime": str(raw.get("receivedDateTime") or ""),
        "hasAttachments": bool(raw.get("hasAttachments")),
        "fromEmail": (_addresses(raw.get("from")) or ("",))[0],
        "replyTo": _addresses(raw.get("replyTo")),
        "to": _addresses(raw.get("toRecipients")),
        "cc": _addresses(raw.get("ccRecipients")),
        "inReplyTo": _header_value(headers, "In-Reply-To"),
        "references": references,
    }

    return HydratedInboundMessage(
        summary=dict(summary or {"id": envelope["graphMessageId"]}),
        full_text=full_text,
        text_for_ai=text_for_ai,
        source_envelope=envelope,
        internet_headers=headers,
        attachment_snapshot=_attachments(raw),
    )


def canonicalize_conversation_state(raw: Mapping[str, Any]) -> CanonicalConversationState:
    """THE canonical conversation projection. Both lanes call exactly this."""
    reply_target = canonicalize_inbound_message(raw.get("reply_target") or {})
    prior = tuple(
        canonicalize_inbound_message(entry)
        for entry in (raw.get("prior_messages") or ())
        if isinstance(entry, Mapping)
    )
    receipts = tuple(
        {str(key): str(value) for key, value in entry.items()}
        for entry in (raw.get("sent_receipts") or ())
        if isinstance(entry, Mapping)
    )
    return CanonicalConversationState(
        reply_target=reply_target,
        prior_messages=prior,
        sent_receipts=receipts,
    )


# ---------------------------------------------------------------------------
# sources - acquisition differs, canonicalization does not
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphInboundMessageSource:
    """Ordinary production. Acquires from Microsoft Graph through an injected call.

    ``request`` is injected rather than imported so this module stays pure and so a
    test can supply a transcript without patching global HTTP.
    """

    request: Callable[..., Mapping[str, Any]]
    headers: Mapping[str, str]

    def hydrate(self, summary: Mapping[str, Any]) -> HydratedInboundMessage:
        raw = self.request(summary, self.headers)
        return canonicalize_inbound_message(raw, summary)


@dataclass(frozen=True)
class FixtureInboundMessageSource:
    """Certification. Acquires from an approved fixture snapshot and NEVER from Graph.

    ``request`` is optional and exists only so a caller can pass an exploding stub to
    prove no Graph call happens on this lane. It is never invoked here.
    """

    snapshot: Mapping[str, Any]
    request: Optional[Callable[..., Mapping[str, Any]]] = None

    def hydrate(self, summary: Mapping[str, Any]) -> HydratedInboundMessage:
        return canonicalize_inbound_message(self.snapshot, summary)


@dataclass(frozen=True)
class GraphConversationStateSource:
    request: Callable[..., Mapping[str, Any]]
    headers: Mapping[str, str]

    def load(self, conversation_key: str) -> CanonicalConversationState:
        raw = self.request(conversation_key, self.headers)
        return canonicalize_conversation_state(raw)


@dataclass(frozen=True)
class FixtureConversationStateSource:
    snapshot: Mapping[str, Any]

    def load(self, conversation_key: str) -> CanonicalConversationState:
        return canonicalize_conversation_state(self.snapshot)
