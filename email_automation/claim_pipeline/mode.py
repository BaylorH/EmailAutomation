"""Fail-closed mode selection for claim-pipeline rollout stages."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple


class ClaimPipelineMode(str, Enum):
    OFF = "off"
    REPLAY = "replay"
    SHADOW = "shadow"
    ENFORCE = "enforce"


@dataclass(frozen=True)
class PipelineScope:
    tenant_id: str
    campaign_id: str

    def __post_init__(self) -> None:
        if not str(self.tenant_id or "").strip():
            raise ValueError("pipeline scope tenant_id must be non-empty")
        if not str(self.campaign_id or "").strip():
            raise ValueError("pipeline scope campaign_id must be non-empty")


def parse_pipeline_mode(value: Optional[str | ClaimPipelineMode]) -> ClaimPipelineMode:
    if isinstance(value, ClaimPipelineMode):
        return value
    cleaned = str(value or "").strip().lower()
    try:
        return ClaimPipelineMode(cleaned)
    except ValueError:
        return ClaimPipelineMode.OFF


@dataclass(frozen=True)
class PipelineGate:
    mode: ClaimPipelineMode = ClaimPipelineMode.OFF
    allowed_scopes: Tuple[PipelineScope, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_scopes", tuple(self.allowed_scopes))
        if not all(isinstance(scope, PipelineScope) for scope in self.allowed_scopes):
            raise ValueError("allowed_scopes must contain PipelineScope values")

    def _allows_scope(self, tenant_id: str, campaign_id: str) -> bool:
        return PipelineScope(tenant_id, campaign_id) in self.allowed_scopes

    def allows_replay(self, tenant_id: str, campaign_id: str) -> bool:
        return (
            self.mode is ClaimPipelineMode.REPLAY
            and self._allows_scope(tenant_id, campaign_id)
        )

    def allows_shadow(self, tenant_id: str, campaign_id: str) -> bool:
        return (
            self.mode is ClaimPipelineMode.SHADOW
            and self._allows_scope(tenant_id, campaign_id)
        )

    def allows_enforcement(self, tenant_id: str, campaign_id: str) -> bool:
        return (
            self.mode is ClaimPipelineMode.ENFORCE
            and self._allows_scope(tenant_id, campaign_id)
        )
