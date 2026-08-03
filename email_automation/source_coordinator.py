"""Pure contracts for B1 exact-source coordination."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


SOURCE_COORDINATOR_MODE_ENV = "SITESIFT_SOURCE_COORDINATOR_MODE"
MAX_SOURCE_ALIAS_BYTES = 1024
_SOURCE_ALIAS_KEY_DOMAIN = "source-alias-v2"
_SOURCE_ALIAS_TYPES = {"graph", "internet_message_id"}


class CoordinatorMode(str, Enum):
    DISABLED = "disabled"
    SHADOW = "shadow"
    ENFORCED = "enforced"


class SourceCoordinatorError(RuntimeError):
    code = "source_coordinator_error"


class SourceCoordinatorRetryable(SourceCoordinatorError):
    code = "source_coordinator_retryable"


class SourceCoordinatorAmbiguous(SourceCoordinatorError):
    code = "source_coordinator_ambiguous"


class SourceCoordinatorConflict(SourceCoordinatorError):
    code = "source_coordinator_conflict"


class SourceCoordinatorConfigError(SourceCoordinatorError):
    code = "source_coordinator_config"


@dataclass(frozen=True)
class SourceAlias:
    alias_type: str
    value: str
    key: str = ""


def resolve_source_coordinator_mode(environ: Mapping[str, str]) -> CoordinatorMode:
    value = environ.get(SOURCE_COORDINATOR_MODE_ENV)
    if type(value) is not str:
        return CoordinatorMode.DISABLED
    if value == CoordinatorMode.SHADOW.value:
        return CoordinatorMode.SHADOW
    if value == CoordinatorMode.ENFORCED.value:
        return CoordinatorMode.ENFORCED
    return CoordinatorMode.DISABLED


def canonical_json_hash(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise SourceCoordinatorConfigError(
            "value is not canonical finite JSON"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _contains_control_character(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def normalize_source_alias(alias_type: str, value: str) -> SourceAlias:
    if type(alias_type) is not str or alias_type not in _SOURCE_ALIAS_TYPES:
        raise SourceCoordinatorConfigError("source alias type is unsupported")
    if type(value) is not str:
        raise SourceCoordinatorConfigError("source alias value must be a string")
    if _contains_control_character(value):
        raise SourceCoordinatorConfigError("source alias contains a control character")

    normalized = value.strip()
    if alias_type == "internet_message_id":
        while len(normalized) >= 2 and normalized[0] == "<" and normalized[-1] == ">":
            normalized = normalized[1:-1].strip()

    if not normalized:
        raise SourceCoordinatorConfigError("source alias value is empty")
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SourceCoordinatorConfigError(
            "source alias value is not valid UTF-8"
        ) from exc
    if len(encoded) > MAX_SOURCE_ALIAS_BYTES:
        raise SourceCoordinatorConfigError("source alias value exceeds byte limit")
    return SourceAlias(alias_type=alias_type, value=normalized)


def source_alias_key(user_id: str, alias: SourceAlias) -> str:
    if type(user_id) is not str or not user_id:
        raise SourceCoordinatorConfigError("user id must be a non-empty string")
    if not isinstance(alias, SourceAlias):
        raise SourceCoordinatorConfigError("source alias is invalid")

    canonical = normalize_source_alias(alias.alias_type, alias.value)
    if (
        canonical.alias_type != alias.alias_type
        or canonical.value != alias.value
    ):
        raise SourceCoordinatorConfigError("source alias is not canonical")

    try:
        encoded = "\0".join(
            (_SOURCE_ALIAS_KEY_DOMAIN, user_id, alias.alias_type, alias.value)
        ).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SourceCoordinatorConfigError(
            "source alias key input is not valid UTF-8"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()
