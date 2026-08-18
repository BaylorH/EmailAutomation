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

from .utils import strip_email_quotes, strip_html_tags

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
    # The envelope's own shape. Carried on the draft rather than decided inside
    # the transport so the four lanes converging here cannot drift into four
    # slightly different messages.
    content_type: str = "HTML"
    internet_headers: Tuple[Mapping[str, str], ...] = ()


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


def normalize_graph_body(body: Mapping[str, Any]) -> str:
    """Byte-identical to the inline normalization at processing.py:6446-6448 and
    processing.py:9107-9109, which were exact duplicates of each other.

    Extracted rather than rewritten. The three lines are reproduced exactly -
    including the "Text" default and the unstripped ``.upper()`` - because the point
    is to have ONE implementation, not a better one. Improving it here would be a
    behavior change smuggled into a refactor.
    """
    raw_content = body.get("content", "") or ""
    content_type = (body.get("contentType") or "Text").upper()
    return strip_html_tags(raw_content) if content_type == "HTML" else raw_content


def merge_readback(summary: Mapping[str, Any], readback: Mapping[str, Any]) -> dict:
    """Byte-identical to the inline merge at processing.py:6457 and :9100.

    Readback values fill only keys the scan-time summary lacks or left falsy, so a
    populated summary field always wins.
    """
    return {
        **summary,
        **{k: v for k, v in readback.items() if k not in summary or not summary.get(k)},
    }


def _body_text(raw: Mapping[str, Any]) -> Tuple[str, str]:
    """Return (full_text, text_for_ai) using PRODUCTION's exact pipeline.

    ``full_text`` keeps quoted history because thread reconstruction and audit need
    it. ``text_for_ai`` drops it, because a model shown the operator's own earlier
    question will happily "extract" it back as though the broker had answered - the
    mechanism behind SiteSift re-asking questions already answered.

    This deliberately calls production's ``strip_email_quotes`` rather than a
    smarter HTML-aware stripper. An earlier draft removed <blockquote> elements with
    BeautifulSoup and produced DIFFERENT text_for_ai than production for the same
    message - certification would then have been measuring a quote stripper that
    ships nowhere. Certification must reproduce production faithfully, weaknesses
    included; production's line-marker stripper only fires on a canonical marker
    such as a line matching "On ... wrote:", and that limitation is a PRODUCT
    observation to record, never something the instrument silently repairs.
    """
    full = normalize_graph_body(raw.get("body") or {})
    return full, strip_email_quotes(full)


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


# ---------------------------------------------------------------------------
# the shared delivery boundary
# ---------------------------------------------------------------------------
#
# Phase A proved there was no common boundary to RELOCATE: four independent
# ``requests.post(.../me/messages/{id}/send)`` sites existed across three
# guarded modules. So this boundary is CREATED, and it is deliberately
# TWO-PHASE rather than one ``deliver()`` call.
#
# The reason is the product's own shape. Between building a draft and sending
# it, the caller re-reads campaign eligibility - the last moment at which a send
# can still be called off. Collapsing that into a single ``deliver()`` would
# force the safety decision INSIDE the transport, which is exactly the coupling
# this refactor exists to remove. ``prepare`` stops short of the irreversible
# call; the caller keeps the decision; ``commit`` or ``discard`` follows.
#
# Rendering and safety logic stay with the caller. This module knows how to talk
# to Graph and nothing about who may be mailed.


class DeliveryPreparationError(RuntimeError):
    """A draft could not be prepared into a sendable state."""


@dataclass(frozen=True)
class PreparedDelivery:
    """A built, identified, NOT-yet-sent message.

    Carrying the identifiers before the send is not an optimization: once the
    send succeeds the message must be indexed, and a send that cannot be matched
    back to a thread orphans every future reply. Reading them first means a
    missing identifier fails BEFORE anything irreversible happens.
    """

    draft: "OutboundDraft"
    provider_message_id: str
    internet_message_id: str
    conversation_id: str
    subject: str


