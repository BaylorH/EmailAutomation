"""Single Microsoft Graph final-send adapter and orchestration boundary.

Draft creation and attachment preparation stay outside this module.  The
callable injected into :class:`GraphFinalSendAdapter` must perform exactly one
irreversible Graph ``/send`` request and must not contain its own retry loop.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, Mapping

import requests

from .effect_gateway import (
    EffectGateway,
    EffectGatewayConfig,
    EffectReceipt,
    ProviderEffectRequest,
    ProviderEffectResult,
    ReceiptState,
    RetryableProviderError,
    TerminalProviderError,
    UncertainProviderOutcomeError,
)


_PROCESS_RUN_ID = f"process-{uuid.uuid4().hex}"


@dataclass(frozen=True)
class GraphFinalSendOutcome:
    receipt: EffectReceipt
    provider_called: bool = False

    @property
    def accepted(self) -> bool:
        """Whether Graph definitely accepted the send.

        A hashed provider reference is persisted only after a 2xx response.
        It therefore remains positive acceptance evidence even if the final
        SUCCEEDED receipt transition needs reconciliation.
        """

        return (
            self.provider_called
            and bool(self.receipt.provider_reference)
            and self.receipt.state
            in {
                ReceiptState.PROVIDER_ACCEPTED,
                ReceiptState.SUCCEEDED,
                ReceiptState.RECONCILIATION_REQUIRED,
            }
        )

    @property
    def already_applied(self) -> bool:
        """A prior invocation sent; this invocation must reconcile, not index."""

        return (
            not self.provider_called
            and bool(self.receipt.provider_reference)
            and self.receipt.state
            in {
                ReceiptState.PROVIDER_ACCEPTED,
                ReceiptState.SUCCEEDED,
                ReceiptState.RECONCILIATION_REQUIRED,
            }
        )


class GraphFinalSendAdapter:
    """Adapt one raw Microsoft Graph final-send call to the gateway contract."""

    def __init__(
        self,
        send_call: Callable[[], Any],
        *,
        provider_reference: str,
    ) -> None:
        if not callable(send_call):
            raise TypeError("send_call must be callable")
        if (
            not isinstance(provider_reference, str)
            or not provider_reference
            or provider_reference != provider_reference.strip()
        ):
            raise ValueError(
                "provider_reference must be an exact non-empty string"
            )
        self._send_call = send_call
        self._provider_reference = provider_reference
        self._called = False

    @property
    def called(self) -> bool:
        return self._called

    def execute(self, _request: ProviderEffectRequest) -> ProviderEffectResult:
        self._called = True
        response = self._send_call()
        status_code = getattr(response, "status_code", None)
        if not isinstance(status_code, int):
            raise UncertainProviderOutcomeError(
                "Graph final-send returned no HTTP status"
            )
        if 200 <= status_code < 300:
            return ProviderEffectResult(self._provider_reference)
        if status_code == 429:
            raise RetryableProviderError(
                "Graph throttled final-send before accepting it"
            )
        if status_code == 408:
            raise UncertainProviderOutcomeError(
                "Graph final-send outcome is uncertain after HTTP 408"
            )
        if 400 <= status_code < 500:
            raise TerminalProviderError(
                f"Graph rejected final-send with HTTP {status_code}"
            )
        raise UncertainProviderOutcomeError(
            f"Graph final-send outcome is uncertain after HTTP {status_code}"
        )


def resolve_effect_run_id() -> str:
    """Return the scheduler-run identity without using user data."""

    explicit = os.getenv("SITESIFT_EFFECT_RUN_ID")
    if explicit:
        return explicit
    github_run = os.getenv("GITHUB_RUN_ID")
    if github_run:
        attempt = os.getenv("GITHUB_RUN_ATTEMPT", "1")
        return f"github-{github_run}-{attempt}"
    return _PROCESS_RUN_ID


def _default_receipt_store():
    from .clients import _fs
    from .firestore_effect_store import FirestoreEffectReceiptStore

    return FirestoreEffectReceiptStore(_fs)


def execute_graph_final_send(
    *,
    user_id: str,
    client_id: str,
    run_id: str | None = None,
    effect_type: str,
    effect_key: str,
    content: Mapping[str, Any],
    provider_reference: str,
    send_call: Callable[[], Any],
    receipt_store=None,
    config: EffectGatewayConfig | None = None,
) -> GraphFinalSendOutcome:
    """Guard and execute one Graph final send.

    The authority read performed by the receipt store is immediately adjacent
    to ``send_call`` inside :class:`EffectGateway`.
    """

    request = ProviderEffectRequest.create(
        run_id=run_id or resolve_effect_run_id(),
        user_id=user_id,
        authority_client_id=client_id,
        provider="graph",
        effect_type=effect_type,
        effect_key=effect_key,
        content=content,
    )
    store = receipt_store or _default_receipt_store()
    adapter = GraphFinalSendAdapter(
        send_call,
        provider_reference=provider_reference,
    )
    receipt = EffectGateway(
        store,
        {"graph": adapter},
        config or EffectGatewayConfig.from_env(),
    ).execute(request)
    return GraphFinalSendOutcome(
        receipt=receipt,
        provider_called=adapter.called,
    )


def _post_graph_draft_send(
    *,
    draft_id: str,
    headers: Mapping[str, str],
    base_url: str,
    timeout: int,
    http_post: Callable[..., Any],
):
    """Perform the one raw irreversible call owned by this module."""

    return http_post(
        f"{base_url}/me/messages/{draft_id}/send",
        headers=dict(headers),
        timeout=timeout,
    )


def execute_graph_draft_final_send(
    *,
    user_id: str,
    client_id: str,
    effect_type: str,
    effect_key: str,
    content: Mapping[str, Any],
    draft_id: str,
    headers: Mapping[str, str],
    run_id: str | None = None,
    base_url: str = "https://graph.microsoft.com/v1.0",
    timeout: int = 30,
    http_post: Callable[..., Any] | None = None,
    receipt_store=None,
    config: EffectGatewayConfig | None = None,
) -> GraphFinalSendOutcome:
    """Guard one Graph draft ``/send`` with the production adapter."""

    return execute_graph_final_send(
        user_id=user_id,
        client_id=client_id,
        run_id=run_id,
        effect_type=effect_type,
        effect_key=effect_key,
        content=content,
        provider_reference=draft_id,
        send_call=partial(
            _post_graph_draft_send,
            draft_id=draft_id,
            headers=dict(headers),
            base_url=base_url,
            timeout=timeout,
            http_post=http_post or requests.post,
        ),
        receipt_store=receipt_store,
        config=config,
    )
