"""Strict immutable types for the production certification request.

The request surface is CLOSED. A caller names an approved scenario, a unique run
id, and the exact revision it expects - nothing else. Every concrete identity
(user, client, recipient, spreadsheet, thread, Drive location, oracle) is resolved
at execution time from the bound immutable fixture-config secret.

That is the whole security model of the instrument: if a caller could name a
recipient or an oracle, certification could be pointed at a real person or made to
assert its own success, and no stamp would mean anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Collection, Mapping

# Exactly these three keys. Anything else is a refusal, including fields that look
# harmless - a caller-chosen field is precisely the attack this schema closes.
ALLOWED_REQUEST_KEYS = ("scenarioId", "runId", "expectedRevision")


class CertificationRequestError(ValueError):
    """A closed-schema refusal. Sanitized: names the field, never a value."""


@dataclass(frozen=True)
class CertificationRequest:
    """One validated, single-use certification invocation."""

    scenario_id: str
    run_id: str
    expected_revision: str

    @staticmethod
    def _require_clean_string(payload: Mapping[str, Any], key: str) -> str:
        value = payload[key]
        if not isinstance(value, str):
            raise CertificationRequestError(
                f"{key} must be a string; found {type(value).__name__}"
            )
        if not value:
            raise CertificationRequestError(f"{key} must not be empty")
        if value != value.strip():
            raise CertificationRequestError(
                f"{key} carries leading or trailing whitespace; ids are exact"
            )
        return value

    @classmethod
    def parse(
        cls,
        payload: Any,
        *,
        current_revision: str,
        known_scenario_ids: Collection[str],
        used_run_ids: Collection[str],
    ) -> "CertificationRequest":
        if not isinstance(payload, Mapping):
            raise CertificationRequestError("request must be a JSON object")

        keys = set(payload)
        missing = [key for key in ALLOWED_REQUEST_KEYS if key not in keys]
        if missing:
            raise CertificationRequestError(
                f"request is missing required key(s): {', '.join(sorted(missing))}"
            )
        extra = sorted(keys - set(ALLOWED_REQUEST_KEYS))
        if extra:
            raise CertificationRequestError(
                f"request carries extra key(s): {', '.join(extra)}; a caller may not "
                "choose a user, client, recipient, body, spreadsheet, thread, "
                "resource location, or oracle"
            )

        scenario_id = cls._require_clean_string(payload, "scenarioId")
        run_id = cls._require_clean_string(payload, "runId")
        expected_revision = cls._require_clean_string(payload, "expectedRevision")

        if scenario_id not in known_scenario_ids:
            raise CertificationRequestError(f"unknown scenario {scenario_id}")

        if run_id in used_run_ids:
            raise CertificationRequestError(
                f"run id {run_id} was already used; preparation and claim are single-use"
            )

        if expected_revision != current_revision:
            raise CertificationRequestError(
                "expected revision does not match the running revision; a stamp may "
                "only bind the revision it actually executed against"
            )

        return cls(
            scenario_id=scenario_id,
            run_id=run_id,
            expected_revision=expected_revision,
        )
