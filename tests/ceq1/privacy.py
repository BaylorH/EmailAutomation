"""Mechanical privacy and synthetic-provenance validation for CE-Q1 fixtures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Mapping

from tests.ceq1.contracts import sha256_json


SCANNER_NONCLAIM = (
    "This mechanical scanner does not detect arbitrary copied prose or arbitrary "
    "numbers; it recognizes only the declared synthetic identities and the closed "
    "credential, path, mailbox, identifier, and clock rules listed here. PDF "
    "decoded-text privacy remains unverified until the Task 7 verified parser "
    "receipt is bound; a caller-supplied decoded-text map cannot produce a privacy "
    "gate pass."
)

SCANNER_RULE_SPECS: dict[str, object] = {
    "CEQ_PRIV_ABSOLUTE_PATH": {"kind": "absolute-path", "version": 5},
    "CEQ_PRIV_ARTIFACT_ID": {"kind": "logical-artifact-id", "version": 1},
    "CEQ_PRIV_CLOCK_RANGE": {"kind": "strict-utc-z-clock", "version": 1},
    "CEQ_PRIV_CREDENTIAL": {"kind": "credential-shape", "version": 1},
    "CEQ_PRIV_FILE_URI": {"kind": "file-uri", "version": 1},
    "CEQ_PRIV_FORBIDDEN_TOKEN": {"kind": "seeded-token", "version": 2},
    "CEQ_PRIV_JSON_SECRET_FIELD": {"kind": "secret-json-field", "version": 1},
    "CEQ_PRIV_NON_INVALID_MAILBOX": {"kind": "non-invalid-mailbox", "version": 3},
    "CEQ_PRIV_OBFUSCATED_IDENTITY": {"kind": "encoded-identity", "version": 1},
    "CEQ_PRIV_OPAQUE_BINARY": {"kind": "opaque-binary", "version": 4},
    "CEQ_PRIV_PRODUCTION_ID": {"kind": "production-shaped-id", "version": 4},
    "CEQ_PRIV_RAW_MESSAGE_ID": {"kind": "raw-message-id", "version": 1},
    "CEQ_PRIV_TREE_LINK": {"kind": "tree-link", "version": 1},
    "CEQ_PRIV_TREE_SPECIAL": {"kind": "tree-special-file", "version": 4},
    "CEQ_PRIV_UNDECLARED_IDENTITY": {"kind": "undeclared-identity", "version": 4},
}
SCANNER_RULE_HASHES = {
    rule_id: sha256_json(specification)
    for rule_id, specification in sorted(SCANNER_RULE_SPECS.items())
}

_PROVENANCE_KEYS = frozenset(
    {
        "schemaVersion",
        "syntheticTemplateVersion",
        "generationMethod",
        "rawCustomerSourcesAccessed",
        "fictionalPeople",
        "fictionalProperties",
        "fictionalDomains",
        "fictionalMailboxes",
        "syntheticClock",
        "scannerRules",
        "scannerNonClaim",
        "independentReviewStatus",
        "independentReviewerRole",
        "reviewedArtifactSetSha256",
        "reviewedCommit",
    }
)
_CLOCK_KEYS = frozenset({"start", "end"})
_RULE_KEYS = frozenset({"ruleId", "sha256"})
_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LOWER_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_LOWER_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.invalid$")
_MAILBOX = re.compile(
    r"^[a-z0-9](?:[a-z0-9.!#$%&'*+/=?^_`{|}~-]*[a-z0-9])?@"
    r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.invalid$"
)
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?"
    r"(?=$|[^A-Za-z0-9.-]|\.+(?:$|[^A-Za-z0-9.-]))"
)
_OBFUSCATED_IDENTITY_CHARACTERS = ("＠", "\u200b", "\u200c", "\u200d", "\ufeff")
_UTF8_OBFUSCATED_IDENTITY_SIGNATURES = tuple(
    character.encode("utf-8") for character in _OBFUSCATED_IDENTITY_CHARACTERS
)
_TIMESTAMP_CANDIDATE = re.compile(
    r"\b\d{4}-\d{1,2}-\d{1,2}T\d{1,2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\b"
)
_STRICT_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_POSIX_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9._~%+/-])/{1,}"
    r"(?:[^\x00\s\"'<>/]+(?:/[^\x00\s\"'<>/]+)*)?"
    r"(?=$|[\s\"'<>),;])"
)
_PDF_RAW_POSIX_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9._~%+/-])/{1,}"
    r"[^\x00\s\"'<>/]+/[^\x00\s\"'<>/]+"
    r"(?:/[^\x00\s\"'<>/]+)*"
    r"(?=$|[\s\"'<>),;])"
)
_URI_SCHEME_SUFFIX = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:$")
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?:^|(?<=[\s\"'=:,(]))(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/][^\\/\s]+)"
)
_SECRET_KEYS = frozenset(
    {
        "apikey",
        "accesstoken",
        "authtoken",
        "clientsecret",
        "credential",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "token",
    }
)
_PERSON_IDENTITY_KEYS = frozenset({"brokername", "sendername"})
_PROPERTY_IDENTITY_KEYS = frozenset({"propertyaddress"})
_PLATFORM_ID_KEYS = frozenset(
    {
        "bucket",
        "bucketid",
        "documentid",
        "driveid",
        "drivefileid",
        "fileid",
        "messageid",
        "projectid",
        "sheetid",
        "spreadsheetid",
        "tenantid",
        "threadid",
    }
)
_PLATFORM_ID_KEY_SUFFIXES = (
    "conversationid",
    "drivefileid",
    "driveitemid",
    "messageid",
    "threadid",
)
_MAX_TREE_FILE_BYTES = 4 * 1024 * 1024


class PrivacyViolation(ValueError):
    """A deliberately redacted privacy failure."""


def _raise(rule_id: str, artifact_id: str) -> None:
    raise PrivacyViolation(rule_id, artifact_id)


def _checked_artifact_id(value: object) -> str:
    if type(value) is not str or _ARTIFACT_ID.fullmatch(value) is None:
        _raise("CEQ_PRIV_ARTIFACT_ID", "invalid-artifact-id")
    return value


def _exact_dict(value: object, keys: frozenset[str], label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an exact JSON object")
    if set(value) != keys:
        raise ValueError(f"{label} has a non-closed schema")
    return value


def _string_list(value: object, label: str, *, nonempty: bool = True) -> tuple[str, ...]:
    if type(value) is not list:
        raise TypeError(f"{label} must be a list")
    if not all(type(item) is str and item for item in value):
        raise TypeError(f"{label} must contain non-empty strings")
    result = tuple(value)
    if result != tuple(sorted(set(result))):
        raise ValueError(f"{label} must be sorted and unique")
    if nonempty and not result:
        raise ValueError(f"{label} must not be empty")
    return result


def _parse_timestamp(value: object) -> datetime:
    if type(value) is not str or _STRICT_TIMESTAMP.fullmatch(value) is None:
        raise ValueError("timestamp must use strict UTC-Z form")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ValueError("timestamp is invalid") from None
    return parsed.replace(tzinfo=timezone.utc)


def _strict_json_bytes(data: object, artifact_id: str) -> object:
    artifact = _checked_artifact_id(artifact_id)
    invalid = type(data) is not bytes or data.startswith(b"\xef\xbb\xbf")
    parsed: object = None
    if not invalid:
        try:
            text = data.decode("utf-8", "strict")

            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError("duplicate")
                    result[key] = value
                return result

            parsed = json.loads(
                text,
                object_pairs_hook=pairs,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            invalid = True
    if invalid or type(parsed) is not dict:
        raise PrivacyViolation("CEQ_JSON_INVALID", artifact)
    return parsed


@dataclass(frozen=True, slots=True)
class GenerationProvenance:
    schemaVersion: int
    syntheticTemplateVersion: str
    generationMethod: str
    rawCustomerSourcesAccessed: bool
    fictionalPeople: tuple[str, ...]
    fictionalProperties: tuple[str, ...]
    fictionalDomains: tuple[str, ...]
    fictionalMailboxes: tuple[str, ...]
    syntheticClockStart: str
    syntheticClockEnd: str
    scannerRules: tuple[tuple[str, str], ...]
    scannerNonClaim: str
    independentReviewStatus: str
    independentReviewerRole: str | None
    reviewedArtifactSetSha256: str | None
    reviewedCommit: str | None
    gateApproved: bool

    def to_mapping(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schemaVersion,
            "syntheticTemplateVersion": self.syntheticTemplateVersion,
            "generationMethod": self.generationMethod,
            "rawCustomerSourcesAccessed": self.rawCustomerSourcesAccessed,
            "fictionalPeople": list(self.fictionalPeople),
            "fictionalProperties": list(self.fictionalProperties),
            "fictionalDomains": list(self.fictionalDomains),
            "fictionalMailboxes": list(self.fictionalMailboxes),
            "syntheticClock": {
                "start": self.syntheticClockStart,
                "end": self.syntheticClockEnd,
            },
            "scannerRules": [
                {"ruleId": rule_id, "sha256": digest}
                for rule_id, digest in self.scannerRules
            ],
            "scannerNonClaim": self.scannerNonClaim,
            "independentReviewStatus": self.independentReviewStatus,
            "independentReviewerRole": self.independentReviewerRole,
            "reviewedArtifactSetSha256": self.reviewedArtifactSetSha256,
            "reviewedCommit": self.reviewedCommit,
        }


def validate_generation_provenance(
    value: object,
    *,
    artifact_set_sha256: str | None = None,
    current_commit: str | None = None,
) -> GenerationProvenance:
    if type(value) is GenerationProvenance:
        value = value.to_mapping()
    item = _exact_dict(value, _PROVENANCE_KEYS, "generation provenance")
    if type(item["schemaVersion"]) is not int or item["schemaVersion"] != 1:
        raise ValueError("generation provenance schemaVersion must be 1")
    if item["syntheticTemplateVersion"] != "ceq1-synthetic-v1":
        raise ValueError("synthetic template version is not approved")
    if item["generationMethod"] != "newly_authored_synthetic_template":
        raise ValueError("generation method is not newly authored synthetic")
    if type(item["rawCustomerSourcesAccessed"]) is not bool:
        raise TypeError("rawCustomerSourcesAccessed must be boolean")
    if item["rawCustomerSourcesAccessed"]:
        raise ValueError("raw customer source access is forbidden")
    people = _string_list(item["fictionalPeople"], "fictionalPeople")
    properties = _string_list(item["fictionalProperties"], "fictionalProperties")
    domains = _string_list(item["fictionalDomains"], "fictionalDomains")
    mailboxes = _string_list(item["fictionalMailboxes"], "fictionalMailboxes")
    if any(_DOMAIN.fullmatch(domain) is None for domain in domains):
        raise ValueError("fictionalDomains must be lowercase .invalid domains")
    if any(_MAILBOX.fullmatch(mailbox) is None for mailbox in mailboxes):
        raise ValueError("fictionalMailboxes must be lowercase .invalid mailboxes")
    if any(mailbox.rsplit("@", 1)[1] not in domains for mailbox in mailboxes):
        raise ValueError("fictional mailbox domain is undeclared")
    clock = _exact_dict(item["syntheticClock"], _CLOCK_KEYS, "syntheticClock")
    clock_start = _parse_timestamp(clock["start"])
    clock_end = _parse_timestamp(clock["end"])
    if clock_start > clock_end:
        raise ValueError("synthetic clock is inverted")
    rules = item["scannerRules"]
    if type(rules) is not list:
        raise TypeError("scannerRules must be a list")
    parsed_rules: list[tuple[str, str]] = []
    for rule in rules:
        record = _exact_dict(rule, _RULE_KEYS, "scanner rule")
        rule_id = record["ruleId"]
        digest = record["sha256"]
        if type(rule_id) is not str or type(digest) is not str:
            raise TypeError("scanner rule fields must be strings")
        parsed_rules.append((rule_id, digest))
    expected_rules = tuple(sorted(SCANNER_RULE_HASHES.items()))
    if tuple(parsed_rules) != expected_rules:
        raise ValueError("scannerRules do not match the closed implementation receipts")
    if item["scannerNonClaim"] != SCANNER_NONCLAIM:
        raise ValueError("scanner non-claim is not exact")
    status = item["independentReviewStatus"]
    if type(status) is not str or status not in {"pending", "approved", "rejected"}:
        raise ValueError("independent review status is outside the closed enum")
    role = item["independentReviewerRole"]
    reviewed_digest = item["reviewedArtifactSetSha256"]
    reviewed_commit = item["reviewedCommit"]
    if status == "pending":
        if any(value is not None for value in (role, reviewed_digest, reviewed_commit)):
            raise ValueError("pending review cannot claim reviewer bindings")
    else:
        if role != "independent_fixture_privacy_reviewer":
            raise ValueError("independent reviewer role is not approved")
        if type(reviewed_digest) is not str or _LOWER_DIGEST.fullmatch(reviewed_digest) is None:
            raise ValueError("reviewed artifact digest is invalid")
        if type(reviewed_commit) is not str or _LOWER_COMMIT.fullmatch(reviewed_commit) is None:
            raise ValueError("reviewed commit is invalid")
    if artifact_set_sha256 is not None and (
        type(artifact_set_sha256) is not str
        or _LOWER_DIGEST.fullmatch(artifact_set_sha256) is None
    ):
        raise ValueError("current artifact digest is invalid")
    if current_commit is not None and (
        type(current_commit) is not str or _LOWER_COMMIT.fullmatch(current_commit) is None
    ):
        raise ValueError("current commit is invalid")
    gate_approved = (
        status == "approved"
        and artifact_set_sha256 is not None
        and current_commit is not None
        and reviewed_digest == artifact_set_sha256
        and reviewed_commit == current_commit
    )
    return GenerationProvenance(
        schemaVersion=1,
        syntheticTemplateVersion="ceq1-synthetic-v1",
        generationMethod="newly_authored_synthetic_template",
        rawCustomerSourcesAccessed=False,
        fictionalPeople=people,
        fictionalProperties=properties,
        fictionalDomains=domains,
        fictionalMailboxes=mailboxes,
        syntheticClockStart=clock["start"],
        syntheticClockEnd=clock["end"],
        scannerRules=tuple(parsed_rules),
        scannerNonClaim=SCANNER_NONCLAIM,
        independentReviewStatus=status,
        independentReviewerRole=role,
        reviewedArtifactSetSha256=reviewed_digest,
        reviewedCommit=reviewed_commit,
        gateApproved=gate_approved,
    )


def validate_generation_provenance_bytes(
    data: bytes,
    *,
    artifact_id: str,
    artifact_set_sha256: str | None = None,
    current_commit: str | None = None,
) -> GenerationProvenance:
    return validate_generation_provenance(
        _strict_json_bytes(data, artifact_id),
        artifact_set_sha256=artifact_set_sha256,
        current_commit=current_commit,
    )


def _clock_bounds(provenance: GenerationProvenance) -> tuple[datetime, datetime]:
    return (
        _parse_timestamp(provenance.syntheticClockStart),
        _parse_timestamp(provenance.syntheticClockEnd),
    )


def _contains_posix_absolute_path(
    text: str,
    pattern: re.Pattern[str] = _POSIX_ABSOLUTE_PATH,
) -> bool:
    for match in pattern.finditer(text):
        if (
            match.group(0) == "/"
            and match.start() > 0
            and match.end() < len(text)
            and text[match.start() - 1].isspace()
            and text[match.end()].isspace()
            and text[: match.start()].strip()
            and text[match.end() :].strip()
        ):
            continue
        if match.group(0).startswith("//") and _URI_SCHEME_SUFFIX.search(
            text[: match.start()]
        ):
            continue
        return True
    return False


def _scan_raw_applicable_text(
    text: str,
    artifact_id: str,
    provenance: GenerationProvenance,
    *,
    posix_path_pattern: re.Pattern[str],
) -> None:
    lowered = text.lower()
    if "file://" in lowered:
        _raise("CEQ_PRIV_FILE_URI", artifact_id)
    if _contains_posix_absolute_path(
        text, posix_path_pattern
    ) or _WINDOWS_ABSOLUTE_PATH.search(text):
        _raise("CEQ_PRIV_ABSOLUTE_PATH", artifact_id)
    if re.search(r"\bprojects/[A-Za-z0-9._-]+/databases/", text) or re.search(
        r"(?:\bgs://[A-Za-z0-9._-]+|\b(?:AAMk|AAQk|AQMk)[A-Za-z0-9+/=_-]{16,})",
        text,
    ):
        _raise("CEQ_PRIV_PRODUCTION_ID", artifact_id)
    if re.search(r"\b(?:graph-message-id:|gmail-message-id:)[A-Za-z0-9_-]{16,}", text):
        _raise("CEQ_PRIV_RAW_MESSAGE_ID", artifact_id)
    if re.search(
        r"(?:\bsk-[A-Za-z0-9_-]{16,}|\bghp_[A-Za-z0-9]{16,}|"
        r"\bAKIA[A-Z0-9]{16}\b|\bAIza[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]+PRIVATE KEY-----)",
        text,
    ):
        _raise("CEQ_PRIV_CREDENTIAL", artifact_id)
    if "%40" in lowered or any(
        character in text for character in _OBFUSCATED_IDENTITY_CHARACTERS
    ):
        _raise("CEQ_PRIV_OBFUSCATED_IDENTITY", artifact_id)
    declared_mailboxes = set(provenance.fictionalMailboxes)
    for mailbox_match in _EMAIL.finditer(text):
        mailbox = mailbox_match.group(0)
        domain = mailbox.rsplit("@", 1)[1]
        if not domain.endswith(".invalid"):
            _raise("CEQ_PRIV_NON_INVALID_MAILBOX", artifact_id)
        if mailbox not in declared_mailboxes:
            _raise("CEQ_PRIV_UNDECLARED_IDENTITY", artifact_id)
    clock_start, clock_end = _clock_bounds(provenance)
    for match in _TIMESTAMP_CANDIDATE.finditer(text):
        candidate = match.group(0)
        invalid = _STRICT_TIMESTAMP.fullmatch(candidate) is None
        parsed = None
        if not invalid:
            try:
                parsed = datetime.strptime(candidate, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                invalid = True
        if invalid or parsed is None or not (clock_start <= parsed <= clock_end):
            _raise("CEQ_PRIV_CLOCK_RANGE", artifact_id)


def _scan_text(text: str, artifact_id: str, provenance: GenerationProvenance) -> None:
    _scan_raw_applicable_text(
        text,
        artifact_id,
        provenance,
        posix_path_pattern=_POSIX_ABSOLUTE_PATH,
    )


def _scan_pdf_raw_patterns(
    data: bytes,
    artifact_id: str,
    provenance: GenerationProvenance,
) -> None:
    if any(
        signature in data for signature in _UTF8_OBFUSCATED_IDENTITY_SIGNATURES
    ):
        _raise("CEQ_PRIV_OBFUSCATED_IDENTITY", artifact_id)
    _scan_raw_applicable_text(
        data.decode("latin-1"),
        artifact_id,
        provenance,
        posix_path_pattern=_PDF_RAW_POSIX_ABSOLUTE_PATH,
    )


def _scan_json_value(
    value: object,
    *,
    artifact_id: str,
    provenance: GenerationProvenance,
    parent_key: str | None = None,
) -> None:
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("scan_json object keys must be strings")
            _scan_text(key, artifact_id, provenance)
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in _SECRET_KEYS:
                _raise("CEQ_PRIV_JSON_SECRET_FIELD", artifact_id)
            _scan_json_value(
                item,
                artifact_id=artifact_id,
                provenance=provenance,
                parent_key=normalized,
            )
        return
    if type(value) is list:
        for item in value:
            _scan_json_value(
                item,
                artifact_id=artifact_id,
                provenance=provenance,
                parent_key=parent_key,
            )
        return
    if type(value) is str:
        _scan_text(value, artifact_id, provenance)
        is_platform_id = parent_key is not None and (
            parent_key in _PLATFORM_ID_KEYS
            or parent_key.endswith(_PLATFORM_ID_KEY_SUFFIXES)
        )
        if is_platform_id and not value.lower().startswith(("ceq1-", "synthetic-")):
            _raise("CEQ_PRIV_PRODUCTION_ID", artifact_id)
        if parent_key in _PERSON_IDENTITY_KEYS:
            if value not in provenance.fictionalPeople:
                _raise("CEQ_PRIV_UNDECLARED_IDENTITY", artifact_id)
        if parent_key in _PROPERTY_IDENTITY_KEYS:
            if value not in provenance.fictionalProperties:
                _raise("CEQ_PRIV_UNDECLARED_IDENTITY", artifact_id)
        return
    if value is None or type(value) in (bool, int, float):
        return
    raise TypeError("scan_json accepts only exact JSON values")


def _checked_provenance(value: object) -> GenerationProvenance:
    if type(value) is GenerationProvenance:
        return validate_generation_provenance(value.to_mapping())
    return validate_generation_provenance(value)


def scan_bytes(
    data: bytes,
    *,
    artifact_id: str,
    provenance: GenerationProvenance | Mapping[str, object],
    forbidden_tokens: Mapping[str, bytes] | None = None,
    require_json: bool = False,
) -> tuple[()]:
    artifact = _checked_artifact_id(artifact_id)
    parsed_provenance = _checked_provenance(provenance)
    if type(data) is not bytes:
        raise TypeError("scan_bytes data must be bytes")
    if type(require_json) is not bool:
        raise TypeError("require_json must be an exact bool")
    if forbidden_tokens is not None:
        if type(forbidden_tokens) is not dict:
            raise TypeError("forbidden_tokens must be an exact mapping")
        for token in forbidden_tokens.values():
            if type(token) is not bytes or not token:
                raise TypeError("forbidden token values must be non-empty bytes")
            if token in data:
                _raise("CEQ_PRIV_FORBIDDEN_TOKEN", artifact)
    parsed_json = _strict_json_bytes(data, artifact) if require_json else None
    invalid_utf8 = False
    try:
        text = data.decode("utf-8", "strict")
    except UnicodeDecodeError:
        invalid_utf8 = True
        text = ""
    if invalid_utf8 or "\x00" in text:
        _raise("CEQ_PRIV_OPAQUE_BINARY", artifact)
    _scan_text(text, artifact, parsed_provenance)
    stripped = text.lstrip()
    if require_json or stripped.startswith(("{", "[")):
        parsed = parsed_json if require_json else _strict_json_bytes(data, artifact)
        _scan_json_value(
            parsed,
            artifact_id=artifact,
            provenance=parsed_provenance,
        )
    return ()


def scan_json(
    value: object,
    *,
    artifact_id: str,
    provenance: GenerationProvenance | Mapping[str, object],
) -> tuple[()]:
    artifact = _checked_artifact_id(artifact_id)
    parsed_provenance = _checked_provenance(provenance)
    _scan_json_value(value, artifact_id=artifact, provenance=parsed_provenance)
    return ()


def _tree_stat_signature(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _tree_file_bytes(
    directory_fd: int,
    name: str,
    observed: os.stat_result,
    artifact: str,
) -> bytes:
    descriptor = None
    failed = False
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > _MAX_TREE_FILE_BYTES
            or (before.st_dev, before.st_ino, before.st_mode)
            != (observed.st_dev, observed.st_ino, observed.st_mode)
        ):
            failed = True
            data = b""
        else:
            chunks = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 65536))
                if not chunk:
                    failed = True
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1) != b"":
                failed = True
            data = b"".join(chunks)
            after = os.fstat(descriptor)
            if (
                remaining
                or len(data) != before.st_size
                or _tree_stat_signature(before) != _tree_stat_signature(after)
            ):
                failed = True
    except OSError:
        failed = True
        data = b""
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                failed = True
    if failed:
        _raise("CEQ_PRIV_TREE_SPECIAL", artifact)
    return data


def _scan_tree_directory(
    directory_fd: int,
    prefix: PurePosixPath,
    *,
    artifact: str,
    provenance: GenerationProvenance,
    forbidden_tokens: Mapping[str, bytes] | None,
    decoded: dict[str, str],
    seen_decoded: set[str],
) -> None:
    failed = False
    try:
        before_directory = os.fstat(directory_fd)
        names = tuple(sorted(os.listdir(directory_fd)))
        if not stat.S_ISDIR(before_directory.st_mode):
            failed = True
    except OSError:
        failed = True
        before_directory = None
        names = ()
    if failed:
        _raise("CEQ_PRIV_TREE_SPECIAL", artifact)
    for name in names:
        if type(name) is not str or not name or name in {".", ".."} or "/" in name:
            _raise("CEQ_PRIV_TREE_SPECIAL", artifact)
        relative_path = prefix / name
        relative = relative_path.as_posix()
        _scan_text(relative, artifact, provenance)
        metadata_failed = False
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            metadata_failed = True
            metadata = None
        if metadata_failed or metadata is None:
            _raise("CEQ_PRIV_TREE_SPECIAL", artifact)
        if stat.S_ISLNK(metadata.st_mode):
            _raise("CEQ_PRIV_TREE_LINK", artifact)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = None
            child_failed = False
            try:
                child_fd = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW
                    | os.O_NONBLOCK
                    | os.O_DIRECTORY,
                    dir_fd=directory_fd,
                )
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino, opened.st_mode) != (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                ):
                    child_failed = True
            except OSError:
                child_failed = True
            if child_failed or child_fd is None:
                if child_fd is not None:
                    try:
                        os.close(child_fd)
                    except OSError:
                        pass
                _raise("CEQ_PRIV_TREE_SPECIAL", artifact)
            child_close_failed = False
            try:
                _scan_tree_directory(
                    child_fd,
                    relative_path,
                    artifact=artifact,
                    provenance=provenance,
                    forbidden_tokens=forbidden_tokens,
                    decoded=decoded,
                    seen_decoded=seen_decoded,
                )
            finally:
                try:
                    os.close(child_fd)
                except OSError:
                    child_close_failed = True
            if child_close_failed:
                _raise("CEQ_PRIV_TREE_SPECIAL", artifact)
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            _raise("CEQ_PRIV_TREE_SPECIAL", artifact)
        data = _tree_file_bytes(directory_fd, name, metadata, artifact)
        if relative_path.suffix.lower() == ".pdf":
            if forbidden_tokens is not None:
                for token in forbidden_tokens.values():
                    if type(token) is not bytes or not token:
                        raise TypeError("forbidden token values must be non-empty bytes")
                    if token in data:
                        _raise("CEQ_PRIV_FORBIDDEN_TOKEN", artifact)
            _scan_pdf_raw_patterns(data, artifact, provenance)
            _raise("CEQ_PRIV_OPAQUE_BINARY", artifact)
        scan_bytes(
            data,
            artifact_id=artifact,
            provenance=provenance,
            forbidden_tokens=forbidden_tokens,
            require_json=relative_path.suffix.lower() == ".json",
        )
    closing_failed = False
    try:
        closing_names = tuple(sorted(os.listdir(directory_fd)))
        after_directory = os.fstat(directory_fd)
    except OSError:
        closing_failed = True
        closing_names = ()
        after_directory = None
    if (
        closing_failed
        or before_directory is None
        or after_directory is None
        or closing_names != names
        or _tree_stat_signature(before_directory) != _tree_stat_signature(after_directory)
    ):
        _raise("CEQ_PRIV_TREE_SPECIAL", artifact)


def scan_tree(
    root: Path,
    *,
    artifact_id: str,
    provenance: GenerationProvenance | Mapping[str, object],
    forbidden_tokens: Mapping[str, bytes] | None = None,
    decoded_text_by_path: Mapping[str, str] | None = None,
) -> tuple[()]:
    artifact = _checked_artifact_id(artifact_id)
    parsed_provenance = _checked_provenance(provenance)
    if not isinstance(root, Path):
        _raise("CEQ_PRIV_TREE_SPECIAL", artifact)
    if decoded_text_by_path is not None and type(decoded_text_by_path) is not dict:
        raise TypeError("decoded_text_by_path must be an exact mapping")
    decoded = {} if decoded_text_by_path is None else dict(decoded_text_by_path)
    seen_decoded: set[str] = set()
    root_fd = None
    failed = False
    try:
        root_fd = os.open(
            os.fspath(root),
            os.O_RDONLY
            | os.O_CLOEXEC
            | os.O_NOFOLLOW
            | os.O_NONBLOCK
            | os.O_DIRECTORY,
        )
        root_metadata = os.fstat(root_fd)
        if not stat.S_ISDIR(root_metadata.st_mode):
            failed = True
    except OSError:
        failed = True
    if failed or root_fd is None:
        if root_fd is not None:
            try:
                os.close(root_fd)
            except OSError:
                pass
        _raise("CEQ_PRIV_TREE_SPECIAL", artifact)
    root_close_failed = False
    try:
        _scan_tree_directory(
            root_fd,
            PurePosixPath(),
            artifact=artifact,
            provenance=parsed_provenance,
            forbidden_tokens=forbidden_tokens,
            decoded=decoded,
            seen_decoded=seen_decoded,
        )
    finally:
        try:
            os.close(root_fd)
        except OSError:
            root_close_failed = True
    if root_close_failed:
        _raise("CEQ_PRIV_TREE_SPECIAL", artifact)
    if set(decoded) != seen_decoded:
        _raise("CEQ_PRIV_TREE_SPECIAL", artifact)
    return ()
