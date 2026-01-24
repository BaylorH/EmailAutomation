#!/usr/bin/env python3
"""
Results Manager - Save and load E2E test results
================================================

This module handles saving test results in a structured format:

tests/results/
├── run_YYYYMMDD_HHMMSS/
│   ├── manifest.json          # Run metadata + input file info
│   ├── 1_kuhlke_dr.json       # Full result for each property
│   ├── 699_industrial_park_dr.json
│   └── summary.json           # Campaign-level summary

Each result file contains:
- input: Property data from Excel (row, columns, values)
- conversation: Message exchange used for testing
- output: AI response (updates, events, response email)
- sheet_state: Before/after column values
- notifications: Derived notifications
- validation: Expected vs actual comparison
"""

import os
import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict


def get_results_dir() -> Path:
    """Get the results directory path."""
    return Path(__file__).parent / "results"


def create_run_directory() -> Path:
    """Create a timestamped run directory."""
    results_dir = get_results_dir()
    results_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = results_dir / f"run_{timestamp}"
    run_dir.mkdir()

    return run_dir


def get_file_hash(filepath: str) -> str:
    """Get MD5 hash of a file for change detection."""
    if not os.path.exists(filepath):
        return ""
    with open(filepath, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def create_manifest(run_dir: Path, scrub_filepath: str, properties: Dict) -> Dict:
    """Create manifest.json with run metadata."""
    manifest = {
        "created_at": datetime.now().isoformat(),
        "input_file": {
            "path": scrub_filepath,
            "filename": os.path.basename(scrub_filepath),
            "hash": get_file_hash(scrub_filepath),
            "property_count": len(properties)
        },
        "properties": list(properties.keys()),
        "tests_run": 0,
        "tests_passed": 0,
        "tests_failed": 0
    }

    manifest_path = run_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    return manifest


def save_result(run_dir: Path, result, property_data: Dict, conversation: Dict, headers: List[str]) -> str:
    """
    Save a single test result to a JSON file.

    Returns the filename of the saved result.
    """
    # Normalize address to filename
    filename = result.property_address.lower()
    filename = filename.replace("[edge] ", "edge_")
    filename = filename.replace(" ", "_").replace(",", "").replace("/", "_")
    filename = f"{filename}.json"

    # Build complete result document
    result_doc = {
        "property_address": result.property_address,
        "passed": result.passed,
        "timestamp": datetime.now().isoformat(),

        # Input from Excel
        "input": {
            "row_number": property_data.get("row"),
            "city": property_data.get("city"),
            "contact": property_data.get("contact"),
            "email": property_data.get("email"),
            "columns": {},
            "raw_values": property_data.get("data", [])
        },

        # Conversation used for testing
        "conversation": {
            "description": conversation.get("description", ""),
            "messages": conversation.get("messages", []),
            "notes": conversation.get("notes", "")
        },

        # AI output
        "output": {
            "updates": result.ai_updates,
            "events": result.ai_events,
            "response_email": result.ai_response,
            "notes": result.ai_notes
        },

        # Sheet state before/after
        "sheet_state": {
            "columns": headers,
            "before": {},
            "after": {},
            "changes": []
        },

        # Notifications derived
        "notifications": result.notifications,

        # Validation results
        "validation": {
            "expected_updates": conversation.get("expected_updates", []),
            "expected_events": conversation.get("expected_events", []),
            "forbidden_updates": conversation.get("forbidden_updates", []),
            "issues": result.issues,
            "warnings": result.warnings
        }
    }

    # Populate column data
    for i, col in enumerate(headers):
        if col:
            before_val = result.sheet_before[i] if i < len(result.sheet_before) else ""
            after_val = result.sheet_after[i] if i < len(result.sheet_after) else ""

            result_doc["input"]["columns"][col] = before_val
            result_doc["sheet_state"]["before"][col] = before_val
            result_doc["sheet_state"]["after"][col] = after_val

            if before_val != after_val:
                result_doc["sheet_state"]["changes"].append({
                    "column": col,
                    "before": before_val,
                    "after": after_val
                })

    # Save to file
    filepath = run_dir / filename
    with open(filepath, "w") as f:
        json.dump(result_doc, f, indent=2)

    return filename


def save_summary(run_dir: Path, results: List, manifest: Dict) -> Dict:
    """
    Save summary.json with campaign-level results.
    """
    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]

    # Collect all unique events and update columns
    all_events = set()
    all_update_columns = set()
    all_notification_kinds = set()

    for r in results:
        for e in r.ai_events:
            all_events.add(e.get("type", "unknown"))
        for u in r.ai_updates:
            all_update_columns.add(u.get("column", ""))
        for n in r.notifications:
            kind = n.get("kind", "")
            reason = n.get("reason", "")
            all_notification_kinds.add(f"{kind}:{reason}" if reason else kind)

    summary = {
        "created_at": datetime.now().isoformat(),
        "input_file": manifest.get("input_file", {}),

        "totals": {
            "tests_run": len(results),
            "tests_passed": len(passed),
            "tests_failed": len(failed),
            "pass_rate": f"{len(passed)/len(results)*100:.1f}%" if results else "0%"
        },

        "coverage": {
            "events_triggered": sorted(all_events),
            "columns_updated": sorted(all_update_columns),
            "notification_types": sorted(all_notification_kinds)
        },

        "results": {
            "passed": [r.property_address for r in passed],
            "failed": [
                {
                    "property": r.property_address,
                    "issues": r.issues
                }
                for r in failed
            ]
        }
    }

    # Update manifest with final counts
    manifest["tests_run"] = len(results)
    manifest["tests_passed"] = len(passed)
    manifest["tests_failed"] = len(failed)

    manifest_path = run_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    # Save summary
    summary_path = run_dir / "summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    return summary


