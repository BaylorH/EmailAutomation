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
)


class CapturingDeliveryTransport:
    """Records the final envelope. Calls nothing."""

    def __init__(self, *, run_id: str) -> None:
        if not run_id:
            raise ValueError("capture requires an exact run id")
        self.run_id = run_id
        self.captured: List[OutboundDraft] = []
        self.discarded: List[OutboundDraft] = []
        self.real_send_calls = 0

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
