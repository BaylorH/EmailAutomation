"""Closed approved scenario registry, loaded from the canonical in-image bytes.

`scenario_registry.json` is the RUNTIME AUTHORITY. The checked-in planning
manifest is planning input only. The route, runner, tests, and ranker all load the
same bytes through this module, so there is no hand-retyped Python copy that could
drift from what the image actually ships.

The registry owns logical fixture and oracle aliases ONLY. Concrete client,
recipient, Sheet, Drive, thread, and event identities come exclusively from the
bound immutable numeric fixture-config secret at execution time.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

from email_automation.certification.canonical_json import digest_of_bytes, loads_strict

REGISTRY_PATH = Path(__file__).resolve().parent / "scenario_registry.json"

EXPECTED_SCENARIO_COUNT = 93
EXPECTED_CLASS_COUNTS = {"bootstrap": 1, "refutation": 1, "capability": 91}


class ScenarioRegistryError(RuntimeError):
    """The in-image registry is missing, unreadable, or does not hold its contract."""


@functools.lru_cache(maxsize=1)
def registry_bytes() -> bytes:
    """The exact canonical bytes shipped in the image."""
    try:
        return REGISTRY_PATH.read_bytes()
    except OSError as exc:
        raise ScenarioRegistryError(
            f"in-image scenario registry is unreadable at {REGISTRY_PATH}: {exc.strerror}"
        ) from None


@functools.lru_cache(maxsize=1)
def registry_digest() -> str:
    """Lowercase SHA-256 of the canonical registry bytes.

    Digested from the bytes on disk, never from a re-serialization, so the value
    covers exactly what the image ships.
    """
    return digest_of_bytes(registry_bytes())


@functools.lru_cache(maxsize=1)
def _loaded() -> Tuple[Dict[str, Any], Dict[str, Mapping[str, Any]]]:
    document = loads_strict(registry_bytes())
    if not isinstance(document, dict):
        raise ScenarioRegistryError("scenario registry must be a JSON object")

    entries = document.get("scenarios")
    if not isinstance(entries, list):
        raise ScenarioRegistryError("scenario registry must declare a scenarios array")

    if len(entries) != EXPECTED_SCENARIO_COUNT:
        raise ScenarioRegistryError(
            f"scenario registry must hold exactly {EXPECTED_SCENARIO_COUNT} scenarios, "
            f"found {len(entries)}"
        )

    by_id: Dict[str, Mapping[str, Any]] = {}
    counts: Dict[str, int] = {}
    for entry in entries:
        scenario_id = entry.get("scenarioId")
        if not isinstance(scenario_id, str) or not scenario_id:
            raise ScenarioRegistryError("every registry scenario needs a string scenarioId")
        if scenario_id in by_id:
            raise ScenarioRegistryError(f"duplicate scenario id in registry: {scenario_id}")
        scenario_class = entry.get("scenarioClass")
        if scenario_class not in EXPECTED_CLASS_COUNTS:
            raise ScenarioRegistryError(
                f"scenario {scenario_id} declares unknown class {scenario_class}"
            )
        counts[scenario_class] = counts.get(scenario_class, 0) + 1
        by_id[scenario_id] = entry

    if counts != EXPECTED_CLASS_COUNTS:
        raise ScenarioRegistryError(
            f"scenario class split must be {EXPECTED_CLASS_COUNTS}, found {counts}"
        )

    return document, by_id


def all_scenarios() -> List[Mapping[str, Any]]:
    """Every registry scenario, in registry order."""
    document, _ = _loaded()
    return list(document["scenarios"])


def scenario_ids() -> Tuple[str, ...]:
    """Every approved scenario id. Membership here is the only admission test."""
    _, by_id = _loaded()
    return tuple(by_id)


def get(scenario_id: str) -> Mapping[str, Any]:
    """One approved scenario. Raises KeyError for anything not in the registry."""
    _, by_id = _loaded()
    if scenario_id not in by_id:
        raise KeyError(scenario_id)
    return by_id[scenario_id]


def capability_scenarios(capability_id: str) -> List[Mapping[str, Any]]:
    """Every capability-class scenario owned by one capability, in registry order."""
    return [
        scenario
        for scenario in all_scenarios()
        if scenario["scenarioClass"] == "capability"
        and scenario["capabilityId"] == capability_id
    ]