def list_runs() -> List[Dict]:
    """List all previous test runs with their summaries."""
    results_dir = get_results_dir()
    if not results_dir.exists():
        return []

    runs = []
    for run_dir in sorted(results_dir.iterdir(), reverse=True):
        if not run_dir.is_dir() or not run_dir.name.startswith("run_"):
            continue

        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            with open(manifest_path) as f:
                manifest = json.load(f)
                runs.append({
                    "run_dir": str(run_dir),
                    "run_name": run_dir.name,
                    **manifest
                })

    return runs


def load_run(run_name: str) -> Dict:
    """Load all results from a specific run."""
    run_dir = get_results_dir() / run_name
    if not run_dir.exists():
        return {}

    results = {
        "manifest": None,
        "summary": None,
        "results": []
    }

    # Load manifest
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        with open(manifest_path) as f:
            results["manifest"] = json.load(f)

    # Load summary
    summary_path = run_dir / "summary.json"
    if summary_path.exists():
        with open(summary_path) as f:
            results["summary"] = json.load(f)

    # Load individual results
    for result_file in sorted(run_dir.glob("*.json")):
        if result_file.name in ["manifest.json", "summary.json"]:
            continue
        with open(result_file) as f:
            results["results"].append(json.load(f))

    return results


def compare_runs(run1_name: str, run2_name: str) -> Dict:
    """Compare results between two runs."""
    run1 = load_run(run1_name)
    run2 = load_run(run2_name)

    if not run1 or not run2:
        return {"error": "One or both runs not found"}

    # Build property -> result mapping
    results1 = {r["property_address"]: r for r in run1.get("results", [])}
    results2 = {r["property_address"]: r for r in run2.get("results", [])}

    comparison = {
        "run1": run1_name,
        "run2": run2_name,
        "input_file_changed": run1.get("manifest", {}).get("input_file", {}).get("hash") !=
                              run2.get("manifest", {}).get("input_file", {}).get("hash"),
        "changes": []
    }

    all_properties = set(results1.keys()) | set(results2.keys())

    for prop in sorted(all_properties):
        r1 = results1.get(prop)
        r2 = results2.get(prop)

        if not r1:
            comparison["changes"].append({"property": prop, "change": "added_in_run2"})
        elif not r2:
            comparison["changes"].append({"property": prop, "change": "removed_in_run2"})
        elif r1.get("passed") != r2.get("passed"):
            comparison["changes"].append({
                "property": prop,
                "change": "status_changed",
                "run1_passed": r1.get("passed"),
                "run2_passed": r2.get("passed")
            })
        elif r1.get("output") != r2.get("output"):
            comparison["changes"].append({
                "property": prop,
                "change": "output_changed",
                "run1_updates": r1.get("output", {}).get("updates", []),
                "run2_updates": r2.get("output", {}).get("updates", [])
            })

    return comparison
