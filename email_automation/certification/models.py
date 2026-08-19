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

import hmac
import re
from dataclasses import dataclass, replace
from typing import Any, Collection, Dict, Mapping

from email_automation.certification.canonical_json import canonical_digest

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


# ---------------------------------------------------------------------------
# One-use run authorization
# ---------------------------------------------------------------------------

AUTHORIZATION_FIELDS = (
    "scenario_id",
    "run_id",
    "source_revision",
    "image_digest",
    "certification_service",
    "certification_revision",
    "production_candidate_revision",
    "caller_identity_digest",
    "fixture_config_secret_version",
    "fixture_config_digest",
    "scenario_registry_digest",
    "launch_class",
    "input_producer_kind",
    "canonical_input_digest",
    "input_producer_artifact_digest",
    "authorization_expires_at",
)

# A producer names WHERE the canonical input came from. An unknown producer
# means an unreviewed input path, so the set is closed rather than validated.
ALLOWED_INPUT_PRODUCER_KINDS = ("backend_registry_v1", "frontend_functions_adapter_v1")

# Exactly YYYY-MM-DDTHH:MM:SSZ. Not "RFC3339-ish": `+00:00` denotes the same
# instant as `Z` but produces different canonical bytes and therefore a
# different digest, so permitting both would make one authorization have two
# valid digests.
_EXPIRY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class AuthorizationInvalid(ValueError):
    """A refusal. Names the field, never a value."""


@dataclass(frozen=True)
class RunAuthorization:
    """The immutable, single-use grant a certification run executes under.

    ``authorization_digest`` is lowercase SHA-256 over SiteSift-canonical-JSON-v1
    bytes of every OTHER field, keyed by the exact dataclass field names. It is
    recomputed and compared before every prepare/claim/recovery transition; the
    stored value is checked, never trusted.
    """

    scenario_id: str
    run_id: str
    source_revision: str
    image_digest: str
    certification_service: str
    certification_revision: str
    production_candidate_revision: str
    caller_identity_digest: str
    fixture_config_secret_version: str
    fixture_config_digest: str
    scenario_registry_digest: str
    launch_class: str
    input_producer_kind: str
    canonical_input_digest: str
    input_producer_artifact_digest: str
    authorization_expires_at: str
    authorization_digest: str = ""

    # -- construction ------------------------------------------------------

    @staticmethod
    def _clean(field_name: str, value: Any) -> str:
        # bool first: it is an int subclass, and `True` must not pass as a value.
        if isinstance(value, bool) or not isinstance(value, str):
            raise AuthorizationInvalid(
                f"{field_name} must be a string; found {type(value).__name__}"
            )
        if not value:
            raise AuthorizationInvalid(f"{field_name} must not be empty")
        if value != value.strip():
            raise AuthorizationInvalid(
                f"{field_name} carries leading or trailing whitespace; fields are exact"
            )
        return value

    @classmethod
    def create(cls, **fields: Any) -> "RunAuthorization":
        absent = [name for name in AUTHORIZATION_FIELDS if name not in fields]
        if absent:
            raise AuthorizationInvalid(
                f"authorization is missing required field(s): {', '.join(sorted(absent))}"
            )
        unknown = sorted(set(fields) - set(AUTHORIZATION_FIELDS))
        if unknown:
            raise AuthorizationInvalid(
                f"authorization carries unknown field(s): {', '.join(unknown)}"
            )
        cleaned = {name: cls._clean(name, fields[name]) for name in AUTHORIZATION_FIELDS}

        if not _EXPIRY_PATTERN.match(cleaned["authorization_expires_at"]):
            raise AuthorizationInvalid(
                "authorization_expires_at must be exactly YYYY-MM-DDTHH:MM:SSZ "
                "with no fractional seconds and no offset alias"
            )
        if cleaned["input_producer_kind"] not in ALLOWED_INPUT_PRODUCER_KINDS:
            raise AuthorizationInvalid(
                "input_producer_kind must be one of "
                f"{', '.join(ALLOWED_INPUT_PRODUCER_KINDS)}"
            )

        authorization = cls(**cleaned)
        return replace(authorization, authorization_digest=authorization.compute_digest())

    # -- digest ------------------------------------------------------------

    def digest_preimage(self) -> Dict[str, str]:
        """Every field except the digest, keyed by exact dataclass field name."""
        return {name: getattr(self, name) for name in AUTHORIZATION_FIELDS}

    def compute_digest(self) -> str:
        return canonical_digest(self.digest_preimage())

    def verify(self) -> None:
        if not hmac.compare_digest(self.authorization_digest, self.compute_digest()):
            raise AuthorizationInvalid(
                "authorization_digest does not match the authorization it claims to cover"
            )

    # -- storage -----------------------------------------------------------

    def to_stored(self) -> Dict[str, str]:
        record = self.digest_preimage()
        record["authorization_digest"] = self.authorization_digest
        return record

    @classmethod
    def from_stored(cls, record: Mapping[str, Any]) -> "RunAuthorization":
        """Rebuild from durable state, revalidating on the way in.

        `create` recomputes the digest from the stored scalars, so a record whose
        field was edited without a matching digest fails here. A record edited
        WITH a matching digest is self-consistent and survives this step by
        design -- `assert_matches_request` is what refuses it, because a forger
        can recompute a digest but cannot change the request that arrived.
        """
        missing = [name for name in AUTHORIZATION_FIELDS if name not in record]
        if missing:
            raise AuthorizationInvalid(
                f"stored authorization is missing: {', '.join(sorted(missing))}"
            )
        extra = sorted(set(record) - set(AUTHORIZATION_FIELDS) - {"authorization_digest"})
        if extra:
            raise AuthorizationInvalid(
                f"stored authorization carries unknown field(s): {', '.join(extra)}"
            )
        rebuilt = cls.create(**{name: record[name] for name in AUTHORIZATION_FIELDS})
        stored_digest = record.get("authorization_digest", "")
        if not isinstance(stored_digest, str) or not hmac.compare_digest(
            stored_digest, rebuilt.authorization_digest
        ):
            raise AuthorizationInvalid(
                "stored authorization_digest disagrees with its own fields"
            )
        return rebuilt

    # -- binding -----------------------------------------------------------

    def assert_matches_request(self, request: "CertificationRequest") -> None:
        """Bind the grant to the request that actually arrived."""
        for field_name, mine, theirs in (
            ("scenarioId", self.scenario_id, request.scenario_id),
            ("runId", self.run_id, request.run_id),
            ("expectedRevision", self.source_revision, request.expected_revision),
        ):
            if not hmac.compare_digest(mine, theirs):
                raise AuthorizationInvalid(
                    f"authorization {field_name} does not match the request"
                )
