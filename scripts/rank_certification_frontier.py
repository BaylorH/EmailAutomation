#!/usr/bin/env python3
"""Deterministic next-capability selection for production automation certification.

Selects EXACTLY ONE active business capability plus AT MOST ONE independent
instrumentation/authority blocker, and recomputes stamp invalidation after a
deployed diff.

Design constraints this script exists to hold:

* It never reads the general backlog. The backlog is durable memory, not a work
  queue, and letting it influence selection is the exact failure this program was
  created to stop.
* It never mutates a tracked product file. Recording a stamp inside the repository
  would change the source SHA that stamp certifies. Optional output goes to an
  explicit non-repository path; durable state lives in the private certification
  ledger and the sanitized Brain checkpoint.
* Unknown input fails closed. An unrecognised or absent identity field means the
  deployed system changed in a way this program cannot reason about, so it must
  refuse rather than silently preserve a stamp.

Usage:

    python3 -B scripts/rank_certification_frontier.py \
        --frontier docs/release-safety/production-certification/frontier.json \
        --stamps /private/path/stamps.json \
        --previous-identity /private/path/previous.json \
        --current-identity /private/path/current.json \
        --changed-paths "a/b.py,c/d.py" \
        --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

VALID_VERDICTS = ("PASS", "FAIL", "INSTRUMENT_BLOCKED", "NOT_TESTED")
EXPECTED_CAPABILITY_SCENARIO_COUNT = 91

# A logical alias may never carry a concrete resource identity.
CONCRETE_IDENTIFIER_MARKERS = ("@", "://", "docs.google", "drive.google", "sitesiftai")


class ValidationError(Exception):
    """A fail-closed refusal. The message is sanitized and safe to print."""


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def load_json(path: Path, label: str) -> Any:
    if not path.is_file():
        raise ValidationError(f"{label} not found: {path}")
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:  # sanitized: position only, never content
        raise ValidationError(
            f"{label} is not valid JSON (line {exc.lineno}, column {exc.colno})"
        ) from None


def resolve_manifest_path(frontier: Dict[str, Any], frontier_path: Path) -> Path:
    declared = frontier.get("approvedManifestPath")
    if not isinstance(declared, str) or not declared:
        raise ValidationError("frontier.approvedManifestPath must be a non-empty string")
    candidate = Path(declared)
    if candidate.is_absolute():
        return candidate
    for base in (REPO_ROOT, frontier_path.resolve().parent):
        resolved = (base / candidate).resolve()
        if resolved.is_file():
            return resolved
    return (REPO_ROOT / candidate).resolve()


# --------------------------------------------------------------------------
# manifest validation
# --------------------------------------------------------------------------


def validate_manifest(manifest: Dict[str, Any], frontier: Dict[str, Any]) -> None:
    """Validate the approved planning manifest on its own.

    Order matters. Each check is ordered so the most specific defect is reported
    rather than a downstream symptom of it.
    """
    capability_order: List[str] = frontier["capabilityOrder"]
    known_capabilities = set(capability_order)

    capabilities = manifest.get("capabilities")
    definitions = manifest.get("scenarioDefinitions")
    if not isinstance(capabilities, list) or not isinstance(definitions, list):
        raise ValidationError("manifest must declare capabilities and scenarioDefinitions lists")

    declared_ids = [capability.get("id") for capability in capabilities]
    if declared_ids != capability_order:
        raise ValidationError(
            "manifest capability order does not match the frontier capabilityOrder"
        )

    required_fields = (
        "scenarioId",
        "capabilityId",
        "logicalFixtureKey",
        "oracleProjectionKey",
        "expectedVerdict",
        "capabilityStamp",
        "inputProducerKind",
        "launchClass",
        "modelRepeatCount",
        "requiresHumanReview",
        "naturalnessRubricVersion",
        "requiredEffects",
        "forbiddenEffects",
    )

    # 1. per-scenario shape. A missing field must never inherit a default.
    for scenario in definitions:
        scenario_id = scenario.get("scenarioId", "<unnamed>")
        for field in required_fields:
            if field not in scenario:
                raise ValidationError(
                    f"scenario {scenario_id} is missing required field {field}; "
                    "a runtime scenario may not inherit an omitted field"
                )
        if not isinstance(scenario["scenarioId"], str) or not scenario["scenarioId"].strip():
            raise ValidationError(f"scenario {scenario_id} has an empty or non-string id")
        if "*" in scenario["scenarioId"]:
            raise ValidationError(
                f"scenario {scenario_id} uses a wildcard id; scenario ids must be finite"
            )
        if scenario["expectedVerdict"] not in VALID_VERDICTS:
            raise ValidationError(
                f"scenario {scenario_id} declares unknown verdict {scenario['expectedVerdict']}"
            )

    # 2. duplicates, before any mapping check that a duplicate would confuse.
    seen: Dict[str, int] = {}
    for scenario in definitions:
        scenario_id = scenario["scenarioId"]
        seen[scenario_id] = seen.get(scenario_id, 0) + 1
    duplicates = sorted(key for key, count in seen.items() if count > 1)
    if duplicates:
        raise ValidationError(f"duplicate scenario id(s): {', '.join(duplicates)}")

    # 3. every definition classifies under a known capability.
    for scenario in definitions:
        if scenario["capabilityId"] not in known_capabilities:
            raise ValidationError(
                f"scenario {scenario['scenarioId']} references "
                f"unknown capability {scenario['capabilityId']}"
            )

    # 4. wildcards and concrete identities in aliases.
    for scenario in definitions:
        for field in ("logicalFixtureKey", "oracleProjectionKey"):
            value = scenario[field]
            if not isinstance(value, str) or not value:
                raise ValidationError(
                    f"scenario {scenario['scenarioId']}.{field} must be a non-empty alias"
                )
            if "*" in value:
                raise ValidationError(
                    f"scenario {scenario['scenarioId']}.{field} uses a wildcard alias"
                )
            for marker in CONCRETE_IDENTIFIER_MARKERS:
                if marker in value:
                    raise ValidationError(
                        f"scenario {scenario['scenarioId']}.{field} is not a "
                        "repository-safe logical alias; concrete resource identities "
                        "come only from the bound fixture-config secret"
                    )

    # 5. effect cardinalities, and no self-asserted success token.
    prefixes = tuple(frontier.get("requiredEffectKeyPrefixes", ()))
    literals = set(frontier.get("requiredEffectKeyLiterals", ()))
    for scenario in definitions:
        required = scenario["requiredEffects"]
        forbidden = scenario["forbiddenEffects"]
        if not isinstance(required, dict) or not required:
            raise ValidationError(
                f"scenario {scenario['scenarioId']} declares no required effect"
            )
        if not isinstance(forbidden, dict) or not forbidden:
            raise ValidationError(
                f"scenario {scenario['scenarioId']} declares no forbidden effect"
            )
        for key, count in list(required.items()) + list(forbidden.items()):
            if "*" in key:
                raise ValidationError(
                    f"scenario {scenario['scenarioId']} uses a wildcard effect key {key}"
                )
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValidationError(
                    f"scenario {scenario['scenarioId']} effect {key} must be an "
                    "exact non-negative integer cardinality"
                )
        for key, count in forbidden.items():
            if count != 0:
                raise ValidationError(
                    f"scenario {scenario['scenarioId']} forbidden effect {key} must be exactly 0"
                )
        for key in required:
            if key in literals or any(key.startswith(prefix) for prefix in prefixes):
                continue
            raise ValidationError(
                f"scenario {scenario['scenarioId']} required effect {key} is not an "
                "observed store, capture, or adapter projection; a scenario may not "
                "emit its own pass token"
            )

    # 6. one-for-one mapping between capability references and definitions.
    defined_ids = {scenario["scenarioId"] for scenario in definitions}
    referenced: List[str] = []
    for capability in capabilities:
        scenario_ids = capability.get("scenarioIds")
        if not isinstance(scenario_ids, list) or not scenario_ids:
            raise ValidationError(f"capability {capability['id']} declares no scenarioIds")
        for scenario_id in scenario_ids:
            if scenario_id not in defined_ids:
                raise ValidationError(
                    f"capability {capability['id']} references "
                    f"undefined scenario {scenario_id}"
                )
            referenced.append(scenario_id)

    referenced_set = set(referenced)
    unreferenced = sorted(defined_ids - referenced_set)
    if unreferenced:
        raise ValidationError(
            f"unreferenced scenario definition(s) with no owning capability: "
            f"{', '.join(unreferenced)}"
        )
    if len(referenced) != len(referenced_set):
        raise ValidationError("a scenario definition is referenced by more than one capability")

    # 7. the pinned count, last: a count error is the least specific diagnosis.
    if len(definitions) != EXPECTED_CAPABILITY_SCENARIO_COUNT:
        raise ValidationError(
            f"expected exactly {EXPECTED_CAPABILITY_SCENARIO_COUNT} capability scenario "
            f"definitions, found {len(definitions)}"
        )

    # 8. bootstrap and refutation are instrument proofs, never capability stamps.
    bootstrap = manifest.get("bootstrapScenario")
    if not isinstance(bootstrap, dict):
        raise ValidationError("manifest must declare a bootstrapScenario")
    if bootstrap.get("capabilityStamp") is not False:
        raise ValidationError("bootstrapScenario must declare capabilityStamp false")
    if bootstrap.get("scenarioId") in defined_ids:
        raise ValidationError("bootstrapScenario id collides with a capability scenario id")

    refutations = manifest.get("refutationScenarios")
    if not isinstance(refutations, list) or not refutations:
        raise ValidationError("manifest must declare at least one refutation scenario")
    for refutation in refutations:
        if refutation.get("expectedVerdict") != "FAIL":
            raise ValidationError("a refutation scenario must expect FAIL")
        if refutation.get("capabilityStamp") is not False:
            raise ValidationError("a refutation scenario must declare capabilityStamp false")


# --------------------------------------------------------------------------
# identity validation
# --------------------------------------------------------------------------


def validate_identity(identity: Any, label: str, schema: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(identity, dict):
        raise ValidationError(f"{label} identity must be a JSON object")

    required = list(schema.get("required", []))
    allowed = set(schema.get("properties", {}))

    missing = [field for field in required if field not in identity]
    if missing:
        raise ValidationError(
            f"{label} identity is missing required field(s): {', '.join(sorted(missing))}"
        )
    unknown = sorted(set(identity) - allowed)
    if unknown:
        raise ValidationError(
            f"{label} identity carries unknown field(s): {', '.join(unknown)}; "
            "an unrecognised identity field fails closed"
        )
    return identity


def identity_differences(previous: Dict[str, Any], current: Dict[str, Any],
                         fields: Sequence[str]) -> List[str]:
    return sorted(field for field in fields if previous.get(field) != current.get(field))


# --------------------------------------------------------------------------
# ranking
# --------------------------------------------------------------------------


def parse_changed_paths(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    parts: List[str] = []
    for chunk in raw.replace("\n", ",").split(","):
        cleaned = chunk.strip()
        if cleaned:
            parts.append(cleaned)
    return sorted(set(parts))


def compute_invalidations(stamps: Sequence[Dict[str, Any]], identity_changed: bool,
                          changed_paths: Sequence[str]) -> List[str]:
    """Identity change invalidates everything; a path change invalidates only its own."""
    if identity_changed:
        return sorted({stamp["capabilityId"] for stamp in stamps})

    changed = set(changed_paths)
    invalidated = set()
    for stamp in stamps:
        declared = stamp.get("productionPaths") or []
        if any(path in changed for path in declared):
            invalidated.add(stamp["capabilityId"])
    return sorted(invalidated)


def rank(frontier: Dict[str, Any], manifest: Dict[str, Any],
         stamps: Sequence[Dict[str, Any]], invalidated: Sequence[str]) -> Dict[str, Any]:
    capability_order: List[str] = frontier["capabilityOrder"]
    dependencies: Dict[str, List[str]] = frontier["dependencies"]
    invalid = set(invalidated)
    reason_codes: List[str] = []

    # Valid PASS stamps, ignoring anything invalidation just killed.
    passing = {
        stamp["capabilityId"]
        for stamp in stamps
        if stamp.get("verdict") == "PASS" and stamp["capabilityId"] not in invalid
    }

    # A recorded safety failure preempts core completion.
    safety_failures = sorted(
        {
            stamp["capabilityId"]
            for stamp in stamps
            if stamp.get("verdict") == "FAIL"
            and stamp.get("safety") is True
            and stamp["capabilityId"] in capability_order
        },
        key=capability_order.index,
    )

    # One blocker at most: an unmet, independently proved prerequisite.
    blocker: Optional[Dict[str, Any]] = None
    blocked: set = set()
    for capability_id, rule in sorted(frontier.get("blockingPrerequisites", {}).items()):
        if capability_id in passing:
            continue  # already stamped; the prerequisite no longer gates anything
        field = rule["manifestField"]
        if manifest.get(field) is None:
            blocked.add(capability_id)
            if blocker is None:
                blocker = {
                    "capabilityId": capability_id,
                    "verdict": rule["verdictWhenUnmet"],
                    "reason": rule["reason"],
                    "unmetManifestField": field,
                }

    if safety_failures:
        active: Optional[str] = safety_failures[0]
        reason_codes.append("safety-failure")
    else:
        active = None
        for capability_id in capability_order:
            if capability_id in passing:
                continue
            if capability_id in blocked:
                continue
            unmet = [dep for dep in dependencies.get(capability_id, []) if dep not in passing]
            if unmet:
                continue
            active = capability_id
            break
        if active is None:
            reason_codes.append("no-eligible-capability")
        else:
            reason_codes.append("next-unstamped-in-dependency-order")

    if invalid:
        reason_codes.append("stamp-invalidated")
    if blocker is not None:
        reason_codes.append("blocker-present")

    return {
        "activeCapability": active,
        "blocker": blocker,
        "invalidatedStamps": sorted(invalid),
        "reasonCodes": sorted(set(reason_codes)),
        "stampedCapabilities": sorted(passing),
    }


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Select the single active certification capability, deterministically."
    )
    parser.add_argument("--frontier", required=True, help="static ranking policy JSON")
    parser.add_argument("--stamps", required=True, help="private sanitized stamps JSON")
    parser.add_argument("--previous-identity", required=True, help="previous bound identity JSON")
    parser.add_argument("--current-identity", required=True, help="current bound identity JSON")
    parser.add_argument("--changed-paths", default="", help="comma or newline separated paths")
    parser.add_argument("--json", action="store_true", help="emit the decision as JSON")
    parser.add_argument(
        "--output",
        default=None,
        help="optional explicit NON-REPOSITORY path to write the decision to",
    )
    return parser


def run(argv: Optional[Sequence[str]] = None) -> Tuple[int, str, str]:
    args = build_parser().parse_args(argv)

    frontier_path = Path(args.frontier)
    frontier = load_json(frontier_path, "frontier")

    for banned in ("stamps", "activeCapability", "currentIdentity", "revision", "verdicts"):
        if banned in frontier:
            raise ValidationError(
                f"frontier.json records dynamic state ({banned}); it is static policy only"
            )

    manifest_path = resolve_manifest_path(frontier, frontier_path)
    manifest = load_json(manifest_path, "approved manifest")
    validate_manifest(manifest, frontier)

    schema_path = frontier_path.resolve().parent / "identity.schema.json"
    if not schema_path.is_file():
        schema_path = (
            REPO_ROOT / "docs" / "release-safety" / "production-certification"
            / "identity.schema.json"
        )
    schema = load_json(schema_path, "identity schema")

    previous = validate_identity(
        load_json(Path(args.previous_identity), "previous identity"), "previous", schema
    )
    current = validate_identity(
        load_json(Path(args.current_identity), "current identity"), "current", schema
    )

    stamps = load_json(Path(args.stamps), "stamps")
    if not isinstance(stamps, list):
        raise ValidationError("stamps must be a JSON array")
    for stamp in stamps:
        if not isinstance(stamp, dict) or "capabilityId" not in stamp:
            raise ValidationError("every stamp must be an object carrying capabilityId")
        if stamp.get("verdict") not in VALID_VERDICTS:
            raise ValidationError(
                f"stamp for {stamp.get('capabilityId')} declares an unknown verdict"
            )

    changed_paths = parse_changed_paths(args.changed_paths)
    changed_identity_fields = identity_differences(
        previous, current, frontier["identityInvalidationFields"]
    )
    invalidated = compute_invalidations(stamps, bool(changed_identity_fields), changed_paths)

    decision = rank(frontier, manifest, stamps, invalidated)
    decision["changedIdentityFields"] = changed_identity_fields
    decision["changedPaths"] = list(changed_paths)

    payload = json.dumps(decision, indent=2, sort_keys=True) if args.json else ""

    if args.output:
        output_path = Path(args.output).resolve()
        try:
            output_path.relative_to(REPO_ROOT)
        except ValueError:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(decision, indent=2, sort_keys=True), encoding="utf-8"
            )
        else:
            raise ValidationError(
                "--output must target an explicit non-repository path; writing a decision "
                "into the repository would change the source SHA it certifies"
            )

    return 0, payload, ""


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        code, stdout, stderr = run(argv)
    except ValidationError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    if stdout:
        sys.stdout.write(stdout + "\n")
    if stderr:
        sys.stderr.write(stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