class GraphDraftDeliveryTransport:
    """Ordinary production delivery: create draft, attach, identify, send.

    ``request`` and ``retry`` are injected so this module stays pure at import
    and so the caller's existing retry policy is preserved exactly rather than
    reimplemented here with subtly different limits.
    """

    def __init__(
        self,
        *,
        headers: Mapping[str, str],
        base: str = "https://graph.microsoft.com/v1.0",
        request: Any = None,
        retry: Optional[Callable[..., Any]] = None,
        max_retries: int = 3,
        send_max_retries: int = 1,
        headers_provider: Optional[Callable[[], Mapping[str, str]]] = None,
    ) -> None:
        self._headers = dict(headers)
        self._base = base.rstrip("/")
        self._request = request
        self._retry = retry
        self._max_retries = max_retries
        self._send_max_retries = send_max_retries
        self._headers_provider = headers_provider

    # -- plumbing ---------------------------------------------------------

    def _http(self) -> Any:
        if self._request is None:
            import requests  # imported here so building a transport needs no network stack

            self._request = requests
        return self._request

    def _current_headers(self) -> Dict[str, str]:
        if self._headers_provider is not None:
            fresh = self._headers_provider()
            if fresh:
                return dict(fresh)
        return dict(self._headers)

    def _call(self, func: Callable[[], Any], *, max_retries: int, operation: Optional[str] = None) -> Any:
        if self._retry is None:
            return func()
        if operation is None:
            return self._retry(func, max_retries=max_retries)
        return self._retry(func, max_retries=max_retries, operation=operation)

    # -- the boundary -----------------------------------------------------

    def prepare(self, draft: "OutboundDraft") -> PreparedDelivery:
        http = self._http()
        headers = self._current_headers()
        message = graph_message_payload(draft)

        create = self._call(
            lambda: http.post(f"{self._base}/me/messages", headers=headers, json=message, timeout=30),
            max_retries=self._max_retries,
        )
        try:
            draft_id = create.json()["id"]
        except Exception as exc:  # noqa: BLE001 - a draft with no id cannot be sent or cleaned up
            raise DeliveryPreparationError(f"Graph returned no draft id: {exc}") from exc

        for attachment in draft.attachments:
            self._call(
                lambda att=attachment: http.post(
                    f"{self._base}/me/messages/{draft_id}/attachments",
                    headers=headers,
                    json=att,
                    timeout=30,
                ),
                max_retries=self._max_retries,
            )

        identified = self._call(
            lambda: http.get(
                f"{self._base}/me/messages/{draft_id}",
                headers=headers,
                params={"$select": "internetMessageId,conversationId,subject,toRecipients"},
                timeout=30,
            ),
            max_retries=self._max_retries,
        )
        data = identified.json() or {}
        internet_message_id = data.get("internetMessageId")
        if not internet_message_id:
            raise DeliveryPreparationError(
                "Graph returned no internetMessageId; a message that cannot be "
                "indexed would orphan every future reply, so it is not sent"
            )

        return PreparedDelivery(
            draft=draft,
            provider_message_id=draft_id,
            internet_message_id=internet_message_id,
            conversation_id=data.get("conversationId") or "",
            subject=data.get("subject") or draft.subject,
        )

    def send_prepared_draft(self, provider_message_id: str) -> Any:
        """THE send call. Every lane routed to this boundary passes through here.

        Kept as one named function so the AST send-site sweep has exactly one
        site to find, and so a future lane cannot converge "almost" here.
        """
        http = self._http()
        return self._call(
            lambda: http.post(
                f"{self._base}/me/messages/{provider_message_id}/send",
                headers=self._current_headers(),
                timeout=30,
            ),
            max_retries=self._send_max_retries,
            operation="graph_send",
        )

    def commit(self, prepared: PreparedDelivery) -> DeliveryReceipt:
        self.send_prepared_draft(prepared.provider_message_id)
        return DeliveryReceipt(
            status="sent",
            provider_message_id=prepared.provider_message_id,
            internet_message_id=prepared.internet_message_id,
            conversation_id=prepared.conversation_id,
        )

    def discard(self, prepared: PreparedDelivery) -> bool:
        """Best-effort cleanup of a draft that lost eligibility before sending."""
        return self.delete_draft(prepared.provider_message_id)

    # -- the reply lane ---------------------------------------------------
    #
    # A reply is not a fresh draft: the provider creates it, proposing its own
    # recipient list, and the caller then filters that list down to the safe
    # set. So the transport hands the raw draft back unedited and takes the
    # caller's final decision on the way through ``apply_reply``.

    def create_reply(
        self,
        source_message_id: str,
        *,
        accepted: Tuple[int, ...] = (200, 201),
    ) -> "ReplyDraftHandle":
        """``accepted`` belongs to the caller.

        The converging lanes do not agree on what counts as a created draft -
        one accepts 202 where another does not - and quietly unifying them here
        would change a lane's behavior under the guise of a refactor.
        """
        http = self._http()
        response = self._call(
            lambda: http.post(
                f"{self._base}/me/messages/{source_message_id}/createReplyAll",
                headers=self._current_headers(),
                timeout=30,
            ),
            max_retries=self._max_retries,
        )
        status_code = getattr(response, "status_code", None)
        ok = isinstance(status_code, int) and status_code in accepted
        raw: Dict[str, Any] = {}
        if ok:
            try:
                raw = response.json() or {}
            except Exception:  # noqa: BLE001 - an unparseable draft is a failed draft
                raw = {}
        return ReplyDraftHandle(
            provider_message_id=str(raw.get("id") or ""),
            raw=raw,
            status_code=status_code,
            ok=ok,
        )

    def apply_reply(self, handle: "ReplyDraftHandle", draft: "OutboundDraft") -> Any:
        http = self._http()
        return self._call(
            lambda: http.patch(
                f"{self._base}/me/messages/{handle.provider_message_id}",
                headers=self._current_headers(),
                json=reply_patch_payload(draft),
                timeout=30,
            ),
            max_retries=self._max_retries,
        )

    def attach(self, handle: "ReplyDraftHandle", attachment: Mapping[str, Any]) -> Any:
        http = self._http()
        return self._call(
            lambda: http.post(
                f"{self._base}/me/messages/{handle.provider_message_id}/attachments",
                headers=self._current_headers(),
                json=attachment,
                timeout=30,
            ),
            max_retries=self._max_retries,
        )

    def fetch_draft_recipients(self, provider_message_id: str) -> Optional[Mapping[str, Any]]:
        """Re-read a sparse reply draft's audience.

        Graph sometimes returns only an id from createReplyAll even though the
        saved draft has the computed To/CC. Returning ``None`` means "nothing
        further to fetch" - which is what a transport whose draft is already
        authoritative should say, rather than inventing an audience.
        """
        http = self._http()
        response = self._call(
            lambda: http.get(
                f"{self._base}/me/messages/{provider_message_id}",
                headers=self._current_headers(),
                params={"$select": "id,toRecipients,ccRecipients"},
                timeout=30,
            ),
            max_retries=self._max_retries,
        )
        if not response or getattr(response, "status_code", None) != 200:
            return None
        return response.json() or {}

    def delete_draft(self, provider_message_id: str) -> bool:
        if not provider_message_id:
            return False
        http = self._http()
        try:
            self._call(
                lambda: http.delete(
                    f"{self._base}/me/messages/{provider_message_id}",
                    headers=self._current_headers(),
                    timeout=30,
                ),
                max_retries=self._max_retries,
            )
            return True
        except Exception:  # noqa: BLE001 - an abandoned draft is untidy, never unsafe
            return False

    def deliver(self, draft: "OutboundDraft") -> DeliveryReceipt:
        return self.commit(self.prepare(draft))


