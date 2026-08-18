"""Certification delivery capture.

The certification lane must exercise the SAME delivery boundary production
uses - otherwise the thing under test is a parallel code path, and proving that
path correct proves nothing about the product. So this transport implements the
identical ``prepare``/``commit``/``discard`` protocol as
``GraphDraftDeliveryTransport`` and differs in exactly one respect: it never
performs a provider call.

Two properties are load-bearing.

**No network, structurally.** There is no HTTP client here to misconfigure, no
base URL, and no credential. A capture run cannot accidentally send because
there is nothing present that could.

**Deterministic, run-scoped identifiers.** Evidence has to be reproducible, so
the same run id and the same envelope must yield the same identifiers; and two
concurrent runs must not collide, so the run id is mixed in. The synthetic
domain is ``.invalid`` (RFC 2606), which is guaranteed never to resolve - an
identifier that leaked into a real header would be inert rather than routable.
"""

from __future__ import annotations

import hashlib
from typing import Any, List, Mapping, Optional

from ..message_transport import (
    DeliveryReceipt,
    OutboundDraft,
    PreparedDelivery,
    ReplyDraftHandle,
)


class CapturingDeliveryTransport:
    """Records the final envelope. Calls nothing."""

    def __init__(self, *, run_id: str, conversations: Any = None) -> None:
        if not run_id:
            raise ValueError("capture requires an exact run id")
        self.run_id = run_id
        self.captured: List[OutboundDraft] = []
        self.discarded: List[OutboundDraft] = []
        self.real_send_calls = 0
        # Canonical conversation state, when the scenario supplies it. The reply
        # lane needs the provider's proposed recipient list in order to filter it
        # down to the safe set; on this lane that list comes from the sealed
        # fixture rather than from a live mailbox.
        self.conversations = conversations
        self.reply_drafts: List[ReplyDraftHandle] = []
        self.applied: List[OutboundDraft] = []

    # -- identity ---------------------------------------------------------

    def _fingerprint(self, draft: OutboundDraft) -> str:
        """Stable over the envelope that would have gone out, and over nothing else.

        Deliberately excludes call ordering and wall-clock time: two identical
        runs of the same scenario must produce identical evidence, or a diff
        between them stops meaning anything.
        """
        material = "\x1f".join(
            [
                self.run_id,
                str(draft.kind.value if hasattr(draft.kind, "value") else draft.kind),
                draft.subject or "",
                draft.body or "",
                ",".join(draft.to),
                ",".join(draft.cc),
                ",".join(draft.bcc),
                draft.reply_to_message_id or "",
                draft.idempotency_key or "",
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    # -- the boundary -----------------------------------------------------

    def prepare(self, draft: OutboundDraft) -> PreparedDelivery:
        fingerprint = self._fingerprint(draft)
        return PreparedDelivery(
            draft=draft,
            provider_message_id=f"captured-{fingerprint}",
            internet_message_id=f"<{fingerprint}@{self.run_id}.certification.invalid>",
            conversation_id=f"captured-conversation-{fingerprint}",
            subject=draft.subject,
        )

    def commit(self, prepared: PreparedDelivery) -> DeliveryReceipt:
        self.captured.append(prepared.draft)
        return DeliveryReceipt(
            status="captured",
            provider_message_id=prepared.provider_message_id,
            internet_message_id=prepared.internet_message_id,
            conversation_id=prepared.conversation_id,
        )

    def discard(self, prepared: PreparedDelivery) -> bool:
        """A suppressed send is evidence too - of a guard that fired correctly."""
        self.discarded.append(prepared.draft)
        return True

    def deliver(self, draft: OutboundDraft) -> DeliveryReceipt:
        return self.commit(self.prepare(draft))

    # -- the reply lane ---------------------------------------------------
    #
    # Mirrors ``GraphDraftDeliveryTransport``'s reply protocol exactly, so the
    # automatic-reply path runs the same branches on both lanes. The provider's
    # "proposed recipients" come from canonical conversation state, which is the
    # whole reason certification can exercise recipient filtering at all without
    # a live mailbox to reply into.

    def create_reply(self, source_message_id: str) -> ReplyDraftHandle:
        proposed = self._proposed_recipients(source_message_id)
        handle = ReplyDraftHandle(
            provider_message_id=f"captured-reply-{self._digest(source_message_id)}",
            raw={
                "id": f"captured-reply-{self._digest(source_message_id)}",
                "toRecipients": [
                    {"emailAddress": {"address": a}} for a in proposed["to"]
                ],
                "ccRecipients": [
                    {"emailAddress": {"address": a}} for a in proposed["cc"]
                ],
            },
            status_code=201,
            ok=True,
        )
        self.reply_drafts.append(handle)
        return handle

    def _digest(self, value: str) -> str:
        return hashlib.sha256(f"{self.run_id}\x1f{value}".encode("utf-8")).hexdigest()[:16]

    def _proposed_recipients(self, source_message_id: str) -> Mapping[str, tuple]:
        """What the provider would have proposed for a reply-all.

        Empty when the scenario supplies no conversation state - which correctly
        drives the product's "no safe recipients remained" branch rather than
        inventing an address to reply to.
        """
        if self.conversations is None:
            return {"to": (), "cc": ()}
        try:
            state = self.conversations.load(source_message_id)
        except Exception:  # noqa: BLE001 - an unavailable fixture is not a recipient
            return {"to": (), "cc": ()}
        target = getattr(state, "reply_target", None)
        envelope = getattr(target, "source_envelope", None) or {}
        if not envelope:
            return {"to": (), "cc": ()}
        # Reply-all, as the provider composes it: the original sender in To, the
        # original To and CC carried into CC. The product then filters this down
        # to the safe set - that filtering is the behavior under test, so this
        # must propose the same starting point a real mailbox would.
        sender = envelope.get("fromEmail") or ""
        carried = tuple(envelope.get("to") or ()) + tuple(envelope.get("cc") or ())
        return {
            "to": (sender,) if sender else (),
            "cc": tuple(dict.fromkeys(a for a in carried if a and a != sender)),
        }

    def apply_reply(self, handle: ReplyDraftHandle, draft: OutboundDraft) -> Any:
        """Record the FINAL envelope - after the caller's recipient filtering."""
        self.applied.append(draft)
        return _CapturedResponse(200)

    def attach(self, handle: ReplyDraftHandle, attachment: Mapping[str, Any]) -> Any:
        return _CapturedResponse(201)

    def send_prepared_draft(self, provider_message_id: str) -> Any:
        """The captured counterpart of the one real send call."""
        for draft in self.applied:
            if draft not in self.captured:
                self.captured.append(draft)
        return _CapturedResponse(202)

    def fetch_draft_recipients(self, provider_message_id: str) -> Optional[Mapping[str, Any]]:
        """Nothing further to fetch: the fixture's audience is authoritative.

        Returning None rather than an empty draft matters. An empty audience is
        a real product state - "no safe recipients remained" - and certification
        must be able to reach it, not paper over it with an invented address.
        """
        return None

    def delete_draft(self, provider_message_id: str) -> bool:
        for draft in self.applied:
            if draft not in self.discarded:
                self.discarded.append(draft)
        return True


class _CapturedResponse:
    """A provider response that never came from a provider."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def json(self) -> dict:
        return {}

    def raise_for_status(self) -> None:
        return None