def graph_message_payload(draft: "OutboundDraft") -> Dict[str, Any]:
    """Render an OutboundDraft into the Graph message body.

    Kept here rather than at each call site so the four lanes Task 6 and 7A-7D
    converge cannot drift into four slightly different envelopes.
    """
    payload: Dict[str, Any] = {
        "subject": draft.subject,
        "body": {"contentType": draft.content_type, "content": draft.body},
        "toRecipients": [{"emailAddress": {"address": address}} for address in draft.to],
    }
    if draft.cc:
        payload["ccRecipients"] = [{"emailAddress": {"address": a}} for a in draft.cc]
    if draft.bcc:
        payload["bccRecipients"] = [{"emailAddress": {"address": a}} for a in draft.bcc]
    if draft.internet_headers:
        payload["internetMessageHeaders"] = [dict(h) for h in draft.internet_headers]
    return payload


@dataclass(frozen=True)
class ReplyDraftHandle:
    """A provider-created reply draft, before the caller has decided anything.

    ``raw`` carries the provider's OWN recipient lists verbatim. That matters:
    the reply lane derives its final safe recipients by filtering what the
    provider proposed, and that filtering is the caller's business, not the
    transport's. Handing the raw draft back unedited is what keeps the decision
    where it belongs.
    """

    provider_message_id: str
    raw: Mapping[str, Any]
    status_code: Optional[int]
    ok: bool


def reply_patch_payload(draft: "OutboundDraft") -> Dict[str, Any]:
    """The body/recipient update applied to a provider-created reply draft."""
    return {
        "body": {"contentType": draft.content_type, "content": draft.body},
        "toRecipients": [{"emailAddress": {"address": a}} for a in draft.to],
        "ccRecipients": [{"emailAddress": {"address": a}} for a in draft.cc],
    }

